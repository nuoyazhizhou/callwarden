"""Agent Rule Memory schema 与迁移测试。

覆盖 Phase 1：
- 新库建表（v23）
- 旧库 v22 -> v23 迁移
- 三张表的字段、默认值、索引存在性
"""

import json
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
    """SCHEMA_VERSION 应为 25"""
    assert SCHEMA_VERSION == 25


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
    """新库 schema_version 表中应有 25 记录"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        v = db.conn.execute(
            "SELECT MAX(version) as v FROM schema_version"
        ).fetchone()
        assert v["v"] == 25
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
            DELETE FROM schema_version WHERE version >= 23;
            """
        )
        conn.commit()
        cur = conn.execute("SELECT MAX(version) as v FROM schema_version")
        row = cur.fetchone()
        assert row[0] is None or row[0] < 24
        conn.close()

        # 重新打开触发迁移
        db2 = CodeGraphDB(db_path=db_path, workspace_root=tmp)
        v = db2.conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert v["version"] == 25
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


# ============================================
# Phase 2: get_applicable_rules 作用域匹配
# ============================================


def _setup_rules(db):
    """辅助：创建一组测试规则并 accept"""
    # 全局规则
    cid_g = db.rule_candidate_create("global-rule", "global text", scope={})
    db.rule_candidate_accept(cid_g)
    # Python + edit 规则（warning）
    cid_py = db.rule_candidate_create(
        "i18n-rule", "use i18n",
        scope={"languages": ["python"], "actions": ["edit"]},
        severity="warning",
    )
    db.rule_candidate_accept(cid_py)
    # file glob 规则（error）
    cid_file = db.rule_candidate_create(
        "cli-rule", "cli rule",
        scope={"file_patterns": ["cli/*.py"]},
        severity="error",
    )
    db.rule_candidate_accept(cid_file)
    # module prefix 规则
    cid_mod = db.rule_candidate_create(
        "server-rule", "server rule",
        scope={"module_prefixes": ["server."]},
    )
    db.rule_candidate_accept(cid_mod)
    # symbol_kind 规则
    cid_kind = db.rule_candidate_create(
        "fn-rule", "fn rule",
        scope={"symbol_kinds": ["function"]},
        severity="info",
    )
    db.rule_candidate_accept(cid_kind)


def test_get_applicable_rules_global_matches_all():
    """空 scope 全局规则应匹配所有上下文"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        _setup_rules(db)
        rs = db.get_applicable_rules({}, limit=10)
        # 空上下文应只匹配全局规则
        assert len(rs) == 1
        assert rs[0]["title"] == "global-rule"
        assert rs[0]["matched_scope"] == ["global"]
        db.close()


def test_get_applicable_rules_language_match():
    """按语言匹配规则"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        _setup_rules(db)
        rs = db.get_applicable_rules({"language": "python"}, limit=10)
        titles = [r["title"] for r in rs]
        assert "global-rule" in titles
        assert "i18n-rule" not in titles  # 上下文缺 action，不应匹配
        # 只匹配 global
        assert len(rs) == 1
        db.close()


def test_get_applicable_rules_language_and_action_match():
    """language + action 同时命中（AND）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        _setup_rules(db)
        rs = db.get_applicable_rules(
            {"language": "python", "action": "edit"}, limit=10
        )
        titles = [r["title"] for r in rs]
        assert "global-rule" in titles
        assert "i18n-rule" in titles
        db.close()


def test_get_applicable_rules_file_glob_match():
    """file_patterns glob 匹配"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        _setup_rules(db)
        rs = db.get_applicable_rules({"file_path": "cli/main.py"}, limit=10)
        titles = [r["title"] for r in rs]
        assert "global-rule" in titles
        assert "cli-rule" in titles
        # 不匹配 server-rule（无 module_prefix 上下文）
        assert "server-rule" not in titles
        db.close()


def test_get_applicable_rules_file_glob_no_match():
    """file_patterns 不匹配时应过滤掉"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        _setup_rules(db)
        rs = db.get_applicable_rules({"file_path": "src/main.rs"}, limit=10)
        titles = [r["title"] for r in rs]
        # 只匹配全局
        assert titles == ["global-rule"]
        db.close()


def test_get_applicable_rules_module_prefix_match():
    """module_prefixes 前缀匹配"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        _setup_rules(db)
        rs = db.get_applicable_rules(
            {"module_prefix": "server.foo"}, limit=10
        )
        titles = [r["title"] for r in rs]
        assert "global-rule" in titles
        assert "server-rule" in titles
        db.close()


def test_get_applicable_rules_module_prefix_no_match():
    """module_prefixes 前缀不匹配"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        _setup_rules(db)
        rs = db.get_applicable_rules(
            {"module_prefix": "cli.main"}, limit=10
        )
        titles = [r["title"] for r in rs]
        # 只匹配全局
        assert titles == ["global-rule"]
        db.close()


def test_get_applicable_rules_symbol_kind_match():
    """symbol_kinds 匹配"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        _setup_rules(db)
        rs = db.get_applicable_rules({"symbol_kind": "function"}, limit=10)
        titles = [r["title"] for r in rs]
        assert "fn-rule" in titles
        db.close()


def test_get_applicable_rules_severity_ordering():
    """按 severity 优先级排序（critical 在前）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        for sev, title in [
            ("info", "info-rule"),
            ("critical", "critical-rule"),
            ("warning", "warning-rule"),
            ("error", "error-rule"),
        ]:
            cid = db.rule_candidate_create(title, "r", severity=sev)
            db.rule_candidate_accept(cid)

        rs = db.get_applicable_rules({}, limit=10)
        # 全部 scope 为空 → 全部匹配
        # 按 severity 排序
        assert rs[0]["severity"] == "critical"
        assert rs[1]["severity"] == "error"
        assert rs[2]["severity"] == "warning"
        assert rs[3]["severity"] == "info"
        db.close()


def test_get_applicable_rules_match_precision_ordering():
    """匹配精度高的规则排在前面（命中字段数多的优先）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        # 全局规则（1 个标签 global）
        cid_g = db.rule_candidate_create("global", "r", scope={}, severity="warning")
        db.rule_candidate_accept(cid_g)
        # 多字段规则（3 个标签 language+action+file）
        cid_multi = db.rule_candidate_create(
            "multi", "r",
            scope={
                "languages": ["python"],
                "actions": ["edit"],
                "file_patterns": ["cli/*.py"],
            },
            severity="warning",
        )
        db.rule_candidate_accept(cid_multi)

        rs = db.get_applicable_rules(
            {"language": "python", "action": "edit", "file_path": "cli/main.py"},
            limit=10,
        )
        # 同 severity，匹配精度高的在前
        assert rs[0]["title"] == "multi"
        assert len(rs[0]["matched_scope"]) == 3
        assert rs[1]["title"] == "global"
        db.close()


def test_get_applicable_rules_limit():
    """limit 限制返回数量"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        for i in range(10):
            cid = db.rule_candidate_create(f"r{i}", "r")
            db.rule_candidate_accept(cid)

        rs = db.get_applicable_rules({}, limit=3)
        assert len(rs) == 3
        db.close()


def test_get_applicable_rules_zero_limit_returns_empty():
    """limit=0 应返回空列表"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        _setup_rules(db)
        rs = db.get_applicable_rules({}, limit=0)
        assert rs == []
        db.close()


