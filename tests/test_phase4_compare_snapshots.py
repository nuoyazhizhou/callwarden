"""Phase 4.8 单元测试：compare_snapshots 同步查询 + snapshot_diff 后台 job

测试范围：
- compare_snapshots 同步查询（file/module/repo scope）
- count_symbols_in_scope（scope 大小检查）
- DaemonClient.compare_snapshots / count_symbols_in_scope / start_snapshot_diff
- snapshot_diff_handler 后台 job
- 同步/异步边界（小 scope 同步、大 scope 转 job）

设计参考：enterprise-daemon-shared-snapshot-plan.md §12.3 / §12.4
"""

import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 跳过条件：callwarden_core 未安装时跳过
callwarden_core = pytest.importorskip("callwarden_core")


# ----------------------------------------------------------------------
# 测试 fixture：构造两个不同版本的 callwarden.db
# ----------------------------------------------------------------------

def _make_db_v1(db_path):
    """版本 1：3 个符号，2 条边
    文件: src/main.py (main, main.init), src/util.py (util.helper)
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY, rel_path TEXT, status TEXT DEFAULT 'active'
        )
    """)
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (1, 'src/main.py')")
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (2, 'src/util.py')")
    cur.execute("""
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT,
            name TEXT, qualified_name TEXT, module_path TEXT,
            start_line INTEGER, end_line INTEGER, depth INTEGER
        )
    """)
    cur.execute("""INSERT INTO symbols VALUES
        (1, 1, 'fn', 'main', 'main', 'app', 10, 20, 0),
        (2, 1, 'fn', 'init', 'main.init', 'app', 5, 8, 1),
        (3, 2, 'fn', 'helper', 'util.helper', 'util', 1, 5, 1)
    """)
    cur.execute("""
        CREATE TABLE calls (
            caller_id INTEGER, callee_id INTEGER, callee_name TEXT,
            call_line INTEGER, is_cross_file INTEGER
        )
    """)
    cur.execute("""INSERT INTO calls VALUES
        (1, 2, 'init', 12, 0),
        (2, 3, 'helper', 7, 1),
        (1, 3, 'helper', 15, 1)
    """)
    conn.commit()
    conn.close()


def _make_db_v2(db_path):
    """版本 2：4 个符号，main 不再调用 helper，新增 new_func
    文件: src/main.py (main, main.init), src/util.py (util.helper, util.new_func)
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY, rel_path TEXT, status TEXT DEFAULT 'active'
        )
    """)
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (1, 'src/main.py')")
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (2, 'src/util.py')")
    cur.execute("""
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT,
            name TEXT, qualified_name TEXT, module_path TEXT,
            start_line INTEGER, end_line INTEGER, depth INTEGER
        )
    """)
    cur.execute("""INSERT INTO symbols VALUES
        (1, 1, 'fn', 'main', 'main', 'app', 10, 20, 0),
        (2, 1, 'fn', 'init', 'main.init', 'app', 5, 8, 1),
        (3, 2, 'fn', 'helper', 'util.helper', 'util', 1, 5, 1),
        (4, 2, 'fn', 'new_func', 'util.new_func', 'util', 1, 5, 1)
    """)
    cur.execute("""
        CREATE TABLE calls (
            caller_id INTEGER, callee_id INTEGER, callee_name TEXT,
            call_line INTEGER, is_cross_file INTEGER
        )
    """)
    cur.execute("""INSERT INTO calls VALUES
        (1, 2, 'init', 12, 0),
        (4, 3, 'helper', 3, 0)
    """)
    conn.commit()
    conn.close()


@pytest.fixture
def two_version_dbs(tmp_path):
    """构造 v1 和 v2 两个版本的 db。"""
    v1 = tmp_path / "v1.db"
    v2 = tmp_path / "v2.db"
    _make_db_v1(v1)
    _make_db_v2(v2)
    return str(v1), str(v2)


def _publish_both(cache, left_db, right_db):
    """在 cache 中发布两个 workspace 的 snapshot。"""
    left_mgr = cache.get_or_create("left_ws")
    left_mgr.build_and_publish(left_db, "ctx_left", None)
    right_mgr = cache.get_or_create("right_ws")
    right_mgr.build_and_publish(right_db, "ctx_right", None)
    return cache


# ----------------------------------------------------------------------
# count_symbols_in_scope 测试
# ----------------------------------------------------------------------

