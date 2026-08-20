"""P1-2 D7 跨仓库检测算法修复验证测试。

复审报告 feature-matrix-code-reaudit-2026-07-21.md §127-131（P1-2）：
- D7 原修复只把 target_symbol_hash 从空字符串改成真实值，但算法仍有 3 个缺陷：
  1. Dict[name] 后写入覆盖重名 symbol（不同 module 下的同名函数只保留最后一个）
  2. import 路径只取最后一段，丢失 FQN 信息（无法区分 a.b.foo 与 x.y.foo）
  3. cross_repo_deps 表无 UNIQUE 约束，重复扫描持续追加记录

P1-2 修复：
- schema v41：cross_repo_deps 加 UNIQUE 索引（五元组）
- db_base.py：新增 _migrate_v40_to_v41（含重复记录去重逻辑）
- db_cross_repo.py：重写 detect_cross_repo_deps
  - Dict[name, Tuple] → Dict[name, List[Tuple]] + FQN 反向索引
  - 三级优先级匹配：FQN 全匹配 > FQN 后缀匹配 > 短名匹配
  - confidence 分级：0.95 / 0.85 / 0.7
  - INSERT INTO → INSERT OR IGNORE（幂等）
  - 删除 break，允许多仓库匹配
"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 辅助函数：构造内存数据库（含必要表 + UNIQUE 索引模拟 schema v41）
# ============================================================

def _build_in_memory_db() -> sqlite3.Connection:
    """构造内存数据库，包含 detect_cross_repo_deps 所需的全部表 + UNIQUE 索引。

    模拟 schema v41 后的 cross_repo_deps 表结构：
    - 包含 idx_cross_repo_unique UNIQUE 索引（五元组）
    - 用于验证 INSERT OR IGNORE 的幂等性
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # workspaces 表
    conn.execute("""
        CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            path TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)

    # file_instances 表
    conn.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL
        )
    """)

    # symbols 表
    conn.execute("""
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            module_path TEXT,
            file_instance_id INTEGER
        )
    """)

    # symbol_contents 表
    conn.execute("""
        CREATE TABLE symbol_contents (
            content_hash TEXT PRIMARY KEY,
            content TEXT
        )
    """)

    # cross_repo_deps 表（含 v41 UNIQUE 索引）
    conn.execute("""
        CREATE TABLE cross_repo_deps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_workspace_id INTEGER NOT NULL,
            target_workspace_id INTEGER NOT NULL,
            dependency_type TEXT NOT NULL,
            source_symbol_hash TEXT NOT NULL,
            target_symbol_hash TEXT NOT NULL,
            evidence TEXT,
            confidence REAL,
            detected_at REAL
        )
    """)

    # v41 UNIQUE 索引
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cross_repo_unique
        ON cross_repo_deps(source_workspace_id, target_workspace_id,
                           source_symbol_hash, target_symbol_hash, dependency_type)
    """)

    return conn


def _add_workspace(conn, name, path="/path"):
    conn.execute(
        "INSERT INTO workspaces (name, path, created_at) VALUES (?, ?, ?)",
        (name, path, 0.0),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_symbol(conn, workspace_id, rel_path, symbol_hash, name, qualified_name,
                module_path, content):
    """添加一个 file_instance + symbol + symbol_contents 记录。"""
    conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path) VALUES (?, ?)",
        (workspace_id, rel_path),
    )
    file_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO symbols (symbol_hash, name, qualified_name, kind, module_path, file_instance_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (symbol_hash, name, qualified_name, "function", module_path, file_id),
    )
    conn.execute(
        "INSERT INTO symbol_contents (content_hash, content) VALUES (?, ?)",
        (symbol_hash, content),
    )
    return file_id


# ============================================================
# P1-2 测试组 1: schema v41 验证
# ============================================================

def test_p1_2_schema_version_is_41():
    """P1-2: SCHEMA_VERSION 已升级到 47（v41 引入 cross_repo 去重索引后持续演进）。"""
    from callwarden.db.schema import SCHEMA_VERSION
    assert SCHEMA_VERSION == 50, f"SCHEMA_VERSION 应为 50，实际：{SCHEMA_VERSION}"


def test_p1_2_schema_has_unique_index_definition():
    """P1-2: schema.py 包含 idx_cross_repo_unique UNIQUE 索引定义。"""
    schema = ROOT / "db" / "schema.py"
    content = schema.read_text(encoding="utf-8")
    assert "idx_cross_repo_unique" in content, (
        "schema.py 必须定义 idx_cross_repo_unique 索引"
    )
    assert "CREATE UNIQUE INDEX" in content, (
        "schema.py 必须包含 CREATE UNIQUE INDEX 语句"
    )
    # 五元组 UNIQUE
    assert "source_workspace_id" in content
    assert "target_workspace_id" in content
    assert "source_symbol_hash" in content
    assert "target_symbol_hash" in content
    assert "dependency_type" in content


def test_p1_2_migration_function_registered():
    """P1-2: _migrate_v40_to_v41 函数已在 _MIGRATIONS 中注册。"""
    db_base = ROOT / "db" / "db_base.py"
    content = db_base.read_text(encoding="utf-8")
    assert "_migrate_v40_to_v41" in content, (
        "db_base.py 必须定义 _migrate_v40_to_v41 函数"
    )
    # 注册在 _MIGRATIONS 字典中
    assert '41:' in content and "_migrate_v40_to_v41" in content, (
        "_migrate_v40_to_v41 必须注册到 _MIGRATIONS dict 的 key 41"
    )


def test_p1_2_migration_creates_unique_index_on_clean_db():
    """P1-2: 在干净 v40 库上调用 _migrate_v40_to_v41 创建 UNIQUE 索引。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # 模拟 v40 库：cross_repo_deps 表无 UNIQUE 索引
    conn.execute("""
        CREATE TABLE cross_repo_deps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_workspace_id INTEGER NOT NULL,
            target_workspace_id INTEGER NOT NULL,
            dependency_type TEXT NOT NULL,
            source_symbol_hash TEXT NOT NULL,
            target_symbol_hash TEXT NOT NULL,
            evidence TEXT,
            confidence REAL,
            detected_at REAL
        )
    """)

    # 调用迁移
    from callwarden.db.db_base import _migrate_v40_to_v41
    _migrate_v40_to_v41(conn)

    # 验证 UNIQUE 索引已创建
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_cross_repo_unique'"
    )
    row = cur.fetchone()
    assert row is not None, "迁移后必须存在 idx_cross_repo_unique 索引"

    # 验证索引是 UNIQUE
    cur = conn.execute("PRAGMA index_info(idx_cross_repo_unique)")
    columns = [r[2] for r in cur.fetchall()]
    assert "source_workspace_id" in columns
    assert "target_workspace_id" in columns
    assert "source_symbol_hash" in columns
    assert "target_symbol_hash" in columns
    assert "dependency_type" in columns


