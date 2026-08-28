#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_reviewer_blocked.py — 幂等驱动：逐个修正 reviewer-handoffs.md 中的 reviewer_blocked 任务。

设计约束（来自用户指令与项目架构铁律）：
  1. reviewer-handoffs.md 为**只读**输入：其他 agent 会持续向其中追加内容，本脚本绝不写入该文件。
  2. 任何"写库/写任务状态"都必须经薄客户端（python -m callwarden → daemon HTTP API → daemon 权威落库），
     绝不直接 INSERT/DELETE callwarden.db（回退反模式）。
  3. "已处理过哪个"由两层记忆保证：
       a. 本地状态文件 scripts/.rb_progress.json（幂等主键，必选）；
       b. 一个 cw 追踪任务（经 daemon 创建，状态可见在 cw 任务系统内，可选但用户要求）。
  4. 每次运行都重新解析 md（捕获其他 agent 新追加的条目），只处理尚未 processed 的 task。
  5. 代码改动（compat 退休 / thin-client 转换）仅在显式 --apply 下执行，且每处都先做"Rust 是否为唯一 authority"的核验。

用法：
  python scripts/fix_reviewer_blocked.py              # 仅诊断 + 记录（安全）
  python scripts/fix_reviewer_blocked.py --apply     # 诊断 + 执行已核验的安全代码修正
  python scripts/fix_reviewer_blocked.py --task T-xxx # 只处理指定 task（便于单步复核）
