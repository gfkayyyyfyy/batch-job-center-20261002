"""旧版 SQLite 数据库自动兼容的公开行为回归测试。

旧版 jobs 表只有 id、type、input、status、result、error 六列（没有
request_key 绑定列）。本模块手工建立这样的临时旧库并预置两条
csv_summary 任务（succeeded 与 failed 各一条，不同 id、同一 demo
输入路径），通过 `python -m task_center` 子进程观察 JSON 输出与
退出码，核对：

- 旧库首次被新版本打开时自动补齐 request_key 列，旧记录原样可读；
- show 不读取输入文件，文件尚不存在时旧记录的状态与结果也不变；
- 再次以独立进程打开旧库，记录、类型、输入路径与无键绑定均保持；
- 在迁移后的旧库里用有效请求键提交同路径任务，正常创建 queued
  任务，同键跨进程去重，键改配其他路径返回 request_conflict 且
  全部已存记录保持不变。

每个用例使用独立的临时 SQLite 数据库（--db 指定）；演示 CSV 由
测试在 demo 目录下自行准备专用文件，同名样例已存在时直接失败且
不覆盖。测试结束仅清理自身数据，保留原有 demo 目录与其他文件，
可连续重复运行。

运行方式（项目根目录下，仅需 Python 3 标准库）：

    python -m unittest tests.test_legacy_db_regression -v

或直接：

    python tests/test_legacy_db_regression.py
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
# 旧任务的输入路径与后续带键提交使用的路径都是它。查询旧库阶段
# 该文件刻意尚不存在，以验证 show 不读取文件。
DEMO_FILE = DEMO_DIR / "legacy_db_regression_case.csv"
DEMO_INPUT = "demo/legacy_db_regression_case.csv"

# demo 内保证不存在的另一条路径，用于验证键改配路径时的冲突拒绝。
OTHER_MISSING_INPUT = "demo/legacy_db_regression_other_missing.csv"

# 合法 CSV：表头 category,amount，数据 a,10、b,20、a,5。
VALID_CSV = "category,amount\na,10\nb,20\na,5\n"
EXPECTED_RESULT = {
    "row_count": 3,
    "total_amount": 35,
    "categories": {"a": 15, "b": 20},
}

# 预置旧任务使用的固定 id（32 位十六进制风格，与 uuid4().hex 同形）。
OLD_OK_ID = "0123abcd" * 4
OLD_FAIL_ID = "fedcba98" * 4

# 旧表仅有的六列；request_key 列由新版本首次打开时自动补齐。
LEGACY_COLUMNS = ["id", "type", "input", "status", "result", "error"]
MIGRATED_COLUMNS = LEGACY_COLUMNS + ["request_key"]

# demo 目录是否在本模块开始准备前就已存在，决定结束时能否删除空目录。
_demo_preexisting = DEMO_DIR.exists()


def setUpModule():
    """准备演示目录并拒绝覆盖同名样例。

    demo 目录不存在时由测试自行创建；专用样例若已存在则直接令本
    模块失败（失败信息包含冲突路径），绝不覆盖或删除任何既有文件。
    """
    if DEMO_FILE.exists():
        raise AssertionError(
            "演示样例已存在，为避免覆盖或误删他人数据而中止：%s" % DEMO_FILE
        )
    DEMO_DIR.mkdir(exist_ok=True)


def tearDownModule():
    """回收本模块自建数据。

    删除专用样例（样例在用例结束后应已不存在，缺失时不报错）；仅
    当 demo 目录由本模块创建且删除样例后已经为空时才移除目录，
    原本存在的 demo 及其中其他文件一律保留。
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


