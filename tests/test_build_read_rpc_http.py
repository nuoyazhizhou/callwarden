"""W3-1（T-1786861820150-bfe5e805）：build 读组 5 工具 HTTP native 迁移 RPC 测试

覆盖 5 个 build 读工具（list_build_contexts / get_build_context /
get_active_build_context / get_resolved_edges / count_resolved_edges）的 6 问验收
（与 test_defect_edit_stats_rpc_http.py 同构）：
① workspace_id 绑定：5 个便捷方法均经 `_ensure_remote_snapshot` 注入权威
   workspace_instance_id（缺注入 Rust handler 强制 require →
   invalid_params，与 W2 各组同构）。
② 结果限定/参数透传：workspace_id / build_context_hash / caller_symbol_id /
   limit 原样透传（params 不增不减不篡改）。
③ 越界参数 fail-closed：`_ensure_remote_snapshot` 返回 None（注册失败
   边界）时不注入 workspace_instance_id，params 保持原样（Rust 侧 require
   拒绝；真实拒绝行为由 .trae-cn/evidence/w3_1_http_verify.py 真实 HTTP
   probe 覆盖）。
④ snapshot_not_ready：`_ensure_remote_snapshot` 抛错（未发布 snapshot）
   时异常原样传播，不回退本地 SQL。
⑤ 跨 workspace 隔离：不同 db_path → 不同 workspace_instance_id 注入，
   同一 db_path 幂等复用。
⑥ Python fallback 边界：工具层已 `_route` 化（常量表达式），HTTP/local 分流
   整体下沉到 `route_rpc`；本文件 ⑥ 组（TestToolRouteContract）只锁定工具
   层「下发哪个 RPC、参数逐字透传（含默认值）、op_class=READ_ONLY、失败
   fail-closed 且不回落本地 get_db」的契约，不再 patch `_get_daemon_client`。
"""

from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_rules

DB_A = "/tmp/w3_1_a.db"
DB_B = "/tmp/w3_1_b.db"

# 便捷方法名 → (RPC method, 业务参数，不含 db_path/workspace_instance_id)
CONVENIENCE_CASES = [
    ("list_build_contexts", "build_context.list",
     {"workspace_id": 101}),
    ("get_build_context", "build_context.get",
     {"workspace_id": 101, "build_context_hash": "abc123"}),
    ("get_active_build_context", "build_context.active",
     {"workspace_id": 101}),
    ("get_resolved_edges", "build_context.resolved_edges",
     {"workspace_id": 101, "build_context_hash": "abc123",
      "caller_symbol_id": 7, "limit": 10}),
    ("count_resolved_edges", "build_context.count_resolved_edges",
     {"workspace_id": 101, "build_context_hash": "abc123"}),
]




def _make_client() -> HttpDaemonRpcClient:
    """绕过 __init__ 构造 HttpDaemonRpcClient 实例（不触发 manifest 发现/网络连接）。"""
    return HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)


def _register_tools(module, mcp=None):
    """注册工具模块到 mock MCP，返回 {name: fn} 字典（与 H4B-N 测试同构）。"""
    if mcp is None:
        mcp = MagicMock()
    registrations = {}

    def tool_capture(name=None):
        def decorator(fn):
            registrations[fn.__name__] = fn
            return fn

        return decorator

    mcp.tool = tool_capture
    module.register(mcp)
    return registrations


# ============================================================
# ① workspace_id 绑定
# ============================================================

class TestWorkspaceIdBinding:
    """5 个便捷方法均注入权威 workspace_instance_id，且 db_path 传给 _ensure_remote_snapshot。"""

    @pytest.mark.parametrize(
        "method,rpc_method,params",
        CONVENIENCE_CASES,
        ids=[c[0] for c in CONVENIENCE_CASES],
    )
    def test_injects_workspace_instance_id(self, method, rpc_method, params):
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-auth-1"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            getattr(client, method)(db_path=DB_A, **params)

        mock_ensure.assert_called_once_with(DB_A)
        assert mock_call.call_count == 1
        called_method, called_params = mock_call.call_args[0]
        assert called_method == rpc_method
        assert called_params["workspace_instance_id"] == "ws-auth-1"
        assert called_params["workspace_id"] == 101

    def test_no_db_path_still_registers_workspace(self):
        """db_path=None 时 _ensure_remote_snapshot(None) 仍执行（仅注册 workspace，跳过 publish）。"""
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-auth-1"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.get_active_build_context(workspace_id=101, db_path=None)
        mock_ensure.assert_called_once_with(None)
        _, params = mock_call.call_args[0]
        assert params["workspace_instance_id"] == "ws-auth-1"
        assert params["workspace_id"] == 101


# ============================================================
# ② 结果限定（参数原样透传）
# ============================================================

