"""Workspace-only file tools and resumable byte transfers; Python stdlib only.

REMOTE_SOURCE is sent to python3 -c in memory. No helper is saved on the server.
User values travel solely as JSON on SSH stdin, never in the shell command.
"""
import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import local_connection as connection

MAX_TEXT = 24000
EDIT_LIMIT = 4 * 1024 * 1024
DOWNLOADS = Path(connection.PROJECT_ROOT) / "downloads"
ACTIVE = {"starting", "running"}


class FileToolError(Exception):
    pass


# This source uses only modules already in the remote Python standard library.
REMOTE_SOURCE = r'''
import os, sys, json, stat, hashlib, codecs, difflib
LIMIT = 4 * 1024 * 1024
CAP = 24000
published = False

def under(p, r):
    return p == r or p.startswith(r.rstrip('/') + '/')

def guarded(p):
    if not isinstance(p, str) or not p or '\x00' in p:
        raise ValueError('无效路径')
    if p == '~' or p.startswith('~/'):
        p = roots[0] + p[1:]
    if not p.startswith('/'):
        p = roots[0] + '/' + p
    p = os.path.normpath(p)
    if not any(under(p, r) for r in roots):
        raise ValueError('路径越界：只允许配置的代码、数据和临时目录')
    real = os.path.realpath(p, strict=True)
    if not any(under(real, r) for r in roots):
        raise ValueError('符号链接目标越界')
    for area in ('/home', '/data'):
        if under(real, area) and not any(under(real, r) for r in roots[:2]):
            raise ValueError('禁止访问其他用户目录')
    if any(under(real, r) for r in roots[2:]) and os.stat(real).st_uid != os.getuid():
        raise ValueError('临时文件或目录不属于当前服务器用户')
    return real

def directory(p):
    # Walk the canonical path with O_NOFOLLOW: ancestor replacement cannot
    # redirect an operation through a symlink after realpath validation.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in p.split('/'):
            if part:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise

def opened(p):
    parent = directory(os.path.dirname(p))
    try:
        fd = os.open(os.path.basename(p), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode):
            os.close(fd)
            raise ValueError('只支持普通文件')
        if any(under(p, r) for r in roots[2:]) and s.st_uid != os.getuid():
            os.close(fd)
            raise ValueError('临时文件不属于当前服务器用户')
        return parent, os.fdopen(fd, 'rb')
    except BaseException:
        os.close(parent)
        raise

def identity(s):
    return dict(device=s.st_dev, inode=s.st_ino, size=s.st_size,
                mtime_ns=s.st_mtime_ns, ctime_ns=s.st_ctime_ns)

def meta(s):
    return dict(size=s.st_size, mtime_ns=s.st_mtime_ns)

def same(f, p, parent, before):
    return identity(os.fstat(f.fileno())) == before and identity(os.stat(os.path.basename(p), dir_fd=parent, follow_symlinks=False)) == before

def emit(value):
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode('utf-8') + b'\n')
    sys.stdout.buffer.flush()

def operation(a):
    global published
    p = guarded(a['path'])
    if a.get('scope_root') and not under(p, a['scope_root']):
        raise ValueError('批量读取必须在指定项目真实目录内')
    op = a['operation']
    if op == 'list':
        fd = directory(p)
        try:
            if any(under(p, r) for r in roots[2:]) and os.fstat(fd).st_uid != os.getuid():
                raise ValueError('临时目录不属于当前服务器用户')
            names = sorted(os.listdir(fd))
            offset, limit = a['offset'], a['limit']
            entries = []
            used = 0
            for name in names[offset:offset + limit]:
                s = os.stat(name, dir_fd=fd, follow_symlinks=False)
                kind = 'symlink' if stat.S_ISLNK(s.st_mode) else 'directory' if stat.S_ISDIR(s.st_mode) else 'file' if stat.S_ISREG(s.st_mode) else 'other'
                item = dict(name=name, type=kind)
                if kind != 'directory':
                    item['size'] = s.st_size
                item['modified_time'] = int(s.st_mtime)
                if any(under(p, r) for r in roots[2:]) and s.st_uid != os.getuid():
                    item = dict(name=name, type='restricted')
                cost = len(json.dumps(item, ensure_ascii=False))
                if used + cost > CAP - 2000:
                    break
                entries.append(item)
                used += cost
            nxt = offset + len(entries)
            return dict(path=p, entries=entries, total=len(names), next_offset=nxt if nxt < len(names) else None)
        finally:
            os.close(fd)
    parent, f = opened(p)
    temporary = None
    try:
        s = os.fstat(f.fileno())
        if a.get('require_owner') and s.st_uid != os.getuid():
            raise ValueError('文件不属于当前服务器用户')
        before = identity(s)
        result = dict(path=p, version=meta(s))
        if op == 'read':
            if s.st_size <= LIMIT:
                raw = f.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise ValueError('读取期间文件发生变化')
                try:
                    raw.decode('utf-8')
                except UnicodeDecodeError:
                    raise ValueError('不是 UTF-8 文本，请下载查看')
                if b'\x00' in raw:
                    raise ValueError('二进制文件，请下载查看')
                result['sha256'] = hashlib.sha256(raw).hexdigest()
                f.seek(0)
            else:
                result['sha256'] = None
                sample = f.read(8192)
                if b'\x00' in sample:
                    raise ValueError('二进制文件，请下载查看')
                try:
                    codecs.getincrementaldecoder('utf-8')().decode(sample, final=False)
                except UnicodeDecodeError:
                    raise ValueError('不是 UTF-8 文本，请下载查看')
                f.seek(0)
            result['editable'] = s.st_size <= LIMIT and s.st_uid == os.getuid() and s.st_nlink == 1
            ranges = a['ranges']
            blocks = [dict(start_line=r['start_line'],text='') for r in ranges]
            lines, number, used, range_index, exhausted = [], 0, 0, 0, False
            next_line = None
            while True:
                raw = f.readline(CAP * 4 + 4)
                if not raw:
                    break
                number += 1
                while range_index<len(ranges) and number>=ranges[range_index]['start_line']+ranges[range_index]['max_lines']:
                    range_index+=1
                if range_index==len(ranges):
                    if not a['multi_range']:
                        next_line=number
                    break
                if number < ranges[range_index]['start_line']:
                    if len(raw) > CAP * 4:
                        raise ValueError('遇到超长单行，请下载查看')
                    continue
                if len(raw) > CAP * 4 or b'\x00' in raw:
                    raise ValueError('二进制或超长单行，请下载查看')
                text = raw.decode('utf-8')
                item = dict(line=number, text=text)
                cost = len(json.dumps(text if a['compact'] else item, ensure_ascii=False))
                if cost > a['max_chars']:
                    if not used:
                        raise ValueError('超长单行，请下载查看')
                    if a['multi_range']:
                        blocks[range_index]['next_line'] = number
                    next_line,exhausted = number,True
                    break
                if used + cost > a['max_chars']:
                    if a['multi_range']:
                        blocks[range_index]['next_line'] = number
                    next_line,exhausted = number,True
                    break
                lines.append(item)
                blocks[range_index]['text']+=text
                used += cost
            if exhausted:
                for index in range(range_index+1,len(ranges)):
                    blocks[index]['next_line']=ranges[index]['start_line']
            if not same(f, p, parent, before):
                raise ValueError('读取期间文件发生变化，请重新读取')
            return dict(result, **({'blocks':blocks} if a['compact'] else {'lines':lines}), next_line=next_line)
        if op == 'edit':
            if s.st_uid != os.getuid():
                raise ValueError('只能编辑当前服务器用户拥有的文件')
            if s.st_size > LIMIT:
                raise ValueError('编辑上限为 4 MiB')
            if s.st_nlink != 1:
                raise ValueError('不编辑具有多个硬链接的文件')
            raw = f.read(LIMIT + 1)
            if len(raw) > LIMIT or b'\x00' in raw:
                raise ValueError('文件超限或为二进制')
            digest = hashlib.sha256(raw).hexdigest()
            if digest != a['expected_sha256']:
                raise ValueError('版本冲突：SHA-256 不匹配，原文件未修改')
            text = raw.decode('utf-8')
            crlf = '\r\n' in text and '\n' not in text.replace('\r\n', '') and '\r' not in text.replace('\r\n', '')
            lf = '\r' not in text
            preview, positions, preview_used, preview_truncated = [], [], 0, False
            for edit in a['edits']:
                old, new = edit['old_text'], edit['new_text']
                if crlf:
                    old = old.replace('\r\n', '\n').replace('\n', '\r\n')
                    new = new.replace('\r\n', '\n').replace('\n', '\r\n')
                elif lf:
                    old = old.replace('\r\n', '\n')
                    new = new.replace('\r\n', '\n')
                if not old or text.count(old) != 1:
                    raise ValueError('每项原文本必须唯一匹配，原文件未修改')
                if a['include_diff']:
                    position=text.index(old)
                    if len(positions)<20:
                        positions.append(text[:position].count('\n')+1)
                    else:
                        preview_truncated=True
                    old_preview=old[:4096].splitlines(keepends=True)[:100]
                    new_preview=new[:4096].splitlines(keepends=True)[:100]
                    if len(old)>4096 or len(new)>4096 or old.count('\n')>100 or new.count('\n')>100:
                        preview_truncated=True
                    for index,line in enumerate(difflib.unified_diff(old_preview,new_preview,fromfile='old',tofile='new',n=1)):
                        if index<2: continue
                        if not line.endswith('\n'): line+='\n'
                        cost=len(json.dumps(line,ensure_ascii=False))
                        if preview_used+cost>a['max_chars']:
                            preview_truncated=True;break
                        preview.append(line);preview_used+=cost
                text = text.replace(old, new, 1)
            data = text.encode('utf-8')
            if len(data) > LIMIT or b'\x00' in data:
                raise ValueError('编辑结果超过 4 MiB 或包含二进制内容')
            temporary = '.remote-ws-edit-' + os.urandom(16).hex()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
                out.flush()
                os.fchown(out.fileno(), s.st_uid, s.st_gid)
                os.fchmod(out.fileno(), stat.S_IMODE(s.st_mode))
                os.fsync(out.fileno())
            f.seek(0)
            if not same(f, p, parent, before) or hashlib.sha256(f.read(LIMIT + 1)).hexdigest() != digest:
                raise ValueError('写入前版本冲突，原文件未修改')
            # Optimistic check, not a universal CAS against unrelated editors.
            os.replace(temporary, os.path.basename(p), src_dir_fd=parent, dst_dir_fd=parent)
            published = True
            temporary = None
            os.fsync(parent)
            result=dict(path=p, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
            if a['include_diff']:
                result.update(diff=''.join(preview),change_lines=positions,diff_truncated=preview_truncated)
            return result
        if op == 'download':
            offset = a.get('offset', 0)
            if a.get('identity') is not None and before != a['identity']:
                raise ValueError('源文件发生变化，拒绝续传')
            if offset < 0 or offset > s.st_size:
                raise ValueError('续传偏移无效')
            h = hashlib.sha256()
            remaining = offset
            while remaining:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('源文件发生变化')
                h.update(chunk)
                remaining -= len(chunk)
            if offset and h.hexdigest() != a.get('prefix_sha256'):
                raise ValueError('已下载部分与源文件不一致，拒绝续传')
            if not same(f, p, parent, before):
                raise ValueError('源文件发生变化')
            emit(dict(ok=True, protocol=1, path=p, identity=before, offset=offset, length=s.st_size-offset))
            remaining = s.st_size - offset
            while remaining:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('源文件截短')
                sys.stdout.buffer.write(chunk)
                h.update(chunk)
                remaining -= len(chunk)
            sys.stdout.buffer.flush()
            emit(dict(ok=True, sha256=h.hexdigest(), stable=same(f, p, parent, before)))
            return None
        raise ValueError('未知操作')
    finally:
        if temporary:
            os.unlink(temporary, dir_fd=parent)
        f.close()
        os.close(parent)

try:
    a = json.load(sys.stdin)
    roots = a['roots']
    value = operation(a)
    if value is not None:
        emit(dict(ok=True, **value))
except Exception as error:
    emit(dict(ok=False, error=str(error), uncertain=published))
    sys.exit(1)
'''


