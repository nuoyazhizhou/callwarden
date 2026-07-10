"""Phase 4.7 单元测试：diff_symbol / diff_signature

测试范围：
- SymbolChangeKind 各分支（added / removed / moved / signature_changed / callers_changed / callees_changed / unchanged / ambiguous）
- SignatureDiff 字段（file_changed / line_range_changed / kind_changed）
- EdgeDeltaSummary（added_callers / removed_callers / added_callees / removed_callees）
- diff_signature 独立调用
- 两侧都不存在时返回 ambiguous
- 一侧未发布 snapshot 时的错误处理
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# 跳过条件：callwarden_core 未安装时跳过
callwarden_core = pytest.importorskip("callwarden_core")


# ----------------------------------------------------------------------
# 测试 fixture：构造两个不同版本的 callwarden.db
# ----------------------------------------------------------------------

def _make_db_v1(db_path):
    """版本 1：main 调用 init 和 helper，helper 在 src/util.py"""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY,
            rel_path TEXT,
            status TEXT DEFAULT 'active'
        )
    """)
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (1, 'src/main.py')")
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (2, 'src/util.py')")

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


def _make_db_v2(db_path):
    """版本 2：main 不再调用 helper，helper 移到 src/helpers/util.py，行号变化"""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY,
            rel_path TEXT,
            status TEXT DEFAULT 'active'
        )
    """)
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (1, 'src/main.py')")
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (2, 'src/helpers/util.py')")

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
        (3, 2, 'fn', 'helper', 'util.helper', '', 11, 20, 1),
        (4, 2, 'fn', 'new_func', 'util.new_func', '', 1, 5, 1)
    """)

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
        (4, 3, 'helper', 3, 0)
    """)
    conn.commit()
    conn.close()


def _make_db_v3_unchanged(db_path):
    """版本 3：与 v1 完全相同（用于测试 unchanged）"""
    _make_db_v1(db_path)


def _make_db_v4_signature_only(db_path):
    """版本 4：helper 行号变化但文件不变"""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY,
            rel_path TEXT,
            status TEXT DEFAULT 'active'
        )
    """)
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (1, 'src/main.py')")
    cur.execute("INSERT INTO file_instances (id, rel_path) VALUES (2, 'src/util.py')")

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
        (3, 2, 'fn', 'helper', 'util.helper', '', 15, 30, 1)
    """)

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


@pytest.fixture
def two_version_dbs(tmp_path):
    """构造 v1 和 v2 两个版本的 db。"""
    v1 = tmp_path / "v1.db"
    v2 = tmp_path / "v2.db"
    _make_db_v1(v1)
    _make_db_v2(v2)
    return str(v1), str(v2)


@pytest.fixture
def unchanged_dbs(tmp_path):
    """构造 v1 和 v3(=v1) 两个版本的 db。"""
    v1 = tmp_path / "v1.db"
    v3 = tmp_path / "v3.db"
    _make_db_v1(v1)
    _make_db_v3_unchanged(v3)
    return str(v1), str(v3)


@pytest.fixture
def signature_only_dbs(tmp_path):
    """构造 v1 和 v4（仅行号变化）两个版本的 db。"""
    v1 = tmp_path / "v1.db"
    v4 = tmp_path / "v4.db"
    _make_db_v1(v1)
    _make_db_v4_signature_only(v4)
    return str(v1), str(v4)


def _publish_both(cache, left_db, right_db):
    """在 cache 中发布两个 workspace 的 snapshot。"""
    left_mgr = cache.get_or_create("left_ws")
    left_mgr.build_and_publish(left_db, "ctx_left", None)
    right_mgr = cache.get_or_create("right_ws")
    right_mgr.build_and_publish(right_db, "ctx_right", None)
    return cache


# ----------------------------------------------------------------------
# diff_symbol 基础测试
# ----------------------------------------------------------------------

class TestDiffSymbolBasic:
    def test_unchanged_symbol(self, unchanged_dbs):
        """完全相同的版本，符号应 unchanged。"""
        from callwarden_core import PySnapshotCache
        v1, v3 = unchanged_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v3)
        result = cache.diff_symbol("left_ws", "right_ws", "util.helper")
        assert result["change_kind"] == "unchanged"
        assert result["qualified_name"] == "util.helper"

    def test_ambiguous_when_not_in_either(self, two_version_dbs):
        """两侧都不存在该符号时返回 ambiguous。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_symbol("left_ws", "right_ws", "nonexistent.func")
        assert result["change_kind"] == "ambiguous"

    def test_added_symbol(self, two_version_dbs):
        """仅在右版本存在的符号应为 added。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_symbol("left_ws", "right_ws", "util.new_func")
        assert result["change_kind"] == "added"

    def test_removed_symbol(self, two_version_dbs):
        """仅在左版本存在的符号应为 removed。
        构造：左版本有 util.new_func，右版本没有。
        """
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        # 交换 left/right：v2 有 new_func，v1 没有 → v1 为左时 new_func 是 added
        # 要测 removed，需要 v2 为左（有 new_func），v1 为右（没有）
        cache = PySnapshotCache(8)
        _publish_both(cache, v2, v1)
        result = cache.diff_symbol("left_ws", "right_ws", "util.new_func")
        assert result["change_kind"] == "removed"


# ----------------------------------------------------------------------
# diff_symbol 签名变化检测
# ----------------------------------------------------------------------

class TestDiffSignatureChanged:
    def test_moved_symbol_file_changed(self, two_version_dbs):
        """util.helper 从 src/util.py 移到 src/helpers/util.py → moved"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_symbol("left_ws", "right_ws", "util.helper")
        # 文件路径变化 → moved
        assert result["change_kind"] == "moved"
        sig = result["signature_change"]
        assert sig["file_changed"] is True
        assert "src/util.py" in sig["left_file"]
        assert "src/helpers/util.py" in sig["right_file"]

    def test_signature_changed_line_range(self, signature_only_dbs):
        """行号范围变化但文件不变 → signature_changed"""
        from callwarden_core import PySnapshotCache
        v1, v4 = signature_only_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v4)
        result = cache.diff_symbol("left_ws", "right_ws", "util.helper")
        assert result["change_kind"] == "signature_changed"
        sig = result["signature_change"]
        assert sig["file_changed"] is False
        assert sig["line_range_changed"] is True
        assert sig["left_start_line"] == 1
        assert sig["right_start_line"] == 15


