from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
from callwarden.db.db import CodeGraphDB

ids = ["T-1787293818274-1b87b6c4", "T-1787293451688-c14b1e44"]
db = CodeGraphDB()
conn = db.conn
out = {"db": str(db.db_path), "tasks": {}}
for task_id in ids:
    def rows(sql, params=()):
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    out["tasks"][task_id] = {
        "task_contract_revisions": rows("SELECT contract_id,revision,contract_hash,profile,workspace_id FROM task_contract_revisions WHERE task_id=? ORDER BY revision", (task_id,)),
        "role_contract_lineages": rows("SELECT role_contract_lineage_id,role,workspace_id FROM role_contract_lineages WHERE task_id=?", (task_id,)),
        "role_contract_revisions": rows("SELECT r.role_contract_revision_id,l.role,r.revision FROM role_contract_revisions r JOIN role_contract_lineages l ON l.role_contract_lineage_id=r.role_contract_lineage_id WHERE l.task_id=?", (task_id,)),
        "step_bindings": rows("SELECT step_id,role_contract_revision_id,role_contract_revision FROM task_step_role_contract_bindings WHERE task_id=?", (task_id,)),
        "legacy_role_contracts": rows("SELECT role,revision,is_current FROM role_contracts WHERE task_id=?", (task_id,)),
        "steps": rows("SELECT id,step_index,action,status,target_file FROM task_steps WHERE task_id=? ORDER BY step_index", (task_id,)),
        "workspace_binding": rows("SELECT b.workspace_id,b.workspace_capture_id,c.workspace_instance_id,c.capture_revision,c.registry_identity_hash FROM task_workspace_bindings b JOIN workspace_authority_captures c ON b.workspace_capture_id=c.workspace_capture_id WHERE b.task_id=?", (task_id,)),
    }
print(json.dumps(out, ensure_ascii=False, indent=2))
conn.close()
