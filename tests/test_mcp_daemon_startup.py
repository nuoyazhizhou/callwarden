"""MCP 启动期 daemon 唤起的最小契约测试。

契约（ADR-002 单轨，2026-11-10 复核）：`_start_daemon_for_mcp_startup` 只按
`is_http_transport_enabled()` 分支，不再读取 `get_daemon_mode`，也不再调用
`daemon_autostart.ensure_daemon_for_startup`（UDS / Named Pipe / bridge 全部下线）：

- HTTP 通道（未设置或 http/auto）：先 `HttpDaemonRpcClient.health()` 探活；
  不可达时单次 `spawn_http_daemon(wait_window=15.0)` 自愈。
- 非 HTTP 通道（named-pipe / uds / windows-bridge / cli-bridge）：fail-closed，
  不探活、不 spawn、不回退任何 legacy 传输。
"""


def _patch_http_health(monkeypatch, *, ready: bool):
    """替换 HTTP client 的 health 探针，返回记录构造/fake 对象。"""
    calls = []

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append(("init", args, kwargs))

        def health(self):
            calls.append(("health",))
            if not ready:
                raise RuntimeError("daemon unreachable (fake)")
            return {"status": "ok"}

    monkeypatch.setattr(
        "callwarden.server.daemon_client.HttpDaemonRpcClient", _FakeClient
    )
    return calls


def test_mcp_startup_probe_skips_local_mode(monkeypatch):
    """local 模式无合法传输 → 等价非 HTTP 分支：不探活、不 spawn。

    ADR-002 已删除 local 模式的 HTTP 分支，`_start_daemon_for_mcp_startup`
    不再读取 `get_daemon_mode`；这里用 legacy 传输显式固定该分支，并断言
    「不产生任何 daemon 唤起副作用」。
    """
    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "named-pipe")
    monkeypatch.setattr("callwarden.config.get_daemon_mode", lambda: "local")
    probed = _patch_http_health(monkeypatch, ready=True)
    spawned = []
    monkeypatch.setattr(
        "callwarden.server.daemon_autostart.spawn_http_daemon",
        lambda **kwargs: spawned.append(kwargs) or "https://127.0.0.1:1",
    )

    from callwarden.server.mcp_server import _start_daemon_for_mcp_startup

    _start_daemon_for_mcp_startup()
    assert probed == [], f"非 HTTP 通道不应构造健康探针：{probed!r}"
    assert spawned == [], f"非 HTTP 通道不应 spawn daemon：{spawned!r}"


def test_mcp_startup_probe_uses_shared_autostart(monkeypatch):
    """HTTP 通道探活失败 → 单次调用共享 autostart（daemon_autostart.spawn_http_daemon）。"""
    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "http")
    probed = _patch_http_health(monkeypatch, ready=False)
    spawned = []
    monkeypatch.setattr(
        "callwarden.server.daemon_autostart.spawn_http_daemon",
        lambda **kwargs: spawned.append(kwargs) or "https://127.0.0.1:8443",
    )

    from callwarden.server.mcp_server import _start_daemon_for_mcp_startup

    _start_daemon_for_mcp_startup()
    assert ("health",) in probed, f"HTTP 通道必须先做健康探活：{probed!r}"
    assert spawned == [{"wait_window": 15.0}], (
        f"探活失败后应单次调用 spawn_http_daemon(wait_window=15.0)：{spawned!r}"
    )


def test_mcp_startup_probe_does_not_start_local_service_for_bridge(monkeypatch):
    """bridge 等 legacy 传输 → fail-closed：不探活、不 spawn、不回退。"""
    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "windows-bridge")
    probed = _patch_http_health(monkeypatch, ready=True)
    spawned = []
    monkeypatch.setattr(
        "callwarden.server.daemon_autostart.spawn_http_daemon",
        lambda **kwargs: spawned.append(kwargs) or "https://127.0.0.1:1",
    )

    from callwarden.server.mcp_server import _start_daemon_for_mcp_startup

    _start_daemon_for_mcp_startup()
    assert probed == [], f"bridge 通道不应构造健康探针：{probed!r}"
    assert spawned == [], f"bridge 通道不应 spawn daemon：{spawned!r}"
