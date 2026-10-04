"""csv_summary 金额文本边界的公开行为回归测试。

固定公开规则：amount 去除首尾空白后仅接受 ASCII 数字串。前导零、
负数与小数已由其他回归测试覆盖，本文件只补充正号、指数写法、
非 ASCII 数字（全角数字、阿拉伯印度数字、混合数字）以及仅含空白
的金额的拒绝行为，并确认合法样例（含两端带空格的前导零金额）的
精确汇总结果。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，不以内部
正则表达式或私有函数为验证对象。每个场景使用独立的临时 SQLite
数据库（--db 指定）和 demo 目录内专用的演示文件；同名演示文件已
存在时用例失败且原文件保持不变，测试结束仅清理自身创建的数据，
可连续重复运行。

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

# 合法样例：首条金额带前导零，第二条金额两端均有空格且带前导零，
# 覆盖"去除首尾空白后仅接受 ASCII 数字串"规则的接受一侧。
OK_CSV = "category,amount\na,000\na, 010 \nb,2\n"
OK_EXPECTED = {
    "row_count": 3,
    "total_amount": 12,
    "categories": {"a": 10, "b": 2},
}

# 拒绝样例：每份文件先放一条合法数据 a,10，再放类别为 b 的待测金额，
# 确认失败不留下合法前缀的部分汇总。键为场景名，值为待测金额文本。
REJECT_AMOUNTS = {
    "plus_sign": "+2",
    "exponent": "1e2",
    "fullwidth_digit": "２",
    "arabic_indic_digit": "٢",
    "mixed_digits": "1２",
    "spaces_only": "   ",
}


class AmountFormatTest(unittest.TestCase):
    """金额格式规则的公开行为：JSON 响应、退出码与查询记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_amount_format_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        # demo 目录缺失时自行创建，并记录以便结束后清理空目录。
        self._demo_dir_created = not DEMO_DIR.exists()
        DEMO_DIR.mkdir(exist_ok=True)
        self.addCleanup(self._remove_demo_dir_if_created)
        # 本用例创建的演示文件，结束后仅清理这些文件。
        self._demo_files = []
        self.addCleanup(self._remove_demo_files)

    def _remove_demo_dir_if_created(self):
        # 仅当 demo 目录由本测试创建且已空时才移除。
        if self._demo_dir_created:
            try:
                DEMO_DIR.rmdir()
            except OSError:
                pass

    def _remove_demo_files(self):
        for path in self._demo_files:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # --- 辅助方法 -------------------------------------------------------

    def _write_demo(self, name, text):
        """在 demo 目录内准备演示文件；同名文件已存在时用例失败且原文件不变。"""
        path = DEMO_DIR / name
        if path.exists():
            self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % path)
        path.write_text(text, encoding="utf-8")
        self._demo_files.append(path)
        return "demo/" + name

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

    def _submit(self, demo_input):
        """提交合法路径：退出码 0，queued，result 与 error 均为 null。"""
        code, record = self._cli("submit", "csv_summary", "--input", demo_input)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )
        return record["id"]

    def _assert_show_matches(self, expected_record):
        """另起查询进程：show 退出码 0，原样返回同一记录（含失败记录）。"""
        code, shown = self._cli("show", expected_record["id"])
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, expected_record)

    # --- 成功路径 -------------------------------------------------------

    def test_ascii_digits_with_surrounding_spaces_accepted(self):
        demo_input = self._write_demo("amount_format_ok.csv", OK_CSV)
        job_id = self._submit(demo_input)
        code, record = self._cli("run", job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(
            record,
            {"id": job_id, "status": "succeeded", "result": OK_EXPECTED, "error": None},
        )
        self._assert_show_matches(record)

    # --- 拒绝路径：submit 排队，run 才报 data_error ----------------------

    def _assert_rejected(self, name, amount_text):
        csv_text = "category,amount\na,10\nb,%s\n" % amount_text
        demo_input = self._write_demo("amount_format_reject_%s.csv" % name, csv_text)
        job_id = self._submit(demo_input)
        code, record = self._cli("run", job_id)
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record, {"id": job_id, "status": "failed", "result": None, "error": "data_error"}
        )
        # 失败不保留合法前缀的部分汇总，result 为 null。
        self.assertIsNone(record["result"], record)
        # 查询进程不把记录中的数据错误当作查询失败：退出码 0，原样返回。
        self._assert_show_matches(record)

    def test_plus_sign_rejected(self):
        self._assert_rejected("plus_sign", REJECT_AMOUNTS["plus_sign"])

    def test_exponent_notation_rejected(self):
        self._assert_rejected("exponent", REJECT_AMOUNTS["exponent"])

    def test_fullwidth_digit_rejected(self):
        self._assert_rejected("fullwidth_digit", REJECT_AMOUNTS["fullwidth_digit"])

    def test_arabic_indic_digit_rejected(self):
        self._assert_rejected("arabic_indic_digit", REJECT_AMOUNTS["arabic_indic_digit"])

    def test_mixed_digits_rejected(self):
        self._assert_rejected("mixed_digits", REJECT_AMOUNTS["mixed_digits"])

    def test_spaces_only_amount_rejected(self):
        self._assert_rejected("spaces_only", REJECT_AMOUNTS["spaces_only"])


if __name__ == "__main__":
    unittest.main()
