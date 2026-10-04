"""amount 金额文本边界的公开行为回归测试。

围绕 csv_summary 对金额字段唯一公开的文本规则：amount 去除首尾空白后
仅接受 ASCII 数字串（[0-9]+，按非负整数汇总）。在不改变既有汇总规则
的前提下，固定以下边界行为：前导零与金额两端空白仍可接受；固定正号
（+2）、指数写法（1e2）、非 ASCII 数字（全角数字、阿拉伯-印度数字）、
ASCII 与非 ASCII 数字混排、以及仅含空白的金额一律拒绝。

通过 `python -m task_center` 子进程观察 JSON 输出、退出码与后续查询
记录，不以内部正则表达式或私有函数作为验证对象。每个用例使用独立的
临时 SQLite 数据库（--db 指定）和 demo 目录内专用的演示文件，测试
结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_amount_format -v

或直接：

    python tests/test_amount_format.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = PROJECT_ROOT / "demo"

# 成功样例：UTF-8 文件，表头 category,amount；金额依次为前导零 000、
# 两端带空格的 " 010 " 与普通数字 2。
SUCCESS_FILE = "amount_format_success_case.csv"
SUCCESS_CSV = "category,amount\na,000\na, 010 \nb,2\n"
SUCCESS_EXPECTED = {
    "row_count": 3,
    "total_amount": 12,
    "categories": {"a": 10, "b": 2},
}

# 拒绝样例：每份文件先放一条合法数据 a,10（合法前缀），再放类别为 b
# 的待测金额。期望 run 整体失败且不留下合法前缀的部分汇总。
# 各用例使用独立的 demo 文件名：全角数字 ２ 为 U+FF12，
# 阿拉伯-印度数字 ٢ 为 U+0662，混排样例为 ASCII 数字 1 后接全角 ２。


def _reject_csv(amount):
    """合法前缀 a,10 之后跟一条类别 b、金额为 amount 的待测数据。"""
    return "category,amount\na,10\nb,%s\n" % amount


class AmountFormatTest(unittest.TestCase):
    """amount 文本边界的公开行为：提交、执行结果/退出码与跨进程查询。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_amount_format_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        # demo 目录缺失时自行创建，并记录以便结束后清理空目录。
        self._demo_dir_created = not DEMO_DIR.exists()
        DEMO_DIR.mkdir(exist_ok=True)
        # 记录本用例实际创建的样例文件，结束时仅删除这些文件。
        self._created_files = []
        self.addCleanup(self._cleanup_demo)

    def _cleanup_demo(self):
        for name in self._created_files:
            try:
                (DEMO_DIR / name).unlink()
            except FileNotFoundError:
                pass
        # 仅当 demo 目录由本测试创建且已空时才移除；原有演示文件保持原样。
        if self._demo_dir_created:
            try:
                DEMO_DIR.rmdir()
            except OSError:
                pass

    # --- 辅助方法 -------------------------------------------------------

    def _prepare_demo(self, name, text):
        """在 demo 目录内写入 UTF-8 样例；同名文件已存在时直接失败且不改写。"""
        path = DEMO_DIR / name
        if path.exists():
            self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % path)
        path.write_text(text, encoding="utf-8")
        self._created_files.append(name)
        return "demo/" + name

    def _cli(self, *args):
        """另起进程调用 python -m task_center，返回 (退出码, 解析后的 JSON 记录)。"""
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
        """提交：退出码 0，queued，result 与 error 均为 null；返回任务 id。"""
        code, record = self._cli("submit", "csv_summary", "--input", demo_input)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )
        return record["id"]

    # --- 成功样例 -------------------------------------------------------

    def test_leading_zeros_and_surrounding_spaces_accepted(self):
        demo_input = self._prepare_demo(SUCCESS_FILE, SUCCESS_CSV)
        job_id = self._submit(demo_input)

        # run 返回原 id、succeeded、error 为 null，结果精确相等。
        code, record = self._cli("run", job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "succeeded", "result": SUCCESS_EXPECTED, "error": None},
        )

        # 另起查询进程：show 退出码 0，成功记录原样返回。
        show_code, shown = self._cli("show", job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)

    # --- 拒绝样例 -------------------------------------------------------

    def _assert_amount_rejected(self, filename, amount):
        demo_input = self._prepare_demo(filename, _reject_csv(amount))
        # 提交只校验类型与路径：样例仍成功排队。
        job_id = self._submit(demo_input)

        # run 返回同一 id、failed、result 为 null、error 为 data_error，
        # 退出码 1；合法前缀 a,10 不得留下任何部分汇总。
        code, record = self._cli("run", job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )
        self.assertIsNone(record["result"], "失败不得保留部分汇总：%r" % record)

        # 另起查询进程：记录中的 data_error 不是查询失败，
        # show 退出码 0 且原样返回同一失败记录。
        show_code, shown = self._cli("show", job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, record)

    def test_plus_sign_amount_rejected(self):
        self._assert_amount_rejected("amount_format_plus_sign_case.csv", "+2")

    def test_exponent_amount_rejected(self):
        self._assert_amount_rejected("amount_format_exponent_case.csv", "1e2")

    def test_fullwidth_digit_amount_rejected(self):
        self._assert_amount_rejected("amount_format_fullwidth_case.csv", "２")

    def test_arabic_indic_digit_amount_rejected(self):
        self._assert_amount_rejected("amount_format_arabic_indic_case.csv", "٢")

    def test_mixed_ascii_and_fullwidth_digits_amount_rejected(self):
        self._assert_amount_rejected("amount_format_mixed_digits_case.csv", "1２")

    def test_spaces_only_amount_rejected(self):
        self._assert_amount_rejected("amount_format_spaces_only_case.csv", "   ")


if __name__ == "__main__":
    unittest.main()
