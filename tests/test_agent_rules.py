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


# ============================================
# Phase 1: AgentRulesMixin CRUD
# ============================================


def test_rule_candidate_create_returns_id():
    """rule_candidate_create 应返回 ARC- 前缀的 ID"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create("title", "text")
        assert cid.startswith("ARC-"), f"ID 应以 ARC- 开头, 实际: {cid}"
        db.close()


def test_rule_candidate_create_default_values():
    """新候选规则应有正确的默认值"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create("t", "r")
        cands = db.rule_candidate_list(status="pending")
        assert len(cands) == 1
        c = cands[0]
        assert c["id"] == cid
        assert c["scope"] == {}
        assert c["severity"] == "info"
        assert c["source"] == "manual"
        assert c["evidence"] == {}
        assert c["confidence"] == 0.0
        assert c["status"] == "pending"
        assert c["reviewed_at"] is None
        assert c["reviewer"] == ""
        assert c["linked_rule_id"] == ""
        db.close()


def test_rule_candidate_create_with_full_fields():
    """带完整字段的候选规则创建"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create(
            title="i18n 规则",
            rule_text="不要硬编码",
            scope={"languages": ["python"], "actions": ["edit"]},
            severity="warning",
            source="auto_quality_findings",
            evidence={"task_id": "T-xxx", "occurrences": 3},
            confidence=0.85,
        )
        c = db.rule_candidate_list()[0]
        assert c["scope"] == {"languages": ["python"], "actions": ["edit"]}
        assert c["severity"] == "warning"
        assert c["source"] == "auto_quality_findings"
        assert c["evidence"] == {"task_id": "T-xxx", "occurrences": 3}
        assert c["confidence"] == 0.85
        db.close()


def test_rule_candidate_create_invalid_severity_falls_back():
    """非法 severity 应回落到 info"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        db.rule_candidate_create("t", "r", severity="bogus")
        c = db.rule_candidate_list()[0]
        assert c["severity"] == "info"
        db.close()


def test_rule_candidate_create_confidence_clamped():
    """confidence 超出 [0, 1] 应被夹紧"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        db.rule_candidate_create("t", "r", confidence=1.5)
        c = db.rule_candidate_list()[0]
        assert c["confidence"] == 1.0

        db.rule_candidate_create("t2", "r", confidence=-0.5)
        cs = db.rule_candidate_list()
        # 找到 confidence 为 0.0 的（第二个）
        c2 = next(x for x in cs if x["title"] == "t2")
        assert c2["confidence"] == 0.0
        db.close()


def test_rule_candidate_create_empty_title_raises():
    """空标题应抛出 ValueError"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        with pytest.raises(ValueError):
            db.rule_candidate_create("", "text")
        with pytest.raises(ValueError):
            db.rule_candidate_create("   ", "text")
        db.close()


def test_rule_candidate_create_empty_text_raises():
    """空正文应抛出 ValueError"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        with pytest.raises(ValueError):
            db.rule_candidate_create("title", "")
        db.close()


def test_rule_candidate_list_status_filter():
    """按状态过滤候选规则"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid1 = db.rule_candidate_create("p1", "r")
        cid2 = db.rule_candidate_create("p2", "r")
        cid3 = db.rule_candidate_create("p3", "r")

        # 全部 pending
        pendings = db.rule_candidate_list(status="pending")
        assert len(pendings) == 3

        # 接受 cid1
        db.rule_candidate_accept(cid1)
        pendings = db.rule_candidate_list(status="pending")
        assert len(pendings) == 2
        accepted = db.rule_candidate_list(status="accepted")
        assert len(accepted) == 1
        assert accepted[0]["id"] == cid1
        db.close()


def test_rule_candidate_list_limit():
    """limit 限制返回数量"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        for i in range(5):
            db.rule_candidate_create(f"t{i}", "r")
        result = db.rule_candidate_list(limit=3)
        assert len(result) == 3
        db.close()


def test_rule_candidate_list_empty_status_returns_all():
    """status='' 返回所有状态"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid1 = db.rule_candidate_create("p1", "r")
        cid2 = db.rule_candidate_create("p2", "r")
        db.rule_candidate_accept(cid1)
        db.rule_candidate_reject(cid2)

        all_cands = db.rule_candidate_list(status="")
        assert len(all_cands) == 2
        db.close()


def test_rule_candidate_accept_creates_active_rule():
    """accept 候选规则应创建 active 规则"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create(
            "i18n", "text", scope={"languages": ["python"]}, severity="warning"
        )
        rid = db.rule_candidate_accept(cid, reviewer="user")

        assert rid.startswith("AR-")
        rules = db.rule_list(status="active")
        assert len(rules) == 1
        r = rules[0]
        assert r["id"] == rid
        assert r["title"] == "i18n"
        assert r["scope"] == {"languages": ["python"]}
        assert r["severity"] == "warning"
        assert r["status"] == "active"
        assert r["source_candidate_id"] == cid
        assert r["synced_to_agents_md"] is False
        assert r["sync_hash"] == ""
        db.close()


def test_rule_candidate_accept_updates_candidate_status():
    """accept 后 candidate 状态应变为 accepted，记录 reviewer/linked_rule_id"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create("t", "r")
        rid = db.rule_candidate_accept(cid, reviewer="alice")

        cands = db.rule_candidate_list(status="accepted")
        assert len(cands) == 1
        c = cands[0]
        assert c["status"] == "accepted"
        assert c["reviewer"] == "alice"
        assert c["linked_rule_id"] == rid
        assert c["reviewed_at"] is not None
        db.close()


