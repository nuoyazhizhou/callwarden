"""H2 HTTP client 集成测试（使用 H0-conformant 假 daemon）。

重要：本文件不连接真实 cw-daemon，不打开 SQLite，不回退 Named Pipe/UDS。
所有断言针对 frozen contract §4.2/§4.3：原始出向信封、request_id 复用、
远程错误透传、超时、经 manifest 发现的端到端调用。真实 daemon round-trip
属于 H2I 的职责，不在此声明。
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient, DaemonUnavailableError
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.config import (
    HTTP_MVP_TRANSPORT_PROFILE,
    compute_http_manifest_hash,
    HTTP_MANIFEST_SCHEMA_VERSION,
)


# ----------------------------------------------------------------------
# 假 daemon（H0-conformant 最小实现）
# ----------------------------------------------------------------------

def _make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def _read_json(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        def do_POST(self):
            req = self._read_json()
            state["last_request"] = req
            state["last_path"] = self.path
            if state.get("sleep", 0.0) and req.get("method") == "__sleep__":
                time.sleep(state["sleep"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if state.get("error"):
                resp = {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "error": {
                        "code": -32000,
                        "message": "snapshot is not ready",
                        "data": {
                            "code": "E_SNAPSHOT_NOT_READY",
                            "message_key": "snapshot_not_ready",
                            "retryable": False,
                            "recovery": "publish a snapshot before querying",
                            "request_id": req.get("id"),
                        },
                    },
                }
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "result": {
                        "ok": True,
                        "method": req.get("method"),
                        "params": req.get("params", {}),
                    },
                    "server": {
                        "protocol_version": "1",
                        "git_commit": "abc",
                        "schema_version": 50,
                    },
                }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.path == "/health":
                resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "manifest_id": state.get("manifest_id", "mfst-0"),
                    "pid": os.getpid(),
                    "endpoint": f"http://127.0.0.1:{state['port']}",
                    "security_profile": HTTP_MVP_TRANSPORT_PROFILE,
                    "git_commit": "abc",
                    "schema_version": 50,
                    "capability_registry_revision": 1,
                    "worker_status": "ready",
                }
            elif self.path == "/capabilities":
                resp = {
                    "protocol_version": "1",
                    "server_mode": HTTP_MVP_TRANSPORT_PROFILE,
                    "methods": {
                        "query.file": {"backend": "rust_native", "status": "available"},
                        "unknown.method": {"backend": "none", "status": "unsupported"},
                    },
                }
            else:
                resp = {}
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        def log_message(self, *args):
            pass

        def log_error(self, *args):
            pass

    return Handler


@pytest.fixture
def fake_factory():
    servers = []

    def make(sleep_seconds=0.0, error=False, manifest_id="mfst-0001"):
        state = {
            "last_request": None,
            "last_path": None,
            "port": 0,
            "sleep": sleep_seconds,
            "error": error,
            "manifest_id": manifest_id,
        }
        handler = _make_handler(state)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        srv.daemon_threads = True
        port = srv.server_address[1]
        state["port"] = port
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv, f"http://127.0.0.1:{port}", state

    yield make
    for s in servers:
        try:
            s.shutdown()
            s.server_close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# 测试
# ----------------------------------------------------------------------

def test_raw_outgoing_envelope(fake_factory):
    srv, url, state = fake_factory()
    client = HttpDaemonRpcClient(endpoint=url, verify_health=False)
    result = client.call("query.file", {"file_path": "src/x.py"})

    req = state["last_request"]
    assert req["jsonrpc"] == "2.0"
    assert req["id"] == client.last_request_id
    assert req["protocol_version"] == "1"
    assert req["method"] == "query.file"
    assert req["params"] == {"file_path": "src/x.py"}
    assert state["last_path"] == "/v1/rpc"
    assert result["ok"] is True
    assert result["method"] == "query.file"


def test_request_id_reuse(fake_factory):
    srv, url, state = fake_factory()
    client = HttpDaemonRpcClient(endpoint=url, verify_health=False)
    client.call("task.claim", {"task_id": "T1"}, request_id="fixed-rid-123")
    first = state["last_request"]["id"]
    client.call("task.claim", {"task_id": "T1"}, request_id="fixed-rid-123")
    second = state["last_request"]["id"]
    assert first == second == "fixed-rid-123"


def test_request_id_auto_generated_unique(fake_factory):
    srv, url, state = fake_factory()
    client = HttpDaemonRpcClient(endpoint=url, verify_health=False)
    client.call("query.file", {"file_path": "a"})
    a = state["last_request"]["id"]
    client.call("query.file", {"file_path": "b"})
    b = state["last_request"]["id"]
    assert a != b


def test_remote_error_preservation(fake_factory):
    srv, url, state = fake_factory(error=True)
    client = HttpDaemonRpcClient(endpoint=url, verify_health=False)
    with pytest.raises(DaemonRemoteError) as exc:
        client.call("query.file", {"file_path": "x"})
    assert exc.value.code == "E_SNAPSHOT_NOT_READY"
    assert exc.value.message == "snapshot is not ready"


def test_timeout_fails_closed(fake_factory):
    srv, url, state = fake_factory(sleep_seconds=2.0)
    client = HttpDaemonRpcClient(endpoint=url, verify_health=False, timeout=0.5)
    with pytest.raises(DaemonUnavailableError) as exc:
        client.call("__sleep__", {})
    assert "E_HTTP_REQUEST_TIMEOUT" in str(exc.value)


def test_discovery_via_manifest_file_end_to_end(fake_factory, tmp_path, monkeypatch):
    srv, url, state = fake_factory(manifest_id="mfst-live-1")
    manifest = {
        "manifest_version": HTTP_MANIFEST_SCHEMA_VERSION,
        "manifest_id": "mfst-live-1",
        "authority_id": "e2e-authority",
        "endpoint": url,
        "pid": None,
        "process_start_time": 0,
        "daemon_executable": "",
        "daemon_binary_sha256": "deadbeef",
        "protocol_version": "1",
        "supported_protocol_versions": ["1"],
        "security_profile": HTTP_MVP_TRANSPORT_PROFILE,
        "git_commit": "abc",
        "schema_version": 50,
        "started_at": "2026-08-14T00:00:00Z",
        "capability_registry_revision": 1,
        "worker_status": "ready",
    }
    manifest["manifest_hash"] = compute_http_manifest_hash(manifest)
    mpath = tmp_path / "http.manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        "callwarden.config.get_default_http_manifest_path",
        lambda authority_id: "/nonexistent/http.manifest.json",
    )
    client = HttpDaemonRpcClient(
        manifest_path=str(mpath), authority_id="e2e-authority", verify_health=True
    )
    ep = client.discover()
    assert ep == url.rstrip("/")
    # 端到端调用成功，且 /health 交叉核对通过
    result = client.call("query.file", {"file_path": "x"})
    assert result["ok"] is True


def test_health_and_capabilities(fake_factory):
    srv, url, state = fake_factory()
    client = HttpDaemonRpcClient(endpoint=url, verify_health=False)
    health = client.health()
    assert health["security_profile"] == HTTP_MVP_TRANSPORT_PROFILE
    caps = client.capabilities()
    assert caps["server_mode"] == HTTP_MVP_TRANSPORT_PROFILE
    assert caps["methods"]["query.file"]["backend"] == "rust_native"


# ----------------------------------------------------------------------
# H2 factory 接线 + no-fallback（production-selection，frozen contract §2.1/§9）
# ----------------------------------------------------------------------

def test_production_factory_selects_http_client(monkeypatch):
    """H2 P0：CW_DAEMON_TRANSPORT=http 时 production factory 返回 HTTP thin client。"""
    from callwarden.server.daemon_client import (
        HttpDaemonRpcClient,
        get_daemon_client,
    )

    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "http")
    HttpDaemonRpcClient.reset_instance()
    client = get_daemon_client()
    assert client.is_http_client is True
    assert isinstance(client, HttpDaemonRpcClient)


def test_production_factory_default_legacy(monkeypatch):
    """非 HTTP 模式下 factory 返回 legacy DaemonClient（is_http_client=False）。"""
    from callwarden.server.daemon_client import DaemonClient, get_daemon_client

    monkeypatch.delenv("CW_DAEMON_TRANSPORT", raising=False)
    DaemonClient.reset_instance()
    client = get_daemon_client()
    assert client.is_http_client is False
    assert isinstance(client, DaemonClient)


def test_http_client_has_no_sqlite_fallback():
    """H2 P0：HTTP client 不含 SQLite/CodeGraphDB fallback，连接失败 fail-closed。

    对应 frozen contract §9 负向矩阵：HTTP client 任意错误时尝试
    CodeGraphDB/sqlite/Named Pipe/UDS fallback 即测试失败并 BLOCKED。
    """
    client = HttpDaemonRpcClient(
        endpoint="http://127.0.0.1:1", verify_health=False, timeout=2.0
    )
    # 结构上不含 SQL fallback 入口
    assert not hasattr(client, "_get_db")
    assert not hasattr(client, "_svc")
    # 连接失败必须抛 DaemonUnavailableError（fail-closed），而非回退 SQLite/Named Pipe/UDS
    with pytest.raises(DaemonUnavailableError):
        client.call("query.file", {"file_path": "x"})
