#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_e2e_matrix_closure.py
==============================

CLI/MCP 验收矩阵收口门禁（只读）。

背景：2026-09-23 的 `T-1790136923625-2803e218`（P0 单实例守卫部署 + CLI/MCP
双入口 E2E 验收测试矩阵）虽已关闭，但 Wave 2（CLI 矩阵产物）、Wave 3（治理写
路径环）、Wave 4（并发/崩溃边界）并无独立闭环记录，其中若干小项也散落在其它
卡里。为避免"父卡关了、子项没做"再次发生，本脚本把收口条件固化成可执行门禁：

  1. 存在看板卡 `[GOV] 9 月 CLI/MCP 验收矩阵收口看板`（唯一）；
  2. 该看板的全部子卡（`tasks.parent_id`）均为 `closed`，且每张子卡至少有一个
     step 且全部 `done`（被 supersede 收编的卡按 daemon GOV-FIX-03 豁免步骤门禁）；
  3. `WATCHED_TASK_IDS` 里的历史遗留卡同样 `closed` 且步骤全 `done`，或按
     `LEGACY_WAIVERS` 提供存在且含该 task id 的豁免证据文件；
  4. 看板本身在看板子卡未闭合前不得为 `closed`（提前关闭视为违规）。

任一条件不满足 → 退出码 1，并逐卡打印缺口；全部满足 → 退出码 0。

用法：
    python scripts/check_e2e_matrix_closure.py            # 人类可读
    python scripts/check_e2e_matrix_closure.py --json     # 机器可读
    python scripts/check_e2e_matrix_closure.py --db <path>

环境变量：
    CALLWARDEN_TASK_DB  任务库路径（默认 C:/Users/wanpi/.callwarden/callwarden.db）

退出码：
    0 = 收口条件全部满足
    1 = 存在未闭合缺口 / 看板缺失或歧义 / 库不可读
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BOARD_TITLE_MARKER = "[GOV] 9 月 CLI/MCP 验收矩阵收口看板"

# 历史遗留、必须在看板关闭前一并处置的卡（不属于看板子卡）。
WATCHED_TASK_IDS: List[str] = [
    # 2026-09-16「[live-e2e] MCP task_create workspace-pair 验证（勿动）」
    "T-1789563951668-ba0c338c",
]

# daemon 侧无法关闭的历史卡豁免（必须提供存在且含 task id 的处置证据）。
# 2026-10-29 实证：该卡绑定已删除的临时 workspace 1707（owner_uid=4294967295），
# supersede 被 E_SUPERSEDE_CROSS_WORKSPACE 拒绝、cascade-close 因占位 pending 步
# 被 leaf_steps_pending 拒绝，形成死锁；scope 已由看板子卡承接。
LEGACY_WAIVERS: Dict[str, str] = {
    "T-1789563951668-ba0c338c": "docs/evidence/supersede_9-16_live_e2e_20261029.json",
}

DEFAULT_DB = r"C:\Users\wanpi\.callwarden\callwarden.db"


def _open_readonly(db_path: str) -> sqlite3.Connection:
    """只读打开任务库（sqlite URI mode=ro），绝不触发建库/写锁。"""
    uri = "file:{}?mode=ro".format(db_path.replace("\\", "/"))
    return sqlite3.connect(uri, uri=True)


def _task_brief(conn: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT id, title, status, parent_id FROM tasks WHERE id = ?1", (task_id,)
    ).fetchone()
    if row is None:
        return None
    steps = conn.execute(
        "SELECT id, step_index, status, action FROM task_steps WHERE task_id = ?1 "
        "ORDER BY step_index, id",
        (task_id,),
    ).fetchall()
    superseded_by = conn.execute(
        "SELECT superseding_task_id FROM task_supersede_relations "
        "WHERE superseded_task_id = ?1 ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return {
        "task_id": row[0],
        "title": row[1],
        "status": row[2],
        "parent_id": row[3],
        "superseded_by": superseded_by[0] if superseded_by else "",
        "steps": [
            {"id": s[0], "step_index": s[1], "status": s[2], "action": s[3]}
            for s in steps
        ],
    }


def _card_gaps(task: Dict[str, Any], require_steps: bool = True) -> List[str]:
    """返回单张卡的未闭合缺口（空列表表示已闭环）。

    被 supersede 收编的叶子卡，其 pending/failed 步骤已由后继卡 scope 承接，
    daemon `task.cascade_close` 按 GOV-FIX-03 豁免步骤门禁（见
    `task_collab_lifecycle_apply.rs` 文档）。此豁免只对已 closed 且存在
    supersede 关系的历史卡生效，不适用于看板当前子卡。
    """
    gaps: List[str] = []
    if task["status"] != "closed":
        gaps.append(f"status={task['status']}（要求 closed）")
    waived = task["status"] == "closed" and bool(task.get("superseded_by"))
    steps = task["steps"]
    if require_steps and not steps and not waived:
        gaps.append("无任何 step（收口卡必须有可核验步骤）")
    pending = [s for s in steps if s["status"] != "done"]
    if pending and not waived:
        desc = ", ".join(f"step#{s['step_index']}({s['status']})" for s in pending)
        gaps.append(f"步骤未完成: {desc}")
    return gaps


