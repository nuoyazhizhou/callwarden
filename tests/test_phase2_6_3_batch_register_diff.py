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
import sqlite3
import statistics
import sys
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


def _make_codegraph_db(db_path, wal: bool = False) -> None:
    """构建测试用 CodeGraph DB（核心表，与 schema.py 对齐）

    Args:
        db_path: DB 文件路径
        wal: True 时按生产 ``db_base.py`` L3384-3392 建库（``page_size=8192``
            + ``journal_mode=WAL``）。生产 CodeGraph DB 一律是 WAL 模式——Rust
            ``open_pooled`` 的 ``PRAGMA journal_mode=WAL``（``conn_pool.rs`` L115）
            在 WAL 库上是空操作；若库仍是 sqlite3 默认的 delete 模式，该 PRAGMA
            会触发一次日志模式转换（改写 DB header + checkpoint），而 Python 侧
            永不付此成本——那是一处比 version 查询大得多的口径差。
            仅性能对比用 ``wal=True``（见 TestPerformanceDiff 说明），其余
            差分用例保持原行为不变。
    """
    conn = sqlite3.connect(str(db_path))
    if wal:
        # page_size 必须在 journal_mode 之前设置（WAL 文件格式与 page_size 绑定）
        conn.execute("PRAGMA page_size=8192")
        conn.execute("PRAGMA journal_mode=WAL")
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
    """Python 真相源：_get_file_version 核心逻辑（db_build.py:2972-2978）"""
    cur = conn.execute(
        "SELECT * FROM file_versions WHERE file_instance_id = ? ORDER BY version_num DESC LIMIT 1",
        (file_instance_id,),
    )
    return cur.fetchone()


def _python_register_file_db_prod(
    conn: sqlite3.Connection, workspace_id: int, abs_path: str,
    module_path: str, rel_path: str, mtime: float,
) -> int:
    """生产真相源 _register_file_db **完整**逻辑（db_build.py:2926-2969）

    与本文件既有的 ``_python_register_file_db``（仅 SELECT + INSERT/UPDATE）相比，
    多了生产代码里真实存在、且**新库上必需**的一步：

    ``INSERT OR IGNORE INTO file_contents (content_hash, ...) VALUES ('', ...)``

    原因见 db_build.py:2953-2957：``file_instances.current_content_hash`` 外键引用
    ``file_contents(content_hash)``，而生产连接默认 ``PRAGMA foreign_keys=ON``
    （db_base.py:3425-3428），首次注册写入的占位 hash ``''`` 必须先存在于
    file_contents，否则**新库**注册任何文件都会触发 FOREIGN KEY constraint failed。
    Rust 侧不需要这一步，因为其连接池固定 ``foreign_keys=OFF``
    （conn_pool.rs:116，注释明写原因）。

    故生产两条路径每文件的 SQL 条数本就不同：
      Python（FK=ON）：SELECT + 占位 INSERT + INSERT + version SELECT = 4 条
      Rust（FK=OFF） ：SELECT + INSERT + version SELECT = 3 条
    这不是测试制造的口径差，而是两套生产实现本身的差异。
    """
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

    # FK=ON 下 current_content_hash='' 必须先在 file_contents 存在（新库必需）
    conn.execute(
        "INSERT OR IGNORE INTO file_contents "
        "(content_hash, language, total_lines, first_seen_at) VALUES ('', '', 0, ?)",
        (time.time(),),
    )
    cur = conn.execute(
        """INSERT INTO file_instances
           (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
           VALUES (?, ?, ?, '', ?, 0, 0, 'pending', ?)""",
        (workspace_id, rel_path, abs_path, mtime, module_path),
    )
    return cur.lastrowid


