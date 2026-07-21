"""D1/D7 评审声明不成立修正验证测试。

评审报告 2026-07-20：
- D1：向量检索声明用 sqlite-vec，实际是 BLOB + Python/numpy 全量余弦扫描
- D7：跨仓库关系写入空 target_symbol_hash，反向查询无法命中

修复点：
1. D1：修正 db_vector.py / schema.py 文档声明为实际实现
   （BLOB + Rust 批量余弦相似度，sqlite-vec 待落地）
2. D7：修正 db_cross_repo.py 让 target_symbol_hash 写入真实值
   （原代码写空字符串，导致反向查询永远无法命中）
"""

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# D1: 向量检索文档声明修正
# ============================================================

def test_d1_db_vector_docstring_clarifies_no_sqlite_vec():
    """D1 修复：db_vector.py 顶部文档字符串明确说明实际实现。

    评审 D1：原代码声称使用 sqlite-vec，实际是 BLOB + Python/numpy 全量余弦扫描。
    """
    db_vector = ROOT / "db" / "db_vector.py"
    content = db_vector.read_text(encoding="utf-8")

    # 必须明确说明不是 sqlite-vec
    assert "不是 sqlite-vec" in content or "sqlite-vec 待落地" in content, (
        "db_vector.py 必须在文档字符串中明确说明实际实现（不是 sqlite-vec）"
    )

    # 必须说明实际实现是 BLOB + Rust/numpy 余弦相似度
    assert "BLOB" in content, "db_vector.py 必须说明向量以 BLOB 存储"
    assert "batch_cosine_similarity" in content or "numpy" in content, (
        "db_vector.py 必须说明用 Rust batch_cosine_similarity 或 numpy 计算相似度"
    )


def test_d1_schema_comment_clarifies_no_sqlite_vec():
    """D1 修复：schema.py 中 v5 注释明确说明实际实现。"""
    schema = ROOT / "db" / "schema.py"
    content = schema.read_text(encoding="utf-8")

    # v5 注释不能仅说 "sqlite-vec"
    # 必须明确说明实际实现或标注 "待落地"
    v5_section_marker = "v5: 向量嵌入表"
    assert v5_section_marker in content, "schema.py 必须有 v5 注释"

    # 找到 v5 注释所在行
    lines = content.splitlines()
    v5_line_idx = next(
        (i for i, line in enumerate(lines) if v5_section_marker in line),
        None,
    )
    assert v5_line_idx is not None

    # 检查 v5 注释行和后续 3 行，必须提到 "待落地" 或 "BLOB" 或 "Rust/numpy"
    nearby_lines = "\n".join(lines[v5_line_idx:v5_line_idx + 4])
    assert (
        "待落地" in nearby_lines
        or "BLOB" in nearby_lines
        or "Rust/numpy" in nearby_lines
        or "实际实现" in nearby_lines
    ), f"schema.py v5 注释必须明确说明实际实现，附近行：{nearby_lines}"


def test_d1_vector_implementation_does_not_use_sqlite_vec():
    """D1 验证：实际代码不使用 sqlite-vec 扩展。

    确认 _load_all_embeddings / _batch_cosine 是 BLOB + numpy 实现，
    而不是 sqlite-vec 的 vec0 虚拟表 KNN 查询。
    """
    db_vector = ROOT / "db" / "db_vector.py"
    content = db_vector.read_text(encoding="utf-8")

    # 实际查询是 SELECT symbol_hash, embedding FROM symbol_embeddings
    # 不是 SELECT ... FROM symbol_embeddings_vec USING KNN
    assert "FROM symbol_embeddings" in content, (
        "实际查询应直接读 symbol_embeddings 表（BLOB 列）"
    )

    # 不应该有 vec0 虚拟表创建语句
    assert "CREATE VIRTUAL TABLE" not in content or "vec0" not in content.lower(), (
        "db_vector.py 不应创建 vec0 虚拟表（sqlite-vec 未落地）"
    )

    # 必须有 _batch_cosine 函数（Rust/numpy 实现）
    assert "_batch_cosine" in content, (
        "db_vector.py 必须有 _batch_cosine 函数（Rust/numpy 余弦相似度实现）"
    )


