"""W4-2（T-1786886251769-22b94ee8-sub-2）：coverage/review 读组 HTTP native 迁移 RPC 测试

覆盖 2 个迁移工具（get_coverage_for_symbol / diff_to_symbol）的 6 问验收
（与 test_git_read_rpc_http.py / test_semgrep_findings_rpc_http.py 同构）：
① workspace_id 绑定：便捷方法经 `_ensure_remote_snapshot` 注入权威
   workspace_instance_id（缺注入 Rust handler 强制 require →
   invalid_params）。
② 结果限定/参数透传：qualified_name / diff_text 原样透传（params 不增不减
   不篡改）。
③ 越界参数 fail-closed：`_ensure_remote_snapshot` 返回 None（注册失败
   边界）时不注入 workspace_instance_id，params 保持原样（Rust 侧 require
   拒绝）。
④ snapshot_not_ready：`_ensure_remote_snapshot` 抛错（未发布 snapshot）
   时异常原样传播，不回退本地 SQL。
⑤ 跨 workspace 隔离：不同 db_path → 不同 workspace_instance_id 注入，
   同一 db_path 幂等复用；Rust 查询按 workspace_id 限定（symbols /
   coverage_data 无 workspace_id 列，经 JOIN file_instances 限定）。
⑥ Python fallback 边界：工具层已 `_route` 化（常量表达式），HTTP/local 分流
   整体下沉到 `route_rpc`；本文件 ⑥ 组（TestToolRouteContract）只锁定工具层
   「下发哪个 RPC、参数逐字透传、op_class=READ_ONLY、失败 fail-closed 且不
   回落本地 get_db」的契约，不再 patch `_get_daemon_client`。

语义差异风险点（记录）：diff_to_symbol 的 change_type 判定保持 Python
先重置 hunk 计数后判定的行为（非文件删除恒为 "modified"）；coverage_pct
用整数精确 round-half-even 复刻 Python round(x, 1)。
"""

from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_summary

DB_A = "/tmp/w4_2_a.db"
DB_B = "/tmp/w4_2_b.db"