class LegacyDbRegressionTest(unittest.TestCase):
    """旧库自动兼容的公开行为：JSON 响应、退出码与保存的任务数据。"""

    def setUp(self):
        # 独立数据库：每个用例一个临时目录，避免互相影响与污染默认库。
        self._tmp = tempfile.TemporaryDirectory(prefix="task_center_legacy_db_test_")
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

    def _create_legacy_db(self):
        """手工建立仅含六列的旧库，并预置一成功一失败两条旧任务。"""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "CREATE TABLE jobs ("
                "id TEXT PRIMARY KEY,"
                "type TEXT NOT NULL,"
                "input TEXT NOT NULL,"
                "status TEXT NOT NULL,"
                "result TEXT,"
                "error TEXT"
                ")"
            )
            # 成功任务：result 为完整汇总 JSON，error 为 NULL。
            conn.execute(
                "INSERT INTO jobs (id, type, input, status, result, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    OLD_OK_ID,
                    "csv_summary",
                    DEMO_INPUT,
                    "succeeded",
                    json.dumps(EXPECTED_RESULT, ensure_ascii=False),
                    None,
                ),
            )
            # 失败任务：result 为 NULL，error 为 data_error。
            conn.execute(
                "INSERT INTO jobs (id, type, input, status, result, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    OLD_FAIL_ID,
                    "csv_summary",
                    DEMO_INPUT,
                    "failed",
                    None,
                    "data_error",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _table_columns(self, conn):
        return [row[1] for row in conn.execute("PRAGMA table_info(jobs)")]

    def _assert_legacy_shape_before_migration(self):
        """确认测试自行建立的确实是只有六列的旧库。"""
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(self._table_columns(conn), LEGACY_COLUMNS)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2
            )
        finally:
            conn.close()

    def _dump_jobs(self):
        """读取全部保存的任务行（按 id 排序）及表结构，供数据核对。"""
        conn = sqlite3.connect(self.db)
        try:
            columns = self._table_columns(conn)
            rows = conn.execute(
                "SELECT id, type, input, status, result, error, request_key"
                " FROM jobs ORDER BY id"
            ).fetchall()
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finally:
            conn.close()
        return columns, rows, count

    def _cli(self, *args):
        """调用 python -m task_center，返回 (退出码, 解析后的 JSON 记录)。

        每次调用都是独立进程，用于验证绑定与迁移结果跨进程保留。
        """
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

    def _submit(self, task_type, input_path, request_key):
        return self._cli(
            "submit", task_type, "--input", input_path, "--request-key", request_key
        )

    def _assert_old_records_shown(self):
        """两条旧任务均按原样返回，退出码 0。"""
        code, ok = self._show(OLD_OK_ID)
        self.assertEqual(code, 0, ok)
        self.assertEqual(
            ok,
            {
                "id": OLD_OK_ID,
                "status": "succeeded",
                "result": EXPECTED_RESULT,
                "error": None,
            },
        )
        code, failed = self._show(OLD_FAIL_ID)
        self.assertEqual(code, 0, failed)
        self.assertEqual(
            failed,
            {
                "id": OLD_FAIL_ID,
                "status": "failed",
                "result": None,
                "error": "data_error",
            },
        )
        return ok, failed

    # --- 旧库首次使用与再次打开 ------------------------------------------

    def test_legacy_db_records_remain_readable_without_input_file(self):
        self._create_legacy_db()
        self._assert_legacy_shape_before_migration()
        # 查询阶段输入文件刻意不存在：show 不应依赖文件存在。
        self.assertFalse(DEMO_FILE.exists())

        # 旧库首次被新版本打开：自动补齐 request_key 列，记录原样可读。
        ok_first, failed_first = self._assert_old_records_shown()
        # 文件未被创建，状态也没有因缺文件而改变。
        self.assertFalse(DEMO_FILE.exists())

        # 另起进程再次打开旧库，得到完全相同的结果。
        ok_again, failed_again = self._assert_old_records_shown()
        self.assertEqual(ok_again, ok_first)
        self.assertEqual(failed_again, failed_first)

        # 库中仍只有两条任务：原类型、输入路径、状态、结果不变，
        # 表已迁移为七列，旧任务 request_key 均为 NULL（无键绑定）。
        columns, rows, count = self._dump_jobs()
        self.assertEqual(columns, MIGRATED_COLUMNS)
        self.assertEqual(count, 2)
        self.assertEqual(
            rows,
            [
                (
                    OLD_OK_ID,
                    "csv_summary",
                    DEMO_INPUT,
                    "succeeded",
                    json.dumps(EXPECTED_RESULT, ensure_ascii=False),
                    None,
                    None,
                ),
                (
                    OLD_FAIL_ID,
                    "csv_summary",
                    DEMO_INPUT,
                    "failed",
                    None,
                    "data_error",
                    None,
                ),
            ],
        )

    # --- 迁移后的旧库上使用请求键 ----------------------------------------

    def test_keyed_submit_coexists_with_legacy_jobs(self):
        self._create_legacy_db()
        self._assert_legacy_shape_before_migration()

        # 在 demo 下准备专用 UTF-8 CSV，使用与旧任务相同的输入路径。
        DEMO_FILE.write_text(VALID_CSV, encoding="utf-8")

        # 首次以有效键 legacy-1 提交：旧任务无键绑定、不参与匹配，
        # 正常生成区别于两个旧 id 的 queued 任务。
        code, created = self._submit("csv_summary", DEMO_INPUT, "legacy-1")
        self.assertEqual(code, 0, created)
        new_id = created["id"]
        self.assertTrue(isinstance(new_id, str) and new_id, created)
        self.assertNotIn(new_id, (OLD_OK_ID, OLD_FAIL_ID))
        self.assertEqual(
            created,
            {"id": new_id, "status": "queued", "result": None, "error": None},
        )

        columns, rows, count = self._dump_jobs()
        self.assertEqual(columns, MIGRATED_COLUMNS)
        self.assertEqual(count, 3)
        bindings = {row[0]: row[6] for row in rows}
        self.assertEqual(bindings[OLD_OK_ID], None)
        self.assertEqual(bindings[OLD_FAIL_ID], None)
        self.assertEqual(bindings[new_id], "legacy-1")

        # 另起进程重复提交同键同请求：返回同一记录，不新增任务。
        code, again = self._submit("csv_summary", DEMO_INPUT, "legacy-1")
        self.assertEqual(code, 0, again)
        self.assertEqual(again, created)
        _columns, _rows, count = self._dump_jobs()
        self.assertEqual(count, 3)

        # 两条旧记录仍然原样可读。
        self._assert_old_records_shown()

        # 全部已存记录快照，用于冲突拒绝后逐字节比对。
        _columns, before_conflict, _count = self._dump_jobs()

        # 将该键改配 demo 下不存在的另一条路径：request_conflict，
        # 退出码 1，id/status/result 均为 null，不检查新路径。
        self.assertFalse((PROJECT_ROOT / OTHER_MISSING_INPUT).exists())
        code, conflict = self._submit(
            "csv_summary", OTHER_MISSING_INPUT, "legacy-1"
        )
        self.assertEqual(code, 1, conflict)
        self.assertEqual(
            conflict,
            {"id": None, "status": None, "result": None, "error": "request_conflict"},
        )

        # 库中总计仍三条任务，全部已存记录（含键绑定）逐行不变。
        _columns, after_conflict, count = self._dump_jobs()
        self.assertEqual(count, 3)
        self.assertEqual(after_conflict, before_conflict)

        # 新任务仍是 queued；另起进程同键同路径提交仍返回同一记录。
        code, shown = self._show(new_id)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown, created)
        code, once_more = self._submit("csv_summary", DEMO_INPUT, "legacy-1")
        self.assertEqual(code, 0, once_more)
        self.assertEqual(once_more, created)
        _columns, _rows, count = self._dump_jobs()
        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
