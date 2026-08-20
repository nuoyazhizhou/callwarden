"""Enterprise task mutation identity routing regression tests."""

from types import SimpleNamespace
import inspect

from callwarden.cli import main as cli_main
from callwarden.server.tools import tools_task


def _identity():
    return {
        "agent_id": "agent-test",
        "session_id": "session-test",
        "model_id": "model-test",
        "role": "implementer",
    }


def test_enterprise_identity_is_delegated_to_daemon(monkeypatch):
    monkeypatch.setattr(cli_main, "get_daemon_mode", lambda: "enterprise")
    ok, reason = cli_main._validate_identity_routed(object(), _identity())
    assert ok is True
    assert reason["code"] == "OK"
    assert cli_main._method_accepts_identity_routed(object(), "task_report_step")
    assert cli_main._method_accepts_identity_routed(object(), "task_apply")
    assert cli_main._method_accepts_identity_routed(object(), "task_close")
    assert cli_main._method_accepts_identity_routed(object(), "task_reopen")


def test_enterprise_identity_does_not_claim_unknown_method(monkeypatch):
    monkeypatch.setattr(cli_main, "get_daemon_mode", lambda: "enterprise")
    assert not cli_main._method_accepts_identity_routed(object(), "task.capture_diff")


def test_local_identity_still_uses_database_validator(monkeypatch):
    monkeypatch.setattr(cli_main, "get_daemon_mode", lambda: "local")
    db = SimpleNamespace(validate_action_identity=lambda identity: (True, {"code": "OK"}))
    ok, reason = cli_main._validate_identity_routed(db, _identity())
    assert ok is True
    assert reason["code"] == "OK"


def test_mcp_task_report_step_declares_identity_parameter():
    """MCP 入口必须把结构化身份显式暴露给 daemon 路由。"""
    functions = {}

    class FakeMcp:
        def tool(self):
            def decorator(func):
                functions[func.__name__] = func
                return func
            return decorator

    tools_task.register(FakeMcp())
    signature = inspect.signature(functions["task_report_step"])
    assert "identity" in signature.parameters
