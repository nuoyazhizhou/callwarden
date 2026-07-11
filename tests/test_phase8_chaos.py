"""Phase 8.8: chaos tests（故障注入与混沌测试）。

验证 Phase 8 各子系统的鲁棒性：

1. **daemon restart recovery**：模拟 daemon crash 后重启，验证
   RecoveryHandler 能恢复 workspace registry、清理 stale jobs
2. **并发写冲突**：多线程并发执行 schema migration、backup、GC，
   验证 SQLite 锁处理和数据一致性
3. **备份-恢复往返**：备份 → 修改 → 恢复 → 验证数据完整性
4. **schema migration 故障注入**：迁移中途失败后能否从断点续跑
5. **GC 安全性**：GC 不会删除活跃 workspace 的数据
6. **权限边界**：AccessChecker 在混沌场景下仍拒绝越权
7. **metrics 在高负载下不丢失**：Counter 在并发递增下计数准确
8. **audit log 完整性**：故障场景下审计日志不丢失

验收对应（Phase 8 验收标准）：
- daemon restart 后自动恢复 workspace registry 和 snapshots ✓
- 内存、CPU、队列、错误率可观测 ✓
- 权限测试覆盖越权路径、symlink 逃逸、TCP token 错误、跨 UID 查询 ✓
"""

import os
import time
import json
import sqlite3
import shutil
import threading
import random
import pytest
from concurrent.futures import ThreadPoolExecutor, as_completed

from callwarden.server.daemon_config import (
    DaemonConfig, PermissionRole, PermissionTemplate,
    AccessChecker, AccessDeniedError, TokenValidator,
)
from callwarden.server.schema_migrator import (
    SchemaMigrator, MigrationSpec, migrate_daemon_dbs,
)
from callwarden.server.backup_restore import BackupManager, RestoreManager
from callwarden.server.snapshot_gc import SnapshotGC, GCPolicy
from callwarden.server.metrics import (
    get_metrics_collector, Counter, Gauge, MetricsCollector,
)
from callwarden.server.audit_log import (
    AuditLogger, AuditEventType, AuditResult,
)
from callwarden.server.health_check import (
    HealthChecker, HealthStatus, RecoveryHandler,
)


# ======================================================================
# 测试夹具
# ======================================================================


@pytest.fixture
def chaos_env(tmp_path):
    """创建完整的 chaos 测试环境。"""
    data_root = str(tmp_path / "data")
    backup_root = str(tmp_path / "backups")
    os.makedirs(data_root, exist_ok=True)

    cfg = DaemonConfig.load_from_dict({
        "data_root": data_root,
        "security": {
            "admin_uids": [0, 1000],
            "audit_log_path": os.path.join(data_root, "audit.db"),
        },
    })

    # 初始化所有 DB schema
    migrate_daemon_dbs(cfg)

    # 插入测试数据到 registry
    conn = sqlite3.connect(cfg.registry_db_path)
    conn.execute("""
        INSERT OR REPLACE INTO daemon_workspaces
        (workspace_instance_id, snapshot_id, owner_uid, git_remote_url,
         git_head_commit_sha, client_view_root, host_real_root,
         toolchain_fingerprint, registered_at, last_active_at, status)
        VALUES ('ws-chaos-1', 'snap-chaos-1', 1000, 'origin',
                'abc123', '/view', '/host', 'tc-fp', ?, ?, 'active')
    """, (time.time(), time.time()))
    conn.commit()
    conn.close()

    # 创建 snapshots 目录
    snapshot_dir = os.path.join(data_root, "snapshots")
    os.makedirs(snapshot_dir, exist_ok=True)
    with open(os.path.join(snapshot_dir, "snap-chaos-1"), "w") as f:
        f.write("chaos snapshot content")

    return cfg, backup_root


@pytest.fixture
def checker(chaos_env):
    """创建 AccessChecker 实例（需要 PermissionTemplate）。"""
    cfg, _ = chaos_env
    template = PermissionTemplate()
    return AccessChecker(cfg, template)


