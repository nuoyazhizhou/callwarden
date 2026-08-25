"""CLI-081（T-1787322799532-d51d9d18）：cw completion-review → task.completion_review thin client 契约。

验证 _handle_task 的 completion-review 分支（_local_completion_review）：
1. daemon 路径：route_task_write 调 task.completion_review，task_id/step_id 透传，
   {task_id, decision, findings} 格式化输出；Rust handler 返回无 summary/counts，
   Python 输出层 fail-soft 兼容（counts 缺省空 dict、summary 缺省空串不打印）。
2. invalid input：daemon 返回 {} / 缺字段时不崩溃。
3. wrong/unknown authority：DaemonRemoteError（method_not_found）原样格式化输出。
4. daemon unavailable：DaemonUnavailableError fail-closed 提示（禁止本地回退）。
5. restart 后行为一致：handler 无本地状态，重复调用输出一致。
6. local fallback 为 forbidden 回调：禁止 db.run_task_completion_review 本地业务路径。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from callwarden.cli import main as main_mod  # noqa: E402
from callwarden.i18n import set_language  # noqa: E402

set_language("zh_CN")


def _handle_completion_review(task_id="T-1", step_id="", **extra_argv):
    argv = ["completion-review", task_id]
    if step_id:
        argv += ["--step-id", step_id]
    argv += [f"--{k}={v}" for k, v in extra_argv.items()]
    return main_mod._handle_task(argv, main_mod.RpcDBProxy(workspace_root="C:/git_work/x"))


def test_cli081_routes_to_daemon(monkeypatch, capsys):
    """daemon 路径：task.completion_review，task_id/step_id 透传，输出格式化。"""
    captured = {}
    calls = []

    def _fake_route_write(method, params, fallback):
        captured["method"] = method
        captured["params"] = params
        calls.append(method)
        return {
            "task_id": "T-1",
            "decision": "block",
            "findings": [
                {"id": 7, "finding_type": "semgrep",
                 "severity": "error", "message": "unused import", "status": "open"},
            ],
        }

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    assert _handle_completion_review(step_id="S-7") is True
    assert "task.completion_review" in calls
    assert captured["params"].get("task_id") == "T-1"
    assert captured["params"].get("step_id") == "S-7"
    out = capsys.readouterr().out
    assert "block" in out
    assert "T-1" in out
    assert "S-7" in out
    assert "unused import" in out


def test_cli081_no_step_id(monkeypatch, capsys):
    """无 --step-id 时 step_id 透传为空串、不打印步骤行。"""
    captured = {}

    def _fake_route_write(method, params, fallback):
        captured["params"] = params
        return {"task_id": "T-1", "decision": "pass", "findings": []}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    _handle_completion_review()
    out = capsys.readouterr().out
    assert captured["params"].get("step_id") == ""
    assert "步骤 ID" not in out
    assert "pass" in out


def test_cli081_invalid_input_graceful(monkeypatch, capsys):
    """invalid input：{} / 缺 decision / findings=null 时不崩溃。"""
    for bad in ({}, {"task_id": "T-1"}, {"task_id": "T-1", "decision": "pass", "findings": None}):
        def _fake_route_write(method, params, fallback):
            return bad

        monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
        assert _handle_completion_review() is True
        capsys.readouterr().out  # 不抛异常即通过


def test_cli081_error_dict_printed(monkeypatch, capsys):
    """daemon 返回 {"error": ...} 结构化错误时格式化输出。"""
    def _fake_route_write(method, params, fallback):
        return {"error": "task not found"}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    assert _handle_completion_review() is True
    out = capsys.readouterr().out
    assert "task not found" in out


def test_cli081_wrong_authority_remote_error(monkeypatch, capsys):
    """wrong/unknown authority：DaemonRemoteError（method_not_found）原样格式化。"""
    def _fake_route_write(method, params, fallback):
        raise main_mod.DaemonRemoteError("method_not_found", "no such method")

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    assert _handle_completion_review() is True
    out = capsys.readouterr().out
    assert "method_not_found" in out
    assert "no such method" in out


def test_cli081_daemon_unavailable_fails_closed(monkeypatch, capsys):
    """daemon unavailable：DaemonUnavailableError fail-closed 提示，无本地回退。"""
    def _fake_route_write(method, params, fallback):
        raise main_mod.DaemonUnavailableError("daemon down")

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    assert _handle_completion_review() is True
    out = capsys.readouterr().out
    assert "daemon down" in out


def test_cli081_http_wrapper_unwrapped(monkeypatch, capsys):
    """HTTP client call_with_autostart 的 {result, degraded:false} 包装被解包。"""
    def _fake_route_write(method, params, fallback):
        return {"result": {"task_id": "T-1", "decision": "pass", "findings": []},
                "degraded": False}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    _handle_completion_review()
    out = capsys.readouterr().out
    assert "pass" in out
    assert "unknown" not in out


def test_cli081_http_degraded_fails_closed(monkeypatch, capsys):
    """HTTP degraded 标记（daemon 不可达）fail-closed 提示，禁止本地回退。"""
    def _fake_route_write(method, params, fallback):
        return {"result": None, "degraded": True,
                "mode": "direct_read", "op_class": "read_only"}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    assert _handle_completion_review() is True
    out = capsys.readouterr().out
    assert "degraded" in out


def test_cli081_restart_consistent(monkeypatch, capsys):
    """restart 后行为一致：handler 无本地状态，重复调用输出一致。"""
    write_count = []

    def _fake_route_write(method, params, fallback):
        write_count.append(method)
        return {"task_id": "T-1", "decision": "pass", "findings": []}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    _handle_completion_review()
    out1 = capsys.readouterr().out
    _handle_completion_review()
    out2 = capsys.readouterr().out
    assert out1 == out2
    # 每次调用都重新走 route_task_write（无结果缓存），等价 daemon restart 后行为一致
    assert write_count.count("task.completion_review") == 2


def test_cli081_local_fallback_forbidden(monkeypatch):
    """local fallback 为 forbidden 回调：禁止 db.run_task_completion_review 本地读。"""
    captured = {}

    def _fake_route_write(method, params, fallback):
        captured["fb"] = fallback
        return {"task_id": "T-1", "decision": "pass", "findings": []}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    _handle_completion_review()
    with pytest.raises(main_mod.DaemonUnavailableError):
        captured["fb"]()
