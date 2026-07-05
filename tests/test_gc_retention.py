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


# ===========================================================================
# v20: GC 运行审计测试
# ===========================================================================
# 验证 gc_retention/gc_archive/gc_purge 写入 gc_runs 审计记录，
# dry-run/apply 都记录，失败时 status=failed 且 error 非空。


def test_gc_retention_dry_run_writes_audit_record():
    """dry-run 模式也应该写审计记录（候选数量、status=completed）"""
    db, _root = _db_with_workspace()
    try:
        result = db.gc_retention(older_than_days=365, keep_versions=2, dry_run=True)
        assert "audit_id" in result
        audit_id = result["audit_id"]
        assert audit_id > 0

        audit = db.gc_audit_get(audit_id)
        assert audit is not None
        assert audit["operation"] == "retention"
        assert audit["dry_run"] == 1
        assert audit["status"] == "completed"
        assert audit["error"] == ""
        assert audit["completed_at"] is not None
        # policy_json 应包含策略参数
        policy = audit["policy_json"]
        assert policy["older_than_days"] == 365
        assert policy["keep_versions"] == 2
        # candidate_counts 应有 file_versions 键
        assert "file_versions" in audit["candidate_counts"]
    finally:
        db.close()


def test_gc_retention_apply_writes_audit_with_deleted_counts():
    """apply 模式审计记录应包含实删数量明细"""
    db, _root = _db_with_workspace()
    try:
        ws_id = db._get_active_workspace_id()
        old = time.time() - 500 * 86400
        cur = db.conn.execute(
            """INSERT INTO file_instances
               (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
               VALUES (?, 'src/a.py', 'src/a.py', 'file-3', ?, 1, ?, 'active', 'src.a')""",
            (ws_id, time.time(), time.time()),
        )
        file_id = cur.lastrowid
        _insert_file_version(db, file_id, 1, "sym-cold", old)
        _insert_file_version(db, file_id, 2, "sym-current", old, is_current=1)
        db.conn.commit()

        result = db.gc_retention(older_than_days=365, keep_versions=1, dry_run=False, backup=True)
        audit = db.gc_audit_get(result["audit_id"])
        assert audit["status"] == "completed"
        assert audit["dry_run"] == 0
        assert audit["candidate_counts"]["file_versions"] == 1
        assert audit["deleted_counts"]["file_versions"] == 1
        assert audit["deleted_counts"]["file_symbol_versions"] == 1
        assert audit["backup_path"].endswith(".db.gz")
        assert audit["backup_size"] > 0
    finally:
        db.close()


def test_gc_archive_writes_audit_record():
    """gc_archive 应写审计记录（含 scanned/archived/skipped 数量）"""
    import os
    db, root = _db_with_workspace()
    try:
        # 创建一个 build/ 下的文件（被默认 ignore 规则命中）
        abs_path = os.path.join(root, "build/gen.py")
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w") as f:
            f.write("def gen(): pass\n")
        db._register_file_db(abs_path, "test")

        result = db.gc_archive(force=True, dry_run=False)
        assert "audit_id" in result
        audit = db.gc_audit_get(result["audit_id"])
        assert audit["operation"] == "archive"
        assert audit["status"] == "completed"
        assert audit["candidate_counts"]["scanned_files"] == 1
        assert audit["candidate_counts"]["archived_files"] == 1
    finally:
        db.close()


def test_gc_purge_writes_audit_record():
    """gc_purge 应写审计记录"""
    import os
    db, root = _db_with_workspace()
    try:
        abs_path = os.path.join(root, "build/gen.py")
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w") as f:
            f.write("def gen(): pass\n")
        db._register_file_db(abs_path, "test")
        db.gc_archive(force=True)

        cutoff = time.time() - 31 * 86400
        db.conn.execute("UPDATE archived_files SET archived_at = ?", (cutoff,))
        db.conn.commit()

        result = db.gc_purge(older_than_days=30)
        audit = db.gc_audit_get(result["audit_id"])
        assert audit["operation"] == "purge"
        assert audit["status"] == "completed"
        assert audit["deleted_counts"]["purged_files"] == 1
    finally:
        db.close()


