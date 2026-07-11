"""Phase 8.5: backup/restore 测试。

测试覆盖：
1. BackupManager：全量备份、DB-only 备份、列表、删除、清理
2. RestoreManager：恢复、验证、校验和检查
3. 备份完整性：SHA-256 验证、元数据校验
4. SQLite backup API：一致性备份
5. snapshots 目录备份/恢复
"""

import os
import time
import json
import sqlite3
import shutil
import pytest

from callwarden.server.backup_restore import BackupManager, RestoreManager
from callwarden.server.daemon_config import DaemonConfig


@pytest.fixture
def setup_daemon_env(tmp_path):
    """创建完整的 daemon 环境用于测试。"""
    data_root = str(tmp_path / "data")
    backup_root = str(tmp_path / "backups")
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
    conn.execute("""
        CREATE TABLE daemon_workspaces (
            workspace_id INTEGER PRIMARY KEY,
            workspace_instance_id TEXT,
            status TEXT,
            last_active_at REAL
        )
    """)
    conn.execute("""
        INSERT INTO daemon_workspaces
        (workspace_id, workspace_instance_id, status, last_active_at)
        VALUES (1, 'ws-test-1', 'active', 12345.0)
    """)
    conn.commit()
    conn.close()

    # 创建 audit DB 并插入数据
    audit_path = cfg.audit_log_path
    conn = sqlite3.connect(audit_path)
    conn.execute("""
        CREATE TABLE audit_log (
            event_id TEXT PRIMARY KEY,
            timestamp REAL,
            event_type TEXT,
            actor_uid INTEGER,
            actor_role TEXT,
            action TEXT,
            target TEXT,
            result TEXT,
            details TEXT,
            client_ip TEXT
        )
    """)
    conn.execute("""
        INSERT INTO audit_log
        (event_id, timestamp, event_type, actor_uid, actor_role, action, target, result, details, client_ip)
        VALUES ('A-1', 12345.0, 'admin_operation', 0, 'admin', 'test', '', 'success', '{}', '')
    """)
    conn.commit()
    conn.close()

    # 创建 snapshots 目录
    snapshot_dir = os.path.join(data_root, "snapshots")
    os.makedirs(snapshot_dir, exist_ok=True)
    with open(os.path.join(snapshot_dir, "snap_0.bin"), "w") as f:
        f.write("snapshot content")

    return cfg, backup_root


# ============================================================
# BackupManager 测试
# ============================================================


