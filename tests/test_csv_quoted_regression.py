"""csv_summary 对带引号字段（逗号、转义双引号、字段内换行）的既有行为回归测试。

固定标准 csv 解析下三类合法引号写法的汇总结果，并固定“带引号的
合法记录之后再出现多余列”时整体失败、不留部分汇总的行为。

通过 `python -m task_center` 子进程观察完整 JSON 输出、退出码及随后
show 查询落库的记录，不依赖内部函数或已有任务。每个用例使用独立的
临时 SQLite 数据库（--db 指定）和 demo 目录内专用的演示文件，测试
结束仅清理自身创建的文件与临时库，可连续重复运行。

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

# 测试自备的演示文件，位于 demo 目录内且不与现有文件冲突；
# 每个用例运行前创建、结束后删除。
DEMO_FILE = DEMO_DIR / "csv_quoted_regression_case.csv"
DEMO_INPUT = "demo/csv_quoted_regression_case.csv"

# 成功用例（以字节写入，强制 UTF-8 与 LF 行尾）：
#   表头                category,amount
#   类别含逗号          "a,b",10
#   转义双引号          "say ""hi""",2
#   字段内一个 LF 换行   "line\nbreak",3
#   逗号类别再次出现     "a,b",5
# 字段内换行属于该字段内容，不能算作额外记录。
QUOTED_CSV = (
    b'category,amount\n'
    b'"a,b",10\n'
    b'"say ""hi""",2\n'
    b'"line\nbreak",3\n'
    b'"a,b",5\n'
)
QUOTED_EXPECTED = {
    "row_count": 4,
    "total_amount": 20,
    "categories": {
        "a,b": 15,
        'say "hi"': 2,
        "line\nbreak": 3,
    },
}

# 失败用例：一条合法的带引号逗号类别记录之后，出现三列的多余字段行
# （"a,b",5,extra 解析为 ["a,b", "5", "extra"]），整体必须失败。
EXTRA_COLUMN_CSV = (
    b'category,amount\n'
    b'"a,b",5\n'
    b'"a,b",5,extra\n'
)


class CsvQuotedRegressionTest(unittest.TestCase):
    """带引号字段的公开行为：完整 JSON 响应、退出码与 show 落库记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_csv_quoted_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        # demo 目录缺失时自行创建，并记录以便结束后清理空目录。
        self._demo_dir_created = not DEMO_DIR.exists()
        DEMO_DIR.mkdir(exist_ok=True)
        # 专用演示文件若已存在则以 AssertionError 中止并保留原文件，
        # 绝不覆盖他人数据。
        if DEMO_FILE.exists():
            raise AssertionError(
                "专用演示文件已存在，为避免误删他人数据而中止：%s" % DEMO_FILE
            )
        self.addCleanup(self._remove_demo_file)

    def _remove_demo_file(self):
        # 仅清理本模块自建的演示文件；断言失败时清理逻辑依旧执行。
        try:
            DEMO_FILE.unlink()
        except FileNotFoundError:
            pass
        # 仅当 demo 目录由本次测试创建且已空时才移除。
        if self._demo_dir_created:
            try:
                DEMO_DIR.rmdir()
            except OSError:
                pass

    # --- 辅助方法 -------------------------------------------------------

    def _write_demo_bytes(self, data):
        # 以字节写入，保证 UTF-8 编码与 LF 行尾不受平台影响。
        DEMO_FILE.write_bytes(data)

    def _cli(self, *args):
        """调用 python -m task_center，返回 (退出码, 解析后的 JSON 记录, stderr)。

        同时固定标准输出恰为一行 JSON（print 自带的行尾除外）。
        """
        proc = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", self.db, *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        stdout = proc.stdout.strip()
        self.assertTrue(stdout, "CLI 应输出一行 JSON：%r" % proc.stderr)
        self.assertEqual(
            len(stdout.splitlines()),
            1,
            "标准输出应只有一行 JSON：%r" % proc.stdout,
        )
        return proc.returncode, json.loads(stdout), proc.stderr

    def _submit(self):
        """提交合法路径：退出码 0，queued，result 与 error 均为 null。"""
        code, record, err = self._cli(
            "submit", "csv_summary", "--input", DEMO_INPUT
        )
        self.assertEqual(code, 0, (record, err))
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )
        return record["id"]

    def _show(self, job_id):
        code, record, err = self._cli("show", job_id)
        self.assertEqual(code, 0, (record, err))
        return record

    # --- 成功路径：引号内逗号、转义双引号、字段内换行 -------------------

    def test_quoted_comma_escaped_quote_and_embedded_newline(self):
        self._write_demo_bytes(QUOTED_CSV)
        job_id = self._submit()

        # run 沿用同一 id：退出码 0，succeeded，error 为 null，
        # 结果为解析后的完整 JSON 值比较。
        code, record, err = self._cli("run", job_id)
        self.assertEqual(code, 0, (record, err))
        self.assertEqual(
            record,
            {"id": job_id, "status": "succeeded", "result": QUOTED_EXPECTED, "error": None},
        )

        result = record["result"]
        # 字段内换行保留在类别名中，四个物理数据行仍是四条记录，
        # 换行不得算作额外记录。
        self.assertEqual(result["row_count"], 4)
        self.assertEqual(result["total_amount"], 20)
        self.assertEqual(
            set(result["categories"]),
            {"a,b", 'say "hi"', "line\nbreak"},
        )
        self.assertEqual(result["categories"]["a,b"], 15)
        self.assertEqual(result["categories"]['say "hi"'], 2)
        self.assertEqual(result["categories"]["line\nbreak"], 3)

        # 新进程查询同一数据库：show 退出 0，完整记录与 run 响应相同。
        shown = self._show(job_id)
        self.assertEqual(shown, record)

    # --- 失败路径：多余列导致整体 data_error，不留部分汇总 --------------

    def test_extra_column_after_quoted_valid_row_fails_without_partial_result(self):
        self._write_demo_bytes(EXTRA_COLUMN_CSV)
        job_id = self._submit()

        # run：退出码 1，同一 id、failed、data_error、result 为 null，
        # 标准错误不含异常堆栈。
        code, record, err = self._cli("run", job_id)
        self.assertEqual(code, 1, (record, err))
        self.assertEqual(err, "", "不应输出异常堆栈：%r" % err)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )

        # 前一条合法记录的部分汇总不得保存。
        self.assertIsNone(record["result"])

        # 新进程 show：退出 0，完整记录与 run 响应相同。
        shown = self._show(job_id)
        self.assertEqual(shown, record)


if __name__ == "__main__":
    unittest.main()
