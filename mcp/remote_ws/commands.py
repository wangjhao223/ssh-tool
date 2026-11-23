"""Shell command tools and shared command environment construction."""
import re
import shlex

import file_tools
import local_connection
from remote_ws import config, transport, write_guard
from remote_ws.validation import NOT_RUN, ToolError, bad_args, clamped_integer, validate_fields

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULTS = dict(config.DEFAULT_ROOTS)


def conf():
    c = dict(DEFAULTS)
    c.update({k: v for k, v in local_connection.read_config().items() if v})
    for k in ("CODE_ROOT", "DATA_ROOT", "CONDA_ROOT"):
        c[k] = c[k].rstrip("/") or "/"
    return c


def resolve_server(name):
    try:
        name = local_connection.resolve_server(name)
    except local_connection.LoginError as error:
        raise ToolError(str(error)) from error
    return name, transport.HOST_PREFIX + name


def qpath(path):
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def remote_dir(cwd, context=None):
    c = conf() if context is None else context
    if not cwd:
        return c["CODE_ROOT"]
    cwd = cwd.rstrip("/") or "/"
    if cwd.startswith(("/", "~")):
        return cwd
    return c["CODE_ROOT"] + "/" + cwd


def default_server():
    default = (conf().get("DEFAULT_SERVER") or "").strip()
    return default[len(transport.HOST_PREFIX):] if default.startswith(transport.HOST_PREFIX) else default


def find_write_violations(command, cwd, conda_env, context=None):
    c = conf() if context is None else context
    return write_guard.find_write_violations(command, cwd, conda_env, c)


def confirm_text(viol):
    return write_guard.confirm_text(viol)


def env_prelude(conda_env, gpus, server=None, context=None):
    c = conf() if context is None else context
    lines = []
    if conda_env:
        if not NAME_RE.match(conda_env):
            raise ToolError(f"非法 conda 环境名: {conda_env}")
        init = c.get("CONDA_INIT") if context is not None else c.get("CONDA_INIT_" + (server or "").upper())
        if init:
            lines.append(f"source {qpath(init)} || "
                         f"{{ echo 'remote-ws: conda 初始化失败: {init}' >&2; exit 98; }}")
            lines.append(f"source activate {shlex.quote(conda_env)} || "
                         f"{{ echo 'remote-ws: conda 环境 {conda_env} 激活失败' >&2; exit 98; }}")
        else:
            lines.append(f"source {qpath(c['CONDA_ROOT'])}/bin/activate {shlex.quote(conda_env)} || "
                         f"{{ echo 'remote-ws: conda 环境 {conda_env} 激活失败' >&2; exit 98; }}")
    if gpus is not None and str(gpus).strip() != "":
        g = str(gpus).strip()
        if not re.match(r"^\d+(,\d+)*$", g):
            raise ToolError(f"gpus 应为逗号分隔的编号，例如 \"0\" 或 \"0,1\"，收到: {g}")
        lines.append(f"export CUDA_VISIBLE_DEVICES={g}")
    return lines


def normalize_gpus(value):
    if type(value) is int:
        if value < 0:
            raise ToolError("gpus 必须是非负整数或逗号分隔的非负整数编号，例如 0 或 \"0,1\"")
        return str(value)
    if isinstance(value, str):
        return value
    raise ToolError("gpus 必须是字符串或非负整数")


def validate_arguments(args, properties, required=()):
    validate_fields(args, properties, required)


def t_exec(args, *, run=None):
    validate_arguments(args, dict(command=str, server=str, cwd=str, conda_env=str,
                                  gpus=(str, int), timeout_sec=int, max_output_chars=int,
                                  confirmed=bool), ('command',))
    run = transport.ssh_run if run is None else run
    cap, clamped_output = clamped_integer(args, 'max_output_chars', 2000, 256,
                                           transport.MAX_OUTPUT)
    command = (args.get("command") or "").strip()
    if not command:
        raise bad_args(ToolError, "command 不能为空")
    gpus = normalize_gpus(args["gpus"]) if "gpus" in args else None
    context = config.resolve_context(args.get("server"))
    name = context['SERVER']
    rdir = remote_dir(args.get("cwd"), context)
    timeout = int(args.get("timeout_sec") or 120)
    note = None
    if timeout > 1800:
        timeout = 1800
        note = "parameter_note: timeout_sec 超过 1800 秒上限，已按 1800 秒执行。"
    elif timeout < 1:
        timeout = 1
        note = "parameter_note: timeout_sec 小于 1，已按 1 秒执行。"
    if clamped_output:
        note = (note + "\n" if note else "") + "parameter_note: max_output_chars 超过 30000，已按 30000 执行。"
    viol = find_write_violations(command, rdir, args.get("conda_env"), context)
    if viol and args.get("confirmed") is not True:
        raise ToolError(confirm_text(viol), code="CONFIRM_REQUIRED", status=NOT_RUN)
    script = "\n".join(
        [f"cd {qpath(rdir)} || {{ echo 'remote-ws: 目录不存在: {rdir}' >&2; exit 97; }}"]
        + env_prelude(args.get("conda_env"), gpus, name, context)
        + [command])
    rc, out, err, el = run(name, script, timeout, max_output_chars=cap)
    extra = f"   服务器: {name}" if name != default_server() else ""
    text = transport.fmt_result(rc, out, err, el, extra)
    if viol:
        text = f"⚠️ 用户已确认越界写操作: {'；'.join(viol)}\n" + text
    if note:
        text = f"{text}\n{note}"
    return text, rc != 0


