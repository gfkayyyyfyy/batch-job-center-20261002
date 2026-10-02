# 轻量批处理任务中心

建设用于本地重复性工作的批处理任务产品，逐步覆盖任务登记、入队执行、状态查询、重试与取消、结果留存、依赖顺序、简单定时计划和执行记录。

计划采用：Python 3 标准库 / sqlite3 / subprocess / argparse。

## 使用说明

仅需 Python 3 标准库，无需安装依赖。所有命令通过 `python -m task_center` 调用，
每次输出一个 JSON（字段：`id`、`status`、`result`、`error`），成功退出码为 0，
拒绝操作或执行失败退出码为 1 且 `error` 填写错误码。

```bash
# 登记任务（不执行），返回唯一 id 与 queued 状态
python -m task_center submit csv_summary --input demo/sales.csv

# 带请求键登记：同一数据库内重复提交同一请求时返回原任务
python -m task_center submit csv_summary --input demo/sales.csv --request-key sales-1

# 手动执行一个 queued 任务，返回最终记录
python -m task_center run <id>

# 查询已保存的任务记录（退出后仍可查询）
python -m task_center show <id>

# 让 failed 任务重新入队（沿用原 id，不立即执行、不读取文件）
python -m task_center retry <id>

# 取消一个 queued 任务（保留记录，不再执行、不读取文件）
python -m task_center cancel <id>

# 指定数据库文件（默认使用项目目录下 tasks.sqlite3，首次使用自动创建）
python -m task_center --db /path/to/tasks.sqlite3 show <id>
```

### csv_summary 任务

- 输入路径相对于项目目录解释，只接受 `demo/` 目录内的普通文件。
- 文件为 UTF-8 CSV，首行表头 `category,amount`；完全空行忽略，每条数据恰两列。
- `category` 去除首尾空格后不能为空、区分大小写；`amount` 去除首尾空格后须匹配
  `[0-9]+`，按非负整数汇总。
- 成功结果含 `row_count`（行数）、`total_amount`（金额总和）、`categories`（类别金额映射）。
- 提交仅检查类型与路径，内容在执行时校验；失败不保留部分结果。

### 请求键（--request-key）

- 可选参数；不提供时保持原行为，同一文件反复提交仍得到不同 id。
- 键须为 1 至 64 个 ASCII 字母、数字、下划线或连字符，区分大小写，不裁剪空白；
  不合法时返回 `invalid_request_key`（先检查任务类型，类型非法仍返回 `invalid_type`）。
- 同一数据库内按键识别重复请求，不同数据库互不影响；绑定跨进程保留，
  run、retry、cancel 均不解除绑定。
- 首次使用有效键时仍只检查输入路径：成功创建 queued 任务（result 与 error 为 null，
  不读取 CSV 内容）；路径不合法返回 `invalid_input`，不创建任务也不占用该键。
- 已有绑定且任务类型与输入路径原字符串都相同时，直接返回该任务当前保存的完整记录
  （退出码 0）：无论 queued、succeeded、failed 还是 cancelled，都不新增任务、不改变
  状态、不重置结果或错误，也不再检查文件。匹配按路径原字符串进行，相对路径与绝对
  路径等不同写法视为不同请求。
- 已有绑定而类型或路径不匹配时返回 `request_conflict`（退出码 1），不检查新路径、
  不修改原任务。
- 旧版数据库首次使用时自动补齐键绑定列；既有任务的键为空，不参与请求键匹配。

### 错误码

| 错误码 | 含义 |
| --- | --- |
| `invalid_type` | 未知任务类型 |
| `invalid_request_key` | 请求键不是 1-64 个 ASCII 字母/数字/下划线/连字符 |
| `invalid_input` | 提交时路径在 demo 目录外、非普通文件或文件不存在（不创建任务，不占用键） |
| `request_conflict` | 请求键已绑定其他任务类型或输入路径（不检查新路径、不修改原任务） |
| `input_error` | 执行时文件不可读 |
| `data_error` | 执行时编码、表头、列数或字段不合法 |
| `job_not_found` | 任务 id 不存在 |
| `invalid_state` | 对非 queued 任务执行 run 或 cancel、或对非 failed 任务执行 retry（原记录不变） |

### 重试

- 仅 `failed` 任务可重试；成功后记录回到 `queued`，`result` 与 `error` 均为 null，退出码为 0。
- retry 只修改任务状态，不读取文件也不校验 CSV：即使原文件缺失或内容仍错误也能入队。
- 沿用原 id、任务类型与输入路径，不新增任务记录，也不立即执行；随后的 run 按原规则执行，
  再次失败后仍可继续 retry。
- id 不存在时返回 `job_not_found`（退出码 1，不创建记录）；对 `queued` 或 `succeeded`
  任务重试时返回 `invalid_state` 与原状态/结果（退出码 1，原记录不变）。

### 取消

- 仅 `queued` 任务可取消；成功后同一条记录变为 `cancelled`，`result` 与 `error` 均为 null，
  退出码为 0。沿用原 id、任务类型与输入路径，不新增或删除任务记录，也不执行汇总。
- cancel 只修改任务状态，不读取文件也不校验 CSV：即使提交后文件被删除或内容变为非法
  也能取消；`failed` 任务经 retry 回到 `queued` 后同样适用。
- `cancelled` 任务不能通过 run 执行，也不能通过 retry 恢复排队，两种操作均返回
  `invalid_state` 与 cancelled 状态（退出码 1，原记录不变）；show 仍正常返回该记录。
- id 不存在时返回 `job_not_found`（退出码 1，不创建记录）；对 `failed`、`succeeded`
  或已 `cancelled` 任务取消时返回 `invalid_state` 与当前状态/结果（退出码 1，
  数据库中的状态、结果及原错误值均不变）。

### 演示

`demo/sales.csv` 内容为 `a,10`、`b,20`、`a,5`，执行后结果为
`{"row_count": 3, "total_amount": 35, "categories": {"a": 15, "b": 20}}`。

### 回归测试

`tests/test_retry_regression.py` 覆盖失败任务显式重试（retry）的公开行为，
`tests/test_cancel_regression.py` 覆盖排队任务取消（cancel）的公开行为，
`tests/test_request_key_regression.py` 覆盖提交去重（--request-key）的公开行为，
仅需 Python 3 标准库，在项目根目录运行：

```bash
python -m unittest tests.test_retry_regression tests.test_cancel_regression tests.test_request_key_regression -v
```

测试通过 `python -m task_center` 子进程观察 JSON 输出与退出码。演示数据由测试
自行准备：`demo/` 目录不存在时自动创建，并在 `demo/` 下创建专用文件
`retry_regression_case.csv`（先写入非法内容
`a,x` 制造 `data_error` 失败任务，再改写为 `a,10`、`b,20`、`a,5` 验证成功路径）与
`cancel_regression_case.csv`（`a,10`，并在取消前删除或改写为非法内容验证 cancel
不读取文件）与 `request_key_regression_case.csv`（`a,10`、`b,20`、`a,5`，验证同键
去重、冲突与键合法性），并通过 `--db` 使用临时目录下的独立 SQLite 数据库。同名样例若已存在，
对应模块直接失败且不改动该文件。测试不依赖预置
任务或外部服务，结束后仅删除自建文件与临时库；`demo/` 由测试创建且已为空时才一并
移除，原本存在的 `demo/` 及其中其他文件保持原样，可连续重复运行。
