#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0-L 受合同约束的任务导入脚本（DRAFT —— 默认不执行，--dry-run）

用途
----
在 P0-L (T-1787801315246-e3e3a08c) 取得正式 reviewer PASS 并 close 之后，将其后续
remediation 子任务（A″ 批次）受合同约束地批量建卡。

硬门禁（不可绕过，对应本次 T-1787850432491 fix_defect 缺 Role Contract 绑定的根因）
----------------------------------------------------------------------
1. 必须经 daemon RPC 写入（cw CLI，禁止 --mode local），保证 fail-closed / 身份 / 租约校验。
2. P0-L 必须 tasks.status == 'closed' 且存在 overall='PASS' 的 verdict 事件，否则拒绝建卡。
3. 每个子任务必须绑定其 parent 的 executor Role Contract lineage（运行时从 DB 解析，
   不硬编码），禁止出现无 Role Contract 绑定的步骤。
4. 批量数量 / scope 须先与用户确认（交接称 11 / P0-L BLOCKED.md 引 A″-01…A″-37 = 37）。
   默认 DRY_RUN，绝不静默建卡。

依赖
----
- daemon 运行中（本机当前 daemon 未运行：127.0.0.1:8004 拒绝连接，故本脚本当前不可执行）。
- `cw` CLI 位于 RUNTIME_CURRENT 或 PATH。
- PYTHONPATH=C:/git_work（callwarden 包）。

用法
----
  python p0l_contract_constrained_import.py --dry-run          # 仅打印计划，不建卡
  python p0l_contract_constrained_import.py --count 11 --go    # 确认数量后，经 daemon 建卡
"""

import argparse
import os
import sqlite3
import subprocess
import sys

DB_PATH = r"C:\Users\wanpi\.callwarden\callwarden.db"
P0L_TASK_ID = "T-1787801315246-e3e3a08c"
# P0-L 的 parent（A′ migration Epic 之一）；子任务将挂在该父任务下，而非 P0-L 自身。
P0L_PARENT_ID = "T-1787203926824-9f873bfc"
RUNTIME_CURRENT = r"C:\Users\wanpi\.callwarden\runtime\current\cw.exe"

# TODO(用户确认): 交接称 11 张；P0-L BLOCKED.md 引 A″-01…A″-37（37 张）。
# 建卡前必须与用户确认真实清单，禁止一次性批量建卡直到 release gates 满足（BLOCKED.md 第7点）。
DEFAULT_COUNT = 11


def read_only_db():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def p0l_gate_ok(cur):
    """P0-L 必须 closed 且有 PASS verdict，否则返回 (False, reason)。"""
    cur.execute("SELECT status FROM tasks WHERE id=?", (P0L_TASK_ID,))
    row = cur.fetchone()
    if not row:
        return False, "P0-L task not found"
    if row[0] != "closed":
        return False, f"P0-L status={row[0]} (required: closed)"
    cur.execute(
        "SELECT overall FROM task_verdict_events WHERE task_id=? AND overall='PASS'",
        (P0L_TASK_ID,),
    )
    if not cur.fetchone():
        return False, "P0-L has no PASS verdict event"
    return True, "ok"


def resolve_executor_lineage(cur, parent_id):
    """从 DB 解析 parent 任务的 executor Role Contract lineage / revision / hash。"""
    cur.execute(
        """SELECT b.role_contract_lineage_id, b.role_contract_revision_id,
                  b.role_contract_revision, b.role_contract_hash,
                  b.canonicalization_version, b.canonicalization_rules_hash
           FROM task_step_role_contract_bindings b
           JOIN role_contracts rc ON rc.contract_id LIKE '%' || b.role_contract_lineage_id
           WHERE b.task_id=? AND rc.role='executor' LIMIT 1""",
        (parent_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def build_markdown_plan(count, lineage):
    """生成受合同约束的子任务 Markdown 计划（供 cw task split 消费）。"""
    lines = ["# P0-L remediation 子任务导入（受合同约束）", ""]
    for i in range(1, count + 1):
        lines.append(f"## A″-{i:02d}: P0-L 后续 remediation 子任务 {i}")
        lines.append("")
        lines.append("- action: fix_defect")
        lines.append(f"- target: rust_ext/src/daemon/")
        lines.append(
            f"- role_contract_lineage: {lineage['role_contract_lineage_id']}"
        )
        lines.append(
            f"- role_contract_revision: {lineage['role_contract_revision']}"
        )
        lines.append(f"- role_contract_hash: {lineage['role_contract_hash']}")
        lines.append(
            "- acceptance: 每个步骤必须带 Role Contract 绑定；禁止无绑定步骤"
        )
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT)
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--go", dest="go", action="store_true", help="经 daemon 真正建卡")
    args = ap.parse_args()
    if args.go:
        args.dry_run = False

    con = read_only_db()
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    ok, reason = p0l_gate_ok(cur)
    lineage = resolve_executor_lineage(cur, P0L_PARENT_ID)
    con.close()

    if not ok:
        print(f"[GATE BLOCKED] {reason} —— 拒绝建卡（须 P0-L closed + PASS）")
        sys.exit(2)
    if not lineage:
        print("[GATE BLOCKED] 无法解析 parent executor Role Contract lineage")
        sys.exit(2)

    plan = build_markdown_plan(args.count, lineage)
    print("=== 受合同约束的导入计划（count=%d）===" % args.count)
    print(plan)

    if args.dry_run:
        print("\n[DRY-RUN] 未建卡。确认数量/scope 且 P0-L 已 PASS 后，用 --go 经 daemon 建卡。")
        return

    # 经 daemon 建卡：cw task split 会走 daemon bootstrap，为每个步骤正确创建
    # task_step_role_contract_bindings（规避本次 fix_defect 缺绑定的 daemon bug）。
    if not os.path.exists(RUNTIME_CURRENT):
        print(f"[ERROR] cw CLI 不存在: {RUNTIME_CURRENT}")
        sys.exit(3)
    plan_path = os.path.join(os.path.dirname(__file__), "p0l_import_plan.md")
    with open(plan_path, "w", encoding="utf-8") as f:
        f.write(plan)
    cmd = [
        RUNTIME_CURRENT, "task", "split",
        "--parent", P0L_PARENT_ID,
        "--plan", plan_path,
        # 注意：不使用 --mode local；默认走 daemon（auto），保证治理校验生效。
    ]
    print("\n[EXEC] 经 daemon 建卡:", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout)
    if r.returncode != 0:
        print("[ERROR]", r.stderr)
        sys.exit(r.returncode)
    print("[OK] 已受合同约束地建卡；请独立 Reviewer 逐卡核验。")


if __name__ == "__main__":
    main()
