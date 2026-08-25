"""CLI-012 (A′ task_projection) `cw audit` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_012_http_rpc.py）：
  - success：verify/keys/rotate-key 经 db 方法 → RpcDBProxy._rpc_call →
    route_rpc（audit_verify_chain / list_audit_signing_keys /
    admin.audit_rotate_key），Python 仅编排输出
  - 结构不变量：rotate-key 为 PROTECTED_MUTATION（写操作无本地 SQLite fallback）

Rust 侧 cli_handle_audit_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli012_audit_verify_routes_to_daemon(monkeypatch, capsys):
    """success：verify 经 route_rpc 调用 audit_verify_chain。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"total_count": 3, "verified_count": 3, "broken_count": 0,
                "security_level": "signed", "broken_records": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_audit(["verify", "--table", "tasks", "--limit", "500"], proxy)
    assert rc is True
    assert captured.get("method") == "audit_verify_chain"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("table_name") == "tasks"
    assert captured["params"].get("limit") == 500
    out = capsys.readouterr().out
    assert "total=3" in out or "3" in out


def test_cli012_audit_keys_routes_to_daemon(monkeypatch, capsys):
    """success：keys 经 route_rpc 调用 list_audit_signing_keys。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return [{"key_id": "key-2026-07", "rotated_at": 1.0, "is_active": True}]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_audit(["keys"], proxy)
    assert rc is True
    assert captured.get("method") == "list_audit_signing_keys"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "key-2026-07" in out


def test_cli012_audit_rotate_key_routes_to_daemon(monkeypatch, capsys):
    """success：rotate-key 经 route_rpc 调用 admin.audit_rotate_key（PROTECTED_MUTATION）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"key_id": "key-2026-08", "rotated_at": 2.0,
                "previous_key_id": "key-2026-07"}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_audit(
        ["rotate-key", "--key-id", "key-2026-08", "--secret", "s3cr3t"], proxy)
    assert rc is True
    assert captured.get("method") == "admin.audit_rotate_key"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("new_key_id") == "key-2026-08"
    assert captured["params"].get("new_key_secret") == "s3cr3t"
    out = capsys.readouterr().out
    assert "key-2026-08" in out
