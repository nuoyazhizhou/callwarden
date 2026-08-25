"""CLI-057 (A′ cli_command_projection) `cw rule-list` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_057_http_rpc.py）：
  - success：db.rule_list 经 RpcDBProxy._rpc_call → route_rpc（rule_list，
    READ_ONLY），Python 仅解包 MCP-061 契约 {"rules": [...], "count": n} 并格式化输出
  - 参数契约：status/limit 原样透传（status 默认 active、limit 默认 100）
  - 结构不变量：Rust 侧 task_collab.rs::handle_rule_list（MCP-061 已迁移）为
    唯一 authority，返回包装 dict；CLI-057 适配解包，不再要求 Python db 直查

Rust 侧由 MCP-061（T-1787321712961-d86f42d0，closed/applied）核验。
"""

import callwarden.cli.main as main_mod


def _rules_sample():
    return [
        {"id": "AR-1", "title": "no raw sql", "severity": "error",
         "rule_text": "avoid sqlite3.connect", "scope": {"languages": ["python"]},
         "synced_to_agents_md": 1},
        {"id": "AR-2", "title": "no secrets", "severity": "warning",
         "rule_text": "avoid hardcoded keys", "scope": {},
         "synced_to_agents_md": 0},
    ]


def test_cli057_rule_list_routes_to_daemon(monkeypatch, capsys):
    """success：rule-list 经 route_rpc 调用 rule_list，解包 rules 输出。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"rules": _rules_sample(), "count": 2}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"status": "active", "limit": 100})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_list(opts, proxy)
    assert rc is True
    assert captured.get("method") == "rule_list"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("status") == "active"
    assert captured["params"].get("limit") == 100
    out = capsys.readouterr().out
    assert "AR-1" in out and "no raw sql" in out
    assert "AR-2" in out and "no secrets" in out
    assert "2" in out  # count


def test_cli057_rule_list_empty(monkeypatch, capsys):
    """空结果：{"rules": [], "count": 0} → 输出 (empty) 不崩溃。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"rules": [], "count": 0}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"status": "", "limit": 50})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_list(opts, proxy)
    assert rc is True
    assert captured["params"].get("status") == ""
    assert captured["params"].get("limit") == 50
    out = capsys.readouterr().out
    assert "empty" in out


def test_cli057_rule_list_accepts_plain_list_fallback(monkeypatch, capsys):
    """兼容旧裸 list 返回：非 dict 时直接用 list，不崩溃。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return _rules_sample()

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"status": "active", "limit": 100})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_list(opts, proxy)
    assert rc is True
    out = capsys.readouterr().out
    assert "AR-1" in out
