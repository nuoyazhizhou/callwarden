#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Report P0-F fix_defect step completion via daemon RPC, then handoff to reviewer.
Usage: PYTHONPATH=C:/git_work python scripts/_report_p0f.py
"""
import sys, os, json, uuid
sys.path.insert(0, r"C:\git_work\callwarden")

TID = "T-1787310376068-44eb5f20"
RSTEP = "S-cd7bd679466ab0b031a1f066"

IDENTITY = {
    "agent_id": "executor-workbuddy-186loop",
    "agent_instance_id": "inst-executor-wb-186loop",
    "session_id": "sess-executor-wb-186loop",
    "model_id": "deepseek-v4-flash",
    "role": "executor",
}

SUMMARY = (
    "P0F fix_defect 整改完成并通过真实 daemon 正向取证。"
    "R1: bootstrap_reviewer_pass 增加 empty-projection 门禁(no_governance_projection)。"
    "R2: bootstrap_executor_evidence 校验步骤必须 done、输入必须覆盖全部步骤。"
    "R3: reviewer lease 不再硬编码 workspace_id/model_id/fencing_counter，改由 bound_workspace、reviewer model、历史 max+1 决定。"
    "R4: executor agent_instance_id 于阶段一 event reason 持久化，阶段二做强制的 agent/instance/session 三重独立性校验(去掉空串短路)。"
    "R6: 可复现部署已达成，health.git_commit==HEAD==f44a872c。"
    "R5: 真实 daemon 两阶段 bridge 取证通过，含同 request-id 幂等重放与冲突(E_REQUEST_ID_REUSE_MISMATCH)。"
    "证据见 docs/evidence/T-1787310376068-44eb5f20-p0f-positive-rerun-*.json。"
)


def main():
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    client = UnixDaemonRpcClient()
    params = {
        "task_id": TID,
        "step_id": RSTEP,
        "success": True,
        "summary": SUMMARY,
        "evidence_path": "docs/evidence/T-1787310376068-44eb5f20-p0f-positive-rerun-20260831-152506.json",
        "evidence_hash": "r5-positive-bridge-replay-verified",
        "agent_session_id": IDENTITY["session_id"],
        "identity": IDENTITY,
        "request_id": "req-report-p0f-fix-" + uuid.uuid4().hex[:10],
    }
    try:
        r = client.call("task.report", params)
        print("REPORT OK:", json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001
        print("REPORT ERR:", repr(e))
        sys.exit(1)


if __name__ == "__main__":
    main()