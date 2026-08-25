"""只读审计用户级 CallWarden 数据库的批量治理回填状态；绝不写库。"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path

DB = Path(r"C:\Users\wanpi\.callwarden\callwarden.db")
OUT = Path(__file__).resolve().parent / "contract_backfill_authority_audit.json"


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def main() -> None:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    contract_cols = table_columns(conn, "task_contract_revisions")
    lease_cols = table_columns(conn, "task_leases")
    task_cols = table_columns(conn, "tasks")
    rows = [dict(row) for row in conn.execute("SELECT task_id, contract_id, revision, profile, created_by, envelope_payload, created_at FROM task_contract_revisions ORDER BY created_at")]
    placeholder = []
    profile_counts: dict[str, int] = {}
    creator_counts: dict[str, int] = {}
    for row in rows:
        profile_counts[row.get("profile") or "<null>"] = profile_counts.get(row.get("profile") or "<null>", 0) + 1
        creator_counts[row.get("created_by") or "<null>"] = creator_counts.get(row.get("created_by") or "<null>", 0) + 1
        payload = row.get("envelope_payload") or ""
        if "scope creep if not whitelisted" in payload or "git revert <commit>" in payload or '"dependencies":[]' in payload.replace(" ", ""):
            placeholder.append({"task_id": row["task_id"], "contract_id": row["contract_id"], "profile": row["profile"], "created_by": row["created_by"], "payload": payload})
    aprime_parent_id = "T-1787293451688-c14b1e44"
    aprime_ids = [row[0] for row in conn.execute("SELECT id FROM tasks WHERE parent_id=? ORDER BY id", (aprime_parent_id,))]
    aprime_set = set(aprime_ids)
    aprime = [row for row in rows if row["task_id"] in aprime_set]
    aprime_placeholder = [row for row in placeholder if row["task_id"] in aprime_set]
    aprime_task_rows = [dict(row) for row in conn.execute("SELECT id,title,status,parent_id FROM tasks WHERE parent_id=? ORDER BY id", (aprime_parent_id,))]
    aprime_without_contract = [row for row in aprime_task_rows if row["id"] not in {contract["task_id"] for contract in aprime}]
    binding_count = 0
    role_lineage_count = 0
    if aprime_ids:
        marks = ",".join("?" for _ in aprime_ids)
        binding_count = scalar(conn, f"SELECT COUNT(*) FROM task_step_role_contract_bindings WHERE task_id IN ({marks})", tuple(aprime_ids))
        role_lineage_count = scalar(conn, f"SELECT COUNT(*) FROM role_contract_lineages WHERE task_id IN ({marks})", tuple(aprime_ids))
    lease_summary = {"columns": lease_cols, "rows": [], "by_status_agent": [], "backfill_reviewer_active": 0, "backfill_reviewer_expired_or_released": 0}
    if lease_cols:
        select = ", ".join(lease_cols)
        lease_summary["rows"] = [dict(row) for row in conn.execute(f"SELECT {select} FROM task_leases ORDER BY rowid DESC LIMIT 250")]
        lease_summary["by_status_agent"] = [dict(row) for row in conn.execute("SELECT status, agent_id, COUNT(*) AS count FROM task_leases GROUP BY status, agent_id ORDER BY status, agent_id")]
        lease_summary["backfill_reviewer_active"] = scalar(conn, "SELECT COUNT(*) FROM task_leases WHERE role='reviewer' AND agent_id='reviewer-wb-186loop' AND status='active'")
        lease_summary["backfill_reviewer_expired_or_released"] = scalar(conn, "SELECT COUNT(*) FROM task_leases WHERE role='reviewer' AND agent_id='reviewer-wb-186loop' AND status<>'active'")
    report = {
        "db": str(DB), "task_contract_columns": contract_cols, "task_columns": task_cols,
        "task_contract_revision_count": len(rows), "contract_profile_counts": profile_counts,
        "contract_creator_counts": creator_counts, "placeholder_contract_count": len(placeholder),
        "placeholder_contracts": placeholder, "aprime_parent_id": aprime_parent_id,
        "aprime_direct_child_count": len(aprime_ids), "aprime_contract_count": len(aprime),
        "aprime_placeholder_contract_count": len(aprime_placeholder), "aprime_placeholder_task_ids": [row["task_id"] for row in aprime_placeholder],
        "aprime_without_contract": aprime_without_contract,
        "aprime_contract_task_ids": [row["task_id"] for row in aprime], "aprime_role_lineage_count": role_lineage_count,
        "aprime_step_binding_count": binding_count, "lease_summary": lease_summary,
        "open_without_contract": scalar(conn, "SELECT COUNT(*) FROM tasks t WHERE t.status='open' AND NOT EXISTS (SELECT 1 FROM task_contract_revisions c WHERE c.task_id=t.id)"),
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("task_contract_revision_count", "placeholder_contract_count", "aprime_contract_count", "aprime_role_lineage_count", "aprime_step_binding_count", "open_without_contract")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
