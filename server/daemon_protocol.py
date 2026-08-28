"""Enterprise daemon 的有界 UDS JSON RPC 协议。

Phase 4-1 wire-production：默认走 Rust 短路（帧编解码/响应解析），
rollback_config 中 feature=rust_daemon_protocol 置为 1 时回退 Python。
Rust 失败时 fail-soft 降级到 Python 路径（与 Phase 2-6/3-4 模式一致）。
"""

from __future__ import annotations

import json
import os
import socket
import struct
import time
from array import array
from typing import Any, Dict


HEADER = struct.Struct("!I")
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_FDS = 1


# ============================================
# Phase 4-1 wire-production: Rust 短路
# ============================================
# daemon_protocol 默认走 Rust PyO3 API（callwarden_core.protocol_*），
# rollback_config 中 feature=rust_daemon_protocol 置为 1 时回退 Python。
# Rust 失败时 fail-soft 降级到 Python 路径。
# Rust 端只暴露纯计算 API（帧编解码/响应解析/常量查询），不涉及 socket 操作。

_RUST_PROTOCOL_AVAILABLE = False
_callwarden_core = None
try:
    import callwarden_core as _callwarden_core  # type: ignore
    # 验证关键 API 可用
    if hasattr(_callwarden_core, "protocol_encode_payload") and hasattr(
        _callwarden_core, "protocol_parse_response"
    ):
        _RUST_PROTOCOL_AVAILABLE = True
except ImportError:
    _callwarden_core = None

# rollback_config 查询缓存（60s TTL，避免每次帧收发都发起 RPC）
_ROLLBACK_CACHE: Dict[str, Any] = {"ts": 0.0, "value": False}
_ROLLBACK_CACHE_TTL = 60.0
# SRV-007 重入守卫：回滚查询本身经本模块帧收发（UnixDaemonRpcClient）传输，
# 缓存过期瞬间存在 _is_rust_protocol_rolled_back → RPC → send_message →
# _is_rust_protocol_rolled_back 的自依赖环；in-flight 时直接视为未回滚。
_ROLLBACK_QUERY_STATE: Dict[str, Any] = {"in_flight": False}


def _is_rust_protocol_rolled_back() -> bool:
    """检查 rust_daemon_protocol feature 是否已回滚（60s 缓存）

    SRV-007 收敛（daemon protocol rollback Python authority → Rust daemon）：
    rollback_config 的 SQLite 权威已下沉到 daemon，本函数为纯薄客户端，
    经 `mcp.daemon_protocol.is_rust_protocol_rolled_back` 查询。只读 authority 读：
    daemon 不可用时 fail-soft 视为未回滚（返回 False，与 SRV-003
    is_rust_backup_rolled_back 的 rollback 探测语义一致），绝不回退本地 SQLite。
    60s 缓存保留，避免热路径（帧收发）频繁 RPC；重入守卫防自依赖递归。
    """
    now = time.time()
    if now - _ROLLBACK_CACHE["ts"] < _ROLLBACK_CACHE_TTL:
        return _ROLLBACK_CACHE["value"]  # type: ignore[return-value]
    if _ROLLBACK_QUERY_STATE["in_flight"]:
        # 重入：查询正在经本模块帧收发传输，视为未回滚（fail-soft），不写缓存
        return False
    _ROLLBACK_QUERY_STATE["in_flight"] = True
    try:
        result = _call_daemon_rpc(
            "mcp.daemon_protocol.is_rust_protocol_rolled_back", {})
        value = bool(isinstance(result, dict) and result.get("rolled_back"))
    except Exception:
        # fail-soft：只读探测，daemon 不可用时视为未回滚（不回退本地 SQLite）
        value = False
    finally:
        _ROLLBACK_QUERY_STATE["in_flight"] = False
    _ROLLBACK_CACHE["ts"] = now
    _ROLLBACK_CACHE["value"] = value
    return value


def _call_daemon_rpc(method: str, params: Dict[str, Any]) -> Any:
    """经 daemon 统一 fail-closed 客户端发起 RPC（SRV-007，不回退本地 SQLite）。

    函数体内延迟导入，避免 daemon_protocol ↔ daemon_client 循环依赖。
    """
    from ._mcp_common import _call_daemon_rpc as _rpc

    return _rpc(method, params)


def _rust_encode_payload(message: Dict[str, Any]) -> bytes:
    """Rust 短路：编码 payload（不含 header）"""
    return bytes(_callwarden_core.protocol_encode_payload(message))


def _rust_build_frame(message: Dict[str, Any]) -> bytes:
    """Rust 短路：构建完整帧（header + payload）"""
    return bytes(_callwarden_core.protocol_build_frame(message))


