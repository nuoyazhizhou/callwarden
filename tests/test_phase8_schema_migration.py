"""Phase 8.6: schema migration 测试。

测试覆盖：
1. SchemaMigrator：注册、版本查询、应用迁移、幂等性
2. MigrationSpec / MigrationResult：数据类行为
3. registry.db 迁移：v1-v3 各阶段
4. audit.db 迁移：v1-v2 各阶段
5. migrate_daemon_dbs：统一入口
6. validate_daemon_dbs：schema 校验
7. 事务回滚：迁移失败时回滚
8. 边界情况：空迁移、重复注册、版本跳跃
"""

import os
import sqlite3
import pytest

from server.schema_migrator import (
    SchemaMigrator,
    MigrationSpec,
    MigrationResult,
    get_registry_migrations,
    get_audit_migrations,
    migrate_daemon_dbs,
    validate_daemon_dbs,
)
from server.daemon_config import DaemonConfig


# ======================================================================
# SchemaMigrator 基础测试
# ======================================================================


class TestSchemaMigratorBasic:
    """SchemaMigrator 基础功能测试。"""

    def test_register_migration(self, tmp_path):
        """注册迁移后应能在 target_version 中反映。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "init", lambda conn: None)
        assert m.target_version == 1

    def test_register_multiple(self, tmp_path):
        """注册多个迁移后 target_version 取最大值。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: None)
        m.register_migration(2, "v2", lambda c: None)
        m.register_migration(3, "v3", lambda c: None)
        assert m.target_version == 3

    def test_register_invalid_version_zero(self, tmp_path):
        """version <= 0 应拒绝。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        with pytest.raises(ValueError):
            m.register_migration(0, "zero", lambda c: None)

    def test_register_invalid_version_negative(self, tmp_path):
        """负数 version 应拒绝。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        with pytest.raises(ValueError):
            m.register_migration(-1, "neg", lambda c: None)

    def test_register_duplicate_version(self, tmp_path):
        """重复注册同一 version 应拒绝。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: None)
        with pytest.raises(ValueError):
            m.register_migration(1, "dup", lambda c: None)

    def test_register_migrations_batch(self, tmp_path):
        """批量注册迁移。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        specs = [
            MigrationSpec(1, "v1", lambda c: None),
            MigrationSpec(2, "v2", lambda c: None),
        ]
        m.register_migrations(specs)
        assert m.target_version == 2

    def test_empty_migrator_target_version(self, tmp_path):
        """未注册任何迁移时 target_version=0。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        assert m.target_version == 0


class TestGetCurrentVersion:
    """get_current_version 只读查询测试。"""

    def test_fresh_db_returns_zero(self, tmp_path):
        """全新 DB（无 schema_version 表）返回 0。"""
        db_path = str(tmp_path / "fresh.db")
        m = SchemaMigrator(db_path)
        assert m.get_current_version() == 0

    def test_nonexistent_db_returns_zero(self, tmp_path):
        """不存在的 DB 文件返回 0。

        注意：sqlite3.connect 会创建空文件，因此无法用文件不存在来
        区分"全新数据库"——但 schema_version 表不存在时仍返回 0。
        """
        db_path = str(tmp_path / "nonexistent.db")
        m = SchemaMigrator(db_path)
        assert m.get_current_version() == 0

    def test_after_migration(self, tmp_path):
        """应用迁移后版本应更新。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "init", lambda c: c.execute("CREATE TABLE t (id INTEGER)"))
        m.apply_migrations()
        assert m.get_current_version() == 1


