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
        # TCP bridge 由另一 authority（通常是 Windows）管理；WSL/Linux
        # 不得把 bridge 端点误当成本地 daemon，启动 systemd/本地进程。
        if not launch_attempted and not _is_tcp_endpoint(endpoint):
            launch_attempted = True
            _start_daemon_platform(endpoint)
        elif _is_tcp_endpoint(endpoint):
            launch_attempted = True

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


def ensure_daemon_for_startup(
    endpoint: str,
    readiness_check: Optional[Callable[[object], bool]] = None,
) -> bool:
    """在宿主进程启动阶段确保 daemon 可用。

    与请求期 autostart 共用同一个跨进程互斥。MCP 多实例同时启动时，
    只有一个实例会真正唤起 daemon，其他实例等待同一个有界窗口。
    """
    from .daemon_mutex import DaemonMutex

    conn = try_connect(endpoint)
    if conn is not None:
        try:
            if readiness_check is None or readiness_check(conn):
                return True
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    mutex = DaemonMutex(endpoint)
    if mutex.try_acquire():
        try:
            conn = ensure_daemon(endpoint, readiness_check=readiness_check)
        finally:
            mutex.release()
    else:
        conn = ensure_daemon(endpoint, readiness_check=readiness_check)

    if conn is None:
        return False
    try:
        conn.close()
    except Exception:
        pass
    return True


def try_connect(endpoint: str) -> Optional[socket.socket]:
    r"""尝试建立到 daemon endpoint 的连接。

    支持端点类型（共存契约 §3.2）：
    - Windows Named Pipe：`\\.\pipe\callwarden-<sid>`
    - Unix Domain Socket：绝对路径（如 `/run/callwarden/callwarden.sock`）
    - TCP bridge：`tcp://host:port` 或 `host:port`（WSL 访问 Windows bridge）

    Returns:
        已连接的 socket，或 None（连接失败）。
    """
    # TCP endpoint 优先于平台判断：即使 Windows 平台也走 TCP（bridge health 用）。
    if _is_tcp_endpoint(endpoint):
        return _try_connect_tcp(endpoint)
    if sys.platform == "win32":
        return _try_connect_windows(endpoint)
    return _try_connect_unix(endpoint)


def _is_tcp_endpoint(endpoint: str) -> bool:
    """判断 endpoint 是否为 TCP bridge 端点。"""
    if endpoint.startswith("tcp://"):
        return True
    # 裸 host:port 形式（如 127.0.0.1:8456）且非 UDS 绝对路径
    if ":" in endpoint and not endpoint.startswith("/"):
        host, _, port = endpoint.rpartition(":")
        if port.isdigit() and host:
            return True
    return False


