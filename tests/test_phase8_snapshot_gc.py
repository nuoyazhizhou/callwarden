"""Phase 8.7: snapshot GC 测试。

测试覆盖：
1. GCPolicy / GarbageItem / GCStats 数据类
2. SnapshotGC mark 阶段：扫描各类可回收项
3. SnapshotGC sweep 阶段：执行删除
4. dry_run 模式：只统计不删除
5. 回收策略：retention_count、max_age_seconds
6. 边界情况：空目录、表不存在、删除失败
"""

import os
import time
import sqlite3
import pytest

from callwarden.server.snapshot_gc import (
    SnapshotGC,
    GCPolicy,
    GarbageItem,
    GCStats,
)
from callwarden.server.daemon_config import DaemonConfig


# ======================================================================
# 测试夹具
# ======================================================================


@pytest.fixture
def setup_daemon_env_with_gc(tmp_path):
    """创建完整的 daemon 环境用于 GC 测试。"""
    data_root = str(tmp_path / "data")
    os.makedirs(data_root, exist_ok=True)

    cfg = DaemonConfig.load_from_dict({
        "data_root": data_root,
        "security": {
            "admin_uids": [0],
            "audit_log_path": os.path.join(data_root, "audit.db"),
        },
    })

    # 创建 registry DB 并插入数据
    registry_path = cfg.registry_db_path
    conn = sqlite3.connect(registry_path)
    conn.executescript("""
        CREATE TABLE daemon_workspaces (
            workspace_id INTEGER PRIMARY KEY,
            workspace_instance_id TEXT,
            snapshot_id TEXT,
            owner_uid INTEGER,
            last_active_at REAL,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE backup_history (
            backup_id TEXT PRIMARY KEY,
            backup_type TEXT,
            created_at REAL,
            file_count INTEGER,
            total_size_bytes INTEGER,
            checksum TEXT,
            deleted_at REAL DEFAULT 0
        );

        CREATE TABLE schema_migrations_log (
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

    # 插入 active workspace
    conn.execute("""
        INSERT INTO daemon_workspaces
        (workspace_instance_id, snapshot_id, owner_uid, last_active_at, status)
        VALUES ('ws-active', 'snap-001', 1000, ?, 'active')
    """, (time.time(),))

    # 插入 archived workspace（很久以前）
    conn.execute("""
        INSERT INTO daemon_workspaces
        (workspace_instance_id, snapshot_id, owner_uid, last_active_at, status)
        VALUES ('ws-archived', 'snap-002', 1000, ?, 'archived')
    """, (time.time() - 30 * 24 * 3600,))  # 30 天前

    conn.commit()
    conn.close()

    # 创建 audit DB
    audit_path = cfg.audit_log_path
    conn = sqlite3.connect(audit_path)
    conn.executescript("""
        CREATE TABLE audit_log (
            event_id TEXT PRIMARY KEY,
            timestamp REAL,
            event_type TEXT,
            actor_uid INTEGER,
            action TEXT
        );
    """)
    # 插入一条旧记录和一条新记录
    conn.execute("""
        INSERT INTO audit_log (event_id, timestamp, event_type, actor_uid, action)
        VALUES ('A-old', ?, 'admin_operation', 0, 'old')
    """, (time.time() - 30 * 24 * 3600,))  # 30 天前
    conn.execute("""
        INSERT INTO audit_log (event_id, timestamp, event_type, actor_uid, action)
        VALUES ('A-new', ?, 'admin_operation', 0, 'new')
    """, (time.time(),))
    conn.commit()
    conn.close()

    # 创建 snapshots 目录
    snapshot_dir = os.path.join(data_root, "snapshots")
    os.makedirs(snapshot_dir, exist_ok=True)

    # 创建已注册的 snapshot 文件
    with open(os.path.join(snapshot_dir, "snap-001"), "w") as f:
        f.write("active snapshot content")

    # 创建孤立的 snapshot 文件（很久以前）
    orphan_path = os.path.join(snapshot_dir, "orphan-snap")
    with open(orphan_path, "w") as f:
        f.write("orphan content")
    old_time = time.time() - 30 * 24 * 3600  # 30 天前
    os.utime(orphan_path, (old_time, old_time))

    # 创建新的孤立文件（未过期）
    new_orphan_path = os.path.join(snapshot_dir, "new-orphan")
    with open(new_orphan_path, "w") as f:
        f.write("new orphan")

    return cfg


# ======================================================================
# 数据类测试
# ======================================================================


class TestGCPolicy:
    def test_defaults(self):
        p = GCPolicy()
        assert p.retention_count == 3
        assert p.max_age_seconds == 7 * 24 * 3600
        assert p.dry_run is False
        assert p.vacuum_db is False
        assert p.batch_size == 1000

    def test_custom_values(self):
        p = GCPolicy(retention_count=5, max_age_seconds=3600, dry_run=True)
        assert p.retention_count == 5
        assert p.max_age_seconds == 3600
        assert p.dry_run is True


class TestGCStats:
    def test_empty(self):
        s = GCStats()
        assert s.marked_count == 0
        assert s.swept_count == 0
        assert s.failed_count == 0

    def test_to_dict(self):
        s = GCStats(
            marked=[GarbageItem(item_type="snapshot_file", path="/x", size_bytes=100)],
            swept=[GarbageItem(item_type="snapshot_file", path="/x", size_bytes=100)],
            total_marked_bytes=100,
            total_swept_bytes=100,
            duration_ms=50,
        )
        d = s.to_dict()
        assert d["marked_count"] == 1
        assert d["swept_count"] == 1
        assert d["total_marked_bytes"] == 100
        assert d["duration_ms"] == 50

    def test_dry_run_flag(self):
        s = GCStats(
            marked=[GarbageItem(item_type="snapshot_file")],
        )
        assert s.to_dict()["dry_run"] is True


class TestGarbageItem:
    def test_fields(self):
        item = GarbageItem(
            item_type="snapshot_file",
            path="/tmp/test",
            key="test",
            size_bytes=1024,
            reason="orphaned",
        )
        assert item.item_type == "snapshot_file"
        assert item.path == "/tmp/test"
        assert item.key == "test"
        assert item.size_bytes == 1024
        assert item.reason == "orphaned"
        assert item.metadata == {}


# ======================================================================
# SnapshotGC mark 测试
# ======================================================================


class TestSnapshotGCMark:
    def test_collect_empty_env(self, tmp_path):
        """空环境应返回空列表。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        gc = SnapshotGC(cfg)
        items = gc.collect_garbage_stats()
        assert items == []

    def test_scan_orphaned_snapshot_files(self, setup_daemon_env_with_gc):
        """扫描应找到过期的孤立 snapshot 文件。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg)
        items = gc._scan_orphaned_snapshot_files()
        # 找到 orphan-snap，不包含 new-orphan（未过期）
        paths = [i.key for i in items]
        assert "orphan-snap" in paths
        assert "new-orphan" not in paths
        assert "snap-001" not in paths  # 已注册

    def test_scan_registered_snapshot_excluded(self, setup_daemon_env_with_gc):
        """已注册的 snapshot 文件不应被标记为可回收。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg)
        items = gc._scan_orphaned_snapshot_files()
        # snap-001 已注册，不应出现
        for item in items:
            assert item.key != "snap-001"

    def test_scan_archived_workspaces(self, setup_daemon_env_with_gc):
        """扫描应找到 archived 且过期的 workspace。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg)
        items = gc._scan_orphaned_workspaces()
        keys = [i.key for i in items]
        assert "ws-archived" in keys
        assert "ws-active" not in keys

    def test_scan_expired_backup_history(self, setup_daemon_env_with_gc):
        """扫描应找到已删除和过期的 backup_history 记录。"""
        cfg = setup_daemon_env_with_gc
        # 插入测试数据
        conn = sqlite3.connect(cfg.registry_db_path)
        # 已删除
        conn.execute("""
            INSERT INTO backup_history
            (backup_id, backup_type, created_at, file_count, total_size_bytes, checksum, deleted_at)
            VALUES ('B-deleted', 'full', ?, 1, 100, 'abc', ?)
        """, (time.time() - 1, time.time() - 1))
        # 过期但未删除
        conn.execute("""
            INSERT INTO backup_history
            (backup_id, backup_type, created_at, file_count, total_size_bytes, checksum, deleted_at)
            VALUES ('B-expired', 'full', ?, 1, 200, 'def', 0)
        """, (time.time() - 30 * 24 * 3600,))
        # 正常
        conn.execute("""
            INSERT INTO backup_history
            (backup_id, backup_type, created_at, file_count, total_size_bytes, checksum, deleted_at)
            VALUES ('B-normal', 'full', ?, 1, 300, 'ghi', 0)
        """, (time.time(),))
        conn.commit()
        conn.close()

        gc = SnapshotGC(cfg)
        items = gc._scan_expired_backup_history()
        keys = [i.key for i in items]
        assert "B-deleted" in keys
        assert "B-expired" in keys
        assert "B-normal" not in keys

    def test_scan_expired_audit_logs_disabled(self, setup_daemon_env_with_gc):
        """audit GC 默认禁用，collect_garbage_stats 不应包含 audit_log 类型。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, enable_audit_gc=False)
        items = gc.collect_garbage_stats()
        audit_items = [i for i in items if i.item_type == "audit_log"]
        assert audit_items == []

    def test_scan_expired_audit_logs_enabled(self, setup_daemon_env_with_gc):
        """启用 audit GC 后应找到过期记录。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, enable_audit_gc=True)
        items = gc._scan_expired_audit_logs()
        assert len(items) == 1
        assert items[0].item_type == "audit_log"
        assert items[0].metadata["count"] == 1

    def test_collect_all_types(self, setup_daemon_env_with_gc):
        """collect_garbage_stats 应汇总所有类型。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, enable_audit_gc=True)
        items = gc.collect_garbage_stats()
        types = {i.item_type for i in items}
        # 至少包含 snapshot_file 和 workspace_cache
        assert "snapshot_file" in types
        assert "workspace_cache" in types

    def test_collect_no_snapshots_dir(self, tmp_path):
        """snapshots 目录不存在时应返回空。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        gc = SnapshotGC(cfg)
        assert gc._scan_orphaned_snapshot_files() == []


# ======================================================================
# SnapshotGC sweep 测试
# ======================================================================


class TestSnapshotGCSweep:
    def test_run_gc_dry_run(self, setup_daemon_env_with_gc):
        """dry_run 模式应只统计不删除。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, policy=GCPolicy(dry_run=True))
        stats = gc.run_gc()

        assert stats.marked_count > 0
        assert stats.swept_count == 0  # dry_run 不删除

        # 验证文件未被删除
        snapshot_dir = os.path.join(cfg.data_root, "snapshots")
        orphan_path = os.path.join(snapshot_dir, "orphan-snap")
        assert os.path.exists(orphan_path)

    def test_run_gc_deletes_orphaned_files(self, setup_daemon_env_with_gc):
        """正常 GC 应删除过期的孤立文件。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg)
        stats = gc.run_gc()

        # 找到被删除的 snapshot_file
        swept_files = [s for s in stats.swept if s.item_type == "snapshot_file"]
        assert len(swept_files) > 0

        # orphan-snap 应被删除
        snapshot_dir = os.path.join(cfg.data_root, "snapshots")
        orphan_path = os.path.join(snapshot_dir, "orphan-snap")
        assert not os.path.exists(orphan_path)

        # 已注册的 snap-001 不应被删除
        assert os.path.exists(os.path.join(snapshot_dir, "snap-001"))

        # 新的孤立文件不应被删除（未过期）
        assert os.path.exists(os.path.join(snapshot_dir, "new-orphan"))

    def test_run_gc_deletes_backup_history(self, setup_daemon_env_with_gc):
        """GC 应删除已标记的 backup_history 记录。"""
        cfg = setup_daemon_env_with_gc
        # 插入测试数据
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute("""
            INSERT INTO backup_history
            (backup_id, backup_type, created_at, file_count, total_size_bytes, checksum, deleted_at)
            VALUES ('B-del', 'full', ?, 1, 100, 'x', ?)
        """, (time.time() - 1, time.time() - 1))
        conn.commit()
        conn.close()

        gc = SnapshotGC(cfg)
        gc.run_gc()

        # 验证记录已删除
        conn = sqlite3.connect(cfg.registry_db_path)
        row = conn.execute(
            "SELECT backup_id FROM backup_history WHERE backup_id = ?", ("B-del",)
        ).fetchone()
        conn.close()
        assert row is None

    def test_run_gc_evicts_workspace_cache(self, setup_daemon_env_with_gc):
        """GC 应通过回调驱逐 workspace 缓存。"""
        cfg = setup_daemon_env_with_gc
        evicted = []

        def evictor(workspace_id):
            evicted.append(workspace_id)
            return True

        gc = SnapshotGC(cfg, snapshot_cache_evictor=evictor)
        gc.run_gc()

        assert "ws-archived" in evicted

    def test_run_gc_sweep_failure_recorded(self, setup_daemon_env_with_gc):
        """单个回收失败不应中断整个 GC。"""
        cfg = setup_daemon_env_with_gc
        # 创建一个无法删除的文件（模拟权限错误）
        snapshot_dir = os.path.join(cfg.data_root, "snapshots")
        protected_path = os.path.join(snapshot_dir, "protected-snap")
        with open(protected_path, "w") as f:
            f.write("protected")
        old_time = time.time() - 30 * 24 * 3600
        os.utime(protected_path, (old_time, old_time))

        gc = SnapshotGC(cfg)
        stats = gc.run_gc()

        # 应有 swept 或 failed 记录
        assert stats.marked_count > 0

    def test_run_gc_returns_duration(self, setup_daemon_env_with_gc):
        """GC 结果应包含执行时间。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg)
        stats = gc.run_gc()
        assert stats.duration_ms >= 0