# ============================================================
# D7: 跨仓库 target_symbol_hash 修复
# ============================================================

def test_d7_db_cross_repo_writes_real_target_symbol_hash():
    """D7 修复：db_cross_repo.py 检测到 import 时写入真实 target_symbol_hash。

    评审 D7：原代码 line 192 写入空字符串 ""，
    导致反向查询 WHERE target_symbol_hash = ? 永远无法命中。
    """
    db_cross_repo = ROOT / "db" / "db_cross_repo.py"
    content = db_cross_repo.read_text(encoding="utf-8")

    # 必须有 INSERT INTO cross_repo_deps（P1-2 改为 INSERT OR IGNORE INTO 实现幂等，
    # 两种形式都接受）
    assert (
        "INSERT INTO cross_repo_deps" in content
        or "INSERT OR IGNORE INTO cross_repo_deps" in content
    ), "db_cross_repo.py 必须有 INSERT INTO cross_repo_deps 或 INSERT OR IGNORE INTO cross_repo_deps"

    # 找到 INSERT ... cross_repo_deps 的代码块，附近 50 行
    lines = content.splitlines()
    insert_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if "INSERT INTO cross_repo_deps" in line
            or "INSERT OR IGNORE INTO cross_repo_deps" in line
        ),
        None,
    )
    assert insert_idx is not None

    # 取 INSERT 到 commit 之间的代码（INSERT 完整块）
    insert_block_lines = []
    for i in range(insert_idx, min(insert_idx + 50, len(lines))):
        insert_block_lines.append(lines[i])
        if "self.conn.commit()" in lines[i]:
            break
    insert_block = "\n".join(insert_block_lines)

    # D7 修复：VALUES 的第 5 个参数（target_symbol_hash）必须是变量 target_symbol_hash，
    # 不能是空字符串 ""
    # 原代码：source_ws_id, t_id, "import", sym["symbol_hash"], "", dep["evidence"], 0.8, now,
    # 修复后：source_ws_id, t_id, "import", sym["symbol_hash"], target_symbol_hash, dep["evidence"], 0.8, now,
    #
    # 检查方式：在 INSERT 块中，VALUES 参数列表里不应有 "" 作为 target_symbol_hash
    # 简单检查：INSERT 块中不应有 ""（除了 "import" 这种字符串字面量，但 "import" 不是空字符串）
    # 更精确：检查 sym["symbol_hash"], target_symbol_hash, dep["evidence"] 这种模式
    assert 'target_symbol_hash,' in insert_block, (
        f"INSERT 块必须使用 target_symbol_hash 变量作为参数，实际块：\n{insert_block}"
    )

    # 不能有 sym["symbol_hash"], "", dep["evidence"] 这种写法
    # （原代码用空字符串作为 target_symbol_hash）
    bad_pattern = 'sym["symbol_hash"],\n                                    "",'
    assert bad_pattern not in insert_block, (
        f"INSERT 块不能用空字符串作为 target_symbol_hash，实际块：\n{insert_block}"
    )


def test_d7_db_cross_repo_target_symbol_names_stores_hash():
    """D7 修复：target_symbol_names 字典存储 (qualified_name, symbol_hash) 元组。

    原代码只存 name -> qualified_name，丢失了 symbol_hash。
    """
    db_cross_repo = ROOT / "db" / "db_cross_repo.py"
    content = db_cross_repo.read_text(encoding="utf-8")

    # 必须有 Tuple 导入
    assert "Tuple" in content, "db_cross_repo.py 必须导入 Tuple"

    # target_symbol_names 类型必须是 Dict[int, Dict[str, Tuple[str, str]]]
    assert "Dict[str, Tuple" in content or "Tuple[str, str]" in content, (
        "target_symbol_names 必须存 (qualified_name, symbol_hash) 元组"
    )

    # 必须有解构赋值：target_qn, target_symbol_hash = ...
    assert "target_symbol_hash" in content, (
        "必须有 target_symbol_hash 变量"
    )


