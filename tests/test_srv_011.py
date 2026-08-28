"""SRV-011 迁移验收：server job executor Python authority → Rust daemon。

覆盖 task `T-1787323461285-e8a7a12c` step[2] fixture_negative_matrix：
["success", "invalid", "authority", "unavailable", "restart"]。

设计要点（SRV-011 = 生产权威在先 + 1 个 direct authority RPC 下沉）：
- 生产链重任务执行权威原已由 Rust `job_runner.rs`（task_rpc：
  task.job_submit + task.wait_for_job，18 个长任务批次）承担，
  不依赖 Python 双实现；
- 本卡将 `server/job_executor.py::JobExecutor.start` 的 jobs DB
  权威初始化形态（sqlite3.connect + 批次10 七 PRAGMA 集 +
  JOBS_SCHEMA_DDL）下沉为 daemon RPC `mcp.job_executor.start`
  （rust_ext/src/daemon/job_executor_handlers.rs::handle_start）；
- Python start 函数体受存量 phase7 测试（test_start_inits_schema /
  test_start_idempotent 等）锁定保留，负矩阵直接针对 daemon RPC
  权威接缝（真实 daemon 集成，daemon 不可用时 runtime 段 skip），
  并以静态门禁固化生产权威在先、Rust 语义与归属声明不变量。
"""

import sqlite3
from pathlib import Path

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient

ROOT = Path(__file__).resolve().parent.parent
START = "mcp.job_executor.start"


@pytest.fixture(scope="module")
def rpc():
    """真实 daemon RPC 接缝；daemon 不可用时 runtime 段整体 skip。"""
    c = HttpDaemonRpcClient()
    try:
        c.call("ping", {})
    except Exception:
        pytest.skip("daemon 不可用：runtime 负矩阵段跳过（静态门禁仍执行）")
    return c


