"""SQLite 任务存储：任务记录的创建、查询与状态更新。"""

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    input      TEXT NOT NULL,
    status     TEXT NOT NULL,
    result     TEXT,
    error      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


class JobNotFound(Exception):
    """指定 id 的任务不存在。"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def create_task(conn: sqlite3.Connection, task_type: str, input_path: str) -> int:
    """登记一个 queued 任务，返回唯一 id。"""
    cur = conn.execute(
        "INSERT INTO tasks (type, input, status) VALUES (?, ?, 'queued')",
        (task_type, input_path),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise JobNotFound(task_id)
    return row


def finish_task(
    conn: sqlite3.Connection,
    task_id: int,
    status: str,
    result: object | None,
    error: str | None,
) -> None:
    """将任务置为最终状态（succeeded/failed），结果整体写入，不保留部分结果。"""
    conn.execute(
        """
        UPDATE tasks
           SET status = ?,
               result = ?,
               error = ?,
               updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
         WHERE id = ?
        """,
        (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
         error, task_id),
    )
    conn.commit()