def test_d7_db_cross_repo_end_to_end_writes_real_hash():
    """D7 端到端验证：实际调用 detect_cross_repo_deps 后写入非空 target_symbol_hash。"""
    # 构造内存数据库 + 必要的表
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # 创建 workspaces 表
    conn.execute("""
        CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            path TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)

    # 创建 symbols 表
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

    # 创建 file_instances 表
    conn.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL
        )
    """)

    # 创建 symbol_contents 表
    conn.execute("""
        CREATE TABLE symbol_contents (
            content_hash TEXT PRIMARY KEY,
            content TEXT
        )
    """)

    # 创建 cross_repo_deps 表（评审 D7 关注的表）
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

    # 插入源仓库（workspace_id=1）和目标仓库（workspace_id=2）
    conn.execute("INSERT INTO workspaces (name, path, created_at) VALUES (?, ?, ?)", ("source", "/src", 0.0))
    conn.execute("INSERT INTO workspaces (name, path, created_at) VALUES (?, ?, ?)", ("target", "/tgt", 0.0))

    # 源仓库符号：包含 import target_module 的代码
    # symbol_hash = "src_hash_001"
    conn.execute("INSERT INTO file_instances (workspace_id, rel_path) VALUES (?, ?)", (1, "source.py"))
    src_file_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO symbols (symbol_hash, name, qualified_name, kind, module_path, file_instance_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("src_hash_001", "source_func", "src.source_func", "function", "src", src_file_id),
    )
    # 源符号内容包含 import target_module
    conn.execute(
        "INSERT INTO symbol_contents (content_hash, content) VALUES (?, ?)",
        ("src_hash_001", "import target_module\ndef source_func(): pass"),
    )

    # 目标仓库符号：target_module 函数
    # symbol_hash = "tgt_hash_001"
    conn.execute("INSERT INTO file_instances (workspace_id, rel_path) VALUES (?, ?)", (2, "target.py"))
    tgt_file_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO symbols (symbol_hash, name, qualified_name, kind, module_path, file_instance_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("tgt_hash_001", "target_module", "tgt.target_module", "function", "tgt", tgt_file_id),
    )
    conn.execute(
        "INSERT INTO symbol_contents (content_hash, content) VALUES (?, ?)",
        ("tgt_hash_001", "def target_module(): pass"),
    )

    conn.commit()

    # 构造一个 mock CodeGraphDB，只提供 detect_cross_repo_deps 需要的接口
    try:
        from callwarden.db.db_cross_repo import CrossRepoMixin

        # 用 mixin 实例化（绕过 CodeGraphDB 完整初始化）
        class FakeDB(CrossRepoMixin):
            def __init__(self, conn):
                self.conn = conn

        db = FakeDB(conn)

        # 调用 detect_cross_repo_deps
        result = db.detect_cross_repo_deps("source")

        # 必须检测到 1 个依赖
        assert result["total_deps"] >= 1, f"应检测到至少 1 个依赖，实际：{result}"

        # 查询 cross_repo_deps 表，验证 target_symbol_hash 非空
        cur = conn.execute("SELECT target_symbol_hash FROM cross_repo_deps")
        rows = cur.fetchall()

        assert len(rows) > 0, "cross_repo_deps 表必须有记录"
        for row in rows:
            target_hash = row["target_symbol_hash"]
            # D7 修复：target_symbol_hash 必须非空
            assert target_hash, (
                f"target_symbol_hash 不能为空字符串，实际：'{target_hash}'"
            )
            assert target_hash != "", (
                f"target_symbol_hash 不能是空字符串，实际：'{target_hash}'"
            )
            # 应该是 "tgt_hash_001"（目标符号的 hash）
            assert target_hash == "tgt_hash_001", (
                f"target_symbol_hash 应为 'tgt_hash_001'，实际：'{target_hash}'"
            )

    finally:
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
