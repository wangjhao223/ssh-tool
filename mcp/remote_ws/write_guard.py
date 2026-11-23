"""启发式扫描命令中可能落在允许 roots 之外的写/修改操作。

这是启发式检查，不是安全沙箱，也不能替代 SSH 权限和用户审查：它只识别常见
命令形式。内联 shell/解释器、xargs/make、env -C 以及含变量、反引号、通配符
或花括号展开的动态写目标无法静态验证，一律保守要求 confirmed=true。
"""
import re
import shlex


WRITE_EXEMPT_ROOTS = ("/tmp", "/var/tmp", "/dev/shm")
DEVICE_TARGETS = ("/dev/null", "/dev/zero", "/dev/full", "/dev/stdout",
                  "/dev/stderr", "/dev/tty", "/dev/fd", "/proc/self/fd")
SYSTEM_COMMANDS = {"sudo", "doas", "apt", "apt-get", "aptitude", "dpkg", "snap", "systemctl", "service",
                   "crontab", "shutdown", "reboot", "useradd", "usermod", "userdel", "passwd", "fdisk",
                   "parted", "mknod", "losetup", "swapon", "swapoff", "sysctl", "exportfs", "iptables",
                   "ufw", "mount", "umount"}
UNVERIFIED_COMMANDS = {"xargs", "make", "gmake"}
MKFS_RE = re.compile(r"mkfs(?:\.\w+)?")
SHELL_RE = re.compile(r"(?:ba|z|da|k|c|tc|fi)?sh")
PYTHON_RE = re.compile(r"(?:python|pypy)[\d.]*")
PROC_SUBST_RE = re.compile(r"(?:ba|z|da)?sh\s+<\(")
REDIRECT_RE = re.compile(r"(\d*&>>|\d*&>|\d*>>|\d*>\||\d*>)(\s*)([^\s;|&()]+)")
PIP_EXE_RE = re.compile(r"\S*pip3?")
DYNAMIC_TARGET_RE = re.compile(r"[$`*?{}\[\]]")
TAR_LONG_MODES = {"--create": "c", "--extract": "x", "--get": "x", "--list": "t",
                  "--update": "u", "--append": "r", "--delete": "d"}
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
OP_TOKENS = {"|", "||", "|&", "&&", ";", "(", ")", "&"}
SKIP_TOKENS = {">", ">>", ">|", "&>", "&>>", "<", "<<", "<<<", ">&", "<&"}
WRAPPERS = {"nice", "env", "timeout", "command", "builtin", "stdbuf", "nohup", "setsid"}
WRITE_ALL_ARGS = {"rm", "mv", "mkdir", "rmdir", "touch", "shred", "chattr", "truncate", "fallocate", "setfacl"}
WRITE_SKIP_FIRST = {"chmod", "chown", "chgrp"}   # 第一个位置参数是模式/属主，不是路径
WRITE_LAST_ARG = {"cp", "ln", "scp", "rsync", "install"}
TARGET_DIRECTORY_COMMANDS = {"cp", "ln", "install"}
TARGET_DIRECTORY_OPTIONS = {"-t", "--target-directory"}
SED_SCRIPT_OPTIONS = {"-e", "--expression", "-f", "--file"}
GZIP_CMDS = {"gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz", "zstd", "unzstd"}
FIND_WRITE_FLAGS = {"delete", "exec", "execdir", "ok", "okdir"}
CURL_WRITE_OPTIONS = {
    "-o", "--output", "--output-dir",
    "-D", "--dump-header", "-c", "--cookie-jar",
    "--trace", "--trace-ascii", "--stderr", "--etag-save",
    "--hsts", "--alt-svc", "--libcurl",
}
WGET_WRITE_OPTIONS = {
    "-O", "--output-document", "-P", "--directory-prefix",
    "-o", "--output-file", "-a", "--append-output",
    "--save-cookies", "--rejected-log", "--warc-file",
}
PYTHON_VALUE_OPTIONS = {"-W", "-X", "-Q", "--check-hash-based-pycs"}
SHELL_VALUE_OPTIONS = {"-O", "-o", "--rcfile", "--init-file"}
PERL_VALUE_OPTIONS = {"-I", "-m", "-M"}
RUBY_VALUE_OPTIONS = {"-C", "-E", "-F", "-I", "-K", "-r", "--encoding",
                      "--external-encoding", "--internal-encoding", "--require"}
