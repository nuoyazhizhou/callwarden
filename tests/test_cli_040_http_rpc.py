"""CLI-040 (A′ cli_command_projection) `cw hotspot` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_040_http_rpc.py）：
  - success：db.hotspot_evolution 经 RpcDBProxy._rpc_call → route_rpc
    （hotspot_evolution，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 handle_hotspot_evolution（MCP-046 已迁移）为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 summary_query_handlers.rs
（MCP-046 已迁移）由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli040_hotspot_routes_to_daemon(monkeypatch, capsys):
    """success：hotspot 经 route_rpc 调用 hotspot_evolution。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"qualified_name": "m::hot", "change_count": 8, "hot_score": 42.5},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_hotspot(["--module", "m", "--limit", "10"], proxy)
    assert rc is True
    assert captured.get("method") == "hotspot_evolution"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("module_filter") == "m"
    out = capsys.readouterr().out
    assert "m::hot" in out
