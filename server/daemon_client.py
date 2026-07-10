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
import logging
import os
from typing import Optional, List, Dict, Any

from callwarden.server.snapshot_manager import SnapshotManagerService, get_snapshot_service
from callwarden.server.query_budget import default_budget

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# workspace_instance_id 推导
# ----------------------------------------------------------------------

def derive_workspace_instance_id(project_root: str) -> str:
    """从项目根路径推导 workspace_instance_id。

    与 config.get_project_db_path 使用相同的 SHA-256 前 16 位哈希，
    确保同一项目的 workspace_instance_id 一致。
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

    def __init__(self):
        self._svc: SnapshotManagerService = get_snapshot_service()
        self._workspace_instance_id: Optional[str] = None
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
        if self._workspace_instance_id is None:
            return False
        return self._svc.ensure_workspace(self._workspace_instance_id)

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
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.query_symbol(
                self._workspace_instance_id, qualified_name
            )
        self._sql_fallbacks += 1
        return self._sql_fallback_get_symbol(qualified_name)

    def get_stats(self, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """获取统计信息。"""
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
