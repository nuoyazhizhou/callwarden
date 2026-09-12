"""W4-3（T-1786886251769-22b94ee8-sub-3）：defect 读组 HTTP native 迁移 RPC 测试

覆盖 5 个迁移工具（defect_correlation / churn_analysis / defect_search /
defect_suggest_fix / get_defect_correlation）的 6 问验收
（与 test_coverage_review_rpc_http.py / test_git_read_rpc_http.py 同构）：
① workspace_id 绑定：便捷方法经 `_ensure_remote_snapshot` 注入权威
   workspace_instance_id（缺注入 Rust handler 强制 require →
   invalid_params）。
② 结果限定/参数透传：业务参数原样透传（params 不增不减不篡改）。
③ 越界参数 fail-closed：`_ensure_remote_snapshot` 返回 None（注册失败
   边界）时不注入 workspace_instance_id，params 保持原样（Rust 侧 require
   拒绝）。
④ snapshot_not_ready：`_ensure_remote_snapshot` 抛错（未发布 snapshot）
   时异常原样传播，不回退本地 SQL。
⑤ 跨 workspace 隔离：不同 db_path → 不同 workspace_instance_id 注入，
   同一 db_path 幂等复用；Rust 查询按 workspace_id 限定（defect_correlation
   / get_defect_correlation / churn_analysis 经 JOIN file_instances 限定；
   defect_search / defect_suggest_fix 涉及 defect_patterns / defect_fixes 无
   workspace_id 列，为全局知识库，隔离由连接级 ACL 保证）。
⑥ Python fallback 边界：HTTP 模式（默认）走 client 便捷方法且 client
   失败时 fail-closed 传播（不调 get_db）；legacy
   （is_http_transport_enabled()=False + local 模式）才进入本地 db 回退。
   defect_learn 为写面（INSERT defect_fixes/defect_patterns），保持
   python_compat（W4-3 决策，见 ledger §9.24）——HTTP 模式仍走
   route_worker_call，不引入 HTTP 分支。

语义差异风险点（记录于 ledger §9.24）：defect_types 的 key 若 rule_id 为
NULL，Python 侧为 None key（JSON 序列化为 "null"），Rust 侧为 ""；suggest_fix
similar_fixes 中 effectiveness 若为 NULL，Python sum() 会抛错而 Rust 按 0.0
参与均值——两者均为 defect_fixes 写入时保证非 NULL 的罕见边界，可接受。
"""

from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_summary, tools_task

DB_A = "/tmp/w4_3_a.db"
DB_B = "/tmp/w4_3_b.db"

# 便捷方法名 → (RPC method, 业务参数，不含 db_path/workspace_instance_id)
CONVENIENCE_CASES = [
    ("defect_correlation", "query.defect_correlation",
     {"symbol_hash": "abc123", "window_commits": 5}),
    ("churn_analysis", "query.churn_analysis",
     {"module_filter": "src/", "time_window": "90d"}),
    ("defect_search", "query.defect_search",
     {"category": "sec", "severity_filter": "error"}),
    ("defect_suggest_fix", "query.defect_suggest_fix",
     {"symbol_hash": "abc123", "finding_id": 0}),
    ("get_defect_correlation", "query.get_defect_correlation",
     {"qualified_name": "src.main:foo", "window_commits": 5}),
]


def _make_client() -> HttpDaemonRpcClient:
    """绕过 __init__ 构造 HttpDaemonRpcClient 实例（不触发 manifest 发现/网络连接）。"""
    return HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)


def _register_tools(module, mcp=None):
    """注册工具模块到 mock MCP，返回 {name: fn} 字典（与 W4-2 测试同构）。"""
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

    @pytest.mark.parametrize(
        "method,rpc_method,params",
        CONVENIENCE_CASES,
        ids=[c[0] for c in CONVENIENCE_CASES],
    )
    def test_no_db_path_still_registers_workspace(self, method, rpc_method, params):
        """db_path=None 时 _ensure_remote_snapshot(None) 仍执行（仅注册 workspace，跳过 publish）。"""
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-auth-1"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            getattr(client, method)(db_path=None, **params)
        mock_ensure.assert_called_once_with(None)
        _, called_params = mock_call.call_args[0]
        assert called_params["workspace_instance_id"] == "ws-auth-1"


