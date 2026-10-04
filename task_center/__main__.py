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
    p_submit.add_argument(
        "--request-key",
        default=None,
        help="可选请求键（1-64 个 ASCII 字母/数字/下划线/连字符）："
        "同一数据库内重复提交同一请求时返回原任务",
    )

    p_run = sub.add_parser("run", help="手动执行一个 queued 任务")
    p_run.add_argument("id", help="任务 id")

    p_show = sub.add_parser("show", help="查询任务记录")
    p_show.add_argument("id", nargs="?", default=None, help="任务 id")
    p_show.add_argument(
        "--request-key",
        default=None,
        help="按请求键查询已绑定的任务（与 id 二选一）",
    )

    p_retry = sub.add_parser("retry", help="让 failed 任务重新入队（不立即执行）")
    p_retry.add_argument("id", nargs="?", default=None, help="任务 id")
    p_retry.add_argument(
        "--request-key",
        default=None,
        help="按请求键重试已绑定的任务（与 id 二选一）",
    )

    p_cancel = sub.add_parser("cancel", help="取消一个 queued 任务（保留记录，不再执行）")
    p_cancel.add_argument("id", help="任务 id")

    # 供 main 在 id 与 --request-key 组合非法时按参数解析失败的方式报错。
    parser.show_parser = p_show
    parser.retry_parser = p_retry

    return parser


def _rejection_record(conn, job_id, exc, lookup=None):
    """把业务异常统一转换为拒绝响应记录（不写数据库）。

    默认回显传入 id，status/result 为 null，error 为异常错误码；
    invalid_state 表示任务存在但当前状态不允许该操作，此时附带已
    保存记录的 status/result，仅把本次响应的 error 改为
    invalid_state，便于调用方了解状态。若任务此时查不到（如
    job_not_found），则保留默认响应。lookup 指定 invalid_state 时
    重新读取记录的方式，默认按 id 查询；按请求键定位的操作传入按
    键查询，且 job_id 为 None（拒绝响应的 id 为 null）。
    """
    record = {"id": job_id, "status": None, "result": None, "error": exc.code}
    if exc.code == core.ERR_INVALID_STATE:
        if lookup is None:
            lookup = lambda: core.show(conn, job_id)
        try:
            current = lookup()
        except core.TaskError:
            pass
        else:
            current["error"] = exc.code
            record = current
    return record


def _exit_code_on_success(record):
    """run 成功返回时：仅 succeeded 退出码为 0。"""
    return 0 if record["status"] == core.STATUS_SUCCEEDED else 1


def _dispatch(conn, action, job_id, success_exit_code, lookup=None):
    """执行 run/retry/cancel 这类按 id 的操作，统一处理拒绝响应。

    成功时由 success_exit_code 依据返回记录决定退出码；业务异常
    一律转换为退出码 1 的拒绝响应。返回 (记录, 退出码)。
    """
    try:
        record = action(conn, job_id)
    except core.TaskError as exc:
        return _rejection_record(conn, job_id, exc, lookup=lookup), 1
    return record, success_exit_code(record)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "show" and (args.id is None) == (args.request_key is None):
        # id 与 --request-key 必须且只能提供一个：按参数解析失败处理，
        # 用法提示写标准错误、退出码 2，标准输出不输出 JSON。
        parser.show_parser.error("show 需要且只能提供 id 或 --request-key 之一")
    if args.command == "retry" and (args.id is None) == (args.request_key is None):
        # 与 show 同一规则：参数解析失败，退出码 2，不连接（也不创建）数据库。
        parser.retry_parser.error("retry 需要且只能提供 id 或 --request-key 之一")
    conn = core.connect(args.db)
    try:
        if args.command == "submit":
            record = core.submit(
                conn, args.type, args.input, request_key=args.request_key
            )
            exit_code = 0
        elif args.command == "run":
            record, exit_code = _dispatch(
                conn, core.run, args.id, _exit_code_on_success
            )
        elif args.command == "retry":
            if args.request_key is not None:
                record, exit_code = _dispatch(
                    conn,
                    lambda conn, _id: core.retry_by_request_key(
                        conn, args.request_key
                    ),
                    None,
                    lambda _r: 0,
                    lookup=lambda: core.show_by_request_key(
                        conn, args.request_key
                    ),
                )
            else:
                record, exit_code = _dispatch(conn, core.retry, args.id, lambda _r: 0)
        elif args.command == "cancel":
            record, exit_code = _dispatch(conn, core.cancel, args.id, lambda _r: 0)
        else:  # show
            if args.request_key is not None:
                record = core.show_by_request_key(conn, args.request_key)
            else:
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
