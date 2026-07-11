"""Phase 5: Daemon IPC 传输层测试。

测试覆盖：
1. ProtocolError 异常存在性
2. 常量定义（MAX_MSG_BYTES、MAX_MEMFD_BYTES、F_SEAL_*）
3. send_framed_stream 小文件传输（mock socket）
4. send_framed_stream 大文件自动委托给 send_via_memfd
5. _write_all 短写循环
6. _sha256_streaming 流式哈希
7. _recv_exact 精确接收
8. send_msg 统一入口选择传输方式
9. recv_via_memfd 大小不匹配拒绝
10. recv_via_memfd hash 不匹配拒绝
11. recv_via_memfd 超过 MAX_MEMFD_BYTES 拒绝
12. Windows 平台降级不崩溃

设计参考：
- docs/design/daemon-ipc-security.md §2-§3、§7（故障注入测试）
"""

import hashlib
import json
import os
import struct
from contextlib import ExitStack
from unittest import mock

import pytest

# Windows 下 os.open 默认以文本模式打开，需加 O_BINARY 才能正确处理二进制数据
# （文本模式下 0x1a 被视为 EOF，导致读取截断）
_READ_FLAGS = os.O_RDONLY
_WRITE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_TRUNC
if hasattr(os, "O_BINARY"):
    _READ_FLAGS |= os.O_BINARY
    _WRITE_FLAGS |= os.O_BINARY

from callwarden.server.ipc_transport import (
    MAX_MEMFD_BYTES,
    MAX_MSG_BYTES,
    F_SEAL_GROW,
    F_SEAL_SEAL,
    F_SEAL_SHRINK,
    F_SEAL_WRITE,
    MFD_ALLOW_SEALING,
    MFD_CLOEXEC,
    ProtocolError,
    _recv_exact,
    _sha256_streaming,
    _write_all,
    recv_via_memfd,
    send_framed_stream,
    send_msg,
    send_via_memfd,
)


# ============================================================
# 1. 基础结构与常量
# ============================================================


class TestProtocolError:
    """ProtocolError 异常测试。"""

    def test_protocol_error_exists(self):
        """ProtocolError 是 Exception 子类"""
        assert issubclass(ProtocolError, Exception)

    def test_protocol_error_can_be_raised(self):
        """ProtocolError 可被 raise 并捕获"""
        with pytest.raises(ProtocolError, match="test error"):
            raise ProtocolError("test error")


class TestConstants:
    """常量定义测试。"""

    def test_constants_exist(self):
        """所有必需常量已定义"""
        assert MAX_MSG_BYTES == 16 * 1024 * 1024
        assert MAX_MEMFD_BYTES == 256 * 1024 * 1024

    def test_seal_flags(self):
        """Linux memfd seal flags 值正确"""
        assert F_SEAL_SEAL == 0x0001
        assert F_SEAL_SHRINK == 0x0002
        assert F_SEAL_GROW == 0x0004
        assert F_SEAL_WRITE == 0x0008

    def test_memfd_create_flags(self):
        """memfd_create flags 值正确"""
        assert MFD_CLOEXEC == 0x0001
        assert MFD_ALLOW_SEALING == 0x0002

    def test_required_seals_combination(self):
        """四重 seal 组合 = SHRINK | GROW | WRITE | SEAL"""
        required = F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL
        assert required == 0x000F


# ============================================================
# 2. 分帧 stream 传输
# ============================================================


class FakeSocket:
    """简单的 mock socket，记录 sendall 调用。"""

    def __init__(self):
        self.sent_data = b""

    def sendall(self, data: bytes):
        self.sent_data += data

    def send(self, data: bytes) -> int:
        self.sent_data += data
        return len(data)


