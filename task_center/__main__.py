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


def _rejection_record(conn, job_id, exc):
    """把业务异常统一转换为拒绝响应记录（不写数据库）。

    默认回显传入 id，status/result 为 null，error 为异常错误码；
    invalid_state 表示任务存在但当前状态不允许该操作，此时附带已
    保存记录的 status/result，仅把本次响应的 error 改为
    invalid_state，便于调用方了解状态。若任务此时查不到（如
    job_not_found），则保留默认响应。
    """
    record = {"id": job_id, "status": None, "result": None, "error": exc.code}
    if exc.code == core.ERR_INVALID_STATE:
        try:
            current = core.show(conn, job_id)
        except core.TaskError:
            pass
        else:
            current["error"] = exc.code
            record = current
    return record


def _exit_code_on_success(record):
    """run 成功返回时：仅 succeeded 退出码为 0。"""
    return 0 if record["status"] == core.STATUS_SUCCEEDED else 1


def _dispatch(conn, action, job_id, success_exit_code):
    """执行 run/retry/cancel 这类按 id 的操作，统一处理拒绝响应。

    成功时由 success_exit_code 依据返回记录决定退出码；业务异常
    一律转换为退出码 1 的拒绝响应。返回 (记录, 退出码)。
    """
    try:
        record = action(conn, job_id)
    except core.TaskError as exc:
        return _rejection_record(conn, job_id, exc), 1
    return record, success_exit_code(record)


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = core.connect(args.db)
    try:
        if args.command == "submit":
            record = core.submit(conn, args.type, args.input)
            exit_code = 0
        elif args.command == "run":
            record, exit_code = _dispatch(
                conn, core.run, args.id, _exit_code_on_success
            )
        elif args.command == "retry":
            record, exit_code = _dispatch(conn, core.retry, args.id, lambda _r: 0)
        elif args.command == "cancel":
            record, exit_code = _dispatch(conn, core.cancel, args.id, lambda _r: 0)
        else:  # show
            record = core.show(conn, args.id)
            exit_code = 0
    except core.TaskError as exc:
        # submit 的参数没有 id（getattr 回退为 None）；show 等命令则回显传入 id。
        record = _rejection_record(conn, getattr(args, "id", None), exc)
        exit_code = 1
    finally:
        conn.close()

    print(json.dumps(record, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
