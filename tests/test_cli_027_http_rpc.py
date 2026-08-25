"""CLI-027 (A′ cli_command_projection) `cw dashboard` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_027_http_rpc.py）：
  - success：dashboard 经 db.get_project_dashboard → route_rpc
    （get_project_dashboard，READ_ONLY），Python 仅编排输出
  - --risks 时附加 get_project_risks 调用

Rust 侧 cli_handle_dashboard_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli027_dashboard_routes_to_daemon(monkeypatch, capsys):
    """success：dashboard 经 route_rpc 调用 get_project_dashboard。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "overview": {"project_type": "python", "file_count": 10},
            "code_scale": {"total_lines": 5000},
            "code_quality": {"comment_coverage": 60.0},
            "call_graph": {"total_symbols": 100},
            "task_risk": {"open_findings": 2},
            "audit": {"ok": True},
            "evolution": {"commits": 5},
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_dashboard(["--top", "8"], proxy)
    assert rc is True
    assert captured.get("method") == "get_project_dashboard"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("with_cycles") is False
    assert captured["params"].get("quick") is True
    assert captured["params"].get("top_n") == 8
    out = capsys.readouterr().out
    assert "项目驾驶舱" in out


def test_cli027_dashboard_risks_extra_call(monkeypatch, capsys):
    """success：--json --risks 附加 get_project_risks 调用。"""
    calls = []

    def _fake_route(method, params, op_class):
        calls.append(method)
        if method == "get_project_dashboard":
            return {"overview": {"project_type": "python"}}
        return [{"risk": "high complexity", "severity": "high"}]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_dashboard(["--json", "--risks"], proxy)
    assert rc is True
    assert calls[0] == "get_project_dashboard"
    assert "get_project_risks" in calls
    out = capsys.readouterr().out
    assert "high complexity" in out
