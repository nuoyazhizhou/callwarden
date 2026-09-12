"""D3 回归：`_migrate_v2_to_v3` 空 hash 回填不得触发外键失败。

背景
----
`db/db_base.py::_migrate_v2_to_v3` 把 v2 的 `files` / `file_versions` 数据回填到
`file_instances` / `file_versions` / `symbols` 等新表。其中若干回填列用
`COALESCE(..., '')` 兜底，而目标表的 `content_hash` 上有
`FOREIGN KEY (...) REFERENCES file_contents(content_hash)` /
`symbol_contents(content_hash)`。

同一函数在填充 `file_contents` 时明确排除了空 hash：
`WHERE fv.content_hash IS NOT NULL AND fv.content_hash != ''`。
于是「无 current 版本的旧文件」回填出的 `''` 在 `file_contents` 里没有对应行；
连接默认 `PRAGMA foreign_keys=ON`（`CW_USE_RUST_STORAGE` 未显式关闭时），
迁移直接 `IntegrityError: FOREIGN KEY constraint failed`。

这与 D1（`db_build.py::_register_file_db` 新库首文件 refresh）是同类缺陷：
都源于「写空 hash 但不落占位行」。

覆盖
----
1. 旧文件无 current 版本 → `file_instances.current_content_hash=''` 可落库；
2. 旧符号无匹配 `symbol_contents` → `symbols.symbol_hash=''` 可落库；
3. 正常（有 current 版本 / 有 content）路径不受影响且数据完整。
"""

import os
import sqlite3
import sys
import tempfile

import pytest

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db_base import _migrate_v2_to_v3  # noqa: E402


