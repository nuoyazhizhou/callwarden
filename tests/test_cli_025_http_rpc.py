"""CLI-025 (A′ cli_command_projection) `cw coupling` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_025_http_rpc.py）：
  - success：db.get_coupling_analysis 经 RpcDBProxy._rpc_call → route_rpc
    （query.coupling_analysis，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.coupling_analysis dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli025_coupling_routes_to_daemon(monkeypatch, capsys):
    """success：coupling 经 route_rpc 调用 query.coupling_analysis。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"module": "core", "afferent": 5, "efferent": 2,
             "total_coupling": 7, "instability": 0.29},
            {"module": "api", "afferent": 1, "efferent": 4,
             "total_coupling": 5, "instability": 0.80},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_coupling([], proxy)
    assert rc is True
    assert captured.get("method") == "query.coupling_analysis"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("limit") == 30
    out = capsys.readouterr().out
    assert "core" in out
    assert "api" in out
