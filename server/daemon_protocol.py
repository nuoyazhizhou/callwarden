"""Enterprise daemon 的有界 UDS JSON RPC 协议。"""

from __future__ import annotations

import json
import os
import socket
import struct
from array import array
from typing import Any, Dict


HEADER = struct.Struct("!I")
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_FDS = 1


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
    """发送单个长度分帧 JSON 对象。"""
    if not isinstance(message, dict):
        raise ProtocolError("消息必须是 JSON object")
    payload = json.dumps(
        message, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > max_bytes:
        raise ProtocolError(f"消息超过限制: {len(payload)} > {max_bytes}")
    sock.sendall(HEADER.pack(len(payload)) + payload)


def recv_message(sock: socket.socket,
                 max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> Dict[str, Any]:
    """接收单个长度分帧 JSON 对象。"""
    (size,) = HEADER.unpack(_recv_exact(sock, HEADER.size))
    if size <= 0 or size > max_bytes:
        raise ProtocolError(f"非法消息长度: {size}")
    try:
        message = json.loads(_recv_exact(sock, size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"JSON 解码失败: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("消息必须是 JSON object")
    return message


def send_message_with_fds(sock: socket.socket, message: Dict[str, Any],
                          fds: list[int],
                          max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
    """发送 JSON 帧并附带少量 SCM_RIGHTS 文件描述符。"""
    if not hasattr(sock, "sendmsg") or not hasattr(socket, "SCM_RIGHTS"):
        raise ProtocolError("当前平台不支持 SCM_RIGHTS")
    if not fds or len(fds) > DEFAULT_MAX_FDS:
        raise ProtocolError(f"FD 数量必须在 1..{DEFAULT_MAX_FDS} 之间")
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
    """接收 JSON 帧及首包携带的 SCM_RIGHTS FD。"""
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
        (size,) = HEADER.unpack(header)
        if size <= 0 or size > max_bytes:
            raise ProtocolError(f"非法消息长度: {size}")
        try:
            message = json.loads(_recv_exact(sock, size).decode("utf-8"))
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
    """解析 RPC 响应，远端错误转换为异常。"""
    if response.get("ok") is True:
        return response.get("result")
    error = response.get("error") or {}
    raise DaemonRemoteError(
        str(error.get("code", "daemon_error")),
        str(error.get("message", "unknown daemon error")),
    )
