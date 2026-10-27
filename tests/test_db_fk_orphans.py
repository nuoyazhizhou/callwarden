"""外键孤儿治理回归测试（T-1793098691536-7c39fbc4）。

覆盖四类契约：
1. 新库版本表/图数据子表外键必须是 ON DELETE CASCADE；
2. GC archive / purge / delete_workspace 删除后 PRAGMA foreign_key_check 必须为空；
3. gc_reap_orphans 默认 dry-run，apply 后受控范围孤儿归零且合法行保留；
4. 清理前后外键开关状态必须恢复。

依据：docs/design/db-fk-orphan-governance/spec.md
"""

import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from callwarden.db.db import CodeGraphDB

# 新库必须级联的子列（spec.md §4.1）。未列出的外键（如 commit_hash →
# git_commits）保持 NO ACTION：提交历史由 delete_workspace 显式按顺序删除。
CASCADE_FKS = {
    "file_versions": ["file_instance_id", "content_hash"],
    "file_symbol_versions": ["file_version_id", "symbol_hash"],
    "call_versions": ["file_version_id"],
    "symbols": ["file_instance_id", "symbol_hash"],
    "calls": ["caller_id"],
    "git_symbol_changes": ["symbol_hash"],
    "git_file_changes": ["file_instance_id"],
    "semgrep_findings": ["file_instance_id"],
    "archived_files": ["file_instance_id", "workspace_id"],
}


def _new_db():
    root = tempfile.mkdtemp(prefix="cw_fk_orphans_")
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _fk_check(db):
    return db.conn.execute("PRAGMA foreign_key_check").fetchall()


def _count(db, table, where="", params=()):
    sql = "SELECT COUNT(*) FROM %s %s" % (table, where)
    return db.conn.execute(sql, params).fetchone()[0]


def _insert_graph(db, ws_id, tag="a"):
    """插入一套合法图数据（file → version → symbol/call version + snapshot）。"""
    content_hash = "content-%s" % tag
    symbol_hash = "sym-%s" % tag
    now = time.time()
    db.conn.execute(
        "INSERT INTO file_contents (content_hash, language, total_lines, first_seen_at) "
        "VALUES (?, 'python', 2, ?)",
        (content_hash, now),
    )
    db.conn.execute(
        "INSERT INTO symbol_contents (content_hash, name, kind, content, qualified_name) "
        "VALUES (?, ?, 'fn', ?, ?)",
        (symbol_hash, "fn_%s" % tag, "def fn(): pass", "mod.fn_%s" % tag),
    )
    fi_id = db.conn.execute(
        """INSERT INTO file_instances
           (workspace_id, rel_path, abs_path, current_content_hash, mtime,
            total_lines, last_parsed, status, module_path)
           VALUES (?, ?, ?, ?, ?, 2, ?, 'active', 'mod')""",
        (ws_id, "src/%s.py" % tag, "/tmp/%s.py" % tag, content_hash, now, now),
    ).lastrowid
    fv_id = db.conn.execute(
        """INSERT INTO file_versions
           (file_instance_id, version_num, content_hash, mtime, total_lines,
            parsed_at, is_current, is_deleted)
           VALUES (?, 1, ?, ?, 2, ?, 1, 0)""",
        (fi_id, content_hash, now, now),
    ).lastrowid
    db.conn.execute(
        """INSERT INTO file_symbol_versions
           (file_version_id, symbol_hash, qualified_name, start_line, end_line,
            module_path, depth, is_deleted)
           VALUES (?, ?, ?, 1, 2, 'mod', 0, 0)""",
        (fv_id, symbol_hash, "mod.fn_%s" % tag),
    )
    db.conn.execute(
        """INSERT INTO call_versions
           (file_version_id, caller_qualified, callee_name, callee_qualified, call_line)
           VALUES (?, ?, 'g', 'mod.g', 2)""",
        (fv_id, "mod.fn_%s" % tag),
    )
    sym_id = db.conn.execute(
        """INSERT INTO symbols
           (file_instance_id, symbol_hash, name, kind, start_line, end_line, depth)
           VALUES (?, ?, ?, 'fn', 1, 2, 0)""",
        (fi_id, symbol_hash, "fn_%s" % tag),
    ).lastrowid
    db.conn.execute(
        """INSERT INTO calls (caller_id, caller_name, caller_module, callee_name)
           VALUES (?, ?, 'mod', 'g')""",
        (sym_id, "fn_%s" % tag),
    )
    db.conn.commit()
    return fi_id, fv_id, sym_id


def test_new_schema_version_fks_cascade():
    """新库 schema：版本表/图数据子表的指定子列外键必须 CASCADE。"""
    db, _root = _new_db()
    try:
        for table, columns in CASCADE_FKS.items():
            rows = list(db.conn.execute("PRAGMA foreign_key_list(%s)" % table))
            assert rows, "%s 应至少有一个外键" % table
            by_column = {row["from"]: row for row in rows}
            for column in columns:
                assert column in by_column, "%s 缺少 %s 外键" % (table, column)
                assert by_column[column]["on_delete"] == "CASCADE", (
                    "%s.%s -> %s 的 on_delete=%r，应为 CASCADE"
                    % (table, column, by_column[column]["table"],
                       by_column[column]["on_delete"])
                )
        # comments 属于用户数据，不应被内容清理级联删除
        comment_fks = list(db.conn.execute("PRAGMA foreign_key_list(comments)"))
        assert comment_fks and all(r["on_delete"] == "NO ACTION" for r in comment_fks)
    finally:
        db.close()


