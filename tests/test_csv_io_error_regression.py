"""CSV 文件读取异常（OSError）时的公开行为回归测试。

在 input_error 既有处理之上补充两个场景：输入仍是 demo 目录内的
普通文件（执行前的路径检查通过），但
- 打开文件时发生 OSError；
- 已读取表头和一条合法数据行后，继续读取时发生 OSError。

两种情况下 run 都应落库 failed/input_error：退出码 1，标准输出只有
一个 JSON 对象，id 沿用原值，result 为 null，不返回 data_error，也
不暴露底层异常文字；中途失败不得留下已读金额（10）的部分汇总。
故障仅注入执行进程，解除后由新的查询进程 show 同一数据库：退出码 0，
完整记录与 run 的失败响应相同，查询不重新执行汇总、不更改保存值。

故障注入通过 PYTHONPATH 指向的临时 sitecustomize 模块实现：仅对测试
专用的演示文件路径生效，并把触发进度写入标记文件，供测试确认故障
位置（打开时 / 已读出表头与数据行之后）。不依赖预置任务、系统权限
差异或外部服务，只使用 Python 3 标准库。

运行方式（项目根目录下）：

    python -m unittest tests.test_csv_io_error_regression -v

或直接：

    python tests/test_csv_io_error_regression.py
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = PROJECT_ROOT / "demo"

# 各场景专用的演示文件：位于 demo 目录内且不与现有文件冲突；
# 每个场景运行前创建、结束后删除；若已存在则直接失败并保留原文件。
OPEN_FAIL_FILE = DEMO_DIR / "csv_io_error_open_regression.csv"
OPEN_FAIL_INPUT = "demo/csv_io_error_open_regression.csv"

READ_FAIL_FILE = DEMO_DIR / "csv_io_error_read_regression.csv"
READ_FAIL_INPUT = "demo/csv_io_error_read_regression.csv"

# 两个场景共用同一份合法内容：表头加一条金额为 10 的数据行。
CSV_TEXT = "category,amount\na,10\n"

FAILED_RECORD = {"status": "failed", "result": None, "error": "input_error"}

# 注入的底层异常文字：绝不允许出现在公开输出中。
INJECTED_MESSAGE = "injected io failure for regression test"

# 注入模块源码：由 PYTHONPATH 指向的临时目录提供，Python 启动时自动
# 加载；仅当设置了对应环境变量时对目标文件制造 OSError，并在触发前
# 把进度写入标记文件。open 模式在打开时失败；read 模式正常打开，但
# 包装后的文件对象在返回表头与数据行（共 2 行）之后抛出 OSError。
SITECUSTOMIZE_SOURCE = '''
"""回归测试故障注入：对指定文件的打开/读取制造 OSError。"""

import builtins
import os

_FAULT_PATH = os.environ.get("CSV_IO_REGRESSION_FAULT_PATH")
_FAULT_MODE = os.environ.get("CSV_IO_REGRESSION_FAULT_MODE")
_MARKER = os.environ.get("CSV_IO_REGRESSION_FAULT_MARKER")
_READ_LIMIT = 2
_REAL_OPEN = builtins.open


def _mark(text):
    if _MARKER:
        with _REAL_OPEN(_MARKER, "w", encoding="ascii") as fh:
            fh.write(text)


class _FailAfterLines:
    """包装文本文件对象：前若干行正常返回，随后抛出 OSError。"""

    def __init__(self, fh):
        self._fh = fh
        self._served = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._served >= _READ_LIMIT:
            _mark("read-failure-after-%d" % self._served)
            raise OSError(__INJECTED_MESSAGE__)
        line = self._fh.readline()
        if line == "":
            raise StopIteration
        self._served += 1
        _mark("served-%d" % self._served)
        return line

    def __getattr__(self, name):
        return getattr(self._fh, name)


if _FAULT_PATH and _FAULT_MODE:

    def _guarded_open(file, *args, **kwargs):
        try:
            target = os.path.realpath(os.fspath(file))
        except TypeError:
            target = None
        if target != _FAULT_PATH:
            return _REAL_OPEN(file, *args, **kwargs)
        if _FAULT_MODE == "open":
            _mark("open-failure")
            raise OSError(__INJECTED_MESSAGE__)
        return _FailAfterLines(_REAL_OPEN(file, *args, **kwargs))

    builtins.open = _guarded_open
'''.replace("__INJECTED_MESSAGE__", repr(INJECTED_MESSAGE))


class CsvIoErrorRegressionTest(unittest.TestCase):
    """打开失败与读取中途失败都落为可查询的 failed/input_error 记录。"""

    def setUp(self):
        # 独立数据库与注入模块目录：每个场景一套临时目录，互不影响，
        # 也不污染默认数据库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_csv_io_error_test_")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.db = str(tmp / "tasks.sqlite3")
        self.marker = tmp / "fault_marker.txt"
        # 注入模块：通过 PYTHONPATH 让执行子进程在启动时自动加载。
        fault_dir = tmp / "fault_injection"
        fault_dir.mkdir()
        self._fault_dir = fault_dir
        (fault_dir / "sitecustomize.py").write_text(
            SITECUSTOMIZE_SOURCE, encoding="utf-8"
        )
        # demo 目录缺失时自行创建；仅当由本测试创建且已空时才移除。
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

    def _write_demo(self, path):
        """创建专用演示文件；若已存在则失败并保留，避免误删他人数据。"""
        if path.exists():
            self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % path)
        path.write_text(CSV_TEXT, encoding="utf-8")
        self.addCleanup(self._remove_demo_file, path)

    @staticmethod
    def _remove_demo_file(path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _cli(self, *args, extra_env=None):
        """调用 python -m task_center，返回 (退出码, stdout 文本, 解析后的 JSON, stderr)。

        标准输出必须恰好是一个 JSON 对象（单行）。
        """
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", self.db, *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            env=env,
        )
        stdout = proc.stdout.strip()
        self.assertTrue(stdout, "CLI 应输出一行 JSON：%r" % proc.stderr)
        self.assertEqual(
            len(stdout.splitlines()), 1, "标准输出应只有一个 JSON 对象：%r" % proc.stdout
        )
        return proc.returncode, stdout, json.loads(stdout), proc.stderr

    def _fault_env(self, mode, demo_file):
        """构造仅对本次执行进程生效的故障注入环境。"""
        pythonpath = [str(self._fault_dir)]
        existing = os.environ.get("PYTHONPATH")
        if existing:
            pythonpath.append(existing)
        return {
            "PYTHONPATH": os.pathsep.join(pythonpath),
            "CSV_IO_REGRESSION_FAULT_PATH": os.path.realpath(demo_file),
            "CSV_IO_REGRESSION_FAULT_MODE": mode,
            "CSV_IO_REGRESSION_FAULT_MARKER": str(self.marker),
        }

    def _submit(self, demo_input):
        """提交合法路径：退出码 0，queued，result 与 error 均为 null。"""
        code, _out, record, err = self._cli(
            "submit", "csv_summary", "--input", demo_input
        )
        self.assertEqual(code, 0, (record, err))
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )
        return record["id"]

    def _assert_failed_run(self, job_id, extra_env):
        """带故障执行 run：failed/input_error，退出码 1，不暴露底层异常。"""
        code, out, record, err = self._cli("run", job_id, extra_env=extra_env)
        self.assertEqual(code, 1, (record, err))
        # id 沿用原值，status 为 failed，result 为 null，error 为
        # input_error：不是 data_error，也不残留 queued 或部分汇总。
        self.assertEqual(record, {"id": job_id, **FAILED_RECORD})
        self.assertEqual(err, "", "不应输出异常堆栈或底层异常文字：%r" % err)
        self.assertNotIn(INJECTED_MESSAGE, out)
        self.assertNotIn("OSError", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("data_error", out)
        return record

    def _assert_saved_record(self, job_id, run_record):
        """解除故障后由新进程 show：退出码 0，完整记录与失败响应相同。

        演示文件内容合法，若查询重新执行汇总会得到 succeeded；返回
        failed/input_error 即证明 show 只读取已保存记录、不更改保存值。
        """
        code, _out, shown, err = self._cli("show", job_id)
        self.assertEqual(code, 0, (shown, err))
        self.assertEqual(shown, run_record)

    def _marker(self):
        self.assertTrue(self.marker.exists(), "故障注入未按预期触发")
        return self.marker.read_text(encoding="ascii")

    # --- 场景 -----------------------------------------------------------

    def test_open_failure_persists_input_error(self):
        self._write_demo(OPEN_FAIL_FILE)
        job_id = self._submit(OPEN_FAIL_INPUT)

        # 打开文件时即抛出 OSError：failed/input_error，退出码 1。
        record = self._assert_failed_run(
            job_id, self._fault_env("open", OPEN_FAIL_FILE)
        )
        # 标记确认故障确实发生在打开阶段，而非路径检查或读取阶段。
        self.assertEqual(self._marker(), "open-failure")

        self._assert_saved_record(job_id, record)

    def test_read_failure_after_valid_rows_persists_input_error(self):
        self._write_demo(READ_FAIL_FILE)
        job_id = self._submit(READ_FAIL_INPUT)

        # 表头与合法数据行读取成功后，继续读取时抛出 OSError。
        record = self._assert_failed_run(
            job_id, self._fault_env("read", READ_FAIL_FILE)
        )
        # 标记证明 2 行（表头 + 数据行）已被读取后故障才触发：不是打开
        # 失败；金额为 10 的数据行虽已被读取，result 仍必须为 null，
        # 不得留下部分汇总。
        self.assertEqual(self._marker(), "read-failure-after-2")
        self.assertIsNone(record["result"])

        self._assert_saved_record(job_id, record)


if __name__ == "__main__":
    unittest.main()
