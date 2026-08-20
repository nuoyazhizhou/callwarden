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
⑥ Python fallback 边界：HTTP 模式（默认）走 client 便捷方法且 client
   失败时 fail-closed 传播（不调 get_db）；legacy
   （is_http_transport_enabled()=False + local 模式）才进入本地 db 回退。

语义差异风险点（记录）：get_file_history 的绝对路径 → rel_path 规范化保留
在 Python 工具层（db 层同源，workspaces.root_path 为真相源），Rust 侧只按
最终 rel_path 精确匹配；get_commit_tasks 复刻 Python 全局查询（无 workspace
维度），git_commits 含 workspace_id 列、git_file_changes 无（经 JOIN 隔离）。
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
# ⑥ Python fallback 边界
# ============================================================

class TestPythonFallbackBoundary:
    """HTTP 模式 fail-closed（不回落 get_db）；legacy 模式才走本地回退。

    tools_query / tools_task 在模块顶层 import `_get_daemon_client` /
    `_get_db_path_for_daemon`（可直接 patch 模块属性）；tools_workspace 在
    函数体内局部 import（需 patch 来源模块 `callwarden.server._mcp_common`）。
    """

    # --------------------------------------------------------
    # tools_query.get_file_history（顶层 import）
    # --------------------------------------------------------

    def test_file_history_http_mode_fail_closed(self, monkeypatch):
        client = MagicMock()
        client.get_file_history.side_effect = DaemonRemoteError(
            "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query._get_db_path_for_daemon", lambda: DB_A
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query.is_http_transport_enabled", lambda: True
        )
        q = _register_tools(tools_query)
        with patch("callwarden.server.tools.tools_query.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                q["get_file_history"]("src/main.py")
            mock_db.assert_not_called()
        client.get_file_history.assert_called_once_with(
            file_path="src/main.py", db_path=DB_A,
        )

    def test_file_history_http_mode_result_passthrough(self, monkeypatch):
        client = MagicMock()
        client.get_file_history.return_value = [
            {"version_num": 2, "rel_path": "src/main.py"}
        ]
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query._get_db_path_for_daemon", lambda: DB_A
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query.is_http_transport_enabled", lambda: True
        )
        q = _register_tools(tools_query)
        result = q["get_file_history"]("src/main.py")
        assert result[0]["version_num"] == 2

    def test_file_history_abs_path_normalized_in_http_mode(self, monkeypatch):
        """绝对路径在 HTTP 分支规范化为 rel_path（复刻 db 层 relpath 语义）。"""
        client = MagicMock()
        client.get_file_history.return_value = []
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query._get_db_path_for_daemon", lambda: DB_A
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query.is_http_transport_enabled", lambda: True
        )
        mock_db = MagicMock()
        mock_db.workspace_root = "C:/repo"
        with patch("callwarden.server.tools.tools_query.get_db", return_value=mock_db):
            q = _register_tools(tools_query)
            q["get_file_history"]("C:/repo/src/main.py")
        client.get_file_history.assert_called_once_with(
            file_path="src/main.py", db_path=DB_A,
        )

    def test_file_history_legacy_local_mode_keeps_db_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode", lambda: "local"
        )
        q = _register_tools(tools_query)
        mock_db = MagicMock()
        mock_db.conn = MagicMock()
        mock_db.get_file_history.return_value = [{"version_num": 1}]
        with patch("callwarden.server.tools.tools_query.get_db") as mock_get_db:
            mock_get_db.return_value = mock_db
            result = q["get_file_history"]("src/main.py")
        assert result[0]["version_num"] == 1
        mock_db.get_file_history.assert_called_once_with("src/main.py")

    # --------------------------------------------------------
    # tools_task.get_commit_tasks（顶层 import）
    # --------------------------------------------------------

    def test_commit_tasks_http_mode_fail_closed(self, monkeypatch):
        client = MagicMock()
        client.get_commit_tasks.side_effect = DaemonRemoteError(
            "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_task._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_task._get_db_path_for_daemon", lambda: DB_A
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_task.is_http_transport_enabled", lambda: True
        )
        t = _register_tools(tools_task)
        with patch("callwarden.server.tools.tools_task.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                t["get_commit_tasks"]("abc123def456")
            mock_db.assert_not_called()
        client.get_commit_tasks.assert_called_once_with(
            commit_hash="abc123def456", include_task_details=True, db_path=DB_A,
        )

    def test_commit_tasks_http_mode_result_passthrough(self, monkeypatch):
        client = MagicMock()
        client.get_commit_tasks.return_value = [{"task_id": "T-1", "change_count": 2}]
        monkeypatch.setattr(
            "callwarden.server.tools.tools_task._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_task._get_db_path_for_daemon", lambda: DB_A
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_task.is_http_transport_enabled", lambda: True
        )
        t = _register_tools(tools_task)
        result = t["get_commit_tasks"]("abc123def456", include_task_details=False)
        assert result[0]["task_id"] == "T-1"
        client.get_commit_tasks.assert_called_once_with(
            commit_hash="abc123def456", include_task_details=False, db_path=DB_A,
        )

    def test_commit_tasks_legacy_local_mode_keeps_db_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "callwarden.server.tools.tools_task.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode", lambda: "local"
        )
        t = _register_tools(tools_task)
        mock_db = MagicMock()
        mock_db.conn = MagicMock()
        mock_db.get_commit_tasks.return_value = [{"task_id": "T-1"}]
        with patch("callwarden.server.tools.tools_task.get_db") as mock_get_db:
            mock_get_db.return_value = mock_db
            result = t["get_commit_tasks"]("abc123def456", include_task_details=False)
        assert result[0]["task_id"] == "T-1"
        mock_db.get_commit_tasks.assert_called_once_with(
            commit_hash="abc123def456", include_task_details=False
        )

    # --------------------------------------------------------
    # tools_workspace.get_git_commits / get_commit_changes / get_git_stats
    # （函数体内局部 import，patch 来源模块）
    # --------------------------------------------------------

    def _patch_workspace_http(self, monkeypatch, client):
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: True
        )
        monkeypatch.setattr(
            "callwarden.server._mcp_common._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server._mcp_common._get_db_path_for_daemon", lambda: DB_A
        )

    def test_git_tools_http_mode_fail_closed(self, monkeypatch):
        client = MagicMock()
        client.get_git_commits.side_effect = DaemonRemoteError(
            "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
        )
        client.get_commit_changes.side_effect = DaemonRemoteError(
            "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
        )
        client.get_git_stats.side_effect = DaemonRemoteError(
            "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
        )
        self._patch_workspace_http(monkeypatch, client)
        w = _register_tools(tools_workspace)
        with patch("callwarden.server.tools.tools_workspace.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                w["get_git_commits"](limit=5)
            with pytest.raises(DaemonRemoteError):
                w["get_commit_changes"]("abc")
            with pytest.raises(DaemonRemoteError):
                w["get_git_stats"]()
            mock_db.assert_not_called()

    def test_git_tools_http_mode_result_passthrough(self, monkeypatch):
        client = MagicMock()
        client.get_git_commits.return_value = [{"commit_hash": "abc"}]
        client.get_commit_changes.return_value = {"commit": None, "file_changes": []}
        client.get_git_stats.return_value = {"commit_count": 0}
        self._patch_workspace_http(monkeypatch, client)
        w = _register_tools(tools_workspace)
        assert w["get_git_commits"](limit=5)[0]["commit_hash"] == "abc"
        assert w["get_commit_changes"]("abc")["commit"] is None
        assert w["get_git_stats"]()["commit_count"] == 0

    def test_git_tools_legacy_local_mode_keeps_db_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: False
        )
        w = _register_tools(tools_workspace)
        mock_db = MagicMock()
        mock_db.conn = MagicMock()
        mock_db.get_git_commits.return_value = [{"commit_hash": "abc"}]
        mock_db.get_commit_changes.return_value = {"commit": None, "file_changes": []}
        mock_db.get_git_stats.return_value = {"commit_count": 0}
        with patch("callwarden.server.tools.tools_workspace.get_db") as mock_get_db:
            mock_get_db.return_value = mock_db
            assert w["get_git_commits"](limit=5)[0]["commit_hash"] == "abc"
            assert w["get_commit_changes"]("abc")["commit"] is None
            assert w["get_git_stats"]()["commit_count"] == 0
        mock_db.get_git_commits.assert_called_once_with(limit=5, offset=0)
        mock_db.get_commit_changes.assert_called_once_with("abc")
        mock_db.get_git_stats.assert_called_once_with()