def _rust_parse_header(header_bytes: bytes) -> int:
    """Rust 短路：解析 header 为 size"""
    return int(_callwarden_core.protocol_parse_header(header_bytes))


def _rust_decode_payload(payload: bytes) -> Dict[str, Any]:
    """Rust 短路：解码 payload 为 dict"""
    return _callwarden_core.protocol_decode_payload(payload)


def _rust_parse_response(response: Dict[str, Any]) -> Any:
    """Rust 短路：解析 RPC 响应"""
    return _callwarden_core.protocol_parse_response(response)


class ProtocolError(RuntimeError):
    """IPC 帧或 JSON 请求不合法。"""

class DaemonRemoteError(RuntimeError):
    """daemon 返回的结构化远端错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ProtocolError("连接在消息接收完成前关闭")
        chunks.extend(chunk)
    return bytes(chunks)


def send_message(sock: socket.socket, message: Dict[str, Any],
                 max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
    """发送单个长度分帧 JSON 对象。

    Phase 4-1 wire-production：默认走 Rust ``protocol_build_frame`` 短路。
    rollback_config 中 feature=rust_daemon_protocol 置为 1 时回退 Python。
    Rust 失败时 fail-soft 降级到 Python 路径。
    """
    if not isinstance(message, dict):
        raise ProtocolError("消息必须是 JSON object")
    # Phase 4-1 wire-production: Rust 短路
    if _RUST_PROTOCOL_AVAILABLE and not _is_rust_protocol_rolled_back():
        try:
            frame = _rust_build_frame(message)
            if len(frame) - HEADER.size > max_bytes:
                raise ProtocolError(f"消息超过限制: {len(frame) - HEADER.size} > {max_bytes}")
            sock.sendall(frame)
            return
        except ProtocolError:
            raise
        except Exception:
            pass  # fail-soft → 降级 Python 路径
    # Python 降级路径
    payload = json.dumps(
        message, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > max_bytes:
        raise ProtocolError(f"消息超过限制: {len(payload)} > {max_bytes}")
    sock.sendall(HEADER.pack(len(payload)) + payload)


def recv_message(sock: socket.socket,
                 max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> Dict[str, Any]:
    """接收单个长度分帧 JSON 对象。

    Phase 4-1 wire-production：默认走 Rust ``protocol_parse_header`` +
    ``protocol_decode_payload`` 短路。rollback_config 中 feature=rust_daemon_protocol
    置为 1 时回退 Python。Rust 失败时 fail-soft 降级到 Python 路径。
    """
    header_bytes = _recv_exact(sock, HEADER.size)
    # Phase 4-1 wire-production: Rust 短路（header 解析）
    size: int
    use_rust = _RUST_PROTOCOL_AVAILABLE and not _is_rust_protocol_rolled_back()
    if use_rust:
        try:
            size = _rust_parse_header(header_bytes)
        except Exception:
            (size,) = HEADER.unpack(header_bytes)  # fail-soft 降级
            use_rust = False
    else:
        (size,) = HEADER.unpack(header_bytes)
    if size <= 0 or size > max_bytes:
        raise ProtocolError(f"非法消息长度: {size}")
    payload = _recv_exact(sock, size)
    # Phase 4-1 wire-production: Rust 短路（payload 解码）
    if use_rust:
        try:
            return _rust_decode_payload(payload)
        except Exception:
            pass  # fail-soft → 降级 Python 路径
    # Python 降级路径
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"JSON 解码失败: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("消息必须是 JSON object")
    return message


def send_message_with_fds(sock: socket.socket, message: Dict[str, Any],
                          fds: list[int],
                          max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
    """发送 JSON 帧并附带少量 SCM_RIGHTS 文件描述符。

    Phase 4-1 wire-production：默认走 Rust ``protocol_encode_payload`` 短路（仅 payload 编码），
    SCM_RIGHTS 仍由 Python socket.sendmsg 处理（Rust 端不暴露 socket 操作）。
    rollback_config 中 feature=rust_daemon_protocol 置为 1 时回退 Python。
    """
    if not hasattr(sock, "sendmsg") or not hasattr(socket, "SCM_RIGHTS"):
        raise ProtocolError("当前平台不支持 SCM_RIGHTS")
    if not fds or len(fds) > DEFAULT_MAX_FDS:
        raise ProtocolError(f"FD 数量必须在 1..{DEFAULT_MAX_FDS} 之间")
    # Phase 4-1 wire-production: Rust 短路（仅 payload 编码）
    payload: bytes
    if _RUST_PROTOCOL_AVAILABLE and not _is_rust_protocol_rolled_back():
        try:
            payload = _rust_encode_payload(message)
        except Exception:
            payload = json.dumps(
                message, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")  # fail-soft 降级
    else:
        payload = json.dumps(
            message, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    if len(payload) > max_bytes:
        raise ProtocolError(f"消息超过限制: {len(payload)} > {max_bytes}")
    frame = HEADER.pack(len(payload)) + payload
    rights = array("i", fds)
    sent = sock.sendmsg(
        [frame], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights.tobytes())]
    )
    if sent < len(frame):
        sock.sendall(frame[sent:])


def recv_message_with_fds(sock: socket.socket,
                          max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
                          max_fds: int = DEFAULT_MAX_FDS) -> tuple[Dict[str, Any], list[int]]:
    """接收 JSON 帧及首包携带的 SCM_RIGHTS FD。

    Phase 4-1 wire-production：默认走 Rust ``protocol_parse_header`` +
    ``protocol_decode_payload`` 短路（仅帧解析），SCM_RIGHTS 仍由 Python socket.recvmsg 处理。
    rollback_config 中 feature=rust_daemon_protocol 置为 1 时回退 Python。
    """
    if not hasattr(sock, "recvmsg") or not hasattr(socket, "SCM_RIGHTS"):
        return recv_message(sock, max_bytes), []
    item_size = array("i").itemsize
    header, ancillary, flags, _addr = sock.recvmsg(
        HEADER.size, socket.CMSG_SPACE(max_fds * item_size)
    )
    if not header:
        raise ProtocolError("连接在消息接收完成前关闭")
    if len(header) < HEADER.size:
        header += _recv_exact(sock, HEADER.size - len(header))
    received_fds: list[int] = []
    try:
        if flags & getattr(socket, "MSG_CTRUNC", 0):
            raise ProtocolError("SCM_RIGHTS ancillary data 被截断")
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array("i")
                values.frombytes(data[:len(data) - (len(data) % item_size)])
                received_fds.extend(values.tolist())
        if len(received_fds) > max_fds:
            raise ProtocolError(f"收到过多 FD: {len(received_fds)} > {max_fds}")
        # Phase 4-1 wire-production: Rust 短路（header 解析）
        size: int
        use_rust = _RUST_PROTOCOL_AVAILABLE and not _is_rust_protocol_rolled_back()
        if use_rust:
            try:
                size = _rust_parse_header(header)
            except Exception:
                (size,) = HEADER.unpack(header)  # fail-soft 降级
                use_rust = False
        else:
            (size,) = HEADER.unpack(header)
        if size <= 0 or size > max_bytes:
            raise ProtocolError(f"非法消息长度: {size}")
        payload = _recv_exact(sock, size)
        # Phase 4-1 wire-production: Rust 短路（payload 解码）
        if use_rust:
            try:
                message = _rust_decode_payload(payload)
                return message, received_fds
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        # Python 降级路径
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"JSON 解码失败: {exc}") from exc
        if not isinstance(message, dict):
            raise ProtocolError("消息必须是 JSON object")
        return message, received_fds
    except Exception:
        for fd in received_fds:
            os.close(fd)
        raise


def parse_response(response: Dict[str, Any]) -> Any:
    """解析 RPC 响应，远端错误转换为异常。

    Phase 4-1 wire-production：默认走 Rust ``protocol_parse_response`` 短路。
    rollback_config 中 feature=rust_daemon_protocol 置为 1 时回退 Python。
    Rust 失败时 fail-soft 降级到 Python 路径。

    注意：Rust 端抛 ``PyRuntimeError``（``"code: message"`` 格式），需转换为
    Python 端 ``DaemonRemoteError`` 以保持异常类型兼容。
    """
    # Phase 4-1 wire-production: Rust 短路
    if _RUST_PROTOCOL_AVAILABLE and not _is_rust_protocol_rolled_back():
        try:
            return _rust_parse_response(response)
        except RuntimeError as exc:
            # Rust 端抛 PyRuntimeError（"code: message" 格式），转换为 DaemonRemoteError
            msg = str(exc)
            if ": " in msg:
                code, _, message = msg.partition(": ")
                raise DaemonRemoteError(code, message)
            raise DaemonRemoteError("daemon_error", msg)
        except Exception:
            pass  # fail-soft → 降级 Python 路径
    # Python 降级路径
    if response.get("ok") is True:
        return response.get("result")
    error = response.get("error") or {}
    raise DaemonRemoteError(
        str(error.get("code", "daemon_error")),
        str(error.get("message", "unknown daemon error")),
    )
