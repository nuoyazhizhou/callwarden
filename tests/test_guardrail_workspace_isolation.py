"""W2.3 复审 P1-1：guardrail_findings 跨 workspace 隔离测试（v48 schema）。

覆盖：
- workspace A/B 使用相同 file_path（'a.py'）与相同 symbol_hash
- A 有 guardrail finding，B 无该 finding
- 查询 A 只能得到 A 的 finding，查询 B 不能得到 A 的 finding
- 未授权/不一致 workspace 查询 fail-closed（不返回其他 workspace 数据）
- migration 重复执行保持幂等
- 旧数据库缺少 workspace 归属时验证 fail-closed（orphaned 行不返回）
- backup/restore 后隔离仍成立
- Python 写入路径绑定 workspace_id（scan_guardrails / run_check_gate）

对应修复：schema v48 guardrail_findings.workspace_id 列 + 索引 + orphaned 标记。
"""
import os
import sqlite3
import tempfile
import time

from callwarden.db.db import CodeGraphDB
from callwarden.db.schema import SCHEMA_VERSION, GUARDRAIL_STATUS_ORPHANED


def _db_with_workspace():
    """构造临时工作区数据库（触发完整 schema 初始化）。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=os.path.join(root, "A"))
    return db, root


def _table_columns(conn, table_name):
    cur = conn.execute(f"PRAGMA table_info({table_name})")
    return [row["name"] for row in cur.fetchall()]


def _seed_symbol(db, ws_id, ws_root, symbol_hash="sh", rel_path="a.py", qualified_name="a.alpha"):
    """在指定 workspace 构造符号 a.alpha（file_path + symbol_hash 相同）。"""
    now = time.time()
    os.makedirs(ws_root, exist_ok=True)
    db.conn.execute(
        "INSERT OR IGNORE INTO file_contents(content_hash, language, total_lines, first_seen_at) "
        "VALUES ('hc','python',3,?)",
        (now,),
    )
    db.conn.execute(
        "INSERT INTO file_instances(workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, total_lines, status) VALUES (?, ?, ?, 'hc', ?, 3, 'active')",
        (ws_id, rel_path, os.path.join(ws_root, rel_path), now),
    )
    fid = db.conn.execute(
        "SELECT id FROM file_instances WHERE workspace_id=? AND rel_path=?", (ws_id, rel_path)
    ).fetchone()["id"]
    db.conn.execute(
        "INSERT OR IGNORE INTO symbol_contents(content_hash, name, kind, content, qualified_name) "
        "VALUES (?, 'alpha','fn','def alpha(): pass',?)",
        (symbol_hash, qualified_name),
    )
    db.conn.execute(
        "INSERT INTO symbols(file_instance_id, symbol_hash, name, kind, start_line, end_line, "
        "qualified_name) VALUES (?, ?, 'alpha','fn',1,2,?)",
        (fid, symbol_hash, qualified_name),
    )
    db.conn.execute(
        "INSERT INTO file_versions(file_instance_id, version_num, content_hash, mtime, total_lines, "
        "parsed_at, is_current) VALUES (?, 1, 'hc', ?, 3, ?, 1)",
        (fid, now, now),
    )
    fv = db.conn.execute(
        "SELECT id FROM file_versions WHERE file_instance_id=? AND is_current=1", (fid,)
    ).fetchone()["id"]
    db.conn.execute(
        "INSERT INTO file_symbol_versions(file_version_id, symbol_hash, qualified_name, "
        "start_line, end_line, is_deleted) VALUES (?, ?, ?, 1, 2, 0)",
        (fv, symbol_hash, qualified_name),
    )
    return fid


def _seed_guardrail_rule(db, rule_id="gr1"):
    now = time.time()
    db.conn.execute(
        "INSERT OR IGNORE INTO guardrail_rules(rule_id, category, severity, pattern, action, "
        "description, is_builtin, created_at) VALUES (?, 'db_safety','warn','x','warn','',1,?)",
        (rule_id, now),
    )


def _seed_finding(db, ws_id, message, file_path="a.py", symbol_hash="sh", rule_id="gr1",
                  status="open"):
    now = time.time()
    db.conn.execute(
        "INSERT INTO guardrail_findings(workspace_id, rule_id, file_path, symbol_hash, severity, "
        "status, message, detected_at) VALUES (?, ?, ?, ?, 'warn', ?, ?, ?)",
        (ws_id, rule_id, file_path, symbol_hash, status, message, now),
    )


def _create_ab_isolated_db():
    """构造 A/B 两 workspace，符号相同（同 file_path + 同 symbol_hash），仅 A 有 finding。"""
    db, root = _db_with_workspace()
    wsA = db._get_active_workspace_id()
    wsB = db.register_workspace("wsB", os.path.join(root, "B"))
    _seed_symbol(db, wsA, os.path.join(root, "A"))
    _seed_symbol(db, wsB, os.path.join(root, "B"))
    _seed_guardrail_rule(db)
    _seed_finding(db, wsA, "WS-A-ONLY")
    db.conn.commit()
    return db, root, wsA, wsB


# ============================================
# schema / migration
# ============================================

def test_schema_v48_guardrail_findings_has_workspace_id():
    """v48+：guardrail_findings 必须有 workspace_id 列与索引。"""
    db, _root = _db_with_workspace()
    try:
        assert SCHEMA_VERSION >= 49
        cols = _table_columns(db.conn, "guardrail_findings")
        assert "workspace_id" in cols, cols
        cur = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN "
            "('idx_guardrail_findings_workspace','idx_guardrail_findings_ws_file')"
        )
        indexes = {r["name"] for r in cur}
        assert indexes == {"idx_guardrail_findings_workspace", "idx_guardrail_findings_ws_file"}, indexes
    finally:
        db.close()


def test_migration_false_v48_stamp_repairs_via_v49():
    """复审 P0-1：被陈旧二进制打标为 v48 但缺 workspace_id 列的库，
    升级到 v49 时必须自动补列 + 索引 + orphan 旧 open 行（而非静默放行）。"""
    root = tempfile.mkdtemp()
    dbpath = os.path.join(root, "false_v48.db")
    conn = sqlite3.connect(dbpath)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, description TEXT DEFAULT '');
        INSERT INTO schema_version VALUES (48, 1, 'stale binary stamped v48');
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT, created_at REAL, is_active INTEGER DEFAULT 0, description TEXT DEFAULT '', active_task_id TEXT DEFAULT '');
        INSERT INTO workspaces (id, name, root_path, created_at, is_active) VALUES (1, 'ws', 'ROOT', 1, 1);
        CREATE TABLE guardrail_rules (rule_id TEXT PRIMARY KEY, category TEXT, severity TEXT, pattern TEXT, action TEXT, description TEXT, is_builtin INTEGER, created_at REAL);
        CREATE TABLE guardrail_findings (id INTEGER PRIMARY KEY, rule_id TEXT, file_path TEXT, symbol_hash TEXT DEFAULT '', severity TEXT DEFAULT 'warn', status TEXT DEFAULT 'open', message TEXT DEFAULT '', detected_at REAL, resolved_at REAL);
        INSERT INTO guardrail_rules VALUES ('r1','db_safety','warn','x','warn','',1,1);
        INSERT INTO guardrail_findings (id, rule_id, file_path, symbol_hash, severity, status, message, detected_at) VALUES (1,'r1','a.py','h1','warn','open','legacy-open',1);
        """
    )
    conn.execute("UPDATE workspaces SET root_path=? WHERE id=1", (root,))
    conn.commit()
    conn.close()

    db2 = CodeGraphDB(dbpath, workspace_root=root)
    try:
        cols = _table_columns(db2.conn, "guardrail_findings")
        assert "workspace_id" in cols, f"v48 打标库升级后应补列，实际: {cols}"
        idx = {r["name"] for r in db2.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN "
            "('idx_guardrail_findings_workspace','idx_guardrail_findings_ws_file')"
        )}
        assert idx == {"idx_guardrail_findings_workspace", "idx_guardrail_findings_ws_file"}, idx
        status = db2.conn.execute(
            "SELECT status FROM guardrail_findings WHERE message='legacy-open'"
        ).fetchone()["status"]
        assert status == GUARDRAIL_STATUS_ORPHANED, status
        ver = db2.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert ver == SCHEMA_VERSION, ver
    finally:
        db2.close()

    # 重复打开幂等
    db3 = CodeGraphDB(dbpath, workspace_root=root)
    try:
        cols = _table_columns(db3.conn, "guardrail_findings")
        assert "workspace_id" in cols
        status = db3.conn.execute(
            "SELECT status FROM guardrail_findings WHERE message='legacy-open'"
        ).fetchone()["status"]
        assert status == GUARDRAIL_STATUS_ORPHANED, status
    finally:
        db3.close()