# ============================================================
# ② 结果限定（参数原样透传）
# ============================================================

class TestParamPropagation:
    """业务参数原样透传（不增不减不篡改）。"""

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

    @pytest.mark.parametrize(
        "method,rpc_method,params",
        CONVENIENCE_CASES,
        ids=[c[0] for c in CONVENIENCE_CASES],
    )
    def test_all_methods_do_not_add_unknown_params(self, method, rpc_method, params):
        """便捷方法不夹带未声明的业务参数（仅注入 workspace_instance_id）。"""
        client = _make_client()
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
    （真实拒绝行为见真实 HTTP probe）。window_commits 负数 Python 语义为空窗口
    （切片不报错），Rust 复刻该语义（不拒绝），故不在此组校验。
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
                client.defect_correlation("abc123", db_path=DB_A)
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
            client.defect_correlation("abc123", db_path=DB_A)
            client.churn_analysis("src/", "30d", db_path=DB_B)

        calls = mock_call.call_args_list
        assert calls[0][0][1]["workspace_instance_id"] == "ws-A"
        assert calls[1][0][1]["workspace_instance_id"] == "ws-B"

    def test_same_db_path_reuses_same_instance_id(self):
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-A"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.defect_correlation("abc123", db_path=DB_A)
            client.defect_search("sec", "error", db_path=DB_A)

        assert mock_ensure.call_count == 2
        for call in mock_call.call_args_list:
            assert call[0][1]["workspace_instance_id"] == "ws-A"
# ============================================================
# ⑥ 工具层 `_route` 契约（HTTP/local 分流已下沉到 route_rpc）
# ============================================================

