"""Regression tests for authority-owned snapshot identity propagation."""

from unittest.mock import MagicMock

import pytest

from callwarden.server.daemon_client import (
    DaemonUnavailableError,
    HttpDaemonRpcClient,
    UnixDaemonRpcClient,
)


def _http_client(monkeypatch, calls):
    client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
    client._remote_workspace_id = None
    client._remote_snapshot_id = None
    client._remote_snapshot_ready = False
    client._project_root = r"C:\git_work\callwarden"
    client.call = MagicMock(side_effect=calls)
    monkeypatch.setattr(
        "callwarden.server.daemon_client._workspace_snapshot_metadata",
        lambda root: {
            "git_remote_url": "https://example.test/callwarden.git",
            "git_head_commit_sha": "head-001",
        },
    )
    return client


def test_http_snapshot_round_trip_binds_registered_identity(monkeypatch, tmp_path):
    client = _http_client(
        monkeypatch,
        [
            {
                "workspace_instance_id": "ws-authority",
                "snapshot_id": "snap-authority",
            },
            {
                "workspace_instance_id": "ws-authority",
                "snapshot_id": "snap-authority",
            },
        ],
    )

    assert client._ensure_remote_snapshot(str(tmp_path / "callwarden.db")) == "ws-authority"

    assert client.call.call_args_list[0].args == (
        "workspace.register",
        {
            "client_view_root": r"C:\git_work\callwarden",
            "git_remote_url": "https://example.test/callwarden.git",
            "git_head_commit_sha": "head-001",
        },
    )
    assert client.call.call_args_list[1].args[0] == "snapshot.publish"
    assert client.call.call_args_list[1].args[1]["snapshot_id"] == "snap-authority"
    assert client._remote_snapshot_id == "snap-authority"


def test_http_snapshot_round_trip_rejects_identity_drift(monkeypatch, tmp_path):
    client = _http_client(
        monkeypatch,
        [
            {
                "workspace_instance_id": "ws-authority",
                "snapshot_id": "snap-authority",
            },
            {
                "workspace_instance_id": "ws-authority",
                "snapshot_id": "snap-other",
            },
        ],
    )

    with pytest.raises(DaemonUnavailableError) as exc_info:
        client._ensure_remote_snapshot(str(tmp_path / "callwarden.db"))
    assert exc_info.value.code == "E_SNAPSHOT_ID_MISMATCH"


def test_unix_snapshot_publish_forwards_authority_identity(monkeypatch):
    client = UnixDaemonRpcClient.__new__(UnixDaemonRpcClient)
    client.call = MagicMock(
        side_effect=[
            {"db_path": r"C:\authority\callwarden.db"},
            {"snapshot_id": "snap-authority"},
        ]
    )

    result = client.publish_snapshot(
        "ws-authority",
        r"C:\local\callwarden.db",
        snapshot_id="snap-authority",
    )

    assert result["snapshot_id"] == "snap-authority"
    assert client.call.call_args_list[0].args[1]["snapshot_id"] == "snap-authority"
    assert client.call.call_args_list[1].args[1]["snapshot_id"] == "snap-authority"
