"""CLI-019 (A′ cli_command_projection) `cw check-gate` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_019_http_rpc.py）：
  - success：check-gate 经 db.get_task_changed_files → route_rpc(task.get_changed_files,
    READ_ONLY) + db.run_check_gate → route_rpc(gate.run_check, PROTECTED_MUTATION)
  - success：--resolve 经 db.resolve_gate_findings → route_rpc(gate.resolve_findings,
    PROTECTED_MUTATION)

Rust 侧 cli_handle_check_gate_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli019_check_gate_run_routes_to_daemon(monkeypatch, capsys):
    """success：check-gate 经 route_rpc 调用 task.get_changed_files + gate.run_check。"""
    calls = []

    def _fake_route(method, params, op_class):
        calls.append((method, op_class))
        if method == "task.get_changed_files":
            return ["a.py", "b.py"]
        return {"passed": True, "summary": "2 files OK",
                "checks_run": ["black", "ruff"], "findings": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_check_gate(["T-1"], proxy)
    assert rc is True
    assert calls[0] == ("task.get_changed_files", "READ_ONLY")
    assert ("gate.run_check", "PROTECTED_MUTATION") in calls
    out = capsys.readouterr().out
    assert "2 files OK" in out


def test_cli019_check_gate_resolve_routes_to_daemon(monkeypatch, capsys):
    """success：--resolve 经 route_rpc 调用 gate.resolve_findings。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return {"resolved_count": 3}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_check_gate(["T-1", "--resolve"], proxy)
    assert rc is True
    assert captured.get("method") == "gate.resolve_findings"
    assert captured.get("op") == "PROTECTED_MUTATION"
    out = capsys.readouterr().out
    assert "3" in out