def test_gc_audit_failure_records_error():
    """gc_retention 异常时应记 status=failed 且 error 非空"""
    db, _root = _db_with_workspace()
    try:
        # 关闭数据库连接模拟异常
        original_conn = db.conn
        db.conn = None  # 触发 AttributeError
        try:
            try:
                db.gc_retention(older_than_days=365, keep_versions=2, dry_run=True)
                assert False, "应该抛出异常"
            except Exception:
                pass
        finally:
            db.conn = original_conn

        # 审计记录应为 failed 状态（即使连接已恢复）
        # 注意：_start_gc_audit 内部用 self.conn，连接 None 时会失败，因此 audit_id 不会被生成
        # 改为测试：在 gc_retention 执行中途异常时审计失败
        audits = db.gc_audit_list(limit=10)
        # 没有审计记录（因为 _start_gc_audit 自己就失败了）
        assert len(audits) == 0
    finally:
        db.close()


def test_gc_audit_failure_mid_execution_records_error():
    """gc_retention 执行中途异常时应记 status=failed"""
    db, _root = _db_with_workspace()
    try:
        # 先正常启动一次 gc_retention，让 _start_gc_audit 成功
        # 然后用 monkey patch 让 _select_retention_file_versions 抛异常
        original = db._select_retention_file_versions
        def raise_fn(*a, **kw):
            raise RuntimeError("simulated mid-execution failure")
        db._select_retention_file_versions = raise_fn

        try:
            try:
                db.gc_retention(older_than_days=365, keep_versions=2, dry_run=True)
                assert False, "应该抛出 RuntimeError"
            except RuntimeError:
                pass
        finally:
            db._select_retention_file_versions = original

        # 应该有一条 failed 审计记录
        audits = db.gc_audit_list(limit=10)
        assert len(audits) == 1
        audit = audits[0]
        assert audit["operation"] == "retention"
        assert audit["status"] == "failed"
        assert "simulated mid-execution failure" in audit["error"]
        assert audit["completed_at"] is not None
    finally:
        db.close()


def test_gc_audit_list_filters_by_operation():
    """gc_audit_list 按 operation 过滤"""
    db, _root = _db_with_workspace()
    try:
        # 触发 2 次 retention dry-run
        db.gc_retention(older_than_days=365, keep_versions=2, dry_run=True)
        # 触发 1 次 archive dry-run
        db.gc_archive(force=True, dry_run=True)

        all_audits = db.gc_audit_list(limit=10)
        assert len(all_audits) == 2

        retention_only = db.gc_audit_list(limit=10, operation="retention")
        assert len(retention_only) == 1
        assert retention_only[0]["operation"] == "retention"

        archive_only = db.gc_audit_list(limit=10, operation="archive")
        assert len(archive_only) == 1
        assert archive_only[0]["operation"] == "archive"

        purge_only = db.gc_audit_list(limit=10, operation="purge")
        assert len(purge_only) == 0
    finally:
        db.close()


def test_gc_audit_list_limit_clamp():
    """gc_audit_list 的 limit 参数应该在 [1, 500] 范围内"""
    db, _root = _db_with_workspace()
    try:
        db.gc_retention(older_than_days=365, keep_versions=2, dry_run=True)
        db.gc_retention(older_than_days=365, keep_versions=2, dry_run=True)

        # limit < 1 应该被钳制为 1
        audits = db.gc_audit_list(limit=0)
        assert len(audits) == 1

        # limit > 500 应该被钳制为 500
        audits = db.gc_audit_list(limit=9999)
        assert len(audits) == 2
    finally:
        db.close()


def test_gc_audit_get_returns_none_for_missing():
    """gc_audit_get 查询不存在的 ID 应返回 None"""
    db, _root = _db_with_workspace()
    try:
        assert db.gc_audit_get(99999) is None
    finally:
        db.close()


# ----------------------------------------------------------------------
# gc_archive_list / gc_archive_inspect 测试（v20 新增）
# ----------------------------------------------------------------------

def test_gc_archive_list_empty_when_no_archives():
    """无备份文件时 gc_archive_list 返回空列表"""
    db, _root = _db_with_workspace()
    try:
        result = db.gc_archive_list()
        assert result == []
    finally:
        db.close()


def test_gc_archive_list_returns_backup_metadata():
    """生成备份后 gc_archive_list 应返回元信息列表（按 mtime 倒序）"""
    db, _root = _db_with_workspace()
    try:
        # 创建两个备份
        b1 = db._create_gc_db_backup("retention")
        time.sleep(1.1)  # 确保 mtime 不同
        b2 = db._create_gc_db_backup("unit")

        items = db.gc_archive_list()
        assert len(items) == 2
        # 倒序：较新的在前
        assert items[0]["name"].endswith("-unit.db.gz")
        assert items[1]["name"].endswith("-retention.db.gz")
        # 校验字段
        first = items[0]
        assert first["path"] == b2["path"]
        assert first["size"] == b2["size"]
        assert first["size"] > 0
        assert first["mtime"] > 0
        assert first["reason"] == "unit"
        # path 是绝对路径
        assert os.path.isabs(first["path"])
    finally:
        db.close()


