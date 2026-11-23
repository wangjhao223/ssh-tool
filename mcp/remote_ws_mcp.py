#!/usr/bin/env python3
"""remote-ws MCP stdio protocol, tool registry and compatibility entry points."""
import json
import os
import subprocess
import sys
import uuid

import local_connection
import file_tools
import job_tools
import guide_tools
import text_tools
import project_tools
from remote_ws import VERSION, commands, config, transport, write_guard
from remote_ws.validation import (REMOTE_RETURNED, UNKNOWN, RemoteWsError, ToolError,
                                  bad_args, format_failure)
from remote_ws.transport import (
    CONNECT_SCRIPT, HOST_PREFIX, MAX_OUTPUT, LimitedText, bounded_run,
    connect_cmd, fmt_result, not_connected_hint, truncate,
)
from remote_ws.commands import (
    DEFAULTS, GPU_SCRIPT, NAME_RE, conf, confirm_text, default_server,
    env_prelude, find_write_violations, qpath, remote_dir, resolve_server,
)

SERVER_NAME = "remote-ws"
SERVER_VERSION = VERSION
PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
NEUTRAL_BRANCH_FIELDS = {('status', 'timeout_sec'), ('diagnose', 'all'),
                         ('diagnose', 'verbose')}


def servers():
    return local_connection.configured_servers()


def ssh_params(host):
    return local_connection.ssh_params(host)


def connected(host):
    return local_connection.is_connected(host)


def ssh_run(name, script, timeout, max_output_chars=None):
    return transport.ssh_run(name, script, timeout, max_output_chars=max_output_chars)


def t_exec(args):
    return commands.t_exec(args, run=ssh_run)


def t_gpus(args):
    return commands.t_gpus(args, run=ssh_run)


def t_conda_envs(args):
    return commands.t_conda_envs(args, run=ssh_run)


def t_resource_query(args):
    if isinstance(args, dict) and args.get('resource') == 'conda' and args.get('name') == '':
        args = dict(args)
        args.pop('name')
    return commands.t_resource_query(args, run=ssh_run)


def checked_project(value, required=False):
    return job_tools.checked_project(value, required=required)


def log_dir(project, context=None):
    return job_tools.log_dir(project, context=context)


def t_run_background(args):
    return job_tools.t_run_background(args, run=ssh_run)


def list_jobs(name, limit=5, project=None, context=None):
    return job_tools.list_jobs(name, limit, project, context)


def find_job(name, job, project=None, unique=False, context=None):
    return job_tools.find_job(name, job, project, unique, context)


def t_jobs(args):
    return job_tools.t_jobs(args)


def t_tail_log(args):
    return job_tools.t_tail_log(args)


def t_job_query(args):
    return job_tools.t_job_query(args)


def t_stop_job(args):
    return job_tools.t_stop_job(args)


def t_init_project(args):
    return project_tools.t_init_project(args, run=ssh_run)


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
    requested = args.get("timeout_sec", 3)
    if type(requested) is not int or requested < 1:
        raise ToolError("timeout_sec 必须是 1 到 10 的整数")
    timeout = min(requested, 10)
    notes = []
    if requested > 10:
        notes.append(f"timeout_sec={requested} 超过 10 秒上限，已按 10 秒执行")
    report = local_connection.diagnose(name, timeout=timeout)
    text = (f"state: {report['state']}\nserver: {report['server']}\n"
            f"endpoint: {report['endpoint']}\n{report['summary']}")
    if notes:
        text += "\nnote: " + "；".join(notes)
    return text


def default_server_name():
    value = (os.environ.get('REMOTE_WS_SERVER') or conf().get('DEFAULT_SERVER') or '').strip()
    if value.startswith(HOST_PREFIX):
        value = value[len(HOST_PREFIX):]
    return value


def t_status(args):
    verbose = file_tools.verbosity(args)
    all_servers = args.get('all', False)
    if type(all_servers) is not bool:
        raise ToolError('all 必须是布尔值')
    requested = args.get('server')
    if all_servers and isinstance(requested, str) and requested.strip():
        raise ToolError("all=true 时不能同时指定 server；请省略 server 后重试，或改为 all=false 只查一台服务器。")
    default = default_server_name()
    if all_servers:
        server_names = servers()
        if not server_names:
            raise ToolError(f"ssh_config 里没有配置任何 {HOST_PREFIX}<名字> 服务器，无法列出状态。")
    else:
        server_names = [resolve_server(requested)[0]]
    lines = [f"default: {default or '(未配置 DEFAULT_SERVER/REMOTE_WS_SERVER)'}"]
    if all_servers and default not in server_names:
        lines.append(f"⚠️ 默认服务器无效，调用其他工具时请显式指定 server（可选: {', '.join(server_names)}）")
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
            try:
                p = ssh_params(host)
                endpoint = f" {p.get('user', '?')}@{p.get('hostname', '?')}:{p.get('port', '?')}"
            except local_connection.LoginError as error:
                endpoint = f" (本地 SSH 配置读取失败: {error})"
        lines.append(f"{sname}: {state}{endpoint}")
        if verbose:
            try:
                context = config.resolve_context(sname, allow_unconfigured=True)
            except local_connection.LoginError as error:
                lines.append(f"  configuration: 读取失败: {error}")
                continue
            if context.get('CONFIGURED', True):
                lines.append("  configured: true")
                for key in ('CODE_ROOT', 'DATA_ROOT', 'CONDA_ROOT'):
                    value = context.get(key)
                    lines.append(f"  {key}={value if value is not None else '(未配置)'}")
            else:
                lines.append("  configured: false")
                lines.append("  roots: 未配置（v3 仅保留服务器名录，不继承全局 roots/Conda）")
    return "\n".join(lines)


