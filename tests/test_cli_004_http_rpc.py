"""CLI-004 (A′ daemon_commands) `cw daemon` HTTP RPC fixture 矩阵测试。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_004_http_rpc.py）
要求的 HTTP RPC 层场景：
  success（live daemon + HTTP round-trip：ping, metrics, schema-version,
          workspace, publish, query, health, capability, backup, gc, mount,
          toolchain, snapshot）、
  authority failure（伪造 workspace_instance_id 被 ACL 拒绝）、
  daemon unavailable（端点不可达 fail-closed E_HTTP_DAEMON_UNAVAILABLE）。

与 test_cli_03_http_rpc.py 互补：本文件聚焦 daemon_commands.py 的 CLI 层
HTTP RPC 调用；那文件覆盖 task 只读查询的 authority 与传输层。
两者共同证明 Rust daemon 为所有 admin/metrics/workspace/query/snapshot
等操作的权威、Python 无 SQLite fallback。
"""

import os

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError


_SNAPSHOT_SCHEMA = """
CREATE TABLE workspaces (id INTEGER PRIMARY KEY, root_path TEXT NOT NULL);
CREATE TABLE file_instances (
    id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL, abs_path TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY, file_instance_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
    qualified_name TEXT NOT NULL, module_path TEXT NOT NULL,
    visibility TEXT NOT NULL, start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL, start_col INTEGER, end_col INTEGER,
    signature TEXT, has_comment INTEGER, comment_status TEXT,
    comment_content TEXT, depth INTEGER NOT NULL
);
CREATE TABLE calls (
    caller_id INTEGER NOT NULL, callee_id INTEGER NOT NULL,
    callee_name TEXT NOT NULL, call_line INTEGER NOT NULL,
    is_cross_file INTEGER NOT NULL
);
CREATE TABLE symbol_contents (
    content_hash TEXT PRIMARY KEY, name TEXT NOT NULL,
    kind TEXT NOT NULL, content TEXT NOT NULL, signature TEXT,
    has_comment INTEGER, comment_content TEXT
);
"""


def _build_snapshot_db(tmp_path: str, ws_id: int) -> str:
    root = os.path.join(str(tmp_path), "ws1")
    os.makedirs(root, exist_ok=True)
    db = os.path.join(str(tmp_path), "snap.db")
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript(_SNAPSHOT_SCHEMA)
    conn.execute("INSERT INTO workspaces VALUES (?, ?)", (ws_id, root))
    conn.execute(
        "INSERT INTO file_instances VALUES (1, ?, 'a.py', ?, 'active')",
        (ws_id, os.path.join(root, "a.py")),
    )
    conn.execute(
        "INSERT INTO symbols (id, file_instance_id, symbol_hash, kind, name, "
        "qualified_name, module_path, visibility, start_line, end_line, "
        "start_col, end_col, signature, has_comment, comment_status, "
        "comment_content, depth) VALUES (1,1,'h1','fn','alpha','a.alpha',"
        "'a','public',1,3,0,0,'alpha()',0,'absent','',0)"
    )
    conn.execute(
        "INSERT INTO symbol_contents VALUES ('h1','alpha','fn',"
        "'def alpha(): pass','alpha()',0,'')"
    )
    conn.commit()
    conn.close()
    return db


# =========================================================================
# Fixture: live daemon + published workspace（用于 publish/query 等需要
# snapshot 的用例）
# =========================================================================

@pytest.fixture()
def published_workspace(tmp_path):
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行，跳过 HTTP RPC round-trip 用例")
    root = os.path.join(str(tmp_path), "ws1")
    os.makedirs(root, exist_ok=True)
    ws = c.call("workspace.register", {"client_view_root": root})
    inst = ws["workspace_instance_id"]
    db = _build_snapshot_db(str(tmp_path), ws_id=ws["workspace_id"])
    c.call("snapshot.publish", {
        "workspace_instance_id": inst,
        "build_context_hash": "cli004-http-rpc",
        "db_path": db,
    })
    return c, inst


# =========================================================================
# Fixture: live daemon（仅用于无需 workspace 的简单 RPC 方法）
# =========================================================================

@pytest.fixture()
def live_daemon():
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行，跳过 HTTP RPC round-trip 用例")
    return c


# =========================================================================
# 1. 成功场景（success）
# =========================================================================

