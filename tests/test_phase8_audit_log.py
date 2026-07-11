"""Phase 8.4: audit log 测试。

测试覆盖：
1. AuditEventType / AuditResult：枚举
2. AuditEvent：创建、序列化、反序列化、event_id 生成
3. AuditLogger：记录、查询、统计
4. 便捷方法：log_admin_operation、log_access_denied、log_workspace_operation、log_token_operation
5. 持久化：SQLite 存储、重载
6. 查询过滤：按时间、类型、UID、结果
7. 全局单例
"""

import os
import time
import json
import sqlite3
import pytest

from callwarden.server.audit_log import (
    AuditEventType,
    AuditResult,
    AuditEvent,
    AuditLogger,
    get_audit_logger,
    reset_audit_logger,
)


# ============================================================
# AuditEventType / AuditResult 测试
# ============================================================


class TestAuditEnums:
    """审计枚举测试。"""

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
    """AuditEvent 测试。"""

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
        # A-<13ts>-<4hex>
        assert event.event_id.startswith("A-")
        parts = event.event_id.split("-")
        assert len(parts) == 3
        assert len(parts[1]) == 13  # 13 位时间戳
        assert len(parts[2]) == 4   # 4 位 hex

    def test_event_id_uniqueness(self):
        event1 = AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="test",
        )
        event2 = AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="test",
        )
        assert event1.event_id != event2.event_id

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
        event = AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="test",
        )
        s = event.to_json()
        assert isinstance(s, str)
        parsed = json.loads(s)
        assert parsed["event_type"] == "admin_operation"

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
        data = original.to_dict()
        restored = AuditEvent.from_dict(data)
        assert restored.event_type == original.event_type
        assert restored.actor_uid == original.actor_uid
        assert restored.action == original.action
        assert restored.details == original.details

    def test_default_values(self):
        event = AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="test",
        )
        assert event.target == ""
        assert event.result == AuditResult.SUCCESS
        assert event.details == {}
        assert event.client_ip == ""
        assert event.timestamp > 0


# ============================================================
# AuditLogger 测试（内存模式）
# ============================================================


class TestAuditLoggerMemory:
    """AuditLogger 内存模式测试（无 DB）。"""

    def test_log_to_buffer(self):
        logger = AuditLogger(db_path="")
        event = AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="test",
        )
        logger.log(event)
        assert len(logger._buffer) == 1

    def test_query_from_buffer(self):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="test1",
        ))
        logger.log(AuditEvent(
            event_type=AuditEventType.WORKSPACE_REGISTER,
            actor_uid=1000,
            actor_role="user",
            action="register",
        ))

        events = logger.query()
        assert len(events) == 2

    def test_query_filter_by_type(self):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="admin_op",
        ))
        logger.log(AuditEvent(
            event_type=AuditEventType.WORKSPACE_REGISTER,
            actor_uid=1000,
            actor_role="user",
            action="register",
        ))

        events = logger.query(event_type=AuditEventType.ADMIN_OPERATION)
        assert len(events) == 1
        assert events[0]["action"] == "admin_op"

    def test_query_filter_by_uid(self):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=1000,
            actor_role="admin",
            action="op1",
        ))
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=1001,
            actor_role="user",
            action="op2",
        ))

        events = logger.query(actor_uid=1000)
        assert len(events) == 1
        assert events[0]["actor_uid"] == 1000

    def test_query_filter_by_result(self):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="ok",
            result=AuditResult.SUCCESS,
        ))
        logger.log(AuditEvent(
            event_type=AuditEventType.ACCESS_DENIED,
            actor_uid=1001,
            actor_role="user",
            action="denied",
            result=AuditResult.DENIED,
        ))

        success_events = logger.query(result=AuditResult.SUCCESS)
        assert len(success_events) == 1
        assert success_events[0]["action"] == "ok"

        denied_events = logger.query(result=AuditResult.DENIED)
        assert len(denied_events) == 1
        assert denied_events[0]["action"] == "denied"

    def test_query_filter_by_time(self):
        logger = AuditLogger(db_path="")
        old_time = time.time() - 100
        new_time = time.time()

        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="old",
            timestamp=old_time,
        ))
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="new",
            timestamp=new_time,
        ))

        events = logger.query(start_time=old_time + 50)
        assert len(events) == 1
        assert events[0]["action"] == "new"

    def test_query_limit_and_offset(self):
        logger = AuditLogger(db_path="")
        for i in range(10):
            logger.log(AuditEvent(
                event_type=AuditEventType.ADMIN_OPERATION,
                actor_uid=0,
                actor_role="admin",
                action=f"op_{i}",
            ))

        events = logger.query(limit=5)
        assert len(events) == 5

        events_offset = logger.query(limit=5, offset=5)
        assert len(events_offset) == 5

    def test_count(self):
        logger = AuditLogger(db_path="")
        for i in range(5):
            logger.log(AuditEvent(
                event_type=AuditEventType.ADMIN_OPERATION,
                actor_uid=0,
                actor_role="admin",
                action=f"op_{i}",
            ))
        assert logger.count() == 5

    def test_count_with_filter(self):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="admin",
        ))
        logger.log(AuditEvent(
            event_type=AuditEventType.ACCESS_DENIED,
            actor_uid=1001,
            actor_role="user",
            action="denied",
        ))

        assert logger.count(actor_uid=0) == 1
        assert logger.count(event_type=AuditEventType.ACCESS_DENIED) == 1

    def test_clear(self):
        logger = AuditLogger(db_path="")
        for i in range(3):
            logger.log(AuditEvent(
                event_type=AuditEventType.ADMIN_OPERATION,
                actor_uid=0,
                actor_role="admin",
                action=f"op_{i}",
            ))
        logger.clear()
        assert logger.count() == 0

    def test_get_stats_memory_mode(self):
        logger = AuditLogger(db_path="")
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="op",
        ))
        stats = logger.get_stats()
        assert stats["total"] == 1
        assert stats["buffer_size"] == 1


