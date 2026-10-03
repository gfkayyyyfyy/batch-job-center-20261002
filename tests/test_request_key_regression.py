"""提交去重（--request-key）的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，并以独立
查询进程（show）和临时 SQLite 数据库中保存的任务记录核对结果。覆盖
请求键合法性、同键同请求去重、类型/路径冲突、键绑定跨状态保留以及
不提供键时的既有行为；本模块不改变产品实现。

每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_request_key_regression -v

或直接：

    python tests/test_request_key_regression.py
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
DEMO_FILE = DEMO_DIR / "request_key_regression_case.csv"
DEMO_INPUT = "demo/request_key_regression_case.csv"

# 合法 CSV：表头 category,amount，数据 a,10、b,20、a,5。
VALID_CSV = "category,amount\na,10\nb,20\na,5\n"
VALID_RESULT = {
    "row_count": 3,
    "total_amount": 35,
    "categories": {"a": 15, "b": 20},
}

# demo 内保证不存在的项目相对路径。
MISSING_INPUT = "demo/request_key_regression_missing_file.csv"


class RequestKeyRegressionTest(unittest.TestCase):
    """--request-key 提交去重的公开行为：JSON 响应、退出码与保存的记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_request_key_test_")
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

    def _cli(self, *args, db=None):
        """调用 python -m task_center，返回 (退出码, 解析后的 JSON 记录)。

        标准输出必须恰为一个 JSON 对象（单行、无额外输出）。
        """
        proc = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", db or self.db, *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertTrue(proc.stdout.strip(), "CLI 应输出一行 JSON：%r" % proc.stderr)
        self.assertEqual(
            len(proc.stdout.strip().splitlines()),
            1,
            "标准输出应恰为一行 JSON：%r" % proc.stdout,
        )
        record = json.loads(proc.stdout)
        self.assertIsInstance(record, dict, record)
        return proc.returncode, record

    def _submit(self, task_type, input_path, request_key=None, db=None):
        args = ["submit", task_type, "--input", input_path]
        if request_key is not None:
            args += ["--request-key", request_key]
        return self._cli(*args, db=db)

    def _show(self, job_id):
        return self._cli("show", job_id)

    def _job_count(self):
        """直接统计保存的任务记录数，确认拒绝或去重没有写入数据库。"""
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finally:
            conn.close()

    def _assert_queued(self, code, record):
        """登记成功：退出码 0，非空 id、queued、result 与 error 均为 null。"""
        self.assertEqual(code, 0, record)
        self.assertIsInstance(record["id"], str, record)
        self.assertTrue(record["id"], record)
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )
        return record["id"]

    def _assert_rejected(self, code, record, error_code):
        """提交被拒绝：退出码 1，id/status/result 为 null，error 为错误码。"""
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": None, "status": None, "result": None, "error": error_code},
        )

    def _register_with_key(self, key="sales-1"):
        """以有效键成功登记一条 queued 任务，返回 (id, 记录)。"""
        self._write_demo(VALID_CSV)
        code, record = self._submit("csv_summary", DEMO_INPUT, request_key=key)
        job_id = self._assert_queued(code, record)
        return job_id, record

    # --- 同键同请求去重 ---------------------------------------------------

    def test_same_key_same_request_returns_same_job_once(self):
        job_id, record = self._register_with_key("sales-1")

        # 同键、同类型、同路径原字符串再次提交：返回同一 id，不新增任务。
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        self.assertEqual(code, 0, again)
        self.assertEqual(again, record)
        self.assertEqual(again["id"], job_id)
        self.assertEqual(self._job_count(), 1)

        # 独立查询进程读取同一数据库，记录与登记响应完全一致。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)

    def test_duplicate_submit_returns_saved_record_after_run(self):
        job_id, _queued = self._register_with_key("sales-1")

        # 执行任务成功。
        code, ran = self._cli("run", job_id)
        self.assertEqual(code, 0, ran)
        self.assertEqual(
            ran,
            {"id": job_id, "status": "succeeded", "result": VALID_RESULT, "error": None},
        )

        # 执行成功后同键同路径提交：返回原成功记录，不新增任务、不重置结果。
        self._write_demo("category,amount\nz,999\n")  # 改写文件不影响返回
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        self.assertEqual(code, 0, again)
        self.assertEqual(again, ran)
        self.assertEqual(self._job_count(), 1)

    def test_duplicate_submit_does_not_recheck_file(self):
        job_id, record = self._register_with_key("sales-1")

        # 原文件被删除后，同键同路径提交仍返回原记录（不再检查文件）。
        DEMO_FILE.unlink()
        self.assertFalse(DEMO_FILE.exists())
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        self.assertEqual(code, 0, again)
        self.assertEqual(again, record)
        self.assertEqual(self._job_count(), 1)

    def test_binding_survives_cancel(self):
        job_id, _queued = self._register_with_key("sales-1")

        code, cancelled = self._cli("cancel", job_id)
        self.assertEqual(code, 0, cancelled)
        self.assertEqual(
            cancelled,
            {"id": job_id, "status": "cancelled", "result": None, "error": None},
        )

        # cancel 不解除绑定：同键同路径提交返回 cancelled 记录，不新增任务。
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        self.assertEqual(code, 0, again)
        self.assertEqual(again, cancelled)
        self.assertEqual(self._job_count(), 1)

    # --- 失败 → 重新入队 → 再次成功：键绑定始终返回原任务当前记录 -----------

    def test_failed_retry_and_succeed_keeps_request_key_binding(self):
        key = "retry-key"

        # 登记：专用文件首行 category,amount，数据行 a,x（金额非法）。
        self._write_demo("category,amount\na,x\n")
        code, queued = self._submit("csv_summary", DEMO_INPUT, request_key=key)
        job_id = self._assert_queued(code, queued)
        self.assertEqual(self._job_count(), 1)
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, queued)

        # 执行失败：退出码 1，failed，error 为 data_error，result 为 null。
        code, failed = self._cli("run", job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(
            failed,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )
        self.assertEqual(self._job_count(), 1)
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, failed)

        # 删除文件后同键、同类型、同路径原字符串再次提交：退出码 0，
        # 完整返回原失败记录，不新增任务、不再检查文件。
        DEMO_FILE.unlink()
        self.assertFalse(DEMO_FILE.exists())
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key=key)
        self.assertEqual(code, 0, again)
        self.assertEqual(again, failed)
        self.assertEqual(self._job_count(), 1)
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, failed)

        # 文件仍缺失时 retry：退出码 0，保留原 id，回到 queued，
        # result 与 error 清为 null（retry 不解除请求键绑定）。
        code, requeued = self._cli("retry", job_id)
        self.assertEqual(code, 0, requeued)
        self.assertEqual(
            requeued,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )
        self.assertEqual(self._job_count(), 1)
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, requeued)

        # 文件仍缺失时同键提交：返回相同排队记录，不重新检查文件或执行任务。
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key=key)
        self.assertEqual(code, 0, again)
        self.assertEqual(again, requeued)
        self.assertEqual(self._job_count(), 1)
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, requeued)

        # 恢复带原表头的有效数据 a,10、b,20、a,5 后执行原 id：
        # 退出码 0，succeeded，row_count 3、total_amount 35、
        # categories {"a":15,"b":20}，error 为 null。
        self._write_demo(VALID_CSV)
        code, succeeded = self._cli("run", job_id)
        self.assertEqual(code, 0, succeeded)
        self.assertEqual(
            succeeded,
            {
                "id": job_id,
                "status": "succeeded",
                "result": VALID_RESULT,
                "error": None,
            },
        )
        self.assertEqual(self._job_count(), 1)
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, succeeded)

        # 再次同键提交：完整返回该成功记录，数据库仍只有一条任务。
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key=key)
        self.assertEqual(code, 0, again)
        self.assertEqual(again, succeeded)
        self.assertEqual(self._job_count(), 1)
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, succeeded)

    # --- 路径原字符串匹配 -------------------------------------------------

    def test_relative_and_absolute_paths_are_distinct_requests(self):
        job_id, record = self._register_with_key("sales-1")

        # 同一文件换用绝对路径写法：与已绑定路径原字符串不同，视为冲突。
        absolute_input = str(DEMO_FILE.resolve())
        code, conflict = self._submit(
            "csv_summary", absolute_input, request_key="sales-1"
        )
        self._assert_rejected(code, conflict, "request_conflict")
        self.assertEqual(self._job_count(), 1)

        # 不同键互不影响：绝对路径配新键可正常登记。
        code, other = self._submit(
            "csv_summary", absolute_input, request_key="sales-2"
        )
        other_id = self._assert_queued(code, other)
        self.assertNotEqual(other_id, job_id)
        self.assertEqual(self._job_count(), 2)

    # --- 冲突：已有绑定而类型或路径不匹配 ---------------------------------

    def test_bound_key_with_different_path_returns_request_conflict(self):
        job_id, record = self._register_with_key("sales-1")

        # 已有绑定而路径不匹配：request_conflict，不检查新路径、不修改原任务。
        code, conflict = self._submit(
            "csv_summary", MISSING_INPUT, request_key="sales-1"
        )
        self._assert_rejected(code, conflict, "request_conflict")
        self.assertEqual(self._job_count(), 1)
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)

    # --- 键合法性 ---------------------------------------------------------

    def test_invalid_request_key_returns_invalid_request_key(self):
        self._write_demo(VALID_CSV)
        for bad_key in (
            "",                # 空串
            " has space",      # 含空白（不裁剪）
            "tab\tkey",        # 含制表符
            "dot.key",         # 含非法字符
            "中文键",           # 非 ASCII
            "k" * 65,          # 超过 64 个字符
        ):
            with self.subTest(key=bad_key):
                code, record = self._submit(
                    "csv_summary", DEMO_INPUT, request_key=bad_key
                )
                self._assert_rejected(code, record, "invalid_request_key")
        self.assertEqual(self._job_count(), 0)

    def test_invalid_type_checked_before_request_key(self):
        self._write_demo(VALID_CSV)

        # 先检查任务类型：类型非法时即使键也非法仍返回 invalid_type。
        code, record = self._submit(
            "no_such_task_type", DEMO_INPUT, request_key="bad key!"
        )
        self._assert_rejected(code, record, "invalid_type")
        self.assertEqual(self._job_count(), 0)

    def test_boundary_keys_are_accepted(self):
        self._write_demo(VALID_CSV)

        # 单字符键与 64 字符键（字母/数字/下划线/连字符）均合法。
        code, one = self._submit("csv_summary", DEMO_INPUT, request_key="k")
        self._assert_queued(code, one)
        long_key = "Ab0_-" + "z" * 59
        self.assertEqual(len(long_key), 64)
        code, long_rec = self._submit("csv_summary", DEMO_INPUT, request_key=long_key)
        self._assert_queued(code, long_rec)
        self.assertEqual(self._job_count(), 2)

    def test_keys_are_case_sensitive(self):
        self._write_demo(VALID_CSV)

        code, lower = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        lower_id = self._assert_queued(code, lower)
        # 区分大小写：SALES-1 是另一个键，独立登记新任务。
        code, upper = self._submit("csv_summary", DEMO_INPUT, request_key="SALES-1")
        upper_id = self._assert_queued(code, upper)
        self.assertNotEqual(lower_id, upper_id)
        self.assertEqual(self._job_count(), 2)

    # --- 首次使用键时仍按原规则校验路径 -----------------------------------

    def test_invalid_input_with_key_creates_no_job_and_key_stays_free(self):
        # 路径不合法：invalid_input，不创建任务，也不占用该键。
        code, record = self._submit(
            "csv_summary", MISSING_INPUT, request_key="sales-1"
        )
        self._assert_rejected(code, record, "invalid_input")
        self.assertEqual(self._job_count(), 0)

        # 同键配合法路径随后可正常登记。
        self._write_demo(VALID_CSV)
        code, queued = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        self._assert_queued(code, queued)
        self.assertEqual(self._job_count(), 1)

    # --- 不提供键时保持原行为 ----------------------------------------------

    def test_submit_without_key_still_creates_distinct_jobs(self):
        self._write_demo(VALID_CSV)

        code, first = self._submit("csv_summary", DEMO_INPUT)
        first_id = self._assert_queued(code, first)
        code, second = self._submit("csv_summary", DEMO_INPUT)
        second_id = self._assert_queued(code, second)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self._job_count(), 2)

        # 无键任务不参与键匹配：带键提交同一路径仍创建新任务。
        code, keyed = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        keyed_id = self._assert_queued(code, keyed)
        self.assertNotIn(keyed_id, (first_id, second_id))
        self.assertEqual(self._job_count(), 3)

    # --- 不同数据库互不影响 -------------------------------------------------

    def test_same_key_in_different_databases_is_independent(self):
        self._write_demo(VALID_CSV)
        other_db = str(Path(self._tmp.name) / "other.sqlite3")

        code, first = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        first_id = self._assert_queued(code, first)
        code, second = self._submit(
            "csv_summary", DEMO_INPUT, request_key="sales-1", db=other_db
        )
        second_id = self._assert_queued(code, second)
        self.assertNotEqual(first_id, second_id)


if __name__ == "__main__":
    unittest.main()
