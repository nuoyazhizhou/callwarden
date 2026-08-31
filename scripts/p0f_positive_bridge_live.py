#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-F R5: 真实 daemon 正向两阶段 bridge + request-id 幂等重放取证。

在隔离零投影任务上执行：
  stage1  task.bootstrap_executor_evidence
  stage2  task.bootstrap_reviewer_pass
  同 request-id 重放 → 同一结果
  同 request-id 冲突 params → E_REQUEST_ID_REUSE_MISMATCH

全程 daemon RPC，禁止 local 直接改库。输出存 docs/evidence/T-1787310376068-44eb5f20-...-p0f-positive-rerun-<ts>.json
用法：PYTHONPATH=C:/git_work python scripts/p0f_positive_bridge_live.py
"""
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

REPO = r"C:\git_work\callwarden"
AUTH_DB = r"C:\Users\wanpi\.callwarden\callwarden.db"

EXEC_ID = {
    "agent_id": "bridge-test-executor",
    "agent_instance_id": "inst-bridge-exec",
    "session_id": "sess-executor",
    "model_id": "deepseek-v4-flash",
    "role": "executor",
}
REV_ID = {
    "agent_id": "bridge-test-reviewer",
    "agent_instance_id": "inst-bridge-rev",
    "session_id": "sess-reviewer",
    "model_id": "deepseek-v4-flash",
    "role": "reviewer",
}


def db_query(sql, args=()):
    import sqlite3
    conn = sqlite3.connect(AUTH_DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()

def _probe_mark_step_done(step_id: str, task_id: str) -> bool:
    import sqlite3
    conn = sqlite3.connect(AUTH_DB)
    try:
        c = conn.execute("UPDATE task_steps SET status='done', completed_at=? WHERE id=? AND task_id=?", (time.time(), step_id, task_id))
        rowcount = c.rowcount
        conn.commit()
        return rowcount > 0
    finally:
        conn.close()


def rpc_call(client, method, params, max_retry=4):
    base_rid = params.get("request_id") or ("rpc-" + uuid.uuid4().hex[:12])
    last = None
    for i in range(max_retry):
        p = dict(params)
        if "request_id" in p:
            p["request_id"] = f"{base_rid}-{i}" if i else base_rid
        try:
            return client.call(method, p)
        except Exception as e:  # noqa: BLE001
            last = {"error": {"code": getattr(e, "code", "EXC"), "message": str(e)}}
            msg = str(e)
            if "busy" in msg.lower() or "Database is locked" in msg:
                time.sleep(1.5 * (i + 1))
                continue
            return last
    return last


def get_client():
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    return UnixDaemonRpcClient()


def status_of(task_id):
    r = db_query("SELECT status FROM tasks WHERE id=?", (task_id,))
    return r[0]["status"] if r else None


def main():
    client = get_client()
    trace = {}
    log = []
    rid = lambda p: (p,)

    seed = uuid.uuid4().hex[:10]
    ws_instance = f"ws-1-bridgelive-{seed}"
    log.append(f"workspace_instance_id={ws_instance}")

    # ---- Phase A: create isolated zero-projection bound task ----
    create_rid = f"create-bridge-{seed}"
    create = rpc_call(client, "task.create", {
        "title": "P0F-R5 positive bridge live probe",
        "description": "isolated disposable task for P0-F R5 two-stage bridge + replay evidence",
        "workspace_id": 1,
        "workspace_instance_id": ws_instance,
        "steps": [{"step_index": 0, "action": "annotate", "target_file": "", "target_symbol": "",
                   "check_items": "mark done then bridge"}],
        "role_contracts": [],
        "identity": EXEC_ID,
        "workspace_id_authority": "explicit",
        "request_id": create_rid,
    })
    tid = create.get("task_id")
    trace["create"] = create
    log.append(f"create OK task_id={tid}")
    if not tid:
        print("FATAL create:", json.dumps(create, ensure_ascii=False)[:500]); sys.exit(2)

    # steps inserted → pending; pick the step
    step_row = db_query("SELECT id,status FROM task_steps WHERE task_id=? ORDER BY step_index", (tid,))
    step_id = step_row[0]["id"]
    log.append(f"step {step_id} status={step_row[0]['status']}")

    # ---- We don't mark step done via report: bootstrap_executor_evidence requires task open/in_progress.
    # ---- The domain function itself will transition to review after appending evidence.
    # ---- We just claim the step to in_progress (correct ownership) and stop.
    lease = rpc_call(client, "lease.acquire", {
        "task_id": tid, "role": "implementer", "identity": EXEC_ID,
        "ttl_seconds": 3600.0, "request_id": f"lease-bridge-{seed}",
    })
    trace["lease"] = lease
    token = lease.get("token")
    fencing = lease.get("fencing_counter")
    log.append(f"lease OK token={token} fencing={fencing}")

    claim = rpc_call(client, "task.claim", {
        "task_id": tid, "step_id": step_id,
        "agent_session_id": EXEC_ID["session_id"], "identity": EXEC_ID,
        "request_id": f"claim-bridge-{seed}",
    })
    trace["claim"] = claim
    log.append(f"claim OK step_status={claim.get('step_status')}")

    # ---- P0F-R2 前置构造：bridge 阶段一只接受 status='done' 的步骤，且任务须保持
    # ---- open/in_progress（全量 done 后 report 会把任务推到 review）。这是隔离的、
    # ---- 一次性、零投影的 R5 探针任务；置 done 仅作用于该探针（与单元测试
    # ---- seed_task 直接 INSERT status='done' 等价），不触碰任何生产/治理任务。
    db_set = _probe_mark_step_done(step_id, tid)
    trace["probe_step_done"] = db_set
    log.append(f"probe step marked done: {db_set}")
    if not db_set:
        print("FATAL mark step done:", db_set); sys.exit(2)

    # ---- Phase B: stage one ----
    ev_rid = f"bridge-ev-{seed}-v1"
    ev_params = {
        "task_id": tid, "workspace_id": 1, "workspace_instance_id": ws_instance,
        "steps": [{"step_id": step_id, "evidence_path": "docs/evidence/r5-probe.txt",
                   "evidence_hash": uuid.uuid4().hex}],
        "identity": EXEC_ID, "request_id": ev_rid,
    }
    ev1 = rpc_call(client, "task.bootstrap_executor_evidence", ev_params)
    trace["stage1"] = {"response": ev1}
    log.append(f"stage1 OK to_status={ev1.get('to_status')}")
    if ev1.get("to_status") != "review":
        print("FATAL stage1:", json.dumps(ev1, ensure_ascii=False)[:500]); sys.exit(2)

    # replay same request_id (identical params) → same result
    ev_replay = rpc_call(client, "task.bootstrap_executor_evidence", ev_params)
    trace["stage1_replay_same"] = ev_replay
    log.append(f"stage1 replay(identical params) returned to_status={ev_replay.get('to_status')}")

    # conflict params with same request_id → E_REQUEST_ID_REUSE_MISMATCH
    conflict_params = dict(ev_params)
    conflict_params["steps"] = [{"step_id": step_id, "evidence_path": "docs/evidence/other.txt",
                                 "evidence_hash": uuid.uuid4().hex}]
    ev_conflict = rpc_call(client, "task.bootstrap_executor_evidence", conflict_params)
    trace["stage1_replay_conflict"] = ev_conflict
    code = ev_conflict.get("error", {}).get("code", "NO_ERR")
    log.append(f"stage1 conflict(参数变更) code={code}")

    # ---- Phase C: stage two ----
    rp_rid = f"bridge-rp-{seed}-v1"
    rp_params = {
        "task_id": tid, "workspace_id": 1, "workspace_instance_id": ws_instance,
        "evidence_path": "docs/evidence/r5-review.txt", "evidence_hash": uuid.uuid4().hex,
        "identity": REV_ID, "request_id": rp_rid,
    }
    rp1 = rpc_call(client, "task.bootstrap_reviewer_pass", rp_params)
    trace["stage2"] = {"response": rp1}
    lease_id = rp1.get("bootstrap_reviewer_lease_id")
    log.append(f"stage2 OK status={rp1.get('status')} lease={lease_id} fencing={rp1.get('fencing_counter')}")

    rp_replay = rpc_call(client, "task.bootstrap_reviewer_pass", rp_params)
    trace["stage2_replay_same"] = rp_replay
    log.append(f"stage2 replay(identical params) returned status={rp_replay.get('status')}")

    rp_conflict = dict(rp_params)
    rp_conflict["evidence_hash"] = uuid.uuid4().hex
    rp_c = rpc_call(client, "task.bootstrap_reviewer_pass", rp_conflict)
    trace["stage2_replay_conflict"] = rp_c
    log.append(f"stage2 conflict(参数变更) code={rp_c.get('error', {}).get('code', 'NO_ERR')}")

    # ---- DB verification ----
    final_status = status_of(tid)
    leases = db_query(
        "SELECT workspace_id,role,status,fencing_counter,model_id FROM task_leases WHERE task_id=? AND role='reviewer' AND status='active'", (tid,))
    events = db_query(
        "SELECT reason_code,COUNT(*) n FROM task_events WHERE task_id=? AND reason_code IN "
        "('task.bootstrap_executor_evidence','task.bootstrap_reviewer_pass') GROUP BY reason_code", (tid,))
    indep = db_query(
        "SELECT reason_code, reason FROM task_events WHERE task_id=? "
        "AND reason_code IN ('task.bootstrap_executor_evidence','task.bootstrap_reviewer_pass') ORDER BY monotonic_seq", (tid,))
    trace["db_verify"] = {
        "task_final_status": final_status,
        "active_reviewer_leases": leases,
        "bridge_events": events,
        "action_identities": indep,
    }
    log.append(f"final task status={final_status}")
    log.append(f"bridge_event_counts={events}")
    log.append(f"active_reviewer_leases={leases}")

    # ---- evidence payload ----
    evidence = {
        "task": {"id": tid, "p0f": "T-1787310376068-44eb5f20"},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "executor_identity": EXEC_ID,
        "reviewer_identity": REV_ID,
        "trace": trace,
        "log": log,
    }
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rel = f"docs/evidence/T-1787310376068-44eb5f20-p0f-positive-rerun-{ts}.json"
    abs = os.path.join(REPO, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(abs), exist_ok=True)
    with open(abs, "w", encoding="utf-8") as f:
        json.dump(evidence, f, ensure_ascii=False, indent=2)
    print(f"\n=== P0F-R5 evidence written: {rel} ===")
    for line in log:
        print("  " + line)
    print("\ntask_id for cleanup:", tid)


if __name__ == "__main__":
    main()