# ============================================================
# AuditLogger 便捷方法测试
# ============================================================


class TestAuditLoggerConvenienceMethods:
    """AuditLogger 便捷方法测试。"""

    def test_log_admin_operation(self):
        logger = AuditLogger(db_path="")
        event = logger.log_admin_operation(
            actor_uid=0,
            actor_role="admin",
            action="gc_run",
            target="database",
            details={"duration": 1.5},
        )
        assert event.event_type == AuditEventType.ADMIN_OPERATION
        assert event.actor_uid == 0
        assert event.action == "gc_run"
        assert event.target == "database"
        assert event.details == {"duration": 1.5}

    def test_log_access_denied(self):
        logger = AuditLogger(db_path="")
        event = logger.log_access_denied(
            actor_uid=1001,
            actor_role="user",
            action="query",
            target="workspace:ws-1",
            reason="cross_uid_query denied",
        )
        assert event.event_type == AuditEventType.ACCESS_DENIED
        assert event.result == AuditResult.DENIED
        assert event.details["reason"] == "cross_uid_query denied"

    def test_log_workspace_operation_register(self):
        logger = AuditLogger(db_path="")
        event = logger.log_workspace_operation(
            event_type=AuditEventType.WORKSPACE_REGISTER,
            actor_uid=1000,
            actor_role="user",
            action="register",
            workspace_id="ws-abc123",
        )
        assert event.event_type == AuditEventType.WORKSPACE_REGISTER
        assert event.target == "workspace:ws-abc123"

    def test_log_workspace_operation_archive(self):
        logger = AuditLogger(db_path="")
        event = logger.log_workspace_operation(
            event_type=AuditEventType.WORKSPACE_ARCHIVE,
            actor_uid=0,
            actor_role="admin",
            action="archive",
            workspace_id="ws-abc123",
        )
        assert event.event_type == AuditEventType.WORKSPACE_ARCHIVE
        assert event.target == "workspace:ws-abc123"

    def test_log_token_operation_generate(self):
        logger = AuditLogger(db_path="")
        event = logger.log_token_operation(
            event_type=AuditEventType.TOKEN_GENERATE,
            actor_uid=0,
            actor_role="admin",
            action="generate",
            container_id="container-1",
        )
        assert event.event_type == AuditEventType.TOKEN_GENERATE
        assert event.target == "token:container-1"
        assert event.details["container_id"] == "container-1"

    def test_log_token_operation_revoke(self):
        logger = AuditLogger(db_path="")
        event = logger.log_token_operation(
            event_type=AuditEventType.TOKEN_REVOKE,
            actor_uid=0,
            actor_role="admin",
            action="revoke",
            container_id="container-1",
        )
        assert event.event_type == AuditEventType.TOKEN_REVOKE
        assert event.target == "token:container-1"


# ============================================================
# AuditLogger 持久化测试
# ============================================================