class TestToolRouteContract:
    """工具层已纯 `_route` 化：不再直连 `HttpDaemonRpcClient` 便捷方法。

    stale 依据（A 桶 / MCP 工具 `_route` 化）：
    `server/tools/tools_summary.py:44` 与 `server/tools/tools_task.py:46` 均为
    `from ..daemon_client import route_rpc as _route`，defect/churn 组工具一律
    `return _route('<rpc method>', {...}, 'READ_ONLY')`（如 tools_summary.py:400
    /:435/:457、tools_task.py:650）。旧用例 patch `tools_summary._get_daemon_client`
    并断言客户端便捷方法被调用——该模块属性虽仍在（故 setattr 不报错）但已无
    调用点，于是断言恒为 `Called 0 times`。

    HTTP / local / compat-worker 的分流语义整体下沉到 `route_rpc`，由
    `route_rpc` 自身的单测覆盖；工具层只需锁定「下发哪个 RPC、参数是否逐字透传、
    op_class 是否为 READ_ONLY、失败是否 fail-closed 且不回落本地 get_db」。
    """

    HTTP_RUNTIME = "C:/git_work"

    ROUTE_CASES = [
        ("defect_correlation", "query.defect_correlation",
         {"symbol_hash": "abc123"},
         {"symbol_hash": "abc123", "window_commits": 5}),
        ("churn_analysis", "query.churn_analysis",
         {"module_filter": "src/", "time_window": "30d"},
         {"module_filter": "src/", "time_window": "30d"}),
        ("defect_search", "query.defect_search",
         {"category": "sec", "severity_filter": "error"},
         {"category": "sec", "severity_filter": "error"}),
        ("defect_suggest_fix", "query.defect_suggest_fix",
         {"symbol_hash": "abc123", "finding_id": 0},
         {"symbol_hash": "abc123", "finding_id": 0}),
    ]

    @pytest.mark.parametrize(
        "tool_name,rpc_method,call_kwargs,expect_params",
        ROUTE_CASES,
        ids=[c[0] for c in ROUTE_CASES],
    )
    def test_summary_tools_route_read_only_rpc(
        self, monkeypatch, tool_name, rpc_method, call_kwargs, expect_params
    ):
        """工具必须经模块级 `_route` 下发 READ_ONLY RPC，参数逐字透传，不碰本地 db。"""
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

    def test_task_get_defect_correlation_route_read_only_rpc(self, monkeypatch):
        """tools_task.get_defect_correlation 同样经 `_route` 下发（Rust native 同名 RPC）。"""
        seen = {}

        def fake_route(method, params, op_class):
            seen["method"] = method
            seen["params"] = dict(params)
            seen["op"] = op_class
            return {
                "qualified_name": "src.main:foo",
                "change_count": 3,
                "defect_count": 1,
                "defect_rate": 0.333,
                "defect_types": {"security": 1},
                "recent_defects": [{"rule_id": "R1", "message": "m", "start_line": 1}],
            }

        monkeypatch.setattr(tools_task, "_route", fake_route)
        q = _register_tools(tools_task)
        with patch("callwarden.server.tools.tools_task.get_db") as mock_db:
            result = q["get_defect_correlation"]("src.main:foo", window_commits=3)
            mock_db.assert_not_called()
        assert result["change_count"] == 3
        assert result["defect_rate"] == 0.333
        assert seen["method"] == "query.get_defect_correlation"
        assert seen["op"] == "READ_ONLY"
        assert seen["params"] == {"qualified_name": "src.main:foo", "window_commits": 3}

    def test_task_get_defect_correlation_fail_closed(self, monkeypatch):
        def fake_route(method, params, op_class):
            raise DaemonRemoteError("E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达")

        monkeypatch.setattr(tools_task, "_route", fake_route)
        q = _register_tools(tools_task)
        with patch("callwarden.server.tools.tools_task.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                q["get_defect_correlation"]("src.main:foo")
            mock_db.assert_not_called()

    def test_runtime_role_is_never_client_side_branched(self, monkeypatch):
        """工具层不做 HTTP/local 分支：无论 transport 如何都走同一 `_route` 契约。

        stale 依据：旧用例 monkeypatch
        `daemon_client.is_http_transport_enabled` / `get_daemon_mode` 后期待
        工具改走本地 db；`_route` 化后工具是常量表达式，分流只发生在
        `route_rpc` 内部（其单测负责），工具层断言不再随 transport 变化。
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
        assert q["defect_search"](category="sec") == {"ok": True}
        assert calls == [("query.defect_search", "READ_ONLY")]

    def test_defect_learn_now_routes_via_route(self, monkeypatch):
        """defect_learn 亦已 `_route` 化（不再经 route_worker_call / python_compat）。

        stale 依据（A 桶 / MCP 工具 `_route` 化）：旧用例断言
        「保持 python_compat，HTTP 模式仍走 route_worker_call」；实际
        `tools_summary.py:492` 为
        `return _route('defect_learn', {"fix_commit_hash": fix_commit_hash}, 'READ_ONLY')`，
        `route_worker_call` 已无调用点。

        注（advisory）：`defect_learn` 语义为写面（INSERT defect_fixes /
        defect_patterns），却以 `READ_ONLY` op_class 下发；本用例只锁当前实际
        契约，若 op_class 需收紧为 PROTECTED_MUTATION 属生产侧决策，不在本卡 scope。
        """
        seen = {}

        def fake_route(method, params, op_class):
            seen["method"] = method
            seen["params"] = dict(params)
            seen["op"] = op_class
            return {"learned": True, "pattern_id": "DP-1"}

        monkeypatch.setattr(tools_summary, "_route", fake_route)
        q = _register_tools(tools_summary)
        with patch("callwarden.server.tools.tools_summary.get_db") as mock_db:
            result = q["defect_learn"]("deadbeef")
            mock_db.assert_not_called()
        assert result == {"learned": True, "pattern_id": "DP-1"}
        assert seen["method"] == "defect_learn"
        assert seen["params"] == {"fix_commit_hash": "deadbeef"}
        assert seen["op"] == "READ_ONLY"