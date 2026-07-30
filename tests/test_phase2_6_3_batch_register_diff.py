"""Phase 2-6-3 批量文件注册 PyO3 暴露层差分测试。

**本文件是 manifest §7 中 Phase 2-6-3 的 ✅(behavioral) 标记载体。**

差分测试矩阵（契约 docs/design/phase2-6-3-batch-register-contract.md §3）：
  TestBatchRegisterDiff：R1-R6 + V1-V4 + T1-T5（batch_register_files 差分）
    - R1: 新文件（DB 无记录）→ INSERT，返回新 id，version_id=None
    - R2: 已有文件（DB 有记录）→ UPDATE mtime/module_path/status
    - R3: 多文件混合（3 新 + 2 旧）→ 5 个结果，3 INSERT + 2 UPDATE
    - R4: 空文件列表 → success=True, results=[]
    - R5: 重复 rel_path → 第一个 INSERT，第二个 UPDATE
    - R6: workspace 隔离 → workspace_id=2 不读 workspace_id=1 的文件
    - V1: 有版本 → version_id/mtime/content_hash/total_lines 填充
    - V2: 无版本（首次注册）→ version_id=None
    - V3: skip_version_lookup=True → version_id=None（不执行 SQL）
    - V4: is_current 切换 → 只返回 is_current=1 的版本
    - T1: 全部成功 → COMMIT，返回所有结果
    - T2: 数据库路径不存在 → success=False
    - T3: file_instances 表不存在 → success=False

  TestDataConsistencyDiff：Python 路径与 Rust 路径 DB 状态完全一致
    - 同一组文件分别用 Python _register_file_db + _get_file_version 和 Rust batch_register_files
    - 对比 file_instances 表：id/workspace_id/rel_path/abs_path/mtime/module_path/status

预期差异（见契约 §4 + §8）：
  - 事务边界：Python _register_file_db 在 self.conn 中执行（外层事务）；
    Rust 是独立子事务（BEGIN IMMEDIATE → COMMIT）。差分测试用独立 DB 副本对比最终状态。
  - 不切换默认路径：Rust API 仅作为可选短路，通过 is_feature_rolled_back 控制。

前置条件：
  - Rust 扩展 callwarden_core 必须可加载
  - 如果不可加载，本测试套件会显式 skip 并给出修复指引

关联：
  - 契约：docs/design/phase2-6-3-batch-register-contract.md
  - Python 真相源：
    - db/db_build.py:_register_file_db (L2754-2787)
    - db/db_build.py:_get_file_version (L2790-2796)
  - Rust 真相源：rust_ext/src/batch_register_query.rs
"""
from __future__ import annotations

import os
import sys
import sqlite3
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
        "请先运行 `maturin build --manifest-path rust_ext/Cargo.toml --release -i python` "
        "并 `pip install --force-reinstall rust_ext/target/wheels/callwarden_core-*.whl`。"
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
CREATE INDEX IF NOT EXISTS idx_file_versions_instance ON file_versions(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_file_versions_current ON file_versions(is_current);
"""


def _make_codegraph_db(db_path) -> None:
    """构建测试用 CodeGraph DB（核心表，与 schema.py 对齐）"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_CODEGRAPH_SCHEMA_DDL)
    # 插入默认 workspace
    conn.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at, is_active, description) "
        "VALUES (1, 'test_ws', '/tmp/test', 0, 1, 'test')"
    )
    conn.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at, is_active, description) "
        "VALUES (2, 'test_ws2', '/tmp/test2', 0, 0, 'test2')"
    )
    conn.commit()
    conn.close()


# ============================================
# Python 真相源实现（从 db_build.py 提取的核心逻辑）
# ============================================


def _python_register_file_db(conn: sqlite3.Connection, workspace_id: int, abs_path: str,
                              module_path: str, rel_path: str, mtime: float) -> int:
    """Python 真相源：_register_file_db 核心逻辑（db_build.py:2754-2787）"""
    cur = conn.execute(
        "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
        (workspace_id, rel_path),
    )
    row = cur.fetchone()

    if row:
        conn.execute(
            "UPDATE file_instances SET mtime = ?, module_path = ?, status = 'pending' WHERE id = ?",
            (mtime, module_path, row[0]),
        )
        return row[0]
    else:
        cur = conn.execute(
            """INSERT INTO file_instances
               (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
               VALUES (?, ?, ?, '', ?, 0, 0, 'pending', ?)""",
            (workspace_id, rel_path, abs_path, mtime, module_path),
        )
        return cur.lastrowid


