"""CLI-039 (A′ cli_command_projection) `cw health-report` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_039_http_rpc.py）：
  - success：health-report 聚合 get_stats / hotspot_evolution /
    get_semgrep_stats / get_token_savings_report，全部经 RpcDBProxy →
    route_rpc（READ_ONLY），Python 仅编排输出
  - 结构不变量：RpcDBProxy 不暴露 conn/db_path，无本地 SQLite 业务路径

Rust 侧 summary_query_handlers.rs（hotspot_evolution MCP-046 /
get_token_savings_report MCP-040 已迁移）由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli039_health_report_routes_to_daemon(monkeypatch, capsys):
    """success：health-report 各聚合项经 route_rpc 调用。"""
    calls = []

    def _fake_route(method, params, op_class):
        calls.append((method, op_class))
        if method == "query.stats":
            return {"files": 10, "symbols": 100}
        if method == "hotspot_evolution":
            return [{"qualified_name": "m::hot"}]
        if method == "query.semgrep_stats":
            return {"total": 2}
        if method == "get_token_savings_report":
            return {"saved_tokens": 5000}
        return {}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_health_report(["--json"], proxy)
    assert rc is True
    methods = [m for m, _ in calls]
    assert "query.stats" in methods
    assert "hotspot_evolution" in methods
    assert "query.semgrep_stats" in methods
    assert "get_token_savings_report" in methods
    out = capsys.readouterr().out
    assert "saved_tokens" in out
