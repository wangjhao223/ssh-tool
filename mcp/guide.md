# remote-ws 按需指南

核心底线在根目录 AGENTS.md。本文件不自动加载；get_guide() 只列目录，指定 topic 只读一个主题。workspace 从当前本地配置生成，不访问服务器。配置以 remote-ws.conf、ssh_config 和 .kimi-code/mcp.json 为准。

## connection — 登录与连接

Kimi Code 在本地 WSL Ubuntu-26.04/root 启动本项目 MCP，服务器账号是 wjh。远程操作直接调用目标工具，工具内置按需登录，无需新对话或每次操作先查 status/connect；工具未出现时重载工作区 MCP 或新开对话。
status(server?, all?, verbose?) 只查本地项目 socket/登录锁，不访问网络。默认只列默认/指定服务器；all=true 查全部，不要求默认服务器有效（默认配置有误也会列出并警告），verbose=true 增加目录配置和端点。存在 socket 不保证链路正常。各工具 server 参数的描述里列有已配置服务器和当前默认服务器。
diagnose(server?, timeout_sec?) 不弹窗、不写远端：无连接只测 SSH 端口，有连接用只读 true 检查链路；默认 3 秒、最大 10 秒。
connect(server?, wait_sec?) 复用已有连接；未连接时先测端口，可达才开独立登录窗口。默认 wait_sec=0，窗口请求发出后立即返回 login_pending；只表示已请求登录，并非认证成功。需要等候可设 1–120 秒。实际远程操作仍最多等 120 秒登录；MCP 启动、status、指南查询不触发登录。写保护先于登录。
用户直接向 SSH 输入密码，MCP 不读窗口。失败窗口保留，可在窗口按 R 手动重试，按 Enter 关闭；重试由用户选择，不自动循环登录。MCP 不捕获窗口判断密码错误：窗口结束但未连上报告 login_incomplete（可能认证失败/取消），窗口一直没开始连接报告 login_nostart；超时后完成登录再重试原操作。
提前连接优先用 WSL 命令，移动工作区后不能沿用旧路径：
```bash
bash scripts/connect.sh
bash scripts/connect.sh 4090a
bash scripts/connect.sh 4090a --close
```
Windows 侧可双击 Connect.cmd，等价 PowerShell 入口：
```powershell
.\Connect.cmd -Server 4090a
.\Connect.cmd -Server 4090a -Close
```
各入口每次读 DEFAULT_SERVER；切换只改 remote-ws.conf。显式 server 覆盖默认值，无效服务器名报错，不静默回退；默认服务器配置无效不影响显式指定 server 的调用。
主机记录在项目 mcp/known_hosts，已忽略且不含密码。socket/登录锁在本地 /tmp/remote-ws-<uid>-<项目真实路径hash>，权限 0700；窗口请求短标记由 MCP 创建、connect.sh 启动时刷新并在退出时清除，15 秒有效期内阻止重复开窗，之后由实际登录锁判定。不同副本独立，不复用/关闭其他项目连接。
当前 ControlPersist 8h 表示无客户端会话空闲 8 小时关闭，持续会话无固定 8 小时总上限。ServerAliveInterval 15、CountMax 3，持续无回应约 45 秒判断失效；心跳不取消空闲关闭。
网络/VPN 中断、休眠、WSL/Windows 重启、服务器重启、管理员终止或手动 Close 均可能断连。只关 Kimi Code 一般保留后台 master，前提是 WSL/SSH 仍运行。root crontab 每小时保活默认服务器（每次实时读 remote-ws.conf，读不到记日志退出），长时间断链多来自 WSL/Windows 重启。

## files — 文件与编辑

