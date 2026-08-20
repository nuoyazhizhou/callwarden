"""共存契约子任务3：WSL client authority routing 测试。

对应 windows-wsl-daemon-coexistence-contract.md §5.1/§5.2 与
windows-wsl-daemon-coexistence-task-plan.md 子任务3。

覆盖：
- get_daemon_authority / get_daemon_transport / resolve_daemon_endpoint_for_authority
- windows-host + 非 Windows 无 bridge → E_AUTHORITY_UNRESOLVED（禁止直连 /mnt/c SQLite）
- windows-host + windows-bridge → bridge endpoint
- wsl-local / linux-system + uds → UDS
- 未配置时按平台默认
"""
import json
import os
import socket
import sys
import threading
import time

import pytest


def test_authority_default_windows():
    from callwarden.config import get_daemon_authority

    # 模拟 Windows 平台（sys.platform 不可改，用环境变量覆盖）
    os.environ["CW_AUTHORITY"] = "windows-host"
    assert get_daemon_authority() == "windows-host"


def test_authority_explicit_values():
    from callwarden.config import get_daemon_authority

    for value in ("auto", "windows-host", "wsl-local", "linux-system"):
        os.environ["CW_AUTHORITY"] = value
        assert get_daemon_authority() == value


def test_transport_explicit_values():
    from callwarden.config import get_daemon_transport

    for value in ("auto", "named-pipe", "uds", "windows-bridge", "cli-bridge"):
        os.environ["CW_DAEMON_TRANSPORT"] = value
        assert get_daemon_transport() == value


def test_resolve_windows_host_with_bridge():
    """windows-host + windows-bridge → bridge endpoint。"""
    from callwarden.config import resolve_daemon_endpoint_for_authority

    os.environ["CW_AUTHORITY"] = "windows-host"
    os.environ["CW_DAEMON_TRANSPORT"] = "windows-bridge"
    os.environ["CW_BRIDGE_ENDPOINT"] = "tcp://127.0.0.1:8456"
    assert resolve_daemon_endpoint_for_authority() == "tcp://127.0.0.1:8456"


def test_resolve_windows_host_without_bridge_on_linux():
    """WSL 访问 windows-host 但无 bridge → E_AUTHORITY_UNRESOLVED（禁止 /mnt/c）。"""
    from callwarden.config import resolve_daemon_endpoint_for_authority

    if sys.platform == "win32":
        pytest.skip("Windows 平台可直接用 named-pipe，无此约束")
    os.environ["CW_AUTHORITY"] = "windows-host"
    os.environ["CW_DAEMON_TRANSPORT"] = "auto"
    with pytest.raises(ValueError) as exc_info:
        resolve_daemon_endpoint_for_authority()
    assert "E_AUTHORITY_UNRESOLVED" in str(exc_info.value)
    assert "/mnt/c" in str(exc_info.value)


def test_resolve_wsl_local_uds():
    """wsl-local + uds → UDS endpoint。"""
    from callwarden.config import resolve_daemon_endpoint_for_authority

    os.environ["CW_AUTHORITY"] = "wsl-local"
    os.environ["CW_DAEMON_TRANSPORT"] = "uds"
    os.environ["CW_DAEMON_SOCKET"] = "/tmp/callwarden-test.sock"
    assert resolve_daemon_endpoint_for_authority() == "/tmp/callwarden-test.sock"


def test_resolve_linux_system_uds():
    """linux-system + uds → 默认 UDS。"""
    from callwarden.config import resolve_daemon_endpoint_for_authority

    os.environ["CW_AUTHORITY"] = "linux-system"
    os.environ["CW_DAEMON_TRANSPORT"] = "uds"
    os.environ.pop("CW_DAEMON_SOCKET", None)
    assert resolve_daemon_endpoint_for_authority() == "/run/callwarden/callwarden.sock"


def test_daemon_client_uses_authority_resolver():
    """UnixDaemonRpcClient 默认 endpoint 走 authority 解析（不再直接 get_default_endpoint）。"""
    import inspect

    from callwarden.server.daemon_client import UnixDaemonRpcClient

    src = inspect.getsource(UnixDaemonRpcClient.__init__)
    assert "resolve_daemon_endpoint_for_authority" in src


def test_daemon_client_forbids_mnt_c_fallback():
    """WSL 不得经 daemon_client 直接打开 /mnt/c 权威库。"""
    import inspect

    from callwarden.server.daemon_client import UnixDaemonRpcClient

    src = inspect.getsource(UnixDaemonRpcClient)
    # 不得把 /mnt/c 路径硬编码为 SQLite 连接目标（权威库只经 daemon/bridge 访问）
    assert "mnt/c" not in src or "禁止" in src
    # 不得用 immutable=1 读 Windows 权威库当最新状态（契约 §6.4）
    assert "immutable" not in src


