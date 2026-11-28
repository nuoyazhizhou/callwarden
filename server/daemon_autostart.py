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
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

# 有界等待窗口默认值（秒），可通过环境变量覆盖 [Req 14.22]
DEFAULT_WAIT_WINDOW: float = float(
    os.environ.get("CW_DAEMON_AUTOSTART_WINDOW", "10.0"))

# 指数退避参数
BACKOFF_BASE: float = 0.1       # 首次退避间隔（秒）
BACKOFF_FACTOR: float = 2.0     # 退避倍增因子
BACKOFF_MAX: float = 2.0        # 单次退避上限（秒）

# 连接尝试超时（秒）——每次 try_connect 的 socket 超时
CONNECT_TIMEOUT: float = 1.0

# 平台服务标识
MACOS_LAUNCHD_LABEL: str = "com.callwarden.daemon"
LINUX_SYSTEMD_UNIT: str = "callwarden-daemon.service"

# D3 自愈守卫（根修卡 T-1795507699058-41d9fcbc / 合同 r2）
# 探活失败不得拉第二 daemon：探活 1s 抖动 → 退避重试窗口 + 冷却。
E_DAEMON_ALREADY_RUNNING: str = "E_DAEMON_ALREADY_RUNNING"
# 探活失败后的退避重试：先给 in-flight RPC 一个短窗收敛，再决定是否 spawn。
D3_PROBE_RETRY_WINDOW: float = float(
    os.environ.get("CW_DAEMON_PROBE_RETRY_WINDOW", "2.0"))
D3_PROBE_RETRY_INTERVAL: float = 0.25
# spawn 冷却：同一进程内两次 spawn 尝试的最小间隔，防 1s 抖动重复拉起。
D3_SPAWN_COOLDOWN: float = float(
    os.environ.get("CW_DAEMON_SPAWN_COOLDOWN", "5.0"))
_spawn_last_attempt_monotonic: Optional[float] = None

