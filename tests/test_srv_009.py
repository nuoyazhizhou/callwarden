"""SRV-009 迁移验收：server durable staging Python authority → Rust daemon。

覆盖 task `T-1787323461150-e09e1a9c` step[2] fixture_negative_matrix：
["success", "invalid", "authority", "unavailable", "restart"]。

设计要点（SRV-009 = 零生产调用方组件的权威下沉）：
- `server/durable_staging.py::DurableStagingLog` 为零生产调用方组件，
  生产链 staging 权威由 Rust 承担（`staging_log.rs` JSONL + 本卡
  `durable_staging_handlers.rs` 的 SQLite WAL 权威初始化/统计）；
  函数体受存量测试（test_phase6 功能测试 / test_phase5 `__init__`
  PRAGMA 源码断言）锁定保留，Python 侧无薄客户端接缝可 mock。
- 因此负矩阵直接针对 daemon RPC 权威接缝 `mcp.durable_staging.{init,stats}`
  （真实 daemon 集成，daemon 不可用时 runtime 段 skip），并以静态零权威
  扫描固化"生产链无 Python durable_staging 路径"不变量。
"""

from pathlib import Path

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient

ROOT = Path(__file__).resolve().parent.parent
INIT = "mcp.durable_staging.init"
STATS = "mcp.durable_staging.stats"


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
# 1) success（权威 schema 初始化 + 空库统计）
# ============================================================


def test_success_init_schema_ready(rpc, tmp_path):
    db = tmp_path / "staging.db"
    res = rpc.call(INIT, {"db_path": str(db)})
    assert res["schema_ready"] is True
    assert res["exists"] is True
    assert res["source"] == "rust"
    assert db.exists()


def test_success_stats_empty(rpc, tmp_path):
    db = tmp_path / "staging.db"
    rpc.call(INIT, {"db_path": str(db)})
    res = rpc.call(STATS, {"db_path": str(db)})
    assert res["total"] == 0
    assert res["max_lsn"] == 0
    assert res["counts"] == {
        "pending": 0, "applying": 0, "applied": 0, "failed": 0,
    }
    assert "reason" not in res


# ============================================================
# 2) invalid（异常路径 fail-soft，绝不抛错）
# ============================================================


def test_invalid_init_dir_path_fail_soft(rpc, tmp_path):
    # db_path 指向目录 → sqlite open 失败 → fail-soft 归一化降级
    res = rpc.call(INIT, {"db_path": str(tmp_path)})
    assert res["schema_ready"] is False
    assert res["reason"] == "db_open_failed"


def test_invalid_stats_missing_db_fail_soft(rpc, tmp_path):
    res = rpc.call(STATS, {"db_path": str(tmp_path / "no_such.db")})
    assert res["total"] == 0
    assert res["reason"] == "db_open_failed"


# ============================================================
# 3) authority（Rust 权威接线 + Python 权威归属声明）
# ============================================================


def test_authority_dispatch_branches_wired():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8")
    # 分支逐字对齐 step0 接线
    assert '"mcp.durable_staging.init"' in src
    assert '"mcp.durable_staging.stats"' in src
    assert "super::durable_staging_handlers::handle_init(params)" in src
    assert "super::durable_staging_handlers::handle_stats(params)" in src


def test_authority_capability_registered():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs").read_text(
        encoding="utf-8")
    assert '"mcp.durable_staging.init"' in src
    assert '"mcp.durable_staging.stats"' in src
    assert "T-1787323461150-e09e1a9c#SRV-009" in src


def test_authority_rust_handler_owns_schema_and_pragma():
    src = (ROOT / "rust_ext" / "src" / "daemon"
           / "durable_staging_handlers.rs").read_text(encoding="utf-8")
    # schema 权威：staging_entries 表 + UNIQUE 约束
    assert "CREATE TABLE IF NOT EXISTS staging_entries" in src
    assert "UNIQUE(workspace_id, rel_path, session_epoch, monotonic_seq)" in src
    # PRAGMA 集对齐 Python __init__ 批次10 补全集
    for pragma in (
        "PRAGMA busy_timeout=5000",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA wal_autocheckpoint",
        "PRAGMA cache_size",
        "PRAGMA mmap_size",
        "PRAGMA temp_store=MEMORY",
    ):
        assert pragma in src, f"Rust 权威缺失 {pragma}"


def test_authority_python_module_declares_attribution():
    src = (ROOT / "server" / "durable_staging.py").read_text(encoding="utf-8")
    # SRV-009 权威归属声明固化（step1 处置）
    assert "SRV-009 权威归属声明" in src
    assert "mcp.durable_staging.init" in src
    assert "compat/test-only" in src


# ============================================================
# 4) unavailable / no local fallback（生产链零 Python 权威路径）
# ============================================================


def test_unavailable_zero_production_callers():
    # 不变量核心：生产目录无任何 durable_staging 导入 →
    # 生产链数据库连接/业务查询不经 Python durable_staging，
    # daemon 不可用时也不存在可降级的本地权威路径
    offenders = []
    for d in ("server", "cli", "db", "analyzers"):
        base = ROOT / d
        for f in base.rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            if f.name == "durable_staging.py":
                continue
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("#"):
                    continue
                if ("import durable_staging" in s
                        or "from .durable_staging" in s
                        or "from server.durable_staging" in s
                        or "from callwarden.server.durable_staging" in s):
                    offenders.append(f"{f}: {s}")
    assert not offenders, f"生产链存在 durable_staging 调用方: {offenders}"


def test_no_business_fallback_in_module_doc():
    # 模块声明显式禁止生产路径直连 SQLite（no local fallback 契约固化）
    src = (ROOT / "server" / "durable_staging.py").read_text(encoding="utf-8")
    assert "生产路径不得使用本模块直连 SQLite" in src


def test_retained_compat_body_locked_by_legacy_assertions():
    # 保留契约：__init__ 签名与 PRAGMA 块形态受存量测试源码级断言锁定，
    # 本卡不得破坏（同步 test_phase5_cas_replicator_wiring 断言语义）
    src = (ROOT / "server" / "durable_staging.py").read_text(encoding="utf-8")
    idx = src.find("def __init__(self, db_path: str):")
    assert idx >= 0, "DurableStagingLog.__init__ 必须存在"
    block = src[idx:idx + 1000]
    for pragma in (
        "PRAGMA busy_timeout=5000",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA wal_autocheckpoint",
        "PRAGMA cache_size",
        "PRAGMA mmap_size",
        "PRAGMA temp_store=MEMORY",
    ):
        assert pragma in block, f"durable_staging.py 必须设置 {pragma}"


# ============================================================
# 5) restart（恢复后重复初始化幂等）
# ============================================================


def test_restart_init_idempotent(rpc, tmp_path):
    # 模拟 daemon 重启后再次执行权威初始化：幂等不破坏已就绪 schema
    db = tmp_path / "staging.db"
    r1 = rpc.call(INIT, {"db_path": str(db)})
    r2 = rpc.call(INIT, {"db_path": str(db)})
    assert r1["schema_ready"] is True
    assert r2["schema_ready"] is True
    # 恢复后统计仍可读
    res = rpc.call(STATS, {"db_path": str(db)})
    assert res["total"] == 0