def _try_connect_tcp(endpoint: str) -> Optional[socket.socket]:
    """TCP bridge 连接尝试（WSL 访问 Windows cw-bridge）。"""
    host_port = endpoint.removeprefix("tcp://")
    host, _, port = host_port.rpartition(":")
    if not host or not port.isdigit():
        return None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        sock.connect((host, int(port)))
        return sock
    except (OSError, socket.error):
        try:
            sock.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return None


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
        from ctypes import wintypes

        # 64 位进程必须显式声明 restype/argtypes，否则 HANDLE 返回被截断为 32 位，
        # 失败的 CreateFileW 会被误判为成功（低 32 位 -1 != 64 位 -1）。
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]

        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        ERROR_PIPE_BUSY = 231

        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
        # 标准命名管道客户端模式：所有实例瞬时繁忙时 CreateFileW 返回
        # ERROR_PIPE_BUSY(231)，按惯例退避重试几次再放弃（服务端 accept 之间
        # 会补建替换实例，重试窗口内必然恢复）。
        for _ in range(5):
            handle = kernel32.CreateFileW(
                endpoint,
                GENERIC_READ | GENERIC_WRITE,
                0,       # 不共享
                None,    # 默认安全属性
                OPEN_EXISTING,
                0,       # 同步 I/O
                None,    # 无模板文件
            )
            if handle != INVALID_HANDLE_VALUE and handle is not None:
                return _WindowsPipeSocket(handle)
            if ctypes.windll.kernel32.GetLastError() != ERROR_PIPE_BUSY:
                return None
            time.sleep(0.05)
        return None
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

    # P0 修复：显式注入权威任务库路径（~/.callwarden/callwarden.db），
    # 确保 daemon 与 Python `cw task` CLI 共享同一套任务状态。
    # daemon 通过环境变量 CW_DAEMON_TASK_DB 覆盖默认推导路径。
    from callwarden.config import DB_PATH as _AUTHORITY_TASK_DB
    child_env = dict(os.environ)
    child_env["CW_DAEMON_TASK_DB"] = _AUTHORITY_TASK_DB

    try:
        subprocess.Popen(
            [daemon_bin, "--socket", endpoint],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            env=child_env,
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
            text=True, encoding="utf-8", errors="replace",
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
            text=True, encoding="utf-8", errors="replace",
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
    1. CW_DAEMON_BIN / CW_DAEMON_BINARY 环境变量
    2. PATH 中的 cw_daemon（或 cw_daemon.exe）
    3. 项目 rust_ext/target/release/ 或 debug/ 下的构建产物
    """
    # 环境变量优先
    env_bin = os.environ.get("CW_DAEMON_BIN") or os.environ.get("CW_DAEMON_BINARY")
    if env_bin and os.path.isfile(env_bin):
        return env_bin

    # PATH 搜索（cargo bin target 名为 cw-daemon，产物为 cw-daemon.exe；兼容旧命名 cw_daemon）
    names = (
        ["cw_daemon.exe", "cw-daemon.exe"]
        if sys.platform == "win32"
        else ["cw_daemon", "cw-daemon"]
    )
    for name in names:
        found = shutil.which(name)
        if found:
            return found

    # 项目构建产物（开发环境）。release 优先，debug 作为本地开发回退。
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.platform == "win32":
        profiles = ("release", "debug")
        names = ("cw-daemon.exe", "cw_daemon.exe")
    else:
        profiles = ("release", "debug")
        names = ("cw-daemon", "cw_daemon")
    candidates = [
        os.path.join(project_root, "rust_ext", "target", profile, name)
        for profile in profiles
        for name in names
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    return None


def get_default_endpoint() -> str:
    """获取当前平台的默认 Daemon_Endpoint。

    Unix: CW_DAEMON_SOCKET 环境变量或 /run/callwarden/callwarden.sock
    Windows: \\\\.\\pipe\\callwarden-<当前用户 SID>
    """
    from callwarden.config import get_default_daemon_endpoint
    return get_default_daemon_endpoint()


def _get_windows_user_sid() -> str:
    """获取当前 Windows 用户的 SID 字符串。"""
    from callwarden.config import _get_windows_user_sid as _cfg_sid
    return _cfg_sid()

    """获取当前 Windows 用户的 SID 字符串。"""
    try:
        import ctypes

        advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        from ctypes import wintypes

        # 64 位进程必须显式声明 argtypes/restype，否则 HANDLE 在指针截断后
        # OpenProcessToken 失败，SID 退化为 "unknown"。
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.LocalFree.argtypes = [wintypes.HANDLE]

        # 获取当前进程 token
        token = wintypes.HANDLE()
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


# ────────────────────────────────────────────────────────────────────────────
# HTTP MVP manifest 发现 / 校验（H2：Python thin client 发现层）
# ────────────────────────────────────────────────────────────────────────────
# 这些函数构成 client 的 endpoint 发现与 fail-closed 校验门禁。它们不启动任何
# daemon 二进制（那是 H1 的职责），也不打开 SQLite；仅读取并校验 authority-scoped
# manifest，或在显式 loopback endpoint 上做 loopback 校验。
# 详见 docs/design/http-daemon-mvp-compatibility-contract.md §4.1。

import json  # noqa: E402  (本段为 HTTP 发现层，独立导入)

from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402


def _pid_alive(pid: int) -> bool:
    """判断 PID 是否仍存活（跨平台）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但无信号权限 → 视为存活
        return True
    return True


def _win_pid_alive(pid: int) -> bool:
    """Windows：OpenProcess + GetExitCodeProcess，STILL_ACTIVE(259) 视为存活。"""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == 259  # STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _pid_executable(pid: int) -> str:
    """返回 PID 对应进程的可执行文件路径（用于与 manifest 交叉校验）。"""
    if sys.platform == "win32":
        return _win_pid_executable(pid)
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def _win_pid_executable(pid: int) -> str:
    """Windows：QueryFullProcessImageNameW 获取进程镜像路径。"""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        size = wintypes.DWORD(wintypes.MAX_PATH)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def read_http_manifest(path: str) -> Dict[str, Any]:
    """读取并解析 HTTP manifest 文件。

    Raises:
        DaemonRemoteError(E_HTTP_MANIFEST_MISSING): 文件不存在。
        DaemonRemoteError(E_HTTP_MANIFEST_STALE): 读取/解析失败或非对象。
    """
    from callwarden.config import (
        E_HTTP_MANIFEST_MISSING,
        E_HTTP_MANIFEST_STALE,
    )

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise DaemonRemoteError(
            E_HTTP_MANIFEST_MISSING, f"HTTP manifest 文件不存在: {path}"
        )
    except (OSError, ValueError) as exc:
        raise DaemonRemoteError(
            E_HTTP_MANIFEST_STALE, f"HTTP manifest 读取/解析失败: {exc}"
        )
    if not isinstance(data, dict):
        raise DaemonRemoteError(E_HTTP_MANIFEST_STALE, "HTTP manifest 不是 JSON 对象")
    return data


def validate_http_endpoint_loopback(endpoint: str) -> str:
    """校验 endpoint 为显式 loopback http；返回规范化 endpoint（去尾斜杠）。

    Raises:
        DaemonRemoteError(E_HTTP_MVP_LOOPBACK_ONLY): scheme 非 http 或 host 非 loopback。
    """
    from callwarden.config import (
        E_HTTP_MVP_LOOPBACK_ONLY,
        HTTP_MVP_TRANSPORT_PROFILE,
        is_loopback_host,
        parse_http_endpoint_host,
    )

    try:
        scheme, host, _port = parse_http_endpoint_host(endpoint)
    except ValueError as exc:
        raise DaemonRemoteError(E_HTTP_MVP_LOOPBACK_ONLY, str(exc))
    if scheme != "http":
        raise DaemonRemoteError(
            E_HTTP_MVP_LOOPBACK_ONLY,
            f"HTTP MVP 仅允许 http（dev-loopback profile），拒绝 scheme={scheme!r}",
        )
    if not host or not is_loopback_host(host):
        raise DaemonRemoteError(
            E_HTTP_MVP_LOOPBACK_ONLY,
            f"HTTP MVP 仅允许 loopback endpoint，拒绝 host={host!r}",
        )
    return endpoint.rstrip("/")


def validate_http_manifest(
    manifest: Dict[str, Any], expected_authority_id: str = ""
) -> Dict[str, Any]:
    """联网前完整校验 manifest（frozen contract §4.1）。

    依次校验：schema_version / manifest_hash / security_profile /
    authority / 协议交集 / endpoint loopback / PID 存活 / executable 匹配。
    任一不符抛结构化 DaemonRemoteError（fail-closed，不连接、不删除 manifest）。

    Raises:
        DaemonRemoteError: 任一校验失败时，code 为 E_HTTP_MANIFEST_* 或
            E_PROTOCOL_VERSION_UNSUPPORTED / E_HTTP_MVP_LOOPBACK_ONLY。
    """
    from callwarden.config import (
        E_HTTP_MANIFEST_HASH_MISMATCH,
        E_HTTP_MANIFEST_STALE,
        E_HTTP_MVP_LOOPBACK_ONLY,
        E_PROTOCOL_VERSION_UNSUPPORTED,
        HTTP_MANIFEST_SCHEMA_VERSION,
        HTTP_MVP_TRANSPORT_PROFILE,
        HTTP_PROTOCOL_VERSION,
        SUPPORTED_HTTP_PROTOCOL_VERSIONS,
        compute_http_manifest_hash,
        norm_path,
    )

    if not isinstance(manifest, dict):
        raise DaemonRemoteError(E_HTTP_MANIFEST_STALE, "manifest 不是 JSON 对象")

    # schema_version
    if manifest.get("manifest_version") != HTTP_MANIFEST_SCHEMA_VERSION:
        raise DaemonRemoteError(
            E_HTTP_MANIFEST_STALE,
            f"manifest_version 不符: 期望 {HTTP_MANIFEST_SCHEMA_VERSION!r}, "
            f"实际 {manifest.get('manifest_version')!r}",
        )

    # manifest_hash（排除自身后 canonical JSON 的 SHA-256）
    if manifest.get("manifest_hash") != compute_http_manifest_hash(manifest):
        raise DaemonRemoteError(
            E_HTTP_MANIFEST_HASH_MISMATCH,
            "manifest_hash 校验失败（可能被篡改或写入未落盘）",
        )

    # security_profile 必须严格等于 dev_loopback_unauthenticated
    if manifest.get("security_profile") != HTTP_MVP_TRANSPORT_PROFILE:
        raise DaemonRemoteError(
            E_HTTP_MANIFEST_STALE,
            f"security_profile 非 {HTTP_MVP_TRANSPORT_PROFILE!r}: "
            f"{manifest.get('security_profile')!r}",
        )

    # authority 作用域
    if expected_authority_id and manifest.get("authority_id") != expected_authority_id:
        raise DaemonRemoteError(
            E_HTTP_MANIFEST_STALE,
            f"authority 不匹配: manifest={manifest.get('authority_id')!r}, "
            f"expected={expected_authority_id!r}",
        )

    # 协议版本交集（客户端仅支持 v1）
    svers = manifest.get("supported_protocol_versions") or []
    if (not isinstance(svers, list)) or HTTP_PROTOCOL_VERSION not in svers:
        raise DaemonRemoteError(
            E_PROTOCOL_VERSION_UNSUPPORTED,
            f"无协议交集: 客户端支持 v1, daemon supported={svers!r} "
            f"(E_PROTOCOL_VERSION_UNSUPPORTED)",
        )

    # endpoint 必须为 loopback
    validate_http_endpoint_loopback(manifest.get("endpoint", ""))

    # PID 存活 + executable 匹配（stale-PID 规则）
    pid = manifest.get("pid")
    if pid is not None:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            pid = None
    if pid is not None:
        if not _pid_alive(pid):
            raise DaemonRemoteError(
                E_HTTP_MANIFEST_STALE,
                f"manifest PID {pid} 已不存活（stale manifest）",
            )
        exe = manifest.get("daemon_executable")
        if exe:
            actual = _pid_executable(pid)
            if actual and norm_path(actual) != norm_path(exe):
                raise DaemonRemoteError(
                    E_HTTP_MANIFEST_STALE,
                    f"manifest executable 与存活 PID 不匹配: "
                    f"manifest={exe!r}, pid={actual!r}",
                )

    return manifest


def _load_manifest_for_authority(
    manifest_path: Optional[str], authority_id: str
) -> Optional[Dict[str, Any]]:
    """按候选路径加载 authority-scoped manifest。

    候选顺序：显式 manifest_path（若存在）→ 当前 authority 默认 manifest。
    文件存在但损坏/非对象时向上抛出结构化错误（fail-closed）；
    两个候选都不存在时返回 None（交由调用方决定）。"""
    from callwarden.config import get_http_manifest_path

    candidates: List[str] = []
    if manifest_path:
        candidates.append(manifest_path)
    candidates.append(get_http_manifest_path(authority_id))
    for path in candidates:
        if path and os.path.isfile(path):
            return read_http_manifest(path)  # 缺失/损坏均抛结构化错误
    return None


def resolve_http_endpoint_and_manifest(
    explicit_endpoint: Optional[str] = None,
    manifest_path: Optional[str] = None,
    authority_id: str = "",
    validate: bool = True,
) -> tuple:
    """按 frozen contract §4.1 优先级解析 HTTP endpoint 与（可选）manifest。

    优先级：
        显式 CW_DAEMON_HTTP_ENDPOINT（仍须 loopback 校验）
        > 显式 manifest_path
        > 当前 authority 的默认 manifest。

    显式 endpoint 本身是合法的独立发现路径（无需 manifest 文件存在），
    但若存在 manifest 仍按其校验（fail-closed）。无显式 endpoint 且未找到
    manifest 时抛 E_HTTP_MANIFEST_MISSING。

    Returns:
        (endpoint, manifest_or_None)

    Raises:
        DaemonRemoteError: loopback 校验失败 / manifest 缺失或校验不通过。
    """
    from callwarden.config import (
        E_HTTP_MANIFEST_MISSING,
        get_http_authority_id,
        get_http_daemon_endpoint,
    )

    authority_id = authority_id or get_http_authority_id()
    explicit = explicit_endpoint or get_http_daemon_endpoint()
    if explicit:
        # 显式 endpoint：必须 loopback，manifest 仅作可选权威校验
        endpoint = validate_http_endpoint_loopback(explicit)
        manifest = _load_manifest_for_authority(manifest_path, authority_id)
        if manifest is not None and validate:
            validate_http_manifest(manifest, authority_id)
        return endpoint, manifest

    # 仅经 manifest 发现
    manifest = _load_manifest_for_authority(manifest_path, authority_id)
    if manifest is None:
        raise DaemonRemoteError(
            E_HTTP_MANIFEST_MISSING,
            "未设置 CW_DAEMON_HTTP_ENDPOINT 且未找到 authority-scoped manifest；"
            "fail-closed（不回退 Named Pipe/UDS/SQLite）",
        )
    if validate:
        validate_http_manifest(manifest, authority_id)
    ep = manifest.get("endpoint", "")
    ep = validate_http_endpoint_loopback(ep)
    return ep, manifest


def try_http_connect(endpoint: str, timeout: float = 2.0) -> bool:
    """对 HTTP endpoint 做一次短超时连通性探针（仅 TCP connect + 读取首字节）。"""
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.close()
        return True
    except OSError:
        return False


def ensure_http_daemon(
    endpoint: str,
    window: Optional[float] = None,
    timeout: float = 2.0,
    readiness_check: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """在 bounded 窗口内等待 HTTP daemon 就绪（不启动未知二进制）。

    仅做 CONNECT/TCP 级或 readiness 级探针并重试；窗口耗尽返回 None。
    与 ensure_daemon 的语义对齐，但面向 HTTP endpoint，不唤起任何进程。

    Args:
        endpoint: 已校验的 loopback http endpoint。
        window: 有界等待窗口（秒），默认 10s。
        timeout: 单次探针超时。
        readiness_check: 可选（endpoint）-> bool 就绪判定（如 GET /health）。

    Returns:
        就绪时返回 endpoint，否则 None。
    """
    if window is None:
        window = DEFAULT_WAIT_WINDOW
    deadline = time.monotonic()
    backoff = BACKOFF_BASE

    def _ready(ep: str) -> bool:
        if readiness_check is not None:
            try:
                return bool(readiness_check(ep))
            except Exception:
                return False
        return try_http_connect(ep, timeout)

    while True:
        if _ready(endpoint):
            return endpoint
        now = time.monotonic()
        if now >= deadline:
            break
        sleep_time = min(backoff, deadline - now)
        if sleep_time <= 0:
            break
        time.sleep(sleep_time)
        backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)
    return None
