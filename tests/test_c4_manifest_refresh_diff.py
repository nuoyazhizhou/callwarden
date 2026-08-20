# -*- coding: utf-8 -*-
"""C4（Manifest 与 refresh commit 完整迁移）差分测试。

验证 manifest / projection / refresh commit 的生产写路径已统一到 Rust
facade，Python adapter（db_workspace_manifest.py）与 Rust facade 行为一致，
daemon overlay merge 的 manifest/无历史语义不回归。

契约：docs/design/c4-manifest-refresh-commit-contract.md §4/§5
"""
import sqlite3

import pytest

callwarden_core = pytest.importorskip("callwarden_core")

from callwarden.db.db_workspace_manifest import (  # noqa: E402
    count_manifests,
    get_manifest,
    get_snapshot_files,
    init_manifest_schema,
    list_manifests,
    link_to_snapshot,
    upsert_manifest,
)


# ---------------------------------------------------------------
# fixture：manifest 层（workspace DB）
# ---------------------------------------------------------------

@pytest.fixture()
def manifest_db(tmp_path):
    """临时 workspace DB，schema 由 Rust manifest_init_schema 初始化。"""
    db_path = str(tmp_path / "workspace.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_manifest_schema(conn)
    yield conn, db_path
    conn.close()


@pytest.fixture()
def merged_manifest(manifest_db):
    """写入 2 行 manifest（Rust facade 路径）：main.rs dirty + util.py clean。"""
    conn, db_path = manifest_db
    upsert_manifest(
        conn, workspace_id=7, rel_path="src/main.rs",
        content_hash="content-1", cas_key="cas-1", raw_hash="raw-1",
        is_dirty=True,
    )
    upsert_manifest(
        conn, workspace_id=7, rel_path="src/util.py",
        content_hash="content-2", cas_key="cas-2",
        is_dirty=False,
    )
    return conn, db_path


# ---------------------------------------------------------------
# C4：manifest adapter 与 Rust facade / SQL 直读一致
# ---------------------------------------------------------------

class TestManifestAdapterConsistency:
    def test_upsert_readback_sql_and_adapter(self, merged_manifest):
        """C1/C4：upsert（Rust facade）落库后，SQL 直读与 adapter 读一致。"""
        conn, _ = merged_manifest
        row = conn.execute(
            "SELECT workspace_id, rel_path, content_hash, cas_key, is_dirty "
            "FROM workspace_manifests WHERE workspace_id = 7 AND rel_path = 'src/main.rs'"
        ).fetchone()
        assert dict(row) == {
            "workspace_id": 7, "rel_path": "src/main.rs",
            "content_hash": "content-1", "cas_key": "cas-1", "is_dirty": 1,
        }
        got = get_manifest(conn, 7, "src/main.rs")
        assert got["content_hash"] == "content-1"
        assert got["cas_key"] == "cas-1"

    def test_rust_manifest_get_equals_adapter(self, merged_manifest):
        """C4：Rust manifest_get 直接调用与 adapter get_manifest 结果一致。"""
        conn, db_path = merged_manifest
        rust_row = callwarden_core.manifest_get(db_path, 7, "src/main.rs")
        assert rust_row is not None
        adapter_row = get_manifest(conn, 7, "src/main.rs")
        for key in ("rel_path", "content_hash", "cas_key", "raw_hash", "is_dirty"):
            assert adapter_row[key] == rust_row[key]

    def test_list_dirty_filter(self, merged_manifest):
        """C4：list_manifests dirty_only 过滤与 SQL 一致。"""
        conn, _ = merged_manifest
        dirty = list_manifests(conn, 7, dirty_only=True)
        assert [m["rel_path"] for m in dirty] == ["src/main.rs"]
        all_rows = list_manifests(conn, 7, dirty_only=False)
        assert {m["rel_path"] for m in all_rows} == {"src/main.rs", "src/util.py"}

    def test_count_manifests(self, merged_manifest):
        """C4：count_manifests 与 SQL COUNT 一致。"""
        conn, _ = merged_manifest
        assert count_manifests(conn, 7) == 2
        assert count_manifests(conn, 7, dirty_only=True) == 1


# ---------------------------------------------------------------
# C5：clean workspace 复用 snapshot（workspace_snapshot_map）
# ---------------------------------------------------------------

class TestSnapshotMap:
    def test_link_and_readback(self, manifest_db):
        """C5：link_to_snapshot（Rust facade）写后 adapter/SQL 读一致。"""
        conn, _ = manifest_db
        link_to_snapshot(conn, "snap-1", "src/main.rs", "content-1", "cas-1")
        files = get_snapshot_files(conn, "snap-1")
        assert len(files) == 1
        assert files[0]["rel_path"] == "src/main.rs"
        row = conn.execute(
            "SELECT snapshot_id, rel_path FROM workspace_snapshot_map "
            "WHERE snapshot_id = 'snap-1'"
        ).fetchone()
        assert row["rel_path"] == "src/main.rs"


# ---------------------------------------------------------------
# C2/C6：daemon overlay merge → manifest is_dirty=1、不写版本历史
# ---------------------------------------------------------------

_CAS_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS cas_file_cache (
    cas_key TEXT PRIMARY KEY, content_hash TEXT, language TEXT,
    file_size INTEGER, total_lines INTEGER, parser_version TEXT,
    callwarden_version TEXT, extraction_config_version TEXT,
    abi_version TEXT, input_abi_version TEXT, state TEXT, parsed_at REAL
);
CREATE TABLE IF NOT EXISTS cas_symbols (
    cas_key TEXT, local_symbol_id INTEGER, symbol_content_hash TEXT,
    name TEXT, local_qualified_name TEXT, kind TEXT,
    start_line INTEGER, end_line INTEGER, start_col INTEGER, end_col INTEGER,
    visibility TEXT, signature TEXT, has_comment INTEGER, depth INTEGER
);
CREATE TABLE IF NOT EXISTS cas_symbol_contents (
    content_hash TEXT PRIMARY KEY, content TEXT
);
CREATE TABLE IF NOT EXISTS cas_raw_calls (
    cas_key TEXT, caller_local_id INTEGER, caller_name TEXT,
    callee_name TEXT, call_line INTEGER
);
"""


def _make_cas_db(tmp_path):
    db_path = str(tmp_path / "cas.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_CAS_SCHEMA_DDL)
    conn.execute(
        "INSERT INTO cas_file_cache VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("k1", "ch1", "python", 100, 10, "0.1.0", "0.2.0",
         "v1", "v1", "v1", "ready", 1000.0),
    )
    conn.execute(
        "INSERT INTO cas_symbol_contents VALUES ('sch_foo', 'def foo():\\n    pass\\n')")
    conn.execute(
        "INSERT INTO cas_symbols VALUES "
        "('k1', 1, 'sch_foo', 'foo', 'foo', 'function', 1, 3, 0, 0, "
        "'public', 'def foo()', 0, -1)")
    conn.commit()
    conn.close()
    return db_path


class TestOverlayMerge:
    def test_merge_writes_snapshot_no_history_dirty_manifest(self, tmp_path):
        """C2/C6：overlay merge 写当前快照 + dirty manifest，不写 file_versions 历史。

        init_codegraph_schema 不建 file_versions 表（daemon overlay 语义）；
        workspace_manifests 由 merge 私有写 is_dirty=1。
        """
        cas_db = _make_cas_db(tmp_path)
        cg_db = str(tmp_path / "codegraph.db")
        assert callwarden_core.cas_merge_init_schema(cg_db) is True

        result = callwarden_core.cas_merge_to_codegraph(
            cas_db, cg_db, "k1", 1, "src/main.py", "/app/src/main.py",
            "ch1", "python", "/app",
        )
        assert result.get("ok", True) is not False

        cg = sqlite3.connect(cg_db)
        cg.row_factory = sqlite3.Row
        try:
            # overlay 当前快照已写
            fi = cg.execute(
                "SELECT COUNT(*) AS n FROM file_instances WHERE workspace_id = 1"
            ).fetchone()
            assert fi["n"] == 1
            sym = cg.execute(
                "SELECT COUNT(*) AS n FROM symbols s "
                "JOIN file_instances fi ON s.file_instance_id = fi.id "
                "WHERE fi.workspace_id = 1"
            ).fetchone()
            assert sym["n"] == 1
            # overlay schema 不含 file_versions（无历史）
            has_history = cg.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master WHERE name = 'file_versions'"
            ).fetchone()
            assert has_history["n"] == 0
            # merge 私有写 manifest：is_dirty=1
            m = cg.execute(
                "SELECT content_hash, cas_key, is_dirty FROM workspace_manifests "
                "WHERE workspace_id = 1 AND rel_path = 'src/main.py'"
            ).fetchone()
            assert m is not None
            assert m["content_hash"] == "ch1"
            assert m["cas_key"] == "k1"
            assert m["is_dirty"] == 1
        finally:
            cg.close()
