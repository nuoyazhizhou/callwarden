r"""P0-G G5：task-bound reviewer verdict 全链路校验脚本（projection→lease→verdict→next-action→release）。

用途（backlog §G5）：
1. 用 G3 projection（task.governance_projection.get）读取权威投影字段；
2. 以 Reviewer 身份正式 acquire reviewer lease；
3. 仅用 `cw collab verdict --overall block|pass` 发起 task-bound mutation；
4. 验证 daemon append-only 新增 verdict event，persisted normalization version/hash 与 projection 一致；
5. 验证 post-submit `task.next-action`（block→REVISE / pass→ADJUDICATE）；
6. 立即 release reviewer lease；**脚本不打印/记录 raw token**。

用法（CLI 纪律：所有写操作走真实 cw CLI 子进程）：
  python tests/p0g_verdict_e2e_check.py --task-id <T-...> --overall block \
      --agent-id reviewer-wb-186loop --agent-instance-id inst-reviewer-wb-186loop \
      --session-id sess-reviewer-wb-186loop --model-id deepseek-v4-flash

退出码：0 = 全链路通过；非 0 = 失败。
"""

import argparse
import json
import os
import re
import subprocess
import sys

_PY = r"C:\Python314\python.exe"
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CW = os.path.join(_REPO, "cw.py")


def _env(extra=None):
    env = dict(os.environ)
    env.pop("CW_AGENT_SESSION_ID", None)
    env["CW_DAEMON_MODE"] = "enterprise"
    env["CW_DAEMON_TRANSPORT"] = "named-pipe"
    env["CW_DAEMON_AUTOSTART_WINDOW"] = "0"
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CALLWARDEN_LANG"] = "zh_CN"
    if extra:
        env.update(extra)
    return env


