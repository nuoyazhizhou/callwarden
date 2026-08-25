"""CLI-048 (A′ cli_command_projection) `cw query` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_048_http_rpc.py）：
  - success：db.get_symbol_location 经 RpcDBProxy._rpc_call → route_rpc
    （query.symbol_location，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.symbol_location dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli048_query_routes_to_daemon(monkeypatch, capsys):
    """success：query 经 route_rpc 调用 query.symbol_location。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"name": "foo", "file_path": "a.py", "line": 10}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_query(["foo", "a.py"], proxy)
    assert rc is True
    assert captured.get("method") == "query.symbol_location"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("name") == "foo"
    assert captured["params"].get("file_path") == "a.py"
    out = capsys.readouterr().out
    assert '"line": 10' in out
