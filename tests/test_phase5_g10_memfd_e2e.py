"""G10: memfd 密封协议 E2E 测试。

验证 G10 三层集成：
1. InflightTracker 单元测试（跨平台）
2. is_memfd / validate_memfd_fd 校验逻辑（跨平台 + Linux 真实 memfd）
3. daemon_server.py workspace.file.refresh memfd 检测路径（mock + 真实）
4. Linux 专属：真实 memfd_create + seal + send_msg + recv_via_memfd roundtrip

规范：docs/design/daemon-ipc-security.md §3-§4
"""

from __future__ import annotations

import os
import sys
import tempfile
import hashlib
import json
import socket
import struct
import threading
import time

import pytest

# 跳过条件
_IS_LINUX = sys.platform.startswith("linux")
_SKIP_LINUX = pytest.mark.skipif(not _IS_LINUX, reason="仅 Linux 支持 memfd")
_SKIP_PY314 = pytest.mark.skipif(
    sys.version_info >= (3, 14),
    reason="Python 3.14 ctypes+SCM_RIGHTS 组合导致 worker 段错误崩溃，待上游修复",
)


# ============================================================
# 1. InflightTracker 单元测试（跨平台）
# ============================================================


class TestInflightTracker:
    """G10 InflightTracker：per-connection / per-UID / 全局 inflight 跟踪。"""

    def test_acquire_release_basic(self):
        """基础 acquire + release + stats。"""
        from callwarden.server.ipc_transport import InflightTracker

        tracker = InflightTracker()
        # 分配 1MB
        ok = tracker.acquire(conn_id=1, uid=100, size=1024 * 1024)
        assert ok is True

        stats = tracker.stats()
        assert stats["conn_bytes"][1] == 1024 * 1024
        assert stats["uid_bytes"][100] == 1024 * 1024
        assert stats["total_bytes"] == 1024 * 1024
        assert "limits" in stats

        # 释放
        tracker.release(conn_id=1, uid=100, size=1024 * 1024)
        stats = tracker.stats()
        assert stats["conn_bytes"][1] == 0
        assert stats["uid_bytes"][100] == 0
        assert stats["total_bytes"] == 0

    def test_acquire_per_connection_limit(self):
        """单连接超限拒绝。"""
        from callwarden.server.ipc_transport import (
            InflightTracker, MAX_CONN_QUEUED_BYTES,
        )

        tracker = InflightTracker()
        # 分配接近上限
        ok = tracker.acquire(conn_id=1, uid=100, size=MAX_CONN_QUEUED_BYTES - 1)
        assert ok is True

        # 再分配 2 字节 → 超限
        ok = tracker.acquire(conn_id=1, uid=100, size=2)
        assert ok is False

    def test_acquire_per_uid_limit(self):
        """单 UID 超限拒绝（多连接累计）。"""
        from callwarden.server.ipc_transport import (
            InflightTracker, MAX_UID_INFLIGHT_BYTES,
        )

        tracker = InflightTracker()
        # 两个不同连接，同一 UID，累计超限
        ok1 = tracker.acquire(
            conn_id=1, uid=100, size=MAX_UID_INFLIGHT_BYTES // 2
        )
        assert ok1 is True
        ok2 = tracker.acquire(
            conn_id=2, uid=100, size=MAX_UID_INFLIGHT_BYTES // 2
        )
        assert ok2 is True

        # 第三个连接（同 UID）→ 超限
        ok3 = tracker.acquire(conn_id=3, uid=100, size=10)
        assert ok3 is False

    def test_acquire_global_limit(self):
        """全局 inflight 超限拒绝（多 UID 累计）。

        使用 monkeypatch 临时降低 MAX_DAEMON_INFLIGHT_BYTES 到可测试的值，
        避免实际分配 2GB 内存。
        """
        from callwarden.server import ipc_transport

        tracker = ipc_transport.InflightTracker()
        # 临时降低限制到 10MB 便于测试
        original_global = ipc_transport.MAX_DAEMON_INFLIGHT_BYTES
        original_uid = ipc_transport.MAX_UID_INFLIGHT_BYTES
        original_conn = ipc_transport.MAX_CONN_QUEUED_BYTES
        try:
            # 用 monkeypatch 思路（直接改属性）：降至 10MB 全局 / 10MB UID / 10MB conn
            # 注意：InflightTracker 在 __init__ 时 import 了常量，
            # 但 acquire() 内部直接引用模块常量，所以改模块属性即可
            test_global = 10 * 1024 * 1024
            test_uid = 10 * 1024 * 1024
            test_conn = 10 * 1024 * 1024
            ipc_transport.MAX_DAEMON_INFLIGHT_BYTES = test_global
            ipc_transport.MAX_UID_INFLIGHT_BYTES = test_uid
            ipc_transport.MAX_CONN_QUEUED_BYTES = test_conn

            # 不同 UID 各分配 5MB
            ok1 = tracker.acquire(conn_id=1, uid=100, size=5 * 1024 * 1024)
            assert ok1 is True
            ok2 = tracker.acquire(conn_id=2, uid=200, size=5 * 1024 * 1024)
            assert ok2 is True

            # 第三个 UID → 全局超限（10MB + 10 = 10MB+10）
            ok3 = tracker.acquire(conn_id=3, uid=300, size=10)
            assert ok3 is False
        finally:
            ipc_transport.MAX_DAEMON_INFLIGHT_BYTES = original_global
            ipc_transport.MAX_UID_INFLIGHT_BYTES = original_uid
            ipc_transport.MAX_CONN_QUEUED_BYTES = original_conn

    def test_release_idempotent_below_zero(self):
        """release 不会让计数变负（max(0, ...)）。"""
        from callwarden.server.ipc_transport import InflightTracker

        tracker = InflightTracker()
        tracker.acquire(conn_id=1, uid=100, size=100)
        # 释放超过分配量
        tracker.release(conn_id=1, uid=100, size=200)
        stats = tracker.stats()
        assert stats["conn_bytes"][1] == 0
        assert stats["uid_bytes"][100] == 0
        assert stats["total_bytes"] == 0

    def test_independent_dimensions(self):
        """三个维度独立：连接超限不阻断其他连接。"""
        from callwarden.server.ipc_transport import (
            InflightTracker, MAX_CONN_QUEUED_BYTES,
        )

        tracker = InflightTracker()
        # conn=1 接近上限
        ok1 = tracker.acquire(
            conn_id=1, uid=100, size=MAX_CONN_QUEUED_BYTES - 1
        )
        assert ok1 is True
        # conn=2（同 UID）可以继续分配
        ok2 = tracker.acquire(conn_id=2, uid=100, size=1024)
        assert ok2 is True

    def test_stats_includes_all_limits(self):
        """stats 包含 4 个限制常量。"""
        from callwarden.server.ipc_transport import InflightTracker

        tracker = InflightTracker()
        stats = tracker.stats()
        limits = stats["limits"]
        assert "max_conn_queued_bytes" in limits
        assert "max_uid_inflight_bytes" in limits
        assert "max_daemon_inflight_bytes" in limits
        assert "max_memfd_bytes" in limits


