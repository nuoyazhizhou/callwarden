"""CLI-054 (A′ cli_command_projection) `cw rule cleanup-sync-log` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_054_http_rpc.py）：
  - success：db.cleanup_sync_log 经 RpcDBProxy._rpc_call → route_rpc
    （admin.cleanup_rule_sync_log，PROTECTED_MUTATION），Python 仅编排输出
  - 参数契约：older_than_days/keep_latest/dry_run 原样透传，dry_run 默认
    True（--apply 才 False）
  - 结构不变量：Rust 侧 cli_handle_rule_cleanup_sync_log_handlers.rs
    handle_cleanup_sync_log（CLI-054 新建）为唯一 authority，修复旧 handler
    忽略 dry_run 导致 dry-run 误删全部旧记录的问题

Rust 侧由 cargo 单测 + live daemon fixture 核验（见
cli_handle_rule_cleanup_sync_log_handlers.rs 单元测试与
test_cli_054_http_rpc_daemon.py live 契约）。
"""

import callwarden.cli.main as main_mod


def test_cli054_cleanup_sync_log_routes_to_daemon(monkeypatch, capsys):
    """success：cleanup-sync-log 经 route_rpc 调用 admin.cleanup_rule_sync_log。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "success": True, "dry_run": True, "deleted_count": 3,
            "remaining_count": 97, "total_before": 100,
            "older_than_days": 90, "keep_latest": 100,
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {
        "older_than": 90, "keep_latest": 100, "apply": False,
    })()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_cleanup_sync_log(opts, proxy)
    assert rc is True
    assert captured.get("method") == "admin.cleanup_rule_sync_log"
    assert captured.get("op") == "PROTECTED_MUTATION"
    # 默认 dry-run（未 --apply）→ dry_run=True
    assert captured["params"].get("older_than_days") == 90
    assert captured["params"].get("keep_latest") == 100
    assert captured["params"].get("dry_run") is True
    out = capsys.readouterr().out
    assert "100" in out and "3" in out and "97" in out


def test_cli054_cleanup_sync_log_apply_sets_dry_run_false(monkeypatch, capsys):
    """--apply → dry_run=False，真正删除仍经 daemon 执行。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "success": True, "dry_run": False, "deleted_count": 42,
            "remaining_count": 58, "total_before": 100,
            "older_than_days": 30, "keep_latest": 50,
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {
        "older_than": 30, "keep_latest": 50, "apply": True,
    })()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_cleanup_sync_log(opts, proxy)
    assert rc is True
    assert captured.get("method") == "admin.cleanup_rule_sync_log"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("older_than_days") == 30
    assert captured["params"].get("keep_latest") == 50
    assert captured["params"].get("dry_run") is False
    out = capsys.readouterr().out
    assert "42" in out


def test_cli054_cleanup_sync_log_failure_surfaces_error(monkeypatch, capsys):
    """daemon 业务失败：Python 只展示 error，不本地兜底。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"success": False, "error": "E_PERMISSION", "dry_run": True}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {
        "older_than": 90, "keep_latest": 100, "apply": False,
    })()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_cleanup_sync_log(opts, proxy)
    assert rc is True
    assert captured.get("method") == "admin.cleanup_rule_sync_log"
    out = capsys.readouterr().out
    assert "E_PERMISSION" in out