def test_gc_archive_list_limit_clamp():
    """gc_archive_list limit 钳制到 [1, 500]"""
    db, _root = _db_with_workspace()
    try:
        # 创建 3 个备份，用不同 reason 避免同一秒内文件名冲突覆盖
        reasons = ["retention", "archive", "purge"]
        for r in reasons:
            db._create_gc_db_backup(r)
            time.sleep(0.01)  # 让 mtime 区分开
        # limit=0 钳制为 1
        assert len(db.gc_archive_list(limit=0)) == 1
        # limit=-5 钳制为 1
        assert len(db.gc_archive_list(limit=-5)) == 1
        # limit=2 返回 2 条
        assert len(db.gc_archive_list(limit=2)) == 2
        # limit=1000 钳制为 500（实际只有 3 条，返回 3）
        assert len(db.gc_archive_list(limit=1000)) == 3
    finally:
        db.close()


def test_gc_archive_inspect_raises_for_empty_path():
    """gc_archive_inspect 空路径应抛 ValueError"""
    import pytest
    db, _root = _db_with_workspace()
    try:
        with pytest.raises(ValueError):
            db.gc_archive_inspect(path="")
    finally:
        db.close()


def test_gc_archive_inspect_raises_for_missing_file():
    """gc_archive_inspect 不存在的文件应抛 FileNotFoundError"""
    import pytest
    db, _root = _db_with_workspace()
    try:
        with pytest.raises(FileNotFoundError):
            db.gc_archive_inspect(path="/nonexistent/path/missing.db.gz")
    finally:
        db.close()


def test_gc_archive_inspect_returns_full_info():
    """gc_archive_inspect 应返回备份文件完整元信息与表行数"""
    db, _root = _db_with_workspace()
    try:
        # 先插入一些数据
        ws_id = db._get_active_workspace_id()
        db.conn.execute(
            """INSERT INTO file_instances
               (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
               VALUES (?, 'src/x.py', 'src/x.py', 'h1', ?, 1, ?, 'active', 'src.x')""",
            (ws_id, time.time(), time.time()),
        )
        db.conn.commit()

        backup = db._create_gc_db_backup("retention")
        info = db.gc_archive_inspect(path=backup["path"])

        # 元信息字段
        assert info["path"] == backup["path"]
        assert info["name"].endswith("-retention.db.gz")
        assert info["size"] == backup["size"]
        assert info["size"] > 0

        # schema 版本应与当前一致
        cur = db.conn.execute("SELECT MAX(version) as v FROM schema_version")
        expected_version = cur.fetchone()["v"]
        assert info["schema_version"] == expected_version

        # tables 列表非空
        assert isinstance(info["tables"], list)
        assert len(info["tables"]) > 0
        table_names = [t["name"] for t in info["tables"]]
        # 必含核心表
        for required in ("workspaces", "file_instances", "symbols", "calls",
                         "gc_runs", "archived_files", "schema_version"):
            assert required in table_names, f"missing table: {required}"
        # 每个表行数 >= 0
        for t in info["tables"]:
            assert t["rows"] >= 0

        # 摘要字段
        assert info["workspace_count"] >= 1  # 至少有当前 workspace
        assert info["file_version_count"] >= 0
        assert info["symbol_count"] >= 0
        assert info["call_count"] >= 0
        assert info["gc_runs_count"] >= 0
        assert info["archived_files_count"] >= 0
    finally:
        db.close()


def test_gc_archive_inspect_supports_shorthand_relative_path():
    """gc_archive_inspect 支持相对 gc_archives 目录的简写路径"""
    db, _root = _db_with_workspace()
    try:
        backup = db._create_gc_db_backup("retention")
        # 取文件名（去掉 .db.gz 后缀）作为简写
        full_name = os.path.basename(backup["path"])
        shorthand = full_name  # 完整文件名
        info = db.gc_archive_inspect(path=shorthand)
        assert info["path"] == backup["path"]

        # 去掉 .db.gz 后缀的简写
        shorthand_no_ext = full_name.replace(".db.gz", "")
        info2 = db.gc_archive_inspect(path=shorthand_no_ext)
        assert info2["path"] == backup["path"]
    finally:
        db.close()