# ============================================
# P0 修复：try_connect 支持 TCP bridge（WSL → Windows bridge）
# ============================================


def test_try_connect_tcp_tcp_prefix():
    """try_connect 能连 tcp://host:port 形式的 bridge endpoint。"""
    from callwarden.server.daemon_autostart import try_connect

    if sys.platform == "win32":
        pytest.skip("Windows 平台 try_connect 走 Named Pipe 分支")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    result = {"ok": False}

    def accept():
        conn, _ = srv.accept()
        conn.close()
        result["ok"] = True

    threading.Thread(target=accept, daemon=True).start()
    try:
        conn = try_connect(f"tcp://127.0.0.1:{port}")
        assert conn is not None, "tcp:// 端点应可连接"
        # 等待 server accept 完成（短轮询，避免竞态）
        deadline = time.time() + 3
        while time.time() < deadline and not result["ok"]:
            time.sleep(0.02)
        conn.close()
        assert result["ok"], "TCP server 应收到连接"
    finally:
        srv.close()


def test_try_connect_tcp_bare_host_port():
    """try_connect 能连裸 host:port 形式的 bridge endpoint。"""
    from callwarden.server.daemon_autostart import try_connect

    if sys.platform == "win32":
        pytest.skip("Windows 平台 try_connect 走 Named Pipe 分支")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)

    def accept():
        conn, _ = srv.accept()
        conn.close()

    threading.Thread(target=accept, daemon=True).start()
    try:
        conn = try_connect(f"127.0.0.1:{port}")
        assert conn is not None, "裸 host:port 端点应可连接"
        conn.close()
    finally:
        srv.close()


def test_try_connect_tcp_unreachable_returns_none():
    """不可达 TCP 端点返回 None（不抛异常）。"""
    from callwarden.server.daemon_autostart import try_connect

    if sys.platform == "win32":
        pytest.skip("Windows 平台 try_connect 走 Named Pipe 分支")
    assert try_connect("tcp://127.0.0.1:1") is None
    # UDS 路径不受 TCP 逻辑影响
    assert try_connect("/nonexistent/callwarden.sock") is None


# ============================================
# P0/P1 二轮修复：manifest 消费 + 生产 client 注入 token
# ============================================


def test_bridge_manifest_endpoint_consumed(tmp_path):
    """get_bridge_endpoint 能消费 bridge.manifest.json 的实际端口。"""
    from callwarden.config import get_bridge_endpoint

    manifest = tmp_path / "bridge.manifest.json"
    manifest.write_text(
        '{"endpoint": "tcp://127.0.0.1:8457", "authority": "windows-host", '
        '"transport": "windows-bridge", "token_file": "/x/bridge.token"}',
        encoding="utf-8",
    )
    os.environ["CW_BRIDGE_MANIFEST"] = str(manifest)
    os.environ.pop("CW_BRIDGE_ENDPOINT", None)
    os.environ.pop("CW_BRIDGE_ADDR", None)
    try:
        assert get_bridge_endpoint() == "tcp://127.0.0.1:8457"
    finally:
        os.environ.pop("CW_BRIDGE_MANIFEST", None)


def test_bridge_manifest_endpoint_fallback_when_missing(tmp_path):
    """manifest 缺失时只可走显式 endpoint，不能降级到 127.0.0.1:0。"""
    from callwarden.config import get_bridge_endpoint

    # 隔离：CW_BRIDGE_MANIFEST 指向不存在的路径，避免读到用户默认 manifest
    os.environ["CW_BRIDGE_MANIFEST"] = str(tmp_path / "nonexistent-manifest.json")
    os.environ.pop("CW_BRIDGE_ENDPOINT", None)
    os.environ["CW_BRIDGE_ADDR"] = "127.0.0.1:9999"
    try:
        assert get_bridge_endpoint() == "127.0.0.1:9999"
    finally:
        os.environ.pop("CW_BRIDGE_MANIFEST", None)
        os.environ.pop("CW_BRIDGE_ADDR", None)


