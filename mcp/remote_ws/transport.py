"""Bounded shell SSH execution and in-memory Python JSON transport."""
import codecs
import json
import os
import selectors
import shlex
import subprocess
import threading
import time

import local_connection as connection
from remote_ws.validation import NOT_RUN, UNKNOWN, FileToolError, ToolError

MAX_OUTPUT = 30000
MAX_JSON_BYTES = 2 * 1024 * 1024
JSON_STDERR_BYTES = 64 * 1024
HOST_PREFIX = "srv-"
CONNECT_SCRIPT = os.path.join(connection.PROJECT_ROOT, "scripts", "connect.sh")


class ResponseTooLargeError(Exception):
    def __init__(self, limit):
        super().__init__(limit)
        self.limit = limit


def connect_cmd(name):
    return f"bash {shlex.quote(CONNECT_SCRIPT)} {shlex.quote(name)}"


def not_connected_hint(name):
    return (f"未连接 {HOST_PREFIX}{name}。请在 WSL 运行 {connect_cmd(name)} 登录；"
            f"密码仅在登录终端输入。")


def truncate(text, limit=MAX_OUTPUT):
    if len(text) <= limit:
        return text
    marker = f"\n…[截断 {len(text) - limit} 字符，可重定向到文件分段读]…\n"
    available = max(0, limit - len(marker))
    head = available // 3
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


class LimitedText:
    """Bounded Unicode head/tail while continuing to drain both pipes."""
    def __init__(self, limit):
        self.limit, self.count, self.head, self.tail = limit, 0, '', ''
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')

    def feed(self, data, final=False):
        text = self.decoder.decode(data, final=final)
        self.count += len(text)
        head = self.limit // 3
        self.head += text[:max(0, head-len(self.head))]
        self.tail = (self.tail + text)[-(self.limit-head):]

    def render(self, limit):
        if not self.count:
            return ''
        if self.count <= limit:
            remaining = self.count-len(self.head)
            return self.head + (self.tail[-remaining:] if remaining else '')
        marker = f'\n[省略约 {self.count-limit} 字符]\n'
        available = max(0, limit-len(marker))
        head = available // 3
        tail = available-head
        return self.head[:head] + marker + (self.tail[-tail:] if tail else '')


