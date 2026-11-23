"""Isolated tests for download state, request compatibility and byte protocol."""
import hashlib
import io
import gc
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'mcp'))

import remote_common
from remote_ws import download_state, downloads
from remote_ws import download_stream
from remote_ws.sources import files as files_source
from remote_ws.validation import FileToolError

TASK_ID = 'a' * 32


class DownloadStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / 'downloads'
        env = mock.patch.dict(os.environ, {'REMOTE_WS_TASK': 'default'})
        env.start()
        self.addCleanup(env.stop)
        root = mock.patch.object(download_state.connection, 'DOWNLOAD_ROOT', str(self.base))
        root.start()
        self.addCleanup(root.stop)

    def write_state(self, **values):
        state = dict(task_id=TASK_ID, state='interrupted', phase='interrupted',
                     downloaded_bytes=0, total_size=None,
                     created_at=time.time(), updated_at=time.time())
        state.update(values)
        download_state.atomic_state(state)
        return state

    def job_path(self):
        return self.base / 'default' / '.jobs'

    def test_completed_hides_prefix_verification_and_rounds_progress(self):
        state = dict(task_id=TASK_ID, state='completed', phase='completed',
                     downloaded_bytes=100, total_size=100, verified_bytes=50)
        result = download_state.report_state(state, verbose=True)
        self.assertEqual(result['progress'], 100)
        self.assertNotIn('verified_bytes', result)
        self.assertNotIn('verification_progress', result)

        partial = dict(state, state='interrupted', phase='downloading',
                       downloaded_bytes=1, total_size=3)
        self.assertEqual(download_state.report_state(partial)['progress'], 33.3)

    def test_verify_local_non_completed_is_not_an_error(self):
        result = download_state.verify_local_file({'state': 'running'})
        self.assertEqual(result['status'], 'not_completed')
        self.assertEqual(result['state'], 'running')
        self.assertIn('尚未完成', result['message'])

    def test_verify_local_completed_still_checks_sha256(self):
        base = download_state.downloads_dir()
        target = base / 'out.bin'
        content = b'completed payload'
        target.write_bytes(content)
        result = download_state.verify_local_file({
            'state': 'completed', 'local_path': str(target),
            'total_size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['size'], len(content))
        self.assertEqual(result['sha256'], hashlib.sha256(content).hexdigest())

    def test_status_ignores_server_clamps_wait_and_reports_not_completed(self):
        self.write_state(message='后台进程已退出')
        result = json.loads(downloads.download_query({
            'action': 'status', 'task_id': TASK_ID, 'server': 'elsewhere',
            'wait_sec': 99, 'verify_local': True}))
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(result['local_verification']['status'], 'not_completed')
        self.assertIn('server 已忽略', result['parameter_note'])
        self.assertIn('wait_sec 超过 10', result['parameter_note'])

        plain = json.loads(downloads.download_query({'action': 'status', 'task_id': TASK_ID}))
        self.assertNotIn('parameter_note', plain)
        self.assertNotIn('local_verification', plain)

    def test_status_task_id_errors_are_actionable(self):
        cases = [
            ({'action': 'status', 'task_id': ''}, '不能为空'),
            ({'action': 'status', 'task_id': 'abc'}, '完整 32 位'),
            ({'action': 'status', 'task_id': 'b' * 32}, '找不到下载任务'),
            ({'action': 'status'}, '必填'),
        ]
        for args, text in cases:
            with self.subTest(args=args):
                with self.assertRaises(FileToolError) as caught:
                    downloads.download_query(args)
                self.assertIn(text, str(caught.exception))
                self.assertIn('list', str(caught.exception))

    def test_cancel_starting_without_pid_reports_not_cancelled(self):
        token = download_state.pid_identity(os.getpid())
        self.assertTrue(token)
        self.write_state(state='starting', phase='starting', server='srv',
                         remote_path='/tmp/source', roots=['/tmp'],
                         local_path=str(self.base / 'default' / 'out.bin'),
                         partial_path=str(self.job_path() / (TASK_ID + '.part')),
                         launcher_pid=os.getpid(), launcher_token=token,
                         launch_at=time.time())
        with mock.patch.object(downloads, 'CANCEL_HANDOFF_WAIT', .05):
            result = json.loads(downloads.download_control(
                {'action': 'cancel', 'task_id': TASK_ID}))
        self.assertEqual(result['state'], 'starting')
        self.assertFalse(result['cancelled'])
        self.assertFalse(result['cancel_requested'])
        self.assertIn('未取消', result['message'])
        with open(download_state.task_path(TASK_ID), encoding='utf-8') as stream:
            self.assertEqual(json.load(stream)['state'], 'starting')

    def test_cancel_after_signal_still_active_marks_requested(self):
        self.write_state(state='running', phase='transferring', server='srv',
                         remote_path='/tmp/source', roots=['/tmp'],
                         local_path=str(self.base / 'default' / 'out.bin'),
                         partial_path=str(self.job_path() / (TASK_ID + '.part')),
                         pid=12345, pid_token='token', downloaded_bytes=1,
                         total_size=10)
        devnull = os.open(os.devnull, os.O_RDONLY)
        with mock.patch.object(downloads, 'alive', lambda state: True), \
             mock.patch.object(download_state, 'alive', lambda state: True), \
             mock.patch.object(downloads.os, 'pidfd_open', create=True, return_value=devnull), \
             mock.patch.object(downloads.signal, 'pidfd_send_signal', create=True) as send, \
             mock.patch.object(downloads, 'download_status',
                               return_value=json.dumps({'task_id': TASK_ID, 'state': 'running'})):
            result = json.loads(downloads.download_control(
                {'action': 'cancel', 'task_id': TASK_ID}))
        self.assertTrue(result['cancel_requested'])
        self.assertIn('尚未退出', result['message'])
        send.assert_called_once_with(devnull, downloads.signal.SIGTERM)

    def test_cancel_second_alive_check_false_never_claims_sent(self):
        self.write_state(state='running', phase='transferring', server='srv',
                         remote_path='/tmp/source', roots=['/tmp'],
                         local_path=str(self.base / 'default' / 'out.bin'),
                         partial_path=str(self.job_path() / (TASK_ID + '.part')),
                         pid=12345, pid_token='token', downloaded_bytes=1,
                         total_size=10)
        devnull = os.open(os.devnull, os.O_RDONLY)
        with mock.patch.object(downloads, 'alive', side_effect=[True, False]), \
             mock.patch.object(download_state, 'alive', lambda state: True), \
             mock.patch.object(downloads.os, 'pidfd_open', create=True, return_value=devnull), \
             mock.patch.object(downloads.signal, 'pidfd_send_signal', create=True) as send, \
             mock.patch.object(downloads, 'download_status',
                               return_value=json.dumps({'task_id': TASK_ID, 'state': 'running'})):
            result = json.loads(downloads.download_control(
                {'action': 'cancel', 'task_id': TASK_ID}))
        send.assert_not_called()
        self.assertFalse(result['cancelled'])
        self.assertFalse(result['cancel_requested'])
        self.assertIn('未发送取消信号', result['message'])
        self.assertNotIn('已发送取消信号', result['message'])

    def test_cancel_works_while_worker_lock_is_held(self):
        self.write_state(state='running', phase='transferring', server='srv',
                         remote_path='/tmp/source', roots=['/tmp'],
                         local_path=str(self.base / 'default' / 'out.bin'),
                         partial_path=str(self.job_path() / (TASK_ID + '.part')),
                         pid=12345, pid_token='token', downloaded_bytes=1,
                         total_size=10)
        active_fd = download_state.active_lock(TASK_ID)
        devnull = os.open(os.devnull, os.O_RDONLY)
        try:
            with mock.patch.object(downloads, 'alive', lambda state: True), \
                 mock.patch.object(download_state, 'alive', lambda state: True), \
                 mock.patch.object(downloads.os, 'pidfd_open', create=True, return_value=devnull), \
                 mock.patch.object(downloads.signal, 'pidfd_send_signal', create=True) as send, \
                 mock.patch.object(downloads, 'download_status',
                                   return_value=json.dumps({'task_id': TASK_ID, 'state': 'running'})):
                result = json.loads(downloads.download_control(
                    {'action': 'cancel', 'task_id': TASK_ID}))
        finally:
            os.close(active_fd)
        self.assertNotEqual(result['state'], 'busy')
        self.assertTrue(result['cancel_requested'])
        send.assert_called_once_with(devnull, downloads.signal.SIGTERM)

    def test_control_lock_makes_concurrent_control_busy_and_persists(self):
        held = download_state.control_lock(TASK_ID)
        try:
            cancelled = json.loads(downloads.download_control(
                {'action': 'cancel', 'task_id': TASK_ID, 'verbose': False}))
            resumed = json.loads(downloads.download_control(
                {'action': 'resume', 'task_id': TASK_ID}))
        finally:
            os.close(held)
        for result in (cancelled, resumed):
            self.assertEqual(result['state'], 'busy')
            self.assertIn('控制操作正在进行', result['message'])
        self.assertIn('verbose=false 已忽略', cancelled['parameter_note'])
        lock_path = self.job_path() / (TASK_ID + '.control')
        self.assertTrue(lock_path.is_file())
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_control_lock_rejects_symlink(self):
        jobs = self.job_path()
        jobs.mkdir(parents=True, exist_ok=True)
        outside = self.root / 'outside.control'
        outside.write_text('do not use')
        (jobs / (TASK_ID + '.control')).symlink_to(outside)
        with self.assertRaisesRegex(FileToolError, '符号链接'):
            download_state.control_lock(TASK_ID)
        self.assertEqual(outside.read_text(), 'do not use')

    def test_start_control_busy_does_not_launch(self):
        launch = mock.Mock()
        with mock.patch.object(downloads, 'control_lock', return_value=None), \
             mock.patch.object(downloads.config, 'resolve_context',
                               lambda server=None: {'SERVER': 'stub'}):
            result = json.loads(downloads.download_control(
                {'action': 'start', 'remote_path': '/tmp/source.bin'},
                login=mock.Mock(), launch_worker=launch,
                resolve_roots=lambda **kwargs: ['/tmp'],
                resolve_path=lambda path, roots: path))
        launch.assert_not_called()
        self.assertEqual(result['state'], 'busy')
        self.assertIn('控制操作正在进行', result['message'])

    def test_resume_missing_partial_fails_before_launch(self):
        self.write_state(server='srv', remote_path='/tmp/source', roots=['/tmp'],
                         local_path=str(self.base / 'default' / 'out.bin'),
                         partial_path=str(self.job_path() / (TASK_ID + '.part')))
        launch = mock.Mock()
        with self.assertRaises(FileToolError) as caught:
            downloads.download_control({'action': 'resume', 'task_id': TASK_ID},
                                       login=mock.Mock(), launch_worker=launch)
        self.assertIn('部分文件缺失', str(caught.exception))
        launch.assert_not_called()

    def test_resume_uses_record_snapshot_and_notes(self):
        self.job_path().mkdir(parents=True, exist_ok=True)
        partial = self.job_path() / (TASK_ID + '.part')
        partial.write_bytes(b'partial')
        self.write_state(server='recorded', remote_path='/tmp/source', roots=['/tmp'],
                         local_path=str(self.base / 'default' / 'out.bin'),
                         partial_path=str(partial))
        login = mock.Mock()
        seen = {}

        def launch(state, lock_fd):
            seen.update(state)
            return json.dumps({'task_id': state['task_id'], 'state': state['state']})

        result = json.loads(downloads.download_control(
            {'action': 'resume', 'task_id': TASK_ID, 'server': 'other',
             'remote_path': '', 'local_path': ''},
            login=login, launch_worker=launch))
        login.assert_called_once_with('recorded')
        self.assertEqual(seen['state'], 'starting')
        self.assertEqual(result['state'], 'starting')
        self.assertIn('server 已忽略', result['parameter_note'])
        self.assertIn('remote_path="" 已忽略', result['parameter_note'])
        self.assertIn('local_path="" 已忽略', result['parameter_note'])

    def test_resume_rejects_nonempty_path_hints(self):
        for key in ('remote_path', 'local_path'):
            with self.subTest(key=key):
                with self.assertRaises(FileToolError) as caught:
                    downloads.download_control(
                        {'action': 'resume', 'task_id': TASK_ID, key: '/elsewhere'})
                self.assertIn(f'不接受非空 {key}', str(caught.exception))

    def test_cancel_and_resume_ignore_neutral_flat_fields(self):
        jobs = self.job_path()
        jobs.mkdir(parents=True, exist_ok=True)
        partial = jobs / (TASK_ID + '.part')
        partial.write_bytes(b'partial')
        self.write_state(server='recorded', remote_path='/tmp/source', roots=['/tmp'],
                         local_path=str(self.base / 'default' / 'out.bin'),
                         partial_path=str(partial))
        neutral = {'verbose': False, 'wait_sec': 0, 'verify_local': False}
        expected = ('verbose=false 已忽略', 'wait_sec=0 已忽略',
                    'verify_local=false 已忽略')

        cancelled = json.loads(downloads.download_control(
            {'action': 'cancel', 'task_id': TASK_ID, **neutral}))
        for text in expected:
            self.assertIn(text, cancelled['parameter_note'])

        launch = mock.Mock(return_value=json.dumps({'state': 'starting'}))
        resumed = json.loads(downloads.download_control(
            {'action': 'resume', 'task_id': TASK_ID, **neutral},
            login=mock.Mock(), launch_worker=launch))
        launch.assert_called_once()
        for text in expected:
            self.assertIn(text, resumed['parameter_note'])

    def test_control_rejects_non_neutral_flat_fields(self):
        cases = [(('verbose', True), 'verbose=true'),
                 (('wait_sec', 5), 'wait_sec=5'),
                 (('verify_local', True), 'verify_local=true')]
        for action in ('start', 'cancel', 'resume'):
            base = ({'action': 'start', 'remote_path': '/tmp/source.bin'}
                    if action == 'start' else {'action': action, 'task_id': TASK_ID})
            for (key, value), text in cases:
                args = dict(base)
                args[key] = value
                with self.subTest(action=action, key=key):
                    with self.assertRaises(FileToolError) as caught:
                        downloads.download_control(args)
                    message = str(caught.exception)
                    self.assertIn('仅兼容中性值', message)
                    self.assertIn(text, message)
        wrong = [('start', {'verbose': 'false'}, 'verbose 必须是布尔值'),
                 ('cancel', {'wait_sec': False}, 'wait_sec 必须是整数'),
                 ('resume', {'verify_local': 0}, 'verify_local 必须是布尔值')]
        for action, values, text in wrong:
            base = ({'action': 'start', 'remote_path': '/tmp/source.bin'}
                    if action == 'start' else {'action': action, 'task_id': TASK_ID})
            with self.subTest(action=action, values=values):
                with self.assertRaises(FileToolError) as caught:
                    downloads.download_control(dict(base, **values))
                self.assertIn(text, str(caught.exception))

    def test_start_strips_neutral_fields_before_download(self):
        captured = {}

        def fake_download(payload, **kwargs):
            captured.update(payload)
            return '{}'

        with mock.patch.object(downloads, 'download_file', side_effect=fake_download):
            result = downloads.download_control(
                {'action': 'start', 'remote_path': '/tmp/source.bin', 'server': 'srv',
                 'local_path': '/tmp/local.bin',
                 'verbose': False, 'wait_sec': 0, 'verify_local': False},
                login=mock.Mock(), launch_worker=mock.Mock())
        # download_file is authoritative for start; neutral fields stay local.
        self.assertEqual(captured, {'remote_path': '/tmp/source.bin',
                                    'server': 'srv', 'local_path': '/tmp/local.bin'})
        self.assertIn('parameter_note', json.loads(result))

    def test_list_compatibility_clamps_and_notes(self):
        args = {'action': 'list', 'limit': 10**30, 'task_id': '',
                'wait_sec': 0, 'verify_local': False, 'server': 'other'}
        result = json.loads(downloads.download_query(args))
        self.assertEqual(result['tasks'], [])
        self.assertEqual(result['total'], 0)
        note = result['parameter_note']
        for text in ('limit 超过 100', 'server 已忽略', '本地下载池',
                     'task_id="" 已忽略', 'wait_sec=0 已忽略',
                     'verify_local=false 已忽略'):
            self.assertIn(text, note)

        self.write_state(server='recorded')
        local = json.loads(downloads.download_query({'action': 'list', 'server': 'other'}))
        self.assertEqual(local['total'], 1)
        self.assertEqual(local['tasks'][0]['task_id'], TASK_ID)
        self.assertIn('server 已忽略', local['parameter_note'])

        plain = json.loads(downloads.download_query({'action': 'list'}))
        self.assertEqual(set(plain), {'tasks', 'total', 'next_offset', 'unreadable_task_ids'})

        for args, text in [
            ({'action': 'list', 'task_id': TASK_ID}, 'task_id 仅兼容空字符串'),
            ({'action': 'list', 'wait_sec': 1}, 'wait_sec=0'),
            ({'action': 'list', 'verify_local': True}, 'verify_local=true'),
            ({'action': 'list', 'server': None}, 'server 类型无效'),
        ]:
            with self.subTest(args=args):
                with self.assertRaises(FileToolError) as caught:
                    downloads.download_query(args)
                self.assertIn(text, str(caught.exception))

    def test_start_empty_task_id_is_ignored_with_note(self):
        launch = mock.Mock(side_effect=lambda state, lock_fd: json.dumps(
            {'task_id': state['task_id'], 'state': state['state']}))
        login = mock.Mock()
        options = dict(login=login, launch_worker=launch,
                       resolve_roots=lambda **kwargs: ['/tmp'],
                       resolve_path=lambda path, roots: path)
        with mock.patch.object(downloads.config, 'resolve_context',
                               lambda server=None: {'SERVER': 'stub'}):
            result = json.loads(downloads.download_control(
                {'action': 'start', 'remote_path': '/tmp/source.bin',
                 'server': 'stub', 'task_id': '',
                 'verbose': False, 'wait_sec': 0, 'verify_local': False}, **options))
            plain = downloads.download_control(
                {'action': 'start', 'remote_path': '/tmp/source.bin',
                 'server': 'stub'}, **options)
        for text in ('task_id="" 已忽略', 'verbose=false 已忽略',
                     'wait_sec=0 已忽略', 'verify_local=false 已忽略'):
            self.assertIn(text, result['parameter_note'])
        self.assertEqual(launch.call_count, 2)
        login.assert_called_with('stub')
        self.assertNotIn('parameter_note', json.loads(plain))

    def test_worker_keeps_partial_and_reports_remote_stderr(self):
        task_id = 'd' * 32
        jobs = self.job_path()
        jobs.mkdir(parents=True, exist_ok=True)
        partial = jobs / (task_id + '.part')
        partial.write_bytes(b'')
        target = self.base / 'default' / 'out.bin'
        download_state.atomic_state(dict(
            task_id=task_id, server='srv', remote_path='/tmp/source', roots=['/tmp'],
            local_path=str(target), partial_path=str(partial), state='running',
            phase='verifying_local', downloaded_bytes=0, total_size=None,
            partial_sha256=hashlib.sha256(b'').hexdigest(),
            created_at=time.time(), updated_at=time.time(), identity=None))
        script = '\n'.join([
            'import json, sys',
            'sys.stdin.read()',
            "head = {'ok': True, 'protocol': 1, 'path': '/tmp/source',",
            "        'identity': {'size': 5, 'mtime_ns': 0, 'device': 1, 'inode': 2, 'ctime_ns': 0},",
            "        'offset': 0, 'length': 5}",
            "sys.stdout.write(json.dumps(head) + '\\n')",
            'sys.stdout.flush()',
            "sys.stdout.buffer.write(b'hello')",
            'sys.stdout.buffer.flush()',
            "sys.stderr.write('remote exploded\\n')",
            'sys.stderr.flush()',
            'sys.exit(1)',
        ])
        fd = download_state.active_lock(task_id)
        old_term = signal.getsignal(signal.SIGTERM)
        old_int = signal.getsignal(signal.SIGINT)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ResourceWarning)
            try:
                download_stream.worker(
                    task_id, fd,
                    command=lambda server: [sys.executable, '-B', '-c', script])
            finally:
                signal.signal(signal.SIGTERM, old_term)
                signal.signal(signal.SIGINT, old_int)
            gc.collect()
        resource_warnings = [str(item.message) for item in caught
                             if issubclass(item.category, ResourceWarning)]
        self.assertEqual(resource_warnings, [])
        result = download_state.load_state(task_id)
        self.assertEqual(result['state'], 'interrupted')
        self.assertIn('远端 stderr: remote exploded', result['message'])
        self.assertEqual(partial.read_bytes(), b'hello')
        self.assertFalse(target.exists())

    def test_worker_success_publishes_and_closes_pipes(self):
        task_id = 'f' * 32
        jobs = self.job_path()
        jobs.mkdir(parents=True, exist_ok=True)
        partial = jobs / (task_id + '.part')
        partial.write_bytes(b'')
        target = self.base / 'default' / 'out.bin'
        download_state.atomic_state(dict(
            task_id=task_id, server='srv', remote_path='/tmp/source', roots=['/tmp'],
            local_path=str(target), partial_path=str(partial), state='running',
            phase='verifying_local', downloaded_bytes=0, total_size=None,
            partial_sha256=hashlib.sha256(b'').hexdigest(),
            created_at=time.time(), updated_at=time.time(), identity=None))
        content = b'published payload'
        digest = hashlib.sha256(content).hexdigest()
        script = f"""
import json, sys
sys.stdin.read()
content = {content!r}
head = {{'ok': True, 'protocol': 1, 'path': '/tmp/source',
        'identity': {{'size': {len(content)}, 'mtime_ns': 0, 'device': 1, 'inode': 2, 'ctime_ns': 0}},
        'offset': 0, 'length': {len(content)}}}
sys.stdout.write(json.dumps(head) + '\\n')
sys.stdout.flush()
sys.stdout.buffer.write(content)
sys.stdout.buffer.flush()
sys.stdout.write(json.dumps({{'ok': True, 'sha256': '{digest}', 'stable': True}}) + '\\n')
sys.stdout.flush()
"""
        fd = download_state.active_lock(task_id)
        old_term = signal.getsignal(signal.SIGTERM)
        old_int = signal.getsignal(signal.SIGINT)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ResourceWarning)
            try:
                download_stream.worker(
                    task_id, fd,
                    command=lambda server: [sys.executable, '-B', '-c', script])
            finally:
                signal.signal(signal.SIGTERM, old_term)
                signal.signal(signal.SIGINT, old_int)
            gc.collect()
        resource_warnings = [str(item.message) for item in caught
                             if issubclass(item.category, ResourceWarning)]
        self.assertEqual(resource_warnings, [])
        result = download_state.load_state(task_id)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['sha256'], digest)
        self.assertEqual(target.read_bytes(), content)
        self.assertFalse(partial.exists())

    def test_worker_closes_lock_fd_when_load_state_fails(self):
        task_id = 'b' * 32
        fd = download_state.active_lock(task_id)
        try:
            with self.assertRaises(FileToolError):
                download_stream.worker(task_id, fd)
            with self.assertRaises(OSError):
                os.fstat(fd)
            again = download_state.active_lock(task_id)
            self.assertIsNotNone(again)
            os.close(again)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


