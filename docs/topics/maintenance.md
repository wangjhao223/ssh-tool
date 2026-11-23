# 维护参考（按需阅读）

0.24.0 共享安装运行时只读。本页是唯一完整的参数/兼容矩阵、配置与历史、协议和验收说明；[根 AGENTS.md](../../AGENTS.md) 与 [操作导航](../../mcp/guide.md) 及各操作主题只保留当前任务必要的安全与操作提示并链接此处。不要为使用而修改本目录、复制工具或创建/保留优化备份，不读取或修改真实配置。

## 版本、工具与输出默认值

仅注册 10 工具：get_guide、connection_query、resource_query、exec、job_query、job_control、file_query、workspace_write、download_query、download_control。不启用 deferred；旧公开名 connect/status/diagnose/run_background/jobs/tail_log/stop_job/gpus/conda_envs/list_files/read_file/search_text/read_project/edit_file/init_project/download_file/download_status/list_downloads 均不注册、不分发、不作为隐藏别名；内部兼容函数不是 MCP 工具。登录由目标工具按需触发。

以下为运行时默认；共享 Schema 不发布分支专用默认值，调用方只传本次分支字段。

默认值（有界输出不等于删除能力，显式预算/详情仍保留）：

- exec：`max_output_chars=2000`（256–30000；超过 30000 按 30000 执行并附 `parameter_note`），`timeout_sec=120`（schema `minimum=1`、无 `maximum`；运行时负数夹为 1、超过 1800 夹为 1800并在输出末尾附 `parameter_note`；直接在运行时传 0 也按 120）；stdout/stderr 共享约 30000 字符总预算。
- job_query list：`limit=5`（运行时 1–500；超过 500 按 500 并提示）；tail：`lines=10`（1–2000；超过 2000 按 2000 并提示）、`max_chars=2000`（256–12000；超过 12000 按 12000 并提示）、`context_lines=2`（0–10；超过 10 按 10 并提示）。
- file_query list：`limit=25`（1–1000；超过 1000 按 1000 并提示）、`offset=0`；read：`start_line=1`、`max_lines=100`（1–1000；超过 1000 按 1000 并提示）、`max_chars=3000`（256–20000；超过 20000 按 20000 并提示）、`compact=true`（旧调用兼容）；search：`limit=8`（1–100；超过 100 按 100 并提示）、`context_lines=0`（0–10；超过 10 按 10 并提示）、`max_chars=1800`（256–12000；超过 12000 按 12000 并提示）、`case_sensitive=true`、`include_sha256=false`、`file_glob='*'`；project 索引：`depth=2`（0–10，严格）、`limit=60`（1–500；超过 500 按 500 并提示）、`max_chars=4000`（运行时 512–20000；超过 20000 按 20000 并提示）；project files 每项 `start_line=1`、`max_lines=120`（1–2000；超过 2000 按 2000 并提示）。
- workspace_write edit：`include_diff=false`、`max_chars=1000`（256–6000）；edits 1–100 项。
- download_query list：`limit=5`（超过 100 按 100 并提示）、`offset=0`；status：`wait_sec=0`（超过 10 按 10 并提示）、`verbose=false`、`verify_local=false`。
- resource_query conda：`details=false`，只列已有环境；`details=true` 才探测 Python/torch/CUDA。
- get_guide：`start_line=1`、`max_lines=80`（无 200 行上限，但仍须正整数）；正文按 JSON 转义后约 6000 字符预算截断，返回 `next_line` 续读，响应另留元数据空间。
- 本地 JSON 工具输出有 `MAX_TEXT=24000` 字符上限；附兼容提示后超限会报错，需缩小范围。远端 JSON 响应另有 2 MiB 硬上限（见连接）。

状态/阶段/`progress`/`next_line`/`next_offset`/`pending`/`more`/`incomplete`/`cursor` 不能被忽略；局部任务只需覆盖相关依赖，不强制全项目通读。

## 完整参数与中性兼容矩阵

通用规则：`action`/`resource` 必须显式给出；未知字段、未知枚举值、缺少必填字段、错误类型（含 null）和结构越界（ranges 数量/总行数、depth、写参数等）在执行前拒绝。只读可选字段（`server`/`project`/`name`/`query`/`cursor` 等）的空字符串按省略处理；输出条数/行数/字符/上下文向上超过上限时按上限执行并写 `parameter_note`，低于下限仍报错。下文 list/read/search/project 等均为相应工具的 action 分支，不是独立公开工具。`server` 可省略 `srv-` 前缀；选择顺序为显式 `server` > `REMOTE_WS_SERVER` > `DEFAULT_SERVER`，无效报错、不回退。

扁平 schema 为旧调用保留跨分支字段；运行时只接受当前分支的字段。非当前分支的字段若为中性值（整数 0、`false`、空字符串、`[]`）会被本地忽略并写 `parameter_note`，不发送远程；非中性值报错并指出应改用哪个 action。兼容字段不发送远程；不放宽未知参数、错误类型、结构越界或 `workspace_write` 等写工具的校验。

### get_guide

- 字段：`topic`（index/workspace/connection/files/downloads/environment/jobs/maintenance/troubleshooting，默认 index）、`start_line`（≥1，默认 1）、`max_lines`（≥1，默认 80）、`server`（可选；空串按省略）、`verbose`（布尔，默认 false）。
- index 与 connection/downloads/files/jobs/maintenance/troubleshooting 等通用静态主题接受但忽略合法类型的 `server`/`verbose`，不解析服务器配置；workspace/environment 使用服务器选择。
- environment 默认只返回通用摘要和所选服务器元数据/说明可用状态；仅 `verbose=true` 读取并拼接 `server-guides/<名称>.md` 正文及路径，续读保持 `verbose=true`。
- 指南只读、无网络、不登录、不创建目录；主题白名单和分页参数由代码检查，不支持任意路径。

### connection_query

