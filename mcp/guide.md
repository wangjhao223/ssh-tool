# remote-ws 操作导航

0.24.0 仅公开 10 工具：get_guide、connection_query、resource_query、exec、job_query、job_control、file_query、workspace_write、download_query、download_control；不启用 deferred，旧公开名不是隐藏别名。核心安全底线见根目录 `AGENTS.md`。工具描述只保留简要用途；完整参数、兼容矩阵、配置、协议与维护验证只在 [`maintenance`](../docs/topics/maintenance.md)，本页只做任务导航。

按任务选择 `get_guide(topic='<主题>')`：

- `workspace`：当前服务器/项目/task、configured 状态与实际路径；`verbose=true` 增加运行中的 tool_version/tool_root、名录和维护路径；仅排障或验收时需要。
- [`connection`](../docs/topics/connection.md)：本地连接状态、网络诊断、用户登录、SSH 错误分类。
- [`files`](../docs/topics/files.md)：定位文件/依赖、分段读取、字面搜索、批读、校验编辑、初始化项目。
- [`jobs`](../docs/topics/jobs.md)：GPU、短命令、后台任务、日志续读与安全停止。
- [`downloads`](../docs/topics/downloads.md)：启动、查进度、本地核验、取消、手动续传。
- [`environment`](../docs/topics/environment.md)：当前服务器环境操作摘要及说明可用状态；`verbose=true` 才展开所选服务器说明。
- [`troubleshooting`](../docs/topics/troubleshooting.md)：按错误选择下一步。
- [`maintenance`](../docs/topics/maintenance.md)：唯一完整参数/兼容矩阵、配置与历史、内部职责、协议与维护验证。

通用约定：直接调用目标工具，自动按需登录；默认紧凑摘要和有界输出，需要更多时显式传 `verbose`、行数/字符预算或续读位置。`action`/`resource` 必须显式给出。只读可选字段（`server`/`project`/`name`/`query`/`cursor` 等）的空字符串按省略处理；输出条数/行/字符/上下文向上超限按既有上限执行并在 `parameter_note` 说明，结构限制仍严格。默认不设置 `REMOTE_WS_TASK`，所有项目共享 `default` 池；显式 TASK 为每个 MCP 进程固定一个池，仅用于特殊隔离。

`get_guide()` 只返回目录，不解析配置。正文来自 [`../docs/topics/`](../docs/topics/) 下的 `<主题>.md`，按 `start_line`/`max_lines` 和返回的 `next_line` 续读；`max_lines` 默认 80，输出按约 6000 字符预算截断。index 等通用静态主题接受但忽略合法类型的 `server`/`verbose`；workspace/environment 使用服务器选择，environment 仅 `verbose=true` 才读取所选说明正文。指南只读、无网络、不创建目录；未配置返回明确状态，不伪造 roots。