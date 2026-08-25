"""CLI-049 (A′ cli_command_projection) `cw refresh` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_049_http_rpc.py）：
  - success：--all 经 db.build_full_graph → route_rpc(build_full_graph,
    PROTECTED_MUTATION) + rule_sync_agents_md（fail-soft）；单文件经
    db.refresh_file → route_rpc(workspace.file.refresh_file, PROTECTED_MUTATION)
  - 结构不变量：refresh 为 daemon 权威写路径，Python 仅编排，无本地 SQLite

Rust 侧 cli_handle_refresh_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli049_refresh_all_routes_to_daemon(monkeypatch, capsys):
    """success：refresh --all 经 route_rpc 调用 build_full_graph + rule_sync_agents_md。"""
    calls = []

    def _fake_route(method, params, op_class):
        calls.append((method, op_class))
        if method == "build_full_graph":
            return {"ok": True}
        return {"success": True, "rule_count": 0}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_refresh(["--all"], proxy)
    assert rc is True
    methods = [m for m, _ in calls]
    assert "build_full_graph" in methods
    assert ("build_full_graph", "PROTECTED_MUTATION") in calls
    assert "rule.sync_agents_md" in methods, "refresh --all 必须触发 AGENTS.md 自动同步"


def test_cli049_refresh_file_routes_to_daemon(monkeypatch, capsys):
    """success：refresh 单文件经 route_rpc 调用 workspace.file.refresh_file。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"ok": True, "symbols_changed": 2}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_refresh(["a.py"], proxy)
    assert rc is True
    assert captured.get("method") == "workspace.file.refresh_file"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("file_path") == "a.py"
