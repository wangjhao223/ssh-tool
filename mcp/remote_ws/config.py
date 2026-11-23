"""Per-server execution configuration and local directory validation."""
import os
import re
import stat

from .validation import LoginError


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
SERVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def user_path(override, xdg_name, fallback, suffix):
    value = os.environ.get(override)
    if value:
        path = os.path.expanduser(value)
    else:
        base = os.environ.get(xdg_name)
        path = os.path.join(os.path.expanduser(base), suffix) if base else os.path.expanduser(fallback + "/" + suffix)
    if not os.path.isabs(path) or os.path.normpath(path) != path or path == "/":
        raise LoginError(f"{override or xdg_name} 必须解析为规范的绝对路径")
    return path


def ensure_user_dir(path):
    current = "/"
    for part in path.split("/"):
        if not part:
            continue
        current = os.path.join(current, part)
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(current)
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise LoginError(f"Unsafe local directory: {current}")
        if current == path:
            if st.st_uid != os.getuid():
                raise LoginError(f"Local directory is not owned by the current user: {path}")
            os.chmod(path, 0o700)
    return path


CONFIG_DIR = user_path("REMOTE_WS_CONFIG_DIR", "XDG_CONFIG_HOME", "~/.config", "remote-ws")
STATE_DIR = user_path("REMOTE_WS_STATE_DIR", "XDG_STATE_HOME", "~/.local/state", "remote-ws")
DOWNLOAD_ROOT = user_path("REMOTE_WS_DOWNLOAD_ROOT", "XDG_DATA_HOME", "~/.local/share", "remote-ws/downloads")
SSH_CONFIG = os.path.join(CONFIG_DIR, "ssh_config")
CONF_FILE = os.path.join(CONFIG_DIR, "remote-ws.conf")
KNOWN_HOSTS = os.path.join(CONFIG_DIR, "known_hosts")


CONFIG_KEYS = {"DEFAULT_SERVER", "CODE_ROOT", "DATA_ROOT", "CONDA_ROOT"}
PATH_CONFIG_KEYS = {"CODE_ROOT", "DATA_ROOT", "CONDA_ROOT"}
DEFAULT_ROOTS = {
    "CODE_ROOT": "/home/wjh",
    "DATA_ROOT": "/data/wjh",
    "CONDA_ROOT": "/home/wjh/miniconda3",
}
MAX_CONFIG_BYTES = 64 * 1024


def check_config_dir():
    try:
        st = os.lstat(CONFIG_DIR)
    except FileNotFoundError:
        raise LoginError(f"配置目录不存在: {CONFIG_DIR}") from None
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise LoginError(f"配置目录不能是符号链接或非目录: {CONFIG_DIR}")
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise LoginError(f"配置目录须属当前用户且权限为 0700: {CONFIG_DIR}")
    return CONFIG_DIR


