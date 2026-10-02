"""排队任务取消（cancel）的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，
每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

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
        # 演示文件若已存在则拒绝覆盖，保证只清理自身数据。
        if DEMO_FILE.exists():
            self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % DEMO_FILE)
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

    # --- queued 任务可取消 ----------------------------------------------

    def test_cancel_queued_job_persists_and_show_matches(self):
        self._write_demo(VALID_CSV)
        job_id = self._submit()

        code, record = self._cancel(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "cancelled", "result": None, "error": None},
        )

        # 进程退出后重新查询，仍得到同一条 cancelled 记录。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)

    def test_cancel_succeeds_after_input_file_deleted(self):
        # 提交后删除输入文件：cancel 不读取文件，仍成功取消。
        self._write_demo(VALID_CSV)
        job_id = self._submit()
        DEMO_FILE.unlink()

        code, record = self._cancel(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "cancelled", "result": None, "error": None},
        )

        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)

    def test_cancel_succeeds_when_file_became_invalid_csv(self):
        self._write_demo(VALID_CSV)
        job_id = self._submit()
        self._write_demo("a,x\n")

        # 内容变为非法 CSV 也不影响取消，且不执行汇总。
        code, record = self._cancel(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(record["status"], "cancelled", record)
        self.assertIsNone(record["result"], record)
        self.assertIsNone(record["error"], record)

    def test_cancel_after_retry_back_to_queued(self):
        # failed 任务经 retry 回到 queued 后同样可以取消。
        self._write_demo("category,amount\na,x\n")
        job_id = self._submit()
        code, failed = self._run(job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(failed["status"], "failed", failed)

        code, record = self._retry(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(record["status"], "queued", record)

        code, record = self._cancel(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "cancelled", "result": None, "error": None},
        )

    # --- cancelled 任务的终态行为 ---------------------------------------

    def test_cancelled_job_cannot_run_or_retry_and_record_unchanged(self):
        self._write_demo(VALID_CSV)
        job_id = self._submit()
        code, cancelled = self._cancel(job_id)
        self.assertEqual(code, 0, cancelled)

        for action in (self._run, self._retry):
            code, record = action(job_id)
            self.assertEqual(code, 1, record)
            self.assertEqual(
                record,
                {"id": job_id, "status": "cancelled", "result": None,
                 "error": "invalid_state"},
            )

        # 保存的记录始终不变；show 仍返回 cancelled、result/error 均为 null。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, cancelled)

    def test_cancel_already_cancelled_rejected(self):
        self._write_demo(VALID_CSV)
        job_id = self._submit()
        self._cancel(job_id)

        code, record = self._cancel(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "cancelled", "result": None,
             "error": "invalid_state"},
        )

    # --- 其他状态拒绝取消 -----------------------------------------------

    def test_cancel_failed_job_rejected_keeps_original_error(self):
        self._write_demo("category,amount\na,x\n")
        job_id = self._submit()
        code, failed = self._run(job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(failed["error"], "data_error", failed)

        show_code, before = self._show(job_id)
        self.assertEqual(show_code, 0, before)

        code, record = self._cancel(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None,
             "error": "invalid_state"},
        )

        # 数据库中的状态与原错误值全部不变（show 的 error 仍是 data_error）。
        show_code, after = self._show(job_id)
        self.assertEqual(show_code, 0, after)
        self.assertEqual(after, before)
        self.assertEqual(after["status"], "failed")
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

        # 已有结果与状态不变。
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

        # 未创建记录：后续查询仍表示不存在。
        show_code, shown = self._show(MISSING_ID)
        self.assertEqual(show_code, 1, shown)
        self.assertEqual(
            shown,
            {"id": MISSING_ID, "status": None, "result": None, "error": "job_not_found"},
        )


if __name__ == "__main__":
    unittest.main()
