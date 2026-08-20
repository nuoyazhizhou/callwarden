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