- `action` 必填：status/diagnose。
- status：`server`（可选字符串）、`all`（布尔，默认 false；true 时不能同时传非空 `server`）、`verbose`（布尔，默认 false）、`timeout_sec`（整数，最小 0；仅兼容 0 并提示）。只查本地 socket/登录锁，无网络、不登录。verbose 显示端点、登录状态和所选服务器真实 configured CODE_ROOT/DATA_ROOT/CONDA_ROOT；未配置显示 `configured: false` 与“roots 未配置”，不补全局默认值。
- diagnose：`server`、`timeout_sec`（整数，默认 3；小于 1 报错，大于 10 按 10 执行并提示）、`all`（仅兼容 false）、`verbose`（仅兼容 false，二者均提示）。不弹窗；无连接测端口，有连接只读探测远程 Shell；ProxyJump/ProxyCommand 时跳过直连 TCP 预检。状态包括 connected/session_present/needs_login/login_pending/local_error/dns_error/network_timeout/connection_refused/network_unreachable/connection_lost/session_probe_failed 等。

### resource_query

- `resource` 必填：gpus/conda。
- gpus：`server`；兼容忽略 `name=''` 与 `details=false` 并写 `parameter_note`；`name` 非空或 `details=true` 报错并提示改用 `resource='conda'`；类型/未知参数严格。未配置服务器在上下文解析阶段失败，不执行远程查询。输出按显卡型号分组，空闲条件为无计算进程且已用显存小于 1000 MiB；有空闲卡时建议第一张，无空闲卡明确写“不要默认使用 GPU 0，请先核实占用或显式选择”，不推荐 0。
- conda：`server`、`name`（已有环境名；空串按省略，列出全部环境；非空时须匹配 `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`）、`details`（布尔，默认 false）。`name` 指定时列出并定位该环境，`details=true` 再查 Python/torch；`details=true` 未指定 name 时列出 base 及环境。`CONDA_INIT` 非空时先 source 初始化脚本，再用 `CONDA_ROOT/bin/python` 和 `CONDA_ROOT/envs/*`、`~/.conda/envs/*` 探测；为空时用 `CONDA_ROOT/bin/conda`。不是安装授权。

### exec

- 字段：`command`（必填非空字符串）、`server`（空串按省略）、`cwd`（空串按省略，默认 CODE_ROOT；相对路径相对 CODE_ROOT，支持绝对/`~/`）、`conda_env`（已有环境名；空串按省略，不激活）、`gpus`（非负整数或逗号分隔编号；空串等同未指定）、`timeout_sec`（默认 120；schema `minimum=1`、无 `maximum`；运行时负数夹为 1、超过 1800 夹为 1800并附 `parameter_note`，直接运行时 0 按 120）、`max_output_chars`（256–30000，默认 2000；超过 30000 按 30000 并附 `parameter_note`）、`confirmed`（布尔，默认 false）。
- `CONDA_INIT` 非空时先 `source <CONDA_INIT>` 再 `source activate <env>`；为空时用 `CONDA_ROOT/bin/activate`；激活失败退出 98，目录不存在退出 97。
- 越界写启发式命中时，未显式 `confirmed=true` 拒绝；确认仅表示用户已同意，不扩大 roots，也不是沙箱。输出含 `exit_code`、stdout/stderr 与截断标记；普通非零退出保留 exit_code、有限输出和 isError，并标 REMOTE_NONZERO；已发送的 SSH 255 不冒充普通命令返回。仅在明确未发送时标未执行；写操作结果未知不自动重试。

### job_query

- `action` 必填：list/tail。
- list：`server`（空串按省略）、`project`（可选；空串按省略；显式值或 REMOTE_WS_PROJECT，均缺失才查看全部项目及公共旧日志）、`limit`（运行时 1–500，默认 5；超过 500 按 500 并附 `parameter_note`）、`verbose`（布尔，默认 false；true 增加 start/last_write/gpus/env/完整 ID）。旧日志缺这些字段时显示 `-`，缺 project 显示 `-`，缺 `job_id` 显示 `legacy`。兼容忽略 tail 中性字段 `job=''`/`cursor=''`/`query=''`/`errors=false`/`compact=false`/`context_lines=0`/`lines=0`/`max_chars=0` 并提示；非中性值指向 tail。
- tail：`server`/`project`（空串按省略）、`job`（必填非空；名称、12 位短 ID、完整 32 位 ID、PID、日志名或路径）、`lines`（1–2000，默认 10；超过 2000 按 2000 并附 note）、`cursor`（空串按省略；非空最长 4096）、`max_chars`（256–12000，默认 2000；超过 12000 按 12000 并附 note）、`query`（空串按省略；非空为 1–256 字符单行字面关键词，不含 NUL）、`errors`（布尔，默认 false；与 query 不混用）、`context_lines`（0–10，默认 2；超过 10 按 10 并附 note）。兼容忽略 list 中性字段 `limit=0`/`verbose=false` 并提示；非中性值指向 list。
- `compact` 可选布尔：显式 project + 完整 job + cursor + compact=true 且任务身份相符才省略重复的 task_id/job_id/project/log；status 和续读/完整性标志不变。已选项目和完整 ID 时定位和读取合并为一次远程请求。
- tail 默认返回 `text`/`cursor`/`more`，并补充 `task_id`（12 位）、完整 `job_id`、`project`、`log`、`status`。非法 UTF-8 以替换字符返回并标记 `invalid_utf8=true`；活跃任务末行未换行时 `pending_line=true`；旧日志只读识别，活跃旧日志同样按 pending 处理，已结束/已退出旧日志返回带替换字符的尾行，`status=legacy/unverified`。游标绑定服务器、日志、设备/ inode/偏移和过滤条件；替换/缩短/旧格式失效需省略 cursor 重读。

### job_control

