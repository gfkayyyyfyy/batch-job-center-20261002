"""带引号 CSV 字段（逗号、转义双引号、字段内换行）的 csv_summary 回归测试。

固定既有行为：类别字段以双引号包围时，字段内的逗号不拆分列、
两个连续双引号还原为一个双引号、字段内换行保留在类别名中且
不构成额外记录；合法行之后出现列数不符的行时，run 落为
failed/data_error 且不保留前一条的部分汇总。

通过 `python -m task_center` 子进程观察完整 JSON 输出与退出码，
并以随后 show 查询的记录为依据，不依赖内部函数或已有任务。
每个场景使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内
各自专用的演示文件，测试结束仅清理自身数据，可连续重复运行，
默认数据库与既有演示数据保持原样。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_csv_quoted_regression -v

或直接：

    python tests/test_csv_quoted_regression.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = PROJECT_ROOT / "demo"

# 两个场景各自专用的演示文件，位于 demo 目录内且不与现有文件冲突；
# 每个场景运行前创建、结束后删除。
SUCCESS_DEMO_FILE = DEMO_DIR / "csv_quoted_regression_success.csv"
SUCCESS_DEMO_INPUT = "demo/csv_quoted_regression_success.csv"
FAILURE_DEMO_FILE = DEMO_DIR / "csv_quoted_regression_failure.csv"
FAILURE_DEMO_INPUT = "demo/csv_quoted_regression_failure.csv"

# 成功场景：UTF-8、LF 换行、category,amount 表头；四条数据记录的
# 类别字段均以双引号包围，依次覆盖逗号、转义双引号（两个连续双引号）
# 与字段内换行（line 与 break 之间一个 LF），最后重复首个类别。
QUOTED_SUCCESS_CSV = (
    "category,amount\n"
    '"a,b",10\n'
    '"say ""hi""",2\n'
    '"line\nbreak",3\n'
    '"a,b",5\n'
)
QUOTED_SUCCESS_EXPECTED = {
    "row_count": 4,
    "total_amount": 20,
    "categories": {"a,b": 15, 'say "hi"': 2, "line\nbreak": 3},
}

# 失败场景：一条合法的带引号逗号类别记录之后，跟随一条列数不符的
# 记录（"a,b",5,extra 解析出三列）。
QUOTED_FAILURE_CSV = (
    "category,amount\n"
    '"a,b",10\n'
    '"a,b",5,extra\n'
)


class CsvQuotedRegressionTest(unittest.TestCase):
    """带引号字段的解析与失败落库：JSON 响应、退出码与 show 记录。"""

    def setUp(self):
        # 独立数据库：每个场景一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_csv_quoted_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        # demo 目录缺失时自行创建，并记录以便结束后清理空目录。
        self._demo_dir_created = not DEMO_DIR.exists()
        DEMO_DIR.mkdir(exist_ok=True)

    def _prepare_demo_file(self, demo_file):
        """专用演示文件若已存在则以 AssertionError 中止并保留原文件。"""
        if demo_file.exists():
            self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % demo_file)
        self.addCleanup(self._remove_demo_file, demo_file)
        return demo_file

    def _remove_demo_file(self, demo_file):
        try:
            demo_file.unlink()
        except FileNotFoundError:
            pass
        # 仅当 demo 目录由本测试创建且已空时才移除。
        if self._demo_dir_created:
            try:
                DEMO_DIR.rmdir()
            except OSError:
                pass

    # --- 辅助方法 -------------------------------------------------------

    def _write_demo(self, demo_file, text):
        # newline="" 保证写入的换行符原样为 LF，不被平台转换。
        with open(demo_file, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def _cli(self, *args):
        """调用 python -m task_center，返回 (退出码, 解析后的 JSON 记录)。"""
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
        return proc.returncode, json.loads(stdout)

    def _submit(self, demo_input):
        """提交合法路径：退出码 0，queued，result 与 error 均为 null。"""
        code, record = self._cli("submit", "csv_summary", "--input", demo_input)
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

    # --- 成功路径：逗号、转义双引号、字段内换行 --------------------------

    def test_quoted_comma_escaped_quotes_and_embedded_newline(self):
        demo_file = self._prepare_demo_file(SUCCESS_DEMO_FILE)
        self._write_demo(demo_file, QUOTED_SUCCESS_CSV)
        job_id = self._submit(SUCCESS_DEMO_INPUT)

        # run 沿用同一 id：退出码 0，succeeded，error 为 null，
        # 结果精确匹配（字段内换行保留在类别名中，且不算额外记录）。
        code, record = self._run(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {
                "id": job_id,
                "status": "succeeded",
                "result": QUOTED_SUCCESS_EXPECTED,
                "error": None,
            },
        )
        categories = record["result"]["categories"]
        self.assertEqual(len(categories), 3, categories)
        self.assertIn("line\nbreak", categories)
        self.assertEqual(record["result"]["row_count"], 4)

        # 新的查询进程读取同一数据库，show 退出 0 且记录与 run 响应相同。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)

    # --- 失败路径：合法带引号记录之后列数不符 ----------------------------

    def test_extra_column_after_valid_quoted_row_fails_without_partial_result(self):
        demo_file = self._prepare_demo_file(FAILURE_DEMO_FILE)
        self._write_demo(demo_file, QUOTED_FAILURE_CSV)
        job_id = self._submit(FAILURE_DEMO_INPUT)

        # run：退出码 1，failed、data_error、result 为 null，
        # 不保存前一条合法记录的部分汇总。
        code, record = self._run(job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )

        # 新的查询进程读取同一数据库，show 退出 0 且记录与 run 响应相同。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)


if __name__ == "__main__":
    unittest.main()
