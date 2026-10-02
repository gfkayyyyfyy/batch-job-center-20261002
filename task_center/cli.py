"""命令行入口：submit / run / show 三个子命令。"""

import argparse
import json
import sys
from pathlib import Path

from . import csv_summary, storage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "tasks.sqlite3"

# 子解析器上的 --db 不给默认值：否则即便 --db 写在子命令之前，
# 也会被子解析器的默认值 None 覆盖。

# 已支持的任务类型：提交时做路径校验，执行时做内容处理。
TASK_TYPES = {
    "csv_summary": {
        "validate_input": csv_summary.resolve_input,
        "execute": csv_summary.run_csv_summary,
    },
}


def emit(payload: dict, ok: bool) -> int:
    """每个命令输出一个 JSON；成功退出码 0，拒绝或失败退出码 1。"""
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 1


def record(row) -> dict:
    return {
        "id": row["id"],
        "status": row["status"],
        "result": json.loads(row["result"]) if row["result"] is not None else None,
        "error": row["error"],
    }


def rejection(error_code: str) -> int:
    return emit({"id": None, "status": None, "result": None, "error": error_code}, False)


def cmd_submit(args, db_path: Path) -> int:
    spec = TASK_TYPES.get(args.type)
    if spec is None:
        return rejection("invalid_type")

    # 提交仅检查类型和路径，不读取文件内容。
    try:
        spec["validate_input"](PROJECT_ROOT, args.input)
    except ValueError:
        return rejection("invalid_input")

    with storage.connect(db_path) as conn:
        task_id = storage.create_task(conn, args.type, args.input)
        row = storage.get_task(conn, task_id)
        return emit(record(row), True)


def cmd_run(args, db_path: Path) -> int:
    try:
        task_id = int(args.id)
    except (TypeError, ValueError):
        return rejection("job_not_found")

    with storage.connect(db_path) as conn:
        try:
            row = storage.get_task(conn, task_id)
        except storage.JobNotFound:
            return rejection("job_not_found")

        if row["status"] != "queued":
            # 非 queued 任务拒绝执行，原记录保持不变。
            payload = record(row)
            payload["error"] = "invalid_state"
            return emit(payload, False)

        spec = TASK_TYPES.get(row["type"])
        try:
            # 执行时重新检查路径：文件在提交后被删除或替换为非普通文件，
            # 按输入不可读处理。
            path = spec["validate_input"](PROJECT_ROOT, row["input"])
            result = spec["execute"](path)
        except ValueError:
            storage.finish_task(conn, task_id, "failed", None, "input_error")
            return emit(record(storage.get_task(conn, task_id)), False)
        except csv_summary.TaskInputError:
            storage.finish_task(conn, task_id, "failed", None, "input_error")
            return emit(record(storage.get_task(conn, task_id)), False)
        except csv_summary.TaskDataError:
            storage.finish_task(conn, task_id, "failed", None, "data_error")
            return emit(record(storage.get_task(conn, task_id)), False)

        storage.finish_task(conn, task_id, "succeeded", result, None)
        return emit(record(storage.get_task(conn, task_id)), True)


def cmd_show(args, db_path: Path) -> int:
    try:
        task_id = int(args.id)
    except (TypeError, ValueError):
        return rejection("job_not_found")

    with storage.connect(db_path) as conn:
        try:
            row = storage.get_task(conn, task_id)
        except storage.JobNotFound:
            return rejection("job_not_found")
        return emit(record(row), True)


def build_parser() -> argparse.ArgumentParser:
    # --db 同时挂在主解析器和各子解析器上，放在子命令前后都可用。
    parser = argparse.ArgumentParser(
        prog="python -m task_center",
        description="轻量批处理任务中心：提交、手动执行、查询本地任务。",
    )
    parser.add_argument("--db", default=None, help="SQLite 数据库文件（默认项目目录 tasks.sqlite3）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 子解析器的 --db 使用 SUPPRESS：未显式给出时不覆盖主解析器已解析的值。
    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_submit = subparsers.add_parser("submit", parents=[sub_common], help="提交一个任务（不执行）")
    p_submit.add_argument("type", help="任务类型，目前支持 csv_summary")
    p_submit.add_argument("--input", required=True, help="输入文件路径（相对于项目目录，限 demo 目录内）")

    p_run = subparsers.add_parser("run", parents=[sub_common], help="手动执行指定任务一次")
    p_run.add_argument("id", help="submit 返回的任务 id")

    p_show = subparsers.add_parser("show", parents=[sub_common], help="查询已保存的任务记录")
    p_show.add_argument("id", help="任务 id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db) if getattr(args, "db", None) else DEFAULT_DB

    if args.command == "submit":
        return cmd_submit(args, db_path)
    if args.command == "run":
        return cmd_run(args, db_path)
    if args.command == "show":
        return cmd_show(args, db_path)
    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover - argparse 会在 error 时直接退出


if __name__ == "__main__":
    sys.exit(main())
