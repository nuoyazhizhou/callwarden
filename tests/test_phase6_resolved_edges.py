"""
Phase 6.3: resolved edges 按 build_context_hash 隔离测试

验证同一源码在不同 build context 下的 resolved edges 互相隔离。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import importlib.util


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_tc_mod = _load_module("db_toolchain", str(Path(__file__).parent.parent / "db" / "db_toolchain.py"))
_schema_mod = _load_module("db_schema", str(Path(__file__).parent.parent / "db" / "schema.py"))

init_toolchain_schema = _tc_mod.init_toolchain_schema
compute_build_context_hash = _tc_mod.compute_build_context_hash
ResolvedEdge = _tc_mod.ResolvedEdge
store_resolved_edges = _tc_mod.store_resolved_edges
get_resolved_edges = _tc_mod.get_resolved_edges
delete_resolved_edges = _tc_mod.delete_resolved_edges
count_resolved_edges = _tc_mod.count_resolved_edges
list_build_context_edges = _tc_mod.list_build_context_edges

SCHEMA_SQL = _schema_mod.SCHEMA_SQL


@pytest.fixture
def db_conn(tmp_path):
    """创建带 schema 的 DB，返回 (conn, ws_id)"""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    init_toolchain_schema(conn)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO workspaces (name, root_path, created_at) VALUES (?, ?, ?)",
        ("test_ws", str(tmp_path), 0.0),
    )
    conn.commit()
    ws_id = conn.execute("SELECT id FROM workspaces WHERE name='test_ws'").fetchone()[0]
    yield conn, ws_id
    conn.close()


def _make_edge(caller_id, callee_id, callee_name="foo", callee_file="bar.c",
               call_line=10, method="exact_match"):
    """创建 edge dict"""
    return {
        "caller_symbol_id": caller_id,
        "callee_symbol_id": callee_id,
        "callee_name": callee_name,
        "callee_file": callee_file,
        "call_line": call_line,
        "resolution_method": method,
    }


# ============================================
# TestStoreResolvedEdges —— 存储测试
# ============================================

class TestStoreResolvedEdges:
    """resolved edges 存储测试"""

    def test_store_basic(self, db_conn):
        """基本存储"""
        conn, ws_id = db_conn
        bch = "debug_hash"
        edges = [
            _make_edge(1, 10, "func_a", "a.c", 5),
            _make_edge(1, 11, "func_b", "b.c", 10),
            _make_edge(2, 10, "func_a", "a.c", 15),
        ]
        count = store_resolved_edges(conn, ws_id, bch, edges)
        assert count == 3

    def test_store_dedup(self, db_conn):
        """重复 edge 不重复插入"""
        conn, ws_id = db_conn
        bch = "debug_hash"
        edge = _make_edge(1, 10, "func_a", "a.c", 5)
        store_resolved_edges(conn, ws_id, bch, [edge])
        count = store_resolved_edges(conn, ws_id, bch, [edge])  # 相同 edge
        assert count == 0  # 已存在

    def test_store_different_build_context(self, db_conn):
        """不同 build context 下相同 edge 可以共存"""
        conn, ws_id = db_conn
        bch1 = "debug_hash"
        bch2 = "release_hash"
        edge = _make_edge(1, 10, "func_a", "a.c", 5)

        store_resolved_edges(conn, ws_id, bch1, [edge])
        count = store_resolved_edges(conn, ws_id, bch2, [edge])  # 相同 edge，不同 context
        assert count == 1  # 在不同 context 下是独立存储

    def test_store_empty_list(self, db_conn):
        """空列表"""
        conn, ws_id = db_conn
        count = store_resolved_edges(conn, ws_id, "debug_hash", [])
        assert count == 0


# ============================================
# TestGetResolvedEdges —— 查询测试
# ============================================

class TestGetResolvedEdges:
    """resolved edges 查询测试"""

    def test_get_all_for_context(self, db_conn):
        """查询某 context 的所有 edges"""
        conn, ws_id = db_conn
        bch = "debug_hash"
        edges = [
            _make_edge(1, 10, "func_a", "a.c", 5),
            _make_edge(1, 11, "func_b", "b.c", 10),
            _make_edge(2, 12, "func_c", "c.c", 15),
        ]
        store_resolved_edges(conn, ws_id, bch, edges)

        result = get_resolved_edges(conn, ws_id, bch)
        assert len(result) == 3

    def test_get_by_caller(self, db_conn):
        """按 caller 查询"""
        conn, ws_id = db_conn
        bch = "debug_hash"
        edges = [
            _make_edge(1, 10, "func_a", "a.c", 5),
            _make_edge(1, 11, "func_b", "b.c", 10),
            _make_edge(2, 12, "func_c", "c.c", 15),
        ]
        store_resolved_edges(conn, ws_id, bch, edges)

        result = get_resolved_edges(conn, ws_id, bch, caller_symbol_id=1)
        assert len(result) == 2
        for edge in result:
            assert edge.caller_symbol_id == 1

    def test_get_isolated_by_build_context(self, db_conn):
        """不同 build context 的 edges 互相隔离"""
        conn, ws_id = db_conn
        bch1 = "debug_hash"
        bch2 = "release_hash"

        # debug context: func_a → func_b
        store_resolved_edges(conn, ws_id, bch1, [
            _make_edge(1, 10, "func_b", "b.c", 5, "exact_match"),
        ])
        # release context: func_a → func_c（不同 callee，因为不同 sysroot）
        store_resolved_edges(conn, ws_id, bch2, [
            _make_edge(1, 20, "func_c", "c.c", 5, "include_path"),
        ])

        # 查询 debug context
        debug_edges = get_resolved_edges(conn, ws_id, bch1, caller_symbol_id=1)
        assert len(debug_edges) == 1
        assert debug_edges[0].callee_symbol_id == 10
        assert debug_edges[0].callee_name == "func_b"
        assert debug_edges[0].resolution_method == "exact_match"

        # 查询 release context
        release_edges = get_resolved_edges(conn, ws_id, bch2, caller_symbol_id=1)
        assert len(release_edges) == 1
        assert release_edges[0].callee_symbol_id == 20
        assert release_edges[0].callee_name == "func_c"
        assert release_edges[0].resolution_method == "include_path"

    def test_get_empty(self, db_conn):
        """空查询"""
        conn, ws_id = db_conn
        result = get_resolved_edges(conn, ws_id, "nonexistent")
        assert result == []


# ============================================
# TestDeleteResolvedEdges —— 删除测试
# ============================================

class TestDeleteResolvedEdges:
    """resolved edges 删除测试"""

    def test_delete_by_context(self, db_conn):
        """按 build context 删除"""
        conn, ws_id = db_conn
        bch1 = "debug_hash"
        bch2 = "release_hash"

        store_resolved_edges(conn, ws_id, bch1, [_make_edge(1, 10)])
        store_resolved_edges(conn, ws_id, bch2, [_make_edge(1, 20)])

        # 只删 debug
        deleted = delete_resolved_edges(conn, ws_id, bch1)
        assert deleted == 1

        # release 还在
        assert count_resolved_edges(conn, ws_id, bch2) == 1
        assert count_resolved_edges(conn, ws_id, bch1) == 0

    def test_delete_all_for_workspace(self, db_conn):
        """删除 workspace 的所有 edges"""
        conn, ws_id = db_conn
        bch1 = "debug_hash"
        bch2 = "release_hash"

        store_resolved_edges(conn, ws_id, bch1, [_make_edge(1, 10)])
        store_resolved_edges(conn, ws_id, bch2, [_make_edge(1, 20)])

        deleted = delete_resolved_edges(conn, ws_id)  # 不指定 context
        assert deleted == 2

        assert count_resolved_edges(conn, ws_id, bch1) == 0
        assert count_resolved_edges(conn, ws_id, bch2) == 0

    def test_delete_nonexistent(self, db_conn):
        """删除不存在的"""
        conn, ws_id = db_conn
        deleted = delete_resolved_edges(conn, ws_id, "nonexistent")
        assert deleted == 0


# ============================================
# TestCountAndList —— 统计测试
# ============================================

class TestCountAndList:
    """统计和列表测试"""

    def test_count(self, db_conn):
        """计数"""
        conn, ws_id = db_conn
        bch = "debug_hash"
        store_resolved_edges(conn, ws_id, bch, [
            _make_edge(1, 10),
            _make_edge(1, 11),
            _make_edge(2, 12),
        ])
        assert count_resolved_edges(conn, ws_id, bch) == 3

    def test_count_empty(self, db_conn):
        """空计数"""
        conn, ws_id = db_conn
        assert count_resolved_edges(conn, ws_id, "nonexistent") == 0

    def test_list_context_edges(self, db_conn):
        """列出各 context 的 edge 统计"""
        conn, ws_id = db_conn
        bch1 = "debug_hash"
        bch2 = "release_hash"

        store_resolved_edges(conn, ws_id, bch1, [
            _make_edge(1, 10),
            _make_edge(1, 11),
        ])
        store_resolved_edges(conn, ws_id, bch2, [
            _make_edge(1, 20),
        ])

        stats = list_build_context_edges(conn, ws_id)
        assert len(stats) == 2

        stat_map = {s["build_context_hash"]: s["edge_count"] for s in stats}
        assert stat_map[bch1] == 2
        assert stat_map[bch2] == 1

    def test_list_empty(self, db_conn):
        """空列表"""
        conn, ws_id = db_conn
        stats = list_build_context_edges(conn, ws_id)
        assert stats == []


# ============================================
# TestResolvedEdgeDataclass —— 数据类
# ============================================

class TestResolvedEdgeDataclass:
    """ResolvedEdge 数据类测试"""

    def test_to_dict(self):
        """序列化"""
        edge = ResolvedEdge(
            id=1, workspace_id=1, build_context_hash="abc",
            caller_symbol_id=10, callee_symbol_id=20,
            callee_name="func_a", callee_file="a.c",
            call_line=5, resolution_method="exact_match",
        )
        d = edge.to_dict()
        assert d["caller_symbol_id"] == 10
        assert d["callee_name"] == "func_a"
        assert d["resolution_method"] == "exact_match"

    def test_summary(self):
        """summary"""
        edge = ResolvedEdge(
            caller_symbol_id=10, callee_name="func_a",
            callee_file="a.c", call_line=5,
            resolution_method="exact_match",
        )
        s = edge.summary()
        assert "10" in s
        assert "func_a" in s
        assert "exact_match" in s


# ============================================
# TestEndToEnd —— 端到端隔离
# ============================================

class TestEndToEnd:
    """端到端：不同 sysroot 下的 resolved edges 隔离"""

    def test_same_source_different_sysroot(self, db_conn):
        """同一源码在不同 sysroot 下解析到不同 callee"""
        conn, ws_id = db_conn

        # 模拟两个 build context（不同 include paths → 不同 sysroot）
        bch_debug = compute_build_context_hash(["-g", "-O0"], {"DEBUG": "1"}, ["/sysroot/debug/include"])
        bch_release = compute_build_context_hash(["-O2"], {"NDEBUG": "1"}, ["/sysroot/release/include"])

        # debug context: func_a 解析到 /sysroot/debug/include/stdio.h 的 printf
        store_resolved_edges(conn, ws_id, bch_debug, [
            _make_edge(1, 100, "printf", "stdio.h", 10, "sysroot"),
        ])

        # release context: func_a 解析到 /sysroot/release/include/stdio.h 的 printf（不同符号 ID）
        store_resolved_edges(conn, ws_id, bch_release, [
            _make_edge(1, 200, "printf", "stdio.h", 10, "sysroot"),
        ])

        # 验证隔离
        debug_edges = get_resolved_edges(conn, ws_id, bch_debug, caller_symbol_id=1)
        assert len(debug_edges) == 1
        assert debug_edges[0].callee_symbol_id == 100

        release_edges = get_resolved_edges(conn, ws_id, bch_release, caller_symbol_id=1)
        assert len(release_edges) == 1
        assert release_edges[0].callee_symbol_id == 200

        # 不共享
        assert debug_edges[0].callee_symbol_id != release_edges[0].callee_symbol_id

    def test_rebuild_context_edges(self, db_conn):
        """重新解析时先删后存"""
        conn, ws_id = db_conn
        bch = "debug_hash"

        # 第一次存储
        store_resolved_edges(conn, ws_id, bch, [
            _make_edge(1, 10, "old_callee", "old.c", 5),
        ])
        assert count_resolved_edges(conn, ws_id, bch) == 1

        # 重新解析：先删再存
        delete_resolved_edges(conn, ws_id, bch)
        store_resolved_edges(conn, ws_id, bch, [
            _make_edge(1, 20, "new_callee", "new.c", 5),
        ])

        edges = get_resolved_edges(conn, ws_id, bch)
        assert len(edges) == 1
        assert edges[0].callee_symbol_id == 20  # 新的 callee
        assert edges[0].callee_name == "new_callee"

    def test_multiple_workspaces_isolated(self, db_conn, tmp_path):
        """多 workspace 的 resolved edges 隔离"""
        conn, ws_id = db_conn
        # 创建第二个 workspace
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at) VALUES (?, ?, ?)",
            ("ws2", str(tmp_path / "ws2"), 0.0),
        )
        conn.commit()
        ws2_id = conn.execute("SELECT id FROM workspaces WHERE name='ws2'").fetchone()[0]

        bch = "shared_context_hash"

        # ws1 和 ws2 用相同 build context hash，但各自独立
        store_resolved_edges(conn, ws_id, bch, [_make_edge(1, 10)])
        store_resolved_edges(conn, ws2_id, bch, [_make_edge(1, 20)])

        # 各自查询
        ws1_edges = get_resolved_edges(conn, ws_id, bch)
        ws2_edges = get_resolved_edges(conn, ws2_id, bch)

        assert len(ws1_edges) == 1
        assert len(ws2_edges) == 1
        assert ws1_edges[0].callee_symbol_id == 10
        assert ws2_edges[0].callee_symbol_id == 20

        # 删 ws1 不影响 ws2
        delete_resolved_edges(conn, ws_id, bch)
        assert count_resolved_edges(conn, ws_id, bch) == 0
        assert count_resolved_edges(conn, ws2_id, bch) == 1

    def test_resolution_methods(self, db_conn):
        """不同解析方法"""
        conn, ws_id = db_conn
        bch = "debug_hash"

        methods = ["exact_match", "include_path", "sysroot", "unresolved"]
        for i, method in enumerate(methods):
            store_resolved_edges(conn, ws_id, bch, [
                _make_edge(1, 100 + i, f"func_{method}", "file.c", 10 + i, method),
            ])

        edges = get_resolved_edges(conn, ws_id, bch)
        assert len(edges) == 4
        edge_methods = {e.resolution_method for e in edges}
        assert edge_methods == set(methods)