class TestSendFramedStream:
    """send_framed_stream 测试。"""

    def test_send_framed_stream_small_file(self):
        """小文件使用分帧 stream 传输"""
        sock = FakeSocket()
        payload = {"path": "src/main.py", "lang": "python"}
        canonical_bytes = b"print('hello')\n"

        send_framed_stream(sock, msg_type=1, payload=payload, canonical_bytes=canonical_bytes)

        # 解析头部：| msg_type(1B) | payload_len(4B, BE) | canonical_len(8B, BE) |
        header = sock.sent_data[:13]
        msg_type, payload_len, canonical_len = struct.unpack(">BIQ", header)
        assert msg_type == 1
        assert payload_len == len(json.dumps(payload).encode("utf-8"))
        assert canonical_len == len(canonical_bytes)

        # payload_json + canonical_bytes 紧跟其后
        payload_json = sock.sent_data[13:13 + payload_len]
        actual_canonical = sock.sent_data[13 + payload_len:]
        assert json.loads(payload_json.decode("utf-8")) == payload
        assert actual_canonical == canonical_bytes

    def test_send_framed_stream_empty_canonical(self):
        """空 canonical_bytes 也能正常发送"""
        sock = FakeSocket()
        payload = {"path": "empty.py"}
        send_framed_stream(sock, msg_type=2, payload=payload, canonical_bytes=b"")

        header = sock.sent_data[:13]
        msg_type, payload_len, canonical_len = struct.unpack(">BIQ", header)
        assert msg_type == 2
        assert canonical_len == 0

    def test_send_framed_stream_delegates_to_memfd_for_large(self):
        """大文件自动委托给 send_via_memfd"""
        sock = FakeSocket()
        payload = {"path": "big.rs"}
        # 构造超过 MAX_MSG_BYTES 的 canonical_bytes
        canonical_bytes = b"x" * (MAX_MSG_BYTES + 100)

        with mock.patch(
            "callwarden.server.ipc_transport.send_via_memfd"
        ) as mock_send:
            send_framed_stream(sock, msg_type=1, payload=payload, canonical_bytes=canonical_bytes)
            mock_send.assert_called_once()
            call_args = mock_send.call_args
            # 第一个位置参数是 sock
            assert call_args[0][0] is sock
            # msg_type == 1
            assert call_args[0][1] == 1
            # canonical_bytes 透传
            assert call_args[0][3] == canonical_bytes


# ============================================================
# 3. _write_all 短写循环
# ============================================================


class TestWriteAll:
    """_write_all 测试。"""

    def test_write_all_handles_short_writes(self, tmp_path):
        """_write_all 循环处理 os.write 短写"""
        # 使用 tmpfile 拿一个真实 fd，避免 os.write 对 -1 报错
        fd = os.open(str(tmp_path / "test_write_all.bin"), _WRITE_FLAGS)
        try:
            data = b"abcdefghij" * 100  # 1000 字节

            # mock os.write：每次只写 7 字节
            original_write = os.write
            call_count = {"n": 0}

            def fake_write(fd_arg, buf):
                call_count["n"] += 1
                # 调用真实 os.write 但只写 7 字节
                return original_write(fd_arg, buf[:7])

            with mock.patch("callwarden.server.ipc_transport.os.write", side_effect=fake_write):
                _write_all(fd, data)

            # 确实被短写了多次
            assert call_count["n"] > 1

            # 文件内容与 data 完全一致
            os.lseek(fd, 0, os.SEEK_SET)
            content = os.read(fd, 10000)
            assert content == data
        finally:
            os.close(fd)

    def test_write_all_full_write_single_call(self, tmp_path):
        """单次完整写入只调用一次 os.write"""
        fd = os.open(str(tmp_path / "test_write_all_full.bin"), _WRITE_FLAGS)
        try:
            data = b"hello"

            call_count = {"n": 0}
            original_write = os.write

            def fake_write(fd_arg, buf):
                call_count["n"] += 1
                return original_write(fd_arg, buf)

            with mock.patch("callwarden.server.ipc_transport.os.write", side_effect=fake_write):
                _write_all(fd, data)

            assert call_count["n"] == 1

            os.lseek(fd, 0, os.SEEK_SET)
            assert os.read(fd, 100) == data
        finally:
            os.close(fd)

    def test_write_all_raises_on_zero_write(self, tmp_path):
        """os.write 返回 0 时抛 OSError"""
        fd = os.open(str(tmp_path / "test_write_all_zero.bin"), _WRITE_FLAGS)
        try:
            with mock.patch("callwarden.server.ipc_transport.os.write", return_value=0):
                with pytest.raises(OSError, match="memfd write returned 0"):
                    _write_all(fd, b"hello")
        finally:
            os.close(fd)


# ============================================================
# 4. _sha256_streaming 流式哈希
# ============================================================