def test_rule_candidate_accept_idempotent():
    """重复 accept 同一 candidate 应返回原 rule_id，不创建重复规则"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create("t", "r")
        rid1 = db.rule_candidate_accept(cid, reviewer="a")
        rid2 = db.rule_candidate_accept(cid, reviewer="b")
        assert rid1 == rid2

        rules = db.rule_list()
        assert len(rules) == 1
        db.close()


def test_rule_candidate_accept_rejected_raises():
    """accept 已 rejected 的 candidate 应抛出 ValueError"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create("t", "r")
        db.rule_candidate_reject(cid, reason="no")
        with pytest.raises(ValueError):
            db.rule_candidate_accept(cid)
        db.close()


def test_rule_candidate_accept_nonexistent_raises():
    """accept 不存在的 candidate 应抛出 ValueError"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        with pytest.raises(ValueError):
            db.rule_candidate_accept("ARC-nonexistent")
        db.close()


def test_rule_candidate_reject_marks_status():
    """reject 候选规则应更新状态为 rejected，并记录 reason"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create("t", "r")
        ok = db.rule_candidate_reject(cid, reviewer="bob", reason="不适用")
        assert ok is True

        cands = db.rule_candidate_list(status="rejected")
        assert len(cands) == 1
        c = cands[0]
        assert c["status"] == "rejected"
        assert c["reviewer"] == "bob"
        assert c["reviewed_at"] is not None
        assert c["evidence"].get("reject_reason") == "不适用"
        db.close()


def test_rule_candidate_reject_idempotent():
    """重复 reject 不报错"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create("t", "r")
        ok1 = db.rule_candidate_reject(cid, reason="first")
        ok2 = db.rule_candidate_reject(cid, reason="second")
        assert ok1 is True
        assert ok2 is True
        db.close()


def test_rule_candidate_reject_accepted_raises():
    """reject 已 accepted 的 candidate 应抛出 ValueError"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create("t", "r")
        db.rule_candidate_accept(cid)
        with pytest.raises(ValueError):
            db.rule_candidate_reject(cid)
        db.close()


def test_rule_candidate_reject_preserves_evidence():
    """reject 应保留原 evidence，只追加 reject_reason 字段"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create(
            "t", "r", evidence={"task_id": "T-xxx", "occurrences": 3}
        )
        db.rule_candidate_reject(cid, reason="不适用")
        c = db.rule_candidate_list(status="rejected")[0]
        assert c["evidence"]["task_id"] == "T-xxx"
        assert c["evidence"]["occurrences"] == 3
        assert c["evidence"]["reject_reason"] == "不适用"
        db.close()


def test_rule_list_default_active_only():
    """rule_list 默认只返回 active 规则"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create("t", "r")
        db.rule_candidate_accept(cid)

        rules = db.rule_list()
        assert len(rules) == 1
        assert rules[0]["status"] == "active"
        db.close()


def test_rule_list_orders_by_severity():
    """rule_list 应按 severity 优先级排序（critical 在前）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        # 创建不同 severity 的候选并 accept
        for sev, title in [
            ("info", "info-rule"),
            ("critical", "critical-rule"),
            ("warning", "warning-rule"),
            ("error", "error-rule"),
        ]:
            cid = db.rule_candidate_create(title, "r", severity=sev)
            db.rule_candidate_accept(cid)

        rules = db.rule_list()
        assert rules[0]["severity"] == "critical"
        assert rules[1]["severity"] == "error"
        assert rules[2]["severity"] == "warning"
        assert rules[3]["severity"] == "info"
        db.close()


def test_rule_list_empty_status_returns_all():
    """rule_list status='' 返回所有规则"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid1 = db.rule_candidate_create("active1", "r")
        cid2 = db.rule_candidate_create("active2", "r")
        db.rule_candidate_accept(cid1)
        db.rule_candidate_accept(cid2)

        # 手动把 cid2 的规则改为 deprecated
        rules = db.rule_list(status="active")
        rid2 = next(r["id"] for r in rules if r["title"] == "active2")
        db.conn.execute(
            "UPDATE agent_rules SET status = ? WHERE id = ?",
            ("deprecated", rid2),
        )
        db.conn.commit()

        all_rules = db.rule_list(status="")
        assert len(all_rules) == 2

        only_active = db.rule_list(status="active")
        assert len(only_active) == 1
        assert only_active[0]["title"] == "active1"
        db.close()


def test_rule_list_limit():
    """rule_list limit 限制返回数量"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        for i in range(5):
            cid = db.rule_candidate_create(f"r{i}", "text")
            db.rule_candidate_accept(cid)

        rules = db.rule_list(limit=3)
        assert len(rules) == 3
        db.close()


def test_rule_candidate_list_orders_by_created_desc():
    """候选规则列表按创建时间倒序（最新在前）"""
    import time as _time

    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        db.rule_candidate_create("first", "r")
        _time.sleep(0.01)  # 确保时间戳不同
        db.rule_candidate_create("second", "r")

        cands = db.rule_candidate_list()
        assert cands[0]["title"] == "second"
        assert cands[1]["title"] == "first"
        db.close()


def test_rule_candidate_reject_nonexistent_raises():
    """reject 不存在的 candidate 应抛出 ValueError"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        with pytest.raises(ValueError):
            db.rule_candidate_reject("ARC-nonexistent")
        db.close()


def test_rule_candidate_accept_empty_id_raises():
    """accept 空 ID 应抛出 ValueError"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        with pytest.raises(ValueError):
            db.rule_candidate_accept("")
        db.close()

