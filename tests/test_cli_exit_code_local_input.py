"""§24.5 退出码口径回归：本地输入校验 = rc=2；daemon 拒绝/运行时失败 = rc=1。

口径（见 `docs/design/daemon-rust-migration-ledger.md` §9.48）：
- rc=0 成功；
- rc=2 参数/文件/JSON/identity/lease 凭证等**本地**可判定的输入校验失败；
- rc=1 daemon 业务拒绝、传输失败、能力未接线等运行时失败。

本文件直接调用 `_handle_task` / `_handle_lease` / `_handle_collab`（db=None），
在触达 daemon 之前拦截，或 monkeypatch 路由函数模拟 daemon 拒绝。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.cli import main as cli_main  # noqa: E402 —— sys.path 注入须先于 import
from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402

_IDENTITY = (
    "--agent-id", "agent-x",
    "--session-id", "session-x",
    "--model-id", "model-x",
)

_LEASE = ("--lease-token", "tok-x", "--fencing-counter", "7")


def _exit_code(fn, argv):
    with pytest.raises(SystemExit) as excinfo:
        fn(argv, None)
    return excinfo.value.code


def _out(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + "\n" + captured.err


# ----------------------------------------------------------------------
# 本地输入校验 → rc=2
# ----------------------------------------------------------------------

def test_task_apply_partial_identity_exit2(capsys):
    code = _exit_code(cli_main._handle_task, ["apply", "T-1", "--agent-id", "a"])
    assert code == 2
    assert "E_IDENTITY_INCOMPLETE" in _out(capsys)


def test_task_apply_lease_token_only_exit2(capsys):
    code = _exit_code(cli_main._handle_task,
                      ["apply", "T-1", "--lease-token", "tok-only"])
    assert code == 2
    assert "E_LEASE_CRED_INCOMPLETE" in _out(capsys)


def _handoff_argv(*extra):
    return [
        "handoff", "T-1",
        "--from-role", "executor",
        "--outcome", "executor_ready_for_review",
        "--next-role", "reviewer",
        "--next-action", "独立复核",
        "--reason", "实现完成",
        "--independence-requirement", "required",
        "--request-id", "req-1",
        "--step-id", "S-1",
        "--report-request-id", "req-report-1",
        "--evidence-path", "docs/evidence/x.json",
        "--evidence-hash", "sha256:x",
        *extra,
    ]


def test_task_handoff_missing_identity_exit2(capsys):
    code = _exit_code(cli_main._handle_task, _handoff_argv())
    assert code == 2
    assert "E_IDENTITY_REQUIRED" in _out(capsys)


def test_task_handoff_invalid_route_exit2(capsys):
    argv = _handoff_argv(
        "--independence-requirement", "not_required",
        "--role", "executor", *_IDENTITY,
    )
    code = _exit_code(cli_main._handle_task, argv)
    assert code == 2
    assert "E_HANDOFF_ROUTE_INVALID" in _out(capsys)


def test_task_split_missing_plan_exit2(tmp_path):
    missing = tmp_path / "missing-plan.md"
    code = _exit_code(cli_main._handle_task,
                      ["split", "T-1", "--plan", str(missing)])
    assert code == 2


def test_task_create_governed_missing_workspace_pair_exit2(capsys):
    code = _exit_code(cli_main._handle_task,
                      ["create", "--title", "x", "--parent-id", "T-parent"])
    assert code == 2


def _contract_bootstrap_argv(envelope_path):
    return [
        "contract-bootstrap", "T-1",
        "--envelope-path", str(envelope_path),
        "--workspace-id", "1",
        "--workspace-instance-id", "inst-1",
        "--request-id", "req-1",
        "--evidence-path", "docs/evidence/x.json",
        "--evidence-hash", "sha256:x",
        "--role", "adjudicator", *_IDENTITY, *_LEASE,
    ]


def test_task_contract_bootstrap_missing_envelope_exit2(tmp_path):
    code = _exit_code(cli_main._handle_task,
                      _contract_bootstrap_argv(tmp_path / "missing.json"))
    assert code == 2


def test_task_contract_revise_empty_instance_exit2(capsys):
    argv = [
        "contract-revise", "T-1",
        "--envelope-path", "envelope.json",
        "--expected-previous-hash", "hash-prev",
        "--workspace-id", "1",
        "--workspace-instance-id", "inst-1",
        "--request-id", "req-1",
        "--evidence-path", "docs/evidence/x.json",
        "--evidence-hash", "sha256:x",
        "--role", "adjudicator", "--agent-instance-id", "",
        *_IDENTITY, *_LEASE,
    ]
    code = _exit_code(cli_main._handle_task, argv)
    assert code == 2
    assert "E_TASK_CONTRACT_REVISE_IDENTITY_INSTANCE_REQUIRED" in _out(capsys)


def test_task_claim_recover_non_adjudicator_exit2(capsys):
    argv = ["claim-recover", "T-1", "--reason", "orphan",
            "--role", "reviewer", *_IDENTITY]
    code = _exit_code(cli_main._handle_task, argv)
    assert code == 2


def test_lease_acquire_missing_identity_exit2(capsys):
    code = _exit_code(cli_main._handle_lease,
                      ["acquire", "T-1", "--role", "implementer"])
    assert code == 2
    assert "E_ASSIGNMENT_INCOMPLETE" in _out(capsys)


def test_collab_verdict_missing_identity_exit2(capsys):
    argv = [
        "verdict",
        "--task-id", "T-1",
        "--step-id", "S-1",
        "--contract-id", "c-1",
        "--contract-hash", "h-1",
        "--contract-revision", "1",
        "--role-contract-id", "rc-1",
        "--role-contract-hash", "rh-1",
        "--role-contract-revision", "1",
        "--snapshot-id", "snap-1",
        "--request-id", "req-1",
        "--phase", "blind_first_pass",
        "--overall", "pass",
        "--attestation", "独立复核通过",
        "--agent-instance-id", "inst-1",
        "--role", "reviewer",
    ]
    code = _exit_code(cli_main._handle_collab, argv)
    assert code == 2
    assert "E_IDENTITY_INCOMPLETE" in _out(capsys)


# ----------------------------------------------------------------------
# daemon 拒绝 / 运行时失败 → rc=1
# ----------------------------------------------------------------------

def test_task_report_governance_gate_exit1(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_main, "_governance_gate",
        lambda *a, **k: {"code": "E_GOVERNANCE_BLOCKED",
                         "message_key": "daemon_errors.error.governance_blocked",
                         "detail": "任务存在 blocked finding"},
    )
    code = _exit_code(cli_main._handle_task,
                      ["report", "T-1", "S-1", "--result", "done"])
    assert code == 1
    assert "E_GOVERNANCE_BLOCKED" in _out(capsys)


def test_task_assignment_heartbeat_daemon_error_exit1(monkeypatch, capsys):
    def _raise(*a, **k):
        raise DaemonRemoteError("E_ASSIGNMENT_NOT_FOUND", "assignment 不存在")

    monkeypatch.setattr(cli_main, "route_task_write", _raise)
    argv = ["assignment-heartbeat", "T-1", "A-1", "--request-id", "req-1",
            "--role", "implementer", *_IDENTITY]
    code = _exit_code(cli_main._handle_task, argv)
    assert code == 1
    assert "E_ASSIGNMENT_NOT_FOUND" in _out(capsys)


def test_task_step_resolve_daemon_error_exit1(monkeypatch, capsys):
    def _raise(*a, **k):
        raise DaemonRemoteError("E_STEP_NOT_FOUND", "failed step 不存在")

    monkeypatch.setattr(cli_main, "route_task_write", _raise)
    argv = ["step-resolve", "T-1", "S-fail", "S-fix", "req-1",
            "--evidence-path", "docs/evidence/fix.json",
            "--evidence-hash", "sha256:x",
            "--role", "implementer", *_IDENTITY, *_LEASE]
    code = _exit_code(cli_main._handle_task, argv)
    assert code == 1
    assert "E_STEP_NOT_FOUND" in _out(capsys)


def test_lease_acquire_daemon_reject_exit1(monkeypatch, capsys):
    monkeypatch.setattr(cli_main, "_governance_gate", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_main, "_route_lease_write",
        lambda *a, **k: (False, {"code": "E_LEASE_TOKEN_MISMATCH",
                                 "message_key": "daemon_errors.error.lease_token_mismatch",
                                 "detail": "token 不匹配"}),
    )
    argv = ["acquire", "T-1", "--role", "implementer", *_IDENTITY]
    code = _exit_code(cli_main._handle_lease, argv)
    assert code == 1
    assert "E_LEASE_TOKEN_MISMATCH" in _out(capsys)
