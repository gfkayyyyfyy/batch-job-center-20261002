"""旧版 SQLite 数据库自动兼容的公开行为回归测试。

旧版数据库的 jobs 表只有 id、type、input、status、result、error 六列，
没有请求键绑定列。README 约定旧库首次使用时自动补齐键绑定列，既有
任务的键为空，不参与请求键匹配。本模块通过 `python -m task_center`
子进程观察 JSON 输出与退出码，并以独立查询进程（show）和临时
SQLite 数据库中保存的任务记录核对结果；不改变产品实现。

每个用例自行建立临时旧库并预置两条 csv_summary 任务（succeeded 与
failed，不同 id、同一 demo 输入路径），测试结束仅清理自身数据，
可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_legacy_db_compat_regression -v

或直接：

    python tests/test_legacy_db_compat_regression.py
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
# 旧任务的输入路径也指向它，用例运行前创建、结束后删除。
DEMO_FILE = DEMO_DIR / "legacy_db_compat_case.csv"
DEMO_INPUT = "demo/legacy_db_compat_case.csv"

# demo 内保证不存在的项目相对路径（用于请求键冲突检查）。
MISSING_INPUT = "demo/legacy_db_compat_missing_file.csv"

# 旧版 jobs 表结构：仅六列，没有 request_key 列。
LEGACY_SCHEMA = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    input TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT
)
"""

# 预置的两条旧任务：不同 id、同一输入路径。
LEGACY_OK_ID = "legacy-ok-0001"
LEGACY_FAIL_ID = "legacy-fail-0002"
LEGACY_RESULT = {"row_count": 3, "total_amount": 35, "categories": {"a": 15, "b": 20}}

# 合法 CSV：表头 category,amount，数据 a,10、b,20、a,5。
VALID_CSV = "category,amount\na,10\nb,20\na,5\n"


