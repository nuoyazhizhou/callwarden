"""只读核查：web-observer-mvp 任务的 handoff / verdict / lease 事件实况。"""
import sqlite3
import time

DB = "file:C:/Users/wanpi/.callwarden/callwarden.db?mode=ro"
conn = sqlite3.connect(DB, uri=True)
cur = conn.cursor()

TASK = "T-1795469778065-1686c8a0"

print("== task_events 最近 10 条 ==")
cur.execute("PRAGMA table_info(task_events)")
cols = [c[1] for c in cur.fetchall()]
print(cols)
cur.execute(
    "SELECT * FROM task_events WHERE task_id = ? ORDER BY monotonic_seq DESC LIMIT 10", (TASK,)
)
for r in cur.fetchall():
    d = dict(zip(cols, r))
    keep = {k: d.get(k) for k in ("event_id", "from_status", "to_status", "reason_code", "role", "evidence_path", "authoritative_timestamp") if k in d}
    print(keep)

print("\n== verdict events ==")
cur.execute(
    "SELECT id, verdict_id, overall, submitted_at FROM task_verdict_events WHERE task_id = ?",
    (TASK,),
)
rows = cur.fetchall()
print(f"共 {len(rows)} 条")
for r in rows:
    print(r)

print("\n== lease events ==")
cur.execute("PRAGMA table_info(task_leases)")
lcols = [c[1] for c in cur.fetchall()]
cur.execute("SELECT * FROM task_leases WHERE task_id = ?", (TASK,))
for r in cur.fetchall():
    d = dict(zip(lcols, r))
    print({k: d.get(k) for k in ("id", "role", "agent_id", "fencing_counter", "status", "acquired_at", "expires_at") if k in d})

conn.close()