def evaluate(db_path: str) -> Dict[str, Any]:
    """执行全部收口检查，返回结构化结果（不做 I/O 打印）。"""
    result: Dict[str, Any] = {
        "ok": False,
        "db": db_path,
        "board": None,
        "children": [],
        "watched": [],
        "violations": [],
        "gaps": [],
    }
    if not os.path.exists(db_path):
        result["gaps"].append(f"任务库不存在: {db_path}")
        return result

    try:
        conn = _open_readonly(db_path)
    except sqlite3.Error as exc:  # pragma: no cover - 环境异常
        result["gaps"].append(f"无法只读打开任务库: {exc}")
        return result

    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE title LIKE ?1 AND depth >= 0 "
            "ORDER BY created_at DESC",
            (f"%{BOARD_TITLE_MARKER}%",),
        ).fetchall()
        board_ids = [r[0] for r in rows]
        if not board_ids:
            result["gaps"].append(f"未找到看板卡（title 含 {BOARD_TITLE_MARKER!r}）")
            return result
        if len(board_ids) > 1:
            result["gaps"].append(
                "看板卡不唯一，存在 {} 张: {}".format(len(board_ids), ", ".join(board_ids))
            )
            return result

        board_id = board_ids[0]
        board = _task_brief(conn, board_id)
        if board is None:
            result["gaps"].append(f"看板卡 {board_id} 读取失败")
            return result
        result["board"] = board

        child_rows = conn.execute(
            "SELECT id FROM tasks WHERE parent_id = ?1 ORDER BY sort_order, created_at",
            (board_id,),
        ).fetchall()
        child_ids = [r[0] for r in child_rows]
        if not child_ids:
            result["gaps"].append(f"看板卡 {board_id} 没有任何子卡")

        children_open = False
        for cid in child_ids:
            child = _task_brief(conn, cid)
            if child is None:
                result["children"].append({"task_id": cid, "missing": True, "gaps": ["卡不存在"]})
                children_open = True
                continue
            gaps = _card_gaps(child)
            result["children"].append(
                {"task_id": cid, "title": child["title"], "status": child["status"], "gaps": gaps}
            )
            if gaps:
                children_open = True

        for wid in WATCHED_TASK_IDS:
            watched = _task_brief(conn, wid)
            if watched is None:
                result["watched"].append({"task_id": wid, "missing": True, "gaps": ["卡不存在"]})
                continue
            gaps = _card_gaps(watched)
            waiver = ""
            if gaps and wid in LEGACY_WAIVERS:
                rel = LEGACY_WAIVERS[wid]
                waiver_path = PROJECT_ROOT / rel
                if waiver_path.is_file() and wid in waiver_path.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    waiver = rel
                    gaps = []
                else:
                    gaps.append(f"豁免证据缺失或未含 task id: {rel}")
            result["watched"].append(
                {
                    "task_id": wid,
                    "title": watched["title"],
                    "status": watched["status"],
                    "waiver": waiver,
                    "gaps": gaps,
                }
            )

        watched_open = any(w.get("gaps") for w in result["watched"])

        if board["status"] == "closed" and (children_open or watched_open):
            result["violations"].append(
                f"看板卡 {board_id} 已 closed，但子卡/遗留卡仍有未闭合项（提前关闭）"
            )

        for item in result["children"] + result["watched"]:
            if item.get("gaps"):
                label = item.get("title") or item["task_id"]
                result["gaps"].append(f"{item['task_id']} {label}: " + "; ".join(item["gaps"]))
        result["gaps"].extend(result["violations"])

        result["ok"] = not result["gaps"]
    finally:
        conn.close()

    return result


def _print_human(payload: Dict[str, Any]) -> None:
    print("== CLI/MCP 验收矩阵收口门禁 ==")
    print(f"任务库: {payload['db']}")
    board = payload.get("board")
    if board:
        print(f"看板卡: {board['task_id']}  [{board['status']}]  {board['title']}")
    for item in payload.get("children", []):
        mark = "OK" if not item.get("gaps") else "GAP"
        print(f"  - [{mark}] {item['task_id']}  {item.get('title', '')}")
        for g in item.get("gaps", []):
            print(f"        ! {g}")
    if payload.get("watched"):
        print("遗留卡:")
        for item in payload["watched"]:
            mark = "OK" if not item.get("gaps") else "GAP"
            print(f"  - [{mark}] {item['task_id']}  {item.get('title', '')}")
            if item.get("waiver"):
                print(f"        ~ 处置豁免证据: {item['waiver']}")
            for g in item.get("gaps", []):
                print(f"        ! {g}")
    if payload["ok"]:
        print("结论: PASS（收口条件全部满足）")
    else:
        print("结论: BLOCKED（以下缺口未闭合）")
        for g in payload["gaps"]:
            print(f"  * {g}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CLI/MCP 验收矩阵收口门禁（只读）")
    parser.add_argument(
        "--db",
        default=os.environ.get("CALLWARDEN_TASK_DB", DEFAULT_DB),
        help="任务库路径（默认 %(default)s）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    payload = evaluate(args.db)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