def test_get_applicable_rules_only_returns_active():
    """只返回 status=active 规则"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid1 = db.rule_candidate_create("active1", "r")
        cid2 = db.rule_candidate_create("active2", "r")
        rid1 = db.rule_candidate_accept(cid1)
        rid2 = db.rule_candidate_accept(cid2)

        # 把 rid2 改成 deprecated
        db.conn.execute(
            "UPDATE agent_rules SET status = ? WHERE id = ?",
            ("deprecated", rid2),
        )
        db.conn.commit()

        rs = db.get_applicable_rules({}, limit=10)
        titles = [r["title"] for r in rs]
        assert "active1" in titles
        assert "active2" not in titles
        db.close()


def test_get_applicable_rules_matched_scope_labels():
    """matched_scope 标签应反映命中的字段"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create(
            "multi", "r",
            scope={
                "languages": ["python"],
                "actions": ["edit"],
                "file_patterns": ["cli/*.py"],
            },
        )
        db.rule_candidate_accept(cid)

        rs = db.get_applicable_rules(
            {"language": "python", "action": "edit", "file_path": "cli/main.py"},
            limit=10,
        )
        rule = next(r for r in rs if r["title"] == "multi")
        assert "language:python" in rule["matched_scope"]
        assert "action:edit" in rule["matched_scope"]
        assert "file:cli/main.py" in rule["matched_scope"]
        db.close()


def test_get_applicable_rules_case_insensitive_language():
    """language 匹配大小写不敏感"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create(
            "py", "r",
            scope={"languages": ["Python"]},
        )
        db.rule_candidate_accept(cid)

        rs = db.get_applicable_rules({"language": "python"}, limit=10)
        titles = [r["title"] for r in rs]
        assert "py" in titles
        db.close()


def test_get_applicable_rules_case_insensitive_action():
    """action 匹配大小写不敏感"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create(
            "edit-rule", "r",
            scope={"actions": ["EDIT"]},
        )
        db.rule_candidate_accept(cid)

        rs = db.get_applicable_rules({"action": "edit"}, limit=10)
        titles = [r["title"] for r in rs]
        assert "edit-rule" in titles
        db.close()


def test_get_applicable_rules_no_active_returns_empty():
    """没有 active 规则时返回空列表"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        rs = db.get_applicable_rules({"language": "python"}, limit=10)
        assert rs == []
        db.close()


def test_get_applicable_rules_multiple_fields_all_must_match():
    """多字段 AND 匹配：缺一不可"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create(
            "strict", "r",
            scope={
                "languages": ["python"],
                "actions": ["edit"],
                "file_patterns": ["cli/*.py"],
            },
        )
        db.rule_candidate_accept(cid)

        # 只传 language → 不匹配（缺 action 和 file_path）
        rs = db.get_applicable_rules({"language": "python"}, limit=10)
        titles = [r["title"] for r in rs]
        assert "strict" not in titles

        # 传 language + action → 仍不匹配（缺 file_path）
        rs = db.get_applicable_rules(
            {"language": "python", "action": "edit"}, limit=10
        )
        titles = [r["title"] for r in rs]
        assert "strict" not in titles

        # 全部字段都传 → 匹配
        rs = db.get_applicable_rules(
            {"language": "python", "action": "edit", "file_path": "cli/main.py"},
            limit=10,
        )
        titles = [r["title"] for r in rs]
        assert "strict" in titles
        db.close()


def test_get_applicable_rules_finding_type_match():
    """finding_types 字段匹配"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create(
            "i18n-finding", "r",
            scope={"finding_types": ["i18n", "semgrep"]},
        )
        db.rule_candidate_accept(cid)

        rs = db.get_applicable_rules({"finding_type": "i18n"}, limit=10)
        titles = [r["title"] for r in rs]
        assert "i18n-finding" in titles

        rs = db.get_applicable_rules({"finding_type": "semgrep"}, limit=10)
        titles = [r["title"] for r in rs]
        assert "i18n-finding" in titles

        # 不匹配的 finding_type
        rs = db.get_applicable_rules({"finding_type": "signature"}, limit=10)
        titles = [r["title"] for r in rs]
        assert "i18n-finding" not in titles
        db.close()


def test_get_applicable_rules_returns_all_expected_fields():
    """返回的 dict 应包含所有期望字段"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        _setup_rules(db)
        rs = db.get_applicable_rules({}, limit=10)
        r = rs[0]
        expected_keys = {
            "id", "title", "rule_text", "scope", "severity", "status",
            "source_candidate_id", "evidence", "created_at", "updated_at",
            "synced_to_agents_md", "sync_hash", "matched_scope",
        }
        assert expected_keys.issubset(set(r.keys())), (
            f"缺少字段: {expected_keys - set(r.keys())}"
        )
        db.close()


# ============================================
# Phase 4: get_symbol / file_symbol_content 注入测试
# ============================================


def _setup_db_with_symbol(tmp):
    """辅助：创建 db，写入 mod.py（含 hello 函数），刷新进图谱

    返回 (db, qualified_name)
    """
    test_file = os.path.join(tmp, "mod.py")
    with open(test_file, "w", encoding="utf-8") as f:
        f.write("def hello():\n    return 'hello'\n")

    db = CodeGraphDB(workspace_root=tmp)
    db.refresh_file(test_file)

    # 查询符号限定名
    row = db.conn.execute(
        """
        SELECT fsv.qualified_name
        FROM file_symbol_versions fsv
        JOIN file_versions fv ON fsv.file_version_id = fv.id
        JOIN file_instances fi ON fv.file_instance_id = fi.id
        WHERE fv.is_current = 1
        LIMIT 1
        """
    ).fetchone()
    qn = row["qualified_name"] if row else ""
    return db, qn


def test_build_rule_context_for_symbol_infers_language_and_module():
    """build_rule_context_for_symbol 应根据 file_path 和 qualified_name 推断上下文"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            ctx = db.build_rule_context_for_symbol(
                qualified_name="cli.main.handle",
                file_path="cli/main.py",
                kind="function",
            )
            assert ctx["file_path"] == "cli/main.py"
            assert ctx["language"] == "python"
            assert ctx["symbol_kind"] == "function"
            assert ctx["module_prefix"] == "cli.main"
        finally:
            db.close()


def test_build_rule_context_for_symbol_rust_double_colon():
    """Rust 风格限定名（:: 分隔）应正确推断 module_prefix"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            ctx = db.build_rule_context_for_symbol(
                qualified_name="mod::Sub::fn",
                file_path="src/lib.rs",
                kind="method",
            )
            assert ctx["language"] == "rust"
            assert ctx["symbol_kind"] == "method"
            assert ctx["module_prefix"] == "mod::Sub"
        finally:
            db.close()