- `action` 必填：start/stop。
- start：`command`、`name`、`project` 必填；空字符串不视为省略，`project` 必须在本次参数中显式提供且匹配 `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`，不回退 REMOTE_WS_PROJECT；`name` 同样匹配该模式。可选 `server`、`cwd`（空串按省略，默认 `CODE_ROOT/<project>`；相对路径相对 CODE_ROOT，支持绝对/`~/`）、`conda_env`（空串不激活）、`gpus`（非负整数或逗号编号；空串表示不指定）、`confirmed`。兼容中性跨分支 `job=''`、`force=false` 并本地忽略；非空 job 或 force=true 拒绝。
- stop：`job` 必填且仅接受完整 32 位小写十六进制任务 ID（`[0-9a-f]{32}`）；可选 `server`/`project`（空串按省略）、`force`（布尔，默认 false；true 为 SIGKILL）。兼容中性跨分支 `command`/`name`/`cwd`/`conda_env`/`gpus` 为空字符串、`confirmed=false` 并本地忽略；非中性值拒绝。短 ID、名称、PID、日志名或路径在上下文解析及远程调用前拒绝；`job_query` 的查询选择器不受此限制。
- start 在 `DATA_ROOT/<project>/logs` 写日志，返回 server/project/name/task_id 及 pid/status/log；不部署服务器辅助脚本。stop 会重新核对日志与进程身份，以 pidfd 只停已核验的同属主/进程组/会话成员；默认 SIGTERM，force=true 为 SIGKILL，不回退猜 PID/killpg；旧日志身份不足、多候选或 identity_mismatch 拒绝。

### file_query

- `action` 必填：list/read/search/project；`path` 必填；`server` 可选。
- list：`server`（空串按省略）、`offset`（≥0，默认 0）、`limit`（1–1000，默认 25；超过 1000 按 1000 并附 `parameter_note`）；`compact` 静默忽略、不增加 `parameter_note`；0–10 的整数 `depth`（布尔不算整数）也忽略，但显式 depth（包括 0）会增加 `parameter_note` 提示改用 project，list 绝不递归。其他跨分支字段按中性/重定向规则处理。
- read：`server`（空串按省略）、`start_line`（≥1，默认 1）、`max_lines`（1–1000，默认 100；超过 1000 按 1000 并附 note）、`ranges`（仅旧调用兼容，不是新调用的详细推荐定义；非空时 1–8 段，每段 `start_line`/`max_lines` 均必填，结构仍严格：总行数 ≤2000、按 start_line 递增且不重叠、不与外层 start_line/max_lines 或 `compact=false` 混用；每段 max_lines 超过 1000 按 1000 并附 note；`ranges=[]` 视为省略）、`max_chars`（256–20000，默认 3000；超过 20000 按 20000 并附 note）、`compact`（旧调用兼容，默认 true）。额外接受并忽略 `offset`/`limit`/`query`/`file_glob`/`context_lines`/`case_sensitive`/`include_sha256`/`files`/`depth`，仅基础类型校验、不发送远程，action 仍为 read；显式传入时 `parameter_note` 列出实际忽略字段，未传且未触发 max_chars 夹取时原输出不变。
- search：`server`（空串按省略）、`query` 必填（空串不是省略，1–256 字符单行字面关键词）；`file_glob`（默认 `*`，空串按 `*` 并提示）、`context_lines`（0–10，默认 0；超过 10 按 10 并附 note）、`limit`（1–100，默认 8；超过 100 按 100 并附 note）、`max_chars`（256–12000，默认 1800；超过 12000 按 12000 并附 note）、`case_sensitive`（默认 true）、`include_sha256`（默认 false）。
- project：`server`（空串按省略）、`files`（0–8 项；空数组或省略返回索引）、`depth`（0–10，默认 2，结构严格；有非空 files 时本地忽略并提示）、`offset`（≥0，默认 0；有非空 files 时忽略并提示）、`limit`（1–500，默认 60；超过 500 按 500 并附 note；有非空 files 时忽略并提示）、`max_chars`（运行时 512–20000，默认 4000；超过 20000 按 20000 并附 note）。files 每项 `path` 由 project 运行时校验为项目内相对路径（不含 `..`），可选 `start_line`（≥1，默认 1）、`max_lines`（1–2000，默认 120；超过 2000 按 2000 并附 note）。
- list/search/project 的跨分支中性值本地忽略并提示，非中性值重定向报错（query→search、files→project、ranges/start_line/max_lines→read、depth→project 等）。未知字段、错误类型（含 null）、低于下限及结构越界仍严格拒绝；输出条数/行数/字符/上下文向上超限按各自上限执行并写 `parameter_note`。`workspace_write` 不参与中性兼容。
- project 批读逐文件返回 blocks、sha256（≤4 MiB 文件为摘要，大文件为 null）及 next_line；代码会移除 version/editable，不返回这两项。`pending` 表示未读/未读完，`complete` 只表示本批无 pending 且无 errors；单个文件缺失/二进制/超限进入 `errors` 并继续，路径越界/符号链接逃逸等安全错误使整次失败。出错详情超预算保留 error_count/errors_truncated；无进展时提示增加预算或减少 files，不返回相同续页。本地 JSON 输出超过 24000 字符报错。
- 搜索/项目索引的排除项、扫描上限、`incomplete`/`reason`/`skipped` 等见下文文件边界。

### workspace_write

- `action` 必填：edit/init。
- edit：`path`、`expected_sha256`（64 位十六进制）、`edits`（1–100 项，每项 `old_text` 非空、`new_text` 字符串）必填；`include_diff`（布尔，默认 false）、`max_chars`（256–6000，默认 1000）、`server`。严格拒绝跨分支/未知字段，不做中性忽略。
- init：`project` 必填且匹配项目模式，空串不视为省略；`server`（空串按省略）。创建 `CODE_ROOT/<project>` 与 `DATA_ROOT/<project>/{data,checkpoints,results,logs}`，已有目录不改。

### download_query

- `action` 必填：list/status。
- list：`state`（starting/running/completed/cancelled/interrupted）、`offset`（≥0，默认 0）、`limit`（≥1，默认 5；超过 100 按 100 并提示）、`verbose`（布尔）；兼容忽略 `task_id=""`、`wait_sec=0`、`verify_local=false`、`server` 并提示；非中性值拒绝。只读当前池本地记录，无网络；损坏记录列 `unreadable_task_ids`。
- status：`task_id` 必填且为完整 32 位小写十六进制 ID；`wait_sec`（整数 ≥0，默认 0；超过 10 按 10 并提示）、`verbose`（布尔）、`verify_local`（布尔）、`server`（可传但忽略并提示）。`verify_local=true` 仅对 completed 核验本地文件，未完成返回 `status=not_completed`，不是错误；结果不写回记录。

### download_control