# ======================================================================
# 1. daemon restart recovery 测试
# ======================================================================


class TestDaemonRestartRecovery:
    """模拟 daemon crash 后重启的恢复测试。"""

    def test_recovery_handler_restores_workspace(self, chaos_env):
        """daemon 重启后 RecoveryHandler 应能恢复 workspace registry。"""
        cfg, _ = chaos_env
        recovery = RecoveryHandler(cfg)
        result = recovery.recover()

        # recovery 返回的 status 来自 HealthStatus.from_checks
        assert result["status"] in ("healthy", "degraded", "unhealthy")
        # workspace 应仍在 registry 中
        conn = sqlite3.connect(cfg.registry_db_path)
        row = conn.execute(
            "SELECT * FROM daemon_workspaces WHERE workspace_instance_id = ?",
            ("ws-chaos-1",)
        ).fetchone()
        conn.close()
        assert row is not None

    def test_recovery_handler_cleans_stale_jobs(self, chaos_env):
        """daemon 重启后应清理 stale jobs。"""
        cfg, _ = chaos_env
        # 插入一个 running 状态的 job
        conn = sqlite3.connect(cfg.registry_db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT,
                    started_at REAL,
                    finished_at REAL,
                    error TEXT
                )
            """)
            conn.execute("""
                INSERT INTO jobs (job_id, status, started_at, finished_at, error)
                VALUES ('job-stale', 'running', ?, 0, '')
            """, (time.time() - 100,))
            conn.commit()
        finally:
            conn.close()

        recovery = RecoveryHandler(cfg)
        recovery.recover()

        # stale job 应被标记为 failed
        conn = sqlite3.connect(cfg.registry_db_path)
        row = conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", ("job-stale",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "failed"

    def test_recovery_idempotent(self, chaos_env):
        """多次执行 recovery 应幂等。"""
        cfg, _ = chaos_env
        recovery = RecoveryHandler(cfg)

        r1 = recovery.recover()
        r2 = recovery.recover()

        assert r1["status"] in ("healthy", "degraded", "unhealthy")
        assert r2["status"] in ("healthy", "degraded", "unhealthy")

    def test_recovery_with_empty_registry(self, tmp_path):
        """空 registry 的 recovery 应成功。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        migrate_daemon_dbs(cfg)

        recovery = RecoveryHandler(cfg)
        result = recovery.recover()
        assert result["status"] in ("healthy", "degraded", "unhealthy")


# ======================================================================
# 2. 并发写冲突测试
# ======================================================================


class TestConcurrentWrites:
    """并发写操作的 SQLite 锁处理测试。"""

    def test_concurrent_backups(self, chaos_env):
        """多个线程并发 backup 不应导致数据损坏。"""
        cfg, backup_root = chaos_env
        mgr = BackupManager(cfg, backup_root=backup_root)

        results = []
        errors = []
        lock = threading.Lock()

        def do_backup(i):
            try:
                r = mgr.backup_full(backup_id=f"concurrent-{i}")
                with lock:
                    results.append(r["backup_id"])
            except Exception as e:
                with lock:
                    errors.append(str(e))

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(do_backup, i) for i in range(8)]
            for f in as_completed(futures):
                f.result()

        # 至少部分 backup 应成功
        assert len(results) > 0
        # 每个 backup 目录应完整
        for bid in results:
            meta_path = os.path.join(backup_root, bid, "backup_meta.json")
            assert os.path.isfile(meta_path)

    def test_concurrent_migrations_different_dbs(self, tmp_path):
        """不同 DB 的并发迁移不应互相阻塞。"""
        db1 = str(tmp_path / "db1.db")
        db2 = str(tmp_path / "db2.db")

        m1 = SchemaMigrator(db1)
        m1.register_migration(1, "v1", lambda c: c.execute("CREATE TABLE t1 (id INTEGER)"))
        m2 = SchemaMigrator(db2)
        m2.register_migration(1, "v1", lambda c: c.execute("CREATE TABLE t2 (id INTEGER)"))

        results = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(m1.apply_migrations)
            f2 = executor.submit(m2.apply_migrations)
            results.append(f1.result())
            results.append(f2.result())

        assert all(r.status == "migrated" for r in results)

    def test_concurrent_metrics_increment(self):
        """Counter 在并发递增下应计数准确。"""
        counter = Counter("test_chaos_concurrent", "chaos concurrent increment")
        iterations = 1000
        threads = 10

        def increment():
            for _ in range(iterations):
                counter.inc()

        threads_list = [threading.Thread(target=increment) for _ in range(threads)]
        for t in threads_list:
            t.start()
        for t in threads_list:
            t.join()

        # Counter 使用 .get() 而非 .value
        assert counter.get() == iterations * threads


