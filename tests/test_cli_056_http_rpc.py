"""CLI-056 (A′ cli_command_projection) `cw rule-insert-block` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_056_http_rpc.py）：
  - success：db.rule_insert_agents_md_block 经 RpcDBProxy._rpc_call →
    route_rpc（rule.insert_agents_md_block，PROTECTED_MUTATION），Python 仅编排输出
  - 参数契约：target_path/actor 原样透传（target 默认 AGENTS.md、actor 默认 agent）
  - 结构不变量：Rust 侧 handle_rule_insert_agents_md_block（CLI-056 修复）返回
    {success, target_path, message} 且标记区已存在时 success=False（不覆盖），
    与 Python db.rule_insert_agents_md_block 契约一致；修复旧实现返回
    {ok, bytes_written} 导致 CLI 永远走失败分支的问题

Rust 侧由 cargo 单测覆盖（见 edit_handlers.rs::tests::insert_agents_md_block_*）。
"""

import callwarden.cli.main as main_mod


def test_cli056_rule_insert_block_routes_to_daemon(monkeypatch, capsys):
    """success：rule-insert-block 经 route_rpc 调用 rule.insert_agents_md_block。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"success": True, "target_path": "AGENTS.md",
                "message": "Marker block inserted into AGENTS.md"}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"target": "AGENTS.md", "actor": "agent"})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_insert_block(opts, proxy)
    assert rc is True
    assert captured.get("method") == "rule.insert_agents_md_block"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("target_path") == "AGENTS.md"
    assert captured["params"].get("actor") == "agent"
    out = capsys.readouterr().out
    assert "Inserted marker block" in out


def test_cli056_rule_insert_block_failure(monkeypatch, capsys):
    """daemon 业务失败：标记区已存在 → success=False，Python 展示 message。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"success": False, "target_path": "AGENTS.md",
                "message": "Marker block already exists in AGENTS.md"}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"target": "AGENTS.md", "actor": "agent"})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_insert_block(opts, proxy)
    assert rc is True
    assert captured.get("method") == "rule.insert_agents_md_block"
    out = capsys.readouterr().out
    assert "already exists" in out
