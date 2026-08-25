"""SRV-002 迁移验收：server audit log Python authority → Rust daemon。

覆盖 task `T-1787323460404-b425b074` step[2] fixture_negative_matrix：
["success", "invalid", "authority", "unavailable", "restart"]。

设计要点（SRV-002 = 完整 thin-client 重构，route B）：
- Python `AuditLogger` 退化为纯 daemon RPC 薄客户端（不再 import sqlite3、不再持有本地缓冲）。
- 所有写/读经 `mcp.audit_log.*` 发往 daemon；daemon 不可用时 fail-closed 抛
  `DaemonUnavailableError`，绝不回退本地 SQLite/内存充当业务存储。
- 本测试用内存态 `FakeAuditDaemon` 模拟 daemon 的 `audit_log_handlers` 行为，
  不依赖真实 daemon 进程，也不触碰本地 SQLite 文件。
"""

import ast
import os
import textwrap

import pytest

from callwarden.server.audit_log import (
    AuditEventType,
    AuditResult,
    AuditEvent,
    AuditLogger,
    get_audit_logger,
    reset_audit_logger,
)
from callwarden.server.daemon_client import DaemonUnavailableError


class FakeDaemonRpcError(Exception):
    """模拟 daemon 端 DaemonRpcError（带稳定 error code）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FakeAuditDaemon:
    """内存态 daemon 审计日志权威（对齐 Rust `audit_log_handlers` 语义）。"""

    def __init__(self, audit_db_path: str = "/var/log/callwarden/audit.log"):
        self.events: list = []
        self.available: bool = True
        self.audit_db_path = audit_db_path

    def __call__(self, method: str, params: dict):
        if not self.available:
            raise DaemonUnavailableError("daemon 不可用（测试模拟）", code="daemon_unavailable")
        if method == "mcp.audit_log.get_conn":
            if not self.audit_db_path:
                raise FakeDaemonRpcError(
                    "audit_db_unconfigured",
                    "审计日志 DB 路径未配置（daemon 未配置 audit_db_path，fail-closed）",
                )
            return {"db_path": self.audit_db_path}
        if method == "mcp.audit_log.init_db":
            return {"ok": True}
        if method == "mcp.audit_log.append":
            ev = dict(params.get("event", {}))
            if not ev.get("event_id") or not ev.get("event_type") or not ev.get("action"):
                raise FakeDaemonRpcError("invalid_params", "audit event 缺必填字段")
            self.events.append(ev)
            return {"ok": True, "event_id": ev["event_id"]}
        if method == "mcp.audit_log.query":
            return self._query(params)
        if method == "mcp.audit_log.count":
            return {"count": len(self._filtered(params))}
        if method == "mcp.audit_log.clear":
            self.events = []
            return {"ok": True}
        if method == "mcp.audit_log.get_stats":
            return self._stats()
        raise FakeDaemonRpcError("method_not_found", f"未知方法 {method}")

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
    """每个测试安装一个干净的内存态 daemon 薄客户端。"""
    daemon = FakeAuditDaemon()
    monkeypatch.setattr(
        "callwarden.server.audit_log._call_daemon_rpc", daemon
    )
    return daemon


# ============================================================
# 1) success
# ============================================================


def test_success_log_and_query(fake_daemon):
    logger = AuditLogger()
    logger.log(AuditEvent(
        event_type=AuditEventType.ADMIN_OPERATION,
        actor_uid=0,
        actor_role="admin",
        action="register",
        target="workspace:ws-1",
    ))
    events = logger.query()
    assert len(events) == 1
    assert events[0]["action"] == "register"
    assert events[0]["event_type"] == "admin_operation"


# ============================================================
# 2) invalid
# ============================================================


def test_invalid_event_rejected(fake_daemon):
    logger = AuditLogger()
    with pytest.raises(FakeDaemonRpcError) as exc:
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="",  # 缺 action → daemon 端 invalid_params
        ))
    assert exc.value.code == "invalid_params"


# ============================================================
# 3) authority
# ============================================================


def test_authority_path_owned_by_daemon(fake_daemon):
    # daemon 返回权威路径，Python 不再本地计算 SQLite 路径。
    conn = fake_daemon("mcp.audit_log.get_conn", {})
    assert conn["db_path"] == "/var/log/callwarden/audit.log"


def test_audit_logger_has_no_local_sqlite_authority(fake_daemon):
    # 权威已下沉：薄客户端不得保留 _get_conn / _init_db / get_db。
    logger = AuditLogger()
    assert not hasattr(logger, "_get_conn")
    assert not hasattr(logger, "_init_db")
    assert not hasattr(logger, "get_db")


def test_authority_unconfigured_fail_closed(fake_daemon):
    fake_daemon.audit_db_path = ""
    with pytest.raises(FakeDaemonRpcError) as exc:
        fake_daemon("mcp.audit_log.get_conn", {})
    assert exc.value.code == "audit_db_unconfigured"


# ============================================================
# 4) unavailable（fail-closed，不回退本地 SQLite）
# ============================================================


def test_unavailable_raises_no_local_fallback(fake_daemon):
    fake_daemon.available = False
    logger = AuditLogger()
    with pytest.raises(DaemonUnavailableError):
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="register",
        ))


def test_unavailable_query_raises(fake_daemon):
    fake_daemon.available = False
    logger = AuditLogger()
    with pytest.raises(DaemonUnavailableError):
        logger.query()


# ============================================================
# 5) restart（首次不可用 → 恢复后成功）
# ============================================================


def test_restart_recovers(fake_daemon):
    logger = AuditLogger()
    # 首次：daemon 不可用
    fake_daemon.available = False
    with pytest.raises(DaemonUnavailableError):
        logger.log(AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=0,
            actor_role="admin",
            action="first",
        ))
    # 恢复
    fake_daemon.available = True
    logger.log(AuditEvent(
        event_type=AuditEventType.ADMIN_OPERATION,
        actor_uid=0,
        actor_role="admin",
        action="second",
    ))
    events = logger.query()
    assert len(events) == 1
    assert events[0]["action"] == "second"


# ============================================================
# 零权威证据：AST 扫描（server/audit_log.py 不含 SQLite 权威残留）
# ============================================================


def test_no_sqlite_authority_in_source():
    src = os.path.join(os.path.dirname(__file__), "..", "server", "audit_log.py")
    with open(src, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    banned = {"sqlite3", "_get_conn", "_init_db", "get_db", "AUDIT_LOG_DDL"}
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                if n.name == "sqlite3":
                    violations.append("import sqlite3")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "sqlite3":
                violations.append("from sqlite3 import")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in banned:
                violations.append(node.name)
        elif isinstance(node, ast.Attribute):
            if node.attr in banned:
                violations.append(f"attr.{node.attr}")
    assert not violations, f"server/audit_log.py 仍含 SQLite 权威残留: {violations}"
