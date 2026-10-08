#!/usr/bin/env python3
"""remote-ws MCP server (stdio, JSON-RPC 2.0, no third-party dependencies).

Runs commands, background jobs and GPU/conda queries on remote GPU servers over SSH.
Configuration (next to this file):
  ssh_config      Host srv-<name> entries (ControlMaster connection reuse)
  remote-ws.conf  conventions (DEFAULT_SERVER, CODE_ROOT, DATA_ROOT, CONDA_ROOT)
SSH never prompts for a password here (BatchMode); it reuses the ControlMaster
connection opened by scripts/connect.sh. Kimi Code can request a visible local login window;
the user types the password directly into SSH, never into MCP.
Write guard: exec/run_background refuse commands that would write outside
CODE_ROOT/DATA_ROOT (plus /tmp, /var/tmp, /dev/shm); after the user approves,
the call is retried unchanged with confirmed=true.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
import codecs
import selectors
import local_connection
import file_tools
import job_tools
import uuid
import guide_tools
import text_tools
import project_tools

SERVER_NAME = "remote-ws"
SERVER_VERSION = "0.12.0"
PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

HERE = os.path.dirname(os.path.realpath(__file__))
CONF_FILE = os.path.join(HERE, "remote-ws.conf")
SSH_CONFIG = os.path.join(HERE, "ssh_config")
HOST_PREFIX = "srv-"
CONNECT_SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "connect.sh")
MAX_OUTPUT = 30000
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

DEFAULTS = {
    "CODE_ROOT": "/home/wjh",
    "DATA_ROOT": "/data/wjh",
    "CONDA_ROOT": "/home/wjh/miniconda3",
}


class ToolError(Exception):
    pass


def conf():
    # DEFAULT_SERVER 保留配置原始值：只用目录约定的工具不因默认服务器无效而失败，
    # 服务器名校验只在实际使用它的 resolve_server() 里做。
    c = dict(DEFAULTS)
    c.update({k: v for k, v in local_connection.read_config().items() if v})
    for k in ("CODE_ROOT", "DATA_ROOT", "CONDA_ROOT"):
        c[k] = c[k].rstrip("/") or "/"
    return c


def servers():
    return local_connection.configured_servers()


def resolve_server(name):
    try:
        name = local_connection.resolve_server(name)
    except local_connection.LoginError as error:
        raise ToolError(str(error)) from error
    return name, HOST_PREFIX + name


def ssh_params(host):
    return local_connection.ssh_params(host)


def connected(host):
    return local_connection.is_connected(host)


def qpath(path):
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def remote_dir(cwd):
    c = conf()
    if not cwd:
        return c["CODE_ROOT"]
    cwd = cwd.rstrip("/") or "/"
    if cwd.startswith(("/", "~")):
        return cwd
    return c["CODE_ROOT"] + "/" + cwd


def truncate(text):
    if len(text) <= MAX_OUTPUT:
        return text
    head = MAX_OUTPUT // 3
    tail = MAX_OUTPUT - head
    return text[:head] + f"\n\n... [省略 {len(text) - MAX_OUTPUT} 字符；如需完整输出，请重定向到文件后用 tail_log 或 head/tail 分段查看] ...\n\n" + text[-tail:]


def connect_cmd(name):
    return f"bash {shlex.quote(CONNECT_SCRIPT)} {shlex.quote(name)}"


def not_connected_hint(name):
    return (f"无法免密连接 {HOST_PREFIX}{name}。可调用 connect(server=\"{name}\") 打开本地登录窗口，"
            f"或把下面的命令原样交给用户在 WSL 终端执行:\n  {connect_cmd(name)}\n"
            f"输入一次服务器密码（连接空闲 8 小时后断开）后再重试。不要索要、保存或处理密码。")


# ---------------------------------------------------------------- write guard

WRITE_EXEMPT_ROOTS = ("/tmp", "/var/tmp", "/dev/shm")
DEVICE_TARGETS = ("/dev/null", "/dev/zero", "/dev/full", "/dev/stdout",
                  "/dev/stderr", "/dev/tty", "/dev/fd", "/proc/self/fd")
SYSTEM_COMMANDS = {"sudo", "doas", "apt", "apt-get", "aptitude", "dpkg", "snap", "systemctl", "service",
                   "crontab", "shutdown", "reboot", "useradd", "usermod", "userdel", "passwd", "fdisk",
                   "parted", "mknod", "losetup", "swapon", "swapoff", "sysctl", "exportfs", "iptables",
                   "ufw", "mount", "umount"}
MKFS_RE = re.compile(r"mkfs(?:\.\w+)?")
SHELL_RE = re.compile(r"(?:ba|z|da)?sh")
PROC_SUBST_RE = re.compile(r"(?:ba|z|da)?sh\s+<\(")
REDIRECT_RE = re.compile(r"(\d*&>>|\d*&>|\d*>>|\d*>\||\d*>)(\s*)([^\s;|&()]+)")
PIP_EXE_RE = re.compile(r"\S*pip3?")
TAR_LONG_MODES = {"--create": "c", "--extract": "x", "--get": "x", "--list": "t",
                  "--update": "u", "--append": "r", "--delete": "d"}
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
OP_TOKENS = {"|", "||", "|&", "&&", ";", "(", ")", "&"}
SKIP_TOKENS = {">", ">>", ">|", "&>", "&>>", "<", "<<", "<<<", ">&", "<&"}
WRAPPERS = {"nice", "env", "timeout", "command", "builtin", "stdbuf", "nohup", "setsid"}
WRITE_ALL_ARGS = {"rm", "mv", "mkdir", "rmdir", "touch", "shred", "chattr", "truncate", "fallocate", "setfacl"}
WRITE_SKIP_FIRST = {"chmod", "chown", "chgrp"}   # 第一个位置参数是模式/属主，不是路径
WRITE_LAST_ARG = {"cp", "ln", "scp", "rsync", "install"}
GZIP_CMDS = {"gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz", "zstd", "unzstd"}
FIND_WRITE_FLAGS = {"delete", "exec", "execdir", "ok", "okdir"}
CONSUMES = {
    "curl": {"-o", "--output"},
    "wget": {"-O", "--output-document"},
    "unzip": {"-d"},
    "mktemp": {"-p", "--tmpdir"},
    "truncate": {"-s", "--size"},
    "fallocate": {"-l", "--length"},
    "setfacl": {"-m", "-x", "-d", "--modify", "--remove"},
    "git": {"-C"},
    "nice": {"-n"},
    "mkdir": {"-m", "--mode"},
}


def is_under(path, roots):
    return any(path == r or path.startswith(r + "/") for r in roots)


def norm_path(token, cwd):
    c = conf()
    token = (token or "").strip().strip("'\"")
    if not token or token == ".":
        return cwd
    if token == "~" or token.startswith("~/"):
        p = c["CODE_ROOT"] + token[1:]
    elif token.startswith("~"):
        p = "/home/" + token[1:]        # ~其他用户：按越界处理
    elif token.startswith("/"):
        p = token
    else:
        p = (cwd.rstrip("/") or "/") + "/" + token
    parts = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def split_tokens(command):
    lex = shlex.shlex(command, posix=True, punctuation_chars="|&;()")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return command.split()


def tar_write_targets(toks):
    """解析 tar 参数，返回 (种类, 目标) 列表：打包/改档写 -f 目标，解包写 -C 目录或 cwd。"""
    modes, fval, cval, pos = "", None, None, []
    i = 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if t == "--":
            pos.extend(toks[i:])
            break
        if t.startswith("--"):
            # 长选项单独解析，不逐字母累加；未知长选项直接忽略。
            name, sep, value = t.partition("=")
            if name in ("--file", "--directory"):
                if not sep:
                    value = toks[i] if i < len(toks) else None
                    i += 1
                if name == "--file":
                    fval = value
                else:
                    cval = value
            elif name in TAR_LONG_MODES:
                modes += TAR_LONG_MODES[name]
            continue
        if (t.startswith("-") and len(t) > 1) or (re.fullmatch(r"[A-Za-z]+", t) and not pos and not modes):
            body = t[1:] if t.startswith("-") else t
            for j, ch in enumerate(body):
                if ch in "fC":
                    rest = body[j + 1:]
                    if not rest and i < len(toks):
                        rest = toks[i]
                        i += 1
                    if ch == "f":
                        fval = rest
                    else:
                        cval = rest
                    break
                modes += ch
            continue
        pos.append(t)
    out = []
    if any(m in modes for m in "crud") and fval:
        out.append(("tar 归档", fval))
    elif "x" in modes:
        out.append(("tar 解压", cval or "."))
    return out


def find_write_violations(command, cwd, conda_env):
    """启发式检测 command 中落在允许区域之外的写/修改操作。返回违规描述列表，空=放行。"""
    c = conf()
    roots = (c["CODE_ROOT"], c["DATA_ROOT"]) + WRITE_EXEMPT_ROOTS
    cwd_n = norm_path(cwd or c["CODE_ROOT"], c["CODE_ROOT"])
    viol, seen = [], set()

    def add(v):
        if v not in seen:
            seen.add(v)
            viol.append(v)

    def check_target(kind, token):
        token = (token or "").strip().strip("'\"")
        if not token or token.startswith(("&", "-")) or token.isdigit():
            return
        if ENV_ASSIGN_RE.match(token):
            return
        p = norm_path(token, cwd_n)
        if is_under(p, DEVICE_TARGETS) or is_under(p, roots):
            return
        add(f"{kind}: {token}（解析为 {p}）")

    def check_cmd_pos(name):
        """命令位 token（已去路径）检查：系统级命令、带子命令的 mkfs。"""
        if name in SYSTEM_COMMANDS or MKFS_RE.fullmatch(name):
            add(f"系统级命令 {name}（修改系统状态，与路径无关）")

    def check_pip(exe, fl):
        """命令位是 pip 且带 install：按安装目标是否越界判定。"""
        if "/" in exe:
            if not is_under(norm_path(exe, cwd_n), roots):
                add(f"pip install 使用越界环境 {exe}")
        elif not (conda_env or "user" in fl):
            add("pip install 未指定 conda_env（会写入系统 python 目录）")

    def piped_shells(toks):
        """|、|& 之后进入的命令位若是 shell 则记录；sudo 和包装命令透明穿透。"""
        out, piped, i = set(), False, 0
        while i < len(toks):
            tok = toks[i]
            i += 1
            if tok in OP_TOKENS:
                piped = tok in ("|", "|&")
                continue
            if piped:
                base = tok.rsplit("/", 1)[-1]
                if base in WRAPPERS or base in ("sudo", "doas"):
                    continue                    # 透明跳过，真正的命令在后
                if SHELL_RE.fullmatch(base):
                    out.add(base)
                piped = False
        return out

    tokens = split_tokens(command)
    for shell in sorted(piped_shells(tokens)):
        add(f"管道执行 shell（| {shell}），可能任意写盘")
    if PROC_SUBST_RE.search(command):
        add("进程替换执行脚本（bash <(...) 等），可能任意写盘")

    write_targets, cd_targets = [], []
    write_ops = 0
    for m in REDIRECT_RE.finditer(command):
        tok = m.group(3).strip().strip("'\"")
        if not tok or tok.startswith(("&", "-")) or tok.isdigit() or ENV_ASSIGN_RE.match(tok):
            continue
        if is_under(norm_path(tok, cwd_n), DEVICE_TARGETS):
            continue                    # >/dev/null 之类
        write_ops += 1
        write_targets.append(("重定向写入", tok))

    def handle_simple(cmd, a, fl):
        nonlocal write_ops
        if cmd == "install" and any(f == "d" or (f.isalpha() and "d" in f) for f in fl):
            write_ops += len(a)                 # install -d 的位置参数全是新建目录
            write_targets.extend(("install -d 写入", x) for x in a)
        elif cmd in WRITE_ALL_ARGS:
            write_ops += len(a)
            write_targets.extend(("写入", x) for x in a)
        elif cmd in WRITE_SKIP_FIRST:
            write_ops += max(0, len(a) - 1)
            write_targets.extend(("写入", x) for x in a[1:])
        elif cmd in WRITE_LAST_ARG:
            if len(a) >= 2:
                write_ops += 1
                write_targets.append(("写入", a[-1]))
        elif cmd == "tee":
            write_ops += len(a)
            write_targets.extend(("tee 写入", x) for x in a)
        elif cmd == "sed" and any(f == "i" or f.startswith("i") for f in fl):
            write_ops += max(0, len(a) - 1)
            write_targets.extend(("sed -i 写入", x) for x in a[1:])
        elif cmd == "dd":
            for x in a:
                if x.startswith("of="):
                    write_ops += 1
                    write_targets.append(("dd 写入", x[3:]))
        elif cmd == "zip":
            if a:
                write_ops += 1
                write_targets.append(("zip 写入", a[0]))
        elif cmd == "unzip":
            write_ops += 1
            write_targets.extend(("解压", x) for x in a)       # -d 值已消费进 a；无 -d 则写 cwd
        elif cmd == "curl":
            write_ops += 1
            write_targets.extend(("curl 写入", x) for x in a)  # -o 值；否则写 cwd
        elif cmd == "wget":
            write_ops += 1
            write_targets.extend(("wget 写入", x) for x in a)  # -O 值；否则写 cwd
        elif cmd == "mktemp":
            write_ops += 1
            write_targets.extend(("mktemp 目录", x) for x in a)
        elif cmd in GZIP_CMDS:
            stdout = any(f in ("c", "stdout", "to-stdout") or (f.isalpha() and "c" in f) for f in fl)
            if not stdout:                      # -c/--stdout 只写标准输出
                write_ops += len(a)
                write_targets.extend(("压缩写入", x) for x in a)
        elif cmd == "find" and fl & FIND_WRITE_FLAGS:
            write_ops += len(a)                 # -delete/-exec 等可写任意路径
            write_targets.extend(("find 写入", x) for x in a)
        elif cmd == "git":
            if a and a[0] == "clean":
                write_ops += 1                  # 由 cwd/cd 越界规则判定
            elif "clone" in a:
                write_ops += 1
                idx = a.index("clone")
                if len(a) >= idx + 3:
                    write_targets.append(("git clone 目录", a[idx + 2]))
        elif cmd == "cd" and a:
            cd_targets.append(a[-1])

    cur, cur_raw, args, flags, expect, timeout_used = None, None, [], set(), None, False

    def end_cmd():
        nonlocal cur, cur_raw, args, flags, expect
        if cur is not None and cur not in WRAPPERS:
            handle_simple(cur, args, flags)
            if "install" in args:
                if PIP_EXE_RE.fullmatch(cur_raw or ""):
                    check_pip(cur_raw, flags)
                elif re.fullmatch(r"python[\d.]*", cur):
                    exe = next((x for x in args if PIP_EXE_RE.fullmatch(x)), None)
                    if exe:
                        check_pip(exe, flags)
        cur, cur_raw, args, flags, expect = None, None, [], set(), None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok in OP_TOKENS:
            end_cmd()
            continue
        if tok in SKIP_TOKENS:
            continue
        if cur == "tar":
            sub = [tok]
            while i < len(tokens) and tokens[i] not in OP_TOKENS:
                sub.append(tokens[i])
                i += 1
            wt = tar_write_targets(sub)
            write_targets.extend(wt)
            write_ops += bool(wt)
            cur, cur_raw = None, None
            continue
        if expect:
            if cur == "git" and expect == "-C":
                cd_targets.append(tok)
            elif cur != "nice":
                args.append(tok)
            expect = None
            continue
        if tok.startswith("-") and tok != "-":
            flags.add(tok.lstrip("-"))
            if cur in CONSUMES and tok in CONSUMES[cur]:
                expect = tok
            continue
        if ENV_ASSIGN_RE.match(tok) and (cur is None or cur == "env"):
            continue
        if cur is None:
            base = tok.rsplit("/", 1)[-1]
            check_cmd_pos(base)
            cur, cur_raw = base, tok
            continue
        if cur == "timeout" and not timeout_used:
            timeout_used = True            # timeout 的第一个位置参数是时长
            continue
        if cur in WRAPPERS:
            base = tok.rsplit("/", 1)[-1]
            check_cmd_pos(base)            # 透明包装，真正的命令在后
            cur, cur_raw = base, tok
            continue
        args.append(tok)
    end_cmd()

    for kind, tok in write_targets:
        check_target(kind, tok)
    if write_ops and not is_under(cwd_n, roots):
        add(f"cwd 越界: {cwd_n}（写操作将落在受保护区域外）")
    if write_ops and cd_targets:
        bad = [t for t in cd_targets if not is_under(norm_path(t, cwd_n), roots)]
        if bad:
            add("cd 到越界目录后执行写操作: " + ", ".join(bad))
    return viol


def confirm_text(viol):
    lines = ["检测到写/修改操作落在受保护区域（/home/wjh、/data/wjh 及 /tmp、/var/tmp、/dev/shm）之外："]
    lines += ["  - " + v for v in viol]
    lines.append("请先向用户说明这些操作并征得明确同意，然后用完全相同的参数重试并加 confirmed=true。")
    return "\n".join(lines)


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
    if not connected(host) and os.environ.get("REMOTE_WS_LOGIN_GUI") == "1":
        try:
            local_connection.ensure_connected(name)
        except local_connection.LoginError as e:
            raise ToolError(str(e)) from e
    cmd = local_connection.ssh_args() + ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "bash -c " + shlex.quote(script)]
    start = time.time()
    try:
        if max_output_chars is None:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
            rc, out, err = r.returncode, r.stdout.decode('utf-8', 'replace'), r.stderr.decode('utf-8', 'replace')
        else:
            rc, out, err = bounded_run(cmd, timeout, max_output_chars)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") + (e.stderr or b"").decode("utf-8", "replace")
        raise ToolError(
            f"命令超过 {timeout}s 未结束，本地已断开（服务器上的进程可能仍在运行）。"
            f"执行状态不确定，不得自动重跑；先检查任务状态。长任务请改用 run_background。已收到的输出:\n{truncate(out)}")
    if rc == 255:
        classified = local_connection.classify_ssh_error(err)
        if classified:
            code, summary = classified
            extra = (not_connected_hint(name) if code == "authentication_failed" else
                     f"请调用 diagnose(server=\"{name}\") 检查；本次命令执行状态不确定，不自动重新登录或重跑命令。")
            raise ToolError(f"[{code}] {summary}\n{extra}\n"
                            + fmt_result(rc, out, err, time.time() - start))
    return rc, out, err, time.time() - start


def env_prelude(conda_env, gpus):
    c = conf()
    lines = []
    if conda_env:
        if not NAME_RE.match(conda_env):
            raise ToolError(f"非法 conda 环境名: {conda_env}")
        lines.append(f"source {qpath(c['CONDA_ROOT'])}/bin/activate {shlex.quote(conda_env)} || "
                     f"{{ echo 'remote-ws: conda 环境 {conda_env} 激活失败' >&2; exit 98; }}")
    if gpus is not None and str(gpus).strip() != "":
        g = str(gpus).strip()
        if not re.match(r"^\d+(,\d+)*$", g):
            raise ToolError(f"gpus 应为逗号分隔的编号，例如 \"0\" 或 \"0,1\"，收到: {g}")
        lines.append(f"export CUDA_VISIBLE_DEVICES={g}")
    return lines


def fmt_result(rc, out, err, elapsed, extra=""):
    parts = [f"exit_code: {rc}   耗时: {elapsed:.1f}s{extra}"]
    if rc:
        parts.append("远程命令返回非零退出码，请检查命令输出；不会因此重新登录或自动重跑。")
    if out:
        parts.append("--- stdout ---\n" + truncate(out.rstrip("\n")))
    if err:
        parts.append("--- stderr ---\n" + truncate(err.rstrip("\n")))
    if not out and not err:
        parts.append("(无输出)")
    return "\n".join(parts)


# ---------------------------------------------------------------- tools

def t_connect(args):
    wait = file_tools.integer(args, 'wait_sec', 0, 0, 120)
    name, host = resolve_server(args.get("server"))
    if connected(host):
        return f"{host}: connected（复用）"
    try:
        if wait == 0:
            state = local_connection.request_login(name)
            return f'{host}: {state}'
        local_connection.ensure_connected(name, timeout=wait)
    except local_connection.LoginError as e:
        raise ToolError(str(e)) from e
    return f"{host}: connected"


def t_diagnose(args):
    name, _ = resolve_server(args.get("server"))
    timeout = max(1, min(int(args.get("timeout_sec") or 3), 10))
    report = local_connection.diagnose(name, timeout=timeout)
    return (f"state: {report['state']}\nserver: {report['server']}\n"
            f"endpoint: {report['endpoint']}\n{report['summary']}")


def t_status(args):
    verbose = file_tools.verbosity(args)
    all_servers = args.get('all', False)
    if type(all_servers) is not bool:
        raise ToolError('all 必须是布尔值')
    # 两个分支都用原始 DEFAULT_SERVER 展示：默认配置有误也要能看到实际值；
    # 只有非 all 分支需要校验本次调用的服务器（resolve_server）。
    c = conf()
    default = (c.get('DEFAULT_SERVER') or '').strip()
    if default.startswith(HOST_PREFIX):
        default = default[len(HOST_PREFIX):]
    if all_servers:
        server_names = servers()
        if not server_names:
            raise ToolError(f"ssh_config 里没有配置任何 {HOST_PREFIX}<名字> 服务器，无法列出状态。")
    else:
        server_names = [resolve_server(args.get('server'))[0]]
    lines = [f"default: {default or '(未配置 DEFAULT_SERVER)'}"]
    if all_servers and default not in server_names:
        lines.append(f"⚠️ 默认服务器无效，调用其他工具时请显式指定 server（可选: {', '.join(server_names)}）")
    if verbose:
        lines += [f"{k}={c[k]}" for k in ('CODE_ROOT','DATA_ROOT','CONDA_ROOT')]
    for sname in server_names:
        host = HOST_PREFIX + sname
        if connected(host):
            state = "session_present"
        elif local_connection.login_busy(host):
            state = "login_pending"
        else:
            state = "needs_login"
        endpoint = ''
        if verbose:
            p = ssh_params(host)
            endpoint = f" {p.get('user', '?')}@{p.get('hostname', '?')}:{p.get('port', '?')}"
        lines.append(f"{sname}: {state}{endpoint}")
    return "\n".join(lines)


def t_exec(args):
    cap = file_tools.integer(args, 'max_output_chars', 6000, 256, MAX_OUTPUT)
    command = (args.get("command") or "").strip()
    if not command:
        raise ToolError("command 不能为空")
    name, _ = resolve_server(args.get("server"))
    rdir = remote_dir(args.get("cwd"))
    timeout = int(args.get("timeout_sec") or 120)
    timeout = max(1, min(timeout, 1800))
    viol = find_write_violations(command, rdir, args.get("conda_env"))
    if viol and args.get("confirmed") is not True:
        raise ToolError(confirm_text(viol))
    script = "\n".join(
        [f"cd {qpath(rdir)} || {{ echo 'remote-ws: 目录不存在: {rdir}' >&2; exit 97; }}"]
        + env_prelude(args.get("conda_env"), args.get("gpus"))
        + [command])
    rc, out, err, el = ssh_run(name, script, timeout, max_output_chars=cap)
    text = fmt_result(rc, out, err, el, f"   服务器: {name}   目录: {rdir}")
    if viol:
        text = f"⚠️ 用户已确认越界写操作: {'；'.join(viol)}\n" + text
    return text, rc != 0


def log_dir(project):
    c = conf()
    if project:
        if not NAME_RE.match(project):
            raise ToolError(f"非法项目名: {project}")
        return f"{c['DATA_ROOT']}/{project}/logs"
    return f"{c['DATA_ROOT']}/logs"


def t_run_background(args):
    command = (args.get("command") or "").strip()
    job = (args.get("name") or "").strip()
    if not command:
        raise ToolError("command 不能为空")
    if not NAME_RE.match(job):
        raise ToolError("name 只能包含字母、数字、_ . -，例如 train-resnet")
    name, _ = resolve_server(args.get("server"))
    rdir = remote_dir(args.get("cwd"))
    ldir = log_dir(args.get("project"))
    viol = find_write_violations(command, rdir, args.get("conda_env"))
    if viol and args.get("confirmed") is not True:
        raise ToolError(confirm_text(viol))
    inner = "\n".join(env_prelude(args.get("conda_env"), args.get("gpus")) + [command])
    job_id = uuid.uuid4().hex
    meta = json.dumps(dict(job_id=job_id, name=job, cwd=rdir,
                          conda_env=args.get('conda_env') or '-', gpus=args.get('gpus') or '-'))
    job_script = (
        f"export REMOTE_WS_JOB_META={shlex.quote(meta)}\n"
        f"python3 -c {shlex.quote(job_tools.HEADER_SOURCE)} || exit 95\n"
        f"printf '# cmd: %s\\n' {shlex.quote(command)}\n"
        f"( {inner}\n)\n"
        "rc=$?\n"
        f"echo \"# remote-ws exit=$rc job_id={job_id} end=$(date -Is)\"\n")
    script = (
        f"mkdir -p {qpath(ldir)} || exit 96\n"
        f"cd {qpath(rdir)} || {{ echo 'remote-ws: 目录不存在: {rdir}' >&2; exit 97; }}\n"
        f"log={qpath(ldir)}/{job}-$(date +%Y%m%d-%H%M%S)-{job_id[:12]}.log\n"
        f"setsid bash -c {shlex.quote(job_script)} > \"$log\" 2>&1 < /dev/null &\n"
        "pid=$!\nsleep 1\n"
        "if kill -0 $pid 2>/dev/null; then st=running; else st=exited; fi\n"
        "echo \"pid=$pid status=$st log=$log\"\n"
        )
    rc, out, err, el = ssh_run(name, script, 60)
    stamp = f"⚠️ 用户已确认越界写操作: {'；'.join(viol)}\n" if viol else ""
    if rc != 0:
        return stamp + fmt_result(rc, out, err, el), True
    return stamp + f"server={name} name={job} task_id={job_id}\n{out.strip()}", False


def list_jobs(name, limit=10):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ToolError('limit 必须为 1 到 500 的整数')
    return job_tools.request(name, 'jobs', data_root=conf()['DATA_ROOT'], limit=limit)['jobs']


def t_jobs(args):
    verbose = file_tools.verbosity(args)
    name, _ = resolve_server(args.get("server"))
    jobs = list_jobs(name, args.get("limit", 10))
    if not jobs:
        return f"{name} 上没有 run_background 启动过的任务（扫描 DATA_ROOT/logs 和 DATA_ROOT/*/logs）。"
    lines = [f"{name} 上的后台任务（新→旧）:"]
    for j in jobs:
        detail = (f" start={j['start']} last_write={j['last_write']} gpus={j['gpus']} env={j['conda_env']}" if verbose else '')
        lines.append(f"- {j['name']} [{j['status']}] id={j.get('job_id') or 'legacy'} pid={j['pid']}{detail}\n  log: {j['log']}")
    return truncate("\n".join(lines))


def find_job(name, job):
    if job.startswith("/"):
        record = job_tools.request(name, 'job', log=job)['job']
        if record:
            return record
        return {"log": job, "pid": None, "name": None, "status": "?"}
    jobs = list_jobs(name, 200)
    for j in jobs:
        if j["name"] == job or os.path.basename(j["log"]) == job or j["pid"] == job or j.get('job_id') == job:
            return j
    raise ToolError(f"在 {name} 上找不到任务 '{job}'。先用 jobs 查看。")


def t_tail_log(args):
    name, _ = resolve_server(args.get("server"))
    job = (args.get("job") or "").strip()
    if not job:
        raise ToolError("job 不能为空（任务名、pid 或日志绝对路径）")
    lines = file_tools.integer(args, 'lines', 20, 1, 2000)
    cap = file_tools.integer(args, 'max_chars', 6000, 256, 12000)
    cursor = args.get('cursor')
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
        raise ToolError('无效日志游标')
    query=args.get('query')
    errors=args.get('errors',False)
    if type(errors) is not bool or (query is not None and
        (not isinstance(query,str) or not query or len(query)>256 or '\n' in query or '\x00' in query)) or (query and errors):
        raise ToolError('query 为单行字面关键词，errors 为布尔值，两者不混用')
    context=file_tools.integer(args,'context_lines',2,0,10)
    j = find_job(name, job)
    result = job_tools.request(name, 'tail', log=j['log'], lines=lines, max_chars=cap,
                              cursor=cursor, source_server=name, job_meta=j if j.get('identity_version') == 2 else None,
                              query=query,errors=errors,context_lines=context)
    return json.dumps(dict(task_id=j.get('job_id'), status=j['status'], **result),
                      ensure_ascii=False, separators=(',', ':'))


def t_stop_job(args):
    name, _ = resolve_server(args.get("server"))
    j = find_job(name, (args.get("job") or "").strip())
    if j['status'] in ('legacy/unverified', 'identity_mismatch'):
        raise ToolError(f"任务身份无法核实（{j['status']}），未发送信号；先核实实际进程。")
    if j["status"] != "running" or not j.get("pid"):
        return f"任务 {j.get('name')} 当前不在运行（{j['status']}），无需停止。"
    result = job_tools.request(name, 'stop', log=j['log'], expected=j, force=args.get('force') is True)
    return json.dumps(result, ensure_ascii=False)


GPU_SCRIPT = r"""
nvidia-smi --query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits || exit 1
echo '@@APPS'
nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits 2>/dev/null | while IFS=', ' read -r u p m; do
  [ -n "$p" ] || continue
  info=$(ps -o user=,etime= -p "$p" 2>/dev/null | awk '{print $1" "$2}')
  printf '%s\t%s\t%s\t%s\n' "$u" "$p" "$m" "${info:-? ?}"