def bounded_run(cmd, timeout, limit):
    streams = [LimitedText(limit), LimitedText(limit)]
    start = time.monotonic()
    child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, bufsize=0)
    def output():
        out, err = streams
        return (out.render(limit-min(err.count, limit//2)),
                err.render(limit-min(out.count, limit//2)))
    try:
        with selectors.DefaultSelector() as selector:
            for stream, buffer in zip((child.stdout, child.stderr), streams):
                selector.register(stream, selectors.EVENT_READ, buffer)
            while selector.get_map():
                remaining = timeout-(time.monotonic()-start)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                for key, _ in selector.select(min(.2, remaining)):
                    data = os.read(key.fd, 65536)
                    key.data.feed(data, final=not data)
                    if not data:
                        selector.unregister(key.fileobj)
            rc = child.wait(timeout=max(.001, timeout-(time.monotonic()-start)))
        return rc, *output()
    except subprocess.TimeoutExpired:
        out, err = output()
        raise subprocess.TimeoutExpired(cmd, timeout, output=out.encode(), stderr=err.encode())
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
        child.stderr.close()


def ssh_run(name, script, timeout, max_output_chars=None):
    host = HOST_PREFIX + name
    if not connection.is_connected(host) and os.environ.get("REMOTE_WS_LOGIN_GUI") == "1":
        try:
            connection.ensure_connected(name)
        except connection.LoginError as error:
            raise ToolError(str(error), code=error.code or "CONNECTION_SETUP", status=NOT_RUN) from error
    cmd = connection.ssh_args() + ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "bash -c " + shlex.quote(script)]
    start = time.time()
    try:
        if max_output_chars is None:
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
            rc, out, err = result.returncode, result.stdout.decode('utf-8', 'replace'), result.stderr.decode('utf-8', 'replace')
        else:
            rc, out, err = bounded_run(cmd, timeout, max_output_chars)
    except subprocess.TimeoutExpired as error:
        out = (error.stdout or b"").decode("utf-8", "replace") + (error.stderr or b"").decode("utf-8", "replace")
        raise ToolError(
            f"超过 {timeout}s 未结束，状态不确定：先用 job_query(action=list/tail) 查任务/日志，勿重跑；长任务用 job_control(action=start)。"
            f"已收到的输出:\n{truncate(out, max_output_chars or MAX_OUTPUT)}", code="OUTCOME_UNKNOWN", status=UNKNOWN)
    if rc == 255:
        message = ssh_failure_hint(name, err)
        raise ToolError(message + '\n' + fmt_result(rc, out, err, time.time() - start),
                        code='SSH_ERROR', status=UNKNOWN)
    return rc, out, err, time.time() - start


def ssh_failure_hint(server, stderr):
    classified = connection.classify_ssh_error(stderr)
    code, summary = classified or ('ssh_error', 'SSH 返回 255，未能分类具体原因。')
    next_step = (not_connected_hint(server) if code == 'authentication_failed' else
                 f'请调用 connection_query(action="diagnose", server="{server}") 检查。')
    return f'[{code}] {summary}\n{next_step} 核验目标状态后再决定是否重试。'


def fmt_result(rc, out, err, elapsed, extra=""):
    out, err = out.rstrip("\n"), err.rstrip("\n")
    limits = (MAX_OUTPUT, MAX_OUTPUT)
    if len(out) + len(err) > MAX_OUTPUT:
        limits = (MAX_OUTPUT - min(len(err), MAX_OUTPUT // 2),
                  MAX_OUTPUT - min(len(out), MAX_OUTPUT // 2))
    parts = [f"exit_code: {rc}" + (f"   耗时: {elapsed:.1f}s" if elapsed >= 5 else "") + extra]
    if out:
        body = truncate(out, limits[0])
        parts.append("--- stdout ---\n" + body if err else body)
    if err:
        parts.append("--- stderr ---\n" + truncate(err, limits[1]))
    if not out and not err:
        parts.append("(无输出)")
    return "\n".join(parts)


def ensure_login(server):
    if not connection.is_connected("srv-" + server):
        if os.environ.get("REMOTE_WS_LOGIN_GUI") == "1":
            try:
                connection.ensure_connected(server)
            except connection.LoginError as error:
                raise FileToolError(str(error), code=error.code or 'CONNECTION_SETUP', status=NOT_RUN) from error
        else:
            raise FileToolError(not_connected_hint(server), code="LOGIN_REQUIRED", status=NOT_RUN)


def ssh_command(server, source):
    return connection.ssh_args() + ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                    "srv-" + server, "python3 -c " + shlex.quote(source)]


def _feed_json(pipe, data):
    try:
        pipe.write(data)
        pipe.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _capture_json(cmd, data, timeout):
    """Run one SSH JSON call, hard-capping stdout before any parsing."""
    child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, bufsize=0)
    out, err = bytearray(), bytearray()
    writer = None
    if data:
        writer = threading.Thread(target=_feed_json, args=(child.stdin, data), daemon=True)
        writer.start()
    else:
        child.stdin.close()
    start = time.monotonic()
    selector = selectors.DefaultSelector()
    try:
        selector.register(child.stdout, selectors.EVENT_READ, 'out')
        selector.register(child.stderr, selectors.EVENT_READ, 'err')
        while selector.get_map():
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            for key, _ in selector.select(min(0.2, remaining)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.data == 'out':
                    if len(out) + len(chunk) > MAX_JSON_BYTES:
                        room = MAX_JSON_BYTES - len(out)
                        if room > 0:
                            out.extend(chunk[:room])
                        raise ResponseTooLargeError(MAX_JSON_BYTES)
                    out.extend(chunk)
                else:
                    room = JSON_STDERR_BYTES - len(err)
                    if room > 0:
                        err.extend(chunk[:room])
        rc = child.wait(timeout=max(.001, timeout - (time.monotonic() - start)))
        return rc, bytes(out), bytes(err)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
        child.stderr.close()
        if child.stdin is not None and not child.stdin.closed:
            try:
                child.stdin.close()
            except OSError:
                pass
        selector.close()
        if writer is not None:
            writer.join(timeout=1)


def response_limit_message(operation):
    message = (f"远端响应超过 {MAX_JSON_BYTES // (1024 * 1024)} MiB 上限，已停止读取且未解析；"
               "请缩小范围后重试（如减少 files/ranges、降低 max_chars、分页 offset/limit 或减少历史条数）")
    if operation == 'edit':
        return message + "；编辑状态不确定，请用 file_query(action=read) 重新读取确认，不得自动重写。"
    if operation == 'stop':
        return message + "；停止状态不确定，请先用 job_query(action=list) 确认，不得自动重复停止。"
    return message + "。"


def remote_json(server, payload, timeout=120, *, source):
    operation = payload.get('operation')
    ensure_login(server)
    mutating = operation in ('edit', 'stop')

    def uncertain(message):
        if not mutating:
            return FileToolError(message)
        tip = ('编辑状态不确定，请用 file_query(action=read) 重读，不得自动重写。' if operation == 'edit'
               else '停止执行状态不确定，请先用 job_query(action=list) 确认，不得自动重复停止。')
        return FileToolError(message + '；' + tip, code='OUTCOME_UNKNOWN', status=UNKNOWN)

    command = ssh_command(server, source)
    try:
        rc, stdout, stderr = _capture_json(command, json.dumps(payload).encode(), timeout)
    except subprocess.TimeoutExpired as error:
        raise uncertain('操作超时，请检查连接') from error
    except ResponseTooLargeError as error:
        raise uncertain(response_limit_message('read')) from error
    except OSError as error:
        raise uncertain(f'本地 SSH 传输异常（errno={error.errno}）') from error
    stderr_text = stderr.decode('utf-8', 'replace')
    if rc == 255:
        raise uncertain(ssh_failure_hint(server, stderr_text))
    try:
        value = json.loads(stdout)
    except (ValueError, UnicodeDecodeError) as error:
        message = (ssh_failure_hint(server, stderr_text) if connection.classify_ssh_error(stderr_text)
                   else '远端 Python 不可用或响应中断，请用 connection_query(action=diagnose) 检查')
        raise uncertain(message) from error
    if not isinstance(value, dict) or type(value.get('ok')) is not bool:
        raise uncertain('远端响应格式无效：期望含布尔 ok 的 JSON 对象')
    if not value['ok']:
        message = str(value.get('error', '远程操作失败'))
        raise uncertain(message) if value.get('uncertain') else FileToolError(message)
    if rc != 0:
        raise uncertain(f'远端返回了成功内容但 SSH 退出码为 {rc}，未确认成功')
    value.pop('ok', None)
    return value
