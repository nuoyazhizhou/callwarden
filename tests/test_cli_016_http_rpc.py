"""CLI-016 (A′ graph_snapshot) `cw call-chain` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_016_http_rpc.py）：
  - success：db.get_call_chain_down 经 RpcDBProxy._rpc_call → route_rpc
    （query.call_chain_down，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.call_chain_down dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli016_call_chain_routes_to_daemon(monkeypatch, capsys):
    """success：call-chain 经 route_rpc 调用 query.call_chain_down。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "start": "foo",
            "total_downstream": 3,
            "max_depth_reached": 2,
            "levels": [
                {"depth": 1, "count": 2, "callees": [{"callee": "bar"}, {"callee": "baz"}]},
                {"depth": 2, "count": 1, "callees": [{"callee": "qux"}]},
            ],
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_call_chain(["foo", "--depth", "5"], proxy)
    assert rc is True
    assert captured.get("method") == "query.call_chain_down"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("qualified_name") == "foo"
    assert captured["params"].get("max_depth") == 5
    out = capsys.readouterr().out
    assert "foo" in out
    assert "→ bar" in out
    assert "→ qux" in out
