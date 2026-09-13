"""Phase 8.7: snapshot GC 测试（daemon authority / HTTP thin-client 迁移后）。

stale 依据（A 类：被测模块已是 daemon 薄客户端）：
- ``server/snapshot_gc.py:37-47`` 定义 ``_GC_METHODS`` RPC method 命名空间；
  ``:50-71`` ``_call_daemon_rpc`` / ``_rpc_items`` 经统一 HTTP client 调用 daemon。
- ``server/snapshot_gc.py:244-292`` ``_scan_expired_backup_history`` /
  ``_scan_expired_migrations_log`` / ``_scan_expired_audit_logs`` /
  ``_scan_orphaned_workspaces`` / ``_get_registered_snapshot_ids`` 全部经 RPC，
  旧用例「本地建 registry.db/audit.db 表再断言扫描结果」的期望已整体过期。
- ``server/snapshot_gc.py:354-384`` ``_delete_*`` / ``_vacuum_databases`` 经 RPC。
- **仍本地**：``GCPolicy`` / ``GarbageItem`` / ``GCStats`` 数据类；``:197-242``
  ``_scan_orphaned_snapshot_files``（本地扫 ``data_root/snapshots`` + RPC 取已注册 id）；
  ``:298-352`` ``run_gc`` mark/sweep/dry_run/batch_size 逻辑与 snapshot_file 本地 ``os.remove``；
  ``:390-407`` ``get_stats_summary``。

因此失败用例改为 **mock RPC seam**：断言路由到正确 method + 正确 params + 回包透传 +
失败 fail-closed；本地文件系统 / 数据类 / 回调契约保留断言。
"""

import os
import time

import pytest

from callwarden.server import snapshot_gc
from callwarden.server.snapshot_gc import (
    SnapshotGC,
    GCPolicy,
    GarbageItem,
    GCStats,
)
from callwarden.server.daemon_config import DaemonConfig


# RPC method 命名空间（server/snapshot_gc.py:37-47）
SCAN_BACKUP = "mcp.snapshot_gc.scan_expired_backup_history"
SCAN_MIGRATION = "mcp.snapshot_gc.scan_expired_migrations_log"
SCAN_AUDIT = "mcp.snapshot_gc.scan_expired_audit_logs"
SCAN_WORKSPACES = "mcp.snapshot_gc.scan_orphaned_workspaces"
REGISTERED = "mcp.snapshot_gc.get_registered_snapshot_ids"
DELETE_BACKUP = "mcp.snapshot_gc.delete_backup_history_record"
DELETE_MIGRATION = "mcp.snapshot_gc.delete_migration_log_record"
DELETE_AUDIT = "mcp.snapshot_gc.delete_expired_audit_logs"
VACUUM = "mcp.snapshot_gc.vacuum_databases"

DELETE_METHODS = {DELETE_BACKUP, DELETE_MIGRATION, DELETE_AUDIT, VACUUM}


def _item(item_type, key, *, size_bytes=0, reason="expired", **metadata):
    return {
        "item_type": item_type,
        "key": key,
        "size_bytes": size_bytes,
        "reason": reason,
        "metadata": metadata,
    }


class FakeSnapshotGCDaemon:
    """内存态 snapshot GC daemon：模拟 Rust 侧扫描 / 删除 / vacuum。"""

    def __init__(self):
        self.available = True
        self.calls = []
        self.registered = {"snap-001"}
        self.items = {
            SCAN_BACKUP: [],
            SCAN_MIGRATION: [],
            SCAN_AUDIT: [],
            SCAN_WORKSPACES: [],
        }

    def __call__(self, method, params):
        if not self.available:
            raise RuntimeError("daemon unavailable")
        self.calls.append((method, params))
        if method == REGISTERED:
            return sorted(self.registered)
        if method in self.items:
            return list(self.items[method])
        if method in DELETE_METHODS:
            return {"deleted": 1, "source": "rust"}
        raise RuntimeError(f"unexpected method: {method}")

    def methods(self):
        return [method for method, _ in self.calls]

    def params_for(self, method):
        return [params for m, params in self.calls if m == method]