def roots_config():
    conf = connection.read_config()
    roots = [conf.get("CODE_ROOT", "/home/wjh"), conf.get("DATA_ROOT", "/data/wjh"),
             "/tmp", "/var/tmp", "/dev/shm"]
    if any(not r.startswith("/") or os.path.normpath(r) != r or r == "/" for r in roots):
        raise FileToolError("配置目录必须是规范的绝对路径，不能为根目录")
    return roots


def request_path(path, roots):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise FileToolError("无效路径")
    if path == "~" or path.startswith("~/"):
        path = roots[0] + path[1:]
    if not path.startswith("/"):
        path = roots[0] + "/" + path
    path = os.path.normpath(path)
    if not any(path == r or path.startswith(r + "/") for r in roots):
        raise FileToolError("路径越界：只允许代码、数据和当前用户的临时目录")
    return path


def ensure_login(server):
    if not connection.is_connected("srv-" + server):
        if os.environ.get("REMOTE_WS_LOGIN_GUI") == "1":
            connection.ensure_connected(server)
        else:
            raise FileToolError("尚未连接，请调用 connect 打开登录窗口，或运行 scripts/connect.sh 登录后重试")


def ssh_command(server, source=None):
    return connection.ssh_args() + ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                    "srv-" + server, "python3 -c " + shlex.quote(source if source is not None else REMOTE_SOURCE)]