def test_p1_2_migration_is_idempotent():
    """P1-2: 迁移函数可重复调用不报错（幂等）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE cross_repo_deps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_workspace_id INTEGER NOT NULL,
            target_workspace_id INTEGER NOT NULL,
            dependency_type TEXT NOT NULL,
            source_symbol_hash TEXT NOT NULL,
            target_symbol_hash TEXT NOT NULL,
            evidence TEXT,
            confidence REAL,
            detected_at REAL
        )
    """)

    from callwarden.db.db_base import _migrate_v40_to_v41
    _migrate_v40_to_v41(conn)
    # 第二次调用不应报错
    _migrate_v40_to_v41(conn)
    # 第三次调用也不应报错
    _migrate_v40_to_v41(conn)

    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_cross_repo_unique'"
    )
    assert cur.fetchone() is not None


def test_p1_2_migration_dedups_existing_duplicates():
    """P1-2: 既有 v40 库已有重复记录时，迁移函数能去重并创建 UNIQUE 索引。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE cross_repo_deps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_workspace_id INTEGER NOT NULL,
            target_workspace_id INTEGER NOT NULL,
            dependency_type TEXT NOT NULL,
            source_symbol_hash TEXT NOT NULL,
            target_symbol_hash TEXT NOT NULL,
            evidence TEXT,
            confidence REAL,
            detected_at REAL
        )
    """)

    # 插入重复记录（五元组相同，evidence 不同）
    now = 1000.0
    for i in range(3):
        conn.execute(
            "INSERT INTO cross_repo_deps (source_workspace_id, target_workspace_id, "
            "dependency_type, source_symbol_hash, target_symbol_hash, evidence, "
            "confidence, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 2, "import", "src_h1", "tgt_h1", f"evidence_{i}", 0.7, now + i),
        )
    # 插入一条不重复记录
    conn.execute(
        "INSERT INTO cross_repo_deps (source_workspace_id, target_workspace_id, "
        "dependency_type, source_symbol_hash, target_symbol_hash, evidence, "
        "confidence, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 2, "import", "src_h2", "tgt_h2", "other", 0.9, now),
    )
    conn.commit()

    # 调用迁移
    from callwarden.db.db_base import _migrate_v40_to_v41
    _migrate_v40_to_v41(conn)

    # 验证去重后只剩 2 条记录
    cur = conn.execute("SELECT COUNT(*) FROM cross_repo_deps")
    count = cur.fetchone()[0]
    assert count == 2, f"去重后应剩 2 条记录，实际：{count}"

    # 验证保留了最大 id（最新记录）的 evidence
    cur = conn.execute(
        "SELECT evidence FROM cross_repo_deps WHERE source_symbol_hash = ? AND target_symbol_hash = ?",
        ("src_h1", "tgt_h1"),
    )
    row = cur.fetchone()
    assert row is not None
    assert row["evidence"] == "evidence_2", (
        f"应保留最新记录（evidence_2），实际：{row['evidence']}"
    )