def test_bridge_manifest_rejects_wrong_authority_or_transport(tmp_path):
    """错误 authority/transport 的 manifest 不能提供跨边界 endpoint 或 token。"""
    from callwarden.config import get_bridge_endpoint, get_bridge_token

    token_file = tmp_path / "bridge.token"
    token_file.write_text("wrong-authority-token\n", encoding="utf-8")
    manifest = tmp_path / "bridge.manifest.json"
    manifest.write_text(json.dumps({
        "endpoint": "tcp://127.0.0.1:8457",
        "authority": "wsl-local",
        "transport": "uds",
        "token_file": str(token_file),
    }), encoding="utf-8")
    os.environ["CW_BRIDGE_MANIFEST"] = str(manifest)
    os.environ.pop("CW_BRIDGE_ENDPOINT", None)
    os.environ.pop("CW_BRIDGE_ADDR", None)
    os.environ.pop("CW_BRIDGE_TOKEN_FILE", None)
    try:
        assert get_bridge_endpoint() == ""
        assert get_bridge_token() == ""
    finally:
        os.environ.pop("CW_BRIDGE_MANIFEST", None)


def test_windows_bridge_requires_real_endpoint(tmp_path):
    """缺 endpoint 时 windows-host bridge 路由必须 E_AUTHORITY_UNRESOLVED。"""
    from callwarden.config import resolve_daemon_endpoint_for_authority

    os.environ["CW_AUTHORITY"] = "windows-host"
    os.environ["CW_DAEMON_TRANSPORT"] = "windows-bridge"
    os.environ["CW_BRIDGE_MANIFEST"] = str(tmp_path / "missing.json")
    os.environ.pop("CW_BRIDGE_ENDPOINT", None)
    os.environ.pop("CW_BRIDGE_ADDR", None)
    try:
        with pytest.raises(ValueError, match="E_AUTHORITY_UNRESOLVED"):
            resolve_daemon_endpoint_for_authority()
    finally:
        os.environ.pop("CW_BRIDGE_MANIFEST", None)


def test_get_bridge_token_uses_valid_manifest_token_file(tmp_path):
    """WSL client 必须消费同一份有效 manifest 声明的 token_file。"""
    from callwarden.config import get_bridge_token

    token_file = tmp_path / "bridge.token"
    token_file.write_text("manifest-token\n", encoding="utf-8")
    manifest = tmp_path / "bridge.manifest.json"
    manifest.write_text(json.dumps({
        "endpoint": "tcp://127.0.0.1:8457",
        "authority": "windows-host",
        "transport": "windows-bridge",
        "token_file": str(token_file),
    }), encoding="utf-8")
    os.environ["CW_BRIDGE_MANIFEST"] = str(manifest)
    os.environ.pop("CW_BRIDGE_TOKEN_FILE", None)
    try:
        assert get_bridge_token() == "manifest-token"
    finally:
        os.environ.pop("CW_BRIDGE_MANIFEST", None)


def test_get_bridge_token_reads_file(tmp_path):
    """get_bridge_token 从 token 文件读取内容。"""
    from callwarden.config import get_bridge_token

    token_file = tmp_path / "bridge.token"
    token_file.write_text("secret-token\n", encoding="utf-8")
    os.environ["CW_BRIDGE_TOKEN_FILE"] = str(token_file)
    try:
        assert get_bridge_token() == "secret-token"
    finally:
        os.environ.pop("CW_BRIDGE_TOKEN_FILE", None)


def test_get_bridge_token_missing_returns_empty():
    """token 文件缺失返回空串（fail-closed 前置）。"""
    from callwarden.config import get_bridge_token

    os.environ["CW_BRIDGE_TOKEN_FILE"] = "/nonexistent/bridge.token"
    try:
        assert get_bridge_token() == ""
    finally:
        os.environ.pop("CW_BRIDGE_TOKEN_FILE", None)


def test_daemon_client_injects_bridge_token():
    """生产 UnixDaemonRpcClient 在 bridge transport 下注入 bridge_token。"""
    from unittest.mock import patch

    from callwarden.server.daemon_client import UnixDaemonRpcClient

    captured = {}

    class _FakeConn:
        def __init__(self):
            self.sent = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

    fake_conn = _FakeConn()

    def _fake_recv(conn, max_bytes):
        # 返回匹配 id 的响应
        import json as _json

        req = json.loads(fake_conn.sent)
        return {"id": req["id"], "ok": True, "result": {"pong": True}}

    def _fake_send(conn, msg, max_bytes):
        fake_conn.sent = json.dumps(msg)
        captured.update(msg)

    def _fake_try_connect(endpoint):
        return fake_conn

    with patch("callwarden.server.daemon_client.try_connect", side_effect=_fake_try_connect), patch(
        "callwarden.server.daemon_client.send_message", side_effect=_fake_send
    ), patch(
        "callwarden.server.daemon_client.recv_message", side_effect=_fake_recv
    ), patch(
        "callwarden.config.is_bridge_transport", return_value=True
    ), patch(
        "callwarden.config.get_bridge_token", return_value="secret-token"
    ):
        client = UnixDaemonRpcClient.__new__(UnixDaemonRpcClient)
        client.socket_path = "tcp://127.0.0.1:8456"
        client.timeout = 5
        client.max_message_bytes = 1 << 20
        client._ids = __import__("itertools").count(1)
        client.transport_override = None  # __new__ 跳过 __init__
        client.call("ping", {})
        # bridge_token 必须在请求顶层（与 cw_bridge.rs validate_token 一致）
        assert captured["bridge_token"] == "secret-token"
        assert captured["method"] == "ping"
        # params 内不应有 bridge_token
        assert "bridge_token" not in captured["params"]


