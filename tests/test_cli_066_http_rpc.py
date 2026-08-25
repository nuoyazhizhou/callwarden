"""CLI-066 (A′ cli_command_projection) `cw test-impact` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_066_http_rpc.py）：
  - success：db.test_impact_selection 经 RpcDBProxy._rpc_call → route_rpc
    （test_impact_selection，READ_ONLY），Python 仅编排输出
  - 参数契约：qualified_name 透传
  - 结构不变量：Rust 侧 handle_test_impact_selection（MCP-032 已迁移）为唯一
    authority，返回测试列表（name/qualified_name/file_path/start_line）；
    Python 未找到时输出 no tests 不崩溃

Rust 侧由 MCP-032（T-1787321710977）核验。
"""

import callwarden.cli.main as main_mod


def test_cli066_test_impact_routes_to_daemon(monkeypatch, capsys):
    """success：test-impact 经 route_rpc 调用 test_impact_selection。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"name": "test_foo", "qualified_name": "tests.test_foo",
             "file_path": "tests/test_a.py", "start_line": 10},
            {"name": "test_bar", "qualified_name": "tests.test_bar",
             "file_path": "tests/test_b.py", "start_line": 20},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_test_impact(["pkg.foo"], proxy)
    assert rc is True
    assert captured.get("method") == "test_impact_selection"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("qualified_name") == "pkg.foo"
    out = capsys.readouterr().out
    assert "test_foo" in out and "tests/test_a.py" in out


def test_cli066_test_impact_empty(monkeypatch, capsys):
    """空结果：输出 no tests，不崩溃。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return []

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_test_impact(["pkg.nope"], proxy)
    assert rc is True
    assert captured["params"].get("qualified_name") == "pkg.nope"
    out = capsys.readouterr().out
    assert "no related tests found" in out
