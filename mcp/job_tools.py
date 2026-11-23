"""Background job orchestration and in-memory process identity helpers."""
import json
import os
import re
import shlex
import uuid

import file_tools
import local_connection
from remote_ws import commands, config, transport
from remote_ws.sources.jobs import HEADER_SOURCE, CURSOR_HELPERS, REMOTE_SOURCE
from remote_ws.validation import NOT_RUN, ToolError


def request(server, operation, **params):
    value = transport.remote_json(server, dict(operation=operation, roots=file_tools.roots_config(server), **params),
                                   timeout=60, source=REMOTE_SOURCE)
    value.pop('ok', None)
    if operation == 'tail':
        job = params.get('job_meta') or {}
        if job.get('job_id'):
            value['task_id'] = job['job_id'][:12]
    return value


def checked_project(value, required=False):
    if value is None:
        try:
            value = local_connection.configured_project()
        except local_connection.LoginError as error:
            raise ToolError(str(error)) from error
    if value is None and not required:
        return None
    project = value.strip() if isinstance(value, str) else ""
    if not commands.NAME_RE.fullmatch(project):
        raise ToolError("project 必填（或设置 REMOTE_WS_PROJECT），且只能包含字母、数字、_ . -")
    return project


def log_dir(project, context=None):
    c = commands.conf() if context is None else context
    return f"{c['DATA_ROOT']}/{checked_project(project, required=True)}/logs"


def t_run_background(args, *, run=None):
    run = transport.ssh_run if run is None else run
    command = (args.get("command") or "").strip()
    job = (args.get("name") or "").strip()
    project = checked_project(args.get("project"), required=True)
    if not command:
        raise ToolError("command 不能为空")
    if not commands.NAME_RE.fullmatch(job):
        raise ToolError("name 只能包含字母、数字、_ . -，例如 train-resnet")
    gpus = commands.normalize_gpus(args["gpus"]) if "gpus" in args else None
    context = config.resolve_context(args.get("server"), project)
    name = context['SERVER']
    project = context['PROJECT']
    default_cwd = f"{context['CODE_ROOT'].rstrip('/') or '/'}/{project}"
    rdir = commands.remote_dir(args.get("cwd"), context) if args.get("cwd") else default_cwd
    ldir = log_dir(project, context)
    viol = commands.find_write_violations(command, rdir, args.get("conda_env"), context)
    if viol and args.get("confirmed") is not True:
        raise ToolError(commands.confirm_text(viol), code="CONFIRM_REQUIRED", status=NOT_RUN)
    inner = "\n".join(commands.env_prelude(args.get("conda_env"), gpus, name, context) + [command])
    job_id = uuid.uuid4().hex
    meta = json.dumps(dict(job_id=job_id, name=job, project=project, cwd=rdir,
                          conda_env=args.get('conda_env') or '-', gpus=gpus or '-'))
    job_script = (
        f"export REMOTE_WS_JOB_META={shlex.quote(meta)}\n"
        f"python3 -c {shlex.quote(HEADER_SOURCE)} || exit 95\n"
        f"printf '# cmd: %s\\n' {shlex.quote(command)}\n"
        f"( {inner}\n)\n"
        "rc=$?\n"
        f"echo \"# remote-ws exit=$rc job_id={job_id} end=$(date -Is)\"\n")
    script = (
        f"mkdir -p {commands.qpath(ldir)} || exit 96\n"
        f"cd {commands.qpath(rdir)} || {{ echo 'remote-ws: 目录不存在: {rdir}' >&2; exit 97; }}\n"
        f"log={commands.qpath(ldir)}/{job}-$(date +%Y%m%d-%H%M%S)-{job_id[:12]}.log\n"
        f"setsid bash -c {shlex.quote(job_script)} > \"$log\" 2>&1 < /dev/null &\n"
        "pid=$!\nsleep 1\n"
        "if kill -0 $pid 2>/dev/null; then st=running; else st=exited; fi\n"
        "echo \"pid=$pid status=$st log=$log\"\n"
        )
    rc, out, err, el = run(name, script, 60, max_output_chars=6000)
    stamp = f"⚠️ 用户已确认越界写操作: {'；'.join(viol)}\n" if viol else ""
    if rc != 0:
        return stamp + transport.fmt_result(rc, out, err, el), True
    return stamp + f"server={name} project={project} name={job} task_id={job_id}\n{out.strip()}", False


def list_jobs(name, limit=5, project=None, context=None):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ToolError('limit 必须为 1 到 500 的整数')
    c = config.resolve_context(name, project) if context is None else context
    name = c['SERVER']
    project = c['PROJECT']
    return request(name, 'jobs', data_root=c['DATA_ROOT'], limit=limit,
                   project=project)['jobs']


