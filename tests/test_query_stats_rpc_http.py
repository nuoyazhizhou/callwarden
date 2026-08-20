"""W2-1（T-1786840097330-dec66710）：query 面 stats HTTP native 迁移 RPC 测试

覆盖 6 问验收：
① workspace_id 绑定：HttpDaemonRpcClient 四便捷方法（get_stats /
   get_uncommented_symbols / get_module_call_stats / get_semgrep_stats）均经
   `_ensure_remote_snapshot` 注入权威 workspace_instance_id（缺注入 Rust
   handler 强制 require → invalid_params，即修复前 get_stats HTTP 恒失败
   的 H6 同类缺陷）。
② 结果限定：kind/module_filter/limit 参数原样透传（int 不默认化不篡改），
   Rust 侧无条件 LIMIT 对齐 Python 工具层 `[:limit]` 截断语义。
③ 越界参数 fail-closed：limit 负值 Python 侧原样透传（不 clamp 不改默认值），
   Rust handler 拒绝 invalid_params（真实拒绝行为由
   .trae-cn/evidence/w2_1_http_verify.py 真实 HTTP probe 覆盖）。
④ snapshot_not_ready：`_ensure_remote_snapshot` 抛错（未发布 snapshot）时
   异常原样传播，不回退本地 SQL。
⑤ 跨 workspace 隔离：不同 db_path → 不同 workspace_instance_id 注入，
   同一 db_path 幂等复用。
⑥ Python fallback 边界：HTTP 模式（默认）三工具走 client 便捷方法且
   client 失败时 fail-closed 传播（不调 get_db）；legacy
   （is_http_transport_enabled()=False + local 模式）才进入 route_worker_call
   本地 db 回退。
"""

from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_query

DB_A = "/tmp/w2_1_a.db"
DB_B = "/tmp/w2_1_b.db"

# 工具名 → (RPC method, 额外 params，不含 db_path/workspace_instance_id)
CONVENIENCE_CASES = [
    ("get_stats", "query.stats", {}),
    ("get_uncommented_symbols", "query.uncommented_symbols",
     {"kind": "fn", "module_filter": "", "limit": 100}),
    ("get_module_call_stats", "query.module_call_stats", {"limit": 30}),
    ("get_semgrep_stats", "query.semgrep_stats", {}),
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
    """四便捷方法均注入权威 workspace_instance_id，且 db_path 传给 _ensure_remote_snapshot。"""

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

    def test_get_stats_no_db_path_still_registers_workspace(self):
        """db_path=None 时 _ensure_remote_snapshot(None) 仍执行（仅注册 workspace，跳过 publish）。"""
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-auth-1"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.get_stats(db_path=None)
        mock_ensure.assert_called_once_with(None)
        _, params = mock_call.call_args[0]
        assert params["workspace_instance_id"] == "ws-auth-1"


# ============================================================
# ② 结果限定（参数原样透传）
# ============================================================

class TestParamPropagation:
    """kind/module_filter/limit 原样透传，int 不默认化不篡改。"""

    def test_module_call_stats_limit_passed_as_int(self):
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value=[]) as mock_call:
            client.get_module_call_stats(limit=10, db_path=DB_A)
        _, params = mock_call.call_args[0]
        assert params["limit"] == 10
        assert isinstance(params["limit"], int)
        assert params["workspace_instance_id"] == "ws-1"

    def test_uncommented_symbols_kind_filter_limit_passed_verbatim(self):
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value=[]) as mock_call:
            client.get_uncommented_symbols(
                kind="struct", module_filter="core::", limit=5, db_path=DB_A,
            )
        _, params = mock_call.call_args[0]
        assert params == {
            "kind": "struct",
            "module_filter": "core::",
            "limit": 5,
            "workspace_instance_id": "ws-1",
        }


# ============================================================
# ③ 越界参数 fail-closed
# ============================================================

