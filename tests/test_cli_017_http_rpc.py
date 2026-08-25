"""CLI-017 (A′ cli_command_projection) `cw callees` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_017_http_rpc.py）：
  - success：db.get_callees 经 RpcDBProxy._rpc_call → route_rpc
    （query.callees，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.callees dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli017_callees_routes_to_daemon(monkeypatch, capsys):
    """success：callees 经 route_rpc 调用 query.callees。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"call_line": 10, "callee_name": "bar", "is_cross_file": True,
             "callee_file": "b.py"},
            {"call_line": 12, "callee_name": "baz", "is_cross_file": False,
             "callee_file": ""},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_callees(["foo"], proxy)
    assert rc is True
    assert captured.get("method") == "query.callees"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("caller_name") == "foo"
    assert captured["params"].get("qualified_name") is None
    out = capsys.readouterr().out
    assert "bar" in out
    assert "baz" in out


def test_cli017_callees_qualified_param(monkeypatch, capsys):
    """success：--qualified 透传到 query.callees。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["params"] = params
        return []

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_callees(["foo", "--qualified", "m::foo"], proxy)
    assert rc is True
    assert captured["params"].get("caller_name") == "foo"
    assert captured["params"].get("qualified_name") == "m::foo"
