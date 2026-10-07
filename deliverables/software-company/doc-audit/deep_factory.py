#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深度前置状态工厂:在真实种子 workspace 上预建真实实体,返回 SeedContext。

预建:workspace(build_graph+publish)→ 真实 task → 注册 agent → acquire lease
(真 token+counter)→ 注册 branch → 建 rule candidate → 拿真实符号 hash。
所有 ID 来自 daemon 真实响应(不臆造),填进 SeedContext 供深度轮 provider 用。

返回:(ctx, meta) —— ctx 为 param_provider.SeedContext,meta 记录预建结果。
"""
import os
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, r"C:\git_work\callwarden")
sys.path.insert(0, r"C:\git_work\callwarden\tests\convergence")

os.environ["CW_DAEMON_TRANSPORT"] = "http"
os.environ["PYTHONUTF8"] = "1"


def build_deep_context():
    from callwarden.server.daemon_client import HttpDaemonRpcClient
    import param_provider as pp

    # 深度轮用 callwarden 仓库根 workspace:它已在 task-DB 有 authority capture
    # (task.create 要求 workspace 已被 task-DB 权威纳管;全新临时 workspace 的
    # task-DB 无行 → E_WORKSPACE_AUTHORITY_MISMATCH,task 编排类工具本就无法在其上
    # PASS)。callwarden 自身已 build_graph 有符号数据,可同时支撑符号查询 + task 编排。
    root = r"C:\git_work\callwarden"

    client = HttpDaemonRpcClient.get_instance()
    client.configure_workspace(root)
    meta = {"root": root, "steps": []}

    def safe(label, fn):
        try:
            r = fn()
            meta["steps"].append({"label": label, "ok": True, "result": str(r)[:300]})
            return r
        except Exception as e:
            meta["steps"].append({"label": label, "ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return None

    # 1. workspace register + publish(callwarden 根已 build_graph 过,跳过重建)
    reg = safe("register", lambda: client.call("workspace.register", {
        "name": f"deep-{uuid.uuid4().hex[:8]}", "client_view_root": root, "description": "deep"}))
    ws_id = reg.get("workspace_id") if isinstance(reg, dict) else None
    ws_inst = reg.get("workspace_instance_id") if isinstance(reg, dict) else None
    # callwarden 根已被生产 daemon 持续 build;深度轮不重建(全仓库 build 过慢),
    # 直接 publish 已有 DB 的 snapshot。若 workspace 无符号,后续符号查询会 EB。
    dbres = safe("get_db_path", lambda: client.call("mcp.common.get_db_path_for_daemon", {}))
    db_path = dbres.get("db_path") if isinstance(dbres, dict) else None
    snapshot_id = None
    if db_path:
        pub = safe("publish", lambda: client.call("snapshot.publish", {
            "workspace_instance_id": ws_inst, "build_context_hash": "", "db_path": db_path}))
        if isinstance(pub, dict):
            snapshot_id = pub.get("snapshot_id")

    # 2. 真实符号 hash(从 search)
    known_qnames, known_files, sym_hash = [], [], None
    found = safe("search", lambda: client.call("query.search", {
        "workspace_instance_id": ws_inst, "query": "compute", "limit": 20}))
    rows = found if isinstance(found, list) else []
    for r in rows:
        if isinstance(r, dict):
            qn = r.get("qualified_name") or r.get("name")
            if qn:
                known_qnames.append(qn)
            fp = r.get("file_path") or r.get("file_rel_path")
            if fp:
                known_files.append(fp)
            if not sym_hash and r.get("symbol_hash"):
                sym_hash = r.get("symbol_hash")
    if not known_qnames:
        known_qnames = ["compute", "add"]
    if not known_files:
        known_files = ["calc.py"]

    # 3. 真实 task(daemon 返回真实 task_id + workspace binding)
    agent_id = f"deep-agent-{uuid.uuid4().hex[:6]}"
    identity = {"agent_id": agent_id, "session_id": f"sess-{agent_id}",
                "model_id": "qa-model", "role": "implementer"}
    task = safe("task_create", lambda: client.call("task.create", {
        "title": "深度轮真实任务", "description": "deep round",
        "workspace_id": ws_id, "workspace_instance_id": ws_inst,
        "steps": [{"title": "step-a", "description": "深度轮步骤a"},
                  {"title": "step-b", "description": "深度轮步骤b"}]}))
    task_id = task.get("task_id") if isinstance(task, dict) else None
    # task.create 返回 canonical task-DB workspace_id(可能 != registry id ws_id);
    # lease/agent 必须用 task 绑定的 canonical id,否则 E_WORKSPACE_AUTHORITY_MISMATCH。
    canonical_ws_id = task.get("workspace_id") if isinstance(task, dict) else None
    canonical_ws_id = canonical_ws_id if canonical_ws_id else ws_id

    # step_id(从 task.create 返回或 task.status 查)
    step_id = None
    if isinstance(task, dict):
        steps_ret = task.get("steps") or []
        if steps_ret and isinstance(steps_ret[0], dict):
            step_id = steps_ret[0].get("step_id") or steps_ret[0].get("id")
    if not step_id and task_id:
        st = safe("task_status", lambda: client.call("task.status", {
            "task_id": task_id, "workspace_id": canonical_ws_id,
            "workspace_instance_id": ws_inst}))
        if isinstance(st, dict):
            steps = st.get("steps") or []
            if steps and isinstance(steps[0], dict):
                step_id = steps[0].get("step_id") or steps[0].get("id")

    # 4. 注册 agent(canonical workspace_id)
    if task_id:
        safe("agent_register", lambda: client.call("agent.register", {
            "agent_id": agent_id, "agent_name": agent_id,
            "workspace_id": canonical_ws_id, **identity}))

    # 5. acquire lease(真 token + counter;canonical workspace_id)
    lease_token, fencing_counter = None, None
    if task_id:
        lease = safe("lease_acquire", lambda: client.call("lease.acquire", {
            "task_id": task_id, "role": "implementer", "ttl_seconds": 3600,
            "workspace_id": canonical_ws_id, "identity": identity}))
        if isinstance(lease, dict):
            lease_token = lease.get("token")
            fencing_counter = lease.get("fencing_counter")

    # 6. 注册 branch
    branch = safe("register_branch", lambda: client.call("register_branch", {
        "workspace_instance_id": ws_inst, "name": "deep-branch",
        "workspace_id": ws_id}))
    branch_name = "deep-branch" if (isinstance(branch, dict) and not branch.get("error")) else None

    # 7. 建 rule candidate
    cand = safe("rule_candidate_create", lambda: client.call("rule_candidate_create", {
        "workspace_instance_id": ws_inst, "title": "deep-cand",
        "rule_text": "禁止裸 except", "scope": {}, "severity": "info",
        "workspace_id": ws_id}))
    candidate_id = None
    if isinstance(cand, dict):
        candidate_id = cand.get("candidate_id") or cand.get("id")

    ctx = pp.SeedContext(
        workspace_id=ws_id, workspace_instance_id=ws_inst, root=root,
        known_qualified_names=known_qnames, known_callee_names=["add", "multiply"],
        known_file_paths=known_files,
        task_id=task_id, step_id=step_id,
        lease_token=lease_token, fencing_counter=fencing_counter,
        agent_id=identity["agent_id"], session_id=identity["session_id"],
        model_id=identity["model_id"], role="implementer",
        request_id=f"req-{uuid.uuid4().hex[:12]}",
        branch_name=branch_name, candidate_id=candidate_id,
        symbol_hash=sym_hash, snapshot_id=snapshot_id,
    )
    meta["ctx_summary"] = {
        "task_id": task_id, "step_id": step_id, "lease_token": bool(lease_token),
        "fencing_counter": fencing_counter, "branch": branch_name,
        "candidate_id": candidate_id, "sym_hash": bool(sym_hash),
        "known_qnames": known_qnames[:3],
    }
    return ctx, meta, client
