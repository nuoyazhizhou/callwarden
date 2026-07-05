"""Agent Rule Memory schema 与迁移测试。

覆盖 Phase 1：
- 新库建表（v23）
- 旧库 v22 -> v23 迁移
- 三张表的字段、默认值、索引存在性
"""

import os
import sqlite3
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB
from callwarden.db.schema import SCHEMA_VERSION


# ============================================
# 新库建表
# ============================================


def test_schema_version_is_23():
    """SCHEMA_VERSION 应为 23"""
    assert SCHEMA_VERSION == 23


def test_new_db_has_agent_rule_tables():
    """新库应包含三张 agent_rule 表"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        tabs = [
            r[0]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_rule%'"
            )
        ]
        assert "agent_rule_candidates" in tabs
        assert "agent_rules" in tabs
        assert "agent_rule_sync_log" in tabs
        db.close()


def test_new_db_schema_version_is_23():
    """新库 schema_version 表中应有 23 记录"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        v = db.conn.execute(
            "SELECT MAX(version) as v FROM schema_version"
        ).fetchone()
        assert v["v"] == 23
        db.close()


def test_agent_rule_candidates_indexes():
    """agent_rule_candidates 应有 status/source/severity 三个索引"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        idx = [
            r[0]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_agent_rule_candidates%'"
            )
        ]
        assert "idx_agent_rule_candidates_status" in idx
        assert "idx_agent_rule_candidates_source" in idx
        assert "idx_agent_rule_candidates_severity" in idx
        db.close()


def test_agent_rules_indexes():
    """agent_rules 应有 status/severity/synced 三个索引"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        idx = [
            r[0]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_agent_rules%'"
            )
        ]
        assert "idx_agent_rules_status" in idx
        assert "idx_agent_rules_severity" in idx
        assert "idx_agent_rules_synced" in idx
        db.close()


def test_agent_rule_sync_log_indexes():
    """agent_rule_sync_log 应有 target/created 两个索引"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        idx = [
            r[0]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_agent_rule_sync_log%'"
            )
        ]
        assert "idx_agent_rule_sync_log_target" in idx
        assert "idx_agent_rule_sync_log_created" in idx
        db.close()


# ============================================
# 字段与默认值
# ============================================


def test_agent_rule_candidates_defaults():
    """agent_rule_candidates 默认值应符合设计"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        # 仅插入必填字段，其余走默认
        db.conn.execute(
            "INSERT INTO agent_rule_candidates (id, title, rule_text, created_at) "
            "VALUES ('ARC-test-1', 't', 'r', 1.0)"
        )
        db.conn.commit()
        row = db.conn.execute(
            "SELECT scope_json, severity, source, evidence_json, confidence, "
            "       status, reviewed_at, reviewer, linked_rule_id "
            "FROM agent_rule_candidates WHERE id = 'ARC-test-1'"
        ).fetchone()
        assert row["scope_json"] == "{}"
        assert row["severity"] == "info"
        assert row["source"] == "manual"
        assert row["evidence_json"] == "{}"
        assert row["confidence"] == 0.0
        assert row["status"] == "pending"
        assert row["reviewed_at"] is None
        assert row["reviewer"] == ""
        assert row["linked_rule_id"] == ""
        db.close()


def test_agent_rules_defaults():
    """agent_rules 默认值应符合设计"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        db.conn.execute(
            "INSERT INTO agent_rules (id, title, rule_text, created_at, updated_at) "
            "VALUES ('AR-test-1', 't', 'r', 1.0, 1.0)"
        )
        db.conn.commit()
        row = db.conn.execute(
            "SELECT scope_json, severity, status, source_candidate_id, evidence_json, "
            "       synced_to_agents_md, sync_hash "
            "FROM agent_rules WHERE id = 'AR-test-1'"
        ).fetchone()
        assert row["scope_json"] == "{}"
        assert row["severity"] == "info"
        assert row["status"] == "active"
        assert row["source_candidate_id"] == ""
        assert row["evidence_json"] == "{}"
        assert row["synced_to_agents_md"] == 0
        assert row["sync_hash"] == ""
        db.close()


def test_agent_rule_sync_log_defaults():
    """agent_rule_sync_log 默认值应符合设计"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        db.conn.execute(
            "INSERT INTO agent_rule_sync_log (id, target_path, created_at) "
            "VALUES ('LOG-1', 'AGENTS.md', 1.0)"
        )
        db.conn.commit()
        row = db.conn.execute(
            "SELECT rule_ids_json, before_hash, after_hash, dry_run, actor "
            "FROM agent_rule_sync_log WHERE id = 'LOG-1'"
        ).fetchone()
        assert row["rule_ids_json"] == "[]"
        assert row["before_hash"] == ""
        assert row["after_hash"] == ""
        assert row["dry_run"] == 1
        assert row["actor"] == "agent"
        db.close()


# ============================================
# 旧库迁移
# ============================================


def test_legacy_v22_db_migrates_to_v23():
    """v22 旧库应能迁移到 v23"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "callwarden.db")
        # 先建一个 v23 库，再回退到 v22 模拟旧库
        db1 = CodeGraphDB(db_path=db_path, workspace_root=tmp)
        db1.close()

        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            DROP TABLE IF EXISTS agent_rule_candidates;
            DROP TABLE IF EXISTS agent_rules;
            DROP TABLE IF EXISTS agent_rule_sync_log;
            DELETE FROM schema_version WHERE version = 23;
            """
        )
        conn.commit()
        cur = conn.execute("SELECT MAX(version) as v FROM schema_version")
        row = cur.fetchone()
        assert row[0] is None or row[0] < 23
        conn.close()

        # 重新打开触发迁移
        db2 = CodeGraphDB(db_path=db_path, workspace_root=tmp)
        v = db2.conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert v["version"] == 23
        tabs = [
            r[0]
            for r in db2.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_rule%'"
            )
        ]
        assert set(tabs) == {
            "agent_rule_candidates",
            "agent_rules",
            "agent_rule_sync_log",
        }
        db2.close()


def test_migration_is_idempotent():
    """迁移函数重复执行应幂等（CREATE TABLE IF NOT EXISTS）"""
    from callwarden.db.db_base import _migrate_v22_to_v23

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "callwarden.db")
        conn = sqlite3.connect(db_path)
        # 第一次执行
        _migrate_v22_to_v23(conn)
        # 第二次执行应不报错
        _migrate_v22_to_v23(conn)
        tabs = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_rule%'"
            )
        ]
        assert len(tabs) == 3
        conn.close()