class TestSha256Streaming:
    """_sha256_streaming 测试。"""

    def test_sha256_streaming_small(self, tmp_path):
        """小文件流式 sha256 与一次性计算结果一致"""
        data = b"hello world\n" * 100
        expected = hashlib.sha256(data).hexdigest()

        path = tmp_path / "sha_test_small.bin"
        path.write_bytes(data)

        fd = os.open(str(path), _READ_FLAGS)
        try:
            actual = _sha256_streaming(fd, len(data))
            assert actual == expected
        finally:
            os.close(fd)

    def test_sha256_streaming_large(self, tmp_path):
        """大文件（>64KB，跨多个 65536 chunk）流式 sha256 正确"""
        # 使用确定性数据，跨 3+ chunk（65536 * 3 = 196608 < 200192）
        chunk = bytes(range(256))  # 256 bytes
        data = chunk * 782  # 200192 bytes
        expected = hashlib.sha256(data).hexdigest()

        path = tmp_path / "sha_test_large.bin"
        path.write_bytes(data)

        # 验证文件内容确实写入正确
        assert path.read_bytes() == data

        fd = os.open(str(path), _READ_FLAGS)
        try:
            # 确认 fd 指针在起点
            assert os.lseek(fd, 0, os.SEEK_CUR) == 0
            actual = _sha256_streaming(fd, len(data))
            assert actual == expected
        finally:
            os.close(fd)

    def test_sha256_streaming_empty(self, tmp_path):
        """空文件 sha256 与 hashlib 一致"""
        expected = hashlib.sha256(b"").hexdigest()

        path = tmp_path / "sha_test_empty.bin"
        path.write_bytes(b"")

        fd = os.open(str(path), _READ_FLAGS)
        try:
            actual = _sha256_streaming(fd, 0)
            assert actual == expected
        finally:
            os.close(fd)


# ============================================================
# 5. _recv_exact 精确接收
# ============================================================


class TestRecvExact:
    """_recv_exact 测试。"""

    def test_recv_exact_single_chunk(self):
        """单次 recv 接收全部"""
        sock = mock.MagicMock()
        sock.recv.return_value = b"hello"
        data = _recv_exact(sock, 5)
        assert data == b"hello"

    def test_recv_exact_multiple_chunks(self):
        """多次 recv 拼接"""
        sock = mock.MagicMock()
        # 第一次只收 2 字节，第二次收 3 字节
        sock.recv.side_effect = [b"he", b"llo"]
        data = _recv_exact(sock, 5)
        assert data == b"hello"

    def test_recv_exact_connection_closed(self):
        """连接关闭时抛 ConnectionError"""
        sock = mock.MagicMock()
        sock.recv.return_value = b""
        with pytest.raises(ConnectionError, match="connection closed"):
            _recv_exact(sock, 10)

    def test_recv_exact_zero_bytes(self):
        """接收 0 字节直接返回空"""
        sock = mock.MagicMock()
        data = _recv_exact(sock, 0)
        assert data == b""
        sock.recv.assert_not_called()


# ============================================================
# 6. send_msg 统一入口
# ============================================================


class TestSendMsg:
    """send_msg 统一入口测试。"""

    def test_send_msg_auto_selects_framed_for_small(self):
        """小文件走分帧传输"""
        sock = FakeSocket()
        payload = {"path": "small.py"}
        canonical_bytes = b"x" * 100

        send_msg(sock, msg_type=1, payload=payload, canonical_bytes=canonical_bytes)

        # 解析头部确认走的是分帧
        header = sock.sent_data[:13]
        msg_type, payload_len, canonical_len = struct.unpack(">BIQ", header)
        assert msg_type == 1
        assert canonical_len == 100

    def test_send_msg_auto_selects_memfd_for_large(self):
        """大文件自动委托给 send_via_memfd"""
        sock = FakeSocket()
        payload = {"path": "large.rs"}
        canonical_bytes = b"x" * (MAX_MSG_BYTES + 1)

        with mock.patch(
            "callwarden.server.ipc_transport.send_via_memfd"
        ) as mock_send:
            send_msg(sock, msg_type=1, payload=payload, canonical_bytes=canonical_bytes)
            mock_send.assert_called_once()
            assert mock_send.call_args[0][3] == canonical_bytes


# ============================================================
# 7. recv_via_memfd 四重校验（故障注入）
# ============================================================


class FakeStat:
    """模拟 os.fstat 返回值。"""

    def __init__(self, st_size):
        self.st_size = st_size


