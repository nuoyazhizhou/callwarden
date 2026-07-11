"""
Phase 7.0: Job Handlers

设计参考：enterprise-daemon-shared-snapshot-plan.md §Phase 7

注册到 JobExecutor 的 handler 集合。每个 handler 接收
JobContext 并返回 result_summary dict。

当前已注册的 handler：
- clone_detect：把 detect_clones_to_groups 包装为后台 job
- vector_embed：把 embed_all_symbols 包装为增量后台 job
"""

from __future__ import annotations

from typing import Any, Dict


def clone_detect_handler(ctx) -> Dict[str, Any]:
    """Clone detect job handler

    ctx.params:
        file_filter: str = ""
        min_lines: int = 5
        similarity_threshold: float = 0.8

    返回：
        {
            "total_groups": int,
            "type1_groups": int,
            "type2_groups": int,
            "type3_groups": int,
            "stored_groups": int,
            "scanned_symbols": int,
            "skipped_symbols": int,
        }
    """
    params = ctx.params
    file_filter = params.get("file_filter", "")
    min_lines = int(params.get("min_lines", 5))
    similarity_threshold = float(params.get("similarity_threshold", 0.8))

    # 通过 _DetectOnlyWrapper 复用 CloneDetectionMixin 的方法
    # 避免 CodeGraphDB 完整初始化（连接管理、workspace 探测）
    wrapper = _DetectOnlyWrapper(ctx.conn, ctx.workspace_id, ctx.conn_lock)
    return wrapper.detect_clones_to_groups(
        file_filter=file_filter,
        min_lines=min_lines,
        similarity_threshold=similarity_threshold,
        progress_callback=ctx.update_progress,
    )


def vector_embed_handler(ctx) -> Dict[str, Any]:
    """Vector embed job handler（Phase 7.2 增量 job）

    把 embed_all_symbols 包装为后台 job，只嵌入尚未有嵌入的符号（增量）。
    适合 20 万符号场景，避免在 MCP 在线请求中同步执行。

    ctx.params:
        batch_size: int = 32
        force: bool = False  # True 时强制重新嵌入所有符号

    返回：
        {
            "total": int,
            "success": int,
            "skipped": int,
            "failed": int,
        }
    """
    params = ctx.params
    batch_size = int(params.get("batch_size", 32))
    force = bool(params.get("force", False))

    wrapper = _VectorEmbedWrapper(ctx.conn, ctx.workspace_id, ctx.conn_lock)
    return wrapper.embed_all_symbols(
        batch_size=batch_size,
        force=force,
        progress_callback=ctx.update_progress,
    )


class _DetectOnlyWrapper:
    """轻量包装：复用 CloneDetectionMixin 的方法但只使用传入的 conn

    避免 CodeGraphDB 完整初始化（连接管理、workspace 探测）。
    所有数据库访问都通过 conn_lock 串行化。
    """

    def __init__(self, conn, workspace_id: int, conn_lock):
        self.conn = conn
        self._conn_lock = conn_lock
        self._workspace_id = workspace_id

    def _get_active_workspace_id(self) -> int:
        return self._workspace_id

    def _detect_clone_groups_core(self, *args, **kwargs):
        from ..db.db_clone_detection import CloneDetectionMixin
        bound = CloneDetectionMixin._detect_clone_groups_core.__get__(self, type(self))
        return bound(*args, **kwargs)

    def detect_clones_to_groups(self, *args, **kwargs):
        from ..db.db_clone_detection import CloneDetectionMixin
        bound = CloneDetectionMixin.detect_clones_to_groups.__get__(self, type(self))
        return bound(*args, **kwargs)


class _VectorEmbedWrapper:
    """轻量包装：复用 VectorMixin 的 embed_all_symbols 方法

    Phase 7.2：增量嵌入只处理没有嵌入的符号（force=False 时），
    避免在 MCP 在线请求中同步执行。
    """

    def __init__(self, conn, workspace_id: int, conn_lock):
        self.conn = conn
        self._conn_lock = conn_lock
        self._workspace_id = workspace_id
        self._embedder_instance = None  # 延迟加载

    def _get_active_workspace_id(self) -> int:
        return self._workspace_id

    def _get_embedder(self):
        """委托到 VectorMixin._get_embedder（延迟加载嵌入模型）"""
        from ..db.db_vector import VectorMixin
        bound = VectorMixin._get_embedder.__get__(self, type(self))
        return bound()

    def _embed_text(self, text):
        """委托到 VectorMixin._embed_text"""
        from ..db.db_vector import VectorMixin
        bound = VectorMixin._embed_text.__get__(self, type(self))
        return bound(text)

    def embed_all_symbols(self, *args, **kwargs):
        from ..db.db_vector import VectorMixin
        bound = VectorMixin.embed_all_symbols.__get__(self, type(self))
        return bound(*args, **kwargs)


def register_default_handlers(executor) -> None:
    """注册默认 job handlers 到 executor

    参数：
        executor: JobExecutor 实例
    """
    executor.register_handler("clone_detect", clone_detect_handler)
    executor.register_handler("vector_embed", vector_embed_handler)