def test_cascade_deletes_version_children():
    """删除 file_instances 时，版本表与图快照必须级联清空且无孤儿。"""
    db, root = _new_db()
    try:
        ws_id = db._get_active_workspace_id()
        fi_id, _fv_id, _sym_id = _insert_graph(db, ws_id, tag="cascade")

        db.conn.execute("DELETE FROM file_instances WHERE id = ?", (fi_id,))
        db.conn.commit()

        for table in ("file_versions", "file_symbol_versions", "call_versions",
                      "symbols", "calls"):
            assert _count(db, table) == 0, "%s 应被级联清空" % table
        assert _fk_check(db) == []
    finally:
        db.close()


def test_gc_archive_leaves_no_orphans():
    """gc_archive 必须按 child→parent 删除 call_versions/版本/符号，不留孤儿。"""
    db, root = _new_db()
    try:
        db._get_active_workspace_id()
        abs_path = os.path.join(root, "build", "gen.py")
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write("def gen():\n    pass\n")

        fi_id = db._register_file_db(abs_path, "mod")
        db.conn.commit()

        now = time.time()
        symbol_hash = "sym-archived"
        db.conn.execute(
            "INSERT INTO symbol_contents (content_hash, name, kind, content) "
            "VALUES (?, 'gen', 'fn', 'def gen(): pass')",
            (symbol_hash,),
        )
        fv_id = db.conn.execute(
            """INSERT INTO file_versions
               (file_instance_id, version_num, content_hash, mtime, total_lines,
                parsed_at, is_current, is_deleted)
               VALUES (?, 1, '', ?, 2, ?, 1, 0)""",
            (fi_id, now, now),
        ).lastrowid
        db.conn.execute(
            """INSERT INTO file_symbol_versions
               (file_version_id, symbol_hash, qualified_name, start_line, end_line,
                module_path, depth, is_deleted)
               VALUES (?, ?, 'mod.gen', 1, 2, 'mod', 0, 0)""",
            (fv_id, symbol_hash),
        )
        db.conn.execute(
            """INSERT INTO call_versions
               (file_version_id, caller_qualified, callee_name, callee_qualified, call_line)
               VALUES (?, 'mod.gen', 'print', 'builtins.print', 2)""",
            (fv_id,),
        )
        sym_id = db.conn.execute(
            """INSERT INTO symbols
               (file_instance_id, symbol_hash, name, kind, start_line, end_line, depth)
               VALUES (?, ?, 'gen', 'fn', 1, 2, 0)""",
            (fi_id, symbol_hash),
        ).lastrowid
        db.conn.execute(
            """INSERT INTO calls (caller_id, caller_name, caller_module, callee_name)
               VALUES (?, 'gen', 'mod', 'print')""",
            (sym_id,),
        )
        db.conn.commit()

        result = db.gc_archive(force=True, dry_run=False)
        assert result["archived"] == 1

        for table in ("file_versions", "file_symbol_versions", "call_versions",
                      "symbols", "calls"):
            assert _count(db, table) == 0, "%s 归档后应清空" % table
        assert _fk_check(db) == []
    finally:
        db.close()


def test_gc_purge_leaves_no_orphans():
    """gc_purge 要补齐历史残留子表（版本/调用/semgrep/git），不留孤儿。"""
    db, root = _new_db()
    try:
        ws_id = db._get_active_workspace_id()
        fi_id, fv_id, sym_id = _insert_graph(db, ws_id, tag="purge")
        now = time.time()

        db.conn.execute(
            """INSERT INTO semgrep_findings
               (file_instance_id, content_hash, rule_id, rule_name, severity, start_line)
               VALUES (?, 'content-purge', 'rule-1', 'r', 'INFO', 1)""",
            (fi_id,),
        )
        db.conn.execute(
            "INSERT INTO git_commits (commit_hash, message, timestamp, workspace_id) "
            "VALUES ('commit-purge', 'm', ?, ?)",
            (now, ws_id),
        )
        db.conn.execute(
            """INSERT INTO git_file_changes
               (commit_hash, file_instance_id, change_type)
               VALUES ('commit-purge', ?, 'modified')""",
            (fi_id,),
        )
        db.conn.execute(
            """INSERT INTO git_symbol_changes
               (commit_hash, symbol_hash, change_type)
               VALUES ('commit-purge', 'sym-purge', 'modified')"""
        )
        db.conn.execute(
            """INSERT INTO archived_files
               (file_instance_id, workspace_id, rel_path, abs_path, archived_at)
               VALUES (?, ?, 'src/purge.py', '/tmp/purge.py', ?)""",
            (fi_id, ws_id, now - 10 * 86400),
        )
        db.conn.commit()

        result = db.gc_purge(older_than_days=0)
        assert result["purged_files"] == 1

        for table in ("file_instances", "file_versions", "file_symbol_versions",
                      "call_versions", "symbols", "calls", "semgrep_findings",
                      "git_file_changes", "archived_files"):
            assert _count(db, table) == 0, "%s purge 后应清空" % table
        assert _fk_check(db) == []
    finally:
        db.close()


