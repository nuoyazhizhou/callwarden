"""CLI-080（T-1787322799482-d215a638）：cw local-commits → task.get_commits thin client 契约。

验证 _print_task_link_section 的 commits 段：
1. daemon 路径：route_task_read 调 task.get_commits，task_id 透传，
   {commits:[{source_commit_hash,commit_subject,commit_author,change_count}]} 解包输出。
2. invalid input：daemon 返回缺 commits key / commits=null 时不崩溃（Related 静默）。
3. wrong/unknown authority：DaemonRemoteError（method_not_found）fail-soft 跳过。
4. daemon unavailable：DaemonUnavailableError 必须上抛（M4 fail-closed，禁止静默跳过）。
5. restart 后行为一致：handler 无本地状态，重复调用输出一致；local fallback 为
   forbidden 回调（禁止 db.get_task_commits 本地读）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import callwarden.cli.main as main_mod  # noqa: E402


def test_cli080_commits_routes_to_daemon(monkeypatch, capsys):
    """daemon 路径：task.get_commits，task_id 透传，commits 解包输出。"""
    captured = {}
    calls = []

    def _fake_route_read(method, params, fallback):
        captured["method"] = method
        captured["params"] = params
        calls.append(method)
        if method == "task.get_commits":
            return {"commits": [
                {"source_commit_hash": "a1b2c3d4e5f6", "commit_subject": "fix: x",
                 "commit_author": "dev", "change_count": 2},
            ]}
        return {"changes": []}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_route_read)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    main_mod._print_task_link_section(proxy, "T-1")
    assert "task.get_commits" in calls
    assert captured["params"].get("task_id") == "T-1"
    out = capsys.readouterr().out
    assert "a1b2c3d4" in out
    assert "fix: x" in out
    assert "by dev" in out
    assert "2 changes" in out


def test_cli080_commits_invalid_input_graceful(monkeypatch, capsys):
    """invalid input：缺 commits key / commits=null 时不崩溃，Related 静默。"""
    for bad in ({}, {"commits": None}):
        calls = []

        def _fake_route_read(method, params, fallback):
            calls.append(method)
            if method == "task.get_commits":
                return bad
            return {"changes": []}

        monkeypatch.setattr(main_mod, "route_task_read", _fake_route_read)
        proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
        main_mod._print_task_link_section(proxy, "T-1")
        out = capsys.readouterr().out
        assert "Related" not in out


def test_cli080_commits_wrong_authority_fail_soft(monkeypatch, capsys):
    """wrong/unknown authority：DaemonRemoteError（method_not_found）fail-soft 跳过。"""
    def _fake_route_read(method, params, fallback):
        if method == "task.get_commits":
            raise main_mod.DaemonRemoteError("method_not_found", "no such method")
        return {"changes": []}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_route_read)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    main_mod._print_task_link_section(proxy, "T-1")
    out = capsys.readouterr().out
    assert "Related" not in out


def test_cli080_commits_daemon_unavailable_fails_closed(monkeypatch):
    """daemon unavailable：DaemonUnavailableError 必须上抛（M4 fail-closed）。"""
    def _fake_route_read(method, params, fallback):
        if method == "task.get_commits":
            raise main_mod.DaemonUnavailableError("daemon down")
        return {"changes": []}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_route_read)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    with pytest.raises(main_mod.DaemonUnavailableError):
        main_mod._print_task_link_section(proxy, "T-1")


def test_cli080_commits_restart_consistent(monkeypatch, capsys):
    """restart 后行为一致：handler 无本地状态，重复调用输出一致。"""
    read_count = []

    def _fake_route_read(method, params, fallback):
        read_count.append(method)
        if method == "task.get_commits":
            return {"commits": [
                {"source_commit_hash": "deadbeef1234", "commit_subject": "s",
                 "commit_author": "a", "change_count": 1}]}
        return {"changes": []}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_route_read)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    main_mod._print_task_link_section(proxy, "T-1")
    out1 = capsys.readouterr().out
    main_mod._print_task_link_section(proxy, "T-1")
    out2 = capsys.readouterr().out
    assert out1 == out2
    # 每次调用都重新走 route_task_read（无结果缓存），等价 daemon restart 后行为一致
    assert read_count.count("task.get_commits") == 2


def test_cli080_commits_local_fallback_forbidden(monkeypatch):
    """local fallback 为 forbidden 回调：禁止 db.get_task_commits 本地读。"""
    captured = {}

    def _fake_route_read(method, params, fallback):
        if method == "task.get_commits":
            captured["fb"] = fallback
            return {"commits": []}
        return {"changes": []}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_route_read)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    main_mod._print_task_link_section(proxy, "T-1")
    with pytest.raises(main_mod.DaemonUnavailableError):
        captured["fb"]()
