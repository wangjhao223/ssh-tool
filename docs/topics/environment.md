# 当前服务器环境操作

先用 `get_guide(topic='workspace')` 确认当前服务器、configured 状态与实际 roots。用户级 MCP 全局可见，不要求业务项目配置；上下文不随业务目录自动切换，默认不设 `REMOTE_WS_TASK`，下载记录/目录在所有项目间共享 `default` 池，显式 TASK 为每个 MCP 进程固定一个池。服务器选择为显式 `server` > `REMOTE_WS_SERVER` > `DEFAULT_SERVER`；`server` 空串按省略，无效值报错，不静默回退。

- 查已有环境：`resource_query(resource='conda', details=false)`；只列环境。需要 Python/torch/CUDA 时显式 `details=true`，可用 `name='<已有环境>'` 限定，`name` 空串按省略；不存在报错。
- 执行：`exec(command='python ...', conda_env='<已有环境>')`；长任务用 `job_control(action='start', project='<项目>', name='<任务名>', command='python ...', conda_env='<已有环境>')`。start 默认 cwd 为 `CODE_ROOT/<project>`。
- `conda_env` 省略不激活 Conda；不在命令里手动 activate。激活按所选服务器配置：`CONDA_INIT` 非空时先 source 它再 `source activate <环境>`，为空时用 `CONDA_ROOT/bin/activate`；失败停止（退出码 98），不在错误环境继续。
- GPU 前调用 `resource_query(resource='gpus')`，选空闲卡，通过 `gpus` 设置 `CUDA_VISIBLE_DEVICES`；查询不是资源预留。无空闲卡时工具明确提示不要默认使用 GPU 0；未配置服务器在上下文解析阶段失败，不执行远程查询。
- 代码放 `CODE_ROOT/<项目>`，数据/权重/结果/日志放 `DATA_ROOT/<项目>/{data,checkpoints,results,logs}`。Conda 路径不扩大写入授权；缺环境、工具、依赖先报告，不自动安装。
- `resource_query conda details=true` 在未指定 `name` 时按 `CONDA_INIT` 分支探测 base 及 `~/.conda/envs/*`、`CONDA_ROOT/envs/*` 的 Python/torch；指定 `name` 时只探测该环境。这只是已有环境信息，不是安装授权。

本主题默认只读本地配置与通用操作摘要，不加载单机说明正文；仅显式 `verbose=true` 才读取所选服务器 `server-guides/<名称>.md` 并展开来源路径。指南查询均不联网、不登录、不执行说明命令。v3 的 roots/Conda 仅来自所选服务器完整独立配置，不从全局或其他服务器继承。名录不等于已配置；`configured=false` 时不补 roots/Conda，也不据此直接执行。说明不是实时状态，有差异先经授权只读核对。需要单机正文时用 `get_guide(topic='environment', verbose=true)`，按 `next_line` 续读时保持 `verbose=true`；说明缺失返回 `server_notes_available=false`，不创建文件。完整配置、旧版本与参数见 [`maintenance`](maintenance.md)。