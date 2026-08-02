"""Phase 4.8 单元测试：DaemonClient

测试范围：
- DaemonClient 单例和配置
- workspace_instance_id 推导
- 查询路由：daemon 优先 + SQL 回退
- 路由统计（daemon_hits / sql_fallbacks）
- diff_symbol / diff_signature 通过 daemon client
"""

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 跳过条件：callwarden_core 未安装时跳过 daemon 路由测试
try:
    import callwarden_core
    HAS_RUST = True
except ImportError:
    HAS_RUST = False


def _make_test_db(db_path):
    """构造测试用 callwarden.db"""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY, rel_path TEXT, status TEXT DEFAULT 'active'
        )
    """)
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (1, 'src/main.py')")
    cur.execute("""
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT,
            name TEXT, qualified_name TEXT, module_path TEXT,
            start_line INTEGER, end_line INTEGER, depth INTEGER
        )
    """)
    cur.execute("""INSERT INTO symbols VALUES
        (1, 1, 'fn', 'main', 'main', '', 10, 20, 0),
        (2, 1, 'fn', 'init', 'main.init', '', 5, 8, 1)
    """)
    cur.execute("""
        CREATE TABLE calls (
            caller_id INTEGER, callee_id INTEGER, callee_name TEXT,
            call_line INTEGER, is_cross_file INTEGER
        )
    """)
    cur.execute("INSERT INTO calls VALUES (1, 2, 'init', 12, 0)")
    conn.commit()
    conn.close()


class TestDaemonClientSingleton:
    def test_singleton(self):
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        c1 = DaemonClient.get_instance()
        c2 = DaemonClient.get_instance()
        assert c1 is c2
        DaemonClient.reset_instance()

    def test_configure_workspace(self):
        from callwarden.server.daemon_client import DaemonClient, derive_workspace_instance_id
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        client.configure_workspace("/some/project/root")
        assert client.workspace_instance_id is not None
        assert len(client.workspace_instance_id) == 16
        # 与 derive_workspace_instance_id 一致
        expected = derive_workspace_instance_id("/some/project/root")
        assert client.workspace_instance_id == expected
        DaemonClient.reset_instance()


class TestDeriveWorkspaceId:
    def test_same_path_same_id(self):
        from callwarden.server.daemon_client import derive_workspace_instance_id
        id1 = derive_workspace_instance_id("/path/to/project")
        id2 = derive_workspace_instance_id("/path/to/project")
        assert id1 == id2

    def test_different_path_different_id(self):
        from callwarden.server.daemon_client import derive_workspace_instance_id
        id1 = derive_workspace_instance_id("/path/to/projectA")
        id2 = derive_workspace_instance_id("/path/to/projectB")
        assert id1 != id2

    def test_id_is_16_chars(self):
        from callwarden.server.daemon_client import derive_workspace_instance_id
        wid = derive_workspace_instance_id("/test")
        assert len(wid) == 16


class TestRoutingStats:
    def test_initial_stats_zero(self):
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        stats = client.get_routing_stats()
        assert stats["daemon_hits"] == 0
        assert stats["sql_fallbacks"] == 0
        assert stats["total_queries"] == 0
        DaemonClient.reset_instance()

    def test_stats_after_sql_fallback(self):
        """无 db_path 时走 SQL 回退，sql_fallbacks 增加。"""
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        # 不提供 db_path，应该走 SQL 回退
        with patch.object(client, '_sql_fallback_get_stats', return_value={"files": 0}):
            client.get_stats(db_path=None)
        stats = client.get_routing_stats()
        assert stats["sql_fallbacks"] == 1
        assert stats["daemon_hits"] == 0
        DaemonClient.reset_instance()


@pytest.mark.skipif(not HAS_RUST, reason="callwarden_core not available")
class TestDaemonRoutingWithRust:
    """Rust 后端可用时的 daemon 路由测试。"""

    def test_get_stats_via_daemon(self, tmp_path):
        from callwarden.server.daemon_client import DaemonClient
        from callwarden.server.snapshot_manager import SnapshotManagerService
        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()

        db_path = tmp_path / "test.db"
        _make_test_db(db_path)

        client = DaemonClient.get_instance()
        # 修复 workspace_instance_id
        parent_dir = os.path.basename(os.path.dirname(str(db_path)))
        # 使用临时 id
        client._workspace_instance_id = "test_ws_daemon_001"

        result = client.get_stats(db_path=str(db_path))
        assert result is not None
        stats = client.get_routing_stats()
        assert stats["daemon_hits"] >= 1
        assert stats["daemon_ready"] is True

        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()

    def test_search_symbols_via_daemon(self, tmp_path):
        from callwarden.server.daemon_client import DaemonClient
        from callwarden.server.snapshot_manager import SnapshotManagerService
        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()

        db_path = tmp_path / "test.db"
        _make_test_db(db_path)

        client = DaemonClient.get_instance()
        client._workspace_instance_id = "test_ws_search_001"

        results = client.search_symbols("main", db_path=str(db_path))
        assert isinstance(results, list)
        assert len(results) > 0
        stats = client.get_routing_stats()
        assert stats["daemon_hits"] >= 1

        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()

    def test_get_callers_via_daemon(self, tmp_path):
        from callwarden.server.daemon_client import DaemonClient
        from callwarden.server.snapshot_manager import SnapshotManagerService
        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()

        db_path = tmp_path / "test.db"
        _make_test_db(db_path)

        client = DaemonClient.get_instance()
        client._workspace_instance_id = "test_ws_callers_001"

        callers = client.get_callers("init", db_path=str(db_path))
        assert isinstance(callers, list)
        # main 调用了 init
        assert len(callers) > 0
        stats = client.get_routing_stats()
        assert stats["daemon_hits"] >= 1

        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()

    def test_get_symbol_via_daemon(self, tmp_path):
        from callwarden.server.daemon_client import DaemonClient
        from callwarden.server.snapshot_manager import SnapshotManagerService
        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()

        db_path = tmp_path / "test.db"
        _make_test_db(db_path)

        client = DaemonClient.get_instance()
        client._workspace_instance_id = "test_ws_symbol_001"

        sym = client.get_symbol("main", db_path=str(db_path))
        assert sym is not None
        assert sym["name"] == "main"

        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()

    def test_detect_cycles_via_daemon(self, tmp_path):
        from callwarden.server.daemon_client import DaemonClient
        from callwarden.server.snapshot_manager import SnapshotManagerService
        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()

        db_path = tmp_path / "test.db"
        _make_test_db(db_path)

        client = DaemonClient.get_instance()
        client._workspace_instance_id = "test_ws_cycles_001"

        cycles = client.detect_cycles(db_path=str(db_path))
        assert isinstance(cycles, list)

        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()

    def test_get_topological_order_via_daemon(self, tmp_path):
        from callwarden.server.daemon_client import DaemonClient
        from callwarden.server.snapshot_manager import SnapshotManagerService
        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()

        db_path = tmp_path / "test.db"
        _make_test_db(db_path)

        client = DaemonClient.get_instance()
        client._workspace_instance_id = "test_ws_topo_001"

        order = client.get_topological_order(limit=10, db_path=str(db_path))
        assert isinstance(order, list)
        assert len(order) > 0

        SnapshotManagerService.reset_instance()
        DaemonClient.reset_instance()


class TestSQLFallback:
    """daemon 不可用时走 SQL 回退。"""

    def test_sql_fallback_when_no_db_path(self):
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()

        # mock SQL 回退方法
        with patch.object(client, '_sql_fallback_get_stats', return_value={"files": 10}) as mock_sql:
            result = client.get_stats(db_path=None)
            assert result == {"files": 10}
            mock_sql.assert_called_once()

        DaemonClient.reset_instance()

    def test_sql_fallback_get_callers(self):
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()

        with patch.object(client, '_sql_fallback_get_callers', return_value=[{"caller": "fn1"}]):
            result = client.get_callers("test_fn", db_path=None)
            assert len(result) == 1
            assert result[0]["caller"] == "fn1"

        DaemonClient.reset_instance()

    def test_sql_fallback_search_symbols(self):
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()

        with patch.object(client, '_sql_fallback_search_symbols', return_value=[]):
            result = client.search_symbols("test", db_path=None)
            assert result == []

        DaemonClient.reset_instance()


class TestDaemonReady:
    def test_daemon_not_ready_without_workspace(self):
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        assert client.is_daemon_ready() is False
        DaemonClient.reset_instance()

    def test_daemon_not_ready_with_nonexistent_db(self):
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        client._workspace_instance_id = "nonexistent_ws"
        # db 不存在，不应该发布成功
        assert client.is_daemon_ready() is False
        DaemonClient.reset_instance()


class TestRoutingStatsSummary:
    def test_routing_stats_after_mixed_queries(self, tmp_path):
        """混合查询后统计正确。"""
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()


class TestDaemonEndpointRouting:
    """验证 auto/enterprise/local 的真实 daemon endpoint 分流。"""

    def test_local_mode_never_probes_or_starts_daemon(self):
        from callwarden.server.daemon_client import DaemonClient

        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        with patch("callwarden.server.daemon_client.get_daemon_mode", return_value="local"):
            with patch.object(client._rpc, "call") as rpc_call:
                with patch.object(client, "_sql_fallback_get_stats", return_value={"files": 1}):
                    assert client.get_stats(db_path="unused.db") == {"files": 1}
                rpc_call.assert_not_called()
        DaemonClient.reset_instance()

    def test_auto_mode_autostarts_when_endpoint_is_missing(self):
        from callwarden.server.daemon_client import DaemonClient, DaemonUnavailableError

        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        client._rpc.probe = MagicMock(side_effect=DaemonUnavailableError("stale socket"))
        with patch("callwarden.server.daemon_client.get_daemon_mode", return_value="auto"):
            with patch("callwarden.server.daemon_client.ensure_daemon", return_value=None) as start:
                with patch.object(client, "_sql_fallback_get_stats", return_value={"files": 2}):
                    assert client.get_stats(db_path="unused.db") == {"files": 2}
                assert start.call_count == 1
                assert start.call_args.args[0] == client._rpc.socket_path
        client._rpc.probe.assert_called_once()
        DaemonClient.reset_instance()

    def test_auto_mode_retries_original_query_after_autostart(self):
        from callwarden.server.daemon_client import DaemonClient, DaemonUnavailableError

        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        daemon_conn = MagicMock()
        rpc_call = MagicMock(side_effect=[
            {"workspace_instance_id": "ws-auto"},
            {"files": 3},
        ])
        client._rpc.call = rpc_call
        client._rpc.probe = MagicMock(side_effect=DaemonUnavailableError("not ready"))
        client._rpc.publish_snapshot = MagicMock()
        client._rpc._probe_connection = MagicMock(return_value=True)

        def fake_ensure(endpoint, **kwargs):
            assert endpoint == client._rpc.socket_path
            kwargs["readiness_check"](daemon_conn)
            return daemon_conn

        with patch("callwarden.server.daemon_client.get_daemon_mode", return_value="auto"):
            with patch("callwarden.server.daemon_client.ensure_daemon", side_effect=fake_ensure):
                with patch.object(client, "_sql_fallback_get_stats") as sql:
                    result = client.get_stats(db_path="unused.db")
        assert result == {"files": 3}
        daemon_conn.close.assert_called_once_with()
        client._rpc._probe_connection.assert_called_once_with(daemon_conn)
        client._rpc.publish_snapshot.assert_called_once()
        sql.assert_not_called()
        assert client.daemon_hits == 1
        DaemonClient.reset_instance()

    def test_enterprise_mode_does_not_fallback_when_ping_fails(self):
        from callwarden.server.daemon_client import DaemonClient, DaemonUnavailableError

        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        client._rpc.probe = MagicMock(side_effect=DaemonUnavailableError("down"))
        with patch("callwarden.server.daemon_client.get_daemon_mode", return_value="enterprise"):
            with patch.object(client, "_sql_fallback_get_stats") as sql:
                with pytest.raises(DaemonUnavailableError, match="enterprise daemon"):
                    client.get_stats(db_path="unused.db")
                sql.assert_not_called()
        DaemonClient.reset_instance()

    def test_public_call_with_autostart_uses_protocol_readiness_probe(self):
        """公开 RPC 入口不能绕过 auto 的协议级 readiness probe。"""
        from callwarden.server.daemon_client import DaemonClient, DaemonUnavailableError

        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        client._rpc.call = MagicMock(side_effect=[
            DaemonUnavailableError("starting"),
            {"files": 4},
        ])
        client._rpc._probe_connection = MagicMock(return_value=True)
        daemon_conn = MagicMock()

        def fake_ensure(endpoint, **kwargs):
            assert endpoint == client._rpc.socket_path
            kwargs["readiness_check"](daemon_conn)
            return daemon_conn

        mutex = MagicMock()
        mutex.try_acquire.return_value = True
        with patch("callwarden.server.daemon_client.get_daemon_mode", return_value="auto"):
            with patch("callwarden.server.daemon_client.ensure_daemon", side_effect=fake_ensure):
                with patch("callwarden.server.daemon_client.DaemonMutex", return_value=mutex):
                    result = client.call_with_autostart("query.stats")

        assert result == {"result": {"files": 4}, "degraded": False}
        client._rpc._probe_connection.assert_called_once_with(daemon_conn)
        daemon_conn.close.assert_called_once_with()
        mutex.release.assert_called_once_with()
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()

        # 一次 SQL 回退
        with patch.object(client, '_sql_fallback_get_stats', return_value={}):
            client.get_stats(db_path=None)

        # 再次 SQL 回退
        with patch.object(client, '_sql_fallback_search_symbols', return_value=[]):
            client.search_symbols("test", db_path=None)

        stats = client.get_routing_stats()
        assert stats["sql_fallbacks"] == 2
        assert stats["daemon_hits"] == 0
        assert stats["total_queries"] == 2
        assert stats["daemon_ratio_percent"] == 0.0

        DaemonClient.reset_instance()


class TestQueryNumericParamsPropagation:
    """G12 批次8：验证 query.* 方法的数字参数（limit/max_depth）作为 int 类型传递给 daemon。

    根因：Rust daemon 原用 get_str_param 只接受字符串，Python client 传 int 时被忽略。
    修复：Rust dispatch.rs 新增 get_int_param/get_int_param_or 支持 JSON 数字 + 字符串。
    本测试验证 Python client 端确实传 int 类型（而非字符串），与 Rust 端的修复配套。
    """

    def test_search_symbols_limit_passed_as_int(self, tmp_path):
        """search_symbols(limit=50) → RPC params["limit"] 是 int 50。"""
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()

        # mock _ensure_remote_snapshot 返回 workspace_id，触发 RPC 路径
        with patch.object(client, '_ensure_remote_snapshot', return_value="ws_test_limit"):
            rpc_mock = MagicMock()
            rpc_mock.call.return_value = []
            client._rpc = rpc_mock

            client.search_symbols("test_query", limit=50, db_path=str(tmp_path / "dummy.db"))

        # 验证 RPC 调用的 params 中 limit 是 int 50
        call_args = rpc_mock.call.call_args
        method = call_args[0][0]
        params = call_args[0][1]
        assert method == "query.search"
        assert params["limit"] == 50
        assert isinstance(params["limit"], int), "limit 应为 int 类型（Python client 默认传 int）"
        assert params["query"] == "test_query"
        assert params["workspace_instance_id"] == "ws_test_limit"

        DaemonClient.reset_instance()

    def test_get_topological_order_limit_passed_as_int(self, tmp_path):
        """get_topological_order(limit=10) → RPC params["limit"] 是 int 10。"""
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()

        with patch.object(client, '_ensure_remote_snapshot', return_value="ws_test_topo"):
            rpc_mock = MagicMock()
            rpc_mock.call.return_value = []
            client._rpc = rpc_mock

            client.get_topological_order(limit=10, db_path=str(tmp_path / "dummy.db"))

        call_args = rpc_mock.call.call_args
        method = call_args[0][0]
        params = call_args[0][1]
        assert method == "query.topological_order"
        assert params["limit"] == 10
        assert isinstance(params["limit"], int)

        DaemonClient.reset_instance()

    def test_get_call_chain_down_max_depth_passed_as_int(self, tmp_path):
        """get_call_chain_down(max_depth=8) → RPC params["max_depth"] 是 int 8。"""
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()

        with patch.object(client, '_ensure_remote_snapshot', return_value="ws_test_chain"):
            rpc_mock = MagicMock()
            rpc_mock.call.return_value = []
            client._rpc = rpc_mock

            client.get_call_chain_down(
                "qualified_name_test", max_depth=8, db_path=str(tmp_path / "dummy.db"),
            )

        call_args = rpc_mock.call.call_args
        method = call_args[0][0]
        params = call_args[0][1]
        assert method == "query.call_chain_down"
        assert params["max_depth"] == 8
        assert isinstance(params["max_depth"], int)
        assert params["qualified_name"] == "qualified_name_test"

        DaemonClient.reset_instance()

    def test_detect_cycles_max_depth_passed_as_int(self, tmp_path):
        """detect_cycles(max_depth=15) → RPC params["max_depth"] 是 int 15。"""
        from callwarden.server.daemon_client import DaemonClient
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()

        with patch.object(client, '_ensure_remote_snapshot', return_value="ws_test_cycles"):
            rpc_mock = MagicMock()
            rpc_mock.call.return_value = []
            client._rpc = rpc_mock

            client.detect_cycles(max_depth=15, db_path=str(tmp_path / "dummy.db"))

        call_args = rpc_mock.call.call_args
        method = call_args[0][0]
        params = call_args[0][1]
        assert method == "query.detect_cycles"
        assert params["max_depth"] == 15
        assert isinstance(params["max_depth"], int)

        DaemonClient.reset_instance()
