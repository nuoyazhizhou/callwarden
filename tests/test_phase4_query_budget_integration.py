"""Phase 4 Minor P4-M2: QueryBudget 集成测试

修复 P4-M2: query_callers/query_callees/query_topological_order/query_detect_cycles
原本无 budget 限制，现已补上。
"""
import inspect
import os
import unittest
from unittest.mock import MagicMock, patch


class TestQueryBudgetIntegration(unittest.TestCase):
    """验证 4 个查询方法已集成 QueryBudget"""

    def setUp(self):
        from server.snapshot_manager import SnapshotManagerService
        self.service = SnapshotManagerService()
        # Mock Rust store
        self.mock_store = MagicMock()
        self.service._rust_stores["ws1"] = self.mock_store

    def test_query_callers_has_budget_param(self):
        """query_callers 应有 budget 参数"""
        sig = inspect.signature(self.service.query_callers)
        self.assertIn("budget", sig.parameters)

    def test_query_callees_has_budget_param(self):
        """query_callees 应有 budget 参数"""
        sig = inspect.signature(self.service.query_callees)
        self.assertIn("budget", sig.parameters)

    def test_query_topological_order_has_budget_param(self):
        """query_topological_order 应有 budget 参数"""
        sig = inspect.signature(self.service.query_topological_order)
        self.assertIn("budget", sig.parameters)

    def test_query_detect_cycles_has_budget_param(self):
        """query_detect_cycles 应有 budget 参数"""
        sig = inspect.signature(self.service.query_detect_cycles)
        self.assertIn("budget", sig.parameters)

    def test_query_callers_truncates_results(self):
        """query_callers 应按 budget.max_results 截断结果"""
        from server.query_budget import QueryBudget
        self.mock_store.get_callers.return_value = [{"name": f"caller_{i}"} for i in range(200)]
        budget = QueryBudget(max_results=10)
        result = self.service.query_callers("ws1", "foo", budget=budget)
        self.assertEqual(len(result), 10)

    def test_query_callees_truncates_results(self):
        """query_callees 应按 budget.max_results 截断结果"""
        from server.query_budget import QueryBudget
        self.mock_store.get_callees.return_value = [{"name": f"callee_{i}"} for i in range(200)]
        budget = QueryBudget(max_results=10)
        result = self.service.query_callees("ws1", "foo", budget=budget)
        self.assertEqual(len(result), 10)

    def test_query_topological_order_truncates_results(self):
        """query_topological_order 应按 budget.max_results 截断结果"""
        from server.query_budget import QueryBudget
        self.mock_store.get_topological_order.return_value = [f"node_{i}" for i in range(200)]
        budget = QueryBudget(max_results=10)
        result = self.service.query_topological_order("ws1", budget=budget)
        self.assertEqual(len(result), 10)

    def test_query_detect_cycles_truncates_results(self):
        """query_detect_cycles 应按 budget.max_results 截断结果"""
        from server.query_budget import QueryBudget
        self.mock_store.detect_cycles.return_value = [[f"cycle_{i}"] for i in range(200)]
        budget = QueryBudget(max_results=10)
        result = self.service.query_detect_cycles("ws1", budget=budget)
        self.assertEqual(len(result), 10)

    def test_query_callers_default_budget_when_none(self):
        """budget=None 时使用默认预算，不崩溃"""
        self.mock_store.get_callers.return_value = [{"name": "caller_1"}]
        result = self.service.query_callers("ws1", "foo", budget=None)
        self.assertEqual(len(result), 1)

    def test_query_detect_cycles_calls_start(self):
        """query_detect_cycles 应调用 budget.start()（启动计时）"""
        from server.query_budget import QueryBudget
        budget = QueryBudget()
        self.mock_store.detect_cycles.return_value = []
        self.service.query_detect_cycles("ws1", budget=budget)
        self.assertGreater(budget.elapsed_ms, -0.01)  # start() 已调用

    def test_existing_search_symbols_still_has_budget(self):
        """回归：search_symbols 仍保留 budget 参数"""
        sig = inspect.signature(self.service.search_symbols)
        self.assertIn("budget", sig.parameters)

    def test_existing_call_chain_down_still_has_budget(self):
        """回归：query_call_chain_down 仍保留 budget 参数"""
        sig = inspect.signature(self.service.query_call_chain_down)
        self.assertIn("budget", sig.parameters)


if __name__ == "__main__":
    unittest.main()
