from __future__ import annotations
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
from callwarden.db.db import CodeGraphDB

TASK_IDS = [
    "T-1787293818274-1b87b6c4",
    "T-1787305175972-8712da28",
    "T-1787305268313-06fcef5c",
    "T-1787307743865-696714f0",
]

def rows(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception as exc:
        return [{"query_error": str(exc)}]

def main() -> None:
    db = CodeGraphDB()
    conn = db.conn
    conn.row_factory = __import__('sqlite3').Row
    out = {}
    for task_id in TASK_IDS:
        out[task_id] = {
            "task": rows(conn, "SELECT id,title,status,created_at,updated_at,parent_id FROM tasks WHERE id=?", (task_id,)),
            "steps": rows(conn, "SELECT id,step_index,action,target_file,target_symbol,status,result,created_at,completed_at FROM task_steps WHERE task_id=? ORDER BY step_index", (task_id,)),
            "events": rows(conn, "SELECT event_type,payload,created_at FROM task_events WHERE task_id=? ORDER BY id", (task_id,)),
            "reports": rows(conn, "SELECT * FROM task_reports WHERE task_id=? ORDER BY id", (task_id,)),
            "handoffs": rows(conn, "SELECT * FROM task_handoffs WHERE task_id=? ORDER BY id", (task_id,)),
            "role_lineages": rows(conn, "SELECT * FROM role_contract_lineages WHERE task_id=? ORDER BY id", (task_id,)),
            "role_revisions": rows(conn, "SELECT * FROM role_contract_revisions WHERE task_id=? ORDER BY id", (task_id,)),
            "step_bindings": rows(conn, "SELECT * FROM task_step_role_contract_bindings WHERE task_id=? ORDER BY id", (task_id,)),
            "task_contracts": rows(conn, "SELECT * FROM task_contract_revisions WHERE task_id=? ORDER BY id", (task_id,)),
        }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))

if __name__ == "__main__":
    main()