class LegacyDbCompatRegressionTest(unittest.TestCase):
    """旧版数据库自动兼容：JSON 响应、退出码与保存的记录。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_legacy_db_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "tasks.sqlite3")
        self._create_legacy_db()
        # demo 目录缺失时自行创建，并记录以便结束后清理空目录。
        self._demo_dir_created = not DEMO_DIR.exists()
        DEMO_DIR.mkdir(exist_ok=True)
        # 演示文件若已存在则拒绝覆盖，保证只清理自身数据。
        if DEMO_FILE.exists():
            self.fail("演示文件已存在，为避免误删他人数据而中止：%s" % DEMO_FILE)
        self.addCleanup(self._remove_demo_file)

    def _create_legacy_db(self):
        """建立仅含六列的旧版数据库并预置两条 csv_summary 任务。"""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO jobs (id, type, input, status, result, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    LEGACY_OK_ID,
                    "csv_summary",
                    DEMO_INPUT,
                    "succeeded",
                    json.dumps(LEGACY_RESULT),
                    None,
                ),
            )
            conn.execute(
                "INSERT INTO jobs (id, type, input, status, result, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (LEGACY_FAIL_ID, "csv_summary", DEMO_INPUT, "failed", None, "data_error"),
            )
            conn.commit()
        finally:
            conn.close()

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

    def _show(self, job_id):
        return self._cli("show", job_id)

    def _submit(self, task_type, input_path, request_key=None):
        args = ["submit", task_type, "--input", input_path]
        if request_key is not None:
            args += ["--request-key", request_key]
        return self._cli(*args)

    def _fetch_all(self, sql, params=()):
        """直接查询数据库，确认旧库迁移与保存的记录。"""
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _job_count(self):
        return self._fetch_all("SELECT COUNT(*) FROM jobs")[0][0]

    def _assert_legacy_records_intact(self):
        """两条旧任务保持原类型、输入路径、状态、结果与错误，且无请求键。"""
        rows = self._fetch_all(
            "SELECT id, type, input, status, result, error, request_key"
            " FROM jobs WHERE id IN (?, ?) ORDER BY id",
            (LEGACY_FAIL_ID, LEGACY_OK_ID),
        )
        self.assertEqual(
            rows,
            [
                (LEGACY_FAIL_ID, "csv_summary", DEMO_INPUT, "failed", None,
                 "data_error", None),
                (LEGACY_OK_ID, "csv_summary", DEMO_INPUT, "succeeded",
                 json.dumps(LEGACY_RESULT), None, None),
            ],
        )

    # --- 旧库首次使用：show 不读取文件、不改变状态 -------------------------

    def test_show_legacy_jobs_without_input_file(self):
        # 输入文件尚不存在，确认旧库初始只有六列、两条任务。
        self.assertFalse(DEMO_FILE.exists())
        columns = [row[1] for row in self._fetch_all("PRAGMA table_info(jobs)")]
        self.assertEqual(columns, ["id", "type", "input", "status", "result", "error"])
        self.assertEqual(self._job_count(), 2)

        # 首次使用旧库：show 成功返回原有记录，退出码 0。
        code, ok = self._show(LEGACY_OK_ID)
        self.assertEqual(code, 0, ok)
        self.assertEqual(
            ok,
            {"id": LEGACY_OK_ID, "status": "succeeded",
             "result": LEGACY_RESULT, "error": None},
        )
        code, fail = self._show(LEGACY_FAIL_ID)
        self.assertEqual(code, 0, fail)
        self.assertEqual(
            fail,
            {"id": LEGACY_FAIL_ID, "status": "failed",
             "result": None, "error": "data_error"},
        )

        # 首次使用后旧库自动补齐请求键列，旧任务的键为空。
        columns = [row[1] for row in self._fetch_all("PRAGMA table_info(jobs)")]
        self.assertIn("request_key", columns)
        self._assert_legacy_records_intact()

        # 另起进程再次查询：结果相同，仍只有两条任务，输入文件仍未被读取。
        self.assertFalse(DEMO_FILE.exists())
        code, ok_again = self._show(LEGACY_OK_ID)
        self.assertEqual(code, 0, ok_again)
        self.assertEqual(ok_again, ok)
        code, fail_again = self._show(LEGACY_FAIL_ID)
        self.assertEqual(code, 0, fail_again)
        self.assertEqual(fail_again, fail)
        self.assertEqual(self._job_count(), 2)
        self._assert_legacy_records_intact()

    # --- 旧库上的请求键提交：去重、冲突与记录保持不变 -----------------------

    def test_request_key_submit_on_legacy_db(self):
        # 准备专用 CSV，使用与旧任务相同的输入路径提交有效请求键。
        DEMO_FILE.write_text(VALID_CSV, encoding="utf-8")
        code, queued = self._submit("csv_summary", DEMO_INPUT, request_key="legacy-1")
        self.assertEqual(code, 0, queued)
        new_id = queued["id"]
        self.assertIsInstance(new_id, str, queued)
        self.assertTrue(new_id, queued)
        # 新任务 id 区别于两个旧 id，queued 且 result 与 error 均为 null。
        self.assertNotIn(new_id, (LEGACY_OK_ID, LEGACY_FAIL_ID))
        self.assertEqual(
            queued,
            {"id": new_id, "status": "queued", "result": None, "error": None},
        )
        self.assertEqual(self._job_count(), 3)

        # 另起进程重复提交同键同路径：返回同一记录，不新增任务。
        code, again = self._submit("csv_summary", DEMO_INPUT, request_key="legacy-1")
        self.assertEqual(code, 0, again)
        self.assertEqual(again, queued)
        self.assertEqual(self._job_count(), 3)

        # 同一键改配 demo 下不存在的另一路径：request_conflict，退出码 1，
        # id、status、result 均为 null（不检查新路径）。
        code, conflict = self._submit(
            "csv_summary", MISSING_INPUT, request_key="legacy-1"
        )
        self.assertEqual(code, 1, conflict)
        self.assertEqual(
            conflict,
            {"id": None, "status": None, "result": None,
             "error": "request_conflict"},
        )

        # 全部已存记录保持不变：两条旧任务原样，新任务仍为 queued。
        self.assertEqual(self._job_count(), 3)
        self._assert_legacy_records_intact()
        code, shown = self._show(new_id)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, queued)
        binding = self._fetch_all(
            "SELECT request_key FROM jobs WHERE id = ?", (new_id,)
        )
        self.assertEqual(binding, [("legacy-1",)])


if __name__ == "__main__":
    unittest.main()