# ============================================================
# P1-2 测试组 2: detect_cross_repo_deps 算法修复
# ============================================================

def _make_db(conn):
    """构造 FakeDB 加载 CrossRepoMixin。"""
    from callwarden.db.db_cross_repo import CrossRepoMixin

    class FakeDB(CrossRepoMixin):
        def __init__(self, c):
            self.conn = c

    return FakeDB(conn)


def test_p1_2_detect_is_idempotent_no_duplicate_rows():
    """P1-2: 重复调用 detect_cross_repo_deps 不追加重复行（INSERT OR IGNORE 幂等）。"""
    conn = _build_in_memory_db()
    src_id = _add_workspace(conn, "source", "/src")
    _add_workspace(conn, "target", "/tgt")

    # 源符号：包含 import target_module
    _add_symbol(
        conn, src_id, "source.py",
        symbol_hash="src_h1",
        name="source_func",
        qualified_name="src.source_func",
        module_path="src.py",
        content="import target_module\ndef source_func(): pass",
    )
    # 目标符号
    _add_symbol(
        conn, 2, "target.py",
        symbol_hash="tgt_h1",
        name="target_module",
        qualified_name="tgt.target_module",
        module_path="tgt.py",
        content="def target_module(): pass",
    )
    conn.commit()

    db = _make_db(conn)

    # 第一次调用
    r1 = db.detect_cross_repo_deps("source")
    assert r1["total_deps"] >= 1

    cur = conn.execute("SELECT COUNT(*) FROM cross_repo_deps")
    count_after_first = cur.fetchone()[0]
    assert count_after_first == 1, f"第一次调用后应只有 1 条，实际：{count_after_first}"

    # 第二次调用（重复扫描）
    r2 = db.detect_cross_repo_deps("source")
    cur = conn.execute("SELECT COUNT(*) FROM cross_repo_deps")
    count_after_second = cur.fetchone()[0]
    assert count_after_second == 1, (
        f"第二次调用后应仍只有 1 条（INSERT OR IGNORE 幂等），实际：{count_after_second}"
    )

    # 第三次调用
    db.detect_cross_repo_deps("source")
    cur = conn.execute("SELECT COUNT(*) FROM cross_repo_deps")
    count_after_third = cur.fetchone()[0]
    assert count_after_third == 1, (
        f"第三次调用后应仍只有 1 条，实际：{count_after_third}"
    )


