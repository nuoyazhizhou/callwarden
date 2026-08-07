"""Phase 4.8: Daemon Client —— MCP 查询工具的统一入口。

职责：
- 高频查询工具（get_callers/get_callees/search_symbols/get_symbol 等）通过 daemon client 查询
- daemon client 优先走 Rust GraphSnapshot（内存只读，无锁）
- snapshot 未发布时自动回退到 Python SQL 查询（兼容 local 模式）

设计参考：enterprise-daemon-shared-snapshot-plan.md §13.2 MCP 过渡策略

路由策略：
1. workspace 已发布 snapshot → 走 Rust GraphStore 查询（零 SQL，零磁盘 I/O）
2. snapshot 未发布 → 回退到 CodeGraphDB SQL 查询（兼容现有行为）
3. 查询结果记录路由来源（daemon/sql），用于监控和验证
"""

import hashlib
import itertools
import logging
import os
import socket
import sqlite3
import sys
from typing import Optional, List, Dict, Any

# 3.28: 自动唤起 + 互斥 + 降级分流接线
from callwarden.server.daemon_autostart import (
    ensure_daemon,
    get_default_endpoint,
    try_connect,
)
from callwarden.server.daemon_mutex import DaemonMutex
from callwarden.server.degraded_mode import (
    OperationClass,
    classify_operation,
    route_degraded,
)

from callwarden.config import (
    get_daemon_mode,
    is_daemon_required,
)
from callwarden.server.daemon_protocol import (
    DEFAULT_MAX_MESSAGE_BYTES,
    parse_response,
    recv_message,
    send_message,
    send_message_with_fds,
)
from callwarden.server.snapshot_manager import SnapshotManagerService, get_snapshot_service
from callwarden.server.query_budget import default_budget

logger = logging.getLogger(__name__)
_NO_REMOTE = object()


class DaemonUnavailableError(RuntimeError):
    """enterprise 模式要求 enterprise daemon，但 endpoint 不可用。"""


