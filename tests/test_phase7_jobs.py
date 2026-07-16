"""
Phase 7.0: 后台任务系统测试

测试 db_jobs.py 的 job 元数据管理：状态机、进度、取消、清理。
"""

import os
import sys
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# 直接导入模块，避免 db/__init__.py 的相对导入链
import importlib.util


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Python 3.14+ 的 dataclass 装饰器通过 sys.modules 查找模块
    # 必须在 exec_module 之前注册，否则报 'NoneType' object has no attribute '__dict__'
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_jobs_path = Path(__file__).parent.parent / "db" / "db_jobs.py"
_jobs_mod = _load_module("db_jobs", str(_jobs_path))

init_jobs_schema = _jobs_mod.init_jobs_schema
submit_job = _jobs_mod.submit_job
get_job = _jobs_mod.get_job
list_jobs = _jobs_mod.list_jobs
mark_job_running = _jobs_mod.mark_job_running
update_job_progress = _jobs_mod.update_job_progress
complete_job = _jobs_mod.complete_job
fail_job = _jobs_mod.fail_job
cancel_job = _jobs_mod.cancel_job
is_cancelled = _jobs_mod.is_cancelled
delete_job = _jobs_mod.delete_job
clear_jobs = _jobs_mod.clear_jobs
get_job_stats = _jobs_mod.get_job_stats
_generate_job_id = _jobs_mod._generate_job_id

JOB_PENDING = _jobs_mod.JOB_PENDING
JOB_RUNNING = _jobs_mod.JOB_RUNNING
JOB_COMPLETED = _jobs_mod.JOB_COMPLETED
JOB_CANCELLED = _jobs_mod.JOB_CANCELLED
JOB_FAILED = _jobs_mod.JOB_FAILED

Job = _jobs_mod.Job


# ============================================
# Fixtures
# ============================================

@pytest.fixture
def db_conn():
    """创建一个临时 SQLite 连接，含 jobs schema 和 workspaces 表"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # 创建 workspaces 表（jobs 表的外键依赖）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            root_path TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL,
            is_active INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            active_task_id TEXT DEFAULT ''
        )
    """)
    conn.execute(
        "INSERT INTO workspaces (name, root_path, created_at, is_active) VALUES (?, ?, ?, 1)",
        ("test-ws", "/tmp/test", time.time()),
    )
    conn.commit()
    ws_id = conn.execute("SELECT id FROM workspaces WHERE is_active = 1").fetchone()["id"]

    init_jobs_schema(conn)
    yield conn, ws_id
    conn.close()


# ============================================
# Job ID 生成
# ============================================

class TestGenerateJobId:
    def test_format(self):
        """job_id 格式：J-<13位时间戳>-<8位hex>"""
        jid = _generate_job_id()
        assert jid.startswith("J-")
        parts = jid.split("-")
        assert len(parts) == 3
        assert parts[0] == "J"
        assert len(parts[1]) == 13  # 毫秒时间戳
        assert len(parts[2]) == 8  # 8 hex chars（32 bit 熵，降低碰撞概率）

    def test_uniqueness(self):
        """连续生成的 job_id 必须唯一（极大概率）"""
        ids = {_generate_job_id() for _ in range(100)}
        assert len(ids) == 100

    def test_hex_suffix(self):
        """后缀是十六进制字符"""
        jid = _generate_job_id()
        suffix = jid.split("-")[-1]
        assert all(c in "0123456789abcdef" for c in suffix)


# ============================================
# 提交 + 查询
# ============================================

