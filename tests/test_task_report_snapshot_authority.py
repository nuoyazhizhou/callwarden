"""HTTP task.report must obtain a daemon-published snapshot before mutation.

The test uses only fake HTTP clients: it proves the Python layer remains a
thin adapter and never reads a local SQLite path or fabricates a snapshot.
"""

import pytest

from callwarden.server import daemon_client


class _SnapshotClient:
    _project_root = object()
    _remote_snapshot_id = ""

    def __init__(self):
        self.calls = []
        self.snapshot_db_path = None

    def call(self, method, params):
        self.calls.append((method, params))
        assert method == "mcp.common.get_db_path_for_daemon"
        assert params == {}
        return {"db_path": "C:\\daemon-authority\\callwarden.db"}

    def _ensure_remote_snapshot(self, db_path):
        self.snapshot_db_path = db_path
        self._remote_snapshot_id = "snap-daemon-issued"
        return "instance-daemon-issued"


class _RpcClient:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return {"ok": True}


def test_http_task_report_publishes_daemon_snapshot_before_write(monkeypatch):
    snapshot_client = _SnapshotClient()
    rpc_client = _RpcClient()
    monkeypatch.setattr(daemon_client, "get_daemon_mode", lambda: "enterprise")
    monkeypatch.setattr(daemon_client, "is_http_transport_enabled", lambda: True)
    monkeypatch.setattr(
        daemon_client.HttpDaemonRpcClient,
        "get_instance",
        classmethod(lambda cls: snapshot_client),
    )
    monkeypatch.setattr(daemon_client, "_get_rpc_client_for_route", lambda: rpc_client)
    monkeypatch.setattr(daemon_client, "_inject_workspace_id", lambda params: dict(params))

    result = daemon_client.route_task_write(
        "task.report", {"task_id": "T-snapshot"}, lambda: pytest.fail("local fallback")
    )

    assert result == {"ok": True}
    assert snapshot_client.snapshot_db_path == "C:\\daemon-authority\\callwarden.db"
    assert len(rpc_client.calls) == 1
    method, params = rpc_client.calls[0]
    assert method == "task.report"
    assert params["task_id"] == "T-snapshot"
    assert params["workspace_instance_id"] == "instance-daemon-issued"
    assert params["snapshot_id"] == "snap-daemon-issued"
    assert str(params["request_id"]).startswith("req-")


def test_http_task_report_rejects_when_daemon_omits_snapshot_db_path(monkeypatch):
    class _NoPathClient(_SnapshotClient):
        def call(self, method, params):
            return {}

    snapshot_client = _NoPathClient()
    monkeypatch.setattr(daemon_client, "get_daemon_mode", lambda: "enterprise")
    monkeypatch.setattr(daemon_client, "is_http_transport_enabled", lambda: True)
    monkeypatch.setattr(
        daemon_client.HttpDaemonRpcClient,
        "get_instance",
        classmethod(lambda cls: snapshot_client),
    )

    with pytest.raises(daemon_client.DaemonUnavailableError) as exc_info:
        daemon_client.route_task_write(
            "task.report", {"task_id": "T-snapshot"}, lambda: pytest.fail("local fallback")
        )

    assert getattr(exc_info.value, "code", "") == "E_TASK_REPORT_SNAPSHOT_REQUIRED"