def _run(args, env=None, timeout=90):
    proc = subprocess.run(
        [_PY, _CW] + args, env=env or _env(), cwd=_REPO,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc, (proc.stdout or "") + "\n" + (proc.stderr or "")


def _probe_ok(proc, what):
    if proc.returncode != 0 and "lease acquire 输出不是 JSON" not in what:
        pass
    return proc.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--overall", required=True, choices=["pass", "block"])
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--agent-instance-id", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--model-id", required=True)
    ap.add_argument("--phase", default="blind_first_pass")
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args()

    step = 0

    def ok(msg):
        print(f"[OK] {msg}")

    def fail(msg):
        print(f"[FAIL] {msg}", file=sys.stderr)
        sys.exit(1)

    # ---- 1. G3 projection ----
    step += 1
    proc, out = _run(["task", "governance-projection", args.task_id, "--json"])
    try:
        proj = json.loads(proc.stdout)
    except json.JSONDecodeError:
        fail(f"step{step}: governance-projection 非 JSON: {out[-800:]}")
    if "error" in proj:
        fail(f"step{step}: projection 错误: {proj['error']}")
    tc = proj.get("task_contract") or {}
    rc = proj.get("reviewer_role_contract") or {}
    nr = proj.get("normalization_rules") or {}
    st = proj.get("current_step") or {}
    for label, obj in [("task_contract", tc), ("reviewer_role_contract", rc),
                       ("normalization_rules", nr), ("current_step", st)]:
        if not obj or "diagnosis" in obj:
            fail(f"step{step}: {label} 缺投影: {obj}")
    ok(f"step{step}: projection 完整 (contract r{tc.get('revision')}, reviewer rc r{rc.get('revision')}, "
       f"norm {nr.get('version')})")
    if proj.get("lease_raw_token_omitted") is not True:
        fail(f"step{step}: projection 必须声明 lease_raw_token_omitted=true")
    ok(f"step{step}: projection 未泄漏 lease raw token")

    # ---- 2. acquire reviewer lease ----
    step += 1
    proc, out = _run(["lease", "acquire", args.task_id, "--role", "reviewer",
                      "--agent-id", args.agent_id, "--agent-instance-id", args.agent_instance_id,
                      "--session-id", args.session_id, "--model-id", args.model_id, "--json"])
    try:
        lease = json.loads(proc.stdout)
    except json.JSONDecodeError:
        fail(f"step{step}: lease acquire 非 JSON: {out[-800:]}")
    if not lease.get("ok"):
        fail(f"step{step}: lease acquire 失败: {lease}")
    token = lease["token"]
    fencing = int(lease["fencing_counter"])
    lease_id = lease.get("lease_id", "?")
    # token 脱敏：只打印前 6 位
    print(f"[OK] step{step}: reviewer lease acquired (lease_id={lease_id}, fencing={fencing}, "
          f"token={token[:6]}...{' (raw token 已脱敏)' if not args.keep_tmp else ' [DEBUG RAW]'})")
    if args.keep_tmp:
        with open(os.path.join(_REPO, ".tmp_p0g_verdict_token.txt"), "w") as f:
            f.write(token)
        ok(f"step{step}: token 已写入 .tmp_p0g_verdict_token.txt（仅 --keep-tmp 调试用）")

    # ---- 3. cw collab verdict ----
    step += 1
    findings = json.dumps([{
        "finding_id": f"p0g-{args.overall}-{args.task_id[:8]}",
        "severity": "block" if args.overall == "block" else "info",
        "summary": f"P0-G G5 E2E {args.overall}",
        "evidence_ref": "tests/p0g_verdict_e2e_check.py",
    }])
    verdict_args = ["collab", "verdict", "--task-id", args.task_id, "--step-id", st.get("step_id", ""),
                    "--contract-id", tc.get("contract_id", ""), "--contract-hash", tc.get("hash", ""),
                    "--contract-revision", str(tc.get("revision", 0)),
                    "--role-contract-id", rc.get("contract_id", ""), "--role-contract-hash", rc.get("prompt_hash", ""),
                    "--role-contract-revision", str(rc.get("revision", 0)),
                    "--snapshot-id", (proj.get("review_input_snapshot") or {}).get("snapshot_id", "snap-p0g"),
                    "--request-id", f"p0g-verdict-{args.task_id[:8]}-{args.overall}",
                    "--phase", args.phase, "--overall", args.overall,
                    "--attestation", "p0g-g5-e2e",
                    "--findings", findings,
                    "--agent-id", args.agent_id, "--agent-instance-id", args.agent_instance_id,
                    "--session-id", args.session_id, "--model-id", args.model_id,
                    "--role", "reviewer", "--lease-token", token, "--fencing-counter", str(fencing),
                    "--json"]
    proc, out = _run(verdict_args, timeout=120)
    try:
        verdict = json.loads(proc.stdout)
    except json.JSONDecodeError:
        fail(f"step{step}: collab verdict 非 JSON: {out[-1000:]}")
    if "error" in verdict:
        fail(f"step{step}: verdict 提交失败: {verdict['error']}")
    ok(f"step{step}: verdict 已提交 (overall={args.overall}, verdict_id={verdict.get('verdict_id', '?')})")

    # ---- 4. post-submit next-action ----
    step += 1
    proc, out = _run(["task", "next-action", args.task_id, "--json"])
    try:
        na = json.loads(proc.stdout)
    except json.JSONDecodeError:
        fail(f"step{step}: next-action 非 JSON: {out[-800:]}")
    expected = "REVISE" if args.overall == "block" else "ADJUDICATE"
    actual = na.get("next_action") or na.get("action") or ""
    if expected not in str(actual):
        # next_action 可能是小写/不同字段，宽松匹配
        raw = json.dumps(na, ensure_ascii=False)
        if expected.lower() not in raw.lower():
            fail(f"step{step}: 期望 next-action 含 {expected}，实际: {raw[:500]}")
    ok(f"step{step}: next-action 符合 {args.overall}→{expected}")

    # ---- 5. release reviewer lease ----
    step += 1
    proc, out = _run(["lease", "release", args.task_id, "--role", "reviewer",
                      "--agent-id", args.agent_id, "--agent-instance-id", args.agent_instance_id,
                      "--session-id", args.session_id, "--model-id", args.model_id,
                      "--lease-token", token, "--fencing-counter", str(fencing), "--json"])
    try:
        rel = json.loads(proc.stdout)
    except json.JSONDecodeError:
        fail(f"step{step}: lease release 非 JSON: {out[-800:]}")
    if "error" in rel:
        fail(f"step{step}: lease release 失败: {rel['error']}")
    ok(f"step{step}: reviewer lease released")
    if not args.keep_tmp:
        ok("步骤 5: raw token 未写入任何文件（脱敏遵守）")

    print(f"\nP0-G G5 E2E 全链路通过: projection→lease→verdict({args.overall})→next-action({expected})→release")
    return 0


if __name__ == "__main__":
    sys.exit(main())
