"""命令行入口：python -m task_center [--db PATH] <submit|run|show|retry|cancel> ..."""

import argparse
import json
import sys

from . import core


def build_parser():
    parser = argparse.ArgumentParser(
        prog="task_center",
        description="轻量批处理任务中心：提交、手动执行与查询任务。",
    )
    parser.add_argument(
        "--db",
        default=str(core.DEFAULT_DB),
        help="SQLite 数据库文件路径（默认：项目目录下 tasks.sqlite3）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="登记任务（不执行）")
    p_submit.add_argument("type", help="任务类型，目前支持 csv_summary")
    p_submit.add_argument("--input", required=True, help="相对项目目录的输入文件（限 demo 目录内）")

    p_run = sub.add_parser("run", help="手动执行一个 queued 任务")
    p_run.add_argument("id", help="任务 id")

    p_show = sub.add_parser("show", help="查询任务记录")
    p_show.add_argument("id", help="任务 id")

    p_retry = sub.add_parser("retry", help="让 failed 任务重新入队（不立即执行）")
    p_retry.add_argument("id", help="任务 id")

    p_cancel = sub.add_parser("cancel", help="取消一个 queued 任务（保留记录，不再执行）")
    p_cancel.add_argument("id", help="任务 id")

    return parser


# run/retry/cancel 与核心操作的对应关系：三者共用同一套拒绝响应转换。
_STATE_CHANGE_ACTIONS = {
    "run": core.run,
    "retry": core.retry,
    "cancel": core.cancel,
}


def _rejection_record(conn, job_id, exc):
    """把核心操作抛出的业务异常转换为拒绝响应记录。

    invalid_state 时任务存在，响应保留数据库当前记录的 id、status、result，
    仅把本次响应的 error 改为 invalid_state（不写回数据库）；其余错误码
    （如 job_not_found）保留传入 id，status、result 为 null。
    """
    record = {"id": job_id, "status": None, "result": None, "error": exc.code}
    if exc.code == core.ERR_INVALID_STATE:
        # invalid_state 时展示当前记录，便于调用方了解状态
        try:
            current = core.show(conn, job_id)
            current["error"] = exc.code
            record = current
        except core.TaskError:
            pass
    return record


def _dispatch_state_change(conn, command, job_id):
    """执行 run/retry/cancel，返回 (响应记录, 退出码)。

    成功时沿用核心操作返回的记录；被拒绝时统一转换业务错误。
    run 以最终状态决定退出码（succeeded 为 0），retry/cancel 成功即 0。
    """
    try:
        record = _STATE_CHANGE_ACTIONS[command](conn, job_id)
    except core.TaskError as exc:
        return _rejection_record(conn, job_id, exc), 1
    if command == "run":
        exit_code = 0 if record["status"] == core.STATUS_SUCCEEDED else 1
    else:
        exit_code = 0
    return record, exit_code


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = core.connect(args.db)
    try:
        if args.command == "submit":
            record = core.submit(conn, args.type, args.input)
            exit_code = 0
        elif args.command == "show":
            record = core.show(conn, args.id)
            exit_code = 0
        else:  # run / retry / cancel：共用同一套拒绝响应转换
            record, exit_code = _dispatch_state_change(conn, args.command, args.id)
    except core.TaskError as exc:
        record = {"id": getattr(args, "id", None), "status": None, "result": None, "error": exc.code}
        exit_code = 1
    finally:
        conn.close()

    print(json.dumps(record, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