class TestCountSymbolsInScope:
    def test_count_repo_scope(self, two_version_dbs):
        """仓库级 scope 应统计所有符号的并集。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        count = cache.count_symbols_in_scope("left_ws", "right_ws", "repo", "")
        # v1: 3 symbols (main, main.init, util.helper)
        # v2: 4 symbols (main, main.init, util.helper, util.new_func)
        # union: 4 (new_func only in v2)
        assert count == 4

    def test_count_file_scope(self, two_version_dbs):
        """文件级 scope 应只统计该文件的符号。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        count = cache.count_symbols_in_scope("left_ws", "right_ws", "file", "src/util.py")
        # v1 src/util.py: util.helper
        # v2 src/util.py: util.helper, util.new_func
        # union: 2
        assert count == 2

    def test_count_module_scope(self, two_version_dbs):
        """模块级 scope 应只统计该模块的符号。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        count = cache.count_symbols_in_scope("left_ws", "right_ws", "module", "app")
        # v1 module app: main, main.init
        # v2 module app: main, main.init
        # union: 2
        assert count == 2

    def test_count_empty_scope(self, two_version_dbs):
        """空文件路径的 scope 应返回 0。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        count = cache.count_symbols_in_scope("left_ws", "right_ws", "file", "nonexistent.py")
        assert count == 0


# ----------------------------------------------------------------------
# compare_snapshots 同步查询测试
# ----------------------------------------------------------------------

class TestCompareSnapshotsSync:
    def test_compare_repo_scope(self, two_version_dbs):
        """仓库级 scope 应返回所有有变化的符号。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        changes = cache.compare_snapshots("left_ws", "right_ws", "repo", "")
        # 应包含 util.new_func (added), main (callees_changed), util.helper (callers_changed)
        # main.init 也变化了：v1 中 main.init 调用 util.helper，v2 中不再调用
        qnames = {c["qualified_name"] for c in changes}
        assert "util.new_func" in qnames  # added
        assert "main" in qnames  # callees_changed (helper removed)
        # util.helper 也有变化（caller 变了），但文件没变所以不是 moved
        assert "util.helper" in qnames  # callers_changed
        # main.init 也变化了（callee delta: 失去 util.helper）
        assert "main.init" in qnames

    def test_compare_file_scope(self, two_version_dbs):
        """文件级 scope 应只返回该文件内的变化。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        changes = cache.compare_snapshots("left_ws", "right_ws", "file", "src/util.py")
        qnames = {c["qualified_name"] for c in changes}
        assert "util.new_func" in qnames  # added
        assert "util.helper" in qnames  # callers_changed
        # main 不在 src/util.py 中，不应出现在结果中
        assert "main" not in qnames

    def test_compare_module_scope(self, two_version_dbs):
        """模块级 scope 应只返回该模块内的变化。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        changes = cache.compare_snapshots("left_ws", "right_ws", "module", "app")
        qnames = {c["qualified_name"] for c in changes}
        # app 模块: main, main.init
        # main 有变化（callees_changed），main.init 也变化了（callee delta 变化）
        assert "main" in qnames
        assert "main.init" in qnames  # callee delta: 失去 util.helper
        # util.helper 不在 app 模块
        assert "util.helper" not in qnames

    def test_compare_empty_scope(self, two_version_dbs):
        """空文件路径的 scope 应返回空列表。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        changes = cache.compare_snapshots("left_ws", "right_ws", "file", "nonexistent.py")
        assert len(changes) == 0

    def test_compare_unchanged_snapshots(self, tmp_path):
        """完全相同的 snapshot 应返回空列表。"""
        from callwarden_core import PySnapshotCache
        v1 = tmp_path / "v1.db"
        v1b = tmp_path / "v1b.db"
        _make_db_v1(v1)
        _make_db_v1(v1b)
        cache = PySnapshotCache(8)
        _publish_both(cache, str(v1), str(v1b))
        changes = cache.compare_snapshots("left_ws", "right_ws", "repo", "")
        assert len(changes) == 0

    def test_compare_returns_list_of_dicts(self, two_version_dbs):
        """compare_snapshots 应返回 dict 列表，每个 dict 有完整字段。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        changes = cache.compare_snapshots("left_ws", "right_ws", "repo", "")
        assert len(changes) > 0
        for change in changes:
            assert "qualified_name" in change
            assert "change_kind" in change
            assert "signature_change" in change
            assert "caller_delta" in change
            assert "callee_delta" in change

    def test_compare_change_kinds_are_valid(self, two_version_dbs):
        """所有 change_kind 应是已知的字符串值。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        changes = cache.compare_snapshots("left_ws", "right_ws", "repo", "")
        valid_kinds = {"added", "removed", "moved", "signature_changed",
                       "callers_changed", "callees_changed", "unchanged", "ambiguous"}
        for change in changes:
            assert change["change_kind"] in valid_kinds, \
                f"invalid change_kind: {change['change_kind']}"


# ----------------------------------------------------------------------
# DaemonClient wrapper 测试
# ----------------------------------------------------------------------

