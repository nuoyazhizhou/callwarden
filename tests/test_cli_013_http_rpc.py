"""CLI-013 (A′ cli_command_projection) `cw bootstrap status` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_013_http_rpc.py）：
  - success：db.bootstrap_status 经 RpcDBProxy._rpc_call → route_rpc
    （bootstrap_status，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 handle_bootstrap_status（MCP-066 已迁移）为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 task_read_handlers.rs
（MCP-066 已迁移）由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli013_bootstrap_status_routes_to_daemon(monkeypatch, capsys):
    """success：bootstrap status 经 route_rpc 调用 bootstrap_status。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "db_stale": False,
            "current_head": "abc123def456",
            "active_rules_count": 5,
            "pending_candidates_count": 2,
            "open_findings_count": 1,
            "blocking_findings_count": 0,
            "audit_verify": {"ok": True},
            "recommended": [],
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_bootstrap(["status"], proxy)
    assert rc is True
    assert captured.get("method") == "bootstrap_status"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "active_rules_count" not in out  # 非 JSON：人类可读摘要
