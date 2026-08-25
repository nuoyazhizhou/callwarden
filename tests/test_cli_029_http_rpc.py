"""CLI-029 (A′ graph_snapshot) `cw dependency` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_029_http_rpc.py）：
  - success：dependency 入口经 db.get_active_workspace → RpcDBProxy._rpc_call
    → route_rpc(workspace.status, READ_ONLY) 获取数值 workspace_id
  - 结构不变量：list/cycle/explain/provider-select 各 action 的 SQL 由
    route_rpc / cli_admin 只读辅助承担，CLI 无 direct SQLite 业务路径

Rust 侧 workspace.status dispatch 分支（dispatch.rs / http_server.rs）由
Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli029_dependency_get_active_workspace_routes(monkeypatch):
    """success：get_active_workspace 经 route_rpc 调用 workspace.status。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return {"workspace_id": 7, "name": "proj", "status": "active",
                "client_view_root": "C:/git_work/proj",
                "workspace_instance_id": "ws-1", "description": ""}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/proj")
    ws = proxy.get_active_workspace()
    assert captured.get("method") == "workspace.status"
    assert captured.get("op") == "READ_ONLY"
    assert ws is not None
    assert ws["id"] == 7
    assert ws["is_active"] is True


def test_cli029_dependency_cycle_uses_workspace_id(monkeypatch, capsys):
    """success：dependency cycle 先取 workspace.status，再经 detect_cycle。"""
    calls = []

    def _fake_route(method, params, op_class):
        calls.append((method, op_class))
        if method == "workspace.status":
            return {"workspace_id": 7, "name": "proj", "status": "active",
                    "client_view_root": "C:/git_work/proj",
                    "workspace_instance_id": "ws-1", "description": ""}
        if method == "detect_cycle":
            return {"has_cycle": False, "cycle_path": []}
        return {}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/proj")
    rc = main_mod._handle_dependency(["cycle"], proxy)
    assert rc is True
    assert calls[0] == ("workspace.status", "READ_ONLY")
    assert ("detect_cycle", "READ_ONLY") in calls
