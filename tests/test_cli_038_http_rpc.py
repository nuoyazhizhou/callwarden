"""CLI-038 (A′ cli_command_projection) `cw guardrail` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_038_http_rpc.py）：
  - success：scan 经 db.scan_guardrails → route_rpc(guardrail_scan,
    PROTECTED_MUTATION)；--category 附加 guardrail_list_rules 做展示层映射
  - success：rules 经 db.guardrail_list_rules → route_rpc(guardrail_list_rules,
    READ_ONLY)

Rust 侧 summary_query_handlers.rs（handle_guardrail_list_rules，MCP-037 已迁移）
/ cli_handle_guardrail_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli038_guardrail_rules_routes_to_daemon(monkeypatch, capsys):
    """success：guardrail rules 经 route_rpc 调用 guardrail_list_rules。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [{"rule_id": "R1", "category": "db_safety", "description": "no raw sql"}]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_guardrail(["rules", "--category", "db_safety"], proxy)
    assert rc is True
    assert captured.get("method") == "guardrail_list_rules"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("category_filter") == "db_safety"
    out = capsys.readouterr().out
    assert "R1" in out


def test_cli038_guardrail_scan_routes_to_daemon(monkeypatch, capsys):
    """success：guardrail scan 经 route_rpc 调用 guardrail_scan。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [{"rule_id": "R1", "severity": "block", "file_path": "a.py",
                 "message": "bad", "symbol_hash": "abc"}]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_guardrail(["scan", "--file", "a.py"], proxy)
    assert rc is True
    assert captured.get("method") == "guardrail_scan"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("file_filter") == "a.py"
    out = capsys.readouterr().out
    assert "R1" in out
