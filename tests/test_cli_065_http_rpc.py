"""CLI-065 (A′ cli_command_projection) `cw symbol-history` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_065_http_rpc.py）：
  - success：db.get_symbol_commit_history / db.get_symbol_change_tasks 经
    RpcDBProxy._rpc_call → route_rpc（READ_ONLY），Python 仅编排输出
  - 参数契约：symbol_hash/limit 透传
  - 结构不变量：Rust 侧 handle_get_symbol_commit_history（MCP-026 已迁移）与
    handle_get_symbol_change_tasks（MCP-063 closed/applied）为唯一 authority；
    Python 未找到时输出 no records 不崩溃

Rust 侧由 MCP-026（T-1787321710602）/ MCP-063（T-1787321713116）核验。
"""

import callwarden.cli.main as main_mod


def test_cli065_symbol_history_routes_to_daemon(monkeypatch, capsys):
    """success：symbol-history 经 route_rpc 调用两个 RPC。"""
    captured = {}
    calls = []

    def _fake_route(method, params, op_class):
        calls.append((method, params, op_class))
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        if method == "get_symbol_commit_history":
            return [
                {"commit_hash": "abc123def456", "change_type": "modified",
                 "author": "alice", "message": "fix foo", "timestamp": 1787497000},
            ]
        return [{"task_id": "T-1", "change_type": "modified",
                 "source_commit_hash": "abc123", "qualified_name": "pkg.foo"}]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_symbol_history(["a1b2c3d4e5f6"], proxy)
    assert rc is True
    methods = [c[0] for c in calls]
    assert "get_symbol_commit_history" in methods
    assert "get_symbol_change_tasks" in methods
    assert captured["params"].get("symbol_hash") == "a1b2c3d4e5f6"
    assert captured["params"].get("limit") == 20
    out = capsys.readouterr().out
    assert "abc123def456" in out and "alice" in out
    assert "Related Tasks" in out and "T-1" in out


def test_cli065_symbol_history_empty(monkeypatch, capsys):
    """空提交：输出 no records，不崩溃。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return []

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_symbol_history(["a1b2c3d4e5f6"], proxy)
    assert rc is True
    out = capsys.readouterr().out
    assert "no Git change records" in out