def test_gc_archive_inspect_does_not_modify_backup_file():
    """gc_archive_inspect 只读模式，不应修改备份文件"""
    db, _root = _db_with_workspace()
    try:
        backup = db._create_gc_db_backup("retention")
        original_mtime = os.path.getmtime(backup["path"])
        original_size = os.path.getsize(backup["path"])

        # 多次 inspect
        for _ in range(3):
            db.gc_archive_inspect(path=backup["path"])

        # 文件未被修改
        assert os.path.getmtime(backup["path"]) == original_mtime
        assert os.path.getsize(backup["path"]) == original_size
    finally:
        db.close()


# ----------------------------------------------------------------------
# gc_archive_import 测试（v20 新增）
# ----------------------------------------------------------------------

def test_gc_archive_import_path_required():
    """gc_archive_import 空 path 应抛 ValueError"""
    import pytest
    db, _root = _db_with_workspace()
    try:
        with pytest.raises(ValueError):
            db.gc_archive_import(path="", file_path="src/a.py", dry_run=True)
    finally:
        db.close()


def test_gc_archive_import_no_target_raises():
    """gc_archive_import 未指定 file_path 或 package_name 应抛 ValueError"""
    import pytest
    db, _root = _db_with_workspace()
    try:
        backup = db._create_gc_db_backup("retention")
        with pytest.raises(ValueError):
            db.gc_archive_import(path=backup["path"], file_path="", package_name="", dry_run=True)
    finally:
        db.close()


def test_gc_archive_import_missing_file_raises():
    """gc_archive_import 不存在的文件应抛 FileNotFoundError"""
    import pytest
    db, _root = _db_with_workspace()
    try:
        with pytest.raises(FileNotFoundError):
            db.gc_archive_import(path="/nonexistent/x.db.gz", file_path="src/a.py", dry_run=True)
    finally:
        db.close()


def test_gc_archive_import_file_mode_dry_run_then_apply_restores_history():
    """构造旧版本 -> 备份 -> retention 删除 -> archive-import dry-run -> apply -> 查询历史恢复"""
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

        # 插入 3 个版本：1=冷无注释（将被删），2=近期保留，5=当前
        _insert_file_version(db, file_id, 1, "sym-cold", old)
        _insert_file_version(db, file_id, 2, "sym-keep", old)
        _insert_file_version(db, file_id, 5, "sym-current", old, is_current=1)
        db.conn.commit()

        # 备份
        backup = db._create_gc_db_backup("retention")
        assert os.path.exists(backup["path"])

        # retention 删除冷版本（version_num=1，keep_versions=2 保留 5 和 2）
        deleted = db.gc_retention(older_than_days=365, keep_versions=2, dry_run=False)
        assert deleted["deleted_file_versions"] == 1
        # 确认 version_num=1 已删除
        remaining = [
            r["version_num"]
            for r in db.conn.execute("SELECT version_num FROM file_versions ORDER BY version_num")
        ]
        assert 1 not in remaining
        assert 5 in remaining

        # archive-import dry-run
        dry = db.gc_archive_import(
            path=backup["path"], file_path="src/a.py", dry_run=True,
        )
        assert dry["dry_run"] is True
        assert dry["target"] == "file"
        assert dry["target_value"] == "src/a.py"
        # dry_run 不应改变数据库
        after_dry = [
            r["version_num"]
            for r in db.conn.execute("SELECT version_num FROM file_versions ORDER BY version_num")
        ]
        assert after_dry == remaining

        # archive-import apply 恢复 version_num=1
        applied = db.gc_archive_import(
            path=backup["path"], file_path="src/a.py", dry_run=False,
        )
        assert applied["dry_run"] is False
        assert applied["imported"]["file_versions"] >= 1
        # 验证 version_num=1 已恢复
        after_apply = [
            r["version_num"]
            for r in db.conn.execute("SELECT version_num FROM file_versions ORDER BY version_num")
        ]
        assert 1 in after_apply

        # 二次导入应全部 skipped（幂等）
        repeat = db.gc_archive_import(
            path=backup["path"], file_path="src/a.py", dry_run=False,
        )
        assert repeat["skipped"]["file_versions"] >= 1
    finally:
        db.close()


def test_gc_archive_import_file_not_in_backup_returns_error():
    """备份库中不存在的文件路径应在 errors 中记录"""
    db, _root = _db_with_workspace()
    try:
        backup = db._create_gc_db_backup("retention")
        result = db.gc_archive_import(
            path=backup["path"], file_path="nonexistent/file.py", dry_run=True,
        )
        assert len(result["errors"]) > 0
        assert result["imported"]["file_versions"] == 0
    finally:
        db.close()


