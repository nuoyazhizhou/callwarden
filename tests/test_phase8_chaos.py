"""Phase 8.8: chaos tests（故障注入与混沌测试）。

stale 依据（daemon authority / HTTP thin-client 迁移后）：
- **A 类（被测模块已是 daemon 薄客户端）**：
  * ``server/schema_migrator.py:1-6/126-149``：``SchemaMigrator.apply_migrations`` /
    ``migrate_daemon_dbs`` 经 ``mcp.schema_migrator.*`` RPC，DDL 与事务回滚在 Rust；
    旧用例「本地注册迁移函数 + 断言 call_log / 本地表 / 断点续跑」的期望已过期。
  * ``server/snapshot_gc.py:244-292``：backup/migration/audit/workspace 扫描与
    ``get_registered_snapshot_ids`` 经 ``mcp.snapshot_gc.*`` RPC。
  * ``server/audit_log.py:154-370``：``AuditLogger`` 为纯 daemon RPC 薄客户端
    （SRV-002），``log``/``query``/``count`` 经 ``mcp.audit_log.*``；旧用例
    「写本地 audit.db 再 sqlite3 读取」已过期。
- **仍本地，保留断言**：``server/health_check.py:390-634`` ``RecoveryHandler``
  （零生产调用方，compat/test-only，本地 sqlite 读写）；``server/backup_restore.py``
  ``BackupManager`` / ``RestoreManager`` 的 Python fallback（本地文件系统）；
  ``AccessChecker`` / ``TokenValidator`` / ``metrics.Counter`` 等纯 Python 逻辑。

因此：``chaos_env`` 改为直接建立本地 registry schema（供本地 compat 组件使用），
并对 RPC 组件注入 ``FakeChaosDaemon``（mock RPC seam），断言路由 / 透传 / fail-closed。
"""

import os
import time
import sqlite3
import threading
import random

import pytest
from concurrent.futures import ThreadPoolExecutor, as_completed

from callwarden.server.daemon_config import (
    DaemonConfig, PermissionTemplate,
    AccessChecker, AccessDeniedError, TokenValidator,
)
from callwarden.server.schema_migrator import (
    SchemaMigrator, migrate_daemon_dbs,
)
from callwarden.server.backup_restore import BackupManager, RestoreManager
from callwarden.server.snapshot_gc import SnapshotGC, GCPolicy
from callwarden.server.metrics import (
    get_metrics_collector, Counter, Gauge,
)
from callwarden.server.audit_log import (
    AuditLogger, AuditEventType,
)
from callwarden.server.health_check import (
    RecoveryHandler,
)


# ----------------------------------------------------------------------
# RPC method 命名空间（生产侧见 server/schema_migrator.py:19-22 /
# server/snapshot_gc.py:37-47 / server/audit_log.py:192-366）
# ----------------------------------------------------------------------
SCHEMA_APPLY = "mcp.schema_migrator.apply_migrations"
SCHEMA_CURRENT = "mcp.schema_migrator.get_current_version"
SCHEMA_HISTORY = "mcp.schema_migrator.get_migration_history"
SCHEMA_VALIDATE = "mcp.schema_migrator.validate_schema"

GC_REGISTERED = "mcp.snapshot_gc.get_registered_snapshot_ids"
GC_SCAN_BACKUP = "mcp.snapshot_gc.scan_expired_backup_history"
GC_SCAN_MIGRATION = "mcp.snapshot_gc.scan_expired_migrations_log"
GC_SCAN_AUDIT = "mcp.snapshot_gc.scan_expired_audit_logs"
GC_SCAN_WORKSPACES = "mcp.snapshot_gc.scan_orphaned_workspaces"
GC_SCAN_METHODS = {GC_SCAN_BACKUP, GC_SCAN_MIGRATION, GC_SCAN_AUDIT, GC_SCAN_WORKSPACES}
GC_DELETE_METHODS = {
    "mcp.snapshot_gc.delete_backup_history_record",
    "mcp.snapshot_gc.delete_migration_log_record",
    "mcp.snapshot_gc.delete_expired_audit_logs",
    "mcp.snapshot_gc.vacuum_databases",
}

AUDIT_INIT = "mcp.audit_log.init_db"
AUDIT_APPEND = "mcp.audit_log.append"
AUDIT_QUERY = "mcp.audit_log.query"
AUDIT_COUNT = "mcp.audit_log.count"


