"""M4 CLI fail-closed 验证（PRD §3.2 M4 + 设计 §4.4 场景 D）。

验证目标：
- daemon 不可用时 CLI 明确报错（DaemonUnavailableError / E_HTTP_DAEMON_UNAVAILABLE），
  绝不降级本地 SQLite 执行；
- local/legacy 模式仅 CW_TEST_MODE=1 下可用，生产环境视为配置错误（E_MODE_DEPRECATED）；
- cli/dispatcher.call_daemon 失败时抛结构化错误（带 E_HTTP_DAEMON_UNAVAILABLE 码）。

方法：用无效 endpoint 环境变量模拟 daemon 不可达（不破坏运行中的生产 daemon）。
注意：daemon mode 由 `CW_DAEMON_MODE` 读取（config.get_daemon_mode），
transport 由 `CW_DAEMON_TRANSPORT` 读取（is_http_transport_enabled），两者分开。
"""
from __future__ import annotations

import os
import sys

import pytest

from callwarden.cli.dispatcher import CliDispatcher, DaemonUnavailableError, call_daemon
from callwarden.server.daemon_protocol import DaemonRemoteError


@pytest.fixture(autouse=True)
def _reset_http_singleton():
    """每个用例结束后重置 HttpDaemonRpcClient 单例，避免跨测试缓存污染。

    M4 合跑回归（2026-08）：`test_route_rpc_local_mode_allowed_with_test_mode`
    在 HTTP 默认开启（未显式设 CW_DAEMON_TRANSPORT 时 is_http_transport_enabled()
    =True）下走 HTTP 分支，`HttpDaemonRpcClient.get_instance()` 会创建单例并
    缓存已解析的真实 daemon 端点；后续 fail-closed 用例即便 monkeypatch 死端点
    也命中缓存，收到真实 daemon 的结构化业务错误（snapshot_not_ready）而非
    连接错误。autouse 重置保证每个用例从干净单例出发。
    """
    yield
    from callwarden.server.daemon_client import HttpDaemonRpcClient

    HttpDaemonRpcClient.reset_instance()


class TestCliFailClosed:
    @staticmethod
    def _assert_fail_closed(exc) -> None:
        """断言异常为 fail-closed：不降级本地执行。

        接受两类结构化拒绝（均不触碰本地 SQLite）：
        - ``E_HTTP_DAEMON_UNAVAILABLE``：连接失败（DaemonUnavailableError）
        - ``E_HTTP_MANIFEST_STALE``：manifest 校验失败/daemon 不可信
          （DaemonRemoteError；测试机残留真实 daemon manifest 时，死端点
          会先命中 stale manifest 校验，错误码不同于连接失败，但同样拒绝执行）
        """
        assert isinstance(exc, RuntimeError), f"应为运行时错误: {exc!r}"
        text = str(exc)
        assert ("E_HTTP_" in text) or ("无法连接" in text), \
            f"应为 E_HTTP_* fail-closed 错误，实际: {text}"

    def test_call_daemon_unreachable_raises(self, monkeypatch):
        """daemon 不可达 → DaemonUnavailableError（fail-closed，不降级本地执行）。"""
        monkeypatch.setenv("CW_DAEMON_MODE", "http")
        monkeypatch.setenv("CW_DAEMON_HTTP_ENDPOINT", "http://127.0.0.1:1")  # 无效端口
        monkeypatch.delenv("CW_DAEMON_ENDPOINT", raising=False)
        monkeypatch.delenv("CW_TEST_MODE", raising=False)
        with pytest.raises(Exception) as ei:
            call_daemon("query.stats", {})
        # fail-closed：必须抛异常（连接失败/不可信），绝不返回结果/降级本地
        assert isinstance(ei.value, (DaemonUnavailableError, RuntimeError))
        self._assert_fail_closed(ei.value)

    def test_cli_dispatcher_unreachable_raises(self, monkeypatch):
        """CliDispatcher.dispatch 未注册/daemon 不可达 → 明确报错。"""
        monkeypatch.setenv("CW_DAEMON_MODE", "http")
        monkeypatch.setenv("CW_DAEMON_HTTP_ENDPOINT", "http://127.0.0.1:1")
        monkeypatch.delenv("CW_DAEMON_ENDPOINT", raising=False)
        monkeypatch.delenv("CW_TEST_MODE", raising=False)
        d = CliDispatcher()
        d.register("stats", "query.stats")
        with pytest.raises(Exception) as ei:
            d.dispatch("stats", {})
        self._assert_fail_closed(ei.value)

    def test_cli_dispatcher_unknown_subcommand_raises(self):
        """未注册子命令 → KeyError（不是静默降级本地执行）。"""
        d = CliDispatcher()
        with pytest.raises(KeyError):
            d.dispatch("nonexistent-subcommand", {})

    def test_route_rpc_local_mode_deprecated_without_test_mode(self, monkeypatch):
        """CW_DAEMON_MODE=local 且无 CW_TEST_MODE → E_MODE_DEPRECATED。"""
        monkeypatch.setenv("CW_DAEMON_MODE", "local")
        monkeypatch.delenv("CW_TEST_MODE", raising=False)
        from callwarden.server.daemon_client import route_rpc, DaemonUnavailableError as SvrDUE
        with pytest.raises(SvrDUE) as ei:
            route_rpc("query.stats", {}, "READ_ONLY")
        assert "E_MODE_DEPRECATED" in str(ei.value)

    def test_route_rpc_local_mode_allowed_with_test_mode(self, monkeypatch, tmp_path):
        """CW_TEST_MODE=1 时 local 允许（测试专用，Q3 决策），不抛模式错误。"""
        monkeypatch.setenv("CW_DAEMON_MODE", "local")
        monkeypatch.setenv("CW_TEST_MODE", "1")
        # 显式关闭 HTTP transport：本用例只验证"local 模式校验不再拦截"，
        # 不应走 HTTP 分支创建 HttpDaemonRpcClient 单例/连接真实 daemon
        # （否则单例缓存会污染后续 fail-closed 用例的端点 monkeypatch）。
        monkeypatch.setenv("CW_DAEMON_TRANSPORT", "named-pipe")
        # local 模式走本地回退，不应抛 E_MODE_DEPRECATED；具体执行结果取决于本地 db，
        # 这里仅验证"模式校验不再拦截"（后续调用可能因缺 workspace 报业务错，均非模式错）。
        from callwarden.server.daemon_client import route_rpc
        try:
            route_rpc("query.stats", {}, "READ_ONLY")
        except Exception as exc:
            assert "E_MODE_DEPRECATED" not in str(exc), f"不应抛模式废弃错误: {exc}"

    def test_route_rpc_fail_closed_never_falls_back_local(self, monkeypatch):
        """daemon 不可达时 route_rpc 抛错而非回退本地 SQLite（HTTP 模式语义）。"""
        monkeypatch.setenv("CW_DAEMON_MODE", "http")
        monkeypatch.setenv("CW_DAEMON_HTTP_ENDPOINT", "http://127.0.0.1:1")
        monkeypatch.delenv("CW_DAEMON_ENDPOINT", raising=False)
        monkeypatch.delenv("CW_TEST_MODE", raising=False)
        from callwarden.server.daemon_client import route_rpc
        with pytest.raises(Exception) as ei:
            route_rpc("query.stats", {}, "READ_ONLY")
        self._assert_fail_closed(ei.value)
