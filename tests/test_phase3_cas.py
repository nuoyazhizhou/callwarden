"""Phase 3 CAS + manifest 单元测试。"""

import os
import tempfile
import sqlite3
import hashlib
import pytest

from callwarden.db.db_cas import (
    init_cas_schema,
    compute_cas_key_v1,
    cas_lookup,
    cas_publish,
    cas_pin,
    cas_gc,
)
from callwarden.db.db_workspace_manifest import (
    init_manifest_schema,
    upsert_manifest,
    get_manifest,
    list_manifests,
    link_to_snapshot,
    get_snapshot_files,
    verify_raw_hash,
)


@pytest.fixture
def tmp_cas_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_cas_schema(conn)
    yield conn
    conn.close()
    os.unlink(db_path)


@pytest.fixture
def tmp_manifest_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_manifest_schema(conn)
    yield conn
    conn.close()
    os.unlink(db_path)


# ── CAS key 测试 ──

class TestCasKey:

    def test_compute_cas_key_deterministic(self):
        """相同输入产生相同 key。"""
        key1 = compute_cas_key_v1("hash1", "python", "0.1", "0.2", "v1", "v1", "v1")
        key2 = compute_cas_key_v1("hash1", "python", "0.1", "0.2", "v1", "v1", "v1")
        assert key1 == key2

    def test_compute_cas_key_different_content(self):
        """不同内容产生不同 key。"""
        key1 = compute_cas_key_v1("hash1", "python", "0.1", "0.2", "v1", "v1", "v1")
        key2 = compute_cas_key_v1("hash2", "python", "0.1", "0.2", "v1", "v1", "v1")
        assert key1 != key2

    def test_compute_cas_key_different_language(self):
        """不同语言产生不同 key。"""
        key1 = compute_cas_key_v1("hash1", "python", "0.1", "0.2", "v1", "v1", "v1")
        key2 = compute_cas_key_v1("hash1", "rust", "0.1", "0.2", "v1", "v1", "v1")
        assert key1 != key2

    def test_compute_cas_key_different_parser_version(self):
        """不同 parser_version 产生不同 key。"""
        key1 = compute_cas_key_v1("hash1", "python", "0.1", "0.2", "v1", "v1", "v1")
        key2 = compute_cas_key_v1("hash1", "python", "0.2", "0.2", "v1", "v1", "v1")
        assert key1 != key2


# ── CAS publish + lookup 测试 ──

class TestCasPublishLookup:

    def test_lookup_miss_returns_none(self, tmp_cas_db):
        """未发布的 key 查询返回 None。"""
        result = cas_lookup(tmp_cas_db, "nonexistent_key")
        assert result is None

    def test_publish_then_lookup_hits(self, tmp_cas_db):
        """发布后查询命中。"""
        cas_key = compute_cas_key_v1("hash1", "python", "0.1", "0.2", "v1", "v1", "v1")
        parse_result = {
            "symbols": [
                {"name": "func1", "content": "def func1(): pass", "kind": "function",
                 "start_line": 1, "end_line": 1},
            ],
            "raw_calls": [],
            "imports": [],
        }
        cas_publish(tmp_cas_db, cas_key, "hash1", "python", parse_result)

        result = cas_lookup(tmp_cas_db, cas_key)
        assert result is not None
        assert result["state"] == "ready"
        assert result["content_hash"] == "hash1"
        assert result["language"] == "python"

    def test_publish_writes_symbols(self, tmp_cas_db):
        """发布后符号写入 cas_symbols。"""
        cas_key = compute_cas_key_v1("hash1", "python", "0.1", "0.2", "v1", "v1", "v1")
        parse_result = {
            "symbols": [
                {"name": "func1", "content": "def func1(): pass", "kind": "function",
                 "start_line": 1, "end_line": 1, "qualified_name": "func1"},
                {"name": "func2", "content": "def func2(): pass", "kind": "function",
                 "start_line": 3, "end_line": 3, "qualified_name": "func2"},
            ],
            "raw_calls": [],
            "imports": [],
        }
        cas_publish(tmp_cas_db, cas_key, "hash1", "python", parse_result)

        symbols = tmp_cas_db.execute(
            "SELECT * FROM cas_symbols WHERE cas_key = ?", (cas_key,)
        ).fetchall()
        assert len(symbols) == 2

    def test_publish_writes_raw_calls(self, tmp_cas_db):
        """发布后 raw calls 写入。"""
        cas_key = compute_cas_key_v1("hash1", "python", "0.1", "0.2", "v1", "v1", "v1")
        parse_result = {
            "symbols": [],
            "raw_calls": [
                {"caller_name": "func1", "callee_name": "func2", "line": 5},
            ],
            "imports": [],
        }
        cas_publish(tmp_cas_db, cas_key, "hash1", "python", parse_result)

        calls = tmp_cas_db.execute(
            "SELECT * FROM cas_raw_calls WHERE cas_key = ?", (cas_key,)
        ).fetchall()
        assert len(calls) == 1
        assert calls[0]["callee_name"] == "func2"


