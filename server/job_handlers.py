"""
Phase 7.0: Job Handlers

设计参考：enterprise-daemon-shared-snapshot-plan.md §Phase 7

注册到 JobExecutor 的 handler 集合。每个 handler 接收
JobContext 并返回 result_summary dict。

当前已注册的 handler：
- clone_detect：把 detect_clones_to_groups 包装为后台 job
- vector_embed：把 embed_all_symbols 包装为增量后台 job
- semgrep_scan：把 run_semgrep_and_save 包装为 bounded external process job
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


def semgrep_scan_handler(ctx) -> Dict[str, Any]:
    """Semgrep scan job handler（Phase 7.3 bounded external process job）

    把 run_semgrep_and_save 包装为后台 job。
    Semgrep CLI 作为外部子进程执行（已有 timeout 限制），
    在后台 job 中执行避免阻塞 MCP 请求。

    ctx.params:
        config: str = "p/default"
        languages: list = None  # 限制扫描的语言
        timeout: int = 300  # Semgrep CLI 超时（秒）

    返回：
        {
            "success": bool,
            "saved_findings": int,
            "total_findings": int,
        }
    """
    params = ctx.params
    config = params.get("config", "p/default")
    languages = params.get("languages")
    timeout = int(params.get("timeout", 300))

    ctx.update_progress(0.1, "starting semgrep scan")

    # 从 workspaces 表查询 workspace_root（Semgrep CLI 需要 cwd）
    workspace_root = ""
    with ctx.conn_lock:
        row = ctx.conn.execute(
            "SELECT root_path FROM workspaces WHERE id = ?", (ctx.workspace_id,)
        ).fetchone()
    if row:
        workspace_root = row["root_path"] if isinstance(row, dict) else row[0]

    wrapper = _SemgrepScanWrapper(
        ctx.conn, ctx.workspace_id, ctx.conn_lock, workspace_root
    )
    result = wrapper.run_semgrep_and_save(
        config=config,
        languages=languages,
        timeout=timeout,
    )

    ctx.update_progress(1.0, f"done: {result.get('total_findings', 0)} findings")
    return result


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


class _SemgrepScanWrapper:
    """轻量包装：复用 IssueAnalyzerMixin 的 semgrep scan 方法

    Phase 7.3：把 Semgrep CLI 作为 bounded external process 在后台 job 中执行。
    Semgrep CLI 自带 timeout 限制，避免长时间运行。
    """

    def __init__(self, conn, workspace_id: int, conn_lock, workspace_root: str = ""):
        self.conn = conn
        self._conn_lock = conn_lock
        self._workspace_id = workspace_id
        self.workspace_root = workspace_root

    def _get_active_workspace_id(self) -> int:
        return self._workspace_id

    def _delegate(self, method_name):
        """委托 IssueAnalyzerMixin 的方法到 self"""
        from ..analyzers.issues import IssueAnalyzerMixin
        method = getattr(IssueAnalyzerMixin, method_name)
        return method.__get__(self, type(self))

    def run_semgrep_and_save(self, *args, **kwargs):
        return self._delegate("run_semgrep_and_save")(*args, **kwargs)

    def run_semgrep(self, *args, **kwargs):
        return self._delegate("run_semgrep")(*args, **kwargs)

    def save_semgrep_findings(self, *args, **kwargs):
        return self._delegate("save_semgrep_findings")(*args, **kwargs)

    def _find_semgrep_cli(self):
        return self._delegate("_find_semgrep_cli")()

    def _detect_language_from_path(self, path: str) -> str:
        return self._delegate("_detect_language_from_path")(path)

    def _get_current_symbol_positions(self):
        return self._delegate("_get_current_symbol_positions")()


def register_default_handlers(executor) -> None:
    """注册默认 job handlers 到 executor

    参数：
        executor: JobExecutor 实例
    """
    executor.register_handler("clone_detect", clone_detect_handler)
    executor.register_handler("vector_embed", vector_embed_handler)
    executor.register_handler("semgrep_scan", semgrep_scan_handler)

