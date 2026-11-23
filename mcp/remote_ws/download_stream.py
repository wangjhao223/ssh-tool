"""Detached local download streaming and checkpoint loop."""
import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import time

from . import transport
from .download_state import atomic_state, load_state, pid_identity, safe_local
from .sources.files import REMOTE_SOURCE
from .validation import FileToolError


class Cancelled(Exception):
    pass


REMOTE_STDERR_LIMIT = 2000


def remote_stderr(errors, limit=REMOTE_STDERR_LIMIT):
    """Read the subprocess stderr without disturbing the shared file offset."""
    try:
        size = os.fstat(errors.fileno()).st_size
        if size <= 0:
            return ''
        start = max(0, size - limit)
        data = os.pread(errors.fileno(), size - start, start)
    except (OSError, ValueError):
        return ''
    return data.decode('utf-8', 'replace').strip()


def remote_error(message, errors):
    detail = remote_stderr(errors)
    return f'{message}；远端 stderr: {detail}' if detail else message


def _close_quietly(stream):
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def worker(task_id, lock_fd, *, command=None):
    """Runs solely in the detached local WSL process, never on the server."""
    if command is None:
        command = lambda server: transport.ssh_command(server, REMOTE_SOURCE)
    try:
        state = load_state(task_id)
    except BaseException:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        raise
    state.update(pid=os.getpid(), pid_token=pid_identity(os.getpid()), state='running',
                 phase='verifying_local', verified_bytes=0, speed_bytes_sec=None, eta_sec=None)
    child, out = None, None
    h = hashlib.sha256()
    last = time.monotonic()
    prefix_verified = False
    cancel_requested = False

    def snapshot():
        if out:
            out.flush()
            os.fsync(out.fileno())
        if prefix_verified:
            state['partial_sha256'] = h.hexdigest()
        state['updated_at'] = time.time()
        atomic_state(state)

    def cancel(signum, frame):
        # A flag keeps a signal from splitting write/hash/byte-count updates.
        # Pipe reads use select so even a stalled SSH channel is cancellable.
        nonlocal cancel_requested
        cancel_requested = True

    def check_cancel():
        if cancel_requested:
            raise Cancelled()

    def stream_read(length):
        while True:
            check_cancel()
            ready, _, _ = select.select([child.stdout], [], [], .2)
            if ready:
                return os.read(child.stdout.fileno(), length)

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    atomic_state(state)
    try:
        target = safe_local(state['local_path'], create=True)
        partial = safe_local(state['partial_path'])
        if target.exists():
            raise FileToolError('目标已存在，不自动覆盖')
        count = state['downloaded_bytes']
        with open(partial, 'rb') as f:
            remaining_prefix = count
            while remaining_prefix:
                check_cancel()
                chunk = f.read(min(1024*1024, remaining_prefix))
                if not chunk:
                    raise FileToolError('部分文件截短，拒绝续传')
                h.update(chunk)
                remaining_prefix -= len(chunk)
                state['verified_bytes'] = count - remaining_prefix
                if time.monotonic() - last >= .5:
                    snapshot()
                    last = time.monotonic()
        if h.hexdigest() != state['partial_sha256']:
            raise FileToolError('部分文件损坏，拒绝续传')
        prefix_verified = True
        # Truncate uncheckpointed crash bytes only after validating the saved
        # prefix, once in the worker; the MCP request performs no large reads.
        with open(partial, 'r+b') as f:
            f.truncate(count)
        state.update(phase='verifying_remote', message='核对源文件身份及已下载部分')
        snapshot()
        payload = dict(operation='download', path=state['remote_path'], roots=state['roots'],
                       offset=state['downloaded_bytes'], identity=state['identity'], prefix_sha256=h.hexdigest())
        # Capture stderr to a local temporary file to avoid pipe backpressure.
        with tempfile.TemporaryFile() as errors:
            child = subprocess.Popen(command(state['server']), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=errors, bufsize=0)
            encoded = memoryview(json.dumps(payload).encode())
            try:
                while encoded:
                    check_cancel()
                    written = child.stdin.write(encoded)
                    if not written:
                        raise FileToolError(remote_error('SSH 参数通道中断', errors))
                    encoded = encoded[written:]
                child.stdin.close()
            except OSError as error:
                raise FileToolError(remote_error(f'SSH 参数通道中断: {error}', errors)) from error

            def header():
                data = bytearray()
                while len(data) < 65536:
                    char = stream_read(1)
                    if not char:
                        raise FileToolError(remote_error(
                            'SSH 字节流中断；可用 connection_query(action=diagnose) 检查后手动续传', errors))
                    data.extend(char)
                    if char == b'\n':
                        try:
                            value = json.loads(data)
                        except ValueError:
                            raise FileToolError(remote_error('下载元数据头无效', errors)) from None
                        if not value.get('ok'):
                            raise FileToolError(remote_error(str(value.get('error') or '远端读取失败'), errors))
                        return value
                raise FileToolError(remote_error('下载元数据头无效', errors))

            head = header()
            if head.get('protocol') != 1 or head.get('offset') != state['downloaded_bytes']:
                raise FileToolError(remote_error('下载协议或偏移无效', errors))
            state.update(identity=head['identity'], total_size=head['identity']['size'], remote_path=head['path'])
            remaining = head['length']
            if remaining != state['total_size'] - state['downloaded_bytes']:
                raise FileToolError(remote_error('下载长度无效', errors))
            if shutil.disk_usage(partial.parent).free < remaining + 1024*1024:
                raise FileToolError('本地磁盘空间不足，未开始接收文件字节')
            out = open(partial, 'ab')
            state.update(phase='transferring', message='传输中')
            transfer_start, transfer_base = time.monotonic(), state['downloaded_bytes']
            rate_start, rate_base = transfer_start, transfer_base
            snapshot()
            while remaining:
                chunk = stream_read(min(1024*1024, remaining))
                if not chunk:
                    raise FileToolError(remote_error('传输中断，已保留部分文件；不自动重连', errors))
                out.write(chunk)
                h.update(chunk)
                state['downloaded_bytes'] += len(chunk)
                remaining -= len(chunk)
                if time.monotonic() - last >= .5:
                    now = time.monotonic()
                    rate = (state['downloaded_bytes'] - rate_base) / max(now-rate_start, .001)
                    prior = state.get('speed_bytes_sec')
                    rate = rate if prior is None else prior*.6 + rate*.4
                    state.update(speed_bytes_sec=round(rate, 1),
                                 eta_sec=round(remaining/rate, 1) if rate > 0 else None)
                    snapshot()
                    last, rate_start, rate_base = now, now, state['downloaded_bytes']
            state.update(phase='verifying_final', message='核对完整 SHA-256 和源文件版本',
                         speed_bytes_sec=None, eta_sec=None)
            snapshot()
            trailer = header()
            if not trailer.get('stable') or trailer.get('sha256') != h.hexdigest():
                raise FileToolError(remote_error('源文件变化或 SHA-256 校验失败，不发布最终文件', errors))
            if child.wait(timeout=15) != 0:
                raise FileToolError(remote_error('SSH 传输进程异常退出，不发布最终文件', errors))
            check_cancel()
            snapshot()
            out.close()
            out = None
            # The partial is always inside .jobs. Create final with hard-link
            # O_EXCL semantics so a late competing file is never overwritten.
            safe_local(target)
            os.link(partial, target, follow_symlinks=False)
            state.update(state='completed', phase='completed', sha256=h.hexdigest(), message='校验通过，下载完成')
            try:
                state['windows_path'] = subprocess.run(['wslpath', '-w', str(target)],
                    capture_output=True, text=True, check=True, timeout=10).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                state['windows_path'] = None
            snapshot()
            try:
                partial.unlink()
            except OSError:
                pass  # Valid final file and completed task remain authoritative.
    except Cancelled:
        state.update(state='cancelled', message='用户取消，已保留部分文件；可手动续传')
    except Exception as error:
        state.update(state='interrupted', message=str(error))
    finally:
        # Ignore additional cancellation while committing the last checkpoint.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            try:
                if child and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
            finally:
                try:
                    snapshot()
                finally:
                    _close_quietly(out)
                    if child:
                        _close_quietly(child.stdin)
                        _close_quietly(child.stdout)
        finally:
            os.close(lock_fd)
