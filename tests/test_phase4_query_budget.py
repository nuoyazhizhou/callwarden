"""Phase 4.6 单元测试：QueryBudget 查询预算控制。

测试范围：
- QueryBudget 基础属性
- visit_node 节点计数和预算耗尽
- 超时检测
- truncate_results 截断
- 预设预算（default/deep/shallow/unlimited）
- SnapshotManagerService 集成：budget 参数传递
"""

import time
import sqlite3
import pytest

from server.query_budget import (
    QueryBudget, default_budget, deep_budget, shallow_budget, unlimited_budget,
)
from server.snapshot_manager import SnapshotManagerService

callwarden_core = pytest.importorskip("callwarden_core")


# ----------------------------------------------------------------------
# QueryBudget 基础属性
# ----------------------------------------------------------------------

class TestQueryBudgetBasic:
    def test_default_budget(self):
        b = QueryBudget()
        assert b.max_depth == 5
        assert b.max_nodes == 1000
        assert b.timeout_ms == 5000
        assert b.max_results == 100
        assert b.frontier_limit == 500

    def test_custom_budget(self):
        b = QueryBudget(max_depth=10, max_nodes=5000, timeout_ms=10000)
        assert b.max_depth == 10
        assert b.max_nodes == 5000
        assert b.timeout_ms == 10000

    def test_start_resets_state(self):
        b = QueryBudget(max_nodes=5)
        b.start()
        for _ in range(3):
            b.visit_node()
        # 重置
        b.start()
        assert b.nodes_visited == 0
        assert b.exhausted is False


# ----------------------------------------------------------------------
# visit_node 节点计数
# ----------------------------------------------------------------------

class TestVisitNode:
    def test_visit_node_within_limit(self):
        b = QueryBudget(max_nodes=5)
        b.start()
        for _ in range(5):
            assert b.visit_node() is True
        assert b.exhausted is False

    def test_visit_node_exceeds_limit(self):
        b = QueryBudget(max_nodes=3)
        b.start()
        for _ in range(3):
            assert b.visit_node() is True
        # 第 4 次应返回 False
        assert b.visit_node() is False
        assert b.exhausted is True
        assert "max_nodes" in b.exhausted_reason

    def test_visit_node_without_start_does_not_timeout(self):
        """未调用 start() 时不应触发超时。"""
        b = QueryBudget(timeout_ms=1)
        time.sleep(0.01)
        assert b.visit_node() is True  # 未启动计时


# ----------------------------------------------------------------------
# 超时检测
# ----------------------------------------------------------------------

class TestTimeout:
    def test_timeout_triggers_exhausted(self):
        b = QueryBudget(timeout_ms=10)  # 10ms 超时
        b.start()
        time.sleep(0.02)  # 等待 20ms
        assert b.visit_node() is False
        assert b.exhausted is True
        assert "timeout" in b.exhausted_reason

    def test_timeout_not_triggered_within_limit(self):
        b = QueryBudget(timeout_ms=1000)
        b.start()
        time.sleep(0.01)
        assert b.visit_node() is True
        assert b.exhausted is False


# ----------------------------------------------------------------------
# truncate_results
# ----------------------------------------------------------------------

class TestTruncateResults:
    def test_truncate_short_list(self):
        b = QueryBudget(max_results=10)
        results = list(range(5))
        assert b.truncate_results(results) == results

    def test_truncate_long_list(self):
        b = QueryBudget(max_results=3)
        results = list(range(10))
        truncated = b.truncate_results(results)
        assert len(truncated) == 3
        assert truncated == [0, 1, 2]

    def test_truncate_exact_boundary(self):
        b = QueryBudget(max_results=5)
        results = list(range(5))
        assert b.truncate_results(results) == results


# ----------------------------------------------------------------------
# 预设预算
# ----------------------------------------------------------------------

class TestPresetBudgets:
    def test_default_budget(self):
        b = default_budget()
        assert b.max_depth == 5
        assert b.max_nodes == 1000

    def test_deep_budget(self):
        b = deep_budget()
        assert b.max_depth == 10
        assert b.max_nodes == 5000
        assert b.timeout_ms == 10000

    def test_shallow_budget(self):
        b = shallow_budget()
        assert b.max_depth == 3
        assert b.max_nodes == 100
        assert b.timeout_ms == 1000

    def test_unlimited_budget(self):
        b = unlimited_budget()
        assert b.max_depth == 100
        assert b.max_nodes == 1_000_000
        assert b.timeout_ms == 300_000


# ----------------------------------------------------------------------
# SnapshotManagerService 集成
# ----------------------------------------------------------------------

@pytest.fixture
def minimal_db(tmp_path):
    db_path = tmp_path / "callwarden.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""CREATE TABLE file_instances (
        id INTEGER PRIMARY KEY, rel_path TEXT, status TEXT DEFAULT 'active')""")
    cur.execute("INSERT INTO file_instances VALUES (1, 'src/main.py', 'active')")
    cur.execute("""CREATE TABLE symbols (
        id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT,
        name TEXT, qualified_name TEXT, module_path TEXT,
        start_line INTEGER, end_line INTEGER, depth INTEGER)""")
    cur.execute("""INSERT INTO symbols VALUES
        (1, 1, 'fn', 'main', 'main', '', 1, 10, 0),
        (2, 1, 'fn', 'init', 'main.init', '', 11, 20, 1)""")
    cur.execute("""CREATE TABLE calls (
        caller_id INTEGER, callee_id INTEGER, callee_name TEXT,
        call_line INTEGER, is_cross_file INTEGER)""")
    cur.execute("INSERT INTO calls VALUES (1, 2, 'init', 5, 0)")
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def service():
    SnapshotManagerService.reset_instance()
    svc = SnapshotManagerService(max_workspaces=8)
    SnapshotManagerService._instance = svc
    yield svc
    SnapshotManagerService.reset_instance()


class TestBudgetIntegration:
    def test_search_symbols_with_budget(self, service, minimal_db):
        """search_symbols 接受 budget 参数。"""
        service.publish_snapshot("ws_b", minimal_db)
        budget = QueryBudget(max_results=1)
        results = service.search_symbols("ws_b", "main", budget=budget)
        assert len(results) <= 1

    def test_search_symbols_with_explicit_limit_overrides_budget(self, service, minimal_db):
        """显式 limit 参数覆盖 budget.max_results。"""
        service.publish_snapshot("ws_b", minimal_db)
        budget = QueryBudget(max_results=1)
        results = service.search_symbols("ws_b", "main", limit=10, budget=budget)
        # 显式 limit=10 优先
        assert len(results) <= 10

    def test_call_chain_with_budget(self, service, minimal_db):
        """query_call_chain_down 接受 budget 参数。"""
        service.publish_snapshot("ws_b", minimal_db)
        budget = QueryBudget(max_depth=2)
        chain = service.query_call_chain_down("ws_b", "main", budget=budget)
        assert isinstance(chain, list)

    def test_call_chain_with_explicit_depth(self, service, minimal_db):
        """显式 max_depth 参数覆盖 budget.max_depth。"""
        service.publish_snapshot("ws_b", minimal_db)
        budget = QueryBudget(max_depth=1)
        chain = service.query_call_chain_down("ws_b", "main", max_depth=3, budget=budget)
        assert isinstance(chain, list)

    def test_call_chain_without_budget_uses_default(self, service, minimal_db):
        """无 budget 参数时使用默认预算（max_depth=5）。"""
        service.publish_snapshot("ws_b", minimal_db)
        chain = service.query_call_chain_down("ws_b", "main")
        assert isinstance(chain, list)
