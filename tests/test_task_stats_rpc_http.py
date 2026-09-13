"""W2-2（T-1786840097330-a9e0ec69）：task 面 stats HTTP native 迁移 RPC 测试

覆盖 6 问验收：
① workspace_id 绑定：HttpDaemonRpcClient 三便捷方法（get_clone_stats /
   get_job_stats / get_clone_group_stats）均经 `_ensure_remote_snapshot`
   注入权威 workspace_instance_id（缺注入 Rust handler 强制 require →
   invalid_params，与 W2-1 同构）。
② 结果限定：三方法无业务参数，params 仅含 workspace_instance_id
   （db_path 原样传给 _ensure_remote_snapshot，不缺不篡改）。
③ 越界参数 fail-closed：`_ensure_remote_snapshot` 返回 None（注册失败
   边界）时不注入 workspace_instance_id，params 保持原样（Rust 侧
   require 拒绝；真实拒绝行为由 .trae-cn/evidence/w2_2_http_verify.py
   真实 HTTP probe 覆盖）。
④ snapshot_not_ready：`_ensure_remote_snapshot` 抛错（未发布 snapshot）
   时异常原样传播，不回退本地 SQL。
⑤ 跨 workspace 隔离：不同 db_path → 不同 workspace_instance_id 注入，
   同一 db_path 幂等复用。
⑥ 工具层 `_route` 契约：三工具已退化为一行式 `_route('task.<stats>', {},
   'READ_ONLY')`，HTTP/local 分流整体下沉 `route_rpc`；旧「HTTP 模式走 client
   便捷方法 / legacy 走 route_worker_call 本地 db 回退」的客户端分支已被删除，
   失败一律 fail-closed 传播（不回落本地 get_db）。
"""

from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_task

DB_A = "/tmp/w2_2_a.db"
DB_B = "/tmp/w2_2_b.db"