专用工具限制 CODE_ROOT、DATA_ROOT、/tmp、/var/tmp、/dev/shm 内真实路径，符号链接目标同样受限，临时对象必须属服务器当前用户。相对路径基于 CODE_ROOT，支持 ~/，无 confirmed 绕过。
read_project(path, server?, files?, depth?, offset?, limit?, max_chars?) 用于理解完整项目。省略 files 返回递归紧凑索引（类型、字节数、项目相对路径），默认深度 4、150 项、正文 12,000 字符；最多深度 10、500 项、20,000 字符，按 next_offset 续页。扫描最多 5000 目录项/5 秒，不跟随符号链接、不进入非当前用户目录，排除 .git/__pycache__/.venv/venv/.jobs；数据和权重目录仍列索引，不读文件内容。incomplete/depth_limited/restricted/reason 指出覆盖不全，需缩小目录或提高深度继续。索引是实时视图，不是不可变快照，分页期间目录变化可能影响顺序。
传 files=[{path:"src/main.py"},{path:"config.yaml",start_line:1,max_lines:1000}] 批量读 1–8 个项目内相对文件，默认每文件从 1 行最多 1000 行（可调最多 2000），共享字符预算，不按关键词裁剪。返回每文件 SHA/版本/原文 blocks 和 pending 续读列表；预算用尽的未读文件也明确列入 pending，按 pending 再调用。complete 只表示本批所选文件从指定起点读到了末尾，不代表全项目已经理解或起点前已读过。索引中的路径使用 JSON 引号及转义，兼容中文、空格和换行文件名。各文件版本独立，批量读取不是全项目一致性快照；续读时版本变化需重新阅读受影响文件。大文件仍按原文件工具规则，UTF-8/二进制/真实路径/属主检查保留；符号链接即使在允许根目录内，批量读取目标也必须在本项目真实目录内。此工具只返回文本，不下载目录或部署服务器文件。
整体阅读顺序：先把分页索引及深度限制看完；阅读入口、依赖声明和配置，再沿数据准备→核心模块→训练/推理/评估→测试与运行脚本逐批通读；依据导入/调用补读关联模块。维护当前对话的已读/待读清单和版本，不用搜索片段代替模块理解。局部改动再 search_text/read_file 定位，修改后做针对性验证；无需将全项目全文永久塞入 AGENTS.md 或重复读取未变文件。
list_files(path, server?, offset?, limit?) 不递归，默认 offset=0/limit=100，最多 1000。返回名称、类型、大小、修改时间、next_offset；到字符上限提前分页。
search_text(path, query, server?, file_glob?, context_lines?, limit?, max_chars?, case_sensitive?) 在指定目录或文件做单行字面搜索，不解释 Shell 或正则。默认 20 处、上下各 2 行、6,000 字符；file_glob 匹配文件名，如 *.py。只扫描自有普通文件，不跟随子符号链接，默认排除 .git/__pycache__/.venv/venv/.jobs/data/datasets/checkpoints/weights 子目录；明确指定其内部文件可读取。二进制/非 UTF-8 不搜索。
搜索上限：单文件 8 MiB、累计 32 MiB、目录项 5000、时间 5 秒；返回 path/match_lines/blocks，可编辑大小的完整文件同时给 SHA-256。incomplete/reason/skipped 明确表示达到限额或存在未扫描对象；不能把部分扫描无匹配当成全目录没有匹配。缩小目录/文件模式继续查，大日志用 tail_log 过滤或分段读取。上下文达到输出预算可能不完整，match_lines 可用于补读；搜索期间文件变化报错。小文件搜索 SHA 可用于后续版本核对，仍需足够上下文且编辑原文本唯一。
read_file(path, server?, start_line?, max_lines?, ranges?, max_chars?, compact?) 默认第 1 行起 200 行（单段最多 1000），正文预算 6,000 字符（256–20,000），返回 blocks=[{start_line,text,next_line}] 紧凑文本、全局 next_line 和版本。compact=false 可恢复逐行 lines 数组。预算按完整行分页，二进制/非 UTF-8/超长单行请下载或增加限额，整个响应仍最多 24,000 字符。
ranges=[{start_line:10,max_lines:20},{start_line:100,max_lines:15}] 一次请求读多个相关行段，最多 8 段、总计 2000 行，须递增且不重叠，不能与 start_line/max_lines/compact=false 混用。预算中断时各 block.next_line 给出该段续读位置；单段全局 next_line 保留原有续读语义。不要为查看修改而反复读整文件。
可编辑文本不超过 4 MiB，读取时返回整文件 sha256；较大文本仅分段读，sha256 为空。读取期间文件变化会报错。
edit_file(path, edits, expected_sha256, server?, include_diff?, max_chars?) 仅编辑当前用户拥有的现有 UTF-8 文件，不创建新文件、不编辑多硬链接，文件及结果均不超过 4 MiB。默认 include_diff=true，差异预算 2,000 字符（256–6,000）；返回短 diff/新 SHA/change_lines/diff_truncated。change_lines 是每项应用前的行号，差异仅是有界预览，不承诺完整；include_diff=false 返回最小成功信息。失败不返回成功预览，差异不能代替运行/语法等功能验证。
```json
{"path":"/home/wjh/myproj/config.py","expected_sha256":"上次 read_file 的 SHA-256","edits":[{"old_text":"batch_size = 16","new_text":"batch_size = 32"}]}
```
edits 1–100 项顺序应用；old_text 非空且在当时文本中恰好匹配一次。全部成功、再次核对版本后，同目录临时数据文件原子替换；保留属主、组、权限和 LF/CRLF 风格，完成清理临时文件。失败不改原文件。
版本校验是乐观并发控制，不是任意编辑器间的通用 CAS。超时/断线或替换后报错可能已写入，先重读确认，不自动重写。

