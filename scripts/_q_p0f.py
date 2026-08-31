import sqlite3
AUTH_DB = r"C:\Users\wanpi\.callwarden\callwarden.db"
conn = sqlite3.connect(AUTH_DB)
conn.row_factory = sqlite3.Row
TID="T-1787310376068-44eb5f20"
print("task:", [dict(r) for r in conn.execute("SELECT id,status FROM tasks WHERE id=?", (TID,))])
print("step:", [dict(r) for r in conn.execute("SELECT id,action,status FROM task_steps WHERE task_id=? ORDER BY step_index", (TID,))])
conn.close()