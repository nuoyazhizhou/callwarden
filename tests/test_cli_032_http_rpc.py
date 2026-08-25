"""CLI-032 (A′ cli_command_projection) `cw fn-metrics` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_032_http_rpc.py）：
  - success：db.get_function_metrics 经 RpcDBProxy._rpc_call → route_rpc
    （query.function_metrics，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.function_metrics dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli032_fn_metrics_routes_to_daemon(monkeypatch, capsys):
    """success：fn-metrics 经 route_rpc 调用 query.function_metrics。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"qualified_name": "m::f", "kind": "function",
                "file_path": "a.py", "start_line": 1, "end_line": 20,
                "line_count": 20, "cyclomatic_complexity": 12,
                "risk_level": "high", "fan_in": 2, "fan_out": 3,
                "depth": 4, "module_path": "m", "signature": "def f()"}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_fn_metrics(["m::f"], proxy)
    assert rc is True
    assert captured.get("method") == "query.function_metrics"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("qualified_name") == "m::f"
    out = capsys.readouterr().out
    assert "m::f" in out
