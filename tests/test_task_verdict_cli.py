"""Task-bound reviewer verdict CLI contract tests.

These tests exercise the supported ``cw collab verdict`` command surface.  The
CLI must submit the complete task-bound provenance envelope to the daemon and
must never replace a daemon failure with a local verdict store.

W17：该路径此前硬编码本地 ``DaemonClient``（恒为 Unix 传输面），与 ``cw lease``
/ ``cw task`` 的 HTTP authority 不一致，导致同一 task 上 verdict 提交
fail-closed（``E_TASK_WORKSPACE_UNBOUND`` / ``E_LEASE_NOT_FOUND``）。现收敛到
统一权威路由 ``route_rpc(..., 'GOVERNANCE_WRITE')`` → ``HttpDaemonRpcClient``。
"""

from __future__ import annotations

import pytest

from callwarden.cli import main as cli_main
from callwarden.server import daemon_client
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


@pytest.fixture(autouse=True)
def _reset_http_singleton():
    """HttpDaemonRpcClient 是单例：跨用例隔离 workspace 绑定状态。"""
    daemon_client.HttpDaemonRpcClient.reset_instance()
    yield
    daemon_client.HttpDaemonRpcClient.reset_instance()


def _install_http_authority(monkeypatch, result=None, error=None):
    """注入 HTTP authority 假体，返回出向 RPC 调用记录。

    只替换最底层 ``HttpDaemonRpcClient.call``（真实类无 call_with_autostart），
    ``route_rpc`` 走的正是该权威路径。
    """
    calls = []

    def fake_call(self, method, params=None, request_id=None):
        calls.append((method, params))
        if error is not None:
            raise error
        return result if result is not None else {"verdict_id": "V-1"}

    monkeypatch.setattr(daemon_client.HttpDaemonRpcClient, "call", fake_call)
    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "http")
    return calls


def test_task_bound_verdict_submits_complete_provenance(monkeypatch):
    calls = _install_http_authority(monkeypatch)

    assert cli_main._handle_collab(BASE_ARGS, None) is True

    assert [method for method, _ in calls] == ["verdict.submit"]
    method, params = calls[0]
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
    # 幂等：沿用显式 --request-id，route_rpc 不得覆盖（GOVERNANCE_WRITE）
    assert params["request_id"] == "review-T-1-S-1-r1"
    # task-scoped authority：不得注入 workspace 面字段（由 daemon 按
    # task_workspace_bindings 解析），否则会打回未知字段/抢占 authority
    assert "workspace_instance_id" not in params
    assert "workspace_root" not in params


@pytest.mark.parametrize("flag,value", [("--clause-results", "not-json"), ("--findings", "{")])
def test_task_bound_verdict_rejects_malformed_structured_inputs(monkeypatch, flag, value):
    calls = _install_http_authority(monkeypatch)

    assert cli_main._handle_collab(BASE_ARGS + [flag, value, "--json"], None) is True

    assert calls == []


def test_task_bound_verdict_requires_reviewer_instance_identity(monkeypatch):
    calls = _install_http_authority(monkeypatch)
    args = list(BASE_ARGS)
    index = args.index("--agent-instance-id")
    del args[index:index + 2]

    with pytest.raises(SystemExit):
        cli_main._handle_collab(args + ["--json"], None)

    assert calls == []


def test_task_bound_verdict_daemon_unavailable_fails_closed(monkeypatch, capsys):
    calls = _install_http_authority(
        monkeypatch, error=DaemonUnavailableError("daemon down")
    )

    with pytest.raises(SystemExit) as excinfo:
        cli_main._handle_collab(BASE_ARGS + ["--json"], None)

    # fail-closed：非零 RC、结构化拒绝、无本地回退
    assert excinfo.value.code not in (0, None)
    output = capsys.readouterr().out
    assert '"code": "E_GOVERNANCE_WRITE_DEGRADED"' in output
    assert "恢复" in output or "recovery" in output.lower()
    assert [method for method, _ in calls] == ["verdict.submit"]
