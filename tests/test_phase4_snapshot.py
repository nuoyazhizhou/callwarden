"""Phase 4.1+4.2 单元测试：GraphSnapshot + ArcSwap 原子发布。

测试范围：
- PySnapshotManager 基础生命周期（new / current_generation / snapshot_stats）
- build_and_publish 从 SQLite 加载并原子发布
- 多次 publish 实现 generation 单调递增
- PySnapshotCache 多 workspace 管理（get_or_create / evict / list / len）
- 并发读不阻塞写（ArcSwap 语义验证）

注：测试需要构建后的 callwarden_core 扩展（maturin develop 或 pip install wheel）。
"""

import os
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest


# 跳过条件：callwarden_core 未安装时跳过
callwarden_core = pytest.importorskip("callwarden_core")


# ----------------------------------------------------------------------
# 测试 fixture：构造一个最小可用的 callwarden.db
# ----------------------------------------------------------------------

@pytest.fixture
def minimal_db(tmp_path):
    """构造一个最小可用的 callwarden.db，包含 symbols + calls + file_instances 表。"""
    db_path = tmp_path / "callwarden.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # file_instances 表
    cur.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY,
            rel_path TEXT,
            status TEXT DEFAULT 'active'
        )
    """)
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (1, 'src/main.py')")
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (2, 'src/util.py')")

    # symbols 表
    cur.execute("""
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY,
            file_instance_id INTEGER,
            kind TEXT,
            name TEXT,
            qualified_name TEXT,
            module_path TEXT,
            start_line INTEGER,
            end_line INTEGER,
            depth INTEGER
        )
    """)
    cur.execute("""INSERT INTO symbols VALUES
        (1, 1, 'fn', 'main', 'main', '', 10, 20, 0),
        (2, 1, 'fn', 'init', 'main.init', '', 5, 8, 1),
        (3, 2, 'fn', 'helper', 'util.helper', '', 1, 5, 1)
    """)

    # calls 表
    cur.execute("""
        CREATE TABLE calls (
            caller_id INTEGER,
            callee_id INTEGER,
            callee_name TEXT,
            call_line INTEGER,
            is_cross_file INTEGER
        )
    """)
    cur.execute("""INSERT INTO calls VALUES
        (1, 2, 'init', 12, 0),
        (2, 3, 'helper', 7, 1),
        (1, 3, 'helper', 15, 1)
    """)

    conn.commit()
    conn.close()
    return str(db_path)


# ----------------------------------------------------------------------
# PySnapshotManager 基础生命周期
# ----------------------------------------------------------------------

class TestSnapshotManagerBasic:
    def test_new_manager_has_zero_generation(self):
        """新创建的 manager 当前 generation 应为 0（尚未发布）。"""
        from callwarden_core import PySnapshotManager
        mgr = PySnapshotManager("ws_test_001")
        assert mgr.current_generation() == 0

    def test_snapshot_stats_none_before_publish(self):
        """未发布前 snapshot_stats 应返回 None。"""
        from callwarden_core import PySnapshotManager
        mgr = PySnapshotManager("ws_test_002")
        assert mgr.snapshot_stats() is None


# ----------------------------------------------------------------------
# build_and_publish + generation 单调递增
# ----------------------------------------------------------------------

class TestBuildAndPublish:
    def test_current_store_shares_published_graph(self, minimal_db):
        """current_store 返回可查询的共享 Arc 视图。"""
        from callwarden_core import PySnapshotManager
        mgr = PySnapshotManager("ws_shared_store")
        mgr.build_and_publish(minimal_db, "ctx_shared", None)
        store = mgr.current_store()
        assert store is not None
        assert store.get_symbol("util.helper")["name"] == "helper"

    def test_build_and_publish_returns_generation_and_counts(self, minimal_db):
        """build_and_publish 后返回 (generation, symbol_count, call_count)。
        注：GraphStore by_id 按 max(symbol_id)+1 resize，因此返回值可能比实际符号数多 1。
        """
        from callwarden_core import PySnapshotManager
        mgr = PySnapshotManager("ws_build_001")
        gen, syms, calls = mgr.build_and_publish(minimal_db, "ctx_hash_abc", None)
        assert gen == 1
        assert syms >= 3  # 可能多 1（by_id resize 留空槽）
        assert calls == 3

    def test_generation_monotonically_increases(self, minimal_db):
        """多次 publish 后 generation 单调递增。"""
        from callwarden_core import PySnapshotManager
        mgr = PySnapshotManager("ws_build_002")
        g1, _, _ = mgr.build_and_publish(minimal_db, "ctx_v1", None)
        g2, _, _ = mgr.build_and_publish(minimal_db, "ctx_v2", None)
        g3, _, _ = mgr.build_and_publish(minimal_db, "ctx_v3", None)
        assert g1 < g2 < g3
        assert (g1, g2, g3) == (1, 2, 3)

    def test_snapshot_stats_after_publish(self, minimal_db):
        """发布后 snapshot_stats 返回完整 dict。"""
        from callwarden_core import PySnapshotManager
        mgr = PySnapshotManager("ws_build_003")
        mgr.build_and_publish(minimal_db, "ctx_hash_xyz", "snap_abc")
        stats = mgr.snapshot_stats()
        assert stats is not None
        assert stats["workspace_instance_id"] == "ws_build_003"
        assert stats["generation"] == 1
        assert stats["symbol_count"] >= 3  # by_id resize 可能多 1
        assert stats["call_count"] == 3
        assert stats["build_context_hash"] == "ctx_hash_xyz"
        assert stats["snapshot_id"] == "snap_abc"

    def test_current_generation_after_publish(self, minimal_db):
        """发布后 current_generation 反映最新版本号。"""
        from callwarden_core import PySnapshotManager
        mgr = PySnapshotManager("ws_build_004")
        mgr.build_and_publish(minimal_db, "ctx", None)
        assert mgr.current_generation() == 1
        mgr.build_and_publish(minimal_db, "ctx2", None)
        assert mgr.current_generation() == 2

    def test_publish_with_none_snapshot_id(self, minimal_db):
        """snapshot_id=None 时 snapshot_stats 不应包含 snapshot_id 字段。"""
        from callwarden_core import PySnapshotManager
        mgr = PySnapshotManager("ws_build_005")
        mgr.build_and_publish(minimal_db, "ctx", None)
        stats = mgr.snapshot_stats()
        assert "snapshot_id" not in stats


# ----------------------------------------------------------------------
# PySnapshotCache 多 workspace 管理
# ----------------------------------------------------------------------

class TestSnapshotCache:
    def test_cache_default_max_workspaces(self):
        """默认 max_workspaces=32。"""
        from callwarden_core import PySnapshotCache
        cache = PySnapshotCache()
        # 不直接断言 32，但应能容纳至少 10 个 workspace
        for i in range(10):
            cache.get_or_create(f"ws_{i}")
        assert cache.len() == 10

    def test_get_or_create_returns_manager(self):
        """get_or_create 返回 PySnapshotManager。"""
        from callwarden_core import PySnapshotCache, PySnapshotManager
        cache = PySnapshotCache(8)
        mgr = cache.get_or_create("ws_a")
        assert isinstance(mgr, PySnapshotManager)

    def test_get_or_create_idempotent(self):
        """同一 workspace_id 多次 get_or_create 返回等价 manager。"""
        from callwarden_core import PySnapshotCache
        cache = PySnapshotCache(8)
        m1 = cache.get_or_create("ws_idem")
        m2 = cache.get_or_create("ws_idem")
        # 应该是同一个 inner Arc（current_generation 一致即可证明）
        assert m1.current_generation() == m2.current_generation()

    def test_get_returns_none_for_unknown(self):
        """get 不存在的 workspace 返回 None。"""
        from callwarden_core import PySnapshotCache
        cache = PySnapshotCache(8)
        assert cache.get("nonexistent") is None

    def test_list_workspaces(self):
        """list_workspaces 返回所有已注册 workspace_id。"""
        from callwarden_core import PySnapshotCache
        cache = PySnapshotCache(8)
        cache.get_or_create("ws_list_1")
        cache.get_or_create("ws_list_2")
        cache.get_or_create("ws_list_3")
        ws_list = cache.list_workspaces()
        assert len(ws_list) == 3
        assert set(ws_list) == {"ws_list_1", "ws_list_2", "ws_list_3"}

    def test_evict_removes_workspace(self):
        """evict 移除指定 workspace，返回 True（成功）。"""
        from callwarden_core import PySnapshotCache
        cache = PySnapshotCache(8)
        cache.get_or_create("ws_evict_me")
        assert cache.len() == 1
        result = cache.evict("ws_evict_me")
        assert result is True
        assert cache.len() == 0
        assert cache.get("ws_evict_me") is None

    def test_evict_unknown_returns_false(self):
        """evict 不存在的 workspace 返回 False。"""
        from callwarden_core import PySnapshotCache
        cache = PySnapshotCache(8)
        assert cache.evict("ghost") is False


# ----------------------------------------------------------------------
# LRU 淘汰策略
# ----------------------------------------------------------------------

class TestSnapshotCacheLRU:
    def test_lru_eviction_when_full(self):
        """max_workspaces 满后插入新 workspace 会淘汰旧的。"""
        from callwarden_core import PySnapshotCache
        cache = PySnapshotCache(2)
        cache.get_or_create("ws_1")
        cache.get_or_create("ws_2")
        assert cache.len() == 2
        # 插入第三个，应淘汰一个旧的
        cache.get_or_create("ws_3")
        assert cache.len() <= 2  # 不超过 max_workspaces


# ----------------------------------------------------------------------
# 并发读不阻塞写（ArcSwap 语义验证）
# ----------------------------------------------------------------------

class TestConcurrentAccess:
    def test_concurrent_reads_during_publish(self, minimal_db):
        """并发读 snapshot_stats 的同时 publish，不应阻塞或抛异常。"""
        from callwarden_core import PySnapshotManager
        mgr = PySnapshotManager("ws_concurrent_001")
        mgr.build_and_publish(minimal_db, "ctx_v1", None)

        errors = []
        barrier = threading.Barrier(2)

        def reader():
            try:
                barrier.wait()
                for _ in range(50):
                    stats = mgr.snapshot_stats()
                    assert stats is not None
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                barrier.wait()
                for i in range(10):
                    mgr.build_and_publish(minimal_db, f"ctx_v{i+2}", None)
                    time.sleep(0.002)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=writer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == [], f"并发访问出错: {errors}"
        # 最终 generation 应为 11（1 次初始 + 10 次 writer）
        assert mgr.current_generation() == 11


# ----------------------------------------------------------------------
# build_and_publish 错误场景
# ----------------------------------------------------------------------

class TestBuildAndPublishErrors:
    def test_nonexistent_db_path(self, tmp_path):
        """不存在的 db_path 应抛异常（PyRuntimeError）。"""
        from callwarden_core import PySnapshotManager
        mgr = PySnapshotManager("ws_err_001")
        with pytest.raises(Exception):
            mgr.build_and_publish(str(tmp_path / "nonexistent.db"), "ctx", None)

    def test_invalid_db_no_symbols_table(self, tmp_path):
        """无效 db（无 symbols 表）应抛异常。"""
        from callwarden_core import PySnapshotManager
        bad_db = tmp_path / "empty.db"
        # 只创建空数据库，不建表
        sqlite3.connect(str(bad_db)).close()
        mgr = PySnapshotManager("ws_err_002")
        with pytest.raises(Exception):
            mgr.build_and_publish(str(bad_db), "ctx", None)
