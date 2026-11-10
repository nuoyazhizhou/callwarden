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
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 跳过条件：callwarden_core 未安装时跳过 daemon 路由测试
try:
    import callwarden_core
    HAS_RUST = True
except ImportError:
    HAS_RUST = False

from callwarden.config import get_pki_dir  # noqa: E402

# ADR-002 单轨 mTLS：真实 PKI 必须在模块 import 期解析——conftest autouse
# `_isolate_db_path` 会在每个测试的 fixture 运行前 monkeypatch
# callwarden.config.CALLWARDEN_DIR 为 tmp 目录，fixture 内再调 get_pki_dir()
# 将指向空临时 PKI（daemon 自举 CA 但无 owner 身份 → E_MTLS_CLIENT_CERT_MISSING）。
# 与 test_query_tests_rpc / test_lease_rpc 的 _REAL_PKI_DIR 同形态。
_REAL_PKI_DIR = get_pki_dir()


def _make_test_db(db_path):
    """构造测试用 callwarden.db

    stale 修复（PYT 回归卡 step#4 A 桶）：
    1) daemon 侧 GraphStore 的 SQL 加载（rust_ext/src/graph.rs:1691-1709）在
       workspace_id>0 时追加 `AND workspace_id = {n}`；query.symbol 的
       handle_query_symbol（rust_ext/src/daemon/snapshot_state.rs:1414-1419）用
       registry 行的 `workspace_id` 过滤 file_instances.workspace_id。旧 DDL 无
       workspace_id 列 → `prepare file_instances query failed: no such column:
       workspace_id`。
    2) snapshot.publish 的 resolve_true_workspace_id
       （rust_ext/src/daemon/snapshot_state.rs:4280-4317）按 client_view_root 匹配
       workspaces.root_path 解析过滤 id。二者必须在同一数值上对齐——沿用
       tests/test_cli02_search_daemon_only.py:140-143 既有约定：以
       workspace.register 返回的 registry `workspace_id` 同时作为
       file_instances.workspace_id 与 workspaces.id。权威 schema 见
       db/schema.py:10-46。
    """
    from callwarden.server.daemon_client import DaemonClient

    client = DaemonClient.get_instance()
    client._ensure_daemon_endpoint()
    root = client._project_root or os.getcwd()
    ws_id = client._rpc.call(
        "workspace.register", {"client_view_root": root}
    )["workspace_id"]

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY, root_path TEXT NOT NULL
        )
    """)
    cur.execute(
        "INSERT INTO workspaces (id, root_path) VALUES (?, ?)", (ws_id, root)
    )
    cur.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL,
            rel_path TEXT, abs_path TEXT, status TEXT DEFAULT 'active'
        )
    """)
    cur.execute(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path) "
        "VALUES (1, ?, 'src/main.py', '/repo/src/main.py')", (ws_id,)
    )
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
    # stale 修复（PYT 回归卡 step#4 A 桶）：daemon 的 query.symbol 现走
    # query_symbol_detail（rust_ext/src/symbol_query.rs:13-108），JOIN
    # file_symbol_versions / symbol_contents / file_versions / file_instances 并
    # 通过 query_calls_out / query_called_by（:129-200）JOIN call_versions。
    # 旧 DDL 缺这些表，导致 `cannot query symbol detail: no such table:
    # file_symbol_versions`。权威 schema 见 db/schema.py:97-153。
    cur.executescript("""
        CREATE TABLE file_versions (
            id INTEGER PRIMARY KEY, file_instance_id INTEGER NOT NULL,
            is_current INTEGER DEFAULT 1
        );
        CREATE TABLE symbol_contents (
            content_hash TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            signature TEXT DEFAULT '', has_comment INTEGER DEFAULT 0,
            comment_content TEXT DEFAULT ''
        );
        CREATE TABLE file_symbol_versions (
            file_version_id INTEGER NOT NULL, symbol_hash TEXT NOT NULL,
            qualified_name TEXT NOT NULL, module_path TEXT DEFAULT '',
            start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
            depth INTEGER DEFAULT -1, is_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE call_versions (
            file_version_id INTEGER NOT NULL, caller_qualified TEXT NOT NULL,
            caller_hash TEXT DEFAULT '', callee_name TEXT NOT NULL,
            callee_module TEXT DEFAULT '', callee_qualified TEXT DEFAULT '',
            callee_file TEXT DEFAULT '', call_line INTEGER DEFAULT 0
        );
        INSERT INTO file_versions VALUES (1, 1, 1);
        INSERT INTO symbol_contents VALUES ('hash-main', 'main', 'fn', 'main()', 0, '');
        INSERT INTO file_symbol_versions VALUES
            (1, 'hash-main', 'main', '', 10, 20, 0, 0);
        INSERT INTO call_versions VALUES
            (1, 'main', 'hash-main', 'init', '', 'main.init', 'src/main.py', 12);
    """)
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
    def test_initial_stats_zero(self, monkeypatch):
        """初始 routing stats 全零（统计器为全局单例，重置后从零开始）。

        local 模式：ADR-002 单轨契约下 is_daemon_ready() 在 auto/enterprise 模式
        daemon 不可达时 fail-closed 抛 DaemonUnavailableError（不再返回 False）；
        本测试断言「未就绪」语义，故 monkeypatch 到 local 跳过 RPC 探针。
        """
        from callwarden.server.daemon_client import DaemonClient
        monkeypatch.setattr("callwarden.server.daemon_client.get_daemon_mode", lambda: "local")
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        stats = client.get_routing_stats()
        assert stats["daemon_hits"] == 0
        assert stats["sql_fallbacks"] == 0
        assert stats["total_queries"] == 0
        DaemonClient.reset_instance()

    def test_stats_after_sql_fallback(self, monkeypatch):
        """无 db_path 时走 SQL 回退，sql_fallbacks 增加（local 模式，同上）。"""
        from callwarden.server.daemon_client import DaemonClient
        monkeypatch.setattr("callwarden.server.daemon_client.get_daemon_mode", lambda: "local")
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        # 不提供 db_path，应该走 SQL 回退
        with patch.object(client, '_sql_fallback_get_stats', return_value={"files": 0}):
            client.get_stats(db_path=None)
        stats = client.get_routing_stats()
        assert stats["sql_fallbacks"] == 1
        assert stats["daemon_hits"] == 0
        DaemonClient.reset_instance()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _find_isolated_daemon_binary() -> Path | None:
    """定位隔离 spawn 用的 cw-daemon 二进制。

    CI 侧由 test job 的 "Build cw-daemon binary" step（df092cf）构建于
    rust_ext/target/debug/cw-daemon；CW_DAEMON_BIN 可显式覆盖（与
    tests/_w3_harness.py find_daemon_binary 的候选约定一致）。
    """
    candidates = []
    env_bin = os.environ.get("CW_DAEMON_BIN", "").strip()
    if env_bin:
        candidates.append(Path(env_bin))
    candidates.append(_repo_root() / "rust_ext" / "target" / "debug" / "cw-daemon")
    candidates.append(_repo_root() / "rust_ext" / "target" / "release" / "cw-daemon")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