# ----------------------------------------------------------------------
# diff_symbol 边变化检测
# ----------------------------------------------------------------------

class TestDiffEdgeChanged:
    def test_callers_changed(self, two_version_dbs):
        """util.helper 的 caller 集合变化 → callers_changed
        v1: main 和 main.init 都调用 helper
        v2: 只有 util.new_func 调用 helper
        由于文件也变了，change_kind 会是 moved，但 caller_delta 应有变化。
        """
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_symbol("left_ws", "right_ws", "util.helper")
        # 文件变化优先级最高 → moved
        assert result["change_kind"] == "moved"
        # 但 caller_delta 应有变化
        caller_delta = result["caller_delta"]
        # v1 的 callers: main, main.init
        # v2 的 callers: util.new_func
        # added: util.new_func, removed: main, main.init
        assert len(caller_delta["added_callers"]) > 0 or len(caller_delta["removed_callers"]) > 0

    def test_callees_changed(self, two_version_dbs):
        """main 的 callee 集合变化
        v1: main 调用 init, helper
        v2: main 只调用 init（不再调用 helper）
        文件路径相同 → 不应该是 moved
        """
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_symbol("left_ws", "right_ws", "main")
        sig = result["signature_change"]
        assert sig["file_changed"] is False
        # main 的 callee 变了（helper 被移除）
        callee_delta = result["callee_delta"]
        assert "util.helper" in callee_delta["removed_callees"]


# ----------------------------------------------------------------------
# diff_signature 独立调用
# ----------------------------------------------------------------------