- `action` 必填：start/cancel/resume。三个分支都兼容中性 `verbose=false`/`wait_sec=0`/`verify_local=false` 并提示；错误类型或非中性值拒绝。
- start：`remote_path` 必填；`server`、`local_path`；兼容忽略精确空字符串 `task_id=""`。目标默认 `<池>/<server>/<远端路径>`，自定义 local_path 须在当前池内且不覆盖、不绕过符号链接、不使用 `.jobs`。返回完整 task_id 和启动状态。
- cancel：`task_id` 必填完整 UUID；兼容忽略 `server`（使用记录快照）、`remote_path=""`、`local_path=""` 并提示；非空路径拒绝。核对 pidfd 后停止 worker，保留部分文件，不关闭共享 SSH。
- resume：`task_id` 必填完整 UUID；与 cancel 相同的兼容字段；记录快照用于服务器/路径/roots。目标已存在、已完成、部分文件缺失或小于记录进度均拒绝；手动触发，不自动重连。

## Schema 与运行时校验

工具描述已精简为用途摘要，参数细节以本页矩阵为准。工具 schema 使用扁平对象、`additionalProperties=false`，保留类型、枚举、必填、最小/最大值、模式、默认值和嵌套结构；`compact_schema` 不丢弃这些关键约束，也不使用 `title` 等膨胀字段。为兼容旧调用，某些分支的中性值会出现在 schema 中（例如 job_query 的 tail 中性 0、job_control 的空字符串、resource_query 的空 name），但不代表该字段在当前分支生效；file_query 的 `compact`/`ranges` 等旧调用字段也不是详细推荐定义。file_query 共享 schema 的 `offset`/`limit`/`start_line`/`max_lines`/`max_chars` 使用 `minimum=0` 以接收其他 action 的中性 0；当前 action 的运行时下限仍严格（list/search/project 的 limit≥1，read 的 start_line/max_lines≥1、max_chars≥256，project 的 max_chars≥512）。嵌套 `files` 项 schema 不标 `required`，由 project 运行时校验 `path`；read 仍整体忽略 `files`。运行时按 action/resource 分支执行完整字段、类型、范围、中性/重定向和写保护检查；向上超预算按上限执行并写 `parameter_note`，schema 放行而运行时不接受的参数以运行时错误为准。`workspace_write` 和未知字段始终严格。

## 配置、目录与接入

默认外部目录：配置 `~/.config/remote-ws`；状态 `~/.local/state/remote-ws`；下载根 `~/.local/share/remote-ws/downloads`。`REMOTE_WS_CONFIG_DIR`/`REMOTE_WS_STATE_DIR`/`REMOTE_WS_DOWNLOAD_ROOT` 分别覆盖；未设时遵循 `XDG_CONFIG_HOME`/`XDG_STATE_HOME`/`XDG_DATA_HOME`，再回退上述位置。覆盖值须为规范绝对路径、不能为 `/`。

配置文件职责：

- `remote-ws.conf`：当前格式 `FORMAT_VERSION=3`，仅版本和可选 `DEFAULT_SERVER`，不接受共享 roots/Conda。
- `ssh_config`：名录/SSH 端点，明确 `Host srv-<名称>` 才注册；`servers.d` 本身不注册。单机 User/Port 写在通配默认块之前（SSH 首个匹配值生效）。不改全局 `~/.ssh/config`。
- `known_hosts`：主机记录，无密码；密钥变化先核实，不删记录绕过。
- `servers.d/<名称>.conf`：v3 每台独立执行配置，只按需读所选服务器，名称精确且区分大小写；缺失不继承。
- `server-guides/<名称>.md`：可选本地说明，不是执行配置/实时诊断，不执行其中命令，不存密码或私钥。

配置目录属当前用户且无组/他人权限（通常 0700）；文件为当前用户的普通 UTF-8 文件，最多 64 KiB，禁止路径符号链接、组/他人写入。`servers.d` 属当前用户且不可被组/他人写。拒绝重复/未知键，v2/v3 拒绝无 `=` 的非注释行。值按字面，不执行 Shell、不展开环境变量或 `~`，可成对引号；`#` 注释独占一行。

v3 单机文件完整提供 CODE_ROOT/DATA_ROOT/CONDA_ROOT/CONDA_INIT，无初始化脚本也显式 `CONDA_INIT=`。ROOT 非空、规范绝对路径且不能 `/`，非空 CONDA_INIT 同样检查。不从全局、内置 roots 或别的服务器继承；缺文件/必需键为未配置，格式/路径/权限不安全仍报错。未选服务器损坏不阻塞当前操作。

选择顺序：`server` 参数 > `REMOTE_WS_SERVER` > `DEFAULT_SERVER`，无效报错、不回退；除 `job_control(action='start')` 外，project 仍按参数 > `REMOTE_WS_PROJECT` 选择。start 每次必须在本次参数中显式提供合法 project，不使用 env 回退，缺失/非法在上下文解析及远程调用前拒绝；stop 的 job 也先按完整 32 位小写十六进制 ID 校验再解析上下文。项目 env 不扩大 roots，Conda 路径不是写入授权。只读 workspace 展示当前上下文，`verbose=true` 查名录和 tool_root/tool_version、配置和状态路径及注册方式等详情，不读其他单机配置；workspace/environment 可查看未配置名录并返回 configured=false，roots/Conda 不补默认值。名录和说明不是执行授权。

每个本地用户只需在 `~/.kimi-code/mcp.json` 注册一次，直接引用本工具的 `mcp/remote_ws_mcp.py`，Python args 含 `-B`/`-u`。同一个工具目录供多个业务项目直接使用，不复制工具到业务项目、不要求业务项目配置 `.kimi-code/mcp.json`，也不要为使用而修改本目录。用户级 MCP 对所有项目全局可见，不启用 deferred，10 个工具定义带来固定上下文代价；按需指南和摘要只减少正文/响应，不消除工具定义成本。现有注册仅在完整验证并获准切换后更新入口和 cwd，不覆盖已有配置或叠加同名项目级注册。