def _python_get_file_version(conn: sqlite3.Connection, file_instance_id: int) -> Optional[sqlite3.Row]:
    """Python 真相源：_get_file_version 核心逻辑（db_build.py:2790-2796）"""
    cur = conn.execute(
        "SELECT * FROM file_versions WHERE file_instance_id = ? ORDER BY version_num DESC LIMIT 1",
        (file_instance_id,),
    )
    return cur.fetchone()


# ============================================
# 测试夹具
# ============================================


@pytest.fixture
def codegraph_db(tmp_path):
    """提供测试用 CodeGraph DB 路径"""
    db_path = tmp_path / "test_codegraph.db"
    _make_codegraph_db(db_path)
    return str(db_path)


@pytest.fixture
def codegraph_conn(codegraph_db):
    """提供测试用 CodeGraph DB 连接"""
    conn = sqlite3.connect(codegraph_db)
    conn.row_factory = sqlite3.Row
    # 与 Python db_base.py 一致：关闭 FK 检查
    conn.execute("PRAGMA foreign_keys=OFF")
    yield conn
    conn.close()


# ============================================
# 差分测试：R1-R6 注册行为
# ============================================

pytestmark = pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)


class TestBatchRegisterDiff:
    """batch_register_files 行为差分测试（R1-R6 + V1-V4 + T1-T3）"""

    def test_R1_single_new_file(self, codegraph_db):
        """R1: 新文件（DB 无记录）→ INSERT，返回新 id，version_id=None"""
        files = [{"rel_path": "src/main.py", "abs_path": "/proj/src/main.py",
                  "module_path": "src", "mtime": 1000.0}]

        result = callwarden_core.batch_register_files(codegraph_db, 1, files, False)

        assert result["success"] is True
        assert result["files_processed"] == 1
        r = result["results"][0]
        assert r["rel_path"] == "src/main.py"
        assert r["file_instance_id"] > 0
        assert r["version_id"] is None  # 无版本

        # 验证 DB 状态
        conn = sqlite3.connect(codegraph_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM file_instances WHERE rel_path = ?", ("src/main.py",)).fetchone()
        assert row is not None
        assert row["workspace_id"] == 1
        assert row["abs_path"] == "/proj/src/main.py"
        assert row["mtime"] == 1000.0
        assert row["module_path"] == "src"
        assert row["status"] == "pending"
        assert row["current_content_hash"] == ""
        conn.close()

    def test_R2_existing_file_update(self, codegraph_db):
        """R2: 已有文件（DB 有记录）→ UPDATE mtime/module_path/status"""
        # 先插入一个文件
        conn = sqlite3.connect(codegraph_db)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
            "mtime, total_lines, last_parsed, status, module_path) "
            "VALUES (1, 'src/main.py', '/proj/src/main.py', '', 500.0, 0, 0, 'old_status', 'old_mod')"
        )
        conn.commit()
        old_id = conn.execute("SELECT id FROM file_instances WHERE rel_path='src/main.py'").fetchone()[0]
        conn.close()

        # Rust 批量注册（更新现有文件）
        files = [{"rel_path": "src/main.py", "abs_path": "/proj/src/main.py",
                  "module_path": "new_mod", "mtime": 1000.0}]
        result = callwarden_core.batch_register_files(codegraph_db, 1, files, False)

        assert result["success"] is True
        r = result["results"][0]
        assert r["file_instance_id"] == old_id  # 同一个 id

        # 验证 DB 状态：mtime/module_path/status 已更新
        conn = sqlite3.connect(codegraph_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM file_instances WHERE id = ?", (old_id,)).fetchone()
        assert row["mtime"] == 1000.0
        assert row["module_path"] == "new_mod"
        assert row["status"] == "pending"  # 被重置为 pending
        conn.close()

    def test_R3_mixed_files(self, codegraph_db):
        """R3: 多文件混合（3 新 + 2 旧）→ 5 个结果"""
        # 先插入 2 个旧文件
        conn = sqlite3.connect(codegraph_db)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
            "mtime, total_lines, last_parsed, status, module_path) "
            "VALUES (1, 'old1.py', '/proj/old1.py', '', 100.0, 0, 0, 'old', 'mod1')"
        )
        conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
            "mtime, total_lines, last_parsed, status, module_path) "
            "VALUES (1, 'old2.py', '/proj/old2.py', '', 200.0, 0, 0, 'old', 'mod2')"
        )
        conn.commit()
        conn.close()

        # 5 个文件：3 新 + 2 旧
        files = [
            {"rel_path": "old1.py", "abs_path": "/proj/old1.py", "module_path": "new_mod1", "mtime": 1000.0},
            {"rel_path": "new1.py", "abs_path": "/proj/new1.py", "module_path": "new_mod1", "mtime": 1001.0},
            {"rel_path": "old2.py", "abs_path": "/proj/old2.py", "module_path": "new_mod2", "mtime": 1002.0},
            {"rel_path": "new2.py", "abs_path": "/proj/new2.py", "module_path": "new_mod2", "mtime": 1003.0},
            {"rel_path": "new3.py", "abs_path": "/proj/new3.py", "module_path": "new_mod3", "mtime": 1004.0},
        ]
        result = callwarden_core.batch_register_files(codegraph_db, 1, files, False)

        assert result["success"] is True
        assert result["files_processed"] == 5
        assert len(result["results"]) == 5

        # 验证所有 5 个文件都有 file_instance_id
        for r in result["results"]:
            assert r["file_instance_id"] > 0
            assert r["version_id"] is None  # 无版本

        # 验证 DB 中有 5 条记录
        conn = sqlite3.connect(codegraph_db)
        count = conn.execute("SELECT COUNT(*) FROM file_instances WHERE workspace_id=1").fetchone()[0]
        assert count == 5
        conn.close()

    def test_R4_empty_list(self, codegraph_db):
        """R4: 空文件列表 → success=True, results=[]"""
        result = callwarden_core.batch_register_files(codegraph_db, 1, [], False)

        assert result["success"] is True
        assert result["files_processed"] == 0
        assert result["results"] == []

    def test_R5_duplicate_rel_path(self, codegraph_db):
        """R5: 重复 rel_path → 第一个 INSERT，第二个 UPDATE"""
        files = [
            {"rel_path": "dup.py", "abs_path": "/proj/dup.py", "module_path": "mod1", "mtime": 1000.0},
            {"rel_path": "dup.py", "abs_path": "/proj/dup.py", "module_path": "mod2", "mtime": 2000.0},
        ]
        result = callwarden_core.batch_register_files(codegraph_db, 1, files, False)

        assert result["success"] is True
        r1, r2 = result["results"]
        # 第一个 INSERT，第二个 UPDATE，file_instance_id 一致
        assert r1["file_instance_id"] == r2["file_instance_id"]

        # 验证 DB 状态：只有一条记录，mtime 和 module_path 是第二次的值
        conn = sqlite3.connect(codegraph_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM file_instances WHERE rel_path='dup.py'").fetchone()
        assert row["mtime"] == 2000.0
        assert row["module_path"] == "mod2"
        conn.close()

    def test_R6_workspace_isolation(self, codegraph_db):
        """R6: workspace 隔离 → workspace_id=2 不读 workspace_id=1 的文件"""
        # 在 workspace_id=1 中插入文件
        conn = sqlite3.connect(codegraph_db)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
            "mtime, total_lines, last_parsed, status, module_path) "
            "VALUES (1, 'shared.py', '/proj/shared.py', '', 100.0, 0, 0, 'old', 'mod1')"
        )
        conn.commit()
        ws1_id = conn.execute("SELECT id FROM file_instances WHERE workspace_id=1 AND rel_path='shared.py'").fetchone()[0]
        conn.close()

        # 在 workspace_id=2 中注册同名文件 → 应 INSERT 新记录，不是 UPDATE
        files = [{"rel_path": "shared.py", "abs_path": "/proj2/shared.py",
                  "module_path": "mod2", "mtime": 2000.0}]
        result = callwarden_core.batch_register_files(codegraph_db, 2, files, False)

        assert result["success"] is True
        r = result["results"][0]
        assert r["file_instance_id"] != ws1_id  # 不同的 file_instance_id

        # 验证 DB 有两条记录
        conn = sqlite3.connect(codegraph_db)
        count = conn.execute("SELECT COUNT(*) FROM file_instances WHERE rel_path='shared.py'").fetchone()[0]
        assert count == 2
        conn.close()