# F6（根修卡 T-1795507699058-41d9fcbc / finding F6）：Windows 分离进程必须
# CREATE_BREAKAWAY_FROM_JOB 才能脱离 IDE/宿主 job object 的 kill-on-close 连坐。
# DETACHED_PROCESS 只脱离控制台、不脱离 job object，故需显式 breakaway 位。
DETACHED_PROCESS: int = 0x00000008
CREATE_NEW_PROCESS_GROUP: int = 0x00000200
CREATE_NO_WINDOW: int = 0x08000000
CREATE_BREAKAWAY_FROM_JOB: int = 0x01000000
_WIN32_DETACHED_BASE_FLAGS: int = (
    DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
# job 未开放 breakaway 权限 / 调用方不在 job 中时 CreateProcess 会失败，降级原 flags 重试。
# ERROR_ACCESS_DENIED=5（job 无 BREAKAWAY_OK 权限）/ ERROR_NOT_IN_JOB=801（不在 job 中）。
_WIN32_BREAKAWAY_FALLBACK_ERRORS = frozenset({5, 801})


def _popen_detached_win32(cmd: List[str], child_env: Dict[str, str]):
    """F6: Windows 分离进程，带 breakaway-from-job + 降级 fallback。

    先以 ``DETACHED|NEW_PROCESS_GROUP|NO_WINDOW|BREAKAWAY_FROM_JOB`` 启动；
    若 job 不允许 breakaway（winerror 5/801），捕获后降级为原三 flags 重试，
    避免「加守卫反而让本来能起的 daemon 起失败」。其他 OSError 原样抛出，
    由调用方既有 ``except OSError`` 处理。返回 subprocess.Popen 对象。
    """
    with_breakaway = _WIN32_DETACHED_BASE_FLAGS | CREATE_BREAKAWAY_FROM_JOB
    common = dict(
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        env=child_env,
    )
    try:
        return subprocess.Popen(cmd, creationflags=with_breakaway, **common)
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror not in _WIN32_BREAKAWAY_FALLBACK_ERRORS:
            raise
        logger.warning(
            "CREATE_BREAKAWAY_FROM_JOB 不被允许(winerror=%s)，降级原 flags 重试",
            winerror)
        return subprocess.Popen(cmd, creationflags=_WIN32_DETACHED_BASE_FLAGS, **common)


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
        endpoint: Daemon endpoint（单轨 HTTPS，如 https://127.0.0.1:<port>）。
                  非 HTTP(S) 形式（裸路径 / Named Pipe / unix:// / tcp://）
                  一律 fail-closed 返回 None，不存在 UDS / Named Pipe / bridge 回退。
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

        # 首次连接失败时尝试唤起 daemon（仅一次）。单轨 HTTPS：不再传任何
        # socket/pipe 参数；daemon 自行监听并写 authority-scoped manifest。
        # 非 loopback（远端 authority）端点不得在本机误启 daemon。
        if not launch_attempted and _is_local_http_endpoint(endpoint):
            launch_attempted = True
            _start_daemon_platform(endpoint)
        elif not _is_local_http_endpoint(endpoint):
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
    r"""尝试建立到 daemon endpoint 的连接（单轨：仅 HTTPS/HTTP）。

    ADR-002 单轨：唯一允许的 daemon RPC 传输是 HTTPS+mTLS。裸路径、
    `\\.\pipe\...`、`unix://`、`tcp://` 等非 HTTP(S) 形式一律返回 None
    （fail-closed，不存在 UDS / Named Pipe / bridge 回退）。

    Returns:
        已连接的 TCP socket，或 None（连接失败 / endpoint 形式非法）。
    """
    if not _is_http_endpoint(endpoint):
        return None
    return _transport_connect_tcp(endpoint)


def _is_http_endpoint(endpoint: str) -> bool:
    """判断 endpoint 是否为 HTTPS/HTTP 端点。"""
    return endpoint.startswith(("https://", "http://"))


def _is_local_http_endpoint(endpoint: str) -> bool:
    """判断 endpoint 是否为本机 HTTP(S) 端点（可本机唤起）。"""
    if not _is_http_endpoint(endpoint):
        return False
    from callwarden.config import is_loopback_host, parse_http_endpoint_host

    try:
        _scheme, host, _port = parse_http_endpoint_host(endpoint)
    except ValueError:
        return False
    return bool(host) and is_loopback_host(host)


def _try_connect_tcp(endpoint: str) -> Dict[str, Any]:
    """TCP bridge 连通探测（SRV-005：authority 已下沉 Rust daemon）。

    原实现为本地 socket connect 并返回 socket；下沉后经 daemon RPC
    `mcp.daemon_autostart.try_connect_tcp` 以探测语义（connect + 立即关闭）
    返回结果 dict（`{"connectable": bool, "host": ..., "port": ..., "error": ...}`）。
    需要活连接的 transport 通信见 `_transport_connect_tcp`（RPC 无法传递 socket）。
    fail-closed：daemon 不可用抛 DaemonUnavailableError（不回退本地探测）。
    """
    result = _call_daemon_rpc(
        "mcp.daemon_autostart.try_connect_tcp",
        {"endpoint": endpoint, "timeout": CONNECT_TIMEOUT},
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"_try_connect_tcp: daemon 返回非对象结果: {result!r}")
    return result


def _transport_connect_tcp(endpoint: str) -> Optional[socket.socket]:
    """Transport bootstrap：客户端连接自身 daemon 的 TCP 活连接。

    SRV-005：探测 authority 已下沉 Rust daemon（见 `_try_connect_tcp`）；
    但 transport 通信所需的活连接无法经 RPC 传递（RPC 本身依赖此连接，
    先有鸡先有蛋约束），故保留最小本地 transport 连接——transport 适配
    职责，非 DB/业务 authority（不 open SQLite、不执行业务 SQL）。
    """
    host_port = endpoint
    for prefix in ("tcp://", "https://", "http://"):
        host_port = host_port.removeprefix(prefix)
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


# ---------------------------------------------------------------------------
# 平台唤起实现
# ---------------------------------------------------------------------------


def _start_daemon_platform(endpoint: str) -> bool:
    """按平台选择唤起方式启动 daemon。

    Returns:
        True 表示启动命令已发出（不代表 daemon 已就绪），False 表示启动失败。
    """
    if sys.platform == "win32":
        return _start_daemon_windows()
    elif sys.platform == "darwin":
        return _start_daemon_macos(endpoint)
    else:
        return _start_daemon_linux(endpoint)


def _start_daemon_windows() -> bool:
    """Windows: 启动分离进程，客户端退出后 daemon 仍存活 [Req 14.24]。

    单轨 HTTPS：daemon 自行监听并写 authority-scoped manifest，绝不传
    `--socket` 之类 UDS/pipe 参数。
    """
    daemon_bin = _find_daemon_binary()
    if daemon_bin is None:
        logger.error("未找到 cw_daemon 可执行文件，无法自动唤起")
        return False

    # P0 修复：显式注入权威任务库路径（~/.callwarden/callwarden.db），
    # 确保 daemon 与 Python `cw task` CLI 共享同一套任务状态。
    # daemon 通过环境变量 CW_DAEMON_TASK_DB 覆盖默认推导路径。
    from callwarden.config import DB_PATH as _AUTHORITY_TASK_DB
    child_env = dict(os.environ)
    child_env["CW_DAEMON_TASK_DB"] = _AUTHORITY_TASK_DB

    try:
        # F6: 经 helper 以 breakaway-from-job flags 启动（job 不允许时自动降级），
        # 脱离 IDE/宿主 job object 的 kill-on-close 连坐。
        _popen_detached_win32([daemon_bin], child_env)
        logger.info("Windows 分离进程已启动: %s", daemon_bin)
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
    env_bin = os.environ.get(
        "CW_DAEMON_BIN") or os.environ.get("CW_DAEMON_BINARY")
    if env_bin and os.path.isfile(env_bin):
        return env_bin

    # 权威 runtime 部署位置优先于 PATH / 开发构建产物：
    # ~/.callwarden/runtime/current/cw-daemon.exe 是 refresh_shared_runtime.ps1
    # 部署的生产 daemon（AGENTS.md §43）。autostart 必须拉起这个而非开发树的
    # debug/release 产物，否则 spawn 出的 daemon 与已部署 runtime 不一致。
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.path.expanduser("~")
    runtime_name = "cw-daemon.exe" if sys.platform == "win32" else "cw-daemon"
    runtime_bin = os.path.join(home, ".callwarden", "runtime", "current", runtime_name)
    if os.path.isfile(runtime_bin):
        return runtime_bin

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


def _authority_id() -> str:
    """当前 authority_id（D3 host 级守卫的统一取值点）。"""
    from callwarden.config import get_http_authority_id
    return get_http_authority_id()


def authority_daemon_diagnostic(authority_id: str = "") -> Dict[str, Any]:
    """读权威 manifest + host 注册表，构造 E_DAEMON_ALREADY_RUNNING 结构化诊断。

    判活铁律（合同 r2）：pid **且** process_start_time 双键匹配才算活实例。
    仅 pid 存活不足以判活——现场 PID 30200 被 smartscreen.exe 复用。
    manifest 是 daemon 权威；注册表只是轻量索引；冲突以 manifest 为准。

    Returns:
        诊断 dict，含 code/pid/endpoint/manifest/process_start_time/
        manifest_path/registry_entries/job_status/alive。
    """
    from callwarden.config import get_http_manifest_path
    from .host_process_registry import (
        compare_with_manifest,
        is_process_alive,
        read_all,
    )

    authority_id = authority_id or _authority_id()
    manifest_path = get_http_manifest_path(authority_id)
    manifest: Dict[str, Any] = {}
    read_error = ""
    try:
        manifest = read_http_manifest(manifest_path)
    except Exception as exc:  # DaemonRemoteError(MISSING/STALE) 等
        read_error = f"{type(exc).__name__}: {exc}"

    pid = manifest.get("pid")
    try:
        pid = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid = None
    start_time = manifest.get("process_start_time")
    try:
        start_time = int(start_time) if start_time is not None else None
    except (TypeError, ValueError):
        start_time = None
    endpoint = manifest.get("endpoint", "") or ""
    alive = is_process_alive(pid, start_time) if (pid and start_time) else False

    # F5：复用 host_process_registry.compare_with_manifest 的权威比对结论，
    # 让 E_DAEMON_ALREADY_RUNNING 诊断带上「manifest vs 注册表」的结构化对比
    # （conflict / authority=manifest / manifest_alive / registry_active_daemons）。
    # 传规范化副本（pid/start_time 已转 int），与外层 alive 判定保持同一双键口径。
    cmp_manifest = dict(manifest)
    cmp_manifest["pid"] = pid
    cmp_manifest["process_start_time"] = start_time
    cmp_manifest["endpoint"] = endpoint
    manifest_comparison = compare_with_manifest(authority_id, cmp_manifest)

    return {
        "code": E_DAEMON_ALREADY_RUNNING,
        "authority_id": authority_id,
        "manifest_path": manifest_path,
        "manifest_read_error": read_error,
        "pid": pid,
        "process_start_time": start_time,
        "endpoint": endpoint,
        "manifest": manifest,
        "registry_entries": read_all(authority_id),
        # F5：manifest 权威比对（原 compare_with_manifest 生产死代码，此处复用）。
        "manifest_comparison": manifest_comparison,
        # 作业状态：全量构建拥塞期 health 超时但 daemon/作业仍活，故标 busy。
        # 这只是一句结构化说明，不伪造 daemon 内部实时进度。
        "job_status": ("alive_but_unresponsive_probe" if alive else "not_running"),
        "alive": alive,
        "restart_hint": "cw daemon restart --force",
    }


def spawn_http_daemon(wait_window: float = 15.0) -> Optional[str]:
    """启动本机 HTTP daemon(detached 进程),返回就绪后的 endpoint;失败返回 None。

    D3 自愈守卫（根修卡 T-1795507699058-41d9fcbc / 合同 r2），严格顺序：

    1. **manifest 权威判活**：读 authority manifest，pid + process_start_time
       双键判活。活着 → 绝不 spawn 第二实例，记录 E_DAEMON_ALREADY_RUNNING
       结构化诊断，返回 None 并打日志（由调用方上报）。
    2. **host 级互斥**：拿 ``acquire_host_daemon_mutex()``（authority + task_db，
       同机所有 endpoint 共享一把锁），消除 TOCTOU；拿不到 → 说明别的会话正在
       启动，本会话退避返回 None。
    3. **显式权威 bind**：不再裸 ``Popen([daemon_bin])``。从 manifest 读权威
       endpoint（或其 host:port）显式传 ``--http-bind``；读不到权威 endpoint
       即 fail-closed（无参启动被拒，绝不落到默认 5854 孤儿绑定）。
    4. **启动后注册自身**：Popen 成功后把 {role=daemon, pid, start_time,
       endpoint_or_bind, binary_sha256} upsert 进 host 进程注册表。
    5. 等待 manifest 翻新 + health 就绪（沿用退避窗口）。

    启动约定(与 cw_daemon.rs H6 一致):
    - 显式 CW_DAEMON_TRANSPORT=http:防止继承环境把 transport 设成 named-pipe/uds
      而禁用 HTTP;
    - 注入 CW_DAEMON_TASK_DB=权威库:与 Python cw task CLI 共享同一任务状态;
    - detached 进程:客户端退出后 daemon 仍存活。

    平台:当前仅实现 Windows 与 POSIX 通用 spawn(Popen detached)。macOS/Linux
    的 launchd/systemd 激活由 _start_daemon_platform 负责,本函数是 HTTP 直启补充。
    """
    global _spawn_last_attempt_monotonic

    # cooldown：1s 探活抖动不得连续触发 spawn（合同 r2 验收栏）。
    now = time.monotonic()
    if (_spawn_last_attempt_monotonic is not None
            and now - _spawn_last_attempt_monotonic < D3_SPAWN_COOLDOWN):
        logger.info(
            "spawn_http_daemon: 仍处于 %.1fs 冷却窗口，跳过重复 spawn",
            D3_SPAWN_COOLDOWN)
        return None

    authority_id = _authority_id()

    # ---- 1) manifest 权威判活：已有活 daemon 绝不拉第二实例 ----
    diag = authority_daemon_diagnostic(authority_id)
    if diag["alive"]:
        logger.warning(
            "spawn_http_daemon: %s 权威 daemon 仍存活 "
            "(pid=%s, start_time=%s, endpoint=%s)，拒绝拉起第二实例；%s",
            E_DAEMON_ALREADY_RUNNING, diag["pid"],
            diag["process_start_time"], diag["endpoint"], diag["restart_hint"])
        return None

    # ---- 2) host 级互斥：临界区内再做一次判活，消除 TOCTOU ----
    # D3/F2 唯一权威守卫：本机所有活跃 daemon 拉起路径（mcp_server / cli
    # `cw daemon start` / daemon_client.call_with_autostart）最终都汇聚本函数，
    # 只有此处 acquire_host_daemon_mutex() 用 host 级锁（authority+task_db，同机
    # 所有 endpoint 共享一把）串行化「判活→bind→Popen→注册→health」全临界区。
    # 外层 call_with_autostart 的 endpoint 级 DaemonMutex 只是请求期去重，
    # **非权威**——两会话不同 endpoint 各自持锁也能走到这里，但只会有一个
    # 拿到 host 锁真正 spawn，第二个在此退避（见 step#1 注释与并发负向测试）。
    from .daemon_mutex import acquire_host_daemon_mutex
    mutex = acquire_host_daemon_mutex()
    if mutex is None:
        logger.info("spawn_http_daemon: 未取得 host 级 daemon 互斥，退避不 spawn")
        return None

    _spawn_last_attempt_monotonic = time.monotonic()
    try:
        diag2 = authority_daemon_diagnostic(authority_id)
        if diag2["alive"]:
            logger.warning(
                "spawn_http_daemon: 临界区内复判 %s 权威 daemon 已存活，放弃 spawn",
                E_DAEMON_ALREADY_RUNNING)
            return None

        # ---- 3) 显式权威 bind：无参启动 fail-closed ----
        bind_spec = _authoritative_bind_spec(diag2)
        if not bind_spec:
            logger.error(
                "spawn_http_daemon: 无法从权威 manifest 解析 bind，fail-closed "
                "拒绝无参启动（禁止落到默认端口产生孤儿 daemon）")
            return None

        daemon_bin = _find_daemon_binary()
        if daemon_bin is None:
            logger.error("spawn_http_daemon: 未找到 cw-daemon 可执行文件(检查 "
                         "CW_DAEMON_BIN 或 ~/.callwarden/runtime/current/)")
            return None

        from callwarden.config import DB_PATH as _AUTHORITY_TASK_DB
        child_env = dict(os.environ)
        child_env["CW_DAEMON_TRANSPORT"] = "http"
        child_env["CW_DAEMON_TASK_DB"] = _AUTHORITY_TASK_DB
        # 显式权威 bind：不再 pop 掉交给默认动态端口。
        child_env["CW_DAEMON_HTTP_BIND"] = bind_spec
        cmd = [daemon_bin, "--http-bind", bind_spec]

        try:
            if sys.platform == "win32":
                # F6: 与 _start_daemon_windows 同一 helper——breakaway-from-job
                # 脱离 IDE/宿主 job object 连坐，job 不允许时自动降级重试。
                proc = _popen_detached_win32(cmd, child_env)
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                    env=child_env,
                )
            logger.info("spawn_http_daemon: 已启动 detached daemon: %s --http-bind %s",
                        daemon_bin, bind_spec)
        except OSError as exc:
            logger.error("spawn_http_daemon: 启动失败: %s", exc)
            return None

        # ---- 4) 注册自身进 host 进程注册表（role=daemon）----
        try:
            from .host_process_registry import register_process
            import hashlib
            bin_sha = ""
            try:
                with open(daemon_bin, "rb") as fh:
                    bin_sha = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                pass
            register_process(
                authority_id=authority_id, role="daemon", pid=proc.pid,
                endpoint_or_bind=bind_spec, binary_sha256=bin_sha)
        except Exception as exc:
            # 注册失败不阻断启动：manifest 仍是权威，注册表只是索引。
            logger.warning("spawn_http_daemon: 注册表 upsert 失败（不阻断）: %s", exc)

        # ---- 5) 等 manifest 翻新 + health 就绪 ----
        deadline = time.monotonic() + wait_window
        backoff = BACKOFF_BASE
        while time.monotonic() < deadline:
            try:
                endpoint, _manifest = resolve_http_endpoint_and_manifest(validate=True)
                if endpoint and try_http_connect(endpoint, timeout=2.0):
                    logger.info("spawn_http_daemon: daemon 就绪 @ %s", endpoint)
                    return endpoint
            except Exception:
                pass  # manifest 尚未写出/stale,继续等
            time.sleep(min(backoff, max(0.0, deadline - time.monotonic())))
            backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)
        logger.error("spawn_http_daemon: 启动后 %.1fs 内未就绪", wait_window)
        return None
    finally:
        mutex.release()


