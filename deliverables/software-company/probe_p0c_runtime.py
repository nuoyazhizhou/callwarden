from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))
from callwarden.server.daemon_client import UnixDaemonRpcClient  # noqa: E402 —— sys.path/环境注入须先于 import(脚本直跑未走 site-packages,顺序为硬约束)

client = UnixDaemonRpcClient()
result = {"ping": client.call("ping", {})}
try:
    client.call("task.contract_bootstrap", {})
    result["contract_bootstrap_probe"] = {"unexpected": "accepted"}
except Exception as exc:
    result["contract_bootstrap_probe"] = {"expected_rejection": str(exc)}
print(json.dumps(result, ensure_ascii=False, indent=2))