class TestAuditLoggerPersistence:
    """AuditLogger SQLite 持久化测试。"""

    def test_log_to_db(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="test",
        ))

        # 查询应能找到
        events = logger.query()
        assert len(events) == 1
        assert events[0]["action"] == "test"

    def test_db_created(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        AuditLogger(db_path=db_path)
        assert os.path.isfile(db_path)

    def test_table_structure(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        AuditLogger(db_path=db_path)

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row[0] for row in cursor.fetchall()]
        assert "audit_log" in tables

        # 验证索引
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
        indexes = [row[0] for row in cursor.fetchall()]
        assert "idx_audit_timestamp" in indexes
        assert "idx_audit_type" in indexes
        assert "idx_audit_actor" in indexes
        assert "idx_audit_result" in indexes
        conn.close()

    def test_persistence_across_instances(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        logger1 = AuditLogger(db_path=db_path)
        logger1.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="persist_test",
        ))

        # 新实例应能读到之前的记录
        logger2 = AuditLogger(db_path=db_path)
        events = logger2.query()
        assert len(events) == 1
        assert events[0]["action"] == "persist_test"

    def test_query_from_db(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)

        for i in range(5):
            is_denied = i >= 3
            logger.log(AuditEvent(
                event_type=AuditEventType.ADMIN_OPERATION if i < 3 else AuditEventType.ACCESS_DENIED,
                actor_uid=i if i < 2 else 1000,
                actor_role="admin" if i < 2 else "user",
                action=f"action_{i}",
                result=AuditResult.DENIED if is_denied else AuditResult.SUCCESS,
            ))

        # 查询所有
        assert logger.count() == 5

        # 按类型过滤
        admin_ops = logger.query(event_type=AuditEventType.ADMIN_OPERATION)
        assert len(admin_ops) == 3

        # 按 UID 过滤
        uid_0_events = logger.query(actor_uid=0)
        assert len(uid_0_events) == 1

        # 按结果过滤
        success_events = logger.query(result=AuditResult.SUCCESS)
        assert len(success_events) == 3

        denied_events = logger.query(result=AuditResult.DENIED)
        assert len(denied_events) == 2

    def test_get_stats_with_db(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)

        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="op1",
        ))
        logger.log(AuditEvent(
            event_type=AuditEventType.ACCESS_DENIED,
            actor_uid=1001,
            actor_role="user",
            action="denied",
            result=AuditResult.DENIED,
        ))

        stats = logger.get_stats()
        assert stats["total"] == 2
        assert "admin_operation" in stats["by_type"]
        assert "access_denied" in stats["by_type"]
        assert "success" in stats["by_result"]
        assert "denied" in stats["by_result"]

    def test_clear_db(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="test",
        ))
        assert logger.count() == 1

        logger.clear()
        assert logger.count() == 0

    def test_creates_parent_dir(self, tmp_path):
        db_path = str(tmp_path / "subdir" / "audit.db")
        AuditLogger(db_path=db_path)
        assert os.path.isfile(db_path)

    def test_details_stored_as_json(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="test",
            details={"key": "value", "number": 42},
        ))

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT details FROM audit_log")
        row = cursor.fetchone()
        details = json.loads(row["details"])
        assert details == {"key": "value", "number": 42}
        conn.close()


# ============================================================
# 全局单例测试
# ============================================================


class TestGlobalAuditLogger:
    """全局 AuditLogger 单例测试。"""

    def test_get_audit_logger_returns_instance(self):
        reset_audit_logger()
        logger = get_audit_logger()
        assert isinstance(logger, AuditLogger)

    def test_get_audit_logger_singleton(self):
        reset_audit_logger()
        l1 = get_audit_logger()
        l2 = get_audit_logger()
        assert l1 is l2

    def test_reset_audit_logger(self):
        l1 = get_audit_logger()
        reset_audit_logger()
        l2 = get_audit_logger()
        assert l1 is not l2

    def test_global_logger_can_log(self):
        reset_audit_logger()
        logger = get_audit_logger()
        logger.clear()
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="global_test",
        ))
        events = logger.query()
        assert len(events) == 1
        logger.clear()


# ============================================================
# 安全审计场景测试
# ============================================================


