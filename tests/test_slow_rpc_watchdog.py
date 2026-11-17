r"""§9.31 长任务心跳：slow RPC watchdog（daemon 死亡/假死快速失败）。

- 判定单测：慢方法（_rpc_timeout_for 放宽）走 watchdog，快方法不走。
- 真实 TLS 栈集成（cryptography 生成测试材料 + 线程内 fake TLS daemon）：
  - daemon 假死（/health 探活连续失败）→ 主调用远早于满超时失败，
    且 code == E_HTTP_DAEMON_UNAVAILABLE；
  - daemon 健康 → 慢方法正常返回（watchdog 不误杀）。
"""

import json
import os
import socket
import ssl
import sys
import threading
import time

import pytest

from callwarden.config import (
    E_HTTP_DAEMON_UNAVAILABLE,
    HTTP_DEFAULT_TIMEOUT,
    HTTP_SLOW_METHOD_TIMEOUT,
    http_method_timeout,
)
from callwarden.server import daemon_client as dc
from callwarden.server.daemon_client import (
    DaemonUnavailableError,
    HttpDaemonRpcClient,
    _rpc_timeout_for,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="§9.31 watchdog 用例需要 Windows + loopback TLS fake daemon",
)

pytest.importorskip("cryptography")

try:
    from test_mtls_control_plane import make_fake_tls_material
except Exception:  # pragma: no cover
    make_fake_tls_material = None


# ----------------------------------------------------------------------
# 判定单测（无需网络）
# ----------------------------------------------------------------------

def test_slow_method_detection():
    """慢方法超时被放宽（> base）→ watchdog 路径；快方法保持 base。"""
    slow = _rpc_timeout_for("workspace.build_graph", HTTP_DEFAULT_TIMEOUT)
    fast = _rpc_timeout_for("ping", HTTP_DEFAULT_TIMEOUT)
    assert slow == HTTP_SLOW_METHOD_TIMEOUT > HTTP_DEFAULT_TIMEOUT
    assert fast == HTTP_DEFAULT_TIMEOUT
    assert slow > fast


def test_explicit_short_timeout_respected_no_watchdog():
    """caller 显式短超时（诊断/测试场景）原样尊重，不进 watchdog。"""
    assert _rpc_timeout_for("workspace.build_graph", 5.0) == 5.0


def test_http_method_timeout_prefix_match():
    """点分命名空间前缀匹配（config 契约，watchdog 判定依赖）。"""
    assert http_method_timeout("workspace.build_graph.full") == HTTP_SLOW_METHOD_TIMEOUT
    assert http_method_timeout("snapshot.publish") == HTTP_SLOW_METHOD_TIMEOUT
    assert http_method_timeout("ping") == HTTP_DEFAULT_TIMEOUT


# ----------------------------------------------------------------------
# Fake TLS daemon（线程内 accept loop，模拟 /health 与 /v1/rpc）
# ----------------------------------------------------------------------

class FakeTLSDaemon:
    """最小 HTTPS server：GET /health 即时 200；POST /v1/rpc 睡 rpc_delay 后返回。

    `health_ok_count`：/health 成功响应的最大次数；超过后 daemon「死亡」
    （关闭 listener 并拒绝新连接），已建立的 RPC 连接继续挂着（假死语义）。
    """

    def __init__(self, material, rpc_delay=0.0, health_ok_count=None):
        self.rpc_delay = rpc_delay
        self.health_ok_count = health_ok_count
        self._health_served = 0
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = self._listener.getsockname()[1]
        self._alive = True
        self._threads = []
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(material["server_cert"], material["server_key"])

    def start(self):
        t = threading.Thread(target=self._accept_loop, daemon=True)
        self._threads.append(t)
        t.start()

    def die(self):
        """关闭 listener：新连接（探活）一律拒绝，已建连接不受影响。"""
        self._alive = False
        try:
            self._listener.close()
        except OSError:
            pass

    def _accept_loop(self):
        while self._alive:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            t = threading.Thread(
                target=self._handle, args=(conn,), daemon=True)
            self._threads.append(t)
            t.start()

    def _handle(self, conn):
        try:
            tls = self._ctx.wrap_socket(conn, server_side=True)
            tls.settimeout(30)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = tls.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            content_length = 0
            for line in head.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())
            while len(rest) < content_length:
                chunk = tls.recv(4096)
                if not chunk:
                    return
                rest += chunk
            request_line = head.split(b"\r\n")[0].decode("ascii")
            method, path = request_line.split(" ")[:2]
            # 回显请求信封的 id（daemon 契约：response id 必须匹配 request id）
            try:
                rid = json.loads(rest.decode("utf-8")).get("id")
            except (ValueError, UnicodeDecodeError):
                rid = None
            if method == "GET" and path == "/health":
                with self._lock:
                    ok = (self.health_ok_count is None
                          or self._health_served < self.health_ok_count)
                    self._health_served += 1
                if not ok:
                    return  # 「死亡」：直接断开，客户端视为探活失败
                body = json.dumps({"pid": os.getpid()}).encode("utf-8")
            elif method == "POST" and path == "/v1/rpc":
                if self.rpc_delay:
                    time.sleep(self.rpc_delay)
                body = json.dumps({
                    "jsonrpc": "2.0", "id": rid if rid is not None else "x",
                    "result": {"ok": True},
                }).encode("utf-8")
            else:
                body = json.dumps({
                    "jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": "method not found"},
                }).encode("utf-8")
            tls.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body)
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _make_client(monkeypatch, material, tmp_path):
    """HttpDaemonRpcClient：注入测试 TLS 材料（绕开 manifest/身份目录解析）。"""
    client = HttpDaemonRpcClient(
        endpoint=None, timeout=HTTP_DEFAULT_TIMEOUT,
        verify_health=False, validate_manifest=False,
    )
    client._manifest = {"tls": {"ca_cert_pem": material["ca_pem"]}}
    client._resolved_endpoint = None
    # _ssl_context() 解析模块级 resolve_client_cert_paths（测试注入 fake 身份）
    monkeypatch.setattr(
        dc, "resolve_client_cert_paths",
        lambda: (material["client_cert"], material["client_key"],
                 material["ca_cert"]),
    )
    # watchdog 加速（测试观测窗口 << 满超时 300s）
    monkeypatch.setattr(dc, "WATCHDOG_PROBE_INTERVAL_SECS", 0.2)
    monkeypatch.setattr(dc, "WATCHDOG_PROBE_TIMEOUT_SECS", 0.2)
    return client


