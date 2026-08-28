"""SRV-010 迁移验收：server health check Python authority → Rust daemon。

覆盖 task `T-1787323461213-e46199b0` step[2] fixture_negative_matrix：
["success", "invalid", "authority", "unavailable", "restart"]。

设计要点（SRV-010 = 生产权威在先 + 4 个 direct authority RPC 下沉）：
- 生产链健康检查权威原已由 Rust `health.rs`（G14）承担，经
  `callwarden_core.health_check_all` PyO3 短路 daemon_server 生产路径
  （rollback feature `rust_daemon_health_check`=1 才回退 Python）；
- 本卡将 `server/health_check.py` 4 个 Python direct authority
  （sqlite3.connect）函数的 daemon RPC 形态下沉至
  `rust_ext/src/daemon/health_check_handlers.rs`；
- Python 函数体受存量测试（test_phase8 功能构造 /
  test_phase4_3 差分真相源）锁定保留，负矩阵直接针对 daemon RPC
  权威接缝 `mcp.health_check.*`（真实 daemon 集成，daemon 不可用时
  runtime 段 skip），并以静态门禁固化 Rust 短路与归属声明不变量。
"""

import sqlite3
from pathlib import Path

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient

ROOT = Path(__file__).resolve().parent.parent
CHECK = "mcp.health_check.check_db_registry"
WS_RECOVER = "mcp.health_check.recover_workspace_registry"
CAS_RECOVER = "mcp.health_check.recover_cas_db"
STALE_JOBS = "mcp.health_check.recover_stale_jobs"


