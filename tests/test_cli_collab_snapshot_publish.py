"""Regression coverage for the daemon-native snapshot.publish CLI binding.

W17：`cw collab publish` 必须经统一权威路由 ``route_rpc(..., 'GOVERNANCE_WRITE')``
提交 ``snapshot.publish``（HTTP authority face，与 ``cw lease`` / ``cw task`` 写
命令面同一真相源）。CLI 不得再构造本地 ``DaemonClient`` / Unix 传输面，也不得
自行派生 ``workspace_instance_id``——权威值只来自 daemon ``workspace.register``。
"""

import os
import re

import pytest

import callwarden.cli.main as main_mod
from callwarden.server import daemon_client


@pytest.fixture(autouse=True)
def _reset_http_singleton():
    """HttpDaemonRpcClient 是单例：隔离 _project_root / 已注册 identity。"""
    daemon_client.HttpDaemonRpcClient.reset_instance()
    yield
    daemon_client.HttpDaemonRpcClient.reset_instance()


def _install_http_authority(monkeypatch, workspace=None, error=None):
    """把 HttpDaemonRpcClient.call 替换为记录式假体（不触网）。

    只替换最底层的 ``call``，保留真实 ``configure_workspace`` /
    ``_ensure_remote_snapshot`` 逻辑，使断言覆盖真实的 authority 注入路径。
    """
    calls = []

    def fake_call(self, method, params=None, request_id=None):
        calls.append((method, params))
        if error is not None:
            raise error
        if method == "workspace.register":
            if workspace is not None:
                return workspace
            return {
                "workspace_instance_id": "authority-ws-1",
                "snapshot_id": "authority-snapshot-1",
            }
        return {"snapshot_id": "snapshot-test-1"}

    monkeypatch.setattr(daemon_client.HttpDaemonRpcClient, "call", fake_call)
    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "http")
    return calls


def test_collab_publish_routes_through_http_authority(monkeypatch, tmp_path):
    calls = _install_http_authority(monkeypatch)

    assert main_mod._handle_collab(
        ["publish", f"--workspace={tmp_path}", "--json"], None
    ) is True

    workspace_root = os.path.abspath(str(tmp_path))
    assert [method for method, _ in calls] == ["workspace.register", "snapshot.publish"]

    register_params = calls[0][1]
    assert register_params["client_view_root"] == workspace_root

    params = calls[1][1]
    assert params["workspace_root"] == workspace_root
    # 权威 instance id 只能来自 workspace.register 的返回值
    assert params["workspace_instance_id"] == "authority-ws-1"
    assert params["db_path"]
    # GOVERNANCE_WRITE 幂等 request_id 由 route_rpc 注入（CLI 不再自造）
    assert re.fullmatch(r"req-[0-9a-f]{12}", params["request_id"])
    # snapshot_id 由 daemon 从 workspace.register 继承，CLI 不再搬运
    # （snapshot_state.rs: (None, Some(registered)) => Some(registered)）
    assert "snapshot_id" not in params


def test_collab_publish_fails_closed_without_authoritative_workspace_id(
    monkeypatch, tmp_path, capsys
):
    calls = _install_http_authority(monkeypatch, workspace={})

    with pytest.raises(SystemExit) as excinfo:
        main_mod._handle_collab(
            ["publish", f"--workspace={tmp_path}", "--json"], None
        )
    # fail-closed：非零 RC，不静默降级、不本地回退
    assert excinfo.value.code not in (0, None)

    output = capsys.readouterr().out
    assert '"code": "E_GOVERNANCE_WRITE_DEGRADED"' in output
    assert "workspace_instance_id" in output
    # 未拿到权威 instance id → 绝不继续发 snapshot.publish
    assert [method for method, _ in calls] == ["workspace.register"]
