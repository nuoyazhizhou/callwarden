"""CLI-058 (A′ cli_command_projection) `cw rule-seed-bootstrap` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_058_http_rpc.py）：
  - success：db.rule_seed_bootstrap 经 RpcDBProxy._rpc_call → route_rpc
    （rule.seed_bootstrap，PROTECTED_MUTATION），Python 仅编排输出
  - 参数契约：dry_run 默认 True（未 --apply），--apply → False
  - 结构不变量：Rust 侧 handle_rule_seed_bootstrap（CLI-058 修复）返回
    {dry_run,total,created,updated,skipped,rules:[{id,title,action}]}，与
    Python db.rule_seed_bootstrap 契约一致；修复旧实现忽略 dry_run 导致
    dry-run 真写库、只 seed 3 条无固定 ID 的问题

Rust 侧由 cargo 单测覆盖（见 edit_handlers.rs::tests::seed_bootstrap_*）。
"""

import callwarden.cli.main as main_mod


def _seed_result():
    return {
        "dry_run": True, "total": 5, "created": 5, "updated": 0, "skipped": 0,
        "rules": [
            {"id": "AR-bootstrap-i18n", "title": "用户可见输出必须使用 i18n key", "action": "create"},
            {"id": "AR-bootstrap-refresh-before-commit", "title": "提交前必须刷新代码图谱", "action": "create"},
            {"id": "AR-bootstrap-task-split", "title": "大任务必须通过 Call Warden task 拆分并推进", "action": "create"},
            {"id": "AR-bootstrap-completion-review", "title": "任务完成后必须运行 completion review", "action": "create"},
            {"id": "AR-bootstrap-capture-diff", "title": "外部编辑完成后必须运行 task capture-diff", "action": "create"},
        ],
    }


def test_cli058_seed_bootstrap_routes_to_daemon(monkeypatch, capsys):
    """success：seed-bootstrap 经 route_rpc 调用 rule.seed_bootstrap（默认 dry-run）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return _seed_result()

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"apply": False})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_seed_bootstrap(opts, proxy)
    assert rc is True
    assert captured.get("method") == "rule.seed_bootstrap"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("dry_run") is True
    out = capsys.readouterr().out
    assert "Dry-Run" in out and "created: 5" in out
    assert "AR-bootstrap-i18n" in out


def test_cli058_seed_bootstrap_apply(monkeypatch, capsys):
    """--apply → dry_run=False，真实写入仍经 daemon 执行。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        result = _seed_result()
        result["dry_run"] = False
        return result

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"apply": True})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_seed_bootstrap(opts, proxy)
    assert rc is True
    assert captured["params"].get("dry_run") is False
    out = capsys.readouterr().out
    assert "Applied" in out
