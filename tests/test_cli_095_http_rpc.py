"""CLI-095 (A′ cli_command_projection) `cw run-subcommand-mode` workspace HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_095_http_rpc.py）：
  - success：RpcDBProxy.list_workspaces / register_workspace / set_active_workspace
    分别经 route_rpc(workspace.list / workspace.register / workspace.activate) 走
    HTTP daemon thin client，Python 仅做行映射与编排
  - 结构不变量：run-subcommand-mode 的 workspace 写操作为 daemon 权威，
    无本地 SQLite 业务路径

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 workspace.* handler
（dispatch.rs / http_server.rs / cli_run_subcommand_mode_handlers.rs）由其它 agent 核验。
"""

import pytest

import callwarden.cli.main as main_mod


def test_cli095_workspace_list_routes_to_daemon(monkeypatch):
    """success：list_workspaces 经 route_rpc 调用 workspace.list。"""
    captured = {}

    def _fake_route(method, params, mode):
        captured["method"] = method
        captured["mode"] = mode
        return [
            {"workspace_id": 1, "name": "proj-a",
             "client_view_root": "C:/git_work/proj-a", "status": "active",
             "workspace_instance_id": "ws-1", "description": ""},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/proj-a")
    rows = proxy.list_workspaces()
    assert captured.get("method") == "workspace.list"
    assert captured.get("mode") == "READ_ONLY"
    assert len(rows) == 1
    # daemon_workspaces 行 → legacy workspaces 兼容视图
    assert rows[0]["id"] == 1
    assert rows[0]["root_path"] == "C:/git_work/proj-a"
    assert rows[0]["is_active"] is True


def test_cli095_workspace_register_routes_to_daemon(monkeypatch):
    """success：register_workspace 经 route_rpc 调用 workspace.register。"""
    captured = {}

    def _fake_route(method, params, mode):
        captured["method"] = method
        captured["params"] = params
        captured["mode"] = mode
        return {"workspace_id": 42}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/proj-a")
    ws_id = proxy.register_workspace("proj-a", "C:/git_work/proj-a")
    assert ws_id == 42
    assert captured.get("method") == "workspace.register"
    assert captured.get("mode") == "PROTECTED_MUTATION"
    assert captured["params"].get("client_view_root") == "C:/git_work/proj-a"


def test_cli095_workspace_activate_routes_to_daemon(monkeypatch):
    """success：set_active_workspace 经 route_rpc 调用 workspace.activate。"""
    captured = {}

    def _fake_route(method, params, mode):
        captured["method"] = method
        captured["params"] = params
        captured["mode"] = mode
        return True

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/proj-a")
    ok = proxy.set_active_workspace(42)
    assert ok is True
    assert captured.get("method") == "workspace.activate"
    assert captured.get("mode") == "PROTECTED_MUTATION"
    assert captured["params"].get("workspace_id_or_name") == "42"
