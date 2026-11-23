"""Download destination selection, detached launch and request handlers."""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config, transport
from .download_state import (
    ACTIVE, ROW_KEYS, TASK_ID_HINT, active_lock, alive, atomic_state, busy_report,
    control_lock, downloads_dir, integer, jobs_dir, load_state, pid_identity, report,
    report_state, safe_local, task_path, verbosity, verify_local_file,
)
from .paths import request_path, roots_config
from .validation import FileToolError


MAX_TEXT = 24000
CANCEL_HANDOFF_WAIT = 2
CONTROL_BUSY = '该下载任务的控制操作正在进行，未建立队列；请稍后重试'


def destination(server, remote_path, local_path):
    base = downloads_dir()
    if local_path:
        if not isinstance(local_path, str) or '\x00' in local_path:
            raise FileToolError('本地路径无效')
        # Accept native Windows paths as well as WSL absolute/task-download paths.
        if re.match(r'^[A-Za-z]:[\\/]', local_path):
            local_path = subprocess.run(['wslpath', '-u', local_path], capture_output=True,
                                        text=True, check=True, timeout=10).stdout.strip()
        p = Path(local_path)
        if not p.is_absolute():
            p = base / p
    else:
        p = base / server / remote_path.lstrip('/')
    p = safe_local(p, create=True)
    if '.jobs' in p.relative_to(base).parts:
        raise FileToolError('不能使用下载任务内部目录 .jobs')
    if os.path.lexists(p):
        raise FileToolError('目标已存在，不自动覆盖')
    return p


