"""CLI-022 (A′ cli_command_projection) `cw comment-coverage` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_022_http_rpc.py）：
  - success：db.get_comment_coverage 经 RpcDBProxy._rpc_call → route_rpc
    （get_comment_coverage，READ_ONLY），Python 仅编排输出
  - 结构不变量：Rust 侧 handle_get_comment_coverage 为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli022_comment_coverage_routes_to_daemon(monkeypatch, capsys):
    """success：comment-coverage 经 route_rpc 调用 get_comment_coverage。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "total": 10,
            "commented": 6,
            "coverage": 60.0,
            "by_kind": {"function": {"total": 8, "commented": 5}},
            "by_module": {"core": {"coverage": 70.0, "commented": 7, "total": 10}},
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_comment_coverage(["--by", "module"], proxy)
    assert rc is True
    assert captured.get("method") == "get_comment_coverage"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("group_by") == "module"
    out = capsys.readouterr().out
    assert "60.0" in out
    assert "function" in out
