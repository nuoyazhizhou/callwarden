"""CLI-042 (A′ cli_command_projection) `cw issues` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_042_http_rpc.py）：
  - success：db.get_symbol_issues 经 RpcDBProxy._rpc_call → route_rpc
    （query.issues，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.issues dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli042_issues_routes_to_daemon(monkeypatch, capsys):
    """success：issues 经 route_rpc 调用 query.issues。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"source": "semgrep", "severity": "warning", "rule_id": "R1",
             "message": "bad", "line": 3},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_issues(["m::f", "--include-info"], proxy)
    assert rc is True
    assert captured.get("method") == "query.issues"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("qualified_name") == "m::f"
    assert captured["params"].get("include_info") is True
    out = capsys.readouterr().out
    assert "R1" in out