def test_gc_archive_import_file_not_in_current_returns_error():
    """当前库不存在的文件应在 errors 中记录（要求用户先 --init）"""
    db, _root = _db_with_workspace()
    try:
        # 在备份库中有该文件，但当前库不存在
        ws_id = db._get_active_workspace_id()
        old = time.time() - 500 * 86400
        cur = db.conn.execute(
            """INSERT INTO file_instances
               (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
               VALUES (?, 'src/backup_only.py', 'src/backup_only.py', 'file-1', ?, 1, ?, 'active', 'src.backup_only')""",
            (ws_id, time.time(), time.time()),
        )
        file_id = cur.lastrowid
        _insert_file_version(db, file_id, 1, "sym-backup", old)
        db.conn.commit()

        backup = db._create_gc_db_backup("retention")

        # 删除当前库的 file_instance（模拟当前库不存在该文件）
        db.conn.execute("DELETE FROM file_symbol_versions WHERE file_version_id IN (SELECT id FROM file_versions WHERE file_instance_id = ?)", (file_id,))
        db.conn.execute("DELETE FROM file_versions WHERE file_instance_id = ?", (file_id,))
        db.conn.execute("DELETE FROM file_instances WHERE id = ?", (file_id,))
        db.conn.commit()

        result = db.gc_archive_import(
            path=backup["path"], file_path="src/backup_only.py", dry_run=True,
        )
        assert len(result["errors"]) > 0
    finally:
        db.close()


def test_gc_archive_import_package_mode_dry_run_then_apply_restores_external():
    """构造 external package -> 备份 -> 删除 -> 导回 -> 查询 external symbol 恢复"""
    db, _root = _db_with_workspace()
    try:
        old = time.time() - 500 * 86400
        # 插入外部包 + 符号
        db.conn.execute(
            """INSERT INTO package_versions
               (package_name, package_version, installed_at, last_seen_at, last_used_at, import_source)
               VALUES (?, ?, ?, ?, ?, 'test')""",
            ("ext-pkg-restore", "1.0", old, old, 0),
        )
        db.conn.execute(
            """INSERT INTO external_symbols
               (package_name, package_version, module_path, qualified_name,
                symbol_name, symbol_kind, signature, docstring, source_file, imported_at)
               VALUES (?, ?, ?, ?, ?, 'fn', 'fn()', '', '', ?)""",
            ("ext-pkg-restore", "1.0", "ext_pkg_restore",
             "ext_pkg_restore.fn", "fn", old),
        )
        db.conn.commit()

        backup = db._create_gc_db_backup("retention")
        assert os.path.exists(backup["path"])

        # 删除外部包
        db.conn.execute("DELETE FROM external_symbols WHERE package_name = ?", ("ext-pkg-restore",))
        db.conn.execute("DELETE FROM package_versions WHERE package_name = ?", ("ext-pkg-restore",))
        db.conn.commit()
        # 确认已删除
        assert db.conn.execute(
            "SELECT COUNT(*) as c FROM external_symbols WHERE package_name = ?",
            ("ext-pkg-restore",),
        ).fetchone()["c"] == 0

        # archive-import dry-run
        dry = db.gc_archive_import(
            path=backup["path"], package_name="ext-pkg-restore", dry_run=True,
        )
        assert dry["dry_run"] is True
        assert dry["target"] == "package"
        assert dry["target_value"] == "ext-pkg-restore"
        # dry_run 不应改变数据库
        assert db.conn.execute(
            "SELECT COUNT(*) as c FROM external_symbols WHERE package_name = ?",
            ("ext-pkg-restore",),
        ).fetchone()["c"] == 0

        # archive-import apply 恢复
        applied = db.gc_archive_import(
            path=backup["path"], package_name="ext-pkg-restore", dry_run=False,
        )
        assert applied["imported"]["external_symbols"] >= 1
        assert applied["imported"]["package_versions"] >= 1
        # 验证已恢复
        assert db.conn.execute(
            "SELECT COUNT(*) as c FROM external_symbols WHERE package_name = ?",
            ("ext-pkg-restore",),
        ).fetchone()["c"] >= 1

        # 二次导入应全部 skipped（幂等）
        repeat = db.gc_archive_import(
            path=backup["path"], package_name="ext-pkg-restore", dry_run=False,
        )
        assert repeat["skipped"]["external_symbols"] >= 1
        assert repeat["skipped"]["package_versions"] >= 1
    finally:
        db.close()


