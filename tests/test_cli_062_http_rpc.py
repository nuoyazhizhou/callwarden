"""CLI-062 (A′ cli_command_projection) `cw stats` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_062_http_rpc.py）：
  - success：db.get_stats 经 RpcDBProxy._rpc_call → route_rpc（query.stats，
    READ_ONLY），Python 仅格式化输出
  - 参数契约：无参数（stats 无参）
  - 结构不变量：Rust 侧 query.stats（handle_query_stats，R3 native）为唯一
    authority；Python 不 direct DB（PyO3 stats_command_run_py 仅输出格式化
    fail-soft 辅助，不产生业务数据）

Rust 侧 query.stats 由 R3/W2 迁移核验。
"""

import callwarden.cli.main as main_mod


def test_cli062_stats_routes_to_daemon(monkeypatch, capsys):
    """success：stats 经 route_rpc 调用 query.stats，输出 JSON。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"files": 100, "symbols": 2000, "edges": 5000}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)
    # 禁用 PyO3 stats_command_run_py（走纯 Python 输出路径，验证 db 转发本身）
    real_import = __import__
    def _blocked_import(name, *a, **k):
        if name == "callwarden_core":
            raise ImportError(name)
        return real_import(name, *a, **k)
    monkeypatch.setattr("builtins.__import__", _blocked_import)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_stats([], proxy)
    assert rc is True
    assert captured.get("method") == "query.stats"
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "symbols" in out and "2000" in out
