"""CLI-041 (A′ cli_command_projection) `cw impact` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_041_http_rpc.py）：
  - success：db.blast_radius 经 RpcDBProxy._rpc_call → route_rpc
    （blast_radius，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 handle_blast_radius（MCP-038 已迁移）为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 summary_query_handlers.rs
（MCP-038 已迁移）由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli041_impact_routes_to_daemon(monkeypatch, capsys):
    """success：impact 经 route_rpc 调用 blast_radius。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"source_symbol": {"name": "foo"}, "source_hash": "abc123",
                "depth": 5, "total_impacted": 5,
                "by_layer": {"code": 4, "db": 1},
                "layers": [{"depth": 1, "symbols": [{"name": "bar"}]}]}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_impact(["abc123", "--depth", "5"], proxy)
    assert rc is True
    assert captured.get("method") == "blast_radius"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("symbol_hash") == "abc123"
    assert captured["params"].get("depth") == 5
    out = capsys.readouterr().out
    assert "foo" in out
