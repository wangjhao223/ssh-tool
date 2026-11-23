"""Task-scoped download records, locks, process identity and local verification."""
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path

import local_connection as connection
from .validation import FileToolError


ACTIVE = {"starting", "running"}
TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TASK_ID_RE = re.compile(r"^[a-f0-9]{32}$")
TASK_ID_HINT = 'task_id 必须是完整 32 位小写十六进制下载 ID；可先用 download_query(action="list") 查询当前下载池'
SUMMARY_KEYS = ('task_id','server','remote_path','state','phase','downloaded_bytes','total_size','progress',
                'verified_bytes','verification_progress','speed_bytes_sec','eta_sec','local_path','windows_path','sha256')
ROW_KEYS = ('task_id','server','remote_path','state','progress')


def task_name():
    value = os.environ.get("REMOTE_WS_TASK", "default")
    if not TASK_RE.fullmatch(value):
        raise FileToolError("REMOTE_WS_TASK 只能包含字母、数字、_ . -，且最多 64 字符")
    return value


def downloads_dir():
    task = task_name()
    connection.ensure_user_dir(connection.DOWNLOAD_ROOT)
    return Path(connection.ensure_user_dir(os.path.join(connection.DOWNLOAD_ROOT, task)))


def safe_local(path, create=False):
    """Reject symlinks in every component below the current task download directory."""
    path = Path(os.path.abspath(path))
    base = downloads_dir()
    if path == base or base not in path.parents:
        raise FileToolError('本地文件必须位于当前任务的下载目录内')
    current = base
    for part in path.relative_to(base).parts[:-1]:
        current = current / part
        if create:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
        s = current.lstat()
        if not stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode):
            raise FileToolError('本地目录不可使用符号链接或非目录')
    if path.is_symlink():
        raise FileToolError('本地文件不可使用符号链接')
    return path


def jobs_dir():
    probe = safe_local(downloads_dir() / '.jobs' / 'probe', create=True)
    return probe.parent


def task_path(task_id):
    if not isinstance(task_id, str) or not task_id:
        raise FileToolError('task_id 不能为空；' + TASK_ID_HINT)
    if not TASK_ID_RE.fullmatch(task_id):
        raise FileToolError(TASK_ID_HINT)
    return safe_local(jobs_dir() / (task_id + '.json'))


