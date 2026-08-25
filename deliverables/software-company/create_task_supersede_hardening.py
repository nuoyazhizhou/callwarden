"""创建 task.supersede governance hardening 子任务。

使用 AGENTS.md 指定的 CodeGraphDB.task_create(parent_id=...) 标准路径，
暂时避开 daemon task.split 在已有 <parent>-sub-1 时触发的 UNIQUE constraint failed: tasks.id。
本脚本不会修改已有任务、代码、schema 或任务状态；同标题子任务已存在时只打印其 ID。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.db.db import CodeGraphDB

PARENT_ID = "T-1787203926824-9f873bfc"
TITLE = "P0-H：task.supersede governance hardening / promotion"
PLAN_PATH = PROJECT_ROOT / "deliverables" / "software-company" / "task_supersede_governance_hardening_task.md"


def main() -> None:
    description = PLAN_PATH.read_text(encoding="utf-8")
    db = CodeGraphDB()
    try:
        parent = db.conn.execute(
            "SELECT id, status FROM tasks WHERE id = ?", (PARENT_ID,)
        ).fetchone()
        if parent is None:
            raise SystemExit(f"parent task not found: {PARENT_ID}")
        existing = db.conn.execute(
            "SELECT id, status FROM tasks WHERE parent_id = ? AND title = ?",
            (PARENT_ID, TITLE),
        ).fetchone()
        if existing is not None:
            print(f"EXISTS {existing['id']} {existing['status']}")
            return
        task_id = db.task_create(
            title=TITLE,
            description=description,
            parent_id=PARENT_ID,
            steps=[],
            creator="executor-planner",
        )
        print(f"CREATED {task_id}")
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