class TestApplyMigrations:
    """apply_migrations 写操作测试。"""

    def test_apply_single_migration(self, tmp_path):
        """应用单个迁移。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "create table", lambda c: c.execute("CREATE TABLE foo (id INTEGER)"))
        result = m.apply_migrations()

        assert result.status == "migrated"
        assert result.from_version == 0
        assert result.to_version == 1
        assert result.applied == [1]
        assert result.failed is None
        assert result.error is None

    def test_apply_multiple_migrations(self, tmp_path):
        """按版本顺序应用多个迁移。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: c.execute("CREATE TABLE t1 (id INTEGER)"))
        m.register_migration(2, "v2", lambda c: c.execute("CREATE TABLE t2 (id INTEGER)"))
        m.register_migration(3, "v3", lambda c: c.execute("CREATE TABLE t3 (id INTEGER)"))
        result = m.apply_migrations()

        assert result.status == "migrated"
        assert result.applied == [1, 2, 3]
        assert result.to_version == 3

    def test_apply_idempotent(self, tmp_path):
        """已应用的迁移不会重复执行。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: c.execute("CREATE TABLE t (id INTEGER)"))

        r1 = m.apply_migrations()
        assert r1.applied == [1]

        r2 = m.apply_migrations()
        assert r2.status == "up_to_date"
        assert r2.applied == []

    def test_apply_partial_then_resume(self, tmp_path):
        """部分迁移后再次调用应从断点续跑。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: c.execute("CREATE TABLE t1 (id INTEGER)"))
        m.register_migration(2, "v2", lambda c: c.execute("CREATE TABLE t2 (id INTEGER)"))

        r1 = m.apply_migrations()
        assert r1.to_version == 2

        # 注册新迁移
        m.register_migration(3, "v3", lambda c: c.execute("CREATE TABLE t3 (id INTEGER)"))
        r2 = m.apply_migrations()
        assert r2.from_version == 2
        assert r2.applied == [3]
        assert r2.to_version == 3

    def test_migration_creates_schema_version_table(self, tmp_path):
        """迁移后应自动创建 schema_version 表。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: None)
        m.apply_migrations()

        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "schema_version" in tables

    def test_migration_records_history(self, tmp_path):
        """迁移历史应记录在 schema_version 表中。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "first migration", lambda c: None)
        m.register_migration(2, "second migration", lambda c: None)
        m.apply_migrations()

        history = m.get_migration_history()
        assert len(history) == 2
        assert history[0]["version"] == 1
        assert history[0]["description"] == "first migration"
        assert history[1]["version"] == 2
        assert history[1]["description"] == "second migration"

    def test_no_migrations_registered(self, tmp_path):
        """未注册任何迁移时返回 up_to_date。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        result = m.apply_migrations()
        assert result.status == "up_to_date"
        assert result.applied == []

    def test_get_pending_versions(self, tmp_path):
        """获取待应用版本列表。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: None)
        m.register_migration(2, "v2", lambda c: None)
        m.register_migration(3, "v3", lambda c: None)

        # 全新 DB，所有迁移都待应用
        assert m.get_pending_versions() == [1, 2, 3]

        # 应用 v1 后
        m.apply_migrations()
        assert m.get_pending_versions() == []


# ======================================================================
# 事务回滚测试
# ======================================================================


class TestMigrationRollback:
    """迁移失败时的事务回滚测试。"""

    def test_failed_migration_rolls_back(self, tmp_path):
        """迁移失败时应回滚，不留下半成品。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)

        def failing_migration(conn):
            conn.execute("CREATE TABLE will_fail (id INTEGER)")
            raise RuntimeError("simulated failure")

        m.register_migration(1, "v1", lambda c: c.execute("CREATE TABLE t1 (id INTEGER)"))
        m.register_migration(2, "failing", failing_migration)

        result = m.apply_migrations()
        assert result.status == "failed"
        assert result.failed == 2
        assert "simulated failure" in result.error

        # v1 应该已应用（在 v2 失败前 commit 了）
        # 注意：每个迁移在单独事务中，v1 已 commit
        assert m.get_current_version() == 1

        # will_fail 表不应存在（v2 回滚了）
        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "will_fail" not in tables
        assert "t1" in tables

    def test_failed_migration_stops_subsequent(self, tmp_path):
        """迁移失败后不应继续执行后续迁移。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: c.execute("CREATE TABLE t1 (id INTEGER)"))
        m.register_migration(2, "failing", lambda c: (_ for _ in ()).throw(RuntimeError("fail")))
        m.register_migration(3, "v3", lambda c: c.execute("CREATE TABLE t3 (id INTEGER)"))

        result = m.apply_migrations()
        assert result.failed == 2

        # v3 不应被执行
        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "t3" not in tables


