"""CLI-031 (A′ cli_command_projection) `cw file` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_031_http_rpc.py）：
  - success：db.get_file_symbols 经 RpcDBProxy._rpc_call → route_rpc
    （query.file，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.file dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli031_file_routes_to_daemon(monkeypatch, capsys):
    """success：file 经 route_rpc 调用 query.file。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"start_line": 1, "end_line": 5, "kind": "function",
             "name": "foo", "visibility": "public"},
            {"start_line": 7, "end_line": 9, "kind": "class",
             "name": "Bar", "visibility": "public"},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_file(["a.py"], proxy)
    assert rc is True
    assert captured.get("method") == "query.file"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("file_path") == "a.py"
    out = capsys.readouterr().out
    assert "foo" in out
    assert "Bar" in out
