"""run 拒绝再次执行已结束任务（succeeded/failed）的公开行为回归测试。

固定“拒绝响应”与“已保存记录”的区别：对已结束任务再次 run 时，
CLI 退出码为 1，响应回显已保存记录的 id/status/result，仅把本次
响应的 error 改为 invalid_state；而数据库中的记录保持不变（成功
任务的 error 仍为 null、结果保留；失败任务的 error 仍为 data_error），
不会因为输入文件缺失或被修正而重新执行。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，
每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_run_state -v

或直接：

    python tests/test_run_state.py
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
DEMO_FILE = DEMO_DIR / "run_state_case.csv"
DEMO_INPUT = "demo/run_state_case.csv"

INVALID_CSV = "category,amount\na,x\n"
VALID_CSV = "category,amount\na,10\nb,20\na,5\n"
EXPECTED_RESULT = {"row_count": 3, "total_amount": 35, "categories": {"a": 15, "b": 20}}


class RunStateRegressionTest(unittest.TestCase):
    """run 对已结束任务的拒绝行为：JSON 响应、退出码与持久化记录。"""

    def setUp(self):
        # demo 目录缺失时自行准备，但只清理自身文件，不删除目录。
        DEMO_DIR.mkdir(parents=True, exist_ok=True)
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_run_state_test_")
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

    # --- succeeded 任务不可再次 run ------------------------------------

    def test_run_succeeded_job_rejected_even_when_input_deleted(self):
        self._write_demo(VALID_CSV)
        job_id = self._submit()

        # 首次 run：退出 0，succeeded，result 为汇总结果，error 为 null。
        code, first_run = self._run(job_id)
        self.assertEqual(code, 0, first_run)
        self.assertEqual(
            first_run,
            {"id": job_id, "status": "succeeded", "result": EXPECTED_RESULT, "error": None},
        )

        # 删除输入文件后再次 run：状态检查先于文件读取，任务被拒绝。
        DEMO_FILE.unlink()
        code, record = self._run(job_id)
        self.assertEqual(code, 1, record)
        # 拒绝响应回显原 id、succeeded 与原结果，仅响应 error 为 invalid_state。
        self.assertEqual(
            record,
            {
                "id": job_id,
                "status": "succeeded",
                "result": EXPECTED_RESULT,
                "error": "invalid_state",
            },
        )

        # 新的查询进程核对持久化记录：与首次成功响应完全一致，
        # 保存的 error 仍为 null——输入缺失不会把任务变成 failed 或清空结果。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, first_run)
        self.assertEqual(shown["status"], "succeeded")
        self.assertEqual(shown["result"], EXPECTED_RESULT)
        self.assertIsNone(shown["error"])

    # --- failed 任务不可再次 run ---------------------------------------

    def test_run_failed_job_rejected_even_after_file_fixed(self):
        self._write_demo(INVALID_CSV)
        job_id = self._submit()

        # 首次 run：退出 1，保存 failed、result 为 null、error 为 data_error。
        code, first_run = self._run(job_id)
        self.assertEqual(code, 1, first_run)
        self.assertEqual(
            first_run,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )

        # 把文件修正为合法内容后直接再次 run（未经 retry）：仍被拒绝。
        self._write_demo(VALID_CSV)
        code, record = self._run(job_id)
        self.assertEqual(code, 1, record)
        # 回显同一 id 与 failed，result 为 null，仅响应 error 为 invalid_state。
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "invalid_state"},
        )

        # 新的查询进程核对持久化记录：保留首次失败的 data_error，
        # 修正文件不会自动重新执行任务。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, first_run)
        self.assertEqual(shown["status"], "failed")
        self.assertIsNone(shown["result"])
        self.assertEqual(shown["error"], "data_error")


if __name__ == "__main__":
    unittest.main()
