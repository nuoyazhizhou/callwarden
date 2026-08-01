import sqlite3
import sys
import types

from callwarden.db.db_workspace_manifest import (
    count_manifests,
    get_manifest,
    get_snapshot_files,
    init_manifest_schema,
    list_manifests,
    link_to_snapshot,
    upsert_manifest,
    verify_raw_hash,
)


def _make_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "manifest.db")
    conn.row_factory = sqlite3.Row
    init_manifest_schema(conn)
    upsert_manifest(
        conn,
        workspace_id=7,
        rel_path="src/main.rs",
        content_hash="content-1",
        cas_key="cas-1",
        raw_hash="raw-1",
        is_dirty=True,
    )
    link_to_snapshot(conn, "snapshot-1", "src/main.rs", "content-1", "cas-1")
    return conn


def test_manifest_read_production_facade_uses_rust(monkeypatch, tmp_path):
    conn = _make_db(tmp_path)
    calls = []

    fake = types.SimpleNamespace(
        manifest_get=lambda path, ws, rel: calls.append(("get", path, ws, rel)) or {"rel_path": rel},
        manifest_list=lambda path, ws, dirty: calls.append(("list", ws, dirty)) or [{"workspace_id": ws}],
        manifest_count=lambda path, ws, dirty: calls.append(("count", ws, dirty)) or 1,
        snapshot_get_files=lambda path, snapshot: calls.append(("snapshot", snapshot)) or [{"snapshot_id": snapshot}],
        manifest_verify_raw_hash=lambda path, ws, rel, expected: calls.append(("verify", ws, rel, expected)) or True,
    )
    monkeypatch.setitem(sys.modules, "callwarden_core", fake)

    assert get_manifest(conn, 7, "src/main.rs")["rel_path"] == "src/main.rs"
    assert list_manifests(conn, 7, True) == [{"workspace_id": 7}]
    assert count_manifests(conn, 7, True) == 1
    assert get_snapshot_files(conn, "snapshot-1") == [{"snapshot_id": "snapshot-1"}]
    assert verify_raw_hash(conn, 7, "src/main.rs", "raw-1") is True
    assert [item[0] for item in calls] == ["get", "list", "count", "snapshot", "verify"]


def test_manifest_read_facade_honors_rollback_flag(monkeypatch, tmp_path):
    conn = _make_db(tmp_path)
    conn.execute(
        "CREATE TABLE rollback_config (feature_name TEXT, rollback_flag INTEGER, updated_at REAL)"
    )
    conn.execute(
        "INSERT INTO rollback_config VALUES ('rust_manifest_query', 1, 1.0)"
    )
    conn.commit()

    fake = types.SimpleNamespace(
        manifest_get=lambda *_args: (_ for _ in ()).throw(AssertionError("Rust must be rolled back"))
    )
    monkeypatch.setitem(sys.modules, "callwarden_core", fake)

    result = get_manifest(conn, 7, "src/main.rs")
    assert result is not None
    assert result["content_hash"] == "content-1"