# ======================================================================
# registry.db 迁移测试
# ======================================================================


class TestRegistryMigrations:
    """registry.db 的迁移测试。"""

    def test_registry_v1_creates_tables(self, tmp_path):
        """v1 迁移应创建 daemon_workspaces 等表。"""
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())
        m.apply_migrations()

        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()

        assert "daemon_workspaces" in tables
        assert "container_mount_mappings" in tables
        assert "daemon_state" in tables

    def test_registry_v1_creates_indexes(self, tmp_path):
        """v1 迁移应创建索引。"""
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())
        m.apply_migrations()

        conn = sqlite3.connect(db_path)
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        conn.close()

        assert "idx_workspaces_owner" in indexes
        assert "idx_workspaces_snapshot" in indexes
        assert "idx_workspaces_status" in indexes

    def test_registry_v2_creates_backup_history(self, tmp_path):
        """v2 迁移应创建 backup_history 表。"""
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())
        m.apply_migrations()

        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()

        assert "backup_history" in tables

    def test_registry_v3_creates_migrations_log(self, tmp_path):
        """v3 迁移应创建 schema_migrations_log 表。"""
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())
        m.apply_migrations()

        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()

        assert "schema_migrations_log" in tables

    def test_registry_full_migration_version(self, tmp_path):
        """完整迁移后版本应为 3。"""
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())
        result = m.apply_migrations()

        assert result.to_version == 3
        assert result.applied == [1, 2, 3]

    def test_registry_tables_usable(self, tmp_path):
        """迁移后表应该可正常读写。"""
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())
        m.apply_migrations()

        conn = sqlite3.connect(db_path)
        # 插入 workspace
        conn.execute("""
            INSERT INTO daemon_workspaces
            (workspace_instance_id, owner_uid, client_view_root,
             host_real_root, registered_at, last_active_at)
            VALUES ('ws-1', 1000, '/view', '/host', 12345.0, 12345.0)
        """)
        conn.commit()

        # 查询
        row = conn.execute(
            "SELECT * FROM daemon_workspaces WHERE workspace_instance_id = ?",
            ("ws-1",)
        ).fetchone()
        assert row is not None

        # 插入 backup_history
        conn.execute("""
            INSERT INTO backup_history
            (backup_id, backup_type, created_at, file_count, total_size_bytes, checksum)
            VALUES ('B-001', 'full', 12345.0, 5, 1024, 'abc123')
        """)
        conn.commit()
        conn.close()


# ======================================================================
# audit.db 迁移测试
# ======================================================================


