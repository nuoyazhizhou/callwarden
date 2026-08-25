"""CLI-068 (A′ cli_command_projection) `cw topo` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_068_http_rpc.py）：
  - success：db.get_topological_order 经 RpcDBProxy._rpc_call → route_rpc
    （query.topological_order，READ_ONLY），Python 仅编排输出
  - 参数契约：limit 透传（默认 50）
  - 结构不变量：Rust 侧 query.topological_order（handle_query_topological_order）
    为唯一 authority，返回符号列表（name/depth/path/start_line）

Rust 侧 query.topological_order 由 R6/W2 迁移核验。
"""

import callwarden.cli.main as main_mod


def test_cli068_topo_routes_to_daemon(monkeypatch, capsys):
    """success：topo 经 route_rpc 调用 query.topological_order。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"name": "foo", "depth": 0, "path": "a.py", "start_line": 1},
            {"name": "bar", "depth": 1, "rel_path": "b.py", "start_line": 10},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_topo(["--limit", "50"], proxy)
    assert rc is True
    assert captured.get("method") == "query.topological_order"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("limit") == 50
    out = capsys.readouterr().out
    assert "foo" in out and "a.py" in out
    assert "bar" in out and "b.py" in out
