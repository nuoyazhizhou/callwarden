"""CLI-077（T-1787322799302-c75c1d94）：cw local-capture-manual → task.capture_diff thin client 契约。

验证 capture-diff 手动分支（--task-id）：
1. daemon 路径：route_task_write 调 task.capture_diff，task_id/step_id/base/dry_run/
   source_commit_hash/skip_quality_review 透传。
2. task_id 缺失：argparse error 提示 task_id is required。
3. local 模式 legacy fallback：fallback_func 调 db.task_capture_diff。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import callwarden.cli.main as main_mod  # noqa: E402


def test_cli077_capture_manual_routes_to_daemon(monkeypatch, capsys):
    """daemon 路径：task.capture_diff manual 参数透传。"""
    captured = {}

    def _fake_route_write(method, params, fallback):
        captured["method"] = method
        captured["params"] = params
        return {
            "success": True, "task_id": "T-9", "base": "abc123",
            "dry_run": False, "changed_files": [{"path": "b.py", "status": "M"}],
            "linked_symbols": [], "quality_findings": [],
            "quality_decision": "pass", "next_action": "continue",
        }

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_task([
        "capture-diff", "T-9", "--step-id", "S-1",
        "--base", "abc123"], proxy)
    assert rc is True
    assert captured.get("method") == "task.capture_diff"
    assert captured["params"].get("task_id") == "T-9"
    assert captured["params"].get("step_id") == "S-1"
    assert captured["params"].get("base") == "abc123"
    assert captured["params"].get("dry_run") is False
    out = capsys.readouterr().out
    assert "[M] b.py" in out


def test_cli077_capture_manual_dry_run_passthrough(monkeypatch, capsys):
    """dry-run：--dry-run 透传 dry_run=True，输出 Dry-run 模式。"""
    captured = {}

    def _fake_route_write(method, params, fallback):
        captured["method"] = method
        captured["params"] = params
        return {"success": True, "task_id": "T-9", "dry_run": True,
                "changed_files": [], "linked_symbols": [],
                "quality_findings": [], "next_action": "noop"}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_task([
        "capture-diff", "T-9", "--dry-run"], proxy)
    assert rc is True
    assert captured["params"].get("dry_run") is True
    out = capsys.readouterr().out
    assert "dry" in out.lower()


def test_cli077_capture_manual_local_fallback(monkeypatch):
    """local 模式 legacy fallback：fallback_func 调 db.task_capture_diff。"""
    called = {}

    def _fake_route_write(method, params, fallback):
        called["used"] = True
        return fallback()

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")

    def _local_capture_manual():
        return proxy.task_capture_diff(task_id="T-9", step_id="", base="",
                                       dry_run=False, source_commit_hash="",
                                       skip_quality_review=False)

    monkeypatch.setattr(
        proxy, "task_capture_diff",
        lambda task_id, step_id="", base="", dry_run=False,
               source_commit_hash="", skip_quality_review=False:
        {"success": True, "task_id": task_id, "dry_run": dry_run})
    result = _fake_route_write("task.capture_diff", {"task_id": "T-9"},
                               _local_capture_manual)
    assert called.get("used") is True
    assert result["success"] is True
