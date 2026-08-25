"""CLI-078（T-1787322799364-cb120c64）：cw local-changes → task.get_symbol_changes thin client 契约。

验证 _print_task_link_section 的 symbol_changes 段：
1. daemon 路径：route_task_read 调 task.get_symbol_changes，task_id/limit 透传，
   {changes:[{qualified_name,change_type,source_commit_hash}]} 解包输出。
2. 空结果：无 commits/changes 时静默跳过（不输出 Related 段）。
3. local 模式 legacy fallback：fallback_func 调 db.get_task_symbol_changes。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import callwarden.cli.main as main_mod  # noqa: E402


def test_cli078_symbol_changes_routes_to_daemon(monkeypatch, capsys):
    """daemon 路径：task.get_symbol_changes，task_id/limit 透传，changes 输出。"""
    captured = {}
    calls = []

    def _fake_route_read(method, params, fallback):
        captured["method"] = method
        captured["params"] = params
        calls.append(method)
        if method == "task.get_commits":
            return {"commits": []}
        return {"changes": [
            {"qualified_name": "pkg.foo", "change_type": "modified",
             "source_commit_hash": "a1b2c3d4"},
        ]}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_route_read)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    main_mod._print_task_link_section(proxy, "T-1")
    assert "task.get_symbol_changes" in calls
    assert captured["params"].get("task_id") == "T-1"
    assert captured["params"].get("limit") == 20
    out = capsys.readouterr().out
    assert "pkg.foo" in out and "modified" in out


def test_cli078_symbol_changes_empty_skips(monkeypatch, capsys):
    """空结果：无 commits/changes 时不输出 Related 段。"""
    def _fake_route_read(method, params, fallback):
        if method == "task.get_commits":
            return {"commits": []}
        return {"changes": []}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_route_read)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    main_mod._print_task_link_section(proxy, "T-1")
    out = capsys.readouterr().out
    assert "Related" not in out


def test_cli078_symbol_changes_local_fallback(monkeypatch, capsys):
    """local 模式 legacy fallback：fallback_func 调 db.get_task_symbol_changes。"""
    used = {}

    def _fake_route_read(method, params, fallback):
        if method == "task.get_commits":
            return fallback() if False else {"commits": []}
        used["changes_fallback"] = True
        return fallback()

    monkeypatch.setattr(main_mod, "route_task_read", _fake_route_read)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    monkeypatch.setattr(proxy, "get_task_symbol_changes",
                        lambda task_id, limit=20: [
                            {"qualified_name": "pkg.bar", "change_type": "added"}])
    main_mod._print_task_link_section(proxy, "T-1")
    assert used.get("changes_fallback") is True
    out = capsys.readouterr().out
    assert "pkg.bar" in out and "added" in out
