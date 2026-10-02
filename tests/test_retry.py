"""retry 公开行为回归测试。

通过 ``python -m task_center`` 子进程观察 JSON 输出与退出码：
- 仅使用 Python 3 标准库（unittest/subprocess/json/tempfile）；
- 每个测试类使用独立的临时数据库（--db），不依赖预置任务；
- 演示数据由测试自行写入项目 ``demo/`` 目录（文件名带 uuid，互不冲突），
  结束时只清理自身创建的文件与数据库；
- 同一环境连续运行两次均可通过。
"""

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = PROJECT_ROOT / "demo"


class CliMixin:
    """以子进程方式调用 task_center 命令行。"""

    def invoke(self, *cmd):
        """运行 python -m task_center --db <db> <cmd...>，返回 (退出码, JSON 记录)。"""
        proc = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", str(self.db), *cmd],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        try:
            record = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(
                f"命令非 JSON 输出: cmd={cmd} rc={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )
        return proc.returncode, record

    def submit(self, rel_path):
        rc, record = self.invoke("submit", "csv_summary", "--input", rel_path)
        self.assertEqual(rc, 0)
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )
        return record["id"]

    def show(self, job_id):
        return self.invoke("show", job_id)

    def write_demo_csv(self, name, text):
        """在 demo 目录写入 UTF-8 CSV 并登记，结束时自动清理。"""
        path = DEMO_DIR / name
        path.write_text(text, encoding="utf-8")
        self.created_files.append(path)
        return path

    def unique_name(self, suffix):
        return f"retry_test_{uuid.uuid4().hex}{suffix}"


class RetryFailedTaskTests(unittest.TestCase, CliMixin):
    """failed 任务的 retry 主流程：原文件非法、已删除、改写后成功。"""

    def setUp(self):
        self.created_files = []
        self.tmpdir = Path(tempfile.mkdtemp(prefix="task_center_retry_"))
        self.db = self.tmpdir / "tasks.sqlite3"

    def tearDown(self):
        for path in self.created_files:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_retry_failed_job_until_success(self):
        # 演示数据：UTF-8 CSV，表头 category,amount，数据行 a,x（amount 非法）
        name = self.unique_name(".csv")
        rel_path = f"demo/{name}"
        real_path = self.write_demo_csv(name, "category,amount\na,x\n")

        # 提交返回 queued
        job_id = self.submit(rel_path)

        # 执行返回 failed / data_error / null 结果，退出码 1
        rc, record = self.invoke("run", job_id)
        self.assertEqual(rc, 1)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )

        # 原文件仍非法时 retry：退出码 0，沿用原 id，回到 queued，result/error 均为 null
        rc, record = self.invoke("retry", job_id)
        self.assertEqual(rc, 0)
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )
        # 随后的 show 与此一致（且是新进程读取已保存记录）
        rc, record = self.show(job_id)
        self.assertEqual(rc, 0)
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )

        # retry 不执行文件：随后的 run 仍按原规则处理，内容非法 -> data_error
        rc, record = self.invoke("run", job_id)
        self.assertEqual(rc, 1)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )
        # 再次失败后仍可 retry
        rc, record = self.invoke("retry", job_id)
        self.assertEqual(rc, 0)
        self.assertEqual(record["status"], "queued")

        # 让任务在文件缺失的情况下再次失败（input_error）
        real_path.unlink()
        rc, record = self.invoke("run", job_id)
        self.assertEqual(rc, 1)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "input_error"},
        )

        # 原文件已删除时 retry 同样成功（retry 不读取文件）
        rc, record = self.invoke("retry", job_id)
        self.assertEqual(rc, 0)
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )
        rc, record = self.show(job_id)
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )

        # 后续 run 才按原规则处理：文件缺失 -> input_error
        rc, record = self.invoke("run", job_id)
        self.assertEqual(rc, 1)
        self.assertEqual(
            record,
            {"id": job_id, "status": "failed", "result": None, "error": "input_error"},
        )

        # 把同一路径文件改成合法内容：表头加 a,10、b,20、a,5
        self.write_demo_csv(
            name, "category,amount\na,10\nb,20\na,5\n"
        )
        # 当前是 failed，需先 retry 入队再 run
        rc, record = self.invoke("retry", job_id)
        self.assertEqual(rc, 0)
        rc, record = self.invoke("run", job_id)
        self.assertEqual(rc, 0)
        self.assertEqual(
            record,
            {
                "id": job_id,
                "status": "succeeded",
                "result": {
                    "row_count": 3,
                    "total_amount": 35,
                    "categories": {"a": 15, "b": 20},
                },
                "error": None,
            },
        )

        # 关闭进程后查询仍得到该结果（全新子进程 + 持久化数据库）
        rc, record = self.show(job_id)
        self.assertEqual(rc, 0)
        self.assertEqual(
            record,
            {
                "id": job_id,
                "status": "succeeded",
                "result": {
                    "row_count": 3,
                    "total_amount": 35,
                    "categories": {"a": 15, "b": 20},
                },
                "error": None,
            },
        )


