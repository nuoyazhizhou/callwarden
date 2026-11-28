#!/usr/bin/env python3
"""Reviewer A'-loop RPC helper for reviewer-t1-t7 (T1/T7).

Usage:
  python cw_reviewer_rpc.py register <session_suffix>
  python cw_reviewer_rpc.py lease <task_id> <session_suffix>
  python cw_reviewer_rpc.py release <task_id> <token> <session_suffix>
Reads verdict payload from stdin as JSON for: verdict <task_id> <token> <fencing> <session_suffix>
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "C:/git_work")

from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402

AGENT_ID = "reviewer-t1-t7"
AGENT_INSTANCE_ID = "reviewer-t1-t7-inst"
MODEL_ID = "workbuddy-agent"
ROLE = "reviewer"


def build_identity(session_suffix: str) -> dict:
    return {
        "agent_id": AGENT_ID,
        "agent_instance_id": AGENT_INSTANCE_ID,
        "client_id": "workbuddy-reviewer",
        "provider": "workbuddy",
        "model_id": MODEL_ID,
        "model_mode": "agent",
        "system_fingerprint": "workbuddy",
        "session_id": f"sess-reviewer-t1-t7-{session_suffix}",
        "role": ROLE,
        "runtime_hash": "workbuddy-reviewer-t1-t7",
    }


def main() -> int:
    cmd = sys.argv[1]
    client = HttpDaemonRpcClient()
    if cmd == "register":
        ident = build_identity(sys.argv[2])
        res = client.call("agent.register", {
            "agent_id": AGENT_ID,
            "agent_name": AGENT_ID,
            "capabilities": ["code-review"],
            "identity": ident,
        })
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    if cmd == "lease":
        task_id = sys.argv[2]
        ident = build_identity(sys.argv[3])
        res = client.call("lease.acquire", {
            "task_id": task_id,
            "role": ROLE,
            "identity": ident,
            "ttl_seconds": 3600,
        })
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    if cmd == "release":
        task_id, token = sys.argv[2], sys.argv[3]
        ident = build_identity(sys.argv[4])
        res = client.call("lease.release", {
            "task_id": task_id,
            "role": ROLE,
            "token": token,
            "identity": ident,
        })
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    if cmd == "verdict":
        task_id, token, fencing = sys.argv[2], sys.argv[3], int(sys.argv[4])
        ident = build_identity(sys.argv[5])
        payload = json.load(sys.stdin)
        params = {
            "task_id": task_id,
            "phase": payload["phase"],
            "overall": payload["overall"],
            "attestation": payload["attestation"],
            "findings": payload.get("findings", ""),
            "clause_results": payload.get("clause_results", ""),
            "lease_token": token,
            "fencing_counter": fencing,
            "identity": ident,
        }
        for key in ("step_id", "contract_id", "contract_revision", "contract_hash",
                    "role_contract_id", "role_contract_revision", "role_contract_hash",
                    "view_manifest_hash", "snapshot_id", "reviewer_identity",
                    "amendment_ref", "request_id"):
            if payload.get(key):
                params[key] = payload[key]
        res = client.call("verdict.submit", params)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
