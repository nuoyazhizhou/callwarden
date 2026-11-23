"""stale/missing manifest → autostart 自愈单测（mock）。

覆盖 T-1795400631648-aef0ca64：daemon 被杀后 manifest 仍指向死 PID，
validate_http_manifest fail-closed 抛 E_HTTP_MANIFEST_STALE /
E_HTTP_MANIFEST_MISSING（DaemonRemoteError）。修复前该错误直接冒泡到
CLI/MCP 退出，自动拉起路径从未执行；修复后在
HttpDaemonRpcClient.call_with_autostart 中映射为 daemon 不可用并进入
ensure/spawn 自愈（不连接 stale endpoint、不改删 manifest）。
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from callwarden.config import (
    E_HTTP_DAEMON_UNAVAILABLE,
    E_HTTP_MANIFEST_MISSING,
    E_HTTP_MANIFEST_STALE,
)
from callwarden.server.daemon_client import (
    DaemonRemoteError,
    DaemonUnavailableError,
    HttpDaemonRpcClient,
)
from callwarden.server.daemon_protocol import DaemonRemoteError as _ProtoRemoteError

assert _ProtoRemoteError is DaemonRemoteError


def _make_client():
    """构造不做任何网络/manifest 副作用的 HTTP client（后续全部 mock）。"""
    return HttpDaemonRpcClient(
        endpoint="https://127.0.0.1:1",
        verify_health=False,
        validate_manifest=False,
    )


def _patch_selfheal(monkeypatch, *, ensure_result, spawn_result, mutex_held=True):
    """mock 自愈依赖（DaemonMutex / ensure_http_daemon / spawn_http_daemon）。"""
    mutex = mock.Mock()
    mutex.try_acquire.return_value = mutex_held
    monkeypatch.setattr(
        "callwarden.server.daemon_client.DaemonMutex", lambda endpoint: mutex
    )
    ensure = mock.Mock(return_value=ensure_result)
    spawn = mock.Mock(return_value=spawn_result)
    monkeypatch.setattr(
        "callwarden.server.daemon_client.ensure_http_daemon", ensure
    )
    monkeypatch.setattr(
        "callwarden.server.daemon_client.spawn_http_daemon", spawn
    )
    return mutex, ensure, spawn


def test_stale_manifest_maps_to_autostart_and_retries(monkeypatch):
    """stale manifest → 映射为 daemon 不可用 → ensure 就绪后重试原请求成功。"""
    client = _make_client()
    stale = DaemonRemoteError(E_HTTP_MANIFEST_STALE, "manifest PID 123 已不存活")
    ok_result = {"tasks": [], "ok": True}
    client.call = mock.Mock(side_effect=[stale, ok_result])
    client.discover = mock.Mock(return_value="https://127.0.0.1:9100")
    client.reset_endpoint_cache = mock.Mock()
    mutex, ensure, spawn = _patch_selfheal(
        monkeypatch, ensure_result="https://127.0.0.1:9100", spawn_result=None
    )

    result = client.call_with_autostart("task.list", {})

    assert result == ok_result
    assert client.call.call_count == 2
    assert spawn.call_count == 0, "ensure 已就绪时不应重复 spawn"
    assert client.reset_endpoint_cache.call_count >= 1
    mutex.release.assert_called_once()


def test_missing_manifest_maps_to_autostart_and_retries(monkeypatch):
    """missing manifest → 同样映射为 daemon 不可用并进入自愈。"""
    client = _make_client()
    missing = DaemonRemoteError(
        E_HTTP_MANIFEST_MISSING, "manifest 不存在"
    )
    ok_result = {"ok": True}
    client.call = mock.Mock(side_effect=[missing, ok_result])
    client.discover = mock.Mock(return_value="https://127.0.0.1:9101")
    client.reset_endpoint_cache = mock.Mock()
    _patch_selfheal(
        monkeypatch, ensure_result="https://127.0.0.1:9101", spawn_result=None
    )

    result = client.call_with_autostart("workspace.status", {})

    assert result == ok_result
    assert client.call.call_count == 2


def test_stale_manifest_spawns_daemon_when_ensure_fails(monkeypatch):
    """ensure 窗口内未就绪（daemon 未运行）→ spawn_http_daemon 显式拉起。"""
    client = _make_client()
    stale = DaemonRemoteError(E_HTTP_MANIFEST_STALE, "manifest PID 123 已不存活")
    ok_result = {"ok": True}
    client.call = mock.Mock(side_effect=[stale, ok_result])
    client.discover = mock.Mock(return_value="")
    client.reset_endpoint_cache = mock.Mock()
    _patch_selfheal(
        monkeypatch, ensure_result=None, spawn_result="https://127.0.0.1:9200"
    )

    result = client.call_with_autostart("task.list", {})

    assert result == ok_result
    assert client.call.call_count == 2


def test_business_error_not_autostarted(monkeypatch):
    """业务错误（task_not_found 等）原样透传，绝不触发自愈。"""
    client = _make_client()
    biz = DaemonRemoteError("E_TASK_NOT_FOUND", "task not found")
    client.call = mock.Mock(side_effect=biz)
    client.discover = mock.Mock(return_value="https://127.0.0.1:9100")
    client.reset_endpoint_cache = mock.Mock()
    _patch_selfheal(
        monkeypatch, ensure_result="https://127.0.0.1:9100", spawn_result=None
    )

    try:
        client.call_with_autostart("task.get", {"task_id": "x"})
        raise AssertionError("业务错误必须原样冒泡")
    except DaemonRemoteError as exc:
        assert exc.code == "E_TASK_NOT_FOUND"
    assert client.reset_endpoint_cache.call_count == 0


def test_autostart_failure_fails_closed(monkeypatch):
    """ensure 与 spawn 均未就绪 → fail-closed 抛 DaemonUnavailableError。"""
    client = _make_client()
    stale = DaemonRemoteError(E_HTTP_MANIFEST_STALE, "manifest PID 123 已不存活")
    client.call = mock.Mock(side_effect=stale)
    client.discover = mock.Mock(return_value="")
    client.reset_endpoint_cache = mock.Mock()
    _patch_selfheal(monkeypatch, ensure_result=None, spawn_result=None)

    try:
        client.call_with_autostart("task.list", {})
        raise AssertionError("自愈失败必须 fail-closed")
    except DaemonUnavailableError as exc:
        assert E_HTTP_DAEMON_UNAVAILABLE in str(exc)


def test_connection_error_maps_to_autostart(monkeypatch):
    """DaemonUnavailableError（连接失败）沿用既有自愈语义。"""
    client = _make_client()
    ok_result = {"ok": True}
    client.call = mock.Mock(
        side_effect=[DaemonUnavailableError("connection refused"), ok_result]
    )
    client.discover = mock.Mock(return_value="https://127.0.0.1:9100")
    client.reset_endpoint_cache = mock.Mock()
    _patch_selfheal(
        monkeypatch, ensure_result="https://127.0.0.1:9100", spawn_result=None
    )

    result = client.call_with_autostart("health", {})
    assert result == ok_result


def test_mcp_runtime_call_uses_autostart(monkeypatch):
    """MCP 运行期 _call_daemon_rpc 对 HTTP client 走 call_with_autostart 自愈。"""
    from callwarden.server import _mcp_common

    fake_client = mock.Mock()
    fake_client.is_http_client = True
    fake_client.call_with_autostart = mock.Mock(return_value={"ok": True})
    monkeypatch.setattr(
        "callwarden.server.daemon_client.get_daemon_client",
        lambda: fake_client,
    )

    result = _mcp_common._call_daemon_rpc("task.list", {})

    assert result == {"ok": True}
    fake_client.call_with_autostart.assert_called_once_with("task.list", {})
    assert not hasattr(fake_client, "call") or fake_client.call.call_count == 0


def test_mcp_runtime_call_legacy_client_keeps_call(monkeypatch):
    """legacy client（无 is_http_client 标志）仍走 .call，不改变既有语义。"""
    from callwarden.server import _mcp_common

    fake_client = mock.Mock(spec=["call"])
    fake_client.call = mock.Mock(return_value={"ok": True})
    monkeypatch.setattr(
        "callwarden.server.daemon_client.get_daemon_client",
        lambda: fake_client,
    )

    result = _mcp_common._call_daemon_rpc("task.list", {})

    assert result == {"ok": True}
    fake_client.call.assert_called_once_with("task.list", {})


def test_mcp_startup_resets_singleton_cache_after_spawn(monkeypatch):
    """MCP 启动期 spawn 成功后必须清共享单例的 endpoint 缓存。"""
    singleton = mock.Mock()
    fake_client = mock.Mock()
    fake_client.health.side_effect = RuntimeError("daemon unreachable")

    class _FakeClientClass:
        def __init__(self, *args, **kwargs):
            pass

        def health(self):
            return fake_client.health()

        @classmethod
        def get_instance(cls):
            return singleton

    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "http")
    monkeypatch.setattr(
        "callwarden.server.daemon_client.is_http_transport_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "callwarden.server.daemon_client.HttpDaemonRpcClient", _FakeClientClass
    )
    monkeypatch.setattr(
        "callwarden.server.daemon_autostart.spawn_http_daemon",
        lambda **kwargs: "https://127.0.0.1:8443",
    )

    from callwarden.server.mcp_server import _start_daemon_for_mcp_startup

    _start_daemon_for_mcp_startup()

    singleton.reset_endpoint_cache.assert_called_once()