def _authoritative_bind_spec(diag: Dict[str, Any]) -> str:
    """从权威 manifest 诊断解析 ``--http-bind`` 参数（host:port）。

    - 显式 CW_DAEMON_HTTP_ENDPOINT 优先（仍是 authority 的显式声明）；
    - 否则取 manifest.endpoint 的 host:port；
    - 都没有 → 返回 ""（fail-closed，禁止无参启动落到默认端口）。
    """
    from callwarden.config import get_http_daemon_endpoint, parse_http_endpoint_host

    for candidate in (get_http_daemon_endpoint(), diag.get("endpoint", "")):
        if not candidate:
            continue
        try:
            _scheme, host, port = parse_http_endpoint_host(candidate)
        except ValueError:
            continue
        if host and port:
            return f"{host}:{port}"
    return ""


def get_default_endpoint() -> str:
    """获取默认 Daemon endpoint（ADR-002 单轨：HTTPS+mTLS）。

    优先读取 authority manifest 中登记的 endpoint；读不到时返回本机默认
    HTTPS 端点。绝不返回 socket / Named Pipe 路径。
    """
    try:
        endpoint, _manifest = resolve_http_endpoint_and_manifest()
    except Exception:
        endpoint = ""
    return endpoint or "https://127.0.0.1:6374"


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
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_wchar_p, ctypes.POINTER(
                wintypes.DWORD)
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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


