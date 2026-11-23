import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MCP = ROOT / 'mcp'
if str(MCP) not in sys.path:
    sys.path.insert(0, str(MCP))

import file_tools
import project_tools
import text_tools


LIST_RESPONSE = {
    'path': '/home/u/demo',
    'entries': [{'name': 'a.py', 'type': 'file', 'size': 3, 'modified_time': 1}],
    'total': 1,
    'next_offset': None,
}
READ_RESPONSE = {
    'path': '/home/u/demo.py',
    'version': {'size': 3, 'mtime_ns': 1},
    'sha256': 'a' * 64,
    'editable': True,
    'blocks': [{'start_line': 1, 'text': 'one\n'}],
    'next_line': None,
}
SEARCH_RESPONSE = {'path': '/home/u/demo', 'files': [], 'matches': 0}
PROJECT_RESPONSE = {
    'path': '/home/u/demo',
    'index': 'file "a.py"\n',
    'total': 1,
    'next_offset': None,
    'incomplete': False,
    'reason': None,
    'excluded': 0,
    'restricted': 0,
    'depth_limited': 0,
}


def schema_accepts(schema, value):
    kinds = schema.get('type')
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        expected = {'string': str, 'integer': int, 'boolean': bool,
                    'array': list, 'object': dict}
        if not any(type(value) is expected.get(kind) for kind in kinds):
            return False
    if 'enum' in schema and value not in schema['enum']:
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
    elif isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            return False
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            return False
        if 'items' in schema and not all(schema_accepts(schema['items'], item)
                                         for item in value):
            return False
    elif isinstance(value, dict):
        properties = schema.get('properties', {})
        if any(key not in value for key in schema.get('required', [])):
            return False
        if schema.get('additionalProperties') is False and any(
                key not in properties for key in value):
            return False
        for key, item in value.items():
            if key in properties and not schema_accepts(properties[key], item):
                return False
    return True


