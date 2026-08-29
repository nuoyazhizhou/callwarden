#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行为级验证 verdict normalization 修复（47c404f 部署后）。

对单张 review 卡经 UnixDaemonRpcClient（直达 daemon）提交 pass verdict：
1. 检查 task_verdict_events 新行 4 个 normalization 列是否落库；
2. next-action 是否从 governance_blocked 转为 adjudication 就绪。

用法：PYTHONPATH=C:/git_work python scripts/verify_verdict_fix.py <task_id>
"""
import json
import sys
import uuid

sys.path.insert(0, r"C:\git_work")
sys.path.insert(0, r"C:\git_work\callwarden\scripts")

from review_aprime_cards import (  # noqa: E402
    REVIEWER_AGENT, REVIEWER_MODEL, WORKSPACE_ID,
    compute_role_contract_hash, fetch_card_bindings, get_db,
)
from callwarden.server.daemon_client import UnixDaemonRpcClient  # noqa: E402


def main():
    task_id = sys.argv[1]
    session = f"sess-verify-{uuid.uuid4().hex[:8]}"
    client = UnixDaemonRpcClient()

    lease = client.call("lease.acquire", {
        "task_id": task_id, "role": "reviewer",
        "identity": {"agent_id": REVIEWER_AGENT, "session_id": session,
                     "model_id": REVIEWER_MODEL, "role": "reviewer"},
        "ttl_seconds": 1800.0, "request_id": f"lease-verify-{uuid.uuid4().hex[:8]}",
    })
    token, fencing = lease["token"], lease["fencing_counter"]
    print(f"[1] lease.acquire OK: {lease['lease_id']} fencing={fencing}")

    conn = get_db()
    b = fetch_card_bindings(conn, task_id)
    if not b:
        print("FATAL: 无 contract binding"); sys.exit(2)
    tcr, rc_dict, step_id = b
    rc_hash = compute_role_contract_hash(rc_dict["contract_id"], rc_dict["revision"], task_id, rc_dict)
    print(f"[2] contract={tcr['contract_id']} rev={tcr['revision']} step={step_id} rc_hash={rc_hash[:24]}…")

    params = {
        "task_id": task_id, "step_id": step_id,
        "contract_id": tcr["contract_id"], "contract_revision": int(tcr["revision"]),
        "contract_hash": tcr["contract_hash"],
        "role_contract_id": rc_dict["contract_id"], "role_contract_revision": int(rc_dict["revision"]),
        "role_contract_hash": rc_hash,
        "phase": "blind_first_pass", "view_manifest_hash": "sha256:verify-norm-fix",
        "snapshot_id": f"ws-1-{task_id[-6:]}",
        "clause_results": [{"clause_id": "independent_review", "decision": "pass"}],
        "findings": [],
        "overall": "pass",
        "attestation": "行为级验证 verdict normalization 修复（47c404f）",
        "request_id": f"verify-{task_id[-6:]}-{uuid.uuid4().hex[:8]}",
        "identity": {"agent_id": REVIEWER_AGENT, "session_id": session,
                     "model_id": REVIEWER_MODEL, "role": "reviewer"},
        "lease_token": token, "fencing_counter": fencing, "workspace_id": WORKSPACE_ID,
    }
    r = client.call("verdict.submit", params)
    print("[3] verdict.submit OK:", json.dumps(r, ensure_ascii=False)[:300])

    row = conn.execute(
        "SELECT verdict_id, overall, normalization_version, normalization_rules_hash, "
        "canonicalization_version, canonicalization_rules_hash "
        "FROM task_verdict_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    print("[4] 新 verdict 行:")
    print("   ", dict(row) if row else None)

    na = client.call("task.next_action", {"task_id": task_id, "workspace_instance_id": "ws-1"})
    print("[5] next-action:", json.dumps({
        "lifecycle_status": na.get("lifecycle_status"),
        "workflow_status": na.get("workflow_status"),
        "required_role": na.get("required_role"),
        "next_action": na.get("next_action"),
        "review_state": (na.get("review") or {}).get("state"),
        "decision": na.get("decision"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
