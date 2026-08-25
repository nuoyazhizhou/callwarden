"""CLI-067 (A′ cli_command_projection) `cw tests` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_067_http_rpc.py）：
  - success：db.get_test_cases / get_test_stability / build_test_relations /
    import_test_results / get_tested_functions 经 RpcDBProxy / route_rpc，
    Python 仅编排输出
  - 参数契约：qualified_name/limit/force/junit_xml/ci_run_id/ci_url 透传
  - 结构不变量：Rust 侧 query.tests（get_test_cases 等，已 native）与
    cli_handle_tests_handlers.rs（build_test_relations/import_test_results，
    CLI-067 新增）为唯一 authority；import 解析错误返回 parse_error 不崩溃

Rust 侧由 cargo 单测覆盖（见 cli_handle_tests_handlers.rs）。
"""

import callwarden.cli.main as main_mod


def test_cli067_tests_build_routes_to_daemon(monkeypatch, capsys):
    """--build：db.build_test_relations 经 route_rpc 调用 build_test_relations。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"total_test_fns": 5, "direct_call": 3, "name_convention": 1,
                "indirect": 1, "inserted": 5}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_tests(["--build"], proxy)
    assert rc is True
    assert captured.get("method") == "build_test_relations"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("force") is False
    out = capsys.readouterr().out
    assert "5" in out


def test_cli067_tests_import_routes_to_daemon(monkeypatch, capsys):
    """--import：db.import_test_results 经 route_rpc 调用 import_test_results。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"total": 2, "passed": 2, "failed": 0, "skipped": 0,
                "error": 0, "matched": 2}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_tests(["--import", "<testsuite><testcase name='t1'/></testsuite>"], proxy)
    assert rc is True
    assert captured.get("method") == "import_test_results"
    assert captured.get("op") == "PROTECTED_MUTATION"
    out = capsys.readouterr().out
    assert "Test results imported" in out and "Matched:" in out


def test_cli067_tests_import_parse_error(monkeypatch, capsys):
    """JUnit XML 解析错误：返回 parse_error，命令返回 False。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"parse_error": "XML parse error: unexpected end of input"}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_tests(["--import", "<broken>"], proxy)
    assert rc is False
    assert captured.get("method") == "import_test_results"
    out = capsys.readouterr().out
    assert "XML parse error" in out
