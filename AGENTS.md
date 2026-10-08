# AGENTS.md

本目录是用户 wjh 的远程 GPU 服务器工作区。本地是 WSL（Ubuntu-26.04/root），**不是服务器**；凡是要在服务器上做的事，都通过本项目 remote-ws MCP 工具完成（工具名形如 `mcp__remote-ws__<工具>`），不要用本地 Bash 假装在服务器上操作。

## 服务器与目录约定

- 服务器配置在 `mcp/ssh_config`，统一使用 `User wjh`、`Port 30002`，别名为 `srv-<名字>`：
  - 4090a：10.180.96.175，默认服务器。Ubuntu 22.04，4×RTX 4090 24GB，多人共用。
  - 3090、4090b：wjh 的密码不对，暂不可用。
  - a100、iflux：端口不通，暂不可用。
- 代码放在 `/home/wjh/<项目>`。
- 数据集、权重、checkpoint、结果、日志放在 `/data/wjh/<项目>/{data,checkpoints,results,logs}`。
- 大文件不要放进代码目录，`/home` 剩余空间少。
- 以上约定在 `mcp/remote-ws.conf` 中（`DEFAULT_SERVER`、`CODE_ROOT`、`DATA_ROOT`、`CONDA_ROOT`）。
- Conda：自己的 Miniconda 在 `/home/wjh/miniconda3`，使用 conda-forge 的清华镜像源。系统 `python3`（3.10）自带 torch 2.3.1+cu121，CUDA 可用。系统没有 `nvcc`。

## 工具用法

- 远程操作直接调用目标工具，工具内置按需登录，无需每次先 `status`/`connect`。只查连接用 `status`（默认一台，`all=true` 查全部且不要求默认服务器有效），网络异常用 `diagnose`，想提前连接用 `connect`（默认开窗即返回，`wait_sec` 最多 120 秒）。各工具的 `server` 参数描述里列有已配置服务器和当前默认服务器。
- 连接不可用时（如 WSL 重启、断网超 8 小时）：先让工具打开登录窗口（`REMOTE_WS_LOGIN_GUI=1`，用户在窗口里输一次密码）；开不了窗时，把工具返回的连接命令 `bash /root/workplace/ssh-tool/scripts/connect.sh <服务器>` 原样交给用户在 WSL 终端执行。绝不索要、保存或处理密码；网络失败不当作密码错误，不重复开窗。
- `exec`：远端短 Bash，返回退出码和输出；默认 6,000 字符、120 秒，可调 `max_output_chars`（≤30,000）、`timeout_sec`（≤1800）。`cwd` 填服务器路径或相对 `CODE_ROOT` 的路径；用 `conda_env` 进已有环境，不要在命令里手动 activate；用 `gpus` 指定卡（会设置 `CUDA_VISIBLE_DEVICES`），用前先查 `gpus`。需要长输出时重定向到数据目录，再用 `read_file`/`tail_log` 分段看。
- 长任务（训练、预处理，以及任何超过几分钟的任务）用 `run_background`，日志写到 `/data/wjh/<项目>/logs/`；之后用 `jobs`、`tail_log`、`stop_job` 管理。`tail_log` 默认 20 行，续读传上次 `cursor` 只读新增内容；可用 `query`/`errors` 过滤。
- 读写服务器文件用文件工具：`list_files`、`read_file`（默认 200 行/6,000 字符，`ranges` 一次读多段）、`edit_file`（先读 SHA-256，`old_text` 唯一匹配才原子替换）、`search_text`（字面搜索，`incomplete` 表示覆盖不全）、`read_project`（项目索引与批量通读）、`download_file`/`download_status`/`list_downloads`/`download_control`（下载到本项目 `downloads/`，不覆盖，成功以 `state=completed` 为准）。
- `get_guide(topic)` 按需读本地帮助；省略 topic 只列目录，`workspace` 返回当前配置，不连服务器。细节以 `mcp/guide.md` 对应节为准。
- `init_project` 按约定创建代码目录和数据目录 `{data,checkpoints,results,logs}`，已有目录不改。

## 本地保活

