"""提交后输入文件变化（run 固定使用执行时文件内容）的公开行为回归测试。

通过 `python -m task_center` 子进程观察标准输出中的单个 JSON 对象与
退出码，每个用例使用独立的临时 SQLite 数据库（--db 指定）和 demo
目录内专用的演示文件。模块开始准备时若 demo 目录不存在会自行创建，
结束时仅在该目录由本模块创建且已经为空的情况下移除；测试结束仅清理
自身数据，可连续重复运行。

覆盖行为：提交只登记 queued 记录、不读取文件内容；run 在执行时读取
当时的文件内容并把最终记录（成功汇总或失败错误码）保存下来，随后以
新的命令进程 show 得到完全一致的完整记录，不会残留 queued 状态或
部分汇总。

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

# 测试自备的演示文件，位于 demo 目录内且不与现有文件冲突；
# 每个用例运行前创建、结束后删除。
DEMO_FILE = DEMO_DIR / "input_change_regression_case.csv"
DEMO_INPUT = "demo/input_change_regression_case.csv"

# demo 目录是否在本模块开始准备前就已存在。该判断在导入时记录：
# 联合运行时 unittest 先导入命令行上的全部模块再依次执行，因此两个
# 顺序执行的模块各自保留独立结论，互不串扰。
_demo_preexisting = DEMO_DIR.exists()


def setUpModule():
    """准备演示目录与样例生命周期的前置条件。

    demo 目录不存在时由测试自行创建；专用样例若已存在则直接令本模块
    失败（失败信息包含冲突路径）并保留该文件，绝不覆盖或删除。
    """
    if DEMO_FILE.exists():
        raise AssertionError(
            "演示样例已存在，为避免覆盖或误删他人数据而中止：%s" % DEMO_FILE
        )
    # 仅创建 demo 自身；项目根目录必然已存在。
    DEMO_DIR.mkdir(exist_ok=True)


def tearDownModule():
    """回收本模块自建数据。

    删除专用样例（样例已在用例中被删除时不报错）；仅当 demo 目录由
    本模块创建且删除样例后已经为空时才移除目录，目录中仍有其他文件
    （含既有演示数据、tasks.sqlite3 等非测试文件）时一律保留。
    """
    try:
        DEMO_FILE.unlink()
    except FileNotFoundError:
        pass
    if not _demo_preexisting:
        try:
            DEMO_DIR.rmdir()
        except OSError:
            # 目录非空或已被移除：非空说明含非本测试文件，保留原样。
            pass


# 提交时文件只有一行数据；run 之前再改写为另外三行，证明 run 固定
# 使用执行时的文件内容而非提交时的内容。
INITIAL_CSV = "category,amount\na,1\n"
CHANGED_CSV = "category,amount\na,10\nb,20\na,5\n"
EXPECTED_RESULT = {"row_count": 3, "total_amount": 35, "categories": {"a": 15, "b": 20}}

# 保留表头、仅把数据改成非法金额，执行时应得到 data_error。
BAD_AMOUNT_CSV = "category,amount\na,x\n"


class InputChangeRegressionTest(unittest.TestCase):
    """提交后输入文件变化：run 的 JSON 响应、退出码与最终记录留存。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_input_change_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        # 样例冲突已在 setUpModule 拒绝；此处登记清理，保证断言失败后
        # 也只回收本测试自己写入的样例文件。
        self.addCleanup(self._remove_demo_file)

    def _remove_demo_file(self):
        try:
            DEMO_FILE.unlink()
        except FileNotFoundError:
            pass

    # --- 辅助方法 -------------------------------------------------------

    def _write_demo(self, text):
        DEMO_FILE.write_text(text, encoding="utf-8")

    def _cli(self, *args):
        """调用 python -m task_center，返回 (退出码, 解析后的 JSON 记录)。

        标准输出中必须恰好有一个 JSON 对象（不允许多余的输出行）。
        """
        proc = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", self.db, *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(
            len(lines), 1, "标准输出应只有一个 JSON 对象：%r" % proc.stdout
        )
        record = json.loads(lines[0])
        return proc.returncode, record

    def _submit_queued(self):
        """提交当前演示文件：退出码 0，queued，result 与 error 均为 null。"""
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

    def _assert_show_matches(self, job_id, expected):
        """以新的命令进程 show：退出码 0，返回与执行响应一致的完整记录。"""
        code, record = self._show(job_id)
        self.assertEqual(code, 0, record)
        self.assertEqual(record, expected)

    # --- 场景 -----------------------------------------------------------

    def test_run_uses_changed_file_content_and_persists_summary(self):
        """提交只有 a,1 一行，run 前改成三行：结果必须来自改写后的文件。"""
        self._write_demo(INITIAL_CSV)
        job_id = self._submit_queued()

        # 提交后、执行前把数据改为三行（金额合计 35）。
        self._write_demo(CHANGED_CSV)

        code, record = self._run(job_id)
        self.assertEqual(code, 0, record)
        expected = {
            "id": job_id,
            "status": "succeeded",
            "result": EXPECTED_RESULT,
            "error": None,
        }
        self.assertEqual(record, expected)

        # 新进程查询到同一条最终记录：没有残留 queued 或旧的一行汇总。
        self._assert_show_matches(job_id, expected)

    def test_run_after_file_deleted_fails_input_error_and_record_persists(self):
        """提交后删除文件：run 返回 failed/input_error，失败记录可查询。"""
        self._write_demo(INITIAL_CSV)
        job_id = self._submit_queued()

        DEMO_FILE.unlink()

        code, record = self._run(job_id)
        self.assertEqual(code, 1, record)
        expected = {
            "id": job_id,
            "status": "failed",
            "result": None,
            "error": "input_error",
        }
        self.assertEqual(record, expected)

        # 失败记录同样在新进程中可查询，且与执行响应完全一致。
        self._assert_show_matches(job_id, expected)

    def test_run_after_data_changed_invalid_fails_data_error_and_record_persists(self):
        """提交后把数据改成 a,x（保留表头）：run 返回 failed/data_error。"""
        self._write_demo(INITIAL_CSV)
        job_id = self._submit_queued()

        self._write_demo(BAD_AMOUNT_CSV)

        code, record = self._run(job_id)
        self.assertEqual(code, 1, record)
        expected = {
            "id": job_id,
            "status": "failed",
            "result": None,
            "error": "data_error",
        }
        self.assertEqual(record, expected)

        # 失败记录同样在新进程中可查询，且与执行响应完全一致。
        self._assert_show_matches(job_id, expected)


if __name__ == "__main__":
    unittest.main()