def t_jobs(args):
    args = dict(args)
    if args.get('project') == '':
        args.pop('project')
    verbose = file_tools.verbosity(args)
    context = config.resolve_context(args.get("server"), args.get("project"))
    name = context['SERVER']
    project = context['PROJECT']
    requested_limit = args.get("limit", 5)
    clamped_limit = type(requested_limit) is int and requested_limit > 500
    limit = 500 if clamped_limit else requested_limit
    jobs = list_jobs(name, limit, project, context)
    if not jobs:
        scope = f"项目 {project} " if project else ""
        result = f"{name} 的{scope}没有 job_control(action=start) 启动过的任务。"
        return result + "\nparameter_note: limit 超过 500，已按 500 执行。" if clamped_limit else result
    lines = [f"{name} 上的后台任务（新→旧）:"]
    for j in jobs:
        project_name = j.get('project') or '-'
        if verbose:
            detail = (f" start={j.get('start', '-')} last_write={j.get('last_write', '-')} "
                      f"gpus={j.get('gpus', '-')} env={j.get('conda_env', '-')}")
            lines.append(f"- {j['name']} [{j['status']}] project={project_name} id={j.get('job_id') or 'legacy'} pid={j['pid']}{detail}\n  log: {j['log']}")
        else:
            jid = (j.get('job_id') or 'legacy')[:12]
            lines.append(f"{j['name']} [{j['status']}] project={project_name} id={jid} pid={j['pid']} log={os.path.basename(j['log'])}")
    if clamped_limit:
        lines.append("parameter_note: limit 超过 500，已按 500 执行。")
    return transport.truncate("\n".join(lines))


def find_job(name, job, project=None, unique=False, context=None):
    c = config.resolve_context(name, project) if context is None else context
    name = c['SERVER']
    project = c['PROJECT']
    if job.startswith("/"):
        job = os.path.normpath(job)
        expected = f"{c['DATA_ROOT']}/{project}/logs/" if project else None
        if expected and not job.startswith(expected):
            raise ToolError("日志路径不在指定项目的 logs 目录内")
        record = request(name, 'job', log=job)['job']
        if record:
            return record
        return {"log": job, "pid": None, "name": None, "status": "?", "project": project}
    if re.fullmatch(r'[a-f0-9]{12}(?:[a-f0-9]{20})?', job):
        historical = request(name, 'jobs', data_root=c['DATA_ROOT'], limit=None,
                             project=project, id_prefix=job[:12])['jobs']
        matches = [j for j in historical if (j.get('job_id') or '') == job] if len(job) == 32 else [
            j for j in historical if (j.get('job_id') or '').startswith(job)]
        if unique and len(matches) > 1:
            raise ToolError(f"任务 ID '{job}' 有 {len(matches)} 个候选；停止操作必须使用唯一日志路径。")
        if len(job) == 12 and len(matches) > 1:
            raise ToolError(f"任务 id 前缀 '{job}' 匹配到 {len(matches)} 个任务，请改用完整 id 或日志路径。")
        if matches:
            return matches[0]
    jobs = list_jobs(name, 200, project, c)
    matches = [j for j in jobs if j["name"] == job or os.path.basename(j["log"]) == job
               or j["pid"] == job or j.get('job_id') == job]
    if unique and len(matches) > 1:
        raise ToolError(f"任务 '{job}' 有 {len(matches)} 个候选；停止操作必须使用完整任务 ID 或唯一日志路径。")
    if matches:
        return matches[0]
    scope = f"项目 {project} 中、" if project else ""
    raise ToolError(f"在 {name} 上{scope}找不到任务 '{job}'。先用 job_query(action=list) 查看。")


