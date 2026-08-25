"""CLI-024 (A′ cli_command_projection) `cw coupled-fns` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_024_http_rpc.py）：
  - success：db.get_most_coupled_functions 经 RpcDBProxy._rpc_call → route_rpc
    （query.most_coupled_functions，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.most_coupled_functions dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli024_coupled_fns_routes_to_daemon(monkeypatch, capsys):
    """success：coupled-fns 经 route_rpc 调用 query.most_coupled_functions。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"fan_in": 5, "fan_out": 3, "total_coupling": 8,
             "qualified_name": "m::hub", "file_path": "a.py"},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_coupled_fns(["10"], proxy)
    assert rc is True
    assert captured.get("method") == "query.most_coupled_functions"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("limit") == 10
    out = capsys.readouterr().out
    assert "m::hub" in out
