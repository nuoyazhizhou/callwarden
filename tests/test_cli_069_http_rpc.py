"""CLI-069（T-1787322798858-ace77210）：cw uncommented → query.uncommented_symbols thin client 契约。

验证 _handle_uncommented：
1. success：db.get_uncommented_symbols 经 route_rpc 调 query.uncommented_symbols READ_ONLY，
   kind/module_filter 透传；输出符号列表（qualified_name/file_path/start_line/signature）。
2. 空结果：0 条输出不崩溃。
3. limit 截断：RPC 不传 limit（仅输出端截断）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import callwarden.cli.main as main_mod  # noqa: E402


def test_cli069_uncommented_routes_to_daemon(monkeypatch, capsys):
    """success：uncommented 经 route_rpc 调用 query.uncommented_symbols。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"depth": 0, "qualified_name": "pkg.foo", "file_path": "a.py",
             "start_line": 10, "signature": "def foo()"},
            {"depth": 1, "qualified_name": "pkg.Bar.method", "file_path": "b.py",
             "start_line": 20},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_uncommented(["fn", "--module", "pkg"], proxy)
    assert rc is True
    assert captured.get("method") == "query.uncommented_symbols"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("kind") == "fn"
    assert captured["params"].get("module_filter") == "pkg"
    # limit 仅输出端截断，RPC 不传
    assert "limit" not in captured["params"]
    out = capsys.readouterr().out
    assert "pkg.foo" in out and "a.py:10" in out
    assert "pkg.Bar.method" in out and "b.py:20" in out


def test_cli069_uncommented_empty(monkeypatch, capsys):
    """空结果：0 条输出不崩溃。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return []

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_uncommented([], proxy)
    assert rc is True
    assert captured.get("method") == "query.uncommented_symbols"
    out = capsys.readouterr().out
    assert "0" in out


def test_cli069_uncommented_limit_truncates(monkeypatch, capsys):
    """limit 截断：只输出前 limit 条，超过时提示 more。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        return [
            {"depth": 0, "qualified_name": f"pkg.f{i}", "file_path": "a.py",
             "start_line": i}
            for i in range(5)
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_uncommented(["fn", "--limit", "2"], proxy)
    assert rc is True
    out = capsys.readouterr().out
    assert "pkg.f0" in out and "pkg.f1" in out
    assert "pkg.f2" not in out
    assert "3" in out  # more 提示剩余 3 条