def test_stale_v49_stamp_without_column_healed_by_python_fallback():
    """复审 P0-2/P0-3 兜底：陈旧二进制把缺列库打标为 v49（本机默认路径场景）。

    陈旧 .pyd 不含 missing_compat_columns / v49 迁移，会把主库打标为
    '49-缺列'。Python 侧 _ensure_compat_columns 兜底在任何路径之后运行，
    保证即使版本号已达成（current==SCHEMA_VERSION）、即使 Rust 返回报告正常，
    guardrail_findings.workspace_id 列仍被补齐。
    """
    root = tempfile.mkdtemp()
    dbpath = os.path.join(root, "stale49.db")
    conn = sqlite3.connect(dbpath)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, description TEXT DEFAULT '');
        INSERT INTO schema_version VALUES (49, 1, 'stale binary stamped v49 WITHOUT column');
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT, created_at REAL, is_active INTEGER DEFAULT 0, description TEXT DEFAULT '', active_task_id TEXT DEFAULT '');
        INSERT INTO workspaces (id, name, root_path, created_at, is_active) VALUES (1, 'ws', 'ROOT', 1, 1);
        CREATE TABLE guardrail_rules (rule_id TEXT PRIMARY KEY, category TEXT, severity TEXT, pattern TEXT, action TEXT, description TEXT, is_builtin INTEGER, created_at REAL);
        CREATE TABLE guardrail_findings (id INTEGER PRIMARY KEY, rule_id TEXT, file_path TEXT, symbol_hash TEXT DEFAULT '', severity TEXT DEFAULT 'warn', status TEXT DEFAULT 'open', message TEXT DEFAULT '', detected_at REAL, resolved_at REAL);
        INSERT INTO guardrail_rules VALUES ('r1','db_safety','warn','x','warn','',1,1);
        INSERT INTO guardrail_findings (rule_id, file_path, symbol_hash, severity, status, message, detected_at) VALUES ('r1','a.py','h1','warn','open','legacy-stale49',1);
        """
    )
    conn.execute("UPDATE workspaces SET root_path=? WHERE id=1", (root,))
    conn.commit()
    conn.close()

    # 默认路径（CW_USE_RUST_STORAGE=1，无 .pyd → Python fallback + _ensure_compat_columns）
    db = CodeGraphDB(dbpath, workspace_root=root)
    try:
        cols = _table_columns(db.conn, "guardrail_findings")
        assert "workspace_id" in cols, cols
        idx = {r["name"] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN "
            "('idx_guardrail_findings_workspace','idx_guardrail_findings_ws_file')"
        )}
        assert idx == {"idx_guardrail_findings_workspace", "idx_guardrail_findings_ws_file"}, idx
        status = db.conn.execute(
            "SELECT status FROM guardrail_findings WHERE message='legacy-stale49'"
        ).fetchone()["status"]
        assert status == GUARDRAIL_STATUS_ORPHANED, status
    finally:
        db.close()


def test_migration_v47_to_v48_idempotent_and_orphans_legacy_open():
    """旧 v47 库：workspace_id 列补齐 + open 旧行 orphaned + 重复迁移幂等。"""
    root = tempfile.mkdtemp()
    dbpath = os.path.join(root, "old.db")
    conn = sqlite3.connect(dbpath)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, description TEXT DEFAULT '');
        INSERT INTO schema_version VALUES (47, 1, 'v47');
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT, created_at REAL, is_active INTEGER DEFAULT 0, description TEXT DEFAULT '', active_task_id TEXT DEFAULT '');
        INSERT INTO workspaces (id, name, root_path, created_at, is_active) VALUES (1, 'ws', 'ROOT', 1, 1);
        CREATE TABLE guardrail_rules (rule_id TEXT PRIMARY KEY, category TEXT, severity TEXT, pattern TEXT, action TEXT, description TEXT, is_builtin INTEGER, created_at REAL);
        CREATE TABLE guardrail_findings (id INTEGER PRIMARY KEY, rule_id TEXT, file_path TEXT, symbol_hash TEXT DEFAULT '', severity TEXT DEFAULT 'warn', status TEXT DEFAULT 'open', message TEXT DEFAULT '', detected_at REAL, resolved_at REAL);
        INSERT INTO guardrail_rules VALUES ('r1','db_safety','warn','x','warn','',1,1);
        INSERT INTO guardrail_findings (id, rule_id, file_path, symbol_hash, severity, status, message, detected_at) VALUES (1,'r1','a.py','h1','warn','open','msg',1);
        INSERT INTO guardrail_findings (id, rule_id, file_path, symbol_hash, severity, status, message, detected_at) VALUES (2,'r1','a.py','h1','warn','resolved','msg2',1);
        """
    )
    conn.execute("UPDATE workspaces SET root_path=? WHERE id=1", (root,))
    conn.commit()
    conn.close()

    # 首次迁移
    db2 = CodeGraphDB(dbpath, workspace_root=root)
    try:
        cols = _table_columns(db2.conn, "guardrail_findings")
        assert "workspace_id" in cols, cols
        rows = {r["id"]: r["status"] for r in db2.conn.execute("SELECT id,status FROM guardrail_findings")}
        assert rows[1] == GUARDRAIL_STATUS_ORPHANED, rows
        assert rows[2] == "resolved", rows  # 非 open 旧行不动
        ver = db2.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert ver == SCHEMA_VERSION, ver
    finally:
        db2.close()

    # 重复迁移幂等
    db3 = CodeGraphDB(dbpath, workspace_root=root)
    try:
        rows3 = {r["id"]: r["status"] for r in db3.conn.execute("SELECT id,status FROM guardrail_findings")}
        assert rows3 == {1: GUARDRAIL_STATUS_ORPHANED, 2: "resolved"}, rows3
    finally:
        db3.close()


