"""Phase 2 子任务 4：批量文件历史版本写入 PyO3 暴露层差分测试。

**本文件是 manifest §7 中 Phase 2-4 的 ✅(behavioral) 标记载体。**

差分测试矩阵（契约 docs/design/phase2-4-batch-save-file-versions-contract.md §3）：
  TestBatchSaveFileVersionsDiff：V1-V12（batch_save_file_versions 差分）
    - V1: 首次写入（无 latest）→ version_num=1, is_current=1
    - V2: 内容变化 → version_num 递增, 旧版本 is_current=0
    - V3: 内容未变（短路分支）→ UPDATE mtime+commit_hash, 不新增版本
    - V4: ast_cache 写入（新版本分支）
    - V5: ast_cache 写入（短路分支）
    - V6: commit_hash 为空（非 git 仓库）
    - V7: file_contents 已存在（INSERT OR IGNORE 去重）
    - V8: 多文件批量（3 个文件，混合首次/变化/短路）
    - V9: 空 file_results → 无副作用
    - V10: is_current toggle 验证（多次版本写入）
    - V11: version_num 递增（连续 3 次内容变化）
    - V12: ast_cache 字段不存在（v27 库降级）

预期差异（见契约 §4）：
  - 单事务 vs 多事务：Rust 单事务覆盖所有文件，Python 外层可能多次事务
  - ast_cache JSON 字节序列：Python json.dumps vs Rust serde_json（断言 JSON 解析后字段一致）

前置条件：
  - Rust 扩展 callwarden_core 必须可加载（为 Python 3.14 编译的 .pyd）
  - 如果当前 Python 不是 3.14，本测试套件会显式 skip 并给出修复指引

关联：
  - 契约：docs/design/phase2-4-batch-save-file-versions-contract.md
  - Python 真相源：db/db_build.py:_save_file_version (L2611-2679)
  - Rust 真相源：rust_ext/src/batch_file_versions_query.rs::batch_save_file_versions
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import pytest

# ============================================
# 前置条件：Rust 扩展可用性检查
# ============================================

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

_RUST_EXT_AVAILABLE = False
_RUST_EXT_SKIP_REASON = ""
try:
    import callwarden_core  # type: ignore
    _RUST_EXT_AVAILABLE = True
except ImportError as _e:
    _RUST_EXT_SKIP_REASON = (
        f"callwarden_core 不可加载：{_e}。"
        "本测试需要 Python 3.14 编译的 Rust 扩展。"
        "在 Windows 上若当前 Python 不是 3.14，请用 C:\\Python314\\python.exe 运行。"
    )

# ============================================
# CodeGraph DB schema（与 db/schema.py 对齐，核心表子集）
# ============================================

_CODEGRAPH_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    root_path TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    is_active INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    active_task_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS file_contents (
    content_hash TEXT PRIMARY KEY,
    language TEXT DEFAULT '',
    total_lines INTEGER DEFAULT 0,
    first_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS file_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    abs_path TEXT NOT NULL,
    current_content_hash TEXT DEFAULT '',
    mtime REAL NOT NULL,
    total_lines INTEGER DEFAULT 0,
    last_parsed REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    module_path TEXT DEFAULT '',
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (current_content_hash) REFERENCES file_contents(content_hash),
    UNIQUE(workspace_id, rel_path)
);
CREATE TABLE IF NOT EXISTS file_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    version_num INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    total_lines INTEGER DEFAULT 0,
    parsed_at REAL NOT NULL,
    is_current INTEGER DEFAULT 1,
    is_deleted INTEGER DEFAULT 0,
    commit_hash TEXT DEFAULT '',
    ast_cache BLOB DEFAULT NULL,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
    FOREIGN KEY (content_hash) REFERENCES file_contents(content_hash)
);
CREATE TABLE IF NOT EXISTS symbol_contents (
    content_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    signature TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    comment_content TEXT DEFAULT '',
    qualified_name TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
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
    depth INTEGER DEFAULT -1,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id)
);
CREATE TABLE IF NOT EXISTS file_symbol_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    module_path TEXT DEFAULT '',
    depth INTEGER DEFAULT -1,
    is_deleted INTEGER DEFAULT 0,
    FOREIGN KEY (file_version_id) REFERENCES file_versions(id),
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);
CREATE INDEX IF NOT EXISTS idx_file_versions_instance ON file_versions(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_file_versions_current ON file_versions(is_current);
"""


