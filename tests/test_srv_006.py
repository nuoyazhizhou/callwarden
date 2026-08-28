"""SRV-006 fixture 负矩阵测试（route B thin-client step 2）。

覆盖 task `T-1787323460703-c5f65380` step[2] fixture_negative_matrix：
["success", "invalid", "authority", "unavailable", "restart"]。

设计要点（SRV-006 = `daemon_client` 端口，route B thin-client）：
- Python `server/daemon_client.py` 原有 12 个本地权威符号：_get_db、
  _inject_workspace_id、8 个 _sql_fallback_*（经 _get_db() 直调 CodeGraphDB
  业务 SQL）、call_with_fd（本地 FD 能力判定）、publish_snapshot（本地
  sqlite3 PASSIVE checkpoint）。
- SRV-006 后全部退化为 daemon RPC 薄客户端（`mcp.daemon_client.*`，
  Rust handler `rust_ext/src/daemon/daemon_client_handlers.rs`）；
  _get_db 方法整体移除。
- 接缝与 SRV-005 不同（无模块级统一接缝）：
  * DaemonClient._sql_fallback_* → `self._rpc.call`；
  * UnixDaemonRpcClient.call_with_fd / publish_snapshot → `self.call`；
  * 模块级 _inject_workspace_id → `_get_rpc_client_for_route().call`。
- 错误语义与下沉前对齐（stable errors）：必填参数缺失 → invalid_params；
  无 active workspace → internal_error（fail-closed，禁止静默回退 workspace 1）。
- daemon 不可用时 fail-closed 上抛 DaemonUnavailableError，
  绝不回退本地 SQLite/业务 SQL（no business fallback）。
- `_transport_call_with_fd`（SCM_RIGHTS 物理 FD 发送）为 transport
  bootstrap：客户端必须自持活 socket 与 fd，无法委托 daemon——
  同 SRV-005 先例，不在本矩阵的退役扫描目标内。
- 本测试用内存态 `FakeDaemonClientDaemon` 模拟 daemon handler 行为，
  不依赖真实 daemon 进程，不打开任何 SQLite 数据库。
"""

import ast
from types import SimpleNamespace

import pytest

from callwarden.server.daemon_client import (
    DaemonClient,
    DaemonUnavailableError,
    UnixDaemonRpcClient,
    _inject_workspace_id,
)