class TestParamPropagation:
    """build 读组的 workspace_id / build_context_hash / caller_symbol_id / limit
    原样透传（不增不减不篡改）。"""

    @pytest.mark.parametrize(
        "method,rpc_method,params",
        CONVENIENCE_CASES,
        ids=[c[0] for c in CONVENIENCE_CASES],
    )
    def test_business_params_verbatim(self, method, rpc_method, params):
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            getattr(client, method)(db_path=DB_A, **params)
        _, called_params = mock_call.call_args[0]
        # 注入的 workspace_instance_id 之外，业务参数原样保留
        for k, v in params.items():
            assert called_params[k] == v
        assert called_params["workspace_instance_id"] == "ws-1"

    def test_get_resolved_edges_default_limit_passthrough(self):
        """get_resolved_edges 未传 limit/caller_symbol_id 时默认值（None/50）原样透传。"""
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.get_resolved_edges(
                workspace_id=101, build_context_hash="abc123", db_path=DB_A,
            )
        _, called_params = mock_call.call_args[0]
        assert called_params["caller_symbol_id"] is None
        assert called_params["limit"] == 50

    def test_get_resolved_edges_custom_limit_passthrough(self):
        """get_resolved_edges limit=0 表示不限定（透传 0，Rust 侧 `if limit > 0` 不 LIMIT）。"""
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.get_resolved_edges(
                workspace_id=101, build_context_hash="abc123",
                caller_symbol_id=None, limit=0, db_path=DB_A,
            )
        _, called_params = mock_call.call_args[0]
        assert called_params["limit"] == 0

    def test_all_methods_do_not_add_unknown_params(self):
        """5 个便捷方法均不夹带未声明的业务参数。"""
        client = _make_client()
        known = {"workspace_id", "build_context_hash", "caller_symbol_id",
                 "limit", "workspace_instance_id"}
        for method, _rpc, params in CONVENIENCE_CASES:
            with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                    patch.object(client, "call", return_value={}) as mock_call:
                getattr(client, method)(db_path=DB_A, **params)
            _, called_params = mock_call.call_args[0]
            assert "workspace_instance_id" in called_params
            assert set(called_params) - known == set()


# ============================================================
# ③ 越界参数 fail-closed
# ============================================================

class TestOutOfRangeFailClosed:
    """`_ensure_remote_snapshot` 返回 None 时不注入 workspace_instance_id。

    说明：Python thin client 不包含业务校验（H2 契约：不含业务 SQL、不预判业务
    错误），Rust handler 对缺失 workspace_instance_id 强制 require → invalid_params
    （真实拒绝行为见真实 HTTP probe）。
    """

    def test_missing_workspace_id_means_no_injection_when_snapshot_none(self):
        client = _make_client()
        for method, _rpc, params in CONVENIENCE_CASES:
            with patch.object(client, "_ensure_remote_snapshot", return_value=None), \
                    patch.object(client, "call", return_value={}) as mock_call:
                getattr(client, method)(db_path=DB_A, **params)
            _, called_params = mock_call.call_args[0]
            assert "workspace_instance_id" not in called_params


# ============================================================
# ④ snapshot_not_ready
# ============================================================

class TestSnapshotNotReady:
    """未发布 snapshot 时错误原样传播（fail-closed，无 SQL 回退）。"""

    def test_ensure_snapshot_error_propagates(self):
        client = _make_client()
        err = DaemonRemoteError(
            "snapshot_not_ready", "workspace ws-1 未发布 snapshot"
        )
        for method, _rpc, params in CONVENIENCE_CASES:
            with patch.object(client, "_ensure_remote_snapshot", side_effect=err):
                with pytest.raises(DaemonRemoteError) as excinfo:
                    getattr(client, method)(db_path=DB_A, **params)
            assert excinfo.value.code == "snapshot_not_ready"

    def test_call_error_propagates_after_snapshot(self):
        """snapshot 已发布但查询失败（如跨 workspace 绑定不一致）→ 远端错误原样传播。"""
        client = _make_client()
        err = DaemonRemoteError("invalid_params", "workspace_id 与权威 workspace 不一致")
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", side_effect=err):
            with pytest.raises(DaemonRemoteError) as excinfo:
                client.get_build_context(
                    workspace_id=101, build_context_hash="abc123", db_path=DB_A,
                )
        assert excinfo.value.code == "invalid_params"


# ============================================================
# ⑤ 跨 workspace 隔离
# ============================================================

class TestCrossWorkspaceIsolation:
    """不同 db_path → 不同 workspace_instance_id；同一 db_path 幂等复用。"""

    def test_distinct_db_paths_get_distinct_instance_ids(self):
        client = _make_client()

        def fake_ensure(db_path):
            return {DB_A: "ws-A", DB_B: "ws-B"}.get(db_path)

        with patch.object(client, "_ensure_remote_snapshot", side_effect=fake_ensure), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.list_build_contexts(workspace_id=101, db_path=DB_A)
            client.get_active_build_context(workspace_id=101, db_path=DB_B)

        calls = mock_call.call_args_list
        assert calls[0][0][1]["workspace_instance_id"] == "ws-A"
        assert calls[1][0][1]["workspace_instance_id"] == "ws-B"

    def test_same_db_path_reuses_same_instance_id(self):
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-A"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.list_build_contexts(workspace_id=101, db_path=DB_A)
            client.get_active_build_context(workspace_id=101, db_path=DB_A)

        assert mock_ensure.call_count == 2
        for call in mock_call.call_args_list:
            assert call[0][1]["workspace_instance_id"] == "ws-A"