def branch_field_note(action, key, value):
    if action == 'status' and key == 'timeout_sec':
        if value != 0:
            raise ToolError(
                "timeout_sec 仅用于 action=diagnose（1 到 10 秒）；action=status 只读本地 socket，"
                "不接受超时设置。请删除 timeout_sec（或传 0），需要网络探测时改用 action=diagnose。")
        return "action=status 忽略 timeout_sec=0（仅 diagnose 使用该参数）"
    if action == 'diagnose' and key == 'all':
        if value is not False:
            raise ToolError("all 仅用于 action=status；action=diagnose 只诊断一台服务器。"
                            "请删除 all（或传 false）。")
        return "action=diagnose 忽略 all=false（仅 status 使用该参数）"
    if action == 'diagnose' and key == 'verbose':
        if value is not False:
            raise ToolError("verbose 仅用于 action=status；action=diagnose 输出固定诊断字段。"
                            "请删除 verbose（或传 false）。")
        return "action=diagnose 忽略 verbose=false（仅 status 使用该参数）"
    raise AssertionError(f"unexpected branch field: {action}/{key}")


def t_connection_query(args):
    if not isinstance(args, dict):
        raise ToolError("arguments 必须是对象")
    action = args.get('action')
    branches = {
        'status': (dict(server=str, all=bool, verbose=bool, timeout_sec=int), t_status),
        'diagnose': (dict(server=str, timeout_sec=int, all=bool, verbose=bool), t_diagnose),
    }
    if not isinstance(action, str) or action not in branches:
        raise ToolError("action 必须是 status 或 diagnose")
    properties, handler = branches[action]
    payload = {key: value for key, value in args.items() if key != 'action'}
    commands.validate_arguments(payload, properties)
    notes = [branch_field_note(action, key, payload[key])
             for key in payload if (action, key) in NEUTRAL_BRANCH_FIELDS]
    text = handler(payload)
    if notes:
        text += "\nnote: " + "；".join(notes)
    return text


def t_job_control(args):
    return job_tools.t_job_control(args, run=ssh_run)


SERVER_ARG = {"type": "string", "description": "服务器名；省略时用当前默认服务器"}
PROJECT_ARG = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
               "description": "项目名；start 必填"}
JOB_NAME_ARG = {"type": "string", "pattern": r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,63})?$",
                "description": "任务名"}
EXEC_CWD_ARG = {"type": "string", "description": "工作目录；默认 CODE_ROOT"}
JOB_CWD_ARG = {"type": "string", "description": "工作目录；默认 CODE_ROOT/<project>"}
ENV_ARG = {"type": "string", "description": "已有 Conda 环境名"}
GPU_ARG = {"type": ["string", "integer"], "pattern": r"^(\d+(,\d+)*)?$", "minimum": 0,
           "description": "GPU 编号；先查空闲卡"}
CONFIRMED_ARG = {"type": "boolean",
                 "description": "仅获准越界写后同参重试传 true"}


def tool_schema(properties, required):
    return dict(type='object', properties=properties, required=required,
                additionalProperties=False)


