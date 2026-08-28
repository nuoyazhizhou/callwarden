#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Executor 整改循环驱动器：对父任务下 116 张"实现已完成、仅缺证据"的卡批量补证据。

每张卡调用 executor_remediate_card.py（独立子进程 = 独立 lease + 幂等），
完成后卡回 review，由 reviewer 凭权威证据重审。

用法：
    PYTHONPATH=C:/git_work python scripts/executor_remediate_loop.py [--limit N]
"""
import argparse
import os
import subprocess
import sys

AUTH_DB = r"C:\Users\wanpi\.callwarden\callwarden.db"
REPO = r"C:\git_work\callwarden"
PARENT = "T-1787293451688-c14b1e44"
CARD_SCRIPT = os.path.join(REPO, "scripts", "executor_remediate_card.py")


def get_cards(limit=None):
    import sqlite3
    conn = sqlite3.connect(AUTH_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM tasks WHERE parent_id=? AND status='in_progress' ORDER BY sort_order, id",
        (PARENT,),
    )
    cards = [r["id"] for r in cur.fetchall()]
    out = []
    for cid in cards:
        cur.execute(
            "SELECT id, step_index, action, status FROM task_steps WHERE task_id=? ORDER BY step_index",
            (cid,),
        )
        steps = cur.fetchall()
        impl = [s for s in steps if s["action"] != "fix_defect"]
        fd_pending = [s for s in steps if s["action"] == "fix_defect" and s["status"] == "pending"]
        if impl and all(s["status"] == "done" for s in impl) and fd_pending:
            out.append((cid, fd_pending[0]["id"]))
    if limit:
        out = out[:limit]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    cards = get_cards(args.limit)
    print(f"待整改证据卡数: {len(cards)}", flush=True)
    ok, fail = [], []
    for i, (cid, rsid) in enumerate(cards, 1):
        print(f"[{i}/{len(cards)}] {cid} rs={rsid}", flush=True)
        try:
            r = subprocess.run(
                [sys.executable, CARD_SCRIPT, cid, rsid],
                capture_output=True, text=True, timeout=120, cwd=REPO,
            )
            if r.returncode == 0 and "task.report OK" in r.stdout:
                ok.append(cid)
                print("  OK → review", flush=True)
            else:
                fail.append((cid, r.stdout[-400:], r.stderr[-200:]))
                print(f"  FAIL rc={r.returncode}: {r.stdout[-250:]}{r.stderr[-150:]}", flush=True)
        except Exception as e:  # noqa: BLE001
            fail.append((cid, "", str(e)))
            print(f"  EXC: {e}", flush=True)
    print(f"\n=== 循环完成: ok={len(ok)} fail={len(fail)} ===", flush=True)
    for cid, so, se in fail:
        print(f"FAIL {cid}: {so} {se}", flush=True)


if __name__ == "__main__":
    main()