def test_p1_2_fqn_full_match_confidence_0_95():
    """P1-2: FQN 全匹配 confidence=0.95（高置信度）。

    源符号 import a.b.c.foo + 目标符号 FQN=a.b.c.foo → 完全匹配。
    """
    conn = _build_in_memory_db()
    src_id = _add_workspace(conn, "source", "/src")
    _add_workspace(conn, "target", "/tgt")

    # 源符号：import a.b.c.foo
    _add_symbol(
        conn, src_id, "source.py",
        symbol_hash="src_h1",
        name="caller",
        qualified_name="src.caller",
        module_path="src.py",
        content="import a.b.c.foo\ndef caller(): pass",
    )
    # 目标符号：FQN = a.b.c.foo（与 import_path 完全一致）
    _add_symbol(
        conn, 2, "target.py",
        symbol_hash="tgt_h1",
        name="foo",
        qualified_name="a.b.c.foo",
        module_path="a/b/c.py",
        content="def foo(): pass",
    )
    conn.commit()

    db = _make_db(conn)
    result = db.detect_cross_repo_deps("source")

    assert result["total_deps"] == 1
    dep = result["detected_deps"][0]
    assert dep["confidence"] == 0.95, (
        f"FQN 全匹配 confidence 应为 0.95，实际：{dep['confidence']}"
    )
    assert dep["target_symbol"] == "a.b.c.foo"

    # 验证数据库中也是 0.95
    cur = conn.execute("SELECT confidence FROM cross_repo_deps")
    row = cur.fetchone()
    assert row["confidence"] == 0.95


def test_p1_2_fqn_suffix_match_confidence_0_85():
    """P1-2: FQN 后缀匹配 confidence=0.85。

    源符号 import x.y.foo + 目标符号 FQN=y.foo（import_path 以 y.foo 结尾）。
    """
    conn = _build_in_memory_db()
    src_id = _add_workspace(conn, "source", "/src")
    _add_workspace(conn, "target", "/tgt")

    _add_symbol(
        conn, src_id, "source.py",
        symbol_hash="src_h1",
        name="caller",
        qualified_name="src.caller",
        module_path="src.py",
        content="import x.y.foo\ndef caller(): pass",
    )
    # 目标符号 FQN = y.foo（import_path=x.y.foo 以 y.foo 结尾）
    _add_symbol(
        conn, 2, "target.py",
        symbol_hash="tgt_h1",
        name="foo",
        qualified_name="y.foo",
        module_path="y.py",
        content="def foo(): pass",
    )
    conn.commit()

    db = _make_db(conn)
    result = db.detect_cross_repo_deps("source")

    assert result["total_deps"] == 1
    dep = result["detected_deps"][0]
    assert dep["confidence"] == 0.85, (
        f"FQN 后缀匹配 confidence 应为 0.85，实际：{dep['confidence']}"
    )