def test_build_rule_context_for_symbol_empty_inputs():
    """空入参应返回空上下文（不抛异常）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            ctx = db.build_rule_context_for_symbol()
            assert ctx == {}
        finally:
            db.close()


def test_get_applicable_rules_for_symbol_returns_matched_rules():
    """get_applicable_rules_for_symbol 应返回匹配规则（精简字段）"""
    with tempfile.TemporaryDirectory() as tmp:
        db, qn = _setup_db_with_symbol(tmp)
        try:
            cid = db.rule_candidate_create(
                "py-rule", "Python 规则",
                scope={"languages": ["python"]},
                severity="warning",
            )
            db.rule_candidate_accept(cid)
            db.rule_candidate_create("global-rule", "global", scope={})
            db.rule_candidate_accept(_last_cid(db))

            rules = db.get_applicable_rules_for_symbol(
                qualified_name=qn,
                file_path="mod.py",
                kind="fn",
            )
            titles = [r["title"] for r in rules]
            assert "py-rule" in titles
            assert "global-rule" in titles

            # 精简字段：只含 id/title/rule_text/severity/matched_scope
            for r in rules:
                assert set(r.keys()) == {
                    "id", "title", "rule_text", "severity", "matched_scope",
                }

            py_rule = next(r for r in rules if r["title"] == "py-rule")
            assert "language:python" in py_rule["matched_scope"]
        finally:
            db.close()


def test_get_applicable_rules_for_symbol_fail_soft_on_missing_table():
    """fail-soft：DROP agent_rules 表后应返回空列表"""
    with tempfile.TemporaryDirectory() as tmp:
        db, qn = _setup_db_with_symbol(tmp)
        try:
            db.conn.execute("DROP TABLE agent_rules")
            db.conn.commit()
            rules = db.get_applicable_rules_for_symbol(
                qualified_name=qn, file_path="mod.py", kind="fn",
            )
            assert rules == []
        finally:
            db.close()


def test_get_symbol_injects_applicable_rules_field():
    """get_symbol 返回值应包含 applicable_rules 字段"""
    with tempfile.TemporaryDirectory() as tmp:
        db, qn = _setup_db_with_symbol(tmp)
        try:
            cid = db.rule_candidate_create(
                "py-rule", "Python 规则",
                scope={"languages": ["python"]},
                severity="warning",
            )
            db.rule_candidate_accept(cid)
            db.rule_candidate_create("global-rule", "global", scope={})
            db.rule_candidate_accept(_last_cid(db))

            sym = db.get_symbol(qn)
            assert sym is not None
            assert "applicable_rules" in sym
            titles = [r["title"] for r in sym["applicable_rules"]]
            assert "py-rule" in titles
            assert "global-rule" in titles
        finally:
            db.close()


def test_get_symbol_applicable_rules_empty_when_no_active_rules():
    """无 active 规则时 applicable_rules 应为空列表"""
    with tempfile.TemporaryDirectory() as tmp:
        db, qn = _setup_db_with_symbol(tmp)
        try:
            sym = db.get_symbol(qn)
            assert sym is not None
            assert sym["applicable_rules"] == []
        finally:
            db.close()


def test_get_symbol_fail_soft_on_missing_agent_rules_table():
    """fail-soft：DROP agent_rules 表后 get_symbol 仍正常返回"""
    with tempfile.TemporaryDirectory() as tmp:
        db, qn = _setup_db_with_symbol(tmp)
        try:
            db.conn.execute("DROP TABLE agent_rules")
            db.conn.commit()
            sym = db.get_symbol(qn)
            assert sym is not None, "fail-soft 时符号查询不应失败"
            assert sym.get("applicable_rules") == []
        finally:
            db.close()


def test_file_symbol_content_mcp_tool_injects_applicable_rules():
    """file_symbol_content MCP 工具应注入 applicable_rules（含 action=read）"""
    import asyncio
    import json as _json

    import callwarden.server.mcp_server as mcp_mod
    from callwarden.server.mcp_server import create_mcp_server

    with tempfile.TemporaryDirectory() as tmp:
        db, _qn = _setup_db_with_symbol(tmp)
        try:
            cid = db.rule_candidate_create(
                "py-read-rule", "Python 读取规则",
                scope={"languages": ["python"], "actions": ["read"]},
                severity="warning",
            )
            db.rule_candidate_accept(cid)
            db.rule_candidate_create("global-rule", "global", scope={})
            db.rule_candidate_accept(_last_cid(db))

            mcp = create_mcp_server()
            orig_get_db = mcp_mod.get_db
            mcp_mod.get_db = lambda workspace=None: db
            try:
                result = asyncio.run(
                    mcp.call_tool("file_symbol_content",
                                  {"file_path": "mod.py", "symbol_name": "hello"})
                )
                if isinstance(result, tuple):
                    result = result[0]
                payload = _json.loads(result[0].text)

                assert "applicable_rules" in payload
                titles = [r["title"] for r in payload["applicable_rules"]]
                assert "py-read-rule" in titles
                assert "global-rule" in titles

                # action=read 应被注入到上下文
                py_rule = next(
                    r for r in payload["applicable_rules"]
                    if r["title"] == "py-read-rule"
                )
                assert "language:python" in py_rule["matched_scope"]
                assert "action:read" in py_rule["matched_scope"]
            finally:
                mcp_mod.get_db = orig_get_db
        finally:
            db.close()


def test_file_symbol_content_mcp_tool_fail_soft_on_missing_table():
    """fail-soft：DROP agent_rules 表后 file_symbol_content 仍正常返回"""
    import asyncio
    import json as _json

    import callwarden.server.mcp_server as mcp_mod
    from callwarden.server.mcp_server import create_mcp_server

    with tempfile.TemporaryDirectory() as tmp:
        db, _qn = _setup_db_with_symbol(tmp)
        try:
            cid = db.rule_candidate_create("global-rule", "global", scope={})
            db.rule_candidate_accept(cid)

            db.conn.execute("DROP TABLE agent_rules")
            db.conn.commit()

            mcp = create_mcp_server()
            orig_get_db = mcp_mod.get_db
            mcp_mod.get_db = lambda workspace=None: db
            try:
                result = asyncio.run(
                    mcp.call_tool("file_symbol_content",
                                  {"file_path": "mod.py", "symbol_name": "hello"})
                )
                if isinstance(result, tuple):
                    result = result[0]
                payload = _json.loads(result[0].text)
                # fail-soft：applicable_rules 应为空列表，符号查询仍正常
                assert payload.get("applicable_rules") == []
                assert payload.get("symbol_name") == "hello"
            finally:
                mcp_mod.get_db = orig_get_db
        finally:
            db.close()


def _last_cid(db):
    """获取最近创建的 candidate id"""
    row = db.conn.execute(
        "SELECT id FROM agent_rule_candidates ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["id"]


# ============================================
# Phase 5: extract_rule_candidates_from_quality_findings 测试
# ============================================


def test_extract_rule_candidates_aggregates_repeated_findings():
    """重复出现的同类 finding 应聚合成 1 个 pending 候选规则"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            tid = db.task_create("q-test", steps=[{"action": "edit"}])
            # 写入 3 个同类型 finding（i18n + warn + semgrep）
            for i in range(3):
                db.record_task_quality_finding(
                    task_id=tid,
                    finding_type="i18n",
                    severity="warn",
                    message=f"hardcoded {i}",
                    source="semgrep",
                )

            cids = db.extract_rule_candidates_from_quality_findings(
                task_id=tid, min_occurrences=2,
            )
            assert len(cids) == 1, "应只生成 1 个候选"
            c = db.rule_candidate_list(status="pending")[0]
            assert c["source"] == "auto_quality_findings"
            assert c["scope"] == {"finding_types": ["i18n"]}
            assert c["severity"] == "warning"
            assert c["evidence"]["occurrences"] == 3
            assert c["evidence"]["source"] == "task_quality_findings"
            assert c["evidence"]["task_id"] == tid
            assert len(c["evidence"]["finding_ids"]) == 3
            # confidence = min(1.0, occurrences/10) = 0.3
            assert c["confidence"] == pytest.approx(0.3)
        finally:
            db.close()