class FileQueryTests(unittest.TestCase):
    def setUp(self):
        self.payloads = []
        self.response = copy.deepcopy(LIST_RESPONSE)
        patches = [
            mock.patch.object(file_tools.config, 'resolve_context',
                              return_value={'SERVER': 'alpha'}),
            mock.patch.object(file_tools, 'roots_config', return_value=['/home/u']),
            mock.patch.object(file_tools, 'request_path',
                              side_effect=lambda path, roots:
                              path if path.startswith('/') else roots[0] + '/' + path),
            mock.patch.object(file_tools.transport, 'remote_json',
                              side_effect=self.remote_json),
            mock.patch('subprocess.run',
                       side_effect=AssertionError('unexpected external access')),
            mock.patch('subprocess.Popen',
                       side_effect=AssertionError('unexpected external access')),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def remote_json(self, server, payload, timeout=120, source=None):
        self.payloads.append(copy.deepcopy(payload))
        return copy.deepcopy(self.response)

    def query(self, arguments):
        return json.loads(file_tools.file_query(arguments))

    def test_published_defaults_do_not_change_file_query_branches(self):
        schema = next(schema for name, _, _, schema in file_tools.tools()
                      if name == 'file_query')
        defaults = {key: prop['default'] for key, prop in schema['properties'].items()
                    if 'default' in prop}
        for action in ('list', 'read', 'search', 'project'):
            with self.subTest(action=action):
                args = dict(path='demo', action=action)
                if action == 'search':
                    args['query'] = 'needle'
                plain = self.query(args)
                payload = self.payloads[-1]
                self.assertEqual(self.query({**defaults, **args}), plain)
                self.assertEqual(self.payloads[-1], payload)

    def test_list_depth_is_ignored_with_note(self):
        value = self.query({'action': 'list', 'path': 'demo', 'depth': 2,
                            'compact': False})
        payload = self.payloads[-1]
        self.assertEqual(payload['operation'], 'list')
        self.assertNotIn('depth', payload)
        self.assertNotIn('compact', payload)
        self.assertIn('action=project', value['parameter_note'])

    def test_list_neutral_cross_branch_fields_are_ignored_without_remote(self):
        value = self.query({
            'action': 'list', 'path': 'demo', 'query': '', 'files': [],
            'ranges': [], 'max_chars': 0, 'start_line': 0, 'max_lines': 0,
            'context_lines': 0, 'case_sensitive': False, 'include_sha256': False,
        })
        note = value['parameter_note']
        for text in ('query="" 已忽略', 'files=[] 已忽略', 'ranges=[] 已忽略',
                     'max_chars=0 已忽略', 'start_line=0 已忽略',
                     'max_lines=0 已忽略', 'context_lines=0 已忽略',
                     'case_sensitive=false 已忽略', 'include_sha256=false 已忽略'):
            self.assertIn(text, note)
        self.assertEqual(set(self.payloads[-1]),
                         {'operation', 'roots', 'path', 'offset', 'limit'})

    def test_list_intentional_cross_branch_values_redirect(self):
        cases = [
            ({'query': 'needle'}, 'search'),
            ({'files': [{'path': 'a.py'}]}, 'project'),
            ({'ranges': [{'start_line': 1, 'max_lines': 1}]}, 'read'),
            ({'max_chars': 100}, 'read'),
            ({'start_line': 1}, 'read'),
        ]
        for extra, target in cases:
            with self.subTest(extra=extra):
                self.payloads.clear()
                with self.assertRaises(file_tools.FileToolError) as caught:
                    file_tools.file_query(dict({'action': 'list', 'path': 'demo'}, **extra))
                self.assertIn(target, str(caught.exception))
                self.assertEqual(self.payloads, [])

    def test_read_keeps_existing_compatibility(self):
        self.response = copy.deepcopy(READ_RESPONSE)
        value = self.query({
            'action': 'read', 'path': 'demo.py', 'start_line': 2, 'max_lines': 3,
            'ranges': [], 'max_chars': 30000, 'offset': 1, 'limit': 2,
            'query': 'needle', 'file_glob': '*.py', 'context_lines': 1,
            'case_sensitive': False, 'include_sha256': True,
            'files': [{'path': 'ignored.py'}], 'depth': 2,
        })
        payload = self.payloads[-1]
        self.assertEqual(payload['operation'], 'read')
        for key in file_tools.READ_COMPAT_KEYS:
            self.assertNotIn(key, payload)
        self.assertFalse(payload['multi_range'])
        self.assertEqual(payload['ranges'], [{'start_line': 2, 'max_lines': 3}])
        self.assertEqual(payload['max_chars'], 20000)
        note = value['parameter_note']
        for key in file_tools.READ_COMPAT_KEYS:
            self.assertIn(key, note)
        self.assertIn('read 操作未改变', note)

    def test_read_empty_ranges_is_omitted_and_other_ranges_stay_strict(self):
        self.response = copy.deepcopy(READ_RESPONSE)
        self.query({'action': 'read', 'path': 'demo.py', 'ranges': []})
        self.assertEqual(self.payloads[-1]['ranges'],
                         [{'start_line': 1, 'max_lines': 100}])
        with self.assertRaises(file_tools.FileToolError):
            file_tools.file_query({
                'action': 'read', 'path': 'demo.py',
                'ranges': [{'start_line': 1, 'max_lines': 1}], 'compact': False,
            })

    def test_nested_items_use_combined_validation(self):
        with self.assertRaises(file_tools.FileToolError) as caught:
            file_tools.file_query({'action': 'read', 'path': 'demo.py',
                                   'ranges': [{'start_line': 1}]})
        message = str(caught.exception)
        self.assertIn('缺少必填字段: max_lines', message)
        self.assertNotIn('允许字段及类型', message)

        with self.assertRaises(file_tools.FileToolError) as caught:
            file_tools.file_query({
                'action': 'project', 'path': 'demo',
                'files': [{'path': 'a.py', 'bogus': 0}],
            })
        message = str(caught.exception)
        self.assertIn('不允许字段: bogus', message)
        self.assertNotIn('允许字段及类型', message)

    def test_search_empty_glob_and_max_chars_are_clamped_with_note(self):
        self.response = copy.deepcopy(SEARCH_RESPONSE)
        value = self.query({'action': 'search', 'path': 'demo', 'query': 'needle',
                            'file_glob': '', 'max_chars': 20000})
        payload = self.payloads[-1]
        self.assertEqual(payload['file_glob'], '*')
        self.assertEqual(payload['max_chars'], 12000)
        self.assertIn('file_glob', value['parameter_note'])
        self.assertIn('12000', value['parameter_note'])

    def test_search_neutral_cross_branch_fields_are_ignored(self):
        self.response = copy.deepcopy(SEARCH_RESPONSE)
        value = self.query({
            'action': 'search', 'path': 'demo', 'query': 'needle',
            'files': [], 'ranges': [], 'start_line': 0, 'max_lines': 0,
            'compact': False, 'offset': 0, 'depth': 0,
        })
        payload = self.payloads[-1]
        for key in ('files', 'ranges', 'start_line', 'max_lines', 'compact',
                    'offset', 'depth'):
            self.assertNotIn(key, payload)
        note = value['parameter_note']
        for text in ('files=[] 已忽略', 'ranges=[] 已忽略',
                     'start_line=0 已忽略', 'max_lines=0 已忽略',
                     'compact=false 已忽略', 'offset=0 已忽略',
                     'depth=0 已忽略'):
            self.assertIn(text, note)

    def test_search_intentional_cross_branch_values_redirect(self):
        cases = [
            ({'files': [{'path': 'a.py'}]}, 'project'),
            ({'ranges': [{'start_line': 1, 'max_lines': 1}]}, 'read'),
            ({'depth': 2}, 'project'),
            ({'start_line': 1}, 'read'),
        ]
        for extra, target in cases:
            with self.subTest(extra=extra):
                self.payloads.clear()
                with self.assertRaises(file_tools.FileToolError) as caught:
                    file_tools.file_query(dict({'action': 'search', 'path': 'demo',
                                                'query': 'needle'}, **extra))
                self.assertIn(target, str(caught.exception))
                self.assertEqual(self.payloads, [])

    def test_project_empty_files_returns_index(self):
        self.response = copy.deepcopy(PROJECT_RESPONSE)
        value = self.query({'action': 'project', 'path': 'demo', 'files': [],
                            'depth': 3})
        payload = self.payloads[-1]
        self.assertNotIn('files', payload)
        self.assertEqual(payload['depth'], 3)
        self.assertNotIn('parameter_note', value)

    def test_project_batch_strips_pagination_fields_with_note(self):
        self.response = {'path': '/home/u/demo',
                         'files': [{'path': 'a.py', 'blocks': []}],
                         'pending': [], 'complete': True}
        value = self.query({
            'action': 'project', 'path': 'demo',
            'files': [{'path': 'a.py', 'start_line': 2, 'max_lines': 5}],
            'depth': 2, 'offset': 1, 'limit': 3, 'max_chars': 25000,
        })
        payload = self.payloads[-1]
        for key in ('depth', 'offset', 'limit'):
            self.assertNotIn(key, payload)
            self.assertIn(key, value['parameter_note'])
        self.assertEqual(payload['files'],
                         [{'path': 'a.py', 'start_line': 2, 'max_lines': 5}])
        self.assertEqual(payload['max_chars'], 20000)
        self.assertIn('20000', value['parameter_note'])

    def test_project_neutral_cross_branch_fields_are_ignored(self):
        self.response = copy.deepcopy(PROJECT_RESPONSE)
        value = self.query({
            'action': 'project', 'path': 'demo', 'query': '', 'file_glob': '',
            'ranges': [], 'start_line': 0, 'max_lines': 0, 'compact': False,
            'context_lines': 0, 'case_sensitive': False, 'include_sha256': False,
        })
        payload = self.payloads[-1]
        for key in ('query', 'file_glob', 'ranges', 'start_line', 'max_lines',
                    'compact', 'context_lines', 'case_sensitive', 'include_sha256'):
            self.assertNotIn(key, payload)
        note = value['parameter_note']
        for text in ('query="" 已忽略', 'file_glob="" 已忽略',
                     'ranges=[] 已忽略', 'start_line=0 已忽略',
                     'max_lines=0 已忽略', 'compact=false 已忽略',
                     'context_lines=0 已忽略', 'case_sensitive=false 已忽略',
                     'include_sha256=false 已忽略'):
            self.assertIn(text, note)

    def test_project_intentional_cross_branch_values_redirect(self):
        cases = [
            ({'query': 'needle'}, 'search'),
            ({'ranges': [{'start_line': 1, 'max_lines': 1}]}, 'read'),
            ({'compact': True}, 'read'),
        ]
        for extra, target in cases:
            with self.subTest(extra=extra):
                self.payloads.clear()
                with self.assertRaises(file_tools.FileToolError) as caught:
                    file_tools.file_query(dict({'action': 'project', 'path': 'demo'},
                                               **extra))
                self.assertIn(target, str(caught.exception))
                self.assertEqual(self.payloads, [])

    def test_workspace_write_rejects_cross_branch_fields(self):
        digest = 'a' * 64
        with self.assertRaises(file_tools.FileToolError) as caught:
            file_tools.workspace_write({
                'action': 'edit', 'path': 'demo.py', 'expected_sha256': digest,
                'edits': [{'old_text': 'a', 'new_text': 'b'}], 'project': 'demo',
            })
        self.assertIn('project', str(caught.exception))
        with self.assertRaises(file_tools.FileToolError) as caught:
            file_tools.workspace_write({'action': 'init', 'project': 'demo',
                                        'path': 'demo.py'})
        self.assertIn('path', str(caught.exception))
        self.assertEqual(self.payloads, [])

    def test_validate_arguments_combines_missing_and_invalid(self):
        with self.assertRaises(file_tools.FileToolError) as caught:
            file_tools.validate_arguments(
                {'action': 'read', 'bogus': 1},
                {'action': str, 'path': str, 'limit': int},
                ['action', 'path'], 'action=read')
        message = str(caught.exception)
        self.assertIn('缺少必填字段: path', message)
        self.assertIn('不允许字段: bogus', message)
        self.assertNotIn('允许字段及类型', message)
        self.assertNotIn('path（字符串）', message)
        self.assertNotIn('limit（整数）', message)

    def test_validate_arguments_reports_chinese_type(self):
        with self.assertRaises(file_tools.FileToolError) as caught:
            file_tools.validate_arguments(
                {'action': 'read', 'path': 'x', 'limit': True},
                {'action': str, 'path': str, 'limit': int},
                ['action', 'path'], 'action=read')
        message = str(caught.exception)
        self.assertIn('参数类型错误', message)
        self.assertIn('limit 期望整数，收到bool', message)
        self.assertNotIn('允许字段及类型', message)

    def test_schema_has_constraints_defaults_and_descriptions(self):
        tools = {name: (description, schema)
                 for name, _, description, schema in file_tools.tools()}
        description, schema = tools['file_query']
        props = schema['properties']
        self.assertEqual(schema['required'], ['action', 'path'])
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(props['action']['enum'],
                         ['list', 'read', 'search', 'project'])
        self.assertEqual(props['offset']['minimum'], 0)
        self.assertNotIn('default', props['offset'])
        self.assertEqual(props['limit']['minimum'], 0)
        self.assertNotIn('maximum', props['limit'])
        self.assertNotIn('default', props['limit'])
        self.assertEqual(props['start_line']['minimum'], 0)
        self.assertNotIn('default', props['start_line'])
        self.assertEqual(props['max_lines']['minimum'], 0)
        self.assertNotIn('maximum', props['max_lines'])
        self.assertNotIn('maximum', props['ranges']['items']['properties']['max_lines'])
        self.assertEqual(props['ranges']['items']['properties']['max_lines']['minimum'], 1)
        self.assertNotIn('minItems', props['ranges'])
        self.assertEqual(props['ranges']['maxItems'], 8)
        self.assertEqual(props['ranges']['items']['required'],
                         ['start_line', 'max_lines'])
        self.assertEqual(props['max_chars']['minimum'], 0)
        self.assertNotIn('maximum', props['max_chars'])
        self.assertNotIn('default', props['max_chars'])
        self.assertIn('输出字符预算', props['max_chars']['description'])
        self.assertNotIn('minItems', props['files'])
        self.assertEqual(props['files']['maxItems'], 8)
        self.assertNotIn('maximum', props['files']['items']['properties']['max_lines'])
        self.assertNotIn('required', props['files']['items'])
        self.assertEqual(props['context_lines']['minimum'], 0)
        self.assertNotIn('maximum', props['context_lines'])
        self.assertNotIn('default', props['depth'])
        self.assertNotIn('default', props['file_glob'])
        self.assertIn('pattern', props['server'])
        self.assertNotIn('maximum', props['limit'])
        self.assertIn('路径', props['path']['description'])
        self.assertNotIn('兼容', description)

        description, schema = tools['workspace_write']
        props = schema['properties']
        self.assertEqual(props['expected_sha256']['pattern'], '^[0-9a-fA-F]{64}$')
        self.assertEqual(props['edits']['minItems'], 1)
        self.assertEqual(props['edits']['maxItems'], 100)
        self.assertEqual(props['edits']['items']['required'],
                         ['old_text', 'new_text'])
        self.assertNotIn('default', props['include_diff'])
        self.assertEqual(props['max_chars']['maximum'], 6000)
        self.assertIn('pattern', props['project'])
        self.assertIn('SHA-256', description)

    def test_output_budget_parameters_are_clamped_and_noted(self):
        self.response = copy.deepcopy(LIST_RESPONSE)
        value = self.query({'action': 'list', 'path': 'demo', 'limit': 5000})
        self.assertEqual(self.payloads[-1]['limit'], 1000)
        self.assertIn('limit 超过 1000', value['parameter_note'])

        self.response = copy.deepcopy(READ_RESPONSE)
        value = self.query({'action': 'read', 'path': 'demo.py', 'max_lines': 3000})
        self.assertEqual(self.payloads[-1]['ranges'][0]['max_lines'], 1000)
        self.assertIn('max_lines 超过 1000', value['parameter_note'])
        value = self.query({'action': 'read', 'path': 'demo.py',
                            'ranges': [{'start_line': 1, 'max_lines': 3000}]})
        self.assertEqual(self.payloads[-1]['ranges'][0]['max_lines'], 1000)
        self.assertIn('ranges.max_lines 超过 1000', value['parameter_note'])

        self.response = copy.deepcopy(SEARCH_RESPONSE)
        value = self.query({'action': 'search', 'path': 'demo', 'query': 'needle',
                            'limit': 500, 'context_lines': 50})
        self.assertEqual(self.payloads[-1]['limit'], 100)
        self.assertEqual(self.payloads[-1]['context_lines'], 10)
        self.assertIn('limit 超过 100', value['parameter_note'])
        self.assertIn('context_lines 超过 10', value['parameter_note'])

        self.response = copy.deepcopy(PROJECT_RESPONSE)
        value = self.query({'action': 'project', 'path': 'demo', 'limit': 900})
        self.assertEqual(self.payloads[-1]['limit'], 500)
        self.assertIn('limit 超过 500', value['parameter_note'])
        self.response = {'path': '/home/u/demo', 'files': [], 'pending': [], 'complete': True}
        value = self.query({'action': 'project', 'path': 'demo',
                            'files': [{'path': 'a.py', 'max_lines': 3000}]})
        self.assertEqual(self.payloads[-1]['files'][0]['max_lines'], 2000)
        self.assertIn('files.max_lines 超过 2000', value['parameter_note'])

    def test_schema_clamp_fields_accept_over_budget_and_empty_server(self):
        tools = {name: schema for name, _, _, schema in file_tools.tools()}
        props = tools['file_query']['properties']
        for key, value in (('limit', 5000), ('max_lines', 3000),
                           ('context_lines', 100), ('server', '')):
            with self.subTest(key=key):
                self.assertTrue(schema_accepts(props[key], value))
        self.assertTrue(schema_accepts(
            props['ranges']['items']['properties']['max_lines'], 3000))
        self.assertTrue(schema_accepts(
            props['files']['items']['properties']['max_lines'], 3000))
        self.assertTrue(schema_accepts(tools['download_query']['properties']['server'], ''))
        self.assertTrue(schema_accepts(tools['download_control']['properties']['server'], ''))

    def test_read_max_chars_24000_is_not_blocked_by_schema_and_is_clamped(self):
        tools = {name: (description, schema)
                 for name, _, description, schema in file_tools.tools()}
        schema = tools['file_query'][1]
        self.assertNotIn('maximum', schema['properties']['max_chars'])
        self.response = copy.deepcopy(READ_RESPONSE)
        value = self.query({'action': 'read', 'path': 'demo.py', 'max_chars': 24000})
        self.assertEqual(self.payloads[-1]['max_chars'], 20000)
        self.assertIn('20000', value['parameter_note'])

    def test_schema_runtime_matrix_for_neutral_zero_and_native_zero(self):
        tools = {name: (description, schema)
                 for name, _, description, schema in file_tools.tools()}
        props = tools['file_query'][1]['properties']
        for field, value in (
                ('limit', 0), ('start_line', 0), ('max_lines', 0),
                ('max_chars', 0), ('files', [{}]), ('ranges', []),
                ('max_chars', 24000)):
            with self.subTest(field=field, value=value):
                self.assertTrue(schema_accepts(props[field], value))
        self.assertFalse(schema_accepts(props['ranges'], [{}]))
        self.assertFalse(schema_accepts(props['files'], [{'unknown': 1}]))

        cases = [
            ('list', {'start_line': 0, 'max_lines': 0, 'max_chars': 0},
             LIST_RESPONSE, True,
             ('start_line=0 已忽略', 'max_lines=0 已忽略',
              'max_chars=0 已忽略')),
            ('search', {'offset': 0, 'start_line': 0, 'max_lines': 0, 'depth': 0},
             SEARCH_RESPONSE, True,
             ('offset=0 已忽略', 'start_line=0 已忽略',
              'max_lines=0 已忽略', 'depth=0 已忽略')),
            ('project', {'start_line': 0, 'max_lines': 0},
             PROJECT_RESPONSE, True,
             ('start_line=0 已忽略', 'max_lines=0 已忽略')),
            ('read', {'limit': 0}, READ_RESPONSE, True, ('limit',)),
            ('read', {'files': [{}]}, READ_RESPONSE, True, ('files',)),
            ('read', {'ranges': []}, READ_RESPONSE, True, ()),
            ('read', {'max_chars': 24000}, READ_RESPONSE, True, ('20000',)),
            ('list', {'limit': 0}, LIST_RESPONSE, False, ()),
            ('search', {'limit': 0}, SEARCH_RESPONSE, False, ()),
            ('project', {'limit': 0}, PROJECT_RESPONSE, False, ()),
            ('read', {'start_line': 0}, READ_RESPONSE, False, ()),
            ('read', {'max_lines': 0}, READ_RESPONSE, False, ()),
            ('read', {'max_chars': 0}, READ_RESPONSE, False, ()),
            ('search', {'max_chars': 0}, SEARCH_RESPONSE, False, ()),
            ('project', {'max_chars': 0}, PROJECT_RESPONSE, False, ()),
            ('project', {'files': [{}]}, PROJECT_RESPONSE, False, ()),
        ]
        for action, extra, response, accepted, notes in cases:
            with self.subTest(action=action, extra=extra):
                self.response = copy.deepcopy(response)
                self.payloads.clear()
                arguments = {'action': action,
                             'path': 'demo.py' if action == 'read' else 'demo'}
                if action == 'search':
                    arguments['query'] = 'needle'
                arguments.update(extra)
                if accepted:
                    value = self.query(arguments)
                    note = value.get('parameter_note', '')
                    for text in notes:
                        self.assertIn(text, note)
                    if action == 'read' and 'ranges' in extra:
                        self.assertEqual(self.payloads[-1]['ranges'],
                                         [{'start_line': 1, 'max_lines': 100}])
                    if action == 'read' and extra.get('max_chars') == 24000:
                        self.assertEqual(self.payloads[-1]['max_chars'], 20000)
                else:
                    with self.assertRaises(file_tools.FileToolError):
                        file_tools.file_query(arguments)
                    self.assertEqual(self.payloads, [])

    def test_download_schemas_match_runtime_compatibility(self):
        tools = {name: (description, schema)
                 for name, _, description, schema in file_tools.tools()}

        description, schema = tools['download_query']
        props = schema['properties']
        self.assertEqual(set(props),
                         {'action', 'state', 'offset', 'limit', 'server', 'task_id',
                          'wait_sec', 'verbose', 'verify_local'})
        self.assertEqual(schema['required'], ['action'])
        self.assertEqual(set(props['state']['enum']),
                         {'starting', 'running', 'completed', 'cancelled', 'interrupted'})
        self.assertNotIn('description', props['state'])
        self.assertEqual(props['offset']['minimum'], 0)
        self.assertNotIn('default', props['offset'])
        self.assertNotIn('maximum', props['offset'])
        self.assertEqual(props['limit']['minimum'], 1)
        self.assertNotIn('default', props['limit'])
        self.assertNotIn('maximum', props['limit'])
        self.assertNotIn('description', props['limit'])
        self.assertTrue(schema_accepts(props['server'], ''))
        self.assertEqual(props['task_id']['pattern'], '^([0-9a-f]{32})?$')
        pattern = props['task_id']['pattern']
        self.assertEqual(pattern, '^([0-9a-f]{32})?$')
        self.assertIsNotNone(re.fullmatch(pattern, ''))
        self.assertIsNotNone(re.fullmatch(pattern, 'a' * 32))
        self.assertIsNone(re.fullmatch(pattern, 'abc'))
        self.assertEqual(props['task_id']['pattern'], '^([0-9a-f]{32})?$')
        self.assertEqual(props['wait_sec']['minimum'], 0)
        self.assertNotIn('default', props['wait_sec'])
        self.assertNotIn('maximum', props['wait_sec'])
        self.assertIn('仅 status', props['wait_sec']['description'])
        self.assertFalse(props['verbose']['default'])
        self.assertNotIn('default', props['verify_local'])
        self.assertNotIn('description', props['verify_local'])

        description, schema = tools['download_control']
        props = schema['properties']
        self.assertEqual(set(props),
                         {'action', 'remote_path', 'server', 'local_path', 'task_id',
                          'verbose', 'wait_sec', 'verify_local'})
        self.assertEqual(schema['required'], ['action'])
        self.assertIn('远端路径', props['remote_path']['description'])
        self.assertTrue(schema_accepts(props['server'], ''))
        pattern = props['task_id']['pattern']
        self.assertEqual(pattern, '^([0-9a-f]{32})?$')
        self.assertIsNotNone(re.fullmatch(pattern, ''))
        self.assertIsNotNone(re.fullmatch(pattern, 'a' * 32))
        self.assertIsNone(re.fullmatch(pattern, 'abc'))
        self.assertEqual(props['task_id']['pattern'], '^([0-9a-f]{32})?$')
        self.assertNotIn('default', props['verbose'])
        self.assertEqual(props['verbose']['enum'], [False])
        self.assertNotIn('default', props['verify_local'])
        self.assertEqual(props['verify_local']['enum'], [False])
        self.assertEqual(props['wait_sec']['minimum'], 0)
        self.assertEqual(props['wait_sec']['maximum'], 0)
        self.assertNotIn('default', props['wait_sec'])
        self.assertNotIn('description', props['wait_sec'])

    def test_notes_cannot_push_output_past_max_text(self):
        text = json.dumps({'path': 'x', 'blob': 'a' * (file_tools.MAX_TEXT - 200)},
                          ensure_ascii=False, separators=(',', ':'))
        self.assertLess(len(text), file_tools.MAX_TEXT)
        with self.assertRaises(file_tools.FileToolError) as caught:
            file_tools.attach_note(text, ['x' * 200])
        self.assertIn(str(file_tools.MAX_TEXT), str(caught.exception))

        self.response = {
            'path': '/home/u/demo', 'entries': [], 'total': 0,
            'next_offset': None, 'blob': 'a' * (file_tools.MAX_TEXT - 120),
        }
        with self.assertRaises(file_tools.FileToolError):
            file_tools.file_query({'action': 'list', 'path': 'demo', 'depth': 2})

    def run_remote_source(self, source, payload):
        class Output:
            def __init__(self):
                self.buffer = io.BytesIO()

        output = Output()
        environment = {'roots': payload['roots'], '__name__': 'remote_test'}
        old_stdin, old_stdout = sys.stdin, sys.stdout
        try:
            sys.stdin = io.StringIO(json.dumps(payload))
            sys.stdout = output
            try:
                exec(compile(source, '<remote>', 'exec'), environment)
            except SystemExit:
                pass
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        return json.loads(output.buffer.getvalue().decode('utf-8'))

    def test_project_batch_continues_after_bad_items(self):
        with tempfile.TemporaryDirectory(prefix='remote-ws-project-') as directory:
            Path(directory, 'good.txt').write_text('good\n', encoding='utf-8')
            Path(directory, 'bad.bin').write_bytes(b'\x00bad\n')
            payload = {
                'roots': [directory], 'path': directory, 'max_chars': 20000,
                'files': [
                    {'path': 'good.txt', 'start_line': 1, 'max_lines': 10},
                    {'path': 'bad.bin', 'start_line': 1, 'max_lines': 10},
                    {'path': 'missing.txt', 'start_line': 1, 'max_lines': 10},
                ],
            }
            value = self.run_remote_source(project_tools.REMOTE_SOURCE, payload)
        self.assertTrue(value['ok'], value)
        self.assertEqual([item['path'] for item in value['files']], ['good.txt'])
        self.assertEqual({item['path'] for item in value['errors']},
                         {'bad.bin', 'missing.txt'})
        self.assertFalse(value['complete'])

    def batch(self, root, files, cap):
        return self.run_remote_source(project_tools.REMOTE_SOURCE, {
            'roots': [root], 'path': root, 'max_chars': cap, 'files': files})

    def test_small_batch_advances_without_loss(self):
        with tempfile.TemporaryDirectory() as root:
            texts = {name: ''.join(f'{i:03}\n' for i in range(120)) for name in ('a', 'b', 'c')}
            for name, text in texts.items():
                Path(root, name).write_text(text)
            pending = [dict(path=name, start_line=1, max_lines=100) for name in texts]
            output = dict.fromkeys(texts, '')
            for _ in range(30):
                before = pending
                page = self.batch(root, before, 650)
                self.assertTrue(page.pop('ok'), page)
                self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(',', ':'))), 650)
                for item in page['files']:
                    output[item['path']] += item['blocks'][0]['text']
                pending = page['pending']
                self.assertNotEqual(before, pending)
                if page['complete']:
                    break
            self.assertEqual(output, texts)
            self.assertEqual(pending, [])

    def test_batch_blocked_page_gives_recovery_not_identical_pending(self):
        with tempfile.TemporaryDirectory() as root:
            name = 'z' * 180
            Path(root, name).write_text('x' * 600 + '\n')
            items = [dict(path=name, start_line=1, max_lines=100)]
            blocked = self.batch(root, items, 512)
            self.assertFalse(blocked['ok'])
            self.assertIn('max_chars', blocked['error'])
            self.assertNotIn('pending', blocked)
            self.assertTrue(self.batch(root, items, 4000)['complete'])

    def test_batch_preserves_error_count_when_details_truncated(self):
        with tempfile.TemporaryDirectory() as root:
            items = [dict(path=str(i) + 'x' * 100, start_line=1, max_lines=1) for i in range(8)]
            value = self.batch(root, items, 512)
            self.assertTrue(value.pop('ok'), value)
            self.assertLessEqual(len(json.dumps(value, ensure_ascii=False, separators=(',', ':'))), 512)
            self.assertTrue(value['errors_truncated'])
            self.assertEqual(value['error_count'], len(items))
            self.assertFalse(value['complete'])

    def test_project_batch_aborts_on_path_safety_error(self):
        with tempfile.TemporaryDirectory(prefix='remote-ws-project-') as directory, \
                tempfile.TemporaryDirectory(prefix='remote-ws-outside-') as outside:
            Path(directory, 'good.txt').write_text('good\n', encoding='utf-8')
            target = Path(outside, 'target.txt')
            target.write_text('outside\n', encoding='utf-8')
            os.symlink(str(target), os.path.join(directory, 'escape'))
            payload = {
                'roots': [directory], 'path': directory, 'max_chars': 20000,
                'files': [
                    {'path': 'good.txt', 'start_line': 1, 'max_lines': 10},
                    {'path': 'escape', 'start_line': 1, 'max_lines': 10},
                ],
            }
            value = self.run_remote_source(project_tools.REMOTE_SOURCE, payload)
        self.assertFalse(value['ok'], value)
        self.assertIn('符号链接目标越界', value['error'])
        self.assertNotIn('files', value)

    def test_search_long_line_still_returns_path_and_match_lines(self):
        with tempfile.TemporaryDirectory(prefix='remote-ws-search-') as directory:
            Path(directory, 'long.txt').write_text('x' * 2000 + 'needle\n',
                                                   encoding='utf-8')
            payload = {
                'roots': [directory], 'path': directory, 'query': 'needle',
                'file_glob': '*.txt', 'case_sensitive': True,
                'include_sha256': False, 'context_lines': 0, 'limit': 8,
                'max_chars': 256,
            }
            value = self.run_remote_source(text_tools.REMOTE_SOURCE, payload)
        self.assertTrue(value['ok'], value)
        self.assertEqual(value['matches'], 1)
        self.assertEqual(len(value['files']), 1)
        self.assertEqual(value['files'][0]['match_lines'], [1])
        self.assertEqual(value['files'][0]['blocks'], [])
        self.assertEqual(value['reason'], 'output_limit')


if __name__ == '__main__':
    unittest.main()