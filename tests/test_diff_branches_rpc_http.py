"""W4-4（T-1786886251769-22b94ee8-sub-4）：diff_branches HTTP native 迁移 RPC 测试

覆盖 1 个迁移工具（diff_branches）的 6 问验收
（与 test_coverage_review_rpc_http.py / test_git_read_rpc_http.py 同构）：
① workspace_id 绑定：便捷方法经 `_ensure_remote_snapshot` 注入权威
   workspace_instance_id（缺注入 Rust handler 强制 require →
   invalid_params）。
② 结果限定/参数透传：source_branch / target_branch 原样透传（params
   不增不减不篡改）。
③ 越界参数 fail-closed：`_ensure_remote_snapshot` 返回 None（注册失败
   边界）时不注入 workspace_instance_id，params 保持原样（Rust 侧 require
   拒绝）。
④ snapshot_not_ready：`_ensure_remote_snapshot` 抛错（未发布 snapshot）
   时异常原样传播，不回退本地 SQL。
⑤ 跨 workspace 隔离：diff_branches 是**跨 workspace 语义**（按分支名查
   source/target 两个 workspace，复刻 db_branch.py `diff_branches`）——
   便捷方法只注入连接级 workspace_instance_id（Rust handler 用于打开
   snapshot 库，owned_workspace ACL），**不**注入任一分支的 workspace_id；
   不同 db_path → 不同 workspace_instance_id，同一 db_path 幂等复用。
⑥ Python fallback 边界：HTTP 模式（默认）走 client 便捷方法且 client
   失败时 fail-closed 传播（不调 get_db）；legacy
   （is_http_transport_enabled()=False + local 模式）才进入本地 db 回退
   （db.diff_branches 纯 SELECT，语义不变）。

语义差异风险点（记录）：
- Rust handler 用 `workspaces WHERE name = ?` 精确匹配（取首行），与 Python
  `_find_workspace_by_name` 一致；任一分支不存在 → {"error": "源分支不存在: X"}
  / {"error": "目标分支不存在: X"}（正常响应体，非 RPC 错误）。
- 三集合顺序：target 遍历 → added（source 无此 qn）/ modified（hash 不同）/
  unchanged_count（hash 相同）；source 遍历 → removed。列表顺序 = SELECT
  行序（Python dict 插入序 = SQLite 行序，无 ORDER BY），Rust 用 Vec 保序 +
  HashMap 索引复刻（重复 qn 覆盖值不改位置）。

写面决策（import_git_history，见 ledger §9.25）：
- import_git_history 是 governance_write（INSERT OR IGNORE git_commits）+
  依赖 git 子进程（workspace_root 下 .git + `git log`），按 MVP 计划 §4
  写面 fail-closed 契约**不迁移** rust_native，保持 python_compat；
- HTTP 模式显式 fail-closed：返回 E_HTTP_COMPAT_UNSUPPORTED，不直连本地
  SQLite 写主库（tools_workspace.py import_git_history 已加拦截）；legacy
  模式保持本地执行（db.import_git_history）。
"""

from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_security
from callwarden.server.tools import tools_workspace

DB_A = "/tmp/w4_4_a.db"
DB_B = "/tmp/w4_4_b.db"

