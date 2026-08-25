"""CLI-021 (A′ cli_command_projection) `cw clone` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_021_http_rpc.py）：
  - success：list 经 db.list_clones → route_rpc(list_clones, READ_ONLY)；
    stats 经 db.get_clone_stats → route_rpc(task.clone_stats, READ_ONLY)
  - 结构不变量：clear 为 admin.clear_clones(PROTECTED_MUTATION)

Rust 侧 task_read_handlers.rs（handle_list_clones，MCP-067 已迁移）由 Rust
专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli021_clone_list_routes_to_daemon(monkeypatch, capsys):
    """success：clone list 经 route_rpc 调用 list_clones。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        if method == "list_clones":
            return [
                {"clone_type": 1, "similarity": 0.95, "file_a": "a.py",
                 "symbol_a_line": 3, "symbol_a_name": "fa",
                 "file_b": "b.py", "symbol_b_line": 9, "symbol_b_name": "fb"},
            ]
        return {"id": 7}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_clone(
        ["list", "--type", "1", "--min-similarity", "0.9", "--limit", "20"],
        proxy)
    assert rc is True
    assert captured.get("method") == "list_clones"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("clone_type") == 1
    assert captured["params"].get("limit") == 20
    out = capsys.readouterr().out
    assert "Type-1" in out
    assert "a.py" in out


def test_cli021_clone_stats_routes_to_daemon(monkeypatch, capsys):
    """success：clone stats 经 route_rpc 调用 task.clone_stats。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return {"total": 3, "type1": 1, "type2": 2}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_clone(["stats"], proxy)
    assert rc is True
    assert captured.get("method") == "task.clone_stats"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "3" in out