默认不设置 REMOTE_WS_TASK/REMOTE_WS_PROJECT/REMOTE_WS_SERVER，使用 default 下载池和 DEFAULT_SERVER。`REMOTE_WS_TASK` 由每个 MCP 进程的环境决定，进程内使用同一个池；未设置时为所有项目共享的 default。长任务启动逐次显式传合法 project，查询、日志和停止建议显式传 project 限定业务范围，停止仅传完整 32 位小写十六进制任务 ID。短命令显式选择业务 cwd，必要时显式传 server。只有特殊隔离需要才显式设置稳定的 REMOTE_WS_TASK。用户级 env 是所有项目共享的固定值，不随业务 cwd 自动切换。不改 `~/.ssh/config`，不修改真实服务器配置，不迁移活动下载。

`config/examples` 只有占位模板，运行时不读，也不是使用本目录的前置安装步骤。只有用户明确授权新增服务器时，才参照示例准备外部独立配置并替换占位值；不要覆盖真实配置或写入秘密，不创建备份。示例只提供占位主机/账号/路径，不携带真实凭据。实际 configured 状态以只读 workspace 查询为准，不从安装历史推断，不为核对文档读取真实配置。

## 旧配置兼容边界（非当前配置建议）

当前约定是 v3 独立配置；实际 configured 状态以只读 workspace 查询为准。v1/v2 仅为旧配置兼容，不是推荐模板，也不自动迁移。

- 未声明版本或 =1：共享 roots，旧 `CONDA_INIT_<服务器名大写>`，不读 servers.d，空 ROOT 沿用旧内置默认值，无 `=` 的旧行忽略。
- v2：内置默认值 < 全局共享值 < 所选单机覆盖，缺文件/键继承，单机空 CONDA_INIT 清除全局值。
- 上述继承规则不适用于 v3；未知版本报错，不降级。

仅在用户明确授权迁移时，完整准备单机有效值，移除全局 roots/Conda/旧初始化键并设版本 3；核对属主/权限/无符号链接后原子应用，不创建或保留优化备份。切换期间暂停配置读取避免混合版本，先在隔离配置离线核对，通过后再恢复读取；不从旧备份恢复或重新启用共享继承。

## 连接、SSH 与登录

`connection_query(status)` 只查本地 socket/登录锁，无网络；`all=true` 不能同时指定 server；verbose 显示端点、登录状态和所选服务器真实 configured roots/Conda，未配置不补默认值。`diagnose` 不弹窗：已有会话只读探测远程 Shell；无会话时先解析 SSH 参数，检测到 ProxyJump/ProxyCommand 则跳过直连 TCP 预检，直接提示需要独立窗口登录；直连则测 DNS/端口/超时/不可达。`timeout_sec` 默认 3，小于 1 报错，大于 10 按 10 并提示。

SSH 错误分类覆盖主机密钥变化/校验失败、DNS、端口拒绝、网络超时/不可达、认证失败、MAC/协议完整性失败、连接被关闭/中断等。`ssh_run` 和 `remote_json` 在 SSH 返回 255 时优先分类：认证失败给出工具返回的完整绝对登录命令；其他分类提示 `connection_query(action='diagnose')`；无法分类时返回 `[ssh_error]` 并指向诊断，不猜密码错误。普通命令非零退出不重新登录。

Tool 调用只有在未连接且 `REMOTE_WS_LOGIN_GUI=1` 时才自动打开登录窗口；否则 `ensure_login`/`ssh_run` 返回带完整绝对连接命令的错误（“本次远程操作尚未执行”）。密码只由用户在窗口/终端输入，MCP 不读取窗口输出。`REMOTE_WS_LOGIN_GUI` 未设或非 1 时一律手动登录。

`remote_json` 用于 file/job 的 JSON 操作：stdout 在解析前硬限制 2 MiB，stderr 最多 64 KiB；超限立即停止且不解析。编辑超限提示状态不确定、先 read 重读、不得自动重写；停止超限提示先 `job_query(action=list)` 确认、不得重复停止；其他操作提示缩小 files/ranges、max_chars、分页或历史条数。超时/非 JSON/非对象/`ok=false` 分别报错；`uncertain` 响应会附加编辑/停止的不确定性提示。

socket/登录锁按工具真实路径+UID 隔离在 `/tmp/remote-ws-<uid>-<路径hash>`，权限 0700，同工具各项目共享，不同副本独立。登录窗口短标记 15 秒防重复，之后以实际登录锁判定。连接脚本先查 socket 再验证真实链路，失效清旧 master 后登录；不捕获窗口输出。ControlPath/UserKnownHostsFile 由工具指定，ControlMaster/ControlPersist/ServerAliveInterval/ServerAliveCountMax 等复用和心跳策略以用户授权的外部 ssh_config 为准，config/examples 不预设这些策略。ControlPersist 是无会话空闲上限，不是会话总时长。Connect.cmd 为 Windows 双击入口，scripts/connect.ps1 管窗口，connect.sh 管 WSL；不部署远端组件。

keepalive.sh 可由用户另行授权的 cron/SessionStart hook 调用 `--quiet`；文档不证明其已安装，也不自动注册。BatchMode 检查默认服务器，不弹密码；无项目 env 时使用 DEFAULT_SERVER，解析失败记录外部状态日志、不猜服务器。日志/状态/通知/锁在状态目录，锁防并发；quiet 正常无日志通知，配置错误仍记。`--notify-test` 测通知，`-h`/`--help` 看帮助；取消 cron/关闭共享连接只由用户明确决定，不由业务任务擅改。

## 写保护：启发式，不是沙箱

`write_guard` 只静态识别常见写/修改形式，不能证明命令安全，也不能阻止所有写盘方式；内联 shell/解释器、`xargs`/`make`、`env -C` 以及含变量、反引号、通配符或花括号展开的动态写目标无法静态验证，一律保守要求 `confirmed=true`。它不是完整 Shell 沙箱，不能替代 SSH 权限和用户审查；文件专用工具没有 confirmed 绕过。

