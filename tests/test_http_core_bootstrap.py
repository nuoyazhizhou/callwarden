"""H4A: HTTP core bootstrap 自举测试

验证 MCP/CLI 核心方法在 HTTP 模式下通过 HttpDaemonRpcClient 调用，
不直连 SQLite，daemon 不可用时 fail-closed。
"""

import os
import sys
import json
from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    _get_rpc_client_for_route,
    route_task_write,
    route_task_read,
)


# ============================================================
# 辅助：Mock HTTP daemon server
# ============================================================


class MockHttpResponse:
    """模拟 urllib.request.urlopen 的 HTTP 响应。"""

    def __init__(self, data: bytes, status: int = 200):
        self._data = data
        self.status = status

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _echo_request_id(urlopen_mock, *, result=None, results_by_method=None,
                     error=None, status: int = 200) -> None:
    """让 mock urlopen 从请求体回显 id。

    stale 修正：server/daemon_client.py:2304-2308 现生成全局唯一 request id
    （显式参数 / params["request_id"] / uuid4），并在 :2367-2370 **严格校验**
    响应 id 必须等于出向 id。旧的静态 id="1" 响应会触发
    `daemon 响应 request id 不匹配`。故 mock 必须回显请求信封的 id。

    results_by_method：按请求 method 返回不同 result（如 get_stats 先
    workspace.register 再 query.stats）。
    """
    def _side_effect(req, *args, **kwargs):
        rid = "1"
        method = ""
        data = getattr(req, "data", None)
        if isinstance(data, (bytes, bytearray)):
            try:
                envelope = json.loads(bytes(data).decode("utf-8"))
                rid = envelope.get("id", "1")
                method = envelope.get("method", "")
            except (ValueError, UnicodeDecodeError, AttributeError):
                rid = "1"
        payload = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            payload["error"] = error
        elif results_by_method is not None:
            payload["result"] = results_by_method.get(method)
        else:
            payload["result"] = result
        return MockHttpResponse(json.dumps(payload).encode("utf-8"), status=status)

    urlopen_mock.side_effect = _side_effect


# ============================================================
# 辅助：Mock HttpDaemonRpcClient 的 discover 方法
# ============================================================


def _make_discovered_client(timeout=1.0):
    """创建一个已 mock discover 的 HttpDaemonRpcClient 实例。"""
    from callwarden.server.daemon_client import HttpDaemonRpcClient
    client = HttpDaemonRpcClient(timeout=timeout, verify_health=False, validate_manifest=False)
    # 直接设置 _resolved_endpoint 绕过 discover()
    client._resolved_endpoint = "http://127.0.0.1:9999"
    client._manifest = {"manifest_id": "test", "pid": 12345}
    return client


# ============================================================
# 1. HttpDaemonRpcClient 基础测试
# ============================================================


class TestHttpDaemonRpcClient:
    """测试 HttpDaemonRpcClient 的基本 RPC 调用。"""

    @patch("urllib.request.urlopen")
    def test_health_ok(self, mock_urlopen):
        """health() 返回 daemon 健康状态。"""
        # health() 调用 _http_get() 走 GET /health，返回裸 JSON（非 JSON-RPC 信封）
        health_body = json.dumps({"status": "ok", "version": "1.0"}).encode("utf-8")
        mock_urlopen.return_value = MockHttpResponse(health_body, status=200)
        client = _make_discovered_client()
        resp = client.health()
        assert resp["status"] == "ok"

    @patch("urllib.request.urlopen")
    def test_call_ok(self, mock_urlopen):
        """call() 成功调用 RPC 方法。"""
        _echo_request_id(mock_urlopen, result={"workspaces": ["ws1", "ws2"]})
        client = _make_discovered_client()
        resp = client.call("workspace.list", {})
        assert resp == {"workspaces": ["ws1", "ws2"]}

    @patch("urllib.request.urlopen")
    def test_call_error(self, mock_urlopen):
        """call() 透传业务错误。"""
        _echo_request_id(mock_urlopen, error={
            "code": -32000, "message": "test error", "data": {"code": "E_TEST"},
        })
        client = _make_discovered_client()
        from callwarden.server.daemon_protocol import DaemonRemoteError
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.list", {})
        assert "E_TEST" in str(exc.value)

    @patch("urllib.request.urlopen")
    def test_call_fail_closed(self, mock_urlopen):
        """daemon 不可用时 fail-closed（不静默回退）。"""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        client = _make_discovered_client()
        with pytest.raises(Exception):
            client.call("workspace.list", {})

    @patch("urllib.request.urlopen")
    def test_get_stats(self, mock_urlopen):
        """get_stats() 通过 HTTP 返回统计数据。"""
        # get_stats 先 _ensure_remote_snapshot → workspace.register，再 query.stats
        _echo_request_id(mock_urlopen, results_by_method={
            "workspace.register": {"workspace_instance_id": "ws-1"},
            "query.stats": {"files": 100, "functions": 500, "calls": 2000},
        })
        client = _make_discovered_client()
        resp = client.get_stats()
        assert resp["files"] == 100

    @patch("urllib.request.urlopen")
    def test_search_symbols(self, mock_urlopen):
        """search_symbols() 通过 HTTP 返回符号搜索结果。"""
        _echo_request_id(mock_urlopen, result=[
            {"name": "test_fn", "kind": "function", "file": "test.py"},
        ])
        client = _make_discovered_client()
        resp = client.search_symbols("test_fn")
        assert len(resp) == 1
        assert resp[0]["name"] == "test_fn"

    @patch("urllib.request.urlopen")
    def test_list_workspaces(self, mock_urlopen):
        """list_workspaces() 通过 HTTP 返回工作区列表。"""
        _echo_request_id(mock_urlopen, result=[
            {"id": 1, "name": "ws1", "root_path": "/path/ws1"},
        ])
        client = _make_discovered_client()
        resp = client.list_workspaces()
        assert len(resp) == 1
        assert resp[0]["name"] == "ws1"


