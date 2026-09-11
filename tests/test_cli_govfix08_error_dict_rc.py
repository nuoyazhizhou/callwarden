"""GOV-FIX-08 回归：task 写/读分支 socket 传输 error dict / no response 路径必须 RC=2。

GOV-FIX-07（dispatch 层 DaemonRemoteError RC=2）的后续：各命令分支在 socket 传输下
拿到 {"error": ...} dict 或 None（no response）时此前 return True → RC=0 假成功。
本测试用 mock route_task_write/route_task_read 驱动 _handle_task，逐命令断言
SystemExit(2)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.cli import main as cli_main


def _run_handle_task(argv, monkeypatch, result):
    """mock route_task_write/read 后执行 _handle_task，返回 SystemExit 或 None。"""
    monkeypatch.setattr(cli_main, "route_task_write", lambda *a, **k: result)
    monkeypatch.setattr(cli_main, "route_task_read", lambda *a, **k: result)
    with pytest.raises(SystemExit) as excinfo:
        cli_main._handle_task(argv, None)
    return excinfo.value.code


def test_apply_error_dict_exits_2(monkeypatch):
    code = _run_handle_task(["apply", "T-1"], monkeypatch, {"error": "E_GATE"})
    assert code == 2


def test_close_error_dict_exits_2(monkeypatch):
    code = _run_handle_task(["close", "T-1"], monkeypatch, {"error": "E_GATE"})
    assert code == 2


def test_claim_recover_error_dict_exits_2(monkeypatch):
    code = _run_handle_task(
        ["claim-recover", "T-1", "--reason", "stale",
         "--evidence-path", "docs/evidence/probe.json",
         "--evidence-hash", "deadbeef"],
        monkeypatch, {"error": "E_CLAIM"})
    assert code == 2


def test_completion_review_error_dict_exits_2(monkeypatch):
    code = _run_handle_task(
        ["completion-review", "T-1"], monkeypatch, {"error": "E_REVIEW"})
    assert code == 2


def test_supersede_error_dict_exits_2(monkeypatch):
    code = _run_handle_task(
        ["supersede", "T-OLD", "T-NEW", "--reason", "r",
         "--evidence-path", "docs/evidence/probe.json",
         "--evidence-hash", "deadbeef"],
        monkeypatch, {"error": "E_TASK_WORKSPACE_UNBOUND"})
    assert code == 2


def test_supersede_no_response_exits_2(monkeypatch):
    code = _run_handle_task(
        ["supersede", "T-OLD", "T-NEW", "--reason", "r",
         "--evidence-path", "docs/evidence/probe.json",
         "--evidence-hash", "deadbeef"],
        monkeypatch, None)
    assert code == 2


def test_governance_projection_error_dict_exits_2(monkeypatch):
    code = _run_handle_task(
        ["governance-projection", "T-1"], monkeypatch, {"error": "E_PROJ"})
    assert code == 2


def test_superseded_error_dict_exits_2(monkeypatch):
    code = _run_handle_task(
        ["superseded", "T-1"], monkeypatch, {"error": "E_NOT_FOUND"})
    assert code == 2


def test_contract_bootstrap_error_dict_exits_2(monkeypatch):
    code = _run_handle_task(
        ["contract-bootstrap", "T-1"], monkeypatch, {"error": "E_BOOT"})
    assert code == 2


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
