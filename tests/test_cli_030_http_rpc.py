"""CLI-030 (A′ cli_command_projection) `cw evolution` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_030_http_rpc.py）：
  - success：evolution 经 db.function_change_frequency → route_rpc
    （function_change_frequency，READ_ONLY），Python 仅编排输出
  - --defects 模式经 db.get_defect_correlation_by_qn → route_rpc

Rust 侧 cli_handle_evolution_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli030_evolution_routes_to_daemon(monkeypatch, capsys):
    """success：evolution 经 route_rpc 调用 function_change_frequency。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"qualified_name": "m::f", "change_count": 5,
                "window": "90d", "commits": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_evolution(["m::f", "--window", "30d"], proxy)
    assert rc is True
    assert captured.get("method") == "function_change_frequency"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("qualified_name") == "m::f"
    assert captured["params"].get("time_window") == "30d"
    out = capsys.readouterr().out
    assert "m::f" in out


def test_cli030_evolution_defects_mode(monkeypatch, capsys):
    """success：--defects 经 route_rpc 调用 get_defect_correlation_by_qn。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"qualified_name": "m::f", "change_count": 3, "defect_count": 1,
                "defect_rate": 0.33, "defect_types": ["null"], "recent_defects": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_evolution(["m::f", "--defects"], proxy)
    assert rc is True
    assert captured.get("method") == "query.get_defect_correlation"
    assert captured["params"].get("qualified_name") == "m::f"
    out = capsys.readouterr().out
    assert "Defect Correlation" in out