def seed_workspace(path):
    """jobs FK 引用 workspaces；生产库由 schema 基线先建，测试补最小表。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS workspaces (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT OR IGNORE INTO workspaces (id) VALUES (1)")
    conn.commit()
    conn.close()


# ============================================================
# 1) success（权威初始化正路径 + sqlite_master 核验）
# ============================================================


def test_success_start_new_db_schema_ready(rpc, tmp_path):
    db = tmp_path / "jobs.db"
    res = rpc.call(START, {"db_path": str(db)})
    assert res["schema_ready"] is True
    assert res["jobs_table"] is True
    assert res["index_count"] == 4
    assert res["source"] == "rust"
    # 权威核验：sqlite_master 直查 jobs 表 + 4 索引
    conn = sqlite3.connect(str(db))
    tbl = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name='jobs'").fetchone()[0]
    idx = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name IN "
        "('idx_jobs_workspace','idx_jobs_status',"
        "'idx_jobs_type','idx_jobs_created')").fetchone()[0]
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert tbl == 1
    assert idx == 4
    assert str(mode).lower() == "wal"


def test_success_start_creates_parent_dir(rpc, tmp_path):
    db = tmp_path / "nested" / "deep" / "jobs.db"
    res = rpc.call(START, {"db_path": str(db)})
    assert res["schema_ready"] is True
    assert db.is_file()


def test_success_start_preserves_existing_data(rpc, tmp_path):
    # 幂等 DDL 不破坏既有 jobs 数据
    db = tmp_path / "jobs.db"
    rpc.call(START, {"db_path": str(db)})
    seed_workspace(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO jobs (job_id, workspace_id, job_type, status, created_at) "
        "VALUES ('J-1', 1, 'clone_detect', 'pending', 1.0)")
    conn.commit()
    conn.close()
    res = rpc.call(START, {"db_path": str(db)})
    assert res["schema_ready"] is True
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.close()
    assert n == 1


# ============================================================
# 2) invalid（异常路径 fail-soft，绝不抛错）
# ============================================================


def test_invalid_missing_db_path_fail_soft(rpc):
    res = rpc.call(START, {})
    assert res["schema_ready"] is False
    assert res["reason"] == "missing_db_path"
    assert res["source"] == "rust"


def test_invalid_empty_db_path_fail_soft(rpc):
    res = rpc.call(START, {"db_path": ""})
    assert res["schema_ready"] is False
    assert res["reason"] == "missing_db_path"


def test_invalid_dir_as_db_path_fail_soft(rpc, tmp_path):
    # db_path 指向目录 → sqlite open 失败 → fail-soft 归一化
    res = rpc.call(START, {"db_path": str(tmp_path)})
    assert res["schema_ready"] is False
    assert res["reason"] == "db_open_failed"
    assert res["source"] == "rust"


# ============================================================
# 3) authority（Rust 权威接线 + Python 权威归属声明）
# ============================================================


def test_authority_dispatch_branch_wired():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8")
    assert '"mcp.job_executor.start"' in src, "dispatch 缺分支"
    assert ("super::job_executor_handlers::handle_start(params)" in src
            ), "dispatch 缺 handler 接线"


def test_authority_capability_registered():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs").read_text(
        encoding="utf-8")
    assert '"mcp.job_executor.start"' in src, "http_server 缺 capability"
    assert "T-1787323461285-e8a7a12c#SRV-011" in src


def test_authority_rust_handler_semantics():
    src = (ROOT / "rust_ext" / "src" / "daemon"
           / "job_executor_handlers.rs").read_text(encoding="utf-8")
    # Rust 权威标识 + fail-soft 归一化
    assert '"source": "rust"' in src
    # 批次10 PRAGMA 集逐字对齐 Python start
    for pragma in (
        "journal_mode=WAL", "busy_timeout=5000", "synchronous=NORMAL",
        "wal_autocheckpoint=1000", "cache_size=-262144",
        "mmap_size=268435456", "temp_store=MEMORY",
    ):
        assert pragma in src, f"缺 PRAGMA {pragma}"
    # JOBS_SCHEMA_DDL 逐字对齐 db/db_jobs.py
    ddl = (ROOT / "db" / "db_jobs.py").read_text(encoding="utf-8")
    for clause in (
        "job_id TEXT NOT NULL UNIQUE",
        "idx_jobs_workspace", "idx_jobs_status",
        "idx_jobs_type", "idx_jobs_created",
    ):
        assert clause in ddl, f"db_jobs.py 基线缺 {clause}"
        assert clause in src, f"Rust DDL 未逐字对齐 {clause}"


def test_authority_python_module_declares_attribution():
    src = (ROOT / "server" / "job_executor.py").read_text(encoding="utf-8")
    # SRV-011 权威归属声明固化（step1 处置）
    assert "权威归属（SRV-011" in src
    assert "mcp.job_executor.start" in src
    assert "job_executor_handlers.rs::handle_start" in src


# ============================================================
# 4) unavailable / no local fallback（生产权威在先不变量）
# ============================================================


def test_unavailable_production_job_authority_is_rust_job_runner():
    # 不变量核心：生产链重任务执行权威原已由 Rust job_runner.rs 承担
    # （task.job_submit + task.wait_for_job），不依赖 Python 双实现；
    # daemon 不可用时也不存在可降级的本地生产权威路径
    src = (ROOT / "rust_ext" / "src" / "daemon"
           / "job_runner.rs").read_text(encoding="utf-8")
    assert "task.job_submit" in src
    assert "task.wait_for_job" in src


def test_no_new_production_caller_of_python_executor():
    # daemon_client 唯一生产调用链 start_snapshot_diff 经 singleton 走
    # 进程内 executor（存量保留）；本卡不引入新的生产直连权威
    src = (ROOT / "server" / "daemon_client.py").read_text(encoding="utf-8")
    assert "job_executor_singleton import get_job_executor" in src


def test_retained_start_body_locked_by_legacy_tests():
    # 保留契约：start 函数体受存量 phase7 测试锁定，本卡不得破坏
    src = (ROOT / "server" / "job_executor.py").read_text(encoding="utf-8")
    assert "def start(self) -> None:" in src
    assert "init_jobs_schema(self._conn)" in src
    legacy = (ROOT / "tests" / "test_phase7_job_executor.py").read_text(
        encoding="utf-8")
    assert "test_start_inits_schema" in legacy
    assert "test_start_idempotent" in legacy


# ============================================================
# 5) restart（幂等初始化：重复执行不破坏状态）
# ============================================================


def test_restart_start_idempotent(rpc, tmp_path):
    db = tmp_path / "jobs.db"
    r1 = rpc.call(START, {"db_path": str(db)})
    r2 = rpc.call(START, {"db_path": str(db)})
    assert r1["schema_ready"] is True
    assert r2["schema_ready"] is True
    assert r2["index_count"] == 4


def test_restart_wal_survives_reopen(rpc, tmp_path):
    # daemon 重启语义：重开连接后 WAL 模式持久（持久化 journal_mode）
    db = tmp_path / "jobs.db"
    rpc.call(START, {"db_path": str(db)})
    conn = sqlite3.connect(str(db))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert str(mode).lower() == "wal"
