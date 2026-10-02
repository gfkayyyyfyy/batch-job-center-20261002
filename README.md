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

# 手动执行一个 queued 任务，返回最终记录
python -m task_center run <id>

# 查询已保存的任务记录（退出后仍可查询）
python -m task_center show <id>

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

### 错误码

| 错误码 | 含义 |
| --- | --- |
| `invalid_type` | 未知任务类型 |
| `invalid_input` | 提交时路径在 demo 目录外、非普通文件或文件不存在（不创建任务） |
| `input_error` | 执行时文件不可读 |
| `data_error` | 执行时编码、表头、列数或字段不合法 |
| `job_not_found` | 任务 id 不存在 |
| `invalid_state` | 对非 queued 任务执行 run（原记录不变） |

### 演示

`demo/sales.csv` 内容为 `a,10`、`b,20`、`a,5`，执行后结果为
`{"row_count": 3, "total_amount": 35, "categories": {"a": 15, "b": 20}}`。