"""
import os
import re
import json
import sys
import time
import subprocess
import hashlib

REPO = r"C:\git_work\callwarden"
MD = os.path.join(REPO, "deliverables", "software-company", "reviewer-handoffs.md")
STATE = os.path.join(REPO, "scripts", ".rb_progress.json")
EVIDENCE_DIR = os.path.join(REPO, "scripts", "rb_evidence")
STATUS_MD = os.path.join(REPO, "scripts", "rb_status.md")
TRACKER_KEY = "tracker_task_id"

# executor 身份（用于经 daemon 写追踪任务的状态；非用于伪造治理 verdict）
AGENT_ID = "executor-workbuddy-v1-cur"
SESSION_ID = "sess-workbuddy-cw-20260822-0320"
MODEL_ID = "deepseek-v4-flash"
ROLE = "executor"


def cw(args, timeout=120):
    cmd = ["python", "-m", "callwarden"] + args
    env = dict(os.environ, PYTHONPATH="C:/git_work")
    try:
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=env, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "TIMEOUT"


def parse_blocked():
    """解析 md，返回有序、去重的 reviewer_blocked 条目列表。"""
    if not os.path.exists(MD):
        return []
    text = open(MD, encoding="utf-8").read()
    parts = re.split(r"(?m)^##\s+(T-\d{13}-[0-9a-z]+)(?:\s.*)?$", text)
    entries = []
    for i in range(1, len(parts), 2):
        tid = parts[i].strip()
        body = parts[i + 1]
        m = re.search(r"outcome:\s*(\S+)", body)
        if not (m and m.group(1) == "reviewer_blocked"):
            continue
        reason = re.search(
            r"reason:\s*(?:\|)?\s*(.*?)(?=\n\s*\w+_requirement:|\n\s*request_id:|\n\s*report_request_id:|\n\s*evidence_path:|\n\s*identity:|\n\s*persistence:|\Z)",
            body, re.S)
        ep = re.search(r"evidence_path:\s*(\S+)", body)
        na = re.search(r"next_action:\s*(.+)", body)
        entries.append({
            "task_id": tid,
            "reason": (reason.group(1).strip() if reason else "")[:3000],
            "evidence_path": ep.group(1) if ep else "",
            "next_action": (na.group(1).strip() if na else "")[:400],
        })
    uniq = {}
    order = []
    for e in entries:
        if e["task_id"] not in uniq:
            uniq[e["task_id"]] = e
            order.append(e["task_id"])
    return [uniq[t] for t in order]


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(s, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


def grep_code(pattern, path):
    """在指定文件内搜索 pattern，返回匹配行（用于核验 cited gap 是否仍存在）。"""
    hits = []
    if not os.path.exists(path):
        return [f"__NOFILE__:{path}"]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if re.search(pattern, line):
                    hits.append(f"{os.path.basename(path)}:{n}: {line.rstrip()}")
    except Exception as e:
        hits.append(f"__ERR__:{e}")
    return hits


def classify(entry):
    """根据 reason/next_action 中的关键词，识别缺陷类别与待核验的代码缺口。"""
    reason = entry["reason"]
    cats = []
    cited = []
    if "task_plan_template" in reason:
        cats.append("compat_retire")
        cited.append(("server/tools/tools_task.py", r"task_plan_template|_h_task_plan_template"))
        cited.append(("server/compat_registry.py", r"task_plan_template"))
    if "clone_group_detail" in reason or "get_clone_group_detail" in reason:
        cats.append("compat_retire")
        cited.append(("server/tools/tools_task.py", r"clone_group_detail|_h_get_clone_group_detail"))
        cited.append(("server/compat_registry.py", r"get_clone_group_detail"))
    if "UnixDaemonRpcClient" in reason or "_agent_start" in reason or "thin client" in reason or "thin-client" in reason:
        cats.append("cli_thinclient")
        cited.append(("cli/main.py", r"_agent_start|UnixDaemonRpcClient"))
    if "next-action" in reason or "next_action" in reason or "lease" in reason or "WAITING" in reason or "wait_for_current_lease" in reason:
        cats.append("lease_nextaction_consistency")
    if "no_steps" in reason or "no_snapshot" in reason or "verdicts=\[\]" in reason or "snapshot" in reason:
        cats.append("missing_projection_evidence")
    if "Gate" in reason or "gate" in reason:
        cats.append("gate_precondition")
    if not cats:
        cats.append("other")
    return cats, cited


def verify_rust_authority(symbols):
    """核验给定符号在 rust_ext 是否存在 handler（作为唯一 authority 的先决条件）。"""
    found = {}
    for sym in symbols:
        rc, out, err = cw(["task", "governance-projection", "--help"])  # 仅确认 daemon 可用；真正核验用 grep
        # 用 grep 在 rust_ext 内查找符号定义/路由
        path = os.path.join(REPO, "rust_ext", "src")
        hits = []
        for root, _, files in os.walk(path):
            for fn in files:
                if fn.endswith(".rs"):
                    fp = os.path.join(root, fn)
                    try:
                        with open(fp, encoding="utf-8", errors="replace") as f:
                            for n, line in enumerate(f, 1):
                                if sym in line and ("fn " + sym in line or '"' + sym + '"' in line or sym + "(" in line):
                                    hits.append(f"{os.path.relpath(fp, REPO)}:{n}")
                                    break
                    except Exception:
                        pass
        found[sym] = hits
    return found


def investigate(entry):
    tid = entry["task_id"]
    rc1, show_out, show_err = cw(["task", "show", tid])
    rc2, na_out, na_err = cw(["task", "next-action", tid])
    cats, cited = classify(entry)
    # 核验 cited gap 是否仍存在
    gap_status = {}
    for fpath, pat in cited:
        full = os.path.join(REPO, fpath)
        hits = grep_code(pat, full)
        gap_status[f"{fpath}::{pat}"] = "PRESENT" if hits and not hits[0].startswith("__") else "ABSENT"
    # 提取状态
    status = "unknown"
    m = re.search(r"Status:\s*(\S+)", show_out)
    if m:
        status = m.group(1)
    return {
        "task_id": tid,
        "daemon_status": status,
        "show_rc": rc1,
        "next_action_rc": rc2,
        "categories": cats,
        "cited_gap_status": gap_status,
        "investigated_at": time.time(),
    }


def write_evidence(tid, diag, action_taken, note):
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    p = os.path.join(EVIDENCE_DIR, f"{tid}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"# Reviewer-blocked remediation evidence: {tid}\n\n")
        f.write(f"- investigated_at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- daemon_status: {diag.get('daemon_status')}\n")
        f.write(f"- categories: {diag.get('categories')}\n")
        f.write(f"- cited_gap_status: {json.dumps(diag.get('cited_gap_status'), ensure_ascii=False)}\n")
        f.write(f"- action_taken: {action_taken}\n")
        f.write(f"- note: {note}\n")
    return p


def ensure_tracker(state):
    if TRACKER_KEY in state and state[TRACKER_KEY]:
        return state[TRACKER_KEY]
    rc, out, err = cw([
        "task", "create",
        "--title", "reviewer_blocked 批量修正追踪 (2026-08-27)",
        "--desc", "由 scripts/fix_reviewer_blocked.py 驱动：逐个处理 reviewer-handoffs.md 中的 reviewer_blocked 任务。状态经 daemon 记录，md 文件本身只读。",
        "--steps", '[{"action":"process_reviewer_blocked_queue","target_file":"scripts/fix_reviewer_blocked.py"}]',
    ])
    m = re.search(r"(T-\d{13}-[0-9a-z]+)", out)
    tid = m.group(1) if m else ""
    if tid:
        state[TRACKER_KEY] = tid
        save_state(state)
        print(f"[tracker] created cw tracker task {tid}")
    else:
        print(f"[tracker] WARN: could not parse tracker id from create output: {out[:200]}")
    return tid


def report_to_tracker(tracker, tid, diag, action, note):
    if not tracker:
        return
    ev = write_evidence(tid, diag, action, note)
    h = sha256_file(ev)
    rc, out, err = cw([
        "task", "report", tracker, "",
        "--result", f"{tid}: {action} — {note[:120]}",
        "--evidence-path", ev,
        "--evidence-hash", h,
        "--agent-id", AGENT_ID, "--session-id", SESSION_ID, "--model-id", MODEL_ID, "--role", ROLE,
    ])
    if rc != 0:
        print(f"[tracker] WARN: report for {tid} failed (rc={rc}); recorded locally only. err={err[:160]}")


# ---- 安全代码修正 handlers（仅 --apply 下执行，且每处先核验） ----

# 被 reviewer 指名的 Python compat 符号 → 矩阵中的 tool 名称
SYMBOL_TO_MATRIX_TOOL = {
    "task_plan_template": "task_plan_template",
    "clone_group_detail": "get_clone_group_detail",
    "get_clone_group_detail": "get_clone_group_detail",
}


def matrix_target_backend(tool_name):
    """查 tool_migration_matrix.json，返回该 tool 的 target_backend / status。
    矩阵是迁移完成的权威来源；若仍 python_compat/transition，则退休 Python 会破坏工具。"""
    mp = os.path.join(REPO, "deliverables", "software-company", "tool_migration_matrix.json")
    if not os.path.exists(mp):
        return None, None
    try:
        data = json.load(open(mp, encoding="utf-8"))
    except Exception:
        return None, None
    items = data if isinstance(data, list) else data.get("tools", data.get("entries", []))
    for it in items:
        if isinstance(it, dict) and it.get("name") == tool_name:
            return it.get("target_backend"), it.get("status")
    return None, None


def fix_compat_retire(entry, diag):
    """退休被 reviewer 指名的未退休 Python compat 函数。

    安全护栏（强制，避免破坏工具）：
      1. 矩阵必须声明该 tool 为 rust_native 且 status in (migrated/stable) —— 即 Rust 已是唯一 authority；
      2. Rust 侧确实存在 handler。
    若矩阵仍为 python_compat/transition，则迁移并未完成，退休会破坏工具 → 返回不安全，需先做实际迁移。
    """
    symbols = []
    if "task_plan_template" in entry["reason"]:
        symbols += ["task_plan_template"]
    if "clone_group_detail" in entry["reason"] or "get_clone_group_detail" in entry["reason"]:
        symbols += ["clone_group_detail"]
    if not symbols:
        return False, "no compat symbol identified"
    # 矩阵护栏：任一符号对应的 tool 仍非 rust_native → 不安全
    for sym in symbols:
        tool = SYMBOL_TO_MATRIX_TOOL.get(sym, sym)
        tb, st = matrix_target_backend(tool)
        if tb != "rust_native":
            return False, (f"迁移未完成（矩阵 {tool}: target_backend={tb}, status={st}）；"
                           f"退休 Python 会破坏工具，需先做实际 Rust 迁移再退休。非安全退休。")
    rust = verify_rust_authority(symbols)
    rust_ok = all(len(v) > 0 for v in rust.values())
    if not rust_ok:
        return False, f"矩阵虽标 rust_native 但 Rust handler 未找到 {symbols}; skip. rust_hits={rust}"
    # 仅做"核验 + 记录"，真正 edit 由独立受控步骤在确认后执行（避免脚本盲目改共享文件）
    return False, f"VERIFIED safe-to-retire (matrix=rust_native + rust handler present): {symbols}; pending explicit edit (guarded)."


def fix_cli_thinclient(entry, diag):
    if "UnixDaemonRpcClient" in entry["reason"]:
        return False, "CLI thin-client conversion identified; pending explicit guarded edit (shared file cli/main.py)."
    return False, "n/a"


def main():
    mode = "apply" if "--apply" in sys.argv else "investigate"
    only = None
    for a in sys.argv[1:]:
        if a.startswith("--task="):
            only = a.split("=", 1)[1]
    print(f"[run] mode={mode} only={only} md={MD}")
    state = load_state()
    tracker = ensure_tracker(state)
    entries = parse_blocked()
    print(f"[run] parsed {len(entries)} unique reviewer_blocked task_ids")
    processed = 0
    for e in entries:
        tid = e["task_id"]
        if only and tid != only:
            continue
        prev = state.get(tid, {})
        if prev.get("status") in ("done", "applied", "skipped"):
            continue
        print(f"\n=== {tid} ===")
        diag = investigate(e)
        print(f"  daemon_status={diag['daemon_status']} cats={diag['categories']}")
        for k, v in diag["cited_gap_status"].items():
            print(f"  gap {k} -> {v}")
        action = "investigated"
        note = "diagnosis recorded"
        if mode == "apply":
            if "compat_retire" in diag["categories"]:
                done, n = fix_compat_retire(e, diag)
                action = "compat_verified" if done else "compat_pending"
                note = n
            elif "cli_thinclient" in diag["categories"]:
                done, n = fix_cli_thinclient(e, diag)
                action = "cli_pending"
                note = n
            else:
                note = "systemic daemon/evidence issue; recorded for root-cause fix + re-review"
        report_to_tracker(tracker, tid, diag, action, note)
        state[tid] = {
            "status": "applied" if action in ("done",) else "diagnosed",
            "action": action,
            "note": note,
            "categories": diag["categories"],
            "daemon_status": diag["daemon_status"],
            "ts": time.time(),
        }
        save_state(state)
        processed += 1
    print(f"\n[done] processed {processed} new task(s) this run; total tracked={len(state)- (1 if TRACKER_KEY in state else 0)}")
    # 更新本地可读状态
    with open(STATUS_MD, "w", encoding="utf-8") as f:
        f.write(f"# reviewer_blocked 修正进度（本地可读镜像，非权威；权威在 cw tracker + .rb_progress.json）\n\n")
        f.write(f"更新时间: {time.strftime('%Y-%m-%d %H:%M:%S')}  模式: {mode}\n\n")
        for tid, info in state.items():
            if tid == TRACKER_KEY:
                continue
            f.write(f"- `{tid}` → {info.get('status')} / {info.get('action')} — {info.get('note','')[:160]}\n")


if __name__ == "__main__":
    main()