class TestSecurityAuditScenarios:
    """安全审计场景测试。

    覆盖设计文档中的安全测试要求：
    - admin 操作应写审计日志
    - 越权路径应记录 access_denied
    """

    def test_admin_operation_audited(self, tmp_path):
        """admin 操作应写审计日志。"""
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)

        logger.log_admin_operation(
            actor_uid=0,
            actor_role="admin",
            action="schema_migration",
            target="database",
            details={"version": "1.0", "tables_added": 3},
        )

        events = logger.query(event_type=AuditEventType.ADMIN_OPERATION)
        assert len(events) == 1
        assert events[0]["action"] == "schema_migration"
        assert events[0]["actor_role"] == "admin"

    def test_cross_uid_denied_audited(self, tmp_path):
        """越权查询应记录 access_denied。"""
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)

        logger.log_access_denied(
            actor_uid=1001,
            actor_role="user",
            action="query",
            target="workspace:ws-1000",
            reason="cross_uid_query denied",
        )

        events = logger.query(event_type=AuditEventType.ACCESS_DENIED)
        assert len(events) == 1
        assert events[0]["result"] == "denied"
        assert "cross_uid" in events[0]["details"]["reason"]

    def test_symlink_escape_denied_audited(self, tmp_path):
        """symlink 逃逸应记录 access_denied。"""
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)

        logger.log_access_denied(
            actor_uid=1001,
            actor_role="user",
            action="file_read",
            target="/workspace/../../etc/passwd",
            reason="path contains '..' escape attempt",
        )

        events = logger.query(event_type=AuditEventType.ACCESS_DENIED)
        assert len(events) == 1
        assert "escape" in events[0]["details"]["reason"]

    def test_tcp_token_denied_audited(self, tmp_path):
        """TCP token 错误应记录 access_denied。"""
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)

        logger.log_access_denied(
            actor_uid=-1,
            actor_role="unknown",
            action="tcp_connect",
            target="daemon",
            reason="TCP token invalid: token not found",
            client_ip="192.168.1.100",
        )

        events = logger.query(event_type=AuditEventType.ACCESS_DENIED)
        assert len(events) == 1
        assert events[0]["client_ip"] == "192.168.1.100"
        assert "token" in events[0]["details"]["reason"]

    def test_token_generation_audited(self, tmp_path):
        """token 生成应写审计日志。"""
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)

        logger.log_token_operation(
            event_type=AuditEventType.TOKEN_GENERATE,
            actor_uid=0,
            actor_role="admin",
            action="generate",
            container_id="container-1",
            details={"expires_in": 3600},
        )

        events = logger.query(event_type=AuditEventType.TOKEN_GENERATE)
        assert len(events) == 1
        assert events[0]["target"] == "token:container-1"

    def test_token_revocation_audited(self, tmp_path):
        """token 撤销应写审计日志。"""
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)

        logger.log_token_operation(
            event_type=AuditEventType.TOKEN_REVOKE,
            actor_uid=0,
            actor_role="admin",
            action="revoke",
            container_id="container-1",
        )

        events = logger.query(event_type=AuditEventType.TOKEN_REVOKE)
        assert len(events) == 1
        assert events[0]["action"] == "revoke"

    def test_audit_trail_completeness(self, tmp_path):
        """审计日志应完整记录操作链。"""
        db_path = str(tmp_path / "audit.db")
        logger = AuditLogger(db_path=db_path)

        # 模拟一次完整的操作链
        # 1. admin 注册 workspace
        logger.log_workspace_operation(
            event_type=AuditEventType.WORKSPACE_REGISTER,
            actor_uid=0,
            actor_role="admin",
            action="register",
            workspace_id="ws-1",
        )

        # 2. user 尝试查询其他人的 workspace（被拒绝）
        logger.log_access_denied(
            actor_uid=1001,
            actor_role="user",
            action="query",
            target="workspace:ws-1",
            reason="cross_uid_query denied",
        )

        # 3. admin 生成 token
        logger.log_token_operation(
            event_type=AuditEventType.TOKEN_GENERATE,
            actor_uid=0,
            actor_role="admin",
            action="generate",
            container_id="container-1",
        )

        # 4. admin 归档 workspace
        logger.log_workspace_operation(
            event_type=AuditEventType.WORKSPACE_ARCHIVE,
            actor_uid=0,
            actor_role="admin",
            action="archive",
            workspace_id="ws-1",
        )

        # 验证审计链
        all_events = logger.query()
        assert len(all_events) == 4

        # 按时间正序排列
        all_events.sort(key=lambda x: x["timestamp"])

        assert all_events[0]["event_type"] == "workspace_register"
        assert all_events[1]["event_type"] == "access_denied"
        assert all_events[2]["event_type"] == "token_generate"
        assert all_events[3]["event_type"] == "workspace_archive"