done
"""


def t_gpus(args):
    name, _ = resolve_server(args.get("server"))
    rc, out, err, el = ssh_run(name, GPU_SCRIPT, 60)
    if rc != 0:
        return fmt_result(rc, out, err, el), True
    gpu_part, _, app_part = out.partition("@@APPS")
    apps = {}
    for line in app_part.strip().splitlines():
        f = line.split("\t")
        if len(f) == 4:
            apps.setdefault(f[0].strip(), []).append(f"{f[3].split()[0]}(pid {f[1]}, {f[2]}MiB, {f[3].split()[-1]})")
    lines = [f"{name} GPU 状态:"]
    free = []
    for line in gpu_part.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 6:
            continue
        idx, uuid, gname, used, total, util = f[:6]
        procs = apps.get(uuid, [])
        is_free = not procs and int(float(used)) < 1000
        if is_free:
            free.append(idx)
        lines.append(f"  GPU {idx} {gname}  显存 {used}/{total} MiB  利用率 {util}%  "
                     f"{'空闲' if is_free else '占用: ' + (', '.join(procs) or '未知进程')}")
    lines.append(f"空闲 GPU: {','.join(free) if free else '无'}（用法: gpus=\"{','.join(free[:1]) or '0'}\"）")
    return "\n".join(lines)


def t_conda_envs(args):
    name, _ = resolve_server(args.get("server"))
    c = conf()
    script = (f"{qpath(c['CONDA_ROOT'])}/bin/conda env list || exit 1\n"
              f"for e in {qpath(c['CONDA_ROOT'])} {qpath(c['CONDA_ROOT'])}/envs/*; do\n"
              "  [ -x \"$e/bin/python\" ] || continue\n"
              "  v=$(\"$e/bin/python\" -c 'import sys;print(\"%d.%d.%d\"%sys.version_info[:3])' 2>/dev/null)\n"
              "  t=$(\"$e/bin/python\" -c 'import torch;print(torch.__version__, \"cuda=\"+str(torch.cuda.is_available()))' 2>/dev/null || echo '无 torch')\n"
              "  n=$(basename \"$e\"); [ \"$e\" = " + qpath(c['CONDA_ROOT']) + " ] && n=base\n"
              "  echo \"$n: python $v, torch: $t\"\n"
              "done\n")
    rc, out, err, el = ssh_run(name, script, 120)
    return fmt_result(rc, out, err, el), rc != 0


def t_init_project(args):
    project = (args.get("project") or "").strip()
    if not NAME_RE.match(project):
        raise ToolError("project 只能包含字母、数字、_ . -")
    name, _ = resolve_server(args.get("server"))
    c = conf()
    code = f"{c['CODE_ROOT']}/{project}"
    data = f"{c['DATA_ROOT']}/{project}"
    subdirs = " ".join(f"{qpath(data)}/{d}" for d in ("data", "checkpoints", "results", "logs"))
    script = (f"mkdir -p {qpath(code)} {subdirs} || exit 1\n"
              f"ls -ld {qpath(code)} {qpath(data)} {subdirs}\n")
    rc, out, err, el = ssh_run(name, script, 60)
    if rc != 0:
        return fmt_result(rc, out, err, el), True
    return (f"已在 {name} 上创建项目 {project}:\n"
            f"  代码:   {code}\n"
            f"  数据:   {data}/data\n  权重:   {data}/checkpoints\n  结果:   {data}/results\n  日志:   {data}/logs\n\n"
            + out.strip()), False


S = {"type": "string"}


SERVER_ARG = {"type": "string", "description": local_connection.server_arg_desc()}
CWD_ARG = {"type": "string", "description": "远端目录，默认/相对 CODE_ROOT，支持 ~/"}
ENV_ARG = {"type": "string", "description": "已有 Conda 环境；省略用系统环境"}
GPU_ARG = {"type": "string", "description": "CUDA_VISIBLE_DEVICES，如 0,1；先查 gpus"}
CONFIRMED_ARG = {"type": "boolean", "description": "越界写已获用户明确同意；仅同参重试"}
VERBOSE_ARG = {"type": "boolean", "default": False, "description": "返回详情"}

TOOLS = [
    ("connect", t_connect, "复用或打开独立登录窗口；默认立即返回，wait_sec 最多等 120 秒。",
     {"type": "object", "properties": {"server": SERVER_ARG, "wait_sec": {"type":"integer","minimum":0,"maximum":120,"default":0}}}),
    ("diagnose", t_diagnose, "诊断端口/复用链路，区分网络及认证故障；不弹窗。",
     {"type": "object", "properties": {"server": SERVER_ARG,
      "timeout_sec": {"type": "integer", "description": "诊断连接超时秒数，默认 3，最大 10"}}}),
    ("status", t_status, "只查本地 socket/登录锁，不访问网络；session_present 不保证链路正常。",
     {"type": "object", "properties": {"server": SERVER_ARG, "all":{"type":"boolean","default":False}, "verbose": VERBOSE_ARG}}),
    ("exec", t_exec, "远端短 Bash，返回退出码及输出；越界写须获准后同参加 confirmed。",
     {"type": "object", "properties": {"command": {**S, "description": "要执行的 bash 命令"}, "server": SERVER_ARG, "cwd": CWD_ARG,
      "conda_env": ENV_ARG, "gpus": GPU_ARG, "timeout_sec": {"type": "integer", "description": "超时秒数，默认 120，最大 1800"},
      "confirmed": CONFIRMED_ARG, "max_output_chars":{"type":"integer","minimum":256,"maximum":30000,"default":6000}},
      "required": ["command"]}),
    ("run_background", t_run_background, "后台长任务，返回 ID/PID/状态/日志；日志进 DATA_ROOT，越界写保护同 exec。",
     {"type": "object", "properties": {"command": {**S, "description": "要执行的命令，如 python train.py --epochs 100"},
      "name": {**S, "description": "任务名，字母数字_.-"}, "project": {**S, "description": "日志项目名"},
      "server": SERVER_ARG, "cwd": CWD_ARG, "conda_env": ENV_ARG, "gpus": GPU_ARG, "confirmed": CONFIRMED_ARG}, "required": ["command", "name"]}),
    ("jobs", t_jobs, "列后台任务及核实状态，默认 10 项；verbose 查时间/GPU/环境。",
     {"type": "object", "properties": {"server": SERVER_ARG, "limit": {"type": "integer", "description": "默认 10，最大 500"}, "verbose": VERBOSE_ARG}}),
    ("tail_log", t_tail_log, "日志续读；query 过滤，errors 保留异常块。cursor 绑定条件，more 表示需续读。",
     {"type": "object", "properties": {"job": {**S, "description": "ID/名称/PID/日志路径，同名最新"},
      "lines": {"type": "integer", "description": "默认 20，最大 2000"}, "cursor": S,
      "max_chars":{"type":"integer","minimum":256,"maximum":12000,"default":6000}, "server": SERVER_ARG,
      "query":S,"errors":{"type":"boolean","default":False},"context_lines":{"type":"integer","minimum":0,"maximum":10,"default":2}}, "required": ["job"]}),
    ("stop_job", t_stop_job, "核实身份后停止组成员；旧日志/身份不符拒绝，force 用 SIGKILL。",
     {"type": "object", "properties": {"job": {**S, "description": "ID/名称/PID/日志路径"}, "force": {"type": "boolean"}, "server": SERVER_ARG},
      "required": ["job"]}),
    ("gpus", t_gpus, "查显存/利用率/进程属主和空闲卡；用 GPU 前调用。",
     {"type": "object", "properties": {"server": SERVER_ARG}}),
    ("conda_envs", t_conda_envs, "列已有 Conda 环境和 Python/torch/CUDA 状态。",
     {"type": "object", "properties": {"server": SERVER_ARG}}),
    ("init_project", t_init_project, "按配置创建代码及 data/checkpoints/results/logs 目录，已有目录不改。",
     {"type": "object", "properties": {"project": {**S, "description": "项目名（字母数字_.-）"}, "server": SERVER_ARG}, "required": ["project"]}),
]
TOOLS.extend(file_tools.tools())
TOOLS.extend(guide_tools.tools())
TOOLS.extend(text_tools.tools())
TOOLS.extend(project_tools.tools())
TOOL_MAP = {name: fn for name, fn, _, _ in TOOLS}


# ---------------------------------------------------------------- JSON-RPC

def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        v = params.get("protocolVersion")
        return {"protocolVersion": v if v in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "Project SSH only. Install nothing remotely; never handle passwords or other users' files. After uncertain execution, inspect state before retrying. Out-of-root writes need user consent and identical confirmed arguments. Check gpus before GPU use; run_background for long jobs. Diagnose connection failures. get_guide(topic) gives local help/config; read only needed topics. server defaults to DEFAULT_SERVER; paths use configured roots. Summaries are default; verbose returns details."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s} for n, _, d, s in TOOLS]}
    if method == "tools/call":
        name = params.get("name")
        fn = TOOL_MAP.get(name)
        if not fn:
            raise LookupError(f"unknown tool: {name}")
        try:
            res = fn(params.get("arguments") or {})
            text, is_error = res if isinstance(res, tuple) else (res, False)
        except (ToolError, file_tools.FileToolError, local_connection.LoginError) as e:
            text, is_error = str(e), True
        except subprocess.TimeoutExpired as e:
            text, is_error = f"本地子进程超时: {e}", True
        except Exception as e:  # noqa: BLE001
            text, is_error = f"{type(e).__name__}: {e}", True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}
    if rid is None:
        return None
    raise LookupError(f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        rid = req.get("id")
        try:
            result = handle(req)
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "result": result if result is not None else {}})
        except LookupError as e:
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": str(e)}})
        except Exception as e:  # noqa: BLE001
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})


if __name__ == "__main__":
    main()