# 便捷方法名 → (RPC method, 业务参数，不含 db_path/workspace_instance_id)
CONVENIENCE_CASES = [
    ("diff_branches", "query.diff_branches",
     {"source_branch": "main", "target_branch": "feature-x"}),
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
            client.diff_branches("main", "feature-x", db_path=None)
        mock_ensure.assert_called_once_with(None)
        _, params = mock_call.call_args[0]
        assert params["workspace_instance_id"] == "ws-auth-1"


# ============================================================
# ② 结果限定（参数原样透传）
# ============================================================

class TestParamPropagation:
    """source_branch / target_branch 原样透传。"""

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
        """便捷方法不夹带未声明的业务参数（仅注入 workspace_instance_id）。

        diff_branches 是跨 workspace 语义：source/target 是分支名（workspace
        name），不是 workspace_id——便捷方法不得注入任一分支的 workspace_id
        （Rust handler 按分支名精确匹配 + 连接级 ACL，见类 docstring）。
        """
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
                client.diff_branches("main", "feature-x", db_path=DB_A)
        assert excinfo.value.code == "invalid_params"


# ============================================================
# ⑤ 跨 workspace 隔离
# ============================================================

class TestCrossWorkspaceIsolation:
    """不同 db_path → 不同 workspace_instance_id；同一 db_path 幂等复用。

    diff_branches 是跨 workspace 语义（按分支名查 source/target 两个
    workspace，复刻 db_branch.py `diff_branches`）：workspace_instance_id
    仅用于连接级 ACL（打开 snapshot 库），不绑定任一分支；分支名作为业务
    参数原样透传（见 TestParamPropagation.test_all_methods_do_not_add_unknown_params）。
    """

    def test_distinct_db_paths_get_distinct_instance_ids(self):
        client = _make_client()

        def fake_ensure(db_path):
            return {DB_A: "ws-A", DB_B: "ws-B"}.get(db_path)

        with patch.object(client, "_ensure_remote_snapshot", side_effect=fake_ensure), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.diff_branches("main", "feature-x", db_path=DB_A)
            client.diff_branches("dev", "main", db_path=DB_B)

        calls = mock_call.call_args_list
        assert calls[0][0][1]["workspace_instance_id"] == "ws-A"
        assert calls[0][0][1]["source_branch"] == "main"
        assert calls[0][0][1]["target_branch"] == "feature-x"
        assert calls[1][0][1]["workspace_instance_id"] == "ws-B"
        assert calls[1][0][1]["source_branch"] == "dev"
        assert calls[1][0][1]["target_branch"] == "main"

    def test_same_db_path_reuses_same_instance_id(self):
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-A"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.diff_branches("main", "feature-x", db_path=DB_A)
            client.diff_branches("main", "dev", db_path=DB_A)

        assert mock_ensure.call_count == 2
        for call in mock_call.call_args_list:
            assert call[0][1]["workspace_instance_id"] == "ws-A"


# ============================================================
# ⑥ Python fallback 边界
# ============================================================

class TestPythonFallbackBoundary:
    """HTTP 模式 fail-closed（不回落 get_db）；legacy 模式才走本地回退。

    tools_security.diff_branches 函数体内局部 import
    `callwarden.server.daemon_client.is_http_transport_enabled` 与
    `.._mcp_common._get_daemon_client / _get_db_path_for_daemon`
    （动态读取，monkeypatch 目标为 daemon_client / _mcp_common 模块属性）。
    """

    # --------------------------------------------------------
    # tools_security.diff_branches
    # --------------------------------------------------------

    def test_diff_http_mode_fail_closed(self, monkeypatch):
        client = MagicMock()
        client.diff_branches.side_effect = DaemonRemoteError(
            "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: True
        )
        monkeypatch.setattr(
            "callwarden.server._mcp_common._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server._mcp_common._get_db_path_for_daemon", lambda: DB_A
        )
        q = _register_tools(tools_security)
        with patch("callwarden.server.tools.tools_security.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                q["diff_branches"]("main", "feature-x")
            mock_db.assert_not_called()
        client.diff_branches.assert_called_once_with(
            source_branch="main", target_branch="feature-x", db_path=DB_A,
        )

    def test_diff_http_mode_result_passthrough(self, monkeypatch):
        client = MagicMock()
        client.diff_branches.return_value = {
            "added": [{"qualified_name": "a", "symbol_hash": "h1",
                       "name": "a", "kind": "function"}],
            "removed": [],
            "modified": [],
            "unchanged_count": 0,
        }
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: True
        )
        monkeypatch.setattr(
            "callwarden.server._mcp_common._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server._mcp_common._get_db_path_for_daemon", lambda: DB_A
        )
        q = _register_tools(tools_security)
        result = q["diff_branches"]("main", "feature-x")
        assert result["added"][0]["qualified_name"] == "a"
        assert result["unchanged_count"] == 0

    def test_diff_http_mode_error_body_passthrough(self, monkeypatch):
        """分支不存在等业务错误是正常响应体（{"error": ...}），原样透传。"""
        client = MagicMock()
        client.diff_branches.return_value = {"error": "源分支不存在: main"}
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: True
        )
        monkeypatch.setattr(
            "callwarden.server._mcp_common._get_daemon_client", lambda: client
        )
        monkeypatch.setattr(
            "callwarden.server._mcp_common._get_db_path_for_daemon", lambda: DB_A
        )
        q = _register_tools(tools_security)
        result = q["diff_branches"]("main", "feature-x")
        assert result == {"error": "源分支不存在: main"}

    def test_diff_legacy_local_mode_keeps_db_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode", lambda: "local"
        )
        q = _register_tools(tools_security)
        mock_db = MagicMock()
        mock_db.conn = MagicMock()
        mock_db.diff_branches.return_value = {
            "added": [], "removed": [], "modified": [], "unchanged_count": 3,
        }
        with patch("callwarden.server.tools.tools_security.get_db") as mock_get_db:
            mock_get_db.return_value = mock_db
            result = q["diff_branches"]("main", "feature-x")
        assert result["unchanged_count"] == 3
        mock_db.diff_branches.assert_called_once_with("main", "feature-x")

    def test_diff_legacy_local_mode_db_error_body(self, monkeypatch):
        """legacy 模式 db 层异常 → {"error": str(e)}（保留原 try-except 降级语义）。"""
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode", lambda: "local"
        )
        q = _register_tools(tools_security)
        with patch("callwarden.server.tools.tools_security.get_db") as mock_get_db:
            mock_get_db.side_effect = RuntimeError("db boom")
            result = q["diff_branches"]("main", "feature-x")
        assert result == {"error": "db boom"}


