"""CLI-035 (A′ cli_command_projection) `cw gc` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_035_http_rpc.py）：
  - success：status 经 db.gc_status → route_rpc(gc_status, READ_ONLY)；
    archive-list 经 db.gc_archive_list → route_rpc(admin.gc_archive_list,
    READ_ONLY)；purge 经 db.gc_purge → route_rpc（PROTECTED_MUTATION）
  - 结构不变量：GC 归档/清理写操作经 daemon RPC，无本地 SQLite 业务路径

Rust 侧 cli_handle_gc_handlers.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli035_gc_status_routes_to_daemon(monkeypatch, capsys):
    """success：gc status 经 route_rpc 调用 gc_status。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return {"active_files": 5, "archived_files": 3, "deleted_files": 1,
                "archive_ratio": 0.5, "archived_symbols": 10,
                "archived_calls": 5, "recent_archives": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_gc(["status"], proxy)
    assert rc is True
    assert captured.get("method") == "gc_status"
    assert captured.get("op") == "READ_ONLY"


def test_cli035_gc_archive_list_routes_to_daemon(monkeypatch, capsys):
    """success：gc archive-list 经 route_rpc 调用 admin.gc_archive_list。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [{"name": "a.db.gz", "path": "a.db.gz", "size": 100,
                 "mtime": 1787323000.0, "reason": "ignored"}]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_gc(["archive-list", "--limit", "10"], proxy)
    assert rc is True
    assert captured.get("method") == "admin.gc_archive_list"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("limit") == 10


def test_cli035_gc_purge_routes_to_daemon(monkeypatch, capsys):
    """success：gc purge 经 route_rpc 调用 admin.gc_purge（PROTECTED_MUTATION）。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"purged_files": 2, "purged_bytes": 1000,
                "purged_symbols": 10, "purged_calls": 5}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_gc(["purge", "--older-than", "45"], proxy)
    assert rc is True
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("older_than_days") == 45
