"""CLI-034 (A′ cli_command_projection) `cw function-issues` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_034_http_rpc.py）：
  - success：db.get_function_issues 经 RpcDBProxy._METHOD_MAP 映射 →
    route_rpc(find_issues, READ_ONLY)，Python 仅编排输出
    （A 类修复 2026-09-30：daemon 原生 handler 为 find_issues，复刻
    analyzers/issues.get_function_issues；参数 issue_filter→issue_type，
    见 cli/main.py _METHOD_MAP / _PARAM_RENAME 的 FIX 注释）
  - 结构不变量：参数名转发前按 _PARAM_RENAME 重命名，不落本地 SQLite

Rust 侧 cli_handle_function_issues_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli034_function_issues_routes_to_daemon(monkeypatch, capsys):
    """success：function-issues 经 route_rpc 调用 find_issues（daemon 原生方法名）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"qualified_name": "m::f", "module_path": "m", "issue_count": 2,
             "issues": [
                 {"severity": "warn", "label": "long function", "count": 1,
                  "description": "too long"},
                 {"severity": "danger", "label": "deep nesting", "count": 1,
                  "description": "too deep"},
             ]},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_function_issues(
        ["m::f", "--type", "warn", "--module", "m", "--limit", "20"], proxy)
    assert rc is True
    assert captured.get("method") == "find_issues"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("qualified_name") == "m::f"
    # 参数名经 _PARAM_RENAME：CLI 侧 issue_filter → daemon 契约 issue_type
    assert captured["params"].get("issue_type") == "warn"
    assert captured["params"].get("module_filter") == "m"
    assert captured["params"].get("limit") == 20
    out = capsys.readouterr().out
    assert "m::f" in out
