"""按请求键查询（show --request-key）的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，并以独立
查询进程和临时 SQLite 数据库中保存的任务记录核对结果。覆盖键合法性、
未绑定键、queued/succeeded/failed/cancelled 各状态查询、输入文件删除
后的持久化查询、不同数据库互不影响、旧库兼容以及 id 与 --request-key
组合非法时的参数解析失败；本模块不改变产品实现。

每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_show_request_key_regression -v

或直接：

    python tests/test_show_request_key_regression.py
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

# 测试自备的演示文件，位于 demo 目录内且不与现有文件冲突；
# 每个用例运行前创建、结束后删除。
DEMO_FILE = DEMO_DIR / "show_request_key_regression_case.csv"
DEMO_INPUT = "demo/show_request_key_regression_case.csv"

# 合法 CSV：表头 category,amount，数据 a,10、b,20、a,5。
VALID_CSV = "category,amount\na,10\nb,20\na,5\n"
VALID_RESULT = {
    "row_count": 3,
    "total_amount": 35,
    "categories": {"a": 15, "b": 20},
}


class ShowRequestKeyRegressionTest(unittest.TestCase):
    """show --request-key 的公开行为：JSON 响应、退出码与保存的记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_show_key_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        # demo 目录缺失时自行创建，并记录以便结束后清理空目录。
        self._demo_dir_created = not DEMO_DIR.exists()
        DEMO_DIR.mkdir(exist_ok=True)
        # 演示文件若已存在则拒绝覆盖，保证只清理自身数据。
        if DEMO_FILE.exists():
            self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % DEMO_FILE)
        self.addCleanup(self._remove_demo_file)

    def _remove_demo_file(self):
        try:
            DEMO_FILE.unlink()
        except FileNotFoundError:
            pass
        # 仅当 demo 目录由本测试创建且已空时才移除。
        if self._demo_dir_created:
            try:
                DEMO_DIR.rmdir()
            except OSError:
                pass

    # --- 辅助方法 -------------------------------------------------------

    def _write_demo(self, text):
        DEMO_FILE.write_text(text, encoding="utf-8")

    def _run_cli(self, *args, db=None):
        """调用 python -m task_center，返回 (退出码, 标准输出, 标准错误)。"""
        proc = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", db or self.db, *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _cli(self, *args, db=None):
        """调用 python -m task_center，返回 (退出码, 解析后的 JSON 记录)。

        标准输出必须恰为一个 JSON 对象（单行、无额外输出）。
        """
        code, stdout, stderr = self._run_cli(*args, db=db)
        self.assertTrue(stdout.strip(), "CLI 应输出一行 JSON：%r" % stderr)
        self.assertEqual(
            len(stdout.strip().splitlines()),
            1,
            "标准输出应恰为一行 JSON：%r" % stdout,
        )
        record = json.loads(stdout)
        self.assertIsInstance(record, dict, record)
        return code, record

    def _show_by_key(self, key, db=None):
        return self._cli("show", "--request-key", key, db=db)

    def _register_with_key(self, key="sales-1"):
        """以有效键成功登记一条 queued 任务，返回 (id, 记录)。"""
        self._write_demo(VALID_CSV)
        code, record = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT, "--request-key", key
        )
        self.assertEqual(code, 0, record)
        job_id = record["id"]
        self.assertIsInstance(job_id, str, record)
        self.assertTrue(job_id, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )
        return job_id, record

    def _assert_lookup_rejected(self, code, record, error_code):
        """查询被拒绝：退出码 1，id/status/result 为 null，error 为错误码。"""
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": None, "status": None, "result": None, "error": error_code},
        )

    # --- 基本查询：返回绑定任务的真实 id 与当前记录 ------------------------

    def test_show_by_key_returns_real_id_and_queued_record(self):
        job_id, queued = self._register_with_key("sales-1")

        # 按键查询：退出码 0，返回原 id、queued 状态、空结果与空错误。
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, queued)
        self.assertEqual(shown["id"], job_id)
        # 响应只含四个既有字段，不额外返回输入路径或请求键。
        self.assertEqual(sorted(shown), ["error", "id", "result", "status"])

    def test_show_by_key_after_run_returns_saved_result(self):
        job_id, _queued = self._register_with_key("sales-1")

        # 执行任务成功：行数 3、金额总和 35、类别汇总 a 的 15 和 b 的 20。
        code, ran = self._cli("run", job_id)
        self.assertEqual(code, 0, ran)
        self.assertEqual(
            ran,
            {"id": job_id, "status": "succeeded", "result": VALID_RESULT, "error": None},
        )

        # 同一键查询到相同的成功记录；与按 id 查询结果一致。
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, ran)
        code, shown_by_id = self._cli("show", job_id)
        self.assertEqual(code, 0, shown_by_id)
        self.assertEqual(shown_by_id, shown)

    def test_show_by_key_survives_input_file_deletion(self):
        job_id, _queued = self._register_with_key("sales-1")
        code, ran = self._cli("run", job_id)
        self.assertEqual(code, 0, ran)

        # 删除输入文件后按键查询：结果仍保持一致（查询不读取输入文件）。
        DEMO_FILE.unlink()
        self.assertFalse(DEMO_FILE.exists())
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, ran)

    def test_show_by_key_does_not_change_saved_record(self):
        job_id, queued = self._register_with_key("sales-1")

        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        # 查询不改变状态、结果、错误或键绑定：随后按 id 查询记录不变。
        code, again = self._cli("show", job_id)
        self.assertEqual(code, 0, again)
        self.assertEqual(again, queued)

    # --- 各状态查询均返回完整记录且退出码为 0 ------------------------------

    def test_failed_record_query_by_key_exits_zero(self):
        # 金额非法（a,x）：执行失败，error 为 data_error。
        self._write_demo("category,amount\na,x\n")
        code, queued = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT, "--request-key", "sales-1"
        )
        self.assertEqual(code, 0, queued)
        job_id = queued["id"]
        code, failed = self._cli("run", job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(
            failed,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )

        # 按键查询 failed 记录：退出码 0，完整返回保存的错误，不当作执行失败。
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, failed)

    def test_cancelled_record_query_by_key_exits_zero(self):
        job_id, _queued = self._register_with_key("sales-1")
        code, cancelled = self._cli("cancel", job_id)
        self.assertEqual(code, 0, cancelled)
        self.assertEqual(
            cancelled,
            {"id": job_id, "status": "cancelled", "result": None, "error": None},
        )

        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, cancelled)

    # --- 键合法性：先检查格式再查绑定 --------------------------------------

    def test_invalid_key_returns_invalid_request_key(self):
        self._register_with_key("sales-1")
        for bad_key in (
            "",                # 空串
            " has space",      # 含空白（不裁剪）
            "tab\tkey",        # 含制表符
            "dot.key",         # 含非法字符
            "中文键",           # 非 ASCII
            "k" * 65,          # 超过 64 个字符
        ):
            with self.subTest(key=bad_key):
                code, record = self._show_by_key(bad_key)
                self._assert_lookup_rejected(code, record, "invalid_request_key")

    def test_unbound_valid_key_returns_job_not_found(self):
        self._register_with_key("sales-1")

        # 合法但未绑定的键：job_not_found；大小写不同也是另一个键。
        for key in ("no-such-key", "SALES-1", "sales-2"):
            with self.subTest(key=key):
                code, record = self._show_by_key(key)
                self._assert_lookup_rejected(code, record, "job_not_found")

    def test_key_format_checked_before_binding_lookup(self):
        # 空数据库中非法键仍返回 invalid_request_key 而非 job_not_found。
        code, record = self._show_by_key("bad key!")
        self._assert_lookup_rejected(code, record, "invalid_request_key")

    def test_boundary_keys_are_accepted(self):
        self._write_demo(VALID_CSV)
        for key in ("k", "Ab0_-" + "z" * 59):
            self.assertLessEqual(len(key), 64)
            with self.subTest(key=key):
                code, queued = self._cli(
                    "submit", "csv_summary", "--input", DEMO_INPUT,
                    "--request-key", key,
                )
                self.assertEqual(code, 0, queued)
                code, shown = self._show_by_key(key)
                self.assertEqual(code, 0, shown)
                self.assertEqual(shown, queued)

    # --- 不同数据库互不影响 -------------------------------------------------

    def test_same_key_in_different_databases_queries_own_binding(self):
        self._write_demo(VALID_CSV)
        other_db = str(Path(self._tmp.name) / "other.sqlite3")

        code, first = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT, "--request-key", "sales-1"
        )
        self.assertEqual(code, 0, first)
        code, second = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT,
            "--request-key", "sales-1", db=other_db,
        )
        self.assertEqual(code, 0, second)
        self.assertNotEqual(first["id"], second["id"])

        # 同名键各自查询各自的绑定。
        code, shown_first = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown_first)
        self.assertEqual(shown_first, first)
        code, shown_second = self._show_by_key("sales-1", db=other_db)
        self.assertEqual(code, 0, shown_second)
        self.assertEqual(shown_second, second)

    # --- 旧库兼容 -----------------------------------------------------------

    def _create_legacy_db(self, db_path, job_id):
        """手工建立仅六列（无 request_key 列）的旧版 jobs 表并插入一条记录。"""
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE jobs ("
                " id TEXT PRIMARY KEY, type TEXT NOT NULL, input TEXT NOT NULL,"
                " status TEXT NOT NULL, result TEXT, error TEXT)"
            )
            conn.execute(
                "INSERT INTO jobs (id, type, input, status, result, error)"
                " VALUES (?, 'csv_summary', ?, 'queued', NULL, NULL)",
                (job_id, DEMO_INPUT),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_db_jobs_still_queryable_by_id_and_key_lookup_works(self):
        legacy_db = str(Path(self._tmp.name) / "legacy.sqlite3")
        self._create_legacy_db(legacy_db, "legacy-job-1")

        # 旧库中没有请求键的任务仍能按 id 查询。
        code, shown = self._cli("show", "legacy-job-1", db=legacy_db)
        self.assertEqual(code, 0, shown)
        self.assertEqual(
            shown,
            {"id": "legacy-job-1", "status": "queued", "result": None, "error": None},
        )

        # 旧记录键为空：按键查询返回 job_not_found。
        code, record = self._show_by_key("sales-1", db=legacy_db)
        self._assert_lookup_rejected(code, record, "job_not_found")

        # 迁移后的旧库上可正常登记并按键查询新任务。
        self._write_demo(VALID_CSV)
        code, queued = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT,
            "--request-key", "sales-1", db=legacy_db,
        )
        self.assertEqual(code, 0, queued)
        code, shown = self._show_by_key("sales-1", db=legacy_db)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, queued)

    # --- 参数组合非法：沿用参数解析失败的表现 --------------------------------

    def test_show_with_both_id_and_request_key_fails_parsing(self):
        job_id, _queued = self._register_with_key("sales-1")

        code, stdout, stderr = self._run_cli(
            "show", job_id, "--request-key", "sales-1"
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)

    def test_show_with_neither_id_nor_request_key_fails_parsing(self):
        code, stdout, stderr = self._run_cli("show")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)


if __name__ == "__main__":
    unittest.main()