class TestOutOfRangeFailClosed:
    """limit 负值原样透传（不 clamp 不改默认值），Rust handler 拒绝 invalid_params。

    说明：Python thin client 不包含业务校验（H2 契约：不含业务 SQL、不预判业务
    错误），越界参数由 Rust handler 结构化拒绝（真实行为见真实 HTTP probe）。
    """

    def test_negative_limit_passthrough_not_clamped(self):
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value=[]) as mock_call:
            client.get_module_call_stats(limit=-5, db_path=DB_A)
        _, params = mock_call.call_args[0]
        assert params["limit"] == -5

    def test_negative_limit_passthrough_uncommented_symbols(self):
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value=[]) as mock_call:
            client.get_uncommented_symbols(limit=-1, db_path=DB_A)
        _, params = mock_call.call_args[0]
        assert params["limit"] == -1

    def test_missing_workspace_id_means_no_injection_when_snapshot_none(self):
        """_ensure_remote_snapshot 返回 None（db_path=None 且注册失败边界）时
        不注入 workspace_instance_id，params 保持原样（Rust 侧 require 拒绝）。"""
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value=None), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.get_semgrep_stats(db_path=DB_A)
        _, params = mock_call.call_args[0]
        assert "workspace_instance_id" not in params


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
        with patch.object(client, "_ensure_remote_snapshot", side_effect=err):
            with pytest.raises(DaemonRemoteError) as excinfo:
                client.get_semgrep_stats(db_path=DB_A)
        assert excinfo.value.code == "snapshot_not_ready"

    def test_call_error_propagates_after_snapshot(self):
        """snapshot 已发布但查询失败（如表缺失）→ 远端错误原样传播。"""
        client = _make_client()
        err = DaemonRemoteError("internal_error", "cannot query semgrep stats")
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", side_effect=err):
            with pytest.raises(DaemonRemoteError) as excinfo:
                client.get_semgrep_stats(db_path=DB_A)
        assert excinfo.value.code == "internal_error"


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
            client.get_stats(db_path=DB_A)
            client.get_stats(db_path=DB_B)

        calls = mock_call.call_args_list
        assert calls[0][0][1]["workspace_instance_id"] == "ws-A"
        assert calls[1][0][1]["workspace_instance_id"] == "ws-B"

    def test_same_db_path_reuses_same_instance_id(self):
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-A"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.get_stats(db_path=DB_A)
            client.get_semgrep_stats(db_path=DB_A)

        assert mock_ensure.call_count == 2
        for call in mock_call.call_args_list:
            assert call[0][1]["workspace_instance_id"] == "ws-A"


# ============================================================
# ⑥ Python fallback 边界
# ============================================================

class TestPythonFallbackBoundary:
    """HTTP 模式 fail-closed（不回落 get_db）；legacy 模式才走本地回退。"""

    def test_http_mode_fail_closed_no_db_fallback(self, monkeypatch):
        """HTTP 模式（默认）三工具走 client；client 抛错时 fail-closed 传播，
        不调用 get_db（无 SQL 回退）。"""
        client = MagicMock()
        client.get_semgrep_stats.side_effect = DaemonRemoteError(
            "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query._get_daemon_client",
            lambda: client,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query._get_db_path_for_daemon",
            lambda: DB_A,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query.is_http_transport_enabled",
            lambda: True,
        )
        tools = _register_tools(tools_query)
        with patch("callwarden.server.tools.tools_query.get_db") as mock_get_db:
            with pytest.raises(DaemonRemoteError):
                tools["get_semgrep_stats"]()
            mock_get_db.assert_not_called()
        client.get_semgrep_stats.assert_called_once_with(db_path=DB_A)

    def test_legacy_local_mode_keeps_db_fallback(self, monkeypatch):
        """is_http_transport_enabled()=False + local 模式 → 三工具走
        route_worker_call 本地 db 回退（get_db 被调用）。"""
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query.is_http_transport_enabled",
            lambda: False,
        )
        # route_worker_call 内部引用 daemon_client 模块内的函数
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode",
            lambda: "local",
        )
        tools = _register_tools(tools_query)
        with patch("callwarden.server.tools.tools_query.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.get_module_call_stats.return_value = [
                {"caller_module": "a", "callee_module": "b", "call_count": 1}
            ]
            mock_get_db.return_value = mock_db

            result = tools["get_module_call_stats"]()

            mock_db.get_module_call_stats.assert_called_once_with(limit=30)
            assert result == [
                {"caller_module": "a", "callee_module": "b", "call_count": 1}
            ]
