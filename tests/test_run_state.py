"""run 拒绝再次执行已结束任务（succeeded / failed）的回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，
每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

固定两类拒绝响应与已保存记录的区别：

- 对已 succeeded 的任务再次 run：响应回显原 id、succeeded 与原
  result，仅本次响应的 error 为 invalid_state；数据库中保存的
  记录不变，error 仍为 null，输入文件缺失也不会改写结果。
- 对已 failed 的任务再次 run：响应回显原 id、failed 与 null 的
  result，本次响应的 error 为 invalid_state；数据库中保存的
  error 仍为首次失败的 data_error，修正输入文件不会自动重跑。

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

VALID_CSV = "category,amount\na,10\nb,20\na,5\n"
INVALID_CSV = "category,amount\na,x\n"
EXPECTED_RESULT = {"row_count": 3, "total_amount": 35, "categories": {"a": 15, "b": 20}}


class RunStateTest(unittest.TestCase):
    """run 对已结束任务的拒绝行为：JSON 响应、退出码与持久化记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_run_state_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        # demo 目录缺失时自行准备（不清理目录本身，只清理自备文件）。
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

    # --- 已 succeeded 的任务 ---------------------------------------------

    def test_run_succeeded_job_rejected_and_saved_record_unchanged(self):
        self._write_demo(VALID_CSV)
        job_id = self._submit()

        # 首次执行成功：退出码 0，succeeded，result 完整，error 为 null。
        code, record = self._run(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {
                "id": job_id,
                "status": "succeeded",
                "result": EXPECTED_RESULT,
                "error": None,
            },
        )

        # 删除输入文件后再次 run：退出码 1，回显原 id、succeeded 与原
        # result，仅本次响应的 error 改为 invalid_state。
        DEMO_FILE.unlink()
        code, rejected = self._run(job_id)
        self.assertEqual(code, 1, rejected)
        self.assertEqual(
            rejected,
            {
                "id": job_id,
                "status": "succeeded",
                "result": EXPECTED_RESULT,
                "error": "invalid_state",
            },
        )

        # 新的查询进程看到的持久化记录与首次成功响应一致：输入缺失
        # 不会把已成功任务变成 failed，也不会清空结果，保存的 error
        # 仍为 null。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)
        self.assertIsNone(shown["error"])

    # --- 已 failed 的任务 ------------------------------------------------

    def test_run_failed_job_rejected_and_saved_error_kept(self):
        self._write_demo(INVALID_CSV)
        job_id = self._submit()

        # 首次执行失败：退出码 1，failed，result 为 null，error 为 data_error。
        code, record = self._run(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )

        # 把文件修正为合法内容后直接再次 run：退出码 1，回显同一 id 与
        # failed，result 仍为 null，本次响应的 error 为 invalid_state。
        self._write_demo(VALID_CSV)
        code, rejected = self._run(job_id)
        self.assertEqual(code, 1, rejected)
        self.assertEqual(
            rejected,
            {"id": job_id, "status": "failed", "result": None, "error": "invalid_state"},
        )

        # 新的查询进程看到的持久化记录与首次失败响应一致：修正文件
        # 不会自动重新执行，保存的 error 仍为 data_error。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)
        self.assertEqual(shown["error"], "data_error")


if __name__ == "__main__":
    unittest.main()
