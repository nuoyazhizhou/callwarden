"""Task-bound reviewer verdict CLI contract tests.

These tests exercise the supported ``cw collab verdict`` command surface.  The
CLI must submit the complete task-bound provenance envelope to the daemon and
must never replace a daemon failure with a local verdict store.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from callwarden.cli import main as cli_main
from callwarden.server.daemon_client import DaemonUnavailableError


BASE_ARGS = [
    "verdict",
    "--task-id", "T-1",
    "--step-id", "S-1",
    "--contract-id", "C-1",
    "--contract-hash", "task-hash",
    "--contract-revision", "2",
    "--role-contract-id", "RC-1",
    "--role-contract-hash", "role-hash",
    "--role-contract-revision", "3",
    "--snapshot-id", "snap-1",
    "--request-id", "review-T-1-S-1-r1",
    "--phase", "blind_first_pass",
    "--overall", "block",
    "--attestation", "independent review completed",
    "--findings", '[{"code":"RUNTIME_FAILURE","message":"round-trip failed"}]',
    "--agent-id", "reviewer-wb-186loop",
    "--agent-instance-id", "inst-reviewer-wb-186loop",
    "--session-id", "sess-reviewer-independent",
    "--model-id", "workbuddy",
    "--role", "reviewer",
    "--lease-token", "raw-reviewer-token",
    "--fencing-counter", "7",
]


def _mock_daemon(monkeypatch, response=None, error=None):
    client = MagicMock()
    if error is not None:
        client.call_with_autostart.side_effect = error
    else:
        client.call_with_autostart.return_value = response or {"result": {"verdict_id": "V-1"}}
    monkeypatch.setattr(
        "callwarden.server.daemon_client.DaemonClient.get_instance",
        lambda: client,
    )
    return client


def test_task_bound_verdict_submits_complete_provenance(monkeypatch):
    client = _mock_daemon(monkeypatch)

    assert cli_main._handle_collab(BASE_ARGS, None) is True

    client.call_with_autostart.assert_called_once()
    method, params = client.call_with_autostart.call_args.args
    assert method == "verdict.submit"
    assert params["task_id"] == "T-1"
    assert params["step_id"] == "S-1"
    assert params["contract_id"] == "C-1"
    assert params["contract_revision"] == 2
    assert params["role_contract_id"] == "RC-1"
    assert params["role_contract_revision"] == 3
    assert params["overall"] == "block"
    assert params["findings"] == [{"code": "RUNTIME_FAILURE", "message": "round-trip failed"}]
    assert params["identity"] == {
        "agent_id": "reviewer-wb-186loop",
        "agent_instance_id": "inst-reviewer-wb-186loop",
        "session_id": "sess-reviewer-independent",
        "model_id": "workbuddy",
        "role": "reviewer",
    }
    assert params["lease_token"] == "raw-reviewer-token"
    assert params["fencing_counter"] == 7


@pytest.mark.parametrize("flag,value", [("--clause-results", "not-json"), ("--findings", "{")])
def test_task_bound_verdict_rejects_malformed_structured_inputs(monkeypatch, flag, value):
    client = _mock_daemon(monkeypatch)

    assert cli_main._handle_collab(BASE_ARGS + [flag, value, "--json"], None) is True

    client.call_with_autostart.assert_not_called()


def test_task_bound_verdict_requires_reviewer_instance_identity(monkeypatch):
    client = _mock_daemon(monkeypatch)
    args = list(BASE_ARGS)
    index = args.index("--agent-instance-id")
    del args[index:index + 2]

    with pytest.raises(SystemExit):
        cli_main._handle_collab(args + ["--json"], None)

    client.call_with_autostart.assert_not_called()


def test_task_bound_verdict_daemon_unavailable_fails_closed(monkeypatch):
    client = _mock_daemon(monkeypatch, error=DaemonUnavailableError("daemon down"))

    assert cli_main._handle_collab(BASE_ARGS + ["--json"], None) is True

    client.call_with_autostart.assert_called_once()
