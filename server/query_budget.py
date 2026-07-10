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
- frontier_limit: frontier 队列最大长度（默认 500）

用法：
    budget = QueryBudget(max_depth=10, max_nodes=5000, timeout_ms=10000)
    chain = service.query_call_chain_down("ws", "mod.fn", max_depth=budget.max_depth)
    if budget.exhausted:
        logger.warning("查询预算耗尽")
"""

import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Any, List


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

    def start(self):
        """开始计时（每次查询前调用）。"""
        self._start_time = time.monotonic()
        self._nodes_visited = 0
        self._exhausted_reason = None

    def visit_node(self) -> bool:
        """记录一次节点访问，返回是否可以继续。

        Returns:
            True 可继续，False 预算耗尽
        """
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
        if len(results) > self.max_results:
            return results[:self.max_results]
        return results


# ----------------------------------------------------------------------
# 预设预算
# ----------------------------------------------------------------------

def default_budget() -> QueryBudget:
    """默认查询预算。"""
    return QueryBudget()


def deep_budget() -> QueryBudget:
    """深度查询预算（用于影响分析等需要更深入的场景）。"""
    return QueryBudget(max_depth=10, max_nodes=5000, timeout_ms=10000, max_results=500)


def shallow_budget() -> QueryBudget:
    """浅层查询预算（用于快速预览）。"""
    return QueryBudget(max_depth=3, max_nodes=100, timeout_ms=1000, max_results=20)


def unlimited_budget() -> QueryBudget:
    """无限制预算（慎用，仅供后台 job 使用）。"""
    return QueryBudget(
        max_depth=100,
        max_nodes=1_000_000,
        timeout_ms=300_000,  # 5 分钟
        max_results=100_000,
        frontier_limit=10_000,
    )
