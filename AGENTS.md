# AGENTS.md

本目录是用户 wjh 的远程 GPU 服务器工作区。本地是 WSL（Ubuntu-26.04/root），**不是服务器**；服务器操作一律通过本项目 remote-ws MCP 工具（`mcp__remote-ws__<工具>`），不要用本地 Bash 假装在服务器上操作。

## 服务器与目录约定

- 默认服务器 4090a（多人共用）；用 GPU 前先调 `gpus` 查空闲卡。配置见 `mcp/ssh_config`、`mcp/remote-ws.conf`，其余服务器状态见 `get_guide('workspace')`。
- 代码放 `/home/wjh/<项目>`；数据/权重/checkpoint/结果/日志放 `/data/wjh/<项目>/{data,checkpoints,results,logs}`。大文件不要放代码目录。
- 环境使用按服务器区分，一律传工具的 `conda_env` 参数，不在命令里手动 activate：4090a 用 `/home/wjh/miniconda3`（用户 Miniconda，自动 `source …/bin/activate`）；a100 用系统预装 anaconda（自动 `source /opt/acd` 后 `source activate`）；3090/4090b/iflux 暂不约定。细节见 `get_guide('environment')`。

## 工具用法

- 直接调用目标工具，工具内置按需登录，无需先 `status`/`connect`；只查连接用 `status`，网络异常用 `diagnose`。
- 连接失败时，把工具返回的 `bash scripts/connect.sh <服务器>` 原样交给用户在 WSL 终端执行（在项目根运行）；绝不索要、保存或处理密码，网络失败不当作密码错误。
- 长任务用 `run_background`，日志在 `DATA_ROOT/<项目>/logs`；细节用 `get_guide(topic)`。

## 本地保活

- root crontab 每小时保活默认服务器，SessionStart hook 静默刷新；细节见 `get_guide('maintenance')`。

## 规则

- 密码只由用户在独立 SSH 登录窗口或 WSL 终端输入；不得索要、读取、保存密码，不捕获窗口输出。
- 不读、不改其他用户在 `/home`、`/data` 下的目录。
- 服务器写/修改只允许 `/home/wjh`、`/data/wjh` 和临时目录（`/tmp`、`/var/tmp`、`/dev/shm`）；越界写须先获明确同意；被拦截时先说明，同意后同参加 `confirmed=true` 重试。脚本运行期写出同样受此约束。
- 超时、断线或部分执行报错后状态不确定：先查任务、日志或文件，禁止自动重跑、重写或重复停止。
- 服务器上不安装额外组件/依赖或本方案脚本；缺少工具时报告限制。

## 本目录结构与维护

- MCP 只用项目 `.kimi-code/mcp.json`，不注册全局，不改 `~/.ssh/config`。
- `mcp/` 按 remote_ws_mcp、local_connection、file_tools、text_tools、project_tools、job_tools、guide_tools 分工，脚本在 `scripts/`；其余维护与 GitHub 发布细节见 `get_guide('maintenance')`。
- 修改 MCP/脚本后必须：`python3 -m py_compile` 所有改动文件；通过 stdin JSON-RPC（initialize、tools/list、tools/call）实测成功与失败边界；删掉 `__pycache__`。改 `.sh` 至少运行 `bash -n`。

改动约定、服务器或工具时，同步更新本文件、`mcp/guide.md` 和工具描述。
