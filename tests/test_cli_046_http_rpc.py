"""CLI-046 (A′ cli_command_projection) `cw metrics` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_046_http_rpc.py）：
  - success：db.get_code_metrics_summary 经 RpcDBProxy._rpc_call → route_rpc
    （query.metrics_summary，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.metrics_summary dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli046_metrics_routes_to_daemon(monkeypatch, capsys):
    """success：metrics 经 route_rpc 调用 query.metrics_summary。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return {"file_count": 10, "function_count": 100,
                "total_lines": 5000, "total_calls": 300,
                "avg_complexity": 4.2, "max_complexity": 15,
                "complexity_distribution": {"low": 80, "high": 20},
                "comment_coverage": 61.0}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_metrics([], proxy)
    assert rc is True
    assert captured.get("method") == "query.metrics_summary"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "100" in out
