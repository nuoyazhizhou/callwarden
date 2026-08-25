"""Phase 8.4: audit log 测试（SRV-002 后：daemon-backed 薄客户端）。

迁移说明（route B 完整 thin-client 重构）：
- 原测试依赖本地 SQLite（`sqlite3` + `db_path` 文件）。SRV-002 后 `AuditLogger` 为纯
  daemon RPC 薄客户端，不再打开本地 SQLite，故本套件改为经 `FakeAuditDaemon`（内存态
  daemon 权威，对齐 Rust `audit_log_handlers` 语义）验收。
- 覆盖：枚举 / AuditEvent 序列化 / 记录·查询·统计 / 便捷方法 / 全局单例 / 安全审计场景。
"""

import time
import json

import pytest

from callwarden.server.audit_log import (
    AuditEventType,
    AuditResult,
    AuditEvent,
    AuditLogger,
    get_audit_logger,
    reset_audit_logger,
)


class FakeAuditDaemon:
    """内存态 daemon 审计日志权威（对齐 Rust `audit_log_handlers`）。"""

    def __init__(self):
        self.events: list = []
        self.available: bool = True

    def __call__(self, method: str, params: dict):
        if method == "mcp.audit_log.init_db":
            return {"ok": True}
        if method == "mcp.audit_log.append":
            ev = dict(params.get("event", {}))
            self.events.append(ev)
            return {"ok": True, "event_id": ev.get("event_id")}
        if method == "mcp.audit_log.query":
            return self._query(params)
        if method == "mcp.audit_log.count":
            return {"count": len(self._filtered(params))}
        if method == "mcp.audit_log.clear":
            self.events = []
            return {"ok": True}
        if method == "mcp.audit_log.get_stats":
            return self._stats()
        if method == "mcp.audit_log.get_conn":
            return {"db_path": "/var/log/callwarden/audit.log"}
        raise RuntimeError(f"unknown method {method}")

    def _filtered(self, params: dict):
        out = []
        st = params.get("start_time")
        et = params.get("end_time")
        etype = params.get("event_type")
        uid = params.get("actor_uid")
        res = params.get("result")
        for e in self.events:
            if st is not None and e.get("timestamp", 0) < st:
                continue
            if et is not None and e.get("timestamp", 0) >= et:
                continue
            if etype is not None and e.get("event_type") != etype:
                continue
            if uid is not None and e.get("actor_uid") != uid:
                continue
            if res is not None and e.get("result") != res:
                continue
            out.append(e)
        return out

    def _query(self, params: dict):
        out = sorted(self._filtered(params), key=lambda x: x.get("timestamp", 0), reverse=True)
        limit = params.get("limit", 100)
        offset = params.get("offset", 0)
        return out[offset : offset + limit]

    def _stats(self):
        by_type: dict = {}
        by_result: dict = {}
        for e in self.events:
            by_type[e.get("event_type")] = by_type.get(e.get("event_type"), 0) + 1
            by_result[e.get("result")] = by_result.get(e.get("result"), 0) + 1
        return {
            "total": len(self.events),
            "by_type": by_type,
            "by_result": by_result,
            "buffer_size": 0,
        }


@pytest.fixture
def fake_daemon(monkeypatch):
    daemon = FakeAuditDaemon()
    monkeypatch.setattr("callwarden.server.audit_log._call_daemon_rpc", daemon)
    return daemon


# ============================================================
# AuditEventType / AuditResult 测试
# ============================================================


class TestAuditEnums:
    def test_event_type_values(self):
        assert AuditEventType.WORKSPACE_REGISTER.value == "workspace_register"
        assert AuditEventType.ACCESS_DENIED.value == "access_denied"
        assert AuditEventType.ADMIN_OPERATION.value == "admin_operation"
        assert AuditEventType.TOKEN_GENERATE.value == "token_generate"

    def test_result_values(self):
        assert AuditResult.SUCCESS.value == "success"
        assert AuditResult.FAILURE.value == "failure"
        assert AuditResult.DENIED.value == "denied"


# ============================================================
# AuditEvent 测试
# ============================================================