@pytest.fixture
def tls_material(tmp_path):
    if make_fake_tls_material is None:
        pytest.skip("test_mtls_control_plane.make_fake_tls_material 不可用")
    return make_fake_tls_material(str(tmp_path / "tls"))


# ----------------------------------------------------------------------
# 集成：watchdog 快速失败 / 不误杀 / 快方法路径
# ----------------------------------------------------------------------

def test_slow_rpc_fails_fast_when_daemon_dies(monkeypatch, tmp_path, tls_material):
    """daemon 假死（探活连续失败）→ 主调用快速失败 E_HTTP_DAEMON_UNAVAILABLE。

    RPC handler 睡 30s（远超观察窗口）；探活 0.2s 间隔、连续 2 次失败后
    中止——断言主调用在 << 30s 内返回且错误码正确。
    """
    daemon = FakeTLSDaemon(tls_material, rpc_delay=30.0, health_ok_count=1)
    daemon.start()
    try:
        client = _make_client(monkeypatch, tls_material, tmp_path)
        client._resolved_endpoint = f"https://127.0.0.1:{daemon.port}"
        t0 = time.monotonic()
        with pytest.raises(DaemonUnavailableError) as ei:
            client.call("workspace.build_graph", {})
        elapsed = time.monotonic() - t0
        assert ei.value.code == E_HTTP_DAEMON_UNAVAILABLE
        assert elapsed < 20.0, (
            f"watchdog 未快速失败：耗时 {elapsed:.1f}s（应远小于满超时 300s）")
    finally:
        daemon.die()


def test_slow_rpc_completes_when_daemon_healthy(monkeypatch, tmp_path, tls_material):
    """daemon 健康（/health 持续可达）→ 慢方法正常返回，watchdog 不误杀。"""
    daemon = FakeTLSDaemon(tls_material, rpc_delay=0.5, health_ok_count=None)
    daemon.start()
    try:
        client = _make_client(monkeypatch, tls_material, tmp_path)
        client._resolved_endpoint = f"https://127.0.0.1:{daemon.port}"
        result = client.call("workspace.build_graph", {})
        assert result == {"ok": True}
    finally:
        daemon.die()


def test_fast_method_skips_watchdog(monkeypatch, tmp_path, tls_material):
    """快方法路径不变：不进入 _call_with_watchdog（记录器哨兵断言）。"""
    daemon = FakeTLSDaemon(tls_material, rpc_delay=0.0, health_ok_count=None)
    daemon.start()
    try:
        client = _make_client(monkeypatch, tls_material, tmp_path)
        client._resolved_endpoint = f"https://127.0.0.1:{daemon.port}"

        def _no_watchdog(*args, **kwargs):
            raise AssertionError("快方法不应进入 watchdog 路径")

        monkeypatch.setattr(client, "_call_with_watchdog", _no_watchdog)
        result = client.call("ping", {})
        assert result == {"ok": True}
    finally:
        daemon.die()
