# 下载、进度与续传

## 操作

```text
download_control {"action":"start","remote_path":"<远端文件>"}
download_query {"action":"list"}
download_query {"action":"status","task_id":"<完整下载ID>"}
download_control {"action":"cancel","task_id":"<完整下载ID>"}
download_control {"action":"resume","task_id":"<中断下载ID>"}
```

默认不设置 `REMOTE_WS_TASK`，所有项目共享 `default` 下载池的记录和目录，目录为 `~/.local/share/remote-ws/downloads/default`；可在 workspace 查看当前池。`REMOTE_WS_TASK` 为每个 MCP 进程固定一个池，仅在特殊隔离时显式设置稳定值（字母数字_.-，最多 64 字符），使用 `<下载根>/<REMOTE_WS_TASK>`。池之间记录/目录/锁隔离，禁止跨池查询或控制；根目录覆盖及维护路径见 [`maintenance`](maintenance.md)。

start 返回完整下载 UUID（task_id），在本地 WSL 后台下载。默认目标为 `<池目录>/<服务器>/<远端绝对路径去掉开头斜杠>`；`local_path` 可自定义，WSL 绝对路径、Windows 盘符路径（转换为 WSL 路径）或相对路径都须落在当前池目录内。不覆盖目标、不绕过符号链接、不使用内部 `.jobs`；同池并行下载同一来源时须指定不同目标，不能覆盖或争用已有成品。

每个下载 UUID 使用独立锁，同 TASK/同 default 池可并行多个下载；同一 UUID 的启动交接、取消和续传仍互斥。每个 UUID 另有 `.control` 控制锁，串行化 start/cancel/resume 决策，与 worker 生命周期 `.lock` 分离；控制操作进行中返回 `state=busy`、提示“控制操作正在进行”且不排队，旧 `.lock`/`.control` 文件保留、不自动清理。显式 TASK 只隔离记录/目录，不是启用并行的前提。cancel/resume 只接受完整 task_id，不支持前缀，沿用记录的服务器/路径快照；server 可传但忽略，remote_path/local_path 只兼容空字符串，非空值拒绝。

list 只读当前池本地记录，无网络；默认 5 项摘要，`limit`（超过 100 按 100 执行并提示）/`offset`/`state` 调整范围，损坏记录列 `unreadable_task_ids`。默认池中新对话/其他项目可找回同一批 ID；显式隔离时须使用相同 REMOTE_WS_TASK。`verbose=true` 查时间等详情。共享记录可见不代表可以取消其他项目的下载，操作前先核对完整 ID、路径与归属。list 兼容忽略 `task_id=""`、`wait_sec=0`、`verify_local=false`、`server`（均提示）；非中性值拒绝，`server` 不切换服务器。

小文件需要马上使用时，start 后用一次 `download_query(action="status", task_id=完整ID, wait_sec=10)` 有界等待；超时仍按实际 state 判断。长下载保存 ID，不反复查空进度。

status 查状态/阶段、字节、进度及路径；传输中有速度/ETA，仅作参考。`verbose=true` 查部分文件/摘要/进程信息，内部令牌、roots、交接字段不对外暴露。`wait_sec` 默认 0，超过 10 按 10 执行并提示；`server` 可传但忽略，使用记录中的服务器快照。默认查询不读成品；`verify_local=true` 仅对 `completed` 记录核验当前本地文件大小和 SHA-256，报告 `verified`/`missing`/`size_mismatch`/`sha256_mismatch`/`changed_during_check`/`not_regular_file`/`present` 等结果，不改记录、不修复、不重下。未完成任务的 `verify_local` 返回 `status=not_completed`，不是错误。completed 记录不再显示仅用于续传前缀核验的 `verified_bytes`/`verification_progress`。两种查询都不接受非中性跨分支参数。

## 判断完成与恢复

starting → verifying_local → verifying_remote → transferring → verifying_final → completed。running 包括校验，100% 字节不是完成；只有完整接收、SHA-256 一致、源稳定且传输成功，才无覆盖发布，成功含 `windows_path` 和整文件 `sha256`。接收前检查磁盘空间。

cancel 核对本地进程身份，以 pidfd 停 worker，保留部分文件，不关闭共享 SSH。刚启动的任务可能还在交接锁：工具最多等待 2 秒寻找已核验 PID；仍处于 starting 且没有可核验 PID 时返回 `cancelled=false`、`cancel_requested=false` 并提示稍后重试，不向未核验 PID 发信号。已发信号但进程尚未退出时，只有信号实际发送成功才返回 `cancel_requested=true`，提示稍后查询、勿重复发送；PID 已消失或信号未发送成功而任务仍 ACTIVE 时返回 `cancelled=false`、`cancel_requested=false`，并提示稍后重新查询。缺 pidfd 支持时报错且不退回数字 PID 信号。

resume 手动触发，必要时按需登录，交接后台后校验双方前缀，不占住 MCP；源身份/大小/修改时间或前缀不符会 interrupted，不自动重连、清空或覆盖。记录快照包含最初服务器及 roots，当前配置/默认服务器变化不改变来源。resume 前若目标已存在则拒绝覆盖；`completed` 任务不能续传；部分文件缺失返回“部分文件缺失，无法续传；请重新开始下载”，部分文件小于已记录进度也拒绝续传。

记录/部分文件留在当前池目录 `.jobs`，重启 MCP/新对话可继续查询。硬中断后只信最后持久化且校验通过的前缀，worker 验证后才丢弃未保存尾部；MCP 退出通常继续，WSL 退出会中断。状态不明先查，不重复启动或取消。

## 协议与兼容

下载字节流固定 `protocol=1`：stdin JSON 请求，stdout 一行 JSON 头（ok/protocol/path/identity/offset/length）＋精确 length 原始字节＋一行 JSON 尾（ok/sha256/stable）。头之后若失败只写 stderr，不再混入 JSON，避免污染部分文件；本地 worker 将远端 stderr 末尾（约 2000 字节）并入中断原因。本地先验证保存前缀的 SHA-256，再截断未持久化尾部；最终文件通过硬链接方式无覆盖发布，失败不自动重连。

download_control 的 start 仅兼容忽略精确空字符串 `task_id=""`；start/cancel/resume 均兼容中性 `verbose=false`/`wait_sec=0`/`verify_local=false` 并提示；cancel/resume 还兼容空字符串 `remote_path=""`/`local_path=""` 和 `server` 忽略提示。响应中的 `parameter_note` 会写明如 `task_id="" 已忽略`、`server 已忽略`、`wait_sec=0 已忽略`、`verify_local=false 已忽略`，不代表操作失败。非空 task_id、null、错误类型、非中性跨分支值及未知参数仍拒绝。action 显式必填；status 需完整 task_id。无上传/整目录下载/删除工具，不部署服务器辅助文件。记录/锁/fd/PID 核验与 protocol=1 字节流保持兼容，完整参数矩阵和状态字段见 [`maintenance`](maintenance.md)。