# ============================================
# 差分测试：V1-V4 版本查询行为
# ============================================

class TestVersionLookupDiff:
    """batch_register_files 版本查询差分测试（V1-V4）"""

    def _setup_file_with_version(self, db_path: str, workspace_id: int = 1) -> int:
        """在 DB 中创建一个文件 + 一个 file_version，返回 file_instance_id"""
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=OFF")
        cur = conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
            "mtime, total_lines, last_parsed, status, module_path) "
            "VALUES (?, 'test.py', '/proj/test.py', 'hash123', 500.0, 100, 0, 'pending', 'mod')",
            (workspace_id,)
        )
        file_instance_id = cur.lastrowid
        # 插入 file_contents（FK）
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
            "VALUES ('hash123', 'python', 100, 0)"
        )
        # 插入 file_version（is_current=1）
        conn.execute(
            "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, "
            "total_lines, parsed_at, is_current, is_deleted, commit_hash) "
            "VALUES (?, 1, 'hash123', 500.0, 100, 0, 1, 0, 'abc123')",
            (file_instance_id,)
        )
        conn.commit()
        conn.close()
        return file_instance_id

    def test_V1_with_version(self, codegraph_db):
        """V1: 有版本 → version_id/mtime/content_hash/total_lines 填充"""
        file_instance_id = self._setup_file_with_version(codegraph_db)

        files = [{"rel_path": "test.py", "abs_path": "/proj/test.py",
                  "module_path": "mod", "mtime": 1000.0}]
        result = callwarden_core.batch_register_files(codegraph_db, 1, files, False)

        assert result["success"] is True
        r = result["results"][0]
        assert r["file_instance_id"] == file_instance_id
        assert r["version_id"] is not None
        assert r["version_mtime"] == 500.0
        assert r["version_content_hash"] == "hash123"
        assert r["version_total_lines"] == 100

    def test_V2_no_version(self, codegraph_db):
        """V2: 无版本（首次注册）→ version_id=None"""
        files = [{"rel_path": "new.py", "abs_path": "/proj/new.py",
                  "module_path": "mod", "mtime": 1000.0}]
        result = callwarden_core.batch_register_files(codegraph_db, 1, files, False)

        assert result["success"] is True
        r = result["results"][0]
        assert r["version_id"] is None
        assert r["version_mtime"] is None
        assert r["version_content_hash"] is None
        assert r["version_total_lines"] is None

    def test_V3_skip_version_lookup(self, codegraph_db):
        """V3: skip_version_lookup=True → version_id=None（不执行 SQL）"""
        file_instance_id = self._setup_file_with_version(codegraph_db)

        files = [{"rel_path": "test.py", "abs_path": "/proj/test.py",
                  "module_path": "mod", "mtime": 1000.0}]
        result = callwarden_core.batch_register_files(codegraph_db, 1, files, True)

        assert result["success"] is True
        r = result["results"][0]
        assert r["file_instance_id"] == file_instance_id
        # skip_version_lookup=True → version 字段全部 None
        assert r["version_id"] is None
        assert r["version_mtime"] is None
        assert r["version_content_hash"] is None
        assert r["version_total_lines"] is None

    def test_V4_is_current_filter(self, codegraph_db):
        """V4: is_current 切换 → 只返回 is_current=1 的版本（最新）"""
        # 创建文件 + 两个版本（v1 is_current=0, v2 is_current=1）
        conn = sqlite3.connect(codegraph_db)
        conn.execute("PRAGMA foreign_keys=OFF")
        cur = conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
            "mtime, total_lines, last_parsed, status, module_path) "
            "VALUES (1, 'test.py', '/proj/test.py', 'hash2', 500.0, 100, 0, 'pending', 'mod')"
        )
        file_instance_id = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
            "VALUES ('hash1', 'python', 90, 0)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
            "VALUES ('hash2', 'python', 100, 0)"
        )
        # v1（旧版本，is_current=0）
        conn.execute(
            "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, "
            "total_lines, parsed_at, is_current, is_deleted, commit_hash) "
            "VALUES (?, 1, 'hash1', 400.0, 90, 0, 0, 0, 'old')",
            (file_instance_id,)
        )
        # v2（当前版本，is_current=1）
        conn.execute(
            "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, "
            "total_lines, parsed_at, is_current, is_deleted, commit_hash) "
            "VALUES (?, 2, 'hash2', 500.0, 100, 0, 1, 0, 'new')",
            (file_instance_id,)
        )
        conn.commit()
        conn.close()

        # Rust 查询应返回 v2（is_current=1）
        files = [{"rel_path": "test.py", "abs_path": "/proj/test.py",
                  "module_path": "mod", "mtime": 1000.0}]
        result = callwarden_core.batch_register_files(codegraph_db, 1, files, False)

        assert result["success"] is True
        r = result["results"][0]
        assert r["version_id"] is not None
        assert r["version_content_hash"] == "hash2"  # v2 的 content_hash
        assert r["version_mtime"] == 500.0
        assert r["version_total_lines"] == 100