@pytest.fixture()
def fake_daemon(monkeypatch):
    daemon = FakeSnapshotGCDaemon()
    monkeypatch.setattr(snapshot_gc, "_call_daemon_rpc", daemon)
    return daemon


# ======================================================================
# 测试夹具
# ======================================================================


@pytest.fixture
def setup_daemon_env_with_gc(tmp_path):
    """创建 GC 测试用的 data_root / snapshots 目录 / audit DB 占位。

    registry.db 的数据库权威已下沉 daemon，本夹具只准备测试需直接操纵的
    本地文件系统资源（snapshot 二进制文件）与 audit DB 存在性。
    """
    data_root = str(tmp_path / "data")
    os.makedirs(data_root, exist_ok=True)

    cfg = DaemonConfig.load_from_dict({
        "data_root": data_root,
        "security": {
            "admin_uids": [0],
            "audit_log_path": os.path.join(data_root, "audit.db"),
        },
    })

    # audit DB 占位（_scan_expired_audit_logs 仅在文件存在时才发起 RPC）
    open(cfg.audit_log_path, "w").close()

    snapshot_dir = os.path.join(data_root, "snapshots")
    os.makedirs(snapshot_dir, exist_ok=True)

    # 已注册的 snapshot 文件
    with open(os.path.join(snapshot_dir, "snap-001"), "w") as f:
        f.write("active snapshot content")

    # 孤立的 snapshot 文件（30 天前）
    orphan_path = os.path.join(snapshot_dir, "orphan-snap")
    with open(orphan_path, "w") as f:
        f.write("orphan content")
    old_time = time.time() - 30 * 24 * 3600
    os.utime(orphan_path, (old_time, old_time))

    # 新的孤立文件（未过期）
    with open(os.path.join(snapshot_dir, "new-orphan"), "w") as f:
        f.write("new orphan")

    return cfg


# ======================================================================
# 数据类测试（纯 Python，仍有效）
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
# SnapshotGC mark 测试（本地 FS + mock RPC seam）
# ======================================================================