def make_registry(path, with_jobs=False):
    """构造临时 registry：daemon_workspaces（1 active + 1 archived）+ 可选 jobs 表。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE daemon_workspaces (
            workspace_id INTEGER PRIMARY KEY,
            workspace_instance_id TEXT,
            status TEXT DEFAULT 'active',
            last_active_at REAL
        );
        INSERT INTO daemon_workspaces VALUES
            (1, 'ws-1', 'active', 0.0), (2, 'ws-2', 'archived', 0.0);
        """
    )
    if with_jobs:
        conn.executescript(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY, status TEXT,
                error TEXT, finished_at REAL
            );
            INSERT INTO jobs VALUES
                ('J-1', 'running', NULL, NULL),
                ('J-2', 'completed', NULL, 12345.0);
            """
        )
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def rpc():
    """真实 daemon RPC 接缝；daemon 不可用时 runtime 段整体 skip。"""
    c = HttpDaemonRpcClient()
    try:
        c.call("ping", {})
    except Exception:
        pytest.skip("daemon 不可用：runtime 负矩阵段跳过（静态门禁仍执行）")
    return c


# ============================================================
# 1) success（权威检查/恢复正路径 + DB 状态核验）
# ============================================================


def test_success_check_db_registry_healthy(rpc, tmp_path):
    reg = tmp_path / "registry.db"
    make_registry(reg)
    res = rpc.call(CHECK, {"registry_db_path": str(reg)})
    assert res["name"] == "db_registry"
    assert res["status"] == "healthy"
    assert res["source"] == "rust"
    assert "daemon_workspaces" in res["details"]["tables"]


def test_success_recover_workspace_registry_updates_last_active(rpc, tmp_path):
    reg = tmp_path / "registry.db"
    make_registry(reg)
    res = rpc.call(WS_RECOVER, {"registry_db_path": str(reg)})
    assert res["status"] == "healthy"
    assert res["details"]["active_workspaces"] == 1
    # 权威核验：active 更新，archived 不受影响
    conn = sqlite3.connect(str(reg))
    lat = conn.execute(
        "SELECT last_active_at FROM daemon_workspaces "
        "WHERE workspace_id=1").fetchone()[0]
    lat2 = conn.execute(
        "SELECT last_active_at FROM daemon_workspaces "
        "WHERE workspace_id=2").fetchone()[0]
    conn.close()
    assert lat and lat > 0
    assert lat2 == 0.0


def test_success_recover_stale_jobs_marks_running_failed(rpc, tmp_path):
    reg = tmp_path / "registry.db"
    make_registry(reg, with_jobs=True)
    res = rpc.call(STALE_JOBS, {"registry_db_path": str(reg)})
    assert res["status"] == "healthy"
    assert res["details"]["stale_jobs_cleaned"] == 1
    # 权威核验：running → failed（带 error），completed 不受影响
    conn = sqlite3.connect(str(reg))
    s1, err1 = conn.execute(
        "SELECT status, error FROM jobs WHERE job_id='J-1'").fetchone()
    s2 = conn.execute(
        "SELECT status FROM jobs WHERE job_id='J-2'").fetchone()[0]
    conn.close()
    assert s1 == "failed"
    assert "daemon restarted" in err1
    assert s2 == "completed"


# ============================================================
# 2) invalid（异常路径 fail-soft，绝不抛错）
# ============================================================


def test_invalid_check_missing_db_fail_soft(rpc, tmp_path):
    res = rpc.call(CHECK, {"registry_db_path": str(tmp_path / "no_such.db")})
    assert res["status"] == "unhealthy"
    assert "not found" in res["message"]
    assert res["source"] == "rust"


def test_invalid_check_missing_table_degraded(rpc, tmp_path):
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE other_table (id INTEGER)")
    conn.commit()
    conn.close()
    res = rpc.call(CHECK, {"registry_db_path": str(db)})
    assert res["status"] == "degraded"
    assert "daemon_workspaces" in res["message"]


def test_invalid_ws_recover_missing_db_fail_soft(rpc, tmp_path):
    # 缺库 → degraded（首次注册时创建），绝不抛错
    res = rpc.call(WS_RECOVER, {"registry_db_path": str(tmp_path / "no.db")})
    assert res["status"] == "degraded"


def test_invalid_cas_recover_missing_is_healthy_first_use(rpc, tmp_path):
    # 对齐 Python：CAS DB 不存在是正常的（首次启动）
    res = rpc.call(CAS_RECOVER, {"cas_db_path": str(tmp_path / "cas.db")})
    assert res["status"] == "healthy"
    assert "first use" in res["message"]


def test_invalid_stale_jobs_missing_db_fail_soft(rpc, tmp_path):
    res = rpc.call(STALE_JOBS, {"registry_db_path": str(tmp_path / "no.db")})
    assert res["status"] == "healthy"
    assert "no registry DB" in res["message"]


# ============================================================
# 3) authority（Rust 权威接线 + Python 权威归属声明）
# ============================================================


def test_authority_dispatch_branches_wired():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8")
    for method, handler in (
        ("mcp.health_check.check_db_registry", "handle_check_db_registry"),
        ("mcp.health_check.recover_workspace_registry",
         "handle_recover_workspace_registry"),
        ("mcp.health_check.recover_cas_db", "handle_recover_cas_db"),
        ("mcp.health_check.recover_stale_jobs", "handle_recover_stale_jobs"),
    ):
        assert f'"{method}"' in src, f"dispatch 缺分支 {method}"
        assert (f"super::health_check_handlers::{handler}(params)" in src
                ), f"dispatch 缺 handler 接线 {handler}"


def test_authority_capability_registered():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs").read_text(
        encoding="utf-8")
    for method in (
        "mcp.health_check.check_db_registry",
        "mcp.health_check.recover_workspace_registry",
        "mcp.health_check.recover_cas_db",
        "mcp.health_check.recover_stale_jobs",
    ):
        assert f'"{method}"' in src, f"http_server 缺 capability {method}"
    assert "T-1787323461213-e46199b0#SRV-010" in src


def test_authority_rust_handler_semantics():
    src = (ROOT / "rust_ext" / "src" / "daemon"
           / "health_check_handlers.rs").read_text(encoding="utf-8")
    # fail-soft 归一化 + Rust 权威标识
    assert '"source": "rust"' in src
    # 锁等待对齐项目规范
    assert "PRAGMA busy_timeout=5000" in src
    # 恢复语义逐字对齐 Python：active 刷新 + running→failed
    assert ("UPDATE daemon_workspaces SET last_active_at" in src)
    assert "daemon restarted, job interrupted" in src


def test_authority_python_module_declares_attribution():
    src = (ROOT / "server" / "health_check.py").read_text(encoding="utf-8")
    # SRV-010 权威归属声明固化（step1 处置）
    assert "SRV-010 权威归属声明" in src
    assert "mcp.health_check.check_db_registry" in src
    assert "compat/test-only" in src


# ============================================================
# 4) unavailable / no local fallback（生产链 Rust 短路不变量）
# ============================================================


def test_unavailable_production_path_is_rust_short_circuit():
    # 不变量核心：daemon_server 生产 health 路径默认 Rust 短路
    # （callwarden_core.health_check_all），仅 rollback feature 或
    # Rust 失败 fail-soft 时才走 Python——Python health_check 无
    # 生产权威地位，daemon 不可用时也不存在可降级的本地权威路径
    src = (ROOT / "server" / "daemon_server.py").read_text(encoding="utf-8")
    assert "health_check_all" in src
    assert "_rust_health_available()" in src
    assert "rust_daemon_health_check" in src


def test_no_business_fallback_in_module_doc():
    # 模块声明显式禁止生产路径直连 SQLite（no local fallback 契约固化）
    src = (ROOT / "server" / "health_check.py").read_text(encoding="utf-8")
    assert "生产路径不得使用本模块直连 SQLite" in src


def test_retained_compat_body_locked_by_legacy_tests():
    # 保留契约：4 个 direct authority 函数体受存量测试锁定，本卡不得破坏
    src = (ROOT / "server" / "health_check.py").read_text(encoding="utf-8")
    for fn in (
        "def _check_db_registry",
        "def _recover_workspace_registry",
        "def _recover_cas_db",
        "def _recover_stale_jobs",
    ):
        assert fn in src, f"health_check.py 必须保留 {fn} 原形态"


# ============================================================
# 5) restart（恢复操作幂等：重复执行不破坏状态）
# ============================================================


def test_restart_ws_recover_idempotent(rpc, tmp_path):
    reg = tmp_path / "registry.db"
    make_registry(reg)
    r1 = rpc.call(WS_RECOVER, {"registry_db_path": str(reg)})
    r2 = rpc.call(WS_RECOVER, {"registry_db_path": str(reg)})
    assert r1["status"] == "healthy"
    assert r2["status"] == "healthy"
    assert r2["details"]["active_workspaces"] == 1


def test_restart_stale_jobs_second_run_zero_cleaned(rpc, tmp_path):
    # daemon 重启恢复语义：首次清理 running → failed；二次执行无 stale
    reg = tmp_path / "registry.db"
    make_registry(reg, with_jobs=True)
    r1 = rpc.call(STALE_JOBS, {"registry_db_path": str(reg)})
    r2 = rpc.call(STALE_JOBS, {"registry_db_path": str(reg)})
    assert r1["details"]["stale_jobs_cleaned"] == 1
    assert r2["details"]["stale_jobs_cleaned"] == 0
    assert r2["status"] == "healthy"