class TestSubmitJob:
    def test_submit_returns_pending_job(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect", {"min_lines": 10})
        assert job.job_id.startswith("J-")
        assert job.status == JOB_PENDING
        assert job.job_type == "clone_detect"
        assert job.workspace_id == ws_id
        assert job.progress == 0.0
        assert job.params == {"min_lines": 10}
        assert job.result_summary == {}
        assert job.error == ""
        assert job.cancel_requested is False
        assert job.created_at > 0
        assert job.started_at == 0
        assert job.finished_at == 0

    def test_submit_with_no_params(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "vector_index")
        assert job.params == {}

    def test_get_job_returns_submitted(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect", {"file_filter": "src/"})
        fetched = get_job(conn, job.job_id)
        assert fetched is not None
        assert fetched.job_id == job.job_id
        assert fetched.job_type == "clone_detect"

    def test_get_job_returns_none_for_unknown(self, db_conn):
        conn, ws_id = db_conn
        assert get_job(conn, "J-nonexistent") is None

    def test_job_id_is_unique(self, db_conn):
        conn, ws_id = db_conn
        j1 = submit_job(conn, ws_id, "clone_detect")
        j2 = submit_job(conn, ws_id, "clone_detect")
        assert j1.job_id != j2.job_id

    def test_params_json_serialization(self, db_conn):
        """params 含中文、嵌套结构时也能正确序列化和反序列化"""
        conn, ws_id = db_conn
        params = {
            "filter": "中文路径",
            "nested": {"a": 1, "b": [1, 2, 3]},
        }
        job = submit_job(conn, ws_id, "clone_detect", params)
        fetched = get_job(conn, job.job_id)
        assert fetched.params == params


# ============================================
# 状态机
# ============================================

class TestStateMachine:
    def test_pending_to_running(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        assert mark_job_running(conn, job.job_id) is True
        updated = get_job(conn, job.job_id)
        assert updated.status == JOB_RUNNING
        assert updated.started_at > 0

    def test_running_to_completed(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        result = {"total_groups": 5}
        assert complete_job(conn, job.job_id, result) is True
        updated = get_job(conn, job.job_id)
        assert updated.status == JOB_COMPLETED
        assert updated.progress == 1.0
        assert updated.result_summary == result
        assert updated.finished_at > 0

    def test_running_to_failed(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        assert fail_job(conn, job.job_id, "out of memory") is True
        updated = get_job(conn, job.job_id)
        assert updated.status == JOB_FAILED
        assert updated.error == "out of memory"
        assert updated.finished_at > 0

    def test_pending_to_failed(self, db_conn):
        """pending 也可以直接 fail"""
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        assert fail_job(conn, job.job_id, "handler not registered") is True
        updated = get_job(conn, job.job_id)
        assert updated.status == JOB_FAILED

    def test_complete_only_from_running(self, db_conn):
        """不能从 pending 直接 complete"""
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        # 没有 mark_running 直接 complete
        assert complete_job(conn, job.job_id, {}) is False
        # 状态不变
        assert get_job(conn, job.job_id).status == JOB_PENDING

    def test_mark_running_only_from_pending(self, db_conn):
        """不能从 completed 转 running"""
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        complete_job(conn, job.job_id, {})
        # 再次 mark_running 应该失败
        assert mark_job_running(conn, job.job_id) is False

    def test_failed_is_terminal(self, db_conn):
        """failed 状态不能转 running"""
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        fail_job(conn, job.job_id, "error")
        assert mark_job_running(conn, job.job_id) is False

    def test_completed_is_terminal(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        complete_job(conn, job.job_id, {})
        # 不能再 fail
        assert fail_job(conn, job.job_id, "late error") is False
        # 不能再 mark_running
        assert mark_job_running(conn, job.job_id) is False


# ============================================
# 进度更新
# ============================================

class TestProgressUpdate:
    def test_update_progress_running(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        assert update_job_progress(conn, job.job_id, 0.5, "half done") is True
        updated = get_job(conn, job.job_id)
        assert updated.progress == 0.5
        assert updated.message == "half done"

    def test_update_progress_clamped(self, db_conn):
        """进度值被 clamp 到 [0, 1]"""
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        update_job_progress(conn, job.job_id, -0.5, "negative")
        assert get_job(conn, job.job_id).progress == 0.0
        update_job_progress(conn, job.job_id, 1.5, "over")
        assert get_job(conn, job.job_id).progress == 1.0

    def test_update_progress_pending_fails(self, db_conn):
        """pending 状态不能更新进度"""
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        assert update_job_progress(conn, job.job_id, 0.5, "x") is False

    def test_update_progress_completed_fails(self, db_conn):
        """completed 状态不能更新进度"""
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        complete_job(conn, job.job_id, {})
        assert update_job_progress(conn, job.job_id, 0.5, "x") is False


# ============================================
# 取消
# ============================================

class TestCancel:
    def test_cancel_pending_direct_to_cancelled(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        assert cancel_job(conn, job.job_id) is True
        updated = get_job(conn, job.job_id)
        assert updated.status == JOB_CANCELLED
        assert updated.cancel_requested is True
        assert updated.finished_at > 0

    def test_cancel_running_sets_flag(self, db_conn):
        """running 状态的取消只设置 cancel_requested 标志"""
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        assert cancel_job(conn, job.job_id) is True
        updated = get_job(conn, job.job_id)
        # 状态仍是 running（executor 自行决定何时退出）
        assert updated.status == JOB_RUNNING
        assert updated.cancel_requested is True

    def test_cancel_completed_returns_false(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        complete_job(conn, job.job_id, {})
        assert cancel_job(conn, job.job_id) is False

    def test_cancel_failed_returns_false(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        fail_job(conn, job.job_id, "err")
        assert cancel_job(conn, job.job_id) is False

    def test_cancel_unknown_job_returns_false(self, db_conn):
        conn, ws_id = db_conn
        assert cancel_job(conn, "J-nonexistent") is False

    def test_is_cancelled_pending_no(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        assert is_cancelled(conn, job.job_id) is False

    def test_is_cancelled_after_cancel_pending(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        cancel_job(conn, job.job_id)
        assert is_cancelled(conn, job.job_id) is True

    def test_is_cancelled_after_cancel_running(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        cancel_job(conn, job.job_id)
        assert is_cancelled(conn, job.job_id) is True

    def test_is_cancelled_unknown_returns_true(self, db_conn):
        """不存在的 job_id 视为应该退出"""
        conn, ws_id = db_conn
        assert is_cancelled(conn, "J-nonexistent") is True


# ============================================
# 列表 + 统计
# ============================================

class TestListAndStats:
    def test_list_jobs_empty(self, db_conn):
        conn, ws_id = db_conn
        assert list_jobs(conn, ws_id) == []

    def test_list_jobs_all(self, db_conn):
        conn, ws_id = db_conn
        for _ in range(3):
            submit_job(conn, ws_id, "clone_detect")
        jobs = list_jobs(conn, ws_id)
        assert len(jobs) == 3

    def test_list_jobs_filter_by_type(self, db_conn):
        conn, ws_id = db_conn
        submit_job(conn, ws_id, "clone_detect")
        submit_job(conn, ws_id, "vector_index")
        submit_job(conn, ws_id, "clone_detect")
        only_clone = list_jobs(conn, ws_id, job_type="clone_detect")
        assert len(only_clone) == 2
        assert all(j.job_type == "clone_detect" for j in only_clone)

    def test_list_jobs_filter_by_status(self, db_conn):
        conn, ws_id = db_conn
        j1 = submit_job(conn, ws_id, "clone_detect")
        j2 = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, j1.job_id)
        complete_job(conn, j1.job_id, {})
        mark_job_running(conn, j2.job_id)
        running = list_jobs(conn, ws_id, status=JOB_RUNNING)
        assert len(running) == 1
        assert running[0].job_id == j2.job_id
        completed = list_jobs(conn, ws_id, status=JOB_COMPLETED)
        assert len(completed) == 1
        assert completed[0].job_id == j1.job_id

    def test_list_jobs_limit(self, db_conn):
        conn, ws_id = db_conn
        for _ in range(5):
            submit_job(conn, ws_id, "clone_detect")
        assert len(list_jobs(conn, ws_id, limit=3)) == 3

    def test_list_jobs_descending_by_created(self, db_conn):
        conn, ws_id = db_conn
        j1 = submit_job(conn, ws_id, "clone_detect")
        time.sleep(0.01)
        j2 = submit_job(conn, ws_id, "clone_detect")
        jobs = list_jobs(conn, ws_id)
        assert jobs[0].job_id == j2.job_id
        assert jobs[1].job_id == j1.job_id

    def test_stats_empty(self, db_conn):
        conn, ws_id = db_conn
        stats = get_job_stats(conn, ws_id)
        assert stats["total"] == 0
        assert stats["pending"] == 0

    def test_stats_with_mixed_states(self, db_conn):
        conn, ws_id = db_conn
        j1 = submit_job(conn, ws_id, "clone_detect")
        j2 = submit_job(conn, ws_id, "clone_detect")
        j3 = submit_job(conn, ws_id, "clone_detect")
        j4 = submit_job(conn, ws_id, "clone_detect")
        # j1: completed
        mark_job_running(conn, j1.job_id)
        complete_job(conn, j1.job_id, {})
        # j2: failed
        fail_job(conn, j2.job_id, "err")
        # j3: cancelled
        cancel_job(conn, j3.job_id)
        # j4: pending
        stats = get_job_stats(conn, ws_id)
        assert stats["pending"] == 1
        assert stats["running"] == 0
        assert stats["completed"] == 1
        assert stats["cancelled"] == 1
        assert stats["failed"] == 1
        assert stats["total"] == 4

    def test_workspace_isolation(self, db_conn):
        """不同 workspace 的 jobs 互不可见"""
        conn, ws_id = db_conn
        # 插入第二个 workspace
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at) VALUES (?, ?, ?)",
            ("ws2", "/tmp/ws2", time.time()),
        )
        conn.commit()
        ws2_id = conn.execute(
            "SELECT id FROM workspaces WHERE name = 'ws2'"
        ).fetchone()["id"]

        submit_job(conn, ws_id, "clone_detect")
        submit_job(conn, ws2_id, "clone_detect")
        assert len(list_jobs(conn, ws_id)) == 1
        assert len(list_jobs(conn, ws2_id)) == 1


# ============================================
# 删除 + 清理
# ============================================

class TestDeleteAndClear:
    def test_delete_completed(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        complete_job(conn, job.job_id, {})
        assert delete_job(conn, job.job_id) is True
        assert get_job(conn, job.job_id) is None

    def test_delete_cancelled(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        cancel_job(conn, job.job_id)
        assert delete_job(conn, job.job_id) is True

    def test_delete_failed(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        fail_job(conn, job.job_id, "err")
        assert delete_job(conn, job.job_id) is True

    def test_delete_pending_fails(self, db_conn):
        """pending 状态不能删除"""
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        assert delete_job(conn, job.job_id) is False
        assert get_job(conn, job.job_id) is not None

    def test_delete_running_fails(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, job.job_id)
        assert delete_job(conn, job.job_id) is False

    def test_delete_unknown_returns_false(self, db_conn):
        conn, ws_id = db_conn
        assert delete_job(conn, "J-nonexistent") is False

    def test_clear_jobs_all_terminal(self, db_conn):
        conn, ws_id = db_conn
        j1 = submit_job(conn, ws_id, "clone_detect")
        j2 = submit_job(conn, ws_id, "clone_detect")
        j3 = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, j1.job_id)
        complete_job(conn, j1.job_id, {})
        cancel_job(conn, j2.job_id)
        fail_job(conn, j3.job_id, "err")
        # j2 处于 running 前 cancel（实际是 pending→cancelled）

        deleted = clear_jobs(conn, ws_id)
        assert deleted == 3
        assert list_jobs(conn, ws_id) == []

    def test_clear_jobs_only_completed(self, db_conn):
        conn, ws_id = db_conn
        j1 = submit_job(conn, ws_id, "clone_detect")
        j2 = submit_job(conn, ws_id, "clone_detect")
        mark_job_running(conn, j1.job_id)
        complete_job(conn, j1.job_id, {})
        # j2 仍 pending

        deleted = clear_jobs(conn, ws_id, [JOB_COMPLETED])
        assert deleted == 1
        assert len(list_jobs(conn, ws_id)) == 1
        assert list_jobs(conn, ws_id)[0].job_id == j2.job_id


# ============================================
# Job dataclass
# ============================================

class TestJobDataclass:
    def test_to_dict(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect", {"k": "v"})
        d = job.to_dict()
        assert d["job_id"] == job.job_id
        assert d["job_type"] == "clone_detect"
        assert d["status"] == JOB_PENDING
        assert d["params"] == {"k": "v"}

    def test_summary(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        s = job.summary()
        assert "Job(" in s
        assert "clone_detect" in s
        assert "pending" in s

    def test_is_terminal(self, db_conn):
        conn, ws_id = db_conn
        job = submit_job(conn, ws_id, "clone_detect")
        assert job.is_terminal is False
        mark_job_running(conn, job.job_id)
        assert get_job(conn, job.job_id).is_terminal is False
        complete_job(conn, job.job_id, {})
        assert get_job(conn, job.job_id).is_terminal is True


# ============================================
# Schema 幂等性
# ============================================

class TestSchemaIdempotent:
    def test_init_schema_twice(self):
        """init_jobs_schema 可以重复调用，不报错"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                root_path TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL,
                is_active INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                active_task_id TEXT DEFAULT ''
            )
        """)
        init_jobs_schema(conn)
        init_jobs_schema(conn)  # 第二次不报错
        # 仍然可以正常插入
        job = submit_job(conn, 1, "clone_detect")
        assert job.job_id
        conn.close()
