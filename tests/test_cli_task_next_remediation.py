"""`cw task next` must forward daemon-issued remediation step IDs unchanged."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from callwarden.cli import main as main_mod  # noqa: E402
from callwarden.i18n import set_language  # noqa: E402

set_language("zh_CN")


class _ForbiddenLocalDb:
    def task_next_step(self, _task_id):
        raise AssertionError("daemon task.claim must not fall back to local DB")


def test_task_next_forwards_explicit_remediation_step_id(monkeypatch):
    captured = {}

    def fake_route_task_write(method, params, _fallback):
        captured["method"] = method
        captured["params"] = params
        return {
            "step_id": params["remediation_step_id"],
            "step_index": 4,
            "action": "fix_defect",
            "status": "in_progress",
        }

    monkeypatch.setattr(main_mod, "route_task_write", fake_route_task_write)
    monkeypatch.setattr(main_mod, "_fetch_contract_claim", lambda _task_id, _role: None)
    monkeypatch.setattr(main_mod, "_resolve_action_session", lambda _identity: "test-session")

    assert main_mod._handle_task(
        ["next", "T-REMEDIATION-CLI-001", "--remediation-step-id", "S-FIX-001"],
        _ForbiddenLocalDb(),
    ) is True
    assert captured["method"] == "task.claim"
    assert captured["params"]["task_id"] == "T-REMEDIATION-CLI-001"
    assert captured["params"]["remediation_step_id"] == "S-FIX-001"
    assert captured["params"]["agent_session_id"] == "test-session"


def test_task_next_omits_remediation_step_id_when_not_requested(monkeypatch):
    captured = {}

    def fake_route_task_write(method, params, _fallback):
        captured["method"] = method
        captured["params"] = params
        return {"step_id": "S-NORMAL-001", "step_index": 0, "action": "implement", "status": "in_progress"}

    monkeypatch.setattr(main_mod, "route_task_write", fake_route_task_write)
    monkeypatch.setattr(main_mod, "_fetch_contract_claim", lambda _task_id, _role: None)
    monkeypatch.setattr(main_mod, "_resolve_action_session", lambda _identity: "test-session")

    assert main_mod._handle_task(["next", "T-NORMAL-CLI-001"], _ForbiddenLocalDb()) is True
    assert captured["method"] == "task.claim"
    assert "remediation_step_id" not in captured["params"]