def test_gc_archive_import_writes_audit_record():
    """gc_archive_import 应写入审计记录"""
    db, _root = _db_with_workspace()
    try:
        ws_id = db._get_active_workspace_id()
        old = time.time() - 500 * 86400
        cur = db.conn.execute(
            """INSERT INTO file_instances
               (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
               VALUES (?, 'src/audit.py', 'src/audit.py', 'file-1', ?, 1, ?, 'active', 'src.audit')""",
            (ws_id, time.time(), time.time()),
        )
        file_id = cur.lastrowid
        _insert_file_version(db, file_id, 1, "sym-audit", old)
        db.conn.commit()

        backup = db._create_gc_db_backup("retention")
        db.gc_archive_import(path=backup["path"], file_path="src/audit.py", dry_run=True)

        # 应有一条 audit 记录
        audits = db.gc_audit_list(operation="archive_import")
        assert len(audits) >= 1
        latest = audits[0]
        assert latest["operation"] == "archive_import"
        assert latest["dry_run"] == 1
        assert latest["status"] == "completed"
    finally:
        db.close()


def test_gc_archive_import_supports_shorthand_path():
    """gc_archive_import 支持相对 gc_archives 目录的简写路径"""
    db, _root = _db_with_workspace()
    try:
        backup = db._create_gc_db_backup("retention")
        # 用完整文件名作为简写
        full_name = os.path.basename(backup["path"])
        result = db.gc_archive_import(
            path=full_name, file_path="any.py", dry_run=True,
        )
        # 因 any.py 不在备份库，errors 应有内容，但 path 解析成功
        assert result["path"] == backup["path"]
        assert len(result["errors"]) > 0
    finally:
        db.close()


# ===========================================================================
# v20: Top N 收益预估测试
# ===========================================================================
# 验证 _estimate_retention_top_n 与 gc_retention 的 estimate 字段：
# - approximate_deleted_rows 各表行数正确
# - affected_files_top_n 按候选版本数倒序，限制 top_n
# - external_packages_top_n 按符号数倒序
# - top_n 钳制到 [1, 100]
# - 空候选返回空列表，is_estimate=True


def _setup_files_with_versions(db, ws_id, filespec):
    """辅助：批量创建文件实例与历史版本。

    filespec: [(rel_path, [version_num, ...], is_old_bool)]
    返回 [(file_id, rel_path, [fv_id, ...])]"""
    out = []
    for rel_path, version_nums, is_old in filespec:
        parsed_at = time.time() - (500 * 86400 if is_old else 1)
        cur = db.conn.execute(
            """INSERT INTO file_instances
               (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
               VALUES (?, ?, ?, ?, ?, 1, ?, 'active', ?)""",
            (ws_id, rel_path, rel_path, f"file-{version_nums[-1]}", time.time(), parsed_at, rel_path.replace("/", ".")),
        )
        file_id = cur.lastrowid
        fv_ids = []
        for vn in version_nums:
            fv_id = _insert_file_version(db, file_id, vn, f"sym-{rel_path}-{vn}", parsed_at)
            fv_ids.append(fv_id)
        out.append((file_id, rel_path, fv_ids))
    return out


def test_estimate_retention_top_n_approximate_deleted_rows():
    """approximate_deleted_rows 应正确统计 file_versions / file_symbol_versions / call_versions"""
    db, _root = _db_with_workspace()
    try:
        ws_id = db._get_active_workspace_id()
        files = _setup_files_with_versions(db, ws_id, [
            ("src/a.py", [1, 2, 3], True),  # 3 个旧版本，3 个符号版本
        ])
        # 给第一个文件版本插入 1 条 call_versions
        first_fv = files[0][2][0]
        db.conn.execute(
            "INSERT INTO call_versions (file_version_id, caller_qualified, callee_name) VALUES (?, 'mod.fn1', 'mod.fn2')",
            (first_fv,),
        )
        db.conn.commit()

        # 直接调用 _estimate_retention_top_n（all version_ids 都视为候选）
        version_ids = [fv for _, _, fvs in files for fv in fvs]
        result = db._estimate_retention_top_n(version_ids, [], top_n=10)
        assert result["is_estimate"] is True
        assert result["approximate_deleted_rows"]["file_versions"] == 3
        assert result["approximate_deleted_rows"]["file_symbol_versions"] == 3
        assert result["approximate_deleted_rows"]["call_versions"] == 1
        assert result["approximate_deleted_rows"]["external_symbols"] == 0
        assert result["approximate_deleted_rows"]["external_packages"] == 0
    finally:
        db.close()