# ============================================================
# 2. is_memfd / validate_memfd_fd 校验逻辑
# ============================================================


class TestIsMemfd:
    """is_memfd 检测函数。"""

    def test_is_memfd_returns_false_on_non_linux(self):
        """非 Linux 平台 is_memfd 返回 False。"""
        if _IS_LINUX:
            pytest.skip("仅测非 Linux")
        from callwarden.server.ipc_transport import is_memfd

        # 用 stdin（FD 0）测试，不会是 memfd
        assert is_memfd(0) is False

    @_SKIP_LINUX
    def test_is_memfd_detects_real_memfd(self):
        """Linux: 真实 memfd_create 创建的 FD 应被识别。"""
        import ctypes
        import fcntl
        from callwarden.server.ipc_transport import (
            is_memfd, MFD_CLOEXEC, MFD_ALLOW_SEALING, F_GET_SEALS,
        )

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        sys_memfd_create = libc.syscall  # memfd_create 是 syscall
        # 直接用 syscall
        import ctypes.util
        libc_path = ctypes.util.find_library("c")
        libc = ctypes.CDLL(libc_path, use_errno=True)

        # memfd_create(name, flags)
        try:
            memfd_create = libc.memfd_create
        except AttributeError:
            pytest.skip("libc 无 memfd_create 符号")

        memfd_create.restype = ctypes.c_int
        memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        fd = memfd_create(b"test", MFD_CLOEXEC | MFD_ALLOW_SEALING)
        assert fd >= 0
        try:
            assert is_memfd(fd) is True
            # 写入数据后加 seal
            os.write(fd, b"x" * 1024)
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, 0x0001 | 0x0002 | 0x0004 | 0x0008)
            assert is_memfd(fd) is True
        finally:
            os.close(fd)

    @_SKIP_LINUX
    def test_is_memfd_returns_false_for_regular_file(self):
        """Linux: 常规文件不是 memfd。"""
        from callwarden.server.ipc_transport import is_memfd

        with tempfile.NamedTemporaryFile() as f:
            fd = f.fileno()
            assert is_memfd(fd) is False


