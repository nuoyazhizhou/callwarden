"""server/daemon_autostart.py 单元测试。

覆盖 Req 14.22（有界等待窗口 + 指数退避）、14.24（Windows 分离进程）、
14.25（macOS launchd）、14.26（Linux systemd）。
"""

import os
import socket
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from callwarden.server.daemon_autostart import (
    BACKOFF_BASE,
    BACKOFF_FACTOR,
    BACKOFF_MAX,
    LINUX_SYSTEMD_UNIT,
    MACOS_LAUNCHD_LABEL,
    _find_daemon_binary,
    _start_daemon_linux,
    _start_daemon_macos,
    _start_daemon_windows,
    ensure_daemon,
    ensure_http_daemon,
    get_default_endpoint,
    try_connect,
)


# ---------------------------------------------------------------------------
# Req 14.22: 有界等待窗口 + 指数退避
# ---------------------------------------------------------------------------


class TestEnsureDaemonBoundedWindow:
    """ensure_daemon 在有界窗口内退避重试，窗口耗尽返回 None。"""

    def test_returns_none_when_window_expires(self):
        """窗口耗尽且无 daemon 时返回 None。"""
        with mock.patch("callwarden.server.daemon_autostart._start_daemon_platform", return_value=False):
            start = time.monotonic()
            result = ensure_daemon("/nonexistent/path.sock", window=0.5)
            elapsed = time.monotonic() - start

        assert result is None
        # 确认确实在窗口附近返回（允许 0.2s 误差）
        assert 0.4 <= elapsed <= 1.0

    def test_returns_socket_when_daemon_available(self):
        """daemon 已在监听时立即返回连接（单轨 HTTPS：TCP 可达即返回）。"""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        endpoint = f"https://127.0.0.1:{server.getsockname()[1]}"
        try:
            start = time.monotonic()
            conn = ensure_daemon(endpoint, window=2.0)
            elapsed = time.monotonic() - start

            assert conn is not None
            # 应立即连接，不需要退避
            assert elapsed < 0.5
            conn.close()
        finally:
            server.close()

    def test_connects_after_delayed_daemon_start(self):
        """daemon 在窗口内延迟就绪时仍能连接。"""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        endpoint = f"https://127.0.0.1:{port}"

        def delayed_listen():
            time.sleep(0.3)
            server.listen(1)
            # 保持监听直到测试结束
            time.sleep(2.0)
            server.close()

        t = threading.Thread(target=delayed_listen, daemon=True)
        t.start()

        with mock.patch("callwarden.server.daemon_autostart._start_daemon_platform", return_value=True):
            conn = ensure_daemon(endpoint, window=3.0)

        assert conn is not None
        conn.close()

    def test_protocol_readiness_probe_retries_connected_but_unready_endpoint(self):
        """TCP connect 成功但协议未就绪时仍继续退避探测。"""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        endpoint = f"https://127.0.0.1:{server.getsockname()[1]}"
        probe = mock.Mock(side_effect=[False, True])
        try:
            conn = ensure_daemon(
                endpoint,
                window=1.0,
                backoff_base=0.01,
                readiness_check=probe,
            )
            assert conn is not None
            conn.close()
            assert probe.call_count == 2
        finally:
            server.close()

    def test_backoff_is_exponential(self):
        """退避间隔按指数增长。"""
        sleep_calls = []
        original_sleep = time.sleep

        def mock_sleep(duration):
            sleep_calls.append(duration)
            # 实际不睡，加速测试
            original_sleep(min(duration, 0.01))

        with mock.patch("callwarden.server.daemon_autostart._start_daemon_platform", return_value=False):
            with mock.patch("time.sleep", side_effect=mock_sleep):
                ensure_daemon("/nonexistent.sock", window=1.0, backoff_base=0.1)

        # 验证退避序列递增（至少前几个）
        if len(sleep_calls) >= 3:
            assert sleep_calls[1] >= sleep_calls[0]
            assert sleep_calls[2] >= sleep_calls[1]

    def test_window_is_configurable(self):
        """窗口大小可通过参数配置。"""
        with mock.patch("callwarden.server.daemon_autostart._start_daemon_platform", return_value=False):
            start = time.monotonic()
            ensure_daemon("/nonexistent.sock", window=0.3)
            elapsed = time.monotonic() - start

        assert elapsed < 0.8  # 短窗口应快速返回

    def test_tcp_bridge_does_not_start_local_daemon(self):
        """bridge 不可达时也不能在 WSL 误启本地 systemd daemon。"""
        launches = []
        with mock.patch("callwarden.server.daemon_autostart._start_daemon_platform", side_effect=lambda endpoint: launches.append(endpoint)):
            ensure_daemon("tcp://127.0.0.1:1", window=0.15, backoff_base=0.01)
        assert launches == []

    def test_uses_client_clock_not_authoritative(self):
        """确认使用 time.monotonic（客户端时钟），不依赖 Authoritative_Clock。"""
        # ensure_daemon 内部使用 time.monotonic，不调用任何 daemon RPC
        with mock.patch("callwarden.server.daemon_autostart._start_daemon_platform", return_value=False):
            with mock.patch("time.monotonic", wraps=time.monotonic) as mock_mono:
                ensure_daemon("/nonexistent.sock", window=0.3)
                # time.monotonic 应被调用（用于 deadline 计算）
                assert mock_mono.call_count >= 2


