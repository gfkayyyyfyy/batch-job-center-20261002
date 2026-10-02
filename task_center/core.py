"""任务中心核心逻辑：SQLite 存储与 csv_summary 任务执行。"""

import csv
import io
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "tasks.sqlite3"
DEMO_DIR = PROJECT_ROOT / "demo"

TASK_TYPES = {"csv_summary"}

STATUS_QUEUED = "queued"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ERR_INVALID_TYPE = "invalid_type"
ERR_INVALID_INPUT = "invalid_input"
ERR_INVALID_REQUEST_KEY = "invalid_request_key"
ERR_REQUEST_CONFLICT = "request_conflict"
ERR_INPUT = "input_error"
ERR_DATA = "data_error"
ERR_JOB_NOT_FOUND = "job_not_found"
ERR_INVALID_STATE = "invalid_state"

_AMOUNT_RE = re.compile(r"[0-9]+")

# 请求键：1-64 个 ASCII 字母、数字、下划线或连字符，区分大小写，不裁剪空白。
_REQUEST_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    input TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT
)
"""

# 请求键与任务的绑定，跨进程持久保留；run/retry/cancel 均不改动该表。
# 旧任务没有绑定行，因此不参与请求键匹配。
_REQUEST_KEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_keys (
    request_key TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    input TEXT NOT NULL
)
"""


class TaskError(Exception):
    """带错误码的业务异常。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def connect(db_path):
    """打开（必要时创建）数据库并初始化表结构。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(_SCHEMA)
    conn.execute(_REQUEST_KEY_SCHEMA)
    conn.commit()
    return conn


def _resolve_input(input_path):
    """把相对于项目目录的输入路径解析为 demo 目录内的普通文件。"""
    candidate = Path(input_path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    real = Path(os.path.realpath(candidate))
    demo_real = Path(os.path.realpath(DEMO_DIR))
    if demo_real not in real.parents:
        raise TaskError(ERR_INVALID_INPUT)
    if not real.is_file():
        raise TaskError(ERR_INVALID_INPUT)
    return real


def _record(job_id, status, result, error):
    return {"id": job_id, "status": status, "result": result, "error": error}


def _row_to_record(row):
    job_id, _type, _input, status, result, error = row
    return _record(job_id, status, json.loads(result) if result else None, error)


def submit(conn, task_type, input_path, request_key=None):
    """登记任务，仅校验类型与路径，不校验文件内容。

    提供 request_key 时按键实现幂等：同一数据库内合法键首次提交照常
    登记并建立绑定；已有绑定时按任务类型与原始输入路径字符串精确匹配，
    匹配则返回该任务当前保存的完整记录（不检查文件、不改变状态），
    不匹配则抛出 request_conflict。校验顺序为任务类型、请求键、
    （已有绑定的）类型/路径一致性，最后才检查本次输入路径。
    """
    if task_type not in TASK_TYPES:
        raise TaskError(ERR_INVALID_TYPE)
    if request_key is not None and not _REQUEST_KEY_RE.fullmatch(request_key):
        raise TaskError(ERR_INVALID_REQUEST_KEY)

    if request_key is not None:
        binding = conn.execute(
            "SELECT job_id, task_type, input FROM request_keys"
            " WHERE request_key = ?",
            (request_key,),
        ).fetchone()
        if binding is not None:
            bound_job_id, bound_type, bound_input = binding
            if bound_type != task_type or bound_input != input_path:
                raise TaskError(ERR_REQUEST_CONFLICT)
            return _row_to_record(_get_job(conn, bound_job_id))

    _resolve_input(input_path)
    job_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO jobs (id, type, input, status, result, error)"
        " VALUES (?, ?, ?, ?, NULL, NULL)",
        (job_id, task_type, input_path, STATUS_QUEUED),
    )
    if request_key is not None:
        conn.execute(
            "INSERT INTO request_keys (request_key, job_id, task_type, input)"
            " VALUES (?, ?, ?, ?)",
            (request_key, job_id, task_type, input_path),
        )
    conn.commit()
    return _record(job_id, STATUS_QUEUED, None, None)


def _get_job(conn, job_id):
    row = conn.execute(
        "SELECT id, type, input, status, result, error FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise TaskError(ERR_JOB_NOT_FOUND)
    return row


def show(conn, job_id):
    """查询已保存的任务记录。"""
    return _row_to_record(_get_job(conn, job_id))


def retry(conn, job_id):
    """让 failed 任务重新入队；仅改状态，不读取或校验输入文件。"""
    row = _get_job(conn, job_id)
    _id, _task_type, _input, status, _result, _error = row
    if status != STATUS_FAILED:
        raise TaskError(ERR_INVALID_STATE)
    conn.execute(
        "UPDATE jobs SET status = ?, result = NULL, error = NULL WHERE id = ?",
        (STATUS_QUEUED, job_id),
    )
    conn.commit()
    return _record(job_id, STATUS_QUEUED, None, None)


def cancel(conn, job_id):
    """取消 queued 任务：仅把状态改为 cancelled，不读取或校验输入文件。"""
    row = _get_job(conn, job_id)
    _id, _task_type, _input, status, _result, _error = row
    if status != STATUS_QUEUED:
        raise TaskError(ERR_INVALID_STATE)
    conn.execute(
        "UPDATE jobs SET status = ?, result = NULL, error = NULL WHERE id = ?",
        (STATUS_CANCELLED, job_id),
    )
    conn.commit()
    return _record(job_id, STATUS_CANCELLED, None, None)


def _run_csv_summary(real_path):
    """执行汇总，返回结果 dict；失败抛出 TaskError。"""
    try:
        with open(real_path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    except UnicodeDecodeError:
        raise TaskError(ERR_DATA)
    except OSError:
        raise TaskError(ERR_INPUT)

    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if not rows:
        raise TaskError(ERR_DATA)
    if [cell.strip() for cell in rows[0]] != ["category", "amount"]:
        raise TaskError(ERR_DATA)

    row_count = 0
    total_amount = 0
    categories = {}
    for row in rows[1:]:
        if len(row) != 2:
            raise TaskError(ERR_DATA)
        category = row[0].strip()
        amount = row[1].strip()
        if not category or not _AMOUNT_RE.fullmatch(amount):
            raise TaskError(ERR_DATA)
        value = int(amount)
        row_count += 1
        total_amount += value
        categories[category] = categories.get(category, 0) + value

    return {
        "row_count": row_count,
        "total_amount": total_amount,
        "categories": categories,
    }


def run(conn, job_id):
    """手动执行一个 queued 任务，返回最终记录。"""
    row = _get_job(conn, job_id)
    _id, task_type, input_path, status, _result, _error = row
    if status != STATUS_QUEUED:
        raise TaskError(ERR_INVALID_STATE)

    try:
        real_path = _resolve_input(input_path)
        result = _run_csv_summary(real_path)
    except TaskError as exc:
        code = ERR_INPUT if exc.code == ERR_INVALID_INPUT else exc.code
        conn.execute(
            "UPDATE jobs SET status = ?, result = NULL, error = ? WHERE id = ?",
            (STATUS_FAILED, code, job_id),
        )
        conn.commit()
        return _record(job_id, STATUS_FAILED, None, code)

    conn.execute(
        "UPDATE jobs SET status = ?, result = ?, error = NULL WHERE id = ?",
        (STATUS_SUCCEEDED, json.dumps(result, ensure_ascii=False), job_id),
    )
    conn.commit()
    return _record(job_id, STATUS_SUCCEEDED, result, None)
