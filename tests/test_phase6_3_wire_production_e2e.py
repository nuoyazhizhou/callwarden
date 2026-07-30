"""Phase 6-3 P2 端到端差分回归：semantic_search + find_similar_functions Rust 短路

验证 D2.6 + D2.7 端到端场景：
  - VectorMixin.semantic_search 走 Rust 短路时，与 Python 全路径输出一致
  - VectorMixin.find_similar_functions 走 Rust 短路时，与 Python 全路径输出一致

测试策略：
  1. 构造一个 in-memory SQLite 数据库 + VectorMixin 实例
  2. 插入预生成的 embeddings（避免依赖 sentence-transformers 模型）
  3. Mock embedder 返回固定向量
  4. 对比 Rust 短路（rollback_flag=0）与 Python 全路径（rollback_flag=1）的输出

前置条件：
  - Rust 扩展 callwarden_core 必须可加载
  - numpy 必须可用

关联：
  - Python 真相源：db/db_vector.py:VectorMixin.semantic_search (L455-L540)
                  db/db_vector.py:VectorMixin.find_similar_functions (L542-L655)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

# ============================================
# 前置条件检查
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
        "请先运行 `maturin develop --manifest-path rust_ext/Cargo.toml --release`。"
    )


# ============================================
# 测试 fixture：构造内存数据库 + VectorMixin 实例
# ============================================

class _StubDB:
    """轻量包装：持有 sqlite3 连接，注入 VectorMixin 方法

    避免 CodeGraphDB 完整初始化（连接管理、workspace 探测）。
    """

    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._workspace_id = 1
        self._embedder_instance = ("mock", None)  # 跳过 sentence-transformers 加载

    def _get_active_workspace_id(self) -> int:
        return self._workspace_id

    def is_feature_rolled_back(self, feature_name: str) -> bool:
        """查询 rollback_config 表判断功能是否回滚"""
        try:
            cur = self.conn.execute(
                "SELECT rollback_flag FROM rollback_config WHERE feature_name = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (feature_name,),
            )
            row = cur.fetchone()
            return bool(row and row["rollback_flag"] == 1)
        except Exception:
            return False

    def set_rollback_flag(self, feature_name: str, flag: int) -> None:
        """手动设置 rollback_flag（测试用）"""
        self.conn.execute(
            "UPDATE rollback_config SET rollback_flag = ?, updated_at = ? "
            "WHERE feature_name = ?",
            (flag, 0.0, feature_name),  # updated_at 不影响测试
        )
        self.conn.commit()

    def register_rollback_config(
        self,
        task_id: str,
        feature_name: str,
        phase: int = 6,
        production_entry: str = "",
        rollback_entry: str = "",
    ) -> None:
        """注册 rollback_config 记录（简化版）"""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO rollback_config
                (workspace_id, task_id, feature_name, phase,
                 production_entry, rollback_entry, rollback_flag,
                 rollback_window_until, config_blob,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, '', '', 0, 0)
            """,
            (self._workspace_id, task_id, feature_name, phase,
             production_entry, rollback_entry),
        )
        self.conn.commit()


