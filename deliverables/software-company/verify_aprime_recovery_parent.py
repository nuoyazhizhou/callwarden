"""只读核验 A′ 恢复父任务的创建原子性与 governance 合同完整性。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.db.db import CodeGraphDB

TASK_ID = "T-1787293451688-c14b1e44"
EXPECTED_PARENT = "T-1787203926824-9f873bfc"
EXPECTED_WORKSPACE_ID = 1
EXPECTED_INSTANCE = "ws-1"
EXPECTED_ROLES = {"executor", "reviewer", "adjudicator"}


def as_dict(row):
    return dict(row) if row is not None else None


def main() -> None:
    db = CodeGraphDB()
    try:
        task = db.conn.execute(
            "SELECT id, title, status, parent_id, creator FROM tasks WHERE id = ?", (TASK_ID,)
        ).fetchone()
        steps = db.conn.execute(
            "SELECT step_index, action, target_file, target_symbol, status FROM task_steps WHERE task_id = ? ORDER BY step_index",
            (TASK_ID,),
        ).fetchall()
        binding = db.conn.execute(
            """
            SELECT b.task_id, b.workspace_id, b.workspace_binding_id, b.workspace_capture_id,
                   c.workspace_instance_id, c.capture_revision, c.registry_identity_hash
              FROM task_workspace_bindings b
              JOIN workspace_authority_captures c ON c.workspace_capture_id = b.workspace_capture_id
             WHERE b.task_id = ?
            """,
            (TASK_ID,),
        ).fetchone()
        contracts = db.conn.execute(
            """
            SELECT role, prompt_template_id, prompt_hash, handoff_to, independence, revision, is_current
              FROM role_contracts
             WHERE task_id = ?
             ORDER BY role
            """,
            (TASK_ID,),
        ).fetchall()
        children = db.conn.execute(
            "SELECT id, title, status FROM tasks WHERE parent_id = ? ORDER BY sort_order, id",
            (TASK_ID,),
        ).fetchall()
        result = {
            "task": as_dict(task),
            "steps": [as_dict(row) for row in steps],
            "binding": as_dict(binding),
            "role_contracts": [as_dict(row) for row in contracts],
            "children": [as_dict(row) for row in children],
        }
        checks = {
            "parent_ok": bool(task and task["parent_id"] == EXPECTED_PARENT),
            "open_ok": bool(task and task["status"] == "open"),
            "has_steps": len(steps) >= 1,
            "binding_workspace_ok": bool(binding and binding["workspace_id"] == EXPECTED_WORKSPACE_ID),
            "binding_instance_ok": bool(binding and binding["workspace_instance_id"] == EXPECTED_INSTANCE),
            "contracts_ok": {row["role"] for row in contracts} == EXPECTED_ROLES,
            "no_child_before_cli01": len(children) == 0,
        }
        print(json.dumps({"checks": checks, "detail": result}, ensure_ascii=False, indent=2))
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
