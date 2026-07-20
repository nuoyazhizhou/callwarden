"""
Phase 5: Daemon IPC 传输层

设计参考：daemon-ipc-security.md §2-§3

传输协议：
1. 小/中文件（≤16MB）：长度分帧 UDS stream（send_framed_stream）
2. 大文件（>16MB）：memfd_create + seals + SCM_RIGHTS（send_via_memfd）
3. Windows/macOS 降级：memfd 不可用时自动降级为分帧传输

安全边界：
- agent 永不直接写 CAS
- daemon 重新计算 sha256，不信任 agent 提供的 hash
- memfd 四重 seal 校验（SHRINK|GROW|WRITE|SEAL）
"""

from __future__ import annotations

import array
import hashlib
import json
import os
import socket
import struct
import sys

# ============================================================
# 常量
# ============================================================

MAX_MSG_BYTES = 16 * 1024 * 1024  # 16 MB — 超过则走 memfd
MAX_MEMFD_BYTES = 256 * 1024 * 1024  # 256 MB — 单 memfd 上限，防 OOM

# G10 inflight bytes 限制（规范：daemon-ipc-security.md §4）
# S7 不变量：inflight bytes 超任一限制 → 暂停该连接 recv
MAX_CONN_QUEUED_BYTES = 256 * 1024 * 1024  # 256 MB — 单连接排队上限
MAX_DAEMON_INFLIGHT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB — 全局 inflight 上限
MAX_UID_INFLIGHT_BYTES = 512 * 1024 * 1024  # 512 MB — 单 UID inflight 上限

# Linux memfd seal flags（来自 <linux/memfd.h>）
F_SEAL_SEAL = 0x0001
F_SEAL_SHRINK = 0x0002
F_SEAL_GROW = 0x0004
F_SEAL_WRITE = 0x0008

# memfd_create flags
MFD_CLOEXEC = 0x0001
MFD_ALLOW_SEALING = 0x0002

# 平台检测
_IS_LINUX = sys.platform.startswith("linux")
_IS_WINDOWS = sys.platform.startswith("win")


# ============================================================
# 异常
# ============================================================


class ProtocolError(Exception):
    """IPC 协议错误。"""

    pass


# ============================================================
# 分帧 stream 传输（小/中文件）
# ============================================================


def send_framed_stream(sock, msg_type: int, payload: dict, canonical_bytes: bytes):
    """长度分帧 UDS stream（小/中文件，≤16MB）。

    规范：daemon-ipc-security.md §2.2
    消息头格式：| msg_type(1B) | payload_len(4B, big-endian) | canonical_len(8B, big-endian) |

    如果总长度超过 MAX_MSG_BYTES，自动降级为 send_via_memfd。
    """
    payload_json = json.dumps(payload).encode("utf-8")
    total_len = 1 + 4 + 8 + len(payload_json) + len(canonical_bytes)
    if total_len > MAX_MSG_BYTES:
        return send_via_memfd(sock, msg_type, payload, canonical_bytes)
    header = struct.pack(">BIQ", msg_type, len(payload_json), len(canonical_bytes))
    sock.sendall(header + payload_json + canonical_bytes)


# ============================================================
# memfd 传输（大文件 > 16MB）
# ============================================================


def _write_all(fd: int, data: bytes):
    """循环写直到全部写完——os.write 可能短写。

    规范：daemon-ipc-security.md §3.1 (S4)
    """
    view = memoryview(data)
    total = 0
    while total < len(view):
        n = os.write(fd, view[total:])
        if n == 0:
            raise OSError("memfd write returned 0 (disk full?)")
        total += n


def _linux_memfd_create(name: str, flags: int) -> int:
    """Linux memfd_create 系统调用（ctypes）。

    规范：daemon-ipc-security.md §3.1
    需要 Linux 3.17+。
    """
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    # memfd_create(const char *name, unsigned int flags)
    libc.memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    libc.memfd_create.restype = ctypes.c_int
    fd = libc.memfd_create(name.encode("utf-8"), flags)
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(f"memfd_create failed: {err}")
    return fd


def _linux_seal(fd: int, seals: int):
    """Linux fcntl F_ADD_SEALS。

    规范：daemon-ipc-security.md §3.1
    完整不可变集合：F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL
    """
    import ctypes
    import ctypes.util

    F_ADD_SEALS = 1033  # <linux/fcntl.h>
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.fcntl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    libc.fcntl.restype = ctypes.c_int
    result = libc.fcntl(fd, F_ADD_SEALS, seals)
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(f"F_ADD_SEALS failed: {err}")


