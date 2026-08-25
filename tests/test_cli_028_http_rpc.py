"""CLI-028 (A′ cli_command_projection) `cw defect` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_028_http_rpc.py）：
  - success：search 经 db.defect_pattern_search → route_rpc(query.defect_search,
    READ_ONLY)；stats 经 db.defect_stats → route_rpc(defect.stats, READ_ONLY)；
    learn 经 db.learn_defect_from_fix → route_rpc(defect_learn, PROTECTED_MUTATION)
  - 结构不变量：learn/suggest 为 PROTECTED_MUTATION

Rust 侧 cli_handle_defect_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli028_defect_search_routes_to_daemon(monkeypatch, capsys):
    """success：defect search 经 route_rpc 调用 query.defect_search。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"pattern_id": "P1", "category": "null", "severity": "error",
             "description": "null deref", "case_count": 3},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_defect(
        ["search", "--category", "null", "--severity", "error", "--limit", "10"],
        proxy)
    assert rc is True
    assert captured.get("method") == "query.defect_search"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("category") == "null"
    assert captured["params"].get("severity_filter") == "error"
    out = capsys.readouterr().out
    assert "P1" in out


def test_cli028_defect_stats_routes_to_daemon(monkeypatch, capsys):
    """success：defect stats 经 route_rpc 调用 defect.stats。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return {"total_patterns": 12, "total_fixes": 30}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_defect(["stats"], proxy)
    assert rc is True
    assert captured.get("method") == "defect.stats"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "12" in out


def test_cli028_defect_learn_routes_to_daemon(monkeypatch, capsys):
    """success：defect learn 经 route_rpc 调用 defect_learn（PROTECTED_MUTATION）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"learned_patterns": 2, "learned_fixes": 1, "details": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_defect(["learn", "abc123"], proxy)
    assert rc is True
    assert captured.get("method") == "defect_learn"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("commit_hash") == "abc123"
    out = capsys.readouterr().out
    assert "abc123" in out