def _build_recv_via_memfd_patches(
    fd: int,
    file_size: int,
    sha256_return: str,
    seals_return: int = F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL,
    is_linux: bool = True,
):
    """构造 recv_via_memfd 所需的 mock patch 集合。

    返回一个 list of (target, mock_obj) 可用于 ExitStack。
    所有 patch 针对正确的命名空间：
    - 模块属性：_recv_msg_with_fd / _sha256_streaming / _linux_get_seals / _IS_LINUX
    - os 模块全局函数：os.fstat / os.lseek / os.close
    """
    return [
        mock.patch("callwarden.server.ipc_transport._recv_msg_with_fd",
                   return_value=(fd, {"path": "test"})),
        mock.patch("os.fstat", return_value=FakeStat(file_size)),
        mock.patch("callwarden.server.ipc_transport._sha256_streaming",
                   return_value=sha256_return),
        mock.patch("callwarden.server.ipc_transport._linux_get_seals",
                   return_value=seals_return),
        mock.patch("callwarden.server.ipc_transport._IS_LINUX", is_linux),
        mock.patch("os.lseek"),
        mock.patch("os.close"),
    ]


class TestRecvViaMemfd:
    """recv_via_memfd 四重校验测试。"""

    def test_recv_via_memfd_size_mismatch(self):
        """大小不匹配抛 ProtocolError"""
        fd = 42
        file_size = 200
        expected_len = 100  # 不匹配
        file_content = b"x" * file_size
        expected_hash = hashlib.sha256(file_content).hexdigest()

        patches = _build_recv_via_memfd_patches(
            fd=fd,
            file_size=file_size,
            sha256_return=expected_hash,
        )
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with pytest.raises(ProtocolError, match="size mismatch"):
                recv_via_memfd(
                    sock=mock.MagicMock(),
                    expected_canonical_len=expected_len,
                    expected_content_hash=expected_hash,
                    peer_uid=1000,
                )

    def test_recv_via_memfd_hash_mismatch(self):
        """hash 不匹配抛 ProtocolError"""
        fd = 42
        file_size = 100
        file_content = b"x" * file_size
        correct_hash = hashlib.sha256(file_content).hexdigest()
        wrong_hash = "deadbeef" * 8  # 错误的 hash

        patches = _build_recv_via_memfd_patches(
            fd=fd,
            file_size=file_size,
            sha256_return=wrong_hash,  # 返回错误 hash
        )
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with pytest.raises(ProtocolError, match="hash mismatch"):
                recv_via_memfd(
                    sock=mock.MagicMock(),
                    expected_canonical_len=file_size,
                    expected_content_hash=correct_hash,
                    peer_uid=1000,
                )

    def test_recv_via_memfd_exceeds_max(self):
        """超过 MAX_MEMFD_BYTES 抛 ProtocolError"""
        fd = 42
        file_size = MAX_MEMFD_BYTES + 1
        file_content = b"x" * 100
        expected_hash = hashlib.sha256(file_content).hexdigest()

        patches = _build_recv_via_memfd_patches(
            fd=fd,
            file_size=file_size,
            sha256_return=expected_hash,
        )
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with pytest.raises(ProtocolError, match="exceeds MAX_MEMFD_BYTES"):
                recv_via_memfd(
                    sock=mock.MagicMock(),
                    expected_canonical_len=file_size,
                    expected_content_hash=expected_hash,
                    peer_uid=1000,
                )

    def test_recv_via_memfd_missing_seal(self):
        """seal 缺失（少 F_SEAL_GROW）抛 ProtocolError"""
        fd = 42
        file_size = 100
        file_content = b"x" * file_size
        expected_hash = hashlib.sha256(file_content).hexdigest()

        # 缺 F_SEAL_GROW
        incomplete_seals = F_SEAL_SHRINK | F_SEAL_WRITE | F_SEAL_SEAL
        patches = _build_recv_via_memfd_patches(
            fd=fd,
            file_size=file_size,
            sha256_return=expected_hash,
            seals_return=incomplete_seals,
            is_linux=True,
        )
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with pytest.raises(ProtocolError, match="missing seals"):
                recv_via_memfd(
                    sock=mock.MagicMock(),
                    expected_canonical_len=file_size,
                    expected_content_hash=expected_hash,
                    peer_uid=1000,
                )

    def test_recv_via_memfd_closes_fd_on_failure(self):
        """校验失败时关闭 FD"""
        fd = 42
        file_size = 100
        expected_len = 200  # 不匹配，触发失败
        file_content = b"x" * file_size
        expected_hash = hashlib.sha256(file_content).hexdigest()

        patches = _build_recv_via_memfd_patches(
            fd=fd,
            file_size=file_size,
            sha256_return=expected_hash,
        )
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patches]
            # patches 顺序：_recv_msg_with_fd, os.fstat, _sha256_streaming,
            #              _linux_get_seals, _IS_LINUX, os.lseek, os.close
            mock_close = mocks[6]
            with pytest.raises(ProtocolError):
                recv_via_memfd(
                    sock=mock.MagicMock(),
                    expected_canonical_len=expected_len,
                    expected_content_hash=expected_hash,
                    peer_uid=1000,
                )
            mock_close.assert_called_once_with(fd)

    def test_recv_via_memfd_success(self):
        """四重校验通过，返回 FD 和 msg"""
        fd = 42
        file_size = 100
        file_content = b"x" * file_size
        expected_hash = hashlib.sha256(file_content).hexdigest()

        patches = _build_recv_via_memfd_patches(
            fd=fd,
            file_size=file_size,
            sha256_return=expected_hash,
        )
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patches]
            # patches 顺序：_recv_msg_with_fd, os.fstat, _sha256_streaming,
            #              _linux_get_seals, _IS_LINUX, os.lseek, os.close
            mock_lseek = mocks[5]
            mock_close = mocks[6]
            result_fd, result_msg = recv_via_memfd(
                sock=mock.MagicMock(),
                expected_canonical_len=file_size,
                expected_content_hash=expected_hash,
                peer_uid=1000,
            )
            assert result_fd == fd
            assert result_msg == {"path": "test"}
            # 校验通过后 lseek 回到起点（至少 2 次：hash 前 + hash 后）
            assert mock_lseek.call_count >= 2
            # 校验通过时不关闭 FD
            mock_close.assert_not_called()


