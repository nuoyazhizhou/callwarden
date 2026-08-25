"""CLI-020 (A′ cli_command_projection) `cw churn` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_020_http_rpc.py）：
  - success：db.churn_analysis 经 RpcDBProxy._rpc_call → route_rpc
    （query.churn_analysis，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.churn_analysis dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli020_churn_routes_to_daemon(monkeypatch, capsys):
    """success：churn 经 route_rpc 调用 query.churn_analysis。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "changed_files": 4,
            "total_lines_current": 1000,
            "total_churned_lines": 200,
            "churn_rate": 0.2,
            "top_churned_files": [
                {"rel_path": "a.py", "change_count": 3, "churned_lines": 120},
            ],
            "trend": [
                {"date": "2026-08-01", "churned_lines": 60},
                {"date": "2026-08-08", "churned_lines": 140},
            ],
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_churn(["--module", "core", "--window", "30d"], proxy)
    assert rc is True
    assert captured.get("method") == "query.churn_analysis"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("module_filter") == "core"
    assert captured["params"].get("time_window") == "30d"
    out = capsys.readouterr().out
    assert "30d" in out
    assert "2026-08-08" in out, "trend 输出（回归验证循环变量遮蔽缺陷修复）"