# 便捷方法名 → (RPC method, 业务参数，不含 db_path/workspace_instance_id)
CONVENIENCE_CASES = [
    ("get_coverage_for_symbol", "query.coverage_for_symbol",
     {"qualified_name": "src.main:foo"}),
    ("diff_to_symbol", "query.diff_to_symbol",
     {"diff_text": "diff --git a/src/main.py b/src/main.py\n"
                   "--- a/src/main.py\n+++ b/src/main.py\n"
                   "@@ -10,5 +10,6 @@ def foo():\n"
                   "     x = 1\n+    y = 2\n"}),
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
    """便捷方法注入权威 workspace_instance_id，且 db_path 传给 _ensure_remote_snapshot。"""

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

    def test_no_db_path_still_registers_workspace(self):
        """db_path=None 时 _ensure_remote_snapshot(None) 仍执行（仅注册 workspace，跳过 publish）。"""
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-auth-1"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.get_coverage_for_symbol("src.main:foo", db_path=None)
        mock_ensure.assert_called_once_with(None)
        _, params = mock_call.call_args[0]
        assert params["workspace_instance_id"] == "ws-auth-1"


# ============================================================
# ② 结果限定（参数原样透传）
# ============================================================

class TestParamPropagation:
    """qualified_name / diff_text 原样透传。"""

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
        for k, v in params.items():
            assert called_params[k] == v
        assert called_params["workspace_instance_id"] == "ws-1"

    def test_all_methods_do_not_add_unknown_params(self):
        """便捷方法不夹带未声明的业务参数（仅注入 workspace_instance_id）。"""
        client = _make_client()
        for method, _rpc, params in CONVENIENCE_CASES:
            with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                    patch.object(client, "call", return_value={}) as mock_call:
                getattr(client, method)(db_path=DB_A, **params)
            _, called_params = mock_call.call_args[0]
            assert "workspace_instance_id" in called_params
            known = set(params) | {"workspace_instance_id"}
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
        """snapshot 已发布但查询失败 → 远端错误原样传播。"""
        client = _make_client()
        err = DaemonRemoteError("invalid_params", "workspace_instance_id 绑定不一致")
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", side_effect=err):
            with pytest.raises(DaemonRemoteError) as excinfo:
                client.diff_to_symbol("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", db_path=DB_A)
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
            client.get_coverage_for_symbol("a.b", db_path=DB_A)
            client.diff_to_symbol("--- a/x\n+++ b/x\n", db_path=DB_B)

        calls = mock_call.call_args_list
        assert calls[0][0][1]["workspace_instance_id"] == "ws-A"
        assert calls[1][0][1]["workspace_instance_id"] == "ws-B"

    def test_same_db_path_reuses_same_instance_id(self):
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-A"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.get_coverage_for_symbol("a.b", db_path=DB_A)
            client.diff_to_symbol("--- a/x\n+++ b/x\n", db_path=DB_A)

        assert mock_ensure.call_count == 2
        for call in mock_call.call_args_list:
            assert call[0][1]["workspace_instance_id"] == "ws-A"


# ============================================================
# ⑥ 工具层 `_route` 契约（HTTP/local 分流已下沉到 route_rpc）
# ============================================================

class TestToolRouteContract:
    """工具层已纯 `_route` 化：不再直连 `HttpDaemonRpcClient` 便捷方法。

    stale 依据（MCP 工具 `_route` 化）：`server/tools/tools_summary.py:44` 为
    `from ..daemon_client import route_rpc as _route`；get_coverage_for_symbol
    （:122）→ `_route('query.coverage_for_symbol', {...}, 'READ_ONLY')`，
    diff_to_symbol（:336）→ `_route('query.diff_to_symbol', {...}, 'READ_ONLY')`，
    review_readiness（:350）→ `_route('review_readiness', {...}, 'READ_ONLY')`。
    旧用例 patch `tools_summary._get_daemon_client` / `_get_db_path_for_daemon` /
    `is_http_transport_enabled` 并断言客户端便捷方法被调用——这些模块属性虽仍在
    （故 monkeypatch 不报错）但已无调用点，断言恒为 `Called 0 times`；legacy
    本地 db 回退分支也已随 `_route` 化移除（HTTP/local 分流下沉到 route_rpc）。
    """

    DIFF_TEXT = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"

    ROUTE_CASES = [
        ("get_coverage_for_symbol", "query.coverage_for_symbol",
         {"qualified_name": "src.main:foo"},
         {"qualified_name": "src.main:foo"}),
        ("diff_to_symbol", "query.diff_to_symbol",
         {"diff_text": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"},
         {"diff_text": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"}),
        ("review_readiness", "review_readiness",
         {"symbol_hash": "hash-123"},
         {"symbol_hash": "hash-123"}),
    ]

    @pytest.mark.parametrize(
        "tool_name,rpc_method,call_kwargs,expect_params",
        ROUTE_CASES,
        ids=[c[0] for c in ROUTE_CASES],
    )
    def test_summary_tools_route_read_only_rpc(
        self, monkeypatch, tool_name, rpc_method, call_kwargs, expect_params
    ):
        """工具经模块级 `_route` 下发 READ_ONLY RPC，参数逐字透传，不碰本地 db。"""
        seen = {}

        def fake_route(method, params, op_class):
            seen["method"] = method
            seen["params"] = dict(params)
            seen["op"] = op_class
            return {"ok": True}

        monkeypatch.setattr(tools_summary, "_route", fake_route)
        q = _register_tools(tools_summary)
        with patch("callwarden.server.tools.tools_summary.get_db") as mock_db:
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
    def test_summary_tools_fail_closed_no_local_fallback(
        self, monkeypatch, tool_name, rpc_method, call_kwargs, expect_params
    ):
        """`_route` 抛 DaemonRemoteError → 原样传播，绝不回落本地 get_db。"""
        def fake_route(method, params, op_class):
            raise DaemonRemoteError("E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达")

        monkeypatch.setattr(tools_summary, "_route", fake_route)
        q = _register_tools(tools_summary)
        with patch("callwarden.server.tools.tools_summary.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                q[tool_name](**call_kwargs)
            mock_db.assert_not_called()

    def test_runtime_role_is_never_client_side_branched(self, monkeypatch):
        """工具层不做 HTTP/local 分支：无论 transport 如何都走同一 `_route` 契约。

        stale 依据：旧用例 monkeypatch `daemon_client.is_http_transport_enabled` /
        `get_daemon_mode` 后期待工具改走本地 db；`_route` 化后工具是常量表达式，
        分流只发生在 route_rpc 内部（其单测负责），工具层断言不再随 transport 变化。
        """
        calls = []

        def fake_route(method, params, op_class):
            calls.append((method, op_class))
            return {"ok": True}

        monkeypatch.setattr(tools_summary, "_route", fake_route)
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode", lambda: "local"
        )
        q = _register_tools(tools_summary)
        assert q["get_coverage_for_symbol"]("src.main:foo") == {"ok": True}
        assert ("query.coverage_for_symbol", "READ_ONLY") in calls