class TestDaemonClientCompareSnapshots:
    def test_daemon_client_compare_snapshots(self, two_version_dbs):
        """DaemonClient.compare_snapshots 应返回包含 changes 的 dict。"""
        from callwarden_core import PySnapshotCache
        from callwarden.server.daemon_client import DaemonClient
        from callwarden.server.snapshot_manager import SnapshotManagerService

        v1, v2 = two_version_dbs
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService.get_instance()
        svc.publish_snapshot("left_ws", v1, "ctx_left")
        svc.publish_snapshot("right_ws", v2, "ctx_right")

        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        result = client.compare_snapshots("left_ws", "right_ws", "repo", "")
        assert result is not None
        assert "changes" in result
        assert "count" in result
        assert result["count"] == len(result["changes"])
        assert result["count"] > 0
        DaemonClient.reset_instance()
        SnapshotManagerService.reset_instance()

    def test_daemon_client_count_symbols_in_scope(self, two_version_dbs):
        """DaemonClient.count_symbols_in_scope 应返回符号数量。"""
        from callwarden_core import PySnapshotCache
        from callwarden.server.daemon_client import DaemonClient
        from callwarden.server.snapshot_manager import SnapshotManagerService

        v1, v2 = two_version_dbs
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService.get_instance()
        svc.publish_snapshot("left_ws", v1, "ctx_left")
        svc.publish_snapshot("right_ws", v2, "ctx_right")

        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        count = client.count_symbols_in_scope("left_ws", "right_ws", "repo", "")
        assert count == 4
        DaemonClient.reset_instance()
        SnapshotManagerService.reset_instance()

    def test_daemon_client_count_file_scope(self, two_version_dbs):
        """DaemonClient.count_symbols_in_scope 文件级 scope。"""
        from callwarden_core import PySnapshotCache
        from callwarden.server.daemon_client import DaemonClient
        from callwarden.server.snapshot_manager import SnapshotManagerService

        v1, v2 = two_version_dbs
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService.get_instance()
        svc.publish_snapshot("left_ws", v1, "ctx_left")
        svc.publish_snapshot("right_ws", v2, "ctx_right")

        DaemonClient.reset_instance()
        client = DaemonClient.get_instance()
        count = client.count_symbols_in_scope("left_ws", "right_ws", "file", "src/util.py")
        assert count == 2
        DaemonClient.reset_instance()
        SnapshotManagerService.reset_instance()


# ----------------------------------------------------------------------
# snapshot_diff_handler 后台 job 测试
# ----------------------------------------------------------------------

class TestSnapshotDiffHandler:
    def test_handler_returns_stats(self, two_version_dbs):
        """snapshot_diff_handler 应返回按 change_kind 分类的统计。"""
        from callwarden_core import PySnapshotCache
        from callwarden.server.snapshot_manager import SnapshotManagerService
        from callwarden.server.job_handlers import snapshot_diff_handler

        v1, v2 = two_version_dbs
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService.get_instance()
        svc.publish_snapshot("left_ws", v1, "ctx_left")
        svc.publish_snapshot("right_ws", v2, "ctx_right")

        ctx = MagicMock()
        ctx.params = {
            "left_workspace_id": "left_ws",
            "right_workspace_id": "right_ws",
            "scope_type": "repo",
            "scope_value": "",
        }
        ctx.update_progress = MagicMock()

        result = snapshot_diff_handler(ctx)

        assert "total_changes" in result
        assert result["total_changes"] > 0
        assert "added" in result
        assert "removed" in result
        assert "moved" in result
        assert "signature_changed" in result
        assert "callers_changed" in result
        assert "callees_changed" in result
        assert "ambiguous" in result
        # 应该有 added（util.new_func）
        assert result["added"] >= 1
        # 进度应被更新
        assert ctx.update_progress.call_count >= 2

        SnapshotManagerService.reset_instance()

    def test_handler_file_scope(self, two_version_dbs):
        """snapshot_diff_handler 文件级 scope。"""
        from callwarden_core import PySnapshotCache
        from callwarden.server.snapshot_manager import SnapshotManagerService
        from callwarden.server.job_handlers import snapshot_diff_handler

        v1, v2 = two_version_dbs
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService.get_instance()
        svc.publish_snapshot("left_ws", v1, "ctx_left")
        svc.publish_snapshot("right_ws", v2, "ctx_right")

        ctx = MagicMock()
        ctx.params = {
            "left_workspace_id": "left_ws",
            "right_workspace_id": "right_ws",
            "scope_type": "file",
            "scope_value": "src/util.py",
        }
        ctx.update_progress = MagicMock()

        result = snapshot_diff_handler(ctx)
        assert result["total_changes"] > 0
        # src/util.py 有 util.new_func (added) 和 util.helper (callers_changed)
        assert result["added"] >= 1

        SnapshotManagerService.reset_instance()

    def test_handler_unchanged(self, tmp_path):
        """snapshot_diff_handler 对完全相同的 snapshot 应返回 0 changes。"""
        from callwarden_core import PySnapshotCache
        from callwarden.server.snapshot_manager import SnapshotManagerService
        from callwarden.server.job_handlers import snapshot_diff_handler

        v1 = tmp_path / "v1.db"
        v1b = tmp_path / "v1b.db"
        _make_db_v1(v1)
        _make_db_v1(v1b)

        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService.get_instance()
        svc.publish_snapshot("left_ws", str(v1), "ctx_left")
        svc.publish_snapshot("right_ws", str(v1b), "ctx_right")

        ctx = MagicMock()
        ctx.params = {
            "left_workspace_id": "left_ws",
            "right_workspace_id": "right_ws",
            "scope_type": "repo",
            "scope_value": "",
        }
        ctx.update_progress = MagicMock()

        result = snapshot_diff_handler(ctx)
        assert result["total_changes"] == 0

        SnapshotManagerService.reset_instance()

    def test_handler_rust_unavailable(self):
        """Rust 不可用时 handler 应返回 error。"""
        from callwarden.server.job_handlers import snapshot_diff_handler

        ctx = MagicMock()
        ctx.params = {
            "left_workspace_id": "left_ws",
            "right_workspace_id": "right_ws",
            "scope_type": "repo",
            "scope_value": "",
        }
        ctx.update_progress = MagicMock()

        with patch("callwarden.server.snapshot_manager.get_snapshot_service") as mock_svc:
            mock_svc_obj = MagicMock()
            mock_svc_obj.rust_available = False
            mock_svc.return_value = mock_svc_obj

            result = snapshot_diff_handler(ctx)
            assert "error" in result
            assert result["total_changes"] == 0