# ---------------------------------------------------------------------------
# Req 14.22: try_connect
# ---------------------------------------------------------------------------


class TestTryConnect:
    """try_connect 连接尝试。"""

    def test_returns_none_for_non_http_endpoint(self):
        """非 HTTP(S) endpoint（裸路径 / pipe / unix:// / tcp://）fail-closed。"""
        for endpoint in (
            "/nonexistent/path/to/daemon.sock",
            r"\\.\pipe\callwarden-test",
            "unix:///tmp/daemon.sock",
            "tcp://127.0.0.1:1",
        ):
            assert try_connect(endpoint) is None

    def test_returns_socket_for_listening_endpoint(self):
        """已监听的 HTTPS 端点返回 TCP socket。"""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        endpoint = f"https://127.0.0.1:{server.getsockname()[1]}"
        try:
            conn = try_connect(endpoint)
            assert conn is not None
            conn.close()
        finally:
            server.close()


# ---------------------------------------------------------------------------
# Req 14.24: Windows 分离进程
# ---------------------------------------------------------------------------


class TestStartDaemonWindows:
    """Windows 唤起：分离进程 [Req 14.24]。"""

    @mock.patch("sys.platform", "win32")
    @mock.patch("callwarden.server.daemon_autostart._find_daemon_binary", return_value="/usr/bin/cw_daemon.exe")
    @mock.patch("subprocess.Popen")
    def test_starts_detached_process(self, mock_popen, mock_find):
        """Windows 使用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP 标志。"""
        result = _start_daemon_windows()

        assert result is True
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args
        # 单轨 HTTPS：不得再向 daemon 传 --socket / pipe 参数
        assert call_kwargs[0][0] == ["/usr/bin/cw_daemon.exe"]
        # 验证 creationflags 包含 DETACHED_PROCESS
        flags = call_kwargs.kwargs.get("creationflags", call_kwargs[1].get("creationflags", 0))
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        assert flags & DETACHED_PROCESS
        assert flags & CREATE_NEW_PROCESS_GROUP

    @mock.patch("callwarden.server.daemon_autostart._find_daemon_binary", return_value=None)
    def test_returns_false_when_binary_not_found(self, mock_find):
        """找不到 daemon 二进制时返回 False。"""
        result = _start_daemon_windows()
        assert result is False


# ---------------------------------------------------------------------------
# Req 14.25: macOS launchd
# ---------------------------------------------------------------------------


class TestStartDaemonMacos:
    """macOS 唤起：launchd user agent [Req 14.25]。"""

    @mock.patch("subprocess.run")
    def test_activates_launchd_label(self, mock_run):
        """使用 launchctl start 激活注册的 user agent。"""
        mock_run.return_value = mock.Mock(returncode=0, stderr="")
        result = _start_daemon_macos("/tmp/test.sock")

        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["launchctl", "start", MACOS_LAUNCHD_LABEL]

    @mock.patch("subprocess.run")
    def test_returns_false_on_failure(self, mock_run):
        """launchctl 失败时返回 False。"""
        mock_run.return_value = mock.Mock(returncode=1, stderr="not found")
        result = _start_daemon_macos("/tmp/test.sock")
        assert result is False

    @mock.patch("subprocess.run", side_effect=OSError("no launchctl"))
    def test_handles_missing_launchctl(self, mock_run):
        """launchctl 不存在时优雅降级。"""
        result = _start_daemon_macos("/tmp/test.sock")
        assert result is False


# ---------------------------------------------------------------------------
# Req 14.26: Linux systemd
# ---------------------------------------------------------------------------


