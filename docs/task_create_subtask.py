"""挂载子任务到父任务的标准脚本模板

CLI `cw task create` 当前不支持 --parent 参数，需要通过 Python API 挂载子任务。
本脚本提供标准模板，直接运行会创建示例子任务；复制后修改 tasks_to_mount 列表即可。

用法：
    python docs/task_create_subtask.py              # 创建示例子任务
    python docs/task_create_subtask.py --dry-run   # 仅打印，不实际创建

检查已存在同名子任务（避免重复创建），支持 closed 状态直接写入（无 steps 的任务）。
"""
import sys
import os
import time
import argparse

# 需要把 callwarden 包的父目录加到 path，使 `from callwarden.db.db import CodeGraphDB` 可用
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB


# ============================================
# 配置区：修改这里
# ============================================

PARENT_ID = "T-XXXXXXXXXXX-XXXX"  # ← 改成你的父任务 ID

# 要挂载的子任务列表
# status: "open"（待办）或 "closed"（已完成，直接关闭）
tasks_to_mount = [
    {
        "title": "示例子任务",
        "desc": "子任务描述，可多行\n第二行",
        "status": "open",
    },
    # 复制此 dict 添加更多子任务
]

# ============================================
# 执行区：一般不需要修改
# ============================================

def main():
    parser = argparse.ArgumentParser(description="挂载子任务到父任务")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不实际创建")
    args = parser.parse_args()

    if not PARENT_ID or PARENT_ID.startswith("T-XXX"):
        print("错误：请先修改 PARENT_ID 为实际父任务 ID")
        sys.exit(1)

    if args.dry_run:
        print(f"[DRY-RUN] 父任务：{PARENT_ID}")
        for t in tasks_to_mount:
            print(f"  [{t['status']}] {t['title']}")
        print(f"共 {len(tasks_to_mount)} 个子任务")
        return

    db = CodeGraphDB()

    # 验证父任务存在
    cur = db.conn.execute("SELECT id, title, status FROM tasks WHERE id = ?", (PARENT_ID,))
    parent = cur.fetchone()
    if not parent:
        print(f"错误：父任务 {PARENT_ID} 不存在")
        sys.exit(1)
    print(f"父任务：{PARENT_ID} [{parent['status']}] {parent['title']}")
    print()

    created = 0
    for t in tasks_to_mount:
        # 检查是否已存在同名子任务（避免重复创建）
        cur = db.conn.execute(
            "SELECT id, status FROM tasks WHERE title = ? AND parent_id = ?",
            (t["title"], PARENT_ID),
        )
        existing = cur.fetchone()
        if existing:
            print(f"  Exists: {existing['id']} [{existing['status']}] {t['title']}")
            task_id = existing["id"]
        else:
            task_id = db.task_create(
                title=t["title"],
                description=t["desc"],
                parent_id=PARENT_ID,
                steps=[],
            )
            print(f"  Created: {task_id} [{t['status']}] {t['title']}")
            created += 1

        # 已完成的任务直接用 SQL 更新到 closed
        # （无 steps 的任务不走 task_next_step/task_apply 流程）
        if t["status"] == "closed":
            now = time.time()
            db.conn.execute(
                "UPDATE tasks SET status = ?, applied_at = ?, closed_at = ? WHERE id = ?",
                ("closed", now, now, task_id),
            )
            db.conn.commit()
            print(f"    → Closed (已完成)")

    print(f"\n共创建 {created} 个新任务，{len(tasks_to_mount) - created} 个已存在")
    db.conn.close()


if __name__ == "__main__":
    main()
