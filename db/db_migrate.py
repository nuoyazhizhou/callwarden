"""db_migrate.py
============

数据库迁移工具：旧版多库架构（~/.callwarden/<hash>/callwarden.db）
迁移到用户级单库架构（~/.callwarden/callwarden.db）。

迁移策略：
- workspaces 表：按 root_path 去重合并（INSERT OR IGNORE）
- tasks / task_steps 表：全局表，直接 INSERT OR IGNORE 合并
- file_instances / symbols / calls 等符号图谱数据：不迁移，建议用户
  迁移后运行 `cw refresh --all` 重建（符号图谱是可重建的派生数据）
- 旧 <hash>/ 目录保留，不删除（用户确认迁移成功后手动删除）

使用方式：
    from callwarden.db.db_migrate import migrate_to_single_db
    result = migrate_to_single_db(dry_run=True)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from typing import Any, Dict, List, Optional

from ..config import (
    CALLWARDEN_DIR,
    DB_PATH,
    list_legacy_hash_db_dirs,
)


def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    """获取表的列名列表"""
    cur = conn.execute(f"PRAGMA table_info({table_name})")
    return [row["name"] for row in cur.fetchall()]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """检查表是否存在"""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cur.fetchone() is not None


def migrate_to_single_db(
    dry_run: bool = True,
    backup: bool = True,
) -> Dict[str, Any]:
    """将旧版多库架构的数据迁移到用户级单库

    迁移内容：
    1. workspaces 表 — 按 root_path 去重合并
    2. tasks 表 — 全局任务记录合并
    3. task_steps 表 — 任务步骤合并

    不迁移：file_instances / symbols / calls 等符号图谱数据（可重建）
    建议迁移后运行 `cw refresh --all` 重建符号图谱。

    Args:
        dry_run: True 只预览不写入，False 实际迁移
        backup: True 在迁移前备份统一库（若已存在）

    Returns:
        迁移结果摘要 dict：
        {
            "legacy_dbs": ["/path/to/hash1", ...],
            "migrated_workspaces": int,
            "migrated_tasks": int,
            "migrated_steps": int,
            "skipped_workspaces": int,  # root_path 已存在跳过
            "backup_path": str,
            "dry_run": bool,
        }
    """
    result: Dict[str, Any] = {
        "legacy_dbs": [],
        "migrated_workspaces": 0,
        "migrated_tasks": 0,
        "migrated_steps": 0,
        "skipped_workspaces": 0,
        "backup_path": "",
        "dry_run": dry_run,
        "errors": [],
    }

    # 1. 扫描旧库
    legacy_dirs = list_legacy_hash_db_dirs()
    result["legacy_dbs"] = legacy_dirs
    if not legacy_dirs:
        result["errors"].append("未找到旧版 hash 目录（~/.callwarden/<16位hex>/）")
        return result

    # 2. 确保统一库存在（通过 CodeGraphDB 初始化 schema）
    os.makedirs(CALLWARDEN_DIR, exist_ok=True)
    from .db import CodeGraphDB
    # 用 CodeGraphDB 打开统一库，触发 schema 初始化
    db = CodeGraphDB(db_path=DB_PATH, workspace_root="")
    db.close()

    # 3. 备份统一库（如果已存在且非 dry_run）
    if backup and not dry_run and os.path.isfile(DB_PATH):
        backup_path = f"{DB_PATH}.backup_{int(time.time())}"
        shutil.copy2(DB_PATH, backup_path)
        result["backup_path"] = backup_path

    # 4. 连接统一库
    main_conn = sqlite3.connect(DB_PATH)
    main_conn.row_factory = sqlite3.Row

    try:
        # 需要迁移的表（全局表，不带 workspace_id 或 workspace_id 可重映射）
        # workspaces: 按 root_path 去重
        # tasks / task_steps: 全局表，按 id 去重
        migrate_tables = ["workspaces", "tasks", "task_steps"]

        for legacy_dir in legacy_dirs:
            legacy_db_path = os.path.join(legacy_dir, "callwarden.db")
            if not os.path.isfile(legacy_db_path):
                continue

            # ATTACH 旧库
            attach_name = f"legacy_{os.path.basename(legacy_dir)}"
            try:
                main_conn.execute(f"ATTACH DATABASE ? AS {attach_name}", (legacy_db_path,))
            except sqlite3.OperationalError as e:
                result["errors"].append(f"ATTACH {legacy_db_path} 失败: {e}")
                continue

            try:
                # 4.1 迁移 workspaces 表（按 root_path 去重）
                if _table_exists(main_conn, f"{attach_name}.workspaces"):
                    cur = main_conn.execute(
                        f"SELECT * FROM {attach_name}.workspaces"
                    )
                    for row in cur.fetchall():
                        root_path = row["root_path"] or ""
                        if not root_path:
                            continue
                        # 检查是否已存在（按 root_path 去重）
                        exists = main_conn.execute(
                            "SELECT 1 FROM workspaces WHERE root_path = ?",
                            (root_path,),
                        ).fetchone()
                        if exists:
                            result["skipped_workspaces"] += 1
                            continue
                        if not dry_run:
                            main_conn.execute(
                                "INSERT INTO workspaces "
                                "(name, root_path, created_at, is_active, description, active_task_id) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    row["name"],
                                    root_path,
                                    row["created_at"],
                                    0,  # is_active 设为 0，避免多 workspace 互相冲突
                                    row["description"] or "",
                                    row["active_task_id"] or "",
                                ),
                            )
                        result["migrated_workspaces"] += 1

                # 4.2 迁移 tasks 表（全局表，按 id 去重）
                if _table_exists(main_conn, f"{attach_name}.tasks"):
                    cur = main_conn.execute(f"SELECT * FROM {attach_name}.tasks")
                    cols = _get_table_columns(main_conn, "tasks")
                    col_list = ", ".join(cols)
                    placeholders = ", ".join(["?"] * len(cols))
                    for row in cur.fetchall():
                        task_id = row["id"] if "id" in row.keys() else None
                        if not task_id:
                            continue
                        exists = main_conn.execute(
                            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
                        ).fetchone()
                        if exists:
                            continue
                        if not dry_run:
                            values = [row[c] for c in cols]
                            main_conn.execute(
                                f"INSERT INTO tasks ({col_list}) VALUES ({placeholders})",
                                values,
                            )
                        result["migrated_tasks"] += 1

                # 4.3 迁移 task_steps 表（按 id 去重）
                if _table_exists(main_conn, f"{attach_name}.task_steps"):
                    cur = main_conn.execute(f"SELECT * FROM {attach_name}.task_steps")
                    cols = _get_table_columns(main_conn, "task_steps")
                    col_list = ", ".join(cols)
                    placeholders = ", ".join(["?"] * len(cols))
                    for row in cur.fetchall():
                        step_id = row["id"] if "id" in row.keys() else None
                        if not step_id:
                            continue
                        exists = main_conn.execute(
                            "SELECT 1 FROM task_steps WHERE id = ?", (step_id,)
                        ).fetchone()
                        if exists:
                            continue
                        if not dry_run:
                            values = [row[c] for c in cols]
                            main_conn.execute(
                                f"INSERT INTO task_steps ({col_list}) VALUES ({placeholders})",
                                values,
                            )
                        result["migrated_steps"] += 1

            finally:
                # DETACH 旧库
                try:
                    main_conn.execute(f"DETACH DATABASE {attach_name}")
                except sqlite3.OperationalError:
                    pass

        if not dry_run:
            main_conn.commit()

    finally:
        main_conn.close()

    return result
