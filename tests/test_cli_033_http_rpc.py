"""CLI-033 (A′ cli_command_projection) `cw fts` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_033_http_rpc.py）：
  - success：status 经 db.get_fts_status → route_rpc(get_fts_status, READ_ONLY)；
    rebuild 经 db.rebuild_fts_index → route_rpc(rebuild_fts_index,
    PROTECTED_MUTATION)
  - 结构不变量：rebuild 为 PROTECTED_MUTATION（写操作无本地 SQLite fallback）

Rust 侧 cli_handle_fts_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli033_fts_status_routes_to_daemon(monkeypatch, capsys):
    """success：fts status 经 route_rpc 调用 get_fts_status。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return {"exists": True, "symbols_count": 100, "fts_rows": 100,
                "triggers": ["trg_sync"], "consistent": True}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_fts(["status"], proxy)
    assert rc is True
    assert captured.get("method") == "get_fts_status"
    assert captured.get("op") == "READ_ONLY"


def test_cli033_fts_rebuild_routes_to_daemon(monkeypatch, capsys):
    """success：fts rebuild 经 route_rpc 调用 rebuild_fts_index（PROTECTED_MUTATION）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"success": True, "symbols_count": 100, "fts_rows": 100,
                "triggers_recreated": 1, "elapsed": 0.5}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_fts(["rebuild"], proxy)
    assert rc is True
    assert captured.get("method") == "rebuild_fts_index"
    assert captured.get("op") == "PROTECTED_MUTATION"