# 工具名 → (RPC method, 额外 params，不含 db_path/workspace_instance_id)
CONVENIENCE_CASES = [
    ("get_clone_stats", "task.clone_stats", {}),
    ("get_job_stats", "task.job_stats", {}),
    ("get_clone_group_stats", "task.clone_group_stats", {}),
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
    """三便捷方法均注入权威 workspace_instance_id，且 db_path 传给 _ensure_remote_snapshot。"""

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

    def test_get_job_stats_no_db_path_still_registers_workspace(self):
        """db_path=None 时 _ensure_remote_snapshot(None) 仍执行（仅注册 workspace，跳过 publish）。"""
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-auth-1"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.get_job_stats(db_path=None)
        mock_ensure.assert_called_once_with(None)
        _, params = mock_call.call_args[0]
        assert params["workspace_instance_id"] == "ws-auth-1"


# ============================================================
# ② 结果限定（参数原样透传）
# ============================================================

class TestParamPropagation:
    """三方法无业务参数：params 仅含注入的 workspace_instance_id，不增不减。"""

    def test_params_contain_only_workspace_instance_id(self):
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.get_clone_stats(db_path=DB_A)
        _, params = mock_call.call_args[0]
        assert params == {"workspace_instance_id": "ws-1"}

    def test_all_three_methods_pass_only_injected_id(self):
        """三个便捷方法均不夹带任何业务参数（无 limit/kind 等）。"""
        client = _make_client()
        for method, _rpc, params in CONVENIENCE_CASES:
            with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                    patch.object(client, "call", return_value={}) as mock_call:
                getattr(client, method)(db_path=DB_A, **params)
            _, called_params = mock_call.call_args[0]
            assert called_params == {"workspace_instance_id": "ws-1"}


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
                client.get_clone_stats(db_path=DB_A)
        assert excinfo.value.code == "snapshot_not_ready"

    def test_call_error_propagates_after_snapshot(self):
        """snapshot 已发布但查询失败（如表缺失）→ 远端错误原样传播。"""
        client = _make_client()
        err = DaemonRemoteError("internal_error", "cannot query clone stats")
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", side_effect=err):
            with pytest.raises(DaemonRemoteError) as excinfo:
                client.get_clone_group_stats(db_path=DB_A)
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
            client.get_clone_stats(db_path=DB_A)
            client.get_job_stats(db_path=DB_B)

        calls = mock_call.call_args_list
        assert calls[0][0][1]["workspace_instance_id"] == "ws-A"
        assert calls[1][0][1]["workspace_instance_id"] == "ws-B"

    def test_same_db_path_reuses_same_instance_id(self):
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-A"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.get_clone_stats(db_path=DB_A)
            client.get_clone_group_stats(db_path=DB_A)

        assert mock_ensure.call_count == 2
        for call in mock_call.call_args_list:
            assert call[0][1]["workspace_instance_id"] == "ws-A"


# ============================================================
# ⑥ 工具层 `_route` 契约
# ============================================================

class TestToolRouteContract:
    """工具层已纯 `_route` 化：不再直连 client 便捷方法 / route_worker_call。

    stale 依据（A 桶 / MCP 工具 `_route` 化）：
    `server/tools/tools_task.py:46` 为
    `from ..daemon_client import route_rpc as _route`；本文件涉及的 3 个工具一律
    退化为一行式 `_route(...)`——get_clone_stats(:495)/
    get_job_stats(:753)/get_clone_group_stats(:841) 均为
    `return _route('task.<stats>', {}, 'READ_ONLY')`。
    旧用例 patch `tools_task._get_daemon_client` / `_get_db_path_for_daemon` /
    `is_http_transport_enabled` 并断言客户端便捷方法被调用——这些模块属性虽仍在
    （故 monkeypatch 不报错）但已无调用点，于是断言恒为 `Called 0 times`；且
    HTTP/legacy 分支已下沉到 `route_rpc`，不再由工具层判断。

    因此工具层只需锁定「下发哪个 RPC、params 是否为空、op_class 是否为
    READ_ONLY、失败是否 fail-closed 且不回落本地 get_db」。
    """

    ROUTE_CASES = [
        ("get_clone_stats", "task.clone_stats"),
        ("get_job_stats", "task.job_stats"),
        ("get_clone_group_stats", "task.clone_group_stats"),
    ]

    @pytest.mark.parametrize(
        "tool_name,rpc_method",
        ROUTE_CASES,
        ids=[c[0] for c in ROUTE_CASES],
    )
    def test_task_stats_tools_route_read_only_rpc(
        self, monkeypatch, tool_name, rpc_method,
    ):
        """工具经模块级 `_route` 下发 READ_ONLY RPC（空 params），不碰本地 db。"""
        seen = {}

        def fake_route(method, params, op_class):
            seen["method"] = method
            seen["params"] = dict(params)
            seen["op"] = op_class
            return {"ok": True}

        monkeypatch.setattr(tools_task, "_route", fake_route)
        q = _register_tools(tools_task)
        with patch("callwarden.server.tools.tools_task.get_db") as mock_db:
            out = q[tool_name]()
            mock_db.assert_not_called()
        assert out == {"ok": True}
        assert seen["method"] == rpc_method
        assert seen["op"] == "READ_ONLY"
        assert seen["params"] == {}

    @pytest.mark.parametrize(
        "tool_name,rpc_method",
        ROUTE_CASES,
        ids=[c[0] for c in ROUTE_CASES],
    )
    def test_task_stats_tools_fail_closed_no_local_fallback(
        self, monkeypatch, tool_name, rpc_method,
    ):
        """`_route` 抛 DaemonRemoteError → 原样传播，绝不回落本地 get_db。"""
        def fake_route(method, params, op_class):
            raise DaemonRemoteError("E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达")

        monkeypatch.setattr(tools_task, "_route", fake_route)
        q = _register_tools(tools_task)
        with patch("callwarden.server.tools.tools_task.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                q[tool_name]()
            mock_db.assert_not_called()