def test_extract_rule_candidates_below_threshold_not_generated():
    """出现次数低于 min_occurrences 不应生成候选"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            tid = db.task_create("q-test", steps=[{"action": "edit"}])
            db.record_task_quality_finding(
                task_id=tid, finding_type="i18n",
                severity="warn", message="only one", source="semgrep",
            )
            cids = db.extract_rule_candidates_from_quality_findings(
                task_id=tid, min_occurrences=2,
            )
            assert cids == [], "1 条 finding 低于阈值 2，不应生成候选"
        finally:
            db.close()


def test_extract_rule_candidates_dedup_when_pending_exists():
    """已有 pending 候选时重复提取应跳过（去重）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            tid = db.task_create("q-test", steps=[{"action": "edit"}])
            for i in range(3):
                db.record_task_quality_finding(
                    task_id=tid, finding_type="i18n",
                    severity="warn", message=f"m{i}", source="semgrep",
                )
            # 第一次提取
            cids1 = db.extract_rule_candidates_from_quality_findings(
                task_id=tid, min_occurrences=2,
            )
            assert len(cids1) == 1
            # 第二次提取应去重
            cids2 = db.extract_rule_candidates_from_quality_findings(
                task_id=tid, min_occurrences=2,
            )
            assert cids2 == [], "已有 pending 候选时不应重复生成"
        finally:
            db.close()


def test_extract_rule_candidates_regenerates_after_accept():
    """accept 候选后再次提取应生成新候选（pending 不存在了）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            tid = db.task_create("q-test", steps=[{"action": "edit"}])
            for i in range(3):
                db.record_task_quality_finding(
                    task_id=tid, finding_type="i18n",
                    severity="warn", message=f"m{i}", source="semgrep",
                )
            cids1 = db.extract_rule_candidates_from_quality_findings(
                task_id=tid, min_occurrences=2,
            )
            assert len(cids1) == 1
            db.rule_candidate_accept(cids1[0])
            # accept 后 pending 不存在，应生成新候选
            cids2 = db.extract_rule_candidates_from_quality_findings(
                task_id=tid, min_occurrences=2,
            )
            assert len(cids2) == 1, "accept 后应重新生成 pending 候选"
            assert cids2[0] != cids1[0], "应是新候选 ID"
        finally:
            db.close()


def test_extract_rule_candidates_full_scan_without_task_id():
    """全库扫描（不限定 task_id）能聚合多个任务的 finding"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            tid1 = db.task_create("t1", steps=[{"action": "edit"}])
            tid2 = db.task_create("t2", steps=[{"action": "edit"}])
            for i in range(2):
                db.record_task_quality_finding(
                    task_id=tid1, finding_type="signature",
                    severity="error", message=f"m{i}", source="call_chain",
                )
                db.record_task_quality_finding(
                    task_id=tid2, finding_type="signature",
                    severity="error", message=f"m{i}", source="call_chain",
                )
            cids = db.extract_rule_candidates_from_quality_findings(
                min_occurrences=2,
            )
            assert len(cids) == 1, "全库扫描应聚合 4 条 signature finding"
            c = db.rule_candidate_list(status="pending")[0]
            assert c["severity"] == "error"
            assert c["evidence"]["occurrences"] == 4
        finally:
            db.close()


def test_extract_rule_candidates_severity_mapping():
    """finding severity 应正确映射到规则 severity"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            tid = db.task_create("q-test", steps=[{"action": "edit"}])
            # error 级 finding
            for i in range(2):
                db.record_task_quality_finding(
                    task_id=tid, finding_type="semgrep",
                    severity="error", message=f"e{i}", source="semgrep",
                )
            # warn 级 finding
            for i in range(2):
                db.record_task_quality_finding(
                    task_id=tid, finding_type="i18n",
                    severity="warn", message=f"w{i}", source="semgrep",
                )
            cids = db.extract_rule_candidates_from_quality_findings(
                task_id=tid, min_occurrences=2,
            )
            assert len(cids) == 2
            cands = db.rule_candidate_list(status="pending")
            semgrep_c = next(c for c in cands if "semgrep" in c["title"])
            i18n_c = next(c for c in cands if "i18n" in c["title"])
            assert semgrep_c["severity"] == "error"
            assert i18n_c["severity"] == "warning"
        finally:
            db.close()


def test_extract_rule_candidates_evidence_preserves_finding_ids():
    """evidence 应保存来源 finding_ids（最多 10 条）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            tid = db.task_create("q-test", steps=[{"action": "edit"}])
            # 写入 15 条同类型 finding（finding_ids 应只保留前 10 条）
            for i in range(15):
                db.record_task_quality_finding(
                    task_id=tid, finding_type="i18n",
                    severity="warn", message=f"m{i}", source="semgrep",
                )
            cids = db.extract_rule_candidates_from_quality_findings(
                task_id=tid, min_occurrences=2,
            )
            assert len(cids) == 1
            c = db.rule_candidate_list(status="pending")[0]
            assert c["evidence"]["occurrences"] == 15
            assert len(c["evidence"]["finding_ids"]) == 10, "最多保留 10 条 finding_ids"
            # confidence = min(1.0, 15/10) = 1.0
            assert c["confidence"] == 1.0
        finally:
            db.close()


def test_extract_rule_candidates_default_min_occurrences_is_2():
    """默认 min_occurrences=2"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            tid = db.task_create("q-test", steps=[{"action": "edit"}])
            # 只写 1 条，低于默认阈值
            db.record_task_quality_finding(
                task_id=tid, finding_type="i18n",
                severity="warn", message="m", source="semgrep",
            )
            cids = db.extract_rule_candidates_from_quality_findings(task_id=tid)
            assert cids == [], "默认阈值 2，1 条 finding 不应被提取"
        finally:
            db.close()


# ============================================
# Phase 6: rule_sync_agents_md / rule_insert_agents_md_block 测试
# ============================================


def _setup_active_rules(db, count=2):
    """辅助：创建并 accept count 条 active 规则，返回 rule_ids"""
    rule_ids = []
    for i in range(count):
        cid = db.rule_candidate_create(
            title=f"rule-{i+1}",
            rule_text=f"text {i+1}",
            severity="warning" if i == 0 else "info",
        )
        rid = db.rule_candidate_accept(cid)
        rule_ids.append(rid)
    return rule_ids


def _write_agents_md(tmp, content="# Project\n\nSome content\n"):
    """辅助：写入 AGENTS.md 文件，返回路径"""
    path = os.path.join(tmp, "AGENTS.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_rule_sync_agents_md_no_marker_returns_error():
    """标记区不存在时应返回 success=False + suggested_block"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=1)
            agents_md = _write_agents_md(tmp, "# Project\n\nno marker\n")

            result = db.rule_sync_agents_md(target_path=agents_md, dry_run=True)

            assert result["success"] is False
            assert result["rule_count"] == 0
            assert result["after_hash"] == ""
            assert "suggested_block" in result
            assert "CALLWARDEN_RULES_START" in result["suggested_block"]
            assert "CALLWARDEN_RULES_END" in result["suggested_block"]
            assert "error" in result and result["error"]
        finally:
            db.close()