class FakeChaosDaemon:
    """内存态 daemon：统一处理 schema_migrator / snapshot_gc / audit_log 三命名空间。

    线程安全（concurrent 用例）；``available=False`` 时 fail-closed（抛错，不落地）。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.available = True
        self.schema_versions = {}
        self.schema_apply_script = None  # list[dict]：按序返回，用于故障注入
        self.registered_snapshots = {"snap-chaos-1"}
        self.gc_items = {}
        self.audit_events = []

    # ----- 观测辅助 -----
    def methods(self):
        with self.lock:
            return [m for m, _ in self.calls]

    def params_for(self, method):
        with self.lock:
            return [p for m, p in self.calls if m == method]

    def __call__(self, method, params):
        if not self.available:
            raise RuntimeError("daemon unavailable")
        with self.lock:
            self.calls.append((method, params))

            if method == SCHEMA_CURRENT:
                return self.schema_versions.get(params["db_path"], 0)
            if method == SCHEMA_HISTORY:
                current = self.schema_versions.get(params["db_path"], 0)
                return [{"version": v, "description": f"v{v}"} for v in range(1, current + 1)]
            if method == SCHEMA_APPLY:
                if self.schema_apply_script:
                    return self.schema_apply_script.pop(0)
                target = 2 if params.get("migration_set") == "audit" else 3
                current = self.schema_versions.get(params["db_path"], 0)
                self.schema_versions[params["db_path"]] = target
                return {
                    "db_path": params["db_path"],
                    "from_version": current,
                    "to_version": target,
                    "applied": list(range(current + 1, target + 1)),
                    "skipped": [],
                    "failed": None,
                    "error": None,
                }
            if method == SCHEMA_VALIDATE:
                return {
                    "valid": True,
                    "missing_tables": [],
                    "missing_indexes": [],
                    "current_version": self.schema_versions.get(params["db_path"], 0),
                    "source": "rust",
                }

            if method == GC_REGISTERED:
                return sorted(self.registered_snapshots)
            if method in GC_SCAN_METHODS:
                return list(self.gc_items.get(method, []))
            if method in GC_DELETE_METHODS:
                return {"deleted": 1, "source": "rust"}

            if method == AUDIT_INIT:
                return {"ok": True}
            if method == AUDIT_APPEND:
                event = dict(params.get("event", {}))
                self.audit_events.append(event)
                return {"ok": True, "event_id": event.get("event_id")}
            if method == AUDIT_QUERY:
                return self._audit_query(params)
            if method == AUDIT_COUNT:
                return {"count": len(self._audit_filtered(params))}

        raise RuntimeError(f"unexpected method: {method}")

    def _audit_filtered(self, params):
        out = []
        etype = params.get("event_type")
        for event in self.audit_events:
            if etype is not None and event.get("event_type") != etype:
                continue
            out.append(event)
        return out

    def _audit_query(self, params):
        out = sorted(self._audit_filtered(params), key=lambda e: e.get("timestamp", 0), reverse=True)
        limit = params.get("limit", 100)
        offset = params.get("offset", 0)
        return out[offset:offset + limit]


@pytest.fixture(autouse=True)
def fake_daemon(monkeypatch):
    """将三个薄客户端模块的 RPC seam 统一指向内存态 daemon。"""
    daemon = FakeChaosDaemon()
    monkeypatch.setattr("callwarden.server.schema_migrator._call_daemon_rpc", daemon)
    monkeypatch.setattr("callwarden.server.snapshot_gc._call_daemon_rpc", daemon)
    monkeypatch.setattr("callwarden.server.audit_log._call_daemon_rpc", daemon)
    return daemon


# ======================================================================
# 测试夹具
# ======================================================================


def _create_local_registry(cfg, workspaces=()):
    """直接建立本地 registry schema（供本地 compat 组件 RecoveryHandler 使用）。"""
    conn = sqlite3.connect(cfg.registry_db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daemon_workspaces (
            workspace_instance_id TEXT PRIMARY KEY,
            snapshot_id TEXT,
            owner_uid INTEGER,
            git_remote_url TEXT,
            git_head_commit_sha TEXT,
            client_view_root TEXT,
            host_real_root TEXT,
            toolchain_fingerprint TEXT,
            registered_at REAL,
            last_active_at REAL,
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS backup_history (
            backup_id TEXT PRIMARY KEY,
            backup_type TEXT,
            created_at REAL,
            file_count INTEGER,
            total_size_bytes INTEGER,
            checksum TEXT,
            deleted_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS schema_migrations_log (
            id INTEGER PRIMARY KEY,
            db_name TEXT,
            from_version INTEGER,
            to_version INTEGER,
            applied_at REAL,
            duration_ms INTEGER,
            status TEXT,
            error TEXT
        );
    """)
    for ws in workspaces:
        conn.execute("""
            INSERT OR REPLACE INTO daemon_workspaces
            (workspace_instance_id, snapshot_id, owner_uid, git_remote_url,
             git_head_commit_sha, client_view_root, host_real_root,
             toolchain_fingerprint, registered_at, last_active_at, status)
            VALUES (?, ?, ?, 'origin', 'abc123', '/view', '/host', 'tc-fp', ?, ?, 'active')
        """, (ws[0], ws[1], ws[2], time.time(), time.time()))
    conn.commit()
    conn.close()