# ============================================================
# 写面决策：import_git_history 保持 python_compat（HTTP fail-closed）
# ============================================================

class TestImportGitHistoryWriteChannel:
    """import_git_history 写面通道决策（ledger §9.25）。

    governance_write（INSERT OR IGNORE git_commits）+ 依赖 git 子进程，按
    http-daemon-mvp-task-plan §4 写面 fail-closed 契约**不迁移** rust_native：
    - HTTP 模式显式返回 E_HTTP_COMPAT_UNSUPPORTED（tools_workspace.py
      import_git_history 已加拦截），不直连本地 SQLite 写主库、不调 get_db；
    - legacy 模式保持本地执行（db.import_git_history）。
    """

    def test_http_mode_fail_closed_no_db_write(self, monkeypatch):
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: True
        )
        q = _register_tools(tools_workspace)
        with patch("callwarden.server.tools.tools_workspace.get_db") as mock_db:
            result = q["import_git_history"](max_commits=50)
            mock_db.assert_not_called()
        assert result["error"] == "E_HTTP_COMPAT_UNSUPPORTED"
        assert result["tool"] == "import_git_history"
        assert result["backend"] == "python_compat"

    def test_legacy_local_mode_keeps_db_write(self, monkeypatch):
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled", lambda: False
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode", lambda: "local"
        )
        q = _register_tools(tools_workspace)
        mock_db = MagicMock()
        mock_db.conn = MagicMock()
        mock_db.import_git_history.return_value = {"ok": True, "imported": 10}
        with patch("callwarden.server.tools.tools_workspace.get_db") as mock_get_db:
            mock_get_db.return_value = mock_db
            result = q["import_git_history"](max_commits=50)
        assert result == {"ok": True, "imported": 10}
        mock_db.import_git_history.assert_called_once_with(max_commits=50)