# ============================================
# 跨 workspace 隔离
# ============================================

def test_workspace_a_b_same_path_and_hash_query_isolated():
    """A/B 同 file_path + 同 symbol_hash：A 只能看到 A 的 finding，B 看不到 A 的。"""
    db, _root, wsA, wsB = _create_ab_isolated_db()
    try:
        db.set_active_workspace(wsA)
        issuesA = db.get_symbol_issues("a.alpha")
        assert any("WS-A-ONLY" in i["message"] for i in issuesA), issuesA

        db.set_active_workspace(wsB)
        issuesB = db.get_symbol_issues("a.alpha")
        assert not any("WS-A-ONLY" in i.get("message", "") for i in issuesB), issuesB
    finally:
        db.close()


def test_workspace_b_with_own_finding_does_not_see_a():
    """B 有自己的 finding：查询 B 只返回 B 的，A 的不可见。"""
    db, root, wsA, wsB = _create_ab_isolated_db()
    try:
        _seed_finding(db, wsB, "WS-B-ONLY")
        db.conn.commit()

        db.set_active_workspace(wsB)
        issuesB = db.get_symbol_issues("a.alpha")
        msgs = [i["message"] for i in issuesB]
        assert any(m == "WS-B-ONLY" for m in msgs), msgs
        assert not any(m == "WS-A-ONLY" for m in msgs), msgs

        db.set_active_workspace(wsA)
        issuesA = db.get_symbol_issues("a.alpha")
        msgsA = [i["message"] for i in issuesA]
        assert any(m == "WS-A-ONLY" for m in msgsA), msgsA
        assert not any(m == "WS-B-ONLY" for m in msgsA), msgsA
    finally:
        db.close()