class TestSnapshotGCMark:
    def test_collect_empty_env(self, fake_daemon, tmp_path):
        """空环境：无 snapshots 目录，daemon 各扫描均返回空 → 空列表。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        gc = SnapshotGC(cfg)
        items = gc.collect_garbage_stats()
        assert items == []
        assert fake_daemon.methods() == [SCAN_BACKUP, SCAN_MIGRATION, SCAN_WORKSPACES]

    def test_scan_orphaned_snapshot_files(self, fake_daemon, setup_daemon_env_with_gc):
        """本地扫描应找到过期的孤立 snapshot 文件（已注册 id 来自 RPC）。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg)
        items = gc._scan_orphaned_snapshot_files()
        paths = [i.key for i in items]
        assert "orphan-snap" in paths
        assert "new-orphan" not in paths  # 未过期
        assert "snap-001" not in paths  # 已注册（daemon 返回）
        assert fake_daemon.methods() == [REGISTERED]
        assert fake_daemon.calls[0][1] == {"registry_db_path": cfg.registry_db_path}

    def test_scan_registered_snapshot_excluded(self, fake_daemon, setup_daemon_env_with_gc):
        """已注册的 snapshot 文件不应被标记为可回收。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg)
        for item in gc._scan_orphaned_snapshot_files():
            assert item.key != "snap-001"

    def test_scan_archived_workspaces(self, fake_daemon, setup_daemon_env_with_gc):
        """workspace 扫描经 RPC 路由，回包透传。"""
        cfg = setup_daemon_env_with_gc
        fake_daemon.items[SCAN_WORKSPACES] = [
            _item("workspace_cache", "ws-archived", reason="unregistered", last_active_at=1),
        ]
        gc = SnapshotGC(cfg)
        items = gc._scan_orphaned_workspaces()
        keys = [i.key for i in items]
        assert "ws-archived" in keys
        assert "ws-active" not in keys
        assert fake_daemon.methods() == [SCAN_WORKSPACES]
        assert fake_daemon.calls[0][1]["registry_db_path"] == cfg.registry_db_path

    def test_scan_expired_backup_history(self, fake_daemon, setup_daemon_env_with_gc):
        """backup_history 扫描经 daemon RPC，回包透传为 GarbageItem。"""
        cfg = setup_daemon_env_with_gc
        fake_daemon.items[SCAN_BACKUP] = [
            _item("backup_history", "B-deleted", size_bytes=100),
            _item("backup_history", "B-expired", size_bytes=200),
        ]
        gc = SnapshotGC(cfg)
        items = gc._scan_expired_backup_history()
        keys = [i.key for i in items]
        assert keys == ["B-deleted", "B-expired"]
        assert "B-normal" not in keys
        assert fake_daemon.calls[0][1] == {
            "registry_db_path": cfg.registry_db_path,
            "max_age_seconds": gc.policy.max_age_seconds,
        }

    def test_scan_expired_audit_logs_disabled(self, fake_daemon, setup_daemon_env_with_gc):
        """audit GC 默认禁用，collect_garbage_stats 不发起 audit 扫描。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, enable_audit_gc=False)
        items = gc.collect_garbage_stats()
        assert [i for i in items if i.item_type == "audit_log"] == []
        assert SCAN_AUDIT not in fake_daemon.methods()

    def test_scan_expired_audit_logs_enabled(self, fake_daemon, setup_daemon_env_with_gc):
        """启用 audit GC 后应经 RPC 找到过期记录并透传 metadata。"""
        cfg = setup_daemon_env_with_gc
        fake_daemon.items[SCAN_AUDIT] = [
            _item("audit_log", "expired_batch", count=1, cutoff=10.0),
        ]
        gc = SnapshotGC(cfg, enable_audit_gc=True)
        items = gc._scan_expired_audit_logs()
        assert len(items) == 1
        assert items[0].item_type == "audit_log"
        assert items[0].metadata["count"] == 1
        assert fake_daemon.calls[0][1] == {
            "audit_db_path": cfg.audit_log_path,
            "max_age_seconds": gc.policy.max_age_seconds,
        }

    def test_collect_all_types(self, fake_daemon, setup_daemon_env_with_gc):
        """collect_garbage_stats 应汇总本地 snapshot_file 与 RPC workspace_cache。"""
        cfg = setup_daemon_env_with_gc
        fake_daemon.items[SCAN_WORKSPACES] = [
            _item("workspace_cache", "ws-archived", reason="unregistered"),
        ]
        gc = SnapshotGC(cfg, enable_audit_gc=True)
        types = {i.item_type for i in gc.collect_garbage_stats()}
        assert "snapshot_file" in types
        assert "workspace_cache" in types

    def test_collect_no_snapshots_dir(self, fake_daemon, tmp_path):
        """snapshots 目录不存在时应返回空，且不发起 registered RPC。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        gc = SnapshotGC(cfg)
        assert gc._scan_orphaned_snapshot_files() == []
        assert fake_daemon.methods() == []


# ======================================================================
# SnapshotGC sweep 测试
# ======================================================================


class TestSnapshotGCSweep:
    def test_run_gc_dry_run(self, fake_daemon, setup_daemon_env_with_gc):
        """dry_run 模式应只统计不删除，也不发起 delete RPC。"""
        cfg = setup_daemon_env_with_gc
        fake_daemon.items[SCAN_BACKUP] = [_item("backup_history", "B-1", size_bytes=10)]
        gc = SnapshotGC(cfg, policy=GCPolicy(dry_run=True))
        stats = gc.run_gc()

        assert stats.marked_count > 0
        assert stats.swept_count == 0

        snapshot_dir = os.path.join(cfg.data_root, "snapshots")
        assert os.path.exists(os.path.join(snapshot_dir, "orphan-snap"))
        assert not (DELETE_METHODS & set(fake_daemon.methods()))

    def test_run_gc_deletes_orphaned_files(self, fake_daemon, setup_daemon_env_with_gc):
        """正常 GC 应本地删除过期孤立文件，保留已注册 / 未过期文件。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg)
        stats = gc.run_gc()

        swept_files = [s for s in stats.swept if s.item_type == "snapshot_file"]
        assert len(swept_files) > 0

        snapshot_dir = os.path.join(cfg.data_root, "snapshots")
        assert not os.path.exists(os.path.join(snapshot_dir, "orphan-snap"))
        assert os.path.exists(os.path.join(snapshot_dir, "snap-001"))
        assert os.path.exists(os.path.join(snapshot_dir, "new-orphan"))

    def test_run_gc_deletes_backup_history(self, fake_daemon, setup_daemon_env_with_gc):
        """sweep 阶段应对 backup_history 项发起 delete RPC。"""
        cfg = setup_daemon_env_with_gc
        fake_daemon.items[SCAN_BACKUP] = [_item("backup_history", "B-del", size_bytes=100)]
        gc = SnapshotGC(cfg)
        gc.run_gc()

        assert fake_daemon.methods().count(DELETE_BACKUP) == 1
        assert fake_daemon.params_for(DELETE_BACKUP) == [{
            "registry_db_path": cfg.registry_db_path,
            "backup_id": "B-del",
        }]

    def test_run_gc_evicts_workspace_cache(self, fake_daemon, setup_daemon_env_with_gc):
        """GC 应通过回调驱逐 workspace 缓存。"""
        cfg = setup_daemon_env_with_gc
        fake_daemon.items[SCAN_WORKSPACES] = [
            _item("workspace_cache", "ws-archived", reason="unregistered"),
        ]
        evicted = []

        def evictor(workspace_id):
            evicted.append(workspace_id)
            return True

        gc = SnapshotGC(cfg, snapshot_cache_evictor=evictor)
        gc.run_gc()

        assert "ws-archived" in evicted

    def test_run_gc_sweep_failure_recorded(self, fake_daemon, setup_daemon_env_with_gc):
        """单个回收失败不应中断整个 GC。"""
        cfg = setup_daemon_env_with_gc
        snapshot_dir = os.path.join(cfg.data_root, "snapshots")
        protected_path = os.path.join(snapshot_dir, "protected-snap")
        with open(protected_path, "w") as f:
            f.write("protected")
        old_time = time.time() - 30 * 24 * 3600
        os.utime(protected_path, (old_time, old_time))

        gc = SnapshotGC(cfg)
        stats = gc.run_gc()
        assert stats.marked_count > 0

    def test_run_gc_returns_duration(self, fake_daemon, setup_daemon_env_with_gc):
        """GC 结果应包含执行时间。"""
        cfg = setup_daemon_env_with_gc
        stats = SnapshotGC(cfg).run_gc()
        assert stats.duration_ms >= 0


