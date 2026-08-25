"""CLI-051 (A′ task_projection) `cw rollback` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_051_http_rpc.py）：
  - success：register 经 db.register_rollback_config → route_rpc
    （register_rollback_config，PROTECTED_MUTATION）；set 经 db.set_rollback_flag →
    route_rpc(set_rollback_flag，PROTECTED_MUTATION)；is-rolled-back 经
    db.is_feature_rolled_back → route_rpc(is_feature_rolled_back, READ_ONLY)

Rust 侧 rollback_handlers.rs / cli_handle_rollback_handlers.rs 由 Rust 专项
agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli051_rollback_set_routes_to_daemon(monkeypatch, capsys):
    """success：rollback set 经 route_rpc 调用 set_rollback_flag（PROTECTED_MUTATION）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"success": True, "feature_name": "rust_cas", "previous_flag": 0}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rollback(
        ["set", "T-1", "1", "--reason", "bug"], proxy)
    assert rc is True
    assert captured.get("method") == "set_rollback_flag"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("task_id") == "T-1"
    assert captured["params"].get("flag") == 1
    out = capsys.readouterr().out
    assert "ROLLED BACK" in out


def test_cli051_rollback_is_rolled_back_routes(monkeypatch, capsys):
    """success：is-rolled-back 经 route_rpc 调用 is_feature_rolled_back。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return False

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)
    monkeypatch.setattr("sys.exit", lambda code: None)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rollback(["is-rolled-back", "rust_cas"], proxy)
    assert rc is True
    assert captured.get("method") == "is_feature_rolled_back"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("feature_name") == "rust_cas"
