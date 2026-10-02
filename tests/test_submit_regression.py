"""任务登记（submit）边界的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，并以保存在
SQLite 中的任务记录（含独立查询进程的 show 结果）为验收依据。覆盖：

- 任务类型白名单：仅 csv_summary 可登记，未知类型返回 invalid_type；
- 输入路径边界：只接受 demo 目录内已存在的普通文件，demo 外文件、
  demo 内不存在的路径、demo 目录本身均返回 invalid_input；
- 登记语义：submit 只校验类型与路径，不读取、不执行、不校验文件内容，
  同一普通文件的项目相对路径与绝对路径分别登记为不同的 queued 任务。

每个用例使用独立的临时 SQLite 数据库（--db 指定），demo 目录缺失时
自行创建，测试文件由测试自备、结束后仅删除自身创建的资源，
不覆盖或删除任何已有文件，可连续重复运行。

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
# 用例运行前创建、结束后删除，已存在则拒绝覆盖。
VALID_DEMO_FILE = DEMO_DIR / "submit_regression_valid.csv"
INVALID_DEMO_FILE = DEMO_DIR / "submit_regression_invalid.csv"
VALID_DEMO_INPUT = "demo/submit_regression_valid.csv"
INVALID_DEMO_INPUT = "demo/submit_regression_invalid.csv"
MISSING_DEMO_INPUT = "demo/sub_regression_nonexistent.csv"
DEMO_DIR_INPUT = "demo"

VALID_CSV = "category,amount\na,10\n"
# 表头合法、金额行非法（a,x）：run 时会判 data_error，但登记时不应被校验。
INVALID_DATA_CSV = "category,amount\na,x\n"

UNKNOWN_TYPE = "not_a_real_task_type"


class SubmitRegressionTest(unittest.TestCase):
    """submit 的公开行为：JSON 响应、退出码与保存的任务记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_submit_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")

        # demo 目录缺失时自行准备；记录是否由本测试创建，结束后只清理自建资源。
        self._created_demo = False
        if not DEMO_DIR.exists():
            DEMO_DIR.mkdir(parents=True)
            self._created_demo = True
        self.addCleanup(self._cleanup_demo_files)

        # 自备演示文件若已存在则拒绝覆盖，保证不破坏他人数据。
        for demo_file in (VALID_DEMO_FILE, INVALID_DEMO_FILE):
            if demo_file.exists():
                self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % demo_file)
        VALID_DEMO_FILE.write_text(VALID_CSV, encoding="utf-8")
        INVALID_DEMO_FILE.write_text(INVALID_DATA_CSV, encoding="utf-8")

        # demo 目录外的普通文件（位于临时目录），用于越界路径拒绝用例。
        self.outside_file = Path(self._tmp.name) / "outside_demo.csv"
        self.outside_file.write_text(VALID_CSV, encoding="utf-8")

    def _cleanup_demo_files(self):
        for demo_file in (VALID_DEMO_FILE, INVALID_DEMO_FILE):
            try:
                demo_file.unlink()
            except FileNotFoundError:
                pass
        # demo 目录由本测试创建且现在为空时才移除，绝不删除原有目录。
        if self._created_demo and DEMO_DIR.exists():
            try:
                DEMO_DIR.rmdir()
            except OSError:
                pass

    # --- 辅助方法 -------------------------------------------------------

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

    def _job_rows(self):
        """以独立连接直接读取数据库中的全部任务行（按登记顺序）。"""
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT id, type, input, status, result, error FROM jobs ORDER BY rowid"
            ).fetchall()
        finally:
            conn.close()

    def _assert_queued_record(self, record):
        """登记成功的统一响应：非空 id、queued、result/error 为 null。"""
        self.assertIsInstance(record["id"], str)
        self.assertTrue(record["id"], "登记成功应返回非空 id")
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )

    def _assert_only_job(self, job_id):
        """数据库中仅有指定的一条 queued 任务，result/error 均为 NULL。"""
        rows = self._job_rows()
        self.assertEqual(len(rows), 1, rows)
        saved_id, task_type, input_path, status, result, error = rows[0]
        self.assertEqual(saved_id, job_id)
        self.assertEqual(task_type, "csv_summary")
        self.assertEqual(status, "queued")
        self.assertIsNone(result)
        self.assertIsNone(error)
        return input_path

    def _register_one_queued_job(self):
        """先成功登记一条任务，作为拒绝用例中“先前记录”的基准。"""
        code, record = self._submit("csv_summary", VALID_DEMO_INPUT)
        self.assertEqual(code, 0, record)
        self._assert_queued_record(record)
        show_code, shown = self._show(record["id"])
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)
        return record["id"], shown

    def _assert_rejected(
        self, task_type, input_path, error_code, existing_id, before_record
    ):
        """拒绝登记的统一行为：退出 1、空字段、错误码，且数据库与旧记录不变。"""
        code, record = self._submit(task_type, input_path)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": None, "status": None, "result": None, "error": error_code},
        )

        # 数据库没有新增任务。
        rows = self._job_rows()
        self.assertEqual([row[0] for row in rows], [existing_id], rows)

        # 先前成功登记的记录完全不变。
        show_code, shown = self._show(existing_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, before_record)

    # --- 成功登记 -------------------------------------------------------

    def test_relative_and_absolute_paths_register_distinct_queued_jobs(self):
        # 项目相对路径提交。
        code, relative_record = self._submit("csv_summary", VALID_DEMO_INPUT)
        self.assertEqual(code, 0, relative_record)
        self._assert_queued_record(relative_record)

        # 同一普通文件以绝对路径提交，同样成功。
        absolute_input = str(VALID_DEMO_FILE.resolve())
        code, absolute_record = self._submit("csv_summary", absolute_input)
        self.assertEqual(code, 0, absolute_record)
        self._assert_queued_record(absolute_record)

        # 两次返回不同的非空 id，互不影响。
        self.assertNotEqual(relative_record["id"], absolute_record["id"])

        # 独立查询进程读取同一数据库，得到完全相同的记录。
        show_code, shown_relative = self._show(relative_record["id"])
        self.assertEqual(show_code, 0, shown_relative)
        self.assertEqual(shown_relative, relative_record)
        show_code, shown_absolute = self._show(absolute_record["id"])
        self.assertEqual(show_code, 0, shown_absolute)
        self.assertEqual(shown_absolute, absolute_record)

        # 保存的任务记录：两条 queued 任务，各自保留提交时的输入路径写法，
        # result/error 均为 NULL（登记时不执行）。
        rows = self._job_rows()
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(
            [row[0] for row in rows],
            [relative_record["id"], absolute_record["id"]],
        )
        for row in rows:
            _id, task_type, _input, status, result, error = row
            self.assertEqual(task_type, "csv_summary")
            self.assertEqual(status, "queued")
            self.assertIsNone(result)
            self.assertIsNone(error)
        self.assertEqual(rows[0][2], VALID_DEMO_INPUT)
        self.assertEqual(rows[1][2], absolute_input)

    def test_submit_does_not_execute_or_validate_file_content(self):
        # 表头合法但金额行为 a,x（run 时会判 data_error），登记仍应成功。
        code, record = self._submit("csv_summary", INVALID_DEMO_INPUT)
        self.assertEqual(code, 0, record)
        self._assert_queued_record(record)

        # 独立查询进程：任务保持 queued，result 与 error 仍为 null，
        # 证明登记既没有提前执行汇总，也没有校验金额。
        show_code, shown = self._show(record["id"])
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)
        self._assert_only_job(record["id"])

    # --- 拒绝登记的边界 -------------------------------------------------

    def test_unknown_task_type_rejected(self):
        existing_id, before = self._register_one_queued_job()
        # 未知类型使用有效的 demo 文件，应返回 invalid_type 而非 invalid_input。
        self._assert_rejected(
            UNKNOWN_TYPE, VALID_DEMO_INPUT, "invalid_type", existing_id, before
        )

    def test_existing_file_outside_demo_rejected(self):
        existing_id, before = self._register_one_queued_job()
        # demo 外已存在的普通文件（绝对路径）。
        self._assert_rejected(
            "csv_summary",
            str(self.outside_file.resolve()),
            "invalid_input",
            existing_id,
            before,
        )

    def test_missing_path_inside_demo_rejected(self):
        existing_id, before = self._register_one_queued_job()
        self.assertFalse(
            (PROJECT_ROOT / MISSING_DEMO_INPUT).exists(),
            "用例前提：该路径不应存在：%s" % MISSING_DEMO_INPUT,
        )
        # demo 内不存在的路径。
        self._assert_rejected(
            "csv_summary", MISSING_DEMO_INPUT, "invalid_input", existing_id, before
        )

    def test_demo_directory_itself_rejected(self):
        existing_id, before = self._register_one_queued_job()
        # 指向 demo 目录本身而非普通文件的输入。
        self._assert_rejected(
            "csv_summary", DEMO_DIR_INPUT, "invalid_input", existing_id, before
        )


if __name__ == "__main__":
    unittest.main()
