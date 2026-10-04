"""任务中心核心逻辑：SQLite 存储与 csv_summary 任务执行。"""

import csv
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

# 请求键：1 至 64 个 ASCII 字母、数字、下划线或连字符，区分大小写，不裁剪空白。
_REQUEST_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

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

# 请求键绑定列：旧库经 ALTER TABLE 补齐，旧记录该列为 NULL，不参与键匹配。
_REQUEST_KEY_COLUMN = "ALTER TABLE jobs ADD COLUMN request_key TEXT"

# 同一数据库内请求键唯一；NULL（未提供键的任务）在 SQLite 唯一索引中互不相等。
_REQUEST_KEY_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_request_key"
    " ON jobs (request_key)"
)


class TaskError(Exception):
    """带错误码的业务异常。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def connect(db_path):
    """打开（必要时创建）数据库并初始化表结构。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "request_key" not in columns:
        conn.execute(_REQUEST_KEY_COLUMN)
    conn.execute(_REQUEST_KEY_INDEX)
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

    提供 request_key 时按同一数据库内的键识别重复请求：已有绑定且
    任务类型与输入路径原字符串都相同时，直接返回该任务当前保存的
    完整记录（不新增任务、不改变状态、不再检查文件）；类型或路径
    不匹配时拒绝（request_conflict，不检查新路径）。首次使用有效
    键时按原规则校验路径并登记 queued 任务；路径不合法则不创建
    任务也不占用该键。不提供键时每次登记都创建新任务。
    """
    if task_type not in TASK_TYPES:
        raise TaskError(ERR_INVALID_TYPE)
    if request_key is not None:
        if not _REQUEST_KEY_RE.fullmatch(request_key):
            raise TaskError(ERR_INVALID_REQUEST_KEY)
        row = conn.execute(
            "SELECT id, type, input, status, result, error FROM jobs"
            " WHERE request_key = ?",
            (request_key,),
        ).fetchone()
        if row is not None:
            _id, bound_type, bound_input, _status, _result, _error = row
            if bound_type == task_type and bound_input == input_path:
                return _row_to_record(row)
            raise TaskError(ERR_REQUEST_CONFLICT)
    _resolve_input(input_path)
    job_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO jobs (id, type, input, status, result, error, request_key)"
        " VALUES (?, ?, ?, ?, NULL, NULL, ?)",
        (job_id, task_type, input_path, STATUS_QUEUED, request_key),
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


def _get_job_by_request_key(conn, request_key):
    """按请求键查找绑定任务：先校验格式再查绑定。

    键格式与提交时同一规则，非法键抛 invalid_request_key；合法但未
    绑定的键抛 job_not_found。
    """
    if not _REQUEST_KEY_RE.fullmatch(request_key):
        raise TaskError(ERR_INVALID_REQUEST_KEY)
    row = conn.execute(
        "SELECT id, type, input, status, result, error FROM jobs"
        " WHERE request_key = ?",
        (request_key,),
    ).fetchone()
    if row is None:
        raise TaskError(ERR_JOB_NOT_FOUND)
    return row


def show_by_request_key(conn, request_key):
    """按请求键查询已绑定的任务记录。

    只读取持久化记录：不执行任务、不读取或校验输入文件，不改变状态、
    结果、错误或键绑定。
    """
    return _row_to_record(_get_job_by_request_key(conn, request_key))


def _requeue_failed(conn, row):
    """重试规则：仅 failed 任务可重新入队，两种定位入口共用。

    仅改状态并清空 result/error，沿用原 id，不新增任务、不解除键
    绑定、不读取或校验输入文件；非 failed 时抛 invalid_state，
    任务记录不变。
    """
    job_id, _task_type, _input, status, _result, _error = row
    if status != STATUS_FAILED:
        raise TaskError(ERR_INVALID_STATE)
    conn.execute(
        "UPDATE jobs SET status = ?, result = NULL, error = NULL WHERE id = ?",
        (STATUS_QUEUED, job_id),
    )
    conn.commit()
    return _record(job_id, STATUS_QUEUED, None, None)


def retry(conn, job_id):
    """让 failed 任务重新入队；仅改状态，不读取或校验输入文件。"""
    return _requeue_failed(conn, _get_job(conn, job_id))


def retry_by_request_key(conn, request_key):
    """按请求键让绑定的 failed 任务重新入队；仅改状态，不读取或校验输入文件。

    定位规则与按键查询一致，重试规则与按 id 重试一致：成功时不新增
    任务、不解除绑定，返回原 id 的 queued 记录。
    """
    return _requeue_failed(conn, _get_job_by_request_key(conn, request_key))


def _cancel_queued(conn, row):
    """取消规则：仅 queued 任务可取消，两种定位入口共用。

    仅把状态改为 cancelled 并清空 result/error，沿用原 id，不新增任务、
    不解除键绑定、不读取或校验输入文件；非 queued 时抛 invalid_state，
    任务记录不变。
    """
    job_id, _task_type, _input, status, _result, _error = row
    if status != STATUS_QUEUED:
        raise TaskError(ERR_INVALID_STATE)
    conn.execute(
        "UPDATE jobs SET status = ?, result = NULL, error = NULL WHERE id = ?",
        (STATUS_CANCELLED, job_id),
    )
    conn.commit()
    return _record(job_id, STATUS_CANCELLED, None, None)


def cancel(conn, job_id):
    """取消 queued 任务：仅把状态改为 cancelled，不读取或校验输入文件。"""
    return _cancel_queued(conn, _get_job(conn, job_id))


def cancel_by_request_key(conn, request_key):
    """按请求键取消绑定的 queued 任务；仅改状态，不读取或校验输入文件。

    定位规则与按键查询一致，取消规则与按 id 取消一致：成功时不新增
    任务、不解除绑定，返回原 id 的 cancelled 记录。
    """
    return _cancel_queued(conn, _get_job_by_request_key(conn, request_key))


def _run_csv_summary(real_path):
    """执行汇总，返回结果 dict；失败抛出 TaskError。

    流式逐行读取与解析：直接让 csv.reader 迭代文件对象（字段内含
    换行时由解析器自行继续取下一物理行），不保留整份文件文本，也
    不物化全部解析行。工作内存只随类别数量、当前记录大小以及文件
    对象的读取缓冲增长，与数据行数无关。
    """
    try:
        fh = open(real_path, "r", encoding="utf-8", newline="")
    except OSError:
        raise TaskError(ERR_INPUT)

    row_count = 0
    total_amount = 0
    categories = {}
    header_seen = False
    try:
        reader = csv.reader(fh)
        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except UnicodeDecodeError:
                # 非合法 UTF-8 属于数据问题；逐行解码下可能在读取
                # 到若干合法记录之后才暴露。
                raise TaskError(ERR_DATA)
            except csv.Error:
                # 解析器拒绝的数据（如字段超过默认长度上限）同样属于数据问题，
                # 归类为 data_error，不把底层异常文字暴露给调用方或写入记录。
                raise TaskError(ERR_DATA)
            except OSError:
                # 打开成功后逐行读取失败，与一次性读取失败同为输入问题。
                raise TaskError(ERR_INPUT)
            if not row:
                # 完全空行解析为空列表：一律忽略，不计入数据行；
                # 表头之前的空行同样跳过，故首个非空记录才是表头。
                continue
            if not header_seen:
                if [cell.strip() for cell in row] != ["category", "amount"]:
                    raise TaskError(ERR_DATA)
                header_seen = True
                continue
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
    finally:
        fh.close()

    if not header_seen:
        # 空文件或仅含完全空行：过滤后无任何记录。
        raise TaskError(ERR_DATA)

    return {
        "row_count": row_count,
        "total_amount": total_amount,
        "categories": categories,
    }


def _run_row(conn, row):
    """执行规则：仅 queued 任务可执行，两种定位入口共用。

    同步读取已保存的输入路径并持久化最终记录，沿用原 id，不新增
    任务、不解除键绑定；非 queued 时抛 invalid_state，不读取文件，
    状态、结果与错误均不变。成功时 result 为汇总结果、error 为
    null；文件缺失或不可读为 failed/input_error，CSV 不合法为
    failed/data_error，失败时 result 为 null。
    """
    job_id, _task_type, input_path, status, _result, _error = row
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


def run(conn, job_id):
    """手动执行一个 queued 任务，返回最终记录。"""
    return _run_row(conn, _get_job(conn, job_id))


def run_by_request_key(conn, request_key):
    """按请求键执行绑定的 queued 任务，返回最终记录。

    定位规则与按键查询一致，执行规则与按 id run 一致：成功或失败
    都沿用原 id 并持久化到同一条记录，不新增任务、不解除绑定；
    failed 任务仍需显式 retry 后才可再次执行。
    """
    return _run_row(conn, _get_job_by_request_key(conn, request_key))