# Windows 上 daemon 用 MoveFileExW(REPLACE_EXISTING) 原子替换 manifest；替换瞬间
# 目标文件处于 delete-pending 状态，此时 open() 会瞬时得到 WinError 5
# （ERROR_ACCESS_DENIED）而不是旧/新任一版本内容。6 并发门禁下实测复现为
# `E_HTTP_MANIFEST_STALE: Permission denied`。仅对该瞬时窗口做有界重试，
# 其它 OSError（真正的权限/IO 故障）仍立即 fail-closed。
_MANIFEST_READ_RETRY_BUDGET_S = 1.0
_MANIFEST_READ_RETRY_INTERVAL_S = 0.05


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

    deadline = time.monotonic() + _MANIFEST_READ_RETRY_BUDGET_S
    while True:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            break
        except FileNotFoundError:
            raise DaemonRemoteError(
                E_HTTP_MANIFEST_MISSING, f"HTTP manifest 文件不存在: {path}"
            )
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise DaemonRemoteError(
                    E_HTTP_MANIFEST_STALE, f"HTTP manifest 读取/解析失败: {exc}"
                )
            time.sleep(_MANIFEST_READ_RETRY_INTERVAL_S)
        except (OSError, ValueError) as exc:
            raise DaemonRemoteError(
                E_HTTP_MANIFEST_STALE, f"HTTP manifest 读取/解析失败: {exc}"
            )
    if not isinstance(data, dict):
        raise DaemonRemoteError(E_HTTP_MANIFEST_STALE,
                                "HTTP manifest 不是 JSON 对象")
    return data


