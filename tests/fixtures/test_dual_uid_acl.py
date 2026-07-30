#!/usr/bin/env python3
"""Phase 4-4 D2: 双 UID SO_PEERCRED ACL E2E 验证脚本

用法：
  python3 /tmp/test_dual_uid_acl.py <method> [params_json]

该脚本连接 daemon UDS socket，发送 RPC 请求，打印响应。
用于验证不同 UID 下的 ACL 行为。
"""
import json
import socket
import struct
import sys
import os


SOCK_PATH = "/run/callwarden/callwarden.sock"
HEADER = struct.Struct("!I")


def send_rpc(sock, method, params=None):
    """发送 JSON-RPC 请求并接收响应。"""
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {},
    }
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    frame = HEADER.pack(len(payload)) + payload
    sock.sendall(frame)

    # 接收响应
    header_data = b""
    while len(header_data) < 4:
        chunk = sock.recv(4 - len(header_data))
        if not chunk:
            raise RuntimeError("连接在 header 接收完成前关闭")
        header_data += chunk
    (resp_len,) = HEADER.unpack(header_data)
    resp_data = b""
    while len(resp_data) < resp_len:
        chunk = sock.recv(resp_len - len(resp_data))
        if not chunk:
            raise RuntimeError("连接在 payload 接收完成前关闭")
        resp_data += chunk
    return json.loads(resp_data.decode("utf-8"))


def main():
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <method> [params_json]", file=sys.stderr)
        sys.exit(2)

    method = sys.argv[1]
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    # 打印当前 UID（供日志核对）
    uid = os.getuid()
    print(f"[client] uid={uid} method={method} params={json.dumps(params)}")

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCK_PATH)
        resp = send_rpc(sock, method, params)
        sock.close()
        print(f"[response] {json.dumps(resp, ensure_ascii=False)}")
    except Exception as e:
        print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