允许写根为所选服务器的 CODE_ROOT、DATA_ROOT 及 `/tmp`、`/var/tmp`、`/dev/shm`；`/dev/null` 等设备目标豁免。常见检查包括：重定向（`>`/`>>`/`&>`/`2>` 等）；`rm`/`mv`/`mkdir`/`rmdir`/`touch`/`shred`/`chattr`/`truncate`/`fallocate`/`setfacl`；`chmod`/`chown`/`chgrp` 跳过模式/属主后的路径；`cp`/`ln`/`scp`/`rsync`/`install` 的最后操作数及 `-t`/`--target-directory`；`install -d`；`tee`；`sed -i`/`--in-place`（含短选项簇，`-e`/`-f` 脚本值不作为目标）；`dd of=`；`zip` 输出；`unzip -d`（未指定时按 cwd）；`curl` 的 `-o`/`--output`/`--output-dir`、`-D`/`--dump-header`、`-c`/`--cookie-jar`、`--trace`/`--trace-ascii`、`--stderr`、`--etag-save`、`--hsts`、`--alt-svc`、`--libcurl`；`wget` 的 `-O`/`--output-document`、`-P`/`--directory-prefix`、`-o`/`--output-file`、`-a`/`--append-output`、`--save-cookies`、`--rejected-log`、`--warc-file`；`mktemp -p`；gzip 家族非 `-c`；`find -delete/-exec` 等；`git clean`/`git clone` 目标及 `--separate-git-dir`；`tar` 的 `-f` 写目标或 `-C` 解压目录；`env -C`；写操作前的 `cd` 目标。`timeout` 先消费时长及 `-s`/`--signal`、`-k`/`--kill-after` 的值，再检查其后的真实命令。系统级命令（sudo/apt/dpkg/systemctl/service/crontab/mount/umount/mkfs 等）、管道执行 shell、进程替换也会保守拦截。

内联解释器只扫描命令自身的前导选项（shell/python/perl/ruby/node/php 的 `-c`/`-e`/`-p`/`-r` 等，以及 `python -m`、脚本/模块之后的参数不参与），避免把 `python train.py -c config` 这类训练参数误报为内联执行。`pip install` 在未指定 conda_env 或安装到越界环境时拦截；`--user` 或已有 conda_env 可以放行，但仍受运行期权限约束。

命中后输出“越界写需用户同意”，列出最多 5 项及总数；用户明确同意后用**相同参数**传 `confirmed=true` 重试。`confirmed` 只表示确认，不是授权扩大 roots，也不会让文件工具绕过路径限制；不自动确认，不因命令放行安装组件。`confirmed=false` 不构成确认。

## 任务、日志、GPU 与 Conda

- start 默认 cwd 为 `CODE_ROOT/<project>`；显式相对 cwd 仍相对 CODE_ROOT，绝对/`~/` 按字面。日志目录为 `DATA_ROOT/<project>/logs`；任务头为 job-v2，含 job_id/name/project/pid/uid/pgid/sid/start_ticks/boot_id。`CONDA_INIT` 非空时先 source 再激活，为空时用 `CONDA_ROOT/bin/activate`；失败退出 98。GPU 通过 `CUDA_VISIBLE_DEVICES` 设置。
- GPU 查询：`nvidia-smi` 列出型号/显存/利用率/计算进程；空闲定义为无计算进程且已用显存 <1000 MiB。有空闲卡时建议第一张的 `gpus="N"`；无空闲卡明确提示不要默认 GPU 0，请核实占用或显式选择。查询不是预留。未配置服务器在上下文解析阶段失败，不执行远程查询。
- conda 查询：`details=false` 只列已有环境；指定 name 时校验环境存在并可选探测 Python/torch。`CONDA_INIT` 非空时先 source 初始化脚本，details 使用 `CONDA_ROOT/bin/python` 和 `CONDA_ROOT/envs/*`、`~/.conda/envs/*`；为空时使用 `CONDA_ROOT/bin/conda`。缺 Python/torch 报告，不安装。
- tail 的完整身份字段：`task_id`（12 位）、`job_id`（完整或 null）、`project`、`log`、`status`。非法 UTF-8 替换并标 `invalid_utf8`；活跃任务（v2 身份匹配或旧日志 pid/uid 存活且无 exit 标记）末行未换行时 `pending_line`；已结束旧日志返回尾行替换字符，状态 `legacy/unverified`；v2 任务身份不匹配为 `identity_mismatch`。游标绑定服务器/日志/文件身份/偏移/过滤条件，旧格式或替换/缩短文件失效。
- 过滤模式 `query`/`errors` 二选一，返回命中上下文及完整 Traceback；扫描窗口约 1 MiB/2000 行/5 秒，`pending_exception`/`exception_incomplete` 表示异常块未完整；整块超预算报错，不静默删栈。
- stop 仅完整 32 位小写十六进制 ID；先查询获取并核对。stop 重新核对日志和实际进程身份，pidfd 只停同属主/组/会话成员，默认 SIGTERM、force=true SIGKILL；不回退猜 PID/killpg。旧日志身份不足、重复候选、identity_mismatch 拒绝；缺 pidfd 支持报告，不安装。停止期间新派生进程不追杀；部分停止/超时不自动重发。

## 下载、状态与字节协议

默认池为 `DOWNLOAD_ROOT/default`；`REMOTE_WS_TASK` 为每个 MCP 进程固定一个池，显式值用于特殊隔离。记录/部分文件位于 `<池>/.jobs/<task_id>.json|.part`；每个 UUID 的 `.lock` 用于 worker 生命周期，另有 `.control` 控制锁串行化 start/cancel/resume 决策，两者分离。`.control` 由工具创建为 0600 普通文件，拒绝符号链接、目录或非当前用户文件。旧 `.lock`/`.control` 文件不自动清理，不迁移活动下载。跨池记录/目录/锁隔离，禁止跨池查询或控制；共享池操作前核对完整 ID、路径和归属。

