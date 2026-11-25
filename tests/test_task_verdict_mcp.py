"""MCP submit_verdict 的 daemon-native identity/provenance 透传测试。"""

from unittest.mock import patch

import pytest

from callwarden.i18n import get_language, set_language
from callwarden.server.tools import tools_collab


@pytest.fixture(autouse=True)
def _zh_cn_locale():
    """固定输出语言为 zh_CN。

    本文件断言的是中文 i18n 文案；`_current_lang` 是进程级全局，全量运行时
    可能被其他用例改为 en_US（如 test_cli_055 固定 en_US）。显式钉住并还原，
    避免依赖宿主机 locale 与用例执行顺序。
    """
    previous = get_language()
    set_language("zh_CN")
    yield
    set_language(previous)


def _registered_tools():
    registrations = {}

    class FakeMcp:
        def tool(self):
            def decorator(fn):
                registrations[fn.__name__] = fn
                return fn

            return decorator

    tools_collab.register(FakeMcp())
    return registrations


def _base_kwargs():
    return {
        "task_id": "T-MCP-VERDICT",
        "step_id": "S-MCP-VERDICT",
        "contract_id": "TC-MCP",
        "contract_revision": 1,
        "contract_hash": "sha256:task",
        "role_contract_id": "RC-MCP",
        "role_contract_revision": 1,
        "role_contract_hash": "sha256:role",
        "phase": "blind_first_pass",
        "overall": "pass",
        "clause_results": '[{"clause_id":"C1","decision":"pass"}]',
        "findings": "[]",
        "snapshot_id": "snapshot-mcp",
        "view_manifest_hash": "sha256:view",
        "attestation": "reviewed independently",
        "request_id": "review-mcp-verdict-r1",
        "identity_role": "reviewer",
        "identity_agent_id": "reviewer-mcp",
        "identity_agent_instance_id": "reviewer-mcp-instance",
        "identity_session_id": "reviewer-mcp-session",
        "identity_model_id": "reviewer-model",
        "lease_token": "reviewer-lease",
        "fencing_counter": 3,
    }


def test_submit_verdict_forwards_complete_native_identity_and_arrays():
    submit_verdict = _registered_tools()["submit_verdict"]
    with patch.object(
        tools_collab,
        "_route",
        return_value={"success": True, "verdict_id": "V-MCP"},
    ) as route:
        result = submit_verdict(**_base_kwargs())

    assert result == {"success": True, "verdict_id": "V-MCP"}
    method, params, op_class = route.call_args.args
    assert method == "verdict.submit"
    assert op_class == "GOVERNANCE_WRITE"
    assert params["clause_results"] == [{"clause_id": "C1", "decision": "pass"}]
    assert params["findings"] == []
    assert params["identity"] == {
        "agent_id": "reviewer-mcp",
        "agent_instance_id": "reviewer-mcp-instance",
        "session_id": "reviewer-mcp-session",
        "model_id": "reviewer-model",
        "role": "reviewer",
    }
    assert "identity_agent_instance_id" not in params


def test_submit_verdict_rejects_non_array_json_before_rpc():
    submit_verdict = _registered_tools()["submit_verdict"]
    kwargs = _base_kwargs()
    kwargs["findings"] = '{"severity":"high"}'

    with patch.object(tools_collab, "_route") as route:
        try:
            submit_verdict(**kwargs)
        except ValueError as exc:
            assert str(exc) == "findings 必须是 JSON array"
        else:
            raise AssertionError("非数组 findings 必须 fail-closed")
        route.assert_not_called()