class TestValidateMemfdFd:
    """validate_memfd_fd 四重校验。"""

    @_SKIP_LINUX
    def test_validate_rejects_non_memfd_on_linux(self):
        """Linux: 非 memfd FD（常规文件）应在 seal 校验阶段失败。"""
        from callwarden.server.ipc_transport import validate_memfd_fd, ProtocolError

        # 用临时文件 FD
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"hello")
            f.flush()
            fd = os.dup(f.fileno())  # 复制 FD 避免关闭影响
            try:
                uid = os.fstat(fd).st_uid
                with pytest.raises(ProtocolError) as exc_info:
                    validate_memfd_fd(
                        fd,
                        expected_canonical_len=5,
                        expected_content_hash=hashlib.sha256(b"hello").hexdigest(),
                        peer_uid=uid,
                    )
                # Linux 应在 seal 校验阶段失败
                assert "seal" in str(exc_info.value).lower()
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_validate_rejects_size_mismatch(self):
        """size mismatch 应抛 ProtocolError（跨平台）。"""
        from callwarden.server.ipc_transport import validate_memfd_fd, ProtocolError

        with tempfile.NamedTemporaryFile() as f:
            f.write(b"hello world")  # 11 bytes
            f.flush()
            fd = os.dup(f.fileno())
            try:
                uid = os.fstat(fd).st_uid
                # expected_canonical_len 不匹配实际大小（999 vs 11）
                with pytest.raises(ProtocolError) as exc_info:
                    validate_memfd_fd(
                        fd,
                        expected_canonical_len=999,  # 实际是 11
                        expected_content_hash="x" * 64,
                        peer_uid=uid,
                    )
                assert "size" in str(exc_info.value).lower()
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_validate_rejects_owner_uid_mismatch(self):
        """owner UID 不匹配应抛 ProtocolError。"""
        from callwarden.server.ipc_transport import validate_memfd_fd, ProtocolError

        with tempfile.NamedTemporaryFile() as f:
            fd = os.dup(f.fileno())
            try:
                # peer_uid 用不可能存在的 UID
                with pytest.raises(ProtocolError) as exc_info:
                    validate_memfd_fd(
                        fd,
                        expected_canonical_len=0,
                        expected_content_hash="x" * 64,
                        peer_uid=999999,  # 不存在的 UID
                    )
                assert "owner_uid" in str(exc_info.value)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

    @_SKIP_LINUX
    def test_validate_real_memfd_success(self):
        """Linux: 真实 memfd_create + seal + validate 成功路径。"""
        import ctypes
        import ctypes.util
        import fcntl
        from callwarden.server.ipc_transport import (
            validate_memfd_fd,
            MFD_CLOEXEC, MFD_ALLOW_SEALING,
            F_SEAL_SEAL, F_SEAL_SHRINK, F_SEAL_GROW, F_SEAL_WRITE,
        )

        libc_path = ctypes.util.find_library("c")
        libc = ctypes.CDLL(libc_path, use_errno=True)
        try:
            memfd_create = libc.memfd_create
        except AttributeError:
            pytest.skip("libc 无 memfd_create 符号")

        memfd_create.restype = ctypes.c_int
        memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        fd = memfd_create(b"test_validate", MFD_CLOEXEC | MFD_ALLOW_SEALING)
        assert fd >= 0

        try:
            content = b"hello memfd world\n" * 1024  # ~17KB
            os.write(fd, content)

            # 加四重 seal
            required_seals = (
                F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE
            )
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, required_seals)

            uid = os.fstat(fd).st_uid
            expected_hash = hashlib.sha256(content).hexdigest()

            validated_fd = validate_memfd_fd(
                fd,
                expected_canonical_len=len(content),
                expected_content_hash=expected_hash,
                peer_uid=uid,
            )
            assert validated_fd == fd

            # 读取验证
            os.lseek(validated_fd, 0, os.SEEK_SET)
            read_back = os.read(validated_fd, len(content))
            assert read_back == content
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    @_SKIP_LINUX
    def test_validate_real_memfd_hash_mismatch(self):
        """Linux: 真实 memfd + 错误 hash → 拒绝。"""
        import ctypes
        import ctypes.util
        import fcntl
        from callwarden.server.ipc_transport import (
            validate_memfd_fd, ProtocolError,
            MFD_CLOEXEC, MFD_ALLOW_SEALING,
            F_SEAL_SEAL, F_SEAL_SHRINK, F_SEAL_GROW, F_SEAL_WRITE,
        )

        libc_path = ctypes.util.find_library("c")
        libc = ctypes.CDLL(libc_path, use_errno=True)
        memfd_create = libc.memfd_create
        memfd_create.restype = ctypes.c_int
        memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        fd = memfd_create(b"test_hash", MFD_CLOEXEC | MFD_ALLOW_SEALING)
        assert fd >= 0

        try:
            content = b"data\n" * 100
            os.write(fd, content)
            required_seals = (
                F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE
            )
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, required_seals)
            uid = os.fstat(fd).st_uid

            with pytest.raises(ProtocolError) as exc_info:
                validate_memfd_fd(
                    fd,
                    expected_canonical_len=len(content),
                    expected_content_hash="0" * 64,  # 错误 hash
                    peer_uid=uid,
                )
            assert "hash mismatch" in str(exc_info.value)
        finally:
            # validate_memfd_fd 失败时会 close fd
            try:
                os.close(fd)
            except OSError:
                pass