TOOLS = [
    ("connection_query", t_connection_query,
     "status 查本地连接；diagnose 探测网络/认证，不弹窗。",
     tool_schema(dict(
         action={'type': 'string', 'enum': ['status', 'diagnose'],
                 'description': 'status 本地状态；diagnose 网络/认证诊断'},
         server=SERVER_ARG,
         all={'type': 'boolean', 'description': '仅 status：列全部服务器'},
         verbose={'type': 'boolean', 'description': '仅 status：显示端点及配置'},
         timeout_sec={'type': 'integer', 'minimum': 0,
                      'description': '仅 diagnose；超过10秒截断；status 接受0'}),
         ['action'])),
    ("exec", t_exec,
     "远端 Bash；越界写须获准后同参 confirmed=true 重试。",
     tool_schema(dict(
         command={'type': 'string', 'minLength': 1, 'description': '远端 Bash 命令'},
         server=SERVER_ARG, cwd=EXEC_CWD_ARG, conda_env=ENV_ARG, gpus=GPU_ARG,
         timeout_sec={'type': 'integer', 'minimum': 1, 'default': 120,
                      'description': '秒；最多1800'},
         max_output_chars={'type': 'integer', 'minimum': 256, 'default': 2000,
                           'description': '输出字符预算；默认2000'},
         confirmed=CONFIRMED_ARG), ['command'])),
    ("job_query", t_job_query,
     "list 列任务；tail 续读日志。",
     tool_schema(dict(
         action={'type': 'string', 'enum': ['list', 'tail'], 'description': 'list 或 tail'},
         server=SERVER_ARG,
         project={'type': 'string', 'pattern': r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,63})?$",
                  'description': '项目过滤'},
         limit={'type': 'integer', 'minimum': 0, 'description': 'list 条数'},
         verbose={'type': 'boolean', 'description': 'list 增加详情'},
         job={'type': 'string', 'description': 'tail 任务名、ID、PID或日志路径'},
         lines={'type': 'integer', 'minimum': 0, 'description': 'tail 行数'},
         cursor={'type': 'string', 'maxLength': 4096, 'description': 'tail 续读游标'},
         compact={'type': 'boolean', 'description': 'tail 按项目和完整ID续读时省略重复身份字段'},
         max_chars={'type': 'integer', 'minimum': 0, 'description': 'tail 字符预算'},
         query={'type': 'string', 'maxLength': 256, 'description': 'tail 单行关键词'},
         errors={'type': 'boolean', 'description': 'tail 仅显示错误行'},
         context_lines={'type': 'integer', 'minimum': 0,
                        'description': 'tail 错误上下文行数，最多10'}),
         ['action'])),
    ("job_control", t_job_control,
     "启动须 command/name/显式 project；停止须完整任务ID；越界写须确认。",
     tool_schema(dict(
         action={'type': 'string', 'enum': ['start', 'stop'], 'description': '启动或停止任务'},
         server=SERVER_ARG, project=PROJECT_ARG,
         command={'type': 'string', 'description': '启动命令'},
         name=JOB_NAME_ARG, cwd=JOB_CWD_ARG, conda_env=ENV_ARG, gpus=GPU_ARG,
         confirmed=CONFIRMED_ARG,
         job={'type': 'string', 'description': 'stop 必须完整32位小写十六进制任务ID'},
         force={'type': 'boolean', 'description': '仅 stop：强制停止剩余进程'}),
         ['action'])),
    ("resource_query", t_resource_query,
     "gpus 查显存/属主/空闲卡；conda 列已有环境，details=true 才探测 Python/torch。",
     tool_schema(dict(
         resource={'type': 'string', 'enum': ['gpus', 'conda'], 'description': '查询 GPU 或 Conda'},
         server=SERVER_ARG,
         name={'type': 'string', 'pattern': r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,63})?$",
               'description': '仅 conda：环境名'},
         details={'type': 'boolean', 'description': '仅 conda：探测 Python/torch/CUDA'}),
         ['resource'])),
]
def compact_schema(schema):
    kept = {'type', 'enum', 'required', 'additionalProperties', 'oneOf',
            'minimum', 'maximum', 'default', 'minItems', 'maxItems',
            'pattern', 'minLength', 'maxLength', 'description'}
    value = {key: item for key, item in schema.items() if key in kept}
    if schema.get('type') == 'object':
        value['properties'] = {key: compact_schema(item)
                               for key, item in schema['properties'].items()}
        value['additionalProperties'] = False
    elif schema.get('type') == 'array':
        value['items'] = compact_schema(schema['items'])
    if 'oneOf' in schema:
        value['oneOf'] = [compact_schema(item) for item in schema['oneOf']]
    return value


TOOLS.extend(file_tools.tools())
TOOLS.extend(guide_tools.tools())
TOOLS = [(name, handler, description, compact_schema(schema))
         for name, handler, description, schema in TOOLS]
TOOL_MAP = {name: fn for name, fn, _, _ in TOOLS}


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
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
                "instructions": "密码仅用户在终端输入，不索取/保存；禁装服务器组件。写限所选服务器授权 CODE_ROOT/DATA_ROOT/临时目录；越界先获准，再同参 confirmed=true。超时/断线/部分失败状态不确定，先查任务/日志/文件，勿重复执行。任务启动须在本次参数中显式提供合法 project，不取 REMOTE_WS_PROJECT；停止的 job 仅接受完整32位小写十六进制任务ID，查询不受此限。下载按 REMOTE_WS_TASK 隔离（默认 default 共享池），同 TASK 可并行，resume 仅锁定下载 ID；勿关闭共享 SSH。"}
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
            arguments = params.get('arguments', {})
            if not isinstance(arguments, dict):
                raise bad_args(ToolError, "arguments 必须是对象")
            res = fn(arguments)
            text, is_error = res if isinstance(res, tuple) else (res, False)
            if is_error:
                text = format_failure('REMOTE_NONZERO', REMOTE_RETURNED, text)
        except RemoteWsError as e:
            text, is_error = format_failure(e.code or 'TOOL_ERROR', e.status, str(e)), True
        except subprocess.TimeoutExpired:
            text, is_error = format_failure('LOCAL_TIMEOUT', UNKNOWN, '先查询目标状态，不要自动重试。'), True
        except Exception as e:  # noqa: BLE001
            text = format_failure('INTERNAL_ERROR', UNKNOWN,
                                  f'{type(e).__name__}；先查询目标状态，不要自动重试。')
            is_error = True
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