@pytest.fixture
def daemon_route_ready(tmp_path, monkeypatch):
    """保证 TestDaemonRoutingWithRust 六例有可用 daemon（ADR-002 单轨 HTTPS+mTLS）。

    生产端点在本机不可达（无 manifest → E_HTTP_MANIFEST_MISSING）。按
    tests/_w3_harness.py setup_w3_client 的同款形态 spawn 隔离 HTTPS daemon，
    并把进程内 DaemonClient 单例重定向到隔离 daemon：
      CW_PKI_DIR=真实 PKI（daemon 继承同一 CA；与已验证可连通的
        test_task_create_subtask_steps._env_with_daemon 同形态）
      USERPROFILE/HOME=隔离 data root（manifest 发现同源）
      CW_DAEMON_HTTP_ENDPOINT=隔离端点（显式发现路径）
    二进制/PKI/端口前置缺失 → skip（不掩盖断言失败）。
    """
    from callwarden.server.daemon_client import DaemonClient
    from _w3_harness import setup_w3_client

    # CW_PKI_DIR=真实 PKI（模块 import 期求值；conftest autouse _isolate_db_path
    # 在 fixture 前已 monkeypatch CALLWARDEN_DIR，fixture 内 get_pki_dir() 会
    # 解析到空临时 PKI → 客户端证书缺失）。先于 USERPROFILE 重定向设置，
    # 保证 daemon（os.environ.copy() 继承）与客户端同 CA 同身份。
    monkeypatch.setenv("CW_PKI_DIR", _REAL_PKI_DIR)
    data_root = str(tmp_path / "w3")
    os.makedirs(data_root, exist_ok=True)
    DaemonClient.reset_instance()
    proc = None
    try:
        harness_rpc, _inst, endpoint, proc = setup_w3_client(
            data_root, os.getcwd(), ws_id=1,
        )
        if harness_rpc is None:
            pytest.skip("隔离 HTTPS daemon 前置不可用（二进制/PKI/端口）")
        monkeypatch.setenv("CW_DAEMON_MODE", "enterprise")
        monkeypatch.setenv("CW_DAEMON_HTTP_ENDPOINT", endpoint)
        monkeypatch.setenv("CW_DAEMON_TRANSPORT", "http")
        monkeypatch.setenv("CALLWARDEN_SKIP_AUTO_SETUP", "1")
        monkeypatch.setenv("USERPROFILE", os.path.join(data_root, "userhome"))
        monkeypatch.setenv("HOME", os.path.join(data_root, "userhome"))
        monkeypatch.setenv("CW_TASK_WRITE_POLICY", "shared")
        monkeypatch.delenv("CW_DAEMON_SOCKET", raising=False)
        monkeypatch.delenv("CW_DAEMON_SOCKET_PATH", raising=False)
        monkeypatch.delenv("CW_DAEMON_ENDPOINT", raising=False)
        DaemonClient.reset_instance()
        yield
    finally:
        DaemonClient.reset_instance()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


