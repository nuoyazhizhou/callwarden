#!/usr/bin/env python3
"""审计 A′ 子 Epic 下 review 任务的 next_action 阻断分布。

用法：
  PYTHONPATH=C:/git_work python scripts/audit_review_blocking.py

输出：按 blocking_conditions 分类的精确分布 + 每类样例任务 ID。
"""
import sqlite3
import sys
from collections import Counter, defaultdict

from callwarden.server.daemon_client import UnixDaemonRpcClient

DB = r"C:\Users\wanpi\.callwarden\callwarden.db"
SUB_EPIC = "T-1787293451688-c14b1e44"
INSTANCE_ID = "4baea3ff12c2ea5c"  # workspace_id=1 已登记 capture


def classify(blocking):
    """把 blocking_conditions 归并为可读类别。"""
    if not blocking:
        return "（无 blocking，但非 READY）"
    text = " ".join(blocking)
    if "Task Contract" in text or "多版本冲突" in text or "revision 链" in text:
        return "Task Contract 缺失/多版本冲突/revision 链不连续"
    if "normalization" in text or "持久化" in text or "verdict" in text:
        return "verdict normalization 持久化规则"
    if "workspace" in text or "binding" in text or "authority" in text:
        return "workspace authority / binding 缺失"
    return "其他: " + text[:60]


def main():
    cl = UnixDaemonRpcClient()

    # 1. 取子 Epic 下全部 review 任务（含嵌套更深的可能，但这里 parent 直接挂子 Epic）
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id, title, parent_id FROM tasks WHERE parent_id=? AND status='review'",
        (SUB_EPIC,),
    ).fetchall()
    c.close()
    ids = [r["id"] for r in rows]
    title_of = {r["id"]: r["title"] for r in rows}
    print(f"子 Epic {SUB_EPIC} 下 review 任务总数: {len(ids)}")

    dist = Counter()
    samples = defaultdict(list)
    errors = 0
    ready = 0
    for tid in ids:
        try:
            na = cl.call("task.next_action", {"task_id": tid, "workspace_instance_id": INSTANCE_ID})
        except Exception as e:
            errors += 1
            dist["RPC_ERR: " + str(e)[:40]] += 1
            continue
        decision = na.get("decision")
        blocking = na.get("blocking_conditions") or []
        if decision == "READY" or (not blocking and decision not in ("BLOCKED",)):
            ready += 1
            dist["READY（可派工）"] += 1
            continue
        cat = classify(blocking)
        dist[cat] += 1
        if len(samples[cat]) < 4:
            samples[cat].append(tid)

    print("\n=== 阻断原因分布 ===")
    for cat, n in dist.most_common():
        print(f"  {n:>4}  {cat}")
    print(f"\n  其中 READY 可派工: {ready} | RPC 错误: {errors}")

    print("\n=== 每类样例任务 ID ===")
    for cat, sids in samples.items():
        print(f"  [{cat}]")
        for sid in sids:
            print(f"    - {sid}  {title_of.get(sid, '')[:50]}")

    # 2. 顺便统计：这 132 个里有多少已有 task_workspace_bindings
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    have_binding = c.execute(
        """SELECT COUNT(*) FROM tasks t
           JOIN task_workspace_bindings b ON b.task_id=t.id
           WHERE t.parent_id=? AND t.status='review'""",
        (SUB_EPIC,),
    ).fetchone()[0]
    have_contract = c.execute(
        """SELECT COUNT(*) FROM tasks t
           JOIN role_contracts rc ON rc.task_id=t.id
           WHERE t.parent_id=? AND t.status='review'""",
        (SUB_EPIC,),
    ).fetchone()[0]
    c.close()
    print(f"\n=== 治理前提覆盖 ===")
    print(f"  有 workspace binding: {have_binding} / {len(ids)}")
    print(f"  有 role contract:     {have_contract} / {len(ids)}")


if __name__ == "__main__":
    main()
