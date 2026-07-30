#!/usr/bin/env python3
"""D2.7: SO_PEERCRED 不可伪造性验证

在请求体 params 中注入 uid=0，验证 daemon 忽略它，
仍使用 SO_PEERCRED 返回的真实 UID 进行 ACL 判定。
"""
import json
import socket
import struct
import os
import sys

SOCK_PATH = "/run/callwarden/callwarden.sock"
HEADER = struct.Struct("!I")


def send_rpc(sock, method, params):
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sock.sendall(HEADER.pack(len(payload)) + payload)
    header = b""
    while len(header) < 4:
        header += sock.recv(4 - len(header))
    (resp_len,) = HEADER.unpack(header)
    data = b""
    while len(data) < resp_len:
        data += sock.recv(resp_len - len(data))
    return json.loads(data.decode("utf-8"))


def main():
    uid = os.getuid()
    # 伪造请求体中的 uid=0（应被 daemon 忽略）
    forged_params = {"uid": 0, "fake_peer_uid": 0, "auth": "admin"}
    print(f"[client] uid={uid} (real SO_PEERCRED uid)")
    print(f"[client] forged params={json.dumps(forged_params)} (应被 daemon 忽略)")

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCK_PATH)
        resp = send_rpc(sock, "backup", forged_params)
        sock.close()
        print(f"[response] {json.dumps(resp, ensure_ascii=False)}")

        # 验证：非 root 用户即使伪造 uid=0，也应被 permission_denied
        if uid != 0:
            if resp.get("error", {}).get("code") == "permission_denied":
                print("[PASS] SO_PEERCRED 不可伪造：daemon 使用真实 UID 拒绝了伪造请求")
            else:
                print("[FAIL] daemon 可能被伪造的 uid 欺骗！", file=sys.stderr)
                sys.exit(1)
        else:
            # root 用户通过 ACL（但可能因参数不足返回 invalid_params）
            if resp.get("error", {}).get("code") == "permission_denied":
                print("[FAIL] root 不应被 permission_denied", file=sys.stderr)
                sys.exit(1)
            else:
                print("[PASS] root 通过 ACL（错误码非 permission_denied）")
    except Exception as e:
        print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
