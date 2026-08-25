"""CLI-045 (A′ cli_command_projection) `cw map` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_045_http_rpc.py）：
  - success：db.repo_map 经 RpcDBProxy._rpc_call → route_rpc
    （repo_map，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 handle_repo_map（MCP-031 已迁移）为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 summary_query_handlers.rs
（MCP-031 已迁移）由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli045_map_routes_to_daemon(monkeypatch, capsys):
    """success：map 经 route_rpc 调用 repo_map。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return "core -> api"

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_map(["--format", "text"], proxy)
    assert rc is True
    assert captured.get("method") == "repo_map"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("format") == "text"
    out = capsys.readouterr().out
    assert "core -> api" in out
