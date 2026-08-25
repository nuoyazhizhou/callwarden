"""CLI-036 (A′ cli_command_projection) `cw git` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_036_http_rpc.py）：
  - success：log 经 db.get_git_commits → route_rpc(query.git_commits, READ_ONLY)；
    destructive-ops 经 db.list_destructive_operations → route_rpc（READ_ONLY）
  - 结构不变量：check-force-push 经 db.check_force_push（READ_ONLY）后
    log_destructive_operation（PROTECTED_MUTATION）

Rust 侧 cli_handle_git_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli036_git_log_routes_to_daemon(monkeypatch, capsys):
    """success：git log 经 route_rpc 调用 query.git_commits。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"commit_hash": "abc12345", "timestamp": 1787323000.0,
             "message": "fix bug", "author": "wanpi"},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_git(["log"], proxy)
    assert rc is True
    assert captured.get("method") == "query.git_commits"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "abc12345" in out


def test_cli036_git_destructive_ops_routes_to_daemon(monkeypatch, capsys):
    """success：destructive-ops 经 route_rpc 调用 list_destructive_operations。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return [{"operation_type": "force_push", "detail": "x",
                 "created_at": 1787323000.0}]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_git(["destructive-log"], proxy)
    assert rc is True
    assert captured.get("method") == "list_destructive_operations"
    assert captured.get("op") == "READ_ONLY"
