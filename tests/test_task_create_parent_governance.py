"""GATE-1B: exercise parser, request boundary and forbidden fallback paths."""
import json

import pytest

from callwarden.cli import main as cli
from callwarden.server import daemon_client as dc


PAIR = {"workspace_id": 1, "workspace_instance_id": "daemon-instance"}
BASE = ["create", "--title", "child", "--workspace-id", "1",
        "--workspace-instance-id", PAIR["workspace_instance_id"]]


def capture_cli(monkeypatch, response=None, error=None):
    calls = []
    def route(method, params, fallback):
        calls.append((method, params, fallback))
        if error:
            raise error
        return response or {"task_id": "T-child", **PAIR}
    monkeypatch.setattr(cli, "route_task_write", route)
    monkeypatch.setattr(cli, "_verify_create_readback", lambda *a, **k: None)
    monkeypatch.setattr(cli, "resolve_workspace_pair_from_daemon",
                        lambda: pytest.fail("governed create must not infer workspace"))
    return calls


def test_parent_cli_exact_request_and_envelope_bytes(monkeypatch):
    calls = capture_cli(monkeypatch)
    envelope = '{ "contract_id" : "TC-T-child", "identity_policy":"legacy_identity_v1" }'
    roles = [{"role": "executor", "allowed_paths": "a.py"}]
    monkeypatch.setattr(cli, "_build_role_contracts", lambda raw: json.loads(raw))
    cli._handle_task(BASE + ["--parent-id", "T-parent", "--identity-policy", "legacy_identity_v1",
                            "--task-contract-envelope", envelope, "--task-id", "T-child",
                            "--role-contracts", json.dumps(roles)], None)
    assert calls[0][0] == "task.create"
    assert calls[0][1] == {"title": "child", "description": "", "steps": [], **PAIR,
                           "role_contracts": roles, "parent_id": "T-parent",
                           "identity_policy": "legacy_identity_v1", "task_id": "T-child",
                           "task_contract_envelope": envelope}
    assert calls[0][1]["task_contract_envelope"].encode() == envelope.encode()


@pytest.mark.parametrize("message", ["E_TASK_PARENT_NOT_FOUND", "E_WORKSPACE_AUTHORITY_MISMATCH",
                                     "E_TASK_PARENT_CONTRACT_REQUIRED", "E_TASK_IDENTITY_POLICY_REQUIRED",
                                     "E_TASK_CREATE_UNKNOWN_FIELD"])
def test_daemon_error_not_reclassified(monkeypatch, message):
    error = RuntimeError(message)
    calls = capture_cli(monkeypatch, error=error)
    with pytest.raises(RuntimeError) as caught:
        cli._handle_task(BASE + ["--parent-id", "T-parent"], None)
    assert caught.value is error
    assert calls[0][1]["role_contracts"] == []
    assert "identity_policy" not in calls[0][1]


def test_unknown_cli_field_rejected_before_write(monkeypatch):
    calls = capture_cli(monkeypatch)
    with pytest.raises(SystemExit):
        cli._handle_task(BASE + ["--unknown-governance-field", "x"], None)
    assert not calls


@pytest.mark.parametrize("args", [["--parent-id", "T-parent"],
                                  ["--identity-policy", "legacy_identity_v1"],
                                  ["--task-contract-envelope", "{}"]])
def test_governed_workspace_must_be_explicit(monkeypatch, args):
    calls = capture_cli(monkeypatch)
    with pytest.raises(dc.DaemonUnavailableError):
        cli._handle_task(["create", "--title", "child"] + args, None)
    assert not calls


def test_legacy_root_request_unchanged(monkeypatch):
    calls = capture_cli(monkeypatch)
    monkeypatch.setattr(cli, "_build_role_contracts", lambda raw: ["legacy-defaults"])
    cli._handle_task(BASE, None)
    assert calls[0][1] == {"title": "child", "description": "", "steps": [],
                           "creator": "agent", "role_contracts": ["legacy-defaults"],
                           **PAIR, "identity_policy": "legacy_identity_v1"}


def test_typed_adapter_preserves_response_and_envelope():
    envelope = ' { "identity_policy": "legacy_identity_v1" } '
    response = {"task_id": "T-child", "extra": [1, 2]}
    class Client:
        def call(self, method, params):
            assert method == "task.create"
            assert "creator" not in params
            assert params["parent_id"] == "T-parent"
            assert params["task_contract_envelope"] == envelope
            assert params["workspace_instance_id"] == PAIR["workspace_instance_id"]
            return response
    assert dc.UnixDaemonRpcClient.task_create(
        Client(), "child", parent_id="T-parent", **PAIR,
        identity_policy="legacy_identity_v1", task_contract_envelope=envelope) is response


@pytest.mark.parametrize("field,value", [("parent_id", "T-parent"),
                                         ("identity_policy", "legacy_identity_v1"),
                                         ("task_contract_envelope", "{}")])
def test_local_governed_route_never_calls_fallback(monkeypatch, field, value):
    monkeypatch.setattr(dc, "get_daemon_mode", lambda: "local")
    with pytest.raises(dc.DaemonUnavailableError):
        dc.route_task_write("task.create", {**PAIR, field: value},
                            lambda: pytest.fail("local fallback called"))


def test_disconnected_daemon_reports_connection_error(monkeypatch):
    class Client:
        def call(self, *args):
            raise ConnectionError("daemon offline")
    monkeypatch.setattr(dc, "get_daemon_mode", lambda: "auto")
    monkeypatch.setattr(dc, "_get_rpc_client_for_route", lambda: Client())
    monkeypatch.setattr(dc, "is_daemon_required", lambda: False)
    with pytest.raises(dc.DaemonUnavailableError, match="daemon offline"):
        dc.route_task_write("task.create", {**PAIR, "parent_id": "T-parent"},
                            lambda: pytest.fail("local fallback called"))


# --- GATE-1B fix_defect (V-155c02fc finding#2): fail-closed CLI exit code ---

def test_governed_create_daemon_rejection_exits_nonzero(monkeypatch, capsys):
    """daemon 业务拒绝的 governed create 必须以非零退出码终止（此前退出 0）。"""
    error = dc.DaemonRemoteError("E_TASK_PARENT_NOT_FOUND", "parent missing")
    capture_cli(monkeypatch, error=error)
    with pytest.raises(SystemExit) as caught:
        cli._handle_task(BASE + ["--parent-id", "T-parent"], None)
    assert caught.value.code == 2
    out = capsys.readouterr().out
    assert "E_TASK_PARENT_NOT_FOUND" in out


def test_governed_create_daemon_unavailable_exits_nonzero(monkeypatch, capsys):
    """daemon 权威不可用时 governed create 同样非零退出（fail-closed 边界）。"""
    error = dc.DaemonUnavailableError("daemon offline")
    capture_cli(monkeypatch, error=error)
    with pytest.raises(SystemExit) as caught:
        cli._handle_task(BASE + ["--parent-id", "T-parent"], None)
    assert caught.value.code == 2


def test_legacy_root_create_daemon_error_behavior_unchanged(monkeypatch):
    """legacy root create 保持 pre-Gate 行为：业务错误原样透传，不改为 SystemExit。"""
    error = dc.DaemonRemoteError("E_RPC_FAILED", "boom")
    capture_cli(monkeypatch, error=error)
    with pytest.raises(dc.DaemonRemoteError):
        cli._handle_task(BASE, None)