def atomic_state(state, *, sync=None, replace=None):
    sync = os.fsync if sync is None else sync
    replace = os.replace if replace is None else replace
    path = task_path(state['task_id'])
    temp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        with open(temp, 'x', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
            f.flush()
            sync(f.fileno())
        replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def pid_identity(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        fields = text.rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


def alive(state):
    return bool(state.get('pid_token') and pid_identity(state.get('pid')) == state['pid_token'])


def load_state(task_id):
    try:
        path = task_path(task_id)
        with open(path, encoding='utf-8') as f:
            state = json.load(f)
    except FileNotFoundError as error:
        raise FileToolError(
            f'找不到下载任务 {task_id}；请用 download_query(action="list") 确认当前下载池和完整 task_id') from error
    if (not isinstance(state, dict) or state.get('task_id') != task_id or
        state.get('state') not in ACTIVE | {'completed','cancelled','interrupted'} or
        type(state.get('downloaded_bytes')) is not int or state['downloaded_bytes'] < 0 or
        not isinstance(state.get('created_at', 0), (int, float))):
        raise FileToolError('下载记录结构损坏')
    launching = (state['state'] == 'starting' and not state.get('pid') and
                 state.get('launcher_token') and pid_identity(state.get('launcher_pid')) == state['launcher_token'] and
                 time.time() - state.get('launch_at', 0) < 10)
    if state['state'] in ACTIVE and not alive(state) and not launching:
        # Do not persist from a reader: a newly started worker could be writing.
        state['state'] = 'interrupted'
        state['message'] = '本地后台进程已退出；保留部分文件，可手动续传'
    return state


def integer(args, name, default, low, high):
    value = args.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise FileToolError(f"{name} 必须是 {low} 到 {high} 的整数")
    return value


def verbosity(args):
    value = args.get('verbose', False)
    if type(value) is not bool:
        raise FileToolError('verbose 必须是布尔值')
    return value


def _percent(value):
    value = round(value, 1)
    return int(value) if value == int(value) else value


def report_state(state, verbose=False):
    size = state.get('total_size')
    count = state.get('downloaded_bytes', 0)
    result = dict(state)
    result['progress'] = (_percent(min(100, count * 100 / size)) if size
                          else 100 if state['state'] == 'completed' else None)
    result.pop('pid_token', None)
    result.pop('roots', None)
    result.pop('launcher_token', None)
    result.pop('launcher_pid', None)
    result.pop('launch_at', None)
    result['verification_progress'] = (round(state.get('verified_bytes', 0)*100/count, 1)
        if state.get('phase') == 'verifying_local' and count else None)
    if state['state'] not in ACTIVE or state.get('phase') != 'transferring':
        result['speed_bytes_sec'] = None
        result['eta_sec'] = None
    elif time.time() - state.get('updated_at', 0) > 2:
        result['speed_bytes_sec'] = 0
        result['eta_sec'] = None
    if not verbose:
        result = {key: result[key] for key in SUMMARY_KEYS if key in result}
        if state['state'] in ('interrupted', 'cancelled') and state.get('message'):
            result['message'] = state['message']
    result = {key: value for key, value in result.items() if value is not None}
    if state['state'] == result.get('phase'):
        result.pop('phase', None)
    if state['state'] == 'completed':
        # verified_bytes only describes prefix validation during resume; it is
        # misleading once the full SHA-256 has been verified and published.
        result.pop('verified_bytes', None)
        result.pop('verification_progress', None)
    return result


def report(state, verbose=False):
    return json.dumps(report_state(state, verbose), ensure_ascii=False, separators=(',', ':'))


def verify_local_file(state):
    if state['state'] != 'completed':
        return {'status': 'not_completed', 'state': state['state'],
                'message': '本地核验仅在 completed 后可用；任务尚未完成'}
    try:
        path = safe_local(state['local_path'])
    except FileNotFoundError:
        return {'status': 'missing'}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'status': 'missing'}
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            return {'status': 'not_regular_file'}
        expected_size = state.get('total_size')
        if expected_size is not None and before.st_size != expected_size:
            return {'status': 'size_mismatch', 'actual_size': before.st_size,
                    'expected_size': expected_size}
        expected_hash = state.get('sha256')
        if not expected_hash:
            return {'status': 'present', 'size': before.st_size}
        digest = hashlib.sha256()
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        current = os.stat(path, follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, key) != getattr(after, key) or getattr(before, key) != getattr(current, key)
               for key in fields):
            return {'status': 'changed_during_check'}
        actual_hash = digest.hexdigest()
        if actual_hash != expected_hash:
            return {'status': 'sha256_mismatch', 'actual_sha256': actual_hash,
                    'expected_sha256': expected_hash}
        return {'status': 'verified', 'size': before.st_size, 'sha256': actual_hash}


def busy_report(task_id, message=None):
    return json.dumps(dict(state='busy', task_id=task_id,
        message=message or '该下载任务忙碌，不建立队列'), ensure_ascii=False, separators=(',', ':'))


def active_lock(task_id):
    path = safe_local(task_path(task_id).with_suffix('.lock'))
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def control_lock(task_id):
    """Serialize start/cancel/resume decisions without taking the worker lock."""
    path = safe_local(task_path(task_id).with_suffix('.control'))
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR):
            raise FileToolError('控制锁文件不安全：不可使用符号链接、目录或其他文件类型') from error
        raise
    busy = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise FileToolError('控制锁文件不安全：必须是当前用户拥有的普通文件')
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            busy = True
    except BaseException:
        os.close(fd)
        raise
    if busy:
        os.close(fd)
        return None
    return fd
