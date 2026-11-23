# 文件与编辑

## 先完成当前任务

- 定位：`file_query(action='search', path='demo', query='batch_size')`。字面搜索，不解释 Shell/正则；可用 `file_glob='*.py'` 缩小范围。
- 阅读：`file_query(action='read', path='demo/config.py')`；按返回的 `next_line` 续读，用 `start_line`/`max_lines`/`max_chars` 显式扩大。
- 相关依赖：`file_query(action='project', path='demo', files=[{'path':'config.py'},{'path':'train.py'}])`，批量读选定文件。
- 编辑：`workspace_write(action='edit', path='demo/config.py', expected_sha256='<读取的SHA-256>', edits=[{'old_text':'batch_size = 16','new_text':'batch_size = 32'}])`。
- 初始化：`workspace_write(action='init', project='demo')`；创建代码目录及数据目录下的 data/checkpoints/results/logs，已有目录不改。

局部任务只读目标、相关调用/导入、依赖声明、必要配置和覆盖该行为的测试；不要求先通读全项目或耗尽整个索引。需要结构时用 project 的索引或 list；根据证据逐步补读关联模块。全项目审查才沿入口和数据流扩大覆盖，维护已读/待续读范围，不把搜索片段当作完整模块理解。

## 读取与显式展开

本节的 list/read/search/project 均指 `file_query` 的 action，edit/init 指 `workspace_write` 的 action，不是独立工具。只传当前 action 的字段；`server` 空串按省略；扁平 schema 中的跨分支字段只是兼容入口，运行时按分支校验。输出条数/行数/字符/上下文向上超限按上限执行并提示，结构限制仍严格。

list 绝不递归，默认 25 项，`limit`/`offset` 分页；`limit` 超过 1000 按 1000 执行并提示。`compact` 静默忽略、不增加 `parameter_note`；0–10 的整数 `depth`（布尔值不算整数）也忽略，但显式传 `depth`（包括 0）会提示改用 `action='project'` 获取递归索引。两参数均不发送远程；list 的未知参数、错误类型（含 null）及越界 depth 仍严格拒绝。可选 `server` 空串按省略。

read 默认 100 行/3000 字符、紧凑 blocks；`max_lines` 超过 1000 按 1000 执行并提示，`max_chars` 超过 20000 按 20000 执行并提示。版本为 size/mtime_ns，可编辑文本返回整文件 SHA-256。单段用顶层 next_line；`compact=false` 与 `ranges` 仅保留旧调用兼容，不再作为详细推荐定义（ranges 的结构限制仍严格，详见 [`maintenance`](maintenance.md)）；`ranges=[]` 等同省略。

read 还接受并忽略 `offset`/`limit`/`query`/`file_glob`/`context_lines`/`case_sensitive`/`include_sha256`/`files`/`depth`，仅做基础类型校验；这些字段不发送远程，action 仍为 read。显式传任一字段时 `parameter_note` 列出实际忽略字段，未传且未触发 `max_chars` 夹取时原输出不变。`max_chars` 超过 20000 按 20000 执行并提示，不提高总输出上限。

共享 schema 把 `offset`/`limit`/`start_line`/`max_lines`/`max_chars` 的最小值写成 0，用于接收其他 action 的中性 0；当前 action 的运行时下限仍严格（如 list/search/project 的 limit≥1，read 的 start_line/max_lines≥1、max_chars≥256）。project 嵌套 `files` 项 schema 不标 `required`，由 project 运行时校验 `path` 必须是项目内相对路径；read 仍整体忽略 `files`。

其他 action（list/search/project）也会接收扁平 schema 中的跨分支字段，但仅限中性值：整数 0、`false`、空字符串或 `[]`。中性值本地忽略并写入 `parameter_note`；非中性值直接报错并指出应改用哪个 action（如 `query`→search、`files`→project、`ranges`→read），不会改写成另一个操作。未知字段、错误类型、低于下限及结构越界仍严格拒绝；输出条数/行数/字符/上下文向上超限按上限执行并提示。`workspace_write` 不参与这类兼容，跨分支字段严格拒绝。

