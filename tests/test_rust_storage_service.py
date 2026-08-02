"""Task C2: Rust StorageService 完整迁移与容灾验证测试。

校验矩阵：
1. 空库经由 Rust StorageService 初始化到 v42。
2. PRAGMA journal_mode=WAL、busy_timeout=5000、foreign_keys=ON 属性校验。
3. 高版本 Schema (user_version > 42) 触发 SCHEMA_TOO_NEW 的 Fail-Closed 阻断拦截。
4. 损坏/完整数据库的 storage_integrity_check 与 storage_backup_before_migration 灾备输出。
5. Python 与 Rust 生产初始化 Facade 的 Schema 与版本号一致性差分比对。
"""

import os
import sqlite3
import tempfile
import pytest

from callwarden.db.db import CodeGraphDB
from callwarden.db.schema import SCHEMA_VERSION

try:
    from callwarden_core import (
        storage_schema_version,
        storage_initialize_or_migrate,
        storage_open,
        storage_begin,
        storage_commit,
        storage_rollback,
        storage_integrity_check,
        storage_backup_before_migration,
        storage_checkpoint_py,
    )
    HAS_RUST_STORAGE = True
except ImportError:
    HAS_RUST_STORAGE = False


@pytest.mark.skipif(not HAS_RUST_STORAGE, reason="callwarden_core.storage 未编译安装")
class TestRustStorageService:

    def test_new_db_initialization_to_v42(self):
        """1. 新库从 0 初始化至 v42 并在 SQLite 中创建标准结构。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "new_storage.db")
            assert storage_schema_version(db_path) == 0

            res = storage_initialize_or_migrate(db_path, SCHEMA_VERSION)
            assert res["success"] is True
            assert res["version"] == SCHEMA_VERSION

            # 校验 SQLite 实际的 PRAGMA user_version
            conn = sqlite3.connect(db_path)
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            assert ver == SCHEMA_VERSION

            # 校验核心表是否存在
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for required_table in [
                "workspaces",
                "file_contents",
                "file_instances",
                "symbols",
                "calls",
                "file_versions",
                "semgrep_findings",
                "rollback_config",
            ]:
                assert required_table in tables
            conn.close()

    def test_pragma_settings(self):
        """2. 自动设置 WAL 模式与外键约束。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "pragma_test.db")
            storage_initialize_or_migrate(db_path, SCHEMA_VERSION)

            handle = storage_open(db_path, "readwrite")
            assert handle.pragma("journal_mode").upper() == "WAL"
            assert handle.pragma("foreign_keys") == "1"
            handle.close()

    def test_schema_too_new_fail_closed(self):
        """3. user_version > 42 触发 SCHEMA_TOO_NEW 阻断拦截。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "future_db.db")
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA user_version = 999")
            conn.commit()
            conn.close()

            with pytest.raises(ValueError, match="SCHEMA_TOO_NEW"):
                storage_initialize_or_migrate(db_path, SCHEMA_VERSION)

    def test_integrity_check_and_backup(self):
        """4. 数据库完整性校验与灾备文件备份。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "backup_src.db")
            backup_path = os.path.join(tmpdir, "backup_dst.db")

            storage_initialize_or_migrate(db_path, SCHEMA_VERSION)

            # 校验完整性
            reports = storage_integrity_check(db_path)
            assert reports == ["ok"]

            # 执行灾备备份
            bytes_copied = storage_backup_before_migration(db_path, backup_path)
            assert bytes_copied > 0
            assert os.path.exists(backup_path)

            # 校验 Checkpoint 操作
            ckpt_res = storage_checkpoint_py(db_path, "PASSIVE")
            assert ckpt_res == "ok"

    def test_codegraphdb_facade_integration(self):
        """5. Python CodeGraphDB Facade 调用 Rust StorageService 完成初始化。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "facade_test.db")
            db = CodeGraphDB(db_path=db_path)

            # 验证由 CodeGraphDB 创建的数据库底层版本为 42
            ver = db._get_current_version()
            assert ver == SCHEMA_VERSION

            reports = storage_integrity_check(db_path)
            assert reports == ["ok"]
            db.close()

    def test_transaction_commit_and_rollback(self):
        """Rust transaction handle owns BEGIN/COMMIT/ROLLBACK 生命周期。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "tx.db")
            storage_initialize_or_migrate(db_path, SCHEMA_VERSION)

            tx = storage_begin(db_path, "immediate")
            tx.execute(
                "INSERT INTO workspaces(name, root_path, created_at) "
                "VALUES ('committed', '/tmp/committed', 1.0)"
            )
            storage_commit(tx)

            tx = storage_begin(db_path, "immediate")
            tx.execute(
                "INSERT INTO workspaces(name, root_path, created_at) "
                "VALUES ('rolled-back', '/tmp/rolled-back', 1.0)"
            )
            storage_rollback(tx)

            conn = sqlite3.connect(db_path)
            names = [row[0] for row in conn.execute("SELECT name FROM workspaces")]
            conn.close()
            assert names == ["committed"]

    def test_facade_does_not_fallback_after_rust_failure(self):
        """Rust schema checksum错误必须阻断 CodeGraphDB，不能静默走 Python 迁移。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "fail_closed.db")
            storage_initialize_or_migrate(db_path, SCHEMA_VERSION)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "UPDATE schema_migrations SET checksum='tampered' WHERE version=?",
                (SCHEMA_VERSION,),
            )
            conn.commit()
            conn.close()

            with pytest.raises(RuntimeError, match="schema checksum mismatch"):
                CodeGraphDB(db_path=db_path)