# ============================================================
# 2. 路由函数测试
# ============================================================


class TestRouteFunctions:
    """测试 _get_rpc_client_for_route / route_task_write / route_task_read。"""

    def test_get_rpc_client_http_mode(self, monkeypatch):
        """HTTP 模式下返回 HttpDaemonRpcClient 实例。"""
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: True,
        )
        client = _get_rpc_client_for_route()
        assert isinstance(client, HttpDaemonRpcClient)

    def test_get_rpc_client_legacy_mode(self, monkeypatch):
        """legacy 模式下返回 UnixDaemonRpcClient 实例。"""
        from callwarden.server.daemon_client import UnixDaemonRpcClient
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: False,
        )
        client = _get_rpc_client_for_route()
        assert isinstance(client, UnixDaemonRpcClient)

    def test_route_task_write_local(self, monkeypatch):
        """local 模式下 route_task_write 执行 fallback。"""
        monkeypatch.setattr("callwarden.server.daemon_client.get_daemon_mode", lambda: "local")
        monkeypatch.setattr("callwarden.server.daemon_client.get_task_write_policy", lambda: "isolated")
        called = []

        def fallback():
            called.append(True)
            return {"ok": True}

        result = route_task_write("task.list", {}, fallback)
        assert called == [True]
        assert result["ok"] is True

    def test_route_task_read_local(self, monkeypatch):
        """local 模式下 route_task_read 执行 fallback。"""
        monkeypatch.setattr("callwarden.server.daemon_client.get_daemon_mode", lambda: "local")
        called = []

        def fallback():
            called.append(True)
            return {"ok": True}

        result = route_task_read("task.list", {}, fallback)
        assert called == [True]
        assert result["ok"] is True


# ============================================================
# 3. 不直连 SQLite 验证
# ============================================================


class TestNoSqliteInHttpMode:
    """HTTP 模式下不直连 SQLite。"""

    def test_http_client_no_sqlite_import(self):
        """HttpDaemonRpcClient 不导入 sqlite3 / CodeGraphDB（仅检查非注释代码）。"""
        import inspect
        source = inspect.getsource(HttpDaemonRpcClient)
        # 只检查函数体（非 docstring）中的引用
        lines = source.split('\n')
        code_lines = []
        in_docstring = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            if stripped and not stripped.startswith('#'):
                code_lines.append(stripped)
        code_body = '\n'.join(code_lines)
        assert "sqlite3.connect" not in code_body, "HttpDaemonRpcClient 不应包含 sqlite3.connect（非注释代码）"
        assert "CodeGraphDB" not in code_body, "HttpDaemonRpcClient 不应引用 CodeGraphDB（非注释代码）"

    @patch("urllib.request.urlopen")
    def test_route_task_write_http_fail_closed(self, mock_urlopen, monkeypatch):
        """HTTP 模式下 route_task_write 失败时 fail-closed，不降级 fallback。"""
        import urllib.error
        monkeypatch.setattr("callwarden.server.daemon_client.get_daemon_mode", lambda: "enterprise")
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: True,
        )
        # Mock HttpDaemonRpcClient.get_instance to return a pre-discovered client
        mock_client = _make_discovered_client()
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        monkeypatch.setattr(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            lambda: mock_client,
        )

        from callwarden.server.daemon_client import DaemonUnavailableError
        with pytest.raises(DaemonUnavailableError):
            route_task_write("task.create", {"title": "test"}, lambda: {"ok": True})


# ============================================================
# 4. 单例模式验证
# ============================================================


class TestHttpClientSingleton:
    """HttpDaemonRpcClient 单例模式。"""

    def test_singleton(self):
        """get_instance() 返回同一实例。"""
        try:
            a = HttpDaemonRpcClient.get_instance()
            b = HttpDaemonRpcClient.get_instance()
            assert a is b
        finally:
            HttpDaemonRpcClient.reset_instance()

    def test_reset_instance(self):
        """reset_instance() 后 get_instance() 返回新实例。"""
        try:
            a = HttpDaemonRpcClient.get_instance()
            HttpDaemonRpcClient.reset_instance()
            b = HttpDaemonRpcClient.get_instance()
            assert a is not b
        finally:
            HttpDaemonRpcClient.reset_instance()