class UnixDaemonRpcClient:
    """每次请求建立一个 IPC 连接（UDS 或 Windows Named Pipe）的轻量 RPC client。"""

    def __init__(self, socket_path: Optional[str] = None,
                 timeout: float = 30.0,
                 max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES):
        self.socket_path = socket_path or os.environ.get("CW_DAEMON_ENDPOINT") or get_default_endpoint()
        self.timeout = timeout
        self.max_message_bytes = max_message_bytes
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------
    # Task 协同 RPC 便利包装
    # ------------------------------------------------------------------

    def task_create(self, title: str, description: str = "", steps: list = None, creator: str = "agent", parent_id: str = "", workspace_id: str = "") -> dict:
        params = {
            "title": title,
            "description": description,
            "steps": steps or [],
            "creator": creator,
            "parent_id": parent_id,
            "workspace_id": workspace_id,
        }
        return self.call("task.create", params)

    def task_claim(self, task_id: str, agent_session_id: str = "") -> dict:
        return self.call("task.claim", {"task_id": task_id, "agent_session_id": agent_session_id})

    def task_work_next(self, task_id: str) -> dict:
        return self.call("task.work_next", {"task_id": task_id})

    def task_report(self, task_id: str, summary: str = "", evidence_path: str = "", evidence_hash: str = "", agent_session_id: str = "") -> dict:
        return self.call("task.report", {
            "task_id": task_id,
            "summary": summary,
            "evidence_path": evidence_path,
            "evidence_hash": evidence_hash,
            "agent_session_id": agent_session_id,
        })

    def task_status(self, task_id: str) -> dict:
        return self.call("task.status", {"task_id": task_id})

    def task_events(self, task_id: str) -> dict:
        return self.call("task.events", {"task_id": task_id})

    def task_list(self, status: str = "", limit: int = 100, parent_id: str = "") -> dict:
        return self.call("task.list", {"status": status, "limit": limit, "parent_id": parent_id})

    def task_rollback(self, task_id: str, reason: str = "") -> dict:
        return self.call("task.rollback", {"task_id": task_id, "reason": reason})

    def task_reopen(self, task_id: str, reason: str = "", reviewer: str = "") -> dict:
        return self.call("task.reopen", {"task_id": task_id, "reason": reason, "reviewer": reviewer})

    def task_apply(self, task_id: str, reviewer: str = "") -> dict:
        return self.call("task.apply", {"task_id": task_id, "reviewer": reviewer})

    def task_close(self, task_id: str, reviewer: str = "") -> dict:
        return self.call("task.close", {"task_id": task_id, "reviewer": reviewer})

    def task_capture_diff(self, task_id: str, step_id: str = "", base: str = "HEAD") -> dict:
        return self.call("task.capture_diff", {"task_id": task_id, "step_id": step_id, "base": base})

    def close(self) -> None:
        """关闭客户端（单次连接模式无持久连接，为空操作）。"""
        pass

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        request_id = next(self._ids)
        conn = None
        try:
            conn = try_connect(self.socket_path)
            if conn is None:
                raise OSError("endpoint 不可连接")
            with conn:
                conn.settimeout(self.timeout)
                send_message(conn, {
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }, self.max_message_bytes)
                response = recv_message(conn, self.max_message_bytes)
        except (OSError, socket.timeout) as exc:
            raise DaemonUnavailableError(
                f"无法连接 daemon endpoint {self.socket_path}: {exc}"
            ) from exc
        if response.get("id") != request_id:
            raise DaemonUnavailableError("daemon 响应 request id 不匹配")
        return parse_response(response)

    def get_authoritative_clock(self) -> float:
        """获取 Daemon 权威时钟时间 (Authoritative_Clock, Req 14.11)"""
        try:
            res = self.call("ping")
            if isinstance(res, dict) and "timestamp" in res:
                return float(res["timestamp"])
        except Exception:
            pass
        return time.time()

    def _probe_connection(self, conn: object) -> bool:
        """在 autostart 的现有连接上完成一次协议级 ping。"""
        request_id = next(self._ids)
        # readiness 探针必须短于 autostart 窗口，不能继承查询的 30 秒超时。
        conn.settimeout(min(self.timeout, 1.0))
        send_message(conn, {
            "id": request_id,
            "method": "ping",
            "params": {},
        }, self.max_message_bytes)
        response = recv_message(conn, self.max_message_bytes)
        if response.get("id") != request_id:
            raise DaemonUnavailableError("daemon readiness 响应 request id 不匹配")
        parse_response(response)
        return True

    def probe(self) -> bool:
        """以短超时在独立连接上探测 daemon 协议是否就绪。"""
        conn = try_connect(self.socket_path)
        if conn is None:
            raise DaemonUnavailableError(
                f"无法连接 daemon endpoint {self.socket_path}"
            )
        try:
            return self._probe_connection(conn)
        except (OSError, socket.timeout) as exc:
            raise DaemonUnavailableError(
                f"daemon endpoint 未就绪 {self.socket_path}: {exc}"
            ) from exc
        finally:
            conn.close()

    def call_with_fd(self, method: str, params: Dict[str, Any], fd: int) -> Any:
        """发送一个带只读 FD 的请求。"""
        if sys.platform == "win32" or not hasattr(socket, "AF_UNIX"):
            raise DaemonUnavailableError("当前平台不支持 SCM_RIGHTS FD 传递")
        request_id = next(self._ids)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.timeout)
                conn.connect(self.socket_path)
                send_message_with_fds(conn, {
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }, [fd], self.max_message_bytes)
                response = recv_message(conn, self.max_message_bytes)
        except (OSError, socket.timeout) as exc:
            raise DaemonUnavailableError(
                f"无法连接 daemon socket {self.socket_path}: {exc}"
            ) from exc
        if response.get("id") != request_id:
            raise DaemonUnavailableError("daemon 响应 request id 不匹配")
        return parse_response(response)

    def publish_snapshot(self, workspace_instance_id: str, db_path: str,
                         build_context_hash: str = "") -> Any:
        """checkpoint 本地 DB，并以只读 FD 发布给 daemon。"""
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            busy, _wal_pages, _checkpointed = conn.execute(
                "PRAGMA wal_checkpoint(FULL)"
            ).fetchone()
            if busy:
                raise DaemonUnavailableError("SQLite WAL checkpoint 被活动 writer 阻塞")
        if sys.platform == "win32":
            return self.call("snapshot.publish", {
                "workspace_instance_id": workspace_instance_id,
                "build_context_hash": build_context_hash,
                "db_path": os.path.abspath(db_path),
            })
        fd = os.open(db_path, os.O_RDONLY)
        try:
            return self.call_with_fd("snapshot.publish", {
                "workspace_instance_id": workspace_instance_id,
                "build_context_hash": build_context_hash,
            }, fd)
        finally:
            os.close(fd)