def _linux_get_seals(fd: int) -> int:
    """Linux fcntl F_GET_SEALS。"""
    import ctypes
    import ctypes.util

    F_GET_SEALS = 1034
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.fcntl.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.fcntl.restype = ctypes.c_int
    result = libc.fcntl(fd, F_GET_SEALS)
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(f"F_GET_SEALS failed: {err}")
    return result


def create_sealed_memfd(canonical_bytes: bytes) -> int:
    """创建带完整 seal 的 memfd，写入 canonical_bytes。

    G10（2026-07-20 批次7）：暴露公共 API 供 agent_protocol.py 大文件路径使用。
    替代之前的「写临时文件 + 传普通 FD」模式——memfd 的 seal flags 让 daemon 端
    能识别为不可变 memfd，执行四重校验（含 seal flags 检查）。

    seal 集合：F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL
    —— 完全不可变，daemon 读取期间内容不会变化。

    Args:
        canonical_bytes: 要写入 memfd 的字节流

    Returns:
        memfd FD（已 seal，已写入内容，文件指针在末尾；调用方负责 close）

    Raises:
        OSError: memfd_create / write / seal 任一步失败
        AttributeError: 非 Linux 平台（memfd_create 不可用）
    """
    fd = _linux_memfd_create("cw_canonical", MFD_CLOEXEC | MFD_ALLOW_SEALING)
    try:
        _write_all(fd, canonical_bytes)
        _linux_seal(fd, F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


def send_via_memfd(sock, msg_type: int, payload: dict, canonical_bytes: bytes):
    """大文件用 memfd_create + seals + SCM_RIGHTS 传 FD。

    规范：daemon-ipc-security.md §3.1
    修复 T-1783751532213-3ef2

    非 Linux 平台降级为分帧传输（不使用 memfd）。
    """
    if not _IS_LINUX:
        # Windows/macOS 降级：分帧传输（不考虑 16MB 限制）
        return _send_framed_unlimited(sock, msg_type, payload, canonical_bytes)

    try:
        fd = create_sealed_memfd(canonical_bytes)
    except (AttributeError, OSError):
        # memfd 不可用，降级
        return _send_framed_unlimited(sock, msg_type, payload, canonical_bytes)

    try:
        payload["canonical_len"] = len(canonical_bytes)
        _send_msg_with_fd(sock, msg_type, payload, fd)
    finally:
        os.close(fd)  # daemon 持有 FD 后 agent 关闭自己的引用


def _send_framed_unlimited(sock, msg_type: int, payload: dict, canonical_bytes: bytes):
    """非 Linux 降级传输：分帧但不限制 16MB。"""
    payload_json = json.dumps(payload).encode("utf-8")
    header = struct.pack(">BIQ", msg_type, len(payload_json), len(canonical_bytes))
    sock.sendall(header + payload_json + canonical_bytes)


def _send_msg_with_fd(sock, msg_type: int, payload: dict, fd: int):
    """通过 SCM_RIGHTS 传递 FD（Linux）。

    规范：daemon-ipc-security.md §3.1
    """
    payload_json = json.dumps(payload).encode("utf-8")
    header = struct.pack(">BI", msg_type, len(payload_json))
    msg = header + payload_json

    # SCM_RIGHTS ancillary data
    fds = array.array("i", [fd])
    sock.sendmsg([msg], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds.tobytes())])


# ============================================================
# daemon 侧接收与四重校验
# ============================================================