def test_p1_2_short_name_match_confidence_0_7():
    """P1-2: 短名匹配 confidence=0.7（有重名风险）。

    P1-2 修复（2026-07-22）：移除 import_path.endswith(cand_qn.split(".")[-1]) 恒真条件后，
    0.7 分支现在可达。场景：import_path=foo，候选 FQN=module.foo，
    FQN 后缀匹配 foo.endswith("module.foo")=False → 取 candidates[0] → confidence=0.7。
    """
    conn = _build_in_memory_db()
    src_id = _add_workspace(conn, "source", "/src")
    _add_workspace(conn, "target", "/tgt")

    # 源 import foo（短名）
    _add_symbol(
        conn, src_id, "source.py",
        symbol_hash="src_h1",
        name="caller",
        qualified_name="src.caller",
        module_path="src.py",
        content="import foo\ndef caller(): pass",
    )
    # 目标 FQN = module.foo
    # 修复后：import_path=foo, cand_qn=module.foo
    #   import_path.endswith(cand_qn) = "foo".endswith("module.foo") = False
    #   → 不匹配 → 取 candidates[0] → confidence=0.7
    _add_symbol(
        conn, 2, "target.py",
        symbol_hash="tgt_h1",
        name="foo",
        qualified_name="module.foo",
        module_path="target.py",
        content="def foo(): pass",
    )

    db = _make_db(conn)
    result = db.detect_cross_repo_deps("source")
    deps = result["detected_deps"]

    assert len(deps) == 1, f"应检测到 1 条依赖，实际: {len(deps)}"
    dep = deps[0]
    assert dep["confidence"] == 0.7, (
        f"短名匹配（无 FQN 后缀匹配）confidence 应为 0.7，实际: {dep['confidence']}"
    )
    assert dep["target_symbol"] == "module.foo"


def test_p1_2_multi_workspace_match_no_break():
    """P1-2: 同一 import 可匹配多个目标仓库（删除 break，改为 continue）。

    源 import shared_module 同时匹配 target_a 和 target_b 两个仓库的 shared_module 符号。
    """
    conn = _build_in_memory_db()
    src_id = _add_workspace(conn, "source", "/src")
    _add_workspace(conn, "target_a", "/a")
    _add_workspace(conn, "target_b", "/b")

    # 源符号：import shared_module（短名匹配）
    _add_symbol(
        conn, src_id, "source.py",
        symbol_hash="src_h1",
        name="caller",
        qualified_name="src.caller",
        module_path="src.py",
        content="import shared_module\ndef caller(): pass",
    )
    # 目标 A: shared_module
    _add_symbol(
        conn, 2, "a.py",
        symbol_hash="a_h1",
        name="shared_module",
        qualified_name="a.shared_module",
        module_path="a.py",
        content="def shared_module(): pass",
    )
    # 目标 B: shared_module
    _add_symbol(
        conn, 3, "b.py",
        symbol_hash="b_h1",
        name="shared_module",
        qualified_name="b.shared_module",
        module_path="b.py",
        content="def shared_module(): pass",
    )
    conn.commit()

    db = _make_db(conn)
    result = db.detect_cross_repo_deps("source")

    # 应该匹配 2 个目标仓库
    assert result["total_deps"] == 2, (
        f"应匹配 2 个目标仓库（target_a + target_b），实际：{result['total_deps']}"
    )

    target_workspaces = {d["target_workspace"] for d in result["detected_deps"]}
    assert target_workspaces == {"target_a", "target_b"}, (
        f"应同时匹配 target_a 和 target_b，实际：{target_workspaces}"
    )

    # 验证数据库中有 2 条记录
    cur = conn.execute("SELECT COUNT(*) FROM cross_repo_deps")
    assert cur.fetchone()[0] == 2