# ---------------------------------------------------------------------------
# v2 fixture：只建 `_migrate_v2_to_v3` 真正读写的旧表
# ---------------------------------------------------------------------------
_V2_SCHEMA = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    abs_path TEXT NOT NULL,
    mtime REAL NOT NULL,
    status TEXT DEFAULT 'pending',
    module_path TEXT DEFAULT ''
);
CREATE TABLE file_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    version_num INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    total_lines INTEGER DEFAULT 0,
    parsed_at REAL NOT NULL,
    is_current INTEGER DEFAULT 1
);
CREATE TABLE symbol_contents (
    content_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    signature TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    comment_content TEXT DEFAULT '',
    qualified_name TEXT DEFAULT ''
);
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    visibility TEXT DEFAULT 'private',
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_col INTEGER DEFAULT 0,
    end_col INTEGER DEFAULT 0,
    signature TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    comment_status TEXT DEFAULT 'pending',
    module_path TEXT DEFAULT '',
    qualified_name TEXT DEFAULT '',
    depth INTEGER DEFAULT -1
);
CREATE TABLE file_symbol_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    module_path TEXT DEFAULT '',
    depth INTEGER DEFAULT -1
);
CREATE TABLE call_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    caller_qualified TEXT DEFAULT ''
);
CREATE TABLE calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id INTEGER NOT NULL,
    caller_name TEXT NOT NULL,
    caller_module TEXT NOT NULL,
    callee_name TEXT NOT NULL,
    callee_module TEXT DEFAULT '',
    callee_qualified TEXT DEFAULT '',
    callee_file TEXT DEFAULT '',
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0
);
CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER,
    comment_type TEXT DEFAULT 'doc',
    content TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE semgrep_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    rule_name TEXT DEFAULT '',
    message TEXT DEFAULT '',
    severity TEXT DEFAULT 'INFO',
    confidence TEXT DEFAULT 'UNKNOWN',
    language TEXT DEFAULT '',
    start_line INTEGER DEFAULT 0,
    end_line INTEGER DEFAULT 0,
    snippet TEXT DEFAULT '',
    fix TEXT DEFAULT '',
    symbol_id INTEGER DEFAULT 0,
    symbol_qualified TEXT DEFAULT '',
    scanned_at REAL DEFAULT 0
);
CREATE TABLE semgrep_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_type TEXT DEFAULT 'full',
    config TEXT DEFAULT '',
    started_at REAL NOT NULL,
    completed_at REAL DEFAULT 0,
    total_findings INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
);
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL,
    description TEXT DEFAULT ''
);
"""


def _build_v2_db(*, with_current_version: bool = True,
                 with_symbol_content: bool = True) -> sqlite3.Connection:
    """构造一个 v2 库（开启 `foreign_keys=ON`，与生产连接策略一致）。

    Args:
        with_current_version: 为 True 时 file 1 有 current 版本（正常路径）；
            False 时 file 1 无任何 `is_current=1` 的版本（触发 `''` 回填）。
        with_symbol_content: 为 True 时 `symbol_contents` 有匹配行（正常路径）；
            False 时符号的 qualified_name 在 `symbol_contents` 中无匹配
            （LEFT JOIN 落空 → `COALESCE(..., '')`）。
    """
    tmpdir = tempfile.mkdtemp(prefix="cw_v2_")
    db_path = os.path.join(tmpdir, "v2.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_V2_SCHEMA)

    now = 1_700_000_000.0
    conn.execute(
        "INSERT INTO schema_version (version, applied_at, description) VALUES (2, ?, 'v2')",
        (now,),
    )
    # 正常文件 + 其 current 版本
    conn.execute(
        "INSERT INTO files (id, path, abs_path, mtime, status, module_path) "
        "VALUES (1, 'pkg/a.py', '/repo/pkg/a.py', ?, 'parsed', 'pkg')",
        (now,),
    )
    conn.execute(
        "INSERT INTO file_versions (id, file_id, version_num, content_hash, mtime, "
        "total_lines, parsed_at, is_current) VALUES (1, 1, 1, 'hash_a', ?, 10, ?, 1)",
        (now, now),
    )
    # D3 触发点：file 2 没有任何 current 版本 → 回填 COALESCE 落 '' 
    conn.execute(
        "INSERT INTO files (id, path, abs_path, mtime, status, module_path) "
        "VALUES (2, 'pkg/orphan.py', '/repo/pkg/orphan.py', ?, 'pending', 'pkg')",
        (now,),
    )
    conn.execute(
        "INSERT INTO file_versions (id, file_id, version_num, content_hash, mtime, "
        "total_lines, parsed_at, is_current) VALUES (2, 2, 1, 'hash_b', ?, 3, ?, 0)",
        (now, now),
    )

    if with_current_version:
        pass  # file 1 已有 current 版本

    # symbol_contents：正常符号内容
    if with_symbol_content:
        conn.execute(
            "INSERT INTO symbol_contents (content_hash, name, kind, content, "
            "qualified_name) VALUES ('sc_a', 'a', 'function', 'def a(): pass', 'pkg.a')"
        )
    # 符号 1：qualified_name 有/无内容行取决于 with_symbol_content
    conn.execute(
        "INSERT INTO symbols (id, file_id, name, kind, start_line, end_line, "
        "qualified_name, depth) VALUES (1, 1, 'a', 'function', 1, 2, 'pkg.a', 0)"
    )
    # 符号 2：qualified_name 在 symbol_contents 中必然无匹配 → '' 回填
    conn.execute(
        "INSERT INTO symbols (id, file_id, name, kind, start_line, end_line, "
        "qualified_name, depth) VALUES (2, 1, 'b', 'function', 3, 4, 'pkg.missing', 0)"
    )

    conn.commit()
    return conn


def _migrated_conn(**kwargs) -> sqlite3.Connection:
    conn = _build_v2_db(**kwargs)
    _migrate_v2_to_v3(conn)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 1. 空 hash 回填（D3 本体）
# ---------------------------------------------------------------------------
class TestV2ToV3EmptyHashBackfill:
    def test_file_without_current_version_does_not_break_fk(self):
        """无 current 版本的旧文件回填 '' 时不得触发 FK 失败（D3）。"""
        conn = _migrated_conn()
        try:
            rows = dict(conn.execute(
                "SELECT rel_path, current_content_hash FROM file_instances"
            ).fetchall())
            assert rows.get("pkg/orphan.py") == "", (
                "无 current 版本的旧文件应以空 hash 落库，实际=%r" % (rows,)
            )
            # 空 hash 必须有占位行，否则 FK 无引用对象（D1 同类根因）
            assert conn.execute(
                "SELECT COUNT(*) FROM file_contents WHERE content_hash = ''"
            ).fetchone()[0] == 1
        finally:
            conn.close()

    def test_symbol_without_content_row_does_not_break_fk(self):
        """符号在 symbol_contents 中无匹配时回填 '' 不得触发 FK 失败。"""
        conn = _migrated_conn()
        try:
            hashes = [r[0] for r in conn.execute(
                "SELECT symbol_hash FROM symbols ORDER BY id"
            ).fetchall()]
            assert hashes == ["sc_a", ""], hashes
            assert conn.execute(
                "SELECT COUNT(*) FROM symbol_contents WHERE content_hash = ''"
            ).fetchone()[0] == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 2. 正常路径回归：占位行不得破坏既有数据
# ---------------------------------------------------------------------------
class TestV2ToV3HappyPath:
    def test_normal_file_and_symbol_preserved(self):
        conn = _migrated_conn()
        try:
            assert conn.execute(
                "SELECT current_content_hash FROM file_instances WHERE rel_path='pkg/a.py'"
            ).fetchone()[0] == "hash_a"
            # file_versions 迁移：file_id → file_instance_id
            assert conn.execute(
                "SELECT COUNT(*) FROM file_versions WHERE content_hash='hash_a' AND is_current=1"
            ).fetchone()[0] == 1
            # 正常符号指向真实 content_hash
            assert conn.execute(
                "SELECT symbol_hash FROM symbols WHERE name='a'"
            ).fetchone()[0] == "sc_a"
        finally:
            conn.close()

    def test_idempotent_placeholder_insert(self):
        """占位行使用 INSERT OR IGNORE，重复执行/已有空行时不得报错。"""
        conn = _build_v2_db()
        try:
            # 预置 file_contents 表 + 空 hash 行（模拟老库历史数据，
            # 与真实 callwarden.db 一致：迁移的 CREATE TABLE IF NOT EXISTS 会保留它）
            conn.execute(
                "CREATE TABLE IF NOT EXISTS file_contents ("
                "content_hash TEXT PRIMARY KEY, language TEXT DEFAULT '', "
                "total_lines INTEGER DEFAULT 0, first_seen_at REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO file_contents (content_hash, language, total_lines, "
                "first_seen_at) VALUES ('', '', 0, 0)"
            )
            conn.commit()
            _migrate_v2_to_v3(conn)
            conn.commit()
            assert conn.execute(
                "SELECT COUNT(*) FROM file_contents WHERE content_hash=''"
            ).fetchone()[0] == 1
        finally:
            conn.close()