def recv_via_memfd(sock, expected_canonical_len: int, expected_content_hash: str, peer_uid: int):
    """daemon 接收端校验——不信任 agent 提供的 memfd。

    规范：daemon-ipc-security.md §3.2
    修复 T-1783751532213-3ef2

    四重校验，任一失败则拒绝并关闭 FD：
    1. fstat().st_size == expected_canonical_len
    2. F_GET_SEALS 包含 F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL
    3. st_size <= MAX_MEMFD_BYTES
    4. sha256(memfd content) == expected_content_hash
    """
    fd, msg = _recv_msg_with_fd(sock)
    try:
        st = os.fstat(fd)

        # 1. 大小校验
        if st.st_size != expected_canonical_len:
            raise ProtocolError(
                f"memfd size mismatch: {st.st_size} != {expected_canonical_len}"
            )

        # 2. seal flags 校验（仅 Linux）
        if _IS_LINUX:
            actual_seals = _linux_get_seals(fd)
            required_seals = (
                F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL
            )
            if (actual_seals & required_seals) != required_seals:
                raise ProtocolError(
                    f"memfd missing seals: got {actual_seals:#x}, "
                    f"need {required_seals:#x}"
                )

        # 3. 最大尺寸校验
        if st.st_size > MAX_MEMFD_BYTES:
            raise ProtocolError(
                f"memfd exceeds MAX_MEMFD_BYTES: {st.st_size}"
            )

        # 4. 内容 hash 校验
        os.lseek(fd, 0, os.SEEK_SET)
        actual_hash = _sha256_streaming(fd, st.st_size)
        if actual_hash != expected_content_hash:
            raise ProtocolError("memfd content hash mismatch")

        os.lseek(fd, 0, os.SEEK_SET)
        return fd, msg
    except Exception:
        os.close(fd)
        raise


def _recv_msg_with_fd(sock):
    """接收 SCM_RIGHTS 传来的 FD（Linux）。"""
    # 先接收消息头
    header = _recv_exact(sock, 5)  # 1B msg_type + 4B payload_len
    msg_type, payload_len = struct.unpack(">BI", header)
    payload_json = _recv_exact(sock, payload_len).decode("utf-8")
    msg = json.loads(payload_json)

    # 接收 FD
    fds = array.array("i")
    msg_data, ancdata, flags, addr = sock.recvmsg(0, socket.CMSG_LEN(fds.itemsize))
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            fds.frombytes(cmsg_data[: fds.itemsize])
            return fds[0], msg
    raise ProtocolError("no FD received via SCM_RIGHTS")


def _recv_exact(sock, n: int) -> bytes:
    """精确接收 n 字节。"""
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("connection closed during recv")
        data += chunk
    return data


def _sha256_streaming(fd: int, size: int) -> str:
    """流式 sha256，不一次性载入。

    规范：daemon-ipc-security.md §3.2 (S6)
    """
    h = hashlib.sha256()
    remaining = size
    while remaining > 0:
        chunk_size = min(65536, remaining)
        data = os.read(fd, chunk_size)
        if not data:
            break
        h.update(data)
        remaining -= len(data)
    return h.hexdigest()


# ============================================================
# 统一发送入口
# ============================================================


def send_msg(sock, msg_type: int, payload: dict, canonical_bytes: bytes):
    """统一发送入口——自动选择分帧或 memfd。

    规范：daemon-ipc-security.md §6 (S10)
    传输路径对 agents 透明：SDK 封装 send_msg，自动选择分帧或 memfd。
    """
    total_len = 1 + 4 + 8 + len(json.dumps(payload)) + len(canonical_bytes)
    if total_len > MAX_MSG_BYTES:
        return send_via_memfd(sock, msg_type, payload, canonical_bytes)
    return send_framed_stream(sock, msg_type, payload, canonical_bytes)


# ============================================================
# G10: 已接收 FD 的 memfd 校验（daemon 侧）
# ============================================================


def is_memfd(fd: int) -> bool:
    """检测 FD 是否为 memfd（通过 F_GET_SEALS）。

    规范：daemon-ipc-security.md §3.2
    仅 Linux 支持 memfd；非 Linux 或非 memfd FD 返回 False。
    """
    if not _IS_LINUX:
        return False
    import fcntl
    try:
        seals = fcntl.fcntl(fd, F_GET_SEALS)
        return seals > 0
    except OSError:
        return False