def _read_text(path, optional=False):
    directory = None
    descriptor = None
    try:
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        parts = path.split("/")
        current = ""
        for part in parts[1:-1]:
            current += "/" + part
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            os.close(directory)
            directory = following
            if current in (CONFIG_DIR, os.path.join(CONFIG_DIR, "servers.d")):
                info = os.fstat(directory)
                forbidden = 0o077 if current == CONFIG_DIR else 0o022
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & forbidden:
                    raise LoginError(f"配置目录属主或权限不安全: {current}")
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise LoginError(f"配置必须是普通文件: {path}")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise LoginError(f"配置文件须属当前用户且不可由组或其他用户写入: {path}")
        if info.st_size > MAX_CONFIG_BYTES:
            raise LoginError(f"配置文件过大（最多 {MAX_CONFIG_BYTES} 字节）: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(MAX_CONFIG_BYTES + 1)
        if len(content) > MAX_CONFIG_BYTES:
            raise LoginError(f"配置文件过大（最多 {MAX_CONFIG_BYTES} 字节）: {path}")
        return content.decode("utf-8")
    except FileNotFoundError as error:
        if optional:
            return None
        raise LoginError(f"配置文件不存在: {path}") from error
    except UnicodeDecodeError as error:
        raise LoginError(f"配置文件必须是 UTF-8: {path}") from error
    except OSError as error:
        raise LoginError(f"无法安全读取配置文件（禁止符号链接）: {path}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _parse_config(content, label):
    values, malformed = {}, []
    for line_number, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            malformed.append(line_number)
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key in values:
            raise LoginError(f"{label} 第 {line_number} 行重复配置: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values, malformed


def _validate_path(key, value, label, allow_empty=False):
    if allow_empty and value == "":
        return
    if (not isinstance(value, str) or not value or not os.path.isabs(value)
            or os.path.normpath(value) != value or value.startswith("//") or value == "/"
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise LoginError(f"{label} 的 {key} 必须是规范的绝对路径且不能为根目录")


def read_config():
    check_config_dir()
    values, malformed = _parse_config(_read_text(CONF_FILE), "remote-ws.conf")
    version = values.get("FORMAT_VERSION", "1")
    if version not in ("1", "2", "3"):
        raise LoginError(f"不支持的 FORMAT_VERSION: {version!r}")
    if version != "1" and malformed:
        raise LoginError(f"remote-ws.conf 第 {malformed[0]} 行配置格式错误")
    allowed = {"FORMAT_VERSION", "DEFAULT_SERVER"} if version == "3" else CONFIG_KEYS | {"FORMAT_VERSION"}
    if version == "2":
        allowed = allowed | {"CONDA_INIT"}
    for key, value in values.items():
        legacy_init = version == "1" and re.fullmatch(r"CONDA_INIT_[A-Z0-9][A-Z0-9_.-]{0,63}", key)
        if key not in allowed and not legacy_init:
            raise LoginError(f"remote-ws.conf 未知配置项: {key}")
        if key in PATH_CONFIG_KEYS or key == "CONDA_INIT" or legacy_init:
            _validate_path(key, value, "remote-ws.conf",
                           allow_empty=version == "1" or key == "CONDA_INIT")
    return values


def configured_servers():
    check_config_dir()
    names = []
    for line in _read_text(SSH_CONFIG).splitlines():
        parts = line.split()
        if parts and parts[0].lower() == "host":
            for alias in parts[1:]:
                if alias.startswith("srv-") and SERVER_RE.fullmatch(alias[4:]) and alias[4:] not in names:
                    names.append(alias[4:])
    return names


def configured_project():
    value = os.environ.get("REMOTE_WS_PROJECT", "").strip()
    if not value:
        return None
    if not SERVER_RE.fullmatch(value):
        raise LoginError("REMOTE_WS_PROJECT 只能包含字母、数字、_ . -，且最多 64 字符")
    return value


def _select_server(server, values=None):
    if server:
        value, source = server, "explicit"
    elif os.environ.get("REMOTE_WS_SERVER"):
        value, source = os.environ["REMOTE_WS_SERVER"], "REMOTE_WS_SERVER"
    else:
        if values is None:
            values = read_config()
        value, source = values.get("DEFAULT_SERVER", ""), "DEFAULT_SERVER"
    if not isinstance(value, str):
        raise LoginError("server 和 REMOTE_WS_SERVER 必须是字符串")
    name = value.strip()
    if name.startswith("srv-"):
        name = name[4:]
    if not name:
        raise LoginError("未设置默认服务器；请配置 DEFAULT_SERVER 或项目级 REMOTE_WS_SERVER。")
    servers = configured_servers()
    if not SERVER_RE.fullmatch(name) or name not in servers:
        raise LoginError(f"未知服务器 '{name}'。已配置: {', '.join(servers)}")
    return name, source


def resolve_server(server=None):
    return _select_server(server)[0]


def _effective_config(name, values, allow_unconfigured=False):
    version = values.get("FORMAT_VERSION", "1")
    result = {} if version == "3" else dict(DEFAULT_ROOTS)
    result.update({key: values[key] for key in PATH_CONFIG_KEYS if values.get(key)})
    if version == "1":
        result["CONDA_INIT"] = values.get("CONDA_INIT_" + name.upper(), "")
        return result
    if version == "2":
        result["CONDA_INIT"] = values.get("CONDA_INIT", "")
    path = os.path.join(CONFIG_DIR, "servers.d", name + ".conf")
    content = _read_text(path, optional=True)
    required = PATH_CONFIG_KEYS | {"CONDA_INIT"}
    if content is not None:
        overrides, malformed = _parse_config(content, path)
        if malformed:
            raise LoginError(f"{path} 第 {malformed[0]} 行配置格式错误")
        for key, value in overrides.items():
            if key not in required:
                raise LoginError(f"{path} 未知配置项: {key}")
            _validate_path(key, value, path, allow_empty=key == "CONDA_INIT")
        result.update(overrides)
    if version == "3" and required - result.keys():
        if allow_unconfigured:
            return None
        reason = "配置文件不存在" if content is None else "缺少配置项: " + ", ".join(sorted(required - result.keys()))
        raise LoginError(f"服务器 '{name}' 未配置: {path}（{reason}）")
    return result


def effective_config(server):
    """Return the selected server's directory and Conda settings, without project overrides."""
    values = read_config()
    name, _ = _select_server(server, values)
    return _effective_config(name, values)


def resolve_context(server=None, project=None, allow_unconfigured=False):
    """Resolve trusted settings, optionally exposing an unconfigured server for local help."""
    values = read_config()
    name, server_source = _select_server(server, values)
    if project is None:
        project = configured_project()
        project_source = "REMOTE_WS_PROJECT" if project is not None else None
    else:
        if not isinstance(project, str) or not SERVER_RE.fullmatch(project.strip()):
            raise LoginError("project 只能包含字母、数字、_ . -，且最多 64 字符")
        project = project.strip()
        project_source = "explicit"
    settings = _effective_config(name, values, allow_unconfigured=allow_unconfigured)
    context = {
        "SERVER": name,
        "PROJECT": project,
        "SERVER_SOURCE": server_source,
        "PROJECT_SOURCE": project_source,
    }
    if values.get("FORMAT_VERSION") == "3":
        context["CONFIGURED"] = settings is not None
    context.update(settings if settings is not None else
                   {key: None for key in PATH_CONFIG_KEYS | {"CONDA_INIT"}})
    return context


def trusted_roots(context):
    """Return only code, data and temporary roots, never the project or Conda root."""
    roots = [context["CODE_ROOT"], context["DATA_ROOT"]]
    for key, value in zip(("CODE_ROOT", "DATA_ROOT"), roots):
        _validate_path(key, value, "context")
    return roots + ["/tmp", "/var/tmp", "/dev/shm"]


def server_arg_desc():
    """Shared server-parameter description without reading runtime configuration."""
    return "服务器名，可省略 srv- 前缀；省略按项目/共享配置的服务器选择项 REMOTE_WS_SERVER / DEFAULT_SERVER 选择；v3 执行配置仅取所选服务器独立文件"
