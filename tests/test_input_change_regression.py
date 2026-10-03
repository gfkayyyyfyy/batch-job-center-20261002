"""提交后输入文件变化的公开行为回归测试。

固定这条已有流程：submit 仅登记 queued 任务，run 读取执行时刻的
文件内容并把最终记录（succeeded/failed）保存下来，show 在任意新
进程中都能查到与执行响应一致的完整记录。

通过 `python -m task_center` 子进程观察标准输出与退出码，每个
场景使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内各自
专用的演示文件；测试结束仅清理本次创建的文件与临时库，既有演示
数据与默认数据库保持原样，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_input_change_regression -v

或直接：

    python tests/test_input_change_regression.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = PROJECT_ROOT / "demo"

# 各场景专用的演示文件，位于 demo 目录内且不与现有文件冲突；
# 每个场景运行前创建、结束后删除；若已存在则直接失败并保留原文件。
SUCCESS_FILE = DEMO_DIR / "input_change_regression_success.csv"
SUCCESS_INPUT = "demo/input_change_regression_success.csv"

DELETED_FILE = DEMO_DIR / "input_change_regression_deleted.csv"
DELETED_INPUT = "demo/input_change_regression_deleted.csv"

BAD_DATA_FILE = DEMO_DIR / "input_change_regression_bad_data.csv"
BAD_DATA_INPUT = "demo/input_change_regression_bad_data.csv"

HEADER = "category,amount\n"
INITIAL_CSV = HEADER + "a,1\n"
# 提交后改写为三条数据：汇总应来自改写后的文件而非提交时的内容。
REWRITTEN_CSV = HEADER + "a,10\nb,20\na,5\n"
REWRITTEN_EXPECTED = {
    "row_count": 3,
    "total_amount": 35,
    "categories": {"a": 15, "b": 20},
}
# 提交后保留表头、数据改为非法金额。
BAD_DATA_CSV = HEADER + "a,x\n"


class InputChangeRegressionTest(unittest.TestCase):
    """提交后输入文件被改写或删除时，run/show 的 JSON 响应与退出码。"""

    def setUp(self):
        # 独立数据库：每个场景一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_input_change_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        # demo 目录缺失时自行创建；目录清理由 addCleanup 按 LIFO 在演示
        # 文件删除之后执行，且仅当目录由本测试创建且已空时才移除。
        if not DEMO_DIR.exists():
            DEMO_DIR.mkdir()
            self.addCleanup(self._remove_demo_dir_if_empty)

    @staticmethod
    def _remove_demo_dir_if_empty():
        try:
            DEMO_DIR.rmdir()
        except OSError:
            pass

    # --- 辅助方法 -------------------------------------------------------

    def _write_demo(self, path, text):
        """创建专用演示文件；若已存在则失败并保留，避免误删他人数据。"""
        if path.exists():
            self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % path)
        path.write_text(text, encoding="utf-8")
        self.addCleanup(self._remove_demo_file, path)

    @staticmethod
    def _remove_demo_file(path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _cli(self, *args):
        """调用 python -m task_center，返回 (退出码, 解析后的 JSON 记录)。

        标准输出必须恰好是一个 JSON 对象（单行）。
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

    def _assert_final_record(self, job_id, run_code, expected):
        """run 响应精确匹配期望记录；随后新进程的 show 返回同一完整记录。"""
        code, record = self._cli("run", job_id)
        self.assertEqual(code, run_code, record)
        self.assertEqual(record, expected)
        # 新的查询进程读取同一数据库，show 退出 0 且记录与执行响应一致：
        # 成功保留完整汇总，失败不留下最初的 queued 状态或部分汇总。
        show_code, shown = self._cli("show", job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, expected)

    # --- 场景 -----------------------------------------------------------

    def test_run_uses_file_content_changed_after_submit(self):
        # 提交时只有一行数据，取得 id 后改写为三行。
        self._write_demo(SUCCESS_FILE, INITIAL_CSV)
        job_id = self._submit(SUCCESS_INPUT)
        SUCCESS_FILE.write_text(REWRITTEN_CSV, encoding="utf-8")
        # run 读取执行时刻的文件内容：同一 id、succeeded、error 为 null。
        self._assert_final_record(
            job_id,
            0,
            {
                "id": job_id,
                "status": "succeeded",
                "result": REWRITTEN_EXPECTED,
                "error": None,
            },
        )

    def test_run_fails_with_input_error_when_file_deleted_after_submit(self):
        self._write_demo(DELETED_FILE, INITIAL_CSV)
        job_id = self._submit(DELETED_INPUT)
        DELETED_FILE.unlink()
        # 提交后删除文件：failed + input_error，退出码 1，result 为 null。
        self._assert_final_record(
            job_id,
            1,
            {"id": job_id, "status": "failed", "result": None, "error": "input_error"},
        )

    def test_run_fails_with_data_error_when_data_becomes_invalid(self):
        self._write_demo(BAD_DATA_FILE, INITIAL_CSV)
        job_id = self._submit(BAD_DATA_INPUT)
        BAD_DATA_FILE.write_text(BAD_DATA_CSV, encoding="utf-8")
        # 提交后数据变非法：failed + data_error，退出码 1，result 为 null。
        self._assert_final_record(
            job_id,
            1,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )


if __name__ == "__main__":
    unittest.main()
