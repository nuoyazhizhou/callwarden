"""CLI-076（T-1787322799244-c3f2f9c0）：cw local-capture-auto → task.capture_diff thin client 契约。

验证 capture-diff --auto 分支：
1. daemon 路径：route_task_write 调 task.capture_diff，{auto: True} 透传，结果输出
   success/changed_files/linked_symbols/quality_decision。
2. fail-soft：daemon 返回 success=False reason=no_in_progress_task → 黄色提示不崩溃。
3. 异常兜底：route_task_write 抛异常 → 封装 fail-soft 结果（cli_exception）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import callwarden.cli.main as main_mod  # noqa: E402


def test_cli076_capture_auto_routes_to_daemon(monkeypatch, capsys):
    """daemon 路径：task.capture_diff {auto:True}，结果输出。"""
    captured = {}

    def _fake_route_write(method, params, fallback):
        captured["method"] = method
        captured["params"] = params
        return {
            "auto": True, "success": True, "task_id": "T-1", "base": "HEAD~1",
            "dry_run": False,
            "changed_files": [{"path": "a.py", "status": "M"}],
            "linked_symbols": ["pkg.foo"],
            "quality_findings": [], "quality_decision": "pass", "next_action": "continue",
        }

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_task(["capture-diff", "--auto"], proxy)
    assert rc is True
    assert captured.get("method") == "task.capture_diff"
    assert captured["params"].get("auto") is True
    out = capsys.readouterr().out
    assert "[M] a.py" in out
    assert "Linked symbol change records: 1" in out


def test_cli076_capture_auto_no_task_fail_soft(monkeypatch, capsys):
    """fail-soft：no_in_progress_task → 黄色提示不崩溃。"""
    def _fake_route_write(method, params, fallback):
        return {
            "auto": True, "success": False, "reason": "no_in_progress_task",
            "task_id": "", "base": "", "dry_run": False,
            "changed_files": [], "linked_symbols": [], "quality_findings": [],
            "quality_decision": "", "next_action": "noop",
        }

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_task(["capture-diff", "--auto"], proxy)
    assert rc is True
    out = capsys.readouterr().out
    assert "no in_progress task found" in out.lower()


def test_cli076_capture_auto_exception_fail_soft(monkeypatch, capsys):
    """异常兜底：route_task_write 抛异常 → cli_exception fail-soft 结果。"""
    def _boom(method, params, fallback):
        raise RuntimeError("daemon down")

    monkeypatch.setattr(main_mod, "route_task_write", _boom)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_task(["capture-diff", "--auto"], proxy)
    assert rc is True
    out = capsys.readouterr().out
    assert "daemon down" in out