GPU_SCRIPT = r"""
nvidia-smi --query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits || exit 1
echo '@@APPS'
nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits 2>/dev/null | while IFS=', ' read -r u p m; do
  [ -n "$p" ] || continue
  info=$(ps -o user=,etime= -p "$p" 2>/dev/null | awk '{print $1" "$2}')
  printf '%s\t%s\t%s\t%s\n' "$u" "$p" "$m" "${info:-? ?}"
done
"""


def t_gpus(args, *, run=None):
    run = transport.ssh_run if run is None else run
    context = config.resolve_context(args.get("server"))
    name = context['SERVER']
    rc, out, err, el = run(name, GPU_SCRIPT, 60, max_output_chars=6000)
    if rc != 0:
        return transport.fmt_result(rc, out, err, el), True
    gpu_part, _, app_part = out.partition("@@APPS")
    apps = {}
    for line in app_part.strip().splitlines():
        f = line.split("\t")
        if len(f) == 4:
            apps.setdefault(f[0].strip(), []).append(f[3].split()[0])
    rows, free = [], []
    for line in gpu_part.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 6:
            continue
        idx, uuid, gname, used, total, util = f[:6]
        owners = sorted(set(apps.get(uuid, [])))
        is_free = not owners and int(float(used)) < 1000
        if is_free:
            free.append(idx)
        rows.append((idx, gname, used, total, util, is_free, owners))
    lines = [f"{name} GPU 状态:"]
    for gname in dict.fromkeys(r[1] for r in rows):
        group = [r for r in rows if r[1] == gname]
        lines.append(f"  {len(group)}×{gname}")
        for idx, _, used, total, util, is_free, owners in group:
            state = "空闲" if is_free else "占用: " + (", ".join(owners) or "未知进程")
            lines.append(f"    GPU {idx}  显存 {used}/{total} MiB  利用率 {util}%  {state}")
    if free:
        lines.append(f"空闲 GPU: {','.join(free)}（用法: gpus=\"{free[0]}\"）")
    else:
        lines.append("空闲 GPU: 无（未找到空闲卡；不要默认使用 GPU 0，请先核实占用或显式选择）")
    return "\n".join(lines)


def t_resource_query(args, *, run=None):
    if not isinstance(args, dict):
        raise ToolError("arguments 必须是对象")
    resource = args.get('resource')
    branches = {
        'gpus': ({'server', 'name', 'details'}, t_gpus),
        'conda': ({'server', 'name', 'details'}, t_conda_envs),
    }
    if not isinstance(resource, str) or resource not in branches:
        raise ToolError("resource 必须是 gpus 或 conda")
    allowed, handler = branches[resource]
    invalid = set(args) - allowed - {'resource'}
    if invalid:
        raise ToolError(f"resource={resource} 不允许参数: {', '.join(sorted(invalid))}")
    payload = {key: value for key, value in args.items() if key != 'resource'}
    if resource == 'conda' and payload.get('name') == '':
        payload.pop('name')
    types = dict(server=str, name=str, details=bool)
    validate_arguments(payload, {key: types[key] for key in allowed})
    if resource == 'gpus':
        neutral, intent = [], []
        if 'name' in payload:
            if payload['name'] == '':
                neutral.append("name=''")
            else:
                intent.append('name')
        if 'details' in payload:
            if payload['details'] is False:
                neutral.append('details=false')
            else:
                intent.append('details=true')
        if intent:
            raise ToolError("resource=gpus 仅兼容中性字段 name='' 与 details=false，"
                            f"不接受非中性字段: {', '.join(intent)}；"
                            "name 非空或 details=true 属于 Conda 查询，请改用 resource='conda'。")
        payload.pop('name', None)
        payload.pop('details', None)
        result = handler(payload, run=run)
        if neutral:
            note = ("resource=gpus 已在本地忽略中性字段 " + "、".join(neutral)
                    + "（兼容字段未发送远程）；需要 Conda 信息请用 resource='conda'。")
            if isinstance(result, tuple):
                text, is_error = result
                return f"{text}\nparameter_note: {note}", is_error
            return f"{result}\nparameter_note: {note}"
        return result
    return handler(payload, run=run)


