# 轻量批处理任务中心

用于本地重复性工作的批处理任务产品。当前版本提供**内置 `csv_summary` 任务**的提交、手动执行与查询，基于 Python 3 标准库与单机 SQLite，无需常驻进程。

## 环境要求

- Python 3.10+（仅使用标准库）
- 无需安装第三方依赖

## 快速开始

在项目目录下执行：

```sh
# 1. 提交任务（只登记，不执行），返回唯一 id 和 queued 状态
python -m task_center submit csv_summary --input demo/sales.csv
# {"id": 1, "status": "queued", "result": null, "error": null}

# 2. 手动执行指定任务（一次只处理该任务）
python -m task_center run 1
# {"id": 1, "status": "succeeded",
#  "result": {"row_count": 3, "total_amount": 35, "categories": {"a": 15, "b": 20}},
#  "error": null}

# 3. 查询已保存的记录（进程退出后仍可查询）
python -m task_center show 1
```

每条命令输出一个 JSON 对象，字段为 `id`、`status`、`result`、`error`：

- 成功退出码为 `0`；操作被拒绝或任务执行失败时退出码为 `1`，`error` 填写错误码。
- 任务未成功时 `result` 为 `null`；无错误时 `error` 为 `null`。

## 数据库

- 默认使用项目目录下的 `tasks.sqlite3`，首次运行自动创建。
- 可用 `--db` 指定其他数据库文件，位置在子命令前后均可：

```sh
python -m task_center --db /tmp/tasks.sqlite3 submit csv_summary --input demo/sales.csv
python -m task_center show 1 --db /tmp/tasks.sqlite3
```

## csv_summary 任务规则

输入文件要求：

- `--input` 路径相对于项目目录解释，**只接受 `demo` 目录内的普通文件**（符号链接指向目录外同样拒绝）。
- 文件采用 UTF-8 编码，首行必须为表头 `category,amount`。
- 完全空行忽略；其余每行恰有两列，以逗号分隔。
- `category` 去除首尾空格后不能为空，区分大小写。
- `amount` 去除首尾空格后匹配 `[0-9]+`，按非负整数汇总。

成功结果包含：

- `row_count`：有效数据行数
- `total_amount`：金额总和
- `categories`：类别到金额的映射

随仓库附带 `demo/sales.csv`（`a,10`、`b,20`、`a,5`），结果为 3 行、总额 35，其中 `a` 为 15、`b` 为 20。只有表头的文件返回 0 行、0 总额与空映射。

提交时只检查任务类型与输入路径，**文件内容在执行时才校验**。

## 任务状态与错误码

任务执行后转为 `succeeded` 或 `failed`，失败不保留部分结果；`run` 一次只处理指定任务，执行后原记录更新为最终状态。

| 错误码 | 含义 |
| --- | --- |
| `invalid_type` | 提交了未知的任务类型 |
| `invalid_input` | 提交时路径位于 `demo` 目录外、不是普通文件或文件不存在，不创建任务 |
| `input_error` | 执行时输入文件不可读（如提交后被删除），任务记为 `failed` |
| `data_error` | 执行时内容不合法（编码、表头、列数或字段问题），任务记为 `failed` |
| `job_not_found` | 指定的任务 id 不存在 |
| `invalid_state` | `run` 的任务不是 `queued` 状态，原记录保持不变 |

## 后续规划

任务重试与取消、结果留存策略、依赖顺序、简单定时计划和执行记录。
