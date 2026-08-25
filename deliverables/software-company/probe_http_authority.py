"""对显式 loopback endpoint 执行只读 HTTP RPC ping；不写入任务库。"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
from callwarden.server.daemon_client import HttpDaemonRpcClient

endpoint = "http://127.0.0.1:14012"
client = HttpDaemonRpcClient(endpoint=endpoint, verify_health=False, validate_manifest=False, timeout=3.0)
try:
    health = client.verify_health(endpoint=endpoint, manifest=None)
    ping = client.call("ping", {})
    print(json.dumps({"endpoint": endpoint, "health": health, "ping": ping}, ensure_ascii=False))
except Exception as exc:
    print(json.dumps({"endpoint": endpoint, "error": repr(exc)}, ensure_ascii=False))
    raise
