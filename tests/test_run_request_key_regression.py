"""按请求键执行（run --request-key）的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，并以独立
查询进程和临时 SQLite 数据库中保存的任务记录核对结果。覆盖按键执行
queued 任务成功、输入文件缺失或数据非法时的失败、键合法性、未绑定
键、非 queued 任务的 invalid_state 拒绝、执行不新增任务也不解除绑定、
不同数据库互不影响、无键任务仍按 id 执行，以及 id 与 --request-key
组合非法时的参数解析失败；本模块不改变产品实现。

每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_run_request_key_regression -v

或直接：

    python tests/test_run_request_key_regression.py
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
DEMO_FILE = DEMO_DIR / "run_request_key_regression_case.csv"
DEMO_INPUT = "demo/run_request_key_regression_case.csv"

# 合法 CSV 与非法 CSV（金额 a,x 触发 data_error）。
VALID_CSV = "category,amount\na,10\nb,20\na,5\n"
VALID_RESULT = {
    "row_count": 3,
    "total_amount": 35,
    "categories": {"a": 15, "b": 20},
}
INVALID_CSV = "category,amount\na,x\n"


class RunRequestKeyRegressionTest(unittest.TestCase):
    """run --request-key 的公开行为：JSON 响应、退出码与保存的记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_run_key_test_")
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

    def _run_by_key(self, key, db=None):
        return self._cli("run", "--request-key", key, db=db)

    def _register_with_key(self, key="sales-1", text=VALID_CSV):
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

    def _assert_run_rejected(self, code, record, error_code):
        """执行被拒绝：退出码 1，id/status/result 为 null，error 为错误码。"""
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": None, "status": None, "result": None, "error": error_code},
        )

    # --- 基本执行：queued 任务按键运行 -------------------------------------

    def test_run_by_key_executes_queued_job_with_same_id(self):
        job_id, _queued = self._register_with_key("sales-1")

        # 按键执行：退出码 0，返回提交时的 id 与汇总结果。
        code, record = self._run_by_key("sales-1")
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "succeeded", "result": VALID_RESULT, "error": None},
        )
        # 响应只含四个既有字段，不额外返回输入路径或请求键。
        self.assertEqual(sorted(record), ["error", "id", "result", "status"])

        # 新进程中按 id show 查询：返回同一记录，结果已持久化。
        code, shown = self._cli("show", job_id)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, record)
        # 按键查询同样得到该记录。
        code, shown_by_key = self._cli("show", "--request-key", "sales-1")
        self.assertEqual(code, 0, shown_by_key)
        self.assertEqual(shown_by_key, record)

    def test_run_by_key_does_not_create_job_or_unbind(self):
        job_id, _queued = self._register_with_key("sales-1")

        code, record = self._run_by_key("sales-1")
        self.assertEqual(code, 0, record)

        # 执行不新增任务：重复提交同一请求仍返回同一 id 的当前记录。
        code, again = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT, "--request-key", "sales-1"
        )
        self.assertEqual(code, 0, again)
        self.assertEqual(again, record)

    def test_run_by_key_missing_input_file_fails_with_input_error(self):
        job_id, _queued = self._register_with_key("sales-1")

        # 删除输入文件后按键执行：failed/input_error，退出码 1。
        DEMO_FILE.unlink()
        self.assertFalse(DEMO_FILE.exists())
        code, record = self._run_by_key("sales-1")
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "input_error"},
        )

        # 失败记录已保存；failed 任务需显式 retry 后才可再执行。
        code, again = self._run_by_key("sales-1")
        self.assertEqual(code, 1, again)
        self.assertEqual(
            again,
            {"id": job_id, "status": "failed", "result": None, "error": "invalid_state"},
        )
        code, retried = self._cli("retry", "--request-key", "sales-1")
        self.assertEqual(code, 0, retried)
        self.assertEqual(
            retried, {"id": job_id, "status": "queued", "result": None, "error": None}
        )

    def test_run_by_key_invalid_csv_fails_with_data_error(self):
        job_id, _queued = self._register_with_key("sales-1", text=INVALID_CSV)

        code, record = self._run_by_key("sales-1")
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )

        # 失败结果已持久化，键绑定不变。
        code, shown = self._cli("show", "--request-key", "sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, record)

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
                code, record = self._run_by_key(bad_key)
                self._assert_run_rejected(code, record, "invalid_request_key")

    def test_unbound_valid_key_returns_job_not_found(self):
        self._register_with_key("sales-1")

        # 合法但未绑定的键：job_not_found；大小写不同也是另一个键。
        for key in ("no-such-key", "SALES-1", "sales-2"):
            with self.subTest(key=key):
                code, record = self._run_by_key(key)
                self._assert_run_rejected(code, record, "job_not_found")

    def test_key_format_checked_before_binding_lookup(self):
        # 空数据库中非法键仍返回 invalid_request_key 而非 job_not_found。
        code, record = self._run_by_key("bad key!")
        self._assert_run_rejected(code, record, "invalid_request_key")

    # --- 非 queued 状态：invalid_state，记录与绑定不变 ----------------------

    def test_run_by_key_succeeded_job_rejected_and_result_kept(self):
        job_id, _queued = self._register_with_key("sales-1")
        code, ran = self._run_by_key("sales-1")
        self.assertEqual(code, 0, ran)

        # 再次按键执行：invalid_state，返回真实 id、当前状态与已保存结果。
        code, record = self._run_by_key("sales-1")
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

        # 已保存记录与键绑定不变。
        code, shown = self._cli("show", "--request-key", "sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, ran)

    def test_run_by_key_failed_job_rejected_with_invalid_state(self):
        job_id, _queued = self._register_with_key("sales-1", text=INVALID_CSV)
        code, failed = self._run_by_key("sales-1")
        self.assertEqual(code, 1, failed)
        self.assertEqual(failed["error"], "data_error", failed)

        # failed 任务未显式 retry 前不能再次执行。
        code, record = self._run_by_key("sales-1")
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "invalid_state"},
        )

        # 原错误仍保存在记录中。
        code, shown = self._cli("show", job_id)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, failed)

    def test_run_by_key_cancelled_job_rejected_with_invalid_state(self):
        job_id, _queued = self._register_with_key("sales-1")
        code, cancelled = self._cli("cancel", job_id)
        self.assertEqual(code, 0, cancelled)

        code, record = self._run_by_key("sales-1")
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "cancelled", "result": None, "error": "invalid_state"},
        )

        code, shown = self._cli("show", "--request-key", "sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, cancelled)

    # --- 不同数据库互不影响；无键任务仍按 id 执行 ----------------------------

    def test_same_key_in_different_databases_runs_own_binding(self):
        other_db = str(Path(self._tmp.name) / "other.sqlite3")
        job_id, _queued = self._register_with_key("sales-1")

        # 在另一个数据库中登记同名键的任务。
        self._write_demo(VALID_CSV)
        code, other_queued = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT,
            "--request-key", "sales-1", db=other_db,
        )
        self.assertEqual(code, 0, other_queued)
        other_id = other_queued["id"]
        self.assertNotEqual(job_id, other_id)

        # 各自按键执行各自库中的绑定。
        code, record = self._run_by_key("sales-1")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["id"], job_id)
        code, other_record = self._run_by_key("sales-1", db=other_db)
        self.assertEqual(code, 0, other_record)
        self.assertEqual(other_record["id"], other_id)

    def test_job_without_key_runs_by_id_only(self):
        # 无键任务：按键执行返回 job_not_found，按 id 执行正常。
        self._write_demo(VALID_CSV)
        code, queued = self._cli("submit", "csv_summary", "--input", DEMO_INPUT)
        self.assertEqual(code, 0, queued)
        job_id = queued["id"]

        code, record = self._run_by_key("sales-1")
        self._assert_run_rejected(code, record, "job_not_found")

        code, record = self._cli("run", job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "succeeded", "result": VALID_RESULT, "error": None},
        )

    # --- 参数组合非法：沿用参数解析失败的表现 --------------------------------

    def test_run_with_both_id_and_request_key_fails_parsing(self):
        job_id, _queued = self._register_with_key("sales-1")
        db_path = Path(self._tmp.name) / "unused.sqlite3"

        code, stdout, stderr = self._run_cli(
            "run", job_id, "--request-key", "sales-1", db=str(db_path)
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        self.assertFalse(db_path.exists(), "参数解析失败时不应创建数据库文件")

    def test_run_with_neither_id_nor_request_key_fails_parsing(self):
        db_path = Path(self._tmp.name) / "unused.sqlite3"

        code, stdout, stderr = self._run_cli("run", db=str(db_path))
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        self.assertFalse(db_path.exists(), "参数解析失败时不应创建数据库文件")

    def test_run_with_request_key_missing_value_fails_parsing(self):
        db_path = Path(self._tmp.name) / "unused.sqlite3"

        code, stdout, stderr = self._run_cli(
            "run", "--request-key", db=str(db_path)
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数解析失败时标准输出不输出 JSON")
        self.assertIn("usage:", stderr)
        self.assertFalse(db_path.exists(), "参数解析失败时不应创建数据库文件")


if __name__ == "__main__":
    unittest.main()