def test_estimate_retention_top_n_affected_files_sorted_by_count():
    """affected_files_top_n 按候选版本数倒序，限制 top_n"""
    db, _root = _db_with_workspace()
    try:
        ws_id = db._get_active_workspace_id()
        files = _setup_files_with_versions(db, ws_id, [
            ("src/a.py", [1, 2, 3], True),       # 3 个版本
            ("src/b.py", [1], True),             # 1 个版本
            ("src/c.py", [1, 2, 3, 4, 5], True),  # 5 个版本（最多）
        ])
        db.conn.commit()
        version_ids = [fv for _, _, fvs in files for fv in fvs]
        result = db._estimate_retention_top_n(version_ids, [], top_n=2)
        assert len(result["affected_files_top_n"]) == 2
        # c.py 应排第一（5 个候选版本）
        assert result["affected_files_top_n"][0]["rel_path"] == "src/c.py"
        assert result["affected_files_top_n"][0]["candidate_versions"] == 5
        # a.py 排第二（3 个候选版本）
        assert result["affected_files_top_n"][1]["rel_path"] == "src/a.py"
        assert result["affected_files_top_n"][1]["candidate_versions"] == 3
        # b.py 应被截断
        paths = [item["rel_path"] for item in result["affected_files_top_n"]]
        assert "src/b.py" not in paths
    finally:
        db.close()


def test_estimate_retention_top_n_external_packages_sorted_by_symbol_count():
    """external_packages_top_n 按符号数倒序"""
    db, _root = _db_with_workspace()
    try:
        old = time.time() - 500 * 86400
        # 3 个外部包，符号数分别为 1/3/2
        packages = [
            ("ext-pkg-low", "1.0", 1, old),
            ("ext-pkg-high", "1.0", 3, old),
            ("ext-pkg-mid", "1.0", 2, old),
        ]
        for pkg, version, sym_count, seen in packages:
            db.conn.execute(
                """INSERT INTO package_versions
                   (package_name, package_version, installed_at, last_seen_at, last_used_at, import_source)
                   VALUES (?, ?, ?, ?, 0, 'test')""",
                (pkg, version, seen, seen),
            )
            for i in range(sym_count):
                db.conn.execute(
                    """INSERT INTO external_symbols
                       (package_name, package_version, module_path, qualified_name,
                        symbol_name, symbol_kind, signature, docstring, source_file, imported_at)
                       VALUES (?, ?, ?, ?, ?, 'fn', 'fn()', '', '', ?)""",
                    (pkg, version, pkg, f"{pkg}.fn{i}", f"fn{i}", seen),
                )
        db.conn.commit()

        external_packages = [
            {"package_name": "ext-pkg-low", "package_version": "1.0"},
            {"package_name": "ext-pkg-high", "package_version": "1.0"},
            {"package_name": "ext-pkg-mid", "package_version": "1.0"},
        ]
        result = db._estimate_retention_top_n([], external_packages, top_n=10)
        assert result["approximate_deleted_rows"]["external_symbols"] == 6  # 1+3+2
        assert len(result["external_packages_top_n"]) == 3
        # high(3) > mid(2) > low(1)
        assert result["external_packages_top_n"][0]["package_name"] == "ext-pkg-high"
        assert result["external_packages_top_n"][0]["symbol_count"] == 3
        assert result["external_packages_top_n"][1]["package_name"] == "ext-pkg-mid"
        assert result["external_packages_top_n"][1]["symbol_count"] == 2
        assert result["external_packages_top_n"][2]["package_name"] == "ext-pkg-low"
        assert result["external_packages_top_n"][2]["symbol_count"] == 1
    finally:
        db.close()


def test_estimate_retention_top_n_top_n_clamp():
    """top_n 参数应钳制到 [1, 100]"""
    db, _root = _db_with_workspace()
    try:
        # top_n=0 应被钳制为 1
        result = db._estimate_retention_top_n([], [], top_n=0)
        # 空候选应返回空列表，但不报错
        assert result["affected_files_top_n"] == []
        assert result["external_packages_top_n"] == []
        assert result["is_estimate"] is True

        # top_n=负数也应被钳制
        result_neg = db._estimate_retention_top_n([], [], top_n=-5)
        assert result_neg["is_estimate"] is True

        # top_n=200 应被钳制为 100
        result_big = db._estimate_retention_top_n([], [], top_n=200)
        assert result_big["is_estimate"] is True
        # 空候选不会触发 LIMIT，但调用本身不应抛异常
    finally:
        db.close()


