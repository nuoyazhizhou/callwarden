"""CLI-010 (A′ graph_snapshot) `cw dependency provider-select` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_010_http_rpc.py）：
  - success：db.select_interface_provider 经 RpcDBProxy._rpc_call → route_rpc
    （admin.select_interface_provider，PROTECTED_MUTATION），Python 仅编排输出
  - 结构不变量：Rust 侧 admin.select_interface_provider dispatch 分支为唯一
    写 authority（写操作，无本地 SQLite fallback）

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 dispatch.rs /
http_server.rs 由 Rust 专项 agent 核验。
"""

import callwarden.cli.main as main_mod


def _opts(use_json=False):
    return type("O", (), {
        "consumer_task_id": "T-C", "contract_id": "C-1", "revision": 2,
        "interface_name": "IFace", "provider_task_id": "T-P",
        "json": use_json,
    })()


def test_cli010_provider_select_routes_to_daemon(monkeypatch, capsys):
    """success：provider-select 经 route_rpc 调用 admin.select_interface_provider。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"success": True, "provider_task_id": "T-P"}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._dependency_provider_select(proxy, 7, _opts(), False)
    assert rc is True
    assert captured.get("method") == "admin.select_interface_provider"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("workspace_id") == 7
    assert captured["params"].get("consumer_task_id") == "T-C"
    assert captured["params"].get("selected_provider_task_id") == "T-P"
    out = capsys.readouterr().out
    assert "IFace" in out and "T-P" in out


def test_cli010_provider_select_failure(monkeypatch, capsys):
    """success：失败结果输出 error 文案，不崩溃。"""
    def _fake_route(method, params, op_class):
        return {"success": False, "error": "provider 不可用"}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._dependency_provider_select(proxy, 7, _opts(), False)
    assert rc is True
    out = capsys.readouterr().out
    assert "provider 不可用" in out
