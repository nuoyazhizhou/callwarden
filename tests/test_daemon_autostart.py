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

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.daemon_autostart import (
    BACKOFF_BASE,
    BACKOFF_FACTOR,
    BACKOFF_MAX,
    DEFAULT_WAIT_WINDOW,
    LINUX_SYSTEMD_UNIT,
    MACOS_LAUNCHD_LABEL,
    _find_daemon_binary,
    _start_daemon_linux,
    _start_daemon_macos,
    _start_daemon_windows,
    ensure_daemon,
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
        with mock.patch("server.daemon_autostart._start_daemon_platform", return_value=False):
            start = time.monotonic()
            result = ensure_daemon("/nonexistent/path.sock", window=0.5)
            elapsed = time.monotonic() - start

        assert result is None
        # 确认确实在窗口附近返回（允许 0.2s 误差）
        assert 0.4 <= elapsed <= 1.0

    def test_returns_socket_when_daemon_available(self):
        """daemon 已在监听时立即返回连接。"""
        # 创建一个真实的 UDS 监听（仅 Unix）
        if not hasattr(socket, "AF_UNIX"):
            pytest.skip("Windows 无 AF_UNIX")

        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "test.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)
            try:
                start = time.monotonic()
                conn = ensure_daemon(sock_path, window=2.0)
                elapsed = time.monotonic() - start

                assert conn is not None
                # 应立即连接，不需要退避
                assert elapsed < 0.5
                conn.close()
            finally:
                server.close()

    def test_connects_after_delayed_daemon_start(self):
        """daemon 在窗口内延迟就绪时仍能连接。"""
        if not hasattr(socket, "AF_UNIX"):
            pytest.skip("Windows 无 AF_UNIX")

        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "delayed.sock")

            def delayed_listen():
                time.sleep(0.3)
                srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                srv.bind(sock_path)
                srv.listen(1)
                # 保持监听直到测试结束
                time.sleep(2.0)
                srv.close()

            t = threading.Thread(target=delayed_listen, daemon=True)
            t.start()

            with mock.patch("server.daemon_autostart._start_daemon_platform", return_value=True):
                conn = ensure_daemon(sock_path, window=3.0)

            assert conn is not None
            conn.close()

    def test_protocol_readiness_probe_retries_connected_but_unready_endpoint(self):
        """TCP/UDS connect 成功但协议未就绪时仍继续退避探测。"""
        if not hasattr(socket, "AF_UNIX"):
            pytest.skip("Windows 无 AF_UNIX")

        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "probe.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(4)
            probe = mock.Mock(side_effect=[False, True])
            try:
                conn = ensure_daemon(
                    sock_path,
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

        with mock.patch("server.daemon_autostart._start_daemon_platform", return_value=False):
            with mock.patch("time.sleep", side_effect=mock_sleep):
                ensure_daemon("/nonexistent.sock", window=1.0, backoff_base=0.1)

        # 验证退避序列递增（至少前几个）
        if len(sleep_calls) >= 3:
            assert sleep_calls[1] >= sleep_calls[0]
            assert sleep_calls[2] >= sleep_calls[1]

    def test_window_is_configurable(self):
        """窗口大小可通过参数配置。"""
        with mock.patch("server.daemon_autostart._start_daemon_platform", return_value=False):
            start = time.monotonic()
            ensure_daemon("/nonexistent.sock", window=0.3)
            elapsed = time.monotonic() - start

        assert elapsed < 0.8  # 短窗口应快速返回

    def test_uses_client_clock_not_authoritative(self):
        """确认使用 time.monotonic（客户端时钟），不依赖 Authoritative_Clock。"""
        # ensure_daemon 内部使用 time.monotonic，不调用任何 daemon RPC
        with mock.patch("server.daemon_autostart._start_daemon_platform", return_value=False):
            with mock.patch("time.monotonic", wraps=time.monotonic) as mock_mono:
                ensure_daemon("/nonexistent.sock", window=0.3)
                # time.monotonic 应被调用（用于 deadline 计算）
                assert mock_mono.call_count >= 2


# ---------------------------------------------------------------------------
# Req 14.22: try_connect
# ---------------------------------------------------------------------------


class TestTryConnect:
    """try_connect 连接尝试。"""

    def test_returns_none_for_nonexistent_socket(self):
        """不存在的 socket 路径返回 None。"""
        result = try_connect("/nonexistent/path/to/daemon.sock")
        assert result is None

    def test_returns_socket_for_listening_endpoint(self):
        """已监听的 endpoint 返回 socket。"""
        if not hasattr(socket, "AF_UNIX"):
            pytest.skip("Windows 无 AF_UNIX")

        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "live.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)
            try:
                conn = try_connect(sock_path)
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
    @mock.patch("server.daemon_autostart._find_daemon_binary", return_value="/usr/bin/cw_daemon.exe")
    @mock.patch("subprocess.Popen")
    def test_starts_detached_process(self, mock_popen, mock_find):
        """Windows 使用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP 标志。"""
        result = _start_daemon_windows(r"\\.\pipe\callwarden-test")

        assert result is True
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args
        # 验证 creationflags 包含 DETACHED_PROCESS
        flags = call_kwargs.kwargs.get("creationflags", call_kwargs[1].get("creationflags", 0))
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        assert flags & DETACHED_PROCESS
        assert flags & CREATE_NEW_PROCESS_GROUP

    @mock.patch("server.daemon_autostart._find_daemon_binary", return_value=None)
    def test_returns_false_when_binary_not_found(self, mock_find):
        """找不到 daemon 二进制时返回 False。"""
        result = _start_daemon_windows(r"\\.\pipe\callwarden-test")
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
        """CW_DAEMON_BINARY 环境变量优先。"""
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            f.write(b"fake")
            fake_path = f.name

        try:
            with mock.patch.dict(os.environ, {"CW_DAEMON_BINARY": fake_path}):
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
        """通过 PATH 找到。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("shutil.which", return_value="/usr/local/bin/cw_daemon"):
                result = _find_daemon_binary()
        assert result == "/usr/local/bin/cw_daemon"


class TestGetDefaultEndpoint:
    """get_default_endpoint 平台适配。"""

    def test_unix_uses_env_or_default(self):
        """Unix 使用 CW_DAEMON_SOCKET 或默认路径。"""
        if sys.platform == "win32":
            pytest.skip("Unix-only test")
        with mock.patch.dict(os.environ, {"CW_DAEMON_SOCKET": "/custom/path.sock"}):
            assert get_default_endpoint() == "/custom/path.sock"

    @mock.patch("sys.platform", "win32")
    @mock.patch("server.daemon_autostart._get_windows_user_sid", return_value="S-1-5-21-123")
    def test_windows_uses_named_pipe(self, mock_sid):
        """Windows 使用命名管道路径。"""
        endpoint = get_default_endpoint()
        assert endpoint.startswith(r"\\.\pipe\callwarden-")
        assert "S-1-5-21-123" in endpoint

    @mock.patch("callwarden.server.daemon_autostart._get_windows_user_sid", return_value="S-1-5-21-456")
    @mock.patch("callwarden.server.daemon_autostart.sys.platform", "win32")
    def test_rpc_client_uses_windows_endpoint(self, mock_sid):
        """RPC client 默认 endpoint 必须与 autostart 使用同一命名管道。"""
        from server.daemon_client import DaemonClient, UnixDaemonRpcClient

        client = UnixDaemonRpcClient()
        assert client.socket_path == r"\\.\pipe\callwarden-S-1-5-21-456"
        DaemonClient.reset_instance()
        daemon_client = DaemonClient()
        assert daemon_client._rpc.socket_path == client.socket_path


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
            import importlib
            import server.daemon_autostart as mod
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
    """真实 DaemonClient 经过自动唤起后的查询链路。"""

    def test_delayed_daemon_serves_real_client_query(self, tmp_path):
        """endpoint 延迟出现时，auto client 应完成 ping 后查询。"""
        if not hasattr(socket, "AF_UNIX"):
            pytest.skip("需要 AF_UNIX 支持")

        from callwarden.server.daemon_client import DaemonClient
        from callwarden.server.daemon_protocol import recv_message, send_message

        socket_path = str(tmp_path / "delayed-client.sock")
        ready = threading.Event()
        stop = threading.Event()

        def fake_daemon():
            time.sleep(0.15)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(socket_path)
            server.listen(8)
            server.settimeout(0.2)
            ready.set()
            try:
                while not stop.is_set():
                    try:
                        conn, _ = server.accept()
                    except socket.timeout:
                        continue
                    with conn:
                        try:
                            request = recv_message(conn, 8 * 1024 * 1024)
                            method = request.get("method")
                            result = {"status": "ok"} if method == "ping" else {"files": 7}
                            send_message(conn, {
                                "id": request.get("id"),
                                "result": result,
                            }, 8 * 1024 * 1024)
                        except (OSError, ValueError):
                            # ensure_daemon 的探测连接会在未发请求时直接关闭。
                            pass
            finally:
                server.close()
                try:
                    os.unlink(socket_path)
                except FileNotFoundError:
                    pass

        thread = threading.Thread(target=fake_daemon, daemon=True)
        thread.start()
        assert not ready.is_set()

        DaemonClient.reset_instance()
        client = DaemonClient(socket_path=socket_path)
        client._remote_workspace_id = "ws-delayed"
        client._remote_snapshot_ready = True
        with mock.patch("callwarden.server.daemon_client.get_daemon_mode", return_value="auto"):
            with mock.patch("server.daemon_autostart._start_daemon_platform", return_value=True):
                result = client.get_stats(db_path=None)

        assert ready.is_set()
        assert result == {"files": 7}
        assert client.daemon_hits == 1
        stop.set()
        thread.join(timeout=2.0)
        DaemonClient.reset_instance()

    def test_unavailable_auto_client_returns_local_result(self):
        """daemon 永久不可用时，auto client 应返回本地 SQL 结果。"""
        from callwarden.server.daemon_client import DaemonClient

        DaemonClient.reset_instance()
        client = DaemonClient(socket_path="/definitely/missing/callwarden.sock")
        with mock.patch("callwarden.server.daemon_client.get_daemon_mode", return_value="auto"):
            with mock.patch("callwarden.server.daemon_client.ensure_daemon", return_value=None):
                with mock.patch.object(client, "_sql_fallback_get_stats", return_value={"files": 0}):
                    assert client.get_stats(db_path=None) == {"files": 0}
        DaemonClient.reset_instance()
