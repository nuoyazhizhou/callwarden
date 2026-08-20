"""W3-2（T-1786861820151-f3cecf40）：job 读组 3 工具 HTTP native 迁移 RPC 测试

覆盖 3 个 job 读工具（get_job_status / list_jobs / wait_for_job）的 6 问验收
（与 test_build_read_rpc_http.py / test_task_stats_rpc_http.py 同构）：
① workspace_id 绑定：3 个便捷方法均经 `_ensure_remote_snapshot` 注入权威
   workspace_instance_id（缺注入 Rust handler 强制 require →
   invalid_params，与 W2/W3-1 各组同构）。
② 结果限定/参数透传：job_id / job_type / status / limit / timeout /
   poll_interval 原样透传（params 不增不减不篡改）。
③ 越界参数 fail-closed：`_ensure_remote_snapshot` 返回 None（注册失败
   边界）时不注入 workspace_instance_id，params 保持原样（Rust 侧 require
   拒绝；limit<0 / timeout<0 / poll_interval<0 由 Rust handler 返回
   invalid_params，真实拒绝行为由真实 HTTP probe 覆盖）。
④ snapshot_not_ready：`_ensure_remote_snapshot` 抛错（未发布 snapshot）
   时异常原样传播，不回退本地 SQL。
⑤ 跨 workspace 隔离：不同 db_path → 不同 workspace_instance_id 注入，
   同一 db_path 幂等复用；Rust 查询按 workspace_id 限定（job 属于其他
   workspace → not found，fail-closed）。
⑥ Python fallback 边界：HTTP 模式（默认）3 工具走 client 便捷方法且
   client 失败时 fail-closed 传播（不调 get_db）；legacy
   （is_http_transport_enabled()=False + local 模式）才进入
   route_worker_call 本地 db 回退。
"""

from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_task

DB_A = "/tmp/w3_2_a.db"
DB_B = "/tmp/w3_2_b.db"

# 便捷方法名 → (RPC method, 业务参数，不含 db_path/workspace_instance_id)
CONVENIENCE_CASES = [
    ("get_job_status", "task.job_status",
     {"job_id": "J-1783698970719-3a4b5c6d"}),
    ("list_jobs", "task.list_jobs",
     {"job_type": "", "status": "", "limit": 50}),
    ("wait_for_job", "task.wait_for_job",
     {"job_id": "J-1783698970719-3a4b5c6d", "timeout": 5.0, "poll_interval": 0.1}),
]

