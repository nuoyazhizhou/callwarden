"""只读盘点 A′ 首批治理任务的 workspace authority 状态。

不创建、更新或删除任何任务、workspace、binding 或 capture；仅输出任务行与
workspace authority 关联，以便决定 A′ 恢复父任务的合法创建路径。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.db.db import CodeGraphDB

TASK_IDS = (
    "T-1787203926824-9f873bfc",
    "T-1787203937201-0a156564",
    "T-1787209948470-a59bcf9c",
    "T-1787277487109-758e56d0",
)


def row_dict(row):
    return dict(row) if row is not None else None


def main() -> None:
    db = CodeGraphDB()
    try:
        print(f"DB={db.db_path}")
        print("TASK_AUTHORITY")
        for task_id in TASK_IDS:
            task = db.conn.execute(
                "SELECT id, title, status, parent_id, creator FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            binding = db.conn.execute(
                """
                SELECT b.task_id, b.workspace_id, b.workspace_binding_id, b.workspace_capture_id,
                       b.created_by, b.authoritative_created_at,
                       c.workspace_instance_id, c.registry_identity_hash,
                       c.workspace_manifest_hash, c.capture_revision
                  FROM task_workspace_bindings b
                  LEFT JOIN workspace_authority_captures c
                    ON c.workspace_capture_id = b.workspace_capture_id
                 WHERE b.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            print({"task": row_dict(task), "binding": row_dict(binding)})
        print("WORKSPACES")
        for workspace in db.conn.execute(
            "SELECT id, name, root_path, is_active, active_task_id FROM workspaces ORDER BY id"
        ).fetchall():
            print(row_dict(workspace))
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
