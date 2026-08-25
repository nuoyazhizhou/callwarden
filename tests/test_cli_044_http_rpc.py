"""CLI-044 (A′ assignment_projection) `cw lease` 读路径 HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_044_http_rpc.py）：
  - success：status 经 db.get_lease_status → route_rpc(lease.status, READ_ONLY)；
    list 经 db.list_lease_events → route_rpc(lease.list_events, READ_ONLY)
  - 结构不变量：写路径（acquire/renew/release）经 _route_lease_write →
    HttpDaemonRpcClient（CLI-094 已锁定），读路径经 route_rpc 走同一 daemon

Rust 侧 cli_handle_lease_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli044_lease_status_routes_to_daemon(monkeypatch, capsys):
    """success：lease status 经 route_rpc 调用 lease.status。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"task_id": "T-1", "role": "reviewer", "status": "active"}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_lease(["status", "T-1", "--role", "reviewer"], proxy)
    assert rc is True
    assert captured.get("method") == "lease.status"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("task_id") == "T-1"
    out = capsys.readouterr().out
    assert "active" in out


def test_cli044_lease_list_events_routes_to_daemon(monkeypatch, capsys):
    """success：lease list 经 route_rpc 调用 lease.list_events。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"event_at": 1.0, "event_type": "acquire", "lease_id": "L-1",
             "fencing_counter": 1, "role": "reviewer"},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_lease(["list", "--task-id", "T-1"], proxy)
    assert rc is True
    assert captured.get("method") == "lease.list_events"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "L-1" in out
