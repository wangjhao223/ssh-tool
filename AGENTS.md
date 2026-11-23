# remote-ws 直接共用版

本目录是 0.24.0 直接共用工具目录，不是业务工作区。同一个目录供多个项目直接使用；不要为使用而修改本目录或向业务项目复制工具、添加项目级 MCP 配置。用户级 MCP 在 `~/.kimi-code/mcp.json` 注册一次后全局可用；入口路径仅在完整验证并获准切换后更新，不覆盖已有注册。

## 使用约定

- 入口：`get_guide()`；当前路径与配置：`get_guide(topic='workspace')`。公开 10 工具，不启用 deferred；旧公开名不注册，也不是隐藏别名。
- 直接调用目标工具，自动按需登录；默认摘要和有界输出，只传本次分支参数；按 `next_line`/`next_offset`/`pending`/`cursor` 续读。
- 只读可选字段（`server`/`project`/`name`/`query`/`cursor` 等）的空字符串按省略处理；`job_control(action='start')` 的 project/name、tail 的 job、stop 的完整任务 ID 等必填约束不放宽。输出条数/行数/字符/上下文向上超限按既有上限执行并在 `parameter_note` 说明；ranges 数量/总行数、depth、写参数等结构限制仍严格。
- 日志首次 `job_query(action='tail')` 保留完整结果；后续用同一 server、显式 project、完整 job ID 和返回的 cursor，并传 `compact=true`，除非需要重看元数据。
- 默认不设置 `REMOTE_WS_TASK`，所有项目共享 `default` 下载池；显式 TASK 为每个 MCP 进程固定一个池，仅用于特殊隔离，禁止跨池查询或控制。
- `job_control(action='start')` 每次调用都在本次参数中显式提供合法 `project`，默认 cwd 为 `CODE_ROOT/<project>`；`stop` 的 `job` 仅接受完整 32 位小写十六进制任务 ID。GPU 前查 `resource_query(resource='gpus')`，选空闲卡；查询不是预留。
- 名录不等于已配置；v3 每台服务器独立提供 roots/Conda，未配置不补默认值、不直接执行。实际状态以只读 `workspace`/`connection_query` 为准，不读真实配置核对文档。
- 完整参数、兼容矩阵、配置和维护见 [`get_guide(topic='maintenance')`](docs/topics/maintenance.md)；主题导航见 [`mcp/guide.md`](mcp/guide.md) 与 [`docs/topics/`](docs/topics/)。

## 安全边界

- 本地是 WSL，不是服务器；远程操作只用 remote-ws MCP，不用本地 Bash 代替。
- 密码只由用户在 SSH 窗口/终端输入，不索要、读取、保存或捕获窗口输出。仅 `REMOTE_WS_LOGIN_GUI=1` 时工具才自动弹窗；否则把工具返回的完整绝对连接命令原样交给用户。网络错误不当密码错误；主机密钥变化先核实，不删除记录绕过。
- 不读写其他用户在 `/home`、`/data` 下的目录。写入仅限所选服务器授权的 CODE_ROOT、DATA_ROOT、`/tmp`、`/var/tmp`、`/dev/shm`；项目或 Conda 环境不扩大授权。脚本运行期写出同样受限；越界先说明并获明确同意，才同参加 `confirmed=true`；文件专用工具不能绕过。`write_guard` 只是常见写法的启发式检查，不是安全沙箱。
- 用 `conda_env` 选择已有环境，不手动 activate；缺依赖报告，不安装服务器组件、依赖或本工具脚本。长任务用 `job_control`；停止核实身份，不猜 PID，不关闭共享 SSH。
- 超时、断线、部分执行报错后先查任务/日志/文件，不自动重跑、重写或重复停止；短 diff 不代替功能验证。
- 所有本地 Python 调用显式用 `-B`；不改用户配置、真实服务器配置或活动下载，不创建备份，不读取密钥，不遗留缓存。
- 标准验证：在本目录运行 `python3 -B -m unittest discover -s tests -q`。测试仅用标准库（stdlib）、无网络，使用临时目录和 mock，不访问真实配置、服务器或下载池。