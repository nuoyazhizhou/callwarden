"""CLI-094 (A′ assignment_projection) `cw lease` 写操作 HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_094_http_rpc.py）：
  - success：lease.acquire/renew/release 经 _route_lease_write → HTTP daemon（thin client），
    Python 仅编排身份与输出格式化
  - fail-closed：daemon 不可用时 DaemonUnavailableError 上抛，不本地 SQLite 回退

_route_lease_write 已实现 HTTP thin client 路由（HttpDaemonRpcClient 优先，
enterprise/auto 下 daemon 不可用禁止 fallback）；Rust 侧 lease.* handler
（dispatch.rs / http_server.rs）的实现与编译由其它 agent 核验。
"""

import pytest

import callwarden.cli.main as main_mod
from callwarden.server.daemon_client import DaemonUnavailableError


def test_cli094_lease_acquire_routes_to_daemon(monkeypatch, capsys):
    """success：lease acquire 经 _route_lease_write 调用 lease.acquire。"""
    captured = {}

    def _fake_route(method, params, fallback_fn):
        captured["method"] = method
        captured["params"] = params
        return True, {
            "lease_id": "L-1",
            "token": "tok-raw-once",
            "fencing_counter": 1,
            "expires_at": 1787323000.0,
        }

    monkeypatch.setattr(main_mod, "_route_lease_write", _fake_route)

    rc = main_mod._handle_lease(
        ["acquire", "T-1", "--role", "reviewer", "--agent-id", "ag-1",
         "--session-id", "ss-1", "--model-id", "md-1"],
        None)
    assert rc is True
    assert captured.get("method") == "lease.acquire"
    assert captured["params"].get("task_id") == "T-1"
    assert captured["params"].get("role") == "reviewer"
    out = capsys.readouterr().out
    assert "Lease acquired" in out
    assert "tok-raw-once" in out, "raw token 必须仅在 acquire 响应返回一次"


def test_cli094_lease_release_routes_to_daemon(monkeypatch):
    """success：lease release 经 _route_lease_write 调用 lease.release。"""
    captured = {}

    def _fake_route(method, params, fallback_fn):
        captured["method"] = method
        captured["params"] = params
        return True, {"lease_id": "L-1", "fencing_counter": 1, "expires_at": 0.0}

    monkeypatch.setattr(main_mod, "_route_lease_write", _fake_route)

    rc = main_mod._handle_lease(
        ["release", "T-1", "--role", "reviewer", "--token", "tok-x",
         "--agent-id", "ag-1", "--session-id", "ss-1", "--model-id", "md-1"],
        None)
    assert rc is True
    assert captured.get("method") == "lease.release"
    assert captured["params"].get("token") == "tok-x"


def test_cli094_lease_write_daemon_unavailable_fail_closed(monkeypatch):
    """daemon 不可用 -> DaemonUnavailableError 上抛，禁止本地 SQLite 回退。"""
    def _boom(method, params, fallback_fn):
        raise DaemonUnavailableError("daemon 连接失败")

    monkeypatch.setattr(main_mod, "_route_lease_write", _boom)

    with pytest.raises(DaemonUnavailableError):
        main_mod._handle_lease(
            ["acquire", "T-1", "--role", "reviewer",
             "--agent-id", "ag-1", "--session-id", "ss-1", "--model-id", "md-1"],
            None)


def test_cli094_lease_write_identity_incomplete(monkeypatch, capsys):
    """身份不完整 -> E_ASSIGNMENT_INCOMPLETE 拒绝，不触达 daemon。"""
    hit = {"called": False}

    def _fake_route(method, params, fallback_fn):
        hit["called"] = True
        return True, {}

    monkeypatch.setattr(main_mod, "_route_lease_write", _fake_route)

    rc = main_mod._handle_lease(
        ["acquire", "T-1", "--role", "reviewer"], None)
    assert rc is True
    assert not hit["called"], "身份不完整时必须 fail-closed，不触达 lease.acquire"
    out = capsys.readouterr().out
    assert "E_ASSIGNMENT_INCOMPLETE" in out
