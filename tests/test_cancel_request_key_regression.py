"""按请求键取消（cancel --request-key）的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，并以独立
查询进程和临时 SQLite 数据库中保存的任务记录核对结果。覆盖键合法性、
未绑定键、queued 任务按键取消、failed/succeeded/cancelled 任务的
invalid_state 拒绝、输入文件删除或改写后的取消、不同数据库互不影响、
无键任务仍只能按 id 取消，以及 id 与 --request-key 组合非法时的参数
解析失败；本模块不改变产品实现。

每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_cancel_request_key_regression -v

或直接：

    python tests/test_cancel_request_key_regression.py
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
DEMO_FILE = DEMO_DIR / "cancel_request_key_regression_case.csv"
DEMO_INPUT = "demo/cancel_request_key_regression_case.csv"

# 合法 CSV 与非法 CSV（金额 a,x 触发 data_error）。
VALID_CSV = "category,amount\na,10\n"
VALID_RESULT = {"row_count": 1, "total_amount": 10, "categories": {"a": 10}}
INVALID_CSV = "category,amount\na,x\n"


class CancelRequestKeyRegressionTest(unittest.TestCase):
    """cancel --request-key 的公开行为：JSON 响应、退出码与保存的记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_cancel_key_test_")
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

    def _cancel_by_key(self, key, db=None):
        return self._cli("cancel", "--request-key", key, db=db)

    def _register_with_key(self, key="cancel-demo", text=VALID_CSV):
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

    def _register_and_run(self, key="cancel-demo", text=VALID_CSV):
        """以有效键登记并执行，返回 (id, run 响应)。"""
        job_id, _queued = self._register_with_key(key, text=text)
        code, ran = self._cli("run", job_id)
        return job_id, code, ran

    def _assert_cancel_rejected(self, code, record, error_code):
        """取消被拒绝且任务不存在/未绑定时：id/status/result 为 null。"""
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": None, "status": None, "result": None, "error": error_code},
        )

    # --- 基本取消：queued 任务按键取消，保留同 id 记录 -----------------------

    def test_cancel_by_key_cancels_queued_job_with_same_id(self):
        job_id, queued = self._register_with_key("cancel-demo")

        # 按键取消：退出码 0，返回原 id、cancelled 状态，result 与 error 均为 null。
        code, record = self._cancel_by_key("cancel-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record, {"id": job_id, "status": "cancelled", "result": None, "error": None}
        )
        # 响应只含四个既有字段，不额外返回输入路径或请求键。
        self.assertEqual(sorted(record), ["error", "id", "result", "status"])

        # 新进程中按该键 show 查询：返回同一 id 的 cancelled 记录，状态已保存。
        code, shown = self._cli("show", "--request-key", "cancel-demo")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, record)
        # 按 id 查询同样得到该记录。
        code, shown_by_id = self._cli("show", job_id)
        self.assertEqual(code, 0, shown_by_id)
        self.assertEqual(shown_by_id, record)
        # 取消前的 queued 响应与取消后不同，确认状态确已落库改变。
        self.assertNotEqual(shown, queued)

    def test_cancel_by_key_keeps_binding_and_does_not_create_job(self):
        job_id, _queued = self._register_with_key("cancel-demo")
        code, cancelled = self._cancel_by_key("cancel-demo")
        self.assertEqual(code, 0, cancelled)

        # 取消不新增任务：重复提交同一请求仍返回同一 id 的 cancelled 记录。
        code, again = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT, "--request-key", "cancel-demo"
        )
        self.assertEqual(code, 0, again)
        self.assertEqual(again, cancelled)

        # 数据库中恰有一条记录，原 id、类型、输入路径与键绑定均保留。
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT id, type, input, status, result, error, request_key"
                " FROM jobs"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            rows,
            [(job_id, "csv_summary", DEMO_INPUT, "cancelled", None, None, "cancel-demo")],
        )

    def test_cancel_by_key_does_not_run_summary_or_read_file(self):
        job_id, _queued = self._register_with_key("cancel-demo", text=VALID_CSV)

        # 取消不执行汇总、不读取文件：删除输入文件后仍成功。
        DEMO_FILE.unlink()
        self.assertFalse(DEMO_FILE.exists())
        code, record = self._cancel_by_key("cancel-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record, {"id": job_id, "status": "cancelled", "result": None, "error": None}
        )

    def test_cancel_by_key_does_not_validate_invalid_csv(self):
        job_id, _queued = self._register_with_key("cancel-demo", text=VALID_CSV)

        # 提交后把内容改成非法 CSV，cancel 不校验内容，仍成功。
        self._write_demo(INVALID_CSV)
        code, record = self._cancel_by_key("cancel-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record, {"id": job_id, "status": "cancelled", "result": None, "error": None}
        )

    def test_cancel_by_key_after_retry_back_to_queued(self):
        # failed 任务经 retry 回到 queued 后同样可按键取消。
        job_id, code, failed = self._register_and_run(
            "cancel-demo", text=INVALID_CSV
        )
        self.assertEqual(code, 1, failed)
        self.assertEqual(failed["status"], "failed", failed)

        code, record = self._cli("retry", "--request-key", "cancel-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["status"], "queued", record)

        code, record = self._cancel_by_key("cancel-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record, {"id": job_id, "status": "cancelled", "result": None, "error": None}
        )

    # --- 键合法性：先检查格式再查绑定 --------------------------------------

    def test_invalid_key_returns_invalid_request_key(self):
        self._register_with_key("cancel-demo")
        for bad_key in (
            "",                # 空串
            " has space",      # 含空白（不裁剪）
            "tab\tkey",        # 含制表符
            "dot.key",         # 含非法字符
            "中文键",           # 非 ASCII
            "k" * 65,          # 超过 64 个字符
        ):
            with self.subTest(key=bad_key):
                code, record = self._cancel_by_key(bad_key)
                self._assert_cancel_rejected(code, record, "invalid_request_key")

    def test_unbound_valid_key_returns_job_not_found(self):
        self._register_with_key("cancel-demo")

        # 合法但未绑定的键：job_not_found；大小写不同也是另一个键。
        for key in ("no-such-key", "CANCEL-DEMO", "cancel-demo-2"):
            with self.subTest(key=key):
                code, record = self._cancel_by_key(key)
                self._assert_cancel_rejected(code, record, "job_not_found")

    def test_key_format_checked_before_binding_lookup(self):
        # 空数据库中非法键仍返回 invalid_request_key 而非 job_not_found，
        # 且不创建任务或绑定。
        code, record = self._cancel_by_key("bad key!")
        self._assert_cancel_rejected(code, record, "invalid_request_key")

        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    # --- 非 queued 状态：invalid_state，记录与绑定不变 ----------------------

    def test_cancel_by_key_failed_job_rejected_and_saved_error_kept(self):
        job_id, code, failed = self._register_and_run(
            "cancel-demo", text=INVALID_CSV
        )
        self.assertEqual(code, 1, failed)
        self.assertEqual(
            failed,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )

        code, before = self._cli("show", "--request-key", "cancel-demo")
        self.assertEqual(code, 0, before)

        code, record = self._cancel_by_key("cancel-demo")
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "invalid_state"},
        )

        # 持久化状态、结果、原错误与绑定均不变：show 的 error 仍为 data_error。
        code, after = self._cli("show", "--request-key", "cancel-demo")
        self.assertEqual(code, 0, after)
        self.assertEqual(after, before)
        self.assertEqual(after["error"], "data_error")

    def test_cancel_by_key_succeeded_job_rejected_and_result_kept(self):
        job_id, code, ran = self._register_and_run("cancel-demo", text=VALID_CSV)
        self.assertEqual(code, 0, ran)
        self.assertEqual(ran["status"], "succeeded", ran)

        code, record = self._cancel_by_key("cancel-demo")
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

        # 成功结果未被清空，键绑定不变；show 中的 error 仍为 null。
        code, shown = self._cli("show", "--request-key", "cancel-demo")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, ran)
        self.assertIsNone(shown["error"])

    def test_cancel_by_key_twice_second_rejected_but_show_error_null(self):
        job_id, _queued = self._register_with_key("cancel-demo")
        code, cancelled = self._cancel_by_key("cancel-demo")
        self.assertEqual(code, 0, cancelled)

        # 再次取消返回 invalid_state……
        code, record = self._cancel_by_key("cancel-demo")
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "cancelled", "result": None, "error": "invalid_state"},
        )

        # ……但查询中的 error 仍为 null，持久化记录未被拒绝操作改变。
        code, shown = self._cli("show", "--request-key", "cancel-demo")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, cancelled)
        self.assertIsNone(shown["error"])

    # --- 不同数据库互不影响；无键任务仍只能按 id 取消 ------------------------

    def test_same_key_in_different_databases_cancels_own_binding(self):
        other_db = str(Path(self._tmp.name) / "other.sqlite3")
        job_id, _queued = self._register_with_key("cancel-demo")

        # 在另一个数据库中登记同名键的 queued 任务。
        self._write_demo(VALID_CSV)
        code, other_queued = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT,
            "--request-key", "cancel-demo", db=other_db,
        )
        self.assertEqual(code, 0, other_queued)
        other_id = other_queued["id"]
        self.assertNotEqual(job_id, other_id)

        # 各自按键取消各自库中的绑定。
        code, record = self._cancel_by_key("cancel-demo")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["id"], job_id)
        code, other_record = self._cancel_by_key("cancel-demo", db=other_db)
        self.assertEqual(code, 0, other_record)
        self.assertEqual(other_record["id"], other_id)

        # 两个库中的记录互不影响。
        code, shown = self._cli("show", "--request-key", "cancel-demo")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["status"], "cancelled")
        code, other_shown = self._cli(
            "show", "--request-key", "cancel-demo", db=other_db
        )
        self.assertEqual(code, 0, other_shown)
        self.assertEqual(other_shown["status"], "cancelled")

    def test_job_without_key_cancels_by_id_only(self):
        # 无键任务：按键取消返回 job_not_found，按 id 取消正常。
        self._write_demo(VALID_CSV)
        code, queued = self._cli("submit", "csv_summary", "--input", DEMO_INPUT)
        self.assertEqual(code, 0, queued)
        job_id = queued["id"]

        code, record = self._cancel_by_key("cancel-demo")
        self._assert_cancel_rejected(code, record, "job_not_found")

        code, record = self._cli("cancel", job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record, {"id": job_id, "status": "cancelled", "result": None, "error": None}
        )

    # --- 参数组合非法：沿用参数解析失败的表现 --------------------------------

    def test_cancel_with_both_id_and_request_key_fails_parsing(self):
        job_id, _queued = self._register_with_key("cancel-demo")
        db_path = Path(self._tmp.name) / "unused.sqlite3"

        code, stdout, stderr = self._run_cli(
            "cancel", job_id, "--request-key", "cancel-demo", db=str(db_path)
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        self.assertFalse(db_path.exists(), "参数解析失败时不应创建数据库文件")

    def test_cancel_with_neither_id_nor_request_key_fails_parsing(self):
        db_path = Path(self._tmp.name) / "unused.sqlite3"

        code, stdout, stderr = self._run_cli("cancel", db=str(db_path))
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        self.assertFalse(db_path.exists(), "参数解析失败时不应创建数据库文件")

    def test_cancel_with_request_key_missing_value_fails_parsing(self):
        db_path = Path(self._tmp.name) / "unused.sqlite3"

        code, stdout, stderr = self._run_cli(
            "cancel", "--request-key", db=str(db_path)
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        self.assertFalse(db_path.exists(), "参数解析失败时不应创建数据库文件")


if __name__ == "__main__":
    unittest.main()