- root 的 crontab 每小时（第 23 分）执行 `keepalive.sh`：用项目专用 ControlPath 检查并刷新默认服务器的连接（每次实时读 `remote-ws.conf` 的 `DEFAULT_SERVER`，读不到会记日志并退出，不静默盯旧机器；打开真实 channel 刷新 8 小时计时，绝不触发密码提示）。
- 日志 `logs/keepalive.log` 只在状态变化或每天一条心跳（04 点）时记录；断线时发 Windows 桌面通知（4 小时最多一次）；`bash keepalive.sh --notify-test` 可发测试通知；`-h|--help` 看用法，未知参数报错退出，不会误触发检查。
- `~/.kimi-code/config.toml` 里的 SessionStart hook 还会在每次会话启动时静默刷新一次连接（`--quiet` 不写日志不通知）。
- 取消保活用 `crontab -e` 删那一行；hook 在该文件末尾的 `[[hooks]]` 块。

## 规则

- 服务器上不安装额外组件、环境或依赖，不部署本方案的辅助脚本/配置，不自动安装 SSH 公钥；缺少已有工具时报告限制。
- MCP 只用项目 `.kimi-code/mcp.json`，不注册到全局，不改 `~/.ssh/config`。连接和登录锁按项目及本地用户隔离。
- 密码只由用户在独立 SSH 登录窗口或 WSL 终端输入。不得索要、读取、保存密码，不捕获窗口输出。
- 不读、不改其他用户在 `/home`、`/data` 下的目录。
- 服务器上的写/修改只允许落在 `/home/wjh`、`/data/wjh` 和临时目录（`/tmp`、`/var/tmp`、`/dev/shm`）；写其他区域必须先征得用户明确同意。`exec`/`run_background` 的越界写检测是启发式，脚本运行期写出同样受本条约束；被拦截时先向用户说明，用户明确同意后才以相同参数加 `confirmed=true` 重试。
- 文件工具校验真实路径和符号链接，临时对象必须属当前服务器用户；只编辑现有 UTF-8 文件，不提供上传、整目录下载或删除工具。
- 超时、断线或部分执行报错后状态可能不确定：先查任务、日志或文件，禁止自动重跑、重写或重复停止。主机密钥异常先核实身份，不自动清除 `mcp/known_hosts` 记录。
- 用 GPU 前调用 `gpus`，指定空闲卡，不要占用别人正在用的卡。旧日志身份不可核实时不自动停止。
- 下载只落在本项目 `downloads/`，不覆盖已有目标；取消保留部分文件，断线不自动重连，仅手动续传。

## 本目录结构与维护

- `.kimi-code/mcp.json`：项目级 MCP 配置（`command`/`args`/`cwd`/`env`/`toolTimeoutMs`），只在本目录启动 Kimi Code 时生效，其他工作区不受影响。修改后新开会话生效。
- `mcp/`：`remote_ws_mcp.py` 管注册和 stdin JSON-RPC，`local_connection.py` 管本地配置/诊断/socket/登录窗口，`file_tools.py` 管文件与下载，`text_tools.py` 管受限搜索，`project_tools.py` 管项目索引/批量阅读，`job_tools.py` 管任务身份与日志，`download_worker.py` 是本地下载入口，`guide_tools.py`/`guide.md` 管按需帮助。
- `scripts/connect.sh` 管 WSL 登录（socket 检查之上再做真实链路探测，失效会清掉旧 master 重新登录，并负责刷新/清理登录窗口 stamp），`scripts/connect.ps1` 管 Windows 登录窗口，`Connect.cmd` 是 Windows 双击入口。
- 修改 MCP/脚本后要做三件事：`python3 -m py_compile` 所有改动文件；通过 stdin 发送 JSON-RPC 请求（initialize、tools/list、tools/call）实际调用工具，验证成功与失败边界；删掉 `__pycache__`。改 `connect.sh`/`keepalive.sh` 至少运行 `bash -n`。
- `mcp/ssh_config`：加服务器时写 `Host srv-<名字>` 和 `HostName <IP>`；某台机器账号或端口不同时，在它自己的块里加 `User` 或 `Port`。不要改 `~/.ssh/config`。
- 服务器上不放本方案的辅助脚本或配置，所有工具都在本目录。不新增 README 或永久测试目录。
- 改动约定、服务器或工具时，同步更新本文件、`mcp/guide.md` 和工具描述。
