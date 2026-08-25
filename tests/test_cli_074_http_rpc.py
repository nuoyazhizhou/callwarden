"""CLI-074（T-1787322799131-bd2db3c8）：cw local → task.has_blocking_findings thin client 契约。

验证 _route_has_blocking_findings：
1. daemon 路径：route_task_read 以 task.has_blocking_findings 调用（READ_ONLY），
   task_id 透传，返回 {has_blocking} 解包为 bool。
2. local 模式 legacy fallback：route_task_read 执行 fallback_func 时调
   db.task_has_blocking_findings（local 模式显式 legacy 语义，非 hidden fallback）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import callwarden.cli.main as main_mod  # noqa: E402


def test_cli074_has_blocking_findings_routes_to_daemon(monkeypatch):
    """daemon 路径：route_task_read 调 task.has_blocking_findings，{has_blocking} 解包。"""
    captured = {}

    def _fake_route(method, params, fallback):
        captured["method"] = method
        captured["params"] = params
        return {"has_blocking": True}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_route)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    result = main_mod._route_has_blocking_findings(proxy, "T-123")
    assert result is True
    assert captured.get("method") == "task.has_blocking_findings"
    assert captured["params"].get("task_id") == "T-123"


def test_cli074_has_blocking_findings_no_blocking(monkeypatch):
    """daemon 返回 {has_blocking: False} → False。"""
    def _fake_route(method, params, fallback):
        return {"has_blocking": False}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_route)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    assert main_mod._route_has_blocking_findings(proxy, "T-123") is False


def test_cli074_local_fallback_uses_db_method(monkeypatch):
    """local 模式 legacy fallback：fallback_func 调 db.task_has_blocking_findings。"""
    def _fake_route(method, params, fallback):
        # 模拟 route_task_read 在 local 模式执行 fallback
        return fallback()

    monkeypatch.setattr(main_mod, "route_task_read", _fake_route)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    monkeypatch.setattr(proxy, "task_has_blocking_findings", lambda task_id: True)
    assert main_mod._route_has_blocking_findings(proxy, "T-123") is True