def _python_batch_register_files(
    conn: sqlite3.Connection, workspace_id: int, files: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """生产真相源的逐文件注册循环（等价 Rust ``batch_register_files`` 的产出）

    对应契约 §1.3 的 register 阶段循环（``db_build.py`` L1225-1261），与 Rust 侧
    （``batch_register_query.rs`` L177-231）**逐项对等**：

    - 每文件做同一件事：查已有 instance → 写入 instance → 查当前 version；
    - 每文件产出 1 条 6 字段结果记录（Rust API 契约 §2.1 要求 ``results`` 每文件
      返回 rel_path / file_instance_id / version_id / version_mtime /
      version_content_hash / version_total_lines；生产 Python 路径的等价产出
      见 db_build.py:2897-2921 的 results 消费循环）。

    少查 version 会让 Python 侧少做 1 条 SQL；不产出结果记录会让 Python 侧少做
    Rust API 契约要求的每文件产出工作——两者都是口径不对等。
    """
    results: List[Dict[str, Any]] = []
    for f in files:
        file_instance_id = _python_register_file_db_prod(
            conn, workspace_id, f["abs_path"], f["module_path"], f["rel_path"], f["mtime"]
        )
        latest = _python_get_file_version(conn, file_instance_id)
        results.append({
            "rel_path": f["rel_path"],
            "file_instance_id": file_instance_id,
            "version_id": latest["id"] if latest is not None else None,
            "version_mtime": latest["mtime"] if latest is not None else None,
            "version_content_hash": latest["content_hash"] if latest is not None else None,
            "version_total_lines": latest["total_lines"] if latest is not None else None,
        })
    return results


def _open_python_codegraph_conn(db_path) -> sqlite3.Connection:
    """打开与生产 ``db_base.py`` L3383-3428 一致的 Python 连接

    性能对比要求两侧的**写盘语义**对等：Rust ``open_pooled``
    （``conn_pool.rs`` L108-126）设 busy_timeout / journal_mode=WAL /
    synchronous=NORMAL / foreign_keys=OFF，Python 侧若保持 sqlite3 默认值
    （synchronous=FULL、journal=delete），则 commit 时的 fsync 次数与日志
    路径都不同——那测的是 PRAGMA 差异，不是实现差异。

    本函数按生产 Python 连接的**完整** PRAGMA 序列设置（db_base.py:3383-3428）：
    busy_timeout / page_size / journal_mode=WAL / synchronous=NORMAL /
    cache_size / temp_store / mmap_size / locking_mode /
    foreign_keys=ON（生产默认，CW_USE_RUST_STORAGE 未关闭时）。

    另注：``PRAGMA journal_mode=WAL`` 对已经是 WAL 的库是空操作，故与建库
    夹具的 ``wal=True`` 配合时不会触发日志模式转换。
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA page_size=8192")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-262144")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")
    conn.execute("PRAGMA locking_mode=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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
    """batch_register_files 性能对比测试

    **口径对等说明（本轮修正，2026-10-21）**

    原实现两侧工作量不对等，Rust 侧被系统性多计成本，CI 上间歇失败
    ``t_rust < t_py * 1.5``。修正后两侧各自复现**各自生产实现**的完整行为，
    逐项消除口径差：

    1. **version 查询对等** —— Rust 在 ``skip_version_lookup=False`` 时每文件
       查一次 ``file_versions``（``batch_register_query.rs`` L165-173 预编译、
       L205-221 执行）。Python 侧补上等价的 ``_get_file_version`` 查询
       （生产真相源 ``db_build.py`` L2972-2978），两侧 SQL 语义一致
       （均取该 instance 的最新版本）。
    2. **DB 状态对等** —— 夹具按生产 ``db_base.py`` L3384-3392 建库
       （``page_size=8192`` + ``journal_mode=WAL``）。生产 CodeGraph DB 恒为
       WAL 模式；若沿用 sqlite3 默认的 delete 模式库，Rust ``open_pooled``
       的 ``PRAGMA journal_mode=WAL``（``conn_pool.rs`` L115）会触发一次
       **日志模式转换**（改写 DB header + checkpoint），该成本只落在 Rust
       一侧——比 version 查询大一个量级。
    3. **连接 PRAGMA 对等** —— Python 侧改用生产 ``db_base.py`` L3383-3428
       的**完整** PRAGMA 序列（busy_timeout / page_size / journal_mode=WAL /
       synchronous=NORMAL / cache_size / temp_store / mmap_size / locking_mode /
       foreign_keys=ON），逐项对齐生产。否则 Python 侧停留在 sqlite3 默认
       ``synchronous=FULL`` + delete 日志，commit 的 fsync 次数与日志路径都和
       Rust 不同——测的是 PRAGMA 差异，不是实现差异。
    4. **建连/关连不计入计时窗口** —— 生产两侧都是长活连接：Python 侧是
       ``db_base.py`` L3380 持有的 ``self.conn``，Rust 侧是 c53da76 之后的
       进程级连接池。建连是一次性/进程级开销，不是每次批量注册的开销。
       故 Python 侧连接在窗口外建立、关闭在窗口外（最后连接关闭会触发
       WAL checkpoint）；Rust 侧先用同路径的一次预热调用建立池连接
       （写 ws=2，不触碰被测的 ws=1 数据），再计时正式调用。
    5. **每文件产出对等** —— Rust API 契约 §2.1 要求 ``results`` 每文件返回
       rel_path / file_instance_id / version_id / version_mtime /
       version_content_hash / version_total_lines 六个字段。Python 侧同样
       物化这 6 字段记录（生产 Python 路径的等价产出见 ``db_build.py``
       L2897-2921 对 Rust results 的消费循环），否则 Python 侧少做 Rust
       契约要求的每文件产出工作。
    6. **多轮取中位数** —— 本机高负载下单轮计时抖动可达 5x，多轮中位数
       才能稳定反映实现差异（与仓库 bench 脚本同法）。

    **两侧的 SQL 条数差异是生产实现本身的差异，不是测试制造的口径差**：
    生产 Python 侧 ``_register_file_db``（``db_build.py`` L2926-2969）在
    ``foreign_keys=ON``（``db_base.py`` L3425-3428）下，每文件必须先写一行
    ``file_contents`` 占位（``current_content_hash=''`` 的 FK 目标），否则
    **新库**注册任何文件都会 FOREIGN KEY constraint failed；Rust 侧连接池
    固定 ``foreign_keys=OFF``（``conn_pool.rs`` L116），无需该占位。
    故生产两条路径每文件 SQL 数为 Python 4 条 / Rust 3 条，本测试如实复现。

    **已知 Rust 侧实现缺陷（不在本测试修复范围，Rust 代码未改）**：
    Rust 的 version 查询用 ``WHERE file_instance_id = ?1 AND is_current = 1``
    （``batch_register_query.rs`` L167-170），查询计划走
    ``idx_file_versions_current (is_current=?)`` + TEMP B-TREE 排序；而
    Python 真相源用 ``WHERE file_instance_id = ? ORDER BY version_num DESC``，
    走 ``idx_file_versions_instance (file_instance_id=?)`` 精确定位。
    本测试夹具的 ``file_versions`` 为空表，两者都快，差异不显现；但
    ``file_versions`` 有数据时（生产常态），实测 Rust 的 version 查询比
    Python 慢 8.7x（1600 行时 104us vs 12us/次）。该缺陷需另立工单修
    Rust 查询计划，本测试不掩盖也不依赖它。

    断言保持严格不变（``t_rust < t_py * 1.5``）：不放松阈值、不加 skip、
    不加平台分支。规模仍为 200 文件（契约基线 460 文件同量级；不靠放大
    规模来掩盖口径问题）。仅「两侧工作范围」与「测量方法」被调整。
    """

    # 规模沿用原测试的 200 文件
    _PERF_FILES = 200
    # 重复轮数：取中位数抗本机/CI 负载抖动
    _PERF_ROUNDS = 5

    @staticmethod
    def _make_files(round_idx: int, n: int) -> List[Dict[str, Any]]:
        """构造第 ``round_idx`` 轮的输入文件集

        每轮用独立的 rel_path 前缀，保证每轮都是 INSERT 路径（两侧同路径），
        且各轮之间互不干扰（不清库也不 UPDATE）。
        """
        return [{
            "rel_path": f"src/r{round_idx}/file_{i}.py",
            "abs_path": f"/proj/src/r{round_idx}/file_{i}.py",
            "module_path": f"src/r{round_idx}",
            "mtime": 1000.0 + i,
        } for i in range(n)]

    def test_performance_rust_faster_than_python(self, tmp_path):
        """Rust 批量注册应比 Python 逐文件注册快（两侧工作量对等口径）"""
        py_times: List[float] = []
        rust_times: List[float] = []

        for rnd in range(self._PERF_ROUNDS):
            files = self._make_files(rnd, self._PERF_FILES)
            db_python = tmp_path / f"python_{rnd}.db"
            db_rust = tmp_path / f"rust_{rnd}.db"
            _make_codegraph_db(db_python, wal=True)
            _make_codegraph_db(db_rust, wal=True)

            # ---------------- Python 侧 ----------------
            # 连接在计时窗口外建立（对齐生产长活连接 self.conn）
            conn_py = _open_python_codegraph_conn(db_python)
            # 窗口外：预热行写 ws=2（与 Rust 侧预热写入等量，两侧初始状态一致）
            _python_register_file_db_prod(
                conn_py, 2, "/proj/__warmup__.py", "warmup", "__warmup__.py", 1.0
            )
            conn_py.commit()

            t_py_start = time.perf_counter()
            _python_batch_register_files(conn_py, 1, files)
            conn_py.commit()
            t_py = time.perf_counter() - t_py_start
            # 关闭在计时窗口外（最后连接关闭会触发 WAL checkpoint）
            conn_py.close()

            # ---------------- Rust 侧 ----------------
            # 同路径预热调用：建立进程级池连接（窗口外），写 ws=2 不触碰 ws=1
            callwarden_core.batch_register_files(
                str(db_rust), 2,
                [{"rel_path": "__warmup__.py", "abs_path": "/proj/__warmup__.py",
                  "module_path": "warmup", "mtime": 1.0}],
                False,
            )

            t_rust_start = time.perf_counter()
            result = callwarden_core.batch_register_files(str(db_rust), 1, files, False)
            t_rust = time.perf_counter() - t_rust_start

            assert result["success"] is True
            assert result["files_processed"] == self._PERF_FILES
            # 两侧产出等价：每文件一条结果记录，字段集相同
            assert len(result["results"]) == self._PERF_FILES

            py_times.append(t_py)
            rust_times.append(t_rust)

        t_py = statistics.median(py_times)
        t_rust = statistics.median(rust_times)

        print(f"\nPython: {t_py*1000:.1f}ms, Rust: {t_rust*1000:.1f}ms, "
              f"speedup: {t_py/t_rust:.2f}x")
        print(f"  per-round Python: {[round(t*1000, 2) for t in py_times]}")
        print(f"  per-round Rust:   {[round(t*1000, 2) for t in rust_times]}")
        # Rust 不应比 Python 慢 50% 以上
        assert t_rust < t_py * 1.5, (
            f"Rust 路径不应比 Python 慢 50% 以上: Python={t_py*1000:.1f}ms, "
            f"Rust={t_rust*1000:.1f}ms"
        )