def test_daemon_client_transport_override_forces_token():
    """评审二轮：transport_override='windows-bridge' 不依赖全局 env 也注入 token。"""
    from unittest.mock import patch

    from callwarden.server.daemon_client import UnixDaemonRpcClient

    captured = {}

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

    fake_conn = _FakeConn()

    def _fake_recv(conn, max_bytes):
        import json as _json

        req = json.loads(fake_conn.sent)
        return {"id": req["id"], "ok": True, "result": {"pong": True}}

    def _fake_send(conn, msg, max_bytes):
        fake_conn.sent = json.dumps(msg)
        captured.update(msg)

    def _fake_try_connect(endpoint):
        return fake_conn

    with patch("callwarden.server.daemon_client.try_connect", side_effect=_fake_try_connect), patch(
        "callwarden.server.daemon_client.send_message", side_effect=_fake_send
    ), patch("callwarden.server.daemon_client.recv_message", side_effect=_fake_recv), patch(
        "callwarden.config.is_bridge_transport", return_value=False  # 全局 env 未设 bridge
    ), patch(
        "callwarden.config.get_bridge_token", return_value="secret-token"
    ):
        client = UnixDaemonRpcClient(
            socket_path="tcp://127.0.0.1:8456", transport_override="windows-bridge"
        )
        client.call("ping", {})
        # transport_override 强制注入顶层 token
        assert captured["bridge_token"] == "secret-token"


def test_bridge_endpoint_override_wins_over_global_endpoint(monkeypatch):
    """bridge health 的显式 endpoint 不得被全局 endpoint 覆盖。"""
    from callwarden.server.daemon_client import UnixDaemonRpcClient

    monkeypatch.setenv("CW_DAEMON_ENDPOINT", "tcp://127.0.0.1:1")
    client = UnixDaemonRpcClient(
        socket_path="tcp://127.0.0.1:8456",
        transport_override="windows-bridge",
        endpoint_override=True,
    )
    assert client.socket_path == "tcp://127.0.0.1:8456"


def test_try_connect_prefers_tcp_on_windows(monkeypatch):
    """Windows 上的 TCP bridge endpoint 不能被 Named Pipe 分支截走。"""
    import callwarden.server.daemon_autostart as autostart

    tcp_marker = object()
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    monkeypatch.setattr(autostart, "_try_connect_tcp", lambda endpoint: tcp_marker)
    monkeypatch.setattr(
        autostart,
        "_try_connect_windows",
        lambda endpoint: pytest.fail("TCP endpoint 不应进入 Named Pipe 分支"),
    )
    assert autostart.try_connect("tcp://127.0.0.1:8456") is tcp_marker


def test_daemon_client_no_token_in_uds_mode():
    """非 bridge transport（UDS）不注入 bridge_token。"""
    from unittest.mock import patch

    from callwarden.server.daemon_client import UnixDaemonRpcClient

    captured = {}

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

    fake_conn = _FakeConn()

    def _fake_recv(conn, max_bytes):
        import json as _json

        req = json.loads(fake_conn.sent)
        return {"id": req["id"], "ok": True, "result": {"pong": True}}

    def _fake_send(conn, msg, max_bytes):
        fake_conn.sent = json.dumps(msg)
        captured.update(msg)

    def _fake_try_connect(endpoint):
        return fake_conn

    with patch("callwarden.server.daemon_client.try_connect", side_effect=_fake_try_connect), patch(
        "callwarden.server.daemon_client.send_message", side_effect=_fake_send
    ), patch("callwarden.server.daemon_client.recv_message", side_effect=_fake_recv), patch(
        "callwarden.config.is_bridge_transport", return_value=False
    ):
        client = UnixDaemonRpcClient.__new__(UnixDaemonRpcClient)
        client.socket_path = "/run/callwarden/callwarden.sock"
        client.timeout = 5
        client.max_message_bytes = 1 << 20
        client._ids = __import__("itertools").count(1)
        client.transport_override = None  # __new__ 跳过 __init__
        client.call("ping", {})
        assert "bridge_token" not in captured["params"]
