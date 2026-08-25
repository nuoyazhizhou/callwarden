"""CLI-055 (A′ cli_command_projection) `cw rule-extract` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_055_http_rpc.py）：
  - success：db.extract_rule_candidates_from_quality_findings 经 RpcDBProxy
    ._rpc_call → route_rpc（rule.extract_candidates，PROTECTED_MUTATION），
    Python 仅编排输出
  - 参数契约：task_id/min_occurrences 原样透传，min_occurrences 默认 2
  - 结构不变量：Rust 侧 handle_extract_rule_candidates（CLI-055 修复）返回
    新建候选 ID 列表（list[str]），Python 对其 len()/迭代直接可用；修复旧
    实现以 semgrep_findings 为数据源并返回 count dict 的语义错误

Rust 侧由 cargo 单测覆盖（见 edit_handlers.rs::tests::extract_candidates_*）。
"""

import callwarden.cli.main as main_mod


def test_cli055_rule_extract_routes_to_daemon(monkeypatch, capsys):
    """success：rule-extract 经 route_rpc 调用 rule.extract_candidates。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return ["ARC-1783253838000-a1b25c6d", "ARC-1783253838001-b2c3d4e5"]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"task_id": "T-1", "min_occurrences": 2})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_extract(opts, proxy)
    assert rc is True
    assert captured.get("method") == "rule.extract_candidates"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("task_id") == "T-1"
    assert captured["params"].get("min_occurrences") == 2
    out = capsys.readouterr().out
    assert "ARC-1783253838000-a1b25c6d" in out
    assert "ARC-1783253838001-b2c3d4e5" in out


def test_cli055_rule_extract_empty(monkeypatch, capsys):
    """空结果：输出 (no repeated findings above threshold)，不崩溃。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return []

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    opts = type("O", (), {"task_id": "", "min_occurrences": 2})()
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_rule_extract(opts, proxy)
    assert rc is True
    assert captured.get("method") == "rule.extract_candidates"
    assert captured["params"].get("task_id") == ""
    assert captured["params"].get("min_occurrences") == 2
    out = capsys.readouterr().out
    assert "no repeated findings above threshold" in out
