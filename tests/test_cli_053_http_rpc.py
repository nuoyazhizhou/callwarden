"""CLI-053 (A′ cli_command_projection) `cw rule candidate` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_053_http_rpc.py）：
  - success：create 经 db.rule_candidate_create → route_rpc(rule.candidate_create,
    PROTECTED_MUTATION)；list 经 db.rule_candidate_list → route_rpc
    (rule_candidate_list, READ_ONLY)
  - 结构不变量：accept/reject 为 PROTECTED_MUTATION

Rust 侧 security_query_handlers.rs（handle_rule_candidate_list，MCP-060 已迁移）
由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli053_rule_candidate_create_routes_to_daemon(monkeypatch, capsys):
    """success：candidate create 经 route_rpc 调用 rule.candidate_create。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return 5

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {
        "cand_action": "create", "title": "no raw sql",
        "text": "avoid sqlite", "scope": '{"languages":["python"]}',
        "severity": "error", "source": "audit",
        "evidence": '{"finding": 1}', "confidence": 0.9,
        "status": "", "limit": 100, "candidate_id": 0, "reviewer": "", "reason": "",
        "target": "", "apply": False, "actor": "", "older_than": 90,
        "task_id": "", "min_occurrences": 2,
    })()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_candidate(opts, proxy)
    assert rc is True
    assert captured.get("method") == "rule.candidate_create"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("title") == "no raw sql"
    out = capsys.readouterr().out
    assert "5" in out


def test_cli053_rule_candidate_list_routes_to_daemon(monkeypatch, capsys):
    """success：candidate list 经 route_rpc 调用 rule_candidate_list。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [{"id": 1, "title": "cand", "severity": "info", "source": "ci",
                 "status": "pending", "rule_text": "if x: pass"}]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {
        "cand_action": "list", "status": "pending", "limit": 50,
        "title": "", "text": "", "scope": "", "severity": "", "source": "",
        "evidence": "", "confidence": 0.0, "candidate_id": 0,
        "reviewer": "", "reason": "", "target": "", "apply": False,
        "actor": "", "older_than": 90, "task_id": "", "min_occurrences": 2,
    })()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_candidate(opts, proxy)
    assert rc is True
    assert captured.get("method") == "rule_candidate_list"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("status") == "pending"
    assert captured["params"].get("limit") == 50
