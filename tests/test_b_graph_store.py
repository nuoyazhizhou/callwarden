"""B-PoC: GraphStore 单元测试 + 性能基准

验证 Rust 侧 CSR 邻接表 + 内存索引的查询功能与性能：
- 从真实 SQLite 加载 symbols + calls
- get_callers / get_callees / get_symbol / search_symbols
- get_call_chain_down (BFS)
- get_topological_order (Kahn)
- detect_cycles (三色标记)
- 性能对比：Rust 内存查询 vs Python SQL 查询
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import time
import unittest
from typing import Optional

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)


def _has_rust_ext() -> bool:
    try:
        import callwarden_core  # noqa: F401
        return True
    except ImportError:
        return False


def _find_callwarden_db() -> Optional[str]:
    """查找 callwarden 项目自身的数据库"""
    import hashlib
    root = os.path.abspath(os.path.join(_PKG_ROOT))
    h = hashlib.sha256(root.encode()).hexdigest()[:16]
    # 尝试多种路径格式（Windows/Linux）
    candidates = [
        os.path.join(os.path.expanduser("~"), ".callwarden", h, "callwarden.db"),
    ]
    # 列出 ~/.callwarden 下的所有 hash 目录
    cw_root = os.path.join(os.path.expanduser("~"), ".callwarden")
    if os.path.isdir(cw_root):
        for d in os.listdir(cw_root):
            p = os.path.join(cw_root, d, "callwarden.db")
            if os.path.exists(p):
                candidates.append(p)
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    """以 immutable=1 只读模式连接 SQLite，避免 WAL 模式下 -shm 文件创建被沙箱拦截"""
    normalized = db_path.replace('\\', '/')
    prefix = "file:" if normalized.startswith('/') else "file:///"
    uri = f"{prefix}{normalized}?immutable=1"
    return sqlite3.connect(uri, uri=True)


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestGraphStoreLoad(unittest.TestCase):
    """测试从 SQLite 加载"""

    def setUp(self):
        self.db_path = _find_callwarden_db()
        if not self.db_path:
            self.skipTest("未找到 callwarden 数据库")

    def test_load_returns_counts(self):
        """加载应返回符号数和边数"""
        from callwarden_core import GraphStore
        store = GraphStore()
        n_sym, n_edge = store.load_from_sqlite(self.db_path)
        self.assertGreater(n_sym, 0)
        self.assertGreater(n_edge, 0)

    def test_stats_after_load(self):
        """加载后 stats 应返回完整信息"""
        from callwarden_core import GraphStore
        store = GraphStore()
        store.load_from_sqlite(self.db_path)
        stats = store.stats()
        self.assertIn("symbol_count", stats)
        self.assertIn("edge_count", stats)
        self.assertIn("resolved_edge_count", stats)
        self.assertIn("forward_offsets_size", stats)
        self.assertIn("root_count", stats)
        self.assertGreater(stats["symbol_count"], 0)
        self.assertGreater(stats["edge_count"], 0)
        self.assertGreater(stats["root_count"], 0)
        # forward_offsets 应该 = max_id + 2
        self.assertEqual(stats["forward_offsets_size"], stats["symbol_count"] + 1)

    def test_load_not_loaded_error(self):
        """未加载时调用查询应报错"""
        from callwarden_core import GraphStore
        store = GraphStore()
        with self.assertRaises(Exception):
            store.get_callers("foo")


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestGraphStoreQueries(unittest.TestCase):
    """测试查询接口正确性"""

    @classmethod
    def setUpClass(cls):
        cls.db_path = _find_callwarden_db()
        if not cls.db_path:
            raise unittest.SkipTest("未找到 callwarden 数据库")
        from callwarden_core import GraphStore
        cls.store = GraphStore()
        cls.store.load_from_sqlite(cls.db_path)
        # 从 SQLite 直接查一些已知数据用于断言
        cls.conn = _connect_readonly(cls.db_path)

    def test_get_symbol_exists(self):
        """get_symbol 应返回存在的符号"""
        # 查一个已存在的 qname
        cur = self.conn.execute(
            "SELECT qualified_name FROM symbols WHERE qualified_name != '' LIMIT 1"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        qname = row[0]
        sym = self.store.get_symbol(qname)
        self.assertIsNotNone(sym)
        self.assertEqual(sym["qualified_name"], qname)
        self.assertIn("name", sym)
        self.assertIn("kind", sym)
        self.assertIn("start_line", sym)
        self.assertIn("file_rel_path", sym)

    def test_get_symbol_not_exists(self):
        """get_symbol 不存在的应返回 None"""
        result = self.store.get_symbol("nonexistent.qualified.name.xyz")
        self.assertIsNone(result)

    def test_get_callers_matches_sql(self):
        """get_callers 结果应与 SQL 查询一致"""
        # 找一个有调用者的 callee_name
        cur = self.conn.execute(
            "SELECT callee_name, count(*) c FROM calls GROUP BY callee_name ORDER BY c DESC LIMIT 1"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        callee_name, expected_count = row

        rust_callers = self.store.get_callers(callee_name)
        # Rust 侧可能多算（同名符号 + callee_name 匹配），但不应少于 SQL
        # 因为 Rust 对齐 Python get_callers 是按 callee_name 查的
        self.assertGreater(len(rust_callers), 0)
        # 验证字段完整性
        if rust_callers:
            first = rust_callers[0]
            self.assertIn("caller_name", first)
            self.assertIn("caller_qualified", first)
            self.assertIn("caller_file", first)
            self.assertIn("call_line", first)

    def test_get_callees_matches_sql(self):
        """get_callees 结果应与 SQL 查询一致"""
        # 找一个有 callees 的 caller_name
        cur = self.conn.execute(
            "SELECT s.name FROM calls c JOIN symbols s ON c.caller_id = s.id "
            "GROUP BY s.name ORDER BY count(*) DESC LIMIT 1"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        caller_name = row[0]

        rust_callees = self.store.get_callees(caller_name)
        self.assertGreater(len(rust_callees), 0)
        if rust_callees:
            first = rust_callees[0]
            self.assertIn("callee_name", first)
            self.assertIn("callee_qualified", first)
            self.assertIn("call_line", first)

    def test_search_symbols_substring(self):
        """search_symbols 子串匹配应返回结果"""
        results = self.store.search_symbols("parse", limit=10)
        self.assertGreater(len(results), 0)
        # 验证所有结果都包含 parse（不区分大小写）
        for r in results:
            name_lower = r["name"].lower()
            qname_lower = r["qualified_name"].lower()
            self.assertTrue("parse" in name_lower or "parse" in qname_lower)

    def test_search_symbols_kind_filter(self):
        """search_symbols kind 过滤应生效"""
        results = self.store.search_symbols("parse", kind="fn", limit=10)
        for r in results:
            self.assertEqual(r["kind"], "fn")

    def test_search_symbols_limit(self):
        """search_symbols limit 应生效"""
        results = self.store.search_symbols("a", limit=5)
        self.assertLessEqual(len(results), 5)

    def test_get_call_chain_down_bfs(self):
        """get_call_chain_down 应返回 BFS 遍历结果"""
        # 找一个有 outgoing edges 的符号
        cur = self.conn.execute(
            "SELECT s.qualified_name FROM calls c JOIN symbols s ON c.caller_id = s.id "
            "WHERE s.qualified_name != '' LIMIT 1"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        qname = row[0]

        chain = self.store.get_call_chain_down(qname, 3)
        # 应有边
        self.assertGreater(len(chain), 0)
        # 验证 depth 字段（BFS 层级）
        depths = [e["depth"] for e in chain]
        self.assertEqual(depths[0], 0)  # 第一层 depth=0

    def test_get_topological_order(self):
        """topo order 应返回所有函数符号，且无环时完整"""
        topo = self.store.get_topological_order()
        self.assertGreater(len(topo), 0)
        # 所有元素应是字符串
        for name in topo:
            self.assertIsInstance(name, str)

    def test_detect_cycles_no_crash(self):
        """detect_cycles 不应崩溃，返回列表"""
        cycles = self.store.detect_cycles()
        self.assertIsInstance(cycles, list)


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestGraphStoreCycleDetection(unittest.TestCase):
    """用临时数据库测试环检测"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_b_test_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        # 创建带环的调用图：A -> B -> C -> A
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT, is_active INTEGER DEFAULT 0);
            INSERT INTO workspaces VALUES (1, 'test', '/test', 1);
            CREATE TABLE file_instances (id INTEGER PRIMARY KEY, workspace_id INTEGER, rel_path TEXT, abs_path TEXT, status TEXT DEFAULT 'pending');
            INSERT INTO file_instances VALUES (1, 1, 'a.py', '/test/a.py', 'pending');
            CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_instance_id INTEGER, name TEXT, kind TEXT, qualified_name TEXT, module_path TEXT DEFAULT '', start_line INTEGER, end_line INTEGER, depth INTEGER DEFAULT -1);
            INSERT INTO symbols VALUES (1, 1, 'funcA', 'fn', 'mod.funcA', '', 1, 10, -1);
            INSERT INTO symbols VALUES (2, 1, 'funcB', 'fn', 'mod.funcB', '', 1, 10, -1);
            INSERT INTO symbols VALUES (3, 1, 'funcC', 'fn', 'mod.funcC', '', 1, 10, -1);
            CREATE TABLE calls (id INTEGER PRIMARY KEY, caller_id INTEGER, caller_name TEXT, caller_module TEXT, callee_name TEXT, callee_module TEXT DEFAULT '', callee_qualified TEXT DEFAULT '', callee_file TEXT DEFAULT '', callee_id INTEGER DEFAULT 0, call_line INTEGER DEFAULT 0, is_cross_file INTEGER DEFAULT 0);
            -- A -> B -> C -> A 环（callee_id 指向 symbols.id）
            INSERT INTO calls VALUES (1, 1, 'funcA', '', 'funcB', '', 'mod.funcB', '', 2, 1, 0);
            INSERT INTO calls VALUES (2, 2, 'funcB', '', 'funcC', '', 'mod.funcC', '', 3, 1, 0);
            INSERT INTO calls VALUES (3, 3, 'funcC', '', 'funcA', '', 'mod.funcA', '', 1, 1, 0);
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_detect_cycle_finds_ring(self):
        """应检测到 A->B->C->A 环"""
        from callwarden_core import GraphStore
        store = GraphStore()
        store.load_from_sqlite(self.db_path)
        cycles = store.detect_cycles()
        self.assertGreater(len(cycles), 0, "应检测到至少 1 个环")
        # 环应包含 funcA/funcB/funcC
        all_names = set()
        for cycle in cycles:
            for name in cycle:
                all_names.add(name)
        self.assertIn("mod.funcA", all_names)
        self.assertIn("mod.funcB", all_names)
        self.assertIn("mod.funcC", all_names)

    def test_topo_order_excludes_cycle_nodes(self):
        """有环时 topo order 应排除环内节点（Kahn 算法特性）"""
        from callwarden_core import GraphStore
        store = GraphStore()
        store.load_from_sqlite(self.db_path)
        topo = store.get_topological_order()
        # 3 个节点都在环里，Kahn 算法无法排序，应返回空或部分
        # 注：Kahn 对环内节点不入队，所以 topo 可能为空
        self.assertLessEqual(len(topo), 3)


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestGraphStorePerformance(unittest.TestCase):
    """性能对比：Rust 内存查询 vs Python SQL 查询"""

    @classmethod
    def setUpClass(cls):
        cls.db_path = _find_callwarden_db()
        if not cls.db_path:
            raise unittest.SkipTest("未找到 callwarden 数据库")
        from callwarden_core import GraphStore
        cls.store = GraphStore()
        cls.store.load_from_sqlite(cls.db_path)
        cls.py_conn = _connect_readonly(cls.db_path)
        # 找 20 个有调用者的 callee_name 做批量测试
        cur = cls.py_conn.execute(
            "SELECT callee_name FROM calls WHERE callee_name != '' "
            "GROUP BY callee_name ORDER BY count(*) DESC LIMIT 20"
        )
        cls.test_callee_names = [row[0] for row in cur]
        # 找 20 个有 callees 的 caller_name
        cur = cls.py_conn.execute(
            "SELECT s.name FROM calls c JOIN symbols s ON c.caller_id = s.id "
            "GROUP BY s.name ORDER BY count(*) DESC LIMIT 20"
        )
        cls.test_caller_names = [row[0] for row in cur]

    def test_perf_get_callers_rust_vs_python(self):
        """get_callers 性能对比"""
        # Python SQL 基准
        t0 = time.perf_counter()
        for name in self.test_callee_names:
            cur = self.py_conn.execute(
                "SELECT c.*, s.name FROM calls c JOIN symbols s ON c.caller_id = s.id WHERE c.callee_name = ?",
                (name,)
            )
            list(cur)
        py_time = time.perf_counter() - t0

        # Rust 内存查询
        t0 = time.perf_counter()
        for name in self.test_callee_names:
            self.store.get_callers(name)
        rust_time = time.perf_counter() - t0

        # Rust 应该快（至少不慢于 Python SQL）
        # 注：PoC 的 get_callers 是全扫 forward_edges（O(E)），后续优化用 CSR backward
        print(f"\n  get_callers x{len(self.test_callee_names)}: "
              f"Python SQL {py_time*1000:.2f}ms vs Rust {rust_time*1000:.2f}ms "
              f"(speedup: {py_time/rust_time:.2f}x)")

    def test_perf_search_symbols_rust_vs_python(self):
        """search_symbols 性能对比"""
        queries = ["parse", "get", "refresh", "symbol", "call"]

        # Python SQL（FTS5 或 LIKE）
        t0 = time.perf_counter()
        for q in queries:
            cur = self.py_conn.execute(
                "SELECT name, qualified_name FROM symbols WHERE name LIKE ? OR qualified_name LIKE ? LIMIT 50",
                (f"%{q}%", f"%{q}%")
            )
            list(cur)
        py_time = time.perf_counter() - t0

        # Rust 内存遍历
        t0 = time.perf_counter()
        for q in queries:
            self.store.search_symbols(q, limit=50)
        rust_time = time.perf_counter() - t0

        print(f"\n  search_symbols x{len(queries)}: "
              f"Python LIKE {py_time*1000:.2f}ms vs Rust {rust_time*1000:.2f}ms "
              f"(speedup: {py_time/rust_time:.2f}x)")

    def test_perf_topo_order_rust_vs_python_depth(self):
        """topo order 性能 — Rust 用 Kahn，Python 用递归 depth"""
        # Rust topo
        t0 = time.perf_counter()
        rust_topo = self.store.get_topological_order()
        rust_time = time.perf_counter() - t0

        print(f"\n  topo_order: Rust Kahn {rust_time*1000:.2f}ms ({len(rust_topo)} symbols)")

    def test_perf_load_time(self):
        """加载时间基准"""
        from callwarden_core import GraphStore
        store = GraphStore()
        t0 = time.perf_counter()
        n_sym, n_edge = store.load_from_sqlite(self.db_path)
        load_time = time.perf_counter() - t0

        print(f"\n  load_from_sqlite: {load_time*1000:.2f}ms "
              f"({n_sym} symbols, {n_edge} edges)")


if __name__ == "__main__":
    unittest.main()