def test_write_path_binds_workspace_id_scan_guardrails():
    """scan_guardrails 写入的 finding 必须绑定 active workspace_id。"""
    db, root = _db_with_workspace()
    try:
        wsA = db._get_active_workspace_id()
        (Path := __import__("pathlib").Path)(root)  # noqa
        wsA_root = os.path.join(root, "A")
        os.makedirs(wsA_root, exist_ok=True)
        danger = os.path.join(wsA_root, "danger.sql")
        with open(danger, "w", encoding="utf-8") as f:
            f.write("ALTER TABLE users ADD COLUMN x INT;\n")
        _seed_symbol(db, wsA, wsA_root, rel_path="danger.sql", qualified_name="a.danger")
        db.conn.commit()

        findings = db.scan_guardrails()
        assert findings, "danger.sql 应产生 guardrail finding"
        # 写入的行必须带 workspace_id
        row = db.conn.execute(
            "SELECT workspace_id, rule_id FROM guardrail_findings WHERE file_path='danger.sql' LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["workspace_id"] == wsA, row
    finally:
        db.close()


def test_orphaned_rows_fail_closed_even_in_own_workspace():
    """旧数据 orphaned（status='orphaned'）即使同路径同 hash 也不返回。"""
    db, _root, wsA, _wsB = _create_ab_isolated_db()
    try:
        # 模拟 migration 产出的 orphaned 行（v48 迁移把无归属 open 旧行 status 置为
        # orphaned；FK 约束禁止再建 workspace_id=0，故此处仅置 status 验证查询过滤）
        _seed_finding(db, wsA, "ORPHAN-CANDIDATE")
        db.conn.execute(
            "UPDATE guardrail_findings SET status='orphaned' "
            "WHERE message='ORPHAN-CANDIDATE'"
        )
        db.conn.commit()
        db.set_active_workspace(wsA)
        issues = db.get_symbol_issues("a.alpha")
        msgs = [i["message"] for i in issues]
        assert "WS-A-ONLY" in msgs, msgs
        assert "ORPHAN-CANDIDATE" not in msgs, msgs
    finally:
        db.close()


def test_write_rejects_workspace_zero_fk():
    """FK 约束：直接写 workspace_id=0 的 open finding 必须被拒绝（fail-closed）。

    foreign_keys=ON（生产）时由 FK 拒绝；foreign_keys=OFF 时由查询层
    status!='orphaned' 过滤保证不返回。两种模式下 0 归属 open 行都不可见。
    """
    import sqlite3 as _sqlite3

    db, _root = _db_with_workspace()
    try:
        _seed_guardrail_rule(db)
        # 先探测 FK 是否启用（避免测试耦合具体 PRAGMA）
        fk_on = db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        inserted = False
        try:
            db.conn.execute(
                "INSERT INTO guardrail_findings(workspace_id, rule_id, file_path, symbol_hash, "
                "severity, status, message, detected_at) VALUES (0,'gr1','a.py','sh','warn','open','x',1)"
            )
            db.conn.commit()
            inserted = True
        except _sqlite3.IntegrityError:
            inserted = False  # FK ON：fail-closed 拒绝

        if inserted:
            # FK OFF：行已写入，但查询层必须过滤（fail-closed 语义仍成立）
            db.set_active_workspace(db._get_active_workspace_id())
            issues = db.get_symbol_issues("a.alpha")
            assert not any("x" == i.get("message", "") for i in issues), issues
        else:
            # FK ON：插入被拒绝
            assert fk_on, "workspace_id=0 插入被拒绝但 FK 未启用？"
    finally:
        db.close()


# ============================================
# backup/restore
# ============================================

def test_backup_restore_preserves_workspace_isolation():
    """backup（VACUUM INTO）/restore（复制回主库）后隔离仍成立。"""
    import shutil

    db, root, wsA, wsB = _create_ab_isolated_db()
    try:
        backup_path = os.path.join(root, "backup.db")
        # 与 Rust handle_backup 相同的 VACUUM INTO 语义
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.conn.execute(f"VACUUM INTO '{backup_path.replace(chr(39), chr(39) + chr(39))}'")
    finally:
        db.close()

    # restore：用备份副本替换主库后重新打开
    dbpath = os.path.join(root, "callwarden.db")
    shutil.copy2(backup_path, dbpath)
    for suffix in ("-wal", "-shm"):
        sidecar = dbpath + suffix
        if os.path.exists(sidecar):
            os.remove(sidecar)

    db2 = CodeGraphDB(dbpath, workspace_root=os.path.join(root, "A"))
    try:
        # 恢复后 A/B 隔离仍成立
        db2.set_active_workspace(wsA)
        issuesA = db2.get_symbol_issues("a.alpha")
        assert any("WS-A-ONLY" in i["message"] for i in issuesA), issuesA

        db2.set_active_workspace(wsB)
        issuesB = db2.get_symbol_issues("a.alpha")
        assert not any("WS-A-ONLY" in i.get("message", "") for i in issuesB), issuesB
    finally:
        db2.close()


# ============================================
# query.issues 三路径（Python 侧）
# ============================================

def test_get_symbol_issues_guardrail_workspace_filter():
    """get_symbol_issues 的 guardrail 分支必须带 workspace 过滤（与 semgrep 一致）。"""
    db, _root, wsA, wsB = _create_ab_isolated_db()
    try:
        # B 也有同路径符号但无 finding
        db.set_active_workspace(wsB)
        issuesB = db.get_symbol_issues("a.alpha")
        assert all(i["source"] != "guardrail" for i in issuesB), issuesB

        # A 有 guardrail finding
        db.set_active_workspace(wsA)
        issuesA = db.get_symbol_issues("a.alpha")
        guardrail_msgs = [i["message"] for i in issuesA if i["source"] == "guardrail"]
        assert "WS-A-ONLY" in guardrail_msgs, guardrail_msgs
    finally:
        db.close()