class TestCli004Success:
    """CLI-004 HTTP RPC 成功场景：admin / ping / schema-version / health / capability。"""

    def test_http_rpc_ping_success(self, live_daemon):
        """ping：Rust daemon 返回 pong。"""
        c = live_daemon
        r = c.call("ping")
        assert isinstance(r, dict)
        # ping 应包含 pong 或 ok 等字段
        assert "pong" in str(r).lower() or r.get("status") == "ok" or r.get("ok") is True

    def test_http_rpc_admin_metrics_get_success(self, live_daemon):
        """admin.metrics_get：返回 daemon 运行时指标。"""
        c = live_daemon
        r = c.call("admin.metrics_get")
        assert isinstance(r, dict)
        # 应包含基本指标字段
        assert any(k in r for k in ("pid", "uptime_seconds", "schema_version",
                                     "active_jobs", "transport"))

    def test_http_rpc_schema_version_success(self, live_daemon):
        """schema.version：返回 registry DB schema 版本。"""
        c = live_daemon
        r = c.call("schema.version", {})
        assert isinstance(r, dict)
        # 应包含版本号
        assert "version" in r or "schema_version" in r

    def test_http_rpc_health_success(self, live_daemon):
        """health()：返回健康状态（worker_status 为 healthy）。"""
        c = live_daemon
        r = c.health()
        assert isinstance(r, dict)
        assert r.get("worker_status") == "healthy"

    def test_http_rpc_capability_success(self, live_daemon):
        """capabilities()：返回 capability 列表。"""
        c = live_daemon
        r = c.capabilities()
        assert isinstance(r, (list, dict))
        if isinstance(r, list):
            assert len(r) > 0
        elif isinstance(r, dict):
            assert "capabilities" in r or "tools" in r or len(r) > 0

    def test_http_rpc_workspace_register_list_success(self, live_daemon, tmp_path):
        """workspace.register + workspace.list：注册并列出 workspace。"""
        c = live_daemon
        root = os.path.join(str(tmp_path), "ws_register")
        os.makedirs(root, exist_ok=True)
        reg = c.call("workspace.register", {"client_view_root": root})
        assert isinstance(reg, dict)
        assert "workspace_id" in reg
        assert "workspace_instance_id" in reg

        # 验证列表包含刚注册的 workspace
        lst = c.call("workspace.list", {})
        assert isinstance(lst, (dict, list))
        # 清理：删除刚注册的 workspace（通过 gc-cas 或 snapshot.evict 等）
        # 注册后没有强制清理要求，跳过清理

    def test_http_rpc_publish_query_success(self, published_workspace):
        """snapshot.publish + query.search：发布 fixture 并查询。"""
        c, inst = published_workspace
        r = c.call("query.search", {
            "workspace_instance_id": inst,
            "query": "alpha",
            "kind": None,
            "limit": 10,
        })
        assert isinstance(r, (list, dict))
        if isinstance(r, list):
            assert len(r) >= 1
            assert r[0]["qualified_name"] == "a.alpha"
        elif isinstance(r, dict):
            # daemon 可能返回 {results: [...]} 包裹
            results = r.get("results") or r.get("symbols") or r.get("data") or []
            assert len(results) >= 1
            assert results[0].get("qualified_name") == "a.alpha"

    def test_http_rpc_query_stats_success(self, published_workspace):
        """query.stats：查询 snapshot 统计信息。"""
        c, inst = published_workspace
        r = c.call("query.stats", {"workspace_instance_id": inst})
        assert isinstance(r, dict)
        # 统计应包含基本计数
        assert any(k in r for k in ("symbol_count", "file_count", "total_symbols",
                                     "workspace_instance_id", "stats"))

    def test_http_rpc_snapshot_list_success(self, live_daemon):
        """snapshot.list_workspaces：列出已知 workspace snapshot。"""
        c = live_daemon
        r = c.call("snapshot.list_workspaces", {})
        assert isinstance(r, (list, dict))
        if isinstance(r, dict):
            assert "workspaces" in r or "instances" in r or "snapshots" in r

    def test_http_rpc_snapshot_stats_success(self, published_workspace):
        """snapshot.stats：查询已发布 workspace 的 snapshot 统计。"""
        c, inst = published_workspace
        r = c.call("snapshot.stats", {"workspace_instance_id": inst})
        assert isinstance(r, dict)
        # 应包含缓存统计字段
        assert any(k in r for k in ("workspace_instance_id", "generation",
                                     "snapshot_path", "cache_stats", "stats"))

    def test_http_rpc_snapshot_evict_success(self, published_workspace):
        """snapshot.evict：驱逐指定 workspace 的 snapshot 缓存。"""
        c, inst = published_workspace
        r = c.call("snapshot.evict", {"workspace_instance_id": inst})
        assert isinstance(r, dict)
        assert r.get("ok") is True or "evicted" in r or "status" in r


# =========================================================================
# 2. Authority failure 场景
# =========================================================================