class RemoteSourceTests(unittest.TestCase):
    class Buffer:
        def __init__(self):
            self.data = bytearray()

        def write(self, data):
            self.data.extend(data)
            return len(data)

        def flush(self):
            pass

    class Stdout:
        def __init__(self):
            self.buffer = RemoteSourceTests.Buffer()

        def write(self, text):
            return len(text)

        def flush(self):
            pass

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'remote'
        self.root.mkdir()

    def run_source(self, payload, fdopen=None):
        stdout, stderr = self.Stdout(), io.StringIO()
        patches = [mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                   mock.patch.object(sys, 'stdout', stdout),
                   mock.patch.object(sys, 'stderr', stderr)]
        if fdopen is not None:
            patches.append(mock.patch.object(os, 'fdopen', fdopen))
        for patch in patches:
            patch.start()
        try:
            try:
                exec(files_source.REMOTE_SOURCE, {'__name__': 'remote_test'})
                code = 0
            except SystemExit as error:
                code = error.code
        finally:
            for patch in reversed(patches):
                patch.stop()
        return code, bytes(stdout.buffer.data), stderr.getvalue()

    def payload(self, path):
        return {'operation': 'download', 'path': str(path), 'roots': [str(self.root)],
                'offset': 0, 'identity': None, 'prefix_sha256': None}

    def test_success_keeps_protocol_one_stream(self):
        content = b'line one\nline two\n' * 500 + b'tail'
        source = self.root / 'data.bin'
        source.write_bytes(content)
        code, output, stderr = self.run_source(self.payload(source))
        self.assertEqual(code, 0)
        self.assertEqual(stderr, '')
        line, rest = output.split(b'\n', 1)
        head = json.loads(line)
        self.assertEqual(head['protocol'], 1)
        self.assertEqual(head['length'], len(content))
        self.assertEqual(head['offset'], 0)
        self.assertEqual(rest[:len(content)], content)
        trailer = json.loads(rest[len(content):])
        self.assertTrue(trailer['stable'])
        self.assertEqual(trailer['sha256'], hashlib.sha256(content).hexdigest())

    def test_failure_after_stream_start_only_writes_stderr(self):
        source = self.root / 'data.bin'
        source.write_bytes(b'x' * 4096)
        real_fdopen = os.fdopen

        class Broken:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def read(self, *args, **kwargs):
                raise OSError('simulated read failure')

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def fdopen(fd, mode='r', *args, **kwargs):
            return Broken(real_fdopen(fd, mode, *args, **kwargs))

        code, output, stderr = self.run_source(self.payload(source), fdopen=fdopen)
        self.assertEqual(code, 1)
        self.assertEqual(output.count(b'\n'), 1)
        self.assertNotIn(b'"ok": false', output)
        self.assertTrue(json.loads(output.split(b'\n', 1)[0])['ok'])
        self.assertIn('simulated read failure', stderr)

    def test_failure_before_stream_still_emits_json_error(self):
        code, output, stderr = self.run_source(self.payload(self.root / 'missing.bin'))
        self.assertEqual(code, 1)
        self.assertEqual(stderr, '')
        value = json.loads(output)
        self.assertFalse(value['ok'])
        self.assertIn('路径不存在', value['error'])


class RemoteCommonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @staticmethod
    def load():
        namespace = {}
        exec(remote_common.SOURCE, namespace)
        return namespace

    def test_symlinked_root_is_allowed_but_escape_is_rejected(self):
        real = self.root / 'real'
        real.mkdir()
        (real / 'file.txt').write_text('ok')
        link = self.root / 'link'
        link.symlink_to(real, target_is_directory=True)
        namespace = self.load()
        namespace['roots'] = [str(link), str(real)]
        resolved = namespace['guarded'](str(link / 'file.txt'))
        self.assertEqual(resolved, str(real / 'file.txt'))
        self.assertEqual(namespace['guarded'](resolved), resolved)

        outside = self.root / 'outside.txt'
        outside.write_text('no')
        (real / 'escape').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, '符号链接目标越界'):
            namespace['guarded'](str(link / 'escape'))

    def test_root_links_cannot_widen_original_roots(self):
        workspace = self.root / 'workspace'
        data_root = workspace / 'data'
        data_root.mkdir(parents=True)
        target = data_root / 'code'
        target.mkdir()
        (target / 'file.txt').write_text('ok')
        code_link = workspace / 'code_link'
        code_link.symlink_to(target, target_is_directory=True)

        namespace = self.load()
        namespace['roots'] = [str(code_link), str(data_root)]
        self.assertEqual(namespace['guarded'](str(code_link / 'file.txt')),
                         str(target / 'file.txt'))

        bad_roots = {
            'parent': workspace,
            '/': Path('/'),
            '/home': Path('/home'),
            '/home/other': Path('/home/other'),
        }
        for name, target_path in bad_roots.items():
            with self.subTest(name=name):
                link = self.root / ('bad_' + name.replace('/', '_').replace('.', '_'))
                link.symlink_to(target_path, target_is_directory=True)
                namespace['roots'] = [str(link)]
                with self.assertRaisesRegex(ValueError, '根目录符号链接目标越界'):
                    namespace['guarded'](str(link / 'anything'))

    def test_old_python_realpath_fallback_keeps_existence_check(self):
        real = self.root / 'real'
        real.mkdir()
        (real / 'file.txt').write_text('ok')
        namespace = self.load()
        namespace['roots'] = [str(real)]
        real_realpath = os.path.realpath

        def old_realpath(path, *args, **kwargs):
            if 'strict' in kwargs:
                raise TypeError("realpath() got an unexpected keyword argument 'strict'")
            return real_realpath(path, *args, **kwargs)

        with mock.patch.object(os.path, 'realpath', side_effect=old_realpath):
            self.assertEqual(namespace['guarded'](str(real / 'file.txt')), str(real / 'file.txt'))
            with self.assertRaisesRegex(ValueError, '路径不存在'):
                namespace['guarded'](str(real / 'missing.txt'))


class DownloadStreamHelperTests(unittest.TestCase):
    def test_remote_stderr_is_included_without_moving_offset(self):
        with tempfile.TemporaryFile() as errors:
            os.write(errors.fileno(), b'remote boom\n')
            self.assertEqual(download_stream.remote_stderr(errors), 'remote boom')
            self.assertEqual(download_stream.remote_error('local failed', errors),
                             'local failed；远端 stderr: remote boom')
            errors.write(b'')  # shared offset remains usable
        with tempfile.TemporaryFile() as empty:
            self.assertEqual(download_stream.remote_error('local failed', empty), 'local failed')


if __name__ == '__main__':
    unittest.main()