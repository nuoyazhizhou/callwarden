"""Daemon 自动唤起与有界等待窗口（Req 14.22, 14.24, 14.25, 14.26）。

客户端连不上 Daemon_Endpoint 时先尝试启动 daemon，并在有界等待窗口内以指数退避
重试连接；窗口内任一次重试成功即在该连接上继续执行原请求，调用方不感知中断。
窗口默认 10 秒、可配置，按客户端时钟计量——此时 daemon 尚未就绪，
Authoritative_Clock 不存在。

三平台唤起方式：
- Windows: 启动分离进程（客户端进程退出后 daemon 仍存活）  [Req 14.24]
- macOS:   经 launchd 激活已注册 user agent               [Req 14.25]
- Linux:   经 systemd 用户级服务激活已注册单元             [Req 14.26]

所有权：本文件（server/daemon_autostart.py）。
设计参考：docs/design/multi-llm-contract-driven-collaboration-design.md §13.5.7
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

# 有界等待窗口默认值（秒），可通过环境变量覆盖 [Req 14.22]
DEFAULT_WAIT_WINDOW: float = float(os.environ.get("CW_DAEMON_AUTOSTART_WINDOW", "10.0"))

# 指数退避参数
BACKOFF_BASE: float = 0.1       # 首次退避间隔（秒）
BACKOFF_FACTOR: float = 2.0     # 退避倍增因子
BACKOFF_MAX: float = 2.0        # 单次退避上限（秒）

# 连接尝试超时（秒）——每次 try_connect 的 socket 超时
CONNECT_TIMEOUT: float = 1.0

# 平台服务标识
MACOS_LAUNCHD_LABEL: str = "com.callwarden.daemon"
LINUX_SYSTEMD_UNIT: str = "callwarden-daemon.service"

# Windows 命名管道前缀
WINDOWS_PIPE_PREFIX: str = "\\\\.\\pipe\\callwarden-"
# 通用命名管道路径前缀（用于识别管道路径）
_WINDOWS_PIPE_PATH_PREFIX: str = "\\\\.\\pipe\\"


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def ensure_daemon(
    endpoint: str,
    window: Optional[float] = None,
    backoff_base: float = BACKOFF_BASE,
    backoff_factor: float = BACKOFF_FACTOR,
    backoff_max: float = BACKOFF_MAX,
    readiness_check: Optional[Callable[[object], bool]] = None,
) -> Optional[socket.socket]:
    """尝试连接 daemon；连不上时自动唤起并在有界窗口内退避重试。

    Args:
        endpoint: Daemon_Endpoint 路径。
                  Unix: UDS 路径（如 /run/callwarden/callwarden.sock）
                  Windows: 命名管道路径（如 \\\\.\\pipe\\callwarden-<sid>）
        window: 有界等待窗口（秒）。None 时使用 DEFAULT_WAIT_WINDOW。
        backoff_base: 首次退避间隔（秒）。
        backoff_factor: 退避倍增因子。
        backoff_max: 单次退避上限（秒）。
        readiness_check: 可选的协议级就绪探针。返回 False 或抛异常时，
                         当前连接会关闭并继续在窗口内重试。

    Returns:
        已连接的 socket 对象（调用方负责关闭），或 None（窗口耗尽，进入 Degraded_Mode）。

    按客户端时钟（time.monotonic）计量，不依赖 Authoritative_Clock [Req 14.22]。
    """
    if window is None:
        window = DEFAULT_WAIT_WINDOW

    deadline = time.monotonic() + window
    backoff = backoff_base
    launch_attempted = False

    while True:
        # 尝试连接
        conn = try_connect(endpoint)
        if conn is not None:
            if readiness_check is None:
                logger.debug("daemon 连接成功: %s", endpoint)
                return conn
            try:
                ready = readiness_check(conn)
            except Exception as exc:
                logger.debug("daemon readiness probe 失败: %s", exc)
                ready = False
            if ready:
                logger.debug("daemon 协议就绪: %s", endpoint)
                return conn
            try:
                conn.close()
            except Exception:
                pass

        # 窗口检查：退避前确认还有时间
        now = time.monotonic()
        if now >= deadline:
            break

        # 首次连接失败时尝试唤起 daemon（仅一次）
        if not launch_attempted:
            launch_attempted = True
            _start_daemon_platform(endpoint)

        # 指数退避，但不超过 deadline
        sleep_time = min(backoff, deadline - time.monotonic())
        if sleep_time <= 0:
            break
        time.sleep(sleep_time)
        backoff = min(backoff * backoff_factor, backoff_max)

    logger.warning(
        "daemon 自动唤起在有界等待窗口（%.1fs）内未成功: %s", window, endpoint
    )
    return None


def try_connect(endpoint: str) -> Optional[socket.socket]:
    """尝试建立到 daemon endpoint 的连接。

    Returns:
        已连接的 socket，或 None（连接失败）。
    """
    if sys.platform == "win32":
        return _try_connect_windows(endpoint)
    else:
        return _try_connect_unix(endpoint)


# ---------------------------------------------------------------------------
# 平台连接实现
# ---------------------------------------------------------------------------


def _try_connect_unix(endpoint: str) -> Optional[socket.socket]:
    """Unix UDS 连接尝试。"""
    if not hasattr(socket, "AF_UNIX"):
        return None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        sock.connect(endpoint)
        return sock
    except (OSError, socket.error):
        try:
            sock.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return None


def _try_connect_windows(endpoint: str) -> Optional[socket.socket]:
    """Windows 命名管道连接尝试。

    使用 AF_INET loopback 模拟不适用；Windows 命名管道通过 CreateFileW 打开。
    返回一个包装了 pipe handle 的 socket-like 对象，或 None。
    为保持与 Unix 路径的接口一致性，返回 _WindowsPipeSocket 包装。
    """
    # 仅对命名管道路径尝试连接，普通文件路径不是有效的 daemon endpoint
    if not endpoint.startswith(_WINDOWS_PIPE_PATH_PREFIX):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        FILE_FLAG_OVERLAPPED = 0x40000000

        handle = kernel32.CreateFileW(
            endpoint,
            GENERIC_READ | GENERIC_WRITE,
            0,       # 不共享
            None,    # 默认安全属性
            OPEN_EXISTING,
            0,       # 同步 I/O
            None,    # 无模板文件
        )

        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        if handle == INVALID_HANDLE_VALUE or handle is None:
            return None

        return _WindowsPipeSocket(handle)
    except Exception:
        return None


class _WindowsPipeSocket:
    """Windows 命名管道 handle 的 socket-like 包装。

    提供与 socket.socket 兼容的 send/recv/close/settimeout 接口，
    使调用方无需区分 Unix socket 和 Windows pipe。
    """

    def __init__(self, handle: int):
        self._handle = handle
        self._timeout: Optional[float] = None

    def settimeout(self, timeout: Optional[float]) -> None:
        self._timeout = timeout

    def send(self, data: bytes) -> int:
        return self._run_with_timeout(lambda: self._send_blocking(data))

    def _send_blocking(self, data: bytes) -> int:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(
            self._handle, data, len(data), ctypes.byref(written), None
        )
        if not ok:
            raise OSError(f"WriteFile failed: {ctypes.GetLastError()}")
        return written.value

    def sendall(self, data: bytes) -> None:
        """按 socket.sendall 语义处理 Windows pipe 的短写。"""
        offset = 0
        while offset < len(data):
            written = self.send(data[offset:])
            if written <= 0:
                raise OSError("WriteFile 写入零字节")
            offset += written

    def recv(self, bufsize: int) -> bytes:
        return self._run_with_timeout(lambda: self._recv_blocking(bufsize))

    def _recv_blocking(self, bufsize: int) -> bytes:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        buf = ctypes.create_string_buffer(bufsize)
        read = wintypes.DWORD(0)
        ok = kernel32.ReadFile(
            self._handle, buf, bufsize, ctypes.byref(read), None
        )
        if not ok:
            raise OSError(f"ReadFile failed: {ctypes.GetLastError()}")
        return buf.raw[: read.value]

    def _run_with_timeout(self, operation):
        """为同步 Win32 I/O 提供真正的客户端超时。

        超时后关闭句柄，阻止调用方继续等待；底层阻塞调用在线程中结束，
        线程为 daemon 线程，不会阻止客户端进程退出。
        """
        if self._timeout is None:
            return operation()

        result = {}
        done = threading.Event()

        def worker():
            try:
                result["value"] = operation()
            except BaseException as exc:  # 传回 Win32 I/O 的原始错误
                result["error"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, name="cw-pipe-io", daemon=True).start()
        if not done.wait(self._timeout):
            self.close()
            raise socket.timeout("Windows named pipe I/O timed out")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def close(self) -> None:
        if self._handle is not None:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# 平台唤起实现
# ---------------------------------------------------------------------------


def _start_daemon_platform(endpoint: str) -> bool:
    """按平台选择唤起方式启动 daemon。

    Returns:
        True 表示启动命令已发出（不代表 daemon 已就绪），False 表示启动失败。
    """
    if sys.platform == "win32":
        return _start_daemon_windows(endpoint)
    elif sys.platform == "darwin":
        return _start_daemon_macos(endpoint)
    else:
        return _start_daemon_linux(endpoint)


def _start_daemon_windows(endpoint: str) -> bool:
    """Windows: 启动分离进程，客户端退出后 daemon 仍存活 [Req 14.24]。"""
    daemon_bin = _find_daemon_binary()
    if daemon_bin is None:
        logger.error("未找到 cw_daemon 可执行文件，无法自动唤起")
        return False

    # DETACHED_PROCESS: 进程不继承控制台，父进程退出后仍存活
    # CREATE_NEW_PROCESS_GROUP: 新进程组，不受父进程 Ctrl+C 影响
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000

    try:
        subprocess.Popen(
            [daemon_bin, "--socket", endpoint],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        logger.info("Windows 分离进程已启动: %s --socket %s", daemon_bin, endpoint)
        return True
    except OSError as exc:
        logger.error("Windows daemon 启动失败: %s", exc)
        return False


def _start_daemon_macos(endpoint: str) -> bool:
    """macOS: 经 launchd 激活已注册 user agent [Req 14.25]。"""
    try:
        result = subprocess.run(
            ["launchctl", "start", MACOS_LAUNCHD_LABEL],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info("macOS launchd 已激活: %s", MACOS_LAUNCHD_LABEL)
            return True
        else:
            logger.warning(
                "launchctl start 返回 %d: %s", result.returncode, result.stderr.strip()
            )
            # launchctl start 对已运行的服务返回非零，不算致命错误
            return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("macOS daemon 唤起失败: %s", exc)
        return False


def _start_daemon_linux(endpoint: str) -> bool:
    """Linux: 经 systemd 用户级服务激活已注册单元 [Req 14.26]。"""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "start", LINUX_SYSTEMD_UNIT],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info("Linux systemd 用户服务已激活: %s", LINUX_SYSTEMD_UNIT)
            return True
        else:
            logger.warning(
                "systemctl --user start 返回 %d: %s",
                result.returncode,
                result.stderr.strip(),
            )
            return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("Linux daemon 唤起失败: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _find_daemon_binary() -> Optional[str]:
    """定位 cw_daemon 可执行文件。

    搜索顺序：
    1. CW_DAEMON_BINARY 环境变量
    2. PATH 中的 cw_daemon（或 cw_daemon.exe）
    3. 项目 rust_ext/target/release/ 下的构建产物
    """
    # 环境变量优先
    env_bin = os.environ.get("CW_DAEMON_BINARY")
    if env_bin and os.path.isfile(env_bin):
        return env_bin

    # PATH 搜索
    name = "cw_daemon.exe" if sys.platform == "win32" else "cw_daemon"
    found = shutil.which(name)
    if found:
        return found

    # 项目构建产物（开发环境）
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.platform == "win32":
        candidate = os.path.join(project_root, "rust_ext", "target", "release", "cw_daemon.exe")
    else:
        candidate = os.path.join(project_root, "rust_ext", "target", "release", "cw_daemon")
    if os.path.isfile(candidate):
        return candidate

    return None


def get_default_endpoint() -> str:
    """获取当前平台的默认 Daemon_Endpoint。

    Unix: CW_DAEMON_SOCKET 环境变量或 /run/callwarden/callwarden.sock
    Windows: \\\\.\\pipe\\callwarden-<当前用户 SID>
    """
    if sys.platform == "win32":
        # Windows 命名管道需要当前用户 SID
        sid = _get_windows_user_sid()
        return f"{WINDOWS_PIPE_PREFIX}{sid}"
    else:
        return os.environ.get("CW_DAEMON_SOCKET", "/run/callwarden/callwarden.sock")


def _get_windows_user_sid() -> str:
    """获取当前 Windows 用户的 SID 字符串。"""
    try:
        import ctypes

        advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        # 获取当前进程 token
        token = ctypes.c_void_p()
        TOKEN_QUERY = 0x0008
        ok = advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
        )
        if not ok:
            return "unknown"

        # 获取 token 中的用户 SID
        TOKEN_USER = 1
        size = ctypes.c_ulong(0)
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))

        buf = ctypes.create_string_buffer(size.value)
        ok = advapi32.GetTokenInformation(token, TOKEN_USER, buf, size.value, ctypes.byref(size))
        kernel32.CloseHandle(token)

        if not ok:
            return "unknown"

        # TOKEN_USER 结构的第一个成员是 SID_AND_ATTRIBUTES，其第一个成员是 SID*
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]

        # 将 SID 转为字符串
        sid_str = ctypes.c_wchar_p()
        ok = advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str))
        if not ok:
            return "unknown"

        result = sid_str.value
        kernel32.LocalFree(sid_str)
        return result or "unknown"
    except Exception:
        return "unknown"
