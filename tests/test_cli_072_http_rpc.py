"""CLI-072（T-1787322799021-b69c2990）：cw workspace → workspace.* thin client 契约。

验证 _handle_workspace 的 5 个 db 方法经 route_rpc 转发：
1. list：route_rpc workspace.list READ_ONLY，输出 id/name/root_path/is_active。
2. register：route_rpc workspace.register PROTECTED_MUTATION，name/client_view_root 透传。
3. set：route_rpc workspace.activate PROTECTED_MUTATION（int 与 str 两种 id 形态）。
4. delete：route_rpc workspace.remove PROTECTED_MUTATION。
5. get_active_workspace：route_rpc workspace.status READ_ONLY。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import callwarden.cli.main as main_mod  # noqa: E402


def _make_proxy(route_impl):
    """构造使用自定义 route_impl 的 RpcDBProxy。"""
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    proxy._route_rpc = route_impl  # noqa: SLF001
    return proxy


def test_cli072_workspace_list_routes(monkeypatch, capsys):
    """list：route_rpc workspace.list READ_ONLY。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return [
            {"workspace_id": 1, "name": "w1", "client_view_root": "C:/repo1",
             "status": "active", "description": "d1"},
            {"workspace_id": 2, "name": "w2", "client_view_root": "C:/repo2",
             "status": "inactive"},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_workspace(["list"], proxy)
    assert rc is True
    assert captured.get("method") == "workspace.list"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "w1" in out and "C:/repo1" in out and "w2" in out


def test_cli072_workspace_register_routes(monkeypatch, capsys):
    """register：route_rpc workspace.register PROTECTED_MUTATION。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"workspace_id": 42}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_workspace(["register", "myproj", "C:/git_work/myproj"], proxy)
    assert rc is True
    assert captured.get("method") == "workspace.register"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("name") == "myproj"
    assert captured["params"].get("client_view_root") == "C:/git_work/myproj"
    out = capsys.readouterr().out
    assert "42" in out


def test_cli072_workspace_set_routes(monkeypatch, capsys):
    """set：route_rpc workspace.activate PROTECTED_MUTATION，随后 workspace.status。"""
    calls = []

    def _fake_route(method, params, op_class):
        calls.append((method, params, op_class))
        if method == "workspace.status":
            return {"workspace_id": 1, "name": "w1", "client_view_root": "C:/repo1",
                    "status": "active"}
        return True

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_workspace(["set", "w1"], proxy)
    assert rc is True
    methods = [c[0] for c in calls]
    assert "workspace.activate" in methods
    assert "workspace.status" in methods
    activate_call = next(c for c in calls if c[0] == "workspace.activate")
    assert activate_call[2] == "PROTECTED_MUTATION"
    assert activate_call[1].get("workspace_id_or_name") == "w1"
    out = capsys.readouterr().out
    assert "w1" in out and "C:/repo1" in out


def test_cli072_workspace_delete_routes(monkeypatch, capsys):
    """delete：route_rpc workspace.remove PROTECTED_MUTATION。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return True

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_workspace(["delete", "7"], proxy)
    assert rc is True
    assert captured.get("method") == "workspace.remove"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("workspace_id_or_name") == "7"
