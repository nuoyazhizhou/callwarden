"""Regression coverage for the daemon-native snapshot.publish CLI binding."""

import callwarden.cli.main as main_mod
from callwarden.server import daemon_client


class _FakeDaemonClient:
    def __init__(self, workspace=None):
        self.calls = []
        self.workspace = workspace

    def call_with_autostart(self, method, params):
        self.calls.append((method, params))
        if method == "workspace.register":
            return {"result": self.workspace if self.workspace is not None else {
                "workspace_instance_id": "authority-ws-1",
                "snapshot_id": "authority-snapshot-1",
            }}
        return {"result": {"snapshot_id": "snapshot-test-1"}}


def test_collab_publish_sends_authoritative_workspace_instance_id(monkeypatch, tmp_path):
    client = _FakeDaemonClient()
    monkeypatch.setattr(
        daemon_client.DaemonClient,
        "get_instance",
        classmethod(lambda cls: client),
    )

    assert main_mod._handle_collab(
        ["publish", f"--workspace={tmp_path}", "--json"], None
    ) is True

    register_method, register_params = client.calls[0]
    method, params = client.calls[1]
    workspace_root = str(tmp_path.resolve())
    assert register_method == "workspace.register"
    assert register_params["client_view_root"] == workspace_root
    assert method == "snapshot.publish"
    assert params["workspace_root"] == workspace_root
    assert params["workspace_instance_id"] == "authority-ws-1"
    assert params["snapshot_id"] == "authority-snapshot-1"
    assert params["db_path"]


def test_collab_publish_fails_closed_without_authoritative_workspace_id(
    monkeypatch, tmp_path, capsys
):
    client = _FakeDaemonClient(workspace={})
    monkeypatch.setattr(
        daemon_client.DaemonClient,
        "get_instance",
        classmethod(lambda cls: client),
        raising=False,
    )

    assert main_mod._handle_collab(
        ["publish", f"--workspace={tmp_path}", "--json"], None
    ) is True

    output = capsys.readouterr().out
    assert '"code": "E_RPC_FAILED"' in output
    assert "workspace.register 未返回权威 workspace_instance_id" in output
    assert [method for method, _ in client.calls] == ["workspace.register"]