class TestAuditEvent:
    def test_create_event(self):
        event = AuditEvent(
            event_type=AuditEventType.WORKSPACE_REGISTER,
            actor_uid=1000,
            actor_role="admin",
            action="register",
            target="workspace:ws-1",
        )
        assert event.event_type == AuditEventType.WORKSPACE_REGISTER
        assert event.actor_uid == 1000
        assert event.actor_role == "admin"
        assert event.action == "register"
        assert event.result == AuditResult.SUCCESS
        assert event.event_id.startswith("A-")

    def test_event_id_format(self):
        event = AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="test",
        )
        assert event.event_id.startswith("A-")
        parts = event.event_id.split("-")
        assert len(parts) == 3
        assert len(parts[1]) == 13
        assert len(parts[2]) == 8

    def test_event_id_uniqueness(self):
        e1 = AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="test")
        e2 = AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="test")
        assert e1.event_id != e2.event_id

    def test_to_dict(self):
        event = AuditEvent(
            event_type=AuditEventType.WORKSPACE_ARCHIVE,
            actor_uid=1000,
            actor_role="admin",
            action="archive",
            target="workspace:ws-1",
            result=AuditResult.SUCCESS,
            details={"reason": "cleanup"},
            client_ip="127.0.0.1",
        )
        d = event.to_dict()
        assert d["event_type"] == "workspace_archive"
        assert d["actor_uid"] == 1000
        assert d["action"] == "archive"
        assert d["target"] == "workspace:ws-1"
        assert d["result"] == "success"
        assert d["details"] == {"reason": "cleanup"}
        assert d["client_ip"] == "127.0.0.1"

    def test_to_json(self):
        event = AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="test")
        s = event.to_json()
        assert isinstance(s, str)
        assert json.loads(s)["event_type"] == "admin_operation"

    def test_from_dict(self):
        data = {
            "event_id": "A-1234567890123-abcd",
            "timestamp": 1783698970.0,
            "event_type": "workspace_register",
            "actor_uid": 1000,
            "actor_role": "admin",
            "action": "register",
            "target": "workspace:ws-1",
            "result": "success",
            "details": {"key": "value"},
            "client_ip": "127.0.0.1",
        }
        event = AuditEvent.from_dict(data)
        assert event.event_id == "A-1234567890123-abcd"
        assert event.event_type == AuditEventType.WORKSPACE_REGISTER
        assert event.actor_uid == 1000
        assert event.action == "register"
        assert event.details == {"key": "value"}

    def test_roundtrip(self):
        original = AuditEvent(
            event_type=AuditEventType.TOKEN_REVOKE,
            actor_uid=0,
            actor_role="admin",
            action="revoke",
            target="token:container-1",
            result=AuditResult.SUCCESS,
            details={"container_id": "container-1"},
        )
        restored = AuditEvent.from_dict(original.to_dict())
        assert restored.event_type == original.event_type
        assert restored.actor_uid == original.actor_uid
        assert restored.action == original.action
        assert restored.details == original.details

    def test_default_values(self):
        event = AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="test")
        assert event.target == ""
        assert event.result == AuditResult.SUCCESS
        assert event.details == {}
        assert event.client_ip == ""
        assert event.timestamp > 0


# ============================================================
# AuditLogger 测试（daemon-backed）
# ============================================================


class TestAuditLoggerMemory:
    def test_log_then_query(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="test"))
        assert len(logger.query()) == 1

    def test_query_filter_by_type(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="admin_op"))
        logger.log(AuditEvent(event_type=AuditEventType.WORKSPACE_REGISTER, actor_uid=1000, actor_role="user", action="register"))
        events = logger.query(event_type=AuditEventType.ADMIN_OPERATION)
        assert len(events) == 1
        assert events[0]["action"] == "admin_op"

    def test_query_filter_by_uid(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=1000, actor_role="admin", action="op1"))
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=1001, actor_role="user", action="op2"))
        events = logger.query(actor_uid=1000)
        assert len(events) == 1
        assert events[0]["actor_uid"] == 1000

    def test_query_filter_by_result(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="ok", result=AuditResult.SUCCESS))
        logger.log(AuditEvent(event_type=AuditEventType.ACCESS_DENIED, actor_uid=1001, actor_role="user", action="denied", result=AuditResult.DENIED))
        assert len(logger.query(result=AuditResult.SUCCESS)) == 1
        assert len(logger.query(result=AuditResult.DENIED)) == 1

    def test_query_filter_by_time(self, fake_daemon):
        logger = AuditLogger(db_path="")
        old_time = time.time() - 100
        new_time = time.time()
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="old", timestamp=old_time))
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="new", timestamp=new_time))
        events = logger.query(start_time=old_time + 50)
        assert len(events) == 1
        assert events[0]["action"] == "new"

    def test_query_limit_and_offset(self, fake_daemon):
        logger = AuditLogger(db_path="")
        for i in range(10):
            logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action=f"op_{i}"))
        assert len(logger.query(limit=5)) == 5
        assert len(logger.query(limit=5, offset=5)) == 5

    def test_count(self, fake_daemon):
        logger = AuditLogger(db_path="")
        for i in range(5):
            logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action=f"op_{i}"))
        assert logger.count() == 5

    def test_count_with_filter(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="admin"))
        logger.log(AuditEvent(event_type=AuditEventType.ACCESS_DENIED, actor_uid=1001, actor_role="user", action="denied"))
        assert logger.count(actor_uid=0) == 1
        assert logger.count(event_type=AuditEventType.ACCESS_DENIED) == 1

    def test_clear(self, fake_daemon):
        logger = AuditLogger(db_path="")
        for i in range(3):
            logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action=f"op_{i}"))
        logger.clear()
        assert logger.count() == 0

    def test_get_stats(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="op"))
        stats = logger.get_stats()
        assert stats["total"] == 1
        assert stats["buffer_size"] == 0


