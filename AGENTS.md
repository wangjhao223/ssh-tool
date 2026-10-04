# AGENTS.md

这是 wjh 的远程 GPU 工作区。Windows Codex 和本地 WSL 都不是服务器；服务器操作必须通过本工作区 remote-ws MCP。本地配置、连接管理和测试可用 PowerShell/WSL。

## 必守约束

- 服务器不安装额外组件、环境或依赖，不部署本方案的辅助脚本/配置，不自动安装 SSH 公钥；缺少已有工具时报告限制。
- MCP 仅用项目 .codex/config.toml，不向全局注册，不改 ~/.ssh/config；全局只保留当前工作区精确路径的信任记录。连接和登录锁按项目及本地用户隔离。
- 密码只由用户在独立 SSH 登录窗口输入。不得索要、读取、保存密码或捕获窗口输出；网络失败不当作密码错误，不重复弹窗。
- 不读、不改其他用户在 /home、/data 下的目录。写入限配置的代码、数据目录及 /tmp、/var/tmp、/dev/shm；其他区域必须先获用户明确同意。exec/run_background 的检测是启发式，脚本运行期写入也受此约束；被拦截后仅用获准的相同参数加 confirmed=true 重试。只读查看其他系统区域不限。
- 专用文件工具校验真实路径及符号链接，临时对象必须属当前服务器用户，没有越界绕过。只编辑现有 UTF-8 文件，先读 SHA-256；不提供上传、整目录下载或删除工具。
- 超时、断线或部分执行报错后，状态可能不确定：先查任务、日志或文件，禁止自动重跑、重写或重复停止。主机密钥异常先核实身份，不自动清除记录。
- 用 GPU 前调用 gpus，指定空闲卡，不占别人正在使用的卡。只用已有环境；长任务用 run_background，日志进数据目录。旧日志身份不可核实时不自动停止。
- 下载只落在本工作区 downloads/，不覆盖已有目标；取消保留部分文件，断线不自动重连，仅手动续传。成功以 state=completed 为准，100% 字节不代表完成。

## 最小操作约定

- 远程操作直接调用目标工具，由工具处理按需登录，无需每次先 status/connect。只查连接用 status（默认一台，all=true 查全部），网络异常用 diagnose。connect 默认开窗即返回，wait_sec 最多 120 秒；失败可在原窗口按 R 重试。窗口未打开可双击本地 Connect.cmd，备用命令用实际项目路径。
- 默认服务器、代码/数据/Conda 目录只读 mcp/remote-ws.conf，各入口共用 DEFAULT_SERVER；无效时报告，不静默换服务器。代码进 CODE_ROOT/<项目>，大文件/数据/权重/结果/日志进 DATA_ROOT/<项目>。
- 参数、状态、排障及维护按需调用 get_guide(topic)；省略 topic 只返回目录，workspace 返回当前配置，不连服务器。工具不可用时读本地 mcp/guide.md 对应节，避免整篇加载。默认返回摘要；需要下载/连接/任务细节再用 verbose=true。
- exec 默认限 6,000 字符，按需调 max_output_chars；jobs 默认 10 项。tail_log 默认 20 行，后续传上次 cursor 只读新增内容，more=true 用返回游标续读；无需无变化时反复轮询。
- 局部查代码/配置/报错先 search_text 定位；incomplete 表示覆盖不全，应缩小范围。read_file 用 ranges 一次读相关行段，默认 blocks 紧凑文本/6,000 字符；edit_file 返回短 diff 和新 SHA，不能代替功能验证。tail_log 可用 query 或 errors 过滤，游标续读须保持过滤条件。
- 需要理解整个项目时先 read_project 查看索引，再按入口/配置/数据流/核心模块/训练评估/测试逐批完整阅读 files，按 pending 续读；记录已读与未读模块，不能把关键词片段、部分索引或一个批次 complete 当成已通读全项目。只对明确的局部任务优先搜索。
- 修改 MCP/脚本前读取 maintenance 节。MCP 仅 Python 标准库；修改后必做编译、stdin 真实 JSON-RPC initialize/tools/list/tools/call，并清理缓存和临时测试文件。改 connect.sh 必须 bash -n。不新增 README 或永久测试目录；同步维护本文件、指南与工具描述。