- start：`remote_path` 必须位于所选服务器可信 roots；默认目标 `<池>/<server>/<远端路径去掉开头斜杠>`；`local_path` 支持 WSL 绝对路径、Windows 盘符（wslpath 转换）或相对路径，均须在池内，拒绝符号链接、`.jobs` 和已存在目标。创建 `.part` 后以独立会话启动本地 worker，并传递锁 fd；同池可并行，同一 UUID 的启动/取消/续传互斥，busy 只针对该 ID。
- 状态：`starting`/`running`/`completed`/`cancelled`/`interrupted`；阶段 `starting`/`verifying_local`/`verifying_remote`/`transferring`/`verifying_final`/`completed`。100% 字节不是完成；只有完整接收、SHA-256 一致、源稳定、SSH 成功退出且无覆盖发布后才为 completed。成功记录含 `sha256`/`windows_path`，部分文件发布后删除；失败保留部分文件。
- list：只读当前池本地记录，无网络；默认 5，limit 超过 100 按 100 并提示，offset 分页；损坏记录列 `unreadable_task_ids`；`verbose` 增加时间；`SERVER` 忽略提示；`task_id=""`/`wait_sec=0`/`verify_local=false` 中性忽略提示；本地输出受 24000 字符限制。
- status：按完整 UUID 读记录；`wait_sec` 0–10（超出按 10 并提示），`verbose` 展示部分文件/摘要/进程；内部 `pid_token`/`roots`/launcher 字段不暴露；`server` 忽略提示。`verify_local=true` 只对 completed 核验当前文件大小/SHA-256，返回 `verified`/`missing`/`size_mismatch`/`sha256_mismatch`/`changed_during_check`/`not_regular_file`/`present`；未完成返回 `not_completed`，不报错、不写回。completed 报告移除仅用于续传前缀的 `verified_bytes`/`verification_progress`。
- cancel：只处理 ACTIVE。若进程未存活，最多等待 2 秒交接；仍处于 starting 且无已核验 PID 时返回 `cancelled=false`/`cancel_requested=false` 并提示稍后重试；不向未核验 PID 发信号。支持 pidfd 时向 worker 发 SIGTERM，保留部分文件，不关闭共享 SSH；只有信号实际发送成功且信号后仍 ACTIVE 时才返回 `cancel_requested=true`，PID 已消失/未发送成功而仍 ACTIVE 时返回 `cancelled=false`/`cancel_requested=false` 并提示重新查询。缺 pidfd 报告且不退回数字 PID。
- resume：completed 拒绝；ACTIVE 拒绝；目标已存在拒绝；部分文件缺失返回“部分文件缺失，无法续传；请重新开始下载”；部分文件小于记录进度拒绝。使用记录中的服务器/路径/roots 快照，手动触发，不自动重连；worker 先校验前缀 SHA-256，再截断未持久化尾部。
- 字节协议固定 `protocol=1`：stdin JSON 请求 `operation/path/roots/offset/identity/prefix_sha256`；stdout 一行 JSON 头（ok/protocol/path/identity/offset/length）＋精确 length 原始字节＋一行 JSON 尾（ok/sha256/stable）。头之后失败只写 stderr，不再写 JSON，避免污染部分文件；本地 worker 把远端 stderr 末尾约 2000 字节并入中断原因。不插日志、不解码字节、不改普通 JSON 应答；失败不自动重连。
- control 兼容：start 仅忽略精确 `task_id=""`；start/cancel/resume 均忽略中性 `verbose=false`/`wait_sec=0`/`verify_local=false` 并提示；cancel/resume 还忽略 `server` 和空字符串 `remote_path=""`/`local_path=""`；非空路径、非中性值、错误类型、未知参数拒绝。每个控制操作先取 `.control` 非阻塞锁；控制操作进行中返回 `state=busy` 和“控制操作正在进行”消息，不排队，也不启动 worker。cancel/resume 只接受完整 UUID，不接受前缀。

## 文件、项目与编辑边界

- list 只列当前目录，默认 25，`offset`/`limit` 分页；不递归；`depth` 兼容忽略并提示，`compact` 静默忽略、不增加 `parameter_note`。输出受 CAP 预算限制。
- read 仅 UTF-8 普通文件；≤4 MiB 返回整文件 SHA-256 与 editable，较大文件只采样检查并分段读；二进制/超长行报错，需下载。默认紧凑 blocks；`ranges`/`compact` 仅旧调用兼容，结构规则见参数矩阵；文件变化报错。`max_chars` 预算内按行返回，`next_line` 续读。
- search 字面匹配（可区分大小写），`file_glob` 只匹配文件名；跳过符号链接、非本人文件、二进制、超大文件；排除 `.git`/`__pycache__`/`.venv`/`venv`/`.jobs`/`data`/`datasets`/`checkpoints`/`weights`；单文件 8 MiB、累计 32 MiB、5000 目录项、5 秒限制。`skipped`/`incomplete`/`reason` 不能当作全目录无匹配；搜索期间文件变化报错；`include_sha256` 只为可编辑大小文件附摘要。
- project 索引不跟随符号链接、排除同类目录、跳过非当前用户项；5000 项/5 秒限制；`incomplete`/`depth_limited`/`restricted`/`reason` 表示覆盖受限。files 批读每项独立，单文件错误进入 `errors` 并继续，路径安全错误整次失败；`pending`/`complete` 语义见参数矩阵；共享 max_chars 预算，输出超过 24000 字符报错。
- edit：只改当前用户拥有的现有普通文件，≤4 MiB，单硬链接；UTF-8，expected_sha256 匹配整文件；edits 顺序应用，每项 old_text 在当时文本中唯一；保留 CRLF/LF、属主/组/权限，原子替换；失败不改原文件。include_diff 为有界预览，不是功能验证；版本校验不是通用 CAS，超时/断线后先重读。
- init 只创建 `CODE_ROOT/<project>` 与 `DATA_ROOT/<project>/{data,checkpoints,results,logs}`，已有目录不改；不扩大 roots。

所有文件/项目/下载请求路径在本地和远端都校验真实路径与符号链接目标，限 CODE_ROOT、DATA_ROOT、`/tmp`、`/var/tmp`、`/dev/shm`，临时对象须属当前用户；根目录本身是符号链接时，其规范目标也必须仍位于原始授权 roots 内，否则报“根目录符号链接目标越界”；指向父级、`/` 或其他用户目录均拒绝。不读其他用户在 `/home`、`/data` 下的目录。项目 env 和 Conda 路径不扩大授权；文件专用工具没有 confirmed 绕过。

## 指南与服务器说明

get_guide index 只返回目录，不解析配置；其他通用静态主题只读 `docs/topics/<主题>.md`，不解析服务器配置。所有静态主题接受但忽略合法类型的 `server`/`verbose`；未知参数和类型仍严格校验。workspace 默认摘要，verbose 增加名录和来源路径；environment 默认通用摘要与所选服务器元数据/说明可用状态，仅 verbose 读取并拼接 `server-guides/<名称>.md` 及路径，续读保持 verbose。说明缺失为 `server_notes_available=false`，不创建模板。外部说明目录/文件属当前用户且不可被组/他人写，普通文件、无符号链接/多硬链接、最多 128 KiB；展开时校验 UTF-8、无二进制。所有指南只读无网络、不登录、不运行说明命令；主题白名单、分页和字符预算由代码检查。