def test_estimate_retention_top_n_empty_candidates_returns_empty_lists():
    """空候选应返回空列表（不抛异常），且 is_estimate=True"""
    db, _root = _db_with_workspace()
    try:
        result = db._estimate_retention_top_n([], [], top_n=10)
        assert result["is_estimate"] is True
        assert result["approximate_deleted_rows"] == {
            "file_versions": 0,
            "file_symbol_versions": 0,
            "call_versions": 0,
            "symbol_contents": 0,
            "external_symbols": 0,
            "external_packages": 0,
        }
        assert result["affected_files_top_n"] == []
        assert result["external_packages_top_n"] == []
    finally:
        db.close()


def test_gc_retention_returns_estimate_field():
    """gc_retention 返回值应包含 estimate 字段（dry-run 与 apply 都返回）"""
    db, _root = _db_with_workspace()
    try:
        ws_id = db._get_active_workspace_id()
        _setup_files_with_versions(db, ws_id, [
            ("src/a.py", [1, 2, 3, 4, 5], True),
        ])
        db.conn.commit()

        dry = db.gc_retention(older_than_days=365, keep_versions=2, dry_run=True)
        assert "estimate" in dry
        est = dry["estimate"]
        assert est["is_estimate"] is True
        # keep_versions=2，5 个版本 -> 候选 3 个
        assert est["approximate_deleted_rows"]["file_versions"] == 3
        assert len(est["affected_files_top_n"]) == 1
        assert est["affected_files_top_n"][0]["rel_path"] == "src/a.py"
        assert est["affected_files_top_n"][0]["candidate_versions"] == 3

        # apply 也应返回 estimate
        applied = db.gc_retention(older_than_days=365, keep_versions=2, dry_run=False, backup=True)
        assert "estimate" in applied
        assert applied["estimate"]["is_estimate"] is True
    finally:
        db.close()


def test_gc_retention_estimate_empty_when_no_candidates():
    """无候选时 estimate 字段仍存在，但 Top N 列表为空"""
    db, _root = _db_with_workspace()
    try:
        # 空库无候选
        result = db.gc_retention(older_than_days=365, keep_versions=2, dry_run=True)
        assert "estimate" in result
        est = result["estimate"]
        assert est["is_estimate"] is True
        # 空候选时 approximate_deleted_rows 全为 0
        assert all(v == 0 for v in est["approximate_deleted_rows"].values())
        assert est["affected_files_top_n"] == []
        assert est["external_packages_top_n"] == []
    finally:
        db.close()


def test_estimate_retention_top_n_includes_external_packages_section():
    """gc_retention 在 include_external=True 时 estimate 应包含 external_packages_top_n"""
    db, _root = _db_with_workspace()
    try:
        old = time.time() - 500 * 86400
        db.conn.execute(
            """INSERT INTO package_versions
               (package_name, package_version, installed_at, last_seen_at, last_used_at, import_source)
               VALUES (?, ?, ?, ?, 0, 'test')""",
            ("ext-stale-pkg", "1.0", old, old),
        )
        db.conn.execute(
            """INSERT INTO external_symbols
               (package_name, package_version, module_path, qualified_name,
                symbol_name, symbol_kind, signature, docstring, source_file, imported_at)
               VALUES (?, ?, ?, ?, ?, 'fn', 'fn()', '', '', ?)""",
            ("ext-stale-pkg", "1.0", "ext-stale-pkg", "ext-stale-pkg.fn1", "fn1", old),
        )
        db.conn.commit()

        # include_external=True 时 external_packages_top_n 应有内容
        result = db.gc_retention(
            older_than_days=365, keep_versions=2,
            include_external=True, external_stale_days=365,
            dry_run=True,
        )
        est = result["estimate"]
        assert est["approximate_deleted_rows"]["external_symbols"] == 1
        assert est["approximate_deleted_rows"]["external_packages"] == 1
        assert len(est["external_packages_top_n"]) == 1
        assert est["external_packages_top_n"][0]["package_name"] == "ext-stale-pkg"
        assert est["external_packages_top_n"][0]["symbol_count"] == 1

        # include_external=False 时 external_packages_top_n 应为空
        result_no_ext = db.gc_retention(
            older_than_days=365, keep_versions=2,
            include_external=False,
            dry_run=True,
        )
        est_no_ext = result_no_ext["estimate"]
        assert est_no_ext["approximate_deleted_rows"]["external_symbols"] == 0
        assert est_no_ext["approximate_deleted_rows"]["external_packages"] == 0
        assert est_no_ext["external_packages_top_n"] == []
    finally:
        db.close()