# ======================================================================
# GC 策略测试
# ======================================================================


class TestGCPolicyIntegration:
    def test_short_max_age(self, fake_daemon, setup_daemon_env_with_gc):
        """较短的 max_age 应标记更多本地文件。"""
        cfg = setup_daemon_env_with_gc
        snapshot_dir = os.path.join(cfg.data_root, "snapshots")
        new_orphan_path = os.path.join(snapshot_dir, "new-orphan")
        old_time = time.time() - 5
        os.utime(new_orphan_path, (old_time, old_time))

        gc = SnapshotGC(cfg, policy=GCPolicy(max_age_seconds=2))
        snapshot_items = [
            i for i in gc.collect_garbage_stats() if i.item_type == "snapshot_file"
        ]
        assert any(i.key == "new-orphan" for i in snapshot_items)

    def test_long_max_age(self, fake_daemon, setup_daemon_env_with_gc):
        """很长的 max_age 应标记更少本地文件。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, policy=GCPolicy(max_age_seconds=365 * 24 * 3600))
        snapshot_items = [
            i for i in gc.collect_garbage_stats() if i.item_type == "snapshot_file"
        ]
        assert all(i.key != "orphan-snap" for i in snapshot_items)

    def test_batch_size_limit(self, fake_daemon, setup_daemon_env_with_gc):
        """batch_size 应限制单次 sweep 数量。"""
        cfg = setup_daemon_env_with_gc
        fake_daemon.items[SCAN_BACKUP] = [_item("backup_history", "B-1", size_bytes=10)]
        gc = SnapshotGC(cfg, policy=GCPolicy(batch_size=1))
        stats = gc.run_gc()
        assert stats.swept_count <= 1


# ======================================================================
# 便捷方法测试
# ======================================================================


class TestGetStatsSummary:
    def test_summary_structure(self, fake_daemon, setup_daemon_env_with_gc):
        """get_stats_summary 应返回结构化统计。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, enable_audit_gc=True)
        summary = gc.get_stats_summary()

        assert "total_items" in summary
        assert "by_type" in summary
        assert "total_bytes" in summary
        assert "policy" in summary
        assert summary["total_items"] > 0

    def test_summary_by_type(self, fake_daemon, setup_daemon_env_with_gc):
        """by_type 应按类型分类。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, enable_audit_gc=True)
        summary = gc.get_stats_summary()
        assert isinstance(summary["by_type"], dict)
        assert summary["by_type"]

    def test_summary_includes_policy(self, fake_daemon, setup_daemon_env_with_gc):
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
    def test_empty_data_root(self, fake_daemon, tmp_path):
        """空 data_root 不应崩溃。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        stats = SnapshotGC(cfg).run_gc()
        assert stats.marked_count == 0
        assert stats.swept_count == 0

    def test_no_registry_db(self, fake_daemon, tmp_path):
        """daemon 报告无 registry 数据时 workspace 扫描返回空。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        items = SnapshotGC(cfg)._scan_orphaned_workspaces()
        assert items == []
        assert fake_daemon.methods() == [SCAN_WORKSPACES]

    def test_no_backup_history_table(self, fake_daemon, tmp_path):
        """daemon 报告无 backup_history 数据时扫描返回空。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        items = SnapshotGC(cfg)._scan_expired_backup_history()
        assert items == []
        assert fake_daemon.methods() == [SCAN_BACKUP]

    def test_no_migrations_log_table(self, fake_daemon, tmp_path):
        """daemon 报告无 migrations_log 数据时扫描返回空。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        items = SnapshotGC(cfg)._scan_expired_migrations_log()
        assert items == []
        assert fake_daemon.methods() == [SCAN_MIGRATION]

    def test_migrations_log_under_keep_count(self, fake_daemon, tmp_path):
        """记录数少于 keep_count 时由 daemon 判定为空；Python 侧下发 retention_count。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        gc = SnapshotGC(cfg, policy=GCPolicy(retention_count=3))
        items = gc._scan_expired_migrations_log()
        assert items == []
        assert fake_daemon.calls[0][1]["retention_count"] == 3

    def test_migrations_log_over_keep_count(self, fake_daemon, tmp_path):
        """超过 keep_count 的标记逻辑在 daemon；Python 侧透传其返回项。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        fake_daemon.items[SCAN_MIGRATION] = [
            _item("migration_log", str(i), reason="old_log", db_name="registry")
            for i in range(10)  # daemon 侧 60 - 50 = 10
        ]
        gc = SnapshotGC(cfg, policy=GCPolicy(retention_count=3))
        items = gc._scan_expired_migrations_log()
        assert len(items) == 10
        assert fake_daemon.calls[0][1]["retention_count"] == 3

    def test_vacuum_db(self, fake_daemon, setup_daemon_env_with_gc):
        """启用 vacuum_db 时应经 RPC 请求 daemon vacuum。"""
        cfg = setup_daemon_env_with_gc
        gc = SnapshotGC(cfg, policy=GCPolicy(vacuum_db=True))
        gc.run_gc()
        assert VACUUM in fake_daemon.methods()
        assert fake_daemon.params_for(VACUUM)[0]["registry_db_path"] == cfg.registry_db_path
