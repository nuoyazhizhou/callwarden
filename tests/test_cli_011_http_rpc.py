"""CLI-011 (A′ assignment_projection) `cw assignment` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_011_http_rpc.py）：
  - success：create/revoke 经 db.create_assignment / db.revoke_assignment →
    RpcDBProxy._rpc_call → route_rpc（admin.assignment_create/revoke，
    GOVERNANCE_WRITE），Python 仅编排输出
  - 结构不变量：daemon RPC 返回结构化 dict（{"ok": true, ...}），CLI 经
    _unwrap_bool_result 归一为 (ok, result)，不再对 dict 元组解包
  - show 经 get_assignment → route_rpc(assignment_show, READ_ONLY)

Rust 侧 cli_handle_assignment_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli011_assignment_create_routes_to_daemon(monkeypatch, capsys):
    """success：create 经 route_rpc 调用 admin.assignment_create（daemon dict 契约）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"ok": True, "assignment_id": 5, "task_id": "T-1",
                "role": "reviewer", "agent_id": "ag", "session_id": "ss",
                "model_id": "md", "created_at": 1.0}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_assignment(
        ["create", "T-1", "--role", "reviewer",
         "--agent-id", "ag", "--session-id", "ss", "--model-id", "md"],
        proxy)
    assert rc is True
    assert captured.get("method") == "admin.assignment_create"
    assert captured.get("op") == "GOVERNANCE_WRITE"
    assert captured["params"].get("task_id") == "T-1"
    out = capsys.readouterr().out
    assert "Assignment created" in out
    assert "5" in out


def test_cli011_assignment_revoke_routes_to_daemon(monkeypatch, capsys):
    """success：revoke 经 route_rpc 调用 admin.assignment_revoke。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return {"ok": True, "task_id": "T-1", "role": "reviewer"}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_assignment(["revoke", "ASG-x"], proxy)
    assert rc is True
    assert captured.get("method") == "admin.assignment_revoke"
    assert captured.get("op") == "GOVERNANCE_WRITE"
    out = capsys.readouterr().out
    assert "Assignment revoked" in out


def test_cli011_assignment_show_routes_to_daemon(monkeypatch, capsys):
    """success：show 经 route_rpc 调用 assignment_show（READ_ONLY）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return {"assignment_id": "ASG-1", "task_id": "T-1", "role": "reviewer",
                "agent_id": "ag", "session_id": "ss", "model_id": "md",
                "created_at": 1.0}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_assignment(["show", "T-1", "--role", "reviewer"], proxy)
    assert rc is True
    assert captured.get("method") == "assignment_show"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "Active Assignment" in out


def test_cli011_assignment_create_identity_incomplete(monkeypatch, capsys):
    """身份不完整 -> E_ASSIGNMENT_INCOMPLETE 拒绝，不触达 daemon。"""
    hit = {"called": False}

    def _fake_route(method, params, op_class):
        hit["called"] = True
        return {}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_assignment(
        ["create", "T-1", "--role", "reviewer"], proxy)
    assert rc is True
    assert not hit["called"], "身份不完整时必须 fail-closed，不触达 assignment_create"
    out = capsys.readouterr().out
    assert "E_ASSIGNMENT_INCOMPLETE" in out