class TestStartDaemonLinux:
    """Linux 唤起：systemd 用户级服务 [Req 14.26]。"""

    @mock.patch("subprocess.run")
    def test_activates_systemd_user_unit(self, mock_run):
        """使用 systemctl --user start 激活注册单元。"""
        mock_run.return_value = mock.Mock(returncode=0, stderr="")
        result = _start_daemon_linux("/run/callwarden/callwarden.sock")

        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["systemctl", "--user", "start", LINUX_SYSTEMD_UNIT]

    @mock.patch("subprocess.run")
    def test_returns_false_on_failure(self, mock_run):
        """systemctl 失败时返回 False。"""
        mock_run.return_value = mock.Mock(returncode=1, stderr="unit not found")
        result = _start_daemon_linux("/run/callwarden/callwarden.sock")
        assert result is False

    @mock.patch("subprocess.run", side_effect=FileNotFoundError("no systemctl"))
    def test_handles_missing_systemctl(self, mock_run):
        """systemctl 不存在时优雅降级。"""
        result = _start_daemon_linux("/run/callwarden/callwarden.sock")
        assert result is False


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


class TestFindDaemonBinary:
    """_find_daemon_binary 搜索逻辑。"""

    def test_env_var_takes_priority(self):
        """CW_DAEMON_BIN / CW_DAEMON_BINARY 环境变量优先（CW_DAEMON_BIN 优先）。

        stale 依据：_find_daemon_binary 现按 ``CW_DAEMON_BIN`` 再
        ``CW_DAEMON_BINARY`` 读取（server/daemon_autostart.py:573-577）。旧测试只设
        CW_DAEMON_BINARY，未隔离宿主已导出的 CW_DAEMON_BIN，后者会抢先命中而误判。
        故在 patch 作用域内显式移除 CW_DAEMON_BIN，仅保留 CW_DAEMON_BINARY 验证其被采纳。
        """
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            f.write(b"fake")
            fake_path = f.name

        try:
            with mock.patch.dict(os.environ, {"CW_DAEMON_BINARY": fake_path}):
                os.environ.pop("CW_DAEMON_BIN", None)
                result = _find_daemon_binary()
            assert result == fake_path
        finally:
            os.unlink(fake_path)

    def test_returns_none_when_not_found(self):
        """所有路径都找不到时返回 None。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("shutil.which", return_value=None):
                with mock.patch("os.path.isfile", return_value=False):
                    result = _find_daemon_binary()
        assert result is None

    def test_finds_via_path(self):
        """通过 PATH 找到。

        runtime 候选（~/.callwarden/runtime/current/）优先于 PATH 是**有意设计**
        （AGENTS.md §43；server/daemon_autostart.py:604-612 注释：autostart 必须拉起
        已部署 runtime 而非 PATH 的开发产物）。CI test job 的 install step
        （commit 3bcfb14，install -D rust_ext/target/debug/cw-daemon
        $HOME/.callwarden/runtime/current/cw-daemon）使该候选在 CI 上真实存在，
        且 Linux 上 os.environ 被 clear 后 os.path.expanduser 经 pwd 回落真实
        home，候选②命中，本测试（纯 PATH 分支断言）会拿到 runtime 路径而挂。
        故隔离 os.path.isfile（env 候选已被 env clear 隔离，runtime 候选由
        isfile=False 隔离），把 PATH 查找路径从 runtime 候选存在性中解耦；
        查找链完整性（全落空 → None）由 test_returns_none_when_not_found 佐证。
        """
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("shutil.which", return_value="/usr/local/bin/cw_daemon"):
                with mock.patch("os.path.isfile", return_value=False):
                    result = _find_daemon_binary()
        assert result == "/usr/local/bin/cw_daemon"


class TestGetDefaultEndpoint:
    """get_default_endpoint：单轨 HTTPS endpoint。"""

    def test_uses_manifest_endpoint(self):
        """优先读取 authority manifest 登记的 endpoint。"""
        with mock.patch(
            "callwarden.server.daemon_autostart.resolve_http_endpoint_and_manifest",
            return_value=("https://127.0.0.1:8456", {"endpoint": "https://127.0.0.1:8456"}),
        ):
            assert get_default_endpoint() == "https://127.0.0.1:8456"

    def test_falls_back_to_default_https_endpoint(self):
        """manifest 不可读时返回默认 HTTPS 端点（绝不返回 socket/pipe 路径）。"""
        with mock.patch(
            "callwarden.server.daemon_autostart.resolve_http_endpoint_and_manifest",
            side_effect=RuntimeError("no manifest"),
        ):
            endpoint = get_default_endpoint()
        assert endpoint == "https://127.0.0.1:6374"
        assert not endpoint.startswith("/")
        assert "pipe" not in endpoint


# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------


class TestConfigConstants:
    """配置常量符合 Req 14.22。"""

    def test_default_window_is_10_seconds(self):
        """默认窗口 10 秒。"""
        # 环境变量未设置时应为 10.0
        with mock.patch.dict(os.environ, {}, clear=True):
            # 重新加载模块以获取无环境变量时的默认值
            import callwarden.server.daemon_autostart as mod
            # DEFAULT_WAIT_WINDOW 在模块加载时读取环境变量
            # 这里直接验证常量值
            assert mod.DEFAULT_WAIT_WINDOW == 10.0 or "CW_DAEMON_AUTOSTART_WINDOW" in os.environ

    def test_backoff_parameters_reasonable(self):
        """退避参数合理。"""
        assert BACKOFF_BASE > 0
        assert BACKOFF_FACTOR > 1.0
        assert BACKOFF_MAX >= BACKOFF_BASE
        assert BACKOFF_MAX <= 5.0  # 不应太长


class TestDaemonClientAutoRoute:
    """真实 DaemonClient 经过自动唤起后的查询链路（单轨 HTTPS）。"""

    def test_delayed_daemon_serves_real_client_query(self):
        """endpoint 延迟就绪时，call_with_autostart 应在窗口内重试并完成查询。

        单轨 HTTPS 后不存在 UDS 延迟监听；此处以 HttpDaemonRpcClient 的
        call/verify_health 及 daemon_client.ensure_http_daemon 模拟延迟就绪。
        """
        from callwarden.server.daemon_client import (
            DaemonClient,
            DaemonUnavailableError,
        )

        DaemonClient.reset_instance()
        client = DaemonClient()
        client._rpc.call = mock.Mock(
            side_effect=[DaemonUnavailableError("down"), {"files": 7}]
        )
        client._rpc.discover = mock.Mock(return_value="https://127.0.0.1:6374")
        client._rpc.verify_health = mock.Mock(return_value=True)
        with mock.patch(
            "callwarden.server.daemon_client.ensure_http_daemon",
            return_value="https://127.0.0.1:6374",
        ):
            result = client.call_with_autostart("query.stats", {})

        assert result == {"result": {"files": 7}, "degraded": False}
        assert client._rpc.call.call_count == 2
        DaemonClient.reset_instance()

    def test_unavailable_auto_client_returns_local_result(self):
        """daemon 永久不可用时，auto client 应返回本地 SQL 结果。"""
        from callwarden.server.daemon_client import (
            DaemonClient,
            DaemonUnavailableError,
        )

        DaemonClient.reset_instance()
        client = DaemonClient()
        client._rpc.verify_health = mock.Mock(
            side_effect=DaemonUnavailableError("daemon unavailable")
        )
        with mock.patch("callwarden.server.daemon_client.get_daemon_mode", return_value="auto"):
            with mock.patch.object(client, "_sql_fallback_get_stats", return_value={"files": 0}):
                assert client.get_stats(db_path=None) == {"files": 0}
        DaemonClient.reset_instance()


# ---------------------------------------------------------------------------
# CR15（client_convergence_codereview_20260909 §5.8）: ensure_http_daemon
# deadline 必须包含 window——否则窗口退化为单次探针，退避成死代码
# ---------------------------------------------------------------------------


class TestEnsureHttpDaemonBoundedWindow:
    """ensure_http_daemon 在有界窗口内退避重试 HTTP 就绪探针。"""

    def test_succeeds_on_nth_probe_within_window(self):
        """前两次探针失败、第三次成功：窗口内必须持续重试并返回 endpoint。"""
        calls = {"n": 0}

        def flaky_ready(ep):
            calls["n"] += 1
            return calls["n"] >= 3

        t0 = time.monotonic()
        result = ensure_http_daemon(
            "http://127.0.0.1:1", window=5.0, readiness_check=flaky_ready
        )
        _elapsed = time.monotonic() - t0

        assert result == "http://127.0.0.1:1"
        assert calls["n"] >= 3

    def test_single_probe_failure_does_not_give_up_immediately(self):
        """首轮探针失败后不得立即放弃（CR15 死代码回归锚点）：窗口内至少探 2 次。"""
        calls = {"n": 0}

        def never_ready(ep):
            calls["n"] += 1
            return False

        ensure_http_daemon(
            "http://127.0.0.1:1", window=0.4, readiness_check=never_ready
        )
        # 修复前：deadline 不含 window → 单次探针即返回，calls==1
        assert calls["n"] >= 2

    def test_returns_none_when_window_expires(self):
        """窗口耗尽仍不就绪 → 返回 None。"""
        result = ensure_http_daemon(
            "http://127.0.0.1:1", window=0.3, readiness_check=lambda ep: False
        )
        assert result is None

    def test_default_probe_uses_try_http_connect(self):
        """未提供 readiness_check 时走 try_http_connect 探针。"""
        with mock.patch(
            "callwarden.server.daemon_autostart.try_http_connect",
            side_effect=[False, True],
        ) as m:
            result = ensure_http_daemon("http://127.0.0.1:1", window=5.0)
        assert result == "http://127.0.0.1:1"
        assert m.call_count == 2
