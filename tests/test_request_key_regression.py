"""请求键（--request-key）幂等提交的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，并以独立
查询进程（show）和临时 SQLite 数据库中保存的记录核对行为。覆盖：

- 不带键的提交保持“每次新建任务”的原行为；
- 同库同键同类型同路径字符串的重复提交返回原任务当前完整记录，
  不新增任务、不改变状态（queued/succeeded/failed/cancelled 均如此），
  且不再检查输入文件（删除或改写文件不影响返回）；
- 键格式校验（1-64 个 ASCII 字母、数字、下划线、连字符，区分大小写、
  不裁剪空白）与校验顺序（类型 -> 键 -> 已绑定的类型/路径 -> 新路径）；
- request_conflict：键已绑定但类型或原始路径字符串不同（相对/绝对写法
  视为不同请求），此时不检查新路径、不修改原任务；
- invalid_input 不占用键；绑定跨进程保留且 run/retry/cancel 不解除；
- 不同数据库互不影响；旧数据库（无 request_keys 表）可平滑迁移，
  旧任务不参与请求键匹配。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_request_key_regression -v

或直接：

    python tests/test_request_key_regression.py
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = PROJECT_ROOT / "demo"

# 测试自备的演示文件，位于 demo 目录内且不与现有文件冲突。
DEMO_FILE = DEMO_DIR / "request_key_regression_case.csv"
DEMO_FILE_2 = DEMO_DIR / "request_key_regression_case_2.csv"
DEMO_INPUT = "demo/request_key_regression_case.csv"
DEMO_INPUT_2 = "demo/request_key_regression_case_2.csv"

VALID_CSV = "category,amount\na,10\nb,20\na,5\n"
VALID_CSV_SUMMARY = {
    "row_count": 3,
    "total_amount": 35,
    "categories": {"a": 15, "b": 20},
}
# 登记合法、执行才会失败的内容（制造 failed 记录）。
BAD_AMOUNT_CSV = "category,amount\na,x\n"


class RequestKeyRegressionTest(unittest.TestCase):
    """--request-key 幂等提交的公开行为：JSON 响应、退出码与保存的记录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_request_key_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        self._demo_dir_created = not DEMO_DIR.exists()
        DEMO_DIR.mkdir(exist_ok=True)
        for demo_file in (DEMO_FILE, DEMO_FILE_2):
            if demo_file.exists():
                self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % demo_file)
        self.addCleanup(self._remove_demo_files)

    def _remove_demo_files(self):
        for demo_file in (DEMO_FILE, DEMO_FILE_2):
            try:
                demo_file.unlink()
            except FileNotFoundError:
                pass
        if self._demo_dir_created:
            try:
                DEMO_DIR.rmdir()
            except OSError:
                pass

    # --- 辅助方法 -------------------------------------------------------

    def _write_demo(self, text, demo_file=DEMO_FILE):
        demo_file.write_text(text, encoding="utf-8")

    def _cli(self, *args, db=None):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "task_center",
                "--db",
                db if db is not None else self.db,
                *args,
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertTrue(proc.stdout.strip(), "CLI 应输出一行 JSON：%r" % proc.stderr)
        return proc.returncode, json.loads(proc.stdout)

    def _submit(self, task_type, input_path, request_key=None, db=None):
        args = ["submit", task_type, "--input", input_path]
        if request_key is not None:
            args += ["--request-key", request_key]
        return self._cli(*args, db=db)

    def _run(self, job_id):
        return self._cli("run", job_id)

    def _retry(self, job_id):
        return self._cli("retry", job_id)

    def _cancel(self, job_id):
        return self._cli("cancel", job_id)

    def _show(self, job_id):
        return self._cli("show", job_id)

    def _job_count(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finally:
            conn.close()

    def _assert_queued(self, code, record):
        self.assertEqual(code, 0, record)
        self.assertTrue(record["id"], record)
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )
        return record["id"]

    def _assert_rejected(self, code, record, error_code):
        """提交被拒绝：退出码 1，id/status/result 为 null，仅四个字段。"""
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": None, "status": None, "result": None, "error": error_code},
        )

    # --- 不带键：保持原行为 ---------------------------------------------

    def test_without_key_repeated_submits_still_create_distinct_jobs(self):
        self._write_demo(VALID_CSV)
        code, first = self._submit("csv_summary", DEMO_INPUT)
        first_id = self._assert_queued(code, first)
        code, second = self._submit("csv_summary", DEMO_INPUT)
        second_id = self._assert_queued(code, second)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self._job_count(), 2)

    # --- 同键重复提交：返回原任务、只登记一个 ----------------------------

    def test_same_key_twice_returns_same_queued_job_without_duplicate(self):
        self._write_demo(VALID_CSV)

        code, first = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        job_id = self._assert_queued(code, first)

        # 第二次提交前删除文件：幂等返回不应再检查文件。
        DEMO_FILE.unlink()
        code, second = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        self.assertEqual(code, 0, second)
        self.assertEqual(second, first)
        self.assertEqual(self._job_count(), 1)

        # 独立查询进程读到的仍是同一条 queued 记录。
        show_code, shown = self._show(job_id)
        self.assertEqual(show_code, 0, shown)
        self.assertEqual(shown, first)

    def test_same_key_after_success_returns_stored_succeeded_record(self):
        self._write_demo(VALID_CSV)
        code, submitted = self._submit(
            "csv_summary", DEMO_INPUT, request_key="sales-1"
        )
        job_id = self._assert_queued(code, submitted)

        code, run_record = self._run(job_id)
        self.assertEqual(code, 0, run_record)
        self.assertEqual(
            run_record,
            {
                "id": job_id,
                "status": "succeeded",
                "result": VALID_CSV_SUMMARY,
                "error": None,
            },
        )

        # 文件被改写后同键同路径再提交：返回保存的成功记录，不重新执行。
        self._write_demo(BAD_AMOUNT_CSV)
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        self.assertEqual(code, 0, again)
        self.assertEqual(again, run_record)
        self.assertEqual(self._job_count(), 1)

    def test_same_key_returns_current_record_through_failed_retry_cancel(self):
        # 初始内容在执行阶段才失败。
        self._write_demo(BAD_AMOUNT_CSV)
        code, submitted = self._submit("csv_summary", DEMO_INPUT, request_key="k")
        job_id = self._assert_queued(code, submitted)

        code, failed = self._run(job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(
            failed,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )
        # failed 状态下重复提交：原样返回 failed 记录，退出码 0，不改动。
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key="k")
        self.assertEqual(code, 0, again)
        self.assertEqual(again, failed)

        # retry 不解除绑定；retry 后重复提交返回 queued 空记录。
        code, retried = self._retry(job_id)
        self.assertEqual(code, 0, retried)
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key="k")
        self.assertEqual(code, 0, again)
        self.assertEqual(
            again,
            {"id": job_id, "status": "queued", "result": None, "error": None},
        )

        # cancel 同样不解除绑定；取消后重复提交返回 cancelled 记录。
        code, cancelled = self._cancel(job_id)
        self.assertEqual(code, 0, cancelled)
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key="k")
        self.assertEqual(code, 0, again)
        self.assertEqual(
            again,
            {"id": job_id, "status": "cancelled", "result": None, "error": None},
        )
        self.assertEqual(self._job_count(), 1)

    # --- 键格式与校验顺序 -----------------------------------------------

    def test_invalid_request_keys_are_rejected_and_occupy_no_job(self):
        self._write_demo(VALID_CSV)
        invalid_keys = [
            "",          # 空键
            " sales-1",  # 前导空白：不裁剪
            "sales-1 ",  # 尾随空白：不裁剪
            "sales 1",   # 含空格
            "sales.1",   # 非法标点
            "键",          # 非 ASCII
            "a" * 65,    # 超长
        ]
        for key in invalid_keys:
            with self.subTest(key=key):
                code, record = self._submit(
                    "csv_summary", DEMO_INPUT, request_key=key
                )
                self._assert_rejected(code, record, "invalid_request_key")
        self.assertEqual(self._job_count(), 0)

    def test_key_of_64_characters_is_accepted(self):
        self._write_demo(VALID_CSV)
        key = "A" * 64
        code, record = self._submit("csv_summary", DEMO_INPUT, request_key=key)
        self._assert_queued(code, record)

    def test_type_is_validated_before_request_key(self):
        # 类型非法 + 键非法：先报 invalid_type。
        code, record = self._submit(
            "no_such_type", DEMO_INPUT, request_key="bad key"
        )
        self._assert_rejected(code, record, "invalid_type")
        self.assertEqual(self._job_count(), 0)

    def test_key_is_validated_before_path_on_first_use(self):
        # 类型合法、键非法、路径不存在：先报 invalid_request_key。
        self.assertFalse((PROJECT_ROOT / DEMO_INPUT).exists())
        code, record = self._submit(
            "csv_summary", DEMO_INPUT, request_key="bad key"
        )
        self._assert_rejected(code, record, "invalid_request_key")
        self.assertEqual(self._job_count(), 0)

    def test_keys_are_case_sensitive(self):
        self._write_demo(VALID_CSV)
        code, lower = self._submit("csv_summary", DEMO_INPUT, request_key="key")
        lower_id = self._assert_queued(code, lower)
        code, upper = self._submit("csv_summary", DEMO_INPUT, request_key="KEY")
        upper_id = self._assert_queued(code, upper)
        self.assertNotEqual(lower_id, upper_id)
        self.assertEqual(self._job_count(), 2)

    # --- request_conflict -----------------------------------------------

    def test_same_key_with_different_path_returns_request_conflict(self):
        self._write_demo(VALID_CSV)
        self._write_demo(VALID_CSV, DEMO_FILE_2)
        code, first = self._submit("csv_summary", DEMO_INPUT, request_key="dup")
        first_id = self._assert_queued(code, first)

        # 同键不同路径字符串 -> request_conflict；即使新路径文件存在也不读取。
        code, record = self._submit(
            "csv_summary", DEMO_INPUT_2, request_key="dup"
        )
        self._assert_rejected(code, record, "request_conflict")
        self.assertEqual(self._job_count(), 1)

        # 新路径不存在时仍为 request_conflict（不检查新路径）。
        DEMO_FILE_2.unlink()
        code, record = self._submit(
            "csv_summary", DEMO_INPUT_2, request_key="dup"
        )
        self._assert_rejected(code, record, "request_conflict")
        self.assertEqual(self._job_count(), 1)

        # 原任务保持 queued 不变，同键同原路径仍返回它。
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key="dup")
        self.assertEqual(code, 0, again)
        self.assertEqual(again["id"], first_id)
        self.assertEqual(again["status"], "queued")

    def test_relative_and_absolute_paths_are_distinct_requests(self):
        self._write_demo(VALID_CSV)
        absolute_input = str(DEMO_FILE.resolve())
        code, rel = self._submit("csv_summary", DEMO_INPUT, request_key="path-k")
        self._assert_queued(code, rel)

        code, conflict = self._submit(
            "csv_summary", absolute_input, request_key="path-k"
        )
        self._assert_rejected(code, conflict, "request_conflict")
        self.assertEqual(self._job_count(), 1)

    def test_invalid_type_takes_precedence_over_bound_key(self):
        self._write_demo(VALID_CSV)
        code, _ = self._submit("csv_summary", DEMO_INPUT, request_key="type-k")
        # 键已绑定 csv_summary；用非法类型同键提交仍先报 invalid_type，
        # 而不是 request_conflict（类型校验先于绑定比较）。
        code, record = self._submit(
            "no_such_type", DEMO_INPUT, request_key="type-k"
        )
        self._assert_rejected(code, record, "invalid_type")
        self.assertEqual(self._job_count(), 1)

    # --- invalid_input 不占用键 ----------------------------------------

    def test_invalid_input_does_not_occupy_key(self):
        # 首次使用键但路径非法：invalid_input，不建任务、不绑定键。
        code, record = self._submit(
            "csv_summary", "demo/missing_request_key_case.csv", request_key="free"
        )
        self._assert_rejected(code, record, "invalid_input")
        self.assertEqual(self._job_count(), 0)

        # 同键随后以合法路径首次提交：正常创建 queued 任务。
        self._write_demo(VALID_CSV)
        code, record = self._submit("csv_summary", DEMO_INPUT, request_key="free")
        self._assert_queued(code, record)
        self.assertEqual(self._job_count(), 1)

    # --- 数据库隔离与旧库迁移 -------------------------------------------

    def test_same_key_in_different_databases_is_independent(self):
        self._write_demo(VALID_CSV)
        other_db = str(Path(self._tmp.name) / "other.sqlite3")

        code, first = self._submit(
            "csv_summary", DEMO_INPUT, request_key="iso", db=self.db
        )
        first_id = self._assert_queued(code, first)
        code, other = self._submit(
            "csv_summary", DEMO_INPUT, request_key="iso", db=other_db
        )
        other_id = self._assert_queued(code, other)
        self.assertNotEqual(first_id, other_id)

        # 各库内重复提交只返回各自的任务。
        code, again = self._submit(
            "csv_summary", DEMO_INPUT, request_key="iso", db=self.db
        )
        self.assertEqual(again["id"], first_id)
        code, again_other = self._submit(
            "csv_summary", DEMO_INPUT, request_key="iso", db=other_db
        )
        self.assertEqual(again_other["id"], other_id)

    def test_legacy_database_migrates_and_old_jobs_do_not_match_keys(self):
        # 手工构造只有旧表结构的数据库，并写入一条无键绑定的旧任务。
        self._write_demo(VALID_CSV)
        legacy = sqlite3.connect(self.db)
        try:
            legacy.execute(
                "CREATE TABLE jobs ("
                "id TEXT PRIMARY KEY, type TEXT NOT NULL, input TEXT NOT NULL,"
                " status TEXT NOT NULL, result TEXT, error TEXT)"
            )
            legacy.execute(
                "INSERT INTO jobs (id, type, input, status, result, error)"
                " VALUES (?, ?, ?, ?, NULL, NULL)",
                ("legacyid", "csv_summary", DEMO_INPUT, "queued"),
            )
            legacy.commit()
        finally:
            legacy.close()

        # CLI 访问时自动补建 request_keys 表；旧任务不参与匹配，同键提交新建任务。
        code, record = self._submit("csv_summary", DEMO_INPUT, request_key="mig")
        new_id = self._assert_queued(code, record)
        self.assertNotEqual(new_id, "legacyid")
        self.assertEqual(self._job_count(), 2)

        # 键绑定的是新任务：重复提交返回新任务，旧记录原样保留。
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key="mig")
        self.assertEqual(code, 0, again)
        self.assertEqual(again["id"], new_id)
        show_code, legacy_record = self._show("legacyid")
        self.assertEqual(show_code, 0, legacy_record)
        self.assertEqual(legacy_record["status"], "queued")


if __name__ == "__main__":
    unittest.main()
