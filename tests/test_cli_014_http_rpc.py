"""CLI-014 (A′ cli_command_projection) `cw brief` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_014_http_rpc.py）：
  - success：db.project_brief 经 RpcDBProxy._rpc_call → route_rpc
    （project_brief，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 handle_project_brief（MCP-030 已迁移）为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 summary_query_handlers.rs
（MCP-030 已迁移）由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli014_brief_routes_to_daemon(monkeypatch, capsys):
    """success：brief 经 route_rpc 调用 project_brief。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "project_type": "python",
            "file_count": 12,
            "function_count": 88,
            "total_lines": 5000,
            "health_score": 85.0,
            "health_level": "good",
            "avg_complexity": 4.2,
            "comment_coverage": 61.0,
            "modules": [
                {"module": "core", "function_count": 40},
                {"module": "api", "function_count": 48},
            ],
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_brief([], proxy)
    assert rc is True
    assert captured.get("method") == "project_brief"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "python" in out
    assert "core" in out
    assert "api" in out
