# 故障处理

`BAD_ARGS · 未执行` 只修提示字段；`LOGIN_REQUIRED · 未执行` 完成登录；`REMOTE_NONZERO` 看退出码和输出；`OUTCOME_UNKNOWN` 或 SSH 发送后结果未知，先核查目标，不自动重试。其他 `TOOL_ERROR` 不代表未执行。

先分清参数、配置、连接和命令错误；状态不确定先核查，不自动重试有副作用的操作。完整参数与兼容矩阵见 [`maintenance`](maintenance.md)。

- 工具不出现：先看 `workspace(verbose=true)` 返回的 `tool_root`、`tool_version`；如与目标版本不符，核对注册入口并重载后复验；业务项目不要求配置 `.kimi-code/mcp.json`。0.24.0 仍为 10 工具，不启用 deferred；旧公开工具名不是隐藏别名。默认不设置 REMOTE_WS_TASK，所有项目共享 default 下载池，显式 TASK 为每个 MCP 进程固定一个池。
- 参数被拒：action/resource 显式必填，未知值/参数、缺必需字段、错误类型及跨分支非中性值均拒绝。只读可选字段（`server`/`project`/`name`/`query`/`cursor` 等）的空字符串按省略处理；`job_control start` 的 project/name、tail 的 job、stop 的完整 ID 等必填约束不放宽。扁平 schema 只是兼容入口，运行时按 action 分支校验；中性值（整数 0、false、空字符串、`[]`）可能被本地忽略并写 `parameter_note`，非中性跨分支值会提示应改用哪个 action。file_query 共享 schema 的 0 只是中性兼容：list/search/project 的 limit=0、read 的 start_line/max_lines/max_chars=0 仍按 native 下限拒绝；project 嵌套 files 的 path 由运行时校验。输出条数/行数/字符/上下文向上超限按上限执行并写 `parameter_note`，ranges 数量/总行数、depth、写参数等结构限制仍严格。写工具和未知字段不放宽。参数错误先修正，不原样重试。
- job 参数：start 每次必须显式传合法 project，不取 REMOTE_WS_PROJECT；stop 的 job 仅接受完整 32 位小写十六进制任务 ID，不接受短 ID、名称、PID、日志名或路径。这些在上下文解析及远程调用前拒绝，不是配置或连接故障。查询仍可用项目内名称/12 位短 ID 等选择器。
- 摘要太短/内容不全：默认有界输出，显式增 limit/lines/max_lines/max_chars/max_output_chars，或用 verbose/next_line/next_offset/pending/cursor 继续；仅沿当前任务相关依赖补读。get_guide 的 max_lines 默认 80，输出按约 6000 字符预算截断，用 next_line 续读。environment 默认摘要不加载单机正文；需要时用 `verbose=true`，续读保持该参数。incomplete/空搜索/空过滤结果不等于全目录或全历史没有内容。
- 响应超过 2 MiB：file_query/workspace_write/job_query/job_control 的远端 JSON 响应在解析前停止，提示缩小 files/ranges、max_chars、分页或历史条数。编辑超限状态不确定，先重读；停止超限先查询，不自动重写或重复停止。
- configured=false：名录不等于已配置，不是网络/密码错误。workspace 不补 roots/Conda；不要直接执行，由用户授权补齐该机独立配置。当前环境摘要见 environment，权限/版本历史见 maintenance，不改真实配置来“试一下”。
- 配置报错：当前 v3 要求 remote-ws.conf 仅含 FORMAT_VERSION/可选 DEFAULT_SERVER，所选 `servers.d/<名称>.conf` 完整提供 CODE_ROOT/DATA_ROOT/CONDA_ROOT/CONDA_INIT，不共享或继承 roots/Conda。格式/权限/路径不安全仍报错，不能当未配置绕过；不自动迁移、混用或降级版本，不从旧备份恢复共享配置。
- 网络/端口异常或已有 socket 仍失败：用 `connection_query(action='diagnose')`；ProxyJump/ProxyCommand 场景会跳过直连预检，端口可达不代表认证成功。查网络/VPN/地址/端口，不捕获窗口判断密码，不自动关闭共享 master。
- SSH 错误：主机密钥变化先核实、不删 known_hosts；认证失败给出工具返回的完整绝对登录命令；连接超时/拒绝/不可达/MAC 中断等按分类处理，网络错误不当密码错误。
- 登录超时/无法弹窗：完成现有窗口，或把工具返回的完整绝对连接命令原样交给用户在 WSL 终端执行；仅 `REMOTE_WS_LOGIN_GUI=1` 时工具才自动弹窗。`login_incomplete` 表示窗口结束但未连上，`login_nostart` 表示没有窗口开始连接的证据。只有确认原操作未执行才重试。
- 命令非零退出：读输出，不把普通命令错误当 SSH 故障。缺环境、命令或依赖报告，不安装。
- 超时/断线/部分执行报错：可能已经执行，先查任务、日志或文件；不自动重跑、重写或重复停止。
- GPU：无空闲卡时工具会提示不要默认用 GPU 0；未配置服务器在上下文解析阶段失败，不执行远程查询。查询不是预留，启动前再次核对占用。
- 编辑冲突：用 file_query 的 read 重读并核对，再组织 workspace_write 的 edit；替换超时先确认是否已写。短 diff 不是功能验证。exec/start 越界先获明确同意再同参加 confirmed=true；文件工具不绕过，`write_guard` 不是沙箱。
- tail 日志含非法 UTF-8：响应以替换字符显示并标记 `invalid_utf8=true`；活跃任务末行未换行时 `pending_line=true`。旧日志可读但身份为 `legacy/unverified`，不要当作已核验进程。
- tail 游标失效：省略 cursor 重读；换过滤条件也重置。`job_query` 查询遇到任务前缀歧义/同名多候选时，用完整 ID 或唯一日志路径并指定 project。停止必须另行获取并核对完整 32 位小写十六进制任务 ID，停止仍执行日志、进程身份和 pidfd 核验，多候选或旧日志身份不足继续拒绝。
- 下载 interrupted：在原下载池（默认 default，特殊隔离时使用原 REMOTE_WS_TASK）用 `download_query(action='status', task_id='<完整下载ID>', verbose=true)` 查阶段/原因，核对归属后在网络恢复时手动 `resume`。源变/前缀损坏不清空、不覆盖，保存的服务器/roots 快照不随当前配置改变，completed 才完成。
- 下载 busy：busy 可能来自该 UUID 的 `.control` 控制锁（start/cancel/resume 决策进行中，不排队）或 `.lock` worker 生命周期；先查该完整 ID 的状态，稍后重试，不能据此判断同池其他下载被阻塞。取消/续传不接受前缀，同池可并行，但目标不覆盖；需要下载同一来源时显式选择不同 local_path。
- 下载 cancel 刚启动：工具最多等待 2 秒交接；仍无已核验 PID 时返回 `cancelled=false`/`cancel_requested=false`，稍后查状态再试。只有信号实际发送成功且任务仍 ACTIVE 时才返回 `cancel_requested=true`，勿重复发送；PID 已消失或信号未发送成功而任务仍 ACTIVE 时返回 `cancelled=false`/`cancel_requested=false`，稍后重新查询。
- 下载 resume：目标已存在、任务已完成、部分文件缺失或小于记录进度都会拒绝并说明原因；不要删除记录或用手工文件绕过，重新开始下载或先核对状态。
- 下载 verify_local：仅 completed 可用；未完成返回 `status=not_completed` 而非错误。completed 报告不再含 `verified_bytes`/`verification_progress`，以整文件 SHA-256 为准。
- 下载参数提示：list 的 limit 超过 100 按 100 执行并提示；status 的 wait_sec 超过 10 按 10 执行并提示；server、`task_id=""`、`wait_sec=0`、`verify_local=false` 等中性兼容会写入 `parameter_note`，不是执行失败。
- 下载字节流中断：协议头之后远端失败只写 stderr，本地保留部分文件并报告远端 stderr 摘要；不会自动重连，核对后用 resume。不要手工拼接或覆盖目标。