def test_p1_2_duplicate_symbol_names_preserved():
    """P1-2: 目标仓库中重名 symbol（不同 FQN）全部保留为候选。

    原代码 Dict[name, (qn, hash)] 只保留最后一个，会覆盖前面的。
    P1-2 改为 Dict[name, List[(qn, hash)]] 保留所有候选。
    """
    conn = _build_in_memory_db()
    src_id = _add_workspace(conn, "source", "/src")
    _add_workspace(conn, "target", "/tgt")

    # 源 import x.mod1.foo（import_path 不与目标 FQN 全匹配，但以 mod1.foo 结尾）
    _add_symbol(
        conn, src_id, "source.py",
        symbol_hash="src_h1",
        name="caller",
        qualified_name="src.caller",
        module_path="src.py",
        content="import x.mod1.foo\ndef caller(): pass",
    )
    # 目标 1: mod1.foo
    _add_symbol(
        conn, 2, "mod1.py",
        symbol_hash="tgt_h1",
        name="foo",
        qualified_name="mod1.foo",
        module_path="mod1.py",
        content="def foo(): pass",
    )
    # 目标 2: mod2.foo（重名 foo，但 FQN 不同）
    _add_symbol(
        conn, 2, "mod2.py",
        symbol_hash="tgt_h2",
        name="foo",
        qualified_name="mod2.foo",
        module_path="mod2.py",
        content="def foo(): pass",
    )
    conn.commit()

    db = _make_db(conn)
    result = db.detect_cross_repo_deps("source")

    # 应匹配到 mod1.foo（FQN 后缀匹配，因为 import_path=x.mod1.foo 以 mod1.foo 结尾）
    assert result["total_deps"] == 1, (
        f"应只匹配 1 个（FQN 后缀匹配 mod1.foo），实际：{result['total_deps']}"
    )
    dep = result["detected_deps"][0]
    assert dep["target_symbol"] == "mod1.foo", (
        f"应匹配到 mod1.foo（FQN 后缀匹配优先于 mod2.foo），实际：{dep['target_symbol']}"
    )
    assert dep["confidence"] == 0.85, (
        f"后缀匹配 confidence 应为 0.85，实际：{dep['confidence']}"
    )

    # 验证数据库中只有 1 条（不重复，不覆盖）
    cur = conn.execute(
        "SELECT target_symbol_hash FROM cross_repo_deps"
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["target_symbol_hash"] == "tgt_h1", (
        f"应写入 mod1.foo 的 symbol_hash（tgt_h1），实际：{rows[0]['target_symbol_hash']}"
    )


def test_p1_2_rust_use_syntax_fqn_match():
    """P1-2: Rust use 语法 FQN 全匹配（:: → . 转换）。

    源符号 use crate::module::foo + 目标 FQN=crate.module.foo（:: 换成 . 后全匹配）。
    """
    conn = _build_in_memory_db()
    src_id = _add_workspace(conn, "source", "/src")
    _add_workspace(conn, "target", "/tgt")

    # 源 use 语句（module_path 含 .rs 触发 rust 语言检测）
    _add_symbol(
        conn, src_id, "source.rs",
        symbol_hash="src_h1",
        name="caller",
        qualified_name="src.caller",
        module_path="source.rs",
        content="use crate::module::foo;\nfn caller() {}",
    )
    # 目标 FQN 用 :: 形式（Rust 风格）
    _add_symbol(
        conn, 2, "target.rs",
        symbol_hash="tgt_h1",
        name="foo",
        qualified_name="crate::module::foo",
        module_path="target.rs",
        content="pub fn foo() {}",
    )
    conn.commit()

    db = _make_db(conn)
    result = db.detect_cross_repo_deps("source")

    # 应匹配到（:: → . 转换后 FQN 全匹配）
    assert result["total_deps"] == 1, (
        f"应匹配 1 个（Rust :: 转 . 后 FQN 全匹配），实际：{result['total_deps']}"
    )
    dep = result["detected_deps"][0]
    assert dep["confidence"] == 0.95, (
        f"FQN 全匹配 confidence 应为 0.95，实际：{dep['confidence']}"
    )


def test_p1_2_records_only_one_per_pair_within_single_scan():
    """P1-2: 同一轮扫描内，同一对 (source_hash, target_hash) 多次 import 只记录一次。

    源符号 content 中有两次 import target_module，但同一对符号只应记录 1 条。
    """
    conn = _build_in_memory_db()
    src_id = _add_workspace(conn, "source", "/src")
    _add_workspace(conn, "target", "/tgt")

    # 源符号 content 中包含两次 import target_module
    _add_symbol(
        conn, src_id, "source.py",
        symbol_hash="src_h1",
        name="caller",
        qualified_name="src.caller",
        module_path="src.py",
        content=(
            "import target_module\n"
            "def func_a(): target_module.x()\n"
            "import target_module\n"  # 重复 import
            "def func_b(): target_module.y()\n"
        ),
    )
    _add_symbol(
        conn, 2, "target.py",
        symbol_hash="tgt_h1",
        name="target_module",
        qualified_name="tgt.target_module",
        module_path="tgt.py",
        content="def target_module(): pass",
    )
    conn.commit()

    db = _make_db(conn)
    result = db.detect_cross_repo_deps("source")

    # 应只记录 1 条（同一对符号）
    assert result["total_deps"] == 1, (
        f"同一对符号重复 import 只应记录 1 条，实际：{result['total_deps']}"
    )
    cur = conn.execute("SELECT COUNT(*) FROM cross_repo_deps")
    assert cur.fetchone()[0] == 1


# ============================================================
# P1-2 测试组 3: 端到端集成（与 D7 原测试衔接）
# ============================================================

def test_p1_2_end_to_end_with_d7_existing_test():
    """P1-2 端到端：与 D7 原测试相同场景，但验证 P1-2 新增的幂等性和 confidence。"""
    conn = _build_in_memory_db()
    src_id = _add_workspace(conn, "source", "/src")
    _add_workspace(conn, "target", "/tgt")

    # 源符号 import target_module
    _add_symbol(
        conn, src_id, "source.py",
        symbol_hash="src_h_001",
        name="source_func",
        qualified_name="src.source_func",
        module_path="src.py",
        content="import target_module\ndef source_func(): pass",
    )
    # 目标符号
    _add_symbol(
        conn, 2, "target.py",
        symbol_hash="tgt_h_001",
        name="target_module",
        qualified_name="tgt.target_module",
        module_path="tgt.py",
        content="def target_module(): pass",
    )
    conn.commit()

    db = _make_db(conn)

    # 第一次扫描
    r1 = db.detect_cross_repo_deps("source")
    assert r1["total_deps"] == 1

    # 第二次扫描（验证幂等）
    r2 = db.detect_cross_repo_deps("source")
    assert r2["total_deps"] == 1  # 内存中的 detected_deps 仍是 1，但数据库不重复
    cur = conn.execute("SELECT COUNT(*) FROM cross_repo_deps")
    assert cur.fetchone()[0] == 1, "数据库中应只有 1 条记录（INSERT OR IGNORE 幂等）"

    # 验证 target_symbol_hash 是真实值（D7 修复）
    cur = conn.execute("SELECT target_symbol_hash FROM cross_repo_deps")
    row = cur.fetchone()
    assert row["target_symbol_hash"] == "tgt_h_001", (
        f"target_symbol_hash 应为 tgt_h_001（D7 修复），实际：{row['target_symbol_hash']}"
    )


# ============================================
# P1-2 补充测试（2026-07-22）：方向修复 + 跨 workspace 去重
# ============================================


def test_p1_2_cross_repo_impact_direction_correct():
    """P1-2 修复：cross_repo_impact 方向正确。

    旧实现查 WHERE source_symbol_hash = changed_symbol 找 target_workspace_id，
    即"我作为调用方依赖了哪些仓库"——但改变调用方不会反向影响被调用方。
    修复后查 WHERE target_symbol_hash = changed_symbol 找 source_workspace_id，
    即"哪些仓库依赖了我"——改变被依赖方才会影响依赖方。
    """
    conn = _build_in_memory_db()
    src_id = _add_workspace(conn, "source_repo", "/src")
    tgt_id = _add_workspace(conn, "target_repo", "/tgt")

    # target_repo 有 lib_fn，source_repo 依赖它
    _add_symbol(
        conn, tgt_id, "lib.py",
        symbol_hash="lib_hash",
        name="lib_fn",
        qualified_name="lib.lib_fn",
        module_path="lib.py",
        content="def lib_fn(): pass",
    )
    _add_symbol(
        conn, src_id, "caller.py",
        symbol_hash="caller_hash",
        name="caller",
        qualified_name="src.caller",
        module_path="caller.py",
        # 用 import lib_fn（短名匹配）让 detect_cross_repo_deps 能记录依赖：
        # 算法匹配 import 模块名与目标符号名，from lib import lib_fn 会捕获
        # 模块 "lib"（与目标符号 "lib_fn" 不匹配），改用 import lib_fn。
        content="import lib_fn\ndef caller(): lib_fn()",
    )

    db = _make_db(conn)
    # 先 detect 建立依赖关系
    db.detect_cross_repo_deps("source_repo")

    # 改变 target_repo 的 lib_fn → 应该影响 source_repo（依赖了 lib_fn 的仓库）
    impact = db.cross_repo_impact("lib_hash", depth=2)

    # source_repo 应在受影响列表中（因为它依赖了 lib_fn）
    impacted_ws_names = [r["workspace"] for r in impact["impacted_repos"]]
    assert "source_repo" in impacted_ws_names, (
        f"改变 lib_fn 应影响 source_repo（它依赖了 lib_fn），"
        f"实际受影响: {impacted_ws_names}"
    )
    # target_repo 不应在受影响列表中（改变 lib_fn 不会影响 lib_fn 所在的仓库自身）
    assert "target_repo" not in impacted_ws_names, (
        f"改变 lib_fn 不应影响 target_repo（它不是依赖方），"
        f"实际受影响: {impacted_ws_names}"
    )


def test_p1_2_cross_workspace_dedup_not_over_dedup():
    """P1-2 修复：同轮去重 key 加入 target_workspace_id。

    旧实现 pair_key = (source_hash, target_hash)，同一 CAS symbol 出现在
    多个目标仓库时后续仓库会被误去重。修复后 pair_key = (source_hash, target_hash, t_id)。
    """
    conn = _build_in_memory_db()
    src_id = _add_workspace(conn, "source", "/src")
    _add_workspace(conn, "target_a", "/a")
    _add_workspace(conn, "target_b", "/b")

    # 两个目标仓库都有 shared_fn，但内容不同（不同 content_hash 避免 UNIQUE 冲突）
    # 短名相同（shared_fn），FQN 不同（target_a.shared_fn / target_b.shared_fn）
    _add_symbol(
        conn, 2, "target_a.py",
        symbol_hash="hash_a_001",
        name="shared_fn",
        qualified_name="target_a.shared_fn",
        module_path="target_a.py",
        content="def shared_fn(): return 'a'",
    )
    _add_symbol(
        conn, 3, "target_b.py",
        symbol_hash="hash_b_001",
        name="shared_fn",
        qualified_name="target_b.shared_fn",
        module_path="target_b.py",
        content="def shared_fn(): return 'b'",
    )

    # source 依赖 shared_fn
    _add_symbol(
        conn, src_id, "source.py",
        symbol_hash="src_h1",
        name="caller",
        qualified_name="src.caller",
        module_path="source.py",
        content="import shared_fn\ndef caller(): shared_fn()",
    )

    db = _make_db(conn)
    result = db.detect_cross_repo_deps("source")
    deps = result["detected_deps"]

    # 应该检测到 2 条依赖（source→target_a, source→target_b），
    # 而不是因去重 key 缺 t_id 只记录 1 条
    assert len(deps) == 2, (
        f"同一短名在 2 个目标仓库应检测到 2 条依赖，实际: {len(deps)}"
    )
    target_workspaces = {d["target_workspace"] for d in deps}
    assert target_workspaces == {"target_a", "target_b"}, (
        f"应覆盖两个目标仓库，实际: {target_workspaces}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
