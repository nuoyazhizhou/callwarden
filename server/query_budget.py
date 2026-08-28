"""Phase 4.6: Query Budget —— 查询预算控制。

设计文档 §8.3 要求：
- chain/impact 默认有 depth、node limit、timeout、frontier
- 不允许无界扫描
- 短名查询必须先提示歧义或限制返回数量

QueryBudget 封装以下约束：
- max_depth: 调用链最大深度（默认 5）
- max_nodes: 最多访问节点数（默认 1000）
- timeout_ms: 超时（默认 5000ms，Python 侧软超时）
- max_results: 最大返回数量（默认 100）
- frontier_limit: BFS/DLS frontier 队列最大长度（默认 500）

用法：
    budget = QueryBudget(max_depth=10, max_nodes=5000, timeout_ms=10000)
    chain = service.query_call_chain_down("ws", "mod.fn", max_depth=budget.max_depth)
    if budget.exhausted:
        logger.warning("查询预算耗尽")

Phase 4-2 wire-production：默认走 Rust PyO3 API（callwarden_core.budget_preset /
budget_tracker_new / budget_tracker_visit_node / budget_tracker_truncate_results），
rollback_config 中 feature=rust_daemon_acl_path_budget 置为 1 时回退 Python。
Rust 失败时 fail-soft 降级到 Python 路径。
"""

import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Any, List, Dict


# ============================================
# Phase 4-2 wire-production: Rust 短路（资源预算）
# ============================================
# query_budget.py 默认走 Rust PyO3 API（callwarden_core.budget_*），
# rollback_config 中 feature=rust_daemon_acl_path_budget 置为 1 时回退 Python。
# Rust 失败时 fail-soft 降级到 Python 路径。
#
# 契约：docs/design/phase4-2-acl-path-budget-contract.md §3.4-5
# - budget_create / budget_preset: 返回 5 字段 dict（max_depth/max_nodes/timeout_ms/
#   max_results/frontier_limit），与 Python QueryBudget 默认值一致
# - budget_tracker_new: 创建运行时跟踪器 dict（含 budget 配置 + 运行时状态）
# - budget_tracker_visit_node: 自增 visited_count，超限时设 exceeded/exhausted_reason
# - budget_tracker_truncate_results: 截断 results 到 max_results 长度

_RUST_BUDGET_AVAILABLE = False
_callwarden_core = None
try:
    import callwarden_core as _callwarden_core  # type: ignore
    if (
        hasattr(_callwarden_core, "budget_create")
        and hasattr(_callwarden_core, "budget_preset")
        and hasattr(_callwarden_core, "budget_tracker_new")
        and hasattr(_callwarden_core, "budget_tracker_visit_node")
        and hasattr(_callwarden_core, "budget_tracker_truncate_results")
    ):
        _RUST_BUDGET_AVAILABLE = True
except ImportError:
    _callwarden_core = None

# rollback_config 查询缓存（60s TTL，与 daemon_server._ACL_ROLLBACK_CACHE 共用
# feature_name=rust_daemon_acl_path_budget；这里独立缓存避免跨模块状态耦合）
_BUDGET_ROLLBACK_CACHE: Dict[str, Any] = {"ts": 0.0, "value": False}
_BUDGET_ROLLBACK_CACHE_TTL = 60.0


def _is_rust_budget_rolled_back() -> bool:
    """检查 query budget Rust feature 的回滚状态（60s daemon RPC 缓存）。

    rollback_config 的数据库权威由 Rust daemon 持有；Python 只负责缓存
    结果，并在 daemon 不可用或响应畸形时按旧 fail-soft 语义视为未回滚。
    """
    now = time.time()
    if now - _BUDGET_ROLLBACK_CACHE["ts"] < _BUDGET_ROLLBACK_CACHE_TTL:
        return _BUDGET_ROLLBACK_CACHE["value"]  # type: ignore[return-value]
    try:
        result = _call_daemon_rpc(
            "mcp.query_budget.is_rust_budget_rolled_back", {})
        value = bool(isinstance(result, dict) and result.get("rolled_back"))
    except Exception:
        # fail-soft：只读 authority 读失败时视为未回滚，绝不回退本地 SQLite。
        value = False
    _BUDGET_ROLLBACK_CACHE["ts"] = now
    _BUDGET_ROLLBACK_CACHE["value"] = value
    return value