# ============================================================
# AuditLogger 便捷方法测试
# ============================================================


class TestAuditLoggerConvenienceMethods:
    def test_log_admin_operation(self, fake_daemon):
        logger = AuditLogger(db_path="")
        event = logger.log_admin_operation(actor_uid=0, actor_role="admin", action="gc_run", target="database", details={"duration": 1.5})
        assert event.event_type == AuditEventType.ADMIN_OPERATION
        assert event.actor_uid == 0
        assert event.action == "gc_run"
        assert event.target == "database"
        assert event.details == {"duration": 1.5}

    def test_log_access_denied(self, fake_daemon):
        logger = AuditLogger(db_path="")
        event = logger.log_access_denied(actor_uid=1001, actor_role="user", action="query", target="workspace:ws-1", reason="cross_uid_query denied")
        assert event.event_type == AuditEventType.ACCESS_DENIED
        assert event.result == AuditResult.DENIED
        assert event.details["reason"] == "cross_uid_query denied"

    def test_log_workspace_operation_register(self, fake_daemon):
        logger = AuditLogger(db_path="")
        event = logger.log_workspace_operation(event_type=AuditEventType.WORKSPACE_REGISTER, actor_uid=1000, actor_role="user", action="register", workspace_id="ws-abc123")
        assert event.event_type == AuditEventType.WORKSPACE_REGISTER
        assert event.target == "workspace:ws-abc123"

    def test_log_workspace_operation_archive(self, fake_daemon):
        logger = AuditLogger(db_path="")
        event = logger.log_workspace_operation(event_type=AuditEventType.WORKSPACE_ARCHIVE, actor_uid=0, actor_role="admin", action="archive", workspace_id="ws-abc123")
        assert event.event_type == AuditEventType.WORKSPACE_ARCHIVE
        assert event.target == "workspace:ws-abc123"

    def test_log_token_operation_generate(self, fake_daemon):
        logger = AuditLogger(db_path="")
        event = logger.log_token_operation(event_type=AuditEventType.TOKEN_GENERATE, actor_uid=0, actor_role="admin", action="generate", container_id="container-1")
        assert event.event_type == AuditEventType.TOKEN_GENERATE
        assert event.target == "token:container-1"
        assert event.details["container_id"] == "container-1"

    def test_log_token_operation_revoke(self, fake_daemon):
        logger = AuditLogger(db_path="")
        event = logger.log_token_operation(event_type=AuditEventType.TOKEN_REVOKE, actor_uid=0, actor_role="admin", action="revoke", container_id="container-1")
        assert event.event_type == AuditEventType.TOKEN_REVOKE
        assert event.target == "token:container-1"


# ============================================================
# AuditLogger 持久化（daemon 权威，跨实例共享）
# ============================================================


class TestAuditLoggerPersistence:
    def test_log_then_query(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="test"))
        assert len(logger.query()) == 1

    def test_persistence_across_instances(self, fake_daemon):
        # 两个实例共享 daemon 权威存储（而非本地文件）。
        logger1 = AuditLogger(db_path="")
        logger1.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="persist_test"))
        logger2 = AuditLogger(db_path="")
        events = logger2.query()
        assert len(events) == 1
        assert events[0]["action"] == "persist_test"

    def test_query_filtered(self, fake_daemon):
        logger = AuditLogger(db_path="")
        for i in range(5):
            is_denied = i >= 3
            logger.log(AuditEvent(
                event_type=AuditEventType.ADMIN_OPERATION if i < 3 else AuditEventType.ACCESS_DENIED,
                actor_uid=i if i < 2 else 1000,
                actor_role="admin" if i < 2 else "user",
                action=f"action_{i}",
                result=AuditResult.DENIED if is_denied else AuditResult.SUCCESS,
            ))
        assert logger.count() == 5
        assert len(logger.query(event_type=AuditEventType.ADMIN_OPERATION)) == 3
        assert len(logger.query(actor_uid=0)) == 1
        assert len(logger.query(result=AuditResult.SUCCESS)) == 3
        assert len(logger.query(result=AuditResult.DENIED)) == 2

    def test_get_stats_with_db(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="op1"))
        logger.log(AuditEvent(event_type=AuditEventType.ACCESS_DENIED, actor_uid=1001, actor_role="user", action="denied", result=AuditResult.DENIED))
        stats = logger.get_stats()
        assert stats["total"] == 2
        assert "admin_operation" in stats["by_type"]
        assert "access_denied" in stats["by_type"]
        assert "success" in stats["by_result"]
        assert "denied" in stats["by_result"]

    def test_clear_db(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="test"))
        assert logger.count() == 1
        logger.clear()
        assert logger.count() == 0

    def test_details_stored_as_object(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="test", details={"key": "value", "number": 42}))
        events = logger.query()
        assert events[0]["details"] == {"key": "value", "number": 42}


