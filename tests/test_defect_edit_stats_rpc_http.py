"""W2-3（T-1786840097331-fd01a3f8）：defect/edit stats HTTP native 迁移 RPC 测试

覆盖 6 问验收（与 test_task_stats_rpc_http.py 同构）：
① workspace_id 绑定：HttpDaemonRpcClient 两便捷方法（defect_stats /
   get_edit_stats）均经 `_ensure_remote_snapshot` 注入权威
   workspace_instance_id（缺注入 Rust handler 强制 require →
   invalid_params，与 W2-1/W2-2 同构）。
② 结果限定/参数透传：defect_stats 无业务参数，params 仅含注入的
   workspace_instance_id；get_edit_stats 的 time_window 原样透传
   （params = {"time_window": ..., "workspace_instance_id": ...}，不增不减不篡改）。
③ 越界参数 fail-closed：`_ensure_remote_snapshot` 返回 None（注册失败
   边界）时不注入 workspace_instance_id，params 保持原样（Rust 侧
   require 拒绝；真实拒绝行为由 .trae-cn/evidence/w2_3_http_verify.py
   真实 HTTP probe 覆盖）。
④ snapshot_not_ready：`_ensure_remote_snapshot` 抛错（未发布 snapshot）
   时异常原样传播，不回退本地 SQL。
⑤ 跨 workspace 隔离：不同 db_path → 不同 workspace_instance_id 注入，
   同一 db_path 幂等复用。
⑥ Python fallback 边界：HTTP 模式（默认）两工具（tools_summary.defect_stats /
   tools_security.get_edit_stats）走 client 便捷方法且 client 失败时
   fail-closed 传播（不调 get_db）；legacy（is_http_transport_enabled()=False
   + local 模式）才进入 route_worker_call 本地 db 回退。
"""

from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_security, tools_summary

DB_A = "/tmp/w2_3_a.db"
DB_B = "/tmp/w2_3_b.db"

