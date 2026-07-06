"""symbols 表 UNIQUE 索引 + UPSERT 测试。

覆盖 A2 任务（T-1783349079760-7dda）：验证 v26 schema 迁移和 UPSERT 行为。

测试内容：
- SCHEMA_VERSION == 26
- UNIQUE 索引 idx_symbols_unique 存在
- v25->v26 迁移清理重复行
- UPSERT：相同 (file_instance_id, name, start_line) 不产生重复行
- UPSERT：冲突时更新字段（symbol_hash / kind / signature 等）
"""

import os
import sqlite3
import tempfile

import pytest

from callwarden.db.schema import SCHEMA_VERSION, SCHEMA_SQL
from callwarden.db.db_base import (
    CodeGraphBase,
    _migrate_v25_to_v26,
)


def _make_v25_db(db_path: str):
    """构造一个停留在 v25 的数据库（不跑 v26 迁移）。"""
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    # SCHEMA_SQL 已含 v26 的 UNIQUE 索引，需要先 DROP 掉以便测试迁移
    conn.execute("DROP INDEX IF EXISTS idx_symbols_unique")
    # 插入默认 workspace
    conn.execute(
        "INSERT INTO workspaces (name, root_path, created_at, is_active) "
        "VALUES ('test', '/tmp', 0, 1)"
    )
    conn.commit()
    return conn


def _insert_symbol_content(conn, content_hash, name="foo", kind="fn"):
    """辅助：插入 symbol_contents 行（使用正确列名）。"""
    conn.execute(
        "INSERT INTO symbol_contents (content_hash, name, kind, content, signature, "
        "has_comment, comment_content, qualified_name) "
        "VALUES (?, ?, ?, 'x', '', 0, '', ?)",
        (content_hash, name, kind, name),
    )


def test_schema_version_is_26():
    """SCHEMA_VERSION 应为 26。"""
    assert SCHEMA_VERSION == 26


def test_unique_index_exists_in_schema_sql():
    """SCHEMA_SQL 应包含 idx_symbols_unique 定义。"""
    assert "idx_symbols_unique" in SCHEMA_SQL
    assert "UNIQUE INDEX" in SCHEMA_SQL


def test_migration_v25_to_v26_creates_unique_index():
    """v25->v26 迁移应创建 UNIQUE 索引。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = _make_v25_db(db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_symbols_unique'"
            ).fetchall()
            assert rows == []
            _migrate_v25_to_v26(conn)
            conn.commit()
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_symbols_unique'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()


def test_migration_cleans_duplicate_rows():
    """v25->v26 迁移应清理重复的 symbols 行。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = _make_v25_db(db_path)
        try:
            conn.execute(
                "INSERT INTO file_instances (workspace_id, rel_path, abs_path, mtime, status) "
                "VALUES (1, 'test.py', '/tmp/test.py', 0, 'parsed')"
            )
            fi_id = conn.execute("SELECT id FROM file_instances").fetchone()[0]
            _insert_symbol_content(conn, "hash1", "foo", "fn")
            # 插入两条重复的 symbols 行
            conn.execute(
                "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, "
                "start_line, end_line) VALUES (?, 'hash1', 'foo', 'fn', 1, 10)",
                (fi_id,),
            )
            conn.execute(
                "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, "
                "start_line, end_line) VALUES (?, 'hash1', 'foo', 'fn', 1, 10)",
                (fi_id,),
            )
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            assert count == 2
            _migrate_v25_to_v26(conn)
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            assert count == 1
        finally:
            conn.close()


def test_migration_is_idempotent():
    """v25->v26 迁移应可重复执行（幂等）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = _make_v25_db(db_path)
        try:
            _migrate_v25_to_v26(conn)
            conn.commit()
            _migrate_v25_to_v26(conn)
            conn.commit()
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_symbols_unique'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()


def test_upsert_does_not_create_duplicate():
    """UPSERT 不应创建重复行。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = _make_v25_db(db_path)
        try:
            _migrate_v25_to_v26(conn)
            conn.commit()
            conn.execute(
                "INSERT INTO file_instances (workspace_id, rel_path, abs_path, mtime, status) "
                "VALUES (1, 'test.py', '/tmp/test.py', 0, 'parsed')"
            )
            fi_id = conn.execute("SELECT id FROM file_instances").fetchone()[0]
            _insert_symbol_content(conn, "hash1", "foo", "fn")
            _insert_symbol_content(conn, "hash2", "foo", "fn")
            # 第一次插入
            conn.execute(
                "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, "
                "start_line, end_line) VALUES (?, 'hash1', 'foo', 'fn', 1, 10) "
                "ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET "
                "symbol_hash = excluded.symbol_hash",
                (fi_id,),
            )
            # 第二次插入相同 key（应 UPDATE 而非 INSERT）
            conn.execute(
                "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, "
                "start_line, end_line) VALUES (?, 'hash2', 'foo', 'fn', 1, 10) "
                "ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET "
                "symbol_hash = excluded.symbol_hash",
                (fi_id,),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT COUNT(*), symbol_hash FROM symbols WHERE name='foo'"
            ).fetchone()
            assert rows[0] == 1
            assert rows[1] == "hash2"
        finally:
            conn.close()


def test_upsert_without_conflict_inserts_normally():
    """UPSERT 在无冲突时应正常插入。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = _make_v25_db(db_path)
        try:
            _migrate_v25_to_v26(conn)
            conn.commit()
            conn.execute(
                "INSERT INTO file_instances (workspace_id, rel_path, abs_path, mtime, status) "
                "VALUES (1, 'test.py', '/tmp/test.py', 0, 'parsed')"
            )
            fi_id = conn.execute("SELECT id FROM file_instances").fetchone()[0]
            _insert_symbol_content(conn, "hash1", "foo", "fn")
            _insert_symbol_content(conn, "hash2", "bar", "fn")
            # 插入两个不同的 symbol（不同 name/start_line）
            conn.execute(
                "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, "
                "start_line, end_line) VALUES (?, 'hash1', 'foo', 'fn', 1, 10) "
                "ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET "
                "symbol_hash = excluded.symbol_hash",
                (fi_id,),
            )
            conn.execute(
                "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, "
                "start_line, end_line) VALUES (?, 'hash2', 'bar', 'fn', 20, 30) "
                "ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET "
                "symbol_hash = excluded.symbol_hash",
                (fi_id,),
            )
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            assert count == 2
        finally:
            conn.close()


def test_full_db_init_creates_unique_index():
    """CodeGraphDB 完整初始化应创建 UNIQUE 索引。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = os.path.join(tmpdir, "project")
        os.makedirs(root)
        db_path = os.path.join(tmpdir, "test.db")
        db = CodeGraphBase(db_path=db_path, workspace_root=root)
        try:
            rows = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_symbols_unique'"
            ).fetchall()
            assert len(rows) == 1
            # 验证 schema_version（表名为 schema_version）
            ver = db.conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            assert ver[0] == 26
        finally:
            db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
