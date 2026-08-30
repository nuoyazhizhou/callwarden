"""Workspace authority binding regression tests.

These tests cover the production HTTP thin-client boundary without opening a
local SQLite connection or mutating the shared daemon registry.
"""

from unittest.mock import MagicMock

from callwarden.server.daemon_client import HttpDaemonRpcClient, route_rpc


def test_http_route_binds_project_root_and_publishes_snapshot_for_status(monkeypatch):
    """workspace.status must use the configured project root and authority DB."""

    client = MagicMock()
    client._project_root = None
    client._ensure_remote_snapshot.return_value = "authority-ws-1"
    client.call.side_effect = [
        {"db_path": r"C:\authority\callwarden.db"},
        {"workspace_instance_id": "authority-ws-1", "snapshot_id": "snap-1"},
    ]
    client.call_with_autostart = client.call
    client.configure_workspace.side_effect = lambda root: setattr(
        client, "_project_root", root
    )

    monkeypatch.setattr(
        "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
        lambda: client,
    )
    monkeypatch.setattr(
        "callwarden.server.daemon_client._get_rpc_client_for_route",
        lambda: client,
    )
    monkeypatch.setattr(
        "callwarden.server.daemon_client.is_http_transport_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "callwarden.server.daemon_client.get_daemon_mode",
        lambda: "enterprise",
    )
    monkeypatch.setattr(
        "callwarden.config.PROJECT_ROOT", r"C:\git_work\callwarden"
    )

    result = route_rpc("workspace.status", {})

    assert result["snapshot_id"] == "snap-1"
    client.configure_workspace.assert_called_once_with(r"C:\git_work\callwarden")
    client._ensure_remote_snapshot.assert_called_once_with(
        r"C:\authority\callwarden.db"
    )
    assert client.call.call_args_list[0].args == (
        "mcp.common.get_db_path_for_daemon",
        {},
    )
    assert client.call.call_args_list[1].args == (
        "workspace.status",
        {"workspace_instance_id": "authority-ws-1"},
    )


def test_http_client_default_registration_root_is_not_process_cwd(monkeypatch):
    """An unconfigured client must not register the MCP runtime cwd."""

    client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
    client._remote_workspace_id = None
    client._remote_snapshot_ready = False
    client._project_root = None
    client.call = MagicMock(
        return_value={"workspace_instance_id": "authority-ws-2"}
    )

    monkeypatch.setattr("callwarden.config.PROJECT_ROOT", r"C:\git_work\callwarden")
    client._ensure_remote_snapshot(None)

    client.call.assert_called_once_with(
        "workspace.register", {"client_view_root": r"C:\git_work\callwarden"}
    )
