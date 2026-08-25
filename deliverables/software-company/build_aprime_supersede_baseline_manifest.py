"""构建 A′ supersede 的只读 evidence manifest。

本脚本不调用 daemon 写操作；不创建、修改、supersede、apply 或 close 任何任务。
它仅冻结工具迁移矩阵与 task workspace binding 前置条件，供有正式 adjudicator
身份及 reviewer lease/fencing 的后续治理动作引用。
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.db.db import CodeGraphDB

MATRIX_PATH = PROJECT_ROOT / "deliverables" / "software-company" / "tool_migration_matrix.json"
OUTPUT_PATH = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_supersede_baseline_manifest.json"
OLD_S2_ORIGINAL = "T-1787203937201-0a156564"
OLD_S2_REBUILT = "T-1787209948470-a59bcf9c"
SUCCESSOR = "T-1787293451688-c14b1e44"


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def task_projection(conn, task_id: str) -> dict:
    task = conn.execute(
        "SELECT id, title, status, parent_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    binding = conn.execute(
        """
        SELECT b.workspace_id, b.workspace_binding_id, b.workspace_capture_id,
               c.workspace_instance_id, c.registry_identity_hash, c.workspace_manifest_hash,
               c.capture_revision
          FROM task_workspace_bindings b
          LEFT JOIN workspace_authority_captures c ON c.workspace_capture_id = b.workspace_capture_id
         WHERE b.task_id = ?
        """,
        (task_id,),
    ).fetchone()
    return {
        "task": dict(task) if task else None,
        "binding": dict(binding) if binding else None,
    }


def main() -> None:
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    tools = matrix["tools"]
    by_backend = Counter(item.get("target_backend", "unknown") for item in tools)
    by_status = Counter(item.get("status", "unknown") for item in tools)
    compat = [
        {
            "name": item["name"],
            "module": item["module"],
            "rpc_method": item["rpc_method"],
            "op_class": item["op_class"],
        }
        for item in tools
        if item.get("target_backend") == "python_compat"
    ]
    db = CodeGraphDB()
    try:
        old_original = task_projection(db.conn, OLD_S2_ORIGINAL)
        old_rebuilt = task_projection(db.conn, OLD_S2_REBUILT)
        successor = task_projection(db.conn, SUCCESSOR)
    finally:
        db.conn.close()

    preflight = {
        "successor_exists": successor["task"] is not None,
        "successor_bound": successor["binding"] is not None,
        "successor_workspace_id": (successor["binding"] or {}).get("workspace_id"),
        "old_s2_original_exists": old_original["task"] is not None,
        "old_s2_original_bound": old_original["binding"] is not None,
        "old_s2_rebuilt_exists": old_rebuilt["task"] is not None,
        "old_s2_rebuilt_bound": old_rebuilt["binding"] is not None,
        "same_workspace_original_successor": bool(
            old_original["binding"] and successor["binding"]
            and old_original["binding"]["workspace_id"] == successor["binding"]["workspace_id"]
        ),
        "same_workspace_rebuilt_successor": bool(
            old_rebuilt["binding"] and successor["binding"]
            and old_rebuilt["binding"]["workspace_id"] == successor["binding"]["workspace_id"]
        ),
    }
    manifest = {
        "manifest_type": "cw.aprime.supersede-baseline.v1",
        "scope": "A′ recovery successor evidence; no state transition performed",
        "authority": {
            "expected_workspace_id": 1,
            "expected_workspace_instance_id": "ws-1",
            "successor_task_id": SUCCESSOR,
        },
        "matrix": {
            "path": "deliverables/software-company/tool_migration_matrix.json",
            "sha256": sha256(MATRIX_PATH),
            "schema_version": matrix.get("schema_version"),
            "generated_at": matrix.get("generated_at"),
            "total_tools": matrix.get("total_tools"),
            "target_backend_counts": dict(sorted(by_backend.items())),
            "status_counts": dict(sorted(by_status.items())),
            "python_compat_scope_count": len(compat),
            "python_compat_scope": compat,
        },
        "tasks": {
            "old_s2_original": old_original,
            "old_s2_rebuilt": old_rebuilt,
            "a_prime_successor": successor,
        },
        "preflight": preflight,
        "required_for_formal_supersede": [
            "adjudicator identity registered for the target workspace",
            "active reviewer lease and current fencing counter",
            "evidence_path and evidence_hash derived from this manifest",
            "both predecessor and successor bound to the same workspace authority",
            "separate daemon task.supersede calls for each predecessor",
        ],
        "planner_result": "prepared evidence only; no task.supersede call performed",
    }
    OUTPUT_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(OUTPUT_PATH)


if __name__ == "__main__":
    main()