def _call_daemon_rpc(method: str, params: Dict[str, Any]) -> Any:
    """经 daemon 统一客户端发起 RPC，避免 query_budget 与 client 循环依赖。"""
    from ._mcp_common import _call_daemon_rpc as _rpc

    return _rpc(method, params)


def _rust_budget_available() -> bool:
    """Rust budget 短路是否可用（模块加载 + 未回滚）"""
    return _RUST_BUDGET_AVAILABLE and not _is_rust_budget_rolled_back()


# Rust tracker exhausted_reason → Python 格式映射（契约 §5.3 预期差异）
_RUST_REASON_TO_PY_FMT = {
    "max_nodes": "max_nodes({max_nodes}) exceeded",
    "timeout": "timeout({timeout_ms}ms) exceeded",
}


@dataclass
class QueryBudget:
    """查询预算。

    Attributes:
        max_depth: 调用链/影响分析最大深度
        max_nodes: 最多访问节点数
        timeout_ms: 超时（毫秒）
        max_results: 最大返回结果数
        frontier_limit: BFS/DLS frontier 队列最大长度
    """
    max_depth: int = 5
    max_nodes: int = 1000
    timeout_ms: int = 5000
    max_results: int = 100
    frontier_limit: int = 500

    # 运行时状态
    _start_time: float = field(default=0.0, init=False, repr=False)
    _nodes_visited: int = field(default=0, init=False, repr=False)
    _exhausted_reason: Optional[str] = field(default=None, init=False, repr=False)
    # Phase 4-2: Rust 短路跟踪器（仅在 start() 后惰性创建）
    _rust_tracker: Optional[dict] = field(default=None, init=False, repr=False)

    def start(self):
        """开始计时（每次查询前调用）。"""
        self._start_time = time.monotonic()
        self._nodes_visited = 0
        self._exhausted_reason = None
        # Phase 4-2 wire-production: 惰性创建 Rust tracker
        self._rust_tracker = None
        if _rust_budget_available():
            try:
                rust_dict = _callwarden_core.budget_create(
                    max_depth=self.max_depth,
                    max_nodes=self.max_nodes,
                    timeout_ms=self.timeout_ms,
                    max_results=self.max_results,
                    frontier_limit=self.frontier_limit,
                )
                self._rust_tracker = _callwarden_core.budget_tracker_new(rust_dict)
            except Exception:
                self._rust_tracker = None  # fail-soft → Python 路径

    def visit_node(self) -> bool:
        """记录一次节点访问，返回是否可以继续。

        Returns:
            True 可继续，False 预算耗尽
        """
        # Phase 4-2 wire-production: Rust 短路 visit_node
        if self._rust_tracker is not None:
            try:
                ok = bool(_callwarden_core.budget_tracker_visit_node(self._rust_tracker))
                # 同步状态回 Python 属性（保持 Python API 向后兼容）
                self._nodes_visited = int(self._rust_tracker["visited_count"])
                if not ok and self._exhausted_reason is None:
                    rust_reason = self._rust_tracker.get("exhausted_reason")
                    fmt = _RUST_REASON_TO_PY_FMT.get(rust_reason or "")
                    if fmt:
                        self._exhausted_reason = fmt.format(
                            max_nodes=self.max_nodes, timeout_ms=self.timeout_ms)
                return ok
            except Exception:
                self._rust_tracker = None  # fail-soft → 降级 Python 路径
        # Python 降级路径
        self._nodes_visited += 1
        if self._nodes_visited > self.max_nodes:
            self._exhausted_reason = f"max_nodes({self.max_nodes}) exceeded"
            return False
        if self._is_timeout():
            self._exhausted_reason = f"timeout({self.timeout_ms}ms) exceeded"
            return False
        return True

    def _is_timeout(self) -> bool:
        """检查是否超时。"""
        if self._start_time == 0.0:
            return False
        elapsed_ms = (time.monotonic() - self._start_time) * 1000
        return elapsed_ms > self.timeout_ms

    @property
    def exhausted(self) -> bool:
        """预算是否已耗尽。"""
        return self._exhausted_reason is not None

    @property
    def exhausted_reason(self) -> Optional[str]:
        """耗尽原因（未耗尽时为 None）。"""
        return self._exhausted_reason

    @property
    def nodes_visited(self) -> int:
        """已访问节点数。"""
        return self._nodes_visited

    @property
    def elapsed_ms(self) -> float:
        """已耗时（毫秒）。"""
        if self._start_time == 0.0:
            return 0.0
        return (time.monotonic() - self._start_time) * 1000

    def truncate_results(self, results: List[Any]) -> List[Any]:
        """截断结果列表到 max_results。"""
        # Phase 4-2 wire-production: Rust 短路 truncate_results
        if self._rust_tracker is not None:
            try:
                return list(_callwarden_core.budget_tracker_truncate_results(
                    self._rust_tracker, results))
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        if len(results) > self.max_results:
            return results[:self.max_results]
        return results