## 内部职责与不可变边界

固定入口（保持路径、参数、退出码、业务 cwd 和协议）：`mcp/remote_ws_mcp.py` 为 stdio JSON-RPC，只往 stdout 写协议；`mcp/local_connection.py` 为连接 CLI；`mcp/download_worker.py <task_id> <lock_fd>` 为独立本地 worker。内部模块不是新入口。所有本地 Python 调用用 `-B`；Shell/PowerShell 调连接 CLI 必须显式 `python3 -B`，不能依赖调用者环境变量。安装目录无运行时缓存。

模块边界：

- `remote_ws/config.py`：v3 解析、v1/v2 兼容、所选服务器执行上下文；`validation.py`：共享严格类型、输出预算和带执行状态的异常；`paths.py`：可信远端 roots/请求路径；`write_guard.py`：越界写启发式（不是完整 Shell 沙箱）。
- `commands.py`：exec/resource_query 参数、cwd/环境/GPU 编排与结果投影；`transport.py`：SSH 复用、Shell/远程 Python、stdin JSON 请求、有限 stdout/stderr、超时/错误边界，不混业务定位或下载字节流。
- `download_state.py`：任务命名空间、本地安全路径、记录/投影、参数、本地核验、进程身份/锁；`downloads.py`：请求/目的地/启动取消续传及交接；`download_stream.py`：前缀校验、传输、checkpoint、最终发布。
- `sources/`：显式组合的内存远程源码，不是部署目录；`remote_common.py` 提供安全源码基础。
- `job_tools.py`：项目范围任务编排/日志/安全停止，`sources/jobs.py` 组合内嵌实现；`project_tools.py`：init 和 project；`file_tools.py`：文件/下载适配；`text_tools.py`：搜索；`guide_tools.py`：按需指南。入口不复制配置、状态或 SOURCE。

保留既有模块兼容导出、异常类身份和 HEADER_SOURCE/CURSOR_HELPERS/REMOTE_SOURCE 等 SOURCE 组合接口；不增加远程执行次数，不重新解释既有任务记录/游标，不自动重试不确定启动/停止。命令用户值经 stdin JSON，不拼入 Shell 源码。

下载记录/部分文件保持在 `<下载根>/<池>/.jobs/`，池名默认 default，显式 TASK 才隔离；每个 UUID 独立 `.lock`（worker 生命周期）与 `.control`（start/cancel/resume 控制决策），不再以 active.lock 串行锁住整个池。保持原子写/fsync、每个 UUID 的非阻塞排他锁、同锁 fd 跨进程交接、启动窗口、PID 启动身份核验；取消用 pidfd，仅停核验 worker，保留部分文件，不关共享 master。新下载固定来源，续传沿用保存的服务器/路径/roots 快照；cancel/resume 只接受完整下载 ID，默认查询不读成品，显式 verify_local 不回写。

下载流 `protocol=1` 不变：stdin JSON 请求 operation/path/roots/offset/identity/prefix_sha256；stdout 一行 JSON 头＋精确 length 原始字节＋一行 JSON 尾。仅在 worker 验证保存前缀后截未持久化尾部；整文件摘要、源稳定性和 SSH 成功退出后才无覆盖发布；失败不自动重连。

任务保留 job-v2 头、旧日志只读识别、任务 ID 退出标记、PID/属主/启动时钟/服务器启动标识/进程组/会话核验和 pidfd 安全停止；无安全支持时报限制，不退回猜 PID。tail 仍用现有紧凑游标编码、服务器/路径/文件身份/偏移/过滤绑定及续读边界，不重新解释旧游标。下载和任务的底层安全/协议不因默认摘要缩小而变化。

## 维护验证

本目录包含 `tests/`，标准命令是在工具根目录运行：

```bash
python3 -B -m unittest discover -s tests -q
```

测试只使用 Python 标准库（stdlib）和 `unittest`，无网络；远程调用均被 mock，配置/下载使用临时目录，不访问真实服务器、真实配置、活动下载或用户目录，也不写 `__pycache__`。当前为 5 个 `tests/test_*.py`（连接/模式、下载、文件、任务、写保护）；以命令的实际结果为准。

维护变更应由用户授权，使用隔离配置离线验收，不读改真实配置。全部本地 Python 调用显式带 `-B`，不使用会写缓存的 `py_compile`；脚本语法用 `bash -n` 检查。通过 stdin JSON-RPC initialize/tools/list/tools/call 验证入口和指南分页；验证 file_query 的 list/read/search/project 中性兼容与重定向、空 ranges/files、max_chars 夹取、批读 errors/pending/complete；验证 workspace_write 严格拒绝跨分支；验证 job start 显式 project、stop 完整 ID、tail 的 UTF-8 替换/旧日志 pending 与完整身份字段；验证下载 start 中性 task_id、cancel 交接/取消请求、resume 部分文件缺失、verify_local 非 completed、completed 去除 verified_bytes、wait/list 夹取与 server 忽略提示；验证可选空串省略、向上夹取 note、SSH 分类/Proxy/2 MiB 上限。文档链接、示例占位值和无旧目录依赖须一起检查。离线验证不等于联网实传。

真实服务器验收只用 remote-ws MCP，经授权使用本人随机隔离文件/进程并清理；不读写其他用户目录、不抓登录输出。超时/断线先查状态，不重复执行。不触碰活动下载，不迁移下载记录，不自动关闭共享连接。

## 安全与变更边界

约定变化同步根 `AGENTS.md`、对应指南和工具描述，导航变化同步 `mcp/guide.md`；服务器长说明只放外部说明。运行前后核对目录，不做 Git 操作，不遗留日志、报告、缓存或备份。私钥仅留 `~/.ssh`，绝不读取或复制；`config/examples` 保持无真实主机/账号/内网地址。用户级接入仅在完整验证并获准后切换，远端不部署组件。不创建新文档或备份；删除重复膨胀时保留所有安全约束。