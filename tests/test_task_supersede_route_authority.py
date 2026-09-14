r"""SR-01 route authority regression tests.

Defect under test: route_task_write / route_task_read unconditionally called
_inject_workspace_id, which resolves the legacy ACTIVE workspace of a shared
multi-project daemon instead of the immutable task_workspace_bindings row.
For task.supersede this made the daemon fail closed with
E_WORKSPACE_AUTHORITY_MISMATCH; for task.superseded_by it silently filtered
the projection by the wrong workspace.

Contract proven here (no daemon, no SQLite):
1. _is_task_scoped_authority_request recognizes a non-blank superseded_id
   (task.supersede / task.superseded_by source-task discriminator) in
   addition to task_id; blank/non-string values never match.
2. route_task_write / route_task_read skip the legacy active-workspace
   injection for methods in TASK_SUPERSEDE_ROUTE_POLICY when the request is
   task-scoped, so the daemon resolves the workspace from the immutable
   binding (omitted workspace_id) or validates explicit consistency.
3. Unrelated task.* writes/reads keep the existing injection behavior.
"""

import pytest

from callwarden.server import daemon_client as dc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _FakeRpcClient:
    """Records the params actually forwarded to the daemon RPC."""

    def __init__(self):
        self.calls = []

    def call(self, rpc_method, params):
        self.calls.append((rpc_method, params))
        return {"ok": True, "method": rpc_method}

    call_with_autostart = call


@pytest.fixture()
def routing_env(monkeypatch):
    """Enterprise mode + fake RPC client + spy on _inject_workspace_id."""
    client = _FakeRpcClient()
    injected = []

    def fake_inject(params):
        injected.append(dict(params))
        params = dict(params)
        params["workspace_id"] = 10  # legacy ACTIVE workspace (ws-10)
        return params

    monkeypatch.setattr(dc, "get_daemon_mode", lambda: "enterprise")
    monkeypatch.setattr(dc, "is_http_transport_enabled", lambda: False)
    monkeypatch.setattr(dc, "_get_rpc_client_for_route", lambda: client)
    monkeypatch.setattr(dc, "_inject_workspace_id", fake_inject)
    return {"client": client, "injected": injected}


NO_FALLBACK = lambda: pytest.fail("local fallback must not be called")  # noqa: E731


# ---------------------------------------------------------------------------
# 1) discriminator
# ---------------------------------------------------------------------------

class TestTaskScopedDiscriminator:
    def test_task_id_matches(self):
        assert dc._is_task_scoped_authority_request({"task_id": "T-1"}) is True

    def test_superseded_id_matches(self):
        assert dc._is_task_scoped_authority_request({"superseded_id": "T-old"}) is True

    def test_blank_values_never_match(self):
        assert dc._is_task_scoped_authority_request({"task_id": ""}) is False
        assert dc._is_task_scoped_authority_request({"task_id": "   "}) is False
        assert dc._is_task_scoped_authority_request({"superseded_id": ""}) is False
        assert dc._is_task_scoped_authority_request({}) is False

    def test_non_string_values_never_match(self):
        assert dc._is_task_scoped_authority_request({"task_id": 123}) is False
        assert dc._is_task_scoped_authority_request({"superseded_id": 456}) is False


# ---------------------------------------------------------------------------
# 2) supersede family skips legacy active-workspace injection
# ---------------------------------------------------------------------------

class TestSupersedeFamilySkipsInjection:
    def test_task_supersede_write_with_superseded_id_skips_injection(self, routing_env):
        params = {
            "superseded_id": "T-old",
            "superseding_id": "T-new",
            "reason": "defective creation",
        }
        result = dc.route_task_write("task.supersede", params, NO_FALLBACK)
        assert result["ok"] is True
        assert routing_env["injected"] == []
        _method, forwarded = routing_env["client"].calls[-1]
        assert "workspace_id" not in forwarded

    def test_task_supersede_write_with_task_id_skips_injection(self, routing_env):
        params = {"task_id": "T-old", "superseding_id": "T-new"}
        dc.route_task_write("task.supersede", params, NO_FALLBACK)
        assert routing_env["injected"] == []
        _method, forwarded = routing_env["client"].calls[-1]
        assert "workspace_id" not in forwarded

    def test_task_superseded_by_read_with_superseded_id_skips_injection(self, routing_env):
        params = {"superseded_id": "T-old"}
        result = dc.route_task_read("task.superseded_by", params, NO_FALLBACK)
        assert result["ok"] is True
        assert routing_env["injected"] == []
        _method, forwarded = routing_env["client"].calls[-1]
        assert "workspace_id" not in forwarded

    def test_task_superseded_by_read_with_task_id_skips_injection(self, routing_env):
        params = {"task_id": "T-old"}
        dc.route_task_read("task.superseded_by", params, NO_FALLBACK)
        assert routing_env["injected"] == []
        _method, forwarded = routing_env["client"].calls[-1]
        assert "workspace_id" not in forwarded

    def test_explicit_workspace_id_still_forwarded_untouched(self, routing_env):
        """An explicitly supplied workspace_id is the caller's authority claim:
        forwarded as-is (daemon fails closed on binding mismatch), never
        silently replaced by the active workspace."""
        params = {"superseded_id": "T-old", "workspace_id": 1}
        dc.route_task_write("task.supersede", params, NO_FALLBACK)
        assert routing_env["injected"] == []
        _method, forwarded = routing_env["client"].calls[-1]
        assert forwarded["workspace_id"] == 1


# ---------------------------------------------------------------------------
# 3) unrelated task.* methods keep the existing injection behavior
# ---------------------------------------------------------------------------

class TestNonSupersedeMethodsStillInject:
    def test_task_claim_write_still_injects(self, routing_env):
        params = {"task_id": "T-1"}
        dc.route_task_write("task.claim", params, NO_FALLBACK)
        assert len(routing_env["injected"]) == 1
        _method, forwarded = routing_env["client"].calls[-1]
        assert forwarded["workspace_id"] == 10

    def test_task_list_read_still_injects(self, routing_env):
        dc.route_task_read("task.list", {}, NO_FALLBACK)
        assert len(routing_env["injected"]) == 1
        _method, forwarded = routing_env["client"].calls[-1]
        assert forwarded["workspace_id"] == 10

    def test_supersede_without_task_discriminator_still_injects(self, routing_env):
        """Defense in depth: if a supersede-family request ever arrives
        without any task discriminator, the legacy injection path still
        applies (daemon then fails closed on its own authority checks)."""
        dc.route_task_write("task.supersede", {"reason": "x"}, NO_FALLBACK)
        assert len(routing_env["injected"]) == 1
        _method, forwarded = routing_env["client"].calls[-1]
        assert forwarded["workspace_id"] == 10