## downloads — 下载、进度与续传

download_file(remote_path, server?, local_path?) 启动独立本地 WSL 后台进程，返回 task_id。默认 downloads/<服务器>/<远端绝对路径去掉开头斜杠>；自定义支持工作区相对、WSL/Windows 绝对路径，均必须在本工作区 downloads/。不覆盖、不绕过符号链接、不使用内部 .jobs 目录。
同工作区仅同时一项下载；忙碌返回 state=busy/当前 task_id，不排队。请求还在登录准备阶段时 ID 可能为空。
list_downloads(state?, offset?, limit?, verbose?) 只读本地，无需登录；从新到旧，默认 10 项、最多 100，返回 total/next_offset。损坏记录单列 unreadable_task_ids，不影响其他任务；新对话用它找回 ID。
download_status(task_id, wait_sec?, verbose?) 最多等 10 秒。摘要含来源、状态、阶段、字节数、总大小、进度及路径，传输中有速度/剩余时间，异常保留原因。verbose=true 增加部分文件路径、前缀 SHA-256、时间和进程信息；内部校验令牌、根目录和启动交接字段不对外返回。
阶段：starting、verifying_local（本地前缀）、verifying_remote（远端身份/前缀）、transferring、verifying_final、completed。running 包括校验，100% 字节不代表完成。
verification_progress/verified_bytes 表示本地校验进度；speed_bytes_sec/eta_sec 仅传输中有效。校验/完成时为空，长时间未更新速度为 0/ETA 为空，仅供参考。
元数据未返回时总大小/进度可能为空。接收前检查磁盘空间；完整接收、SHA-256 一致且源未变化才发布最终文件。成功摘要含 windows_path（显示图片/读取报告）及整文件 sha256。
download_control(task_id, action) 的 cancel 核对本地身份，以 pidfd 停止下载、保留部分文件，不关闭 master。resume 必要时按原流程登录，后台交接后返回，大文件前缀校验不占住 MCP 请求。
后台核对源身份/大小/修改时间和双方前缀 SHA-256；变化/损坏返回 interrupted，不自动重连。校验失败保留原前缀及校验值，目标存在不覆盖。
任务保存实际服务器/目录配置，改 DEFAULT_SERVER 不改变来源。状态/部分文件在 downloads/.jobs/，整个 downloads/ 已忽略；重启 MCP/新对话可查询，后台进程死则标 interrupted。
硬中断仅信任最后持久化且校验通过的前缀；校验后丢弃未保存进度的尾部。MCP 退出后传输通常继续，WSL 退出会中断。无上传/整目录下载/删除工具，服务器不存下载辅助脚本。

## jobs — 命令、后台任务和 GPU

