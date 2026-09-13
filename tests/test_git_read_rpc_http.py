"""W4-1（T-1786886251769-22b94ee8-sub-1）：git 读组 5 工具 HTTP native 迁移 RPC 测试

覆盖 5 个工具（get_file_history / get_git_commits / get_commit_changes /
get_git_stats / get_commit_tasks）的 6 问验收（与
test_semgrep_findings_rpc_http.py / test_job_read_rpc_http.py /
test_build_read_rpc_http.py 同构）：
① workspace_id 绑定：便捷方法经 `_ensure_remote_snapshot` 注入权威
   workspace_instance_id（缺注入 Rust handler 强制 require →
   invalid_params）。
② 结果限定/参数透传：file_path / limit / offset / commit_hash /
   include_task_details 原样透传（params 不增不减不篡改）。
③ 越界参数 fail-closed：`_ensure_remote_snapshot` 返回 None（注册失败
   边界）时不注入 workspace_instance_id，params 保持原样（Rust 侧 require
   拒绝；limit/offset<0 由 Rust handler 返回 invalid_params）。
④ snapshot_not_ready：`_ensure_remote_snapshot` 抛错（未发布 snapshot）
   时异常原样传播，不回退本地 SQL。
⑤ 跨 workspace 隔离：不同 db_path → 不同 workspace_instance_id 注入，
   同一 db_path 幂等复用；Rust 查询按 workspace_id 限定。
⑥ Python fallback 边界：工具层已 `_route` 化（常量表达式），HTTP/local 分流
   整体下沉到 `route_rpc`；本文件 ⑥ 组（TestToolRouteContract）只锁定工具层
   「下发哪个 RPC、参数逐字透传（含默认值）、op_class=READ_ONLY、失败
   fail-closed 且不回落本地 get_db」的契约，不再 patch `_get_daemon_client`。
   get_file_history 的绝对路径规范化随 HTTP 分支移除 → file_path 逐字透传。

语义差异风险点（记录）：`_route` 化后 get_file_history 的 file_path 由工具层
逐字透传给 Rust（旧的 Python 侧绝对路径 → rel_path 规范化分支已随 HTTP 分支
移除）；get_commit_tasks 复刻 Python 全局查询（无 workspace 维度），git_commits
含 workspace_id 列、git_file_changes 无（经 JOIN 隔离）。
"""

from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_query, tools_task, tools_workspace

DB_A = "/tmp/w4_1_a.db"
DB_B = "/tmp/w4_1_b.db"