# ----------------------------------------------------------------------
# 同步/异步边界测试
# ----------------------------------------------------------------------

class TestSyncAsyncBoundary:
    def test_small_scope_sync(self, two_version_dbs):
        """小 scope（file）应走同步路径直接返回结果。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)

        # 文件级 scope，符号数 < 500，走同步
        count = cache.count_symbols_in_scope("left_ws", "right_ws", "file", "src/util.py")
        assert count < 500  # 小 scope

        changes = cache.compare_snapshots("left_ws", "right_ws", "file", "src/util.py")
        assert len(changes) > 0

    def test_scope_threshold_logic(self, two_version_dbs):
        """验证 scope 阈值逻辑：超阈值时应建议走后台 job。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)

        SYNC_THRESHOLD = 500

        # 文件级 scope，远小于阈值
        file_count = cache.count_symbols_in_scope("left_ws", "right_ws", "file", "src/util.py")
        assert file_count < SYNC_THRESHOLD
        # 应走同步
        file_changes = cache.compare_snapshots("left_ws", "right_ws", "file", "src/util.py")
        assert len(file_changes) > 0

        # 仓库级 scope，也小于阈值（测试 DB 很小）
        repo_count = cache.count_symbols_in_scope("left_ws", "right_ws", "repo", "")
        assert repo_count < SYNC_THRESHOLD
        # 应走同步
        repo_changes = cache.compare_snapshots("left_ws", "right_ws", "repo", "")
        assert len(repo_changes) > 0


# ----------------------------------------------------------------------
# 错误处理
# ----------------------------------------------------------------------

class TestCompareSnapshotsErrors:
    def test_workspace_not_in_cache(self):
        """workspace 不在 cache 中应抛异常。"""
        from callwarden_core import PySnapshotCache
        cache = PySnapshotCache(8)
        with pytest.raises(Exception):
            cache.compare_snapshots("left_ws", "right_ws", "repo", "")

    def test_workspace_no_snapshot(self, two_version_dbs):
        """workspace 已注册但未发布 snapshot 应抛异常。"""
        from callwarden_core import PySnapshotCache
        v1, _ = two_version_dbs
        cache = PySnapshotCache(8)
        cache.get_or_create("left_ws").build_and_publish(v1, "ctx", None)
        cache.get_or_create("right_ws")  # 注册但未 publish
        with pytest.raises(Exception):
            cache.compare_snapshots("left_ws", "right_ws", "repo", "")

    def test_count_workspace_not_in_cache(self):
        """count_symbols_in_scope workspace 不在 cache 中应抛异常。"""
        from callwarden_core import PySnapshotCache
        cache = PySnapshotCache(8)
        with pytest.raises(Exception):
            cache.count_symbols_in_scope("left_ws", "right_ws", "repo", "")