def start_worker(state, lock_fd):
    state.update(launcher_pid=os.getpid(), launcher_token=pid_identity(os.getpid()), launch_at=time.time())
    atomic_state(state)
    process = subprocess.Popen([sys.executable, '-B', str(Path(config.PROJECT_ROOT) / 'mcp' / 'download_worker.py'),
                                state['task_id'], str(lock_fd)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(lock_fd,))
    # Worker initializes its PID state. Wait only for the local handoff, never
    # for remote metadata or the actual download.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with open(task_path(state['task_id']), encoding='utf-8') as f:
            current = json.load(f)
        if current.get('pid') == process.pid:
            return report(current)
        if process.poll() is not None:
            raise FileToolError('本地下载进程启动失败；任务状态保留')
        time.sleep(.02)
    return report(state)


def download_file(args, *, login=None, launch_worker=None, resolve_roots=None, resolve_path=None):
    login = transport.ensure_login if login is None else login
    launch_worker = start_worker if launch_worker is None else launch_worker
    resolve_roots = roots_config if resolve_roots is None else resolve_roots
    resolve_path = request_path if resolve_path is None else resolve_path
    context = config.resolve_context(args.get('server'))
    server = context['SERVER']
    roots = resolve_roots(context=context)
    remote = resolve_path(args.get('remote_path'), roots)
    task_id = uuid.uuid4().hex
    control_fd = control_lock(task_id)
    if control_fd is None:
        return busy_report(task_id, CONTROL_BUSY)
    try:
        fd = active_lock(task_id)
        if fd is None:
            return busy_report(task_id)
        try:
            target = destination(server, remote, args.get('local_path'))
            login(server)
            partial = safe_local(jobs_dir() / (task_id + '.part'))
            with open(partial, 'xb'):
                pass
            state = dict(task_id=task_id, server=server, remote_path=remote, roots=roots,
                         local_path=str(target), partial_path=str(partial), state='starting', phase='starting',
                         downloaded_bytes=0, total_size=None, partial_sha256=hashlib.sha256(b'').hexdigest(),
                         created_at=time.time(), updated_at=time.time(), identity=None)
            return launch_worker(state, fd)
        finally:
            os.close(fd)
    finally:
        os.close(control_fd)


def _merge_note(value, notes):
    if not notes:
        return value
    existing = value.get('parameter_note')
    merged = ([existing] if isinstance(existing, str) and existing else []) + list(notes)
    value['parameter_note'] = '；'.join(merged)
    return value


def _with_note(text, notes):
    if not notes:
        return text
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(value, dict):
        return text
    _merge_note(value, notes)
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def download_query(args):
    if not isinstance(args, dict) or 'action' not in args:
        raise FileToolError('action 必填，且必须是 list 或 status')
    action = args['action']
    if not isinstance(action, str):
        raise FileToolError('action 必须是 list 或 status')
    types = dict(state=str, offset=int, limit=int, task_id=str, wait_sec=int,
                 verbose=bool, verify_local=bool, server=str)
    for key, value in args.items():
        if key in types and type(value) is not types[key]:
            raise FileToolError(f'{key} 类型无效')
    if action == 'list':
        allowed = {'action', 'state', 'offset', 'limit', 'verbose', 'server',
                   'task_id', 'wait_sec', 'verify_local'}
        if set(args) - allowed:
            raise FileToolError(
                'action=list 仅接受 state/offset/limit/verbose/server 及兼容参数 '
                'task_id=""/wait_sec=0/verify_local=false')
        return list_downloads({key: value for key, value in args.items() if key != 'action'})
    if action == 'status':
        allowed = {'action', 'task_id', 'wait_sec', 'verbose', 'verify_local', 'server'}
        if set(args) - allowed:
            raise FileToolError('action=status 仅接受 task_id/wait_sec/verbose/verify_local/server')
        if 'task_id' not in args:
            raise FileToolError('action=status 时 task_id 必填；' + TASK_ID_HINT)
        return download_status({key: value for key, value in args.items() if key != 'action'})
    raise FileToolError('action 必须是 list 或 status')


def download_status(args):
    verbose = verbosity(args)
    notes = []
    if 'server' in args:
        if not isinstance(args['server'], str):
            raise FileToolError('server 必须是字符串')
        notes.append('server 已忽略：status 使用记录中的服务器快照')
    wait = args.get('wait_sec', 0)
    if type(wait) is not int or wait < 0:
        raise FileToolError('wait_sec 必须是非负整数')
    if wait > 10:
        wait = 10
        notes.append('wait_sec 超过 10，已截为 10 秒')
    verify = args.get('verify_local', False)
    if type(verify) is not bool:
        raise FileToolError('verify_local 必须是布尔值')
    deadline = time.monotonic() + wait
    while True:
        state = load_state(args.get('task_id'))
        if state['state'] not in ACTIVE or time.monotonic() >= deadline:
            result = report_state(state, verbose)
            if verify:
                result['local_verification'] = verify_local_file(state)
            _merge_note(result, notes)
            return json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        time.sleep(min(.2, max(0, deadline-time.monotonic())))


def list_downloads(args):
    verbose = verbosity(args)
    limit = args.get('limit', 5)
    if type(limit) is not int or limit < 1:
        raise FileToolError('limit 必须是正整数')
    offset = integer(args, 'offset', 0, 0, 2**63-1)
    notes = []
    if limit > 100:
        limit = 100
        notes.append('limit 超过 100，已截为 100')
    if 'server' in args:
        value = args['server']
        if not isinstance(value, str):
            raise FileToolError('server 必须是字符串')
        notes.append('server 已忽略：list 使用本地下载池，不切换服务器')
    if 'task_id' in args:
        value = args['task_id']
        if not isinstance(value, str):
            raise FileToolError('task_id 必须是字符串')
        if value:
            raise FileToolError('action=list 的 task_id 仅兼容空字符串；查询单个任务请用 action=status')
        notes.append('task_id="" 已忽略：list 不按任务过滤')
    if 'wait_sec' in args:
        value = args['wait_sec']
        if type(value) is not int:
            raise FileToolError('wait_sec 必须是整数')
        if value != 0:
            raise FileToolError('action=list 仅兼容 wait_sec=0；等待请用 action=status')
        notes.append('wait_sec=0 已忽略：list 不等待')
    if 'verify_local' in args:
        value = args['verify_local']
        if type(value) is not bool:
            raise FileToolError('verify_local 必须是布尔值')
        if value:
            raise FileToolError('action=list 不支持 verify_local=true；请对单个 completed 任务使用 action=status')
        notes.append('verify_local=false 已忽略：list 不核验本地文件')
    wanted = args.get('state')
    if wanted is not None and wanted not in ACTIVE | {'completed', 'cancelled', 'interrupted'}:
        raise FileToolError('无效下载状态')
    records, unreadable = [], []
    for path in jobs_dir().glob('*.json'):
        if not re.fullmatch('[a-f0-9]{32}', path.stem):
            continue
        try:
            state = load_state(path.stem)
            if wanted is None or state['state'] == wanted:
                records.append(state)
        except (OSError, ValueError, KeyError, TypeError, FileToolError):
            unreadable.append(path.stem)
    records.sort(key=lambda s: (s.get('created_at', 0), s['task_id']), reverse=True)
    rows = []
    for state in records[offset:offset+limit]:
        row = report_state(state, verbose)
        keys = ROW_KEYS + (('created_at','updated_at') if verbose else ())
        projected = {k: row[k] for k in keys if k in row}
        if state['state'] in ('interrupted', 'cancelled') and row.get('message'):
            projected['message'] = row['message']
        rows.append(projected)
        if len(json.dumps(rows, ensure_ascii=False)) > MAX_TEXT - 2000:
            rows.pop()
            break
    next_offset = offset + len(rows)
    value = dict(tasks=rows, total=len(records),
        next_offset=next_offset if next_offset < len(records) else None,
        unreadable_task_ids=unreadable[:100])
    _merge_note(value, notes)
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def download_control(args, *, login=None, launch_worker=None, resolve_roots=None, resolve_path=None):
    if not isinstance(args, dict):
        raise FileToolError('下载控制参数必须是对象')
    action = args.get('action')
    if not isinstance(action, str) or action not in ('start', 'cancel', 'resume'):
        raise FileToolError('action 必填，且必须是 start/cancel/resume')
    notes = []
    if action == 'start' and type(args.get('task_id')) is str and args['task_id'] == '':
        args = args.copy()
        del args['task_id']
        notes.append('task_id="" 已忽略：start 不使用既有下载 ID')
    allowed = ({'action', 'remote_path', 'server', 'local_path',
                'verbose', 'wait_sec', 'verify_local'} if action == 'start'
               else {'action', 'task_id', 'server', 'remote_path', 'local_path',
                     'verbose', 'wait_sec', 'verify_local'})
    if set(args) - allowed:
        raise FileToolError(f'action={action} 不接受其他分支或未知参数')
    required = 'remote_path' if action == 'start' else 'task_id'
    if required not in args:
        raise FileToolError(f'action={action} 时 {required} 必填')
    string_keys = {'action', 'remote_path', 'server', 'local_path', 'task_id'}
    for key, value in args.items():
        if key in string_keys:
            if not isinstance(value, str):
                raise FileToolError(f'{key} 必须是字符串')
        elif key in ('verbose', 'verify_local'):
            if type(value) is not bool:
                raise FileToolError(f'{key} 必须是布尔值')
        elif key == 'wait_sec':
            if type(value) is not int:
                raise FileToolError('wait_sec 必须是整数')
    if not args[required] or '\x00' in args[required]:
        if required == 'task_id':
            raise FileToolError('task_id 不能为空；' + TASK_ID_HINT)
        raise FileToolError(f'{required} 无效')
    for key, expected in (('verbose', False), ('wait_sec', 0), ('verify_local', False)):
        if key not in args:
            continue
        expected_text = 'false' if type(expected) is bool else str(expected)
        value = args[key]
        value_text = str(value).lower() if type(value) is bool else str(value)
        if value != expected:
            raise FileToolError(
                f'action={action} 仅兼容中性值 {key}={expected_text}，收到 {key}={value_text}')
        notes.append(f'{key}={expected_text} 已忽略：download_control 不使用该参数')
    if action == 'start':
        payload = {key: value for key, value in args.items()
                   if key not in ('action', 'verbose', 'wait_sec', 'verify_local')}
        return _with_note(download_file(payload, login=login, launch_worker=launch_worker,
                                       resolve_roots=resolve_roots, resolve_path=resolve_path), notes)
    for key in ('remote_path', 'local_path'):
        if key in args:
            if args[key]:
                raise FileToolError(f'action={action} 不接受非空 {key}；请使用记录中的路径快照')
            notes.append(f'{key}="" 已忽略：使用记录中的路径快照')
    if 'server' in args:
        notes.append(f'server 已忽略：{action} 使用记录中的服务器快照')
    login = transport.ensure_login if login is None else login
    launch_worker = start_worker if launch_worker is None else launch_worker
    task_id = args['task_id']
    control_fd = control_lock(task_id)
    if control_fd is None:
        return _with_note(busy_report(task_id, CONTROL_BUSY), notes)
    try:
        state = load_state(task_id)
        if action == 'cancel':
            if state['state'] not in ACTIVE:
                return _with_note(report(state), notes)
            if not alive(state):
                # A freshly started task may still be handing the lock to its
                # detached worker. Wait briefly for a verifiable PID before deciding.
                deadline = time.monotonic() + CANCEL_HANDOFF_WAIT
                while time.monotonic() < deadline:
                    time.sleep(min(.05, max(0, deadline - time.monotonic())))
                    state = load_state(task_id)
                    if state['state'] not in ACTIVE or alive(state):
                        break
                if state['state'] not in ACTIVE:
                    return _with_note(report(state), notes)
                if not alive(state):
                    result = report_state(state)
                    result['cancelled'] = False
                    result['cancel_requested'] = False
                    result['message'] = ('未取消：任务仍处于 starting，尚无已核验的下载进程 PID；'
                                         '请稍后用 download_query(action="status") 确认状态再重试 cancel')
                    _merge_note(result, notes)
                    return json.dumps(result, ensure_ascii=False, separators=(',', ':'))
            # Signal the worker itself. Its handler closes its SSH child;
            # never signal a master connection or an unverified process group.
            if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
                raise FileToolError('本地 WSL 不支持安全 pidfd 取消，未向数字 PID 发送信号；任务仍在运行')
            if not isinstance(state.get('pid'), int):
                raise FileToolError('任务记录缺少可核验的整数 PID，未发送取消信号；任务仍在运行')
            try:
                pidfd = os.pidfd_open(state['pid'])
            except ProcessLookupError:
                return _with_note(report(load_state(task_id)), notes)
            sent = False
            try:
                if alive(state):
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                    sent = True
            except ProcessLookupError:
                sent = False
            finally:
                os.close(pidfd)
            result = json.loads(download_status(dict(task_id=task_id, wait_sec=2)))
            if result.get('state') in ACTIVE:
                if sent:
                    result['cancel_requested'] = True
                    result['message'] = '已发送取消信号，下载进程尚未退出；请稍后重新查询状态，勿重复发送'
                else:
                    result['cancelled'] = False
                    result['cancel_requested'] = False
                    result['message'] = ('未发送取消信号：下载进程已不可核验或已退出；'
                                         '请稍后重新查询状态')
            _merge_note(result, notes)
            return json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        if state['state'] == 'completed':
            raise FileToolError('任务已经完成')
        fd = active_lock(task_id)
        if fd is None:
            return _with_note(busy_report(task_id), notes)
        try:
            state = load_state(task_id)
            if state['state'] in ACTIVE:
                raise FileToolError('任务仍在运行')
            safe_local(state['local_path'], create=True)
            if os.path.lexists(state['local_path']):
                raise FileToolError('目标已存在，不自动覆盖')
            partial = safe_local(state['partial_path'])
            if not partial.is_file():
                raise FileToolError('部分文件缺失，无法续传；请重新开始下载')
            if partial.stat().st_size < state['downloaded_bytes']:
                raise FileToolError('部分文件小于已记录进度，无法续传；请重新开始下载')
            login(state['server'])
            state.update(state='starting', phase='verifying_local', verified_bytes=0,
                         pid=None, pid_token=None, message='后台校验后手动续传',
                         speed_bytes_sec=None, eta_sec=None)
            return _with_note(launch_worker(state, fd), notes)
        finally:
            os.close(fd)
    finally:
        os.close(control_fd)
