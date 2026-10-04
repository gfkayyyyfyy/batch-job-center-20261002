"""按请求键重试（retry --request-key）的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，并以独立
查询进程和临时 SQLite 数据库中保存的任务记录核对结果。覆盖键合法性、
未绑定键、failed 任务按键重试、queued/succeeded/cancelled 任务的
invalid_state 拒绝、输入文件删除后的重试、不同数据库互不影响、无键
任务仍只能按 id 重试，以及 id 与 --request-key 组合非法时的参数解析
失败；本模块不改变产品实现。

每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_retry_request_key_regression -v

或直接：

    python tests/test_retry_request_key_regression.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = PROJECT_ROOT / "demo"

# 测试自备的演示文件，位于 demo 目录内且不与现有文件冲突；
# 每个用例运行前创建、结束后删除。
DEMO_FILE = DEMO_DIR / "retry_request_key_regression_case.csv"
DEMO_INPUT = "demo/retry_request_key_regression_case.csv"

# 合法 CSV 与非法 CSV（金额 a,x 触发 data_error）。
VALID_CSV = "category,amount\na,10\nb,20\na,5\n"
VALID_RESULT = {
    "row_count": 3,
    "total_amount": 35,
    "categories": {"a": 15, "b": 20},
}
INVALID_CSV = "category,amount\na,x\n"


class RetryRequestKeyRegressionTest(unittest.TestCase):
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

    def _retry_by_key(self, key, db=None):
        return self._cli("retry", "--request-key", key, db=db)

    def _register_with_key(self, key="retry-demo", text=VALID_CSV):
        """以有效键成功登记一条 queued 任务，返回 (id, 记录)。"""
        self._write_demo(text)
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

    def _register_and_fail_with_data_error(self, key="retry-demo"):
        """以有效键登记并执行到 failed/data_error，返回任务 id。"""
        job_id, _queued = self._register_with_key(key, text=INVALID_CSV)
        code, failed = self._cli("run", job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(
            failed,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )
        return job_id

    def _assert_retry_rejected(self, code, record, error_code):
        """重试被拒绝：退出码 1，id/status/result 为 null，error 为错误码。"""
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": None, "status": None, "result": None, "error": error_code},
        )

    # --- 基本重试：failed 任务按键盘新入队 ---------------------------------

    def test_retry_by_key_requeues_failed_job_with_same_id(self):
        job_id = self._register_and_fail_with_data_error("retry-demo")

        # 按键重试：退出码 0，返回原 id、queued 状态，result 与 error 均为 null。
        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record, {"id": job_id, "status": "queued", "result": None, "error": None}
        )
        # 响应只含四个既有字段，不额外返回输入路径或请求键。
        self.assertEqual(sorted(record), ["error", "id", "result", "status"])

        # 新进程中按该键 show 查询：返回同一 id 的 queued 记录，状态已保存。
        code, shown = self._cli("show", "--request-key", "retry-demo")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, record)
        # 按 id 查询同样得到该记录。
        code, shown_by_id = self._cli("show", job_id)
        self.assertEqual(code, 0, shown_by_id)
        self.assertEqual(shown_by_id, record)

    def test_retry_by_key_keeps_binding_and_does_not_create_job(self):
        job_id = self._register_and_fail_with_data_error("retry-demo")

        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 0, record)

        # 重试不新增任务：重复提交同一请求仍返回同一 id 的当前记录。
        code, again = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT, "--request-key", "retry-demo"
        )
        self.assertEqual(code, 0, again)
        self.assertEqual(again, record)

        # 重试不立即执行汇总：文件仍非法，状态保持 queued 而非 failed。
        code, shown = self._cli("show", job_id)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["status"], "queued", shown)

        # 随后的 run 才按原规则校验内容，再次得到 data_error。
        code, failed = self._cli("run", job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(
            failed,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )
        # 再次失败后仍可按键重试。
        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["id"], job_id)

    def test_retry_by_key_survives_input_file_deletion(self):
        job_id = self._register_and_fail_with_data_error("retry-demo")

        # 删除输入文件后按键重试：不读取文件，仍成功入队。
        DEMO_FILE.unlink()
        self.assertFalse(DEMO_FILE.exists())
        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record, {"id": job_id, "status": "queued", "result": None, "error": None}
        )

        # 随后的 run 才发现文件缺失，得到 input_error。
        code, failed = self._cli("run", job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(
            failed,
            {"id": job_id, "status": "failed", "result": None, "error": "input_error"},
        )

    # --- 键合法性：先检查格式再查绑定 --------------------------------------

    def test_invalid_key_returns_invalid_request_key(self):
        self._register_and_fail_with_data_error("retry-demo")
        for bad_key in (
            "",                # 空串
            " has space",      # 含空白（不裁剪）
            "tab\tkey",        # 含制表符
            "dot.key",         # 含非法字符
            "中文键",           # 非 ASCII
            "k" * 65,          # 超过 64 个字符
        ):
            with self.subTest(key=bad_key):
                code, record = self._retry_by_key(bad_key)
                self._assert_retry_rejected(code, record, "invalid_request_key")

    def test_unbound_valid_key_returns_job_not_found(self):
        self._register_and_fail_with_data_error("retry-demo")

        # 合法但未绑定的键：job_not_found；大小写不同也是另一个键。
        for key in ("no-such-key", "RETRY-DEMO", "retry-demo-2"):
            with self.subTest(key=key):
                code, record = self._retry_by_key(key)
                self._assert_retry_rejected(code, record, "job_not_found")

    def test_key_format_checked_before_binding_lookup(self):
        # 空数据库中非法键仍返回 invalid_request_key 而非 job_not_found。
        code, record = self._retry_by_key("bad key!")
        self._assert_retry_rejected(code, record, "invalid_request_key")

    # --- 非 failed 状态：invalid_state，记录与绑定不变 ----------------------

    def test_retry_by_key_queued_job_rejected_with_invalid_state(self):
        job_id, queued = self._register_with_key("retry-demo")

        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record, {"id": job_id, "status": "queued", "result": None, "error": "invalid_state"}
        )

        # 任务记录与键绑定保持不变。
        code, shown = self._cli("show", "--request-key", "retry-demo")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, queued)

    def test_retry_by_key_succeeded_job_rejected_and_result_kept(self):
        job_id, _queued = self._register_with_key("retry-demo")
        code, ran = self._cli("run", job_id)
        self.assertEqual(code, 0, ran)
        self.assertEqual(ran["status"], "succeeded", ran)

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

        # 成功结果未被清空，键绑定不变。
        code, shown = self._cli("show", "--request-key", "retry-demo")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, ran)

    def test_retry_by_key_cancelled_job_rejected_with_invalid_state(self):
        job_id, _queued = self._register_with_key("retry-demo")
        code, cancelled = self._cli("cancel", job_id)
        self.assertEqual(code, 0, cancelled)

        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "cancelled", "result": None, "error": "invalid_state"},
        )

        code, shown = self._cli("show", "--request-key", "retry-demo")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, cancelled)

    # --- 不同数据库互不影响；无键任务仍只能按 id 重试 ------------------------

    def test_same_key_in_different_databases_retries_own_binding(self):
        other_db = str(Path(self._tmp.name) / "other.sqlite3")
        job_id = self._register_and_fail_with_data_error("retry-demo")

        # 在另一个数据库中登记同名键的 failed 任务。
        self._write_demo(INVALID_CSV)
        code, other_queued = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT,
            "--request-key", "retry-demo", db=other_db,
        )
        self.assertEqual(code, 0, other_queued)
        other_id = other_queued["id"]
        self.assertNotEqual(job_id, other_id)
        code, other_failed = self._cli("run", other_id, db=other_db)
        self.assertEqual(code, 1, other_failed)

        # 各自按键重试各自库中的绑定。
        code, record = self._retry_by_key("retry-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["id"], job_id)
        code, other_record = self._retry_by_key("retry-demo", db=other_db)
        self.assertEqual(code, 0, other_record)
        self.assertEqual(other_record["id"], other_id)

    def test_job_without_key_retries_by_id_only(self):
        # 无键任务：按键重试返回 job_not_found，按 id 重试正常。
        self._write_demo(INVALID_CSV)
        code, queued = self._cli("submit", "csv_summary", "--input", DEMO_INPUT)
        self.assertEqual(code, 0, queued)
        job_id = queued["id"]
        code, failed = self._cli("run", job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(failed["error"], "data_error", failed)

        code, record = self._retry_by_key("retry-demo")
        self._assert_retry_rejected(code, record, "job_not_found")

        code, record = self._cli("retry", job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record, {"id": job_id, "status": "queued", "result": None, "error": None}
        )

    # --- 参数组合非法：沿用参数解析失败的表现 --------------------------------

    def test_retry_with_both_id_and_request_key_fails_parsing(self):
        job_id = self._register_and_fail_with_data_error("retry-demo")
        db_path = Path(self._tmp.name) / "unused.sqlite3"

        code, stdout, stderr = self._run_cli(
            "retry", job_id, "--request-key", "retry-demo", db=str(db_path)
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        self.assertFalse(db_path.exists(), "参数解析失败时不应创建数据库文件")

    def test_retry_with_neither_id_nor_request_key_fails_parsing(self):
        db_path = Path(self._tmp.name) / "unused.sqlite3"

        code, stdout, stderr = self._run_cli("retry", db=str(db_path))
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        self.assertFalse(db_path.exists(), "参数解析失败时不应创建数据库文件")

    def test_retry_with_request_key_missing_value_fails_parsing(self):
        db_path = Path(self._tmp.name) / "unused.sqlite3"

        code, stdout, stderr = self._run_cli(
            "retry", "--request-key", db=str(db_path)
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        self.assertFalse(db_path.exists(), "参数解析失败时不应创建数据库文件")


if __name__ == "__main__":
    unittest.main()