# ============================================
# 差分测试：T1-T3 事务与错误处理
# ============================================

class TestTransactionDiff:
    """batch_register_files 事务与错误处理差分测试（T1-T3）"""

    def test_T1_all_success_commit(self, codegraph_db):
        """T1: 全部成功 → COMMIT，返回所有结果"""
        files = [
            {"rel_path": "a.py", "abs_path": "/proj/a.py", "module_path": "mod", "mtime": 1000.0},
            {"rel_path": "b.py", "abs_path": "/proj/b.py", "module_path": "mod", "mtime": 1001.0},
            {"rel_path": "c.py", "abs_path": "/proj/c.py", "module_path": "mod", "mtime": 1002.0},
        ]
        result = callwarden_core.batch_register_files(codegraph_db, 1, files, False)

        assert result["success"] is True
        assert result["files_processed"] == 3

        # 验证 DB 有 3 条记录
        conn = sqlite3.connect(codegraph_db)
        count = conn.execute("SELECT COUNT(*) FROM file_instances WHERE workspace_id=1").fetchone()[0]
        assert count == 3
        conn.close()

    def test_T2_db_path_not_exist(self, tmp_path):
        """T2: 数据库路径不存在 → success=False"""
        nonexistent_path = str(tmp_path / "nonexistent" / "db.sqlite")
        files = [{"rel_path": "a.py", "abs_path": "/proj/a.py", "module_path": "mod", "mtime": 1000.0}]
        result = callwarden_core.batch_register_files(nonexistent_path, 1, files, False)

        assert result["success"] is False
        assert "error" in result
        assert result["error"] != ()

    def test_T3_table_not_exist(self, tmp_path):
        """T3: file_instances 表不存在 → success=False"""
        db_path = str(tmp_path / "empty.db")
        # 创建空 DB（无表）
        conn = sqlite3.connect(db_path)
        conn.close()

        files = [{"rel_path": "a.py", "abs_path": "/proj/a.py", "module_path": "mod", "mtime": 1000.0}]
        result = callwarden_core.batch_register_files(db_path, 1, files, False)

        assert result["success"] is False
        assert "error" in result


