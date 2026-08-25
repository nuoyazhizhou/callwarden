import json, hashlib, os
BP = r"C:\git_work\callwarden\bootstrap_prep"
REQ = ["objective","interfaces","allowed_edit_scope","acceptance_clauses","risks","rollback","dependencies"]
PROFILES = {"research","design","code_change","high_risk","review"}

for short, tid in [("MCP-002","T-1787321708760-de068a9c"),("MCP-003","T-1787321708856-e3c10624")]:
    env = json.load(open(f"{BP}\\envelope_{short}_{tid}.json", encoding="utf-8"))
    ev = json.load(open(f"{BP}\\evidence_{short}_{tid}.json", encoding="utf-8"))
    cid = env.get("contract_id"); rev = env.get("revision"); prof = env.get("profile")
    errs = []
    if not cid: errs.append("contract_id empty")
    if rev != 1: errs.append(f"revision!=1 ({rev})")
    if prof not in PROFILES: errs.append(f"profile invalid ({prof})")
    for k in REQ:
        if k not in env: errs.append(f"missing {k}")
    if env.get("identity_policy") not in ("legacy_identity_v1","role_worker_v1"):
        errs.append("identity_policy invalid")
    if cid != f"C-{tid}": errs.append(f"contract_id mismatch ({cid} vs C-{tid})")
    if ev.get("task_id") != tid: errs.append("evidence task_id mismatch")
    if ev.get("workspace_instance_id") != "ws-1": errs.append("evidence ws mismatch")
    raw = open(f"{BP}\\evidence_{short}_{tid}.json","rb").read()
    h = "sha256:"+hashlib.sha256(raw).hexdigest()
    man = json.load(open(f"{BP}\\bootstrap_manifest.json", encoding="utf-8"))
    mh = [it["evidence_hash"] for it in man["items"] if it["task_id"]==tid][0]
    if h != mh: errs.append(f"evidence_hash mismatch ({h} vs {mh})")
    print(f"{short}: {'OK' if not errs else 'ERRORS: '+str(errs)}")