def test_reap_orphans_dry_run_and_apply():
    """gc_reap_orphans：dry-run 不改数；apply 清空受控孤儿、保留合法行并备份。"""
    db, root = _new_db()
    try:
        ws_id = db._get_active_workspace_id()
        valid_fi, valid_fv, valid_sym = _insert_graph(db, ws_id, tag="keep")

        # 造历史孤儿（模拟老库在 foreign_keys=OFF 下 GC 的产物）
        db.conn.execute("PRAGMA foreign_keys=OFF")
        orphan_fv = db.conn.execute(
            """INSERT INTO file_versions
               (file_instance_id, version_num, content_hash, mtime, total_lines,
                parsed_at, is_current, is_deleted)
               VALUES (999999, 1, 'missing-content', ?, 1, ?, 0, 0)""",
            (time.time(), time.time()),
        ).lastrowid
        db.conn.execute(
            """INSERT INTO file_symbol_versions
               (file_version_id, symbol_hash, qualified_name, start_line, end_line)
               VALUES (?, 'missing-sym', 'mod.dead', 1, 2)""",
            (orphan_fv,),
        )
        db.conn.execute(
            """INSERT INTO call_versions
               (file_version_id, caller_qualified, callee_name)
               VALUES (?, 'mod.dead', 'g')""",
            (orphan_fv,),
        )
        db.conn.execute(
            """INSERT INTO git_symbol_changes (commit_hash, symbol_hash, change_type)
               VALUES ('deadbeef', 'missing-sym', 'modified')"""
        )
        db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys=ON")

        original_fk = db.conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert original_fk == 1
        before = len(_fk_check(db))
        assert before > 0

        dry = db.gc_reap_orphans(dry_run=True)
        assert dry["dry_run"] is True
        assert dry["deleted"] == {}
        assert dry["after"] == dry["before"]
        assert len(_fk_check(db)) == before

        applied = db.gc_reap_orphans(dry_run=False, backup=True)
        assert applied["dry_run"] is False
        assert sum(applied["deleted"].values()) > 0
        assert applied["after"] == {}
        assert applied["out_of_scope"] == {}
        assert applied["backup_path"] and os.path.exists(applied["backup_path"])
        assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == original_fk
        assert _fk_check(db) == []

        # 合法图数据必须保留
        assert _count(db, "file_instances", "WHERE id = ?", (valid_fi,)) == 1
        assert _count(db, "file_versions", "WHERE id = ?", (valid_fv,)) == 1
        assert _count(db, "symbols", "WHERE id = ?", (valid_sym,)) == 1
        assert _count(db, "file_symbol_versions", "WHERE file_version_id = ?",
                      (valid_fv,)) == 1
    finally:
        db.close()


def test_delete_workspace_leaves_no_orphans():
    """delete_workspace 补齐 archived/git 子表并把 gc_runs 归属置空。"""
    db, root = _new_db()
    try:
        ws_id = db._get_active_workspace_id()
        fi_id, _fv_id, _sym_id = _insert_graph(db, ws_id, tag="ws")
        now = time.time()
        db.conn.execute(
            """INSERT INTO archived_files
               (file_instance_id, workspace_id, rel_path, abs_path, archived_at)
               VALUES (?, ?, 'src/ws.py', '/tmp/ws.py', ?)""",
            (fi_id, ws_id, now),
        )
        db.conn.execute(
            "INSERT INTO git_commits (commit_hash, message, timestamp, workspace_id) "
            "VALUES ('commit-ws', 'm', ?, ?)",
            (now, ws_id),
        )
        db.conn.execute(
            """INSERT INTO git_file_changes
               (commit_hash, file_instance_id, change_type)
               VALUES ('commit-ws', ?, 'modified')""",
            (fi_id,),
        )
        db.conn.execute(
            """INSERT INTO git_symbol_changes
               (commit_hash, symbol_hash, change_type)
               VALUES ('commit-ws', 'sym-ws', 'modified')"""
        )
        db.conn.execute(
            """INSERT INTO gc_runs
               (workspace_id, operation, started_at, status)
               VALUES (?, 'archive', ?, 'completed')""",
            (ws_id, now),
        )
        db.conn.commit()

        assert db.delete_workspace(ws_id) is True

        for table in ("file_instances", "file_versions", "file_symbol_versions",
                      "call_versions", "symbols", "calls", "archived_files",
                      "git_file_changes", "git_commits", "git_symbol_changes"):
            assert _count(db, table) == 0, "%s 删工作区后应清空" % table
        assert _count(db, "gc_runs", "WHERE workspace_id IS NULL") == 1
        assert _fk_check(db) == []
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