@pytest.fixture
def chaos_env(tmp_path):
    """chaos 测试环境：本地 compat 组件所需的 registry schema + snapshot 文件。

    注：daemon authority 迁移后 ``migrate_daemon_dbs`` 不再建立本地 DB schema；
    此处直接建立本地表，仅服务于仍为本地实现的 compat 组件（RecoveryHandler 等）。
    """
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

    _create_local_registry(cfg, workspaces=[("ws-chaos-1", "snap-chaos-1", 1000)])

    # audit DB 占位（BackupManager 备份 audit.db 需文件存在）
    open(cfg.audit_log_path, "w").close()

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
# 1. daemon restart recovery 测试（RecoveryHandler 仍为本地 compat 实现）
# ======================================================================


class TestDaemonRestartRecovery:
    """模拟 daemon crash 后重启的恢复测试。"""

    def test_recovery_handler_restores_workspace(self, chaos_env):
        cfg, _ = chaos_env
        recovery = RecoveryHandler(cfg)
        result = recovery.recover()

        assert result["status"] in ("healthy", "degraded", "unhealthy")
        conn = sqlite3.connect(cfg.registry_db_path)
        row = conn.execute(
            "SELECT * FROM daemon_workspaces WHERE workspace_instance_id = ?",
            ("ws-chaos-1",)
        ).fetchone()
        conn.close()
        assert row is not None

    def test_recovery_handler_cleans_stale_jobs(self, chaos_env):
        cfg, _ = chaos_env
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

        RecoveryHandler(cfg).recover()

        conn = sqlite3.connect(cfg.registry_db_path)
        row = conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", ("job-stale",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "failed"

    def test_recovery_idempotent(self, chaos_env):
        cfg, _ = chaos_env
        recovery = RecoveryHandler(cfg)

        r1 = recovery.recover()
        r2 = recovery.recover()

        assert r1["status"] in ("healthy", "degraded", "unhealthy")
        assert r2["status"] in ("healthy", "degraded", "unhealthy")

    def test_recovery_with_empty_registry(self, fake_daemon, tmp_path):
        """空 registry：migrate 经 daemon RPC（不落地本地 DB），recovery 仍可完成。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        results = migrate_daemon_dbs(cfg)
        assert results["registry"].status in ("migrated", "up_to_date")
        assert not os.path.isfile(cfg.registry_db_path)  # 薄客户端不建本地 DB

        result = RecoveryHandler(cfg).recover()
        assert result["status"] in ("healthy", "degraded", "unhealthy")


# ======================================================================
# 2. 并发写冲突测试
# ======================================================================


class TestConcurrentWrites:
    """并发写操作的锁 / 计数正确性测试。"""

    def test_concurrent_backups(self, chaos_env):
        """多个线程并发 backup 不应导致数据损坏（本地 fallback）。"""
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
            except Exception as e:  # noqa: BLE001 - 记录锁冲突，不使测试崩溃
                with lock:
                    errors.append(str(e))

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(do_backup, i) for i in range(8)]
            for f in as_completed(futures):
                f.result()

        assert len(results) > 0
        for bid in results:
            meta_path = os.path.join(backup_root, bid, "backup_meta.json")
            assert os.path.isfile(meta_path)

    def test_concurrent_migrations_different_dbs(self, fake_daemon, tmp_path):
        """不同 DB 的并发迁移各自路由到 daemon，均返回 migrated。"""
        db1 = str(tmp_path / "db1.db")
        db2 = str(tmp_path / "db2.db")

        m1 = SchemaMigrator(db1)
        m1.register_migration(1, "v1")
        m2 = SchemaMigrator(db2)
        m2.register_migration(1, "v1")

        results = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(m1.apply_migrations)
            f2 = executor.submit(m2.apply_migrations)
            results.append(f1.result())
            results.append(f2.result())

        assert all(r.status == "migrated" for r in results)
        assert sorted(p["db_path"] for p in fake_daemon.params_for(SCHEMA_APPLY)) == sorted([db1, db2])

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

        assert counter.get() == iterations * threads


# ======================================================================
# 3. 备份-恢复往返测试（BackupManager/RestoreManager 本地 fallback）
# ======================================================================


class TestBackupRestoreRoundtrip:
    """备份 → 修改 → 恢复 → 验证完整性。"""

    def test_full_roundtrip(self, chaos_env):
        cfg, backup_root = chaos_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()
        assert backup_result["backup_type"] == "full"

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

        restore_result = restore_mgr.restore(backup_result["backup_id"])
        assert restore_result["status"] == "success"

        conn = sqlite3.connect(cfg.registry_db_path)
        row = conn.execute("SELECT workspace_instance_id FROM daemon_workspaces").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "ws-chaos-1"

    def test_backup_then_verify(self, chaos_env):
        cfg, backup_root = chaos_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()
        verify_result = restore_mgr.verify_backup(backup_result["backup_id"])
        assert verify_result["status"] == "valid"

    def test_multiple_backups_restore_latest(self, chaos_env):
        cfg, backup_root = chaos_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_mgr.backup_full()
        time.sleep(0.01)
        b2 = backup_mgr.backup_full()

        result = restore_mgr.restore(b2["backup_id"])
        assert result["status"] == "success"

    def test_backup_with_audit_db(self, chaos_env):
        cfg, backup_root = chaos_env
        logger = AuditLogger(cfg.audit_log_path)
        logger.log_admin_operation(
            actor_uid=0, actor_role="admin",
            action="backup_test", target="system",
            details={"reason": "chaos test"}
        )
        logger.flush()

        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        result = backup_mgr.backup_full()

        file_names = [f["name"] for f in result["files"]]
        assert "audit.db" in file_names


# ======================================================================
# 4. schema migration 故障注入（A 类：经 daemon RPC）
# ======================================================================


class TestMigrationFaultInjection:
    """迁移故障注入：Python 侧只透传 daemon 结果并 fail-closed。"""

    def test_migration_failure_then_resume(self, fake_daemon, tmp_path):
        """daemon 报告 v2 失败后，下一次 apply 从断点续跑（透传 daemon 结果）。"""
        db_path = str(tmp_path / "fault.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1")
        m.register_migration(2, "v2")
        m.register_migration(3, "v3")

        fake_daemon.schema_apply_script = [
            {"db_path": db_path, "from_version": 0, "to_version": 1, "applied": [1],
             "skipped": [], "failed": 2, "error": "injected failure"},
            {"db_path": db_path, "from_version": 1, "to_version": 3, "applied": [2, 3],
             "skipped": [], "failed": None, "error": None},
        ]

        r1 = m.apply_migrations()
        assert r1.failed == 2
        assert r1.to_version == 1

        r2 = m.apply_migrations()
        assert r2.status == "migrated"
        assert r2.from_version == 1
        assert r2.applied == [2, 3]
        assert r2.to_version == 3
        assert fake_daemon.methods() == [SCHEMA_APPLY, SCHEMA_APPLY]

    def test_migration_failure_does_not_touch_local_db(self, fake_daemon, tmp_path):
        """daemon 事务失败时 Python 不得触碰本地 DB（回滚由 daemon 保证）。"""
        db_path = str(tmp_path / "data.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE users (id INTEGER, name TEXT)")
        conn.execute("INSERT INTO users VALUES (1, 'alice')")
        conn.commit()
        conn.close()

        fake_daemon.schema_apply_script = [
            {"db_path": db_path, "from_version": 2, "to_version": 2, "applied": [],
             "skipped": [], "failed": 3, "error": "update failed"},
        ]

        m = SchemaMigrator(db_path)
        m.register_migration(1, "init")
        m.register_migration(2, "insert")
        m.register_migration(3, "failing update")
        r = m.apply_migrations()
        assert r.failed == 3

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT name FROM users WHERE id = 1").fetchone()
        conn.close()
        assert row[0] == "alice"  # 本地数据未被 Python 迁移逻辑修改

    def test_migration_gap_handling(self, fake_daemon, tmp_path):
        """版本跳号仅是本地元数据；实际迁移经 daemon。"""
        m = SchemaMigrator(str(tmp_path / "gap.db"))
        m.register_migration(1, "v1")
        m.register_migration(10, "v10")
        assert m.target_version == 10

        result = m.apply_migrations()
        assert fake_daemon.methods() == [SCHEMA_APPLY]
        assert result.status == "migrated"


# ======================================================================
# 5. GC 安全性测试
# ======================================================================


class TestGCSafety:
    """GC 不会删除活跃数据的安全测试。"""

    def test_gc_preserves_active_workspace(self, chaos_env, fake_daemon):
        cfg, _ = chaos_env
        SnapshotGC(cfg).run_gc()

        snapshot_dir = os.path.join(cfg.data_root, "snapshots")
        assert os.path.exists(os.path.join(snapshot_dir, "snap-chaos-1"))

    def test_gc_preserves_registered_backup(self, chaos_env, fake_daemon):
        """daemon 报告无过期 backup 时不发起任何 delete RPC。"""
        cfg, _ = chaos_env
        fake_daemon.gc_items[GC_SCAN_BACKUP] = []

        SnapshotGC(cfg).run_gc()

        methods = fake_daemon.methods()
        assert not (GC_DELETE_METHODS & set(methods))

    def test_gc_dry_run_never_deletes(self, chaos_env, fake_daemon):
        cfg, _ = chaos_env
        snapshot_dir = os.path.join(cfg.data_root, "snapshots")

        orphan_path = os.path.join(snapshot_dir, "orphan")
        with open(orphan_path, "w") as f:
            f.write("orphan")
        old_time = time.time() - 30 * 24 * 3600
        os.utime(orphan_path, (old_time, old_time))

        stats = SnapshotGC(cfg, policy=GCPolicy(dry_run=True)).run_gc()

        assert stats.marked_count > 0
        assert stats.swept_count == 0
        assert os.path.exists(orphan_path)

    def test_gc_with_zero_retention(self, chaos_env, fake_daemon):
        cfg, _ = chaos_env
        stats = SnapshotGC(cfg, policy=GCPolicy(retention_count=0)).run_gc()
        assert stats.duration_ms >= 0


# ======================================================================
# 6. 权限边界混沌测试（AccessChecker / TokenValidator 仍为本地实现）
# ======================================================================


class TestPermissionChaos:
    """混沌场景下的权限边界测试。"""

    def test_path_traversal_chaos(self, checker):
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
        with pytest.raises(AccessDeniedError):
            checker.check_workspace_access(
                uid=1001,
                workspace_owner_uid=1000,
                operation="query",
            )

    def test_admin_uid_access_any_workspace(self, checker):
        # admin UID 0 应能访问（不抛异常即通过）
        checker.check_workspace_access(
            uid=0,
            workspace_owner_uid=1000,
            operation="query",
        )

    def test_invalid_token_rejected(self, chaos_env):
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
        cfg, _ = chaos_env
        token_store = str(tmp_path / "tokens.json")
        validator = TokenValidator(token_store_path=token_store)
        token = validator.generate_token(
            container_id="container-1", uid=1000, role="user"
        )
        validator.revoke_token(token)

        template = PermissionTemplate()
        checker = AccessChecker(cfg, template)
        with pytest.raises(AccessDeniedError):
            checker.check_tcp_token(
                token=token,
                config=cfg,
                validator=validator,
            )

    def test_expired_token_rejected(self, chaos_env, tmp_path):
        cfg, _ = chaos_env
        token_store = str(tmp_path / "tokens.json")
        validator = TokenValidator(token_store_path=token_store)
        token = validator.generate_token(
            container_id="container-2", uid=1000, role="user",
            expires_in=-1
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
# 7. metrics 高负载测试（纯 Python）
# ======================================================================


class TestMetricsUnderLoad:
    """metrics 在高负载下的正确性测试。"""

    def test_counter_high_concurrency(self):
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

        assert counter.get() == total_increments

    def test_gauge_concurrent_set(self):
        gauge = Gauge("chaos_gauge", "chaos gauge")
        values = list(range(100))

        def set_value(v):
            gauge.set(v)

        with ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(set_value, values))

        assert gauge.get() in values

    def test_metrics_collector_export_consistency(self):
        collector = get_metrics_collector()
        counter = collector.register_counter("chaos_export", "chaos export test")
        counter.inc(10)

        exports = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(collector.to_prometheus) for _ in range(10)]
            for f in as_completed(futures):
                exports.append(f.result())

        for text in exports:
            assert "chaos_export" in text


# ======================================================================
# 8. audit log 完整性测试（A 类：AuditLogger 经 daemon RPC）
# ======================================================================


class TestAuditLogIntegrity:
    """故障场景下审计日志的完整性测试。"""

    def test_audit_log_survives_migration(self, chaos_env, fake_daemon):
        cfg, _ = chaos_env

        logger = AuditLogger(cfg.audit_log_path)
        logger.log_admin_operation(
            actor_uid=0, actor_role="admin",
            action="test_before_migration", target="system"
        )
        logger.flush()

        migrate_daemon_dbs(cfg)

        events = logger.query(event_type=AuditEventType.ADMIN_OPERATION)
        assert any(e["action"] == "test_before_migration" for e in events)

    def test_audit_log_survives_gc(self, chaos_env, fake_daemon):
        cfg, _ = chaos_env

        logger = AuditLogger(cfg.audit_log_path)
        logger.log_admin_operation(
            actor_uid=0, actor_role="admin", action="gc_test", target="system"
        )
        logger.flush()

        SnapshotGC(cfg, enable_audit_gc=False).run_gc()

        events = logger.query(event_type=AuditEventType.ADMIN_OPERATION)
        assert any(e["action"] == "gc_test" for e in events)

    def test_audit_log_records_failures(self, chaos_env, fake_daemon):
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

    def test_concurrent_audit_writes(self, chaos_env, fake_daemon):
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

        count = AuditLogger(cfg.audit_log_path).count()
        assert count >= per_thread * threads_count * 0.8  # 至少 80% 成功


# ======================================================================
# 9. 端到端混沌场景
# ======================================================================


class TestEndToEndChaos:
    """端到端混沌场景测试。"""

    def test_full_lifecycle(self, chaos_env, fake_daemon):
        cfg, backup_root = chaos_env

        results = migrate_daemon_dbs(cfg)
        assert results["registry"].status in ("up_to_date", "migrated")

        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        backup_result = backup_mgr.backup_full()
        assert backup_result["backup_type"] == "full"

        logger = AuditLogger(cfg.audit_log_path)
        logger.log_admin_operation(
            actor_uid=0, actor_role="admin",
            action="lifecycle_test", target="system"
        )
        logger.flush()

        SnapshotGC(cfg, policy=GCPolicy(dry_run=True)).run_gc()

        restore_mgr = RestoreManager(cfg, backup_root=backup_root)
        verify_result = restore_mgr.verify_backup(backup_result["backup_id"])
        assert verify_result["status"] == "valid"

        conn = sqlite3.connect(cfg.registry_db_path)
        row = conn.execute("SELECT workspace_instance_id FROM daemon_workspaces").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "ws-chaos-1"

    def test_restart_backup_gc_cycle(self, chaos_env, fake_daemon):
        cfg, backup_root = chaos_env
        backup_mgr = None

        for cycle in range(3):
            RecoveryHandler(cfg).recover()

            backup_mgr = BackupManager(cfg, backup_root=backup_root)
            backup_mgr.backup_full(backup_id=f"cycle-{cycle}")

            SnapshotGC(cfg, policy=GCPolicy(dry_run=True)).run_gc()

        backups = backup_mgr.list_backups()
        assert len(backups) >= 3

    def test_chaos_random_operations(self, chaos_env, fake_daemon):
        cfg, backup_root = chaos_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        gc = SnapshotGC(cfg, policy=GCPolicy(dry_run=True))

        operations = [
            ("backup", lambda: backup_mgr.backup_full()),
            ("list_backups", lambda: backup_mgr.list_backups()),
            ("gc", lambda: gc.run_gc()),
            ("migrate", lambda: migrate_daemon_dbs(cfg)),
        ]

        for _ in range(20):
            _, op = random.choice(operations)
            try:
                op()
            except Exception:  # noqa: BLE001 - 操作可能失败，但不应崩溃
                pass

        backups = backup_mgr.list_backups()
        for b in backups:
            assert os.path.isdir(os.path.join(backup_root, b["backup_id"]))