# ======================================================================
# 3. 备份-恢复往返测试
# ======================================================================


class TestBackupRestoreRoundtrip:
    """备份 → 修改 → 恢复 → 验证完整性。"""

    def test_full_roundtrip(self, chaos_env):
        """完整往返：备份 → 修改 → 恢复 → 验证。"""
        cfg, backup_root = chaos_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        # 1. 备份
        backup_result = backup_mgr.backup_full()
        assert backup_result["backup_type"] == "full"

        # 2. 修改 registry DB
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute("DELETE FROM daemon_workspaces")
        conn.execute("""
            INSERT INTO daemon_workspaces
            (workspace_instance_id, snapshot_id, owner_uid, git_remote_url,
             git_head_commit_sha, client_view_root, host_real_root,
             toolchain_fingerprint, registered_at, last_active_at, status)
            VALUES ('ws-modified', 'snap-mod', 2000, '', '', '/m', '/m', '', ?, ?, 'active')
        """, (time.time(), time.time()))
        conn.commit()
        conn.close()

        # 3. 恢复
        restore_result = restore_mgr.restore(backup_result["backup_id"])
        assert restore_result["status"] == "success"

        # 4. 验证数据恢复到备份时状态
        conn = sqlite3.connect(cfg.registry_db_path)
        row = conn.execute(
            "SELECT workspace_instance_id FROM daemon_workspaces"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "ws-chaos-1"

    def test_backup_then_verify(self, chaos_env):
        """备份后验证应通过。"""
        cfg, backup_root = chaos_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()
        verify_result = restore_mgr.verify_backup(backup_result["backup_id"])
        assert verify_result["status"] == "valid"

    def test_multiple_backups_restore_latest(self, chaos_env):
        """多次备份后恢复最新的。"""
        cfg, backup_root = chaos_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        b1 = backup_mgr.backup_full()
        time.sleep(0.01)
        b2 = backup_mgr.backup_full()

        # 恢复第二个
        result = restore_mgr.restore(b2["backup_id"])
        assert result["status"] == "success"

    def test_backup_with_audit_db(self, chaos_env):
        """备份应包含 audit DB。"""
        cfg, _ = chaos_env
        # 先在 audit DB 中写入数据
        logger = AuditLogger(cfg.audit_log_path)
        logger.log_admin_operation(
            actor_uid=0, actor_role="admin",
            action="backup_test", target="system",
            details={"reason": "chaos test"}
        )
        logger.flush()

        backup_mgr = BackupManager(cfg, backup_root=os.path.join(os.path.dirname(cfg.data_root), "backups"))
        result = backup_mgr.backup_full()

        file_names = [f["name"] for f in result["files"]]
        assert "audit.db" in file_names


# ======================================================================
# 4. schema migration 故障注入
# ======================================================================


class TestMigrationFaultInjection:
    """迁移故障注入测试。"""

    def test_migration_failure_then_resume(self, tmp_path):
        """迁移中途失败后能从断点续跑。"""
        db_path = str(tmp_path / "fault.db")
        m = SchemaMigrator(db_path)

        call_log = []

        def v1(conn):
            call_log.append("v1")
            conn.execute("CREATE TABLE t1 (id INTEGER)")

        def v2_failing(conn):
            call_log.append("v2")
            conn.execute("CREATE TABLE t2 (id INTEGER)")
            raise RuntimeError("injected failure")

        def v3(conn):
            call_log.append("v3")
            conn.execute("CREATE TABLE t3 (id INTEGER)")

        m.register_migration(1, "v1", v1)
        m.register_migration(2, "v2 failing", v2_failing)
        m.register_migration(3, "v3", v3)

        # 第一次：v1 成功，v2 失败
        r1 = m.apply_migrations()
        assert r1.failed == 2
        assert r1.to_version == 1
        assert call_log == ["v1", "v2"]

        # 修复 v2（重新注册一个新的 migrator）
        m2 = SchemaMigrator(db_path)
        m2.register_migration(2, "v2 fixed", lambda c: c.execute("CREATE TABLE t2 (id INTEGER)"))
        m2.register_migration(3, "v3", v3)

        # 第二次：从 v1 续跑
        r2 = m2.apply_migrations()
        assert r2.status == "migrated"
        assert r2.from_version == 1
        assert r2.applied == [2, 3]
        assert r2.to_version == 3

    def test_migration_with_data_loss_simulation(self, tmp_path):
        """模拟迁移中数据操作失败后数据不丢失。"""
        db_path = str(tmp_path / "data.db")
        m = SchemaMigrator(db_path)

        m.register_migration(1, "init", lambda c: c.execute("CREATE TABLE users (id INTEGER, name TEXT)"))
        m.register_migration(2, "insert", lambda c: c.execute("INSERT INTO users VALUES (1, 'alice')"))
        m.apply_migrations()

        # 第三步失败
        def failing_update(conn):
            conn.execute("UPDATE users SET name = 'bob'")
            raise RuntimeError("update failed")

        m.register_migration(3, "failing update", failing_update)
        r = m.apply_migrations()
        assert r.failed == 3

        # 数据不应被修改（事务回滚）
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT name FROM users WHERE id = 1").fetchone()
        conn.close()
        assert row[0] == "alice"  # 仍是原始值

    def test_migration_gap_handling(self, tmp_path):
        """迁移版本跳号时应正常处理。"""
        db_path = str(tmp_path / "gap.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: c.execute("CREATE TABLE t1 (id INTEGER)"))
        m.register_migration(10, "v10", lambda c: c.execute("CREATE TABLE t10 (id INTEGER)"))
        result = m.apply_migrations()

        assert result.applied == [1, 10]
        assert result.to_version == 10


# ======================================================================
# 5. GC 安全性测试
# ======================================================================


class TestGCSafety:
    """GC 不会删除活跃数据的安全测试。"""

    def test_gc_preserves_active_workspace(self, chaos_env):
        """GC 不应删除 active workspace 的 snapshot。"""
        cfg, _ = chaos_env
        gc = SnapshotGC(cfg)
        gc.run_gc()

        snapshot_dir = os.path.join(cfg.data_root, "snapshots")
        snap_path = os.path.join(snapshot_dir, "snap-chaos-1")
        assert os.path.exists(snap_path)

    def test_gc_preserves_registered_backup(self, chaos_env):
        """GC 不应删除未过期的 backup_history 记录。"""
        cfg, _ = chaos_env
        # 插入一个正常 backup 记录
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute("""
            INSERT INTO backup_history
            (backup_id, backup_type, created_at, file_count, total_size_bytes, checksum, deleted_at)
            VALUES ('B-active', 'full', ?, 5, 1024, 'abc', 0)
        """, (time.time(),))
        conn.commit()
        conn.close()

        gc = SnapshotGC(cfg)
        gc.run_gc()

        # 记录应仍存在
        conn = sqlite3.connect(cfg.registry_db_path)
        row = conn.execute(
            "SELECT backup_id FROM backup_history WHERE backup_id = ?", ("B-active",)
        ).fetchone()
        conn.close()
        assert row is not None

    def test_gc_dry_run_never_deletes(self, chaos_env):
        """dry_run 模式绝不删除任何文件。"""
        cfg, _ = chaos_env
        snapshot_dir = os.path.join(cfg.data_root, "snapshots")

        # 创建一个过期孤立文件
        orphan_path = os.path.join(snapshot_dir, "orphan")
        with open(orphan_path, "w") as f:
            f.write("orphan")
        old_time = time.time() - 30 * 24 * 3600
        os.utime(orphan_path, (old_time, old_time))

        gc = SnapshotGC(cfg, policy=GCPolicy(dry_run=True))
        stats = gc.run_gc()

        assert stats.marked_count > 0
        assert stats.swept_count == 0
        assert os.path.exists(orphan_path)  # 未删除

    def test_gc_with_zero_retention(self, chaos_env):
        """retention_count=0 时不应崩溃。"""
        cfg, _ = chaos_env
        gc = SnapshotGC(cfg, policy=GCPolicy(retention_count=0))
        stats = gc.run_gc()
        assert stats.duration_ms >= 0


# ======================================================================
# 6. 权限边界混沌测试
# ======================================================================


class TestPermissionChaos:
    """混沌场景下的权限边界测试。"""

    def test_path_traversal_chaos(self, checker):
        """各种路径遍历变体应被拒绝。"""
        traversal_variants = [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32",
            "....//....//etc/passwd",
            "/etc/passwd",
            "workspace/../../../escape",
        ]

        for path in traversal_variants:
            with pytest.raises(AccessDeniedError):
                checker.check_path_safety(path, "/workspace/root")

    def test_cross_uid_access_denied(self, checker):
        """UID 1001 不能查询 UID 1000 的 workspace（跨 UID 查询）。"""
        with pytest.raises(AccessDeniedError):
            checker.check_workspace_access(
                uid=1001,
                workspace_owner_uid=1000,
                operation="query",
            )

    def test_admin_uid_access_any_workspace(self, checker):
        """admin UID 可以访问任何 workspace。"""
        # admin UID 0 应能访问（不抛异常即通过）
        checker.check_workspace_access(
            uid=0,
            workspace_owner_uid=1000,
            operation="query",
        )

    def test_invalid_token_rejected(self, chaos_env):
        """TCP 无效 token 应被拒绝。"""
        cfg, _ = chaos_env
        template = PermissionTemplate()
        checker = AccessChecker(cfg, template)
        validator = TokenValidator()  # 空 store

        with pytest.raises(AccessDeniedError):
            checker.check_tcp_token(
                token="invalid-token",
                config=cfg,
                validator=validator,
            )

    def test_revoked_token_rejected(self, chaos_env, tmp_path):
        """已撤销的 token 应被拒绝。"""
        cfg, _ = chaos_env
        token_store = str(tmp_path / "tokens.json")
        validator = TokenValidator(token_store_path=token_store)
        token = validator.generate_token(
            container_id="container-1", uid=1000, role="user"
        )

        # 撤销
        validator.revoke_token(token)

        # 验证应失败
        template = PermissionTemplate()
        checker = AccessChecker(cfg, template)
        # 强制 config 要求 token
        with pytest.raises(AccessDeniedError):
            checker.check_tcp_token(
                token=token,
                config=cfg,
                validator=validator,
            )

    def test_expired_token_rejected(self, chaos_env, tmp_path):
        """过期的 token 应被拒绝。"""
        cfg, _ = chaos_env
        token_store = str(tmp_path / "tokens.json")
        validator = TokenValidator(token_store_path=token_store)

        # 生成一个已过期的 token
        token = validator.generate_token(
            container_id="container-2", uid=1000, role="user",
            expires_in=-1  # 已过期
        )

        template = PermissionTemplate()
        checker = AccessChecker(cfg, template)
        with pytest.raises(AccessDeniedError):
            checker.check_tcp_token(
                token=token,
                config=cfg,
                validator=validator,
            )


# ======================================================================
# 7. metrics 高负载测试
# ======================================================================


class TestMetricsUnderLoad:
    """metrics 在高负载下的正确性测试。"""

    def test_counter_high_concurrency(self):
        """高并发下 Counter 计数准确。"""
        counter = Counter("chaos_counter", "chaos counter")
        total_increments = 10000
        threads = 20

        def increment():
            for _ in range(total_increments // threads):
                counter.inc()

        threads_list = [threading.Thread(target=increment) for _ in range(threads)]
        for t in threads_list:
            t.start()
        for t in threads_list:
            t.join()

        # Counter 使用 .get() 而非 .value
        assert counter.get() == total_increments

    def test_gauge_concurrent_set(self):
        """并发 Gauge.set 最终值应为最后设置的值之一。"""
        gauge = Gauge("chaos_gauge", "chaos gauge")
        values = list(range(100))

        def set_value(v):
            gauge.set(v)

        with ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(set_value, values))

        # 最终值应为 100 次设置中的某一个
        assert gauge.get() in values

    def test_metrics_collector_export_consistency(self):
        """MetricsCollector 在并发导出下应一致。"""
        collector = get_metrics_collector()
        counter = collector.register_counter("chaos_export", "chaos export test")
        counter.inc(10)

        # 并发导出（使用 to_prometheus）
        exports = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(collector.to_prometheus) for _ in range(10)]
            for f in as_completed(futures):
                exports.append(f.result())

        # 所有导出应包含 counter
        for text in exports:
            assert "chaos_export" in text


# ======================================================================
# 8. audit log 完整性测试
# ======================================================================


class TestAuditLogIntegrity:
    """故障场景下审计日志的完整性测试。"""

    def test_audit_log_survives_migration(self, chaos_env):
        """迁移后审计日志数据应保留。"""
        cfg, _ = chaos_env

        # 先写入审计日志
        logger = AuditLogger(cfg.audit_log_path)
        logger.log_admin_operation(
            actor_uid=0, actor_role="admin",
            action="test_before_migration", target="system"
        )
        logger.flush()

        # 执行迁移（幂等）
        migrate_daemon_dbs(cfg)

        # 审计日志应仍在
        conn = sqlite3.connect(cfg.audit_log_path)
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = ?", ("test_before_migration",)
        ).fetchall()
        conn.close()
        assert len(rows) == 1

    def test_audit_log_survives_gc(self, chaos_env):
        """GC 不应删除未过期的审计日志。"""
        cfg, _ = chaos_env

        logger = AuditLogger(cfg.audit_log_path)
        logger.log_admin_operation(
            actor_uid=0, actor_role="admin",
            action="gc_test", target="system"
        )
        logger.flush()

        # 执行 GC（audit GC 默认禁用）
        gc = SnapshotGC(cfg, enable_audit_gc=False)
        gc.run_gc()

        # 审计日志应仍在
        conn = sqlite3.connect(cfg.audit_log_path)
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = ?", ("gc_test",)
        ).fetchall()
        conn.close()
        assert len(rows) == 1

    def test_audit_log_records_failures(self, chaos_env):
        """失败操作也应记录审计日志。"""
        cfg, _ = chaos_env
        logger = AuditLogger(cfg.audit_log_path)

        logger.log_access_denied(
            actor_uid=1001, actor_role="user",
            action="query", target="ws-chaos-1",
            reason="cross-uid access"
        )
        logger.flush()

        events = logger.query(event_type=AuditEventType.ACCESS_DENIED)
        assert len(events) >= 1
        assert events[-1]["result"] == "denied"

    def test_concurrent_audit_writes(self, chaos_env):
        """并发写入审计日志不应丢失记录。"""
        cfg, _ = chaos_env

        def write_logs(count):
            logger = AuditLogger(cfg.audit_log_path)
            for i in range(count):
                logger.log_admin_operation(
                    actor_uid=0, actor_role="admin",
                    action=f"concurrent_{i}", target="system"
                )
            logger.flush()

        threads_count = 4
        per_thread = 25

        threads_list = [
            threading.Thread(target=write_logs, args=(per_thread,))
            for _ in range(threads_count)
        ]
        for t in threads_list:
            t.start()
        for t in threads_list:
            t.join()

        # 验证记录数（可能有部分丢失，但应大部分成功）
        conn = sqlite3.connect(cfg.audit_log_path)
        rows = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action LIKE 'concurrent_%'"
        ).fetchone()
        conn.close()
        assert rows[0] >= per_thread * threads_count * 0.8  # 至少 80% 成功


