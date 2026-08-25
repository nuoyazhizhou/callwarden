"""CLI-018 (A′ cli_command_projection) `cw callers` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_018_http_rpc.py）：
  - success：db.get_callers 经 RpcDBProxy._rpc_call → route_rpc
    （query.callers，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.callers dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli018_callers_routes_to_daemon(monkeypatch, capsys):
    """success：callers 经 route_rpc 调用 query.callers。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"caller_file": "a.py", "call_line": 5, "caller_name": "top",
             "is_cross_file": False},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_callers(["foo"], proxy)
    assert rc is True
    assert captured.get("method") == "query.callers"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("callee_name") == "foo"
    assert captured["params"].get("qualified_name") is None
    out = capsys.readouterr().out
    assert "top" in out


def test_cli018_callers_qualified_param(monkeypatch, capsys):
    """success：--qualified 透传到 query.callers。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["params"] = params
        return []

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_callers(["foo", "--qualified", "m::foo"], proxy)
    assert rc is True
    assert captured["params"].get("callee_name") == "foo"
    assert captured["params"].get("qualified_name") == "m::foo"
