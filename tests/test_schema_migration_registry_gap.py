"""C6 回归测试：schema 迁移注册表缺口与 fail-closed 守卫。

背景（巡检候选 C6）：`db_base.CodeGraphBase._get_migrations` 曾只登记 2..52、59、61，
v53-v58/v60 的迁移实现存在却未注册，`_migrate_schema` 对未注册版本 `continue`
静默跳过，导致 v52 库升级后 `MAX(version)=61` 却缺表缺列（fail-open）。

验收：
- 注册表覆盖 2..SCHEMA_VERSION 无缺口；
- v52 库迁移后 v53-v57 表与 v57 provenance 列齐备，MAX(version)=SCHEMA_VERSION；
- 注册表缺口场景抛 RuntimeError（fail-closed）；
- v52-v58 幂等补建可重复执行。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db import db_base  # noqa: E402 —— sys.path 注入须先于 import
from callwarden.db.schema import SCHEMA_VERSION  # noqa: E402

GOVERNANCE_TABLES = [
    "canonicalization_rule_sets",
    "canonicalization_rule_revocations",
    "task_operation_ledger",
    "workspace_authority_captures",
    "task_workspace_bindings",
    "task_loop_capability_promotion_events",
    "role_contract_lineages",
    "role_contract_revisions",
    "task_step_role_contract_bindings",
    "verdict_normalization_rules",
    "verdict_normalization_rule_revocations",
]


def _migrations() -> dict:
    db = db_base.CodeGraphBase.__new__(db_base.CodeGraphBase)
    return db._get_migrations()


def _make_v52_conn() -> sqlite3.Connection:
    """v52 基线库：schema_version=52 + 既有任务协同表（无 v57 列）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE schema_version ("
        "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
        "description TEXT DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO schema_version (version, applied_at, description) "
        "VALUES (52, ?, 'v52 baseline')",
        (time.time(),),
    )
    conn.execute("CREATE TABLE task_contract_revisions (contract_id TEXT)")
    conn.execute("CREATE TABLE task_verdict_events (event_id TEXT)")
    conn.execute("CREATE TABLE task_gate_decisions (decision_id TEXT)")
    conn.execute(
        "CREATE TABLE task_quality_findings (id INTEGER PRIMARY KEY, message TEXT)"
    )
    conn.commit()
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def test_registry_covers_every_version_without_gap():
    migrations = _migrations()
    missing = [v for v in range(2, SCHEMA_VERSION + 1) if v not in migrations]
    assert missing == [], "迁移注册表存在缺口: %s" % missing
    for version, entry in migrations.items():
        assert callable(entry["func"]), "v%s 缺迁移函数" % version
        assert entry["description"], "v%s 缺描述" % version


@pytest.mark.parametrize("version", [53, 54, 55, 56, 57, 58, 60])
def test_registry_includes_governance_versions(version):
    """C6 缺失的 7 个版本必须已登记。"""
    assert version in _migrations()


def test_canonical_schema_defines_governance_tables():
    """迁移补建 DDL 来自 canonical SCHEMA_TABLES_SQL 单一真相源。"""
    statements = db_base.canonical_table_statements()
    for name in GOVERNANCE_TABLES:
        assert name in statements, "canonical schema 缺表定义: %s" % name


def test_migrate_schema_fails_closed_on_registry_gap():
    """注册表缺任一中途版本时必须抛错，不能静默打标目标版本。"""
    broken = {k: v for k, v in _migrations().items() if k != 55}
    db = db_base.CodeGraphBase.__new__(db_base.CodeGraphBase)
    db.conn = _make_v52_conn()
    db._get_migrations = lambda: broken
    with pytest.raises(RuntimeError, match="missing target versions"):
        db._migrate_schema(52, SCHEMA_VERSION)


def test_v52_db_migrates_to_current_with_governance_schema():
    """v52 库经 _migrate_schema 后表/列齐备且版本推进到 SCHEMA_VERSION。"""
    conn = _make_v52_conn()
    db = db_base.CodeGraphBase.__new__(db_base.CodeGraphBase)
    db.conn = conn
    db._migrate_schema(52, SCHEMA_VERSION)

    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == SCHEMA_VERSION

    present = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for name in GOVERNANCE_TABLES:
        assert name in present, "迁移后缺表: %s" % name

    for table, columns in db_base.TASK_CONTRACT_V57_COLUMNS.items():
        have = _table_columns(conn, table)
        missing = [column for column, _ in columns if column not in have]
        assert missing == [], "%s 迁移后缺列: %s" % (table, missing)

    assert "severity" in _table_columns(conn, "task_quality_findings")


def test_v52_to_v58_repair_is_idempotent():
    """v52-v58 幂等补建重复执行不报错（v58 为漂移修复重放）。"""
    conn = _make_v52_conn()
    for _ in range(2):
        db_base._migrate_v51_to_v52(conn)
        db_base._migrate_v52_to_v53(conn)
        db_base._migrate_v53_to_v54(conn)
        db_base._migrate_v54_to_v55(conn)
        db_base._migrate_v55_to_v56(conn)
        db_base._migrate_v56_to_v57(conn)
        db_base._migrate_v57_to_v58(conn)
    assert set(GOVERNANCE_TABLES) <= {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
