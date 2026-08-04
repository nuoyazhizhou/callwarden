"""T-1785831377543-8d626745: refresh-all 卡死根因回归测试。

**背景**：`rust_ext/src/incremental_build_query.rs` 的 `open_readonly()`
中 `PRAGMA wal_checkpoint(PASSIVE)` 在 Windows + WAL 模式下，register 写事务
（590+ 文件）之后无限阻塞——SQLite 内部 walIndexLock/recovery 的 sleep 循环
不受 `busy_timeout=5000` 控制。证据：py-spy 抓 `SleepEx`、Rust 插桩日志定位到
checkpoint、注释掉后 5/5 轮 refresh-all 正常完成。

**修复**：4 个 rust 文件的 `open_readonly`（cas_query / cas_merge_query /
manifest_query / incremental_build_query）移除 wal_checkpoint(PASSIVE)，
open 改为 8s 有界超时 + 全局降级标记（`READONLY_DEGRADED`），超时后本次进程
后续只读连接快速失败，Python 侧 `_load_file_result_from_db_python` 用主连接
降级查询，不挂死。

**测试目标**：
1. 模拟 register 写事务（大批量写入）后多次 `load_file_result_from_db`，
   断言不挂死（wall-clock 有界）。
2. 活跃写事务下 Rust 只读查询仍能快速返回（移除 checkpoint 后读 WAL
   不需要写锁）。
3. Rust 短路不可用时（扩展缺失/降级标记触发），Python 主连接降级路径
   `_load_file_result_from_db_python` 返回正确结果。

前置条件：
- Rust 扩展 callwarden_core 必须可加载（Windows 编译的 .pyd）。
- 扩展不可用时 Rust 侧用例显式 skip，纯 Python 降级用例仍运行。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
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
        "本测试需要 Windows 编译的 Rust 扩展（callwarden_core.pyd）。"
    )

# 卡死回归判定阈值：正常只读查询应在数百 ms 内返回，
# 取 10s 上界（远小于规则 32 hook 看门狗的 90s），避免 CI 抖动误报。
_STALL_BOUND_SECONDS = 10.0


# ============================================
# 与 db/schema.py 对齐的核心表子集（复制自 test_phase2_6_1）
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_symbols_unique
    ON symbols(file_instance_id, name, start_line);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id INTEGER NOT NULL,
    caller_name TEXT NOT NULL,
    caller_module TEXT NOT NULL,
    callee_name TEXT NOT NULL,
    callee_module TEXT DEFAULT '',
    callee_qualified TEXT DEFAULT '',
    callee_file TEXT DEFAULT '',
    callee_id INTEGER DEFAULT 0,
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0,
    FOREIGN KEY (caller_id) REFERENCES symbols(id)
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
CREATE INDEX IF NOT EXISTS idx_symbols_file_instance ON symbols(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS idx_fsv_file_version ON file_symbol_versions(file_version_id);
"""


def _make_codegraph_db(db_path, journal_mode_wal: bool = True):
    """构建测试用 CodeGraph DB（WAL 模式，模拟生产环境）"""
    conn = sqlite3.connect(str(db_path))
    if journal_mode_wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_CODEGRAPH_SCHEMA_DDL)
    conn.commit()
    conn.close()


def _call_with_stall_bound(fn, timeout: float = _STALL_BOUND_SECONDS):
    """在后台线程调用 fn，断言其在 timeout 内返回（防止回归卡死挂住 pytest）。

    若线程未在 timeout 内结束，抛出断言错误（卡死回归已复现），
    不等待后台线程（daemon=True，进程结束时终止）。
    """
    result: List[Any] = []
    error: List[BaseException] = []

    def runner():
        try:
            result.append(fn())
        except BaseException as e:  # noqa: BLE001 - 传给主线程
            error.append(e)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AssertionError(
            f"调用超过 {timeout}s 仍未返回（疑似 wal_checkpoint 卡死回归）"
        )
    if error:
        raise error[0]
    return result[0] if result else None


# ============================================
# 纯 Python 降级路径用例（不依赖 Rust 扩展，VM 内可跑）
# ============================================

