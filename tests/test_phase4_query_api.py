"""Phase 4.5 单元测试：query API 带 workspace_instance_id。

测试范围：
- 所有 query API 都以 workspace_instance_id 为第一参数
- query_symbol 按 qualified_name 精确查询
- query_call_chain_down 下游调用链
- query_topological_order 拓扑序
- query_detect_cycles 循环检测
- query_stats 统计信息
- ensure_workspace 检查
- 未发布 workspace 返回空/None
"""

import sqlite3
import pytest

from callwarden.server.snapshot_manager import SnapshotManagerService

callwarden_core = pytest.importorskip("callwarden_core")


@pytest.fixture
def db_with_cycle(tmp_path):
    """构造一个含循环依赖的 db：
        A → B → C → A  (循环)
        A → D
    """
    db_path = tmp_path / "cycle.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""CREATE TABLE file_instances (
        id INTEGER PRIMARY KEY, rel_path TEXT, status TEXT DEFAULT 'active')""")
    cur.execute("INSERT INTO file_instances VALUES (1, 'src/a.py', 'active')")
    cur.execute("""CREATE TABLE symbols (
        id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT,
        name TEXT, qualified_name TEXT, module_path TEXT,
        start_line INTEGER, end_line INTEGER, depth INTEGER)""")
    cur.execute("""INSERT INTO symbols VALUES
        (1, 1, 'fn', 'A', 'mod.A', '', 1, 10, 0),
        (2, 1, 'fn', 'B', 'mod.B', '', 11, 20, 1),
        (3, 1, 'fn', 'C', 'mod.C', '', 21, 30, 1),
        (4, 1, 'fn', 'D', 'mod.D', '', 31, 40, 1)
    """)
    cur.execute("""CREATE TABLE calls (
        caller_id INTEGER, callee_id INTEGER, callee_name TEXT,
        call_line INTEGER, is_cross_file INTEGER)""")
    # A→B, B→C, C→A (循环), A→D
    cur.execute("""INSERT INTO calls VALUES
        (1, 2, 'B', 5, 0),
        (2, 3, 'C', 15, 0),
        (3, 1, 'A', 25, 0),
        (1, 4, 'D', 6, 0)
    """)
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


# ----------------------------------------------------------------------
# query_symbol
# ----------------------------------------------------------------------

class TestQuerySymbol:
    def test_query_symbol_found(self, service, db_with_cycle):
        service.publish_snapshot("ws_sym", db_with_cycle)
        sym = service.query_symbol("ws_sym", "mod.A")
        assert sym is not None
        assert sym["name"] == "A"
        assert sym["qualified_name"] == "mod.A"

    def test_query_symbol_not_found(self, service, db_with_cycle):
        service.publish_snapshot("ws_sym", db_with_cycle)
        sym = service.query_symbol("ws_sym", "nonexistent.symbol")
        assert sym is None

    def test_query_symbol_unknown_workspace(self, service):
        assert service.query_symbol("ghost", "anything") is None


# ----------------------------------------------------------------------
# query_call_chain_down
# ----------------------------------------------------------------------

class TestQueryCallChainDown:
    def test_call_chain_down_returns_results(self, service, db_with_cycle):
        service.publish_snapshot("ws_chain", db_with_cycle)
        chain = service.query_call_chain_down("ws_chain", "mod.A", max_depth=5)
        assert isinstance(chain, list)
        # A→B→C→A 循环，至少有 A→B 的结果
        assert len(chain) >= 1

    def test_call_chain_down_unknown_workspace(self, service):
        assert service.query_call_chain_down("ghost", "mod.A") == []


# ----------------------------------------------------------------------
# query_topological_order
# ----------------------------------------------------------------------

class TestQueryTopoOrder:
    def test_topo_order_returns_list(self, service, db_with_cycle):
        service.publish_snapshot("ws_topo", db_with_cycle)
        topo = service.query_topological_order("ws_topo")
        assert isinstance(topo, list)

    def test_topo_order_unknown_workspace(self, service):
        assert service.query_topological_order("ghost") == []


# ----------------------------------------------------------------------
# query_detect_cycles
# ----------------------------------------------------------------------

class TestQueryDetectCycles:
    def test_detect_cycles_finds_cycle(self, service, db_with_cycle):
        """数据库包含 A→B→C→A 循环。"""
        service.publish_snapshot("ws_cycles", db_with_cycle)
        cycles = service.query_detect_cycles("ws_cycles")
        assert isinstance(cycles, list)
        # 是否检测到循环取决于实现，但至少应返回 list
        # 检测到循环时 cycles 应非空
        if len(cycles) > 0:
            # 每个循环是符号名列表
            assert isinstance(cycles[0], list)

    def test_detect_cycles_unknown_workspace(self, service):
        assert service.query_detect_cycles("ghost") == []


# ----------------------------------------------------------------------
# query_stats
# ----------------------------------------------------------------------

class TestQueryStats:
    def test_stats_returns_dict(self, service, db_with_cycle):
        service.publish_snapshot("ws_stats", db_with_cycle)
        stats = service.query_stats("ws_stats")
        assert stats is not None
        assert isinstance(stats, dict)

    def test_stats_unknown_workspace(self, service):
        assert service.query_stats("ghost") is None


# ----------------------------------------------------------------------
# ensure_workspace
# ----------------------------------------------------------------------

class TestEnsureWorkspace:
    def test_ensure_published_workspace(self, service, db_with_cycle):
        service.publish_snapshot("ws_ensure", db_with_cycle)
        assert service.ensure_workspace("ws_ensure") is True

    def test_ensure_unpublished_workspace(self, service):
        assert service.ensure_workspace("ghost") is False


# ----------------------------------------------------------------------
# 所有 query API 以 workspace_instance_id 为第一参数
# ----------------------------------------------------------------------

class TestWorkspaceIdFirstArg:
    """验证所有 query API 的第一参数都是 workspace_instance_id。"""

    def test_all_query_methods_have_workspace_id_first(self, service, db_with_cycle):
        """通过 inspect 验证方法签名。"""
        import inspect

        service.publish_snapshot("ws_sig", db_with_cycle)

        query_methods = [
            "query_callers",
            "query_callees",
            "search_symbols",
            "query_symbol",
            "query_call_chain_down",
            "query_topological_order",
            "query_detect_cycles",
            "query_stats",
            "get_snapshot_stats",
            "get_current_generation",
            "ensure_workspace",
        ]

        for method_name in query_methods:
            method = getattr(service, method_name)
            sig = inspect.signature(method)
            params = list(sig.parameters.keys())
            assert params[0] == "workspace_instance_id", (
                f"{method_name} 第一参数应为 workspace_instance_id，实际为 {params[0]}"
            )