exec(command, server?, cwd?, conda_env?, gpus?, timeout_sec?, confirmed?, max_output_chars?) 用于短 Bash，返回退出码/输出；默认 120 秒、最大 1800。输出总预算默认 6,000 字符，可调 256–30,000；持续读取两路输出，仅保留有界头尾并标明省略，超量不停止命令、不写额外服务器文件。需要全文时自行将命令日志写入允许的数据目录，再分段读取或下载。cwd 绝对/~/路径或相对 CODE_ROOT，默认 CODE_ROOT；conda_env 激活已有环境；gpus 为逗号编号并设置 CUDA_VISIBLE_DEVICES。
越界写启发式拦截；先说明并获明确同意，才以相同参数加 confirmed=true 重试。未检测到的脚本期越界写也须获准；检测不是任意 Shell 的完整隔离。命令放行不代表允许安装组件。
run_background(command, name, project?, server?, cwd?, conda_env?, gpus?, confirmed?) 用 setsid 启动长任务；name/project 仅字母数字_.-。日志默认 DATA_ROOT/logs，指定项目则 DATA_ROOT/<project>/logs。摘要返回任务 ID、PID、状态、唯一日志路径；内容用 tail_log。
新日志首行 # remote-ws job-v2 JSON 记录 ID/PID/属主/启动时钟/服务器启动标识/进程组/会话，文件名有唯一后缀，退出标记带任务 ID。无辅助脚本/配置落服务器。
jobs(server?, limit?, verbose?) 默认 10、最大 500，摘要含名称/核实状态/ID/PID/日志；verbose=true 增加启动/写入时间/GPU/环境，支持中文及空格路径。
tail_log(job, lines?, server?, cursor?, max_chars?) 默认最后 20 行、最大 2000，输出预算默认 6,000 字符，可调 256–12,000。返回 text/cursor/more，后续传上次 cursor 只读新增内容；more=true 表示还有完整内容可用返回游标续读。无变化时 text 为空，不重复旧日志。活跃任务未写完的末行等待换行，pending_line=true；完成后可读无换行尾行。超长单行不跳过，提示增大限额或下载。
游标仅用于阅读位置，绑定服务器、真实日志路径、设备、inode 和字节偏移；换文件、换服务器、替换或缩短时拒绝，省略 cursor 重新读取。初次倒查最多约 1 MiB，长单行不无限读取。job 支持 ID/名称/PID/日志文件名或绝对路径，同名最新；已有日志绝对路径可直接核对，不先扫描任务列表。stop_job(job, force?, server?) 同样定位。
tail_log 还可传 query（大小写不敏感字面关键词）或 errors=true（二选一），context_lines 默认 2、最多 10。过滤初次查末尾约 1 MiB，后续仍用 cursor，只返回命中上下文/完整 Python Traceback 异常块；普通日志也会推进游标，避免重复扫描。游标绑定过滤条件，切换 query/errors/context_lines 必须省略旧游标。一次最多扫描约 1 MiB/2000 行/5 秒；matched_blocks、scanned_bytes、window_start 和 more 表示本次窗口与进度，空结果不代表历史日志无错误。过滤时 lines 限制返回块数。
运行中异常尚未完整写完时 pending_exception=true，游标停在异常开始，等后续查询；已退出但原日志缺尾则 exception_incomplete=true。完整异常块超过输出或扫描预算时明确报错，使用更大限额、read_file 或下载，不静默切掉调用栈。上下文可能在分页边缘重叠，cursor 仍向前推进；无过滤时保留原有行为。
stop_job 重新核对日志/实际身份，以 pidfd 对本次检查到的同属主/同进程组/会话成员发 SIGTERM，force=true 用 SIGKILL，不回退到数字 PID/killpg。身份不符或旧日志信息不足拒绝；缺已有 pidfd 支持报告，不安装。
停止期间新派生进程不自动追杀；部分停止报错/超时先查 jobs，不自动再发送。旧日志可查看及识别退出码，不足显示 legacy/unverified。
gpus 查看显存/利用率/进程及用户，用卡前查并选空闲卡，不占别人卡；检查不是预留。conda_envs 列已有环境及 Python/torch/CUDA，不安装依赖、不手动 activate。
init_project(project, server?) 创建 CODE_ROOT/<project>、DATA_ROOT/<project>/{data,checkpoints,results,logs}，已存在目录不改。

## troubleshooting — 故障处理

工具不出现：重载本工作区 MCP/新对话，必要时重启 Kimi Code 打开项目；不要把 remote-ws 加到全局 MCP 配置。
网络/端口异常用 diagnose，查网络/VPN/地址/端口，不当密码错误。不捕获登录窗口输出判断密码。
登录等待超时：原操作未执行时，完成现有窗口再重试，不开重复窗口。失败可在原窗口按 R 重试；无法弹窗用 WSL 命令 bash scripts/connect.sh。connect 返回 login_pending 时只表示已请求开窗，实际目标工具会继续按需等待；纯状态查询不等待密码输入。
已有 socket 仍失败：diagnose 验链路，不自动关闭其他项目连接。SSH 故障与命令非零码分开，普通报错查输出，不重新登录。
超时/断线可能已执行，先查任务/日志/文件，不自动重跑/重写/重复停止。主机密钥变化先核实，不删 mcp/known_hosts。
保活断链通知只是提醒，不代替用户登录：让 MCP 按需开窗，或用户在 WSL 终端执行 bash scripts/connect.sh 4090a。
编辑冲突重读并核对实际内容再组织替换；超时/断线先确认已否写入。
下载 interrupted 先 download_status（必要 verbose=true）看阶段/原因，网络恢复及登录后手动 resume；源变化/前缀损坏拒续传，不自动覆盖/清空。completed 才是发布结果。
旧任务日志身份不足，先核实进程，不凭 PID 猜测或绕过 stop_job。缺少环境/命令/依赖时报告，不安装。

## environment — 历史环境记录

