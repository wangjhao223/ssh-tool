"""Unit tests for connection/status/schema behavior; stdlib unittest only."""
import json
import os
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MCP_DIR = Path(__file__).resolve().parents[1] / 'mcp'
sys.path.insert(0, str(MCP_DIR))

import job_tools  # noqa: E402
import local_connection  # noqa: E402
import remote_ws_mcp  # noqa: E402
from remote_ws import commands, transport  # noqa: E402
from remote_ws.validation import FileToolError, LoginError, ToolError  # noqa: E402

EXPECTED_TOOLS = [
    'get_guide', 'connection_query', 'resource_query', 'exec', 'job_query',
    'job_control', 'file_query', 'workspace_write', 'download_query', 'download_control',
]


def schema_of(name):
    for tool_name, _, _, schema in remote_ws_mcp.TOOLS:
        if tool_name == name:
            return schema
    raise AssertionError(f'missing tool schema: {name}')


def schema_accepts(schema, value):
    """Small JSON-Schema subset checker for the constraints used by these tools."""
    types = schema.get('type')
    if types is not None:
        types = types if isinstance(types, list) else [types]
        expected = {'string': str, 'integer': int, 'boolean': bool,
                    'array': list, 'object': dict}
        if not any(type(value) is expected.get(item) for item in types):
            return False
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0):
            return False
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            return False
        if 'pattern' in schema and re.search(schema['pattern'], value) is None:
            return False
    elif type(value) is int:
        if 'minimum' in schema and value < schema['minimum']:
            return False
        if 'maximum' in schema and value > schema['maximum']:
            return False
    if 'enum' in schema and value not in schema['enum']:
        return False
    return True


