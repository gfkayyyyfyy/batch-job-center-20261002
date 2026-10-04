"""show --request-key 按请求键查询任务的公开行为回归测试。

通过 `python -m task_center` 子进程观察 JSON 输出与退出码，并以独立
进程和临时 SQLite 数据库核对：show 在位置参数 id 之外新增
--request-key 查询方式，两种方式的输出都是单行 JSON，保留 id、
status、result、error 四个字段及既有含义。覆盖：

- 键合法性沿用提交规则（1-64 个 ASCII 字母/数字/下划线/连字符，
  区分大小写、不裁剪空白），先检查格式再查绑定；
- 非法键返回 invalid_request_key、合法未绑定键返回 job_not_found，
  两类响应 id/status/result 均为 null，退出码 1；
- queued/succeeded/failed/cancelled 各状态按键查询都返回当前保存的
  完整记录，退出码均为 0（error 为 data_error/input_error 也不算
  查询失败）；
- 查询不执行任务、不读取输入文件：文件删除或改写后结果不变，跨进程
  查询返回相同的持久化记录，且不改变状态、结果、错误或键绑定；
- 不同数据库中的同名键各自查询各自的绑定；
- 旧库（无 request_key 列）自动兼容：无键任务仍能按 id 查询；
- show 同时传 id 与 --request-key 或两者都不传时按参数解析失败处理：
  退出码 2，标准错误输出用法提示，标准输出不输出 JSON；
- 原有 show <id> 与 submit/run/retry/cancel 行为保持不变。

每个用例使用独立的临时 SQLite 数据库（--db 指定）；演示 CSV 由
测试在 demo 目录下自行准备专用文件，同名样例已存在时直接失败且
不覆盖。测试结束仅清理自身数据，可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_show_request_key -v

或直接：

    python tests/test_show_request_key.py
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

# 测试自备的演示文件，位于 demo 目录内且不与现有文件冲突；
# 每个用例运行前创建、结束后删除。
DEMO_FILE = DEMO_DIR / "show_request_key_case.csv"
DEMO_INPUT = "demo/show_request_key_case.csv"

# 合法 CSV：表头 category,amount，数据 a,10、b,20、a,5。
VALID_CSV = "category,amount\na,10\nb,20\na,5\n"
EXPECTED_RESULT = {
    "row_count": 3,
    "total_amount": 35,
    "categories": {"a": 15, "b": 20},
}

# 金额非法的 CSV：执行后以 data_error 失败。
BAD_AMOUNT_CSV = "category,amount\na,x\n"


class ShowByRequestKeyTest(unittest.TestCase):
    """show --request-key 的公开行为：JSON 响应、退出码与持久化记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_show_key_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        # demo 目录缺失时自行创建，并记录以便结束后清理空目录。
        self._demo_dir_created = not DEMO_DIR.exists()
        DEMO_DIR.mkdir(exist_ok=True)
        # 演示文件若已存在则拒绝覆盖，保证只清理自身数据。
        if DEMO_FILE.exists():
            self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % DEMO_FILE)
        self.addCleanup(self._remove_demo_file)

    def _remove_demo_file(self):
        try:
            DEMO_FILE.unlink()
        except FileNotFoundError:
            pass
        # 仅当 demo 目录由本测试创建且已空时才移除。
        if self._demo_dir_created:
            try:
                DEMO_DIR.rmdir()
            except OSError:
                pass

    # --- 辅助方法 -------------------------------------------------------

    def _write_demo(self, text):
        DEMO_FILE.write_text(text, encoding="utf-8")

    def _run_cli(self, *args, db=None):
        """调用 python -m task_center，返回 (退出码, 标准输出, 标准错误)。"""
        proc = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", db or self.db, *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _cli(self, *args, db=None):
        """调用 python -m task_center，返回 (退出码, 解析后的 JSON 记录)。

        标准输出必须恰为一个 JSON 对象（单行、无额外输出）。
        """
        code, stdout, stderr = self._run_cli(*args, db=db)
        self.assertTrue(stdout.strip(), "CLI 应输出一行 JSON：%r" % stderr)
        self.assertEqual(
            len(stdout.strip().splitlines()),
            1,
            "标准输出应恰为一行 JSON：%r" % stdout,
        )
        record = json.loads(stdout)
        self.assertIsInstance(record, dict, record)
        return code, record

    def _submit(self, task_type, input_path, request_key=None, db=None):
        args = ["submit", task_type, "--input", input_path]
        if request_key is not None:
            args += ["--request-key", request_key]
        return self._cli(*args, db=db)

    def _show(self, job_id, db=None):
        return self._cli("show", job_id, db=db)

    def _show_by_key(self, request_key, db=None):
        return self._cli("show", "--request-key", request_key, db=db)

    def _job_count(self):
        """直接统计保存的任务记录数。"""
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finally:
            conn.close()

    def _assert_queued(self, code, record):
        """登记成功：退出码 0，非空 id、queued、result 与 error 均为 null。"""
        self.assertEqual(code, 0, record)
        self.assertIsInstance(record["id"], str, record)
        self.assertTrue(record["id"], record)
        self.assertEqual(
            record,
            {"id": record["id"], "status": "queued", "result": None, "error": None},
        )
        return record["id"]

    def _assert_lookup_rejected(self, code, record, error_code):
        """查询被拒绝：退出码 1，id/status/result 为 null，error 为错误码。"""
        self.assertEqual(code, 1, record)
        self.assertEqual(
            record,
            {"id": None, "status": None, "result": None, "error": error_code},
        )

    def _register_with_key(self, key="sales-1"):
        """以有效键成功登记一条 queued 任务，返回 (id, 记录)。"""
        self._write_demo(VALID_CSV)
        code, record = self._submit("csv_summary", DEMO_INPUT, request_key=key)
        job_id = self._assert_queued(code, record)
        return job_id, record

    # --- 基本查询：queued → succeeded → 删除输入文件 ----------------------

    def test_show_by_key_returns_saved_record_across_states(self):
        job_id, queued = self._register_with_key("sales-1")

        # 登记后立即按键查询：返回绑定任务的真实 id 与 queued 记录，
        # 不额外返回输入路径或请求键。
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, queued)
        self.assertEqual(shown["id"], job_id)
        self.assertEqual(set(shown), {"id", "status", "result", "error"})

        # 独立进程按 id 查询同一数据库，两条路径返回同一记录。
        show_code, by_id = self._show(job_id)
        self.assertEqual(show_code, 0, by_id)
        self.assertEqual(by_id, shown)

        # 执行成功：row_count 3、total_amount 35、categories a=15 b=20。
        code, ran = self._cli("run", job_id)
        self.assertEqual(code, 0, ran)
        self.assertEqual(
            ran,
            {
                "id": job_id,
                "status": "succeeded",
                "result": EXPECTED_RESULT,
                "error": None,
            },
        )

        # 成功后再按同一键查询：完整返回当前保存的成功记录。
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, ran)

        # 删除输入文件后查询结果仍保持一致（查询不读取输入文件）。
        DEMO_FILE.unlink()
        self.assertFalse(DEMO_FILE.exists())
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, ran)
        show_code, by_id = self._show(job_id)
        self.assertEqual(show_code, 0, by_id)
        self.assertEqual(by_id, ran)

    def test_show_by_key_does_not_execute_or_mutate(self):
        job_id, queued = self._register_with_key("sales-1")

        # 文件被改写后按键查询：仍返回登记时的 queued 记录，
        # 不执行、不重新读取文件、不改变状态/结果/错误/键绑定。
        self._write_demo("category,amount\nz,999\n")
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, queued)
        self.assertEqual(self._job_count(), 1)
        # 查询后任务仍是 queued，可以正常执行且使用改写后的文件。
        code, ran = self._cli("run", job_id)
        self.assertEqual(code, 0, ran)
        self.assertEqual(ran["status"], "succeeded")
        self.assertEqual(ran["result"]["total_amount"], 999)

    # --- failed / cancelled 记录按键查询退出码仍为 0 ----------------------

    def test_failed_record_with_data_error_is_queryable_by_key(self):
        self._write_demo(BAD_AMOUNT_CSV)
        code, queued = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        job_id = self._assert_queued(code, queued)

        code, failed = self._cli("run", job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(
            failed,
            {"id": job_id, "status": "failed", "result": None, "error": "data_error"},
        )

        # error 为 data_error 的 failed 记录按键查询：退出码 0，原样返回。
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, failed)

    def test_failed_record_with_input_error_is_queryable_by_key(self):
        job_id, _queued = self._register_with_key("sales-1")
        DEMO_FILE.unlink()

        code, failed = self._cli("run", job_id)
        self.assertEqual(code, 1, failed)
        self.assertEqual(
            failed,
            {"id": job_id, "status": "failed", "result": None, "error": "input_error"},
        )

        # error 为 input_error 的 failed 记录按键查询：退出码 0，原样返回。
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, failed)

    def test_cancelled_record_is_queryable_by_key(self):
        job_id, _queued = self._register_with_key("sales-1")

        code, cancelled = self._cli("cancel", job_id)
        self.assertEqual(code, 0, cancelled)
        self.assertEqual(
            cancelled,
            {"id": job_id, "status": "cancelled", "result": None, "error": None},
        )

        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, cancelled)

    # --- 键合法性与未绑定 -------------------------------------------------

    def test_invalid_request_key_returns_invalid_request_key(self):
        for bad_key in (
            "",                # 空串
            " has space",      # 含空白（不裁剪）
            "tab\tkey",        # 含制表符
            "dot.key",         # 含非法字符
            "中文键",           # 非 ASCII
            "k" * 65,          # 超过 64 个字符
        ):
            with self.subTest(key=bad_key):
                code, record = self._show_by_key(bad_key)
                self._assert_lookup_rejected(code, record, "invalid_request_key")

    def test_valid_but_unbound_key_returns_job_not_found(self):
        code, record = self._show_by_key("sales-1")
        self._assert_lookup_rejected(code, record, "job_not_found")

    def test_key_format_checked_before_binding(self):
        job_id, _queued = self._register_with_key("sales-1")

        # 库中已有绑定时，非法键仍先报 invalid_request_key 而非其他错误。
        code, record = self._show_by_key("bad key!")
        self._assert_lookup_rejected(code, record, "invalid_request_key")
        # 合法但未绑定的键报 job_not_found。
        code, record = self._show_by_key("sales-2")
        self._assert_lookup_rejected(code, record, "job_not_found")
        # 原绑定不受查询影响。
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["id"], job_id)

    def test_boundary_and_case_sensitive_keys(self):
        self._write_demo(VALID_CSV)

        # 单字符键与 64 字符键均可登记并按键查询。
        code, one = self._submit("csv_summary", DEMO_INPUT, request_key="k")
        one_id = self._assert_queued(code, one)
        long_key = "Ab0_-" + "z" * 59
        self.assertEqual(len(long_key), 64)
        code, long_rec = self._submit("csv_summary", DEMO_INPUT, request_key=long_key)
        long_id = self._assert_queued(code, long_rec)

        code, shown = self._show_by_key("k")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, one)
        code, shown = self._show_by_key(long_key)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, long_rec)

        # 区分大小写：K 与 k 是不同的键。
        code, record = self._show_by_key("K")
        self._assert_lookup_rejected(code, record, "job_not_found")
        self.assertNotEqual(one_id, long_id)

    # --- 不同数据库互不影响 -------------------------------------------------

    def test_same_key_in_different_databases_queries_own_binding(self):
        self._write_demo(VALID_CSV)
        other_db = str(Path(self._tmp.name) / "other.sqlite3")

        code, first = self._submit("csv_summary", DEMO_INPUT, request_key="sales-1")
        first_id = self._assert_queued(code, first)
        code, second = self._submit(
            "csv_summary", DEMO_INPUT, request_key="sales-1", db=other_db
        )
        second_id = self._assert_queued(code, second)
        self.assertNotEqual(first_id, second_id)

        # 同名键在各自数据库中分别返回各自的绑定记录。
        code, shown = self._show_by_key("sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, first)
        code, shown = self._show_by_key("sales-1", db=other_db)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, second)

        # 一个库中执行成功不影响另一个库中同键任务的查询结果。
        code, ran = self._cli("run", first_id)
        self.assertEqual(code, 0, ran)
        code, shown = self._show_by_key("sales-1", db=other_db)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, second)

    # --- 旧数据库自动兼容 -------------------------------------------------

    def test_legacy_db_without_request_key_column(self):
        # 手工建立只有六列的旧版 jobs 表并预置一条无键任务。
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "CREATE TABLE jobs ("
                "id TEXT PRIMARY KEY, type TEXT NOT NULL, input TEXT NOT NULL,"
                " status TEXT NOT NULL, result TEXT, error TEXT)"
            )
            conn.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
                ("0" * 32, "csv_summary", DEMO_INPUT, "queued", None, None),
            )
            conn.commit()
        finally:
            conn.close()

        # 旧库中的无键任务仍能按 id 查询（自动补齐列后原样可读）。
        code, shown = self._show("0" * 32)
        self.assertEqual(code, 0, shown)
        self.assertEqual(
            shown,
            {"id": "0" * 32, "status": "queued", "result": None, "error": None},
        )
        # 旧记录没有键绑定：按任何合法键查询都是 job_not_found。
        code, record = self._show_by_key("sales-1")
        self._assert_lookup_rejected(code, record, "job_not_found")

    # --- 参数组合错误 ------------------------------------------------------

    def test_show_with_both_id_and_request_key_is_usage_error(self):
        code, stdout, stderr = self._run_cli("show", "some-id", "--request-key", "k")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数错误时标准输出不应有 JSON：%r" % stdout)
        self.assertIn("usage", stderr.lower())

    def test_show_with_neither_id_nor_request_key_is_usage_error(self):
        code, stdout, stderr = self._run_cli("show")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "参数错误时标准输出不应有 JSON：%r" % stdout)
        self.assertIn("usage", stderr.lower())

    # --- 默认数据库 ---------------------------------------------------------

    def test_default_db_used_when_db_omitted(self):
        default_db = PROJECT_ROOT / "tasks.sqlite3"
        if default_db.exists():
            self.skipTest("默认数据库已存在，为避免干扰既有数据而跳过：%s" % default_db)
        self.addCleanup(lambda: default_db.unlink(missing_ok=True))
        self._write_demo(VALID_CSV)

        def run_cli(*args):
            proc = subprocess.run(
                [sys.executable, "-m", "task_center", *args],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertTrue(proc.stdout.strip(), proc.stderr)
            return proc.returncode, json.loads(proc.stdout)

        code, queued = run_cli(
            "submit", "csv_summary", "--input", DEMO_INPUT, "--request-key", "sales-1"
        )
        job_id = self._assert_queued(code, queued)
        code, shown = run_cli("show", "--request-key", "sales-1")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, queued)
        self.assertEqual(shown["id"], job_id)


if __name__ == "__main__":
    unittest.main()
