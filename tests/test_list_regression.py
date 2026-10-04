"""任务列表查询（list / list --status）的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，核对：

- list 返回外层四字段（id/status/error 为 null，result 为记录数组），
  数组按任务 id 字符串升序，每项与逐条 show 的四字段及持久化值一致，
  不含输入路径、类型或请求键；含 failed 任务时退出码仍为 0；
- 带请求键重复提交只产生一条任务，不同数据库互不混入；
- --status 精确筛选（区分大小写），空库或无匹配返回 [] 且退出码 0；
- 非法状态、选项缺值、多余位置参数按参数解析失败处理（退出码 2、
  用法写标准错误、标准输出为空、不创建尚不存在的数据库）；
- 查询不读取输入文件（文件删除后仍可列出）、不改变任何记录；
- 尚不存在的数据库沿用初始化行为返回空数组；
- 旧版六列数据库按现有兼容规则打开后原任务同样可列出。

每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_list_regression -v

或直接：

    python tests/test_list_regression.py
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = PROJECT_ROOT / "demo"

DEMO_FILE = DEMO_DIR / "list_regression_case.csv"
DEMO_INPUT = "demo/list_regression_case.csv"

VALID_CSV = "category,amount\na,10\nb,20\na,5\n"
VALID_RESULT = {
    "row_count": 3,
    "total_amount": 35,
    "categories": {"a": 15, "b": 20},
}

STATUSES = ("queued", "succeeded", "failed", "cancelled")
OUTER_NULL_FIELDS = {"id": None, "status": None, "error": None}


class ListRegressionTest(unittest.TestCase):
    """list 的公开行为：JSON 响应、退出码与保存的记录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_list_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        self._demo_dir_created = not DEMO_DIR.exists()
        DEMO_DIR.mkdir(exist_ok=True)
        if DEMO_FILE.exists():
            self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % DEMO_FILE)
        self.addCleanup(self._remove_demo_file)

    def _remove_demo_file(self):
        try:
            DEMO_FILE.unlink()
        except FileNotFoundError:
            pass
        if self._demo_dir_created:
            try:
                DEMO_DIR.rmdir()
            except OSError:
                pass

    # --- 辅助方法 -------------------------------------------------------

    def _write_demo(self, text):
        DEMO_FILE.write_text(text, encoding="utf-8")

    def _run_cli(self, *args, db=None):
        proc = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", db or self.db, *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _cli(self, *args, db=None):
        code, stdout, stderr = self._run_cli(*args, db=db)
        self.assertTrue(stdout.strip(), "CLI 应输出一行 JSON：%r" % stderr)
        self.assertEqual(len(stdout.strip().splitlines()), 1, stdout)
        return code, json.loads(stdout)

    def _list(self, *args, db=None):
        return self._cli("list", *args, db=db)

    def _submit(self, request_key):
        code, record = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT,
            "--request-key", request_key,
        )
        self.assertEqual(code, 0, record)
        return record["id"]

    def _assert_outer_envelope(self, response):
        self.assertIsInstance(response, dict)
        self.assertEqual(
            {k: response[k] for k in ("id", "status", "error")}, OUTER_NULL_FIELDS
        )
        self.assertIsInstance(response["result"], list)
        self.assertEqual(sorted(response), ["error", "id", "result", "status"])

    def _prepare_all_statuses(self):
        """准备 queued/succeeded/failed/cancelled 各一条，返回 id 与状态映射。"""
        self._write_demo(VALID_CSV)
        queued_id = self._submit("k-queued")

        succeeded_id = self._submit("k-succeeded")
        code, _ = self._cli("run", succeeded_id)
        self.assertEqual(code, 0)

        failed_id = self._submit("k-failed")
        # 改写为非法内容以制造 data_error。
        self._write_demo("category,amount\na,x\n")
        code, failed_rec = self._cli("run", failed_id)
        self.assertEqual(code, 1, failed_rec)
        self.assertEqual(failed_rec["error"], "data_error")

        cancelled_id = self._submit("k-cancelled")
        code, _ = self._cli("cancel", cancelled_id)
        self.assertEqual(code, 0)

        self._write_demo(VALID_CSV)
        return {
            queued_id: "queued",
            succeeded_id: "succeeded",
            failed_id: "failed",
            cancelled_id: "cancelled",
        }

    # --- 成功响应外层结构与排序 -------------------------------------------

    def test_list_empty_database_returns_empty_array(self):
        code, response = self._list()
        self.assertEqual(code, 0)
        self._assert_outer_envelope(response)
        self.assertEqual(response["result"], [])

    def test_list_returns_all_records_sorted_by_id_like_show(self):
        by_id = self._prepare_all_statuses()

        code, response = self._list()
        self.assertEqual(code, 0, response)
        self._assert_outer_envelope(response)
        records = response["result"]

        # 四种状态各一条，顺序按 id 字符串升序。
        self.assertEqual(len(records), 4)
        self.assertEqual([r["id"] for r in records], sorted(by_id))
        self.assertEqual({r["status"] for r in records}, set(STATUSES))

        for rec in records:
            # 每项恰好四个字段，不含输入路径、类型或请求键。
            self.assertEqual(sorted(rec), ["error", "id", "result", "status"])
            self.assertEqual(rec["status"], by_id[rec["id"]])
            # 与逐条 show 的内容完全一致。
            code, shown = self._cli("show", rec["id"])
            self.assertEqual(code, 0, shown)
            self.assertEqual(shown, rec)

        # 成功记录保留结果对象；失败记录 result 为 null、error 保留原码。
        by_status = {r["status"]: r for r in records}
        self.assertEqual(by_status["succeeded"]["result"], VALID_RESULT)
        self.assertEqual(by_status["succeeded"]["error"], None)
        self.assertEqual(by_status["failed"]["result"], None)
        self.assertEqual(by_status["failed"]["error"], "data_error")
        self.assertEqual(by_status["queued"]["result"], None)
        self.assertEqual(by_status["cancelled"]["result"], None)

    def test_list_with_failed_records_still_exits_zero(self):
        self._write_demo("category,amount\na,x\n")
        job_id = self._submit("k-fail")
        code, _ = self._cli("run", job_id)
        self.assertEqual(code, 1)
        code, response = self._list()
        self.assertEqual(code, 0, response)
        self.assertEqual(len(response["result"]), 1)

    # --- --status 精确筛选 -------------------------------------------------

    def test_list_status_filter_returns_only_matching_records(self):
        by_id = self._prepare_all_statuses()
        for status in STATUSES:
            with self.subTest(status=status):
                code, response = self._list("--status", status)
                self.assertEqual(code, 0, response)
                self._assert_outer_envelope(response)
                records = response["result"]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["id"], [
                    i for i, s in by_id.items() if s == status
                ][0])
                self.assertEqual(records[0]["status"], status)
                self.assertEqual(
                    [r["id"] for r in records], sorted(r["id"] for r in records)
                )

    def test_list_status_filter_empty_match_returns_empty_array(self):
        # 空库上按任意状态筛选均为空数组。
        empty_db = str(Path(self._tmp.name) / "empty.sqlite3")
        code, response = self._list("--status", "queued", db=empty_db)
        self.assertEqual(code, 0, response)
        self._assert_outer_envelope(response)
        self.assertEqual(response["result"], [])

    def test_list_status_equals_form_matches_spaced_form(self):
        self._prepare_all_statuses()
        _, spaced = self._list("--status", "failed")
        _, equals = self._list("--status=failed")
        self.assertEqual(equals, spaced)

    # --- 参数解析失败：退出码 2、无标准输出、不建库 --------------------------

    def test_invalid_status_arguments_fail_parsing_without_creating_db(self):
        missing_db = str(Path(self._tmp.name) / "must_not_exist.sqlite3")
        for args in (
            ["list", "--status", "Failed"],      # 大小写不匹配
            ["list", "--status", "done"],        # 非法值
            ["list", "--status", "queued", "x"],  # 多余位置参数
            ["list", "extra"],                    # 多余位置参数
            ["list", "--status"],                 # 选项缺少值
        ):
            with self.subTest(args=args):
                self.assertFalse(Path(missing_db).exists())
                code, stdout, stderr = self._run_cli(*args, db=missing_db)
                self.assertEqual(code, 2, stderr)
                self.assertEqual(stdout, "", "参数解析失败时标准输出应为空")
                self.assertIn("usage:", stderr)
                self.assertFalse(
                    Path(missing_db).exists(), "参数解析失败不得创建数据库文件"
                )

    # --- 去重、跨库隔离与只读 -----------------------------------------------

    def test_duplicate_request_key_appears_once(self):
        self._write_demo(VALID_CSV)
        first = self._submit("dup-1")
        code, again = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT,
            "--request-key", "dup-1",
        )
        self.assertEqual(code, 0, again)
        self.assertEqual(again["id"], first)

        code, response = self._list()
        self.assertEqual(code, 0, response)
        self.assertEqual([r["id"] for r in response["result"]], [first])

    def test_other_database_jobs_do_not_mix_in(self):
        self._write_demo(VALID_CSV)
        self._submit("db-a")
        other_db = str(Path(self._tmp.name) / "other.sqlite3")
        code, other = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT,
            "--request-key", "db-b", db=other_db,
        )
        self.assertEqual(code, 0, other)

        code, here = self._list()
        self.assertEqual(code, 0, here)
        code, there = self._list(db=other_db)
        self.assertEqual(code, 0, there)
        self.assertEqual(len(here["result"]), 1)
        self.assertEqual(len(there["result"]), 1)
        self.assertNotEqual(here["result"][0]["id"], there["result"][0]["id"])

    def test_list_does_not_read_input_file_or_change_records(self):
        by_id = self._prepare_all_statuses()

        # 删除输入文件后仍可列出原记录。
        DEMO_FILE.unlink()
        self.assertFalse(DEMO_FILE.exists())
        code, after_delete = self._list()
        self.assertEqual(code, 0, after_delete)
        # 重新写回后再列一次，内容与删文件后完全一致（查询不依赖文件）。
        self._write_demo(VALID_CSV)
        code, after_restore = self._list()
        self.assertEqual(code, 0, after_restore)
        self.assertEqual(after_restore, after_delete)

        # 列表结果与各记录当前持久化状态一致；重复 list 不改变任何记录。
        code, second = self._list()
        self.assertEqual(second, after_restore)
        for rec in second["result"]:
            self.assertEqual(rec["status"], by_id[rec["id"]])

    # --- 旧版六列数据库 -----------------------------------------------------

    def test_legacy_six_column_database_jobs_are_listed(self):
        legacy_db = str(Path(self._tmp.name) / "legacy.sqlite3")
        conn = sqlite3.connect(legacy_db)
        try:
            conn.execute(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, type TEXT NOT NULL,"
                " input TEXT NOT NULL, status TEXT NOT NULL, result TEXT,"
                " error TEXT)"
            )
            conn.execute(
                "INSERT INTO jobs VALUES ('zid', 'csv_summary', ?, 'failed',"
                " NULL, 'input_error')",
                (DEMO_INPUT,),
            )
            conn.execute(
                "INSERT INTO jobs VALUES ('aid', 'csv_summary', ?, 'succeeded',"
                " ?, NULL)",
                (DEMO_INPUT, json.dumps(VALID_RESULT, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()

        # 输入文件刻意不存在：list 不读取文件，旧记录按 id 升序列出。
        self.assertFalse(DEMO_FILE.exists())
        code, response = self._list(db=legacy_db)
        self.assertEqual(code, 0, response)
        self._assert_outer_envelope(response)
        self.assertEqual(
            response["result"],
            [
                {"id": "aid", "status": "succeeded", "result": VALID_RESULT,
                 "error": None},
                {"id": "zid", "status": "failed", "result": None,
                 "error": "input_error"},
            ],
        )

        code, only_failed = self._list("--status", "failed", db=legacy_db)
        self.assertEqual(code, 0, only_failed)
        self.assertEqual([r["id"] for r in only_failed["result"]], ["zid"])


if __name__ == "__main__":
    unittest.main()
