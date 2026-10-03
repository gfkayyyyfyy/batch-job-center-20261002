"""CSV 解析器自身拒绝数据（csv.Error）时的失败落库与重试恢复回归测试。

标准 csv 解析器对单个字段有默认长度上限（csv.field_size_limit()，
CPython 默认为 131072）。超长字段会让 csv.reader 抛出 csv.Error；
该异常与编码、表头、字段校验失败一样属于数据问题，必须走既有
失败路径：run 返回 failed/data_error 并持久化，而不是输出异常堆栈。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，
每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
专用的演示文件，测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_csv_parse_error_regression -v

或直接：

    python tests/test_csv_parse_error_regression.py
"""

import csv
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
DEMO_FILE = DEMO_DIR / "csv_parse_error_regression_case.csv"
DEMO_INPUT = "demo/csv_parse_error_regression_case.csv"

# 首行表头，第二行合法数据，最后一行的类别超过解析器默认字段上限
# （200000 > 131072），各行均以换行符结束。
OVERLONG_CATEGORY = "a" * 200000
PARSE_ERROR_CSV = "category,amount\na,10\n%s,1\n" % OVERLONG_CATEGORY

# 修复后的文件：仅表头加一条合法数据。
FIXED_CSV = "category,amount\na,10\n"
FIXED_EXPECTED = {
    "row_count": 1,
    "total_amount": 10,
    "categories": {"a": 10},
}

FAILED_RECORD = {"status": "failed", "result": None, "error": "data_error"}


class CsvParseErrorRegressionTest(unittest.TestCase):
    """解析器拒绝的数据同样落为可查询的 failed/data_error 记录。"""

    def setUp(self):
        # 该用例依赖标准解析器的默认字段上限：200000 个字符必须确实
        # 触发 csv.Error，否则用例失去意义。
        self.assertGreater(
            len(OVERLONG_CATEGORY),
            csv.field_size_limit(),
            "测试类别长度应超过解析器默认字段上限",
        )
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_parse_error_test_")
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
        """调用 python -m task_center，返回 (退出码, stdout 文本, 解析后的 JSON, stderr)。"""
        proc = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", self.db, *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        stdout = proc.stdout.strip()
        self.assertTrue(stdout, "CLI 应输出一行 JSON：%r" % proc.stderr)
        self.assertEqual(
            len(stdout.splitlines()), 1, "标准输出应只有一个 JSON 对象：%r" % proc.stdout
        )
        return proc.returncode, stdout, json.loads(stdout), proc.stderr

    def _submit(self):
        """提交合法路径：退出码 0，queued，result 与 error 均为 null。"""
        code, _out, record, err = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT
        )
        self.assertEqual(code, 0, (record, err))
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )
        return record["id"]

    def _assert_saved_failed_record(self, job_id):
        """新进程 show：退出码 0，返回与执行响应一致的失败记录。"""
        code, _out, shown, err = self._cli("show", job_id)
        self.assertEqual(code, 0, (shown, err))
        self.assertEqual(
            shown,
            {"id": job_id, **FAILED_RECORD},
        )
        return shown

    # --- 解析失败落库 ---------------------------------------------------

    def test_parser_rejected_field_persists_as_data_error_without_traceback(self):
        self._write_demo(PARSE_ERROR_CSV)
        job_id = self._submit()

        # run：同一 id、failed、result 为 null、error 为 data_error；
        # 退出码 1，标准输出恰好一行 JSON，标准错误不含异常堆栈，
        # 记录里也不出现底层异常文字。
        code, out, record, err = self._cli("run", job_id)
        self.assertEqual(code, 1, (record, err))
        self.assertEqual(record, {"id": job_id, **FAILED_RECORD})
        self.assertEqual(err, "", "不应输出异常堆栈：%r" % err)
        self.assertNotIn("field larger", out)
        self.assertNotIn("Traceback", out)

        # 任务不能留在 queued，也不能保留前面合法行的部分汇总。
        self.assertNotEqual(record["status"], "queued")
        self.assertIsNone(record["result"])

        # 新进程中 show 同一 id：退出码 0，记录与执行响应完全相同。
        shown = self._assert_saved_failed_record(job_id)

        # 再次 run 按原有状态规则拒绝：invalid_state，回显失败记录，
        # 退出码 1。
        code, _out, record, err = self._cli("run", job_id)
        self.assertEqual(code, 1, (record, err))
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "invalid_state"},
        )

        # 拒绝执行不改变已保存记录：后续 show 仍是 data_error。
        code, _out, later, err = self._cli("show", job_id)
        self.assertEqual(code, 0, (later, err))
        self.assertEqual(later, shown)
        self.assertEqual(later["error"], "data_error")

    # --- retry 恢复 -----------------------------------------------------

    def test_parser_failure_recovers_via_retry_after_file_fixed(self):
        self._write_demo(PARSE_ERROR_CSV)
        job_id = self._submit()

        # 首次执行失败并落库。
        code, _out, record, err = self._cli("run", job_id)
        self.assertEqual(code, 1, (record, err))
        self.assertEqual(record, {"id": job_id, **FAILED_RECORD})

        # 把同一路径文件改成表头加一条合法数据后 retry：沿用原 id 回到
        # queued，清空 result 与 error，退出码 0。
        self._write_demo(FIXED_CSV)
        code, _out, record, err = self._cli("retry", job_id)
        self.assertEqual(code, 0, (record, err))
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )

        # 随后的 show 与 retry 响应一致：旧的 data_error 已清空。
        code, _out, shown, err = self._cli("show", job_id)
        self.assertEqual(code, 0, (shown, err))
        self.assertEqual(shown, record)

        # 再次 run 按既有规则成功：行数 1、金额总和 10、类别映射 {"a": 10}。
        code, _out, record, err = self._cli("run", job_id)
        self.assertEqual(code, 0, (record, err))
        self.assertEqual(
            record,
            {"id": job_id, "status": "succeeded", "result": FIXED_EXPECTED, "error": None},
        )

        # 新进程查询得到完全相同的成功结果。
        code, _out, shown, err = self._cli("show", job_id)
        self.assertEqual(code, 0, (shown, err))
        self.assertEqual(shown, record)


if __name__ == "__main__":
    unittest.main()