def validate_http_endpoint_loopback(endpoint: str, allow_remote: bool = False) -> str:
    """校验 endpoint 为 https（ADR-002 单轨）；返回规范化 endpoint（去尾斜杠）。

    - 明文 `http://` 一律拒绝（`E_TLS_REQUIRED`，单轨不保留降级路径）。
    - 默认要求 loopback；`allow_remote=True`（manifest `bind_scope=remote`）时
      允许非 loopback。

    Raises:
        DaemonRemoteError(E_TLS_REQUIRED / E_HTTP_MVP_LOOPBACK_ONLY)。
    """
    from callwarden.config import (
        E_HTTP_MVP_LOOPBACK_ONLY,
        E_TLS_REQUIRED,
        is_loopback_host,
        parse_http_endpoint_host,
    )

    try:
        scheme, host, _port = parse_http_endpoint_host(endpoint)
    except ValueError as exc:
        raise DaemonRemoteError(E_TLS_REQUIRED, str(exc))
    if scheme != "https":
        raise DaemonRemoteError(
            E_TLS_REQUIRED,
            f"仅允许 https（ADR-002 单轨 mTLS），拒绝 scheme={scheme!r}",
        )
    if not allow_remote and (not host or not is_loopback_host(host)):
        raise DaemonRemoteError(
            E_HTTP_MVP_LOOPBACK_ONLY,
            f"默认仅允许 loopback endpoint，拒绝 host={host!r}",
        )
    if not host:
        raise DaemonRemoteError(E_TLS_REQUIRED, "endpoint 缺少主机名")
    return endpoint.rstrip("/")