class TestDiffSignatureStandalone:
    def test_diff_signature_returns_none_for_nonexistent(self, two_version_dbs):
        """符号不存在时 diff_signature 返回 None。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_signature("left_ws", "right_ws", "nonexistent.func")
        assert result is None

    def test_diff_signature_returns_diff(self, signature_only_dbs):
        """签名变化时 diff_signature 返回完整 diff。"""
        from callwarden_core import PySnapshotCache
        v1, v4 = signature_only_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v4)
        result = cache.diff_signature("left_ws", "right_ws", "util.helper")
        assert result is not None
        assert result["file_changed"] is False
        assert result["line_range_changed"] is True
        assert result["kind_changed"] is False

    def test_diff_signature_unchanged(self, unchanged_dbs):
        """完全相同时 diff_signature 返回 file_changed=False。"""
        from callwarden_core import PySnapshotCache
        v1, v3 = unchanged_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v3)
        result = cache.diff_signature("left_ws", "right_ws", "util.helper")
        assert result is not None
        assert result["file_changed"] is False
        assert result["line_range_changed"] is False
        assert result["kind_changed"] is False


# ----------------------------------------------------------------------
# 错误处理
# ----------------------------------------------------------------------

class TestDiffErrors:
    def test_workspace_not_in_cache(self, two_version_dbs):
        """workspace 不在 cache 中应抛异常。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        cache.get_or_create("left_ws").build_and_publish(v1, "ctx", None)
        # right_ws 未注册
        with pytest.raises(Exception):
            cache.diff_symbol("left_ws", "right_ws", "main")

    def test_workspace_no_snapshot(self, two_version_dbs):
        """workspace 已注册但未发布 snapshot 应抛异常。"""
        from callwarden_core import PySnapshotCache
        v1, _ = two_version_dbs
        cache = PySnapshotCache(8)
        cache.get_or_create("left_ws").build_and_publish(v1, "ctx", None)
        cache.get_or_create("right_ws")  # 注册但未 publish
        with pytest.raises(Exception):
            cache.diff_symbol("left_ws", "right_ws", "main")


# ----------------------------------------------------------------------
# diff 结果结构完整性
# ----------------------------------------------------------------------

class TestDiffResultStructure:
    def test_result_has_all_fields(self, two_version_dbs):
        """diff_symbol 返回的 dict 应包含所有字段。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_symbol("left_ws", "right_ws", "main")
        assert "qualified_name" in result
        assert "change_kind" in result
        assert "signature_change" in result
        assert "caller_delta" in result
        assert "callee_delta" in result

    def test_signature_change_has_all_fields(self, two_version_dbs):
        """signature_change 子 dict 包含所有字段。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_symbol("left_ws", "right_ws", "main")
        sig = result["signature_change"]
        for field in ["left_file", "right_file", "file_changed",
                       "left_start_line", "left_end_line",
                       "right_start_line", "right_end_line",
                       "line_range_changed", "kind_changed"]:
            assert field in sig, f"missing field: {field}"

    def test_edge_delta_has_all_fields(self, two_version_dbs):
        """caller_delta / callee_delta 子 dict 包含所有字段。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_symbol("left_ws", "right_ws", "main")
        for delta_key in ["caller_delta", "callee_delta"]:
            delta = result[delta_key]
            for field in ["added_callers", "removed_callers",
                          "added_callees", "removed_callees"]:
                assert field in delta, f"missing field in {delta_key}: {field}"

    def test_change_kind_values(self, two_version_dbs):
        """change_kind 应是已知的字符串值。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        valid_kinds = {"added", "removed", "moved", "signature_changed",
                       "callers_changed", "callees_changed", "unchanged", "ambiguous"}
        for qname in ["main", "main.init", "util.helper", "util.new_func", "nonexistent"]:
            result = cache.diff_symbol("left_ws", "right_ws", qname)
            assert result["change_kind"] in valid_kinds, \
                f"invalid change_kind for {qname}: {result['change_kind']}"
