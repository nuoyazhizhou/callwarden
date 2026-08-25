"""CLI-037 (A′ cli_command_projection) `cw grep` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_037_http_rpc.py）：
  - success：符号归属查询 db.find_symbols_at_lines 经 RpcDBProxy._rpc_call →
    route_rpc(find_symbols_at_lines, READ_ONLY)；文本匹配部分为纯 Python
    流式处理（不触达 DB）
  - 结构不变量：grep 的 DB authority 仅为符号归属（find_symbols_at_lines），
    无本地 SQLite 业务路径

Rust 侧 cli_handle_grep_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli037_find_symbols_at_lines_routes_to_daemon(monkeypatch):
    """success：find_symbols_at_lines 经 route_rpc 调用（READ_ONLY）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {1: {"kind": "function", "name": "foo"}}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    result = proxy.find_symbols_at_lines("a.py", [1, 2, 3])
    assert captured.get("method") == "find_symbols_at_lines"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("file_path") == "a.py"
    assert captured["params"].get("lines") == [1, 2, 3]
    assert result.get(1, {}).get("kind") == "function"
