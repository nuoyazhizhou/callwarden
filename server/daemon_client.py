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
from typing import Optional, List, Dict, Any

from callwarden.config import (
    DAEMON_SOCKET_PATH,
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
    """enterprise 模式要求 daemon，但 socket 不可用。"""


class UnixDaemonRpcClient:
    """每次请求建立一个 UDS 连接的轻量 RPC client。"""

    def __init__(self, socket_path: str = DAEMON_SOCKET_PATH,
                 timeout: float = 30.0,
                 max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES):
        self.socket_path = socket_path
        self.timeout = timeout
        self.max_message_bytes = max_message_bytes
        self._ids = itertools.count(1)

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not hasattr(socket, "AF_UNIX"):
            raise DaemonUnavailableError("当前平台不支持 Unix domain socket")
        request_id = next(self._ids)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.timeout)
                conn.connect(self.socket_path)
                send_message(conn, {
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }, self.max_message_bytes)
                response = recv_message(conn, self.max_message_bytes)
        except (OSError, socket.timeout) as exc:
            raise DaemonUnavailableError(
                f"无法连接 daemon socket {self.socket_path}: {exc}"
            ) from exc
        if response.get("id") != request_id:
            raise DaemonUnavailableError("daemon 响应 request id 不匹配")
        return parse_response(response)

    def call_with_fd(self, method: str, params: Dict[str, Any], fd: int) -> Any:
        """发送一个带只读 FD 的请求。"""
        if not hasattr(socket, "AF_UNIX"):
            raise DaemonUnavailableError("当前平台不支持 Unix domain socket")
        request_id = next(self._ids)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.timeout)
                conn.connect(self.socket_path)
                send_message_with_fds(conn, {
                    "id": request_id,
                    "method": method,
                    "params": params,
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
        fd = os.open(db_path, os.O_RDONLY)
        try:
            return self.call_with_fd("snapshot.publish", {
                "workspace_instance_id": workspace_instance_id,
                "build_context_hash": build_context_hash,
            }, fd)
        finally:
            os.close(fd)


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
        self._rpc = UnixDaemonRpcClient(socket_path or DAEMON_SOCKET_PATH)
        self._workspace_instance_id: Optional[str] = None
        self._remote_workspace_id: Optional[str] = None
        self._remote_snapshot_ready = False
        self._project_root: Optional[str] = None
        # 路由统计
        self._daemon_hits: int = 0
        self._sql_fallbacks: int = 0

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

    def is_daemon_ready(self) -> bool:
        """daemon snapshot 是否已就绪（已发布且 Rust 后端可用）。"""
        if get_daemon_mode() != "local" and os.path.exists(self._rpc.socket_path):
            try:
                self._rpc.call("ping")
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

    def _ensure_remote_snapshot(self, db_path: Optional[str]) -> Optional[str]:
        """在 auto/enterprise 模式注册 workspace 并发布 snapshot。"""
        mode = get_daemon_mode()
        if mode == "local":
            return None
        if not os.path.exists(self._rpc.socket_path):
            if mode == "enterprise":
                raise DaemonUnavailableError(
                    f"enterprise 模式要求 daemon: {self._rpc.socket_path}"
                )
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
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is None:
            return _NO_REMOTE
        request = dict(params)
        request["workspace_instance_id"] = workspace_id
        result = self._rpc.call(method, request)
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