def test_rule_sync_agents_md_dry_run_returns_preview():
    """dry_run=True 时返回 preview，不写文件"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            rule_ids = _setup_active_rules(db, count=2)
            agents_md = _write_agents_md(tmp, "# Project\n\nno marker\n")

            # 先插入标记块
            db.rule_insert_agents_md_block(target_path=agents_md)

            result = db.rule_sync_agents_md(target_path=agents_md, dry_run=True)

            assert result["success"] is True
            assert result["dry_run"] is True
            assert result["rule_count"] == 2
            assert result["rule_ids"] == rule_ids
            assert "rule-1" in result["preview"]
            assert "rule-2" in result["preview"]
            assert result["after_hash"]  # dry_run 也计算 after_hash

            # dry_run 不应改文件
            with open(agents_md, "r", encoding="utf-8") as f:
                content = f.read()
            assert "rule-1" not in content, "dry_run 不应改文件"
            assert "# Project" in content, "标记区外内容不应变"
        finally:
            db.close()


def test_rule_sync_agents_md_apply_writes_file_and_log():
    """apply（dry_run=False）应写文件、记录 sync_log、标记 synced_to_agents_md"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            rule_ids = _setup_active_rules(db, count=2)
            agents_md = _write_agents_md(tmp, "# Project\n\nno marker\n")
            db.rule_insert_agents_md_block(target_path=agents_md)

            result = db.rule_sync_agents_md(target_path=agents_md, dry_run=False)

            assert result["success"] is True
            assert result["dry_run"] is False
            assert result["rule_count"] == 2
            assert result["after_hash"]
            assert result["before_hash"] != result["after_hash"]

            # 验证文件已写入规则内容
            with open(agents_md, "r", encoding="utf-8") as f:
                content = f.read()
            assert "rule-1" in content
            assert "rule-2" in content
            assert "CALLWARDEN_RULES_START" in content
            assert "CALLWARDEN_RULES_END" in content

            # 验证 sync_log 已记录 apply（dry_run=0）
            logs = db.conn.execute(
                "SELECT * FROM agent_rule_sync_log WHERE dry_run = 0"
            ).fetchall()
            assert len(logs) == 1
            apply_log = logs[0]
            assert apply_log["target_path"] == agents_md
            assert apply_log["before_hash"]
            assert apply_log["after_hash"] == result["after_hash"]
            ids = json.loads(apply_log["rule_ids_json"])
            assert sorted(ids) == sorted(rule_ids)

            # 验证规则 synced_to_agents_md=1
            rules = db.rule_list(status="active")
            assert all(r["synced_to_agents_md"] is True for r in rules)
            assert all(r["sync_hash"] == result["after_hash"] for r in rules)
        finally:
            db.close()


def test_rule_sync_agents_md_apply_preserves_outside_marker():
    """apply 只改标记区，标记区外的人工内容不应变化"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=1)
            # 文件含人工维护内容
            original = (
                "# Project\n"
                "\n"
                "## 规范\n"
                "这是一段人工维护内容，不应被自动同步覆盖。\n"
                "\n"
            )
            agents_md = _write_agents_md(tmp, original)
            db.rule_insert_agents_md_block(target_path=agents_md)

            db.rule_sync_agents_md(target_path=agents_md, dry_run=False)

            with open(agents_md, "r", encoding="utf-8") as f:
                content = f.read()
            # 人工内容保留
            assert "# Project" in content
            assert "这是一段人工维护内容" in content
            assert "## 规范" in content
            # 自动同步的规则在标记区内
            assert "rule-1" in content
        finally:
            db.close()


def test_rule_sync_agents_md_re_apply_syncs_new_rule():
    """重新 apply 应同步新增的 active 规则"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=2)
            agents_md = _write_agents_md(tmp, "# Project\n")
            db.rule_insert_agents_md_block(target_path=agents_md)
            db.rule_sync_agents_md(target_path=agents_md, dry_run=False)

            # 新增第 3 条规则
            cid3 = db.rule_candidate_create("rule-3", "third", severity="error")
            db.rule_candidate_accept(cid3)

            result2 = db.rule_sync_agents_md(target_path=agents_md, dry_run=False)
            assert result2["rule_count"] == 3

            with open(agents_md, "r", encoding="utf-8") as f:
                content = f.read()
            assert "rule-3" in content
            assert "# Project" in content, "标记区外内容仍应保留"
        finally:
            db.close()


def test_rule_insert_agents_md_block_success():
    """rule_insert_agents_md_block 成功插入标记块"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            agents_md = _write_agents_md(tmp, "# Project\n\n人工内容\n")

            result = db.rule_insert_agents_md_block(target_path=agents_md)

            assert result["success"] is True
            assert result["target_path"] == agents_md
            assert "message" in result and result["message"]

            with open(agents_md, "r", encoding="utf-8") as f:
                content = f.read()
            assert "CALLWARDEN_RULES_START" in content
            assert "CALLWARDEN_RULES_END" in content
            assert "# Project" in content
            assert "人工内容" in content
        finally:
            db.close()


def test_rule_insert_agents_md_block_already_exists_fails():
    """标记块已存在时再 insert 应失败"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            agents_md = _write_agents_md(tmp, "# Project\n")
            # 第一次插入成功
            r1 = db.rule_insert_agents_md_block(target_path=agents_md)
            assert r1["success"] is True
            # 第二次应失败
            r2 = db.rule_insert_agents_md_block(target_path=agents_md)
            assert r2["success"] is False
            assert "message" in r2 and r2["message"]
        finally:
            db.close()


def test_rule_insert_agents_md_block_records_sync_log():
    """insert 标记块也应记录 sync_log（dry_run=1，rule_ids=[]）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            agents_md = _write_agents_md(tmp, "# Project\n")

            db.rule_insert_agents_md_block(target_path=agents_md)

            logs = db.conn.execute(
                "SELECT * FROM agent_rule_sync_log WHERE dry_run = 1"
            ).fetchall()
            assert len(logs) == 1
            log = logs[0]
            assert log["target_path"] == agents_md
            assert log["before_hash"]
            assert log["after_hash"]
            assert json.loads(log["rule_ids_json"]) == []
            assert log["actor"] == "agent"
        finally:
            db.close()


def test_rule_insert_agents_md_block_creates_file_if_missing():
    """目标文件不存在时应创建空文件并写入标记块"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            agents_md = os.path.join(tmp, "AGENTS.md")
            assert not os.path.isfile(agents_md)

            result = db.rule_insert_agents_md_block(target_path=agents_md)

            assert result["success"] is True
            assert os.path.isfile(agents_md)
            with open(agents_md, "r", encoding="utf-8") as f:
                content = f.read()
            assert "CALLWARDEN_RULES_START" in content
            assert "CALLWARDEN_RULES_END" in content
        finally:
            db.close()