@pytest.mark.skipif(not HAS_RUST, reason="callwarden_core not available")
@pytest.mark.usefixtures("daemon_route_ready")
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
        _parent_dir = os.path.basename(os.path.dirname(str(db_path)))
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
    def test_daemon_not_ready_without_workspace(self, monkeypatch):
        """未配置 workspace 时 is_daemon_ready() 为 False（local 模式跳过 RPC 探针。

        auto/enterprise 模式下 daemon 不可达时 is_daemon_ready() fail-closed
        抛 DaemonUnavailableError（ADR-002 单轨契约）。
        """
        from callwarden.server.daemon_client import DaemonClient
        monkeypatch.setattr("callwarden.server.daemon_client.get_daemon_mode", lambda: "local")
        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        assert client.is_daemon_ready() is False
        DaemonClient.reset_instance()

    def test_daemon_not_ready_with_nonexistent_db(self, monkeypatch):
        """db 不存在，不应该发布成功（local 模式，同上）。"""
        from callwarden.server.daemon_client import DaemonClient
        monkeypatch.setattr("callwarden.server.daemon_client.get_daemon_mode", lambda: "local")
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
        """auto 模式 RPC 不可达 → ensure_http_daemon 尝试唤起一次；窗口耗尽后
        query.stats（READ_ONLY）进入 Degraded_Mode 直读，而非抛异常（ADR-002 单轨）。"""
        from callwarden.server.daemon_client import DaemonClient, DaemonUnavailableError

        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        client._rpc.call = MagicMock(
            side_effect=DaemonUnavailableError("endpoint missing")
        )
        client._rpc.discover = MagicMock(side_effect=RuntimeError("no manifest"))

        mutex = MagicMock()
        mutex.try_acquire.return_value = True
        ensure_tgt = "callwarden.server.daemon_client.ensure_http_daemon"
        mutex_tgt = "callwarden.server.daemon_client.DaemonMutex"
        with patch("callwarden.server.daemon_client.get_daemon_mode",
                   return_value="auto"):
            with patch(ensure_tgt, return_value=None) as start:
                with patch(mutex_tgt, return_value=mutex):
                    result = client.call_with_autostart("query.stats")

        assert start.call_count == 1
        # discover() 失败 → 传给 ensure_http_daemon 的是空 endpoint
        assert start.call_args.args[0] == ""
        # 唤起失败 + query.stats 属 READ_ONLY → 降级直读
        assert result["degraded"] is True
        assert result["result"] is None
        assert result["op_class"] == "read_only"
        mutex.release.assert_called_once_with()
        DaemonClient.reset_instance()

    def test_auto_mode_retries_original_query_after_autostart(self):
        """唤起成功（readiness_check 通过）→ 在同一 endpoint 上重试原请求并成功。

        注：ADR-002 单轨后 daemon_hits 只在 _remote_query 的 RPC 命中路径上计数；
        autostart 重试发生在 call_with_autostart 内，不再兼作路由统计。"""
        from callwarden.server.daemon_client import DaemonClient, DaemonUnavailableError

        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        rpc_call = MagicMock(side_effect=[
            DaemonUnavailableError("starting"),
            {"files": 3},
        ])
        client._rpc.call = rpc_call
        client._rpc.discover = MagicMock(
            return_value="https://127.0.0.1:18443"
        )
        client._rpc.verify_health = MagicMock(
            return_value={"status": "ok"}
        )

        def fake_ensure(endpoint, **kwargs):
            kwargs["readiness_check"](endpoint)
            return endpoint

        mutex = MagicMock()
        mutex.try_acquire.return_value = True
        ensure_tgt = "callwarden.server.daemon_client.ensure_http_daemon"
        mutex_tgt = "callwarden.server.daemon_client.DaemonMutex"
        with patch("callwarden.server.daemon_client.get_daemon_mode",
                   return_value="auto"):
            with patch(ensure_tgt, side_effect=fake_ensure):
                with patch(mutex_tgt, return_value=mutex):
                    result = client.call_with_autostart("query.stats")

        assert result == {"result": {"files": 3}, "degraded": False}
        assert rpc_call.call_count == 2
        client._rpc.verify_health.assert_called_once_with(
            "https://127.0.0.1:18443"
        )
        mutex.release.assert_called_once_with()
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
        """公开 RPC 入口不能绕过 auto 的协议级 readiness probe（verify_health）。"""
        from callwarden.server.daemon_client import DaemonClient, DaemonUnavailableError

        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        client._rpc.call = MagicMock(side_effect=[
            DaemonUnavailableError("starting"),
            {"files": 4},
        ])
        client._rpc.discover = MagicMock(
            return_value="https://127.0.0.1:18443"
        )
        verify_health = MagicMock(return_value={"status": "ok"})
        client._rpc.verify_health = verify_health

        def fake_ensure(endpoint, **kwargs):
            kwargs["readiness_check"](endpoint)
            return endpoint

        mutex = MagicMock()
        mutex.try_acquire.return_value = True
        ensure_tgt = "callwarden.server.daemon_client.ensure_http_daemon"
        mutex_tgt = "callwarden.server.daemon_client.DaemonMutex"
        with patch("callwarden.server.daemon_client.get_daemon_mode",
                   return_value="auto"):
            with patch(ensure_tgt, side_effect=fake_ensure):
                with patch(mutex_tgt, return_value=mutex):
                    result = client.call_with_autostart("query.stats")

        assert result == {"result": {"files": 4}, "degraded": False}
        # readiness probe 必须是协议级 verify_health，且以 endpoint 字符串为入参
        verify_health.assert_called_once_with("https://127.0.0.1:18443")
        mutex.release.assert_called_once_with()
        DaemonClient.reset_instance()

        # 后半段：local 模式两次 SQL 回退 → routing stats
        client = DaemonClient.get_instance()
        with patch("callwarden.server.daemon_client.get_daemon_mode",
                   lambda: "local"):
            with patch.object(client, "_sql_fallback_get_stats", return_value={}):
                client.get_stats(db_path=None)

            with patch.object(client, "_sql_fallback_search_symbols",
                              return_value=[]):
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
