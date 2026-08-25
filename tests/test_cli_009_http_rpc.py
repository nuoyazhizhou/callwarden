"""CLI-009 (A′ graph_snapshot) `cw dependency list` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_009_http_rpc.py）：
  - success：db.get_dependency_edges 经 RpcDBProxy._rpc_call → route_rpc
    （get_dependency_edges，READ_ONLY），Python 仅编排输出与本地过滤
  - 结构不变量：Rust 侧 handle_get_dependency_edges（MCP-009 已迁移）为唯一 authority

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dependency_query_handlers.rs
（MCP-009 已迁移）由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def _opts(contract_id=None, use_json=False):
    return type("O", (), {"contract_id": contract_id, "json": use_json})()


def test_cli009_dependency_list_routes_to_daemon(monkeypatch, capsys):
    """success：dependency list 经 route_rpc 调用 get_dependency_edges。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return [
            {"provider_task_id": "T-A", "consumer_task_id": "T-B",
             "edge_type": "import", "source_type": "hard", "is_hard": True,
             "contract_id": "C-1"},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._dependency_list(proxy, 7, _opts(), False)
    assert rc is True
    assert captured.get("method") == "get_dependency_edges"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("workspace_id") == 7
    out = capsys.readouterr().out
    assert "硬依赖图边: 1 条" in out
    assert "T-A → T-B" in out


def test_cli009_dependency_list_contract_filter(monkeypatch, capsys):
    """success：--contract-id 在 Python 侧做展示层过滤。"""
    def _fake_route(method, params, op_class):
        return [
            {"provider_task_id": "T-A", "consumer_task_id": "T-B",
             "edge_type": "import", "source_type": "hard", "is_hard": True,
             "contract_id": "C-1"},
            {"provider_task_id": "T-C", "consumer_task_id": "T-D",
             "edge_type": "call", "source_type": "info", "is_hard": False,
             "contract_id": "C-2"},
        ]

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._dependency_list(proxy, 7, _opts(contract_id="C-2"), False)
    assert rc is True
    out = capsys.readouterr().out
    assert "硬依赖图边: 1 条" in out
    assert "T-C → T-D" in out