NODE_VALUE_OPTIONS = {"-r", "-C", "--require", "--loader",
                      "--experimental-loader", "--conditions", "--import"}
PHP_VALUE_OPTIONS = {"-d", "-c", "-z", "--define", "--php-ini", "--zend-extension"}
INTERPRETER_VALUE_OPTIONS = {
    "perl": PERL_VALUE_OPTIONS,
    "ruby": RUBY_VALUE_OPTIONS,
    "node": NODE_VALUE_OPTIONS,
    "nodejs": NODE_VALUE_OPTIONS,
    "php": PHP_VALUE_OPTIONS,
}
CONSUMES = {
    "curl": CURL_WRITE_OPTIONS,
    "wget": WGET_WRITE_OPTIONS,
    "unzip": {"-d"},
    "mktemp": {"-p", "--tmpdir"},
    "truncate": {"-s", "--size"},
    "fallocate": {"-l", "--length"},
    "setfacl": {"-m", "-x", "-d", "--modify", "--remove"},
    "git": {"-C", "-c", "--config", "--git-dir", "--work-tree", "--namespace",
            "-b", "--branch", "--depth", "-o", "--origin", "-u", "--upload-pack",
            "--reference", "--reference-if-able", "--separate-git-dir",
            "--template", "--filter", "--shallow-since", "--shallow-exclude",
            "-j", "--jobs", "--server-option", "--negotiation-tip",
            "--revision", "--bundle-uri", "--ref-format", "--object-format"},
    "nice": {"-n"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "mkdir": {"-m", "--mode"},
    "env": {"-C", "--chdir"},
    "sed": SED_SCRIPT_OPTIONS,
}
CONSUMES.update({cmd: TARGET_DIRECTORY_OPTIONS for cmd in TARGET_DIRECTORY_COMMANDS})


def is_under(path, roots):
    return any(path == r or path.startswith(r + "/") for r in roots)


def norm_path(token, cwd, config):
    c = config
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


def command_consumes(cmd):
    """返回该命令需要消费下一个 token 的选项集合。"""
    if not cmd:
        return ()
    opts = CONSUMES.get(cmd)
    if opts is not None:
        return opts
    if PYTHON_RE.fullmatch(cmd):
        return PYTHON_VALUE_OPTIONS
    if SHELL_RE.fullmatch(cmd):
        return SHELL_VALUE_OPTIONS
    return INTERPRETER_VALUE_OPTIONS.get(cmd, ())


def scan_inline_options(raw_opts, letters, long_names=(), value_letters="", stop_letters=""):
    """只扫描命令自身的前导选项；脚本/模块位置参数由调用方提前截断。"""
    for raw in raw_opts:
        if raw.startswith("--"):
            if raw.partition("=")[0] in long_names:
                return "-" + letters[0].lower()
            continue
        if not raw.startswith("-") or raw == "-":
            continue
        for ch in raw[1:]:
            if ch in letters:
                return "-" + ch.lower()
            if ch in stop_letters:
                return None
            if ch in value_letters:
                break
    return None


def inline_execution(cmd, raw_opts):
    """返回内联执行的选项说明；None 表示不是已识别的内联执行。"""
    if SHELL_RE.fullmatch(cmd):
        return scan_inline_options(raw_opts, "c", value_letters="Oo")
    if PYTHON_RE.fullmatch(cmd):
        return scan_inline_options(raw_opts, "c", value_letters="WXQ",
                                   stop_letters="m")
    spec = {
        "perl": ("eE", (), "0dDFiIlmM"),
        "ruby": ("e", ("--eval",), "0CEFIKTWirx"),
        "node": ("ep", ("--eval", "--print"), "rC"),
        "nodejs": ("ep", ("--eval", "--print"), "rC"),
        "php": ("rR", (), "dczf"),
    }
    if cmd in spec:
        letters, long_names, value_letters = spec[cmd]
        return scan_inline_options(raw_opts, letters, long_names, value_letters)
    return None


def sed_in_place(raw_flags):
    """sed 的 -i 可在短选项簇中（-Ei/-ni），也支持 --in-place[=后缀]。"""
    for raw in raw_flags:
        if raw == "--in-place" or raw.startswith("--in-place="):
            return True
        if raw.startswith("-") and not raw.startswith("--") and "i" in raw[1:]:
            return True
    return False


def install_directory_mode(raw_flags):
    for raw in raw_flags:
        if raw == "--directory" or raw.startswith("--directory="):
            return True
        if raw.startswith("-") and not raw.startswith("--") and "d" in raw[1:]:
            return True
    return False


def env_chdir(raw):
    return raw == "-C" or raw.startswith("-C") or raw == "--chdir" or raw.startswith("--chdir=")


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


def find_write_violations(command, cwd, conda_env, config):
    """启发式返回越界写描述；空列表表示未发现明确越界写。

    本函数不是安全沙箱：内联执行、xargs/make、env -C 和动态目标只能保守拦截，
    不能证明命令安全，也不能阻止所有写盘方式。
    """
    c = config
    roots = (c["CODE_ROOT"], c["DATA_ROOT"]) + WRITE_EXEMPT_ROOTS
    cwd_n = norm_path(cwd or c["CODE_ROOT"], c["CODE_ROOT"], c)
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
        if DYNAMIC_TARGET_RE.search(token):
            add(f"{kind}: {token}（含变量/通配符，写目标未验证）")
            return
        p = norm_path(token, cwd_n, c)
        if is_under(p, DEVICE_TARGETS) or is_under(p, roots):
            return
        add(f"{kind}: {token}（解析为 {p}）")

    def check_cmd_pos(name):
        """命令位 token（已去路径）检查：系统级命令、带子命令的 mkfs。"""
        if name in SYSTEM_COMMANDS or MKFS_RE.fullmatch(name):
            add(f"系统级命令 {name}（修改系统状态，与路径无关）")
        elif name in UNVERIFIED_COMMANDS:
            add(f"命令 {name} 可能执行未经验证的写操作（需 confirmed=true）")

    def check_pip(exe, fl):
        """命令位是 pip 且带 install：按安装目标是否越界判定。"""
        if "/" in exe:
            if not is_under(norm_path(exe, cwd_n, c), roots):
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
        if is_under(norm_path(tok, cwd_n, c), DEVICE_TARGETS):
            continue                    # >/dev/null 之类
        write_ops += 1
        write_targets.append(("重定向写入", tok))

    cur, cur_raw, args, flags, expect, timeout_used = None, None, [], set(), None, False
    raw_flags, opt_values, options_done = [], {}, False
    leading_opts, positional_seen = [], False

    def take_option_value(opt, value):
        opt_values.setdefault(opt, []).append(value)
        if cur == "git":
            if opt == "-C":
                cd_targets.append(value)
            return                          # 其余 git 选项值不是位置参数
        if cur in ("nice", "env", "timeout"):
            return
        if (PYTHON_RE.fullmatch(cur or "") or SHELL_RE.fullmatch(cur or "")
                or cur in INTERPRETER_VALUE_OPTIONS):
            return
        if cur == "sed" and opt in SED_SCRIPT_OPTIONS:
            return
        if cur in TARGET_DIRECTORY_COMMANDS and opt in TARGET_DIRECTORY_OPTIONS:
            return
        args.append(value)

    def handle_simple(cmd, a, fl):
        nonlocal write_ops
        target_dirs = []
        if cmd in TARGET_DIRECTORY_COMMANDS:
            target_dirs = opt_values.get("-t", []) + opt_values.get("--target-directory", [])
        if target_dirs:
            write_ops += len(target_dirs)
            write_targets.extend(("写入", x) for x in target_dirs)
        elif cmd == "install" and install_directory_mode(raw_flags):
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
        elif cmd == "sed" and sed_in_place(raw_flags):
            scripts = set(opt_values) & SED_SCRIPT_OPTIONS
            files = a if scripts else a[1:]
            write_ops += len(files)
            write_targets.extend(("sed -i 写入", x) for x in files)
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
            write_targets.extend(("解压", x) for x in opt_values.get("-d", []))
        elif cmd == "curl":
            write_ops += 1
            for opt in CURL_WRITE_OPTIONS:
                write_targets.extend((f"curl {opt} 写入", x)
                                     for x in opt_values.get(opt, []))
        elif cmd == "wget":
            write_ops += 1
            for opt in WGET_WRITE_OPTIONS:
                write_targets.extend((f"wget {opt} 写入", x)
                                     for x in opt_values.get(opt, []))
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
                write_targets.extend(("git clone 独立 git 目录", x)
                                     for x in opt_values.get("--separate-git-dir", []))
        elif cmd == "cd" and a:
            cd_targets.append(a[-1])

    def end_cmd():
        nonlocal cur, cur_raw, args, flags, expect, timeout_used
        nonlocal raw_flags, opt_values, options_done, leading_opts, positional_seen
        if cur is not None and cur not in WRAPPERS:
            handle_simple(cur, args, flags)
            inline = inline_execution(cur, leading_opts)
            if inline:
                add(f"内联执行 {cur} {inline}（代码内容未验证，可能任意写盘）")
            if "install" in args:
                if PIP_EXE_RE.fullmatch(cur_raw or ""):
                    check_pip(cur_raw, flags)
                elif re.fullmatch(r"python[\d.]*", cur):
                    exe = next((x for x in args if PIP_EXE_RE.fullmatch(x)), None)
                    if exe:
                        check_pip(exe, flags)
        cur, cur_raw, args, flags, expect = None, None, [], set(), None
        timeout_used = False
        raw_flags, opt_values, options_done = [], {}, False
        leading_opts, positional_seen = [], False

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
            take_option_value(expect, tok)
            expect = None
            continue
        if tok == "--" and not options_done:
            options_done = True
            continue
        if tok.startswith("-") and tok != "-" and not options_done:
            raw = tok
            flags.add(raw.lstrip("-"))
            raw_flags.append(raw)
            if cur is not None and not positional_seen:
                leading_opts.append(raw)
            if cur == "env" and env_chdir(raw):
                add("env -C 改变目录后执行命令（写目标未验证）")
            opts = command_consumes(cur)
            if opts:
                opt, value = None, None
                if raw.startswith("--") and "=" in raw:
                    name, _, attached = raw.partition("=")
                    if name in opts:
                        opt, value = name, attached
                elif raw in opts:
                    expect = raw
                elif not raw.startswith("--"):
                    body = raw[1:]
                    for pos, ch in enumerate(body):
                        candidate = "-" + ch
                        if candidate in opts:
                            rest = body[pos + 1:]
                            if rest:
                                opt, value = candidate, rest
                            else:
                                expect = candidate
                            break
                if opt is not None:
                    take_option_value(opt, value)
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
            args, flags, raw_flags, opt_values = [], set(), [], {}
            leading_opts, positional_seen, options_done = [], False, False
            timeout_used = False
            continue
        args.append(tok)
        positional_seen = True
    end_cmd()

    for kind, tok in write_targets:
        check_target(kind, tok)
    if write_ops and not is_under(cwd_n, roots):
        add(f"cwd 越界: {cwd_n}（写操作将落在受保护区域外）")
    if write_ops and cd_targets:
        bad = [t for t in cd_targets if not is_under(norm_path(t, cwd_n, c), roots)]
        if bad:
            add("cd 到越界目录后执行写操作: " + ", ".join(bad))
    return viol


def confirm_text(viol):
    shown = "；".join(viol[:5])
    more = f" 等 {len(viol)} 处" if len(viol) > 5 else ""
    return f"越界写需用户同意：{shown}{more}\n同意后同参重试并加 confirmed=true。"