# 便捷方法名 → (RPC method, 业务参数，不含 db_path/workspace_instance_id)
CONVENIENCE_CASES = [
    ("get_file_history", "query.file_history", {"file_path": "src/main.py"}),
    ("get_git_commits", "query.git_commits", {"limit": 30, "offset": 10}),
    ("get_commit_changes", "query.git_commit_changes",
     {"commit_hash": "abc123def456"}),
    ("get_git_stats", "query.git_stats", {}),
    ("get_commit_tasks", "query.commit_tasks",
     {"commit_hash": "abc123def456", "include_task_details": False}),
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
            client.get_git_stats(db_path=None)
        mock_ensure.assert_called_once_with(None)
        _, params = mock_call.call_args[0]
        assert params["workspace_instance_id"] == "ws-auth-1"


# ============================================================
# ② 结果限定（参数原样透传）
# ============================================================

class TestParamPropagation:
    """file_path / limit / offset / commit_hash / include_task_details 原样透传。"""

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

    def test_git_commits_defaults_passthrough(self):
        """未传 limit/offset 时默认值（20/0）原样透传。"""
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.get_git_commits(db_path=DB_A)
        _, called_params = mock_call.call_args[0]
        assert called_params["limit"] == 20
        assert called_params["offset"] == 0

    def test_commit_tasks_defaults_passthrough(self):
        """未传 include_task_details 时默认 True 原样透传。"""
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.get_commit_tasks("abc123def456", db_path=DB_A)
        _, called_params = mock_call.call_args[0]
        assert called_params["commit_hash"] == "abc123def456"
        assert called_params["include_task_details"] is True

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
    错误），Rust handler 对缺失 workspace_instance_id 强制 require → invalid_params；
    limit/offset<0 亦由 Rust handler 拒绝（真实拒绝行为见真实 HTTP probe）。
    """

    def test_missing_workspace_id_means_no_injection_when_snapshot_none(self):
        client = _make_client()
        for method, _rpc, params in CONVENIENCE_CASES:
            with patch.object(client, "_ensure_remote_snapshot", return_value=None), \
                    patch.object(client, "call", return_value={}) as mock_call:
                getattr(client, method)(db_path=DB_A, **params)
            _, called_params = mock_call.call_args[0]
            assert "workspace_instance_id" not in called_params

    def test_negative_limit_offset_passthrough_to_rust(self):
        """limit=-1 / offset=-1 原样透传（Python 不拦截），Rust 侧 invalid_params。"""
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.get_git_commits(limit=-1, offset=-1, db_path=DB_A)
        _, called_params = mock_call.call_args[0]
        assert called_params["limit"] == -1
        assert called_params["offset"] == -1


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
                client.get_git_commits(db_path=DB_A)
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
            client.get_git_commits(db_path=DB_A)
            client.get_git_stats(db_path=DB_B)

        calls = mock_call.call_args_list
        assert calls[0][0][1]["workspace_instance_id"] == "ws-A"
        assert calls[1][0][1]["workspace_instance_id"] == "ws-B"

    def test_same_db_path_reuses_same_instance_id(self):
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-A"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.get_commit_changes("abc", db_path=DB_A)
            client.get_commit_tasks("abc", db_path=DB_A)

        assert mock_ensure.call_count == 2
        for call in mock_call.call_args_list:
            assert call[0][1]["workspace_instance_id"] == "ws-A"


# ============================================================
# ⑥ 工具层 `_route` 契约（HTTP/local 分流已下沉到 route_rpc）
# ============================================================

class TestToolRouteContract:
    """工具层已纯 `_route` 化：不再直连 daemon client 便捷方法。

    stale 依据（MCP 工具 `_route` 化）：`server/tools/tools_query.py:56` /
    `server/tools/tools_task.py:46` / `server/tools/tools_workspace.py:29` 均为
    `from ..daemon_client import route_rpc as _route`；get_file_history（:162）→
    `_route('query.file_history', {...}, 'READ_ONLY')`，get_commit_tasks（:237）→
    `_route('query.commit_tasks', {...}, 'READ_ONLY')`，get_git_commits（:270）/
    get_commit_changes（:287）/ get_git_stats（:301）同款 READ_ONLY。旧用例 patch
    `_get_daemon_client` / `_get_db_path_for_daemon` / `is_http_transport_enabled`
    并断言客户端便捷方法被调用——这些模块属性虽仍在但已无调用点，断言恒为
    `Called 0 times`；legacy 本地 db 回退分支也已随 `_route` 化移除。
    """

    ROUTE_CASES = [
        (tools_query, "get_file_history", "query.file_history",
         ("src/main.py",), {}, {"file_path": "src/main.py"}),
        (tools_task, "get_commit_tasks", "query.commit_tasks",
         ("abc123def456",), {}, {"commit_hash": "abc123def456",
                                 "include_task_details": True}),
        (tools_workspace, "get_git_commits", "query.git_commits",
         (), {"limit": 5}, {"limit": 5, "offset": 0}),
        (tools_workspace, "get_commit_changes", "query.git_commit_changes",
         ("abc",), {}, {"commit_hash": "abc"}),
        (tools_workspace, "get_git_stats", "query.git_stats", (), {}, {}),
    ]
    _IDS = ["get_file_history", "get_commit_tasks", "get_git_commits",
            "get_commit_changes", "get_git_stats"]

    @pytest.mark.parametrize(
        "module,tool_name,rpc_method,args,kwargs,expect_params",
        ROUTE_CASES,
        ids=_IDS,
    )
    def test_tools_route_read_only_rpc(
        self, monkeypatch, module, tool_name, rpc_method, args, kwargs, expect_params
    ):
        """工具经模块级 `_route` 下发 READ_ONLY RPC，参数逐字透传，不碰本地 db。"""
        seen = {}

        def fake_route(method, params, op_class):
            seen["method"] = method
            seen["params"] = dict(params)
            seen["op"] = op_class
            return {"ok": True}

        monkeypatch.setattr(module, "_route", fake_route)
        q = _register_tools(module)
        with patch(f"{module.__name__}.get_db") as mock_db:
            out = q[tool_name](*args, **kwargs)
            mock_db.assert_not_called()
        assert out == {"ok": True}
        assert seen["method"] == rpc_method
        assert seen["op"] == "READ_ONLY"
        assert seen["params"] == expect_params

    def test_file_history_abs_path_passthrough_verbatim(self, monkeypatch):
        """`_route` 化后绝对路径不再在工具层规范化，file_path 逐字透传。"""
        seen = {}

        def fake_route(method, params, op_class):
            seen["params"] = dict(params)
            return []

        monkeypatch.setattr(tools_query, "_route", fake_route)
        q = _register_tools(tools_query)
        q["get_file_history"]("C:/repo/src/main.py")
        assert seen["params"] == {"file_path": "C:/repo/src/main.py"}

    def test_commit_tasks_explicit_false_passthrough(self, monkeypatch):
        """include_task_details=False 原样透传。"""
        seen = {}

        def fake_route(method, params, op_class):
            seen["params"] = dict(params)
            return []

        monkeypatch.setattr(tools_task, "_route", fake_route)
        t = _register_tools(tools_task)
        t["get_commit_tasks"]("abc123def456", include_task_details=False)
        assert seen["params"] == {
            "commit_hash": "abc123def456", "include_task_details": False,
        }

    @pytest.mark.parametrize(
        "module,tool_name,rpc_method,args,kwargs,expect_params",
        ROUTE_CASES,
        ids=_IDS,
    )
    def test_tools_fail_closed_no_local_fallback(
        self, monkeypatch, module, tool_name, rpc_method, args, kwargs, expect_params
    ):
        """`_route` 抛 DaemonRemoteError → 原样传播，绝不回落本地 get_db。"""
        def fake_route(method, params, op_class):
            raise DaemonRemoteError("E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达")

        monkeypatch.setattr(module, "_route", fake_route)
        q = _register_tools(module)
        with patch(f"{module.__name__}.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                q[tool_name](*args, **kwargs)
            mock_db.assert_not_called()
