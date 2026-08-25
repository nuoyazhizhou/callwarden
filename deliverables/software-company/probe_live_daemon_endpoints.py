"""只读探测本机已监听 daemon endpoint；不启动服务、不写任务库。"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
from callwarden.server.daemon_client import UnixDaemonRpcClient

endpoints = [
    r"\\.\pipe\callwarden-S-1-5-21-1583625257-826939952-3615027596-1001",
    *(f"tcp://127.0.0.1:{port}" for port in (2134, 7630, 14889)),
]
for endpoint in endpoints:
    try:
        result = UnixDaemonRpcClient(socket_path=endpoint, endpoint_override=True, timeout=2.0).call("ping", {})
        print(json.dumps({"endpoint": endpoint, "ok": True, "ping": result}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"endpoint": endpoint, "ok": False, "error": repr(exc)}, ensure_ascii=False))
