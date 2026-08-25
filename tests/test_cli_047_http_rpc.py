"""CLI-047 (A′ cli_command_projection) `cw ownership-map` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_047_http_rpc.py）：
  - success：db.get_ownership_map 经 RpcDBProxy._rpc_call → route_rpc
    （get_ownership_map，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 handle_get_ownership_map（MCP-034 已迁移）为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 summary_query_handlers.rs
（MCP-034 已迁移）由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli047_ownership_map_routes_to_daemon(monkeypatch, capsys):
    """success：ownership-map 经 route_rpc 调用 get_ownership_map。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return [
            {"module": "core", "primary_owner": "wanpi", "file_count": 5,
             "owners": [{"name": "wanpi", "file_count": 5}]},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_ownership_map([], proxy)
    assert rc is True
    assert captured.get("method") == "get_ownership_map"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "core" in out
    assert "wanpi" in out
