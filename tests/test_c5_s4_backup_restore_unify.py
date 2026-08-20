"""C5/S4：backup/restore 统一（C8/B1-B8）差分与回退路径测试。

覆盖：
- B1: get_backup_info 经 Rust list_backups 过滤（明确等价）
- B2: RestoreManager checksum 走 Rust 短路 + fail-soft 降级
- B3: Python 回退 backup_full 补齐 daemon.json（布局与 Rust 默认一致）
- B4: Python 回退原子发布（临时目录 + rename + 重复 ID fail-closed + 失败清理）
- B8: meta checksum Rust/Python byte-for-byte 一致（契约固化）

契约：docs/design/c5-replicator-snapshot-disaster-recovery-contract.md §2.3/§3 C8
"""

import json
import os
import pytest

from callwarden.server import backup_restore as br
from callwarden.server.backup_restore import BackupManager, RestoreManager
from callwarden.server.daemon_config import DaemonConfig


@pytest.fixture
def setup_daemon_env(tmp_path):
    """创建完整的 daemon 环境（registry/audit/cas DB + snapshots + daemon.json）。"""
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

    # registry DB
    import sqlite3
    conn = sqlite3.connect(cfg.registry_db_path)
    conn.execute(
        "CREATE TABLE daemon_workspaces (workspace_id INTEGER PRIMARY KEY, "
        "workspace_instance_id TEXT, status TEXT, last_active_at REAL)"
    )
    conn.execute(
        "INSERT INTO daemon_workspaces VALUES (1, 'ws-test-1', 'active', 12345.0)"
    )
    conn.commit()
    conn.close()

    # audit DB
    conn = sqlite3.connect(cfg.audit_log_path)
    conn.execute(
        "CREATE TABLE audit_log (event_id TEXT PRIMARY KEY, timestamp REAL, "
        "event_type TEXT, action TEXT, result TEXT)"
    )
    conn.commit()
    conn.close()

    # snapshots 目录
    snapshot_dir = os.path.join(data_root, "snapshots")
    os.makedirs(snapshot_dir, exist_ok=True)
    with open(os.path.join(snapshot_dir, "snap_0.bin"), "w") as f:
        f.write("snapshot content")

    # daemon.json（B3 需要）
    with open(os.path.join(data_root, "daemon.json"), "w", encoding="utf-8") as f:
        json.dump({"version": "1.0.0", "mode": "test"}, f)

    return cfg, backup_root


@pytest.fixture
def force_fallback(monkeypatch):
    """强制走 Python 回退路径（模拟 Rust 不可用）。"""
    monkeypatch.setattr(br, "_RUST_BACKUP_MANAGER_AVAILABLE", False)
    monkeypatch.setattr(br, "_RUST_BACKUP_AVAILABLE", False)
    yield


# ============================================================
# B1: get_backup_info 经 Rust list_backups 过滤（明确等价）
# ============================================================


class TestGetBackupInfoRustShortcut:
    def test_get_backup_info_routes_via_list_backups(self, setup_daemon_env, monkeypatch):
        """Rust 可用时 get_backup_info 经 list_backups 过滤。"""
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        calls = []
        orig = BackupManager.list_backups

        def spy(self):
            calls.append(1)
            return orig(self)

        monkeypatch.setattr(BackupManager, "list_backups", spy)
        info = mgr.get_backup_info(result["backup_id"])
        assert calls == [1]  # 确实走了 list_backups
        assert info is not None
        assert info["backup_id"] == result["backup_id"]

    def test_get_backup_info_not_found_via_list_backups(self, setup_daemon_env, monkeypatch):
        """不存在的备份经 Rust 过滤返回 None。"""
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        mgr.backup_full()

        calls = []
        orig = BackupManager.list_backups

        def spy(self):
            calls.append(1)
            return orig(self)

        monkeypatch.setattr(BackupManager, "list_backups", spy)
        assert mgr.get_backup_info("nonexistent") is None
        assert calls == [1]


# ============================================================
# B2: RestoreManager checksum 走 Rust 短路 + fail-soft
# ============================================================