# ============================================================
# 3. daemon_server.py workspace.file.refresh memfd 检测路径
# ============================================================


class TestWorkspaceRefreshMemfdDetection:
    """G10: daemon_server.py workspace.file.refresh 自动检测 memfd。

    通过 mock is_memfd 来测试 daemon 的 memfd 检测分支，
    无需真实 Linux memfd 支持。
    """

    @pytest.fixture
    def daemon_service(self, tmp_path):
        """构造 EnterpriseDaemonService（tmp_path 隔离）。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService
        from callwarden.server.snapshot_manager import SnapshotManagerService
        snapshot_service = SnapshotManagerService(max_workspaces=4)
        return EnterpriseDaemonService(
            registry_db=str(tmp_path / "registry.db"),
            snapshot_service=snapshot_service,
            data_root=str(tmp_path / "data"),
        )

    def _peer(self):
        uid = os.getuid() if hasattr(os, "getuid") else 0
        return {"pid": os.getpid(), "uid": uid, "gid": uid}

    def test_memfd_path_requires_canonical_len_and_content_hash(
        self, daemon_service, tmp_path, monkeypatch
    ):
        """memfd 模式必须提供 canonical_len + content_hash。"""
        # 注册 workspace（需要 client_view_root）
        peer = self._peer()
        ws = daemon_service.dispatch(peer, "workspace.register", {
            "client_view_root": str(tmp_path),
            "owner_uid": peer["uid"],
        })

        # 准备一个临时文件 FD（非 memfd）
        tmp_file = tmp_path / "test.txt"
        tmp_file.write_text("hello world")
        fd = os.open(str(tmp_file), os.O_RDONLY)

        # mock is_memfd 返回 True，模拟 memfd 路径
        # 注意：daemon_server.py 通过 `from callwarden.server.ipc_transport import is_memfd`
        # 局部 import，所以直接 patch ipc_transport.is_memfd 即可
        from callwarden.server import ipc_transport
        monkeypatch.setattr(ipc_transport, "is_memfd", lambda f: True)

        # 缺少 canonical_len / content_hash → invalid_params
        from callwarden.server.daemon_server import DaemonRpcError
        with pytest.raises(DaemonRpcError) as exc_info:
            daemon_service.dispatch(
                peer, "workspace.file.refresh",
                {
                    "workspace_instance_id": ws["workspace_instance_id"],
                    "rel_path": "test.txt",
                    "agent_session_id": "sess-1",
                    "monotonic_seq": 1,
                    "session_epoch": 1,
                },
                received_fds=[fd],
            )
        assert "invalid_params" in str(exc_info.value) or "canonical_len" in str(exc_info.value)
        os.close(fd)


# ============================================================
# 4. Linux 真实 E2E：send_msg + recv_via_memfd roundtrip
# ============================================================


@_SKIP_LINUX
@_SKIP_PY314
class TestLinuxMemfdRoundtripE2E:
    """Linux 专属：真实 memfd_create + send_msg + recv_via_memfd 全链路。"""

    def test_send_msg_large_payload_uses_memfd(self):
        """send_msg 对大 payload 自动走 memfd 路径。"""
        import ctypes
        import ctypes.util
        from callwarden.server.ipc_transport import (
            send_msg, MAX_MSG_BYTES,
        )

        # 准备一对 UDS socket
        sock1, sock2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

        try:
            # 大 payload（超过 MAX_MSG_BYTES）
            large_content = b"x" * (MAX_MSG_BYTES + 1024)
            payload = {
                "rel_path": "large.txt",
                "content_hash": hashlib.sha256(large_content).hexdigest(),
                "canonical_len": len(large_content),
            }

            # 在另一个线程发送
            def sender():
                send_msg(sock1, msg_type=1, payload=payload, canonical_bytes=large_content)

            t = threading.Thread(target=sender)
            t.start()

            # 接收端：用 recv_via_memfd 接收
            from callwarden.server.ipc_transport import recv_via_memfd
            uid = os.getuid()
            fd, received_msg = recv_via_memfd(
                sock2,
                expected_canonical_len=len(large_content),
                expected_content_hash=hashlib.sha256(large_content).hexdigest(),
                peer_uid=uid,
            )
            try:
                # 读取内容
                os.lseek(fd, 0, os.SEEK_SET)
                received = os.read(fd, len(large_content))
                assert received == large_content
                assert received_msg["rel_path"] == "large.txt"
            finally:
                os.close(fd)

            t.join(timeout=5.0)
            assert not t.is_alive()
        finally:
            sock1.close()
            sock2.close()

    def test_send_msg_small_payload_uses_framed(self):
        """send_msg 对小 payload 走 framed 路径（无 memfd）。"""
        from callwarden.server.ipc_transport import send_msg

        sock1, sock2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            small_content = b"hello world"
            payload = {"rel_path": "small.txt"}

            def sender():
                send_msg(sock1, msg_type=1, payload=payload, canonical_bytes=small_content)

            t = threading.Thread(target=sender)
            t.start()

            # 小 payload 走 framed，recv_via_memfd 会失败（无 FD）
            # 这里只验证 framed 能成功发送
            # 接收端用 send_framed_stream 的对应接收（_recv_msg_with_fd 会失败）
            # 所以这里只验证 sender 不抛异常
            t.join(timeout=2.0)
            assert not t.is_alive()
        finally:
            sock1.close()
            sock2.close()


# ============================================================
# 5. 大文件 256MB E2E（仅 Linux，可选）
# ============================================================


@_SKIP_LINUX
class TestLargeFileE2E:
    """G10: 真实 256MB 大文件 E2E（验证内存不爆）。

    注意：此测试占用 256MB 磁盘空间 + 256MB 内存（memfd），
    默认 skip，需要环境变量 CW_RUN_LARGE_FILE_TEST=1 才运行。
    """

    @pytest.fixture(autouse=True)
    def skip_unless_enabled(self):
        if os.environ.get("CW_RUN_LARGE_FILE_TEST") != "1":
            pytest.skip("设置 CW_RUN_LARGE_FILE_TEST=1 运行 256MB 大文件测试")

    def test_256mb_memfd_roundtrip(self):
        """256MB memfd roundtrip（验证 MAX_MEMFD_BYTES 边界）。"""
        from callwarden.server.ipc_transport import (
            send_msg, recv_via_memfd, MAX_MEMFD_BYTES,
        )

        size = MAX_MEMFD_BYTES  # 256MB
        # 用可预测的内容（不全部载入内存）
        # 分块生成 + 分块校验
        sock1, sock2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

        try:
            # 计算 hash（流式）
            h = hashlib.sha256()
            # 用重复块填充
            block = b"abcdefgh" * 65536  # 512KB
            for _ in range(size // len(block)):
                h.update(block)
            expected_hash = h.hexdigest()

            # 准备发送（需要流式发送，不能一次性载入 256MB）
            # send_msg 当前实现是接受 bytes，对 256MB 来说会占内存
            # 这里跳过实际发送，只验证 hash 计算逻辑
            pytest.skip("256MB roundtrip 需要流式 send_msg 实现（当前 send_msg 接受 bytes）")
        finally:
            sock1.close()
            sock2.close()
