"""结构化 task.handoff 信封的纯函数回归测试。"""

import pytest

from callwarden.db.db_tasks import normalize_structured_handoff
from callwarden.cli.main import normalize_structured_handoff as normalize_cli_handoff


def _payload(**overrides):
    value = {
        "task_id": "T-test",
        "from_role": "executor",
        "outcome": "executor_ready_for_review",
        "next_role": "reviewer",
        "next_action": "独立复核",
        "reason": "实现和证据已完成",
        "independence_requirement": "required",
        "request_id": "req-test-1",
        "step_id": "S-test",
        "report_request_id": "req-report-1",
        "evidence_path": "docs/evidence/test.json",
        "evidence_hash": "sha256:test",
        "identity": {
            "agent_id": "agent-test",
            "session_id": "session-test",
            "model_id": "gpt-test",
            "role": "implementer",
        },
    }
    value.update(overrides)
    return value


def test_normalize_structured_handoff_accepts_runtime_executor_role():
    normalized = normalize_structured_handoff(_payload())
    assert normalized["outcome"] == "executor_ready_for_review"


def test_cli_normalize_accepts_task_level_reviewer_blocked_null_step():
    payload = _payload(
        from_role="reviewer",
        outcome="reviewer_blocked",
        next_role="executor",
        next_action="修复 task-level finding",
        reason="结构化 finding",
        independence_requirement="not_required",
        step_id=None,
        identity={
            "agent_id": "reviewer-test",
            "session_id": "reviewer-session",
            "model_id": "reviewer-model",
            "role": "reviewer",
        },
    )
    normalized = normalize_cli_handoff(payload)
    assert normalized["step_id"] is None


def test_cli_normalize_rejects_null_step_for_executor_handoff():
    with pytest.raises(ValueError, match="E_HANDOFF_STEP_REQUIRED"):
        normalize_cli_handoff(_payload(step_id=None))


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"next_role": "reviewer", "independence_requirement": "not_required"}, "E_HANDOFF_ROUTE_INVALID"),
        ({"outcome": "unknown"}, "E_HANDOFF_OUTCOME_INVALID"),
        ({"identity": {"agent_id": "agent-test"}}, "E_IDENTITY_REQUIRED"),
        ({"target_agent": "reviewer"}, None),
    ],
)
def test_normalize_structured_handoff_rejects_invalid_or_legacy_payload(overrides, code):
    if code is None:
        payload = {"task_id": "T-test", "target_agent": "reviewer", "reason": "legacy"}
    else:
        payload = _payload(**overrides)
    with pytest.raises(ValueError) as excinfo:
        normalize_structured_handoff(payload)
    if code:
        assert str(excinfo.value).startswith(code)