def remote_json(server, payload, timeout=120, source=None):
    ensure_login(server)
    try:
        command = ssh_command(server) if source is None else ssh_command(server, source)
        result = subprocess.run(command, input=json.dumps(payload).encode(),
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        message = {'edit': '编辑执行状态不确定，请重新读取确认结果，不得自动重写',
                   'stop': '停止执行状态不确定，请先查询 jobs 确认，不得自动重复停止'}.get(
                       payload['operation'], '读取超时，请先诊断连接')
        raise FileToolError(message) from error
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as error:
        extra = {'edit': '；编辑状态不确定，请重新读取，不自动重写',
                 'stop': '；停止状态不确定，请先查询 jobs，不自动重复停止'}.get(payload['operation'], '')
        classified = connection.classify_ssh_error(result.stderr.decode('utf-8', 'replace'))
        raise FileToolError((f"[{classified[0]}] {classified[1]}" if classified
                             else "远端 Python 不可用或响应中断，请 diagnose") + extra) from error
    if not value.get('ok'):
        message = value.get('error', '远程操作失败')
        if value.get('uncertain'):
            message += ('；停止可能已部分执行，请查询 jobs 确认，不自动重复停止'
                        if payload['operation'] == 'stop' else '；编辑可能已写入，请重新读取确认，不自动重写')
        raise FileToolError(message)
    value.pop('ok', None)
    return value


def integer(args, name, default, low, high):
    value = args.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise FileToolError(f"{name} 必须是 {low} 到 {high} 的整数")
    return value


def file_call(operation, args):
    roots = roots_config()
    payload = dict(operation=operation, roots=roots, path=request_path(args.get('path'), roots))
    if operation == 'list':
        payload.update(offset=integer(args, 'offset', 0, 0, 2**63-1), limit=integer(args, 'limit', 50, 1, 1000))
    elif operation == 'read':
        compact=args.get('compact',True)
        if type(compact) is not bool:
            raise FileToolError('compact 必须是布尔值')
        multi='ranges' in args
        if multi and ('start_line' in args or 'max_lines' in args or not compact):
            raise FileToolError('ranges 不与 start_line/max_lines 或 compact=false 混用')
        ranges=args.get('ranges',[dict(start_line=integer(args,'start_line',1,1,2**63-1),max_lines=integer(args,'max_lines',200,1,1000))])
        if not isinstance(ranges,list) or not 1<=len(ranges)<=8:
            raise FileToolError('ranges 必须含 1–8 个 start_line/max_lines 行段')
        values=[]
        for item in ranges:
            if not isinstance(item,dict) or set(item)!={'start_line','max_lines'}:
                raise FileToolError('行段仅接受 start_line/max_lines')
            values.append(dict(start_line=integer(item,'start_line',1,1,2**63-1),max_lines=integer(item,'max_lines',200,1,1000)))
        if sum(r['max_lines'] for r in values)>2000 or any(
            values[i]['start_line']<values[i-1]['start_line']+values[i-1]['max_lines'] for i in range(1,len(values))):
            raise FileToolError('行段须按顺序且不重叠，总计最多 2000 行')
        payload.update(ranges=values,multi_range=multi,compact=compact,max_chars=integer(args,'max_chars',6000,256,20000))
    elif operation == 'edit':
        edits = args.get('edits')
        digest = args.get('expected_sha256', '')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{64}', digest):
            raise FileToolError('expected_sha256 必须是整文件 SHA-256')
        if not isinstance(edits, list) or not 1 <= len(edits) <= 100 or any(
            not isinstance(e, dict) or not isinstance(e.get('old_text'), str) or not e['old_text']
            or not isinstance(e.get('new_text'), str) for e in edits):
            raise FileToolError('edits 必须含 1–100 项 old_text/new_text 精确替换')
        if sum(len(e['old_text'].encode()) + len(e['new_text'].encode()) for e in edits) > 2 * EDIT_LIMIT:
            raise FileToolError('替换参数过大')
        include=args.get('include_diff',True)
        if type(include) is not bool:
            raise FileToolError('include_diff 必须是布尔值')
        payload.update(edits=edits,expected_sha256=digest.lower(),include_diff=include,
                       max_chars=integer(args,'max_chars',1000,256,6000))
    value = remote_json(connection.resolve_server(args.get('server')), payload)
    text = json.dumps(value, ensure_ascii=False,separators=(',',':'))
    if len(text) > MAX_TEXT:
        raise FileToolError('元数据或输出过长，请缩小分页范围')
    return text


def safe_local(path, create=False):
    """Reject symlinks in every local component, including downloads itself."""
    path = Path(os.path.abspath(path))
    base = Path(connection.PROJECT_ROOT)
    if path == DOWNLOADS or DOWNLOADS not in path.parents:
        raise FileToolError('本地文件必须位于当前工作区 downloads/ 内')
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
    probe = safe_local(DOWNLOADS / '.jobs' / 'probe', create=True)
    return probe.parent


def task_path(task_id):
    if not isinstance(task_id, str) or not re.fullmatch('[a-f0-9]{32}', task_id):
        raise FileToolError('无效任务 ID')
    return safe_local(jobs_dir() / (task_id + '.json'))


def atomic_state(state):
    path = task_path(state['task_id'])
    temp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        with open(temp, 'x', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
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
        raise FileToolError('找不到下载任务') from error
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


def verbosity(args):
    value = args.get('verbose', False)
    if type(value) is not bool:
        raise FileToolError('verbose 必须是布尔值')
    return value


SUMMARY_KEYS = ('task_id','server','remote_path','state','phase','downloaded_bytes','total_size','progress',
                'verified_bytes','verification_progress','speed_bytes_sec','eta_sec','local_path','windows_path','sha256')
# Task rows are a narrow view: id/status/progress only; error message stays.
ROW_KEYS = ('task_id','server','remote_path','state','progress')


def report_state(state, verbose=False):
    size = state.get('total_size')
    count = state.get('downloaded_bytes', 0)
    result = dict(state)
    result['progress'] = (min(100, count * 100 / size) if size else 100 if state['state'] == 'completed' else None)
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
    if (state['state'] == 'completed' and size is not None
            and result.get('verified_bytes') == size):
        result.pop('verified_bytes', None)
    return result


def report(state, verbose=False):
    return json.dumps(report_state(state, verbose), ensure_ascii=False, separators=(',', ':'))


def list_downloads(args):
    verbose = verbosity(args)
    limit = integer(args, 'limit', 10, 1, 100)
    offset = integer(args, 'offset', 0, 0, 2**63-1)
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
    return json.dumps(dict(tasks=rows, total=len(records),
        next_offset=next_offset if next_offset < len(records) else None,
        unreadable_task_ids=unreadable[:100]), ensure_ascii=False, separators=(',', ':'))


def busy_report():
    active = []
    for path in jobs_dir().glob('*.json'):
        if re.fullmatch('[a-f0-9]{32}', path.stem):
            try:
                row = load_state(path.stem)
                if row['state'] in ACTIVE:
                    active.append(report_state(row))
            except (OSError, ValueError, KeyError, TypeError, FileToolError):
                pass
    return json.dumps(dict(state='busy', task_id=active[0]['task_id'] if active else None,
        message='本工作区忙碌，不建立队列'), ensure_ascii=False, separators=(',', ':'))


def active_lock():
    path = safe_local(jobs_dir() / 'active.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def destination(server, remote_path, local_path):
    if local_path:
        if not isinstance(local_path, str) or '\x00' in local_path:
            raise FileToolError('本地路径无效')
        # Accept native Windows paths as well as WSL absolute/workspace paths.
        if re.match(r'^[A-Za-z]:[\\/]', local_path):
            local_path = subprocess.run(['wslpath', '-u', local_path], capture_output=True,
                                        text=True, check=True, timeout=10).stdout.strip()
        p = Path(local_path)
        if not p.is_absolute():
            p = Path(connection.PROJECT_ROOT) / p
    else:
        p = DOWNLOADS / server / remote_path.lstrip('/')
    p = safe_local(p, create=True)
    if '.jobs' in p.relative_to(DOWNLOADS).parts:
        raise FileToolError('不能使用下载任务内部目录 .jobs')
    if os.path.lexists(p):
        raise FileToolError('目标已存在，不自动覆盖')
    return p


def start_worker(state, lock_fd):
    state.update(launcher_pid=os.getpid(), launcher_token=pid_identity(os.getpid()), launch_at=time.time())
    atomic_state(state)
    process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('download_worker.py')),
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


def download_file(args):
    roots = roots_config()
    remote = request_path(args.get('remote_path'), roots)
    server = connection.resolve_server(args.get('server'))
    fd = active_lock()
    if fd is None:
        return busy_report()
    try:
        target = destination(server, remote, args.get('local_path'))
        ensure_login(server)
        task_id = uuid.uuid4().hex
        partial = safe_local(jobs_dir() / (task_id + '.part'))
        with open(partial, 'xb'):
            pass
        state = dict(task_id=task_id, server=server, remote_path=remote, roots=roots,
                     local_path=str(target), partial_path=str(partial), state='starting', phase='starting',
                     downloaded_bytes=0, total_size=None, partial_sha256=hashlib.sha256(b'').hexdigest(),
                     created_at=time.time(), updated_at=time.time(), identity=None)
        return start_worker(state, fd)
    finally:
        os.close(fd)


def download_status(args):
    verbose = verbosity(args)
    wait = integer(args, 'wait_sec', 0, 0, 10)
    deadline = time.monotonic() + wait
    while True:
        state = load_state(args.get('task_id'))
        if state['state'] not in ACTIVE or time.monotonic() >= deadline:
            return report(state, verbose)
        time.sleep(min(.2, max(0, deadline-time.monotonic())))


def download_control(args):
    task_id, action = args.get('task_id'), args.get('action')
    if action not in ('cancel', 'resume'):
        raise FileToolError('action 必须是 cancel 或 resume')
    state = load_state(task_id)
    if action == 'cancel':
        if state['state'] in ACTIVE and alive(state):
            # Signal the worker itself. Its handler closes its SSH child;
            # never signal a master connection or an unverified process group.
            if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
                raise FileToolError('本地 WSL 不支持安全 pidfd 取消，未向数字 PID 发送信号')
            try:
                pidfd = os.pidfd_open(state['pid'])
            except ProcessLookupError:
                return report(load_state(task_id))
            try:
                if alive(state):
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            except ProcessLookupError:
                pass
            finally:
                os.close(pidfd)
            return download_status(dict(task_id=task_id, wait_sec=2))
        return report(state)
    if state['state'] == 'completed':
        raise FileToolError('任务已经完成')
    fd = active_lock()
    if fd is None:
        return busy_report()
    try:
        state = load_state(task_id)
        if state['state'] in ACTIVE:
            raise FileToolError('任务仍在运行')
        safe_local(state['local_path'], create=True)
        if os.path.lexists(state['local_path']):
            raise FileToolError('目标已存在，不自动覆盖')
        safe_local(state['partial_path'])
        ensure_login(state['server'])
        state.update(state='starting', phase='verifying_local', verified_bytes=0,
                     pid=None, pid_token=None, message='后台校验后手动续传',
                     speed_bytes_sec=None, eta_sec=None)
        return start_worker(state, fd)
    finally:
        os.close(fd)


class Cancelled(Exception):
    pass


def worker(task_id, lock_fd):
    """Runs solely in the detached local WSL process, never on the server."""
    state = load_state(task_id)
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
            child = subprocess.Popen(ssh_command(state['server']), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=errors, bufsize=0)
            encoded = memoryview(json.dumps(payload).encode())
            while encoded:
                check_cancel()
                written = child.stdin.write(encoded)
                if not written:
                    raise FileToolError('SSH 参数通道中断')
                encoded = encoded[written:]
            child.stdin.close()

            def header():
                data = bytearray()
                while len(data) < 65536:
                    char = stream_read(1)
                    if not char:
                        raise FileToolError('SSH 字节流中断；可 diagnose 后手动续传')
                    data.extend(char)
                    if char == b'\n':
                        value = json.loads(data)
                        if not value.get('ok'):
                            raise FileToolError(value.get('error', '远端读取失败'))
                        return value
                raise FileToolError('下载元数据头无效')

            head = header()
            if head.get('protocol') != 1 or head['offset'] != state['downloaded_bytes']:
                raise FileToolError('下载协议或偏移无效')
            state.update(identity=head['identity'], total_size=head['identity']['size'], remote_path=head['path'])
            remaining = head['length']
            if remaining != state['total_size'] - state['downloaded_bytes']:
                raise FileToolError('下载长度无效')
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
                    raise FileToolError('传输中断，已保留部分文件；不自动重连')
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
                raise FileToolError('源文件变化或 SHA-256 校验失败，不发布最终文件')
            if child.wait(timeout=15) != 0:
                raise FileToolError('SSH 传输进程异常退出，不发布最终文件')
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
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        snapshot()
        if out:
            out.close()
        os.close(lock_fd)


def tools():
    string = {'type': 'string'}
    server = {**string, 'description': connection.server_arg_desc()}
    path = {**string, 'description': '远端路径，相对 CODE_ROOT'}
    verbose = {'type':'boolean','default':False,'description':'返回详情'}
    def schema(properties, required):
        return dict(type='object', properties=properties, required=required, additionalProperties=False)
    return [
        ('list_files', lambda a: file_call('list', a), '列目录，不递归；默认 50 项，next_offset 续读。',
         schema(dict(path=path, server=server, offset={'type':'integer','minimum':0}, limit={'type':'integer','minimum':1,'maximum':1000}), ['path'])),
        ('read_file', lambda a: file_call('read', a), '读 UTF-8 局部，默认 200 行/6000 字符；blocks 紧凑文本，ranges 多段，保留 SHA-256。',
         schema(dict(path=path,server=server,start_line={'type':'integer','minimum':1},max_lines={'type':'integer','minimum':1,'maximum':1000},
             ranges={'type':'array','minItems':1,'maxItems':8,'items':schema(dict(start_line={'type':'integer','minimum':1},max_lines={'type':'integer','minimum':1,'maximum':1000}),['start_line','max_lines'])},
             max_chars={'type':'integer','minimum':256,'maximum':20000,'default':6000},compact={'type':'boolean','default':True}), ['path'])),
        ('edit_file', lambda a: file_call('edit', a), 'SHA-256 核对且原文本唯一匹配后原子替换；默认返回短 diff，不代替功能验证。',
         schema(dict(path=path,server=server,expected_sha256=string,
             edits={'type':'array','minItems':1,'maxItems':100,'items':schema(dict(old_text=string,new_text=string), ['old_text','new_text'])},
             include_diff={'type':'boolean','default':True},max_chars={'type':'integer','minimum':256,'maximum':6000,'default':1000}), ['path','edits','expected_sha256'])),
        ('download_file', download_file, '后台单文件下载到本地 downloads/，返回 task_id；一项并发，不覆盖。',
         schema(dict(remote_path=path, server=server, local_path=string), ['remote_path'])),
        ('download_status', download_status, '查阶段/进度/路径；仅 completed 为完成。默认摘要省略空值和重复字段，verbose 查详情。',
         schema(dict(task_id=string, wait_sec={'type':'integer','minimum':0,'maximum':10},verbose=verbose), ['task_id'])),
        ('list_downloads', list_downloads, '本地找回任务 ID，新到旧；默认 10 项，只含标识/状态/进度，详情用 download_status，next_offset 续读，无网络。',
         schema(dict(state={'type':'string','enum':sorted(ACTIVE | {'completed','cancelled','interrupted'})},
                     offset={'type':'integer','minimum':0}, limit={'type':'integer','minimum':1,'maximum':100}, verbose=verbose), [])),
        ('download_control', download_control, 'cancel 保留部分文件且不关闭 SSH；resume 后台校验并手动续传。',
         schema(dict(task_id=string, action={'type':'string','enum':['cancel','resume']}), ['task_id','action'])),
    ]
