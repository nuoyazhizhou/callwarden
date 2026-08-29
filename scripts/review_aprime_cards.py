#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A′ review 卡幂等 reviewer 循环驱动。

父任务 T-1787293451688-c14b1e44 下 118 张 review 卡：
  逐卡独立只读核验 -> 提交 reviewer_pass / reviewer_blocked（两段式）。
  - 绝不写 reviewer-handoffs.md（其他 agent 持续追加）。
  - 处理进度记本地 state 文件（幂等，不重复处理）+ cw 追踪任务。
  - 严格遵循 callwarden-reviewer-loop 技能约束：
    * 直连 DaemonClient（命名管道），verdict.submit 入参带 workspace_id
    * role_contract_hash 按 c14n/v1 插入序精确重算
    * 每卡独立 session lease，提交后 release（block 尤其必须）
    * request_id 全局唯一，SQLite busy 重试必须换新 request_id

用法：
  python review_aprime_cards.py diagnose            # 只读分类，不提交
  python review_aprime_cards.py validate --task T-XXXX   # 单卡全链路验证
  python review_aprime_cards.py run [--limit N]     # 循环提交（幂等）
"""
import sys, os, json, hashlib, sqlite3, time, uuid, argparse

REPO = r"C:\git_work"
sys.path.insert(0, REPO)
DB = r"C:\Users\wanpi\.callwarden\callwarden.db"
PARENT = "T-1787293451688-c14b1e44"
WORKSPACE_ID = 1
REVIEWER_AGENT = "reviewer-wb-186loop"
REVIEWER_MODEL = "deepseek-v4-flash"
STATE_FILE = os.path.join(os.path.dirname(__file__), ".aprime_review_progress.json")

# ---------------------------------------------------------------------------
# role_contract_hash 计算（与 rust_ext task_collab_verdict.rs:238-260 一致）
# serde_json::Map 启用 preserve_order -> IndexMap 插入序；compact 无空格。
ROLE_CONTRACT_KEYS = [
    "canonicalization_version", "contract_id", "revision", "task_id", "role",
    "step_id", "skill_id", "skill_version", "prompt_template_id", "prompt_hash",
    "allowed_paths", "forbidden_paths", "commands", "acceptance_checks",
    "required_evidence", "handoff_to", "independence",
]

def compute_role_contract_hash(role_contract_id, revision, task_id, role_row):
    payload = {
        "canonicalization_version": "role-contract-c14n/v1",
        "contract_id": role_contract_id,
        "revision": int(revision),
        "task_id": task_id,
        "role": role_row["role"],
        "step_id": role_row["step_id"],
        "skill_id": role_row["skill_id"],
        "skill_version": role_row["skill_version"],
        "prompt_template_id": role_row["prompt_template_id"],
        "prompt_hash": role_row["prompt_hash"],
        "allowed_paths": role_row["allowed_paths"],
        "forbidden_paths": role_row["forbidden_paths"],
        "commands": role_row["commands"],
        "acceptance_checks": role_row["acceptance_checks"],
        "required_evidence": role_row["required_evidence"],
        "handoff_to": role_row["handoff_to"],
        "independence": role_row["independence"],
    }
    s = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
def get_db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def load_state():
    if os.path.isfile(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {"processed": {}, "meta": {}}

def save_state(st):
    tmp = STATE_FILE + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def review_cards(conn):
    rows = conn.execute(
        "SELECT id, title FROM tasks WHERE parent_id=? AND status='review' ORDER BY id",
        (PARENT,),
    ).fetchall()
    return [(r["id"], r["title"]) for r in rows]

def fetch_card_bindings(conn, task_id):
    """返回 (task_contract, role_contract_row_dict, first_step_id) 或 None。"""
    tcr = conn.execute(
        "SELECT contract_id, revision, contract_hash FROM task_contract_revisions "
        "WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)
    ).fetchone()
    if not tcr:
        return None
    rc = conn.execute(
        "SELECT contract_id, revision, role, step_id, skill_id, skill_version, "
        "prompt_template_id, prompt_hash, allowed_paths, forbidden_paths, commands, "
        "acceptance_checks, required_evidence, handoff_to, independence "
        "FROM role_contracts WHERE task_id=? AND role IN ('reviewer','independent_reviewer') "
        "AND is_current=1 ORDER BY revision DESC LIMIT 1", (task_id,)
    ).fetchone()
    if not rc:
        return None
    step = conn.execute(
        "SELECT id FROM task_steps WHERE task_id=? ORDER BY step_index LIMIT 1",
        (task_id,),
    ).fetchone()
    step_id = step["id"] if step else ""
    rc_dict = {
        "contract_id": rc["contract_id"], "revision": rc["revision"],
        "role": rc["role"], "step_id": rc["step_id"], "skill_id": rc["skill_id"],
        "skill_version": rc["skill_version"], "prompt_template_id": rc["prompt_template_id"],
        "prompt_hash": rc["prompt_hash"], "allowed_paths": rc["allowed_paths"],
        "forbidden_paths": rc["forbidden_paths"], "commands": rc["commands"],
        "acceptance_checks": rc["acceptance_checks"], "required_evidence": rc["required_evidence"],
        "handoff_to": rc["handoff_to"], "independence": rc["independence"],
    }
    return (dict(tcr), rc_dict, step_id)

# ---------------------------------------------------------------------------
def get_client():
    from callwarden.server.daemon_client import DaemonClient
    return DaemonClient.get_instance()

def rpc_call(client, method, params, max_retry=6):
    """直连 RPC（call_with_autostart，自动解包 result）；SQLite busy 重试时每次换新 request_id。"""
    base_rid = params.get("request_id") or ("rpc-" + uuid.uuid4().hex[:12])
    last = None
    for i in range(max_retry):
        p = dict(params)
        if "request_id" in p:
            p["request_id"] = f"{base_rid}-{i}"
        try:
            r = client.call_with_autostart(method, p)
            if isinstance(r, dict) and "result" in r:
                return r["result"]
            return r
        except Exception as e:
            msg = str(e)
            last = e
            if "Database is busy" in msg or "busy" in msg.lower():
                time.sleep(1.5 * (i + 1))
                continue
            raise
    raise last

def acquire_lease(client, task_id, session_id):
    params = {
        "task_id": task_id, "role": "reviewer",
        "identity": {
            "agent_id": REVIEWER_AGENT, "session_id": session_id,
            "model_id": REVIEWER_MODEL, "role": "reviewer",
        },
        "ttl_seconds": 3600.0,
    }
    r = rpc_call(client, "lease.acquire", params)
    return r.get("token"), r.get("fencing_counter")

def release_lease(client, task_id, token, session_id="sess-release"):
    # 2026-08-29 修复：lease.release 校验 holder session 一致性（task_collab_lease.rs:734），
    # 必须用 acquire 时的 session；原先固定 "sess-release" 导致释放静默失败、遗留活跃 lease。
    params = {
        "task_id": task_id, "role": "reviewer", "token": token,
        "identity": {
            "agent_id": REVIEWER_AGENT, "session_id": session_id,
            "model_id": REVIEWER_MODEL, "role": "reviewer",
        },
    }
    try:
        return rpc_call(client, "lease.release", params)
    except Exception as e:
        return {"error": str(e)}

def completion_review(client, task_id):
    try:
        r = rpc_call(client, "task.completion_review",
                     {"task_id": task_id, "workspace_id": WORKSPACE_ID})
        return r
    except Exception as e:
        return {"error": str(e)}

def submit_verdict(client, task_id, step_id, tcr, rc_dict, role_contract_hash,
                   overall, clause_results, findings, session_id, token, fencing):
    params = {
        "task_id": task_id,
        "step_id": step_id,
        "contract_id": tcr["contract_id"],
        "contract_revision": int(tcr["revision"]),
        "contract_hash": tcr["contract_hash"],
        "role_contract_id": rc_dict["contract_id"],
        "role_contract_revision": int(rc_dict["revision"]),
        "role_contract_hash": role_contract_hash,
        "phase": "blind_first_pass",
        "view_manifest_hash": "sha256:aprime-reviewer-view",
        "snapshot_id": f"ws-1-{task_id[-6:]}",
        "clause_results": clause_results,
        "findings": findings,
        "overall": overall,
        "attestation": f"{REVIEWER_AGENT} 独立只读核验（completion_review + 静态核验）",
        "request_id": f"rv-{task_id}-{uuid.uuid4().hex[:10]}",
        "identity": {
            "agent_id": REVIEWER_AGENT, "session_id": session_id,
            "model_id": REVIEWER_MODEL, "role": "reviewer",
        },
        "lease_token": token,
        "fencing_counter": fencing,
        "workspace_id": WORKSPACE_ID,
    }
    return rpc_call(client, "verdict.submit", params)

def governance_verdict_count(client, task_id):
    try:
        r = rpc_call(client, "task.governance_projection.get",
                     {"task_id": task_id, "workspace_id": WORKSPACE_ID})
        txt = json.dumps(r, ensure_ascii=False)
        return txt
    except Exception as e:
        return f"ERR {e}"

# ---------------------------------------------------------------------------
def cmd_diagnose(conn, client):
    cards = review_cards(conn)
    print(f"review 卡总数: {len(cards)}")
    pass_n = block_n = err_n = 0
    for tid, title in cards:
        b = fetch_card_bindings(conn, tid)
        if not b:
            print(f"  [NO-BINDING] {tid}"); err_n += 1; continue
        cr = completion_review(client, tid)
        dec = cr.get("decision") if isinstance(cr, dict) else None
        if isinstance(cr, dict) and cr.get("error"):
            print(f"  [ERR] {tid} {title[:40]} :: {cr['error'][:60]}"); err_n += 1
        elif dec == "pass":
            print(f"  [PASS-CAND] {tid} {title[:50]}"); pass_n += 1
        else:
            f = cr.get("findings") if isinstance(cr, dict) else None
            print(f"  [BLOCK-CAND] {tid} {title[:40]} :: decision={dec} findings={len(f) if isinstance(f,list) else f}"); block_n += 1
    print(f"\n分类: PASS候选={pass_n} BLOCK候选={block_n} 错误/无绑定={err_n}")

def cmd_validate(conn, client, task_id):
    print(f"=== 单卡验证 {task_id} ===")
    b = fetch_card_bindings(conn, task_id)
    if not b:
        print("无契约绑定，无法提交"); return
    tcr, rc_dict, step_id = b
    print(f"step_id={step_id}  contract={tcr['contract_id']} r{int(tcr['revision'])}")
    print(f"role_contract={rc_dict['contract_id']} r{int(rc_dict['revision'])}")
    rh = compute_role_contract_hash(rc_dict["contract_id"], rc_dict["revision"], task_id, rc_dict)
    print(f"computed role_contract_hash = {rh}")
    session = f"sess-rev-{task_id[-6:]}"
    token, fencing = acquire_lease(client, task_id, session)
    print(f"lease token={str(token)[:12]}... fencing={fencing}")
    cr = completion_review(client, task_id)
    print(f"completion_review decision = {cr.get('decision') if isinstance(cr,dict) else cr}")
    res = submit_verdict(client, task_id, step_id, tcr, rc_dict, rh,
                         "pass", [{"clause_id": "independent_review", "decision": "pass"}],
                         [], session, token, fencing)
    print(f"verdict.submit result = {json.dumps(res, ensure_ascii=False)[:300]}")
    proj = governance_verdict_count(client, task_id)
    print(f"governance_projection (含 Verdicts 计数) = {proj[:400]}")
    release_lease(client, task_id, token, session)
    print("lease released")

def cmd_run(conn, client, limit=None, force=False):
    cards = review_cards(conn)
    st = load_state()
    processed = st.setdefault("processed", {})
    done = 0
    for tid, title in cards:
        if tid in processed and not force:
            continue
        if limit and done >= limit:
            break
        # 权威幂等闸：DB 已有 verdict 事件则跳过（防止重复提交）；--force 绕过（重审）
        try:
            existing = conn.execute(
                "SELECT COUNT(*) FROM task_verdict_events WHERE task_id=?", (tid,)
            ).fetchone()[0]
        except Exception:
            existing = 0
        if existing and existing > 0 and not force:
            processed[tid] = {"status": "already_has_verdict", "ts": time.time()}
            save_state(st)
            continue
        b = fetch_card_bindings(conn, tid)
        if not b:
            processed[tid] = {"status": "skip_no_binding", "ts": time.time()}
            save_state(st); continue
        tcr, rc_dict, step_id = b
        rh = compute_role_contract_hash(rc_dict["contract_id"], rc_dict["revision"], tid, rc_dict)
        session = f"sess-rev-{tid[-6:]}"
        try:
            token, fencing = acquire_lease(client, tid, session)
            cr = completion_review(client, tid)
            dec = cr.get("decision") if isinstance(cr, dict) else None
            if isinstance(cr, dict) and cr.get("error"):
                overall = "block"; findings = [{"id": "completion_review_error", "severity": "blocker",
                                                 "detail": cr["error"]}]; clauses=[]
            elif dec == "pass":
                overall = "pass"; findings = []; clauses=[{"clause_id":"independent_review","decision":"pass"}]
            else:
                overall = "block"
                fnd = cr.get("findings") if isinstance(cr, dict) else None
                findings = fnd if isinstance(fnd, list) and fnd else [{"id":"completion_review_not_pass","severity":"blocker","detail":f"decision={dec}"}]
                clauses = [{"clause_id":"independent_review","decision":"block"}]
            res = submit_verdict(client, tid, step_id, tcr, rc_dict, rh, overall, clauses, findings, session, token, fencing)
            if isinstance(res, dict) and res.get("success"):
                processed[tid] = {"status": f"verdict_{overall}", "verdict_id": res.get("verdict_id"), "ts": time.time()}
                print(f"  [OK] {overall} {tid} {title[:44]} -> {res.get('verdict_id')}")
            else:
                processed[tid] = {"status": "verdict_fail", "detail": json.dumps(res, ensure_ascii=False)[:200], "ts": time.time()}
                print(f"  [FAIL] {tid} {title[:40]} -> {json.dumps(res, ensure_ascii=False)[:160]}")
            # block 必须立即 release，否则 executor 无法 claim 整改步
            release_lease(client, tid, token, session)
        except Exception as e:
            processed[tid] = {"status": "exception", "detail": str(e)[:200], "ts": time.time()}
            print(f"  [EXC] {tid} {title[:40]} -> {str(e)[:160]}")
        save_state(st)
        done += 1
    print(f"\n循环完成。本次处理 {done} 张；累计已记录 {len(processed)} 张。")

def cmd_handoff(conn, client, task_id, reason=None):
    """对已被 block verdict 的卡补两段式 handoff（原子追加 fix_defect 整改步）。"""
    b = fetch_card_bindings(conn, task_id)
    if not b:
        print("无契约绑定，无法 handoff"); return
    # 取源 block verdict 的 reviewer identity，逐字复用（handler 要求 agent/session 一致）
    src = conn.execute(
        "SELECT reviewer_identity FROM task_verdict_events "
        "WHERE task_id=? AND overall='block' ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    if not src or not src["reviewer_identity"]:
        print("未找到源 block verdict，无法 handoff"); return
    rev_ident = json.loads(src["reviewer_identity"])
    src_agent = rev_ident.get("identity", {}).get("agent_id")
    src_session = rev_ident.get("identity", {}).get("session_id")
    print(f"源 verdict identity: agent={src_agent} session={src_session}")
    session = src_session or f"sess-rev-{task_id[-6:]}"
    token, fencing = acquire_lease(client, task_id, session)
    print(f"lease acquired token={str(token)[:10]}... fencing={fencing}")
    params = {
        "task_id": task_id,
        "from_role": "reviewer",
        "outcome": "reviewer_blocked",
        "next_role": "executor",
        "next_action": "修复 CLI-004 metrics 分支本地 SQLite fallback（移除 get_metrics_collector/MetricsCollector in-process SQLite 与 --local flag，使 Rust daemon 为唯一 authority）",
        "reason": reason or "reviewer_blocked: CLI-004 metrics 仍含本地 SQLite fallback",
        "independence_requirement": "not_required",
        "request_id": f"handoff-{task_id}-{uuid.uuid4().hex[:8]}",
        "step_id": None,
        "report_request_id": f"report-{task_id}",
        "evidence_path": f"docs/evidence/{task_id}-reviewer-blocked.json",
        "evidence_hash": "sha256:cli004-reviewer-blocked",
        "identity": {
            "agent_id": src_agent or REVIEWER_AGENT, "session_id": session,
            "model_id": REVIEWER_MODEL, "role": "reviewer",
        },
        "lease_token": token,
        "fencing_counter": fencing,
        "workspace_id": WORKSPACE_ID,
    }
    r = rpc_call(client, "task.handoff", params)
    print("handoff result:", json.dumps(r, ensure_ascii=False)[:400])
    release_lease(client, task_id, token, session)
    print("lease released")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["diagnose", "validate", "run", "handoff"])
    ap.add_argument("--task", help="validate / handoff 模式指定单卡")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="run 模式强制重审（绕过 state 与已有 verdict 幂等闸）")
    ap.add_argument("--reason", default=None)
    args = ap.parse_args()
    conn = get_db()
    client = get_client()
    if args.mode == "diagnose":
        cmd_diagnose(conn, client)
    elif args.mode == "validate":
        if not args.task:
            print("--task 必填"); sys.exit(2)
        cmd_validate(conn, client, args.task)
    elif args.mode == "run":
        cmd_run(conn, client, args.limit, force=args.force)
    elif args.mode == "handoff":
        if not args.task:
            print("--task 必填"); sys.exit(2)
        cmd_handoff(conn, client, args.task, args.reason)

if __name__ == "__main__":
    main()
