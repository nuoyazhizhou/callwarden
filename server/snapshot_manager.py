"""Phase 4.3: Python 侧 Snapshot Manager —— 协调 Rust PySnapshotCache 与 workspace registry。

职责：
- 管理 workspace_instance_id → PySnapshotManager 的映射
- build_and_publish：调用 Rust 侧构建 GraphSnapshot 并原子发布
- query：通过 GraphStore 的 get_callers/get_callees/search_symbols 查询当前 snapshot
- 兼容 local 模式：daemon 不可用时回退到 Python SQL 查询

设计要点：
- Python 侧不直接操作 SQLite 共享库（避免与 daemon 写入撞锁）
- 通过 Rust PySnapshotManager 持有 Arc<GraphSnapshot>，发布时原子替换
- 多 workspace 通过 PySnapshotCache 统一管理
- 兼容层：local 模式仍用 CodeGraphDB._get_graph_store() 查询本地 DB

演进路径（GraphStore → SnapshotManager）：
- 旧路径：CodeGraphDB._get_graph_store() → GraphStore.load_from_sqlite() → get_callers
- 新路径：SnapshotManagerService.publish_snapshot() → PySnapshotManager.build_and_publish()
         → SnapshotManagerService.query_callers()（通过 cached GraphStore）
- 查询时 GraphStore 从 ArcSwap 保护的 snapshot 读取，发布时不阻塞

Phase 4.6: 加入 QueryBudget 控制 depth/limit/timeout/frontier
"""

import logging
import os
import threading
from typing import Optional, List, Dict, Any

from callwarden.server.query_budget import QueryBudget, default_budget

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Rust 扩展可用性检测
# ----------------------------------------------------------------------

def _try_import_rust_snapshot() -> Optional[Any]:
    """尝试导入 Rust 侧 PySnapshotCache/PySnapshotManager/GraphStore，不可用时返回 None。"""
    try:
        from callwarden_core import PySnapshotCache, PySnapshotManager, GraphStore
        return PySnapshotCache, PySnapshotManager, GraphStore
    except ImportError:
        return None


_RUST_AVAILABLE = _try_import_rust_snapshot()


# ----------------------------------------------------------------------
# SnapshotManagerService — 单例服务
# ----------------------------------------------------------------------

