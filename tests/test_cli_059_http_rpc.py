"""CLI-059 (A′ cli_command_projection) `cw rule-sync` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_059_http_rpc.py）：
  - success：db.rule_sync_agents_md 经 RpcDBProxy._rpc_call → route_rpc
    （rule.sync_agents_md，PROTECTED_MUTATION），Python 仅编排输出
  - 参数契约：target_path/dry_run/actor 透传（dry_run 默认 True，--apply → False）
  - 结构不变量：Rust 侧 handle_rule_sync_agents_md（CLI-059 修复）返回
    {success,dry_run,target_path,rule_count,rule_ids,before_hash,after_hash,
    preview,error,suggested_block}，与 Python db.rule_sync_agents_md 契约一致；
    修复旧实现 marker 常量错误、标记区缺失静默追加、未回写 synced 的问题

Rust 侧由 cargo 单测覆盖（见 edit_handlers.rs::tests::sync_agents_md_*）。
"""

import callwarden.cli.main as main_mod


def test_cli059_rule_sync_routes_to_daemon(monkeypatch, capsys):
    """success：rule-sync 经 route_rpc 调用 rule.sync_agents_md（默认 dry-run）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "success": True, "dry_run": True, "target_path": "AGENTS.md",
            "rule_count": 2, "rule_ids": ["AR-1", "AR-2"],
            "before_hash": "abc", "after_hash": "def",
            "preview": "<!-- CALLWARDEN_RULES_START -->\n- [AR-1] **r1** (severity: warning): t1",
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"target": "AGENTS.md", "apply": False, "actor": "agent"})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_sync(opts, proxy)
    assert rc is True
    assert captured.get("method") == "rule.sync_agents_md"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("target_path") == "AGENTS.md"
    assert captured["params"].get("dry_run") is True
    assert captured["params"].get("actor") == "agent"
    out = capsys.readouterr().out
    assert "Dry-run Preview" in out and "AR-1" in out


def test_cli059_rule_sync_apply(monkeypatch, capsys):
    """--apply → dry_run=False。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "success": True, "dry_run": False, "target_path": "AGENTS.md",
            "rule_count": 2, "rule_ids": ["AR-1", "AR-2"],
            "before_hash": "abc", "after_hash": "def", "preview": "",
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"target": "AGENTS.md", "apply": True, "actor": "agent"})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_sync(opts, proxy)
    assert rc is True
    assert captured["params"].get("dry_run") is False
    out = capsys.readouterr().out
    assert "Synced" in out


def test_cli059_rule_sync_marker_missing(monkeypatch, capsys):
    """标记区缺失：success=False + suggested_block，Python 展示建议。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "success": False, "dry_run": True, "target_path": "AGENTS.md",
            "rule_count": 0, "rule_ids": [], "before_hash": "abc", "after_hash": "",
            "error": "Marker block not found in AGENTS.md. Insert the block first.",
            "suggested_block": "\n\n## Call Warden 自动沉淀规则\n\n<!-- CALLWARDEN_RULES_START -->\n<!-- CALLWARDEN_RULES_END -->\n",
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"target": "AGENTS.md", "apply": False, "actor": "agent"})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_sync(opts, proxy)
    assert rc is True
    out = capsys.readouterr().out
    assert "Marker block not found" in out
    assert "Suggested marker block" in out