class TestPythonDegradePath:
    """Rust 短路不可用时（扩展缺失 / 降级标记触发），Python 主连接降级查询正常。"""

    def _make_minimal_db(self, db_path):
        """构造带 _load_file_result_from_db_python 最小依赖的 Mock DB。"""
        from callwarden.db.db_build import BuildMixin

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        obj = object.__new__(BuildMixin)
        obj.conn = conn
        return obj, conn

    def test_python_degrade_returns_data(self, tmp_path):
        """降级路径：注册一批文件后，Python 主连接查询返回正确结果（不挂死）。"""
        db_path = tmp_path / "codegraph.db"
        _make_codegraph_db(db_path)

        # 模拟 register 写事务：大批量写入 file_instances + file_versions
        conn = sqlite3.connect(str(db_path))
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) "
            "VALUES (1, 'ws1', '/app', ?, 1)",
            (now,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, "
            "first_seen_at) VALUES ('ch_v1', 'python', 10, ?)",
            (now,),
        )
        # 590+ 文件模拟 register 写事务规模
        for i in range(600):
            conn.execute(
                "INSERT OR IGNORE INTO file_instances (workspace_id, rel_path, abs_path, "
                "current_content_hash, mtime, total_lines, last_parsed, status, module_path) "
                "VALUES (1, ?, ?, 'ch_v1', ?, 10, ?, 'parsed', '')",
                (f"src/f{i}.py", f"/app/src/f{i}.py", now, now),
            )
        conn.commit()
        # 取一个 file_instance + 创建 file_version
        cur = conn.execute(
            "SELECT id FROM file_instances WHERE rel_path = 'src/f0.py'"
        )
        fi_id = cur.fetchone()[0]
        cur = conn.execute(
            "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, "
            "total_lines, parsed_at, is_current, is_deleted, commit_hash) "
            "VALUES (?, 1, 'ch_v1', ?, 10, ?, 1, 0, '')",
            (fi_id, now, now),
        )
        fv_id = cur.lastrowid
        conn.commit()

        obj, _conn = self._make_minimal_db(db_path)
        result = obj._load_file_result_from_db_python(
            fi_id, fv_id, "src/f0.py", "/app/src/f0.py", ""
        )
        assert result is not None, "Python 降级路径应返回结果"
        assert result["_from_db"] is True
        assert result["content_hash"] == "ch_v1"
        assert result["file_instance_id"] == fi_id

    def test_python_degrade_not_found_returns_none(self, tmp_path):
        """降级路径：版本不存在返回 None（正常业务语义）。"""
        db_path = tmp_path / "codegraph.db"
        _make_codegraph_db(db_path)
        obj, conn = self._make_minimal_db(db_path)
        result = obj._load_file_result_from_db_python(
            1, 999999, "nope.py", "/app/nope.py", ""
        )
        assert result is None
        conn.close()


