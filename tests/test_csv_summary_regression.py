"""csv_summary 汇总规则的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，
每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_csv_summary_regression -v

或直接：

    python tests/test_csv_summary_regression.py
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
DEMO_FILE = DEMO_DIR / "csv_summary_regression_case.csv"
DEMO_INPUT = "demo/csv_summary_regression_case.csv"

# 主成功用例：首条数据两个字段带首尾空格，数据之间穿插完全空行；
# 覆盖空行不计数、字段去空格、前导零可接受及类别大小写区分。
SPACED_CSV = "category,amount\n\n  a  ,  010  \n\nA,0\n\na,5\n"
SPACED_EXPECTED = {
    "row_count": 3,
    "total_amount": 15,
    "categories": {"a": 15, "A": 0},
}

HEADER_ONLY_CSV = "category,amount\n"
HEADER_ONLY_EXPECTED = {"row_count": 0, "total_amount": 0, "categories": {}}


class CsvSummaryRegressionTest(unittest.TestCase):
    """csv_summary 数据校验与结果留存的公开行为：JSON 响应与退出码。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_csv_summary_test_")
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

    def _submit(self):
        """提交合法路径：退出码 0，queued，result 与 error 均为 null。"""
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

    def _assert_run_succeeds(self, job_id, expected_result):
        """run 成功：退出码 0，同一 id、succeeded、error 为 null、结果精确匹配。"""
        code, record = self._run(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "succeeded", "result": expected_result, "error": None},
        )
        # 新的查询进程读取同一数据库，show 退出 0 且记录与执行结果一致。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)
        return record

    def _assert_run_fails_with_data_error(self, job_id):
        """run 失败：退出码 1，同一 id、failed、data_error、result 为 null。"""
        code, record = self._run(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record, {"id": job_id, "status": "failed", "result": None, "error": "data_error"}
        )
        # 新的查询进程读取同一数据库，show 退出 0 并保留同一失败记录。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)
        return record

    # --- 成功路径 -------------------------------------------------------

    def test_summary_with_blank_lines_spaces_leading_zero_and_case(self):
        self._write_demo(SPACED_CSV)
        job_id = self._submit()
        self._assert_run_succeeds(job_id, SPACED_EXPECTED)

    def test_header_only_file_succeeds_with_zero_summary(self):
        self._write_demo(HEADER_ONLY_CSV)
        job_id = self._submit()
        self._assert_run_succeeds(job_id, HEADER_ONLY_EXPECTED)

    # --- 数据校验失败：submit 排队，run 才报 data_error ------------------

    def test_empty_file_fails_with_data_error(self):
        self._write_demo("")
        job_id = self._submit()
        self._assert_run_fails_with_data_error(job_id)

    def test_wrong_header_fails_with_data_error(self):
        self._write_demo("cat,amount\na,1\n")
        job_id = self._submit()
        self._assert_run_fails_with_data_error(job_id)

    def test_row_with_fewer_columns_fails_with_data_error(self):
        self._write_demo("category,amount\na,1\nb\n")
        job_id = self._submit()
        self._assert_run_fails_with_data_error(job_id)

    def test_row_with_more_columns_fails_with_data_error(self):
        self._write_demo("category,amount\na,1\nb,2,extra\n")
        job_id = self._submit()
        self._assert_run_fails_with_data_error(job_id)

    def test_blank_category_after_strip_fails_with_data_error(self):
        self._write_demo("category,amount\n   ,5\n")
        job_id = self._submit()
        self._assert_run_fails_with_data_error(job_id)

    def test_negative_amount_fails_with_data_error(self):
        self._write_demo("category,amount\na,-3\n")
        job_id = self._submit()
        self._assert_run_fails_with_data_error(job_id)

    def test_decimal_amount_fails_with_data_error(self):
        self._write_demo("category,amount\na,1.5\n")
        job_id = self._submit()
        self._assert_run_fails_with_data_error(job_id)

    def test_non_utf8_bytes_fail_with_data_error(self):
        self._write_demo_bytes(b"category,amount\na,\xff\xfe\n")
        job_id = self._submit()
        self._assert_run_fails_with_data_error(job_id)

    # --- 失败不留部分汇总 -------------------------------------------------

    def test_invalid_row_after_valid_rows_leaves_no_partial_result(self):
        # 错误行位于合法行之后：失败不保留部分汇总，result 为 null。
        self._write_demo("category,amount\na,10\nb,20\nc\n")
        job_id = self._submit()
        record = self._assert_run_fails_with_data_error(job_id)
        self.assertIsNone(record["result"], record)

        # 随后的 show 仍退出 0 并保留同一失败记录（无部分结果）。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(
            shown, {"id": job_id, "status": "failed", "result": None, "error": "data_error"}
        )


if __name__ == "__main__":
    unittest.main()
