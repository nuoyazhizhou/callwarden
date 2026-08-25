"""CLI-083（T-1787322799648-dc001930）：cw findings → task.quality_findings thin client 契约。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from callwarden.cli import main as main_mod  # noqa: E402
from callwarden.i18n import set_language  # noqa: E402

set_language("zh_CN")


def _handle_findings(task_id="T-1", status="open", severity=""):
    argv = ["findings", task_id, "--status", status]
    if severity:
        argv.extend(["--severity", severity])
    return main_mod._handle_task(argv, main_mod.RpcDBProxy(workspace_root="C:/git_work/x"))


def _finding(**overrides):
    result = {
        "id": 7,
        "task_id": "T-1",
        "step_id": 3,
        "finding_type": "guardrail",
        "severity": "warn",
        "message": "needs attention",
        "details": "{}",
        "status": "open",
        "created_at": 1.0,
        "resolved_at": 0.0,
        "source": "daemon",
    }
    result.update(overrides)
    return result


def test_cli083_routes_findings_to_daemon_with_filters(monkeypatch, capsys):
    """success：仅调用 task.quality_findings，并透传 task/status/severity。"""
    captured = {}

    def fake_route_read(method, params, fallback):
        captured["method"] = method
        captured["params"] = params
        return [_finding()]

    monkeypatch.setattr(main_mod, "route_task_read", fake_route_read)
    assert _handle_findings("T-7", status="resolved", severity="error") is True
    assert captured == {
        "method": "task.quality_findings",
        "params": {"task_id": "T-7", "status": "resolved", "severity": "error"},
    }
    out = capsys.readouterr().out
    assert "T-7" in out
    assert "needs attention" in out


def test_cli083_empty_or_invalid_result_is_graceful(monkeypatch, capsys):
    """invalid input：daemon 返回空数组时，CLI 输出无发现且不触发本地查询。"""
    monkeypatch.setattr(main_mod, "route_task_read", lambda method, params, fallback: [])
    assert _handle_findings() is True
    assert "(无质量发现)" in capsys.readouterr().out


def test_cli083_wrong_authority_error_propagates_without_local_fallback(monkeypatch):
    """wrong/unknown authority：远端稳定错误原样传播，绝不改走本地查询。"""
    def fake_route_read(method, params, fallback):
        raise main_mod.DaemonRemoteError("method_not_found", "no such method")

    monkeypatch.setattr(main_mod, "route_task_read", fake_route_read)
    with pytest.raises(main_mod.DaemonRemoteError, match="no such method"):
        _handle_findings()


def test_cli083_daemon_unavailable_fails_closed(monkeypatch):
    """daemon unavailable：fail-closed 异常传播，不允许查询本地 SQLite。"""
    def fake_route_read(method, params, fallback):
        raise main_mod.DaemonUnavailableError("daemon down")

    monkeypatch.setattr(main_mod, "route_task_read", fake_route_read)
    with pytest.raises(main_mod.DaemonUnavailableError, match="daemon down"):
        _handle_findings()


def test_cli083_restart_consistent(monkeypatch, capsys):
    """restart 后行为一致：每次调用均重新请求 daemon，不缓存本地结果。"""
    calls = []

    def fake_route_read(method, params, fallback):
        calls.append((method, params.copy()))
        return [_finding()]

    monkeypatch.setattr(main_mod, "route_task_read", fake_route_read)
    _handle_findings()
    first_output = capsys.readouterr().out
    _handle_findings()
    second_output = capsys.readouterr().out
    assert first_output == second_output
    assert calls == [
        ("task.quality_findings", {"task_id": "T-1", "status": "open", "severity": ""}),
        ("task.quality_findings", {"task_id": "T-1", "status": "open", "severity": ""}),
    ]


def test_cli083_local_fallback_is_forbidden(monkeypatch):
    """local fallback：回调必须拒绝，禁止 db.get_task_quality_findings。"""
    captured = {}

    def fake_route_read(method, params, fallback):
        captured["fallback"] = fallback
        return []

    monkeypatch.setattr(main_mod, "route_task_read", fake_route_read)
    _handle_findings()
    with pytest.raises(main_mod.DaemonUnavailableError):
        captured["fallback"]()
