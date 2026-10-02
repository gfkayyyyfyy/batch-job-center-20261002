"""csv_summary 汇总规则的公开行为测试。

通过 `python -m task_center` 子进程观察 submit/run/show 的 JSON 输出与
退出码，确认数据校验规则与结果留存约定：

- 成功：空行不计数、字段首尾去空格、金额前导零可接受、类别大小写区分；
- 仅含合法表头的文件得到零行汇总；
- 失败：空文件、错误表头、列数不符、类别为空、金额为负数或小数、
  非 UTF-8 字节等一律在 run 阶段得到 data_error，且不留部分汇总。

每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身创建的文件、数据库及空目录，
不依赖预置 sales.csv，不覆盖已有文件，不使用默认数据库，
可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_csv_summary_rules -v

或直接：

    python tests/test_csv_summary_rules.py
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
DEMO_FILE = DEMO_DIR / "csv_summary_rules_case.csv"
DEMO_INPUT = "demo/csv_summary_rules_case.csv"

# 成功用例：首条数据两字段带首尾空格，数据之间穿插完全空行；
# 金额 010 带前导零，类别 a 与 A 必须区分。
VALID_WITH_BLANKS_CSV = (
    "category,amount\n"
    "\n"
    " a , 010 \n"
    "\n"
    "A,0\n"
    "\n"
    "a,5\n"
)
VALID_WITH_BLANKS_RESULT = {
    "row_count": 3,
    "total_amount": 15,
    "categories": {"a": 15, "A": 0},
}

# 仅含合法表头：零行、零总额、空类别映射。
HEADER_ONLY_CSV = "category,amount\n"
HEADER_ONLY_RESULT = {"row_count": 0, "total_amount": 0, "categories": {}}


class CsvSummaryRulesTest(unittest.TestCase):
    """csv_summary 的公开行为：JSON 响应、退出码与结果留存。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_csv_rules_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")

        # demo 目录缺失时自行创建，结束后仅在目录为空时删除自建目录。
        self._demo_created = not DEMO_DIR.exists()
        if self._demo_created:
            DEMO_DIR.mkdir(parents=True)
        self.addCleanup(self._remove_demo_dir)

        # 演示文件若已存在则拒绝覆盖，保证只清理自身数据。
        if DEMO_FILE.exists():
            self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % DEMO_FILE)
        self.addCleanup(self._remove_demo_file)

    def _remove_demo_file(self):
        try:
            DEMO_FILE.unlink()
        except FileNotFoundError:
            pass

    def _remove_demo_dir(self):
        if self._demo_created:
            try:
                DEMO_DIR.rmdir()
            except OSError:
                pass

    # --- 辅助方法 -------------------------------------------------------

    def _write_demo(self, text):
        DEMO_FILE.write_text(text, encoding="utf-8")

    def _write_demo_bytes(self, data):
        DEMO_FILE.write_bytes(data)

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

    def _submit_expecting_queued(self):
        """submit 只校验类型与路径：合法路径一律排队，result/error 为 null。"""
        code, record = self._cli("submit", "csv_summary", "--input", DEMO_INPUT)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )
        return record["id"]

    def _assert_failed_data_error_persisted(self, job_id):
        """run 失败为 data_error，随后的 show 进程读到同一失败记录。"""
        code, record = self._cli("run", job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )

        # 新的查询进程读取同一数据库：失败记录被留存而非部分汇总。
        show_code, shown = self._cli("show", job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)

    # --- 成功用例 -------------------------------------------------------

    def test_run_valid_file_trims_fields_skips_blank_lines_and_keeps_case(self):
        self._write_demo(VALID_WITH_BLANKS_CSV)
        job_id = self._submit_expecting_queued()

        code, record = self._cli("run", job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(record["id"], job_id)
        self.assertEqual(record["status"], "succeeded")
        self.assertIsNone(record["error"])
        # 空行不计数；字段去空格；前导零可接受；类别 a/A 区分。
        self.assertEqual(record["result"], VALID_WITH_BLANKS_RESULT)

        # 每次执行后由新的查询进程读取同一数据库，记录与执行响应一致。
        show_code, shown = self._cli("show", job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)

    def test_run_header_only_file_succeeds_with_empty_summary(self):
        self._write_demo(HEADER_ONLY_CSV)
        job_id = self._submit_expecting_queued()

        code, record = self._cli("run", job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "succeeded", "result": HEADER_ONLY_RESULT, "error": None},
        )

        show_code, shown = self._cli("show", job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)

    # --- 失败用例：submit 排队，run 才判 data_error ----------------------

    def test_submit_queues_empty_file_and_run_fails(self):
        self._write_demo("")
        job_id = self._submit_expecting_queued()
        self._assert_failed_data_error_persisted(job_id)

    def test_submit_queues_wrong_header_and_run_fails(self):
        self._write_demo("type,sum\na,1\n")
        job_id = self._submit_expecting_queued()
        self._assert_failed_data_error_persisted(job_id)

    def test_submit_queues_row_with_too_few_columns_and_run_fails(self):
        self._write_demo("category,amount\na\n")
        job_id = self._submit_expecting_queued()
        self._assert_failed_data_error_persisted(job_id)

    def test_submit_queues_row_with_too_many_columns_and_run_fails(self):
        self._write_demo("category,amount\na,1,2\n")
        job_id = self._submit_expecting_queued()
        self._assert_failed_data_error_persisted(job_id)

    def test_submit_queues_blank_category_after_strip_and_run_fails(self):
        self._write_demo("category,amount\n   ,1\n")
        job_id = self._submit_expecting_queued()
        self._assert_failed_data_error_persisted(job_id)

    def test_submit_queues_negative_amount_and_run_fails(self):
        # 错误行位于合法行之后：失败不得留下任何部分汇总。
        self._write_demo("category,amount\na,5\na,-3\n")
        job_id = self._submit_expecting_queued()
        self._assert_failed_data_error_persisted(job_id)

    def test_submit_queues_decimal_amount_and_run_fails(self):
        self._write_demo("category,amount\na,1.5\n")
        job_id = self._submit_expecting_queued()
        self._assert_failed_data_error_persisted(job_id)

    def test_submit_queues_non_utf8_bytes_and_run_fails(self):
        # 0xff 不是合法 UTF-8 起始字节，读取阶段即按数据错误处理。
        self._write_demo_bytes(b"category,amount\na,1\n\xff,2\n")
        job_id = self._submit_expecting_queued()
        self._assert_failed_data_error_persisted(job_id)


if __name__ == "__main__":
    unittest.main()