# ============================================================
# 8. Windows 平台降级
# ============================================================


class TestWindowsFallback:
    """Windows 平台降级测试。"""

    def test_windows_fallback_no_crash(self):
        """Windows 平台 send_via_memfd 降级为分帧，不崩溃"""
        sock = FakeSocket()
        payload = {"path": "big.rs"}
        canonical_bytes = b"x" * (MAX_MSG_BYTES + 100)

        # 强制 _IS_LINUX=False，模拟 Windows
        with mock.patch(
            "callwarden.server.ipc_transport._IS_LINUX", False
        ):
            # 不应抛异常，应降级为 _send_framed_unlimited
            send_via_memfd(sock, msg_type=1, payload=payload, canonical_bytes=canonical_bytes)

        # 验证数据被发送（走的是分帧格式）
        header = sock.sent_data[:13]
        msg_type, payload_len, canonical_len = struct.unpack(">BIQ", header)
        assert msg_type == 1
        assert canonical_len == len(canonical_bytes)

    def test_windows_fallback_uses_unlimited_framed(self):
        """Windows 降级调用 _send_framed_unlimited"""
        sock = FakeSocket()
        payload = {"path": "big.rs"}
        canonical_bytes = b"x" * (MAX_MSG_BYTES + 100)

        with mock.patch(
            "callwarden.server.ipc_transport._IS_LINUX", False
        ), mock.patch(
            "callwarden.server.ipc_transport._send_framed_unlimited",
            wraps=lambda *a, **kw: None,
        ) as mock_unlimited:
            send_via_memfd(sock, msg_type=1, payload=payload, canonical_bytes=canonical_bytes)
            mock_unlimited.assert_called_once()

    def test_linux_memfd_unavailable_falls_back(self):
        """Linux 上 memfd_create 不可用时降级"""
        sock = FakeSocket()
        payload = {"path": "big.rs"}
        canonical_bytes = b"x" * (MAX_MSG_BYTES + 100)

        # _IS_LINUX=True 但 memfd_create 失败
        with mock.patch(
            "callwarden.server.ipc_transport._IS_LINUX", True
        ), mock.patch(
            "callwarden.server.ipc_transport._linux_memfd_create",
            side_effect=OSError("memfd_create not available"),
        ), mock.patch(
            "callwarden.server.ipc_transport._send_framed_unlimited",
            wraps=lambda *a, **kw: None,
        ) as mock_unlimited:
            send_via_memfd(sock, msg_type=1, payload=payload, canonical_bytes=canonical_bytes)
            mock_unlimited.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