class RetryRejectedTests(unittest.TestCase, CliMixin):
    """拒绝重试的边界：queued / succeeded / 不存在的 id。"""

    def setUp(self):
        self.created_files = []
        self.tmpdir = Path(tempfile.mkdtemp(prefix="task_center_retry_"))
        self.db = self.tmpdir / "tasks.sqlite3"

    def tearDown(self):
        for path in self.created_files:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_retry_queued_job_rejected(self):
        name = self.unique_name(".csv")
        rel_path = f"demo/{name}"
        self.write_demo_csv(name, "category,amount\na,10\n")
        job_id = self.submit(rel_path)

        before_rc, before = self.show(job_id)
        self.assertEqual(before_rc, 0)

        # queued 任务 retry：退出码 1，保留原 id/状态/结果，error=invalid_state
        rc, record = self.invoke("retry", job_id)
        self.assertEqual(rc, 1)
        self.assertEqual(
            record,
            {"id": job_id, "status": "queued", "result": None, "error": "invalid_state"},
        )

        # 操作前后的 show 输出完全一致
        after_rc, after = self.show(job_id)
        self.assertEqual(after_rc, 0)
        self.assertEqual(after, before)

    def test_retry_succeeded_job_rejected_and_result_kept(self):
        name = self.unique_name(".csv")
        rel_path = f"demo/{name}"
        self.write_demo_csv(name, "category,amount\na,10\nb,20\na,5\n")
        job_id = self.submit(rel_path)

        rc, record = self.invoke("run", job_id)
        self.assertEqual(rc, 0)
        self.assertEqual(record["status"], "succeeded")
        success_result = record["result"]

        before_rc, before = self.show(job_id)
        self.assertEqual(before_rc, 0)
        self.assertEqual(before["result"], success_result)

        # succeeded 任务 retry：退出码 1，原 id/状态/结果保留，error=invalid_state
        rc, record = self.invoke("retry", job_id)
        self.assertEqual(rc, 1)
        self.assertEqual(
            record,
            {
                "id": job_id,
                "status": "succeeded",
                "result": success_result,
                "error": "invalid_state",
            },
        )

        # show 输出与操作前完全一致，成功结果不得被清空
        after_rc, after = self.show(job_id)
        self.assertEqual(after_rc, 0)
        self.assertEqual(after, before)
        self.assertEqual(after["status"], "succeeded")
        self.assertEqual(after["result"], success_result)
        self.assertIsNone(after["error"])

    def test_retry_unknown_id_returns_job_not_found(self):
        missing_id = f"missing-{uuid.uuid4().hex}"

        # 不存在的 id：退出码 1，id 保留请求值，status/result 为 null
        rc, record = self.invoke("retry", missing_id)
        self.assertEqual(rc, 1)
        self.assertEqual(
            record,
            {"id": missing_id, "status": None, "result": None, "error": "job_not_found"},
        )

        # 后续查询仍表示不存在，同样不创建记录
        rc, record = self.show(missing_id)
        self.assertEqual(rc, 1)
        self.assertEqual(
            record,
            {"id": missing_id, "status": None, "result": None, "error": "job_not_found"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