class SnapshotManagerService:
    """Python 侧 Snapshot Manager 服务。

    线程安全：通过 threading.Lock 保护 _cache 字段。
    Rust 侧 PySnapshotCache 内部通过 parking_lot::RwLock 保护。

    用法：
        svc = SnapshotManagerService.get_instance()
        svc.publish_snapshot("ws_abc", db_path="/path/to/callwarden.db")
        callers = svc.query_callers("ws_abc", "function_name")
    """

    _instance: Optional["SnapshotManagerService"] = None
    _instance_lock = threading.Lock()

    def __init__(self, max_workspaces: int = 32):
        """初始化 SnapshotManagerService。

        Args:
            max_workspaces: 最多同时缓存的 workspace snapshot 数量
        """
        self._max_workspaces = max_workspaces
        self._cache = None
        self._lock = threading.Lock()
        # workspace_id → GraphStore 实例（用于查询，与 Rust 侧 ArcSwap 配合）
        # 注：当前为过渡方案，长期应通过 GraphSnapshot.store 暴露查询方法
        self._rust_stores: Dict[str, Any] = {}
        if _RUST_AVAILABLE is not None:
            PySnapshotCache, _, _ = _RUST_AVAILABLE
            self._cache = PySnapshotCache(max_workspaces)
            logger.info("SnapshotManagerService 初始化（Rust 后端可用，max=%d）", max_workspaces)
        else:
            logger.warning("SnapshotManagerService 初始化（Rust 后端不可用，仅 local 模式）")

    @classmethod
    def get_instance(cls) -> "SnapshotManagerService":
        """获取单例。"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（测试用）。"""
        with cls._instance_lock:
            cls._instance = None

    @property
    def rust_available(self) -> bool:
        """Rust 后端是否可用。"""
        return self._cache is not None

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    def publish_snapshot(
        self,
        workspace_instance_id: str,
        db_path: str,
        build_context_hash: str = "",
        snapshot_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """为指定 workspace 构建 snapshot 并原子发布。

        同时创建一个 GraphStore 实例缓存到 _rust_stores，供后续查询使用。

        Args:
            workspace_instance_id: workspace 实例 ID
            db_path: SQLite 数据库路径（callwarden.db）
            build_context_hash: build context 哈希
            snapshot_id: 共享 snapshot ID（clean workspace 可共享）

        Returns:
            发布结果 dict（含 generation/symbol_count/call_count），Rust 不可用时返回 None
        """
        if self._cache is None:
            logger.debug("Rust 后端不可用，跳过 publish_snapshot（ws=%s）", workspace_instance_id)
            return None

        with self._lock:
            # 1. 通过 PySnapshotManager 构建 snapshot 并原子发布
            mgr = self._cache.get_or_create(workspace_instance_id)
            gen, syms, calls = mgr.build_and_publish(db_path, build_context_hash, snapshot_id)

            # 2. 同时创建 GraphStore 用于查询（过渡方案）
            # 长期方案：从 GraphSnapshot.store 暴露查询方法，避免双份内存
            if _RUST_AVAILABLE is not None:
                _, _, GraphStore = _RUST_AVAILABLE
                store = GraphStore()
                store.load_from_sqlite(db_path)
                self._rust_stores[workspace_instance_id] = store

            logger.info(
                "snapshot 发布成功 ws=%s gen=%d syms=%d calls=%d",
                workspace_instance_id, gen, syms, calls
            )
            return {
                "workspace_instance_id": workspace_instance_id,
                "generation": gen,
                "symbol_count": syms,
                "call_count": calls,
                "snapshot_id": snapshot_id,
                "build_context_hash": build_context_hash,
            }

    # ------------------------------------------------------------------
    # 查询统计
    # ------------------------------------------------------------------

    def get_snapshot_stats(self, workspace_instance_id: str) -> Optional[Dict[str, Any]]:
        """获取指定 workspace 的 snapshot 统计信息。"""
        if self._cache is None:
            return None
        mgr = self._cache.get(workspace_instance_id)
        if mgr is None:
            return None
        return mgr.snapshot_stats()

    def get_current_generation(self, workspace_instance_id: str) -> int:
        """获取当前 generation（不存在返回 0）。"""
        if self._cache is None:
            return 0
        mgr = self._cache.get(workspace_instance_id)
        if mgr is None:
            return 0
        return mgr.current_generation()

    def list_workspaces(self) -> List[str]:
        """列出所有已发布 snapshot 的 workspace。"""
        if self._cache is None:
            return []
        return list(self._cache.list_workspaces())

    def evict_workspace(self, workspace_instance_id: str) -> bool:
        """从缓存中移除指定 workspace。"""
        if self._cache is None:
            return False
        with self._lock:
            self._rust_stores.pop(workspace_instance_id, None)
            return self._cache.evict(workspace_instance_id)

    # ------------------------------------------------------------------
    # 查询代理：通过 Rust GraphStore 查询（ArcSwap 读路径无锁）
    # ------------------------------------------------------------------

    def _get_rust_graph_store(self, workspace_instance_id: str) -> Optional[Any]:
        """获取 workspace 对应的 Rust GraphStore 实例。"""
        return self._rust_stores.get(workspace_instance_id)

    def query_callers(
        self,
        workspace_instance_id: str,
        callee_name: str,
        qualified_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询指定 workspace 中谁调用了 callee_name。"""
        store = self._get_rust_graph_store(workspace_instance_id)
        if store is None:
            return []
        return store.get_callers(callee_name, qualified_name)

    def query_callees(
        self,
        workspace_instance_id: str,
        caller_name: str,
        qualified_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询指定 workspace 中 caller_name 调用了哪些函数。"""
        store = self._get_rust_graph_store(workspace_instance_id)
        if store is None:
            return []
        return store.get_callees(caller_name, qualified_name)

    def search_symbols(
        self,
        workspace_instance_id: str,
        query: str,
        kind: Optional[str] = None,
        limit: Optional[int] = None,
        budget: Optional[QueryBudget] = None,
    ) -> List[Dict[str, Any]]:
        """在指定 workspace 中搜索符号。

        Args:
            workspace_instance_id: workspace 实例 ID
            query: 搜索关键词
            kind: 符号类型过滤（可选）
            limit: 最大返回数（若为 None，使用 budget.max_results 或默认 50）
            budget: 查询预算
        """
        store = self._get_rust_graph_store(workspace_instance_id)
        if store is None:
            return []
        b = budget or default_budget()
        if limit is None:
            limit = b.max_results
        return store.search_symbols(query, kind, limit)

    def query_symbol(
        self,
        workspace_instance_id: str,
        qualified_name: str,
    ) -> Optional[Dict[str, Any]]:
        """按 qualified_name 精确查询符号。"""
        store = self._get_rust_graph_store(workspace_instance_id)
        if store is None:
            return None
        return store.get_symbol(qualified_name)

    def query_call_chain_down(
        self,
        workspace_instance_id: str,
        qualified_name: str,
        max_depth: Optional[int] = None,
        budget: Optional[QueryBudget] = None,
    ) -> List[Dict[str, Any]]:
        """查询下游调用链（BFS）。

        Args:
            workspace_instance_id: workspace 实例 ID
            qualified_name: 起始符号的限定名
            max_depth: 最大深度（若为 None，使用 budget.max_depth 或默认 5）
            budget: 查询预算（若为 None，使用默认预算）

        Returns:
            调用链结果列表
        """
        store = self._get_rust_graph_store(workspace_instance_id)
        if store is None:
            return []
        b = budget or default_budget()
        if max_depth is None:
            max_depth = b.max_depth
        b.start()
        result = store.get_call_chain_down(qualified_name, max_depth)
        return b.truncate_results(result)

    def query_topological_order(
        self,
        workspace_instance_id: str,
    ) -> List[str]:
        """获取 workspace 的拓扑序。"""
        store = self._get_rust_graph_store(workspace_instance_id)
        if store is None:
            return []
        return store.get_topological_order()

    def query_detect_cycles(
        self,
        workspace_instance_id: str,
    ) -> List[List[str]]:
        """检测 workspace 中的循环依赖。"""
        store = self._get_rust_graph_store(workspace_instance_id)
        if store is None:
            return []
        return store.detect_cycles()

    def query_stats(
        self,
        workspace_instance_id: str,
    ) -> Optional[Dict[str, Any]]:
        """获取 workspace 的统计信息。"""
        store = self._get_rust_graph_store(workspace_instance_id)
        if store is None:
            return None
        return store.stats()

    def ensure_workspace(self, workspace_instance_id: str) -> bool:
        """检查 workspace 是否已发布 snapshot。"""
        return self._cache is not None and self._cache.get(workspace_instance_id) is not None


# ----------------------------------------------------------------------
# 便捷函数
# ----------------------------------------------------------------------

def get_snapshot_service() -> SnapshotManagerService:
    """获取 SnapshotManagerService 单例。"""
    return SnapshotManagerService.get_instance()


def publish_workspace_snapshot(
    workspace_instance_id: str,
    db_path: str,
    build_context_hash: str = "",
    snapshot_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """便捷函数：发布 workspace snapshot。"""
    return get_snapshot_service().publish_snapshot(
        workspace_instance_id, db_path, build_context_hash, snapshot_id
    )


def get_workspace_snapshot_stats(workspace_instance_id: str) -> Optional[Dict[str, Any]]:
    """便捷函数：获取 workspace snapshot 统计。"""
    return get_snapshot_service().get_snapshot_stats(workspace_instance_id)
