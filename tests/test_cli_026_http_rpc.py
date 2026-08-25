"""CLI-026 (A′ cli_command_projection) `cw coverage` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_026_http_rpc.py）：
  - success：fn 经 db.get_coverage_for_symbol → route_rpc(query.coverage_for_symbol,
    READ_ONLY)；uncovered 经 db.find_uncovered_functions → route_rpc
    (find_uncovered_functions, READ_ONLY)；import 经 db.import_lcov/cobertura →
    route_rpc(import_lcov/import_cobertura, PROTECTED_MUTATION)

Rust 侧 cli_handle_coverage_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli026_coverage_fn_routes_to_daemon(monkeypatch, capsys):
    """success：coverage fn 经 route_rpc 调用 query.coverage_for_symbol。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"qualified_name": "m::f", "file_path": "a.py",
                "start_line": 1, "end_line": 10, "total_lines": 10,
                "tracked_lines": 8, "covered_lines": 6, "coverage_pct": 75.0,
                "uncovered_lines": [3]}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_coverage(["fn", "m::f"], proxy)
    assert rc is True
    assert captured.get("method") == "query.coverage_for_symbol"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("qualified_name") == "m::f"
    out = capsys.readouterr().out
    assert "m::f" in out


def test_cli026_coverage_uncovered_routes_to_daemon(monkeypatch, capsys):
    """success：uncovered 经 route_rpc 调用 find_uncovered_functions。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return [
            {"coverage_pct": 0.0, "qualified_name": "m::dead",
             "file_path": "a.py", "start_line": 1, "end_line": 5,
             "covered_lines": 0, "tracked_lines": 5},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_coverage(["uncovered"], proxy)
    assert rc is True
    assert captured.get("method") == "find_uncovered_functions"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "m::dead" in out


def test_cli026_coverage_import_lcov_routes_to_daemon(monkeypatch, capsys):
    """success：import lcov 经 route_rpc 调用 import_lcov（PROTECTED_MUTATION）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"files_total": 2, "files_matched": 1,
                "lines_imported": 100, "symbols_matched": 5}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_coverage(["import", "cov.info"], proxy)
    assert rc is True
    assert captured.get("method") == "import_lcov"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("file_path") == "cov.info"
    out = capsys.readouterr().out
    assert "100" in out
