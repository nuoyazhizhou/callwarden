"""bootstrap capture 闭环测试（v25 schema）

验证 workspace_scan_runs 表的 schema 与迁移：
1. SCHEMA_VERSION == 25
2. 新库直接包含 workspace_scan_runs 表（无需迁移）
3. 三个索引存在：workspace / task / git_head
4. v24 -> v25 迁移幂等（CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS）
5. 旧 v24 库通过 _init_schema 自动迁移到 v25
6. 字段完整性（17 个字段）
7. 默认值正确（purpose='bootstrap', baseline_type='git', status='running', changed_files_json='[]' 等）
"""
import os
import sqlite3
import tempfile

from callwarden.db.schema import SCHEMA_VERSION
from callwarden.db.db import CodeGraphDB


# ----------------------------------------------------------------------
# 基础断言
# ----------------------------------------------------------------------

def test_schema_version_is_25():
    """SCHEMA_VERSION 常量已升级到 25。"""
    assert SCHEMA_VERSION == 25


def test_new_db_has_workspace_scan_runs_table():
    """新库直接包含 workspace_scan_runs 表（无需迁移）。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            cur = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='workspace_scan_runs'"
            )
            assert cur.fetchone() is not None, "workspace_scan_runs 表不存在"
        finally:
            db.close()


def test_workspace_scan_runs_indexes_exist():
    """workspace_scan_runs 表有三个索引：workspace、task、git_head。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            cur = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_workspace_scan_runs_%'"
            )
            names = {row[0] for row in cur.fetchall()}
            assert "idx_workspace_scan_runs_workspace" in names
            assert "idx_workspace_scan_runs_task" in names
            assert "idx_workspace_scan_runs_git_head" in names
        finally:
            db.close()


# ----------------------------------------------------------------------
# 字段与默认值
# ----------------------------------------------------------------------

def test_workspace_scan_runs_columns():
    """workspace_scan_runs 表字段完整（17 个字段）。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            cur = db.conn.execute("PRAGMA table_info(workspace_scan_runs)")
            cols = {row[1] for row in cur.fetchall()}
            expected = {
                "id", "workspace_id", "purpose", "task_id", "step_id",
                "baseline_type", "git_head", "git_merge_base", "git_status_hash",
                "root_mtime", "file_count", "manifest_hash", "changed_files_json",
                "metadata_json", "started_at", "completed_at", "status",
            }
            missing = expected - cols
            assert not missing, f"缺少字段: {missing}"
        finally:
            db.close()


def test_workspace_scan_runs_defaults():
    """workspace_scan_runs 表默认值正确。

    验证：purpose='bootstrap', baseline_type='git', git_head='', git_merge_base='',
    git_status_hash='', root_mtime=0, file_count=0, manifest_hash='',
    changed_files_json='[]', metadata_json='{}', completed_at=NULL, status='running'
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            # 取 workspace_id
            cur = db.conn.execute(
                "SELECT id FROM workspaces WHERE is_active = 1 LIMIT 1"
            )
            row = cur.fetchone()
            workspace_id = row[0] if row else 1

            # 仅插入必填字段，其余走默认值
            db.conn.execute(
                "INSERT INTO workspace_scan_runs (workspace_id, started_at) VALUES (?, ?)",
                (workspace_id, 1.0),
            )
            db.conn.commit()

            cur = db.conn.execute(
                "SELECT purpose, task_id, step_id, baseline_type, git_head, "
                "       git_merge_base, git_status_hash, root_mtime, file_count, "
                "       manifest_hash, changed_files_json, metadata_json, "
                "       completed_at, status "
                "FROM workspace_scan_runs WHERE workspace_id = ?",
                (workspace_id,),
            )
            r = cur.fetchone()
            assert r[0] == "bootstrap", f"purpose 默认值应为 'bootstrap', 实际: {r[0]}"
            assert r[1] == "", f"task_id 默认值应为 '', 实际: {r[1]}"
            assert r[2] == "", f"step_id 默认值应为 '', 实际: {r[2]}"
            assert r[3] == "git", f"baseline_type 默认值应为 'git', 实际: {r[3]}"
            assert r[4] == "", f"git_head 默认值应为 '', 实际: {r[4]}"
            assert r[5] == "", f"git_merge_base 默认值应为 '', 实际: {r[5]}"
            assert r[6] == "", f"git_status_hash 默认值应为 '', 实际: {r[6]}"
            assert r[7] == 0, f"root_mtime 默认值应为 0, 实际: {r[7]}"
            assert r[8] == 0, f"file_count 默认值应为 0, 实际: {r[8]}"
            assert r[9] == "", f"manifest_hash 默认值应为 '', 实际: {r[9]}"
            assert r[10] == "[]", f"changed_files_json 默认值应为 '[]', 实际: {r[10]}"
            assert r[11] == "{}", f"metadata_json 默认值应为 '{{}}', 实际: {r[11]}"
            assert r[12] is None, f"completed_at 默认值应为 NULL, 实际: {r[12]}"
            assert r[13] == "running", f"status 默认值应为 'running', 实际: {r[13]}"
        finally:
            db.close()


