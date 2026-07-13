"""GraphStore 紧凑名称索引与 callee CSR 回归测试。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


RUST_TARGET = Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"
sys.path.insert(0, str(RUST_TARGET))
callwarden_core = pytest.importorskip("callwarden_core")


@pytest.fixture
def compact_index_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "compact-index.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY,
            rel_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY,
            file_instance_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            module_path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            depth INTEGER NOT NULL
        );
        CREATE TABLE calls (
            caller_id INTEGER NOT NULL,
            callee_id INTEGER NOT NULL,
            callee_name TEXT NOT NULL,
            call_line INTEGER NOT NULL,
            is_cross_file INTEGER NOT NULL
        );

        INSERT INTO file_instances VALUES
            (1, 'src/a.py', 'active'),
            (2, 'src/b.py', 'active');
        INSERT INTO symbols VALUES
            (1, 1, 'fn', 'foo', 'mod_a.foo', 'mod_a', 1, 10, 0),
            (2, 2, 'fn', 'foo', 'mod_b.foo', 'mod_b', 1, 10, 0),
            (3, 1, 'fn', 'target', 'mod_a.target', 'mod_a', 20, 30, 0),
            (4, 2, 'fn', 'target', 'mod_b.target', 'mod_b', 20, 30, 0);
        INSERT INTO calls VALUES
            (1, 3, 'target', 3, 0),
            (1, 0, 'target', 4, 1),
            (2, 4, 'target', 5, 0);
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _caller_qnames(store) -> list[str]:
    return sorted(row["caller_qualified"] for row in store.get_callers("target"))


def test_staged_load_exposes_symbols_before_calls(compact_index_db: Path):
    store = callwarden_core.GraphStore()
    assert store.load_state() == "empty"

    assert store.load_symbols_from_sqlite(str(compact_index_db)) == 5
    assert store.load_state() == "symbols_ready"
    assert store.get_symbol("mod_a.target")["name"] == "target"
    assert len(store.search_symbols("target", None, 10)) == 2
    with pytest.raises(RuntimeError, match="calls not ready"):
        store.get_callers("target")

    assert store.load_from_sqlite(str(compact_index_db)) == (5, 3)
    assert store.load_state() == "graph_ready"
    assert _caller_qnames(store) == ["mod_a.foo", "mod_a.foo", "mod_b.foo"]


def test_compact_indexes_preserve_short_and_qualified_queries(compact_index_db: Path):
    store = callwarden_core.GraphStore()
    store.load_from_sqlite(str(compact_index_db))

    assert _caller_qnames(store) == ["mod_a.foo", "mod_a.foo", "mod_b.foo"]
    precise = list(store.get_callers("target", "mod_a.target"))
    assert [row["caller_qualified"] for row in precise] == ["mod_a.foo"]

    all_callees = store.get_callees("foo")
    assert len(all_callees) == 3
    precise_callees = store.get_callees("foo", "mod_a.foo")
    assert len(precise_callees) == 2
    assert dict(store.compute_depth_all()) == {1: 1, 2: 1, 3: 0, 4: 0}


def test_compact_indexes_round_trip_existing_snapshot_format(
    compact_index_db: Path, tmp_path: Path
):
    snapshot_path = tmp_path / "compact.cwsnap"
    source = callwarden_core.GraphStore()
    source.load_from_sqlite(str(compact_index_db))
    source.dump_to_file(str(snapshot_path))

    restored = callwarden_core.GraphStore()
    restored.load_from_file(str(snapshot_path))
    assert _caller_qnames(restored) == _caller_qnames(source)
    assert restored.get_callees("foo") == source.get_callees("foo")


def test_snapshot_v1_is_rejected_explicitly(compact_index_db: Path, tmp_path: Path):
    snapshot_path = tmp_path / "old-format.cwsnap"
    source = callwarden_core.GraphStore()
    source.load_from_sqlite(str(compact_index_db))
    source.dump_to_file(str(snapshot_path))

    data = bytearray(snapshot_path.read_bytes())
    data[4:8] = (1).to_bytes(4, "little")
    snapshot_path.write_bytes(data)

    restored = callwarden_core.GraphStore()
    with pytest.raises(RuntimeError, match=r"unsupported snapshot version: 1"):
        restored.load_from_file(str(snapshot_path))


def test_memory_breakdown_reports_compact_capacity(compact_index_db: Path):
    store = callwarden_core.GraphStore()
    store.load_from_sqlite(str(compact_index_db))

    stats = store.stats()
    memory = store.memory_breakdown()
    assert stats["callee_name_pool_size"] == len("target")
    assert memory["simple_name_sorted_ids"] > 0
    assert memory["backward_positions"] == 3 * 4
    assert "backward_edges" not in memory
    assert memory["callee_positions"] == 3 * 4
    assert memory["known_heap_total"] == sum(
        value for key, value in memory.items() if key != "known_heap_total"
    )