def t_tail_log(args):
    args = dict(args)
    if args.get('project') == '':
        args.pop('project')
    for key in ('query', 'cursor'):
        if args.get(key) == '':
            args.pop(key)
    requested_lines = args.get('lines', 10)
    requested_cap = args.get('max_chars', 2000)
    requested_context_lines = args.get('context_lines', 2)
    clamped_lines = type(requested_lines) is int and requested_lines > 2000
    clamped_cap = type(requested_cap) is int and requested_cap > 12000
    clamped_context_lines = type(requested_context_lines) is int and requested_context_lines > 10
    if clamped_lines:
        args['lines'] = 2000
    if clamped_cap:
        args['max_chars'] = 12000
    if clamped_context_lines:
        args['context_lines'] = 10
    context = config.resolve_context(args.get("server"), args.get("project"))
    name = context['SERVER']
    job = (args.get("job") or "").strip()
    if not job:
        raise ToolError("job 不能为空（任务名、pid 或日志绝对路径）")
    lines = file_tools.integer(args, 'lines', 10, 1, 2000)
    cap = file_tools.integer(args, 'max_chars', 2000, 256, 12000)
    cursor = args.get('cursor')
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
        raise ToolError('无效日志游标')
    query=args.get('query')
    errors=args.get('errors',False)
    if type(errors) is not bool or (query is not None and
        (not isinstance(query,str) or not query or len(query)>256 or '\n' in query or '\x00' in query)) or (query and errors):
        raise ToolError('query 为单行字面关键词，errors 为布尔值，两者不混用')
    context_lines=file_tools.integer(args,'context_lines',2,0,10)
    options = dict(lines=lines, max_chars=cap, cursor=cursor, source_server=name,
                   query=query, errors=errors, context_lines=context_lines)
    if context['PROJECT'] and re.fullmatch(r'[a-f0-9]{32}', job):
        result = request(name, 'tail_id', project=context['PROJECT'], job_id=job,
                         data_root=context['DATA_ROOT'], **options)
        if (args.get('compact') and args.get('project') and cursor and
                result.get('job_id') == job and result.get('project') == args['project']):
            for key in ('task_id', 'job_id', 'log', 'project'):
                result.pop(key, None)
    else:
        j = find_job(name, job, context['PROJECT'], context=context)
        result = request(name, 'tail', log=j['log'], job_meta=j, **options)
        result.update(task_id=(j.get('job_id') or '')[:12], job_id=j.get('job_id'),
                      project=j.get('project', context['PROJECT']), log=j['log'], status=j['status'])
    notes = []
    if clamped_lines:
        notes.append("lines 超过 2000，已按 2000 执行")
    if clamped_cap:
        notes.append("max_chars 超过 12000，已按 12000 执行")
    if clamped_context_lines:
        notes.append("context_lines 超过 10，已按 10 执行")
    if notes:
        result["parameter_note"] = "；".join(notes)
    return json.dumps(result, ensure_ascii=False, separators=(',', ':'))


def t_job_control(args, *, run=None):
    if not isinstance(args, dict):
        raise ToolError("arguments 必须是对象")
    action = args.get('action')
    required_by_action = {'start': ('command', 'name', 'project'), 'stop': ('job',)}
    if not isinstance(action, str) or action not in required_by_action:
        raise ToolError("action 必须是 start 或 stop")
    properties = dict(server=str, project=str, command=str, name=str, cwd=str,
                      conda_env=str, gpus=(str, int), confirmed=bool, job=str, force=bool)
    required = required_by_action[action]
    payload = {key: value for key, value in args.items() if key != 'action'}
    commands.validate_arguments(payload, properties, required)
    for key in required:
        if not payload[key].strip():
            raise ToolError(f"action={action} 时 {key} 必填且须为非空字符串")
    if action == 'start':
        if not commands.NAME_RE.fullmatch(payload['project'].strip()):
            raise ToolError("action=start 时须显式提供合法 project，只能包含字母、数字、_ . -；不使用 REMOTE_WS_PROJECT")
        intent = []
        if 'job' in payload and payload['job'] != '':
            intent.append('job')
        if 'force' in payload and payload['force'] is not False:
            intent.append('force=true')
        if intent:
            raise ToolError("action=start 仅兼容中性跨分支字段 job='' 与 force=false，不接受非空字段: "
                            + ", ".join(intent) + "；job/force 只用于 action=stop，停止请提供完整32位任务ID。")
        if "gpus" in payload:
            payload["gpus"] = commands.normalize_gpus(payload["gpus"])
        return t_run_background(payload, run=run)
    if not re.fullmatch(r'[a-f0-9]{32}', payload['job']):
        raise ToolError("action=stop 时 job 必须是完整32位小写十六进制任务ID，不接受短ID、名称、PID或日志路径")
    intent = []
    for key in ('command', 'name', 'cwd', 'conda_env', 'gpus'):
        if key in payload and payload[key] != '':
            intent.append(key if key != 'gpus' or not isinstance(payload[key], int) else f"{key}={payload[key]}")
    if 'confirmed' in payload and payload['confirmed'] is not False:
        intent.append('confirmed=true')
    if intent:
        raise ToolError("action=stop 仅兼容中性跨分支字段 command/name/cwd/conda_env/gpus 为空字符串且 confirmed=false，"
                        "不接受非空字段: " + ", ".join(intent) + "；这些字段只用于 action=start。")
    return t_stop_job(payload)


def neutral_label(key, value):
    if isinstance(value, str):
        return f"{key}=''"
    if value is False:
        return f"{key}=false"
    return f"{key}={value}"


