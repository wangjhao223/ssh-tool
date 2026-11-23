# 登录与连接

## 先做什么

远程操作直接调用目标工具：自动复用连接或按需登录，无需先查状态；公开 connect 已移除。需要当前路径/配置用 `get_guide(topic='workspace')`。未配置名录不能执行，不补 roots。

- `connection_query(action='status')`：仅查本地 socket/登录锁，无网络、不登录；socket 存在不保证链路正常。默认选中服务器，`all=true` 查全部且不能同时传 `server`，`verbose=true` 查端点及**所选服务器真实**的 configured roots/Conda 状态。
- `connection_query(action='diagnose')`：不弹窗，无连接测 SSH 端口，有连接只读检查链路；`timeout_sec` 默认 3，超过 10 按 10 秒执行并提示。检测到 ProxyJump/ProxyCommand 时跳过直连 TCP 预检，直接提示需要独立登录；端口可达不代表认证成功。
- 无法弹窗、需要提前登录或工具连接失败：把工具返回的**完整绝对连接命令**交给用户在 WSL 终端执行，不要自行拼接本机安装路径。不索要密码或捕获窗口输出。

仅 `REMOTE_WS_LOGIN_GUI=1` 时，工具在未连接时会自动打开 Windows 登录窗口；否则返回登录命令，远程操作不执行。密码只由用户输入；失败窗口按 R 手动重试或 Enter 关闭，不自动循环。`login_incomplete` 表示窗口结束但未连接（可能认证失败/取消），`login_nostart` 表示一直没有窗口开始连接的证据。完成现有登录窗口后，仅确认原操作未执行才重试，不重复开窗。

MCP 启动、指南及 connection_query 的 status/diagnose 分支不请求密码；写保护先于登录。SSH 故障与命令非零退出分开处理，普通命令错误不重新登录。超时、断线可能已执行，先查任务/日志/文件，不自动重跑。

## 服务器与共享连接

服务器选择：显式 `server` > `REMOTE_WS_SERVER` > `DEFAULT_SERVER`，可省略 `srv-` 前缀；`server` 空串按省略处理；无效值报错，不静默回退。名录在外部 ssh_config，不代表已配置；v3 每台独立 roots/Conda，只读所选配置。`status verbose=true` 显示所选服务器的实际配置路径；`configured=false` 时不补默认值。

SSH 错误按 stderr 分类：主机密钥、DNS、端口拒绝、网络超时/不可达、认证失败、MAC/连接中断、连接被关闭等；认证失败给出工具返回的登录命令，其余指向 `diagnose`。远端 JSON 操作（file_query/workspace_write/job_query/job_control）响应超过 2 MiB 会在解析前停止，需缩小范围；编辑/停止超限或超时时状态不确定，先重读/查询，不自动重写或重复停止。

同一工具目录和本地 UID 的项目共享 socket/登录锁，不由单个任务关闭；`Connect.cmd` 的 -Close/--close 同样影响全部接入项目，只能用户显式决定。Windows 可双击 `Connect.cmd`。断网/VPN、休眠、WSL/Windows/服务器重启或人为关闭均可能断连；仅关闭 Kimi Code 通常保留 master，前提是 WSL/SSH 仍运行。保活不代替用户登录，不改真实配置或主机密钥记录。

用户级 MCP 在所有项目全局可见，不启用 deferred，10 个工具定义有固定上下文代价；业务项目不要求配置 `.kimi-code/mcp.json`。默认不设置 `REMOTE_WS_TASK`，所有项目共享 `default` 下载池；显式 TASK 只用于特殊隔离，详见 downloads/maintenance。完整参数矩阵见 [`maintenance`](maintenance.md)。