class TestAuditMigrations:
    """audit.db 的迁移测试。"""

    def test_audit_v1_creates_table(self, tmp_path):
        """v1 迁移应创建 audit_log 表。"""
        db_path = str(tmp_path / "audit.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_audit_migrations())
        m.apply_migrations()

        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()

        assert "audit_log" in tables

    def test_audit_v2_creates_indexes(self, tmp_path):
        """v2 迁移应创建索引。"""
        db_path = str(tmp_path / "audit.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_audit_migrations())
        m.apply_migrations()

        conn = sqlite3.connect(db_path)
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        conn.close()

        assert "idx_audit_log_timestamp" in indexes
        assert "idx_audit_log_event_type" in indexes
        assert "idx_audit_log_actor_uid" in indexes
        assert "idx_audit_log_result" in indexes

    def test_audit_full_migration_version(self, tmp_path):
        """完整迁移后版本应为 2。"""
        db_path = str(tmp_path / "audit.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_audit_migrations())
        result = m.apply_migrations()

        assert result.to_version == 2
        assert result.applied == [1, 2]

    def test_audit_table_usable(self, tmp_path):
        """迁移后 audit_log 表应可正常读写。"""
        db_path = str(tmp_path / "audit.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_audit_migrations())
        m.apply_migrations()

        conn = sqlite3.connect(db_path)
        conn.execute("""
            INSERT INTO audit_log
            (event_id, timestamp, event_type, actor_uid, action)
            VALUES ('A-1', 12345.0, 'admin_operation', 0, 'test')
        """)
        conn.commit()
        row = conn.execute("SELECT * FROM audit_log WHERE event_id = ?", ("A-1",)).fetchone()
        assert row is not None
        conn.close()


# ======================================================================
# migrate_daemon_dbs 统一入口测试
# ======================================================================


class TestMigrateDaemonDbs:
    """migrate_daemon_dbs 统一入口测试。"""

    def test_migrate_all_dbs(self, tmp_path):
        """统一入口应迁移 registry 和 audit 两个 DB。"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {
                "admin_uids": [0],
                "audit_log_path": os.path.join(data_root, "audit.db"),
            },
        })

        results = migrate_daemon_dbs(cfg)

        assert "registry" in results
        assert "audit" in results
        assert results["registry"].status == "migrated"
        assert results["audit"].status == "migrated"

    def test_migrate_idempotent(self, tmp_path):
        """二次调用应返回 up_to_date。"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {
                "admin_uids": [0],
                "audit_log_path": os.path.join(data_root, "audit.db"),
            },
        })

        migrate_daemon_dbs(cfg)
        results = migrate_daemon_dbs(cfg)

        assert results["registry"].status == "up_to_date"
        assert results["audit"].status == "up_to_date"

    def test_migrate_without_audit_path(self, tmp_path):
        """未配置 audit_log_path 时只迁移 registry。"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {
                "admin_uids": [0],
                "audit_log_path": "",  # 空 audit 路径
            },
        })

        results = migrate_daemon_dbs(cfg)
        assert "registry" in results
        assert "audit" not in results

    def test_migrate_with_extra_migrators(self, tmp_path):
        """extra_migrators 应被应用。"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {"admin_uids": [0]},
        })

        extra_db = str(tmp_path / "extra.db")
        extra_m = SchemaMigrator(extra_db)
        extra_m.register_migration(1, "extra v1", lambda c: c.execute("CREATE TABLE extra (id INTEGER)"))

        results = migrate_daemon_dbs(cfg, extra_migrators=[extra_m])
        assert "extra_0" in results
        assert results["extra_0"].status == "migrated"


# ======================================================================
# validate_daemon_dbs 测试
# ======================================================================


class TestValidateDaemonDbs:
    """validate_daemon_dbs 校验测试。"""

    def test_validate_before_migration(self, tmp_path):
        """未迁移前校验应报告缺失表。"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {
                "admin_uids": [0],
                "audit_log_path": os.path.join(data_root, "audit.db"),
            },
        })

        results = validate_daemon_dbs(cfg)
        assert results["registry"]["valid"] is False
        assert "daemon_workspaces" in results["registry"]["missing_tables"]

    def test_validate_after_migration(self, tmp_path):
        """迁移后校验应通过。"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {
                "admin_uids": [0],
                "audit_log_path": os.path.join(data_root, "audit.db"),
            },
        })

        migrate_daemon_dbs(cfg)
        results = validate_daemon_dbs(cfg)

        assert results["registry"]["valid"] is True
        assert results["registry"]["missing_tables"] == []
        assert results["audit"]["valid"] is True

    def test_validate_returns_current_version(self, tmp_path):
        """校验结果应包含 current_version。"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {
                "admin_uids": [0],
                "audit_log_path": os.path.join(data_root, "audit.db"),
            },
        })

        migrate_daemon_dbs(cfg)
        results = validate_daemon_dbs(cfg)

        assert results["registry"]["current_version"] == 3  # registry 最新版本
        assert results["audit"]["current_version"] == 2     # audit 最新版本

    def test_validate_skips_nonexistent_audit(self, tmp_path):
        """audit DB 不存在时不校验。"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {
                "admin_uids": [0],
                "audit_log_path": os.path.join(data_root, "audit.db"),
            },
        })

        # 只迁移 registry，不迁移 audit
        results = validate_daemon_dbs(cfg)
        assert "registry" in results
        assert "audit" not in results


# ======================================================================
# MigrationResult 数据类测试
# ======================================================================


