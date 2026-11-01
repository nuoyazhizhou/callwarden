"""socket 传输 error dict / no response 路径退出码回归：必须 RC=1。

历史 GOV-FIX-08 曾把各命令分支的 `{"error": ...}` / `None` 响应定为 RC=2；
阶段 3 残差卡 T-1793505963640-e9540b78 统一口径后，daemon 拒绝与传输失败一律
RC=1，RC=2 仅保留 argparse 用法错误。本测试用 mock route_task_write/read 驱动
_handle_task，逐命令断言 SystemExit(1)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.cli import main as cli_main  # noqa: E402 —— sys.path/环境注入须先于 import(脚本直跑未走 site-packages,顺序为硬约束)


def _run_handle_task(argv, monkeypatch, result):
    """mock route_task_write/read 后执行 _handle_task，返回 SystemExit 退出码。"""
    monkeypatch.setattr(cli_main, "route_task_write", lambda *a, **k: result)
    monkeypatch.setattr(cli_main, "route_task_read", lambda *a, **k: result)
    with pytest.raises(SystemExit) as excinfo:
        cli_main._handle_task(argv, None)
    return excinfo.value.code


def test_apply_error_dict_exits_1(monkeypatch):
    code = _run_handle_task(["apply", "T-1"], monkeypatch, {"error": "E_GATE"})
    assert code == 1


def test_close_error_dict_exits_1(monkeypatch):
    code = _run_handle_task(["close", "T-1"], monkeypatch, {"error": "E_GATE"})
    assert code == 1


def test_claim_recover_error_dict_exits_1(monkeypatch):
    code = _run_handle_task(
        ["claim-recover", "T-1", "--reason", "stale",
         "--request-id", "req-claim-1",
         "--lease-token", "tok", "--fencing-counter", "1",
         "--agent-id", "a", "--session-id", "s", "--model-id", "m",
         "--role", "adjudicator"],
        monkeypatch, {"error": "E_CLAIM"})
    assert code == 1


def test_completion_review_error_dict_exits_1(monkeypatch):
    code = _run_handle_task(
        ["completion-review", "T-1"], monkeypatch, {"error": "E_REVIEW"})
    assert code == 1


def test_supersede_error_dict_exits_1(monkeypatch):
    code = _run_handle_task(
        ["supersede", "T-OLD", "T-NEW", "--reason", "r",
         "--evidence-path", "docs/evidence/probe.json",
         "--evidence-hash", "deadbeef"],
        monkeypatch, {"error": "E_TASK_WORKSPACE_UNBOUND"})
    assert code == 1


def test_supersede_no_response_exits_1(monkeypatch):
    code = _run_handle_task(
        ["supersede", "T-OLD", "T-NEW", "--reason", "r",
         "--evidence-path", "docs/evidence/probe.json",
         "--evidence-hash", "deadbeef"],
        monkeypatch, None)
    assert code == 1


def test_governance_projection_error_dict_exits_1(monkeypatch):
    code = _run_handle_task(
        ["governance-projection", "T-1"], monkeypatch, {"error": "E_PROJ"})
    assert code == 1


def test_superseded_error_dict_exits_1(monkeypatch):
    code = _run_handle_task(
        ["superseded", "T-1"], monkeypatch, {"error": "E_NOT_FOUND"})
    assert code == 1


def test_contract_bootstrap_error_dict_exits_1(monkeypatch, tmp_path):
    envelope = tmp_path / "envelope.json"
    envelope.write_text("{}", encoding="utf-8")
    code = _run_handle_task(
        ["contract-bootstrap", "T-1",
         "--envelope-path", str(envelope),
         "--workspace-id", "1", "--workspace-instance-id", "1",
         "--request-id", "req-boot-1",
         "--evidence-path", "docs/evidence/probe.json",
         "--evidence-hash", "deadbeef",
         "--lease-token", "tok", "--fencing-counter", "1",
         "--agent-id", "a", "--session-id", "s", "--model-id", "m",
         "--role", "adjudicator"],
        monkeypatch, {"error": "E_BOOT"})
    assert code == 1


def test_supersede_success_path_rc0(monkeypatch, capsys):
    """成功路径语义不破坏：supersede 成功输出不抛 SystemExit。"""
    monkeypatch.setattr(cli_main, "route_task_write", lambda *a, **k: {
        "superseded_task_id": "T-OLD", "superseding_task_id": "T-NEW",
        "reason": "r", "supersedence_id": "S-1", "workspace_id": 1,
    })
    ret = cli_main._handle_task(
        ["supersede", "T-OLD", "T-NEW", "--reason", "r",
         "--evidence-path", "docs/evidence/probe.json",
         "--evidence-hash", "deadbeef"], None)
    assert ret is True
    out = capsys.readouterr().out
    assert "T-OLD" in out and "T-NEW" in out