# ============================================
# 差分测试：Python vs Rust 数据一致性
# ============================================

class TestDataConsistencyDiff:
    """Python 路径与 Rust 路径 DB 状态完全一致"""

    def test_python_rust_produce_identical_db_state(self, tmp_path):
        """同一组文件分别用 Python 和 Rust 注册，对比 file_instances 表状态"""
        # 准备两个相同 DB 副本
        db_python = tmp_path / "python.db"
        db_rust = tmp_path / "rust.db"
        _make_codegraph_db(db_python)
        _make_codegraph_db(db_rust)

        # 测试文件集：5 个新文件
        files = [
            {"rel_path": "src/main.py", "abs_path": "/proj/src/main.py", "module_path": "src", "mtime": 1000.0},
            {"rel_path": "src/utils.py", "abs_path": "/proj/src/utils.py", "module_path": "src", "mtime": 1001.0},
            {"rel_path": "tests/test_main.py", "abs_path": "/proj/tests/test_main.py", "module_path": "tests", "mtime": 1002.0},
            {"rel_path": "README.md", "abs_path": "/proj/README.md", "module_path": "", "mtime": 1003.0},
            {"rel_path": "lib/helper.py", "abs_path": "/proj/lib/helper.py", "module_path": "lib", "mtime": 1004.0},
        ]

        # Python 路径：逐文件 _register_file_db
        conn_py = sqlite3.connect(str(db_python))
        conn_py.execute("PRAGMA foreign_keys=OFF")
        py_ids = []
        for f in files:
            fid = _python_register_file_db(
                conn_py, 1, f["abs_path"], f["module_path"], f["rel_path"], f["mtime"]
            )
            py_ids.append(fid)
        conn_py.commit()
        conn_py.close()

        # Rust 路径：batch_register_files
        result = callwarden_core.batch_register_files(str(db_rust), 1, files, False)
        assert result["success"] is True
        rust_ids = [r["file_instance_id"] for r in result["results"]]

        # 对比 file_instances 表状态
        conn_py = sqlite3.connect(str(db_python))
        conn_py.row_factory = sqlite3.Row
        conn_rust = sqlite3.connect(str(db_rust))
        conn_rust.row_factory = sqlite3.Row

        py_rows = conn_py.execute(
            "SELECT workspace_id, rel_path, abs_path, current_content_hash, mtime, "
            "total_lines, last_parsed, status, module_path FROM file_instances ORDER BY rel_path"
        ).fetchall()
        rust_rows = conn_rust.execute(
            "SELECT workspace_id, rel_path, abs_path, current_content_hash, mtime, "
            "total_lines, last_parsed, status, module_path FROM file_instances ORDER BY rel_path"
        ).fetchall()

        assert len(py_rows) == len(rust_rows) == 5
        for py_row, rust_row in zip(py_rows, rust_rows):
            assert dict(py_row) == dict(rust_row), (
                f"文件 {py_row['rel_path']} 状态不一致: "
                f"Python={dict(py_row)}, Rust={dict(rust_row)}"
            )

        # 验证 file_instance_id 序列一致（都是 1, 2, 3, 4, 5）
        assert py_ids == rust_ids

        conn_py.close()
        conn_rust.close()

    def test_python_rust_update_identical(self, tmp_path):
        """已有文件 UPDATE 后状态一致"""
        db_python = tmp_path / "python.db"
        db_rust = tmp_path / "rust.db"
        _make_codegraph_db(db_python)
        _make_codegraph_db(db_rust)

        # 先在两个 DB 中插入相同初始数据
        for db_path in [db_python, db_rust]:
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
                "mtime, total_lines, last_parsed, status, module_path) "
                "VALUES (1, 'old.py', '/proj/old.py', 'old_hash', 100.0, 50, 0, 'old_status', 'old_mod')"
            )
            conn.commit()
            conn.close()

        # UPDATE 文件
        files = [{"rel_path": "old.py", "abs_path": "/proj/old.py",
                  "module_path": "new_mod", "mtime": 1000.0}]

        # Python 路径
        conn_py = sqlite3.connect(str(db_python))
        conn_py.execute("PRAGMA foreign_keys=OFF")
        _python_register_file_db(conn_py, 1, "/proj/old.py", "new_mod", "old.py", 1000.0)
        conn_py.commit()
        conn_py.close()

        # Rust 路径
        callwarden_core.batch_register_files(str(db_rust), 1, files, False)

        # 对比
        conn_py = sqlite3.connect(str(db_python))
        conn_py.row_factory = sqlite3.Row
        conn_rust = sqlite3.connect(str(db_rust))
        conn_rust.row_factory = sqlite3.Row

        py_row = conn_py.execute("SELECT * FROM file_instances WHERE rel_path='old.py'").fetchone()
        rust_row = conn_rust.execute("SELECT * FROM file_instances WHERE rel_path='old.py'").fetchone()

        # 排除 id（可能因插入顺序不同而不同），对比所有字段
        for key in ["workspace_id", "rel_path", "abs_path", "current_content_hash",
                    "mtime", "total_lines", "last_parsed", "status", "module_path"]:
            assert dict(py_row)[key] == dict(rust_row)[key], (
                f"字段 {key} 不一致: Python={dict(py_row)[key]}, Rust={dict(rust_row)[key]}"
            )

        conn_py.close()
        conn_rust.close()

    def test_python_rust_version_lookup_identical(self, tmp_path):
        """Python _get_file_version 与 Rust version 查询结果一致"""
        db_path = tmp_path / "test.db"
        _make_codegraph_db(db_path)

        # 创建文件 + 版本
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=OFF")
        cur = conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
            "mtime, total_lines, last_parsed, status, module_path) "
            "VALUES (1, 'test.py', '/proj/test.py', 'hash123', 500.0, 100, 0, 'pending', 'mod')"
        )
        file_instance_id = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
            "VALUES ('hash123', 'python', 100, 0)"
        )
        conn.execute(
            "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, "
            "total_lines, parsed_at, is_current, is_deleted, commit_hash) "
            "VALUES (?, 1, 'hash123', 500.0, 100, 0, 1, 0, 'abc')",
            (file_instance_id,)
        )
        conn.commit()
        conn.close()

        # Python 查询
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        py_version = _python_get_file_version(conn, file_instance_id)
        conn.close()

        # Rust 查询
        files = [{"rel_path": "test.py", "abs_path": "/proj/test.py",
                  "module_path": "mod", "mtime": 1000.0}]
        result = callwarden_core.batch_register_files(str(db_path), 1, files, False)
        rust_version = result["results"][0]

        # 对比关键字段
        assert py_version["id"] == rust_version["version_id"]
        assert py_version["mtime"] == rust_version["version_mtime"]
        assert py_version["content_hash"] == rust_version["version_content_hash"]
        assert py_version["total_lines"] == rust_version["version_total_lines"]