# ── CAS pin + GC 测试 ──

class TestCasGc:

    def test_pin_protects_from_gc(self, tmp_cas_db):
        """pin 的 key 不被 GC 清理。"""
        cas_key = compute_cas_key_v1("hash1", "python", "0.1", "0.2", "v1", "v1", "v1")
        parse_result = {"symbols": [], "raw_calls": [], "imports": []}
        cas_publish(tmp_cas_db, cas_key, "hash1", "python", parse_result)
        cas_pin(tmp_cas_db, cas_key, workspace_id=1, ttl_seconds=3600)

        # GC 时 live_keys 为空，但 pending_refs 保护
        result = cas_gc(tmp_cas_db, set())
        assert result is True

        # cas_key 应该还在
        assert cas_lookup(tmp_cas_db, cas_key) is not None

    def test_gc_removes_unpinned(self, tmp_cas_db):
        """未 pin 的 key 被 GC 清理。"""
        cas_key = compute_cas_key_v1("hash1", "python", "0.1", "0.2", "v1", "v1", "v1")
        parse_result = {"symbols": [], "raw_calls": [], "imports": []}
        cas_publish(tmp_cas_db, cas_key, "hash1", "python", parse_result)

        # GC 时 live_keys 不含此 key
        result = cas_gc(tmp_cas_db, set())
        assert result is True

        # cas_key 应被删除
        assert cas_lookup(tmp_cas_db, cas_key) is None

    def test_gc_keeps_live(self, tmp_cas_db):
        """live 的 key 不被 GC 清理。"""
        cas_key = compute_cas_key_v1("hash1", "python", "0.1", "0.2", "v1", "v1", "v1")
        parse_result = {"symbols": [], "raw_calls": [], "imports": []}
        cas_publish(tmp_cas_db, cas_key, "hash1", "python", parse_result)

        result = cas_gc(tmp_cas_db, {cas_key})
        assert result is True
        assert cas_lookup(tmp_cas_db, cas_key) is not None


# ── manifest 测试 ──

class TestWorkspaceManifest:

    def test_upsert_and_get(self, tmp_manifest_db):
        """插入并查询 manifest。"""
        upsert_manifest(tmp_manifest_db, 1, "src/main.py", "abc123",
                        cas_key="cas_key_1", raw_hash="raw_abc",
                        file_size=100, mtime_ns=1234567890)

        result = get_manifest(tmp_manifest_db, 1, "src/main.py")
        assert result is not None
        assert result["content_hash"] == "abc123"
        assert result["cas_key"] == "cas_key_1"
        assert result["file_size"] == 100

    def test_upsert_replaces(self, tmp_manifest_db):
        """重复插入替换。"""
        upsert_manifest(tmp_manifest_db, 1, "src/main.py", "hash1")
        upsert_manifest(tmp_manifest_db, 1, "src/main.py", "hash2")

        result = get_manifest(tmp_manifest_db, 1, "src/main.py")
        assert result["content_hash"] == "hash2"

    def test_list_manifests_all(self, tmp_manifest_db):
        """列出所有 manifest。"""
        upsert_manifest(tmp_manifest_db, 1, "a.py", "hash1")
        upsert_manifest(tmp_manifest_db, 1, "b.py", "hash2", is_dirty=True)

        result = list_manifests(tmp_manifest_db, 1)
        assert len(result) == 2

    def test_list_manifests_dirty_only(self, tmp_manifest_db):
        """只列出 dirty manifest。"""
        upsert_manifest(tmp_manifest_db, 1, "a.py", "hash1", is_dirty=False)
        upsert_manifest(tmp_manifest_db, 1, "b.py", "hash2", is_dirty=True)

        result = list_manifests(tmp_manifest_db, 1, dirty_only=True)
        assert len(result) == 1
        assert result[0]["rel_path"] == "b.py"

    def test_link_to_snapshot(self, tmp_manifest_db):
        """链接文件到 snapshot。"""
        link_to_snapshot(tmp_manifest_db, "snap1", "a.py", "hash1", "cas1")
        files = get_snapshot_files(tmp_manifest_db, "snap1")
        assert len(files) == 1
        assert files[0]["rel_path"] == "a.py"

    def test_verify_raw_hash_match(self, tmp_manifest_db):
        """raw hash 匹配返回 True。"""
        upsert_manifest(tmp_manifest_db, 1, "a.py", "hash1", raw_hash="raw1")
        assert verify_raw_hash(tmp_manifest_db, 1, "a.py", "raw1") is True

    def test_verify_raw_hash_mismatch(self, tmp_manifest_db):
        """raw hash 不匹配返回 False。"""
        upsert_manifest(tmp_manifest_db, 1, "a.py", "hash1", raw_hash="raw1")
        assert verify_raw_hash(tmp_manifest_db, 1, "a.py", "raw2") is False

    def test_verify_raw_hash_not_found(self, tmp_manifest_db):
        """不存在的 manifest 返回 False。"""
        assert verify_raw_hash(tmp_manifest_db, 1, "nonexistent.py", "raw1") is False