# 工具名 → 业务参数（tools_task 注册的 MCP 工具签名）
RULES_TOOL_CASES = [
    ("get_job_status", {"job_id": "J-1783698970719-3a4b5c6d"}),
    ("list_jobs", {"job_type": "", "status": "", "limit": 50}),
    ("wait_for_job", {"job_id": "J-1783698970719-3a4b5c6d",
                      "timeout": 5.0, "poll_interval": 0.1}),
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
    """3 个便捷方法均注入权威 workspace_instance_id，且 db_path 传给 _ensure_remote_snapshot。"""

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
            client.get_job_status(
                job_id="J-1", db_path=None,
            )
        mock_ensure.assert_called_once_with(None)
        _, params = mock_call.call_args[0]
        assert params["workspace_instance_id"] == "ws-auth-1"


# ============================================================
# ② 结果限定（参数原样透传）
# ============================================================

class TestParamPropagation:
    """job 读组的 job_id / job_type / status / limit / timeout / poll_interval
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

    def test_list_jobs_defaults_passthrough(self):
        """list_jobs 未传 job_type/status/limit 时默认值（""/""/100）原样透传。"""
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.list_jobs(db_path=DB_A)
        _, called_params = mock_call.call_args[0]
        assert called_params["job_type"] == ""
        assert called_params["status"] == ""
        assert called_params["limit"] == 100

    def test_wait_for_job_defaults_passthrough(self):
        """wait_for_job 未传 timeout/poll_interval 时默认值（30.0/0.5）原样透传。"""
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.wait_for_job(job_id="J-1", db_path=DB_A)
        _, called_params = mock_call.call_args[0]
        assert called_params["timeout"] == 30.0
        assert called_params["poll_interval"] == 0.5

    def test_all_methods_do_not_add_unknown_params(self):
        """3 个便捷方法均不夹带未声明的业务参数。"""
        client = _make_client()
        known = {"job_id", "job_type", "status", "limit", "timeout",
                 "poll_interval", "workspace_instance_id"}
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
    错误），Rust handler 对缺失 workspace_instance_id 强制 require → invalid_params；
    limit<0 / timeout<0 / poll_interval<0 亦由 Rust handler 拒绝
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

    def test_negative_limit_passthrough_to_rust(self):
        """list_jobs limit=-1 原样透传（Python 不拦截），Rust 侧 invalid_params fail-closed。"""
        client = _make_client()
        with patch.object(client, "_ensure_remote_snapshot", return_value="ws-1"), \
                patch.object(client, "call", return_value={}) as mock_call:
            client.list_jobs(limit=-1, db_path=DB_A)
        _, called_params = mock_call.call_args[0]
        assert called_params["limit"] == -1


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
                client.get_job_status(job_id="J-1", db_path=DB_A)
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
            client.get_job_status(job_id="J-1", db_path=DB_A)
            client.list_jobs(db_path=DB_B)

        calls = mock_call.call_args_list
        assert calls[0][0][1]["workspace_instance_id"] == "ws-A"
        assert calls[1][0][1]["workspace_instance_id"] == "ws-B"

    def test_same_db_path_reuses_same_instance_id(self):
        client = _make_client()
        with patch.object(
            client, "_ensure_remote_snapshot", return_value="ws-A"
        ) as mock_ensure, patch.object(client, "call", return_value={}) as mock_call:
            client.get_job_status(job_id="J-1", db_path=DB_A)
            client.wait_for_job(job_id="J-1", timeout=1.0, db_path=DB_A)

        assert mock_ensure.call_count == 2
        for call in mock_call.call_args_list:
            assert call[0][1]["workspace_instance_id"] == "ws-A"


# ============================================================
# ⑥ Python fallback 边界
# ============================================================

class TestPythonFallbackBoundary:
    """HTTP 模式 fail-closed（不回落 get_db）；legacy 模式才走本地回退。"""

    def _monkeypatch_http_mode(self, monkeypatch, client):
        monkeypatch.setattr(
            "callwarden.server.tools.tools_task._get_daemon_client",
            lambda: client,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_task._get_db_path_for_daemon",
            lambda: DB_A,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_task.is_http_transport_enabled",
            lambda: True,
        )

    def test_http_mode_fail_closed_no_db_fallback(self, monkeypatch):
        """HTTP 模式（默认）3 工具走 client 便捷方法；client 抛错时 fail-closed
        传播，不调用 get_db（无 SQL 回退）。"""
        client = MagicMock()
        for method, _rpc, _params in CONVENIENCE_CASES:
            getattr(client, method).side_effect = DaemonRemoteError(
                "E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达"
            )
        self._monkeypatch_http_mode(monkeypatch, client)
        task_tools = _register_tools(tools_task)
        with patch("callwarden.server.tools.tools_task.get_db") as mock_db:
            for tool_name, params in RULES_TOOL_CASES:
                with pytest.raises(DaemonRemoteError):
                    task_tools[tool_name](**params)
            mock_db.assert_not_called()
        client.get_job_status.assert_called_once_with(
            job_id="J-1783698970719-3a4b5c6d", db_path=DB_A,
        )
        client.list_jobs.assert_called_once_with(
            job_type="", status="", limit=50, db_path=DB_A,
        )
        client.wait_for_job.assert_called_once_with(
            job_id="J-1783698970719-3a4b5c6d", timeout=5.0,
            poll_interval=0.1, db_path=DB_A,
        )

    def test_http_mode_client_result_passthrough(self, monkeypatch):
        """HTTP 模式 3 工具直接返回 client 便捷方法结果（不加工不包装）。"""
        client = MagicMock()
        client.get_job_status.return_value = {
            "job_id": "J-1", "status": "completed", "progress": 1.0,
            "result_summary": {"ok": True}, "error": "",
        }
        client.list_jobs.return_value = [
            {"job_id": "J-1", "status": "completed", "workspace_id": 101}
        ]
        client.wait_for_job.return_value = {
            "job_id": "J-1", "status": "completed", "progress": 1.0,
            "result_summary": {"ok": True}, "error": "", "elapsed": 0.1,
        }
        self._monkeypatch_http_mode(monkeypatch, client)
        task_tools = _register_tools(tools_task)

        assert task_tools["get_job_status"](job_id="J-1")["status"] == "completed"
        assert task_tools["list_jobs"]()[0]["job_id"] == "J-1"
        assert task_tools["wait_for_job"](job_id="J-1")["status"] == "completed"

    def test_legacy_local_mode_keeps_db_fallback(self, monkeypatch):
        """is_http_transport_enabled()=False + local 模式 → 3 工具走
        route_worker_call 本地 db 回退（get_db 被调用，db 层查询函数被调用）。"""
        monkeypatch.setattr(
            "callwarden.server.tools.tools_task.is_http_transport_enabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode",
            lambda: "local",
        )
        task_tools = _register_tools(tools_task)

        mock_db = MagicMock()
        mock_db.conn = MagicMock()

        job = MagicMock()
        job.to_dict.return_value = {
            "job_id": "J-1", "job_type": "clone_detect", "status": "completed",
            "progress": 1.0, "result_summary": {"ok": True}, "error": "",
            "is_terminal": True,
        }
        job.is_terminal = True
        job.status = "completed"
        job.progress = 1.0
        job.result_summary = {"ok": True}
        job.error = ""

        mock_db.get_job.return_value = job
        mock_db.list_jobs.return_value = [job]

        with patch("callwarden.server.tools.tools_task.get_db") as mock_get_db:
            mock_get_db.return_value = mock_db

            r1 = task_tools["get_job_status"](job_id="J-1")
            r2 = task_tools["list_jobs"](job_type="", status="", limit=50)
            r3 = task_tools["wait_for_job"](
                job_id="J-1", timeout=1.0, poll_interval=0.01,
            )

            assert r1["status"] == "completed"
            assert r2[0]["job_id"] == "J-1"
            assert r3["status"] == "completed"
            mock_db.get_job.assert_called()
            mock_db.list_jobs.assert_called_once_with(
                job_type=None, status=None, limit=50,
            )