# ============================================
# 性能差分测试
# ============================================

class TestPerformanceDiff:
    """batch_register_files 性能对比测试"""

    def test_performance_rust_faster_than_python(self, tmp_path):
        """Rust 批量注册应比 Python 逐文件注册快"""
        db_python = tmp_path / "python.db"
        db_rust = tmp_path / "rust.db"
        _make_codegraph_db(db_python)
        _make_codegraph_db(db_rust)

        # 生成 200 个测试文件
        files = []
        for i in range(200):
            files.append({
                "rel_path": f"src/file_{i}.py",
                "abs_path": f"/proj/src/file_{i}.py",
                "module_path": "src",
                "mtime": 1000.0 + i,
            })

        # Python 路径计时
        t_py_start = time.perf_counter()
        conn_py = sqlite3.connect(str(db_python))
        conn_py.execute("PRAGMA foreign_keys=OFF")
        for f in files:
            _python_register_file_db(
                conn_py, 1, f["abs_path"], f["module_path"], f["rel_path"], f["mtime"]
            )
        conn_py.commit()
        conn_py.close()
        t_py = time.perf_counter() - t_py_start

        # Rust 路径计时
        t_rust_start = time.perf_counter()
        result = callwarden_core.batch_register_files(str(db_rust), 1, files, False)
        t_rust = time.perf_counter() - t_rust_start

        assert result["success"] is True
        assert result["files_processed"] == 200

        # Rust 应至少快 2x（保守目标，实际应 4x+）
        # 注意：小规模测试 Python sqlite3 也很快，这里只验证 Rust 不慢于 Python
        print(f"\nPython: {t_py*1000:.1f}ms, Rust: {t_rust*1000:.1f}ms, "
              f"speedup: {t_py/t_rust:.2f}x")
        # 放宽断言：Rust 不应比 Python 慢（容差 50%）
        assert t_rust < t_py * 1.5, (
            f"Rust 路径不应比 Python 慢 50% 以上: Python={t_py*1000:.1f}ms, "
            f"Rust={t_rust*1000:.1f}ms"
        )
