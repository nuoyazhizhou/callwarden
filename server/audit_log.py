"""Phase 8.4: Daemon 审计日志。

设计参考：
- docs/design/enterprise-daemon-shared-snapshot-plan.md §Phase 8（audit log）
- docs/design/daemon-ipc-security.md §5（admin 操作应写审计日志）
- 验收：权限测试覆盖越权路径，admin 操作应写审计日志

提供：
1. AuditLogger：审计日志记录器
   - 记录 admin 操作（workspace 注册/归档、token 生成/撤销、job 取消等）
   - 记录权限拒绝事件（越权查询、symlink 逃逸、TCP token 错误等）
   - 记录配置变更
2. AuditEvent：审计事件数据结构
3. 日志存储：SQLite（持久化）+ 内存缓冲（低延迟）
4. 日志查询：按时间、UID、操作类型、结果过滤

审计事件类型：
- workspace_register: workspace 注册
- workspace_archive: workspace 归档
- token_generate: token 生成
- token_revoke: token 撤销
- job_submit: job 提交
- job_cancel: job 取消
- access_denied: 权限拒绝
- config_change: 配置变更
- admin_operation: 其他管理员操作

审计日志格式（JSON）：
{
    "event_id": "A-<13ts>-<4hex>",
    "timestamp": 1783698970.0,
    "event_type": "workspace_register",
    "actor_uid": 1000,
    "actor_role": "admin",
    "action": "register",
    "target": "workspace:ws-abc123",
    "result": "success",
    "details": {...},
    "client_ip": "127.0.0.1"
}
"""

from __future__ import annotations

import os
import time
import json
import sqlite3
import secrets
import threading
from typing import Any, Dict, List, Optional
from enum import Enum


# ============================================================
# 审计事件类型和结果
# ============================================================


class AuditEventType(str, Enum):
    """审计事件类型。"""

    WORKSPACE_REGISTER = "workspace_register"
    WORKSPACE_ARCHIVE = "workspace_archive"
    WORKSPACE_QUERY = "workspace_query"
    TOKEN_GENERATE = "token_generate"
    TOKEN_REVOKE = "token_revoke"
    JOB_SUBMIT = "job_submit"
    JOB_CANCEL = "job_cancel"
    ACCESS_DENIED = "access_denied"
    CONFIG_CHANGE = "config_change"
    ADMIN_OPERATION = "admin_operation"
    SCHEMA_MIGRATION = "schema_migration"
    BACKUP = "backup"
    RESTORE = "restore"


class AuditResult(str, Enum):
    """审计操作结果。"""

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


# ============================================================
# AuditEvent
# ============================================================


class AuditEvent:
    """单个审计事件。"""

    def __init__(
        self,
        event_type: AuditEventType,
        actor_uid: int,
        actor_role: str,
        action: str,
        target: str = "",
        result: AuditResult = AuditResult.SUCCESS,
        details: Optional[Dict[str, Any]] = None,
        client_ip: str = "",
        timestamp: Optional[float] = None,
        event_id: Optional[str] = None,
    ):
        self.event_id = event_id or self._generate_event_id()
        self.timestamp = timestamp if timestamp is not None else time.time()
        self.event_type = event_type
        self.actor_uid = actor_uid
        self.actor_role = actor_role
        self.action = action
        self.target = target
        self.result = result
        self.details = details or {}
        self.client_ip = client_ip

    @staticmethod
    def _generate_event_id() -> str:
        """生成事件 ID：A-<13ts>-<4hex>。"""
        ts = int(time.time() * 1000)
        hex_part = secrets.token_hex(2)
        return f"A-{ts}-{hex_part}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type.value,
            "actor_uid": self.actor_uid,
            "actor_role": self.actor_role,
            "action": self.action,
            "target": self.target,
            "result": self.result.value,
            "details": self.details,
            "client_ip": self.client_ip,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditEvent":
        return cls(
            event_type=AuditEventType(data["event_type"]),
            actor_uid=data["actor_uid"],
            actor_role=data["actor_role"],
            action=data["action"],
            target=data.get("target", ""),
            result=AuditResult(data.get("result", "success")),
            details=data.get("details", {}),
            client_ip=data.get("client_ip", ""),
            timestamp=data.get("timestamp"),
            event_id=data.get("event_id"),
        )


