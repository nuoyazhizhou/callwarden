"""结构化 task.handoff 信封的纯函数回归测试。"""

import pytest

from callwarden.db.db_tasks import normalize_structured_handoff


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

