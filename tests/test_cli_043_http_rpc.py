"""CLI-043 (A′ cli_command_projection) `cw largest-fns` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_043_http_rpc.py）：
  - success：db.get_largest_functions 经 RpcDBProxy._rpc_call → route_rpc
    （query.largest_functions，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 query.largest_functions dispatch 分支为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli043_largest_fns_routes_to_daemon(monkeypatch, capsys):
    """success：largest-fns 经 route_rpc 调用 query.largest_functions。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"line_count": 120, "depth": 5, "qualified_name": "m::big",
             "file_path": "a.py", "start_line": 10},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_largest_fns(["10"], proxy)
    assert rc is True
    assert captured.get("method") == "query.largest_functions"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("limit") == 10
    out = capsys.readouterr().out
    assert "m::big" in out
