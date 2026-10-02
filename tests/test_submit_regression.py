"""任务登记（submit）边界的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，并以独立
查询进程（show）和临时 SQLite 数据库中保存的任务记录核对登记结果。
覆盖任务类型白名单、demo 目录内普通文件路径以及“登记时不校验内容”
的既有语义；show 仅用于核对登记结果，本模块不调用 run，不改变产品实现。

每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_submit_regression -v

或直接：

    python tests/test_submit_regression.py
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
DEMO_FILE = DEMO_DIR / "submit_regression_case.csv"
DEMO_INPUT = "demo/submit_regression_case.csv"

# 合法 CSV：表头 category,amount，一条数据 a,10。
VALID_CSV = "category,amount\na,10\n"
# 金额列非法：登记阶段不读取内容，仍应成功入队（执行阶段才会 data_error）。
NON_NUMERIC_CSV = "category,amount\na,x\n"

# demo 内保证不存在的项目相对路径。
MISSING_INPUT = "demo/submit_regression_missing_file.csv"


class SubmitRegressionTest(unittest.TestCase):
    """submit 登记边界的公开行为：JSON 响应、退出码与保存的任务记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_submit_test_")
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

    def _submit(self, task_type, input_path):
        return self._cli("submit", task_type, "--input", input_path)

    def _show(self, job_id):
        return self._cli("show", job_id)

    def _job_count(self):
        """直接统计保存的任务记录数，确认拒绝登记没有写入数据库。"""
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

    def _assert_show_matches(self, job_id, expected):
        """独立查询进程读取同一数据库，应得到完全相同的记录。"""
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, expected)

    def _register_anchor_job(self):
        """先成功登记一条任务，用于核对后续被拒绝的操作不影响既有记录。"""
        self._write_demo(VALID_CSV)
        code, record = self._submit("csv_summary", DEMO_INPUT)
        job_id = self._assert_queued(code, record)
        return job_id, record

    def _assert_rejected(self, code, record, error_code):
        """登记被拒绝：退出码 1，id/status/result 为 null，error 为错误码。"""
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": None, "status": None, "result": None, "error": error_code},
        )

    def _assert_rejected_without_persisting(self, code, record, error_code, anchor):
        """拒绝后数据库无新增任务，先前成功登记的记录保持不变。"""
        self._assert_rejected(code, record, error_code)
        anchor_id, anchor_record = anchor
        self.assertEqual(self._job_count(), 1)
        self._assert_show_matches(anchor_id, anchor_record)

    # --- 合法登记：项目相对路径与绝对路径 -------------------------------

    def test_relative_and_absolute_paths_register_distinct_queued_jobs(self):
        self._write_demo(VALID_CSV)

        # 同一普通文件以项目相对路径登记。
        code, rel_record = self._submit("csv_summary", DEMO_INPUT)
        rel_id = self._assert_queued(code, rel_record)

        # 同一普通文件以绝对路径再次登记，同样成功入队。
        absolute_input = str(DEMO_FILE.resolve())
        code, abs_record = self._submit("csv_summary", absolute_input)
        abs_id = self._assert_queued(code, abs_record)

        # 两次登记返回不同的非空 id，数据库中各有一条记录。
        self.assertNotEqual(rel_id, abs_id)
        self.assertEqual(self._job_count(), 2)

        # 独立查询进程读取同一数据库，记录与登记响应完全一致。
        self._assert_show_matches(rel_id, rel_record)
        self._assert_show_matches(abs_id, abs_record)

    # --- 登记时不校验内容、不提前执行汇总 -------------------------------

    def test_submit_with_non_numeric_amount_still_queues_without_result(self):
        # 表头合法但金额为 a,x：登记不读取内容，仍成功入队。
        self._write_demo(NON_NUMERIC_CSV)

        code, record = self._submit("csv_summary", DEMO_INPUT)
        job_id = self._assert_queued(code, record)
        # 没有提前执行汇总：result 与 error 均为 null。
        self.assertIsNone(record["result"], record)
        self.assertIsNone(record["error"], record)

        # 独立查询进程看到的仍是同一条空结果的 queued 记录。
        self._assert_show_matches(job_id, record)
        self.assertEqual(self._job_count(), 1)

    # --- 任务类型白名单 -------------------------------------------------

    def test_unknown_task_type_returns_invalid_type_and_creates_no_job(self):
        anchor = self._register_anchor_job()

        # 未知任务类型即使配合合法的 demo 内普通文件也被拒绝。
        code, record = self._submit("no_such_task_type", DEMO_INPUT)
        self._assert_rejected_without_persisting(code, record, "invalid_type", anchor)

    # --- 输入路径边界 ---------------------------------------------------

    def test_existing_regular_file_outside_demo_returns_invalid_input(self):
        anchor = self._register_anchor_job()

        # demo 外已存在的普通文件（位于临时目录，随临时目录一并清理）。
        outside_file = Path(self._tmp.name) / "outside_demo.csv"
        outside_file.write_text(VALID_CSV, encoding="utf-8")
        self.assertTrue(outside_file.is_file())

        code, record = self._submit("csv_summary", str(outside_file.resolve()))
        self._assert_rejected_without_persisting(code, record, "invalid_input", anchor)

    def test_missing_path_inside_demo_returns_invalid_input(self):
        anchor = self._register_anchor_job()
        missing_path = PROJECT_ROOT / MISSING_INPUT
        self.assertFalse(missing_path.exists(), "前置条件：该路径不应存在：%s" % missing_path)

        code, record = self._submit("csv_summary", MISSING_INPUT)
        self._assert_rejected_without_persisting(code, record, "invalid_input", anchor)

        # 拒绝不会在磁盘上创建任何文件。
        self.assertFalse(missing_path.exists())

    def test_demo_directory_itself_returns_invalid_input(self):
        anchor = self._register_anchor_job()
        self.assertTrue(DEMO_DIR.is_dir())

        # 指向 demo 目录本身：目录不是普通文件，登记被拒绝。
        code, record = self._submit("csv_summary", "demo")
        self._assert_rejected_without_persisting(code, record, "invalid_input", anchor)


if __name__ == "__main__":
    unittest.main()