# ============================================================
# 全局单例测试
# ============================================================


class TestGlobalAuditLogger:
    def test_get_audit_logger_returns_instance(self):
        reset_audit_logger()
        assert isinstance(get_audit_logger(), AuditLogger)

    def test_get_audit_logger_singleton(self):
        reset_audit_logger()
        assert get_audit_logger() is get_audit_logger()

    def test_reset_audit_logger(self):
        l1 = get_audit_logger()
        reset_audit_logger()
        assert l1 is not get_audit_logger()

    def test_global_logger_can_log(self, fake_daemon):
        reset_audit_logger()
        logger = get_audit_logger()
        logger.clear()
        logger.log(AuditEvent(event_type=AuditEventType.ADMIN_OPERATION, actor_uid=0, actor_role="admin", action="global_test"))
        assert len(logger.query()) == 1
        logger.clear()


# ============================================================
# 安全审计场景测试
# ============================================================


class TestSecurityAuditScenarios:
    def test_admin_operation_audited(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log_admin_operation(actor_uid=0, actor_role="admin", action="schema_migration", target="database", details={"version": "1.0", "tables_added": 3})
        events = logger.query(event_type=AuditEventType.ADMIN_OPERATION)
        assert len(events) == 1
        assert events[0]["action"] == "schema_migration"
        assert events[0]["actor_role"] == "admin"

    def test_cross_uid_denied_audited(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log_access_denied(actor_uid=1001, actor_role="user", action="query", target="workspace:ws-1000", reason="cross_uid_query denied")
        events = logger.query(event_type=AuditEventType.ACCESS_DENIED)
        assert len(events) == 1
        assert events[0]["result"] == "denied"
        assert "cross_uid" in events[0]["details"]["reason"]

    def test_symlink_escape_denied_audited(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log_access_denied(actor_uid=1001, actor_role="user", action="file_read", target="/workspace/../../etc/passwd", reason="path contains '..' escape attempt")
        events = logger.query(event_type=AuditEventType.ACCESS_DENIED)
        assert len(events) == 1
        assert "escape" in events[0]["details"]["reason"]

    def test_tcp_token_denied_audited(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log_access_denied(actor_uid=-1, actor_role="unknown", action="tcp_connect", target="daemon", reason="TCP token invalid: token not found", client_ip="192.168.1.100")
        events = logger.query(event_type=AuditEventType.ACCESS_DENIED)
        assert len(events) == 1
        assert events[0]["client_ip"] == "192.168.1.100"
        assert "token" in events[0]["details"]["reason"]

    def test_token_generation_audited(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log_token_operation(event_type=AuditEventType.TOKEN_GENERATE, actor_uid=0, actor_role="admin", action="generate", container_id="container-1", details={"expires_in": 3600})
        events = logger.query(event_type=AuditEventType.TOKEN_GENERATE)
        assert len(events) == 1
        assert events[0]["target"] == "token:container-1"

    def test_token_revocation_audited(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log_token_operation(event_type=AuditEventType.TOKEN_REVOKE, actor_uid=0, actor_role="admin", action="revoke", container_id="container-1")
        events = logger.query(event_type=AuditEventType.TOKEN_REVOKE)
        assert len(events) == 1
        assert events[0]["action"] == "revoke"

    def test_audit_trail_completeness(self, fake_daemon):
        logger = AuditLogger(db_path="")
        logger.log_workspace_operation(event_type=AuditEventType.WORKSPACE_REGISTER, actor_uid=0, actor_role="admin", action="register", workspace_id="ws-1")
        logger.log_access_denied(actor_uid=1001, actor_role="user", action="query", target="workspace:ws-1", reason="cross_uid_query denied")
        logger.log_token_operation(event_type=AuditEventType.TOKEN_GENERATE, actor_uid=0, actor_role="admin", action="generate", container_id="container-1")
        logger.log_workspace_operation(event_type=AuditEventType.WORKSPACE_ARCHIVE, actor_uid=0, actor_role="admin", action="archive", workspace_id="ws-1")

        all_events = logger.query()
        assert len(all_events) == 4
        all_events.sort(key=lambda x: x["timestamp"])
        assert all_events[0]["event_type"] == "workspace_register"
        assert all_events[1]["event_type"] == "access_denied"
        assert all_events[2]["event_type"] == "token_generate"
        assert all_events[3]["event_type"] == "workspace_archive"
