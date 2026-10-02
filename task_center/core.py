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
ERR_INPUT = "input_error"
ERR_DATA = "data_error"
ERR_JOB_NOT_FOUND = "job_not_found"
ERR_INVALID_STATE = "invalid_state"

_AMOUNT_RE = re.compile(r"[0-9]+")

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


class TaskError(Exception):
    """带错误码的业务异常。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def connect(db_path):
    """打开（必要时创建）数据库并初始化表结构。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(_SCHEMA)
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


def submit(conn, task_type, input_path):
    """登记任务，仅校验类型与路径，不校验文件内容。"""
    if task_type not in TASK_TYPES:
        raise TaskError(ERR_INVALID_TYPE)
    _resolve_input(input_path)
    job_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO jobs (id, type, input, status, result, error)"
        " VALUES (?, ?, ?, ?, NULL, NULL)",
        (job_id, task_type, input_path, STATUS_QUEUED),
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
    """取消 queued 任务；仅改状态，不读取或校验输入文件、不执行汇总。"""
    row = _get_job(conn, job_id)
    _id, _task_type, _input, status, _result, _error = row
    if status != STATUS_QUEUED:
        raise TaskError(ERR_INVALID_STATE)
    conn.execute(
        "UPDATE jobs SET status = ? WHERE id = ?",
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