def test_rule_sync_agents_md_empty_active_rules_returns_zero():
    """无 active 规则时 sync 应返回 rule_count=0 但 success=True"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            agents_md = _write_agents_md(tmp, "# Project\n")
            db.rule_insert_agents_md_block(target_path=agents_md)

            result = db.rule_sync_agents_md(target_path=agents_md, dry_run=False)

            assert result["success"] is True
            assert result["rule_count"] == 0
            assert result["rule_ids"] == []
            # 标记块仍在文件中（空内容）
            with open(agents_md, "r", encoding="utf-8") as f:
                content = f.read()
            assert "CALLWARDEN_RULES_START" in content
            assert "CALLWARDEN_RULES_END" in content
            assert "# Project" in content
        finally:
            db.close()


# ============================================
# Phase 7: MCP / CLI 可用性测试
# ============================================


def test_mcp_server_registers_all_rule_tools():
    """MCP Server 应注册全部 9 个 rule MCP 工具"""
    import asyncio

    from callwarden.server.mcp_server import create_mcp_server

    mcp = create_mcp_server()
    tools = asyncio.run(mcp.list_tools())
    tool_names = {t.name for t in tools}

    expected = {
        "rule_candidate_create",
        "rule_candidate_list",
        "rule_candidate_accept",
        "rule_candidate_reject",
        "rule_list",
        "get_applicable_rules",
        "rule_sync_agents_md",
        "rule_insert_agents_md_block",
        "extract_rule_candidates_from_quality_findings",
    }
    missing = expected - tool_names
    assert not missing, f"缺少 MCP 工具: {missing}"


def _parse_mcp_result(result):
    """辅助：解析 FastMCP call_tool 返回值

    FastMCP 0.x 返回 list[TextContent]（每个 text 是 JSON 字符串）
    FastMCP 1.x 返回 (list[TextContent], structured_dict) 元组
    """
    if isinstance(result, tuple) and len(result) >= 2:
        # FastMCP 1.x：第二项是 structured dict
        structured = result[1]
        if isinstance(structured, dict):
            return structured
        # 兜底：从第一项 TextContent 解析 JSON
        text_contents = result[0]
    else:
        text_contents = result

    # FastMCP 0.x：list[TextContent]，每个 .text 是 JSON
    if isinstance(text_contents, list) and text_contents:
        first = text_contents[0]
        text = getattr(first, "text", str(first))
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return {}
    return {}


def test_mcp_rule_candidate_create_returns_candidate_id():
    """MCP rule_candidate_create 应返回 candidate_id"""
    import asyncio

    from callwarden.server import mcp_server as mcp_mod
    from callwarden.server.mcp_server import create_mcp_server
    from callwarden.db.db import CodeGraphDB

    with tempfile.TemporaryDirectory() as tmp:
        # 注入临时 db 实例到 mcp_server 单例
        db = CodeGraphDB(workspace_root=tmp)
        old_db = mcp_mod._db_instance
        mcp_mod._db_instance = db
        try:
            mcp = create_mcp_server()

            # 调用 rule_candidate_create
            result = asyncio.run(mcp.call_tool(
                "rule_candidate_create",
                {"title": "mcp-test", "rule_text": "use i18n", "severity": "warning"},
            ))
            structured = _parse_mcp_result(result)
            assert "candidate_id" in structured, f"返回缺少 candidate_id: {structured}"
            assert structured["candidate_id"].startswith("ARC-")
        finally:
            mcp_mod._db_instance = old_db
            db.close()


def test_mcp_rule_list_returns_rules():
    """MCP rule_list 应返回 rules 列表"""
    import asyncio

    from callwarden.server import mcp_server as mcp_mod
    from callwarden.server.mcp_server import create_mcp_server
    from callwarden.db.db import CodeGraphDB

    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        cid = db.rule_candidate_create("r1", "text1")
        db.rule_candidate_accept(cid)

        old_db = mcp_mod._db_instance
        mcp_mod._db_instance = db
        try:
            mcp = create_mcp_server()
            # 调用 MCP rule_list
            result = asyncio.run(mcp.call_tool("rule_list", {"status": "active"}))
            structured = _parse_mcp_result(result)
            assert structured["count"] == 1, f"期望 1 条规则，返回: {structured}"
            assert structured["rules"][0]["title"] == "r1"
        finally:
            mcp_mod._db_instance = old_db
            db.close()


def test_cli_rule_subcommand_registered():
    """cw rule 子命令应在 _SUBCOMMANDS 中注册"""
    from callwarden.cli.main import _SUBCOMMANDS

    assert "rule" in _SUBCOMMANDS, "rule 应在 _SUBCOMMANDS 集合中"


def test_cli_handle_rule_dispatches_to_subcommands():
    """_handle_rule 应能分发到 6 个 action 子命令"""
    import argparse

    from callwarden.cli.main import _handle_rule

    # 构造一个模拟 db
    class _MockDB:
        def __init__(self):
            self.calls = []

        def rule_list(self, status="active", limit=100):
            self.calls.append(("rule_list", status, limit))
            return []

        def get_applicable_rules(self, context=None, limit=10):
            self.calls.append(("get_applicable_rules", context, limit))
            return []

        def rule_sync_agents_md(self, target_path="AGENTS.md", dry_run=True, actor="agent"):
            self.calls.append(("rule_sync_agents_md", target_path, dry_run, actor))
            return {"success": True, "dry_run": dry_run, "rule_count": 0, "rule_ids": [],
                    "before_hash": "", "after_hash": "", "preview": ""}

        def rule_insert_agents_md_block(self, target_path="AGENTS.md", actor="agent"):
            self.calls.append(("rule_insert_agents_md_block", target_path, actor))
            return {"success": True, "target_path": target_path, "message": "ok"}

        def extract_rule_candidates_from_quality_findings(self, task_id="", min_occurrences=2):
            self.calls.append(("extract", task_id, min_occurrences))
            return []

    mock_db = _MockDB()

    # 测试 list action
    _handle_rule(["list", "--status", "active", "--limit", "5"], mock_db)
    assert mock_db.calls[-1][0] == "rule_list"

    # 测试 applicable action
    _handle_rule(["applicable", "--context", '{"languages":["python"]}', "--limit", "3"], mock_db)
    assert mock_db.calls[-1][0] == "get_applicable_rules"

    # 测试 sync action（dry-run 默认）
    _handle_rule(["sync", "--target", "AGENTS.md"], mock_db)
    assert mock_db.calls[-1][0] == "rule_sync_agents_md"
    assert mock_db.calls[-1][2] is True  # dry_run=True

    # 测试 sync action（apply）
    _handle_rule(["sync", "--target", "AGENTS.md", "--apply"], mock_db)
    assert mock_db.calls[-1][2] is False  # dry_run=False

    # 测试 insert-block action
    _handle_rule(["insert-block", "--target", "AGENTS.md"], mock_db)
    assert mock_db.calls[-1][0] == "rule_insert_agents_md_block"

    # 测试 extract action
    _handle_rule(["extract", "--task-id", "T-xxx", "--min-occurrences", "3"], mock_db)
    assert mock_db.calls[-1][0] == "extract"


def test_cli_handle_rule_candidate_subcommands():
    """_handle_rule candidate 子命令组应能分发到 create/list/accept/reject"""
    from callwarden.cli.main import _handle_rule

    class _MockDB:
        def __init__(self):
            self.calls = []

        def rule_candidate_create(self, **kwargs):
            self.calls.append(("create", kwargs))
            return "ARC-test-1"

        def rule_candidate_list(self, status="pending", limit=50):
            self.calls.append(("list", status, limit))
            return []

        def rule_candidate_accept(self, candidate_id, reviewer="agent"):
            self.calls.append(("accept", candidate_id, reviewer))
            return "AR-test-1"

        def rule_candidate_reject(self, candidate_id, reviewer="agent", reason=""):
            self.calls.append(("reject", candidate_id, reviewer, reason))
            return True

    mock_db = _MockDB()

    # candidate create
    _handle_rule(["candidate", "create", "--title", "t", "--text", "r", "--severity", "warning"], mock_db)
    assert mock_db.calls[-1][0] == "create"
    assert mock_db.calls[-1][1]["title"] == "t"
    assert mock_db.calls[-1][1]["severity"] == "warning"

    # candidate list
    _handle_rule(["candidate", "list", "--status", "pending", "--limit", "10"], mock_db)
    assert mock_db.calls[-1][0] == "list"
    assert mock_db.calls[-1][1] == "pending"
    assert mock_db.calls[-1][2] == 10

    # candidate accept
    _handle_rule(["candidate", "accept", "ARC-xxx", "--reviewer", "human"], mock_db)
    assert mock_db.calls[-1][0] == "accept"
    assert mock_db.calls[-1][1] == "ARC-xxx"
    assert mock_db.calls[-1][2] == "human"

    # candidate reject
    _handle_rule(["candidate", "reject", "ARC-xxx", "--reason", "duplicate"], mock_db)
    assert mock_db.calls[-1][0] == "reject"
    assert mock_db.calls[-1][3] == "duplicate"


def test_parse_json_arg_returns_default_on_invalid():
    """_parse_json_arg 应在非法 JSON 时返回 default

    约定：default=None 时 fallback 到 {}（便于上层直接 .get/.keys）
    """
    from callwarden.cli.main import _parse_json_arg

    # 空 string 返回 default
    assert _parse_json_arg("", default={}) == {}
    assert _parse_json_arg("", default=None) == {}
    assert _parse_json_arg("", default=[]) == []

    # 合法 JSON
    assert _parse_json_arg('{"a":1}', default={}) == {"a": 1}
    assert _parse_json_arg("[1,2]", default=[]) == [1, 2]

    # 非法 JSON 返回 default
    assert _parse_json_arg("not json", default={}) == {}
    assert _parse_json_arg("{invalid", default=[]) == []
    assert _parse_json_arg("{invalid", default=None) == {}

    # 非 dict/list（如 string/number）返回 default
    assert _parse_json_arg('"string"', default={}) == {}
    assert _parse_json_arg("42", default=[]) == []


# ============================================
# Bootstrap 种子规则 (rule_seed_bootstrap)
# ============================================


def test_rule_seed_bootstrap_dry_run_returns_5_created():
    """dry_run=True 时返回 5 条 created 计划，但不写库"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        result = db.rule_seed_bootstrap(dry_run=True)

        assert result["dry_run"] is True
        assert result["total"] == 5
        assert result["created"] == 5
        assert result["updated"] == 0
        assert result["skipped"] == 0
        assert len(result["rules"]) == 5

        # 验证 action 全为 create
        actions = [r["action"] for r in result["rules"]]
        assert actions == ["create"] * 5

        # 验证 db 中实际未写入
        count = db.conn.execute("SELECT COUNT(*) AS c FROM agent_rules").fetchone()["c"]
        assert count == 0, "dry_run 不应写入数据库"
        db.close()


