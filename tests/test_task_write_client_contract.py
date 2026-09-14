"""Focused thin-client contract tests for T-1788425319705-505b7f04.

These tests exercise CLI boundary behavior only.  They do not start a daemon,
write SQLite, or print a real credential.
"""

from __future__ import annotations

import json

import pytest

import callwarden.cli.main as main_mod


def test_task_report_daemon_rejection_exits_nonzero(monkeypatch, capsys):
    """A rejected task.report must fail a calling shell instead of reporting success."""

    def reject(_method, _params, _fallback):
        raise main_mod.DaemonRemoteError("E_REPORT_REJECTED", "synthetic rejection")

    monkeypatch.setattr(main_mod, "route_task_write", reject)
    monkeypatch.setattr(main_mod, "_fetch_contract_claim", lambda *_args: None)

    with pytest.raises(SystemExit) as exit_info:
        main_mod._handle_task(["report", "T-1", "S-1", "--result", "done"], None)

    assert exit_info.value.code == 2
    output = capsys.readouterr().out
    assert "E_REPORT_REJECTED" in output
    assert "Task Report" not in output


def test_task_report_contract_lookup_failure_exits_nonzero(monkeypatch, capsys):
    """A contract-read rejection must not be swallowed before task.report."""

    def reject_claim(*_args):
        raise main_mod.DaemonRemoteError(
            "E_CONTRACT_LOOKUP_REJECTED", "synthetic contract rejection"
        )

    monkeypatch.setattr(main_mod, "_fetch_contract_claim", reject_claim)
    monkeypatch.setattr(
        main_mod,
        "route_task_write",
        lambda *_args: pytest.fail("task.report must not run after claim failure"),
    )

    with pytest.raises(SystemExit) as exit_info:
        main_mod._handle_task(["report", "T-1", "S-1", "--result", "done"], None)

    assert exit_info.value.code == 2
    output = capsys.readouterr().out
    assert "E_CONTRACT_LOOKUP_REJECTED" in output
    assert "Task Report" not in output


def test_lease_acquire_json_exposes_canonical_and_compat_token(monkeypatch, capsys):
    """Machine output uses lease_token while preserving token for old callers."""

    def acquire(_method, _params, _fallback):
        return True, {
            "lease_id": "L-test",
            "token": "synthetic-secret-for-test-only",
            "fencing_counter": 7,
            "expires_at": 1.0,
        }

    monkeypatch.setattr(main_mod, "_route_lease_write", acquire)

    assert main_mod._handle_lease(
        [
            "acquire", "T-1", "--role", "reviewer", "--agent-id", "agent-1",
            "--session-id", "session-1", "--model-id", "model-1", "--json",
        ],
        None,
    ) is True

    response = json.loads(capsys.readouterr().out)
    assert response["lease_token"] == "synthetic-secret-for-test-only"
    assert response["token"] == response["lease_token"]
    assert response["fencing_counter"] == 7
