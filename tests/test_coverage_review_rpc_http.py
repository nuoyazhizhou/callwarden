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
⑥ Python fallback 边界：HTTP 模式（默认）走 client 便捷方法且 client
   失败时 fail-closed 传播（不调 get_db）；legacy
   （is_http_transport_enabled()=False + local 模式）才进入本地 db 回退。
   review_readiness 依赖 blast_radius 与 cross_layer_impact（均未迁移），
   保持 python_compat（W4-2 决策，见 ledger §9.23）——HTTP 模式仍走
   route_worker_call，不引入 HTTP 分支。

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
# ⑥ Python fallback 边界
# ============================================================

class TestPythonFallbackBoundary:
    """HTTP 模式 fail-closed（不回落 get_db）；legacy 模式才走本地回退。

    tools_summary 在模块顶层 import `_get_daemon_client` /
    `_get_db_path_for_daemon` / `is_http_transport_enabled`（可直接 patch
    模块属性）。review_readiness 保持 python_compat：HTTP 模式仍走
    route_worker_call（无 HTTP 分支），不直连 daemon client。
    """

    # --------------------------------------------------------
    # tools_summary.get_coverage_for_symbol（顶层 import）
    # --------------------------------------------------------

    def test_coverage_http_mode_fail_closed(self, monkeypatch):
        client = MagicMock()
        client.get_coverage_for_symbol.side_effect = DaemonRemoteError(
            "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_db_path_for_daemon", lambda: DB_A
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary.is_http_transport_enabled", lambda: True
        )
        q = _register_tools(tools_summary)
        with patch("callwarden.server.tools.tools_summary.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                q["get_coverage_for_symbol"]("src.main:foo")
            mock_db.assert_not_called()
        client.get_coverage_for_symbol.assert_called_once_with(
            qualified_name="src.main:foo", db_path=DB_A,
        )

    def test_coverage_http_mode_result_passthrough(self, monkeypatch):
        client = MagicMock()
        client.get_coverage_for_symbol.return_value = {
            "qualified_name": "src.main:foo",
            "coverage_pct": 66.7,
            "tracked_lines": 3,
        }
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_db_path_for_daemon", lambda: DB_A
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary.is_http_transport_enabled", lambda: True
        )
        q = _register_tools(tools_summary)
        result = q["get_coverage_for_symbol"]("src.main:foo")
        assert result["coverage_pct"] == 66.7

    def test_coverage_legacy_local_mode_keeps_db_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode", lambda: "local"
        )
        q = _register_tools(tools_summary)
        mock_db = MagicMock()
        mock_db.conn = MagicMock()
        mock_db.get_coverage_for_symbol.return_value = {"qualified_name": "src.main:foo"}
        with patch("callwarden.server.tools.tools_summary.get_db") as mock_get_db:
            mock_get_db.return_value = mock_db
            result = q["get_coverage_for_symbol"]("src.main:foo")
        assert result["qualified_name"] == "src.main:foo"
        mock_db.get_coverage_for_symbol.assert_called_once_with(qualified_name="src.main:foo")

    # --------------------------------------------------------
    # tools_summary.diff_to_symbol（顶层 import，保留 try-except 降级）
    # --------------------------------------------------------

    def test_diff_http_mode_fail_closed(self, monkeypatch):
        client = MagicMock()
        client.diff_to_symbol.side_effect = DaemonRemoteError(
            "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_db_path_for_daemon", lambda: DB_A
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary.is_http_transport_enabled", lambda: True
        )
        q = _register_tools(tools_summary)
        diff_text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
        with patch("callwarden.server.tools.tools_summary.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                q["diff_to_symbol"](diff_text)
            mock_db.assert_not_called()
        client.diff_to_symbol.assert_called_once_with(
            diff_text=diff_text, db_path=DB_A,
        )

    def test_diff_http_mode_result_passthrough(self, monkeypatch):
        client = MagicMock()
        client.diff_to_symbol.return_value = [
            {"symbol_hash": "h1", "qualified_name": "a", "change_type": "modified"}
        ]
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_db_path_for_daemon", lambda: DB_A
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary.is_http_transport_enabled", lambda: True
        )
        q = _register_tools(tools_summary)
        diff_text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
        result = q["diff_to_symbol"](diff_text)
        assert result[0]["change_type"] == "modified"

    def test_diff_legacy_local_mode_keeps_db_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode", lambda: "local"
        )
        q = _register_tools(tools_summary)
        mock_db = MagicMock()
        mock_db.conn = MagicMock()
        mock_db.diff_to_symbol.return_value = [{"symbol_hash": "h1"}]
        with patch("callwarden.server.tools.tools_summary.get_db") as mock_get_db:
            mock_get_db.return_value = mock_db
            result = q["diff_to_symbol"]("--- a/x\n+++ b/x\n")
        assert result[0]["symbol_hash"] == "h1"
        mock_db.diff_to_symbol.assert_called_once_with("--- a/x\n+++ b/x\n")

    # --------------------------------------------------------
    # tools_summary.review_readiness（保持 python_compat）
    # --------------------------------------------------------

    def test_review_readiness_stays_compat_in_http_mode(self, monkeypatch):
        """review_readiness 无 HTTP 分支：HTTP 模式仍经 route_worker_call（compat worker）。

        W4-2 决策：review_readiness 依赖 blast_radius 与 cross_layer_impact
        （均未迁移），保持 python_compat（见 ledger §9.23）。断言 HTTP 模式下
        不调用 daemon client 便捷方法（模块属性 _get_daemon_client 不被调用）。
        """
        called = {"client": False}

        def fake_client():
            called["client"] = True
            return MagicMock()

        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_daemon_client", fake_client
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_db_path_for_daemon", lambda: DB_A
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary.is_http_transport_enabled", lambda: True
        )
        q = _register_tools(tools_summary)
        mock_db = MagicMock()
        mock_db.conn = MagicMock()
        mock_db.review_readiness_report.return_value = {"ready": True}
        # route_worker_call（HTTP/enterprise 分支）→ compat worker 执行 _local
        # 注意 patch 目标是 tools_summary 命名空间的引用（顶层 from import），
        # 不能 patch daemon_client 模块属性（不影响本模块调用）。
        with patch("callwarden.server.tools.tools_summary.get_db", return_value=mock_db), \
                patch(
                    "callwarden.server.tools.tools_summary.route_worker_call",
                    side_effect=lambda method, params, local_fn: local_fn(),
                ):
            result = q["review_readiness"]("hash-123")
        assert result == {"ready": True}
        assert called["client"] is False
        mock_db.review_readiness_report.assert_called_once_with("hash-123")
