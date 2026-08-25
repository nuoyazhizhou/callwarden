"""CLI-060 (A′ cli_command_projection) `cw search` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_060_http_rpc.py）：
  - success：db.search_symbols 经 RpcDBProxy._rpc_call → route_rpc
    （query.search，READ_ONLY），Python 仅编排输出
  - 参数契约：query/kind/limit 原样透传（limit 默认 50，kind 可空）
  - 结构不变量：Rust 侧 query.search（handle_query_search，W2 迁移）为唯一
    authority；CLI-02（T-1787321708568）已处理 has_comment/signature/file_path
    兼容字段，本卡验证完整 thin-client 契约（success/空结果/缺省字段稳定）

Rust 侧 query.search 由 W2 迁移与 CLI-02 核验；本卡聚焦 Python 契约。
"""

import callwarden.cli.main as main_mod


def test_cli060_search_routes_to_daemon(monkeypatch, capsys):
    """success：search 经 route_rpc 调用 query.search。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"depth": 1, "has_comment": True, "signature": "def foo()",
             "file_path": "a.py", "kind": "function", "qualified_name": "pkg.foo",
             "start_line": 10},
            {"depth": -1, "kind": "class", "qualified_name": "pkg.Bar",
             "start_line": 20},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_search(["foo"], proxy)
    assert rc is True
    assert captured.get("method") == "query.search"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("query") == "foo"
    assert captured["params"].get("kind") is None
    assert captured["params"].get("limit") == 50
    out = capsys.readouterr().out
    assert "pkg.foo" in out and "✓" in out  # has_comment ✓


def test_cli060_search_empty(monkeypatch, capsys):
    """空结果：0 条输出不崩溃。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return []

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_search(["zzz", "--kind", "function"], proxy)
    assert rc is True
    assert captured["params"].get("kind") == "function"
    out = capsys.readouterr().out
    assert "0" in out


def test_cli060_search_missing_optional_fields_stable(monkeypatch, capsys):
    """缺省字段稳定：无 has_comment/signature/file_path 时输出不 KeyError。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [{"depth": 0, "kind": "function", "qualified_name": "pkg.f",
                 "start_line": 1}]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_search(["f"], proxy)
    assert rc is True
    out = capsys.readouterr().out
    assert "pkg.f" in out
