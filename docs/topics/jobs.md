# 命令、后台任务和 GPU

## 选择执行方式

GPU 前 `resource_query(resource='gpus')` 查显存/利用率/占用者，选空闲卡；检查不是预留。没有空闲卡时工具会明确提示不要默认使用 GPU 0。服务器未配置时在上下文解析阶段失败，不执行远程 GPU 查询。环境列表用 `resource_query(resource='conda', details=false)`，需要 Python/torch/CUDA 才显式 `details=true`；缺依赖报告，不安装。conda_env 选择已有环境，省略不激活，不手动 activate。

短命令用 `exec(command='<Bash>', cwd='demo', conda_env='<已有环境>')`。cwd 默认 CODE_ROOT，相对路径基于 CODE_ROOT，支持绝对/`~/` 路径。gpus 为非负整数或逗号编号，设置 CUDA_VISIBLE_DEVICES；空串等同未指定。输出默认 2000 字符，`max_output_chars`（256–30000）显式调整，超过 30000 按 30000 执行并提示；`timeout_sec` 默认 120，超过 1800 按 1800 执行并在输出末尾附 `parameter_note`。stdout/stderr 持续读取，只保留有界头尾并标省略，超量不停止命令或偷偷落盘。需要全文时主动将日志写到获授权的数据目录，再分段读取或下载。

用户级 MCP 的 env 在所有业务项目间固定共享，不随当前目录自动切换；不要求业务项目配置 MCP，也不默认设置 REMOTE_WS_PROJECT。`job_control(action='start')` 每次调用都必须在本次参数中显式提供合法 `project`，不能依赖 REMOTE_WS_PROJECT；缺失或非法时在上下文解析及远程调用前拒绝。查询、日志和停止建议显式传 project 以限定业务范围。下载 TASK 与远程 project 是不同范围，默认所有项目共享 default 下载池，显式 TASK 为每个 MCP 进程固定一个池，只用于特殊下载隔离。

长任务用 `job_control(action='start', project='demo', name='train', command='python train.py', conda_env='<已有环境>')`。name/project 均须匹配 `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`（字母或数字开头，总长 1–64，只含字母数字_.-）；start 的 project/name 空串不视为省略，可选 server/cwd/conda_env/gpus 空串按省略。cwd 省略时默认 `CODE_ROOT/<project>`；显式相对 cwd 仍相对 CODE_ROOT。日志在 `DATA_ROOT/<project>/logs`，返回任务 ID、PID、状态和唯一日志路径，不部署服务器辅助脚本。`CONDA_INIT` 非空时 start 先 source 它再激活环境；为空时用 `CONDA_ROOT/bin/activate`，激活失败退出 98。

exec/start 的越界写启发式拦截不代替授权；脚本运行期写出同样受限。先说明并获明确同意，才同参加 confirmed=true；不自动确认，不因命令放行安装组件。`write_guard` 只识别常见写法，不是安全沙箱。超时/断线或部分执行先查实际状态，禁止自动再启动。

## 查询、续读与停止

```text
job_query {"action":"list","project":"demo","verbose":true}
job_query {"action":"tail","project":"demo","job":"<项目内名称或12位短ID>"}
job_query {"action":"tail","project":"demo","job":"<完整任务ID>","errors":true}
job_query {"action":"tail","project":"demo","job":"<同一完整任务ID>","errors":true,"cursor":"<上一页游标>","compact":true}
job_control {"action":"stop","project":"demo","job":"<完整32位小写十六进制任务ID>"}
```

list 按 project（显式值或 REMOTE_WS_PROJECT）限定日志，均缺失才查看全部项目及公共旧日志；`project`/`server` 空串按省略，`limit` 超过 500 按 500 并提示。默认 5 项摘要，`limit`/`verbose=true` 显式扩展数量/元数据。旧日志缺少 `start`/`last_write`/`gpus`/`conda_env` 时显示 `-`，缺 project 也显示 `-`，缺 `job_id` 显示 `legacy`。需要停止时，可先用 `verbose=true` 获取完整任务 ID；不得把摘要中的 12 位 ID 直接用于停止。list 也兼容忽略 tail 中性字段 `job=''`/`cursor=''`/`query=''`/`errors=false`/`compact=false`/`context_lines=0`/`lines=0`/`max_chars=0` 并提示，非中性交叉字段报错。

tail 默认末尾 10 行/2000 字符，返回 text/cursor/more，`lines`/`max_chars`/`context_lines` 显式调整；超过 2000/12000/10 分别按上限执行并提示。`project`/`query`/`cursor` 空串按省略，`job` 仍必填。后续携 cursor 只读新增，more=true 按游标继续，无变化不重复旧日志。活跃任务未写完末行等待换行，pending_line=true；完成后可读无换行尾行。日志中的非法 UTF-8 字节以替换字符返回并标记 `invalid_utf8=true`。旧格式日志仍可只读识别；旧日志活跃且末行未换行时按 pending_line 处理，已结束/已退出任务返回带替换字符的尾行，`status=legacy/unverified`，不冒充已核验身份。

指定项目和完整 ID 时，一次远程请求定位并读取。显式传 `project`、完整 `job`、`cursor` 和 `compact=true` 可省略重复的 task_id/job_id/project/log；仍返回 status、游标、正文和异常标志。首读、其他选择器或不传 compact 时保持原格式。

tail 默认返回 `task_id`（12 位）、完整 `job_id`、`project`、`log`、`status`，以及扫描/命中信息。`job_query` 的 job 仍支持完整 ID、12 位前缀、项目内名称、PID、日志名或路径；完整/短 ID 可定向定位历史任务，不受最近名称查找窗口限制。project 限定范围，跨项目路径拒绝；前缀歧义用完整 ID。查询选择器规则不变，不受 stop 的完整 ID 限制。

过滤用 query（不区分大小写字面关键词）或 errors=true，二选一；context_lines 调整上下文。返回命中上下文及完整 Python Traceback，普通日志也推进游标。初次/后续扫描窗口有界（约 1 MiB/2000 行/5 秒），空结果不代表全部历史无错误；lines 限返回块数。异常未写完时 pending_exception=true，已结束但原文缺尾为 exception_incomplete=true；整块超预算报错，不静默删调用栈，可增预算、分段读或下载。

游标绑定服务器、真实日志、文件身份/偏移及过滤条件；切换过滤、日志或服务器，替换/缩短文件、旧格式游标失效时省略 cursor 重读。分页边缘上下文可能重叠。tail 也兼容忽略 list 中性字段 `limit=0`/`verbose=false` 并提示，非中性值报错。

`job_control(action='stop')` 的 job 必须是完整 32 位小写十六进制任务 ID（`[0-9a-f]{32}`）。缺失或不匹配时在上下文解析及远程调用前拒绝；短 ID、名称、PID、日志名或路径均不可用于停止。先通过 job_query 获取并核对完整 ID，不从短 ID 猜补。stop 兼容中性字段 command/name/cwd/conda_env/gpus 为空字符串、confirmed=false；非中性交叉字段拒绝。

通过参数校验后，stop 仍重新核对日志及实际身份，以 pidfd 只停已核验的同属主/组/会话成员；默认 SIGTERM，force=true 为 SIGKILL，不回退到猜测 PID/killpg。重复 ID 等多候选仍拒绝，不得改用名称/PID/日志路径绕过；旧日志身份不足也拒绝。缺 pidfd 支持报告，不安装。停止期间新派生进程不追杀；部分停止/超时先查任务，不自动再次发送。完整参数矩阵与身份/游标兼容见 [`maintenance`](maintenance.md)。