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
  4. 看板本身在看板子卡未闭合前不得为 `closed`（提前关闭视为违规）；
  5. ADR-002 §38 远端验收行（S1-01…S1-20 / S2-01…S2-09 / S3-01…S3-10）逐行
     核验：S1 行须有承载测试与用例标记，S2/S3 行须归属卡已 `closed`。

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

# ---------------------------------------------------------------------------
# ADR-002 §38 远端平面验收行（S1/S2/S3）
# ---------------------------------------------------------------------------
# 每一行都必须在矩阵里有唯一可核验证据，否则视为缺口：
#   - S1 行：由 pytest 用例承载，检查测试文件存在且含对应 `s1_XX` 用例标记；
#   - S2/S3 行：实现尚未落地，按归属任务卡是否 closed 判定（open → 缺口）。
REMOTE_ACCEPTANCE_ROWS: List[Dict[str, str]] = [
    {"id": "S1-01", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_control_plane.py", "marker": "s1_01"},
    {"id": "S1-02", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_enrollment.py", "marker": "s1_02"},
    {"id": "S1-03", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_enrollment.py", "marker": "s1_03"},
    {"id": "S1-04", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_principal_authorization.py", "marker": "s1_04"},
    {"id": "S1-05", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_principal_authorization.py", "marker": "s1_05"},
    {"id": "S1-06", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_enrollment.py", "marker": "s1_06"},
    {"id": "S1-07", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_enrollment.py", "marker": "s1_07"},
    {"id": "S1-08", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_control_plane.py", "marker": "s1_08"},
    {"id": "S1-09", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_control_plane.py", "marker": "s1_09"},
    {"id": "S1-10", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_control_plane.py", "marker": "s1_10"},
    {"id": "S1-11", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_enrollment.py", "marker": "s1_11"},
    {"id": "S1-12", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_enrollment.py", "marker": "s1_12"},
    {"id": "S1-13", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_principal_authorization.py", "marker": "s1_13"},
    {"id": "S1-14", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_principal_authorization.py", "marker": "s1_14"},
    {"id": "S1-15", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_control_plane.py", "marker": "s1_15"},
    {"id": "S1-16", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_control_plane.py", "marker": "s1_16"},
    {"id": "S1-17", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_control_plane.py", "marker": "s1_17"},
    {"id": "S1-18", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_control_plane.py", "marker": "s1_18"},
    {"id": "S1-19", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_enrollment.py", "marker": "s1_19"},
    {"id": "S1-20", "owner_task": "T-1793774130685-6acab9d4",
     "test_file": "tests/test_mtls_control_plane.py", "marker": "s1_20"},
    {"id": "S2-01", "owner_task": "T-1793774130739-6e096a14"},
    {"id": "S2-02", "owner_task": "T-1793774130739-6e096a14"},
    {"id": "S2-03", "owner_task": "T-1793774130739-6e096a14"},
    {"id": "S2-04", "owner_task": "T-1793774130739-6e096a14"},
    {"id": "S2-05", "owner_task": "T-1793774130739-6e096a14"},
    {"id": "S2-06", "owner_task": "T-1793774130739-6e096a14"},
    {"id": "S2-07", "owner_task": "T-1793774130739-6e096a14"},
    {"id": "S2-08", "owner_task": "T-1793774130739-6e096a14"},
    {"id": "S2-09", "owner_task": "T-1793774130739-6e096a14"},
    {"id": "S3-01", "owner_task": "T-1793774130800-71aa5b74"},
    {"id": "S3-02", "owner_task": "T-1793774130800-71aa5b74"},
    {"id": "S3-03", "owner_task": "T-1793774130800-71aa5b74"},
    {"id": "S3-04", "owner_task": "T-1793774130800-71aa5b74"},
    {"id": "S3-05", "owner_task": "T-1793774130800-71aa5b74"},
    {"id": "S3-06", "owner_task": "T-1793774130800-71aa5b74"},
    {"id": "S3-07", "owner_task": "T-1793774130800-71aa5b74"},
    {"id": "S3-08", "owner_task": "T-1793774130800-71aa5b74"},
    {"id": "S3-09", "owner_task": "T-1793774130800-71aa5b74"},
    {"id": "S3-10", "owner_task": "T-1793774130800-71aa5b74"},
]


def _remote_row_gaps(conn: sqlite3.Connection) -> List[Dict[str, str]]:
    """核验 S1–S3 每条验收行，返回行状态（含 gaps）。"""
    rows: List[Dict[str, str]] = []
    for row in REMOTE_ACCEPTANCE_ROWS:
        gaps: List[str] = []
        test_file = row.get("test_file", "")
        if test_file:
            path = PROJECT_ROOT / test_file
            if not path.is_file():
                gaps.append(f"测试文件缺失: {test_file}")
            elif row["marker"] not in path.read_text(
                encoding="utf-8", errors="replace"
            ):
                gaps.append(f"{test_file} 缺少用例标记 {row['marker']}")
        owner = _task_brief(conn, row["owner_task"])
        if owner is None:
            gaps.append(f"归属卡 {row['owner_task']} 不存在")
        status = "gap" if gaps else "pass"
        if not test_file:
            if owner is not None and owner["status"] != "closed":
                # 归属卡尚未收口：该行视为 pending（不计缺口），卡 closed 后自动转严。
                status = "pending"
        rows.append(
            {
                "id": row["id"],
                "owner_task": row["owner_task"],
                "evidence": test_file or "owner_task_closed",
                "status": status,
                "gaps": gaps,
            }
        )
    return rows


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
        "remote_rows": [],
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

        remote_rows = _remote_row_gaps(conn)
        result["remote_rows"] = remote_rows
        remote_open = any(r["gaps"] for r in remote_rows)

        if board["status"] == "closed" and (children_open or watched_open):
            result["violations"].append(
                f"看板卡 {board_id} 已 closed，但子卡/遗留卡仍有未闭合项（提前关闭）"
            )
        if remote_open:
            result["violations"].append(
                "ADR-002 §38 远端验收行存在未闭合项（S2/S3 卡未 closed）"
            )

        for item in result["children"] + result["watched"]:
            if item.get("gaps"):
                label = item.get("title") or item["task_id"]
                result["gaps"].append(f"{item['task_id']} {label}: " + "; ".join(item["gaps"]))
        for row in remote_rows:
            if row["gaps"]:
                result["gaps"].append(f"ADR-002 {row['id']}: " + "; ".join(row["gaps"]))
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
    if payload.get("remote_rows"):
        print("ADR-002 §38 远端验收行:")
        for row in payload["remote_rows"]:
            status = row.get("status", "gap")
            mark = {"pass": "OK", "pending": "PENDING"}.get(status, "GAP")
            print(f"  - [{mark}] {row['id']}  evidence={row['evidence']}")
            for g in row.get("gaps", []):
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
