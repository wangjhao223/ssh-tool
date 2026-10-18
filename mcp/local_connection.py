#!/usr/bin/env python3
"""Project-local SSH state and interactive login. No remote installation."""
import fcntl
import hashlib
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SSH_CONFIG = os.path.join(PROJECT_ROOT, "mcp", "ssh_config")
CONF_FILE = os.path.join(PROJECT_ROOT, "mcp", "remote-ws.conf")
SERVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class LoginError(Exception):
    pass


def read_config():
    values = {}
    with open(CONF_FILE, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def configured_servers():
    names = []
    with open(SSH_CONFIG, encoding="utf-8") as stream:
        for line in stream:
            parts = line.split()
            if parts and parts[0].lower() == "host":
                for alias in parts[1:]:
                    if alias.startswith("srv-") and SERVER_RE.fullmatch(alias[4:]) and alias[4:] not in names:
                        names.append(alias[4:])
    return names


def resolve_server(server=None):
    name = (server or read_config().get("DEFAULT_SERVER", "")).strip()
    if name.startswith("srv-"):
        name = name[4:]
    if not name:
        raise LoginError("remote-ws.conf 缺少 DEFAULT_SERVER，请配置默认服务器。")
    if not SERVER_RE.fullmatch(name) or name not in configured_servers():
        raise LoginError(f"未知服务器 '{name}'。已配置: {', '.join(configured_servers())}")
    return name


def server_arg_desc():
    """Shared server-parameter description; the server list lives in status and errors."""
    try:
        default = resolve_server()
    except (OSError, LoginError):
        default = ""
    if default:
        return f"服务器名，可省略 srv- 前缀；省略时用默认服务器 {default}"
    return "服务器名，可省略 srv- 前缀；DEFAULT_SERVER 未配置或无效，调用时必须显式指定"


def runtime_dir():
    identity = hashlib.sha256(os.fsencode(PROJECT_ROOT)).hexdigest()[:16]
    path = f"/tmp/remote-ws-{os.getuid()}-{identity}"
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise LoginError(f"Unsafe local SSH state directory: {path}")
    os.chmod(path, 0o700)
    return path


def ssh_args():
    return ["ssh", "-F", SSH_CONFIG,
            "-o", f"ControlPath={runtime_dir()}/cm-%C",
            "-o", f"UserKnownHostsFile={PROJECT_ROOT}/mcp/known_hosts"]


def is_connected(host):
    """Local control-socket check; a local subprocess failure means not connected."""
    try:
        result = subprocess.run(ssh_args() + ["-O", "check", host],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def ssh_params(host):
    try:
        result = subprocess.run(ssh_args() + ["-G", host], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise LoginError("本地 ssh -G 读取项目 SSH 配置超过 10 秒没有返回；请检查本地 ssh 状态后重试。") from None
    except subprocess.SubprocessError as error:
        raise LoginError(f"读取项目 SSH 配置失败: {error}") from error
    except OSError as error:
        raise LoginError(f"无法执行本地 ssh: {error}") from error
    if result.returncode:
        raise LoginError("无法读取项目 SSH 配置: " + result.stderr.strip())
    values = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key in ("hostname", "port", "user"):
            values[key] = value
    return values


def classify_ssh_error(stderr):
    """Classify noninteractive SSH diagnostics, never login-window output."""
    text = stderr.lower()
    if "remote host identification has changed" in text or "host key verification failed" in text:
        return "host_key_error", "服务器主机密钥校验失败；先核实主机身份，不自动删除 known_hosts 记录。"
    if "could not resolve hostname" in text:
        return "dns_error", "无法解析服务器地址；检查项目 SSH 配置和本地网络。"
    if "connection refused" in text:
        return "connection_refused", "SSH 端口拒绝连接；检查地址、端口和服务器 SSH 服务。"
    if "connection timed out" in text or "operation timed out" in text:
        return "network_timeout", "SSH 连接超时；检查网络、VPN 和服务器可达性，重新输入密码不能解决此问题。"
    if "no route to host" in text or "network is unreachable" in text:
        return "network_unreachable", "网络不可达；检查本地网络、VPN 和路由。"
    if "permission denied (" in text or "authentication failed" in text or "too many authentication failures" in text:
        return "authentication_failed", "SSH 身份认证未通过；在独立登录窗口检查账号和认证提示，MCP 不读取窗口输出。"
    if any(value in text for value in ("broken pipe", "connection reset", "connection closed",
                                      "mux_client_request_session", "control socket connect")):
        return "connection_lost", "SSH 连接已失效或中断；先诊断，再由用户重新连接。"
    return None


def diagnose(server=None, timeout=3, probe_session=True):
    """Check one configured host; no login window, password prompt or writes."""
    name = resolve_server(server)
    host = "srv-" + name
    try:
        params = ssh_params(host)
    except LoginError as error:
        return {"server": name, "endpoint": host, "state": "local_error", "summary": str(error)}
    result = {"server": name, "endpoint": f"{params['user']}@{params['hostname']}:{params['port']}"}

    def state(code, summary):
        return {**result, "state": code, "summary": summary}

    if is_connected(host):
        if not probe_session:
            return state("session_present", "存在项目专用复用连接；尚未验证服务器链路。")
        try:
            check = subprocess.run(ssh_args() + ["-T", "-o", "BatchMode=yes", "-o",
                                   f"ConnectTimeout={timeout}", host, "true"],
                                   stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=timeout + 1)
        except subprocess.TimeoutExpired:
            return state("connection_lost", "项目复用连接没有及时响应；链路可能失效，请检查网络后重新连接。")
        if check.returncode == 0:
            return state("connected", "项目连接及远程 Shell 响应正常，无需重新登录。")
        err = check.stderr.decode("utf-8", "replace")
        classified = classify_ssh_error(err)
        if classified:
            return state(*classified)
        return state("session_probe_failed", f"已存在复用连接，但远程 Shell 检查返回 {check.returncode}；检查服务器登录环境。")
    if login_busy(host):
        return state("login_pending", "登录窗口正在处理；请在已有窗口完成登录，不重复打开窗口。")
    try:
        with socket.create_connection((params["hostname"], int(params["port"])), timeout=timeout):
            pass
    except socket.gaierror:
        return state("dns_error", "无法解析服务器地址；检查项目 SSH 配置。")
    except (TimeoutError, socket.timeout):
        return state("network_timeout", "SSH 端口连接超时；检查网络、VPN 和服务器状态，不弹出登录窗口。")
    except ConnectionRefusedError:
        return state("connection_refused", "SSH 端口拒绝连接；检查地址、端口和 SSH 服务，不弹出登录窗口。")
    except OSError as error:
        return state("network_unreachable", f"网络连接失败（errno={error.errno}）；检查本地网络、VPN 和路由。")
    return state("needs_login", "SSH 端口可达，但没有本项目的复用连接；需要在独立窗口登录。端口可达不代表身份认证成功。")


def preflight(server=None):
    result = diagnose(server, probe_session=False)
    if result["state"] not in ("needs_login", "session_present", "login_pending"):
        raise LoginError(f"[{result['state']}] {result['endpoint']}\n{result['summary']}")
    return result


def window_stamp(host):
    return os.path.join(runtime_dir(), f"window-{host}.stamp")


def login_busy(host):
    with open(os.path.join(runtime_dir(), f"login-{host}.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        try:
            return time.time() - os.stat(window_stamp(host)).st_mtime < 15
        except FileNotFoundError:
            return False


def login_window_state(host):
    """Login-window evidence only, no network access.

    working  - a login window is running (connect.sh holds the login lock) or was just opened
    starting - a window was opened but has not started working yet (stamp exists but is stale)
    ended    - no window is visible (connect.sh removed the stamp on exit)
    """
    if login_busy(host):
        return "working"
    if os.path.exists(window_stamp(host)):
        return "starting"
    return "ended"


def launch_window(server):
    powershell = shutil.which("powershell.exe")
    distro = os.environ.get("WSL_DISTRO_NAME")
    if not powershell or not distro:
        raise LoginError("当前环境无法打开 Windows 登录窗口，请使用项目的一键连接入口。")
    path = subprocess.run(["wslpath", "-w", os.path.join(PROJECT_ROOT, "scripts", "connect.ps1")],
                          capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    user = pwd.getpwuid(os.getuid()).pw_name
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path,
         "-Server", server, "-Distribution", distro, "-WslUser", user],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=15)
    if result.returncode:
        raise LoginError(f"无法打开登录窗口（exit={result.returncode}），请使用项目的一键连接入口。")


def request_login(server):
    """Open once and return; a short local stamp covers window-startup races."""
    server = resolve_server(server)
    host = 'srv-' + server
    if is_connected(host):
        return 'session_present'
    with open(os.path.join(runtime_dir(), f'request-{host}.lock'), 'a') as gate:
        try:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'login_pending'
        if is_connected(host):
            return 'session_present'
        if login_busy(host):
            return 'login_pending'
        preflight(server)
        stamp = window_stamp(host)
        with open(stamp, 'w'):
            pass
        try:
            launch_window(server)
        except Exception:
            os.unlink(stamp)
            raise
        return 'login_pending'


def ensure_connected(server, timeout=120):
    """Open at most one login window; passwords never enter this process."""
    server = resolve_server(server)
    host = "srv-" + server
    if is_connected(host):
        return
    with open(os.path.join(runtime_dir(), f"request-{host}.lock"), "a") as gate:
        try:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LoginError("此服务器的登录窗口已在处理，请完成登录后重试；不会重复打开窗口。")
        if is_connected(host):
            return
        already_open = login_busy(host)
        if not already_open:
            preflight(server)
            stamp = window_stamp(host)
            with open(stamp, 'w'):
                pass
            try:
                launch_window(server)
            except Exception:
                os.unlink(stamp)
                raise
        deadline = time.monotonic() + timeout
        seen_login = already_open
        started = time.monotonic()
        while time.monotonic() < deadline:
            if is_connected(host):
                return
            window = login_window_state(host)
            if window == "working":
                seen_login = True
                started = time.monotonic()
            elif window == "ended" and seen_login:
                # 窗口退出与复用连接就绪之间可能只差一瞬间，补一次检查再下结论
                time.sleep(1)
                if is_connected(host):
                    return
                report = preflight(server)
                if report["state"] == "session_present":
                    return
                raise LoginError("[login_incomplete] 登录窗口已结束但没有建立连接，可能是认证失败或用户取消；请查看登录窗口，不自动重试。")
            elif time.monotonic() - started > 15:
                if window == "starting":
                    raise LoginError("[login_nostart] 还没有窗口开始工作的证据：登录窗口已打开，但连接脚本一直没有开始连接。请在窗口里完成登录；窗口若已关闭，请用项目的一键连接入口重新打开。")
                raise LoginError("[login_nostart] 还没有窗口开始工作的证据：没有登录窗口在运行。请使用项目的一键连接入口打开登录窗口后重试。")
            time.sleep(1)
        raise LoginError("仍未检测到连接。请在登录窗口完成密码输入，然后重试原操作；本次远程命令尚未执行。")


if __name__ == "__main__":
    try:
        action = sys.argv[1] if len(sys.argv) > 1 else "runtime"
        name = sys.argv[2] if len(sys.argv) > 2 else None
        if action == "runtime":
            print(runtime_dir())
        elif action == "resolve-server":
            print(resolve_server(name))
        elif action == "preflight":
            preflight(name)
        else:
            raise LoginError(f"Unknown local action: {action}")
    except (LoginError, OSError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
