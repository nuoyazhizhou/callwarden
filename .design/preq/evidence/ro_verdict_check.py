"""只读核查：verdict 落账实况（team lead 用，禁止写操作）。"""
import sqlite3
import sys

DB = "file:C:/Users/wanpi/.callwarden/callwarden.db?mode=ro"
conn = sqlite3.connect(DB, uri=True)
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
print("== 相关表 ==")
print([t for t in tables if any(k in t for k in ("verdict", "lease", "evidence", "agent", "handoff"))])

for t in tables:
    if "verdict" in t:
        print(f"\n== {t} schema ==")
        cur.execute(f"PRAGMA table_info({t})")
        print([c[1] for c in cur.fetchall()])
        cur.execute(f"SELECT * FROM {t} ORDER BY rowid DESC LIMIT 10")
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            print(dict(zip(cols, row)))

conn.close()