class FakeDaemonRpcError(Exception):
    """模拟 daemon 端 DaemonRpcError（带稳定 error code）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FakeDaemonClientDaemon:
    """内存态 daemon_client 权威（对齐 Rust `daemon_client_handlers` 语义）。"""

    def __init__(self):
        self.available: bool = True
        self.active_workspace_id = 7  # None 模拟无 active workspace
        self.calls: list = []

    def __call__(self, method: str, params: dict):
        if not self.available:
            raise DaemonUnavailableError("daemon 不可用（测试模拟）")
        self.calls.append((method, dict(params)))

        if method == "mcp.daemon_client.inject_workspace_id":
            inner = params.get("params")
            if not isinstance(inner, dict):
                raise FakeDaemonRpcError("invalid_params", "缺少字段: params")
            if inner.get("workspace_id"):
                # truthy 短路：已有显式 workspace_id 不再改写
                return {"params": inner, "injected": False}
            if self.active_workspace_id is None:
                raise FakeDaemonRpcError("internal_error", "无 active workspace，禁止回退 workspace 1")
            injected = dict(inner)
            injected["workspace_id"] = self.active_workspace_id
            return {"params": injected, "injected": True}

        if method == "mcp.daemon_client.sql_fallback_get_callers":
            if not params.get("callee_name"):
                raise FakeDaemonRpcError("invalid_params", "缺少字段: callee_name")
            return {"callers": [
                {"caller_name": "alpha", "qualified_name": "mod.alpha"},
            ]}

        if method == "mcp.daemon_client.sql_fallback_get_callees":
            if not params.get("caller_name"):
                raise FakeDaemonRpcError("invalid_params", "缺少字段: caller_name")
            return {"callees": [
                {"callee_name": "beta", "qualified_name": "mod.beta"},
            ]}

        if method == "mcp.daemon_client.sql_fallback_search_symbols":
            if not params.get("query"):
                raise FakeDaemonRpcError("invalid_params", "缺少字段: query")
            return {"symbols": [
                {"qualified_name": "mod.alpha", "kind": params.get("kind") or "function"},
            ]}

        if method == "mcp.daemon_client.sql_fallback_get_symbol":
            qn = params.get("qualified_name")
            if not qn:
                raise FakeDaemonRpcError("invalid_params", "缺少字段: qualified_name")
            if qn == "missing.sym":
                return {"symbol": None}
            return {"symbol": {"qualified_name": qn, "kind": "function",
                                "start_line": 1, "end_line": 5}}

        if method == "mcp.daemon_client.sql_fallback_get_stats":
            return {"stats": {"total_symbols": 3, "total_files": 2}}

        if method == "mcp.daemon_client.sql_fallback_get_topological_order":
            order = ["mod.alpha", "mod.beta"]
            limit = params.get("limit") or 50
            return {"order": order[:limit]}

        if method == "mcp.daemon_client.sql_fallback_get_call_chain_down":
            qn = params.get("qualified_name")
            if not qn:
                raise FakeDaemonRpcError("invalid_params", "缺少字段: qualified_name")
            return {"start": qn, "edges": [[qn, "mod.beta"]], "levels": 1,
                    "max_depth_reached": False, "total_downstream": 1}

        if method == "mcp.daemon_client.sql_fallback_detect_cycles":
            if params.get("max_depth") == 99:
                return {"cycles": [["mod.alpha", "mod.beta", "mod.alpha"]]}
            return {"cycles": []}

        if method == "mcp.daemon_client.call_with_fd":
            return {"supported": params.get("method") != "never_supported",
                    "transport": "scm_rights"}

        if method == "mcp.daemon_client.publish_snapshot":
            if not params.get("workspace_instance_id"):
                raise FakeDaemonRpcError("invalid_params", "缺少字段: workspace_instance_id")
            if not params.get("db_path"):
                raise FakeDaemonRpcError("invalid_params", "缺少字段: db_path")
            return {
                "checkpointed": True,
                "db_path": params["db_path"],
                "workspace_instance_id": params["workspace_instance_id"],
                "build_context_hash": params.get("build_context_hash", ""),
                "transport": "db_path",
            }

        if method == "snapshot.publish":
            return {"published": True, "db_path": params.get("db_path")}

        raise FakeDaemonRpcError("method_not_found", f"未知方法 {method}")


@pytest.fixture
def fake_daemon(monkeypatch):
    """安装内存态 daemon，并把 _get_rpc_client_for_route 接缝指向它。"""
    daemon = FakeDaemonClientDaemon()
    adapter = SimpleNamespace(call=daemon)
    monkeypatch.setattr(
        "callwarden.server.daemon_client._get_rpc_client_for_route",
        lambda: adapter,
    )
    return daemon


@pytest.fixture
def client(fake_daemon):
    """薄客户端 DaemonClient（_rpc 接缝指向内存态 daemon）。"""
    c = DaemonClient.__new__(DaemonClient)
    c._rpc = SimpleNamespace(call=fake_daemon)
    return c


@pytest.fixture
def unix_client(fake_daemon):
    """薄客户端 UnixDaemonRpcClient（call 接缝指向内存态 daemon）。"""
    u = UnixDaemonRpcClient.__new__(UnixDaemonRpcClient)
    u.call = fake_daemon
    return u


# ============================================================
# 1) success
# ============================================================


def test_success_sql_fallback_field_extraction(client, fake_daemon):
    assert client._sql_fallback_get_callers("beta") == [
        {"caller_name": "alpha", "qualified_name": "mod.alpha"},
    ]
    assert client._sql_fallback_get_callees("alpha") == [
        {"callee_name": "beta", "qualified_name": "mod.beta"},
    ]
    assert client._sql_fallback_search_symbols("alp") == [
        {"qualified_name": "mod.alpha", "kind": "function"},
    ]
    assert client._sql_fallback_get_stats() == {"total_symbols": 3, "total_files": 2}
    assert client._sql_fallback_get_topological_order(limit=1) == ["mod.alpha"]
    assert client._sql_fallback_detect_cycles(max_depth=99) == [
        ["mod.alpha", "mod.beta", "mod.alpha"],
    ]


def test_success_get_symbol_optional_semantics(client):
    sym = client._sql_fallback_get_symbol("mod.alpha")
    assert sym["qualified_name"] == "mod.alpha"
    # daemon 对不存在符号返回 {"symbol": null}，薄客户端保持原 Optional 语义
    assert client._sql_fallback_get_symbol("missing.sym") is None


def test_success_chain_down_dict_normalized_to_edges(client):
    edges = client._sql_fallback_get_call_chain_down("mod.alpha", max_depth=3)
    assert edges == [["mod.alpha", "mod.beta"]]


def test_success_inject_workspace_id_truthy_short_circuit(fake_daemon):
    params = {"workspace_id": 3, "foo": "bar"}
    assert _inject_workspace_id(params) is params
    assert fake_daemon.calls == []  # 短路：不打扰 daemon


def test_success_inject_workspace_id_daemon_injects(fake_daemon):
    result = _inject_workspace_id({"foo": "bar"})
    assert result == {"foo": "bar", "workspace_id": 7}
    method, params = fake_daemon.calls[-1]
    assert method == "mcp.daemon_client.inject_workspace_id"
    assert params == {"params": {"foo": "bar"}}  # 嵌套传递


def test_success_publish_snapshot_checkpoint_delegated(unix_client, fake_daemon):
    import os

    result = unix_client.publish_snapshot("ws-inst-1", "rel/x.db", "hash9")
    # 返回值为 snapshot.publish 的 daemon 结果（db_path 为 daemon 权威归一化路径）
    assert result == {"published": True, "db_path": os.path.abspath("rel/x.db")}
    methods = [m for m, _ in fake_daemon.calls]
    assert methods == [
        "mcp.daemon_client.publish_snapshot",  # daemon 权威 checkpoint
        "snapshot.publish",                    # 传输统一 db_path 形式
    ]
    _, pub = fake_daemon.calls[1]
    assert pub["db_path"] == os.path.abspath("rel/x.db")
    assert pub["workspace_instance_id"] == "ws-inst-1"
    assert pub["build_context_hash"] == "hash9"


def test_success_call_with_fd_probe_returns_metadata(unix_client):
    result = unix_client.call_with_fd("query.symbols", {"q": 1}, fd=3)
    assert result == {"supported": True, "transport": "scm_rights"}
    result_no = unix_client.call_with_fd("never_supported", {}, fd=3)
    assert result_no["supported"] is False


# ============================================================
# 2) invalid（stable errors：必填缺失 → invalid_params，薄客户端透传）
# ============================================================


def test_invalid_get_callers_missing_name(client):
    with pytest.raises(FakeDaemonRpcError) as exc:
        client._sql_fallback_get_callers("")
    assert exc.value.code == "invalid_params"


def test_invalid_inject_no_active_workspace(fake_daemon):
    fake_daemon.active_workspace_id = None
    with pytest.raises(FakeDaemonRpcError) as exc:
        _inject_workspace_id({"foo": "bar"})
    assert exc.value.code == "internal_error"


def test_invalid_publish_snapshot_missing_workspace_instance_id(fake_daemon):
    # 薄客户端透传 daemon 参数校验（Rust handler 必填契约）
    adapter = SimpleNamespace(call=fake_daemon)
    with pytest.raises(FakeDaemonRpcError) as exc:
        adapter.call("mcp.daemon_client.publish_snapshot", {"db_path": "/tmp/x.db"})
    assert exc.value.code == "invalid_params"


def test_invalid_unknown_method(fake_daemon):
    with pytest.raises(FakeDaemonRpcError) as exc:
        SimpleNamespace(call=fake_daemon).call("mcp.daemon_client.not_exist", {})
    assert exc.value.code == "method_not_found"


# ============================================================
# 3) authority（权威在 daemon；Python 不再本地 SQLite/业务 SQL）
# ============================================================


def test_authority_all_calls_route_through_daemon(client, unix_client, fake_daemon):
    client._sql_fallback_get_callers("beta")
    client._sql_fallback_get_callees("alpha")
    client._sql_fallback_search_symbols("alp")
    client._sql_fallback_get_symbol("mod.alpha")
    client._sql_fallback_get_stats()
    client._sql_fallback_get_topological_order()
    client._sql_fallback_get_call_chain_down("mod.alpha")
    client._sql_fallback_detect_cycles()
    _inject_workspace_id({"foo": "bar"})
    unix_client.call_with_fd("query.symbols", {}, fd=3)
    unix_client.publish_snapshot("ws-inst-1", "/tmp/x.db")
    methods = [m for m, _ in fake_daemon.calls]
    assert methods == [
        "mcp.daemon_client.sql_fallback_get_callers",
        "mcp.daemon_client.sql_fallback_get_callees",
        "mcp.daemon_client.sql_fallback_search_symbols",
        "mcp.daemon_client.sql_fallback_get_symbol",
        "mcp.daemon_client.sql_fallback_get_stats",
        "mcp.daemon_client.sql_fallback_get_topological_order",
        "mcp.daemon_client.sql_fallback_get_call_chain_down",
        "mcp.daemon_client.sql_fallback_detect_cycles",
        "mcp.daemon_client.inject_workspace_id",
        "mcp.daemon_client.call_with_fd",
        "mcp.daemon_client.publish_snapshot",
        "snapshot.publish",
    ]


def test_authority_params_forwarded_intact(client, fake_daemon):
    client._sql_fallback_get_callers("beta", qualified_name="mod.beta")
    _, params = fake_daemon.calls[-1]
    assert params == {"callee_name": "beta", "qualified_name": "mod.beta"}

    client._sql_fallback_search_symbols("alp", kind="class", limit=5)
    _, params = fake_daemon.calls[-1]
    assert params == {"query": "alp", "kind": "class", "limit": 5}

    client._sql_fallback_get_call_chain_down("mod.alpha", max_depth=4)
    _, params = fake_daemon.calls[-1]
    assert params == {"qualified_name": "mod.alpha", "max_depth": 4}


def test_authority_get_db_removed_and_no_sqlite_module():
    # _get_db 方法整体移除（退役符号之一）
    assert not hasattr(DaemonClient, "_get_db")
    # 模块命名空间不再持有 sqlite3（零 SQLite 打开）
    import callwarden.server.daemon_client as dc
    assert not hasattr(dc, "sqlite3")
    # _transport_call_with_fd 保留（transport bootstrap，非 DB authority）
    assert hasattr(UnixDaemonRpcClient, "_transport_call_with_fd")


# ============================================================
# 4) unavailable（fail-closed 上抛，绝不回退本地 SQLite/业务 SQL）
# ============================================================


def test_unavailable_sql_fallbacks_all_raise(client, fake_daemon):
    fake_daemon.available = False
    probes = [
        lambda: client._sql_fallback_get_callers("beta"),
        lambda: client._sql_fallback_get_callees("alpha"),
        lambda: client._sql_fallback_search_symbols("alp"),
        lambda: client._sql_fallback_get_symbol("mod.alpha"),
        lambda: client._sql_fallback_get_stats(),
        lambda: client._sql_fallback_get_topological_order(),
        lambda: client._sql_fallback_get_call_chain_down("mod.alpha"),
        lambda: client._sql_fallback_detect_cycles(),
    ]
    for probe in probes:
        with pytest.raises(DaemonUnavailableError):
            probe()


def test_unavailable_inject_publish_call_with_fd_raise(unix_client, fake_daemon):
    fake_daemon.available = False
    with pytest.raises(DaemonUnavailableError):
        _inject_workspace_id({"foo": "bar"})
    with pytest.raises(DaemonUnavailableError):
        unix_client.call_with_fd("query.symbols", {}, fd=3)
    with pytest.raises(DaemonUnavailableError):
        unix_client.publish_snapshot("ws-inst-1", "/tmp/x.db")


# ============================================================
# 5) restart（不可用 → 恢复后立即成功，无缓存/状态污染）
# ============================================================


def test_restart_recovers_after_unavailable(client, unix_client, fake_daemon):
    fake_daemon.available = False
    with pytest.raises(DaemonUnavailableError):
        client._sql_fallback_get_stats()
    with pytest.raises(DaemonUnavailableError):
        _inject_workspace_id({"foo": "bar"})
    fake_daemon.available = True
    assert client._sql_fallback_get_stats() == {"total_symbols": 3, "total_files": 2}
    assert _inject_workspace_id({"foo": "bar"})["workspace_id"] == 7
    result = unix_client.call_with_fd("query.symbols", {}, fd=3)
    assert result["supported"] is True


# ============================================================
# 零权威证据：AST 扫描（11 个退役符号函数体不再含本地 SQLite/业务 SQL
# 残留；模块无 sqlite3 导入。_get_db 已整体移除（见 authority 段），
# _transport_call_with_fd 为 transport bootstrap，物理必需且非 DB
# authority，不在扫描目标内。）
# ============================================================


def test_no_sqlite_authority_in_source():
    # 直接取已加载模块的 __file__，确保扫描的是当前生效（worktree）的迁移后源码
    import callwarden.server.daemon_client as dc

    src_path = dc.__file__
    with open(src_path, "r", encoding="utf-8") as f:
        full_src = f.read()
    tree = ast.parse(full_src)

    banned_imports = {"sqlite3"}
    target_funcs = {
        "_sql_fallback_get_callers",
        "_sql_fallback_get_callees",
        "_sql_fallback_search_symbols",
        "_sql_fallback_get_symbol",
        "_sql_fallback_get_stats",
        "_sql_fallback_get_topological_order",
        "_sql_fallback_get_call_chain_down",
        "_sql_fallback_detect_cycles",
        "_inject_workspace_id",
        "call_with_fd",
        "publish_snapshot",
    }
    banned_tokens = {
        "sqlite3",
        "get_db(",
        "_get_active_workspace_id",
        "_mcp_common",
        "PRAGMA ",
        "SELECT ",
        "checkpoint(",
    }

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                if n.name in banned_imports:
                    violations.append("import sqlite3")
        elif isinstance(node, ast.ImportFrom):
            if node.module in banned_imports:
                violations.append("from sqlite3 import")

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in target_funcs:
            # 仅扫描函数体实际代码（排除 docstring，docstring 可合法描述下沉前历史）
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0].value, "value", None), str)
            ):
                body = body[1:]
            code_seg = "\n".join(ast.get_source_segment(full_src, s) or "" for s in body)
            for tok in banned_tokens:
                if tok in code_seg:
                    violations.append(f"{node.name}: contains {tok}")

    assert not violations, f"server/daemon_client.py 仍含 SQLite/业务权威残留: {violations}"