# ----------------------------------------------------------------------
# 迁移幂等性
# ----------------------------------------------------------------------

def test_v25_migration_idempotent():
    """v24 -> v25 迁移幂等：重复执行不报错，表不重复创建。"""
    from callwarden.db.db_base import _migrate_v24_to_v25

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = sqlite3.connect(db_path)
        # 构造一个最小 v24 库（含 workspaces 和 schema_version 表）
        conn.execute("CREATE TABLE workspaces (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER, applied_at REAL, description TEXT)"
        )
        conn.execute("INSERT INTO schema_version VALUES (24, 0, 'v24')")
        conn.commit()

        # 第一次迁移
        _migrate_v24_to_v25(conn)
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='workspace_scan_runs'"
        )
        assert cur.fetchone()[0] == 1

        # 第二次迁移（幂等，IF NOT EXISTS 保证不报错）
        _migrate_v24_to_v25(conn)
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='workspace_scan_runs'"
        )
        assert cur.fetchone()[0] == 1

        # 索引也不重复
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_workspace_scan_runs_%'"
        )
        assert cur.fetchone()[0] == 3

        conn.close()


def test_legacy_v24_db_migrates_to_v25():
    """旧 v24 库通过 _init_schema 自动迁移到 v25。

    先建一个完整 v25 库（CodeGraphDB 打开即 v25），降级版本号到 24 并删除
    workspace_scan_runs 表模拟旧库，再重新打开触发 v24→v25 迁移。
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        # 先建一个完整 v25 库
        db1 = CodeGraphDB(db_path=db_path, workspace_root=tmp)
        db1.close()

        # 降级到 v24：删除 workspace_scan_runs 表 + 索引，回退版本号
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM schema_version WHERE version = 25")
        conn.execute("DROP TABLE IF EXISTS workspace_scan_runs")
        conn.execute("DROP INDEX IF EXISTS idx_workspace_scan_runs_workspace")
        conn.execute("DROP INDEX IF EXISTS idx_workspace_scan_runs_task")
        conn.execute("DROP INDEX IF EXISTS idx_workspace_scan_runs_git_head")
        conn.commit()
        conn.close()

        # 重新打开触发 v24 -> v25 迁移
        db = CodeGraphDB(db_path=db_path, workspace_root=tmp)
        try:
            v = db.conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            assert v["version"] == 25

            cur = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='workspace_scan_runs'"
            )
            assert cur.fetchone() is not None

            # 索引也被重建
            cur = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_workspace_scan_runs_%'"
            )
            names = {row[0] for row in cur.fetchall()}
            assert "idx_workspace_scan_runs_workspace" in names
            assert "idx_workspace_scan_runs_task" in names
            assert "idx_workspace_scan_runs_git_head" in names
        finally:
            db.close()