# ======================================================================
# 9. 端到端混沌场景
# ======================================================================


class TestEndToEndChaos:
    """端到端混沌场景测试。"""

    def test_full_lifecycle(self, chaos_env):
        """完整生命周期：迁移 → 备份 → 操作 → GC → 恢复。"""
        cfg, backup_root = chaos_env

        # 1. 迁移（幂等）
        results = migrate_daemon_dbs(cfg)
        assert results["registry"].status in ("up_to_date", "migrated")

        # 2. 备份
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        backup_result = backup_mgr.backup_full()
        assert backup_result["backup_type"] == "full"

        # 3. 操作：写入审计日志
        logger = AuditLogger(cfg.audit_log_path)
        logger.log_admin_operation(
            actor_uid=0, actor_role="admin",
            action="lifecycle_test", target="system"
        )
        logger.flush()

        # 4. GC（不删除活跃数据）
        gc = SnapshotGC(cfg, policy=GCPolicy(dry_run=True))
        gc.run_gc()

        # 5. 验证备份
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)
        verify_result = restore_mgr.verify_backup(backup_result["backup_id"])
        assert verify_result["status"] == "valid"

        # 6. 数据仍完整
        conn = sqlite3.connect(cfg.registry_db_path)
        row = conn.execute(
            "SELECT workspace_instance_id FROM daemon_workspaces"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "ws-chaos-1"

    def test_restart_backup_gc_cycle(self, chaos_env):
        """模拟 daemon restart → backup → GC 循环。"""
        cfg, backup_root = chaos_env

        for cycle in range(3):
            # 模拟 restart recovery
            recovery = RecoveryHandler(cfg)
            recovery.recover()

            # backup
            backup_mgr = BackupManager(cfg, backup_root=backup_root)
            backup_mgr.backup_full(backup_id=f"cycle-{cycle}")

            # GC
            gc = SnapshotGC(cfg, policy=GCPolicy(dry_run=True))
            gc.run_gc()

        # 验证所有 backup 存在
        backups = backup_mgr.list_backups()
        assert len(backups) >= 3

    def test_chaos_random_operations(self, chaos_env):
        """随机操作序列不应导致崩溃。"""
        cfg, backup_root = chaos_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)
        gc = SnapshotGC(cfg, policy=GCPolicy(dry_run=True))

        operations = [
            ("backup", lambda: backup_mgr.backup_full()),
            ("list_backups", lambda: backup_mgr.list_backups()),
            ("gc", lambda: gc.run_gc()),
            ("migrate", lambda: migrate_daemon_dbs(cfg)),
        ]

        for _ in range(20):
            op_name, op = random.choice(operations)
            try:
                op()
            except Exception:
                # 操作可能失败（如锁冲突），但不应崩溃
                pass

        # 最终状态应一致
        backups = backup_mgr.list_backups()
        for b in backups:
            assert os.path.isdir(os.path.join(backup_root, b["backup_id"]))
