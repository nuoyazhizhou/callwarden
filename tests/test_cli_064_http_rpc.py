"""CLI-064 (A′ cli_command_projection) `cw symbol` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_064_http_rpc.py）：
  - success：db.get_symbol 经 RpcDBProxy._rpc_call → route_rpc（query.symbol，
    READ_ONLY），Python 仅编排输出
  - 参数契约：qualified_name 透传
  - 结构不变量：Rust 侧 query.symbol（M2.2 参数校验 + SnapshotDaemonState
    handle_query_symbol）为唯一 authority，返回完整字段（qualified_name/kind/
    depth/file_path/start_line/end_line/signature/has_comment/comment_content/
    calls_out/called_by）；Python 未找到时输出 not found 不崩溃

Rust 侧 query.symbol 由 M2.2 迁移核验。
"""

import callwarden.cli.main as main_mod


def _symbol_sample():
    return {
        "qualified_name": "pkg.foo", "kind": "function", "depth": 2,
        "file_path": "a.py", "start_line": 10, "end_line": 20,
        "signature": "def foo(x: int) -> str", "has_comment": True,
        "comment_content": "Docstring line 1",
        "calls_out": [{"target_name": "pkg.bar", "call_line": 12}],
        "called_by": [{"caller_name": "pkg.main", "call_line": 5}],
    }


def test_cli064_symbol_routes_to_daemon(monkeypatch, capsys):
    """success：symbol 经 route_rpc 调用 query.symbol，输出详情。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return _symbol_sample()

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_symbol(["pkg.foo"], proxy)
    assert rc is True
    assert captured.get("method") == "query.symbol"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("qualified_name") == "pkg.foo"
    out = capsys.readouterr().out
    assert "pkg.foo" in out and "a.py:10-20" in out
    assert "→ pkg.bar" in out  # calls_out


def test_cli064_symbol_not_found(monkeypatch, capsys):
    """未找到：输出 not found + search hint，不崩溃。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return None

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_symbol(["pkg.nope"], proxy)
    assert rc is True
    assert captured["params"].get("qualified_name") == "pkg.nope"
    out = capsys.readouterr().out
    assert "not found" in out.lower() or "未找到" in out
