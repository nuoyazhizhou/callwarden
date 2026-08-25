"""Phase 8.4: Daemon 审计日志（SRV-002 后：纯 daemon RPC 薄客户端）。

设计参考：
- docs/design/enterprise-daemon-shared-snapshot-plan.md §Phase 8（audit log）
- docs/design/daemon-ipc-security.md §5（admin 操作应写审计日志）
- 验收：权限测试覆盖越权路径，admin 操作应写审计日志

SRV-002 收敛（server audit log Python authority → Rust daemon）：
- SQLite 权威（连接/建表/读写）全部下沉到 daemon（`audit_log_handlers.rs`，
  `mcp.audit_log.{get_conn,init_db,append,query,count,clear,get_stats}`）。
- 本模块为**纯薄客户端**：不再 import `sqlite3`、不再持有缓冲区、不再打开本地 DB。
- fail-closed：`_call_daemon_rpc` 在 daemon 不可用时抛 `DaemonUnavailableError`，
  绝不回退本地 SQLite/内存缓冲充当业务存储。

提供：
1. AuditLogger：审计日志记录器（薄客户端）
   - 记录 admin 操作（workspace 注册/归档、token 生成/撤销、job 取消等）
   - 记录权限拒绝事件（越权查询、symlink 逃逸、TCP token 错误等）
   - 记录配置变更
2. AuditEvent：审计事件数据结构
3. 日志存储：daemon 拥有的 audit.db（SQLite，由 daemon 端 `audit_log_handlers` 写入）
4. 日志查询：按时间、UID、操作类型、结果过滤（经 daemon RPC）
"""

from __future__ import annotations

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
        self.timestamp = timestamp if timestamp is not None else _now()
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
        """生成事件 ID：A-<13ts>-<8hex>。

        后缀 8 位 hex（32 bit）而非 4 位 hex：降低快速循环内碰撞概率（生日悖论）。
        """
        import secrets
        import time

        ts = int(time.time() * 1000)
        hex_part = secrets.token_hex(4)
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
        import json

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


def _now() -> float:
    import time

    return time.time()


# ============================================================
# AuditLogger（纯 daemon RPC 薄客户端）
# ============================================================


class AuditLogger:
    """审计日志记录器（daemon RPC 薄客户端）。

    SRV-002 后：SQLite 权威已下沉到 daemon。本类不再打开本地 SQLite、不再维护内存缓冲，
    所有记录/查询均经 `_call_daemon_rpc` 发往 daemon 的 `mcp.audit_log.*` 方法。

    用法：
        logger = AuditLogger()
        logger.log(AuditEvent(
            event_type=AuditEventType.WORKSPACE_REGISTER,
            actor_uid=1000, actor_role="admin", action="register",
            target="workspace:ws-abc123",
        ))
        events = logger.query(actor_uid=1000, limit=100)
    """

    def __init__(self, db_path: str = "", buffer_size: int = 1000):
        """初始化审计日志薄客户端。

        Args:
            db_path: 兼容性保留字段（仅作归属标签），不再用于打开本地 SQLite。
            buffer_size: 兼容性保留字段（薄客户端无本地缓冲，忽略）。
        """
        self._db_path = db_path
        self._buffer_size = buffer_size
        self._initialized = False
        self._lock = threading.Lock()

    def _ensure_init(self) -> None:
        """确保 daemon 端 audit.db schema 就绪（懒初始化，每实例一次）。

        fail-closed：daemon 不可用时由 `_call_daemon_rpc` 抛 `DaemonUnavailableError`。
        """
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            _call_daemon_rpc("mcp.audit_log.init_db", {})
            self._initialized = True

    # ----- 记录日志 -----

    def log(self, event: AuditEvent) -> None:
        """记录审计事件（即时经 daemon RPC 写入 audit.db）。"""
        self._ensure_init()
        _call_daemon_rpc("mcp.audit_log.append", {"event": event.to_dict()})

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
        """记录管理员操作。"""
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
        """记录权限拒绝事件。"""
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
        """记录 workspace 操作。"""
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
        """记录 token 操作。"""
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

    # ----- 查询日志（经 daemon RPC）-----

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
        """查询审计日志（daemon 端过滤，倒序）。"""
        self._ensure_init()
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if start_time is not None:
            params["start_time"] = start_time
        if end_time is not None:
            params["end_time"] = end_time
        if event_type is not None:
            params["event_type"] = event_type.value
        if actor_uid is not None:
            params["actor_uid"] = actor_uid
        if result is not None:
            params["result"] = result.value
        return _call_daemon_rpc("mcp.audit_log.query", params) or []

    def count(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        event_type: Optional[AuditEventType] = None,
        actor_uid: Optional[int] = None,
        result: Optional[AuditResult] = None,
    ) -> int:
        """统计审计日志条数。"""
        self._ensure_init()
        params: Dict[str, Any] = {}
        if start_time is not None:
            params["start_time"] = start_time
        if end_time is not None:
            params["end_time"] = end_time
        if event_type is not None:
            params["event_type"] = event_type.value
        if actor_uid is not None:
            params["actor_uid"] = actor_uid
        if result is not None:
            params["result"] = result.value
        return _call_daemon_rpc("mcp.audit_log.count", params).get("count", 0)

    def clear(self) -> None:
        """清空所有审计日志（仅测试/运维用）。"""
        self._ensure_init()
        _call_daemon_rpc("mcp.audit_log.clear", {})

    def get_stats(self) -> Dict[str, Any]:
        """获取审计日志统计信息。"""
        self._ensure_init()
        return _call_daemon_rpc("mcp.audit_log.get_stats", {}) or {
            "total": 0,
            "by_type": {},
            "by_result": {},
            "buffer_size": 0,
        }

    def flush(self) -> None:
        """兼容接口：薄客户端无本地缓冲，记录即写，无需 flush。"""
        self._ensure_init()


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


# ============================================================
# daemon RPC 薄客户端接入
# ============================================================


def _call_daemon_rpc(method: str, params: Dict[str, Any]) -> Any:
    """经 daemon 统一 fail-closed 客户端发起 RPC（不回退本地 SQLite）。"""
    from ._mcp_common import _call_daemon_rpc as _rpc

    return _rpc(method, params)