# ============================================
# Rust 只读查询不挂死回归用例（需 Rust 扩展）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestLoadFileResultNoStall:
    """核心回归：register 写事务后多次 load_file_result_from_db 不挂死。"""

    def _setup_db_with_data(self, db_path):
        """构造 WAL DB + 600 文件 register 数据 + 1 个带符号的版本。"""
        _make_codegraph_db(db_path)
        conn = sqlite3.connect(str(db_path))
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) "
            "VALUES (1, 'ws1', '/app', ?, 1)",
            (now,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, "
            "first_seen_at) VALUES ('ch_v1', 'python', 10, ?)",
            (now,),
        )
        for i in range(600):
            conn.execute(
                "INSERT OR IGNORE INTO file_instances (workspace_id, rel_path, abs_path, "
                "current_content_hash, mtime, total_lines, last_parsed, status, module_path) "
                "VALUES (1, ?, ?, 'ch_v1', ?, 10, ?, 'parsed', '')",
                (f"src/f{i}.py", f"/app/src/f{i}.py", now, now),
            )
        conn.commit()
        cur = conn.execute("SELECT id FROM file_instances WHERE rel_path = 'src/f0.py'")
        fi_id = cur.fetchone()[0]
        cur = conn.execute(
            "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, "
            "total_lines, parsed_at, is_current, is_deleted, commit_hash) "
            "VALUES (?, 1, 'ch_v1', ?, 10, ?, 1, 0, '')",
            (fi_id, now, now),
        )
        fv_id = cur.lastrowid
        # 写入 3 个符号
        for name, sh in [("fn_a", "sh_a"), ("fn_b", "sh_b"), ("fn_c", "sh_c")]:
            conn.execute(
                "INSERT OR IGNORE INTO symbol_contents (content_hash, name, kind, content, "
                "signature, has_comment, comment_content, qualified_name) "
                "VALUES (?, ?, 'function', '', '', 0, '', ?)",
                (sh, name, name),
            )
            conn.execute(
                "INSERT INTO file_symbol_versions (file_version_id, symbol_hash, qualified_name, "
                "start_line, end_line, module_path, depth, is_deleted) "
                "VALUES (?, ?, ?, 1, 10, '', -1, 0)",
                (fv_id, sh, name),
            )
        conn.commit()
        conn.close()
        return fi_id, fv_id

    def test_multiple_loads_after_register(self, tmp_path):
        """模拟 register 写事务提交后多次 load_file_result_from_db：不挂死且数据正确。

        5 次连续调用（对应 refresh-all 多文件场景），每次必须有界返回。
        """
        db_path = tmp_path / "codegraph.db"
        fi_id, fv_id = self._setup_db_with_data(db_path)

        for i in range(5):
            started = time.time()
            result = _call_with_stall_bound(
                lambda: callwarden_core.load_file_result_from_db(
                    str(db_path), fi_id, fv_id,
                    "src/f0.py", "/app/src/f0.py", "",
                ),
                timeout=_STALL_BOUND_SECONDS,
            )
            elapsed = time.time() - started
            assert result is not None, f"第 {i + 1} 次调用应返回结果"
            assert result["_from_db"] is True
            assert len(result["symbols"]) == 3
            assert elapsed < _STALL_BOUND_SECONDS, (
                f"第 {i + 1} 次调用耗时 {elapsed:.2f}s 超过上界 {_STALL_BOUND_SECONDS}s"
            )

    def test_load_with_active_write_transaction(self, tmp_path):
        """活跃写事务（未提交）下 Rust 只读查询快速返回，不阻塞。

        移除 wal_checkpoint 后只读连接读 WAL 不需要写锁，
        即使另一连接持有 BEGIN IMMEDIATE 也能快速返回。
        """
        db_path = tmp_path / "codegraph.db"
        fi_id, fv_id = self._setup_db_with_data(db_path)

        # 另一连接持有活跃写事务（BEGIN IMMEDIATE 未提交），模拟 daemon/register 写锁
        writer = sqlite3.connect(str(db_path))
        writer.execute("BEGIN IMMEDIATE")
        try:
            writer.execute(
                "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) "
                "VALUES (999, 'ws_hold', '/hold', ?, 0)",
                (time.time(),),
            )
        except sqlite3.OperationalError:
            pass  # 已有 active workspace 锁场景，允许失败

        try:
            started = time.time()
            result = _call_with_stall_bound(
                lambda: callwarden_core.load_file_result_from_db(
                    str(db_path), fi_id, fv_id,
                    "src/f0.py", "/app/src/f0.py", "",
                ),
                timeout=_STALL_BOUND_SECONDS,
            )
            elapsed = time.time() - started
            # 只读查询应快速返回：要么读到已提交数据（WAL 特性），要么快速失败返回 None
            assert elapsed < _STALL_BOUND_SECONDS, (
                f"活跃写事务下只读查询耗时 {elapsed:.2f}s 超过上界"
            )
            if result is not None:
                assert result["_from_db"] is True
        finally:
            try:
                writer.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            writer.close()

    def test_load_nonexistent_version_fast(self, tmp_path):
        """版本不存在返回 None 且快速（不因 checkpoint 卡死）。"""
        db_path = tmp_path / "codegraph.db"
        fi_id, _fv_id = self._setup_db_with_data(db_path)
        started = time.time()
        result = _call_with_stall_bound(
            lambda: callwarden_core.load_file_result_from_db(
                str(db_path), fi_id, 999999,
                "src/f0.py", "/app/src/f0.py", "",
            ),
            timeout=_STALL_BOUND_SECONDS,
        )
        elapsed = time.time() - started
        assert result is None
        assert elapsed < _STALL_BOUND_SECONDS


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCasAndManifestNoStall:
    """cas_query / manifest_query 只读 facade 在 register 写事务后不挂死。"""

    def _setup_cas_db(self, db_path):
        """构造 CAS 表 + 1 条 ready 记录。"""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cas_file_cache (
                cas_key TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                language TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                total_lines INTEGER DEFAULT 0,
                parser_version TEXT NOT NULL,
                callwarden_version TEXT NOT NULL,
                extraction_config_version TEXT NOT NULL,
                abi_version TEXT NOT NULL,
                input_abi_version TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'ready',
                parsed_at REAL NOT NULL
            );
            """
        )
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO cas_file_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)",
            ("k1", "ch1", "python", 100, 10, "0.1.0", "0.2.0", "v1", "v1", "v1", now),
        )
        conn.commit()
        conn.close()

    def test_cas_lookup_after_register(self, tmp_path):
        """cas_global_lookup 在 WAL DB + 写事务后快速返回。"""
        db_path = tmp_path / "cas.db"
        self._setup_cas_db(db_path)
        started = time.time()
        result = _call_with_stall_bound(
            lambda: callwarden_core.cas_global_lookup(str(db_path), "k1"),
            timeout=_STALL_BOUND_SECONDS,
        )
        elapsed = time.time() - started
        assert elapsed < _STALL_BOUND_SECONDS
        assert result is not None
        assert result["cas_key"] == "k1"