# ----------------------------------------------------------------------
# 预设预算
# ----------------------------------------------------------------------

def default_budget() -> QueryBudget:
    """默认查询预算。"""
    # Phase 4-2 wire-production: Rust 短路 preset
    if _rust_budget_available():
        try:
            d = dict(_callwarden_core.budget_preset("default"))
            return QueryBudget(
                max_depth=int(d["max_depth"]),
                max_nodes=int(d["max_nodes"]),
                timeout_ms=int(d["timeout_ms"]),
                max_results=int(d["max_results"]),
                frontier_limit=int(d["frontier_limit"]),
            )
        except Exception:
            pass  # fail-soft → 降级 Python 路径
    return QueryBudget()


def deep_budget() -> QueryBudget:
    """深度查询预算（用于影响分析等需要更深入的场景）。"""
    if _rust_budget_available():
        try:
            d = dict(_callwarden_core.budget_preset("deep"))
            return QueryBudget(
                max_depth=int(d["max_depth"]),
                max_nodes=int(d["max_nodes"]),
                timeout_ms=int(d["timeout_ms"]),
                max_results=int(d["max_results"]),
                frontier_limit=int(d["frontier_limit"]),
            )
        except Exception:
            pass  # fail-soft → 降级 Python 路径
    return QueryBudget(max_depth=10, max_nodes=5000, timeout_ms=10000, max_results=500)


def shallow_budget() -> QueryBudget:
    """浅层查询预算（用于快速预览）。"""
    if _rust_budget_available():
        try:
            d = dict(_callwarden_core.budget_preset("shallow"))
            return QueryBudget(
                max_depth=int(d["max_depth"]),
                max_nodes=int(d["max_nodes"]),
                timeout_ms=int(d["timeout_ms"]),
                max_results=int(d["max_results"]),
                frontier_limit=int(d["frontier_limit"]),
            )
        except Exception:
            pass  # fail-soft → 降级 Python 路径
    return QueryBudget(max_depth=3, max_nodes=100, timeout_ms=1000, max_results=20)


def unlimited_budget() -> QueryBudget:
    """无限制预算（慎用，仅供后台 job 使用）。"""
    if _rust_budget_available():
        try:
            d = dict(_callwarden_core.budget_preset("unlimited"))
            return QueryBudget(
                max_depth=int(d["max_depth"]),
                max_nodes=int(d["max_nodes"]),
                timeout_ms=int(d["timeout_ms"]),
                max_results=int(d["max_results"]),
                frontier_limit=int(d["frontier_limit"]),
            )
        except Exception:
            pass  # fail-soft → 降级 Python 路径
    return QueryBudget(
        max_depth=100,
        max_nodes=1_000_000,
        timeout_ms=300_000,  # 5 分钟
        max_results=100_000,
        frontier_limit=10_000,
    )