def t_conda_envs(args, *, run=None):
    run = transport.ssh_run if run is None else run
    requested = args.get("name")
    if "name" in args and (not isinstance(requested, str) or not NAME_RE.fullmatch(requested)):
        raise ToolError("name 必须是有效的 Conda 环境名（字母数字_.-，最多 64 字符）")
    details = args.get("details", False)
    if type(details) is not bool:
        raise ToolError("details 必须是布尔值")
    c = config.resolve_context(args.get("server"))
    name = c['SERVER']
    init = c.get("CONDA_INIT")
    if requested is not None or not details:
        setup = f"source {qpath(init)} || exit 1\n" if init else ""
        listing = "conda env list" if init else f"{qpath(c['CONDA_ROOT'])}/bin/conda env list"
        if requested is None:
            script = setup + listing + " || exit 1\n"
        else:
            script = (setup + f"envs=$({listing}) || exit 1\n"
                      f"requested={shlex.quote(requested)}\n"
                      "found=0\n"
                      "while read -r env prefix; do\n"
                      "  [ \"$env\" = \"$requested\" ] || continue\n"
                      "  prefix=\"${prefix#\\*}\"\n"
                      "  prefix=\"${prefix#\"${prefix%%[![:space:]]*}\"}\"\n"
                      "  [ -d \"$prefix\" ] || continue\n"
                      "  found=1\n"
                      "  printf '%s %s\\n' \"$env\" \"$prefix\"\n")
            if details:
                script += ("  [ -x \"$prefix/bin/python\" ] || { echo 'remote-ws: 环境缺少 Python' >&2; exit 1; }\n"
                           "  v=$(\"$prefix/bin/python\" -c 'import sys;print(\"%d.%d.%d\"%sys.version_info[:3])' 2>/dev/null)\n"
                           "  t=$(\"$prefix/bin/python\" -c 'import torch;print(torch.__version__, \"cuda=\"+str(torch.cuda.is_available()))' 2>/dev/null || echo '无 torch')\n"
                           "  echo \"$env: python $v, torch: $t\"\n")
            script += ("  break\n"
                       "done <<< \"$envs\"\n"
                       "[ \"$found\" = 1 ] || { echo \"remote-ws: Conda 环境不存在: $requested\" >&2; exit 1; }\n")
    elif init:
        script = (f"source {qpath(init)} || exit 1\n"
                  "conda env list || exit 1\n"
                  f"[ -x {qpath(c['CONDA_ROOT'])}/bin/python ] || {{ echo 'remote-ws: base 环境缺少 Python' >&2; exit 1; }}\n"
                  f"v=$({qpath(c['CONDA_ROOT'])}/bin/python -c 'import sys;print(\"%d.%d.%d\"%sys.version_info[:3])' 2>/dev/null)\n"
                  f"t=$({qpath(c['CONDA_ROOT'])}/bin/python -c 'import torch;print(torch.__version__, \"cuda=\"+str(torch.cuda.is_available()))' 2>/dev/null || echo '无 torch')\n"
                  "echo \"base: python $v, torch: $t\"\n"
                  f"for e in \"$HOME\"/.conda/envs/*/ {qpath(c['CONDA_ROOT'])}/envs/*/; do\n"
                  "  [ -x \"$e/bin/python\" ] || continue\n"
                  "  v=$(\"$e/bin/python\" -c 'import sys;print(\"%d.%d.%d\"%sys.version_info[:3])' 2>/dev/null)\n"
                  "  t=$(\"$e/bin/python\" -c 'import torch;print(torch.__version__, \"cuda=\"+str(torch.cuda.is_available()))' 2>/dev/null || echo '无 torch')\n"
                  "  echo \"$(basename \"$e\"): python $v, torch: $t\"\n"
                  "done\n")
    else:
        script = (f"{qpath(c['CONDA_ROOT'])}/bin/conda env list || exit 1\n"
                  f"for e in {qpath(c['CONDA_ROOT'])} {qpath(c['CONDA_ROOT'])}/envs/*; do\n"
                  "  [ -x \"$e/bin/python\" ] || continue\n"
                  "  v=$(\"$e/bin/python\" -c 'import sys;print(\"%d.%d.%d\"%sys.version_info[:3])' 2>/dev/null)\n"
                  "  t=$(\"$e/bin/python\" -c 'import torch;print(torch.__version__, \"cuda=\"+str(torch.cuda.is_available()))' 2>/dev/null || echo '无 torch')\n"
                  "  n=$(basename \"$e\"); [ \"$e\" = " + qpath(c['CONDA_ROOT']) + " ] && n=base\n"
                  "  echo \"$n: python $v, torch: $t\"\n"
                  "done\n")
    rc, out, err, el = run(name, script, 120, max_output_chars=6000)
    return transport.fmt_result(rc, out, err, el), rc != 0