def _make_codegraph_db(db_path):
    """构建测试用 CodeGraph DB（核心表，与 schema.py 对齐）"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_CODEGRAPH_SCHEMA_DDL)
    conn.commit()
    conn.close()


def _make_codegraph_db_v27(db_path):
    """构建 v27 兼容 DB（无 ast_cache 字段，用于 V12 降级测试）"""
    conn = sqlite3.connect(str(db_path))
    # 去掉 ast_cache 字段（模拟 v27 库未迁移到 v28）
    ddl = _CODEGRAPH_SCHEMA_DDL.replace(
        "ast_cache BLOB DEFAULT NULL,\n    FOREIGN KEY (file_instance_id)",
        "FOREIGN KEY (file_instance_id)",
    )
    conn.executescript(ddl)
    conn.commit()
    conn.close()


def _prep_file_instance(db_path, workspace_id=1, rel_path="src/main.py",
                       abs_path="/app/src/main.py", content_hash="ch_file1",
                       language="python", total_lines=10):
    """预填 workspace + file_instances（不含 file_versions，用于测试首次写入）"""
    conn = sqlite3.connect(str(db_path))
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) "
        "VALUES (?, ?, ?, ?, 1)",
        (workspace_id, f"ws-{workspace_id}", f"/app-{workspace_id}", now),
    )
    cur = conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, total_lines, last_parsed, status, module_path) "
        "VALUES (?, ?, ?, '', ?, ?, ?, 'pending', '')",
        (workspace_id, rel_path, abs_path, now, total_lines, now),
    )
    file_instance_id = cur.lastrowid
    conn.commit()
    conn.close()
    return file_instance_id


def _prep_file_with_version(db_path, workspace_id=1, rel_path="src/main.py",
                            abs_path="/app/src/main.py", content_hash="ch_v1",
                            language="python", total_lines=10):
    """预填 workspace + file_instances + file_versions（含一个当前版本）"""
    conn = sqlite3.connect(str(db_path))
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) "
        "VALUES (?, ?, ?, ?, 1)",
        (workspace_id, f"ws-{workspace_id}", f"/app-{workspace_id}", now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (content_hash, language, total_lines, now),
    )
    cur = conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, total_lines, last_parsed, status, module_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed', '')",
        (workspace_id, rel_path, abs_path, content_hash, now, total_lines, now),
    )
    file_instance_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, "
        "total_lines, parsed_at, is_current, is_deleted, commit_hash) "
        "VALUES (?, 1, ?, ?, ?, ?, 1, 0, '')",
        (file_instance_id, content_hash, now, total_lines, now),
    )
    file_version_id = cur.lastrowid
    conn.commit()
    conn.close()
    return file_instance_id, file_version_id


# ============================================
# Python 路径与 Rust 路径封装
# ============================================

def _make_ast_cache_metadata(content_hash="ch1", file_content_hash="fch1",
                             parsed_at=1000000.0, incremental=False,
                             changed_ranges_count=0, language="python"):
    """构建 ast_cache 元数据 dict（与 Python _update_ast_cache 中的 metadata 一致）"""
    return {
        "content_hash": content_hash,
        "file_content_hash": file_content_hash,
        "parsed_at": parsed_at,
        "incremental": incremental,
        "changed_ranges_count": changed_ranges_count,
        "language": language,
    }


def _py_save_file_version(codegraph_db_path, file_instance_id, result, commit_hash=""):
    """Python 路径：调用 db_build.BuildMixin._save_file_version_python（unbound method）

    使用最小 db-like 对象模拟所需方法：
    - self.conn
    - self._get_file_version(file_instance_id)
    - self._get_head_commit_cached() -> commit_hash (默认 ""，非 git 环境)
    - self._update_ast_cache(file_version_id, result, content_hash, parsed_at)
    - self._compute_and_apply_symbol_diff(prev, curr) (no-op for test)

    commit_hash 参数用于匹配 Rust 路径的 commit_hash 输入（两端一致）。

    注意：调用 _save_file_version_python 而非 _save_file_version，
    后者在 Phase 2-4 wire-production 后会先走 Rust 短路（is_feature_rolled_back 检查），
    导致差分测试变成 Rust↔Rust 而非 Python↔Rust。
    """
    from callwarden.db.db_build import BuildMixin

    class _MinimalDb:
        """最小 db-like 对象，提供 _save_file_version_python 所需的方法"""
        def __init__(self, conn, head_commit=""):
            self.conn = conn
            self._cached_head_commit = head_commit

        def _get_file_version(self, file_instance_id):
            cur = self.conn.execute(
                "SELECT * FROM file_versions WHERE file_instance_id = ? "
                "ORDER BY version_num DESC LIMIT 1",
                (file_instance_id,),
            )
            return cur.fetchone()

        def _get_head_commit_cached(self):
            return self._cached_head_commit

        def _update_ast_cache(self, file_version_id, result, content_hash, parsed_at):
            import json as _json
            file_content_hash = ""
            abs_path = result.get("abs_path")
            if abs_path and os.path.exists(abs_path):
                try:
                    from callwarden.config import read_file_normalized
                    _, file_content_hash = read_file_normalized(abs_path)
                except Exception:
                    pass
            metadata = {
                "content_hash": content_hash,
                "file_content_hash": file_content_hash,
                "parsed_at": parsed_at,
                "incremental": result.get("incremental", False),
                "changed_ranges_count": len(result.get("changed_ranges", [])),
                "language": result.get("language", ""),
            }
            try:
                self.conn.execute(
                    "UPDATE file_versions SET ast_cache = ? WHERE id = ?",
                    (_json.dumps(metadata).encode("utf-8"), file_version_id),
                )
            except sqlite3.OperationalError:
                pass

        def _compute_and_apply_symbol_diff(self, prev_version_id, curr_version_id):
            # 测试中 no-op（差分测试不验证 symbol_diff）
            pass

    conn = sqlite3.connect(str(codegraph_db_path))
    conn.row_factory = sqlite3.Row
    db = _MinimalDb(conn, head_commit=commit_hash)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        version_id = BuildMixin._save_file_version_python(db, file_instance_id, result)
        conn.execute("COMMIT;")
        return version_id
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def _rust_save_file_versions(codegraph_db_path, file_results):
    """Rust 路径：调用 callwarden_core.batch_save_file_versions

    file_results: List[Dict] 每个含 file_instance_id, content_hash, mtime,
                  total_lines, parsed_at, language, commit_hash, ast_cache_metadata
    """
    return callwarden_core.batch_save_file_versions(
        str(codegraph_db_path),
        file_results,
    )


# ============================================
# 查询辅助函数
# ============================================

def _query_file_versions(db_path, file_instance_id):
    """查询 file_versions 表（按 version_num 排序），自动检测 ast_cache 列是否存在"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # 检测 ast_cache 列是否存在（v28+ vs v27）
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(file_versions)").fetchall()]
    has_ast_cache = "ast_cache" in cols
    select_cols = (
        "id, file_instance_id, version_num, content_hash, mtime, total_lines, "
        "parsed_at, is_current, is_deleted, commit_hash"
    )
    if has_ast_cache:
        select_cols += ", ast_cache"
    rows = conn.execute(
        f"SELECT {select_cols} FROM file_versions WHERE file_instance_id = ? ORDER BY version_num",
        (file_instance_id,),
    ).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    # 如无 ast_cache 列，补 None
    if not has_ast_cache:
        for r in result:
            r["ast_cache"] = None
    return result


