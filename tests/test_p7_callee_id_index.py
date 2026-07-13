"""P7：resolved callee_id 部分索引迁移与 SQL 降级路径测试。"""

from __future__ import annotations

import sqlite3

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_base import _migrate_v32_to_v33
from callwarden.db.schema import SCHEMA_INDEXES_SQL, SCHEMA_TABLES_SQL


def _index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    return {row[0] for row in rows}


def test_v33_migration_replaces_text_index_atomically():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE calls (
            id INTEGER PRIMARY KEY,
            callee_qualified TEXT DEFAULT '',
            callee_id INTEGER DEFAULT 0
        );
        CREATE INDEX idx_calls_callee_qualified ON calls(callee_qualified);
        """
    )

    conn.execute("BEGIN")
    _migrate_v32_to_v33(conn)
    conn.commit()

    names = _index_names(conn)
    assert "idx_calls_callee_qualified" not in names
    assert "idx_calls_callee_id_resolved" in names
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_calls_callee_id_resolved'"
    ).fetchone()[0]
    assert "WHERE callee_id > 0" in sql


def test_resolved_callee_query_uses_partial_integer_index():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE calls (
            id INTEGER PRIMARY KEY,
            callee_qualified TEXT DEFAULT '',
            callee_id INTEGER DEFAULT 0
        );
        CREATE INDEX idx_calls_callee_id_resolved
            ON calls(callee_id) WHERE callee_id > 0;
        """
    )
    plan = conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT id FROM calls WHERE callee_id > 0 AND callee_id = ?",
        (42,),
    ).fetchall()
    detail = " ".join(str(row[3]) for row in plan)
    assert "idx_calls_callee_id_resolved" in detail


def test_symbols_unique_constraint_is_available_before_delayed_indexes():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_TABLES_SQL)
    names = _index_names(conn)
    assert "idx_symbols_unique" in names
    assert "idx_calls_callee_id_resolved" not in names


def _insert_symbol(
    db: CodeGraphDB,
    file_instance_id: int,
    symbol_hash: str,
    name: str,
    qualified_name: str,
) -> int:
    cur = db.conn.execute(
        """
        INSERT INTO symbols (
            file_instance_id, symbol_hash, name, kind, visibility,
            start_line, end_line, qualified_name, module_path
        ) VALUES (?, ?, ?, 'fn', 'public', 1, 2, ?, 'mod')
        """,
        (file_instance_id, symbol_hash, name, qualified_name),
    )
    return int(cur.lastrowid)


def _insert_file(db: CodeGraphDB, workspace_id: int, rel_path: str) -> int:
    cur = db.conn.execute(
        """
        INSERT INTO file_instances (
            workspace_id, rel_path, abs_path, mtime, status, module_path
        ) VALUES (?, ?, ?, 0, 'active', 'mod')
        """,
        (workspace_id, rel_path, f"/{rel_path}"),
    )
    return int(cur.lastrowid)


def test_integer_reverse_queries_preserve_workspace_isolation(tmp_path):
    db = CodeGraphDB(str(tmp_path / "p7.db"), workspace_root=str(tmp_path))
    try:
        ws_a = db.register_workspace("ws-a", str(tmp_path / "a"), "")
        ws_b = db.register_workspace("ws-b", str(tmp_path / "b"), "")

        file_a = _insert_file(db, ws_a, "a.py")
        file_b = _insert_file(db, ws_b, "b.py")
        target_a = _insert_symbol(db, file_a, "target-a", "target", "mod.target")
        caller_a = _insert_symbol(db, file_a, "caller-a", "caller_a", "mod.caller_a")
        test_caller_a = _insert_symbol(
            db, file_a, "test-caller-a", "test_caller_a", "mod.test_caller_a"
        )
        target_b = _insert_symbol(db, file_b, "target-b", "target", "mod.target")
        caller_b = _insert_symbol(db, file_b, "caller-b", "caller_b", "mod.caller_b")

        db.conn.executemany(
            """
            INSERT INTO calls (
                caller_id, caller_name, caller_module, callee_name,
                callee_qualified, callee_id, call_line
            ) VALUES (?, ?, 'mod', 'target', 'mod.target', ?, 10)
            """,
            [
                (caller_a, "caller_a", target_a),
                (caller_b, "caller_b", target_b),
                (test_caller_a, "test_caller_a", caller_a),
            ],
        )
        db.conn.execute(
            """
            INSERT INTO calls (
                caller_id, caller_name, caller_module, callee_name,
                callee_qualified, callee_id, call_line
            ) VALUES (?, 'target', 'mod', 'external_api', '', 0, 11)
            """,
            (target_a,),
        )
        db.conn.executescript(SCHEMA_INDEXES_SQL)
        db.conn.commit()
        db.set_active_workspace(ws_a)

        original_get_graph_store = db._get_graph_store
        db._get_graph_store = lambda: None
        try:
            callers = db.get_callers("target", qualified_name="mod.target")
        finally:
            db._get_graph_store = original_get_graph_store

        assert [row["caller_name"] for row in callers] == ["caller_a"]

        impact = db.blast_radius("target-a", depth=1)
        assert impact["total_impacted"] == 2
        assert impact["layers"][1]["symbols"][0]["qualified_name"] == "mod.caller_a"

        metrics = db.get_function_metrics("mod.target")
        assert metrics is not None
        assert metrics["fan_in"] == 1

        coupled = db.get_most_coupled_functions(limit=10)
        by_name = {row["qualified_name"]: row for row in coupled}
        assert by_name["mod.target"]["fan_in"] == 1

        selected_tests = db.test_impact_selection("mod.target")
        assert [row["qualified_name"] for row in selected_tests] == ["mod.test_caller_a"]
    finally:
        db.close()
