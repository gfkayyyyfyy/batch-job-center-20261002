"""CSV 打开失败与读取中途失败（OSError）时的公开行为回归测试。

在既有 input_error 处理（如提交后删除文件的路径检查用例）之上补充
文件读取异常路径：输入仍是 demo 目录内的普通文件，submit 与 run 的
路径检查都通过，但 open 本身失败、或已读出表头和一条合法数据行之后
继续读取失败。两种 OSError 都必须走既有失败路径：run 退出码 1、
failed/input_error 落库、不暴露底层异常文字；解除故障后由新进程的
show 读到与执行响应一致的完整记录，且不重新执行汇总。

故障注入不依赖系统权限差异或外部服务：测试在临时目录生成一个
sitecustomize.py，仅对目标文件路径包装 builtins.open，通过
PYTHONPATH 与几个 CSV_IO_ERROR_* 环境变量传给 `python -m task_center`
子进程；submit 与 show 子进程不携带这些变量，行为与正常调用完全一致。

每个场景使用独立的临时 SQLite 数据库（--db 指定）和 demo 目录内各自
专用的演示文件；测试结束仅清理本次创建的文件与临时库，既有演示数据
与默认数据库保持原样，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

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

# 各场景专用的演示文件，位于 demo 目录内且不与现有文件冲突；
# 每个场景运行前创建、结束后删除；若已存在则直接失败并保留原文件。
OPEN_FILE = DEMO_DIR / "csv_io_error_regression_open.csv"
OPEN_INPUT = "demo/csv_io_error_regression_open.csv"

MIDREAD_FILE = DEMO_DIR / "csv_io_error_regression_midread.csv"
MIDREAD_INPUT = "demo/csv_io_error_regression_midread.csv"

# 表头加一条合法数据行：中途失败场景在读出这两行之后才触发 OSError。
CSV_TEXT = "category,amount\na,10\n"

FAILED_RECORD = {"status": "failed", "result": None, "error": "input_error"}

# 注入子进程的 sitecustomize：仅当 CSV_IO_ERROR_TARGET 与
# CSV_IO_ERROR_MODE 同时存在时，对目标路径包装 builtins.open；
# 其余路径一律走原始 open，不影响解释器与任务中心自身行为。
SHIM_SOURCE = '''\
"""测试注入的 sitecustomize：仅对指定文件模拟打开/读取 OSError。