class ToolRegistryTests(unittest.TestCase):
    def test_version_and_exact_tool_set(self):
        names = [item[0] for item in remote_ws_mcp.TOOLS]
        self.assertEqual(remote_ws_mcp.SERVER_VERSION, '0.24.0')
        self.assertEqual(len(names), 10)
        self.assertEqual(sorted(names), sorted(EXPECTED_TOOLS))
        self.assertEqual(len(set(names)), 10)
        result = remote_ws_mcp.handle({'method': 'initialize',
                                       'params': {'protocolVersion': '2025-06-18'}})
        self.assertEqual(result['serverInfo'], {'name': 'remote-ws', 'version': '0.24.0'})

    def test_compact_schema_keeps_constraints_and_descriptions(self):
        schema = {
            'type': 'object',
            'title': 'drop-me',
            'properties': {
                'text': {'type': 'string', 'pattern': '^[a-z]+$', 'minLength': 1,
                         'maxLength': 64, 'description': '短说明'},
                'count': {'type': 'integer', 'minimum': 0, 'maximum': 10, 'default': 3},
                'items': {'type': 'array', 'minItems': 1, 'maxItems': 4,
                          'items': {'type': 'string', 'enum': ['a', 'b']}},
                'choice': {'oneOf': [{'type': 'string'}, {'type': 'integer'}]},
            },
            'required': ['text'],
            'additionalProperties': False,
        }
        value = remote_ws_mcp.compact_schema(schema)
        self.assertNotIn('title', value)
        self.assertNotIn('title', json.dumps(value, ensure_ascii=False))
        text = value['properties']['text']
        self.assertEqual(text['description'], '短说明')
        self.assertEqual(text['pattern'], '^[a-z]+$')
        self.assertEqual((text['minLength'], text['maxLength']), (1, 64))
        count = value['properties']['count']
        self.assertEqual((count['minimum'], count['maximum'], count['default']), (0, 10, 3))
        items = value['properties']['items']
        self.assertEqual((items['minItems'], items['maxItems']), (1, 4))
        self.assertEqual(items['items']['enum'], ['a', 'b'])
        self.assertEqual(value['properties']['choice']['oneOf'],
                         [{'type': 'string'}, {'type': 'integer'}])
        self.assertEqual(value['required'], ['text'])
        self.assertFalse(value['additionalProperties'])

    def test_tool_schemas_have_branch_hints_and_bounds(self):
        connection = schema_of('connection_query')
        timeout = connection['properties']['timeout_sec']
        self.assertEqual(timeout['minimum'], 0)
        self.assertNotIn('maximum', timeout)
        self.assertIn('10', timeout['description'])
        self.assertIn('diagnose', timeout['description'])
        self.assertIn('status', timeout['description'])
        self.assertNotIn('default', connection['properties']['all'])
        self.assertIn('仅 status', connection['properties']['all']['description'])

        job_query = schema_of('job_query')
        self.assertEqual(job_query['properties']['limit']['minimum'], 0)
        self.assertNotIn('maximum', job_query['properties']['limit'])
        self.assertEqual(job_query['properties']['lines']['minimum'], 0)
        self.assertNotIn('maximum', job_query['properties']['lines'])
        self.assertEqual(job_query['properties']['max_chars']['minimum'], 0)
        self.assertNotIn('maximum', job_query['properties']['max_chars'])
        project_schema = job_query['properties']['project']
        self.assertTrue(schema_accepts(project_schema, ''))
        self.assertTrue(schema_accepts(project_schema, 'proj-1'))
        self.assertFalse(schema_accepts(project_schema, 'bad project'))
        control = schema_of('job_control')
        self.assertFalse(schema_accepts(control['properties']['project'], ''))
        self.assertNotIn('maximum', job_query['properties']['context_lines'])
        self.assertEqual(job_query['properties']['context_lines']['minimum'], 0)
        self.assertTrue(schema_accepts(job_query['properties']['context_lines'], 11))
        self.assertEqual(control['required'], ['action'])
        self.assertEqual(control['properties']['project']['pattern'],
                         r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
        self.assertNotIn('pattern', control['properties']['job'])
        self.assertNotIn('minLength', control['properties']['command'])
        self.assertIn('完整32位', control['properties']['job']['description'])
        self.assertIn('<project>', control['properties']['cwd']['description'])

        resource = schema_of('resource_query')
        resource_name = resource['properties']['name']
        self.assertTrue(schema_accepts(resource_name, ''))
        self.assertTrue(schema_accepts(resource_name, 'env-1'))
        self.assertFalse(schema_accepts(resource_name, 'bad env'))
        self.assertNotIn('default', resource['properties']['details'])

        self.assertNotIn('maximum', schema_of('exec')['properties']['max_output_chars'])

        guide = schema_of('get_guide')
        self.assertEqual(guide['properties']['max_lines']['default'], 80)
        self.assertIn('description', guide['properties']['server'])

        exec_cwd = schema_of('exec')['properties']['cwd']['description']
        self.assertIn('CODE_ROOT', exec_cwd)
        self.assertNotIn('<project>', exec_cwd)

    def test_gpu_schema_accepts_string_or_nonnegative_integer(self):
        exec_gpu = schema_of('exec')['properties']['gpus']
        self.assertEqual(exec_gpu['type'], ['string', 'integer'])
        self.assertEqual(exec_gpu['minimum'], 0)
        for value in ('', '0', '0,1', 0, 3):
            self.assertTrue(schema_accepts(exec_gpu, value), value)
        for value in ('a', '-1', '0,a', -1):
            self.assertFalse(schema_accepts(exec_gpu, value), value)

        job_gpu = schema_of('job_control')['properties']['gpus']
        self.assertEqual(job_gpu['type'], ['string', 'integer'])
        self.assertEqual(job_gpu['minimum'], 0)
        for value in ('', '0', '0,1', 0, 3):
            self.assertTrue(schema_accepts(job_gpu, value), value)
        for value in ('a', '-1', '0,a', -1):
            self.assertFalse(schema_accepts(job_gpu, value), value)

        self.assertEqual(commands.normalize_gpus(0), '0')
        self.assertEqual(commands.normalize_gpus(2), '2')
        self.assertEqual(commands.normalize_gpus(''), '')
        self.assertEqual(commands.normalize_gpus('0,1'), '0,1')
        with self.assertRaises(ToolError):
            commands.normalize_gpus(-1)

    def test_exec_gpu_runtime_empty_and_integer(self):
        captured = {}

        def fake_run(name, script, timeout, max_output_chars=None):
            captured['script'] = script
            return 0, '', '', 0

        context = {'SERVER': 'a', 'CODE_ROOT': '/code', 'CONDA_ROOT': '/conda',
                   'CONDA_INIT': ''}
        with mock.patch.object(commands.config, 'resolve_context', return_value=context), \
                mock.patch.object(commands, 'find_write_violations', return_value=[]), \
                mock.patch.object(commands, 'default_server', return_value='a'):
            commands.t_exec({'command': 'echo hi', 'gpus': ''}, run=fake_run)
            self.assertNotIn('CUDA_VISIBLE_DEVICES', captured['script'])
            commands.t_exec({'command': 'echo hi', 'gpus': 1}, run=fake_run)
            self.assertIn('CUDA_VISIBLE_DEVICES=1', captured['script'])

    def test_exec_write_guard_is_called_once_with_or_without_confirmation(self):
        context = {'SERVER': 'a', 'CODE_ROOT': '/code', 'DATA_ROOT': '/data',
                   'CONDA_ROOT': '/conda', 'CONDA_INIT': ''}
        for violations, confirmed, should_run in (([], False, True),
                (['outside'], False, False), (['outside'], True, True)):
            with self.subTest(violations=violations, confirmed=confirmed), \
                    mock.patch.object(commands.config, 'resolve_context', return_value=context), \
                    mock.patch.object(commands, 'find_write_violations', return_value=violations) as guard, \
                    mock.patch.object(commands, 'default_server', return_value='a'):
                run = mock.Mock(return_value=(0, '', '', 0))
                args = {'command': 'true', 'confirmed': confirmed}
                if should_run:
                    commands.t_exec(args, run=run)
                    run.assert_called_once()
                else:
                    with self.assertRaises(ToolError):
                        commands.t_exec(args, run=run)
                    run.assert_not_called()
                guard.assert_called_once_with('true', '/code', None, context)

    def test_workspace_details_show_running_version_without_project_registration(self):
        with mock.patch.object(remote_ws_mcp.guide_tools, '_context', return_value={
                'SERVER': 'a', 'CONFIGURED': True}), \
                mock.patch.object(remote_ws_mcp.guide_tools.config, 'configured_servers', return_value=['a']):
            guide = remote_ws_mcp.TOOL_MAP['get_guide']
            summary = json.loads(guide({'topic': 'workspace'}))
            details = json.loads(guide({'topic': 'workspace', 'verbose': True}))
        self.assertNotIn('tool_version', summary)
        self.assertEqual(details['tool_version'], remote_ws_mcp.SERVER_VERSION)
        self.assertEqual(details['tool_root'], str(MCP_DIR.parent))
        self.assertIn('用户级', details['project_config'])

    def test_exec_timeout_schema_and_runtime_clamp(self):
        timeout = schema_of('exec')['properties']['timeout_sec']
        self.assertEqual(timeout['minimum'], 1)
        self.assertEqual(timeout['default'], 120)
        self.assertNotIn('maximum', timeout)
        self.assertTrue(schema_accepts(timeout, 1801))
        self.assertTrue(schema_accepts(timeout, 86400))
        self.assertFalse(schema_accepts(timeout, 0))
        self.assertIn('1800', timeout['description'])

        captured = {}

        def fake_run(name, script, value, max_output_chars=None):
            captured['timeout'] = value
            return 0, '', '', 0

        context = {'SERVER': 'a', 'CODE_ROOT': '/code', 'CONDA_ROOT': '/conda',
                   'CONDA_INIT': ''}
        with mock.patch.object(commands.config, 'resolve_context', return_value=context), \
                mock.patch.object(commands, 'find_write_violations', return_value=[]), \
                mock.patch.object(commands, 'default_server', return_value='a'):
            commands.t_exec({'command': 'echo hi', 'timeout_sec': 5000}, run=fake_run)
        self.assertEqual(captured['timeout'], 1800)

    def test_diagnose_timeout_schema_does_not_block_large_values(self):
        timeout = schema_of('connection_query')['properties']['timeout_sec']
        self.assertTrue(schema_accepts(timeout, 30))
        self.assertTrue(schema_accepts(timeout, 11))
        self.assertFalse(schema_accepts(timeout, -1))

    def test_flat_job_control_schema_allows_neutral_fields(self):
        properties = schema_of('job_control')['properties']
        for key in ('job', 'command', 'name', 'cwd', 'conda_env', 'gpus'):
            with self.subTest(field=key):
                self.assertTrue(schema_accepts(properties[key], ''))
        self.assertTrue(schema_accepts(properties['force'], False))
        self.assertTrue(schema_accepts(properties['confirmed'], False))
        self.assertTrue(schema_accepts(properties['gpus'], 0))
        self.assertFalse(schema_accepts(properties['gpus'], -1))

    def test_resource_schema_and_runtime_name_edges(self):
        name = schema_of('resource_query')['properties']['name']
        self.assertTrue(schema_accepts(name, ''))
        self.assertTrue(schema_accepts(name, 'env-1'))
        self.assertFalse(schema_accepts(name, 'bad env'))
        self.assertFalse(schema_accepts(name, None))

        with mock.patch.object(commands, 't_gpus', return_value='gpu-ok') as gpus:
            text = commands.t_resource_query({'resource': 'gpus', 'name': ''}, run=None)
        self.assertIn('gpu-ok', text)
        self.assertIn('parameter_note', text)
        self.assertNotIn('name', gpus.call_args.args[0])
        with self.assertRaises(ToolError):
            commands.t_resource_query({'resource': 'gpus', 'name': 'env-1'}, run=None)
        with mock.patch.object(remote_ws_mcp.commands, 't_conda_envs', return_value='conda-ok') as conda:
            self.assertEqual(remote_ws_mcp.t_resource_query(
                {'resource': 'conda', 'name': ''}), 'conda-ok')
            self.assertNotIn('name', conda.call_args.args[0])
        with self.assertRaises(ToolError):
            commands.t_conda_envs({'name': ''}, run=None)

    def test_job_query_zero_minimum_schema_and_runtime_edges(self):
        job_query = schema_of('job_query')
        for field in ('limit', 'lines', 'max_chars'):
            with self.subTest(field=field):
                self.assertTrue(schema_accepts(job_query['properties'][field], 0))
                self.assertTrue(schema_accepts(job_query['properties'][field], 999999))
                self.assertFalse(schema_accepts(job_query['properties'][field], -1))

        with mock.patch.object(job_tools, 't_jobs', return_value='list-ok') as jobs:
            text = job_tools.t_job_query({'action': 'list', 'lines': 0, 'max_chars': 0})
        self.assertIn('list-ok', text)
        self.assertIn('parameter_note', text)
        payload = jobs.call_args.args[0]
        self.assertNotIn('lines', payload)
        self.assertNotIn('max_chars', payload)

        with mock.patch.object(job_tools, 't_tail_log', return_value='{}') as tail:
            text = job_tools.t_job_query(
                {'action': 'tail', 'job': 'job1', 'limit': 0, 'project': ''})
        self.assertIn('limit=0', json.loads(text)['parameter_note'])
        self.assertNotIn('limit', tail.call_args.args[0])
        self.assertNotIn('project', tail.call_args.args[0])

        with mock.patch.object(job_tools, 'config') as config, \
                mock.patch.object(job_tools, 'find_job', return_value={
                    'log': '/logs/job1', 'status': 'done', 'job_id': 'a' * 32}), \
                mock.patch.object(job_tools, 'request', return_value={}) as request:
            config.resolve_context.return_value = {'SERVER': 'srv-a', 'PROJECT': 'proj'}
            result = json.loads(job_tools.t_tail_log(
                {'job': 'job1', 'context_lines': 11}))
        self.assertIn('10', result['parameter_note'])
        self.assertEqual(request.call_args.kwargs['context_lines'], 10)

        with self.assertRaises(ToolError):
            job_tools.t_job_query({'action': 'list', 'lines': 1})
        with self.assertRaises(ToolError):
            job_tools.t_job_query({'action': 'tail', 'job': 'job1', 'limit': 1})

    def test_job_control_runtime_matches_neutral_schema_edges(self):
        with mock.patch.object(job_tools, 't_run_background', return_value='start-ok') as start:
            result = job_tools.t_job_control(
                {'action': 'start', 'command': 'echo hi', 'name': 'job1',
                 'project': 'proj', 'job': '', 'force': False, 'gpus': 1},
                run=None)
        self.assertEqual(result, 'start-ok')
        self.assertEqual(start.call_args.args[0]['gpus'], '1')

        neutral_stop = {'action': 'stop', 'job': 'a' * 32, 'command': '', 'name': '',
                        'cwd': '', 'conda_env': '', 'gpus': '', 'confirmed': False}
        with mock.patch.object(job_tools, 't_stop_job', return_value='stop-ok'):
            self.assertEqual(job_tools.t_job_control(neutral_stop, run=None), 'stop-ok')

        with self.assertRaises(ToolError):
            job_tools.t_job_control({'action': 'start', 'command': 'echo hi',
                                     'name': 'job1'}, run=None)
        with self.assertRaises(ToolError):
            job_tools.t_job_control({'action': 'stop'}, run=None)
        with self.assertRaises(ToolError):
            job_tools.t_job_control({'action': 'stop', 'job': 'abc'}, run=None)
        with mock.patch.object(job_tools, 't_run_background') as start:
            with self.assertRaises(ToolError):
                job_tools.t_job_control(
                    {'action': 'start', 'command': 'echo hi', 'name': 'job1',
                     'project': 'proj', 'job': 'a' * 32}, run=None)
            start.assert_not_called()
        with mock.patch.object(job_tools, 't_stop_job') as stop:
            with self.assertRaises(ToolError):
                job_tools.t_job_control(dict(neutral_stop, gpus=0), run=None)
            stop.assert_not_called()

    def test_every_schema_is_closed_object(self):
        for name, _, _, schema in remote_ws_mcp.TOOLS:
            with self.subTest(name=name):
                self.assertEqual(schema['type'], 'object')
                self.assertFalse(schema['additionalProperties'])
                self.assertIsInstance(schema.get('required', []), list)
                for item in schema['properties'].values():
                    self.assertIn('type', item)


class ConnectionStatusTests(unittest.TestCase):
    def status_env(self, context, **overrides):
        values = dict(
            conf=mock.patch.object(remote_ws_mcp, 'conf',
                                   return_value={'DEFAULT_SERVER': 'srv-a', 'CODE_ROOT': '/wrong'}),
            resolve=mock.patch.object(remote_ws_mcp, 'resolve_server', return_value=('a', 'srv-a')),
            servers=mock.patch.object(remote_ws_mcp, 'servers', return_value=['a']),
            connected=mock.patch.object(remote_ws_mcp, 'connected', return_value=False),
            busy=mock.patch.object(remote_ws_mcp.local_connection, 'login_busy', return_value=False),
            params=mock.patch.object(remote_ws_mcp, 'ssh_params',
                                     return_value={'user': 'u', 'hostname': 'h', 'port': '22'}),
            context=mock.patch.object(remote_ws_mcp.config, 'resolve_context', return_value=context),
            remote=mock.patch.object(remote_ws_mcp, 'ssh_run', side_effect=AssertionError('remote call')),
        )
        values.update(overrides)
        for item in values.values():
            item.start()
            self.addCleanup(item.stop)
        return values

    def test_verbose_uses_selected_server_configured_roots(self):
        self.status_env({'SERVER': 'a', 'CONFIGURED': True, 'CODE_ROOT': '/code/a',
                         'DATA_ROOT': '/data/a', 'CONDA_ROOT': '/conda/a'})
        text = remote_ws_mcp.t_status({'verbose': True})
        self.assertIn('a: needs_login', text)
        self.assertIn('configured: true', text)
        self.assertIn('CODE_ROOT=/code/a', text)
        self.assertIn('DATA_ROOT=/data/a', text)
        self.assertIn('CONDA_ROOT=/conda/a', text)
        self.assertNotIn('/wrong', text)

    def test_verbose_marks_unconfigured_without_global_roots(self):
        self.status_env({'SERVER': 'a', 'CONFIGURED': False, 'CODE_ROOT': None,
                         'DATA_ROOT': None, 'CONDA_ROOT': None})
        text = remote_ws_mcp.t_status({'verbose': True})
        self.assertIn('configured: false', text)
        self.assertIn('未配置', text)
        self.assertNotIn('/home/wjh', text)
        self.assertNotIn('/wrong', text)

    def test_status_rejects_all_with_server(self):
        self.status_env({'SERVER': 'a', 'CONFIGURED': True})
        with self.assertRaises(ToolError) as ctx:
            remote_ws_mcp.t_status({'all': True, 'server': 'a'})
        self.assertIn('all=true', str(ctx.exception))
        self.assertIn('server', str(ctx.exception))

    def test_status_verbose_shows_each_server_in_all_mode(self):
        self.status_env({'SERVER': 'a', 'CONFIGURED': True, 'CODE_ROOT': '/code/a',
                         'DATA_ROOT': '/data/a', 'CONDA_ROOT': '/conda/a'},
                        servers=mock.patch.object(remote_ws_mcp, 'servers',
                                                  return_value=['a', 'b']))
        text = remote_ws_mcp.t_status({'all': True, 'verbose': True})
        self.assertIn('a: needs_login', text)
        self.assertIn('b: needs_login', text)
        self.assertEqual(text.count('configured: true'), 2)


class NeutralBranchFieldTests(unittest.TestCase):
    def test_status_accepts_neutral_timeout_zero_with_note(self):
        with mock.patch.object(remote_ws_mcp, 't_status', return_value='status-ok') as handler:
            text = remote_ws_mcp.t_connection_query({'action': 'status', 'timeout_sec': 0})
        self.assertTrue(text.startswith('status-ok\nnote: '))
        self.assertIn('timeout_sec=0', text)
        handler.assert_called_once()

    def test_status_rejects_nonzero_timeout(self):
        with mock.patch.object(remote_ws_mcp, 't_status') as handler:
            with self.assertRaises(ToolError) as ctx:
                remote_ws_mcp.t_connection_query({'action': 'status', 'timeout_sec': 3})
        self.assertIn('diagnose', str(ctx.exception))
        handler.assert_not_called()

    def test_diagnose_accepts_neutral_all_and_verbose_false(self):
        with mock.patch.object(remote_ws_mcp, 't_diagnose', return_value='diag-ok'):
            text = remote_ws_mcp.t_connection_query(
                {'action': 'diagnose', 'all': False, 'verbose': False})
        self.assertTrue(text.startswith('diag-ok\nnote: '))
        self.assertIn('all=false', text)
        self.assertIn('verbose=false', text)

    def test_diagnose_rejects_truthy_all_or_verbose(self):
        for field in ('all', 'verbose'):
            with self.subTest(field=field):
                with self.assertRaises(ToolError) as ctx:
                    remote_ws_mcp.t_connection_query(
                        {'action': 'diagnose', field: True})
                self.assertIn('status', str(ctx.exception))

    def test_wrong_types_and_unknown_fields_stay_strict(self):
        with self.assertRaises(ToolError):
            remote_ws_mcp.t_connection_query({'action': 'status', 'all': 'true'})
        with self.assertRaises(ToolError):
            remote_ws_mcp.t_connection_query({'action': 'status', 'unknown': 1})

    def test_diagnose_truncates_timeout_and_notes_it(self):
        report = {'state': 'needs_login', 'server': 'a', 'endpoint': 'u@h:22', 'summary': 'ok'}
        with mock.patch.object(remote_ws_mcp, 'resolve_server', return_value=('a', 'srv-a')), \
                mock.patch.object(remote_ws_mcp.local_connection, 'diagnose',
                                  return_value=report) as diagnose:
            text = remote_ws_mcp.t_diagnose({'timeout_sec': 30})
        self.assertIn('note: ', text)
        self.assertIn('10 秒', text)
        diagnose.assert_called_once_with('a', timeout=10)

    def test_diagnose_rejects_timeout_below_one(self):
        with self.assertRaises(ToolError):
            remote_ws_mcp.t_diagnose({'timeout_sec': 0})


class SshClassificationTests(unittest.TestCase):
    def test_new_error_patterns_are_classified(self):
        cases = [
            ('Connection to srv-a closed by remote host.', 'connection_lost'),
            ('Connection to 10.0.0.1 closed.', 'connection_lost'),
            ('Timeout, server 10.0.0.1 not responding.', 'network_timeout'),
            ('ssh_dispatch_run_fatal: Connection to 10.0.0.1: message authentication code incorrect',
             'connection_lost'),
            ('Corrupted MAC on input.', 'connection_lost'),
            ('Permission denied (publickey).', 'authentication_failed'),
            ('Host key verification failed.', 'host_key_error'),
        ]
        for stderr, code in cases:
            with self.subTest(stderr=stderr):
                classified = local_connection.classify_ssh_error(stderr)
                self.assertIsNotNone(classified)
                self.assertEqual(classified[0], code)
                self.assertTrue(classified[1])

    def test_unknown_error_is_not_guessed(self):
        self.assertIsNone(local_connection.classify_ssh_error('something entirely different'))


class ProxyPreflightTests(unittest.TestCase):
    def diagnose_with(self, params):
        with mock.patch.object(local_connection, 'resolve_server', return_value='a'), \
                mock.patch.object(local_connection, 'ssh_params', return_value=params), \
                mock.patch.object(local_connection, 'is_connected', return_value=False), \
                mock.patch.object(local_connection, 'login_busy', return_value=False), \
                mock.patch.object(local_connection.socket, 'create_connection',
                                  side_effect=AssertionError('direct probe must be skipped')):
            return local_connection.diagnose('a')

    def test_proxyjump_skips_direct_socket_probe(self):
        report = self.diagnose_with({'user': 'u', 'hostname': 'internal', 'port': '22',
                                     'proxyjump': 'bastion', 'proxycommand': 'none'})
        self.assertEqual(report['state'], 'needs_login')
        self.assertIn('ProxyJump', report['summary'])

    def test_proxycommand_skips_direct_socket_probe(self):
        report = self.diagnose_with({'user': 'u', 'hostname': 'internal', 'port': '22',
                                     'proxyjump': 'none',
                                     'proxycommand': 'ssh -W %h:%p bastion'})
        self.assertEqual(report['state'], 'needs_login')
        self.assertIn('ProxyJump/ProxyCommand', report['summary'])

    def test_direct_host_still_probes_socket(self):
        class DummyConnection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.object(local_connection, 'resolve_server', return_value='a'), \
                mock.patch.object(local_connection, 'ssh_params',
                                  return_value={'user': 'u', 'hostname': 'h', 'port': '22',
                                                'proxyjump': 'none', 'proxycommand': 'none'}), \
                mock.patch.object(local_connection, 'is_connected', return_value=False), \
                mock.patch.object(local_connection, 'login_busy', return_value=False), \
                mock.patch.object(local_connection.socket, 'create_connection',
                                  return_value=DummyConnection()) as connect:
            report = local_connection.diagnose('a')
        self.assertEqual(report['state'], 'needs_login')
        connect.assert_called_once()


class RemoteJsonTests(unittest.TestCase):
    def call(self, captured=None, error=None, operation='read'):
        patches = [
            mock.patch.object(transport, 'ensure_login'),
            mock.patch.object(transport, 'ssh_command', return_value=['ssh']),
        ]
        if error is not None:
            patches.append(mock.patch.object(transport, '_capture_json', side_effect=error))
        else:
            patches.append(mock.patch.object(transport, '_capture_json', return_value=captured))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return transport.remote_json('a', {'operation': operation, 'roots': ['/tmp']},
                                     source='source')

    def test_authentication_failure_includes_absolute_connect_command(self):
        with self.assertRaises(FileToolError) as ctx:
            self.call((255, b'', b'Permission denied (publickey).'))
        message = str(ctx.exception)
        self.assertIn('[authentication_failed]', message)
        self.assertIn(transport.connect_cmd('a'), message)
        self.assertIn('/scripts/connect.sh', message)

    def test_unclassified_255_points_to_diagnose(self):
        with self.assertRaises(FileToolError) as ctx:
            self.call((255, b'', b'strange ssh failure'))
        message = str(ctx.exception)
        self.assertIn('[ssh_error]', message)
        self.assertIn('diagnose', message)
        self.assertIn('server="a"', message)

    def test_oversized_response_is_rejected_before_parsing(self):
        with self.assertRaises(FileToolError) as ctx:
            self.call(error=transport.ResponseTooLargeError(transport.MAX_JSON_BYTES))
        message = str(ctx.exception)
        self.assertIn('MiB', message)
        self.assertIn('未解析', message)
        self.assertIn('缩小范围', message)

    def test_edit_overflow_reports_uncertain_state(self):
        with self.assertRaises(FileToolError) as ctx:
            self.call(error=transport.ResponseTooLargeError(transport.MAX_JSON_BYTES),
                      operation='edit')
        message = str(ctx.exception)
        self.assertIn('编辑状态不确定', message)
        self.assertIn('不得自动重写', message)

    def test_timeout_keeps_operation_specific_uncertainty(self):
        with self.assertRaises(FileToolError) as ctx:
            self.call(error=subprocess.TimeoutExpired(['ssh'], 1), operation='stop')
        message = str(ctx.exception)
        self.assertIn('停止执行状态不确定', message)
        self.assertIn('job_query(action=list)', message)

    def test_valid_json_and_error_payloads(self):
        self.assertEqual(self.call((0, b'{"ok":true,"value":1}', b'')), {'value': 1})
        with self.assertRaises(FileToolError) as ctx:
            self.call((0, b'{"ok":false,"error":"boom"}', b''))
        self.assertEqual(str(ctx.exception), 'boom')

    def test_uncertain_write_responses_never_recommend_blind_retry(self):
        for op in ('edit', 'stop'):
            for response in ((255, b'', b'connection reset'), (0, b'[]', b''),
                             (0, b'{}', b''), (1, b'{"ok":true}', b''),
                             (1, b'{"ok":false,"uncertain":true}', b'')):
                with self.subTest(op=op, response=response), self.assertRaises(FileToolError) as ctx:
                    self.call(response, operation=op)
                self.assertEqual((ctx.exception.code, ctx.exception.status), ('OUTCOME_UNKNOWN', '结果未知'))
                self.assertIn('不得自动', str(ctx.exception))

    def test_wire_errors_keep_only_actionable_fields(self):
        def invoke(name, args):
            res = remote_ws_mcp.handle({'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
            self.assertTrue(res['isError'])
            return res['content'][0]['text']

        text = invoke('exec', {'unknown': 'do not echo this value'})
        self.assertTrue(text.startswith('BAD_ARGS · 未执行'))
        self.assertIn('command', text)
        self.assertIn('unknown', text)
        self.assertNotIn('do not echo', text)
        self.assertNotIn('允许字段及类型', text)
        text = invoke('file_query', {'action': 'list', 'path': 'demo', 'start_line': 1})
        self.assertIn('删除 start_line', text)
        with mock.patch.dict(remote_ws_mcp.TOOL_MAP, exec=lambda args: ('exit_code: 1\nPermission denied', True)):
            self.assertIn('REMOTE_NONZERO · 远端已返回非零', invoke('exec', {}))
        with mock.patch.object(local_connection, 'is_connected', return_value=False), \
                mock.patch.dict(os.environ, REMOTE_WS_LOGIN_GUI='0'), \
                mock.patch.dict(remote_ws_mcp.TOOL_MAP, exec=lambda args: transport.ensure_login('a')):
            text = invoke('exec', {})
            self.assertIn('LOGIN_REQUIRED · 未执行', text)
            self.assertIn(transport.connect_cmd('a'), text)

    def test_non_object_payload_is_rejected(self):
        with self.assertRaises(FileToolError) as ctx:
            self.call((0, b'[]', b''))
        self.assertIn('JSON 对象', str(ctx.exception))

    def test_parse_failure_with_auth_stderr_still_includes_connect_command(self):
        with self.assertRaises(FileToolError) as ctx:
            self.call((0, b'not-json', b'Permission denied (publickey).'))
        message = str(ctx.exception)
        self.assertIn('[authentication_failed]', message)
        self.assertIn(transport.connect_cmd('a'), message)


class LoginHelpersTests(unittest.TestCase):
    def test_ensure_login_quotes_server(self):
        server = 'bad; name'
        with mock.patch.object(transport.connection, 'is_connected', return_value=False), \
                mock.patch.dict(os.environ, {'REMOTE_WS_LOGIN_GUI': '0'}):
            with self.assertRaises(FileToolError) as ctx:
                transport.ensure_login(server)
        message = str(ctx.exception)
        self.assertIn(transport.connect_cmd(server), message)
        self.assertIn(shlex.quote(server), message)

    def test_launch_window_without_windows_reports_connect_command(self):
        with mock.patch.object(local_connection.shutil, 'which', return_value=None), \
                mock.patch.dict(os.environ, {'WSL_DISTRO_NAME': 'Ubuntu'}):
            with self.assertRaises(LoginError) as ctx:
                local_connection.launch_window('a')
        self.assertIn(local_connection.connect_hint('a'), str(ctx.exception))

    def test_launch_window_subprocess_error_becomes_login_error(self):
        with mock.patch.object(local_connection.shutil, 'which', return_value='powershell.exe'), \
                mock.patch.dict(os.environ, {'WSL_DISTRO_NAME': 'Ubuntu'}), \
                mock.patch.object(local_connection.subprocess, 'run',
                                  side_effect=OSError('boom')):
            with self.assertRaises(LoginError) as ctx:
                local_connection.launch_window('a')
        self.assertIn(local_connection.connect_hint('a'), str(ctx.exception))

    def test_launch_window_nonzero_exit_reports_connect_command(self):
        wslpath = SimpleNamespace(stdout='/tmp/connect.ps1\n')
        powershell = SimpleNamespace(returncode=1)
        with mock.patch.object(local_connection.shutil, 'which', return_value='powershell.exe'), \
                mock.patch.dict(os.environ, {'WSL_DISTRO_NAME': 'Ubuntu'}), \
                mock.patch.object(local_connection.subprocess, 'run',
                                  side_effect=[wslpath, powershell]):
            with self.assertRaises(LoginError) as ctx:
                local_connection.launch_window('a')
        self.assertIn(local_connection.connect_hint('a'), str(ctx.exception))


class GuideCompatibilityTests(unittest.TestCase):
    def test_generic_topic_still_ignores_server_and_verbose(self):
        text = remote_ws_mcp.TOOL_MAP['get_guide'](
            {'topic': 'index', 'server': 'not-configured', 'verbose': True})
        value = json.loads(text)
        self.assertIn('topics', value)
        self.assertEqual(value['topics'][0]['topic'], 'workspace')

    def test_empty_server_is_treated_as_omitted(self):
        with mock.patch.object(remote_ws_mcp.guide_tools, '_context', return_value={
                'SERVER': 'configured', 'CONFIGURED': False}):
            empty = json.loads(remote_ws_mcp.TOOL_MAP['get_guide'](
                {'topic': 'workspace', 'server': ''}))
            omitted = json.loads(remote_ws_mcp.TOOL_MAP['get_guide'](
                {'topic': 'workspace'}))
        self.assertEqual(empty['server'], omitted['server'])

    def test_non_string_server_is_rejected(self):
        for server in (None, 0, False):
            with self.subTest(server=server), self.assertRaises(FileToolError):
                remote_ws_mcp.TOOL_MAP['get_guide'](
                    {'topic': 'workspace', 'server': server})


if __name__ == '__main__':
    unittest.main()