def _init_schema(db: _StubDB) -> None:
    """初始化测试所需的最小 schema"""
    db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id INTEGER PRIMARY KEY,
            name TEXT,
            is_active INTEGER DEFAULT 0
        );
        INSERT OR REPLACE INTO workspaces (id, name, is_active) VALUES (1, 'test', 1);

        CREATE TABLE IF NOT EXISTS file_instances (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            rel_path TEXT,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS symbols (
            symbol_hash TEXT,
            file_instance_id INTEGER,
            qualified_name TEXT,
            name TEXT,
            kind TEXT,
            module_path TEXT,
            visibility TEXT,
            start_line INTEGER,
            end_line INTEGER
        );

        CREATE TABLE IF NOT EXISTS symbol_contents (
            content_hash TEXT PRIMARY KEY,
            content TEXT
        );

        CREATE TABLE IF NOT EXISTS symbol_summaries (
            symbol_hash TEXT,
            summary TEXT,
            is_current INTEGER DEFAULT 1,
            version INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS symbol_embeddings (
            symbol_hash TEXT PRIMARY KEY,
            embedding BLOB,
            model_version TEXT,
            dim INTEGER,
            embedded_at REAL
        );

        CREATE TABLE IF NOT EXISTS rollback_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER,
            task_id TEXT,
            feature_name TEXT UNIQUE,
            phase INTEGER,
            production_entry TEXT,
            rollback_entry TEXT,
            rollback_flag INTEGER DEFAULT 0,
            rollback_window_until TEXT,
            config_blob TEXT,
            created_at REAL,
            updated_at REAL
        );
    """)
    db.conn.commit()


def _populate_test_data(db: _StubDB, num_symbols: int = 10, dim: int = 64) -> None:
    """插入测试数据：N 个符号 + 对应 embeddings

    使用确定性向量生成（symbol_hash 派生），避免依赖 numpy.random
    """
    import numpy

    for i in range(num_symbols):
        symbol_hash = f"hash_{i:03d}"
        qualified_name = f"module.fn_{i}"
        file_id = (i // 5) + 1  # 每文件 5 个符号
        rel_path = f"src/file_{file_id}.py"

        # 创建 file_instance（如果不存在）
        db.conn.execute(
            "INSERT OR IGNORE INTO file_instances (id, workspace_id, rel_path, status) "
            "VALUES (?, ?, ?, 'active')",
            (file_id, db._workspace_id, rel_path),
        )

        # 插入符号
        db.conn.execute(
            "INSERT INTO symbols (symbol_hash, file_instance_id, qualified_name, "
            "name, kind, module_path, visibility, start_line, end_line) "
            "VALUES (?, ?, ?, ?, 'fn', ?, 'public', ?, ?)",
            (symbol_hash, file_id, qualified_name, f"fn_{i}",
             f"module", i * 10 + 1, i * 10 + 5),
        )

        # 插入 content + summary
        db.conn.execute(
            "INSERT OR REPLACE INTO symbol_contents (content_hash, content) VALUES (?, ?)",
            (symbol_hash, f"def fn_{i}(): pass"),
        )
        db.conn.execute(
            "INSERT INTO symbol_summaries (symbol_hash, summary, is_current, version) "
            "VALUES (?, ?, 1, 1)",
            (symbol_hash, f"Summary of fn_{i}"),
        )

        # 生成 embedding（确定性，fn_0 和 fn_1 故意相似）
        if i == 0:
            vec = numpy.ones(dim, dtype=numpy.float32)
        elif i == 1:
            vec = numpy.ones(dim, dtype=numpy.float32) * 0.99  # 与 fn_0 高度相似
        else:
            # 其他符号用 hash 派生的伪随机向量
            seed = hash(symbol_hash) % 1000
            rng = numpy.random.RandomState(seed)
            vec = rng.randn(dim).astype(numpy.float32)

        db.conn.execute(
            "INSERT OR REPLACE INTO symbol_embeddings "
            "(symbol_hash, embedding, model_version, dim, embedded_at) "
            "VALUES (?, ?, 'test-model', ?, 0)",
            (symbol_hash, vec.tobytes(), dim),
        )

    db.conn.commit()


@pytest.fixture
def stub_db(tmp_path):
    """构造带测试数据的 stub DB"""
    if not _RUST_EXT_AVAILABLE:
        pytest.skip(_RUST_EXT_SKIP_REASON)

    db_path = str(tmp_path / "test.db")
    db = _StubDB(db_path)
    _init_schema(db)
    _populate_test_data(db, num_symbols=10, dim=64)

    # 注册 rust_vector_topk rollback_config（默认 rollback_flag=0，走 Rust）
    db.register_rollback_config(
        task_id="T-test-phase6-3",
        feature_name="rust_vector_topk",
        phase=6,
        production_entry="test:rust short-circuit",
        rollback_entry="test:python fallback",
    )

    # 注入 VectorMixin 方法
    # 注意：@staticmethod（如 _blob_to_vec / _vec_to_blob）不能用 __get__ 绑定，
    # 否则会变成 bound method 把 db 作为第一个参数传入。
    # 使用 inspect.getattr_static 检测 staticmethod descriptor
    import inspect
    from callwarden.db.db_vector import VectorMixin
    for name in dir(VectorMixin):
        if name.startswith("__"):
            continue  # 跳过 dunder
        if hasattr(db, name):
            continue  # 不覆盖 stub_db 已定义的方法
        raw_attr = inspect.getattr_static(VectorMixin, name, None)
        if isinstance(raw_attr, staticmethod):
            # staticmethod：直接赋值底层函数（不绑定 self）
            setattr(db, name, raw_attr.__func__)
        else:
            attr = getattr(VectorMixin, name, None)
            if callable(attr):
                bound = attr.__get__(db, type(db))
                setattr(db, name, bound)

    # 显式注入私有方法（dir() 也会返回，但确保 _vector_topk_via_rust 一定存在）
    if not hasattr(db, "_vector_topk_via_rust"):
        db._vector_topk_via_rust = VectorMixin._vector_topk_via_rust.__get__(db, type(db))

    # Mock _get_embedder 返回固定后端，避免加载 sentence-transformers
    def _mock_get_embedder():
        return ("mock", None)
    db._get_embedder = _mock_get_embedder

    # Mock _embed_text 返回固定向量（用于 semantic_search）
    import numpy
    def _mock_embed_text(text: str):
        # 根据查询文本派生确定性向量，让 fn_0/fn_1 高度匹配
        if "fn_0" in text or "first" in text.lower():
            return numpy.ones(64, dtype=numpy.float32).tolist()
        # 默认返回与 fn_0 相似的向量
        return numpy.ones(64, dtype=numpy.float32).tolist()
    db._embed_text = _mock_embed_text

    yield db

    db.conn.close()


# ============================================
# D2.6: semantic_search 端到端差分
# ============================================

class TestSemanticSearchE2E:
    """D2.6: semantic_search Rust 短路与 Python 全路径输出一致"""

    def test_rust_path_returns_results(self, stub_db):
        """Rust 短路（rollback_flag=0）应返回非空结果"""
        # 默认 rollback_flag=0，走 Rust
        results = stub_db.semantic_search("find fn_0", top_k=5)
        assert len(results) > 0, "Rust 路径应返回结果"
        # 验证结果字段
        for r in results:
            assert "qualified_name" in r
            assert "file_path" in r
            assert "similarity" in r
            assert "summary" in r
            assert 0.0 <= r["similarity"] <= 1.0

    def test_python_path_returns_results(self, stub_db):
        """Python 全路径（rollback_flag=1）应返回非空结果"""
        stub_db.set_rollback_flag("rust_vector_topk", 1)
        results = stub_db.semantic_search("find fn_0", top_k=5)
        assert len(results) > 0, "Python 路径应返回结果"

    def test_rust_python_output_equal(self, stub_db):
        """关键测试：Rust 与 Python 输出应字段级一致"""
        # Rust 路径
        stub_db.set_rollback_flag("rust_vector_topk", 0)
        rust_results = stub_db.semantic_search("find fn_0", top_k=5)

        # Python 路径
        stub_db.set_rollback_flag("rust_vector_topk", 1)
        py_results = stub_db.semantic_search("find fn_0", top_k=5)

        # 对比结果
        assert len(rust_results) == len(py_results), (
            f"长度不一致：rust={len(rust_results)} py={len(py_results)}"
        )
        for r, p in zip(rust_results, py_results):
            assert r["qualified_name"] == p["qualified_name"], (
                f"qualified_name 不一致：rust={r['qualified_name']} py={p['qualified_name']}"
            )
            assert r["file_path"] == p["file_path"]
            assert r["start_line"] == p["start_line"]
            # similarity 可能有浮点误差，容差 1e-4
            assert abs(r["similarity"] - p["similarity"]) < 1e-4, (
                f"similarity 不一致：rust={r['similarity']} py={p['similarity']}"
            )
            assert r["summary"] == p["summary"]

    def test_topk_limit_respected(self, stub_db):
        """top_k=3 时返回最多 3 个结果"""
        stub_db.set_rollback_flag("rust_vector_topk", 0)
        rust_results = stub_db.semantic_search("find fn_0", top_k=3)
        assert len(rust_results) <= 3

        stub_db.set_rollback_flag("rust_vector_topk", 1)
        py_results = stub_db.semantic_search("find fn_0", top_k=3)
        assert len(py_results) <= 3


# ============================================
# D2.7: find_similar_functions 端到端差分
# ============================================

class TestFindSimilarFunctionsE2E:
    """D2.7: find_similar_functions Rust 短路与 Python 全路径输出一致"""

    def test_rust_path_returns_results(self, stub_db):
        """Rust 短路应返回相似函数列表"""
        stub_db.set_rollback_flag("rust_vector_topk", 0)
        results = stub_db.find_similar_functions("module.fn_0", threshold=0.0, top_k=5)
        assert len(results) > 0, "Rust 路径应返回结果"
        # fn_0 不应在结果中（过滤自身）
        for r in results:
            assert r["qualified_name"] != "module.fn_0", "不应包含目标函数自身"

    def test_python_path_returns_results(self, stub_db):
        """Python 全路径应返回相似函数列表"""
        stub_db.set_rollback_flag("rust_vector_topk", 1)
        results = stub_db.find_similar_functions("module.fn_0", threshold=0.0, top_k=5)
        assert len(results) > 0, "Python 路径应返回结果"

    def test_rust_python_output_equal(self, stub_db):
        """关键测试：Rust 与 Python 输出应字段级一致"""
        # Rust 路径
        stub_db.set_rollback_flag("rust_vector_topk", 0)
        rust_results = stub_db.find_similar_functions(
            "module.fn_0", threshold=0.0, top_k=10
        )

        # Python 路径
        stub_db.set_rollback_flag("rust_vector_topk", 1)
        py_results = stub_db.find_similar_functions(
            "module.fn_0", threshold=0.0, top_k=10
        )

        # 对比结果
        assert len(rust_results) == len(py_results), (
            f"长度不一致：rust={len(rust_results)} py={len(py_results)}"
        )
        for r, p in zip(rust_results, py_results):
            assert r["qualified_name"] == p["qualified_name"], (
                f"qualified_name 不一致：rust={r['qualified_name']} py={p['qualified_name']}"
            )
            assert r["file_path"] == p["file_path"]
            assert r["start_line"] == p["start_line"]
            # similarity 已 round 到 4 位小数，应完全相等
            assert r["similarity"] == p["similarity"], (
                f"similarity 不一致：rust={r['similarity']} py={p['similarity']}"
            )
            assert r["summary"] == p["summary"]

    def test_threshold_filter_respected(self, stub_db):
        """threshold=0.99 应过滤掉低相似度的结果"""
        # Rust 路径
        stub_db.set_rollback_flag("rust_vector_topk", 0)
        rust_results = stub_db.find_similar_functions(
            "module.fn_0", threshold=0.99, top_k=10
        )

        # Python 路径
        stub_db.set_rollback_flag("rust_vector_topk", 1)
        py_results = stub_db.find_similar_functions(
            "module.fn_0", threshold=0.99, top_k=10
        )

        # 两个路径应返回相同数量的结果
        assert len(rust_results) == len(py_results), (
            f"threshold 过滤后数量不一致：rust={len(rust_results)} py={len(py_results)}"
        )
        # 所有 similarity 应 >= threshold
        for r in rust_results:
            assert r["similarity"] >= 0.99, (
                f"Rust 路径返回低于 threshold 的结果：{r['similarity']}"
            )

    def test_nonexistent_target_returns_empty(self, stub_db):
        """不存在的目标函数应返回空列表"""
        stub_db.set_rollback_flag("rust_vector_topk", 0)
        results = stub_db.find_similar_functions("nonexistent.fn", threshold=0.0, top_k=10)
        assert len(results) == 0

        stub_db.set_rollback_flag("rust_vector_topk", 1)
        py_results = stub_db.find_similar_functions("nonexistent.fn", threshold=0.0, top_k=10)
        assert len(py_results) == 0
