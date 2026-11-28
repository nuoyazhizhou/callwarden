"""只读盘点：最近 48h 更新的非 closed 任务（本会话相关线）。"""
import sqlite3
import time

DB = "file:C:/Users/wanpi/.callwarden/callwarden.db?mode=ro"
conn = sqlite3.connect(DB, uri=True)
cur = conn.cursor()

cutoff = time.time() - 48 * 3600
print("== 最近 48h 更新的非 closed 任务 ==")
cur.execute(
    "SELECT id, title, status, updated_at FROM tasks "
    "WHERE status != 'closed' AND updated_at > ? ORDER BY updated_at DESC",
    (cutoff,),
)
rows = cur.fetchall()
print(f"共 {len(rows)} 条")
for r in rows:
    age_h = (time.time() - r[3]) / 3600
    print(f"{r[0]} | {r[2]} | {age_h:.1f}h 前 | {r[1][:80]}")

conn.close()
