"""CLI-007 (A′ graph_snapshot) `cw dependency cycle` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_007_http_rpc.py）：
  - success：db.detect_cycle(workspace_id) 经 RpcDBProxy._rpc_call → route_rpc
    （detect_cycle，READ_ONLY），Python 仅编排输出，无本地 SQLite 业务路径
  - 结构不变量：Rust 侧 handle_detect_cycle（MCP-007 已迁移）为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dependency_query_handlers.rs
（MCP-007 已迁移）由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def test_cli007_dependency_cycle_routes_to_daemon(monkeypatch, capsys):
    """success：dependency cycle 经 route_rpc 调用 detect_cycle。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "has_cycle": True,
            "cycle_path": ["A", "B", "A"],
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._dependency_cycle(proxy, 7, type("O", (), {"json": False})(), False)
    assert rc is True
    assert captured.get("method") == "detect_cycle"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("workspace_id") == 7
    out = capsys.readouterr().out
    assert "A → B → A" in out


def test_cli007_dependency_cycle_json(monkeypatch, capsys):
    """success：--json 输出原始 daemon 结果。"""
    def _fake_route(method, params, op_class):
        return {"has_cycle": False, "cycle_path": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._dependency_cycle(proxy, 7, type("O", (), {"json": True})(), True)
    assert rc is True
    out = capsys.readouterr().out
    assert '"has_cycle": false' in out
