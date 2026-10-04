"""按请求键重试（retry --request-key）的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，并以独立
查询进程（show）和临时 SQLite 数据库中保存的任务记录核对结果。覆盖
failed 任务按键重试成功（原 id 回到 queued、不读文件、不执行、保留
类型/输入路径/键绑定、不新增任务）、queued/succeeded/cancelled 的
invalid_state 拒绝、键合法性（先格式后绑定）、id 与 --request-key
二选一的参数解析失败、不同数据库互不影响、无键任务仍只能按 id 重试、
旧库兼容以及既有按 id 重试行为不变；本模块不改变产品实现。

每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_retry_by_key_regression -v

或直接：

    python tests/test_retry_by_key_regression.py
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
DEMO_FILE = DEMO_DIR / "retry_by_key_regression_case.csv"
DEMO_INPUT = "demo/retry_by_key_regression_case.csv"

# 非法 CSV（金额 a,x → data_error）与合法 CSV。
INVALID_CSV = "category,amount\na,x\n"
VALID_CSV = "category,amount\na,10\nb,20\na,5\n"
VALID_RESULT = {
    "row_count": 3,
    "total_amount": 35,
    "categories": {"a": 15, "b": 20},
}


class RetryByKeyRegressionTest(unittest.TestCase):
    """retry --request-key 的公开行为：JSON 响应、退出码与保存的记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_retry_key_test_")
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

    def _submit_with_key(self, key="retry-demo", text=INVALID_CSV):
        """以指定键登记一条任务，返回任务 id。"""
        self._write_demo(text)
        code, record = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT, "--request-key", key
        )
        self.assertEqual(code, 0, record)
        return record["id"]

    def _submit_without_key(self, text=INVALID_CSV):
        """登记一条无键任务，返回任务 id。"""
        self._write_demo(text)
        code, record = self._cli("submit", "csv_summary", "--input", DEMO_INPUT)
        self.assertEqual(code, 0, record)
        return record["id"]

    def _fail_with_data_error(self, key="retry-demo"):
        """以 retry-demo 键建立 data_error 失败任务，返回任务 id。"""
        job_id = self._submit_with_key(key)
        code, failed = self._cli("run", job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(
            failed,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )
        return job_id

    def _retry_by_key(self, key, db=None):
        return self._cli("retry", "--request-key", key, db=db)

    def _show_by_key(self, key, db=None):
        return self._cli("show", "--request-key", key, db=db)

    def _assert_lookup_rejected(self, code, record, error_code):
        """按键重试被拒绝且无任务可回显：id/status/result 为 null。"""
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": None, "status": None, "result": None, "error": error_code},
        )

    def _saved_row(self, job_id, db=None):
        """直接读取数据库中该任务保存的全部列。"""
        conn = sqlite3.connect(db or self.db)
        try:
            return conn.execute(
                "SELECT id, type, input, status, result, error, request_key"
                " FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()

    # --- failed 任务按键重试成功 ----------------------------------------

    def test_retry_failed_by_key_requeues_same_id(self):
        job_id = self._fail_with_data_error("retry-demo")

        # 按键重试：退出码 0，真实原 id、queued、result 与 error 均为 null。
        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )

        # 全新进程按键查询：同一条 queued 记录已持久化。
        code, shown = self._show_by_key("retry-demo")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, record)
        # 按 id 查询结果一致。
        code, shown_by_id = self._cli("show", job_id)
        self.assertEqual(code, 0, shown_by_id)
        self.assertEqual(shown_by_id, record)

    def test_retry_by_key_keeps_type_input_and_binding_without_new_job(self):
        job_id = self._fail_with_data_error("retry-demo")

        code, _record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 0)

        # 保存的行：类型、输入路径原字符串、键绑定均不变，状态回到 queued。
        self.assertEqual(
            self._saved_row(job_id),
            (job_id, "csv_summary", DEMO_INPUT, "queued", None, None, "retry-demo"),
        )
        # 数据库中仍只有这一条任务。
        conn = sqlite3.connect(self.db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

        # 同键同请求再次提交：去重命中并返回重试后的 queued 记录，
        # 证明键绑定仍在且未新增任务（不读取文件）。
        code, deduped = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT, "--request-key", "retry-demo"
        )
        self.assertEqual(code, 0, deduped)
        self.assertEqual(
            deduped,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )

    def test_retry_by_key_with_still_invalid_file_does_not_execute(self):
        job_id = self._fail_with_data_error("retry-demo")

        # 原文件仍含错误数据：retry 不读取文件也不执行汇总，照样入队。
        self.assertEqual(DEMO_FILE.read_text(encoding="utf-8"), INVALID_CSV)
        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["status"], "queued")

        # 随后的 run 才再次校验内容，仍为 data_error；之后还能继续按键重试。
        code, failed = self._cli("run", job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(failed["error"], "data_error")
        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )

    def test_retry_by_key_with_deleted_file_requeues(self):
        job_id = self._fail_with_data_error("retry-demo")

        # 重试前删除文件：retry 不读取文件，仍成功入队并持久化。
        DEMO_FILE.unlink()
        self.assertFalse(DEMO_FILE.exists())
        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )
        code, shown = self._show_by_key("retry-demo")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, record)

        # 恢复文件并改成合法内容后 run 才成功，结果正常落库。
        self._write_demo(VALID_CSV)
        code, ran = self._cli("run", job_id)
        self.assertEqual(code, 0, ran)
        self.assertEqual(
            ran,
            {"id": job_id, "status": "succeeded", "result": VALID_RESULT, "error": None},
        )

    # --- 非 failed 绑定任务：invalid_state，记录不变 ---------------------

    def test_retry_queued_by_key_rejected_with_invalid_state(self):
        job_id = self._submit_with_key("retry-demo", VALID_CSV)

        code, before = self._show_by_key("retry-demo")
        self.assertEqual(code, 0, before)
        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": "invalid_state"},
        )
        # 数据库记录与绑定保持不变。
        self.assertEqual(
            self._saved_row(job_id),
            (job_id, "csv_summary", DEMO_INPUT, "queued", None, None, "retry-demo"),
        )
        code, after = self._show_by_key("retry-demo")
        self.assertEqual(code, 0, after)
        self.assertEqual(after, before)

    def test_retry_succeeded_by_key_rejected_and_result_kept(self):
        job_id = self._submit_with_key("retry-demo", VALID_CSV)
        code, ran = self._cli("run", job_id)
        self.assertEqual(code, 0, ran)

        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {
                "id": job_id,
                "status": "succeeded",
                "result": VALID_RESULT,
                "error": "invalid_state",
            },
        )
        # 保存的成功结果未被清空，仅响应 error 为 invalid_state。
        self.assertEqual(
            self._saved_row(job_id),
            (
                job_id,
                "csv_summary",
                DEMO_INPUT,
                "succeeded",
                json.dumps(VALID_RESULT, ensure_ascii=False),
                None,
                "retry-demo",
            ),
        )

    def test_retry_cancelled_by_key_rejected_with_invalid_state(self):
        job_id = self._submit_with_key("retry-demo", VALID_CSV)
        code, cancelled = self._cli("cancel", job_id)
        self.assertEqual(code, 0, cancelled)

        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "cancelled", "result": None, "error": "invalid_state"},
        )
        self.assertEqual(
            self._saved_row(job_id),
            (job_id, "csv_summary", DEMO_INPUT, "cancelled", None, None, "retry-demo"),
        )

    # --- 键合法性：先格式再查绑定 ----------------------------------------

    def test_invalid_key_returns_invalid_request_key(self):
        self._fail_with_data_error("retry-demo")
        for bad_key in (
            "",                # 空串
            " retry-demo",     # 含前导空白（不裁剪）
            "retry-demo ",     # 含尾随空白（不裁剪）
            "tab\tkey",        # 含制表符
            "dot.key",         # 含非法字符
            "中文键",           # 非 ASCII
            "k" * 65,          # 超过 64 个字符
        ):
            with self.subTest(key=bad_key):
                code, record = self._retry_by_key(bad_key)
                self._assert_lookup_rejected(code, record, "invalid_request_key")

    def test_unbound_valid_key_returns_job_not_found(self):
        self._fail_with_data_error("RETRY-DEMO")
        # 合法但未绑定：大小写不同是另一个键；空白不裁剪故也不匹配。
        for key in ("retry-demo", "no-such-key", "retry-dem"):
            with self.subTest(key=key):
                code, record = self._retry_by_key(key)
                self._assert_lookup_rejected(code, record, "job_not_found")

    def test_key_format_checked_before_binding_lookup(self):
        # 空数据库中非法键仍返回 invalid_request_key 而非 job_not_found。
        code, record = self._retry_by_key("bad key!")
        self._assert_lookup_rejected(code, record, "invalid_request_key")

    def test_keyless_job_only_retryable_by_id(self):
        # 无键任务失败：按任何键都找不到；仍只能按 id 重试。
        self._write_demo(INVALID_CSV)
        job_id = self._submit_without_key()
        code, failed = self._cli("run", job_id)
        self.assertEqual(code, 1, failed)

        code, record = self._retry_by_key("retry-demo")
        self._assert_lookup_rejected(code, record, "job_not_found")

        code, record = self._cli("retry", job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )

    # --- 不同数据库互不影响 -----------------------------------------------

    def test_same_key_in_different_databases_retries_own_binding(self):
        other_db = str(Path(self._tmp.name) / "other.sqlite3")
        first_id = self._fail_with_data_error("retry-demo")

        # 另一个数据库以同键提交并失败。
        self._write_demo(INVALID_CSV)
        code, queued = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT,
            "--request-key", "retry-demo", db=other_db,
        )
        self.assertEqual(code, 0, queued)
        second_id = queued["id"]
        self.assertNotEqual(first_id, second_id)
        code, failed = self._cli("run", second_id, db=other_db)
        self.assertEqual(code, 1, failed)

        # 各自按键重试各自的绑定。
        code, retried_first = self._retry_by_key("retry-demo")
        self.assertEqual(code, 0, retried_first)
        self.assertEqual(retried_first["id"], first_id)
        code, retried_second = self._retry_by_key("retry-demo", db=other_db)
        self.assertEqual(code, 0, retried_second)
        self.assertEqual(retried_second["id"], second_id)

        # 跨库查询互不串扰。
        code, shown_first = self._show_by_key("retry-demo")
        self.assertEqual(code, 0, shown_first)
        self.assertEqual(shown_first, retried_first)
        code, shown_second = self._show_by_key("retry-demo", db=other_db)
        self.assertEqual(code, 0, shown_second)
        self.assertEqual(shown_second, retried_second)

    # --- 旧库兼容 ---------------------------------------------------------

    def _create_legacy_db(self, db_path, job_id, status):
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
                " VALUES (?, 'csv_summary', ?, ?, NULL, ?)",
                (job_id, DEMO_INPUT, status, "data_error" if status == "failed" else None),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_db_key_retry_unbound_but_id_retry_works(self):
        legacy_db = str(Path(self._tmp.name) / "legacy.sqlite3")
        self._create_legacy_db(legacy_db, "legacy-job-1", "failed")

        # 旧记录键为空：按键重试返回 job_not_found（id/status/result 为 null）。
        code, record = self._retry_by_key("retry-demo", db=legacy_db)
        self._assert_lookup_rejected(code, record, "job_not_found")

        # 旧库无键任务仍可按 id 重试。
        code, record = self._cli("retry", "legacy-job-1", db=legacy_db)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": "legacy-job-1", "status": "queued", "result": None, "error": None},
        )

        # 迁移后的旧库上可正常带键提交、失败再按键重试。
        self._write_demo(INVALID_CSV)
        code, queued = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT,
            "--request-key", "retry-demo", db=legacy_db,
        )
        self.assertEqual(code, 0, queued)
        job_id = queued["id"]
        code, _failed = self._cli("run", job_id, db=legacy_db)
        self.assertEqual(code, 1)
        code, retried = self._retry_by_key("retry-demo", db=legacy_db)
        self.assertEqual(code, 0, retried)
        self.assertEqual(
            retried,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )

    # --- 参数组合非法：沿用参数解析失败的表现 ------------------------------

    def test_retry_with_both_id_and_request_key_fails_parsing(self):
        job_id = self._fail_with_data_error("retry-demo")

        fresh_db = str(Path(self._tmp.name) / "should_not_exist.sqlite3")
        code, stdout, stderr = self._run_cli(
            "retry", job_id, "--request-key", "retry-demo", db=fresh_db
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        # 失败发生在打开数据库之前：不创建指定的数据库文件，原任务也未变化。
        self.assertFalse(Path(fresh_db).exists())
        self.assertEqual(
            self._saved_row(job_id)[3:6], ("failed", None, "data_error")
        )

    def test_retry_with_neither_id_nor_request_key_fails_parsing(self):
        self._fail_with_data_error("retry-demo")

        fresh_db = str(Path(self._tmp.name) / "should_not_exist_either.sqlite3")
        code, stdout, stderr = self._run_cli("retry", db=fresh_db)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        self.assertFalse(Path(fresh_db).exists())

    def test_retry_request_key_without_value_fails_parsing(self):
        fresh_db = str(Path(self._tmp.name) / "should_not_exist_value.sqlite3")
        code, stdout, stderr = self._run_cli("retry", "--request-key", db=fresh_db)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        self.assertFalse(Path(fresh_db).exists())


if __name__ == "__main__":
    unittest.main()