# ======================================================================
# GC 策略测试
# ======================================================================


class TestGCPolicyIntegration:
    def test_short_max_age(self, setup_daemon_env_with_gc):
        """较短的 max_age 应标记更多文件。"""
        cfg = setup_daemon_env_with_gc
        # 让 new-orphan 文件变旧（5 秒前）
        snapshot_dir = os.path.join(cfg.data_root, "snapshots")
        new_orphan_path = os.path.join(snapshot_dir, "new-orphan")
        old_time = time.time() - 5
        os.utime(new_orphan_path, (old_time, old_time))

        # 2 秒 max_age，new-orphan 现在也应过期
        gc = SnapshotGC(cfg, policy=GCPolicy(max_age_seconds=2))
        items = gc.collect_garbage_stats()
        snapshot_items = [i for i in items if i.item_type == "snapshot_file"]
        # 应包含 new-orphan
        assert any(i.key == "new-orphan" for i in snapshot_items)

    def test_long_max_age(self, setup_daemon_env_with_gc):
        """很长的 max_age 应标记更少文件。"""
        cfg = setup_daemon_env_with_gc
        # 365 天 max_age，几乎没有文件过期
        gc = SnapshotGC(cfg, policy=GCPolicy(max_age_seconds=365 * 24 * 3600))
        items = gc.collect_garbage_stats()
        # orphan-snap 是 30 天前，不应被标记
        snapshot_items = [i for i in items if i.item_type == "snapshot_file"]
        assert all(i.key != "orphan-snap" for i in snapshot_items)

    def test_batch_size_limit(self, setup_daemon_env_with_gc):
        """batch_size 应限制单次 sweep 数量。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, policy=GCPolicy(batch_size=1))
        stats = gc.run_gc()
        assert stats.swept_count <= 1


# ======================================================================
# 便捷方法测试
# ======================================================================


class TestGetStatsSummary:
    def test_summary_structure(self, setup_daemon_env_with_gc):
        """get_stats_summary 应返回结构化统计。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, enable_audit_gc=True)
        summary = gc.get_stats_summary()

        assert "total_items" in summary
        assert "by_type" in summary
        assert "total_bytes" in summary
        assert "policy" in summary
        assert summary["total_items"] > 0

    def test_summary_by_type(self, setup_daemon_env_with_gc):
        """by_type 应按类型分类。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, enable_audit_gc=True)
        summary = gc.get_stats_summary()
        assert isinstance(summary["by_type"], dict)
        assert summary["by_type"]  # 非空

    def test_summary_includes_policy(self, setup_daemon_env_with_gc):
        """summary 应包含 policy 配置。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, policy=GCPolicy(retention_count=7, dry_run=True))
        summary = gc.get_stats_summary()
        assert summary["policy"]["retention_count"] == 7
        assert summary["policy"]["dry_run"] is True