def t_job_query(args):
    if not isinstance(args, dict):
        raise ToolError("arguments 必须是对象")
    action = args.get('action')
    list_fields = {'server', 'project', 'limit', 'verbose'}
    tail_fields = {'server', 'project', 'job', 'lines', 'cursor', 'max_chars',
                   'query', 'errors', 'context_lines', 'compact'}
    branches = {
        'list': (list_fields | tail_fields, t_jobs),
        'tail': (list_fields | tail_fields, t_tail_log),
    }
    if not isinstance(action, str) or action not in branches:
        raise ToolError("action 必须是 list 或 tail")
    allowed, handler = branches[action]
    invalid = set(args) - allowed - {'action'}
    if invalid:
        raise ToolError(f"action={action} 不允许参数: {', '.join(sorted(invalid))}")
    if action == 'tail' and (not isinstance(args.get('job'), str) or not args['job'].strip()):
        raise ToolError("action=tail 时 job 必填且须为非空字符串")
    payload = dict(args)
    if action in ('list', 'tail') and payload.get('project') == '':
        payload.pop('project')
    if action == 'tail':
        for key in ('query', 'cursor'):
            if payload.get(key) == '':
                payload.pop(key)
    types = dict(server=str, project=str, limit=int, verbose=bool, job=str,
                 lines=int, cursor=str, max_chars=int, query=str, errors=bool,
                 context_lines=int, compact=bool)
    payload.pop('action', None)
    commands.validate_arguments(payload, {key: types[key] for key in allowed})
    if action == 'list':
        neutral, intent = [], []
        for key in ('job', 'cursor', 'query'):
            if key in payload:
                if payload[key] == '':
                    neutral.append(neutral_label(key, payload[key]))
                else:
                    intent.append(key)
        for key in ('errors', 'compact'):
            if key in payload:
                if payload[key] is False:
                    neutral.append(neutral_label(key, False))
                else:
                    intent.append(key)
        for key in ('context_lines', 'lines', 'max_chars'):
            if key in payload:
                if payload[key] == 0:
                    neutral.append(neutral_label(key, payload[key]))
                else:
                    intent.append(key)
        if intent:
            raise ToolError("action=list 仅兼容 tail 中性字段 job=''/cursor=''/query=''、"
                            "errors=false、context_lines=0、lines=0、max_chars=0；"
                            f"不接受非中性字段: {', '.join(intent)}；读取日志请改用 action=tail。")
        for key in ('job', 'cursor', 'query', 'errors', 'compact', 'context_lines', 'lines', 'max_chars'):
            payload.pop(key, None)
        result = handler(payload)
        if neutral:
            result += ("\nparameter_note: action=list 已在本地忽略 tail 中性字段 "
                       + "、".join(neutral) + "（未改变 list 操作）。")
        return result
    neutral, intent = [], []
    if 'limit' in payload:
        if payload['limit'] == 0:
            neutral.append('limit=0')
        else:
            intent.append('limit')
    if 'verbose' in payload:
        if payload['verbose'] is False:
            neutral.append('verbose=false')
        else:
            intent.append('verbose=true')
    if intent:
        raise ToolError("action=tail 仅兼容 list 中性字段 limit=0 与 verbose=false；"
                        f"不接受非中性字段: {', '.join(intent)}；列出任务请改用 action=list。")
    payload.pop('limit', None)
    payload.pop('verbose', None)
    result = handler(payload)
    if neutral:
        value = json.loads(result)
        value["parameter_note"] = ("action=tail 已在本地忽略 list 中性字段 "
                                   + "、".join(neutral) + "（未改变 tail 操作）。")
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return result


def t_stop_job(args):
    context = config.resolve_context(args.get("server"), args.get("project"))
    name = context['SERVER']
    j = find_job(name, (args.get("job") or "").strip(), context['PROJECT'], unique=True, context=context)
    if j['status'] in ('legacy/unverified', 'identity_mismatch'):
        raise ToolError(f"任务身份无法核实（{j['status']}），未发送信号；先核实实际进程。")
    if j["status"] != "running" or not j.get("pid"):
        return f"任务 {j.get('name')} 当前不在运行（{j['status']}），无需停止。"
    result = request(name, 'stop', log=j['log'], expected=j, force=args.get('force') is True)
    result.pop("ok", None)
    sent = result.get("signalled_pids") or []
    remaining = result.get("remaining_pids") or []
    result["message"] = (f"已核实身份并停止 {len(sent)} 个进程"
                         + (f"，剩余 {len(remaining)} 个仍在运行" if remaining else ""))
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
