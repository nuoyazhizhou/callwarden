"""CLI-071（T-1787322798971-b3a9f30c）：cw who → who_to_ask thin client 契约。

验证 _handle_who：
1. success：db.who_to_ask 经 route_rpc 调 who_to_ask READ_ONLY，file 透传；
   输出 owner/source/confidence/last_commit 字段。
2. 未找到：who not found + hint 不崩溃。
3. 缺省字段稳定：无 last_commit_* 时不 KeyError。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import callwarden.cli.main as main_mod  # noqa: E402


def test_cli071_who_routes_to_daemon(monkeypatch, capsys):
    """success：who 经 route_rpc 调用 who_to_ask。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "file_path": "a.py", "owner": "alice", "source": "codeowners",
            "confidence": 0.95,
            "last_commit_author": "bob", "last_commit_time": 1700000000,
            "last_commit_hash": "a1b2c3d4e5f6",
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_who(["a.py"], proxy)
    assert rc is True
    assert captured.get("method") == "who_to_ask"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("file_path") == "a.py"
    out = capsys.readouterr().out
    assert "alice" in out and "codeowners" in out
    assert "bob" in out and "a1b2c3d4" in out


def test_cli071_who_not_found(monkeypatch, capsys):
    """未找到：not found + hint 输出不崩溃。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        return None

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_who(["nope.py"], proxy)
    assert rc is True
    out = capsys.readouterr().out
    assert "nope.py" in out


def test_cli071_who_missing_optional_fields_stable(monkeypatch, capsys):
    """缺省字段稳定：无 last_commit_* 字段时不 KeyError。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        return {"file_path": "b.py", "owner": "carol",
                "source": "git_blame", "confidence": 0.8}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_who(["b.py"], proxy)
    assert rc is True
    out = capsys.readouterr().out
    assert "carol" in out and "git_blame" in out
