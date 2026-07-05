"""GC retention 策略测试。"""

import gzip
import os
import sqlite3
import tempfile
import time

from callwarden.db.db import CodeGraphDB


def _db_with_workspace():
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _insert_file_version(db, file_id, version_num, symbol_hash, parsed_at, is_current=0):
    db.conn.execute(
        "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) VALUES (?, 'python', 1, ?)",
        (f"file-{version_num}", parsed_at),
    )
    cur = db.conn.execute(
        """INSERT INTO file_versions
           (file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, is_current, is_deleted)
           VALUES (?, ?, ?, ?, 1, ?, ?, 0)""",
        (file_id, version_num, f"file-{version_num}", parsed_at, parsed_at, is_current),
    )
    fv_id = cur.lastrowid
    db.conn.execute(
        """INSERT OR IGNORE INTO symbol_contents
           (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name)
           VALUES (?, ?, 'fn', ?, 'def fn()', 0, '', ?)""",
        (symbol_hash, f"fn_{version_num}", f"def fn_{version_num}(): pass", f"mod.fn_{version_num}"),
    )
    db.conn.execute(
        """INSERT INTO file_symbol_versions
           (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted)
           VALUES (?, ?, ?, 1, 1, 'mod', 0, 0)""",
        (fv_id, symbol_hash, f"mod.fn_{version_num}"),
    )
    return fv_id


def test_gc_retention_prunes_only_cold_unprotected_versions():
    db, _root = _db_with_workspace()
    try:
        ws_id = db._get_active_workspace_id()
        old = time.time() - 500 * 86400
        cur = db.conn.execute(
            """INSERT INTO file_instances
               (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
               VALUES (?, 'src/a.py', 'src/a.py', 'file-5', ?, 1, ?, 'active', 'src.a')""",
            (ws_id, time.time(), time.time()),
        )
        file_id = cur.lastrowid

        _insert_file_version(db, file_id, 1, "sym-cold", old)
        _insert_file_version(db, file_id, 2, "sym-commented", old)
        _insert_file_version(db, file_id, 3, "sym-task", old)
        _insert_file_version(db, file_id, 4, "sym-recent-kept", old)
        _insert_file_version(db, file_id, 5, "sym-current", old, is_current=1)
        db.conn.execute("UPDATE symbol_contents SET has_comment = 1 WHERE content_hash = 'sym-commented'")
        db.conn.execute(
            """INSERT INTO task_symbol_changes
               (workspace_id, task_id, file_path, symbol_hash_before, change_type, created_at)
               VALUES (?, 'task-1', 'src/a.py', 'sym-task', 'modified', ?)""",
            (ws_id, time.time()),
        )
        db.conn.commit()

        dry = db.gc_retention(older_than_days=365, keep_versions=2, dry_run=True)
        assert dry["candidate_file_versions"] == 1
        assert dry["deleted_file_versions"] == 0

        applied = db.gc_retention(older_than_days=365, keep_versions=2, dry_run=False)
        assert applied["deleted_file_versions"] == 1
        assert applied["deleted_file_symbol_versions"] == 1
        assert applied["backup_path"].endswith(".db.gz")
        assert os.path.exists(applied["backup_path"])

        with gzip.open(applied["backup_path"], "rb") as gz:
            assert gz.read(16).startswith(b"SQLite format 3")

        remaining_versions = [
            row["version_num"]
            for row in db.conn.execute("SELECT version_num FROM file_versions ORDER BY version_num")
        ]
        assert remaining_versions == [2, 3, 4, 5]
        assert db.conn.execute(
            "SELECT COUNT(*) as cnt FROM symbol_contents WHERE content_hash = 'sym-cold'"
        ).fetchone()["cnt"] == 0
    finally:
        db.close()


