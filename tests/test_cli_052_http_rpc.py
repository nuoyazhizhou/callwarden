"""CLI-052 (A′ cli_command_projection) `cw rule-applicable` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_052_http_rpc.py）：
  - success：db.get_applicable_rules 经 RpcDBProxy._rpc_call → route_rpc
    （get_applicable_rules，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 handle_get_applicable_rules（MCP-062 已迁移）为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 security_query_handlers.rs
（MCP-062 已迁移）由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli052_rule_applicable_routes_to_daemon(monkeypatch, capsys):
    """success：rule-applicable 经 route_rpc 调用 get_applicable_rules。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [{"id": "R1", "title": "no raw sql", "severity": "error",
                 "rule_text": "avoid sqlite3.connect"}]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {
        "context": '{"task":"x"}', "limit": 20,
        "target": "", "apply": False, "actor": "cli",
    })()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_applicable(opts, proxy)
    assert rc is True
    assert captured.get("method") == "get_applicable_rules"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("limit") == 20
    assert captured["params"]["context"].get("task") == "x"
    out = capsys.readouterr().out
    assert "R1" in out