# 便捷方法名 → (RPC method, 业务参数，不含 db_path/workspace_instance_id)
CONVENIENCE_CASES = [
    ("defect_stats", "defect.stats", {}),
    ("get_edit_stats", "edit.stats", {"time_window": "30d"}),
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
    """两便捷方法均注入权威 workspace_instance_id，且 db_path 传给 _ensure_remote_snapshot。"""

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
            client.get_edit_stats(time_window="7d", db_path=None)
        mock_ensure.assert_called_once_with(None)
        _, params = mock_call.call_args[0]
        assert params["workspace_instance_id"] == "ws-auth-1"
        assert params["time_window"] == "7d"


# ============================================================
# ② 结果限定（参数原样透传）
# ============================================================

class TestParamPropagation:
    """defect_stats 无业务参数：params 仅含注入的 workspace_instance_id；
    get_edit_stats 的 time_window 原样透传（不增不减不篡改）。"""

    def test_defect_stats_params_contain_only_workspace_instance_id(self):
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.defect_stats(db_path=DB_A)
        _, params = mock_call.call_args[0]
        assert params == {"workspace_instance_id": "ws-1"}

    def test_get_edit_stats_passes_time_window_verbatim(self):
        """get_edit_stats 的 time_window 参数原样透传（含默认值，不篡改）。"""
        client = _make_client()
        # 默认 time_window="30d" 透传
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.get_edit_stats(db_path=DB_A)
        _, params = mock_call.call_args[0]
        assert params == {"time_window": "30d", "workspace_instance_id": "ws-1"}

        # 自定义 time_window 原样透传
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.get_edit_stats(time_window="7d", db_path=DB_A)
        _, params = mock_call.call_args[0]
        assert params == {"time_window": "7d", "workspace_instance_id": "ws-1"}

    def test_all_methods_do_not_add_unknown_params(self):
        """两便捷方法均不夹带未声明的业务参数（无 limit/kind 等）。"""
        client = _make_client()
        for method, _rpc, params in CONVENIENCE_CASES:
            with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                    patch.object(client, "call", return_value={}) as mock_call:
                getattr(client, method)(db_path=DB_A, **params)
            _, called_params = mock_call.call_args[0]
            assert "workspace_instance_id" in called_params
            extra = set(called_params) - {"workspace_instance_id", "time_window"}
            assert extra == set()


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
        with patch.object(client, "_ensure_remote_snapshot", side_effect=err):
            with pytest.raises(DaemonRemoteError) as excinfo:
                client.defect_stats(db_path=DB_A)
        assert excinfo.value.code == "snapshot_not_ready"

    def test_call_error_propagates_after_snapshot(self):
        """snapshot 已发布但查询失败（如表缺失）→ 远端错误原样传播。"""
        client = _make_client()
        err = DaemonRemoteError("internal_error", "cannot query edit stats")
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", side_effect=err):
            with pytest.raises(DaemonRemoteError) as excinfo:
                client.get_edit_stats(db_path=DB_A)
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
            client.defect_stats(db_path=DB_A)
            client.get_edit_stats(db_path=DB_B)

        calls = mock_call.call_args_list
        assert calls[0][0][1]["workspace_instance_id"] == "ws-A"
        assert calls[1][0][1]["workspace_instance_id"] == "ws-B"

    def test_same_db_path_reuses_same_instance_id(self):
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-A"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.defect_stats(db_path=DB_A)
            client.get_edit_stats(db_path=DB_A)

        assert mock_ensure.call_count == 2
        for call in mock_call.call_args_list:
            assert call[0][1]["workspace_instance_id"] == "ws-A"


# ============================================================
# ⑥ Python fallback 边界
# ============================================================

class TestPythonFallbackBoundary:
    """HTTP 模式 fail-closed（不回落 get_db）；legacy 模式才走本地回退。"""

    def test_http_mode_fail_closed_no_db_fallback(self, monkeypatch):
        """HTTP 模式（默认）两工具走 client 便捷方法；client 抛错时 fail-closed
        传播，不调用 get_db（无 SQL 回退）。"""
        client = MagicMock()
        client.defect_stats.side_effect = DaemonRemoteError(
            "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
        )
        client.get_edit_stats.side_effect = DaemonRemoteError(
            "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_daemon_client",
            lambda: client,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary._get_db_path_for_daemon",
            lambda: DB_A,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary.is_http_transport_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_security._get_daemon_client",
            lambda: client,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_security._get_db_path_for_daemon",
            lambda: DB_A,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_security.is_http_transport_enabled",
            lambda: True,
        )
        summary_tools = _register_tools(tools_summary)
        security_tools = _register_tools(tools_security)
        with patch("callwarden.server.tools.tools_summary.get_db") as mock_sum_db, \
                patch("callwarden.server.tools.tools_security.get_db") as mock_sec_db:
            with pytest.raises(DaemonRemoteError):
                summary_tools["defect_stats"]()
            with pytest.raises(DaemonRemoteError):
                security_tools["get_edit_stats"]()
            mock_sum_db.assert_not_called()
            mock_sec_db.assert_not_called()
        client.defect_stats.assert_called_once_with(db_path=DB_A)
        client.get_edit_stats.assert_called_once_with(time_window="30d", db_path=DB_A)

    def test_http_mode_custom_time_window_propagated(self, monkeypatch):
        """HTTP 模式 get_edit_stats(time_window="7d") 原样传给便捷方法。"""
        client = MagicMock()
        client.get_edit_stats.return_value = {"time_window": "7d", "total": 0}
        monkeypatch.setattr(
            "callwarden.server.tools.tools_security._get_daemon_client",
            lambda: client,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_security._get_db_path_for_daemon",
            lambda: DB_A,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_security.is_http_transport_enabled",
            lambda: True,
        )
        security_tools = _register_tools(tools_security)
        result = security_tools["get_edit_stats"](time_window="7d")
        client.get_edit_stats.assert_called_once_with(time_window="7d", db_path=DB_A)
        assert result == {"time_window": "7d", "total": 0}

    def test_legacy_local_mode_keeps_db_fallback(self, monkeypatch):
        """is_http_transport_enabled()=False + local 模式 → 两工具走
        route_worker_call 本地 db 回退（get_db 被调用）。"""
        monkeypatch.setattr(
            "callwarden.server.tools.tools_summary.is_http_transport_enabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_security.is_http_transport_enabled",
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
        summary_tools = _register_tools(tools_summary)
        security_tools = _register_tools(tools_security)

        mock_db = MagicMock()
        mock_db.defect_stats.return_value = {"total_patterns": 3, "total_fixes": 1}
        mock_db.get_edit_stats.return_value = {
            "time_window": "30d", "total": 5,
            "by_status": {"applied": 4, "reverted": 1, "failed": 0, "pending": 0},
            "by_operation": {"edit": 3, "create": 2, "delete": 0},
            "revert_rate": 0.2,
        }

        with patch("callwarden.server.tools.tools_summary.get_db") as mock_sum_db, \
                patch("callwarden.server.tools.tools_security.get_db") as mock_sec_db:
            mock_sum_db.return_value = mock_db
            mock_sec_db.return_value = mock_db

            r1 = summary_tools["defect_stats"]()
            r2 = security_tools["get_edit_stats"](time_window="30d")

            mock_db.defect_stats.assert_called_once_with()
            mock_db.get_edit_stats.assert_called_once_with(time_window="30d")
            assert r1 == {"total_patterns": 3, "total_fixes": 1}
            assert r2["total"] == 5 and r2["revert_rate"] == 0.2