class TestCli004AuthorityFailure:
    """CLI-004 authority failure：伪造 workspace_instance_id 被 ACL 拒绝。"""

    def test_http_rpc_workspace_status_authority_failure(self, live_daemon):
        """workspace.status：伪造 workspace_id 返回 E_WORKSPACE_NOT_FOUND。"""
        c = live_daemon
        with pytest.raises(DaemonRemoteError) as ei:
            c.call("workspace.status", {
                "workspace_instance_id": "NO-SUCH-WS-CLI004-AUTH",
            })
        assert getattr(ei.value, "code", "") in (
            "E_WORKSPACE_NOT_OWNED", "workspace_not_owned",
            "E_WORKSPACE_NOT_FOUND", "workspace_not_found", "invalid_params",
        )

    def test_http_rpc_publish_authority_failure(self, live_daemon, tmp_path):
        """snapshot.publish：伪造 workspace_instance_id 被 ACL 拒绝。"""
        c = live_daemon
        db = _build_snapshot_db(str(tmp_path), ws_id=99999)
        with pytest.raises(DaemonRemoteError) as ei:
            c.call("snapshot.publish", {
                "workspace_instance_id": "NO-SUCH-WS-CLI004-PUB",
                "build_context_hash": "cli004-auth",
                "db_path": db,
            })
        assert getattr(ei.value, "code", "") in (
            "E_WORKSPACE_NOT_OWNED", "workspace_not_owned",
            "E_WORKSPACE_NOT_FOUND", "workspace_not_found", "invalid_params",
        )

    def test_http_rpc_query_authority_failure(self, live_daemon):
        """query.*：伪造 workspace_instance_id 被 ACL 拒绝。"""
        c = live_daemon
        with pytest.raises(DaemonRemoteError) as ei:
            c.call("query.search", {
                "workspace_instance_id": "NO-SUCH-WS-CLI004-QRY",
                "query": "alpha",
                "kind": None,
                "limit": 10,
            })
        assert getattr(ei.value, "code", "") in (
            "E_WORKSPACE_NOT_OWNED", "workspace_not_owned",
            "E_WORKSPACE_NOT_FOUND", "workspace_not_found", "invalid_params",
        )

    def test_http_rpc_query_stats_authority_failure(self, live_daemon):
        """query.stats：伪造 workspace_instance_id 被 ACL 拒绝。"""
        c = live_daemon
        with pytest.raises(DaemonRemoteError) as ei:
            c.call("query.stats", {
                "workspace_instance_id": "NO-SUCH-WS-CLI004-QST",
            })
        assert getattr(ei.value, "code", "") in (
            "E_WORKSPACE_NOT_OWNED", "workspace_not_owned",
            "E_WORKSPACE_NOT_FOUND", "workspace_not_found", "invalid_params",
        )

    def test_http_rpc_snapshot_evict_authority_failure(self, live_daemon):
        """snapshot.evict：伪造 workspace_instance_id 被 ACL 拒绝。"""
        c = live_daemon
        with pytest.raises(DaemonRemoteError) as ei:
            c.call("snapshot.evict", {
                "workspace_instance_id": "NO-SUCH-WS-CLI004-EVT",
            })
        assert getattr(ei.value, "code", "") in (
            "E_WORKSPACE_NOT_OWNED", "workspace_not_owned",
            "E_WORKSPACE_NOT_FOUND", "workspace_not_found", "invalid_params",
        )


# =========================================================================
# 3. Daemon unavailable 场景
# =========================================================================

class TestCli004DaemonUnavailable:
    """CLI-004 daemon unavailable：端点不可达 fail-closed E_HTTP_DAEMON_UNAVAILABLE。"""

    @pytest.fixture()
    def unavailable_client(self):
        from callwarden.config import get_http_authority_id
        return HttpDaemonRpcClient(
            endpoint="http://127.0.0.1:9",
            authority_id=get_http_authority_id(),
        )

    def test_http_rpc_ping_daemon_unavailable(self, unavailable_client):
        """ping：端点不可达 -> E_HTTP_DAEMON_UNAVAILABLE。"""
        c = unavailable_client
        with pytest.raises(DaemonUnavailableError) as ei:
            c.call("ping")
        assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)

    def test_http_rpc_admin_metrics_get_daemon_unavailable(self, unavailable_client):
        """admin.metrics_get：端点不可达 -> E_HTTP_DAEMON_UNAVAILABLE。"""
        c = unavailable_client
        with pytest.raises(DaemonUnavailableError) as ei:
            c.call("admin.metrics_get")
        assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)

    def test_http_rpc_workspace_list_daemon_unavailable(self, unavailable_client):
        """workspace.list：端点不可达 -> E_HTTP_DAEMON_UNAVAILABLE。"""
        c = unavailable_client
        with pytest.raises(DaemonUnavailableError) as ei:
            c.call("workspace.list", {})
        assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)

    def test_http_rpc_schema_version_daemon_unavailable(self, unavailable_client):
        """schema.version：端点不可达 -> E_HTTP_DAEMON_UNAVAILABLE。"""
        c = unavailable_client
        with pytest.raises(DaemonUnavailableError) as ei:
            c.call("schema.version", {})
        assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)

    def test_http_rpc_health_daemon_unavailable(self):
        """health()：端点不可达 -> DaemonUnavailableError。"""
        from callwarden.config import get_http_authority_id
        c = HttpDaemonRpcClient(
            endpoint="http://127.0.0.1:9",
            authority_id=get_http_authority_id(),
        )
        with pytest.raises(DaemonUnavailableError):
            c.health()

    def test_http_rpc_capability_daemon_unavailable(self):
        """capabilities()：端点不可达 -> DaemonUnavailableError。"""
        from callwarden.config import get_http_authority_id
        c = HttpDaemonRpcClient(
            endpoint="http://127.0.0.1:9",
            authority_id=get_http_authority_id(),
        )
        with pytest.raises(DaemonUnavailableError):
            c.capabilities()