def validate_memfd_fd(
    fd: int,
    expected_canonical_len: int,
    expected_content_hash: str,
    peer_uid: int,
) -> int:
    """G10 daemon 侧：对已接收的 memfd FD 执行四重校验。

    规范：daemon-ipc-security.md §3.2（与 recv_via_memfd 相同的四重校验，但
    FD 已经通过 SCM_RIGHTS 接收到，无需再调 _recv_msg_with_fd）。

    四重校验（任一失败抛 ProtocolError 并关闭 FD）：
    1. fstat().st_size == expected_canonical_len
    2. F_GET_SEALS 包含 F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL
    3. st_size <= MAX_MEMFD_BYTES
    4. sha256(memfd content) == expected_content_hash

    Args:
        fd: 已通过 SCM_RIGHTS 接收的 FD
        expected_canonical_len: agent 声明的 canonical bytes 长度
        expected_content_hash: agent 声明的 canonical bytes sha256 hex
        peer_uid: 发送方 UID（用于 owner 校验，可选）

    Returns:
        校验通过的 FD（已 lseek 到 0，可直接 os.read 或传给 parser）
    """
    try:
        st = os.fstat(fd)

        # 0. owner UID 校验（与 _validate_snapshot_frame 一致）
        if st.st_uid != peer_uid:
            raise ProtocolError(
                f"memfd owner_uid={st.st_uid}，peer_uid={peer_uid}"
            )

        # 1. 大小校验
        if st.st_size != expected_canonical_len:
            raise ProtocolError(
                f"memfd size mismatch: {st.st_size} != {expected_canonical_len}"
            )

        # 2. seal flags 校验（仅 Linux）
        if _IS_LINUX:
            import fcntl
            actual_seals = fcntl.fcntl(fd, F_GET_SEALS)
            required_seals = (
                F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL
            )
            if (actual_seals & required_seals) != required_seals:
                raise ProtocolError(
                    f"memfd missing seals: got {actual_seals:#x}, "
                    f"need {required_seals:#x}"
                )

        # 3. 最大尺寸校验
        if st.st_size > MAX_MEMFD_BYTES:
            raise ProtocolError(
                f"memfd exceeds MAX_MEMFD_BYTES: {st.st_size}"
            )

        # 4. 内容 hash 校验（流式 sha256，64KB chunk）
        os.lseek(fd, 0, os.SEEK_SET)
        actual_hash = _sha256_streaming(fd, st.st_size)
        if actual_hash != expected_content_hash:
            raise ProtocolError(
                f"memfd content hash mismatch: {actual_hash} != "
                f"{expected_content_hash}"
            )

        # 重置到开头，供后续 os.read 或传给 parser
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


# ============================================================
# G10: Inflight Bytes 跟踪器（S7 不变量）
# ============================================================


class InflightTracker:
    """G10: 跟踪 per-connection / per-UID / 全局 inflight bytes。

    规范：daemon-ipc-security.md §4
    S7 不变量：inflight bytes 超任一限制 → 暂停该连接 recv。

    三个维度：
    - per-connection: MAX_CONN_QUEUED_BYTES (256MB)
    - per-UID: MAX_UID_INFLIGHT_BYTES (512MB)
    - global: MAX_DAEMON_INFLIGHT_BYTES (2GB)

    线程安全：所有方法都加锁。
    """

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._conn_bytes: dict[int, int] = {}  # conn_id → bytes
        self._uid_bytes: dict[int, int] = {}  # uid → bytes
        self._total_bytes: int = 0

    def acquire(self, conn_id: int, uid: int, size: int) -> bool:
        """尝试为 size 字节分配 inflight 配额。

        Returns:
            True 若三个维度都未超限；False 若任一维度超限（调用方应暂停 recv）。
        """
        with self._lock:
            new_conn = self._conn_bytes.get(conn_id, 0) + size
            new_uid = self._uid_bytes.get(uid, 0) + size
            new_total = self._total_bytes + size

            if new_conn > MAX_CONN_QUEUED_BYTES:
                return False
            if new_uid > MAX_UID_INFLIGHT_BYTES:
                return False
            if new_total > MAX_DAEMON_INFLIGHT_BYTES:
                return False

            self._conn_bytes[conn_id] = new_conn
            self._uid_bytes[uid] = new_uid
            self._total_bytes = new_total
            return True

    def release(self, conn_id: int, uid: int, size: int) -> None:
        """释放 inflight 配额（处理完成后调用）。"""
        with self._lock:
            self._conn_bytes[conn_id] = max(
                0, self._conn_bytes.get(conn_id, 0) - size
            )
            self._uid_bytes[uid] = max(
                0, self._uid_bytes.get(uid, 0) - size
            )
            self._total_bytes = max(0, self._total_bytes - size)

    def stats(self) -> dict:
        """返回当前 inflight 统计（监控用）。"""
        with self._lock:
            return {
                "conn_bytes": dict(self._conn_bytes),
                "uid_bytes": dict(self._uid_bytes),
                "total_bytes": self._total_bytes,
                "limits": {
                    "max_conn_queued_bytes": MAX_CONN_QUEUED_BYTES,
                    "max_uid_inflight_bytes": MAX_UID_INFLIGHT_BYTES,
                    "max_daemon_inflight_bytes": MAX_DAEMON_INFLIGHT_BYTES,
                    "max_memfd_bytes": MAX_MEMFD_BYTES,
                },
            }
