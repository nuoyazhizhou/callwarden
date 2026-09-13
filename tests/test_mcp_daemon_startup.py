"""MCP 启动期 daemon 唤起的最小契约测试。"""

import sys


def test_mcp_startup_probe_skips_local_mode(monkeypatch):
    monkeypatch.setattr("callwarden.config.get_daemon_mode", lambda: "local")
    called = []
    monkeypatch.setattr(
        "callwarden.server.daemon_autostart.ensure_daemon_for_startup",
        lambda *args, **kwargs: called.append(True),
    )

    from callwarden.server.mcp_server import _start_daemon_for_mcp_startup

    _start_daemon_for_mcp_startup()
    assert called == []


def test_mcp_startup_probe_uses_shared_autostart(monkeypatch):
    # stale 修正：is_http_transport_enabled() 迁移期默认 True（config.py:1747-1755），
    # 默认路径走 HTTP health 探测而不再调用 ensure_daemon_for_startup。
    # 显式固定旧通道（CW_DAEMON_TRANSPORT=named-pipe）以覆盖 legacy 分支。
    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "named-pipe")
    monkeypatch.setattr("callwarden.config.get_daemon_mode", lambda: "auto")
    monkeypatch.setattr(
        "callwarden.config.resolve_daemon_endpoint_for_authority",
        lambda: "tcp://127.0.0.1:8456",
    )
    calls = []
    monkeypatch.setattr(
        "callwarden.server.daemon_autostart.ensure_daemon_for_startup",
        lambda endpoint, readiness_check=None: calls.append(endpoint) or True,
    )

    from callwarden.server.mcp_server import _start_daemon_for_mcp_startup

    _start_daemon_for_mcp_startup()
    assert calls == ["tcp://127.0.0.1:8456"]


def test_mcp_startup_probe_does_not_start_local_service_for_bridge(monkeypatch):
    # stale 修正：同上，固定 legacy 通道以覆盖 ensure_daemon_for_startup 分支。
    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "named-pipe")
    monkeypatch.setattr("callwarden.config.get_daemon_mode", lambda: "auto")
    monkeypatch.setattr(
        "callwarden.config.resolve_daemon_endpoint_for_authority",
        lambda: "tcp://127.0.0.1:8456",
    )
    calls = []
    monkeypatch.setattr(
        "callwarden.server.daemon_autostart.ensure_daemon_for_startup",
        lambda endpoint, readiness_check=None: calls.append(endpoint) or True,
    )

    from callwarden.server.mcp_server import _start_daemon_for_mcp_startup

    _start_daemon_for_mcp_startup()
    assert calls == ["tcp://127.0.0.1:8456"]