# ============================================================
# AuditLogger
# ============================================================


# audit_log schema DDL
AUDIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    event_id TEXT PRIMARY KEY,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    actor_uid INTEGER NOT NULL,
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT DEFAULT '',
    result TEXT NOT NULL,
    details TEXT DEFAULT '{}',
    client_ip TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_uid);
CREATE INDEX IF NOT EXISTS idx_audit_result ON audit_log(result);
"""


class AuditLogger:
    """审计日志记录器。

    用法：
        logger = AuditLogger("/var/log/callwarden/audit.db")

        # 记录 admin 操作
        logger.log(AuditEvent(
            event_type=AuditEventType.WORKSPACE_REGISTER,
            actor_uid=1000,
            actor_role="admin",
            action="register",
            target="workspace:ws-abc123",
            result=AuditResult.SUCCESS,
        ))

        # 记录权限拒绝
        logger.log_access_denied(
            actor_uid=1001,
            actor_role="user",
            action="query",
            target="workspace:ws-def456",
            reason="cross_uid_query denied",
        )

        # 查询审计日志
        events = logger.query(actor_uid=1000, limit=100)
    """

    def __init__(self, db_path: str = "", buffer_size: int = 1000):
        """初始化审计日志记录器。

        Args:
            db_path: SQLite 数据库路径（为空时只使用内存缓冲）
            buffer_size: 内存缓冲区大小（超出后批量写入 DB）
        """
        self._db_path = db_path
        self._buffer_size = buffer_size
        self._buffer: List[AuditEvent] = []
        self._lock = threading.Lock()

        # 初始化 DB
        if db_path:
            self._init_db()

    def _init_db(self) -> None:
        """初始化 SQLite 数据库。"""
        dir_path = os.path.dirname(self._db_path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)

        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.executescript(AUDIT_LOG_DDL)
        conn.commit()
        conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        """获取 DB 连接。"""
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    # ----- 记录日志 -----

    def log(self, event: AuditEvent) -> None:
        """记录审计事件。

        先写入内存缓冲，缓冲满后批量写入 DB。

        Args:
            event: 审计事件
        """
        with self._lock:
            self._buffer.append(event)
            if len(self._buffer) >= self._buffer_size:
                self._flush_to_db()

        # 即使缓冲未满，也尝试写入 DB（确保持久化）
        if self._db_path:
            self._write_single(event)

    def log_admin_operation(
        self,
        actor_uid: int,
        actor_role: str,
        action: str,
        target: str = "",
        result: AuditResult = AuditResult.SUCCESS,
        details: Optional[Dict[str, Any]] = None,
        client_ip: str = "",
    ) -> AuditEvent:
        """记录管理员操作。

        Args:
            actor_uid: 操作者 UID
            actor_role: 操作者角色
            action: 操作类型
            target: 操作目标
            result: 操作结果
            details: 额外详情
            client_ip: 客户端 IP

        Returns:
            创建的 AuditEvent 实例
        """
        event = AuditEvent(
            event_type=AuditEventType.ADMIN_OPERATION,
            actor_uid=actor_uid,
            actor_role=actor_role,
            action=action,
            target=target,
            result=result,
            details=details,
            client_ip=client_ip,
        )
        self.log(event)
        return event

    def log_access_denied(
        self,
        actor_uid: int,
        actor_role: str,
        action: str,
        target: str = "",
        reason: str = "",
        client_ip: str = "",
    ) -> AuditEvent:
        """记录权限拒绝事件。

        Args:
            actor_uid: 被拒绝者 UID
            actor_role: 被拒绝者角色
            action: 尝试的操作
            target: 操作目标
            reason: 拒绝原因
            client_ip: 客户端 IP

        Returns:
            创建的 AuditEvent 实例
        """
        event = AuditEvent(
            event_type=AuditEventType.ACCESS_DENIED,
            actor_uid=actor_uid,
            actor_role=actor_role,
            action=action,
            target=target,
            result=AuditResult.DENIED,
            details={"reason": reason},
            client_ip=client_ip,
        )
        self.log(event)
        return event

    def log_workspace_operation(
        self,
        event_type: AuditEventType,
        actor_uid: int,
        actor_role: str,
        action: str,
        workspace_id: str,
        result: AuditResult = AuditResult.SUCCESS,
        details: Optional[Dict[str, Any]] = None,
        client_ip: str = "",
    ) -> AuditEvent:
        """记录 workspace 操作。

        Args:
            event_type: 事件类型（WORKSPACE_REGISTER / WORKSPACE_ARCHIVE / WORKSPACE_QUERY）
            actor_uid: 操作者 UID
            actor_role: 操作者角色
            action: 操作类型
            workspace_id: workspace ID
            result: 操作结果
            details: 额外详情
            client_ip: 客户端 IP

        Returns:
            创建的 AuditEvent 实例
        """
        event = AuditEvent(
            event_type=event_type,
            actor_uid=actor_uid,
            actor_role=actor_role,
            action=action,
            target=f"workspace:{workspace_id}",
            result=result,
            details=details,
            client_ip=client_ip,
        )
        self.log(event)
        return event

    def log_token_operation(
        self,
        event_type: AuditEventType,
        actor_uid: int,
        actor_role: str,
        action: str,
        container_id: str = "",
        result: AuditResult = AuditResult.SUCCESS,
        details: Optional[Dict[str, Any]] = None,
        client_ip: str = "",
    ) -> AuditEvent:
        """记录 token 操作。

        Args:
            event_type: 事件类型（TOKEN_GENERATE / TOKEN_REVOKE）
            actor_uid: 操作者 UID
            actor_role: 操作者角色
            action: 操作类型
            container_id: 容器 ID
            result: 操作结果
            details: 额外详情
            client_ip: 客户端 IP

        Returns:
            创建的 AuditEvent 实例
        """
        event_details = details or {}
        if container_id:
            event_details["container_id"] = container_id

        event = AuditEvent(
            event_type=event_type,
            actor_uid=actor_uid,
            actor_role=actor_role,
            action=action,
            target=f"token:{container_id}" if container_id else "token",
            result=result,
            details=event_details,
            client_ip=client_ip,
        )
        self.log(event)
        return event

    # ----- 查询日志 -----

    def query(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        event_type: Optional[AuditEventType] = None,
        actor_uid: Optional[int] = None,
        result: Optional[AuditResult] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """查询审计日志。

        Args:
            start_time: 起始时间戳（包含）
            end_time: 结束时间戳（不包含）
            event_type: 事件类型过滤
            actor_uid: 操作者 UID 过滤
            result: 结果过滤
            limit: 返回条数上限
            offset: 偏移量

        Returns:
            审计事件列表
        """
        if not self._db_path:
            # 从内存缓冲查询
            return self._query_from_buffer(
                start_time, end_time, event_type, actor_uid, result, limit, offset
            )

        # 从 DB 查询
        conditions = []
        params = []

        if start_time is not None:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time is not None:
            conditions.append("timestamp < ?")
            params.append(end_time)
        if event_type is not None:
            conditions.append("event_type = ?")
            params.append(event_type.value)
        if actor_uid is not None:
            conditions.append("actor_uid = ?")
            params.append(actor_uid)
        if result is not None:
            conditions.append("result = ?")
            params.append(result.value)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM audit_log WHERE {where_clause} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        conn = self._get_conn()
        try:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                # 反序列化 details（DB 中存储为 JSON 字符串）
                if isinstance(d.get("details"), str):
                    try:
                        d["details"] = json.loads(d["details"])
                    except (json.JSONDecodeError, TypeError):
                        d["details"] = {}
                result.append(d)
            return result
        finally:
            conn.close()

    def _query_from_buffer(
        self,
        start_time: Optional[float],
        end_time: Optional[float],
        event_type: Optional[AuditEventType],
        actor_uid: Optional[int],
        result: Optional[AuditResult],
        limit: int,
        offset: int,
    ) -> List[Dict[str, Any]]:
        """从内存缓冲查询。"""
        with self._lock:
            events = list(self._buffer)

        filtered = []
        for event in events:
            if start_time is not None and event.timestamp < start_time:
                continue
            if end_time is not None and event.timestamp >= end_time:
                continue
            if event_type is not None and event.event_type != event_type:
                continue
            if actor_uid is not None and event.actor_uid != actor_uid:
                continue
            if result is not None and event.result != result:
                continue
            filtered.append(event.to_dict())

        # 按时间倒序
        filtered.sort(key=lambda x: x["timestamp"], reverse=True)
        return filtered[offset:offset + limit]

    def count(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        event_type: Optional[AuditEventType] = None,
        actor_uid: Optional[int] = None,
        result: Optional[AuditResult] = None,
    ) -> int:
        """统计审计日志条数。"""
        if not self._db_path:
            events = self._query_from_buffer(
                start_time, end_time, event_type, actor_uid, result,
                limit=10**9, offset=0
            )
            return len(events)

        conditions = []
        params = []

        if start_time is not None:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time is not None:
            conditions.append("timestamp < ?")
            params.append(end_time)
        if event_type is not None:
            conditions.append("event_type = ?")
            params.append(event_type.value)
        if actor_uid is not None:
            conditions.append("actor_uid = ?")
            params.append(actor_uid)
        if result is not None:
            conditions.append("result = ?")
            params.append(result.value)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT COUNT(*) as count FROM audit_log WHERE {where_clause}"

        conn = self._get_conn()
        try:
            cursor = conn.execute(sql, params)
            return cursor.fetchone()["count"]
        finally:
            conn.close()

    # ----- 内部方法 -----

    def _flush_to_db(self) -> None:
        """将缓冲区批量写入 DB。"""
        if not self._db_path or not self._buffer:
            return

        events = self._buffer[:]
        self._buffer.clear()

        conn = self._get_conn()
        try:
            for event in events:
                conn.execute(
                    """INSERT OR REPLACE INTO audit_log
                       (event_id, timestamp, event_type, actor_uid, actor_role,
                        action, target, result, details, client_ip)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event.event_id, event.timestamp, event.event_type.value,
                     event.actor_uid, event.actor_role, event.action,
                     event.target, event.result.value,
                     json.dumps(event.details), event.client_ip)
                )
            conn.commit()
        finally:
            conn.close()

    def _write_single(self, event: AuditEvent) -> None:
        """写入单个事件到 DB。"""
        if not self._db_path:
            return

        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO audit_log
                   (event_id, timestamp, event_type, actor_uid, actor_role,
                    action, target, result, details, client_ip)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event.event_id, event.timestamp, event.event_type.value,
                 event.actor_uid, event.actor_role, event.action,
                 event.target, event.result.value,
                 json.dumps(event.details), event.client_ip)
            )
            conn.commit()
        finally:
            conn.close()

    def flush(self) -> None:
        """手动刷新缓冲区到 DB。"""
        with self._lock:
            self._flush_to_db()

    def clear(self) -> None:
        """清空所有审计日志（仅用于测试）。"""
        with self._lock:
            self._buffer.clear()
        if self._db_path:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM audit_log")
                conn.commit()
            finally:
                conn.close()

    def get_stats(self) -> Dict[str, Any]:
        """获取审计日志统计信息。"""
        if not self._db_path:
            with self._lock:
                buffer_count = len(self._buffer)
            return {
                "total": buffer_count,
                "by_type": {},
                "by_result": {},
                "buffer_size": buffer_count,
            }

        conn = self._get_conn()
        try:
            # 总数
            total = conn.execute("SELECT COUNT(*) as count FROM audit_log").fetchone()["count"]

            # 按类型统计
            cursor = conn.execute(
                "SELECT event_type, COUNT(*) as count FROM audit_log GROUP BY event_type"
            )
            by_type = {row["event_type"]: row["count"] for row in cursor.fetchall()}

            # 按结果统计
            cursor = conn.execute(
                "SELECT result, COUNT(*) as count FROM audit_log GROUP BY result"
            )
            by_result = {row["result"]: row["count"] for row in cursor.fetchall()}

            return {
                "total": total,
                "by_type": by_type,
                "by_result": by_result,
                "buffer_size": len(self._buffer),
            }
        finally:
            conn.close()


# ============================================================
# 全局单例
# ============================================================


_global_logger: Optional[AuditLogger] = None
_global_lock = threading.Lock()


def get_audit_logger(db_path: str = "") -> AuditLogger:
    """获取全局 AuditLogger 单例。"""
    global _global_logger
    if _global_logger is None:
        with _global_lock:
            if _global_logger is None:
                _global_logger = AuditLogger(db_path=db_path)
    return _global_logger


def reset_audit_logger() -> None:
    """重置全局 AuditLogger（仅用于测试）。"""
    global _global_logger
    with _global_lock:
        _global_logger = None