以下是此前用户服务器记录，不是实时诊断。当前名字/默认值/目录用 workspace，地址/账号/端口看 ssh_config；差异经 MCP 只读核对，不自动修复/安装。
4090a 曾验证 Ubuntu 22.04、4 张 RTX 4090 24GB，多人共用。3090/4090b 此前认证未通过，a100/iflux 此前端口不通；不可当成当前实时状态。
用户 Miniconda 曾在 CONDA_ROOT（当时 /home/wjh/miniconda3），conda-forge 清华镜像；系统 python3 3.10 有 torch 2.3.1+cu121、CUDA 可用，未发现 nvcc。
代码 CODE_ROOT/<项目>，数据/权重/checkpoint/结果/日志 DATA_ROOT/<项目>/{data,checkpoints,results,logs}。此前 /home 空间少，大文件进数据目录；实际空间只读核对。

## maintenance — 本地结构与维护

根目录 AGENTS.md、Connect.cmd、keepalive.sh、.gitignore、.gitattributes、.kimi-code/、scripts/、mcp/、运行时 logs/ 与 downloads/。不新增 README 或永久测试目录。.gitattributes 保证 Git 克隆后 WSL 脚本使用 LF，Windows cmd/ps1 使用 CRLF。
mcp/remote_ws_mcp.py 管注册/stdin JSON-RPC，local_connection.py 管本地配置/诊断/socket/登录，file_tools.py 管文件/下载，text_tools.py 管受限搜索，project_tools.py 管项目索引/批量阅读，job_tools.py 管身份/日志，download_worker.py 是本地独立下载入口；guide_tools.py/guide.md 管按需帮助。
scripts/connect.ps1 管 Windows 窗口，connect.sh 管 WSL 连接（socket 检查之上追加真实链路探测，失效清旧 master 再登录），Connect.cmd 双击入口。组件全在本地，服务器不安装/部署辅助脚本及配置。
keepalive.sh 由 root crontab（每小时第 23 分）和 ~/.kimi-code/config.toml 的 SessionStart hook（--quiet）调用，用项目 ControlPath 检查/刷新默认服务器（实时读 remote-ws.conf 的 DEFAULT_SERVER），日志在 logs/，断线时发 Windows 通知；保活失败只提醒，不代替用户登录。
.kimi-code/mcp.json 是项目级 MCP 配置（仅本工作区生效，env 里 REMOTE_WS_LOGIN_GUI=1 让工具可按需开登录窗口），不注册全局 MCP，不改 ~/.ssh/config。当前本地 WSL Ubuntu-26.04/root，不是远端用户 wjh。
移动目录更新项目 MCP 里的 WSL 绝对脚本路径；更换发行版/本地用户同步 connect.ps1 默认值，配置改后重载/新对话，不把 remote-ws 加到全局 MCP。
remote-ws.conf 唯一配置 DEFAULT_SERVER/CODE_ROOT/DATA_ROOT/CONDA_ROOT。ssh_config 新机写 Host srv-<名字>/HostName，独有 User/Port 在该机块优先匹配，不改全局 SSH。
socket/锁按真实路径+本地 UID 隔离，权限 0700；项目 known_hosts 已忽略，无密码。downloads/.jobs/ 保存状态/部分文件/跨 MCP 锁，整个 downloads/ 已忽略。
MCP 仅 Python 标准库；改后编译修改文件，stdin 真实 initialize/tools/list/tools/call 验证成功/失败边界，完成清理 __pycache__。改 connect.sh/keepalive.sh 至少 bash -n。
临时测试可本地；服务器测试必须经 remote-ws MCP，仅用用户自己的隔离测试文件/进程，完成清理，不抓登录窗口输出。
同步核心 AGENTS.md、对应指南、工具 schema/描述，避免重复详细规则。工具执行参数/路径/版本/身份校验，AGENTS.md 保留无法完全限制的行为底线。
get_guide 固定本地文件/配置，主题允许列表/分页/字符限额是代码检查，无任意路径/命令/远程连接。指南未加载时可本地读取对应节。
默认摘要，verbose=true 查连接/任务/下载详情；内部身份令牌/根目录/启动交接字段不暴露。对比相同请求的说明/元数据/结果字节长度，不能等同推算账单 token 比例。
GitHub 保存完整本地方案：AGENTS.md、Connect.cmd、keepalive.sh、.gitignore、.gitattributes、.kimi-code/mcp.json、scripts/ 及 mcp/ 源码/配置/指南。个人仓库默认私有；downloads/、logs/、known_hosts、任务状态、缓存及临时测试文件不提交。克隆后先按本机实际路径/WSL 发行版/用户更新项目配置，配置服务器地址及目录，再将该工作区标为可信并重载 MCP；服务器不部署任何组件。完整 SSH 地址和本机路径仅适合用户自己的私有仓库。
