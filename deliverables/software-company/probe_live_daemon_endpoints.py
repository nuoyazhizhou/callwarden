"""只读探测本机已监听 daemon endpoint（ADR-002 单轨 HTTPS+mTLS）；不启动服务、不写任务库。"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402 —— sys.path/环境注入须先于 import(脚本直跑未走 site-packages,顺序为硬约束)

# 单轨语义：仅 https 端点可为 daemon endpoint（Named Pipe/UDS 传输已删除）。
# 6374 为 daemon_autostart.get_default_endpoint() 的回退默认值；其余为历史
# 候选监听端口（scheme 统一改 https 后按连通性逐个探测）。
endpoints = [f"https://127.0.0.1:{port}" for port in (6374, 2134, 7630, 14889)]
for endpoint in endpoints:
    try:
        result = HttpDaemonRpcClient(endpoint=endpoint, timeout=2.0).call("ping", {})
        print(json.dumps({"endpoint": endpoint, "ok": True, "ping": result}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"endpoint": endpoint, "ok": False, "error": repr(exc)}, ensure_ascii=False))