def _query_file_instances(db_path, file_instance_id):
    """查询 file_instances 表"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, current_content_hash, last_parsed, total_lines, mtime "
        "FROM file_instances WHERE id = ?",
        (file_instance_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _query_file_contents(db_path, content_hash=None):
    """查询 file_contents 表"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if content_hash:
        rows = conn.execute(
            "SELECT content_hash, language, total_lines, first_seen_at "
            "FROM file_contents WHERE content_hash = ?",
            (content_hash,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT content_hash, language, total_lines, first_seen_at "
            "FROM file_contents ORDER BY content_hash"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _parse_ast_cache(ast_cache_bytes):
    """解析 ast_cache BLOB 为 dict（如 Python _read_ast_cache）"""
    if ast_cache_bytes is None:
        return None
    if isinstance(ast_cache_bytes, bytes):
        return json.loads(ast_cache_bytes.decode("utf-8"))
    return json.loads(ast_cache_bytes)


def _make_file_result(file_instance_id, content_hash, mtime, total_lines,
                     parsed_at=None, language="python", commit_hash="abc123",
                     abs_path=None, incremental=False, changed_ranges=None,
                     ast_cache_metadata=None, tmp_dir=None):
    """构建 Python 路径的 result dict + Rust 路径的 file_result dict

    返回 (py_result, rust_file_result)

    注意：
    - Python `_save_file_version` 内部调用 `os.path.getmtime(result["abs_path"])` 和 `time.time()`，
      因此 abs_path 必须指向真实文件，mtime/parsed_at 由 Python 内部计算（忽略测试传入的值）。
    - 如 tmp_dir 提供，会在其中创建临时文件。
    - parsed_at 参数保留只为向后兼容，实际始终用 time.time()（与 Python 内部 time.time() 对齐）。
    - mtime 始终从真实文件重新计算（与 Python 内部 os.path.getmtime 对齐）。
    - 如 ast_cache_metadata 提供，file_content_hash 始终从真实文件重新计算
      （与 Python _update_ast_cache 内部 read_file_normalized 对齐）。
    """
    # parsed_at 始终用 time.time()（与 Python _save_file_version 内部 time.time() 对齐）
    parsed_at = time.time()
    # 创建真实临时文件（供 Python os.path.getmtime 访问）
    if abs_path is None:
        if tmp_dir is not None:
            abs_path = os.path.join(str(tmp_dir), f"file_{file_instance_id}_{content_hash}.py")
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(f"# content for {content_hash}\n" * max(total_lines, 1))
        else:
            abs_path = f"/app/file_{file_instance_id}.py"

    # 始终从真实文件更新 mtime（与 Python _save_file_version 内部 os.path.getmtime 对齐）
    if os.path.exists(abs_path):
        mtime = os.path.getmtime(abs_path)

    # 如有 ast_cache_metadata，自动重算 file_content_hash
    # （与 Python _update_ast_cache 内部 read_file_normalized 对齐）
    if ast_cache_metadata is not None and os.path.exists(abs_path):
        try:
            from callwarden.config import read_file_normalized
            _, fch = read_file_normalized(abs_path)
            ast_cache_metadata = dict(ast_cache_metadata)
            ast_cache_metadata["file_content_hash"] = fch
        except Exception:
            pass

    py_result = {
        "file_instance_id": file_instance_id,
        "content_hash": content_hash,
        "abs_path": abs_path,
        "total_lines": total_lines,
        "incremental": incremental,
        "changed_ranges": changed_ranges or [],
        "language": language,
        "rel_path": f"src/file_{file_instance_id}.py",
    }

    rust_file_result = {
        "file_instance_id": file_instance_id,
        "content_hash": content_hash,
        "mtime": mtime,
        "total_lines": total_lines,
        "parsed_at": parsed_at,
        "language": language,
        "commit_hash": commit_hash,
    }
    if ast_cache_metadata is not None:
        rust_file_result["ast_cache_metadata"] = ast_cache_metadata

    return py_result, rust_file_result


def _assert_file_versions_equal(py_rows, rust_rows, ignore_ids=True):
    """断言两端 file_versions 表行一致（可能忽略 id 因自增不同）"""
    assert len(py_rows) == len(rust_rows), \
        f"file_versions 行数不一致: py={len(py_rows)} rust={len(rust_rows)}"
    for py_row, rust_row in zip(py_rows, rust_rows):
        for key in ["version_num", "content_hash", "total_lines",
                    "is_current", "is_deleted", "commit_hash"]:
            assert py_row[key] == rust_row[key], \
                f"file_versions.{key} 不一致: py={py_row[key]} rust={rust_row[key]}"
        # mtime/parsed_at 是浮点数，用近似比较（容忍 1 秒差异，因 Python 用 time.time()）
        assert abs(py_row["mtime"] - rust_row["mtime"]) < 1.0, \
            f"mtime 不一致: py={py_row['mtime']} rust={rust_row['mtime']}"
        assert abs(py_row["parsed_at"] - rust_row["parsed_at"]) < 1.0, \
            f"parsed_at 不一致: py={py_row['parsed_at']} rust={rust_row['parsed_at']}"
        # ast_cache：JSON 解析后字段一致（不断言字节一致）
        # 如 Rust 端未传 ast_cache_metadata（None），则跳过 ast_cache 断言
        py_ast = _parse_ast_cache(py_row["ast_cache"])
        rust_ast = _parse_ast_cache(rust_row["ast_cache"])
        if rust_ast is None:
            # Rust 没写 ast_cache（调用方未传 metadata），跳过
            continue
        if py_ast is None:
            assert False, f"ast_cache 不一致: py=None rust={rust_ast}"
        for key in ["content_hash", "file_content_hash", "incremental",
                    "changed_ranges_count", "language"]:
            assert py_ast.get(key) == rust_ast.get(key), \
                f"ast_cache.{key} 不一致: py={py_ast.get(key)} rust={rust_ast.get(key)}"
        # parsed_at 用 1 秒容忍（Python 用 time.time()，Rust 用传入值）
        assert abs(py_ast.get("parsed_at", 0) - rust_ast.get("parsed_at", 0)) < 1.0, \
            f"ast_cache.parsed_at 不一致: py={py_ast.get('parsed_at')} rust={rust_ast.get('parsed_at')}"


# ============================================
# 差分测试
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestBatchSaveFileVersionsDiff:
    """V1-V12: batch_save_file_versions Python↔Rust 行为差分"""

    def test_v1_first_write_no_latest(self, tmp_path):
        """V1: 首次写入（无 latest）→ version_num=1, is_current=1"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        # 两端各预填一个 file_instance（无 file_versions）
        py_fi = _prep_file_instance(py_db, rel_path="src/main.py")
        rust_fi = _prep_file_instance(rust_db, rel_path="src/main.py")

        content_hash = "ch_v1"
        mtime = 1000.0
        py_result, rust_fr = _make_file_result(
            py_fi, content_hash, mtime, 10,
            commit_hash="abc123", tmp_dir=tmp_path,
        )

        # Python 路径
        py_vid = _py_save_file_version(py_db, py_fi, py_result, commit_hash=rust_fr["commit_hash"])

        # Rust 路径
        rust_result = _rust_save_file_versions(rust_db, [rust_fr])

        assert rust_result["success"] is True
        assert rust_result["files_processed"] == 1
        assert rust_result["new_versions"] == 1
        assert rust_result["short_circuited"] == 0
        rust_vid = rust_result["results"][0]["file_version_id"]
        assert rust_result["results"][0]["is_new_version"] is True

        # 差分断言
        py_rows = _query_file_versions(py_db, py_fi)
        rust_rows = _query_file_versions(rust_db, rust_fi)
        _assert_file_versions_equal(py_rows, rust_rows)
        assert py_rows[0]["version_num"] == 1
        assert rust_rows[0]["version_num"] == 1
        assert py_rows[0]["is_current"] == 1
        assert rust_rows[0]["is_current"] == 1
        assert py_rows[0]["commit_hash"] == "abc123"
        assert rust_rows[0]["commit_hash"] == "abc123"

        # file_instances 更新一致
        py_fi_row = _query_file_instances(py_db, py_fi)
        rust_fi_row = _query_file_instances(rust_db, rust_fi)
        assert py_fi_row["current_content_hash"] == content_hash
        assert rust_fi_row["current_content_hash"] == content_hash
        assert py_fi_row["total_lines"] == 10
        assert rust_fi_row["total_lines"] == 10

    def test_v2_content_changed_new_version(self, tmp_path):
        """V2: 内容变化 → version_num 递增, 旧版本 is_current=0"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        # 两端各预填 file_instance + v1 版本
        py_fi, py_vid1 = _prep_file_with_version(py_db, content_hash="ch_v1")
        rust_fi, rust_vid1 = _prep_file_with_version(rust_db, content_hash="ch_v1")

        content_hash_v2 = "ch_v2"
        mtime = 2000.0
        py_result, rust_fr = _make_file_result(
            py_fi, content_hash_v2, mtime, 20,
            commit_hash="def456", tmp_dir=tmp_path,
        )

        # Python 路径
        py_vid2 = _py_save_file_version(py_db, py_fi, py_result, commit_hash=rust_fr["commit_hash"])

        # Rust 路径
        rust_result = _rust_save_file_versions(rust_db, [rust_fr])
        assert rust_result["success"] is True
        assert rust_result["new_versions"] == 1
        rust_vid2 = rust_result["results"][0]["file_version_id"]

        # 差分断言
        py_rows = _query_file_versions(py_db, py_fi)
        rust_rows = _query_file_versions(rust_db, rust_fi)
        _assert_file_versions_equal(py_rows, rust_rows)
        assert len(py_rows) == 2
        assert len(rust_rows) == 2
        # 旧版本 is_current=0
        assert py_rows[0]["is_current"] == 0
        assert rust_rows[0]["is_current"] == 0
        # 新版本 is_current=1, version_num=2
        assert py_rows[1]["is_current"] == 1
        assert rust_rows[1]["is_current"] == 1
        assert py_rows[1]["version_num"] == 2
        assert rust_rows[1]["version_num"] == 2
        assert py_rows[1]["content_hash"] == "ch_v2"
        assert rust_rows[1]["content_hash"] == "ch_v2"

    def test_v3_short_circuit_content_unchanged(self, tmp_path):
        """V3: 内容未变（短路分支）→ UPDATE mtime+commit_hash, 不新增版本"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        # 两端各预填 file_instance + v1 版本
        py_fi, py_vid1 = _prep_file_with_version(py_db, content_hash="ch_same")
        rust_fi, rust_vid1 = _prep_file_with_version(rust_db, content_hash="ch_same")

        mtime = 5000.0
        py_result, rust_fr = _make_file_result(
            py_fi, "ch_same", mtime, 10,
            commit_hash="new_commit", tmp_dir=tmp_path,
        )

        # Python 路径
        py_vid = _py_save_file_version(py_db, py_fi, py_result, commit_hash=rust_fr["commit_hash"])

        # Rust 路径
        rust_result = _rust_save_file_versions(rust_db, [rust_fr])
        assert rust_result["success"] is True
        assert rust_result["new_versions"] == 0
        assert rust_result["short_circuited"] == 1
        rust_vid = rust_result["results"][0]["file_version_id"]
        assert rust_result["results"][0]["is_new_version"] is False

        # 应返回相同的 version_id
        assert py_vid == py_vid1
        assert rust_vid == rust_vid1

        # 差分断言：只有 1 个版本（不新增）
        py_rows = _query_file_versions(py_db, py_fi)
        rust_rows = _query_file_versions(rust_db, rust_fi)
        _assert_file_versions_equal(py_rows, rust_rows)
        assert len(py_rows) == 1
        assert len(rust_rows) == 1
        # mtime 和 commit_hash 已更新
        assert py_rows[0]["commit_hash"] == "new_commit"
        assert rust_rows[0]["commit_hash"] == "new_commit"

    def test_v4_ast_cache_new_version(self, tmp_path):
        """V4: ast_cache 写入（新版本分支）"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi = _prep_file_instance(py_db)
        rust_fi = _prep_file_instance(rust_db)

        content_hash = "ch_ast_v1"
        mtime = 1000.0
        ast_meta = _make_ast_cache_metadata(
            content_hash=content_hash, file_content_hash="fch_v1",
            parsed_at=time.time(), incremental=True, changed_ranges_count=3,
            language="python",
        )
        py_result, rust_fr = _make_file_result(
            py_fi, content_hash, mtime, 10,
            commit_hash="abc123", incremental=True, changed_ranges=[1, 2, 3], tmp_dir=tmp_path,
            ast_cache_metadata=ast_meta,
        )

        # Python 路径
        py_vid = _py_save_file_version(py_db, py_fi, py_result, commit_hash=rust_fr["commit_hash"])

        # Rust 路径
        rust_result = _rust_save_file_versions(rust_db, [rust_fr])
        assert rust_result["success"] is True

        py_rows = _query_file_versions(py_db, py_fi)
        rust_rows = _query_file_versions(rust_db, rust_fi)
        _assert_file_versions_equal(py_rows, rust_rows)

        # ast_cache 非 NULL
        assert py_rows[0]["ast_cache"] is not None
        assert rust_rows[0]["ast_cache"] is not None

        # JSON 解析后字段一致
        py_ast = _parse_ast_cache(py_rows[0]["ast_cache"])
        rust_ast = _parse_ast_cache(rust_rows[0]["ast_cache"])
        assert py_ast["content_hash"] == "ch_ast_v1"
        assert rust_ast["content_hash"] == "ch_ast_v1"
        assert py_ast["incremental"] is True
        assert rust_ast["incremental"] is True
        assert py_ast["changed_ranges_count"] == 3
        assert rust_ast["changed_ranges_count"] == 3

    def test_v5_ast_cache_short_circuit(self, tmp_path):
        """V5: ast_cache 写入（短路分支）"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_vid1 = _prep_file_with_version(py_db, content_hash="ch_same")
        rust_fi, rust_vid1 = _prep_file_with_version(rust_db, content_hash="ch_same")

        mtime = 5000.0
        # _make_file_result 会自动从真实文件计算 file_content_hash
        ast_meta = _make_ast_cache_metadata(
            content_hash="ch_same", file_content_hash="placeholder",
            parsed_at=time.time(), incremental=False, changed_ranges_count=0,
            language="python",
        )
        py_result, rust_fr = _make_file_result(
            py_fi, "ch_same", mtime, 10,
            commit_hash="commit_v5", incremental=False, changed_ranges=[],
            tmp_dir=tmp_path,
            ast_cache_metadata=ast_meta,
        )

        # Python 路径
        py_vid = _py_save_file_version(py_db, py_fi, py_result, commit_hash=rust_fr["commit_hash"])

        # Rust 路径
        rust_result = _rust_save_file_versions(rust_db, [rust_fr])
        assert rust_result["success"] is True
        assert rust_result["short_circuited"] == 1

        py_rows = _query_file_versions(py_db, py_fi)
        rust_rows = _query_file_versions(rust_db, rust_fi)
        _assert_file_versions_equal(py_rows, rust_rows)

        # ast_cache 已更新（短路分支也写 ast_cache）
        assert py_rows[0]["ast_cache"] is not None
        assert rust_rows[0]["ast_cache"] is not None
        py_ast = _parse_ast_cache(py_rows[0]["ast_cache"])
        rust_ast = _parse_ast_cache(rust_rows[0]["ast_cache"])
        assert py_ast["content_hash"] == "ch_same"
        assert rust_ast["content_hash"] == "ch_same"

    def test_v6_empty_commit_hash(self, tmp_path):
        """V6: commit_hash 为空（非 git 仓库）"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi = _prep_file_instance(py_db)
        rust_fi = _prep_file_instance(rust_db)

        py_result, rust_fr = _make_file_result(
            py_fi, "ch_v6", 1000.0, 10, commit_hash="", tmp_dir=tmp_path,
        )

        # Python 路径（_MinimalDb._cached_head_commit = ""）
        py_vid = _py_save_file_version(py_db, py_fi, py_result, commit_hash=rust_fr["commit_hash"])

        # Rust 路径
        rust_result = _rust_save_file_versions(rust_db, [rust_fr])
        assert rust_result["success"] is True

        py_rows = _query_file_versions(py_db, py_fi)
        rust_rows = _query_file_versions(rust_db, rust_fi)
        _assert_file_versions_equal(py_rows, rust_rows)
        assert py_rows[0]["commit_hash"] == ""
        assert rust_rows[0]["commit_hash"] == ""

    def test_v7_file_contents_already_exists(self, tmp_path):
        """V7: file_contents 已存在（INSERT OR IGNORE 去重）"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        # 预填 file_contents
        now = time.time()
        for db in [py_db, rust_db]:
            conn = sqlite3.connect(str(db))
            conn.execute(
                "INSERT INTO file_contents (content_hash, language, total_lines, first_seen_at) "
                "VALUES (?, ?, ?, ?)",
                ("ch_exists", "python", 10, now),
            )
            conn.commit()
            conn.close()

        py_fi = _prep_file_instance(py_db)
        rust_fi = _prep_file_instance(rust_db)

        py_result, rust_fr = _make_file_result(
            py_fi, "ch_exists", 1000.0, 10, 2000.0, commit_hash="abc", tmp_dir=tmp_path,
        )

        py_vid = _py_save_file_version(py_db, py_fi, py_result, commit_hash=rust_fr["commit_hash"])
        rust_result = _rust_save_file_versions(rust_db, [rust_fr])
        assert rust_result["success"] is True

        # file_contents 应只有 1 条记录（去重）
        py_fc = _query_file_contents(py_db)
        rust_fc = _query_file_contents(rust_db)
        assert len(py_fc) == 1
        assert len(rust_fc) == 1
        assert py_fc[0]["content_hash"] == "ch_exists"
        assert rust_fc[0]["content_hash"] == "ch_exists"

    def test_v8_multi_files_batch(self, tmp_path):
        """V8: 多文件批量（3 个文件，混合首次/变化/短路）"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        # 文件 1：首次写入（无 latest）
        py_fi1 = _prep_file_instance(py_db, rel_path="src/a.py")
        rust_fi1 = _prep_file_instance(rust_db, rel_path="src/a.py")

        # 文件 2：内容变化（有 v1 版本）
        py_fi2, _ = _prep_file_with_version(py_db, rel_path="src/b.py", content_hash="ch_b_v1")
        rust_fi2, _ = _prep_file_with_version(rust_db, rel_path="src/b.py", content_hash="ch_b_v1")

        # 文件 3：短路（有同 content_hash 版本）
        py_fi3, _ = _prep_file_with_version(py_db, rel_path="src/c.py", content_hash="ch_c_same")
        rust_fi3, _ = _prep_file_with_version(rust_db, rel_path="src/c.py", content_hash="ch_c_same")

        # Python 路径（逐个调用，模拟外层循环）
        py1_r, _ = _make_file_result(py_fi1, "ch_a_v1", 1000.0, 10, 2000.0, commit_hash="c1", tmp_dir=tmp_path)
        py2_r, _ = _make_file_result(py_fi2, "ch_b_v2", 3000.0, 20, 4000.0, commit_hash="c2", tmp_dir=tmp_path)
        py3_r, _ = _make_file_result(py_fi3, "ch_c_same", 5000.0, 15, 6000.0, commit_hash="c3", tmp_dir=tmp_path)

        _py_save_file_version(py_db, py_fi1, py1_r, commit_hash="c1")
        _py_save_file_version(py_db, py_fi2, py2_r, commit_hash="c2")
        _py_save_file_version(py_db, py_fi3, py3_r, commit_hash="c3")

        # Rust 路径（单次批量调用）
        _, rust1_fr = _make_file_result(rust_fi1, "ch_a_v1", 1000.0, 10, 2000.0, commit_hash="c1", tmp_dir=tmp_path)
        _, rust2_fr = _make_file_result(rust_fi2, "ch_b_v2", 3000.0, 20, 4000.0, commit_hash="c2", tmp_dir=tmp_path)
        _, rust3_fr = _make_file_result(rust_fi3, "ch_c_same", 5000.0, 15, 6000.0, commit_hash="c3", tmp_dir=tmp_path)

        rust_result = _rust_save_file_versions(rust_db, [rust1_fr, rust2_fr, rust3_fr])
        assert rust_result["success"] is True
        assert rust_result["files_processed"] == 3
        assert rust_result["new_versions"] == 2  # 文件1+文件2
        assert rust_result["short_circuited"] == 1  # 文件3

        # 差分断言
        for fi_py, fi_rust in [(py_fi1, rust_fi1), (py_fi2, rust_fi2), (py_fi3, rust_fi3)]:
            py_rows = _query_file_versions(py_db, fi_py)
            rust_rows = _query_file_versions(rust_db, fi_rust)
            _assert_file_versions_equal(py_rows, rust_rows)

    def test_v9_empty_file_results(self, tmp_path):
        """V9: 空 file_results → 无副作用"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        # Rust 路径
        rust_result = _rust_save_file_versions(rust_db, [])
        assert rust_result["success"] is True
        assert rust_result["files_processed"] == 0
        assert rust_result["new_versions"] == 0
        assert rust_result["short_circuited"] == 0
        assert len(rust_result["results"]) == 0

        # 无副作用
        conn = sqlite3.connect(str(rust_db))
        count = conn.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0]
        conn.close()
        assert count == 0

    def test_v10_is_current_toggle(self, tmp_path):
        """V10: is_current toggle 验证（多次版本写入）"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi = _prep_file_instance(py_db)
        rust_fi = _prep_file_instance(rust_db)

        # 连续 3 次内容变化
        for i in range(1, 4):
            ch = f"ch_toggle_v{i}"
            py_r, rust_fr = _make_file_result(
                py_fi, ch, 1000.0 + i, 10 + i, 2000.0 + i, commit_hash=f"c{i}", tmp_dir=tmp_path,
            )
            _py_save_file_version(py_db, py_fi, py_r, commit_hash=rust_fr["commit_hash"])
            rust_result = _rust_save_file_versions(rust_db, [rust_fr])
            assert rust_result["success"] is True

        py_rows = _query_file_versions(py_db, py_fi)
        rust_rows = _query_file_versions(rust_db, rust_fi)
        _assert_file_versions_equal(py_rows, rust_rows)
        assert len(py_rows) == 3
        assert len(rust_rows) == 3
        # 前两个版本 is_current=0，最后版本 is_current=1
        for i in range(2):
            assert py_rows[i]["is_current"] == 0
            assert rust_rows[i]["is_current"] == 0
        assert py_rows[2]["is_current"] == 1
        assert rust_rows[2]["is_current"] == 1

    def test_v11_version_num_increment(self, tmp_path):
        """V11: version_num 递增（连续 3 次内容变化）"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi = _prep_file_instance(py_db)
        rust_fi = _prep_file_instance(rust_db)

        for i in range(1, 4):
            ch = f"ch_incr_v{i}"
            py_r, rust_fr = _make_file_result(
                py_fi, ch, 1000.0 + i, 10 + i, 2000.0 + i, commit_hash=f"c{i}", tmp_dir=tmp_path,
            )
            _py_save_file_version(py_db, py_fi, py_r, commit_hash=rust_fr["commit_hash"])
            _rust_save_file_versions(rust_db, [rust_fr])

        py_rows = _query_file_versions(py_db, py_fi)
        rust_rows = _query_file_versions(rust_db, rust_fi)
        _assert_file_versions_equal(py_rows, rust_rows)
        # version_num 序列：1, 2, 3
        assert [r["version_num"] for r in py_rows] == [1, 2, 3]
        assert [r["version_num"] for r in rust_rows] == [1, 2, 3]

    def test_v12_ast_cache_column_missing_v27(self, tmp_path):
        """V12: ast_cache 字段不存在（v27 库降级）"""
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        # 用 v27 schema（无 ast_cache 字段）
        _make_codegraph_db_v27(py_db)
        _make_codegraph_db_v27(rust_db)

        py_fi = _prep_file_instance(py_db)
        rust_fi = _prep_file_instance(rust_db)

        ast_meta = _make_ast_cache_metadata(content_hash="ch_v12")
        py_result, rust_fr = _make_file_result(
            py_fi, "ch_v12", 1000.0, 10, 2000.0, commit_hash="abc", tmp_dir=tmp_path,
            ast_cache_metadata=ast_meta,
        )

        # Python 路径（_update_ast_cache try/except 降级）
        py_vid = _py_save_file_version(py_db, py_fi, py_result, commit_hash=rust_fr["commit_hash"])

        # Rust 路径（检测 ast_cache 字段不存在时跳过 UPDATE）
        rust_result = _rust_save_file_versions(rust_db, [rust_fr])
        assert rust_result["success"] is True
        assert rust_result["new_versions"] == 1

        # 差分断言：file_versions 表行一致（ast_cache 字段不存在，两端都不写）
        py_rows = _query_file_versions(py_db, py_fi)
        rust_rows = _query_file_versions(rust_db, rust_fi)
        _assert_file_versions_equal(py_rows, rust_rows)
        assert py_rows[0]["version_num"] == 1
        assert rust_rows[0]["version_num"] == 1
        assert py_rows[0]["content_hash"] == "ch_v12"
        assert rust_rows[0]["content_hash"] == "ch_v12"
