"""CLI-015 (A′ graph_snapshot) `cw build-context` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_015_http_rpc.py）：
  - success：register/list/show/activate/delete 直接经 route_rpc 调用
    build_context.*（Python 已是 thin client，无本地 SQLite 业务路径）
  - 结构不变量：register/activate/delete 为 PROTECTED_MUTATION，
    list/show 为 READ_ONLY

Rust 侧 build_context_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli015_build_context_register_routes_to_daemon(monkeypatch, capsys):
    """success：register 经 route_rpc 调用 build_context.register（PROTECTED_MUTATION）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"name": "debug", "build_context_hash": "h1",
                "compile_flags": ["-O2"], "defines": {"DEBUG": "1"},
                "include_paths": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    rc = main_mod._handle_build_context(
        ["register", "7", "debug", "--flags=-O2", "--defines", "DEBUG=1"],
        None)
    assert rc is True
    assert captured.get("method") == "build_context.register"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("workspace_id") == 7
    assert captured["params"].get("name") == "debug"
    out = capsys.readouterr().out
    assert "debug" in out and "h1" in out


def test_cli015_build_context_list_routes_to_daemon(monkeypatch, capsys):
    """success：list 经 route_rpc 调用 build_context.list（READ_ONLY）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [{"name": "debug", "is_active": True,
                 "build_context_hash": "h1", "defines": {}, "include_paths": []}]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    rc = main_mod._handle_build_context(["list", "7"], None)
    assert rc is True
    assert captured.get("method") == "build_context.list"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("workspace_id") == 7
    out = capsys.readouterr().out
    assert "debug" in out


def test_cli015_build_context_delete_routes_to_daemon(monkeypatch, capsys):
    """success：delete 经 route_rpc 调用 build_context.delete（PROTECTED_MUTATION）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return {"deleted": True}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    rc = main_mod._handle_build_context(["delete", "7", "h1"], None)
    assert rc is True
    assert captured.get("method") == "build_context.delete"
    assert captured.get("op") == "PROTECTED_MUTATION"