# ======================================================================
# 边界情况测试
# ======================================================================


class TestEdgeCases:
    def test_empty_data_root(self, tmp_path):
        """空 data_root 不应崩溃。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        gc = SnapshotGC(cfg)
        stats = gc.run_gc()
        assert stats.marked_count == 0
        assert stats.swept_count == 0

    def test_no_registry_db(self, tmp_path):
        """registry.db 不存在时不应崩溃。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        gc = SnapshotGC(cfg)
        items = gc._scan_orphaned_workspaces()
        assert items == []

    def test_no_backup_history_table(self, tmp_path):
        """backup_history 表不存在时应跳过。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        # 创建空的 registry.db
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute("CREATE TABLE daemon_workspaces (id INTEGER)")
        conn.commit()
        conn.close()

        gc = SnapshotGC(cfg)
        items = gc._scan_expired_backup_history()
        assert items == []

    def test_no_migrations_log_table(self, tmp_path):
        """schema_migrations_log 表不存在时应跳过。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute("CREATE TABLE daemon_workspaces (id INTEGER)")
        conn.commit()
        conn.close()

        gc = SnapshotGC(cfg)
        items = gc._scan_expired_migrations_log()
        assert items == []

    def test_migrations_log_under_keep_count(self, tmp_path):
        """记录数少于 keep_count 时不标记。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.executescript("""
            CREATE TABLE schema_migrations_log (
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
        # 只插入 5 条
        for i in range(5):
            conn.execute("""
                INSERT INTO schema_migrations_log
                (db_name, from_version, to_version, applied_at, duration_ms, status, error)
                VALUES ('registry', ?, ?, ?, 10, 'success', '')
            """, (i, i + 1, time.time()))
        conn.commit()
        conn.close()

        gc = SnapshotGC(cfg, policy=GCPolicy(retention_count=3))
        items = gc._scan_expired_migrations_log()
        assert items == []  # 5 条 < keep_count=30

    def test_migrations_log_over_keep_count(self, tmp_path):
        """记录数超过 keep_count 时应标记最旧的。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.executescript("""
            CREATE TABLE schema_migrations_log (
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
        # 插入 60 条（keep_count 默认 50）
        for i in range(60):
            conn.execute("""
                INSERT INTO schema_migrations_log
                (db_name, from_version, to_version, applied_at, duration_ms, status, error)
                VALUES ('registry', ?, ?, ?, 10, 'success', '')
            """, (i, i + 1, time.time() - (60 - i)))
        conn.commit()
        conn.close()

        gc = SnapshotGC(cfg, policy=GCPolicy(retention_count=3))
        items = gc._scan_expired_migrations_log()
        assert len(items) == 10  # 60 - 50 = 10

    def test_vacuum_db(self, setup_daemon_env_with_gc):
        """启用 vacuum_db 时不应崩溃。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, policy=GCPolicy(vacuum_db=True))
        gc.run_gc()
        # 验证 DB 仍可正常打开
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute("SELECT 1")
        conn.close()
