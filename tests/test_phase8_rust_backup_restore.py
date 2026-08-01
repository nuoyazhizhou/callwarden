"""R5: Rust 完整 backup/restore 生产 wrapper 验收。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.backup_restore import BackupManager, RestoreManager


def _config(root: Path) -> SimpleNamespace:
    data_root = root / "data"
    data_root.mkdir()
    paths = {
        "registry_db_path": root / "registry.db",
        "cas_db_path": root / "cas.db",
        "audit_log_path": root / "audit.db",
    }
    for name, path in paths.items():
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        conn.execute("INSERT INTO marker VALUES (?)", (name,))
        conn.commit()
        conn.close()
    snapshot_dir = data_root / "snapshots"
    snapshot_dir.mkdir()
    (snapshot_dir / "snapshot.bin").write_bytes(b"snapshot-v1")
    (data_root / "daemon.json").write_text('{"mode":"enterprise"}', encoding="utf-8")
    return SimpleNamespace(
        data_root=str(data_root),
        registry_db_path=str(paths["registry_db_path"]),
        cas_db_path=str(paths["cas_db_path"]),
        audit_log_path=str(paths["audit_log_path"]),
    )


def test_rust_full_backup_roundtrip_and_python_api(tmp_path: Path):
    cfg = _config(tmp_path)
    backup_root = tmp_path / "backups"
    backup = BackupManager(cfg, backup_root=str(backup_root))
    result = backup.backup_full(backup_id="B-rust-roundtrip")

    assert result["backup_type"] == "full"
    names = {item["name"] for item in result["files"]}
    assert {"registry.db", "cas.db", "audit.db", "daemon.json", "snapshots/"} <= names
    assert RestoreManager(cfg, backup_root=str(backup_root)).verify_backup(
        "B-rust-roundtrip"
    )["status"] == "valid"

    for path in (
        Path(cfg.registry_db_path),
        Path(cfg.cas_db_path),
        Path(cfg.audit_log_path),
    ):
        path.unlink()
    shutil.rmtree(Path(cfg.data_root) / "snapshots")
    (Path(cfg.data_root) / "daemon.json").unlink()

    restored = RestoreManager(cfg, backup_root=str(backup_root)).restore(
        "B-rust-roundtrip"
    )
    assert restored["status"] == "success"
    assert Path(cfg.registry_db_path).is_file()
    assert Path(cfg.cas_db_path).is_file()
    assert Path(cfg.audit_log_path).is_file()
    assert (Path(cfg.data_root) / "snapshots" / "snapshot.bin").read_bytes() == b"snapshot-v1"
    assert (Path(cfg.data_root) / "daemon.json").is_file()


def test_rust_restore_rejects_tampered_file_without_overwrite(tmp_path: Path):
    cfg = _config(tmp_path)
    backup_root = tmp_path / "backups"
    backup = BackupManager(cfg, backup_root=str(backup_root))
    backup.backup_full(backup_id="B-rust-tamper")
    target = Path(cfg.registry_db_path)
    original = target.read_bytes()
    with (backup_root / "B-rust-tamper" / "registry.db").open("ab") as stream:
        stream.write(b"tampered")

    restore_result = RestoreManager(cfg, backup_root=str(backup_root)).restore(
        "B-rust-tamper"
    )
    assert restore_result["status"] == "failure"
    assert target.read_bytes() == original


def test_rust_backup_rejects_path_traversal_and_duplicate(tmp_path: Path):
    cfg = _config(tmp_path)
    backup = BackupManager(cfg, backup_root=str(tmp_path / "backups"))
    with pytest.raises(Exception, match="非法 backup_id"):
        backup.backup_full("../escape")
    backup.backup_full("B-rust-duplicate")
    with pytest.raises(Exception, match="备份已存在"):
        backup.backup_full("B-rust-duplicate")
    assert not (tmp_path / "escape").exists()


def test_rust_db_only_and_cleanup_are_wired(tmp_path: Path):
    cfg = _config(tmp_path)
    backup = BackupManager(cfg, backup_root=str(tmp_path / "backups"))
    first = backup.backup_db_only("B-rust-db-only")
    assert first["backup_type"] == "db_only"
    assert "snapshots/" not in {item["name"] for item in first["files"]}
    backup.backup_db_only("B-rust-db-only-2")
    assert len(backup.list_backups()) == 2
    assert backup.cleanup_old_backups(keep_count=1) == 1
    assert len(backup.list_backups()) == 1


def test_rust_backup_meta_is_python_compatible(tmp_path: Path):
    cfg = _config(tmp_path)
    backup = BackupManager(cfg, backup_root=str(tmp_path / "backups"))
    result = backup.backup_full("B-rust-meta")
    meta_path = Path(cfg.data_root).parent / "backups" / "B-rust-meta" / "backup_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["checksum"] == backup._compute_meta_checksum(meta)
    assert result["backup_id"] == meta["backup_id"]
