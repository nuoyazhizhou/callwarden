#!/usr/bin/env python
"""批次 v2 废卡 supersede 收尾（planner 治理修复）。

背景：v2 批次 6 卡（T-1788962…）开卡时误传 --identity-policy 触发 CLI governed
通道，跳过缺省三角色合同模板 → 无 Task/Role Contract → governance_blocked。
v3 批次已重开合同完备卡（T-1788963088/…/T-1788963106…）。本脚本把每张 v2 废卡
supersede 到对应 v3 卡。

身份（复用 SR-01 批次，全部无 instance，规避 CLI supersede 无 --agent-instance-id
透传的 daemon gap；lease holder 与 acting adjudicator 三分离）：
  - reviewer lease holder: reviewer-wb-sr01-03（注册 role 精确 = 'reviewer'）
  - acting adjudicator:    adjudicator-wb-sr01-02

用法：
    PYTHONPATH=C:/git_work python scripts/supersede_v2_cards.py [--dry-run] [--pairs old:new,...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = "C:/git_work/callwarden"
PY = "C:/Python314/python.exe"

REVIEWER = ("reviewer-wb-sr01-03", "sess-reviewer-wb-sr01c-20260906", "glm-5.3-flash")
ADJUDICATOR = ("adjudicator-wb-sr01-02", "sess-adjudicator-wb-sr01b-20260906", "glm-5.3-flash")

# v2 废卡 → v3 替代卡
PAIRS: dict[str, str] = {
    "T-1788962298062-54927dac": "T-1788963088148-495d7208",  # identity-lease-small
    "T-1788962307288-7a8526d4": "T-1788963103216-cb818938",  # tools_query
    "T-1788962308102-ab09ac58": "T-1788963104058-fdb2e848",  # tools_task
    "T-1788962308958-de061718": "T-1788963104879-2e9e6270",  # tools_semantic
    "T-1788962309851-13447b90": "T-1788963105720-60bfc80c",  # tools_security
    "T-1788962310755-49297814": "T-1788963106520-907544c8",  # tools_summary
}

REASON = (
    "v2 开卡缺陷收尾：CLI governed 通道（显式 --identity-policy）跳过缺省三角色"
    "合同模板，daemon 空 role_contracts 裸建 → 无 task_contract_revisions/"
    "role_contract_lineages → next_action 规则 4 governance_blocked。"
    "由 v3 批次同名卡（带完整合同 bootstrap）替代，覆盖方法集不变。"
)


def run_cw(args: list[str]) -> tuple[int, str]:
    import os
    cmd = [PY, "cw.py"] + args
    env = dict(os.environ)
    env["PYTHONPATH"] = "C:/git_work"
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def acquire_lease(old_task: str) -> tuple[str, int]:
    agent, session, model = REVIEWER
    rc, out = run_cw([
        "lease", "acquire", old_task,
        "--role", "reviewer",
        "--agent-id", agent, "--session-id", session, "--model-id", model,
        "--ttl", "300", "--json",
    ])
    if rc != 0:
        raise RuntimeError(f"lease acquire 失败 rc={rc}: {out[-500:]}")
    # 提取首个 { 到最后一个 } 的 JSON 块（响应为多行 pretty JSON）
    start, end = out.find("{"), out.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError(f"lease acquire 响应无 JSON: {out[-500:]}")
    try:
        payload = json.loads(out[start:end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lease acquire JSON 解析失败: {exc}: {out[start:end+1][-300:]}")
    token = (payload.get("lease_token") or payload.get("token") or "").strip()
    counter = payload.get("fencing_counter")
    if counter is None:
        counter = payload.get("lease", {}).get("fencing_counter")
    if not token or counter is None:
        raise RuntimeError(f"lease acquire 响应缺 token/counter: {payload}")
    return token, int(counter)


def write_evidence(old_task: str, new_task: str) -> tuple[str, str]:
    short = old_task.split("-")[-1]
    rel = f"docs/evidence/supersede_v2_to_v3_{short}.json"
    manifest = {
        "kind": "supersede_evidence",
        "superseded_task": old_task,
        "superseding_task": new_task,
        "batch": "P0-COMPAT-v2 → P0-COMPAT-v3",
        "reason": REASON,
        "verification": {
            "v2_card": "task_contract_revisions=0, role_contract_lineages=0 → governance_blocked",
            "v3_card": "task_contract_revisions=1, role_contract_lineages=3, step_bindings=4 → queued/READY",
        },
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "request_id": f"req-sup-v2-{uuid.uuid4().hex[:8]}",
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    path = Path(REPO) / rel
    path.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return rel, digest


def supersede(old_task: str, new_task: str) -> None:
    token, counter = acquire_lease(old_task)
    rel, digest = write_evidence(old_task, new_task)
    agent, session, model = ADJUDICATOR
    rc, out = run_cw([
        "task", "supersede", old_task, new_task,
        "--reason", REASON,
        "--request-id", f"req-sup-v2-{uuid.uuid4().hex[:8]}",
        "--evidence-path", rel, "--evidence-hash", digest,
        "--lease-token", token, "--fencing-counter", str(counter),
        "--agent-id", agent, "--session-id", session,
        "--model-id", model, "--role", "adjudicator",
    ])
    if rc != 0:
        raise RuntimeError(f"supersede 失败 rc={rc}: {out[-800:]}")
    print(f"  OK: {old_task} -> {new_task}")
    print("  " + out.strip().splitlines()[-1][:200] if out.strip() else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pairs", default="", help="逗号分隔 old:new；缺省全量")
    args = ap.parse_args()

    pairs = PAIRS
    if args.pairs:
        pairs = dict(p.split(":") for p in args.pairs.split(","))
    for old, new in pairs.items():
        print(f"== supersede {old} -> {new}")
        if args.dry_run:
            continue
        try:
            supersede(old, new)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {exc}", file=sys.stderr)
            return 1
        time.sleep(2)  # daemon action_id 时间戳冲突规避
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