def validate_http_manifest(
    manifest: Dict[str, Any], expected_authority_id: str = ""
) -> Dict[str, Any]:
    """联网前完整校验 manifest（ADR-002 §6 / §15.4 v2）。

    依次校验：schema（v2；旧 v1 视为过期而非降级）/ manifest_hash /
    security_profile / authority / 协议交集 / TLS 块（CA pin 指纹）/
    endpoint（https + 按 bind_scope 判定 loopback）/ PID 存活 / executable 匹配。
    任一不符抛结构化 DaemonRemoteError（fail-closed，不连接、不删除 manifest）。
    """
    from callwarden.config import (
        E_HTTP_MANIFEST_HASH_MISMATCH,
        E_HTTP_MANIFEST_STALE,
        E_MTLS_UNKNOWN_CA,
        E_PROTOCOL_VERSION_UNSUPPORTED,
        HTTP_MANIFEST_SCHEMA_VERSION_V2,
        HTTP_PROTOCOL_VERSION,
        HTTPS_MTLS_TRANSPORT_PROFILE,
        compute_http_manifest_hash,
        norm_path,
    )

    if not isinstance(manifest, dict):
        raise DaemonRemoteError(E_HTTP_MANIFEST_STALE, "manifest 不是 JSON 对象")

    # schema：只接受 v2；旧 v1（manifest_version / schema_version=1）视为过期。
    if manifest.get("schema") != HTTP_MANIFEST_SCHEMA_VERSION_V2:
        legacy = manifest.get("manifest_version") or (
            "v1" if manifest.get("schema_version") == 1 else None
        )
        raise DaemonRemoteError(
            E_HTTP_MANIFEST_STALE,
            "manifest 非 v2（旧 v1 manifest 视为过期，不降级到明文）"
            if legacy
            else f"manifest schema 不符: {manifest.get('schema')!r}",
        )

    # manifest_hash（排除自身后 canonical JSON 的 SHA-256）
    if manifest.get("manifest_hash") != compute_http_manifest_hash(manifest):
        raise DaemonRemoteError(
            E_HTTP_MANIFEST_HASH_MISMATCH,
            "manifest_hash 校验失败（可能被篡改或写入未落盘）",
        )

    # security_profile 必须严格等于 https_mtls_v1
    if manifest.get("security_profile") != HTTPS_MTLS_TRANSPORT_PROFILE:
        raise DaemonRemoteError(
            E_HTTP_MANIFEST_STALE,
            f"security_profile 非 {HTTPS_MTLS_TRANSPORT_PROFILE!r}: "
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

    # TLS 块：CA PEM + pin 指纹必须自洽（篡改 CA 即可被检出）
    tls = manifest.get("tls")
    if not isinstance(tls, dict):
        raise DaemonRemoteError(E_HTTP_MANIFEST_STALE, "manifest 缺少 tls 块")
    ca_pem = tls.get("ca_cert_pem") or ""
    if not ca_pem:
        raise DaemonRemoteError(E_MTLS_UNKNOWN_CA, "manifest 缺少 tls.ca_cert_pem")
    fingerprint = (tls.get("ca_fingerprint_sha256") or "").strip().lower()
    if fingerprint and fingerprint != _pem_sha256(ca_pem):
        raise DaemonRemoteError(
            E_MTLS_UNKNOWN_CA,
            "manifest CA 指纹与 ca_cert_pem 不一致（CA pin 失败）",
        )

    # endpoint：https；bind_scope=remote 时允许非 loopback。
    allow_remote = manifest.get("bind_scope") == "remote"
    validate_http_endpoint_loopback(manifest.get("endpoint", ""), allow_remote=allow_remote)

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


def _pem_sha256(pem: str) -> str:
    """PEM 证书 DER 的 SHA-256（用于 manifest CA pin 校验）。

    §9.34 C rotate-ca：`tls.ca_cert_pem` 在双 CA 窗口期为
    「新 CA + 旧 CA」的 PEM bundle——`ca_fingerprint_sha256` 恒为 bundle
    **首张**（当前签发 CA）的指纹，故 pin 校验同样取首张证书块。
    """
    import hashlib
    import ssl

    first_block = _first_pem_cert_block(pem)
    if first_block is None:
        return ""
    try:
        der = ssl.PEM_cert_to_DER_cert(first_block)
    except (ValueError, TypeError):
        return ""
    return hashlib.sha256(der).hexdigest()


def _first_pem_cert_block(pem: str) -> Optional[str]:
    """返回 PEM 文本中的首个 CERTIFICATE 块（含 BEGIN/END 行）。

    支持 bundle（多证书）；无合法块返回 None。
    """
    lines = pem.splitlines()
    begin = "-----BEGIN CERTIFICATE-----"
    end = "-----END CERTIFICATE-----"
    collecting: Optional[list] = None
    for line in lines:
        stripped = line.strip()
        if collecting is None:
            if stripped == begin:
                collecting = [line]
        else:
            collecting.append(line)
            if stripped == end:
                return "\n".join(collecting)
    return None


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
        # 显式 endpoint：https 必填；manifest 仅作可选权威校验（remote 时允许非 loopback）。
        manifest = _load_manifest_for_authority(manifest_path, authority_id)
        allow_remote = bool(manifest and manifest.get("bind_scope") == "remote")
        endpoint = validate_http_endpoint_loopback(explicit, allow_remote=allow_remote)
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
    """对 HTTP endpoint 做一次短超时连通性探针（SRV-005：authority 已下沉 Rust daemon）。

    原实现为本地 socket connect；下沉后经 daemon RPC
    `mcp.daemon_autostart.try_http_connect` 以探测语义（TCP connect + 立即关闭）
    返回 bool，与原签名一致（ensure_http_daemon 等调用方兼容）。
    fail-closed：daemon 不可用抛 DaemonUnavailableError（不回退本地探测）。
    """
    result = _call_daemon_rpc(
        "mcp.daemon_autostart.try_http_connect",
        {"endpoint": endpoint, "timeout": timeout},
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"try_http_connect: daemon 返回非对象结果: {result!r}")
    return bool(result.get("connectable"))


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
    # CR15（client_convergence_codereview_20260909 §5.8）：deadline 必须包含
    # window——否则首轮探针失败后 now >= deadline 立即成立，等待窗口退化为
    # 单次探针，退避逻辑成死代码。
    deadline = time.monotonic() + window
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


# ---------------------------------------------------------------------------
# daemon RPC 委托（SRV-005：探测 authority 下沉后的 fail-closed 客户端）
# ---------------------------------------------------------------------------


def _call_daemon_rpc(method: str, params: Dict[str, Any]) -> Any:
    """经 daemon 统一 fail-closed 客户端发起 RPC（不回退本地探测/SQLite）。

    函数体内延迟导入，避免 daemon_autostart → _mcp_common → daemon_client
    → daemon_autostart 的模块级循环依赖（transport bootstrap 先于 RPC 存在）。
    """
    from ._mcp_common import _call_daemon_rpc as _rpc

    return _rpc(method, params)
