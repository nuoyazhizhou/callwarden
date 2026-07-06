"""重复代码检测（clone detection）测试。

覆盖 B1 任务（T-1783349079761-a9a0）：验证 Type-1/2/3 克隆检测、
持久化、查询和清空。

测试内容：
- SCHEMA_VERSION == 27 且 clone_pairs 表存在
- Type-1 检测：完全相同的符号内容
- Type-2 检测：重命名克隆（token 序列相同）
- Type-3 检测：微调克隆（Jaccard 相似度）
- list_clones 查询
- get_clone_stats 统计
- clear_clones 清空
- detect_clones 幂等性（重复执行不产生重复行）
"""

import os
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB
from callwarden.db.schema import SCHEMA_VERSION


def _db_with_workspace():
    """构造临时工作区数据库。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _seed_symbol(
    db,
    rel_path,
    symbol_name,
    content,
    start_line=1,
    end_line=10,
    symbol_hash=None,
):
    """辅助：创建一个文件实例 + 符号 + 符号内容。

    Args:
        db: CodeGraphDB 实例
        rel_path: 文件相对路径
        symbol_name: 符号名
        content: 符号源代码内容
        start_line: 起始行
        end_line: 结束行
        symbol_hash: 自定义 content_hash；为 None 时用 f"hash_{symbol_name}_{rel_path}"

    Returns:
        symbol id（int）
    """
    ws_id = db._get_active_workspace_id()
    ch = symbol_hash or f"hash_{symbol_name}_{rel_path}"

    # 插入 file_contents
    db.conn.execute(
        "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
        "VALUES (?, 'python', ?, 0)",
        (ch, end_line - start_line + 1),
    )
    # 插入 file_instances
    db.conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, status, module_path) VALUES (?, ?, ?, ?, 0, 'parsed', '')",
        (ws_id, rel_path, os.path.join(db.workspace_root, rel_path), ch),
    )
    fi_id = db.conn.execute(
        "SELECT id FROM file_instances WHERE rel_path=?", (rel_path,)
    ).fetchone()[0]

    # 插入 symbol_contents（关联 content_hash 与源代码内容）
    db.conn.execute(
        "INSERT OR REPLACE INTO symbol_contents (content_hash, name, kind, content, signature, "
        "has_comment, comment_content, qualified_name) "
        "VALUES (?, ?, 'fn', ?, '', 0, '', ?)",
        (ch, symbol_name, content, symbol_name),
    )

    # 插入 symbols（UPSERT）
    db.conn.execute(
        "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, start_line, end_line, "
        "qualified_name, comment_status) VALUES (?, ?, ?, 'fn', ?, ?, ?, 'pending') "
        "ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET symbol_hash = excluded.symbol_hash",
        (fi_id, ch, symbol_name, start_line, end_line, symbol_name),
    )
    sym_id = db.conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id=? AND name=? AND start_line=?",
        (fi_id, symbol_name, start_line),
    ).fetchone()[0]

    db.conn.commit()
    return sym_id


def test_schema_version_is_27():
    """SCHEMA_VERSION 不低于 27（clone_pairs 引入版本）。"""
    assert SCHEMA_VERSION >= 27


def test_clone_pairs_table_exists_on_fresh_db():
    """全新数据库直接包含 clone_pairs 表。"""
    db, _root = _db_with_workspace()
    try:
        cur = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='clone_pairs'"
        )
        assert cur.fetchone() is not None, "clone_pairs 表不存在"
    finally:
        db.close()


def test_clone_pairs_indexes_exist():
    """clone_pairs 表有 5 个索引（含 UNIQUE）。"""
    db, _root = _db_with_workspace()
    try:
        cur = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_clone_pairs_%'"
        )
        names = {row[0] for row in cur.fetchall()}
        assert "idx_clone_pairs_workspace" in names
        assert "idx_clone_pairs_symbol_a" in names
        assert "idx_clone_pairs_symbol_b" in names
        assert "idx_clone_pairs_type" in names
        assert "idx_clone_pairs_unique" in names  # UNIQUE
    finally:
        db.close()


def test_detect_clones_type1_identical_content():
    """Type-1 检测：两个符号内容完全相同应识别为 Type-1 克隆。"""
    db, _root = _db_with_workspace()
    try:
        # 使用相同的 content_hash，模拟两个文件引用同一份 symbol_contents
        shared_hash = "shared_hash_t1"
        content = "def foo():\n    return 42\n"
        _seed_symbol(db, "a.py", "foo", content, symbol_hash=shared_hash)
        _seed_symbol(db, "b.py", "foo", content, symbol_hash=shared_hash)

        result = db.detect_clones(min_lines=2, similarity_threshold=0.8)
        assert result["total_pairs"] >= 1
        assert result["type1_pairs"] >= 1
    finally:
        db.close()


def test_detect_clones_type2_renamed():
    """Type-2 检测：token 序列相同但标识符名不同应识别为 Type-2。"""
    db, _root = _db_with_workspace()
    try:
        # 两个符号除了函数名不同外，结构完全相同（都是 def X(): return 1 + 2）
        # 注意：symbol_hash 必须不同才会被识别为 Type-2（否则是 Type-1）
        _seed_symbol(
            db, "a.py", "alpha",
            "def alpha():\n    x = 1\n    y = 2\n    return x + y\n",
        )
        _seed_symbol(
            db, "b.py", "beta",
            "def beta():\n    x = 1\n    y = 2\n    return x + y\n",
        )

        result = db.detect_clones(min_lines=3, similarity_threshold=0.95)
        # Type-2 应至少有 1 对（归一化后 token 序列相同）
        assert result["type2_pairs"] >= 1, f"Expected Type-2 pair, got: {result}"
    finally:
        db.close()


def test_detect_clones_type3_modified():
    """Type-3 检测：相似但不完全相同的符号应识别为 Type-3。"""
    db, _root = _db_with_workspace()
    try:
        # 两个符号名字相同前缀，内容有部分共享 token 但集合不同
        # 第一个用 if 分支，第二个用 for 循环，token 集合有交集但不完全相同
        _seed_symbol(
            db, "a.py", "process",
            "def process():\n"
            "    x = 1\n"
            "    y = 2\n"
            "    if x > 0:\n"
            "        return x + y\n"
            "    return 0\n",
        )
        _seed_symbol(
            db, "b.py", "process_data",
            "def process_data():\n"
            "    x = 1\n"
            "    y = 2\n"
            "    for i in range(10):\n"
            "        x += i\n"
            "    return x + y\n",
        )

        # 阈值设低一点（0.3）确保能识别到相似但不完全相同的对
        result = db.detect_clones(min_lines=3, similarity_threshold=0.3)
        # Type-3 应至少有 1 对（相似度高但小于 1.0）
        assert result["type3_pairs"] >= 1, f"Expected Type-3 pair, got: {result}"
    finally:
        db.close()


def test_list_clones_returns_pairs():
    """list_clones 应返回检测到的克隆对。"""
    db, _root = _db_with_workspace()
    try:
        shared_hash = "shared_hash_list"
        content = "def foo():\n    return 42\n"
        _seed_symbol(db, "a.py", "foo", content, symbol_hash=shared_hash)
        _seed_symbol(db, "b.py", "foo", content, symbol_hash=shared_hash)

        db.detect_clones(min_lines=2, similarity_threshold=0.8)
        clones = db.list_clones()
        assert len(clones) >= 1
        first = clones[0]
        assert "clone_type" in first
        assert "similarity" in first
        assert "file_a" in first
        assert "file_b" in first
    finally:
        db.close()


def test_get_clone_stats():
    """get_clone_stats 应返回正确的统计信息。"""
    db, _root = _db_with_workspace()
    try:
        shared_hash = "shared_hash_stats"
        content = "def foo():\n    return 42\n"
        _seed_symbol(db, "a.py", "foo", content, symbol_hash=shared_hash)
        _seed_symbol(db, "b.py", "foo", content, symbol_hash=shared_hash)

        db.detect_clones(min_lines=2, similarity_threshold=0.8)
        stats = db.get_clone_stats()
        assert stats["total"] >= 1
        assert stats["type1"] >= 1
        assert stats["affected_files"] >= 2
        assert stats["affected_symbols"] >= 2
    finally:
        db.close()


def test_clear_clones():
    """clear_clones 应清空所有克隆记录。"""
    db, _root = _db_with_workspace()
    try:
        shared_hash = "shared_hash_clear"
        content = "def foo():\n    return 42\n"
        _seed_symbol(db, "a.py", "foo", content, symbol_hash=shared_hash)
        _seed_symbol(db, "b.py", "foo", content, symbol_hash=shared_hash)

        db.detect_clones(min_lines=2, similarity_threshold=0.8)
        assert db.get_clone_stats()["total"] >= 1

        deleted = db.clear_clones()
        assert deleted >= 1
        assert db.get_clone_stats()["total"] == 0
    finally:
        db.close()


def test_detect_clones_idempotent():
    """重复执行 detect_clones 不应产生重复行（UNIQUE 索引 UPSERT）。"""
    db, _root = _db_with_workspace()
    try:
        shared_hash = "shared_hash_idem"
        content = "def foo():\n    return 42\n"
        _seed_symbol(db, "a.py", "foo", content, symbol_hash=shared_hash)
        _seed_symbol(db, "b.py", "foo", content, symbol_hash=shared_hash)

        # 第一次检测
        r1 = db.detect_clones(min_lines=2, similarity_threshold=0.8)
        # 第二次检测（应 UPSERT，不产生重复行）
        r2 = db.detect_clones(min_lines=2, similarity_threshold=0.8)

        assert r1["total_pairs"] == r2["total_pairs"]
        # 数据库中的总记录数应等于 total_pairs（没有重复）
        cur = db.conn.execute("SELECT COUNT(*) as c FROM clone_pairs")
        actual = cur.fetchone()["c"]
        assert actual == r2["total_pairs"], f"DB has {actual} rows but reported {r2['total_pairs']}"
    finally:
        db.close()


def test_detect_clones_filters_short_symbols():
    """min_lines 过滤应跳过行数不足的符号。"""
    db, _root = _db_with_workspace()
    try:
        shared_hash = "shared_hash_short"
        # 只有 2 行的符号
        content = "def foo():\n    return 42\n"
        _seed_symbol(db, "a.py", "foo", content, start_line=1, end_line=2,
                     symbol_hash=shared_hash)
        _seed_symbol(db, "b.py", "foo", content, start_line=1, end_line=2,
                     symbol_hash=shared_hash)

        # min_lines=5 应跳过 2 行符号
        result = db.detect_clones(min_lines=5, similarity_threshold=0.8)
        assert result["total_pairs"] == 0
        assert result["scanned_symbols"] == 0

        # min_lines=1 应检测到
        result = db.detect_clones(min_lines=1, similarity_threshold=0.8)
        assert result["total_pairs"] >= 1
    finally:
        db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
