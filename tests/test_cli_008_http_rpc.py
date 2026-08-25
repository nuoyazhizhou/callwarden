"""CLI-008 (A′ graph_snapshot) `cw dependency explain` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_008_http_rpc.py）：
  - success：db.validate_revision_dependencies 经 RpcDBProxy._rpc_call → route_rpc
    （validate_revision_dependencies，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 handle_validate_revision_dependencies（MCP-008 已迁移）
    为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dependency_query_handlers.rs
（MCP-008 已迁移）由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def _opts(use_json):
    return type("O", (), {"contract_id": "C-1", "revision": 3, "json": use_json})()


def test_cli008_dependency_explain_valid(monkeypatch, capsys):
    """success：valid 结果经 route_rpc 编排输出。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"valid": True, "errors": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._dependency_explain(proxy, 7, _opts(False), False)
    assert rc is True
    assert captured.get("method") == "validate_revision_dependencies"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("workspace_id") == 7
    assert captured["params"].get("contract_id") == "C-1"
    assert captured["params"].get("revision") == 3
    out = capsys.readouterr().out
    assert "C-1@3" in out


def test_cli008_dependency_explain_invalid(monkeypatch, capsys):
    """success：invalid 结果带 errors 输出。"""
    def _fake_route(method, params, op_class):
        return {"valid": False, "errors": ["依赖 A 缺失"]}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._dependency_explain(proxy, 7, _opts(False), False)
    assert rc is True
    out = capsys.readouterr().out
    assert "依赖 A 缺失" in out
