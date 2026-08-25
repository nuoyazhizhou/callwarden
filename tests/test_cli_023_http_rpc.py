"""CLI-023 (A′ cli_command_projection) `cw complexity` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_023_http_rpc.py）：
  - success：db.get_complexity_hotspots 经 RpcDBProxy._rpc_call → route_rpc
    （query.complexity_hotspots，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.complexity_hotspots dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli023_complexity_routes_to_daemon(monkeypatch, capsys):
    """success：complexity 经 route_rpc 调用 query.complexity_hotspots。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"cyclomatic_complexity": 15, "line_count": 80, "depth": 4,
             "qualified_name": "m::hot", "file_path": "a.py", "start_line": 10},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_complexity(["10", "--module", "m"], proxy)
    assert rc is True
    assert captured.get("method") == "query.complexity_hotspots"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("limit") == 10
    assert captured["params"].get("module_filter") == "m"
    out = capsys.readouterr().out
    assert "m::hot" in out
