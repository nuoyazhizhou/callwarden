"""CLI-063 (A′ cli_command_projection) `cw status` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_063_http_rpc.py）：
  - success：db.get_status 经 RpcDBProxy._rpc_call → route_rpc（query.status，
    READ_ONLY），Python 仅格式化输出
  - 结构不变量：Rust 侧 handle_status（CLI-063 修复）返回完整嵌套结构
    {workspace:{name,root,db_size}, files:{tracked,on_disk,new,stale,deleted,
    new_files,stale_files,deleted_files,by_language}, symbols:{total,by_kind,
    uncommented_fns}, calls:{total,resolved,cross_file,resolve_rate}, depth,
    last_build, needs_rebuild}，与 Python db.get_status 契约一致；修复旧实现
    只返回扁平计数导致 CLI KeyError 的问题

Rust 侧由 cargo 单测覆盖（见 metrics_handlers.rs）。
"""

import callwarden.cli.main as main_mod


def _status_sample():
    return {
        "workspace": {"name": "callwarden", "root": "C:/git_work/callwarden", "db_size": 102400},
        "files": {
            "tracked": 10, "on_disk": 12, "new": 1, "stale": 1, "deleted": 0,
            "new_files": ["n.py"], "stale_files": ["s.py"], "deleted_files": [],
            "by_language": {"py": 8, "rs": 2},
        },
        "symbols": {"total": 100, "by_kind": {"fn": 60, "class": 40}, "uncommented_fns": 5},
        "calls": {"total": 200, "resolved": 180, "cross_file": 20, "resolve_rate": 90.0},
        "depth": {}, "last_build": 1787497000.0, "needs_rebuild": True,
    }


def test_cli063_status_routes_to_daemon(monkeypatch, capsys):
    """success：status 经 route_rpc 调用 query.status，输出完整概览。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return _status_sample()

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_status([], proxy)
    assert rc is True
    assert captured.get("method") == "query.status"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "callwarden" in out and "C:/git_work/callwarden" in out
    assert "100" in out and "90.0" in out
    assert "⚠" in out  # needs_rebuild hint


def test_cli063_status_up_to_date(monkeypatch, capsys):
    """needs_rebuild=False → 输出 up-to-date。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        s = _status_sample()
        s["needs_rebuild"] = False
        s["files"]["new"] = 0
        s["files"]["stale"] = 0
        s["files"]["new_files"] = []
        s["files"]["stale_files"] = []
        return s

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_status([], proxy)
    assert rc is True
    out = capsys.readouterr().out
    assert "up to date" in out