# ============================================================
# ⑥ 工具层 `_route` 契约（HTTP/local 分流已下沉到 route_rpc）
# ============================================================

class TestToolRouteContract:
    """工具层已纯 `_route` 化：不再直连 `HttpDaemonRpcClient` 便捷方法。

    stale 依据（A 桶 / MCP 工具 `_route` 化）：
    `server/tools/tools_rules.py:39` 为
    `from ..daemon_client import route_rpc as _route`，5 个 build 读工具一律
    `return _route('<rpc method>', {...}, 'READ_ONLY')`（build_context.list /
    .get / .active / .resolved_edges / .count_resolved_edges，tools_rules.py:81
    /:91/:100/:121/:137）。旧用例 patch `tools_rules._get_daemon_client` /
    `_get_db_path_for_daemon` 并断言客户端便捷方法被调用——这些模块属性虽仍在
    （故 monkeypatch 不报错）但已无调用点，于是断言恒为 `Called 0 times`；且
    `is_http_transport_enabled()` 已被 `_route` 化后的常量表达式取代。

    HTTP / local / compat-worker 的分流语义整体下沉到 `route_rpc`，由
    `route_rpc` 自身的单测覆盖；工具层只需锁定「下发哪个 RPC、参数是否逐字透传
    （含默认值）、op_class 是否为 READ_ONLY、失败是否 fail-closed 且不回落
    本地 get_db」。
    """

    ROUTE_CASES = [
        ("list_build_contexts", "build_context.list",
         {"workspace_id": 101},
         {"workspace_id": 101}),
        ("get_build_context", "build_context.get",
         {"workspace_id": 101, "build_context_hash": "abc123"},
         {"workspace_id": 101, "build_context_hash": "abc123"}),
        ("get_active_build_context", "build_context.active",
         {"workspace_id": 101},
         {"workspace_id": 101}),
        ("get_resolved_edges", "build_context.resolved_edges",
         {"workspace_id": 101, "build_context_hash": "abc123"},
         {"workspace_id": 101, "build_context_hash": "abc123",
          "caller_symbol_id": None, "limit": 50}),
        ("count_resolved_edges", "build_context.count_resolved_edges",
         {"workspace_id": 101, "build_context_hash": "abc123"},
         {"workspace_id": 101, "build_context_hash": "abc123"}),
    ]

    @pytest.mark.parametrize(
        "tool_name,rpc_method,call_kwargs,expect_params",
        ROUTE_CASES,
        ids=[c[0] for c in ROUTE_CASES],
    )
    def test_rules_tools_route_read_only_rpc(
        self, monkeypatch, tool_name, rpc_method, call_kwargs, expect_params
    ):
        """工具经模块级 `_route` 下发 READ_ONLY RPC，参数逐字透传，不碰本地 db。"""
        seen = {}

        def fake_route(method, params, op_class):
            seen["method"] = method
            seen["params"] = dict(params)
            seen["op"] = op_class
            return {"ok": True}

        monkeypatch.setattr(tools_rules, "_route", fake_route)
        q = _register_tools(tools_rules)
        with patch("callwarden.server.tools.tools_rules.get_db") as mock_db:
            out = q[tool_name](**call_kwargs)
            mock_db.assert_not_called()
        assert out == {"ok": True}
        assert seen["method"] == rpc_method
        assert seen["op"] == "READ_ONLY"
        assert seen["params"] == expect_params

    @pytest.mark.parametrize(
        "tool_name,rpc_method,call_kwargs,expect_params",
        ROUTE_CASES,
        ids=[c[0] for c in ROUTE_CASES],
    )
    def test_rules_tools_fail_closed_no_local_fallback(
        self, monkeypatch, tool_name, rpc_method, call_kwargs, expect_params
    ):
        """`_route` 抛 DaemonRemoteError → 原样传播，绝不回落本地 get_db。"""
        def fake_route(method, params, op_class):
            raise DaemonRemoteError("E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达")

        monkeypatch.setattr(tools_rules, "_route", fake_route)
        q = _register_tools(tools_rules)
        with patch("callwarden.server.tools.tools_rules.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                q[tool_name](**call_kwargs)
            mock_db.assert_not_called()

    def test_runtime_role_is_never_client_side_branched(self, monkeypatch):
        """工具层不做 HTTP/local 分支：无论 transport 如何都走同一 `_route` 契约。

        stale 依据：旧用例 monkeypatch
        `daemon_client.is_http_transport_enabled` / `get_daemon_mode` 后期待工具
        改走本地 db；`_route` 化后工具是常量表达式，分流只发生在 `route_rpc`
        内部（其单测负责），工具层断言不再随 transport 变化。
        """
        calls = []

        def fake_route(method, params, op_class):
            calls.append((method, op_class))
            return {"ok": True}

        monkeypatch.setattr(tools_rules, "_route", fake_route)
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode", lambda: "local"
        )
        q = _register_tools(tools_rules)
        assert q["list_build_contexts"](workspace_id=101) == {"ok": True}
        assert ("build_context.list", "READ_ONLY") in calls
