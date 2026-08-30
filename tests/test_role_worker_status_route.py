"""Regression tests for the credential-free Role Worker status route.

``role_worker.status`` is owner-scoped by the daemon transport peer and does
not require a published workspace snapshot.  Keeping that distinction in the
thin client prevents a status diagnostic from being turned into an unrelated
workspace-authority failure.
"""

from unittest.mock import patch

from callwarden.server import daemon_client


class _FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return {"ok": True, "status": "active"}


def test_role_worker_status_does_not_inject_workspace_or_publish_snapshot():
    fake = _FakeClient()
    params = {"role_worker_id": "cw-adjudicator-p0j-v1"}

    with patch.object(daemon_client, "get_daemon_mode", return_value="enterprise"), \
            patch.object(daemon_client, "is_http_transport_enabled", return_value=True), \
            patch.object(daemon_client, "_get_rpc_client_for_route", return_value=fake):
        result = daemon_client.route_rpc("role_worker.status", params)

    assert result == {"ok": True, "status": "active"}
    assert fake.calls == [("role_worker.status", params)]
    assert "workspace_instance_id" not in fake.calls[0][1]
