"""CLI-073（T-1787322799077-b9fe69b8）：cw identity-revoke → workspace.status thin client 契约。

验证：
1. RpcDBProxy.get_active_workspace 经 route_rpc 调 workspace.status READ_ONLY（"必须移除
   的 Python 业务路径" db.get_active_workspace 的迁移验证）。
2. 命令层 fail-closed：revocation-mode 缺失 → E_REVOCATION_MODE_REQUIRED，不调用写。
3. 命令层 gate fail-closed：daemon 模式下 → E_TASK_LOOP_CAPABILITY_DISABLED（authority
   写必须经 CapabilityMutationGate，本命令不直写 DB）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import callwarden.cli.main as main_mod  # noqa: E402


def test_cli073_get_active_workspace_routes_to_daemon(monkeypatch):
    """get_active_workspace：route_rpc workspace.status READ_ONLY。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"workspace_id": 3, "name": "w1", "client_view_root": "C:/repo1",
                "status": "active"}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    ws = proxy.get_active_workspace()
    assert captured.get("method") == "workspace.status"
    assert captured.get("op") == "READ_ONLY"
    assert ws is not None
    assert ws["id"] == 3 and ws["root_path"] == "C:/repo1"


def test_cli073_revoke_missing_mode_fails_closed(monkeypatch, capsys):
    """revocation-mode 缺失：E_REVOCATION_MODE_REQUIRED，不调用任何 db 写。"""
    called = []

    class _Opts:
        action = "revoke"
        revocation_mode = None
        reason = ""
        agent_id = session_id = model_id = role = ""
        json = False

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    monkeypatch.setattr(proxy, "get_active_workspace", lambda: called.append("get"))
    rc = main_mod._identity_revoke(proxy, _Opts(), False)
    assert rc is True
    out = capsys.readouterr().out
    assert "E_REVOCATION_MODE_REQUIRED" in out
    assert called == []  # 未触发任何 db 调用


def test_cli073_revoke_daemon_gate_fails_closed(monkeypatch, capsys):
    """daemon 模式：authority 写经 CapabilityMutationGate fail-closed。"""
    class _Opts:
        action = "revoke"
        revocation_mode = "compromised"
        reason = "leak"
        agent_id = session_id = model_id = role = ""
        json = False

    monkeypatch.setattr(main_mod, "get_daemon_mode", lambda: "daemon")
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    monkeypatch.setattr(proxy, "get_active_workspace", lambda: None)
    rc = main_mod._identity_revoke(proxy, _Opts(), False)
    assert rc is True
    out = capsys.readouterr().out
    assert "E_TASK_LOOP_CAPABILITY_DISABLED" in out