class TestRestoreManagerChecksumRust:
    def test_compute_file_sha256_uses_rust(self, setup_daemon_env, monkeypatch):
        cfg, backup_root = setup_daemon_env
        rm = RestoreManager(cfg, backup_root=backup_root)

        monkeypatch.setattr(
            br._callwarden_core,
            "backup_compute_file_sha256",
            lambda path: "rust-sha",
        )
        assert rm._compute_file_sha256(cfg.registry_db_path) == "rust-sha"

    def test_compute_file_sha256_fail_soft(self, setup_daemon_env, monkeypatch):
        cfg, backup_root = setup_daemon_env
        rm = RestoreManager(cfg, backup_root=backup_root)

        def boom(_path):
            raise RuntimeError("rust unavailable")

        monkeypatch.setattr(br._callwarden_core, "backup_compute_file_sha256", boom)
        result = rm._compute_file_sha256(cfg.registry_db_path)
        assert len(result) == 64  # 降级到 Python hashlib sha256

    def test_compute_meta_checksum_uses_rust(self, setup_daemon_env, monkeypatch):
        cfg, backup_root = setup_daemon_env
        rm = RestoreManager(cfg, backup_root=backup_root)

        meta = {"backup_id": "B-1", "timestamp": 1.0, "files": []}
        monkeypatch.setattr(
            br._callwarden_core,
            "backup_compute_meta_checksum",
            lambda s: "rust-meta-cs",
        )
        assert rm._compute_meta_checksum(meta) == "rust-meta-cs"

    def test_compute_meta_checksum_fail_soft(self, setup_daemon_env, monkeypatch):
        cfg, backup_root = setup_daemon_env
        rm = RestoreManager(cfg, backup_root=backup_root)

        def boom(_s):
            raise RuntimeError("rust unavailable")

        monkeypatch.setattr(br._callwarden_core, "backup_compute_meta_checksum", boom)
        meta = {"backup_id": "B-1", "timestamp": 1.0, "files": []}
        result = rm._compute_meta_checksum(meta)
        assert len(result) == 64  # 降级到 Python hashlib sha256


# ============================================================
# B3: 回退 backup_full 补齐 daemon.json
# ============================================================


class TestFallbackBackupLayout:
    def test_fallback_backup_full_includes_daemon_json(self, setup_daemon_env, force_fallback):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()

        backup_dir = os.path.join(backup_root, result["backup_id"])
        assert os.path.isfile(os.path.join(backup_dir, "daemon.json"))
        names = {f.get("name") for f in result["files"]}
        assert "daemon.json" in names
        # 布局与 Rust 默认一致：registry.db + cas.db + audit.db + daemon.json + snapshots/
        assert {"registry.db", "audit.db", "daemon.json", "snapshots/"} <= names

    def test_fallback_backup_db_only_excludes_daemon_json(self, setup_daemon_env, force_fallback):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_db_only()

        names = {f.get("name") for f in result["files"]}
        assert "daemon.json" not in names
        assert {"registry.db", "audit.db"} <= names


# ============================================================
# B4: 回退原子发布（重复 ID fail-closed + 失败清理）
# ============================================================


class TestFallbackAtomicPublish:
    def test_duplicate_id_fail_closed(self, setup_daemon_env, force_fallback):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        mgr.backup_full(backup_id="dup-id")
        with pytest.raises(FileExistsError):
            mgr.backup_full(backup_id="dup-id")
        with pytest.raises(FileExistsError):
            mgr.backup_db_only(backup_id="dup-id")

    def test_no_partial_leftover_after_success(self, setup_daemon_env, force_fallback):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full()
        partial = os.path.join(backup_root, f".{result['backup_id']}.partial")
        assert not os.path.exists(partial)
        assert os.path.isdir(os.path.join(backup_root, result["backup_id"]))

    def test_temp_cleaned_on_failure(self, setup_daemon_env, force_fallback, monkeypatch):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        orig = BackupManager._backup_file

        def flaky(self, src_path, dest_dir, dest_name):
            if dest_name == "audit.db":
                raise RuntimeError("模拟备份中途失败")
            return orig(self, src_path, dest_dir, dest_name)

        monkeypatch.setattr(BackupManager, "_backup_file", flaky)
        with pytest.raises(RuntimeError):
            mgr.backup_full(backup_id="failing")
        # 失败后临时目录被清理，最终目录不残留
        partial = os.path.join(backup_root, ".failing.partial")
        assert not os.path.exists(partial)
        assert not os.path.exists(os.path.join(backup_root, "failing"))


