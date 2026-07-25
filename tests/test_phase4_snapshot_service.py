"""Phase 4.3 单元测试：Python 侧 SnapshotManagerService。

测试范围：
- 单例 get_instance / reset_instance
- publish_snapshot 发布 + 查询统计
- query_callers / query_callees / search_symbols 通过 GraphStore 查询
- evict_workspace 清理
- Rust 不可用时降级（None / []）
"""

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from callwarden.server.snapshot_manager import (
    SnapshotManagerService,
    get_snapshot_service,
    publish_workspace_snapshot,
    get_workspace_snapshot_stats,
)


# 跳过条件：callwarden_core 未安装时跳过
callwarden_core = pytest.importorskip("callwarden_core")


# ----------------------------------------------------------------------
# 测试 fixture
# ----------------------------------------------------------------------

@pytest.fixture
def minimal_db(tmp_path):
    """构造一个最小可用的 callwarden.db。"""
    db_path = tmp_path / "callwarden.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""CREATE TABLE file_instances (
        id INTEGER PRIMARY KEY, rel_path TEXT, status TEXT DEFAULT 'active')""")
    cur.execute("INSERT INTO file_instances VALUES (1, 'src/main.py', 'active')")
    cur.execute("""CREATE TABLE symbols (
        id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT,
        name TEXT, qualified_name TEXT, module_path TEXT,
        start_line INTEGER, end_line INTEGER, depth INTEGER)""")
    cur.execute("""INSERT INTO symbols VALUES
        (1, 1, 'fn', 'main', 'main', '', 10, 20, 0),
        (2, 1, 'fn', 'init', 'main.init', '', 5, 8, 1)""")
    cur.execute("""CREATE TABLE calls (
        caller_id INTEGER, callee_id INTEGER, callee_name TEXT,
        call_line INTEGER, is_cross_file INTEGER)""")
    cur.execute("INSERT INTO calls VALUES (1, 2, 'init', 12, 0)")
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def service():
    """每个测试用独立的 SnapshotManagerService 实例。"""
    SnapshotManagerService.reset_instance()
    svc = SnapshotManagerService(max_workspaces=8)
    # 替换单例
    SnapshotManagerService._instance = svc
    yield svc
    SnapshotManagerService.reset_instance()


# ----------------------------------------------------------------------
# 单例测试
# ----------------------------------------------------------------------

class TestSingleton:
    def test_get_instance_returns_singleton(self):
        SnapshotManagerService.reset_instance()
        s1 = SnapshotManagerService.get_instance()
        s2 = SnapshotManagerService.get_instance()
        assert s1 is s2

    def test_reset_instance_creates_new(self):
        SnapshotManagerService.reset_instance()
        s1 = SnapshotManagerService.get_instance()
        SnapshotManagerService.reset_instance()
        s2 = SnapshotManagerService.get_instance()
        assert s1 is not s2


# ----------------------------------------------------------------------
# publish_snapshot 测试
# ----------------------------------------------------------------------

class TestPublishSnapshot:
    def test_publish_returns_dict_with_metadata(self, service, minimal_db):
        result = service.publish_snapshot("ws_svc_001", minimal_db, "ctx_abc", "snap_1")
        assert result is not None
        assert result["workspace_instance_id"] == "ws_svc_001"
        assert result["generation"] == 1
        assert result["symbol_count"] >= 2
        assert result["call_count"] == 1
        assert result["snapshot_id"] == "snap_1"
        assert result["build_context_hash"] == "ctx_abc"

    def test_publish_increments_generation(self, service, minimal_db):
        r1 = service.publish_snapshot("ws_svc_002", minimal_db, "ctx_v1")
        r2 = service.publish_snapshot("ws_svc_002", minimal_db, "ctx_v2")
        assert r1["generation"] == 1
        assert r2["generation"] == 2

    def test_get_snapshot_stats_after_publish(self, service, minimal_db):
        service.publish_snapshot("ws_svc_003", minimal_db, "ctx_xyz", "snap_x")
        stats = service.get_snapshot_stats("ws_svc_003")
        assert stats is not None
        assert stats["generation"] == 1
        assert stats["symbol_count"] >= 2

    def test_get_snapshot_stats_unknown_workspace(self, service):
        assert service.get_snapshot_stats("nonexistent") is None

    def test_get_current_generation(self, service, minimal_db):
        assert service.get_current_generation("ws_gen") == 0
        service.publish_snapshot("ws_gen", minimal_db, "ctx")
        assert service.get_current_generation("ws_gen") == 1
        service.publish_snapshot("ws_gen", minimal_db, "ctx2")
        assert service.get_current_generation("ws_gen") == 2

    def test_get_current_generation_unknown(self, service):
        assert service.get_current_generation("ghost") == 0


# ----------------------------------------------------------------------
# 查询代理测试
# ----------------------------------------------------------------------

class TestQueryProxy:
    def test_query_callers_returns_results(self, service, minimal_db):
        service.publish_snapshot("ws_q_001", minimal_db)
        callers = service.query_callers("ws_q_001", "init")
        assert isinstance(callers, list)
        # 应该能找到 main 调用了 init
        assert len(callers) >= 1

    def test_query_callers_unknown_workspace(self, service):
        assert service.query_callers("ghost", "init") == []

    def test_query_callers_unknown_function(self, service, minimal_db):
        service.publish_snapshot("ws_q_002", minimal_db)
        callers = service.query_callers("ws_q_002", "nonexistent_func")
        assert callers == []

    def test_query_callees_returns_results(self, service, minimal_db):
        service.publish_snapshot("ws_q_003", minimal_db)
        callees = service.query_callees("ws_q_003", "main")
        assert isinstance(callees, list)
        assert len(callees) >= 1

    def test_search_symbols_returns_results(self, service, minimal_db):
        service.publish_snapshot("ws_q_004", minimal_db)
        results = service.search_symbols("ws_q_004", "main")
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_search_symbols_unknown_workspace(self, service):
        assert service.search_symbols("ghost", "main") == []


# ----------------------------------------------------------------------
# evict_workspace 测试
# ----------------------------------------------------------------------

class TestEvictWorkspace:
    def test_evict_removes_workspace(self, service, minimal_db):
        service.publish_snapshot("ws_evict", minimal_db)
        assert service.get_current_generation("ws_evict") == 1
        result = service.evict_workspace("ws_evict")
        assert result is True
        assert service.get_current_generation("ws_evict") == 0
        assert service.get_snapshot_stats("ws_evict") is None

    def test_evict_unknown_returns_false(self, service):
        result = service.evict_workspace("nonexistent")
        assert result is False

    def test_evict_clears_query_cache(self, service, minimal_db):
        service.publish_snapshot("ws_evict_q", minimal_db)
        assert service.query_callers("ws_evict_q", "init") != []
        service.evict_workspace("ws_evict_q")
        assert service.query_callers("ws_evict_q", "init") == []


# ----------------------------------------------------------------------
# list_workspaces 测试
# ----------------------------------------------------------------------

class TestListWorkspaces:
    def test_list_empty(self, service):
        assert service.list_workspaces() == []

    def test_list_after_publish(self, service, minimal_db):
        service.publish_snapshot("ws_l1", minimal_db)
        service.publish_snapshot("ws_l2", minimal_db)
        ws_list = service.list_workspaces()
        assert set(ws_list) == {"ws_l1", "ws_l2"}

    def test_list_after_evict(self, service, minimal_db):
        service.publish_snapshot("ws_l3", minimal_db)
        service.publish_snapshot("ws_l4", minimal_db)
        service.evict_workspace("ws_l3")
        ws_list = service.list_workspaces()
        assert ws_list == ["ws_l4"]


# ----------------------------------------------------------------------
# 便捷函数测试
# ----------------------------------------------------------------------

class TestConvenienceFunctions:
    def test_publish_workspace_snapshot(self, minimal_db):
        SnapshotManagerService.reset_instance()
        result = publish_workspace_snapshot("ws_conv_001", minimal_db, "ctx")
        assert result is not None
        assert result["generation"] == 1
        SnapshotManagerService.reset_instance()

    def test_get_workspace_snapshot_stats(self, minimal_db):
        SnapshotManagerService.reset_instance()
        publish_workspace_snapshot("ws_conv_002", minimal_db, "ctx")
        stats = get_workspace_snapshot_stats("ws_conv_002")
        assert stats is not None
        assert stats["generation"] == 1
        SnapshotManagerService.reset_instance()


# ----------------------------------------------------------------------
# rust_available 属性
# ----------------------------------------------------------------------

class TestRustAvailable:
    def test_rust_available_true(self, service):
        # callwarden_core 已 import 成功（pytest.importorskip 保证）
        assert service.rust_available is True