DaemonRpcClient = UnixDaemonRpcClient


# ----------------------------------------------------------------------
# workspace_instance_id 推导
# ----------------------------------------------------------------------

def derive_workspace_instance_id(project_root: str) -> str:
    """从项目根路径推导 workspace_instance_id（跨进程标识符）。

    用项目根路径的 SHA-256 前 16 位作为 workspace_instance_id，确保同一项目
    在不同进程（CLI / MCP / daemon）中标识一致。
    注意：此 hash 仅用于 workspace 标识，不再用于数据库路径（数据库已改为用户级统一路径）。
    """
    abs_root = os.path.abspath(project_root)
    norm_root = abs_root.replace("\\", "/")
    return hashlib.sha256(norm_root.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------
# DaemonClient
# ----------------------------------------------------------------------

class DaemonClient:
    """MCP 查询工具的 daemon client。

    优先走 Rust GraphSnapshot（内存只读），回退到 Python SQL。

    用法：
        client = DaemonClient.get_instance()
        callers = client.get_callers("function_name", qualified_name="mod.fn")
    """

    _instance: Optional["DaemonClient"] = None

    def __init__(self, socket_path: Optional[str] = None):
        self._svc: SnapshotManagerService = get_snapshot_service()
        self._rpc = UnixDaemonRpcClient(socket_path or get_default_endpoint())
        self._workspace_instance_id: Optional[str] = None
        self._remote_workspace_id: Optional[str] = None
        self._remote_snapshot_ready = False
        self._project_root: Optional[str] = None
        # 路由统计
        self._daemon_hits: int = 0
        self._sql_fallbacks: int = 0
        # 3.28: Degraded_Mode 计数 [Req 14.33]
        self._degraded_count: int = 0

    @classmethod
    def get_instance(cls) -> "DaemonClient":
        """获取单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（测试用）。"""
        cls._instance = None

    def configure_workspace(self, project_root: str):
        """配置当前 workspace。

        Args:
            project_root: 项目根目录路径
        """
        self._project_root = project_root
        self._workspace_instance_id = derive_workspace_instance_id(project_root)
        self._remote_workspace_id = None
        self._remote_snapshot_ready = False
        logger.debug(
            "DaemonClient 配置 workspace: root=%s id=%s",
            project_root, self._workspace_instance_id,
        )

    @property
    def workspace_instance_id(self) -> Optional[str]:
        return self._workspace_instance_id

    @property
    def daemon_hits(self) -> int:
        """通过 daemon（Rust snapshot）查询的次数。"""
        return self._daemon_hits

    @property
    def sql_fallbacks(self) -> int:
        """回退到 SQL 查询的次数。"""
        return self._sql_fallbacks

    @property
    def degraded_count(self) -> int:
        """Degraded_Mode 下执行的操作次数 [Req 14.33]。"""
        return self._degraded_count

    def is_daemon_ready(self) -> bool:
        """daemon snapshot 是否已就绪（已发布且 Rust 后端可用）。"""
        if get_daemon_mode() != "local" and os.path.exists(self._rpc.socket_path):
            try:
                self._rpc.probe()
                return self._remote_snapshot_ready
            except Exception:
                if is_daemon_required():
                    raise
        if self._workspace_instance_id is None:
            return False
        return self._svc.ensure_workspace(self._workspace_instance_id)

    def rpc_call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """公开的底层 RPC 入口，供 CLI 管理命令使用。"""
        return self._rpc.call(method, params)

    def call_with_autostart(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """带自动唤起与降级分流的 RPC 调用 [Req 14.22–14.30, 14.33]。

        流程：
        1. 尝试 RPC 调用
        2. 连接失败 → 获取互斥 → 自动唤起 daemon → 退避重试
        3. 唤起成功 → 在新连接上继续原请求
        4. 唤起失败（窗口耗尽）→ 按 class(op) 分流：
           - read_only: 返回降级标记，由调用方走 SQL 回退
           - Index_Write: 返回降级标记，由调用方直连写入
           - Governance_Write: fail closed，抛出 DaemonUnavailableError

        Returns:
            {"result": ..., "degraded": False} 正常路径
            {"result": None, "degraded": True, "mode": "direct_read"/"direct_write",
             "op_class": ...} 降级路径（read_only/Index_Write）

        Raises:
            DaemonUnavailableError: Governance_Write 在 Degraded_Mode 下被拒绝
        """
        # 第一次尝试
        try:
            result = self._rpc.call(method, params)
            return {"result": result, "degraded": False}
        except DaemonUnavailableError:
            pass

        # 连接失败：尝试自动唤起（带互斥）[Req 14.22, 14.23]
        endpoint = self._rpc.socket_path
        mutex = DaemonMutex(endpoint)
        if mutex.try_acquire():
            try:
                conn = ensure_daemon(
                    endpoint, readiness_check=self._rpc._probe_connection
                )
                if conn is not None:
                    conn.close()  # ensure_daemon 返回的连接仅用于验证可达性
                    # daemon 已就绪，重试原请求
                    result = self._rpc.call(method, params)
                    return {"result": result, "degraded": False}
            finally:
                mutex.release()
        else:
            # 其他会话正在启动 daemon，等待窗口内退避重试
            conn = ensure_daemon(
                endpoint, readiness_check=self._rpc._probe_connection
            )
            if conn is not None:
                conn.close()
                result = self._rpc.call(method, params)
                return {"result": result, "degraded": False}

        # 唤起失败：进入 Degraded_Mode [Req 14.27–14.30]
        import sys
        platform = "windows" if sys.platform == "win32" else (
            "macos" if sys.platform == "darwin" else "linux"
        )
        decision = route_degraded(method, endpoint, platform)
        self._degraded_count += 1

        if decision.allowed:
            # read_only 或 Index_Write：返回降级标记
            logger.info(
                "Degraded_Mode: %s → %s (method=%s)",
                decision.op_class.value, decision.mode, method,
            )
            return {
                "result": None,
                "degraded": True,
                "mode": decision.mode,
                "op_class": decision.op_class.value,
            }
        else:
            # Governance_Write: fail closed [Req 14.30]
            reason = decision.reason
            raise DaemonUnavailableError(
                f"Degraded_Mode: Governance_Write 被拒 (method={method}, "
                f"code={reason.code if reason else 'unknown'}, "
                f"recovery={reason.recovery_guidance if reason else 'N/A'})"
            )

    def _ensure_daemon_endpoint(self) -> bool:
        """确认 daemon 可接受请求，auto 模式必要时自动唤起。

        仅检查 socket 路径会把陈旧 socket 当成可用 daemon，也会让 auto
        模式在 daemon 尚未启动时直接绕过共享 snapshot。这里用 ping 作为
        活性探针，并复用已有的有界 autostart 窗口。
        """
        mode = get_daemon_mode()
        if mode == "local":
            return False

        endpoint = self._rpc.socket_path
        try:
            self._rpc.probe()
            return True
        except Exception as exc:
            if mode == "enterprise":
                raise DaemonUnavailableError(
                    f"enterprise 模式要求 enterprise daemon，但 endpoint {endpoint} 不可用: {exc}"
                ) from exc

        conn = ensure_daemon(endpoint, readiness_check=self._rpc._probe_connection)
        if conn is None:
            return False
        conn.close()
        return True

    def _ensure_remote_snapshot(self, db_path: Optional[str]) -> Optional[str]:
        """在 auto/enterprise 模式注册 workspace 并发布 snapshot。"""
        mode = get_daemon_mode()
        if mode == "local":
            return None
        if not self._ensure_daemon_endpoint():
            return None
        try:
            if self._remote_workspace_id is None:
                root = self._project_root or os.getcwd()
                workspace = self._rpc.call("workspace.register", {
                    "client_view_root": root,
                })
                self._remote_workspace_id = workspace["workspace_instance_id"]
            if db_path and not self._remote_snapshot_ready:
                self._rpc.publish_snapshot(self._remote_workspace_id, db_path)
                self._remote_snapshot_ready = True
            return self._remote_workspace_id if self._remote_snapshot_ready else None
        except Exception:
            if mode == "enterprise":
                raise
            logger.warning("daemon UDS 请求失败，auto 模式回退 local", exc_info=True)
            return None

    def _remote_query(self, method: str, params: Dict[str, Any],
                      db_path: Optional[str]) -> Any:
        try:
            workspace_id = self._ensure_remote_snapshot(db_path)
        except DaemonUnavailableError:
            if get_daemon_mode() == "enterprise":
                raise
            return _NO_REMOTE
        if workspace_id is None:
            return _NO_REMOTE
        request = dict(params)
        request["workspace_instance_id"] = workspace_id
        try:
            result = self._rpc.call(method, request)
        except DaemonUnavailableError:
            self._remote_snapshot_ready = False
            if get_daemon_mode() == "enterprise":
                raise
            return _NO_REMOTE
        self._daemon_hits += 1
        return result

    # ------------------------------------------------------------------
    # 内部：确保 snapshot 已发布
    # ------------------------------------------------------------------

    def _ensure_snapshot(self, db_path: str) -> bool:
        """确保 workspace 的 snapshot 已发布。

        如果 snapshot 未发布且 Rust 后端可用，自动从 db_path 发布。
        """
        if self._workspace_instance_id is None:
            # 从 db_path 反推 workspace_instance_id
            parent_dir = os.path.basename(os.path.dirname(db_path))
            if len(parent_dir) == 16:
                self._workspace_instance_id = parent_dir

        if self._workspace_instance_id is None:
            return False

        if self._svc.ensure_workspace(self._workspace_instance_id):
            return True

        # 自动发布 snapshot
        if self._svc.rust_available and os.path.exists(db_path):
            try:
                result = self._svc.publish_snapshot(
                    self._workspace_instance_id, db_path
                )
                return result is not None
            except Exception as e:
                logger.warning("自动发布 snapshot 失败: %s", e)
                return False

        return False

    # ------------------------------------------------------------------
    # 查询接口（与 MCP 工具签名对齐）
    # ------------------------------------------------------------------

    def get_callers(
        self,
        callee_name: str,
        qualified_name: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询谁调用了指定函数。"""
        remote = self._remote_query("query.callers", {
            "callee_name": callee_name,
            "qualified_name": qualified_name,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.query_callers(
                self._workspace_instance_id, callee_name, qualified_name
            )
        self._sql_fallbacks += 1
        return self._sql_fallback_get_callers(callee_name, qualified_name)

    def get_callees(
        self,
        caller_name: str,
        qualified_name: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询指定函数调用了哪些函数。"""
        remote = self._remote_query("query.callees", {
            "caller_name": caller_name,
            "qualified_name": qualified_name,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.query_callees(
                self._workspace_instance_id, caller_name, qualified_name
            )
        self._sql_fallbacks += 1
        return self._sql_fallback_get_callees(caller_name, qualified_name)

    def search_symbols(
        self,
        query: str,
        kind: Optional[str] = None,
        limit: int = 20,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """搜索符号。"""
        remote = self._remote_query("query.search", {
            "query": query,
            "kind": kind,
            "limit": limit,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.search_symbols(
                self._workspace_instance_id, query, kind, limit
            )
        self._sql_fallbacks += 1
        return self._sql_fallback_search_symbols(query, kind, limit)

    def get_symbol(
        self,
        qualified_name: str,
        db_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """按 qualified_name 精确查询符号。"""
        remote = self._remote_query("query.symbol", {
            "qualified_name": qualified_name,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.query_symbol(
                self._workspace_instance_id, qualified_name
            )
        self._sql_fallbacks += 1
        return self._sql_fallback_get_symbol(qualified_name)

    def get_symbol_location(
        self,
        name: str,
        file_path: str = "",
        db_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """在指定文件中定位符号，优先走 enterprise snapshot RPC。"""
        remote = self._remote_query("query.symbol_location", {
            "name": name,
            "file_path": file_path,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        self._sql_fallbacks += 1
        return self._get_db().get_symbol_location(name, file_path=file_path or None)

    def get_file_symbols(
        self,
        file_path: str,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询文件符号，优先走 enterprise snapshot RPC。"""
        remote = self._remote_query("query.file", {"file_path": file_path}, db_path)
        if remote is not _NO_REMOTE:
            return remote
        self._sql_fallbacks += 1
        return self._get_db().get_file_symbols(file_path)

    def query_grep(
        self,
        patterns: List[str],
        fixed: bool = False,
        limit: int = 200,
        path: Optional[str] = None,
        include_all: bool = False,
        kind: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Any:
        """查询 enterprise workspace 文件；local fallback 由 grep CLI/MCP 负责。"""
        return self._remote_query("query.grep", {
            "patterns": patterns,
            "fixed": fixed,
            "limit": limit,
            "path": path,
            "include_all": include_all,
            "kind": kind,
        }, db_path)

    def get_symbol_issues(
        self,
        qualified_name: str,
        include_info: bool = False,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询符号问题，优先走 enterprise snapshot RPC。"""
        remote = self._remote_query("query.issues", {
            "qualified_name": qualified_name,
            "include_info": include_info,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        self._sql_fallbacks += 1
        return self._get_db().get_symbol_issues(qualified_name, include_info=include_info)

    def get_test_cases(
        self,
        qualified_name: str,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询正向测试关系，优先走 enterprise snapshot RPC。"""
        return self._query_tests_or_local(
            qualified_name, reverse=False, history=False, limit=50, db_path=db_path
        )

    def get_tested_functions(
        self,
        test_qualified_name: str,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """反向查询测试覆盖函数，优先走 enterprise snapshot RPC。"""
        return self._query_tests_or_local(
            test_qualified_name, reverse=True, history=False, limit=50, db_path=db_path
        )

    def get_test_stability(
        self,
        qualified_name: str,
        limit: int = 50,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """查询测试稳定性历史，优先走 enterprise snapshot RPC。"""
        return self._query_tests_or_local(
            qualified_name, reverse=False, history=True, limit=limit, db_path=db_path
        )

    def _query_tests_or_local(
        self,
        qualified_name: str,
        reverse: bool,
        history: bool,
        limit: int,
        db_path: Optional[str],
    ) -> Any:
        remote = self._remote_query("query.tests", {
            "qualified_name": qualified_name,
            "reverse": reverse,
            "history": history,
            "limit": limit,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        self._sql_fallbacks += 1
        db = self._get_db()
        if history:
            return db.get_test_stability(qualified_name, limit=limit)
        if reverse:
            return db.get_tested_functions(qualified_name)
        return db.get_test_cases(qualified_name)

    def get_stats(self, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """获取统计信息。"""
        remote = self._remote_query("query.stats", {}, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.query_stats(self._workspace_instance_id)
        self._sql_fallbacks += 1
        return self._sql_fallback_get_stats()

    def get_topological_order(
        self, limit: int = 50, db_path: Optional[str] = None,
    ) -> List[str]:
        """获取拓扑排序。"""
        # J8 协议闭合：优先走 RPC（query.topological_order），Rust daemon 端已实现
        remote = self._remote_query("query.topological_order", {"limit": limit}, db_path)
        if remote is not _NO_REMOTE:
            return remote if isinstance(remote, list) else []
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            result = self._svc.query_topological_order(self._workspace_instance_id)
            return result[:limit] if limit > 0 else result
        self._sql_fallbacks += 1
        return self._sql_fallback_get_topological_order(limit)

    def get_call_chain_down(
        self,
        qualified_name: str,
        max_depth: int = 10,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """向下调用链（BFS）。"""
        # J8 协议闭合：优先走 RPC（query.call_chain_down），Rust daemon 端已实现
        remote = self._remote_query("query.call_chain_down", {
            "qualified_name": qualified_name,
            "max_depth": max_depth,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote if isinstance(remote, list) else []
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            budget = default_budget()
            budget.max_depth = min(max_depth, budget.max_depth)
            return self._svc.query_call_chain_down(
                self._workspace_instance_id, qualified_name, max_depth, budget
            )
        self._sql_fallbacks += 1
        return self._sql_fallback_get_call_chain_down(qualified_name, max_depth)

    def detect_cycles(
        self, max_depth: int = 10, db_path: Optional[str] = None,
    ) -> List[List[str]]:
        """检测循环依赖。"""
        # J8 协议闭合：优先走 RPC（query.detect_cycles），Rust daemon 端已实现
        remote = self._remote_query("query.detect_cycles", {"max_depth": max_depth}, db_path)
        if remote is not _NO_REMOTE:
            return remote if isinstance(remote, list) else []
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.query_detect_cycles(self._workspace_instance_id)
        self._sql_fallbacks += 1
        return self._sql_fallback_detect_cycles(max_depth)

    def diff_symbol(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        qualified_name: str,
    ) -> Optional[Dict[str, Any]]:
        """对比两个 workspace 中同一符号的差异。"""
        if not self._svc.rust_available:
            return None
        cache = self._svc._cache
        if cache is None:
            return None
        return cache.diff_symbol(left_workspace_id, right_workspace_id, qualified_name)

    def diff_signature(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        qualified_name: str,
    ) -> Optional[Dict[str, Any]]:
        """对比两个 workspace 中同一符号的签名差异。"""
        if not self._svc.rust_available:
            return None
        cache = self._svc._cache
        if cache is None:
            return None
        return cache.diff_signature(left_workspace_id, right_workspace_id, qualified_name)

    def diff_callers(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        qualified_name: str,
    ) -> Optional[Dict[str, Any]]:
        """对比两个 workspace 中同一符号的 caller 边集合（基于 resolved edge delta）。"""
        if not self._svc.rust_available:
            return None
        cache = self._svc._cache
        if cache is None:
            return None
        return cache.diff_callers(left_workspace_id, right_workspace_id, qualified_name)

    def diff_callees(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        qualified_name: str,
    ) -> Optional[Dict[str, Any]]:
        """对比两个 workspace 中同一符号的 callee 边集合（基于 resolved edge delta）。"""
        if not self._svc.rust_available:
            return None
        cache = self._svc._cache
        if cache is None:
            return None
        return cache.diff_callees(left_workspace_id, right_workspace_id, qualified_name)

    def compare_snapshots(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        scope_type: str = "repo",
        scope_value: str = "",
    ) -> Optional[Dict[str, Any]]:
        """对比两个 workspace 中指定 scope 内的所有符号差异（同步查询）。

        同步路径：小 scope（file/module）直接返回结果。
        仓库级 scope 应先调用 count_symbols_in_scope 检查大小，
        超阈值时改用 start_snapshot_diff 转后台 job。

        Args:
            left_workspace_id: 左 workspace ID
            right_workspace_id: 右 workspace ID
            scope_type: "file" / "module" / "repo"
            scope_value: 文件路径或模块路径（repo 时忽略）

        Returns:
            {"changes": [...], "scope_type": str, "scope_value": str, "count": int}
            Rust 不可用时返回 None
        """
        if not self._svc.rust_available:
            return None
        cache = self._svc._cache
        if cache is None:
            return None
        changes = cache.compare_snapshots(
            left_workspace_id, right_workspace_id, scope_type, scope_value
        )
        return {
            "changes": changes,
            "scope_type": scope_type,
            "scope_value": scope_value,
            "count": len(changes),
        }

    def count_symbols_in_scope(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        scope_type: str = "repo",
        scope_value: str = "",
    ) -> int:
        """统计两个 workspace 中匹配 scope 的符号数量（并集）。

        用于判断 compare_snapshots 是否应走同步路径还是转后台 job。

        Returns:
            符号数量（并集），Rust 不可用时返回 0
        """
        if not self._svc.rust_available:
            return 0
        cache = self._svc._cache
        if cache is None:
            return 0
        return cache.count_symbols_in_scope(
            left_workspace_id, right_workspace_id, scope_type, scope_value
        )

    def start_snapshot_diff(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        scope_type: str = "repo",
        scope_value: str = "",
    ) -> Optional[str]:
        """启动仓库级 snapshot diff 后台 job。

        设计参考：enterprise-daemon-shared-snapshot-plan.md §12.4 start_snapshot_diff

        Args:
            left_workspace_id: 左 workspace ID
            right_workspace_id: 右 workspace ID
            scope_type: "file" / "module" / "repo"
            scope_value: 文件路径或模块路径（repo 时忽略）

        Returns:
            job_id 字符串，Rust 不可用时返回 None
        """
        if not self._svc.rust_available:
            return None
        # 延迟导入避免循环依赖
        from callwarden.config import get_project_db_path
        from callwarden.server.job_executor_singleton import get_job_executor
        db_path = get_project_db_path(self._project_root or ".")
        executor = get_job_executor(db_path)
        params = {
            "left_workspace_id": left_workspace_id,
            "right_workspace_id": right_workspace_id,
            "scope_type": scope_type,
            "scope_value": scope_value,
        }
        job = executor.submit("snapshot_diff", params)
        return job.job_id

    # ------------------------------------------------------------------
    # SQL 回退（兼容 local 模式，daemon 不可用时走 Python SQL）
    # ------------------------------------------------------------------

    def _get_db(self):
        """获取 CodeGraphDB 实例（延迟导入避免循环依赖）。"""
        from callwarden.server.mcp_server import get_db
        return get_db()

    def _sql_fallback_get_callers(self, callee_name, qualified_name=None):
        db = self._get_db()
        return db.get_callers(callee_name, qualified_name)

    def _sql_fallback_get_callees(self, caller_name, qualified_name=None):
        db = self._get_db()
        return db.get_callees(caller_name, qualified_name)

    def _sql_fallback_search_symbols(self, query, kind=None, limit=20):
        db = self._get_db()
        return db.search_symbols(query, kind=kind, limit=limit)

    def _sql_fallback_get_symbol(self, qualified_name):
        db = self._get_db()
        return db.get_symbol(qualified_name)

    def _sql_fallback_get_stats(self):
        db = self._get_db()
        return db.get_stats()

    def _sql_fallback_get_topological_order(self, limit=50):
        db = self._get_db()
        return db.get_topological_order(limit=limit)

    def _sql_fallback_get_call_chain_down(self, qualified_name, max_depth=10):
        db = self._get_db()
        result = db.get_call_chain_down(qualified_name, max_depth=max_depth)
        # Python 侧返回 dict，统一为 list
        if isinstance(result, dict):
            return result.get("chain", result.get("edges", []))
        return result

    def _sql_fallback_detect_cycles(self, max_depth=10):
        db = self._get_db()
        return db.detect_cycles(max_depth=max_depth)

    # ------------------------------------------------------------------
    # 路由统计
    # ------------------------------------------------------------------

    def get_routing_stats(self) -> Dict[str, Any]:
        """获取路由统计信息。"""
        total = self._daemon_hits + self._sql_fallbacks
        daemon_ratio = (self._daemon_hits / total * 100) if total > 0 else 0
        return {
            "daemon_hits": self._daemon_hits,
            "sql_fallbacks": self._sql_fallbacks,
            "total_queries": total,
            "daemon_ratio_percent": round(daemon_ratio, 2),
            "workspace_instance_id": self._workspace_instance_id,
            "daemon_ready": self.is_daemon_ready(),
        }


# ----------------------------------------------------------------------
# 便捷函数
# ----------------------------------------------------------------------

def get_daemon_client() -> DaemonClient:
    """获取 DaemonClient 单例。"""
    return DaemonClient.get_instance()


# ----------------------------------------------------------------------
# 统一 Task 写/读 路由规则函数
# ----------------------------------------------------------------------

def route_task_write(rpc_method: str, params: dict, fallback_func):
    """统一任务写操作路由规则：
    1. local 模式 -> 直接执行 fallback_func（本地 SQLite）
    2. enterprise / auto 模式 -> 通过 daemon RPC 执行
    3. enterprise / auto 模式下若 daemon 不可用，禁止 fallback 本地 SQLite，抛出 DaemonUnavailableError (fail-closed)
    """
    mode = get_daemon_mode()
    if mode == "local":
        return fallback_func()

    if isinstance(params, dict) and "request_id" not in params:
        import uuid
        params["request_id"] = f"req-{uuid.uuid4().hex[:12]}"

    rpc_client = UnixDaemonRpcClient()
    try:
        return rpc_client.call(rpc_method, params)
    except Exception as exc:
        if is_daemon_required() or mode == "auto":
            raise DaemonUnavailableError(f"enterprise/auto 模式下任务写操作 daemon 连接失败: {exc}") from exc
        raise


def route_task_read(rpc_method: str, params: dict, fallback_func):
    """统一任务读操作路由规则：
    1. local 模式 -> 直接执行 fallback_func
    2. enterprise 模式 -> 走 daemon RPC，不可用时 fail-closed
    3. auto 模式 -> 优先走 daemon RPC，不可用时降级执行 fallback_func
    """
    mode = get_daemon_mode()
    if mode == "local":
        return fallback_func()

    rpc_client = UnixDaemonRpcClient()
    try:
        return rpc_client.call(rpc_method, params)
    except Exception as exc:
        if is_daemon_required():
            raise DaemonUnavailableError(f"enterprise 模式下任务读操作 daemon 连接失败: {exc}") from exc
        return fallback_func()
