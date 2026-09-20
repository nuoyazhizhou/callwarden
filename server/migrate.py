"""migrate.py — 旧版多库 → 用户级单库迁移（db/ 退休 phase-3 本地化）

原 callwarden/db/db_migrate.py 的 migrate_to_single_db 搬迁至本模块，
去除对 callwarden.db（CodeGraphDB / db_migrate）的依赖：

- schema 初始化改用 Rust ``callwarden_core.sqlite_migrate_schema``（幂等，
  返回 committed version），旧 pyd 缺失时退化为“文件已存在即跳过”。
- 其余迁移逻辑（ATTACH 旧库 + workspaces/tasks/task_steps 去重合并）逐行
  保持原语义。

迁移策略（不变）：
- workspaces 表：按 root_path 去重合并（INSERT OR IGNORE 语义）
- tasks / task_steps 表：全局表，按 id 去重合并
- file_instances / symbols / calls 等符号图谱数据：不迁移，建议用户
  迁移后运行 `cw refresh --all` 重建（符号图谱是可重建的派生数据）
- 旧 <hash>/ 目录保留，不删除（用户确认迁移成功后手动删除）
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from typing import Any, Dict, List

from callwarden.config import (
    CALLWARDEN_DIR,
    DB_PATH,
    list_legacy_hash_db_dirs,
)

logger = logging.getLogger(__name__)

try:
    import callwarden_core as _callwarden_core  # type: ignore
except ImportError:
    _callwarden_core = None


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


def _ensure_schema() -> None:
    """确保统一库 schema 存在（Rust 优先，回退文件存在判断）。

    原 db_migrate 通过 ``CodeGraphDB(...)`` 打开触发 schema 初始化；
    db/ 退休后改用 Rust ``sqlite_migrate_schema``（幂等）。
    """
    _rust_migrate = getattr(_callwarden_core, "sqlite_migrate_schema", None) if _callwarden_core else None
    if _rust_migrate is not None:
        _rust_migrate(DB_PATH)
        return
    # 旧 pyd 缺该函数：daemon 已运行时库与 schema 必然存在，跳过即可
    if not os.path.isfile(DB_PATH):
        raise RuntimeError(
            f"统一库不存在且 Rust sqlite_migrate_schema 不可用：{DB_PATH}；"
            "请先启动 daemon 或升级 callwarden_core。"
        )


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

    # 2. 确保统一库存在（Rust schema 迁移，替代原 CodeGraphDB 初始化）
    os.makedirs(CALLWARDEN_DIR, exist_ok=True)
    _ensure_schema()

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