project 省略 `files` 或传 `files=[]` 返回紧凑索引，默认 depth=2、limit=60、max_chars=4000；limit 超过 500 按 500、max_chars 超过 20000 按 20000 并提示；按 next_offset 分页。不跟随符号链接，排除 .git/缓存/环境/.jobs，不进入非当前用户目录。`depth` 严格限 0–10。`incomplete`、`depth_limited`、`restricted`、`reason` 表示覆盖受限，缩小范围或继续分页；局部任务不必遍历无关目录。索引是实时视图，目录变化会影响分页。

project 的 `files` 一次 1–8 个项目内相对文件行段，每项 `path` 由 project 运行时校验，默认从第 1 行读最多 120 行；`max_lines` 超过 2000 按 2000、`max_chars` 超过 20000 按 20000 并提示。files 非空时 depth/offset/limit 本地忽略并提示，不与索引分页混用。单个文件缺失、二进制或不可读会进入 `errors` 并继续读余下文件；路径越界、符号链接逃逸等安全错误会使整次批读失败。`pending` 给出未读/未读完文件及续读位置，`complete` 只表示本批无 pending 且无 errors，不表示读过起点前或全项目。批读放不下错误明细时给 `error_count` 和 `errors_truncated=true`；本页无法推进时明确报错，增大 max_chars 或减少 files，不原样重试。文件独立读取，不是项目快照；版本变化需重读受影响文件。本地 JSON 输出超过 `MAX_TEXT`（24000 字符）会报错，需减少文件数或预算。

search 的 `query` 必填，空串不是省略；默认 limit=8、context_lines=0、max_chars=1800，有界返回，可显式扩大；超过上限分别按 100/10/12000 执行并提示。`include_sha256=true` 为可编辑大小文件增加版本核对。只扫描自有普通 UTF-8 文件，不跟随子符号链接；默认排除 .git/__pycache__/.venv/venv/.jobs/data/datasets/checkpoints/weights，明确指定内部文件可读取。扫描受单文件 8 MiB、累计 32 MiB、5000 目录项和 5 秒限制。`incomplete`、`reason` 或 `skipped` 不能当作全目录无匹配；用 match_lines 补读、缩小范围。输出预算小于长行时仍会返回命中文件与 match_lines，blocks 可能为空。二进制/排除项计入 skipped；搜索期间文件变化报错。大日志优先 job_query 的 tail 分支或下载。

## 安全与编辑

相对路径基于所选服务器 CODE_ROOT，支持 `~/`；真实路径及符号链接目标仅限 CODE_ROOT、DATA_ROOT、`/tmp`、`/var/tmp`、`/dev/shm`，临时对象须属当前用户。根目录本身是符号链接时，其规范目标也必须仍位于该服务器的原始授权 roots 内，否则报“根目录符号链接目标越界”；指向父级、`/` 或其他用户目录均拒绝。project 不扩大 roots，不读其他用户目录；文件专用工具没有 confirmed 绕过。

read/edit 文本须为 UTF-8；可编辑文件与结果均不超过 4 MiB，较大文件只分段读，二进制/超长行可下载。下载默认使用所有项目共享的 default 池，无需配置 TASK；同池可并行，但目标不覆盖，详见 downloads。read 期间变化报错，响应受总预算限制。

edit 只改当前用户拥有的现有普通文件，不创建文件、不改多硬链接。expected_sha256 必须匹配整文件；edits 1–100 项顺序应用，每项 old_text 非空且在当时文本中唯一匹配。全部成功并再次核对版本才同目录原子替换，保留属主/组/权限和 LF/CRLF，失败不改原文件。默认 include_diff=false；`include_diff=true` 显式查短 diff，`max_chars` 调整差异预算（默认 1000）。短 diff 仅有界预览，不能替代语法/运行验证。

版本校验不是任意编辑器间的通用 CAS。超时、断线或替换后报错可能已经写入，先重读核对，不自动重写。完整参数、默认值和兼容矩阵见 [`maintenance`](maintenance.md)。