def test_rule_seed_bootstrap_apply_writes_5_rules():
    """dry_run=False 时实际写入 5 条 active 规则"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        result = db.rule_seed_bootstrap(dry_run=False)

        assert result["dry_run"] is False
        assert result["created"] == 5
        assert result["updated"] == 0
        assert result["skipped"] == 0

        # 验证 db 中有 5 条 active 规则
        rows = db.conn.execute(
            "SELECT id, title, severity, status FROM agent_rules ORDER BY id"
        ).fetchall()
        assert len(rows) == 5

        # 验证所有规则都是 active
        for row in rows:
            assert row["status"] == "active"

        # 验证固定 ID 存在
        ids = {row["id"] for row in rows}
        expected_ids = {
            "AR-bootstrap-i18n",
            "AR-bootstrap-refresh-before-commit",
            "AR-bootstrap-task-split",
            "AR-bootstrap-completion-review",
            "AR-bootstrap-capture-diff",
        }
        assert ids == expected_ids, f"ID 不匹配: {ids ^ expected_ids}"
        db.close()


def test_rule_seed_bootstrap_is_idempotent():
    """重复 --apply 应全部 skip，不重复创建"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)

        # 第一次 apply
        r1 = db.rule_seed_bootstrap(dry_run=False)
        assert r1["created"] == 5
        assert r1["skipped"] == 0

        # 第二次 apply
        r2 = db.rule_seed_bootstrap(dry_run=False)
        assert r2["created"] == 0
        assert r2["updated"] == 0
        assert r2["skipped"] == 5

        # db 中仍只有 5 条
        count = db.conn.execute("SELECT COUNT(*) AS c FROM agent_rules").fetchone()["c"]
        assert count == 5
        db.close()


def test_rule_seed_bootstrap_updates_when_text_changes():
    """已存在规则内容变化时应 update"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)

        # 先 apply 一次
        db.rule_seed_bootstrap(dry_run=False)

        # 手动修改一条规则的 rule_text
        db.conn.execute(
            "UPDATE agent_rules SET rule_text = 'old text' WHERE id = ?",
            ("AR-bootstrap-i18n",),
        )
        db.conn.commit()

        # 再次 apply，应检测到变化并 update
        result = db.rule_seed_bootstrap(dry_run=False)
        assert result["created"] == 0
        assert result["updated"] == 1
        assert result["skipped"] == 4

        # 找到 update 的那条
        updated_rule = next(r for r in result["rules"] if r["action"] == "update")
        assert updated_rule["id"] == "AR-bootstrap-i18n"

        # 验证 db 中 text 已被还原
        text = db.conn.execute(
            "SELECT rule_text FROM agent_rules WHERE id = ?",
            ("AR-bootstrap-i18n",),
        ).fetchone()["rule_text"]
        assert text != "old text"
        db.close()


def test_rule_seed_bootstrap_severity_levels():
    """种子规则应包含正确的 severity 级别"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        db.rule_seed_bootstrap(dry_run=False)

        # 检查 critical 级别规则
        critical = db.conn.execute(
            "SELECT id FROM agent_rules WHERE severity = 'critical' ORDER BY id"
        ).fetchall()
        critical_ids = {row["id"] for row in critical}
        assert "AR-bootstrap-refresh-before-commit" in critical_ids
        assert "AR-bootstrap-completion-review" in critical_ids
        assert len(critical_ids) == 2

        # 检查 warning 级别规则
        warning = db.conn.execute(
            "SELECT id FROM agent_rules WHERE severity = 'warning' ORDER BY id"
        ).fetchall()
        warning_ids = {row["id"] for row in warning}
        assert "AR-bootstrap-i18n" in warning_ids
        assert "AR-bootstrap-task-split" in warning_ids
        assert "AR-bootstrap-capture-diff" in warning_ids
        assert len(warning_ids) == 3
        db.close()


def test_rule_seed_bootstrap_scope_serialized():
    """种子规则的 scope_json 应正确序列化为 JSON 字符串"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        db.rule_seed_bootstrap(dry_run=False)

        # 验证 refresh-before-commit 的 scope 包含 actions=[commit]
        row = db.conn.execute(
            "SELECT scope_json FROM agent_rules WHERE id = ?",
            ("AR-bootstrap-refresh-before-commit",),
        ).fetchone()
        scope = json.loads(row["scope_json"])
        assert scope == {"actions": ["commit"]}

        # 验证 i18n 规则 scope 为空 {}
        row = db.conn.execute(
            "SELECT scope_json FROM agent_rules WHERE id = ?",
            ("AR-bootstrap-i18n",),
        ).fetchone()
        scope = json.loads(row["scope_json"])
        assert scope == {}
        db.close()


def test_rule_seed_bootstrap_evidence_serialized():
    """种子规则的 evidence_json 应包含 source=bootstrap-seed"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        db.rule_seed_bootstrap(dry_run=False)

        rows = db.conn.execute(
            "SELECT id, evidence_json FROM agent_rules"
        ).fetchall()
        for row in rows:
            ev = json.loads(row["evidence_json"])
            assert ev.get("source") == "bootstrap-seed"
            assert ev.get("plan") == "bootstrap-closure-plan.md"
        db.close()


# ============================================
# CLI 测试 (cw rule seed-bootstrap)
# ============================================


def test_cli_rule_seed_bootstrap_dispatched():
    """_handle_rule 应能分发到 seed-bootstrap action"""
    from callwarden.cli.main import _handle_rule

    class _MockDB:
        def __init__(self):
            self.calls = []

        def rule_seed_bootstrap(self, dry_run=True):
            self.calls.append(("rule_seed_bootstrap", dry_run))
            return {
                "dry_run": dry_run,
                "total": 5,
                "created": 5 if not dry_run else 5,
                "updated": 0,
                "skipped": 0,
                "rules": [
                    {"id": "AR-bootstrap-i18n", "title": "i18n", "action": "create"}
                ],
            }

    mock_db = _MockDB()

    # 默认 dry-run
    _handle_rule(["seed-bootstrap"], mock_db)
    assert mock_db.calls[-1][0] == "rule_seed_bootstrap"
    assert mock_db.calls[-1][1] is True  # dry_run=True

    # --apply
    _handle_rule(["seed-bootstrap", "--apply"], mock_db)
    assert mock_db.calls[-1][0] == "rule_seed_bootstrap"
    assert mock_db.calls[-1][1] is False  # dry_run=False