def test_gc_retention_external_is_explicit_and_age_based():
    db, _root = _db_with_workspace()
    try:
        old = time.time() - 500 * 86400
        now = time.time()
        packages = [
            ("stdlib", "3.14", old, old),
            ("ext-python-oldpkg", "1.0", old, 0),
            ("ext-python-hotpkg", "1.0", old, now),
        ]
        for pkg, version, seen, used in packages:
            db.conn.execute(
                """INSERT INTO package_versions
                   (package_name, package_version, installed_at, last_seen_at, last_used_at, import_source)
                   VALUES (?, ?, ?, ?, ?, 'test')""",
                (pkg, version, seen, seen, used),
            )
            db.conn.execute(
                """INSERT INTO external_symbols
                   (package_name, package_version, module_path, qualified_name,
                    symbol_name, symbol_kind, signature, docstring, source_file, imported_at)
                   VALUES (?, ?, ?, ?, 'fn', 'fn', 'fn()', '', '', ?)""",
                (pkg, version, pkg, f"{pkg}.fn", seen),
            )
        db.conn.commit()

        no_external = db.gc_retention(
            older_than_days=365,
            keep_versions=1,
            include_external=False,
            dry_run=True,
        )
        assert no_external["candidate_external_packages"] == 0

        applied = db.gc_retention(
            older_than_days=365,
            keep_versions=1,
            include_external=True,
            external_stale_days=365,
            dry_run=False,
        )
        assert applied["candidate_external_packages"] == 1
        assert applied["deleted_external_packages"] == 1
        assert applied["deleted_external_symbols"] == 1

        remaining = [
            row["package_name"]
            for row in db.conn.execute("SELECT package_name FROM package_versions ORDER BY package_name")
        ]
        assert remaining == ["ext-python-hotpkg", "stdlib"]
    finally:
        db.close()


def test_gc_retention_backup_is_readable_sqlite():
    db, _root = _db_with_workspace()
    try:
        info = db._create_gc_db_backup("unit")
        assert os.path.exists(info["path"])
        extracted = info["path"][:-3]
        with gzip.open(info["path"], "rb") as src, open(extracted, "wb") as dst:
            dst.write(src.read())
        con = sqlite3.connect(extracted)
        try:
            assert con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'workspaces'").fetchone()
        finally:
            con.close()
    finally:
        db.close()


def test_gc_policy_controls_retention_and_runtime_overrides_do_not_persist():
    db, _root = _db_with_workspace()
    try:
        policy = db.get_gc_policy()
        assert policy["older_than_days"] == 365
        assert policy["keep_versions"] == 100
        assert policy["include_external"] is False

        updated = db.set_gc_policy(
            older_than_days=730,
            keep_versions=200,
            include_external=True,
            external_stale_days=720,
            backup_enabled=False,
            vacuum_enabled=True,
        )
        assert updated["older_than_days"] == 730
        assert updated["keep_versions"] == 200
        assert updated["include_external"] is True
        assert updated["backup_enabled"] is False
        assert updated["vacuum_enabled"] is True

        dry = db.gc_retention(
            older_than_days=30,
            keep_versions=2,
            include_external=False,
            backup=True,
            vacuum=False,
            dry_run=True,
        )
        assert dry["policy"]["older_than_days"] == 30
        assert dry["policy"]["keep_versions"] == 2
        assert dry["policy"]["include_external"] is False
        persisted = db.get_gc_policy()
        assert persisted["older_than_days"] == 730
        assert persisted["keep_versions"] == 200
        assert persisted["include_external"] is True

        saved = db.gc_retention(
            older_than_days=90,
            keep_versions=10,
            include_external=False,
            backup=True,
            vacuum=False,
            dry_run=True,
            save_policy=True,
        )
        assert saved["saved_policy"] is True
        persisted = db.get_gc_policy()
        assert persisted["older_than_days"] == 90
        assert persisted["keep_versions"] == 10
        assert persisted["include_external"] is False
        assert persisted["backup_enabled"] is True
        assert persisted["vacuum_enabled"] is False
    finally:
        db.close()
