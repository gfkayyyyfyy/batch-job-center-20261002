"""排队任务取消（cancel）的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，
每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件。模块开始准备时若 demo 目录不存在会自行创建，
结束时仅在该目录由本模块创建且已经为空的情况下移除；测试结束
仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_cancel_regression -v

或直接：

    python tests/test_cancel_regression.py
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
DEMO_FILE = DEMO_DIR / "cancel_regression_case.csv"
DEMO_INPUT = "demo/cancel_regression_case.csv"

# demo 目录是否在本模块开始准备前就已存在。该判断在导入时记录：
# 联合运行时 unittest 先导入命令行上的全部模块再依次执行，因此两个
# 顺序执行的模块各自保留独立结论，互不串扰。
_demo_preexisting = DEMO_DIR.exists()


def setUpModule():
    """准备演示目录与样例生命周期的前置条件。

    demo 目录不存在时由测试自行创建；专用样例若已存在则直接令本模块
    失败（失败信息包含冲突路径），绝不覆盖或删除任何既有文件。
    """
    if DEMO_FILE.exists():
        raise AssertionError(
            "演示样例已存在，为避免覆盖或误删他人数据而中止：%s" % DEMO_FILE
        )
    # 仅创建 demo 自身；项目根目录必然已存在。
    DEMO_DIR.mkdir(exist_ok=True)


def tearDownModule():
    """回收本模块自建数据。

    删除专用样例（样例已在用例中被删除时不报错）；仅当 demo 目录由
    本模块创建且删除样例后已经为空时才移除目录，目录中仍有其他文件
    （含 tasks.sqlite3 等非测试文件）时一律保留。
    """
    try:
        DEMO_FILE.unlink()
    except FileNotFoundError:
        pass
    if not _demo_preexisting:
        try:
            DEMO_DIR.rmdir()
        except OSError:
            # 目录非空或已被移除：非空说明含非本测试文件，保留原样。
            pass

INVALID_CSV = "category,amount\na,x\n"
VALID_CSV = "category,amount\na,10\n"
EXPECTED_RESULT = {"row_count": 1, "total_amount": 10, "categories": {"a": 10}}

MISSING_ID = "0" * 32  # 不存在的任务 id


class CancelRegressionTest(unittest.TestCase):
    """cancel 的公开行为：JSON 响应与退出码。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_cancel_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        # 样例冲突已在 setUpModule 拒绝；此处登记清理，保证断言失败后
        # 也只回收本测试自己写入的样例文件。
        self.addCleanup(self._remove_demo_file)

    def _remove_demo_file(self):
        try:
            DEMO_FILE.unlink()
        except FileNotFoundError:
            pass

    # --- 辅助方法 -------------------------------------------------------

    def _write_demo(self, text):
        DEMO_FILE.write_text(text, encoding="utf-8")

    def _cli(self, *args):
        """调用 python -m task_center，返回 (退出码, 解析后的 JSON 记录)。"""
        proc = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", self.db, *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertTrue(proc.stdout.strip(), "CLI 应输出一行 JSON：%r" % proc.stderr)
        return proc.returncode, json.loads(proc.stdout)

    def _submit(self):
        code, record = self._cli("submit", "csv_summary", "--input", DEMO_INPUT)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )
        return record["id"]

    def _run(self, job_id):
        return self._cli("run", job_id)

    def _show(self, job_id):
        return self._cli("show", job_id)

    def _retry(self, job_id):
        return self._cli("retry", job_id)

    def _cancel(self, job_id):
        return self._cli("cancel", job_id)

    def _assert_cancel_ok(self, job_id):
        """cancel 成功：退出码 0，原 id、cancelled、result 与 error 均为 null。"""
        code, record = self._cancel(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record, {"id": job_id, "status": "cancelled", "result": None, "error": None}
        )
        # 随后的 show 与 cancel 响应一致（进程退出后记录仍保留）。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)
        return record

    def _assert_terminal_after_cancel(self, job_id):
        """cancelled 任务不能再 run 或 retry，且记录保持不变。"""
        expected = {"id": job_id, "status": "cancelled", "result": None, "error": None}

        code, record = self._run(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "cancelled", "result": None, "error": "invalid_state"},
        )

        code, record = self._retry(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "cancelled", "result": None, "error": "invalid_state"},
        )

        # 两次拒绝操作均未改变保存的记录。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, expected)

    # --- 排队任务可取消 -------------------------------------------------

    def test_cancel_queued_job_then_show_consistent(self):
        self._write_demo(VALID_CSV)
        job_id = self._submit()

        self._assert_cancel_ok(job_id)
        self._assert_terminal_after_cancel(job_id)

    def test_cancel_does_not_read_deleted_input_file(self):
        self._write_demo(VALID_CSV)
        job_id = self._submit()

        # 提交后删除输入文件，cancel 不读取文件，仍成功。
        DEMO_FILE.unlink()
        self._assert_cancel_ok(job_id)
        self._assert_terminal_after_cancel(job_id)

    def test_cancel_does_not_validate_invalid_csv(self):
        self._write_demo(VALID_CSV)
        job_id = self._submit()

        # 提交后把内容改成非法 CSV，cancel 不校验内容，仍成功。
        self._write_demo(INVALID_CSV)
        self._assert_cancel_ok(job_id)
        self._assert_terminal_after_cancel(job_id)

    def test_cancel_after_retry_back_to_queued(self):
        self._write_demo(INVALID_CSV)
        job_id = self._submit()
        code, record = self._run(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(record["status"], "failed", record)

        # failed 任务经 retry 回到 queued 后同样可取消。
        code, record = self._retry(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(record["status"], "queued", record)

        self._assert_cancel_ok(job_id)
        self._assert_terminal_after_cancel(job_id)

    def test_cancel_twice_rejected_and_record_unchanged(self):
        self._write_demo(VALID_CSV)
        job_id = self._submit()
        self._assert_cancel_ok(job_id)

        # 对已 cancelled 的任务再次 cancel 被拒绝。
        code, record = self._cancel(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "cancelled", "result": None, "error": "invalid_state"},
        )

        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(
            shown, {"id": job_id, "status": "cancelled", "result": None, "error": None}
        )

    # --- 拒绝取消的边界 -------------------------------------------------

    def test_cancel_failed_job_rejected_and_record_unchanged(self):
        self._write_demo(INVALID_CSV)
        job_id = self._submit()
        code, record = self._run(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record, {"id": job_id, "status": "failed", "result": None, "error": "data_error"}
        )

        show_code, before = self._show(job_id)
        self.assertEqual(show_code, 0, before)

        code, record = self._cancel(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "invalid_state"},
        )

        # 操作前后的 show 输出完全一致，原 error 值不变。
        show_code, after = self._show(job_id)
        self.assertEqual(show_code, 0, after)
        self.assertEqual(after, before)
        self.assertEqual(after["error"], "data_error")

    def test_cancel_succeeded_job_rejected_and_result_kept(self):
        self._write_demo(VALID_CSV)
        job_id = self._submit()
        code, record = self._run(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(record["status"], "succeeded", record)

        show_code, before = self._show(job_id)
        self.assertEqual(show_code, 0, before)

        code, record = self._cancel(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {
                "id": job_id,
                "status": "succeeded",
                "result": EXPECTED_RESULT,
                "error": "invalid_state",
            },
        )

        # 操作前后的 show 输出完全一致，成功结果未被清空。
        show_code, after = self._show(job_id)
        self.assertEqual(show_code, 0, after)
        self.assertEqual(after, before)
        self.assertEqual(after["result"], EXPECTED_RESULT)

    def test_cancel_unknown_id_returns_job_not_found(self):
        code, record = self._cancel(MISSING_ID)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": MISSING_ID, "status": None, "result": None, "error": "job_not_found"},
        )

        # 不创建记录：后续查询仍表示不存在。
        show_code, shown = self._show(MISSING_ID)
        self.assertEqual(show_code, 1, shown)
        self.assertEqual(
            shown,
            {"id": MISSING_ID, "status": None, "result": None, "error": "job_not_found"},
        )


if __name__ == "__main__":
    unittest.main()