def test_cli_rule_seed_bootstrap_help_no_db():
    """cw rule seed-bootstrap --help 不应触发数据库初始化"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main
    from callwarden.db.db import CodeGraphDB

    old_argv = sys.argv
    sys.argv = ["cw", "rule", "seed-bootstrap", "--help"]
    try:
        db_init_called = {"count": 0}

        def fake_init(self, *args, **kwargs):
            db_init_called["count"] += 1
            raise RuntimeError("db should not be initialized for --help")

        with mock.patch.object(CodeGraphDB, "__init__", fake_init):
            with mock.patch.object(cli_main, "CodeGraphDB", CodeGraphDB):
                try:
                    cli_main._run_subcommand_mode()
                except RuntimeError as e:
                    if "should not" in str(e):
                        pytest.fail("db initialized during cw rule seed-bootstrap --help")
                    raise
                except SystemExit:
                    pass  # argparse --help 通常会 SystemExit(0)
        assert db_init_called["count"] == 0, "--help 不应触发数据库初始化"
    finally:
        sys.argv = old_argv


# ============================================
# MCP 测试 (rule_seed_bootstrap)
# ============================================


def test_mcp_rule_seed_bootstrap_registered():
    """MCP Server 应注册 rule_seed_bootstrap 工具"""
    import asyncio

    from callwarden.server.mcp_server import create_mcp_server

    mcp = create_mcp_server()
    tools = asyncio.run(mcp.list_tools())
    tool_names = {t.name for t in tools}
    assert "rule_seed_bootstrap" in tool_names, "缺少 rule_seed_bootstrap MCP 工具"


def test_mcp_rule_seed_bootstrap_dry_run():
    """MCP rule_seed_bootstrap dry_run=True 应返回 5 条 created 计划"""
    import asyncio

    from callwarden.server import mcp_server as mcp_mod
    from callwarden.server.mcp_server import create_mcp_server

    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        old_db = mcp_mod._db_instance
        mcp_mod._db_instance = db
        try:
            mcp = create_mcp_server()
            result = asyncio.run(mcp.call_tool(
                "rule_seed_bootstrap",
                {"dry_run": True},
            ))
            structured = _parse_mcp_result(result)
            assert structured["dry_run"] is True
            assert structured["total"] == 5
            assert structured["created"] == 5
            assert structured["skipped"] == 0
        finally:
            mcp_mod._db_instance = old_db
            db.close()


def test_mcp_rule_seed_bootstrap_apply_writes_5():
    """MCP rule_seed_bootstrap dry_run=False 应实际写入 5 条规则"""
    import asyncio

    from callwarden.server import mcp_server as mcp_mod
    from callwarden.server.mcp_server import create_mcp_server

    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        old_db = mcp_mod._db_instance
        mcp_mod._db_instance = db
        try:
            mcp = create_mcp_server()
            result = asyncio.run(mcp.call_tool(
                "rule_seed_bootstrap",
                {"dry_run": False},
            ))
            structured = _parse_mcp_result(result)
            assert structured["dry_run"] is False
            assert structured["created"] == 5

            # 验证 db 中有 5 条
            count = db.conn.execute("SELECT COUNT(*) AS c FROM agent_rules").fetchone()["c"]
            assert count == 5
        finally:
            mcp_mod._db_instance = old_db
            db.close()


# ============================================
# 集成测试：seed 后规则可被 get_applicable_rules 查询
# ============================================


def test_seed_bootstrap_rules_visible_to_get_applicable_rules():
    """种子化后 get_applicable_rules 应能返回 bootstrap 规则

    scope 匹配规则：
    - scope={} (i18n 规则) 为 global，匹配任何 context
    - scope={"actions": ["commit"]} 仅匹配 context={"action": "commit"}（注意：context 用 action 单数）
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        db.rule_seed_bootstrap(dry_run=False)

        # 用 action=commit 查询，应匹配 refresh-before-commit（scope.actions=[commit]）
        # 以及 i18n（scope={} global）
        rules = db.get_applicable_rules(context={"action": "commit"}, limit=10)
        rule_ids = {r["id"] for r in rules}
        assert "AR-bootstrap-refresh-before-commit" in rule_ids
        assert "AR-bootstrap-i18n" in rule_ids  # global 规则也匹配

        # 用 action=task_apply 查询，应匹配 completion-review
        rules = db.get_applicable_rules(context={"action": "task_apply"}, limit=10)
        rule_ids = {r["id"] for r in rules}
        assert "AR-bootstrap-completion-review" in rule_ids

        # 空 context 只匹配 scope={} 的 global 规则
        rules = db.get_applicable_rules(context={}, limit=10)
        rule_ids = {r["id"] for r in rules}
        assert "AR-bootstrap-i18n" in rule_ids
        assert len(rule_ids) == 1, f"空 context 只应匹配 global 规则，实际: {rule_ids}"
        db.close()


def test_seed_bootstrap_injects_into_get_symbol():
    """种子化后 get_symbol 返回的 applicable_rules 应包含 bootstrap 全局规则"""
    with tempfile.TemporaryDirectory() as tmp:
        db, qn = _setup_db_with_symbol(tmp)
        try:
            # 种子化 5 条 bootstrap 规则
            db.rule_seed_bootstrap(dry_run=False)

            # get_symbol 应在返回值中注入 applicable_rules
            sym = db.get_symbol(qn)
            assert sym is not None, "符号查询不应失败"
            assert "applicable_rules" in sym, "返回值应包含 applicable_rules 字段"

            # 验证 bootstrap 规则被注入
            rule_ids = {r.get("id", "") for r in sym["applicable_rules"]}
            # global 规则（scope={}）应匹配
            assert "AR-bootstrap-i18n" in rule_ids, \
                f"i18n 全局规则应被注入到 get_symbol，实际: {rule_ids}"
        finally:
            db.close()


def test_seed_bootstrap_injects_into_file_symbol_content_mcp():
    """种子化后 MCP file_symbol_content 应在返回值中包含 bootstrap 全局规则"""
    import asyncio
    import json as _json

    import callwarden.server.mcp_server as mcp_mod
    from callwarden.server.mcp_server import create_mcp_server

    with tempfile.TemporaryDirectory() as tmp:
        db, _qn = _setup_db_with_symbol(tmp)
        # 种子化 bootstrap 规则
        db.rule_seed_bootstrap(dry_run=False)

        mcp = create_mcp_server()
        orig_get_db = mcp_mod.get_db
        mcp_mod.get_db = lambda workspace=None: db
        try:
            result = asyncio.run(
                mcp.call_tool("file_symbol_content",
                              {"file_path": "mod.py", "symbol_name": "hello"})
            )
            if isinstance(result, tuple):
                result = result[0]
            payload = _json.loads(result[0].text)

            assert "applicable_rules" in payload, \
                f"file_symbol_content 返回应包含 applicable_rules: {payload}"
            rule_ids = {r.get("id", "") for r in payload["applicable_rules"]}
            # i18n 规则 scope={} 是 global，应被注入
            assert "AR-bootstrap-i18n" in rule_ids, \
                f"i18n 全局规则应被注入到 file_symbol_content，实际: {rule_ids}"
        finally:
            mcp_mod.get_db = orig_get_db
            db.close()