mode=open 时 open(目标) 直接抛 OSError；mode=midread 时 open 成功，
但迭代提供前 CSV_IO_ERROR_SERVE_LINES 行之后抛 OSError，并把已提供
的行数写入 CSV_IO_ERROR_MARKER（供测试确认读取进度）。
"""

import builtins
import os

_TARGET = os.environ.get("CSV_IO_ERROR_TARGET")
_MODE = os.environ.get("CSV_IO_ERROR_MODE")

if _TARGET and _MODE:
    _SERVE_LINES = int(os.environ.get("CSV_IO_ERROR_SERVE_LINES", "2"))
    _MARKER = os.environ.get("CSV_IO_ERROR_MARKER") or None
    _real_open = builtins.open

    def _is_target(file):
        try:
            return os.path.realpath(file) == _TARGET
        except (OSError, TypeError, ValueError):
            return False

    class _MidReadFailFile:
        """包装真实文本文件：正常提供前若干行，之后抛 OSError。"""

        def __init__(self, fh):
            self._fh = fh
            self._remaining = _SERVE_LINES
            self._served = 0
            self._recorded = False

        def __iter__(self):
            return self

        def __next__(self):
            if self._remaining <= 0:
                self._record_served()
                raise OSError("simulated mid-read failure")
            line = self._fh.readline()
            if line == "":
                raise StopIteration
            self._remaining -= 1
            self._served += 1
            return line

        def _record_served(self):
            if self._recorded or _MARKER is None:
                return
            self._recorded = True
            with _real_open(_MARKER, "w", encoding="utf-8") as marker_fh:
                marker_fh.write(str(self._served))

        def close(self):
            return self._fh.close()

        def __getattr__(self, name):
            return getattr(self._fh, name)

    def _faulty_open(file, *args, **kwargs):
        if _is_target(file):
            if _MODE == "open":
                raise OSError("simulated open failure")
            if _MODE == "midread":
                return _MidReadFailFile(_real_open(file, *args, **kwargs))
        return _real_open(file, *args, **kwargs)

    builtins.open = _faulty_open
'''


class CsvIoErrorRegressionTest(unittest.TestCase):
    """打开/读取 OSError 落为 failed/input_error，且不留下部分结果。"""

    def setUp(self):
        # 独立数据库：每个场景一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_csv_io_error_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        # 故障注入 shim：写入临时目录，经 PYTHONPATH 传给 run 子进程。
        self._shim_dir = Path(self._tmp.name) / "shim"
        self._shim_dir.mkdir()
        (self._shim_dir / "sitecustomize.py").write_text(
            SHIM_SOURCE, encoding="utf-8"
        )
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

    def _cli(self, *args, fault=None, target=None, marker=None):
        """调用 python -m task_center，返回 (退出码, stdout 文本, 解析后的 JSON, stderr)。

        标准输出必须恰好是一个 JSON 对象（单行）。fault 为 "open" 或
        "midread" 时，通过 shim 对 target 路径注入对应的 OSError。
        """
        env = None
        if fault is not None:
            env = os.environ.copy()
            pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = str(self._shim_dir) + (
                os.pathsep + pythonpath if pythonpath else ""
            )
            env["CSV_IO_ERROR_TARGET"] = os.path.realpath(target)
            env["CSV_IO_ERROR_MODE"] = fault
            env["CSV_IO_ERROR_SERVE_LINES"] = "2"
            env["CSV_IO_ERROR_MARKER"] = str(marker) if marker is not None else ""
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

    def _assert_io_failure(self, job_id, code, out, record, err):
        """run 响应：退出码 1、failed/input_error、不暴露底层异常文字。"""
        self.assertEqual(code, 1, (record, err))
        self.assertEqual(record, {"id": job_id, **FAILED_RECORD})
        self.assertEqual(err, "", "不应输出异常堆栈：%r" % err)
        for leaked in ("data_error", "OSError", "simulated", "Traceback"):
            self.assertNotIn(leaked, out)

    def _assert_saved_failed_record(self, job_id, expected):
        """解除故障后由新进程 show：退出码 0，记录与执行响应完全相同。

        输入文件此刻内容合法：若 show 重新执行汇总会得到 succeeded，
        因此返回失败记录本身即可证明查询没有重新执行或更改保存值。
        """
        code, _out, shown, err = self._cli("show", job_id)
        self.assertEqual(code, 0, (shown, err))
        self.assertEqual(shown, expected)
        # 再次查询结果不变。
        code, _out, again, err = self._cli("show", job_id)
        self.assertEqual(code, 0, (again, err))
        self.assertEqual(again, expected)

    # --- 场景 -----------------------------------------------------------

    def test_open_failure_fails_with_input_error(self):
        self._write_demo(OPEN_FILE, CSV_TEXT)
        job_id = self._submit(OPEN_INPUT)

        # 文件存在且路径合法，但 open 本身抛 OSError。
        code, out, record, err = self._cli(
            "run", job_id, fault="open", target=OPEN_FILE
        )
        self._assert_io_failure(job_id, code, out, record, err)

        # 解除故障后新进程 show 同一 id：与 run 的失败响应完全一致。
        self._assert_saved_failed_record(job_id, record)

    def test_mid_read_failure_fails_with_input_error_without_partial_result(self):
        self._write_demo(MIDREAD_FILE, CSV_TEXT)
        job_id = self._submit(MIDREAD_INPUT)

        # 表头与一条合法数据行读出之后，继续读取抛 OSError。
        marker = Path(self._tmp.name) / "served_lines.txt"
        code, out, record, err = self._cli(
            "run", job_id, fault="midread", target=MIDREAD_FILE, marker=marker
        )
        self._assert_io_failure(job_id, code, out, record, err)

        # 合法数据行确实已被读取（表头 + 数据行共 2 行），不是打开失败：
        # 若表头或数据行非法，错误码会是 data_error 而非 input_error。
        self.assertEqual(marker.read_text(encoding="utf-8"), "2")
        # 已读出的金额 10 不能留下部分结果。
        self.assertIsNone(record["result"])

        # 解除故障后新进程 show 同一 id：与 run 的失败响应完全一致。
        self._assert_saved_failed_record(job_id, record)


if __name__ == "__main__":
    unittest.main()