# ============================================================
# P1（复审缺口）: fallback backup_id 路径穿越校验
# ============================================================


class TestFallbackBackupIdValidation:
    """Python fallback 必须与 Rust `validate_backup_id` 同规则拒绝路径穿越。

    触发点：backup_full/backup_db_only（经 _prepare_backup_dir_atomic）、
    restore、verify_backup、delete_backup、get_backup_info 的 fallback 分支。
    """

    TRAVERSAL_IDS = [
        "",
        ".",
        "..",
        "../outside",
        "..\\outside",
        "a/b",
        "a\\b",
        "a/.",
        "/abs",
        "C:\\abs",
        "C:foo",
        "a:",
    ]
    # backup_full/backup_db_only 的 "" 语义为「为空自动生成 ID」（与 Rust 路径一致），
    # 不构成穿越，故备份方法用去除 "" 的 ID 集。
    BACKUP_TRAVERSAL_IDS = [i for i in TRAVERSAL_IDS if i != ""]

    def test_fallback_backup_full_rejects_traversal(self, setup_daemon_env, force_fallback, tmp_path):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        for bad in self.BACKUP_TRAVERSAL_IDS:
            with pytest.raises(ValueError):
                mgr.backup_full(backup_id=bad)
        # 未逃出 backup_root
        assert not (tmp_path / "outside").exists()

    def test_fallback_backup_db_only_rejects_traversal(self, setup_daemon_env, force_fallback):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        for bad in self.BACKUP_TRAVERSAL_IDS:
            with pytest.raises(ValueError):
                mgr.backup_db_only(backup_id=bad)

    def test_fallback_delete_rejects_traversal(self, setup_daemon_env, force_fallback):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        for bad in self.TRAVERSAL_IDS:
            with pytest.raises(ValueError):
                mgr.delete_backup(bad)

    def test_fallback_restore_rejects_traversal(self, setup_daemon_env, force_fallback):
        cfg, backup_root = setup_daemon_env
        rm = RestoreManager(cfg, backup_root=backup_root)
        for bad in self.TRAVERSAL_IDS:
            with pytest.raises(ValueError):
                rm.restore(bad)

    def test_fallback_verify_rejects_traversal(self, setup_daemon_env, force_fallback):
        cfg, backup_root = setup_daemon_env
        rm = RestoreManager(cfg, backup_root=backup_root)
        for bad in self.TRAVERSAL_IDS:
            with pytest.raises(ValueError):
                rm.verify_backup(bad)

    def test_fallback_get_backup_info_rejects_traversal(self, setup_daemon_env, force_fallback):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        for bad in self.TRAVERSAL_IDS:
            with pytest.raises(ValueError):
                mgr.get_backup_info(bad)

    def test_valid_id_still_accepted(self, setup_daemon_env, force_fallback):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)
        rm = RestoreManager(cfg, backup_root=backup_root)
        result = mgr.backup_full(backup_id="B-123-abc")
        assert result["backup_id"] == "B-123-abc"
        assert mgr.get_backup_info("B-123-abc") is not None
        assert rm.verify_backup("B-123-abc")["status"] == "valid"
        assert mgr.delete_backup("B-123-abc") is True


# ============================================================
# B8: meta checksum Rust/Python byte-for-byte 一致（契约固化）
# ============================================================


class TestMetaChecksumParity:
    def test_python_and_rust_checksum_identical(self, setup_daemon_env, monkeypatch):
        cfg, backup_root = setup_daemon_env
        mgr = BackupManager(cfg, backup_root=backup_root)

        meta = {
            "backup_id": "B-test",
            "timestamp": 12345.0,
            "backup_type": "full",
            "daemon_version": "1.0.0",
            "files": [{"name": "a.db", "size": 10, "sha256": "x" * 64}],
            "total_size": 10,
        }
        # Python 参考实现（回退路径）
        monkeypatch.setattr(br, "_RUST_BACKUP_AVAILABLE", False)
        py_cs = mgr._compute_meta_checksum(meta)
        # Rust 实现（借用 Python json 模块）
        rs_cs = br._callwarden_core.backup_compute_meta_checksum(
            json.dumps(meta, ensure_ascii=False)
        )
        assert py_cs == rs_cs
        assert len(py_cs) == 64
