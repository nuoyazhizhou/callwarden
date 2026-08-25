"""CLI-096 (A′ cli_command_projection) `cw main` RpcDBProxy HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_096_http_rpc.py）：
  - success：db.<方法>(...) 经 RpcDBProxy._rpc_call 转发为 route_rpc（daemon RPC），
    Python 仅参数映射，无本地 SQLite 业务路径
  - 结构不变量：conn / db_path 显式禁止（CLI 纯 client 化，fail-closed）
  - 任务/治理写面方法映射为 GOVERNANCE_WRITE（task.* / lease.* / admin.*）

Python 侧已通过 route_rpc 路由到 Rust daemon；Rust 侧 cli_main_handlers.rs /
query_compat_handlers.rs / semantic_query_handlers.rs / summary_query_handlers.rs
由其它 agent 核验。
"""

import pytest

import callwarden.cli.main as main_mod


def test_cli096_proxy_routes_query_to_daemon(monkeypatch):
    """success：detect_cycles 经 _rpc_call 转发 route_rpc(READ_ONLY)。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return []

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    proxy.detect_cycles("sym-a")
    assert captured.get("method") == "query.detect_cycles"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("max_depth") == "sym-a"


def test_cli096_proxy_task_write_is_governance(monkeypatch):
    """结构不变量：task_create 映射为 task.create + GOVERNANCE_WRITE。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["op"] = op_class
        return {"task_id": "T-1"}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    proxy.task_create("标题", "描述", [], "me")
    assert captured.get("method") == "task.create"
    assert captured.get("op") == "GOVERNANCE_WRITE"


def test_cli096_proxy_blocks_direct_db_access(monkeypatch):
    """结构不变量：conn / db_path 显式禁止，fail-closed 不暴露本地 SQLite。"""
    def _fake_route(method, params, op_class):
        return []

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    with pytest.raises(AttributeError):
        _ = proxy.conn
    with pytest.raises(AttributeError):
        _ = proxy.db_path


def test_cli096_proxy_close_is_noop(monkeypatch):
    """结构不变量：close 为 no-op（daemon 拥有数据库生命周期）。"""
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    assert proxy.close() is None