class TestMigrationResult:
    """MigrationResult 数据类行为测试。"""

    def test_status_up_to_date(self):
        r = MigrationResult(db_path="x", from_version=1, to_version=1)
        assert r.status == "up_to_date"

    def test_status_migrated(self):
        r = MigrationResult(db_path="x", from_version=0, to_version=2, applied=[1, 2])
        assert r.status == "migrated"

    def test_status_failed(self):
        r = MigrationResult(db_path="x", from_version=0, to_version=0, failed=1, error="boom")
        assert r.status == "failed"

    def test_to_dict(self):
        r = MigrationResult(
            db_path="/tmp/test.db",
            from_version=0,
            to_version=2,
            applied=[1, 2],
        )
        d = r.to_dict()
        assert d["db_path"] == "/tmp/test.db"
        assert d["from_version"] == 0
        assert d["to_version"] == 2
        assert d["applied"] == [1, 2]
        assert d["status"] == "migrated"


# ======================================================================
# 边界情况测试
# ======================================================================


class TestEdgeCases:
    """边界情况测试。"""

    def test_version_gap_ok(self, tmp_path):
        """迁移版本可以不连续（跳号）。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: c.execute("CREATE TABLE t1 (id INTEGER)"))
        m.register_migration(5, "v5", lambda c: c.execute("CREATE TABLE t5 (id INTEGER)"))
        result = m.apply_migrations()

        assert result.applied == [1, 5]
        assert result.to_version == 5

    def test_migration_function_can_use_executescript(self, tmp_path):
        """迁移函数内部可以使用 executescript 批量执行。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)

        def up(conn):
            conn.executescript("""
                CREATE TABLE a (id INTEGER);
                CREATE TABLE b (id INTEGER);
                CREATE INDEX idx_a ON a(id);
            """)

        m.register_migration(1, "multi", up)
        m.apply_migrations()

        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert {"a", "b"} <= tables

    def test_migration_with_data_manipulation(self, tmp_path):
        """迁移可以包含 DML（数据操作）。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "init", lambda c: c.execute("CREATE TABLE t (id INTEGER, val TEXT)"))

        def insert_data(conn):
            conn.execute("INSERT INTO t (id, val) VALUES (1, 'hello')")

        m.register_migration(2, "insert", insert_data)
        m.apply_migrations()

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT val FROM t WHERE id = 1").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "hello"

    def test_get_history_empty(self, tmp_path):
        """全新 DB 的迁移历史为空。"""
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        assert m.get_migration_history() == []

    def test_concurrent_migrators_different_dbs(self, tmp_path):
        """不同 DB 的 migrator 互不干扰。"""
        db1 = str(tmp_path / "db1.db")
        db2 = str(tmp_path / "db2.db")

        m1 = SchemaMigrator(db1)
        m1.register_migration(1, "v1", lambda c: c.execute("CREATE TABLE t1 (id INTEGER)"))
        r1 = m1.apply_migrations()

        m2 = SchemaMigrator(db2)
        m2.register_migration(1, "v1", lambda c: c.execute("CREATE TABLE t2 (id INTEGER)"))
        r2 = m2.apply_migrations()

        assert r1.to_version == 1
        assert r2.to_version == 1

        # 各自独立
        assert m1.get_current_version() == 1
        assert m2.get_current_version() == 1

    def test_migration_spec_dataclass(self):
        """MigrationSpec 数据类字段。"""
        def up(conn):
            pass

        spec = MigrationSpec(version=1, description="test", up=up)
        assert spec.version == 1
        assert spec.description == "test"
        assert spec.up is up
        assert spec.down is None

    def test_migrator_with_existing_db(self, tmp_path):
        """已有数据的 DB 也能应用迁移。"""
        db_path = str(tmp_path / "test.db")
        # 先创建一个已有表的 DB
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE legacy (id INTEGER)")
        conn.execute("INSERT INTO legacy VALUES (1)")
        conn.commit()
        conn.close()

        m = SchemaMigrator(db_path)
        m.register_migration(1, "add table", lambda c: c.execute("CREATE TABLE new (id INTEGER)"))
        result = m.apply_migrations()

        assert result.status == "migrated"
        # 原有数据应保留
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT * FROM legacy").fetchone()
        assert row is not None
        conn.close()
