"""CLI-050 (A′ cli_command_projection) `cw review` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_050_http_rpc.py）：
  - success：db.review_readiness_report 经 RpcDBProxy._rpc_call → route_rpc
    （review_readiness，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 handle_review_readiness_report 为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧
cli_handle_review_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli050_review_routes_to_daemon(monkeypatch, capsys):
    """success：review 经 route_rpc 调用 review_readiness。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"impact_scope": "medium", "total_impacted": 5,
                "recommendations": ["review carefully"]}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_review(["abc123"], proxy)
    assert rc is True
    assert captured.get("method") == "review_readiness"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("symbol_hash") == "abc123"
    out = capsys.readouterr().out
    assert "5" in out