class TestBackupManagerFull:
    """BackupManager 全量备份测试。"""

    def test_backup_full_returns_meta(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        assert "backup_id" in result
        assert result["backup_type"] == "full"
        assert "timestamp" in result
        assert "files" in result
        assert "checksum" in result

    def test_backup_full_creates_directory(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        backup_dir = os.path.join(backup_root, result["backup_id"])
        assert os.path.isdir(backup_dir)

    def test_backup_full_includes_registry_db(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        file_names = [f["name"] for f in result["files"]]
        assert "registry.db" in file_names

    def test_backup_full_includes_audit_db(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        file_names = [f["name"] for f in result["files"]]
        assert "audit.db" in file_names

    def test_backup_full_includes_snapshots(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        file_names = [f["name"] for f in result["files"]]
        assert "snapshots/" in file_names

    def test_backup_full_has_sha256(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        for f in result["files"]:
            if f.get("type") == "file":
                assert "sha256" in f
                assert len(f["sha256"]) == 64  # SHA-256 hex

    def test_backup_full_has_backup_meta(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        meta_path = os.path.join(backup_root, result["backup_id"], "backup_meta.json")
        assert os.path.isfile(meta_path)

        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["backup_id"] == result["backup_id"]

    def test_backup_id_format(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        backup_id = result["backup_id"]
        assert backup_id.startswith("B-")
        parts = backup_id.split("-")
        assert len(parts) == 3
        assert len(parts[1]) == 13
        assert len(parts[2]) == 4

    def test_backup_id_uniqueness(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        r1 = mgr.backup_full()
        r2 = mgr.backup_full()
        assert r1["backup_id"] != r2["backup_id"]

    def test_backup_custom_id(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full(backup_id="custom-backup-001")
        assert result["backup_id"] == "custom-backup-001"


class TestBackupManagerDbOnly:
    """BackupManager DB-only 备份测试。"""

    def test_backup_db_only_returns_meta(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_db_only()

        assert result["backup_type"] == "db_only"
        assert "files" in result

    def test_backup_db_only_excludes_snapshots(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_db_only()

        file_names = [f["name"] for f in result["files"]]
        assert "snapshots/" not in file_names

    def test_backup_db_only_includes_db_files(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_db_only()

        file_names = [f["name"] for f in result["files"]]
        assert "registry.db" in file_names
        assert "audit.db" in file_names


class TestBackupManagerList:
    """BackupManager 列表测试。"""

    def test_list_backups_empty(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        assert mgr.list_backups() == []

    def test_list_backups_after_backup(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        mgr.backup_full()
        mgr.backup_full()

        backups = mgr.list_backups()
        assert len(backups) == 2

    def test_list_backups_sorted_by_time_desc(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        r1 = mgr.backup_full()
        time.sleep(0.01)
        r2 = mgr.backup_full()

        backups = mgr.list_backups()
        assert backups[0]["timestamp"] >= backups[1]["timestamp"]

    def test_get_backup_info(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        info = mgr.get_backup_info(result["backup_id"])
        assert info is not None
        assert info["backup_id"] == result["backup_id"]

    def test_get_backup_info_not_found(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        assert mgr.get_backup_info("nonexistent") is None


class TestBackupManagerDelete:
    """BackupManager 删除测试。"""

    def test_delete_backup(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        assert mgr.delete_backup(result["backup_id"]) is True
        assert mgr.get_backup_info(result["backup_id"]) is None

    def test_delete_backup_not_found(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        assert mgr.delete_backup("nonexistent") is False

    def test_cleanup_old_backups(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        for _ in range(7):
            mgr.backup_full()
            time.sleep(0.01)

        deleted = mgr.cleanup_old_backups(keep_count=3)
        assert deleted == 4
        assert len(mgr.list_backups()) == 3

    def test_cleanup_nothing_when_under_limit(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        mgr.backup_full()

        deleted = mgr.cleanup_old_backups(keep_count=5)
        assert deleted == 0


# ============================================================
# RestoreManager 测试
# ============================================================


class TestRestoreManagerRestore:
    """RestoreManager 恢复测试。"""

    def test_restore_returns_success(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()
        restore_result = restore_mgr.restore(backup_result["backup_id"])

        assert restore_result["status"] == "success"
        assert "restored_files" in restore_result

    def test_restore_restores_registry_db(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()

        # 删除 registry DB
        os.remove(cfg.registry_db_path)

        restore_result = restore_mgr.restore(backup_result["backup_id"])
        assert restore_result["status"] == "success"

        # registry DB 应该恢复
        assert os.path.isfile(cfg.registry_db_path)

        # 验证数据
        conn = sqlite3.connect(cfg.registry_db_path)
        cursor = conn.execute("SELECT * FROM daemon_workspaces")
        rows = cursor.fetchall()
        assert len(rows) == 1
        conn.close()

    def test_restore_restores_audit_db(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()

        # 删除 audit DB
        os.remove(cfg.audit_log_path)

        restore_result = restore_mgr.restore(backup_result["backup_id"])
        assert restore_result["status"] == "success"
        assert os.path.isfile(cfg.audit_log_path)

    def test_restore_restores_snapshots(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()

        # 删除 snapshots 目录
        snapshot_dir = os.path.join(cfg.data_root, "snapshots")
        shutil.rmtree(snapshot_dir)

        restore_result = restore_mgr.restore(backup_result["backup_id"])
        assert restore_result["status"] == "success"
        assert os.path.isdir(snapshot_dir)

    def test_restore_not_found(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        result = restore_mgr.restore("nonexistent")
        assert result["status"] == "failure"
        assert "not found" in result["error"]

    def test_restore_no_meta_file(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        # 创建一个没有 meta 文件的备份目录
        fake_dir = os.path.join(backup_root, "fake-backup")
        os.makedirs(fake_dir, exist_ok=True)

        result = restore_mgr.restore("fake-backup")
        assert result["status"] == "failure"
        assert "backup_meta.json" in result["error"]

    def test_restore_file_status(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()
        restore_result = restore_mgr.restore(backup_result["backup_id"])

        # 检查每个文件的状态
        for f in restore_result["restored_files"]:
            assert f["status"] in ("restored", "skipped", "failed")


class TestRestoreManagerVerify:
    """RestoreManager 验证测试。"""

    def test_verify_backup_valid(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()
        verify_result = restore_mgr.verify_backup(backup_result["backup_id"])

        assert verify_result["status"] == "valid"
        assert "files" in verify_result

    def test_verify_backup_not_found(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        result = restore_mgr.verify_backup("nonexistent")
        assert result["status"] == "invalid"

    def test_verify_backup_no_meta(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        # 创建一个没有 meta 文件的备份目录
        fake_dir = os.path.join(backup_root, "fake-backup")
        os.makedirs(fake_dir, exist_ok=True)

        result = restore_mgr.verify_backup("fake-backup")
        assert result["status"] == "invalid"

    def test_verify_backup_corrupted_file(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()

        # 篡改备份文件
        backup_dir = os.path.join(backup_root, backup_result["backup_id"])
        registry_path = os.path.join(backup_dir, "registry.db")
        with open(registry_path, "a") as f:
            f.write("corruption")

        result = restore_mgr.verify_backup(backup_result["backup_id"])
        assert result["status"] == "corrupted"

    def test_verify_checks_each_file(self, setup_daemon_env):
        cfg, backup_root = setup_daemon_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()
        verify_result = restore_mgr.verify_backup(backup_result["backup_id"])

        for f in verify_result["files"]:
            assert "name" in f
            assert "valid" in f


# ============================================================
# 备份一致性测试
# ============================================================


class TestBackupConsistency:
    """备份一致性测试。"""

    def test_sqlite_backup_consistency(self, setup_daemon_env):
        """SQLite backup API 应创建一致性副本。"""
        cfg, backup_root = setup_daemon_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()
        backup_dir = os.path.join(backup_root, backup_result["backup_id"])
        backup_registry = os.path.join(backup_dir, "registry.db")

        # 备份的 DB 应该可以正常打开和查询
        conn = sqlite3.connect(backup_registry)
        cursor = conn.execute("SELECT * FROM daemon_workspaces")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "ws-test-1"
        conn.close()

    def test_checksum_validation(self, setup_daemon_env):
        """备份的 checksum 应该与元数据匹配。"""
        cfg, backup_root = setup_daemon_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        backup_result = backup_mgr.backup_full()
        verify_result = restore_mgr.verify_backup(backup_result["backup_id"])

        assert verify_result["status"] == "valid"

    def test_restore_preserves_data_integrity(self, setup_daemon_env):
        """恢复后数据应与备份前一致。"""
        cfg, backup_root = setup_daemon_env
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)

        # 备份
        backup_result = backup_mgr.backup_full()

        # 修改 registry DB
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute("DELETE FROM daemon_workspaces")
        conn.execute("""
            INSERT INTO daemon_workspaces
            (workspace_id, workspace_instance_id, status, last_active_at)
            VALUES (2, 'ws-modified', 'active', 99999.0)
        """)
        conn.commit()
        conn.close()

        # 恢复
        restore_result = restore_mgr.restore(backup_result["backup_id"])
        assert restore_result["status"] == "success"

        # 恢复后数据应该是备份时的状态
        conn = sqlite3.connect(cfg.registry_db_path)
        cursor = conn.execute("SELECT * FROM daemon_workspaces")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "ws-test-1"
        conn.close()


# ============================================================
# 边界情况测试
# ============================================================


class TestBackupEdgeCases:
    """边界情况测试。"""

    def test_backup_with_no_audit_db(self, tmp_path):
        """audit DB 不存在时备份不应失败。"""
        data_root = str(tmp_path / "data")
        backup_root = str(tmp_path / "backups")
        os.makedirs(data_root, exist_ok=True)

        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {
                "admin_uids": [0],
                "audit_log_path": os.path.join(data_root, "audit.db"),
            },
        })

        # 只创建 registry DB
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute("CREATE TABLE daemon_workspaces (id INTEGER)")
        conn.commit()
        conn.close()

        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        file_names = [f["name"] for f in result["files"]]
        assert "registry.db" in file_names
        # audit.db 不存在，不应在备份中
        assert "audit.db" not in file_names

    def test_backup_with_no_snapshots_dir(self, tmp_path):
        """snapshots 目录不存在时备份不应失败。"""
        data_root = str(tmp_path / "data")
        backup_root = str(tmp_path / "backups")
        os.makedirs(data_root, exist_ok=True)

        cfg = DaemonConfig.load_from_dict({"data_root": data_root})

        # 创建 registry DB
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute("CREATE TABLE daemon_workspaces (id INTEGER)")
        conn.commit()
        conn.close()

        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        file_names = [f["name"] for f in result["files"]]
        assert "snapshots/" not in file_names

    def test_list_backups_no_dir(self, tmp_path):
        """备份目录不存在时 list_backups 返回空列表。"""
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        mgr = BackupManager(cfg, backup_root=str(tmp_path / "nonexistent"))
        assert mgr.list_backups() == []

    def test_default_backup_root(self, tmp_path):
        """不指定 backup_root 时使用 data_root/backups。"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({"data_root": data_root})

        # 创建 registry DB
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute("CREATE TABLE daemon_workspaces (id INTEGER)")
        conn.commit()
        conn.close()

        mgr = BackupManager(cfg)
        result = mgr.backup_full()

        expected_root = os.path.join(data_root, "backups")
        assert os.path.isdir(expected_root)

    def test_multiple_backups_independent(self, setup_daemon_env):
        """多个备份应相互独立。"""
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)

        r1 = mgr.backup_full()
        r2 = mgr.backup_full()

        assert r1["backup_id"] != r2["backup_id"]
        assert mgr.get_backup_info(r1["backup_id"]) is not None
        assert mgr.get_backup_info(r2["backup_id"]) is not None
