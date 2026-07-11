"""Phase 4.8 单元测试：diff_callers / diff_callees

测试范围：
- diff_callers：基于 resolved edge delta 查询两个 snapshot 间的 caller 增删
- diff_callees：基于 resolved edge delta 查询两个 snapshot 间的 callee 增删
- 符号不存在时返回 None
- 一侧 workspace 未注册/未发布 snapshot 时的错误处理
- 返回结构完整性（added_callers/removed_callers/added_callees/removed_callees 字段）
- 与 diff_symbol 的一致性（diff_callers + diff_callees 应与 diff_symbol 的 edge delta 一致）

设计参考：enterprise-daemon-shared-snapshot-plan.md §12.3 Query API
"""

import sqlite3
import pytest

# 跳过条件：callwarden_core 未安装时跳过
callwarden_core = pytest.importorskip("callwarden_core")


# ----------------------------------------------------------------------
# 测试 fixture：构造两个不同版本的 callwarden.db
# ----------------------------------------------------------------------

def _make_db_v1(db_path):
    """版本 1：main 调用 init 和 helper，helper 在 src/util.py
    调用图：
        main → main.init
        main.init → util.helper
        main → util.helper
    """
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
    """版本 2：main 不再调用 helper，新增 util.new_func 调用 helper
    调用图：
        main → main.init          (helper 调用被移除)
        util.new_func → util.helper  (新 caller)
    """
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
        (3, 2, 'fn', 'helper', 'util.helper', '', 1, 5, 1),
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
    """版本 3：与 v1 完全相同（用于测试无变化）"""
    _make_db_v1(db_path)


def _make_db_v5_callee_added(db_path):
    """版本 5：main.init 新增调用 util.helper（v1 中 main.init 已调用 helper，这里改为 main.init 还调用 new_func）
    实际上为了测试 callee 增加的场景，构造 main.init 在 v5 中新增调用 new_func
    调用图：
        main → main.init
        main.init → util.helper
        main.init → util.new_func  (新增 callee)
        main → util.helper
    """
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
        (3, 2, 'fn', 'helper', 'util.helper', '', 1, 5, 1),
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
        (2, 3, 'helper', 7, 1),
        (2, 4, 'new_func', 8, 1),
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
def callee_added_dbs(tmp_path):
    """构造 v1 和 v5（main.init 新增 callee new_func）两个版本的 db。"""
    v1 = tmp_path / "v1.db"
    v5 = tmp_path / "v5.db"
    _make_db_v1(v1)
    _make_db_v5_callee_added(v5)
    return str(v1), str(v5)


def _publish_both(cache, left_db, right_db):
    """在 cache 中发布两个 workspace 的 snapshot。"""
    left_mgr = cache.get_or_create("left_ws")
    left_mgr.build_and_publish(left_db, "ctx_left", None)
    right_mgr = cache.get_or_create("right_ws")
    right_mgr.build_and_publish(right_db, "ctx_right", None)
    return cache


# ----------------------------------------------------------------------
# diff_callers 基础测试
# ----------------------------------------------------------------------

class TestDiffCallersBasic:
    def test_callers_unchanged(self, unchanged_dbs):
        """完全相同的版本，caller 集合应无变化。"""
        from callwarden_core import PySnapshotCache
        v1, v3 = unchanged_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v3)
        result = cache.diff_callers("left_ws", "right_ws", "util.helper")
        assert result is not None
        assert result["added_callers"] == []
        assert result["removed_callers"] == []
        # callee 字段应为空
        assert result["added_callees"] == []
        assert result["removed_callees"] == []

    def test_callers_changed_helper(self, two_version_dbs):
        """util.helper 的 caller 集合变化
        v1: main, main.init 调用 helper
        v2: util.new_func 调用 helper
        added: util.new_func, removed: main, main.init
        """
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callers("left_ws", "right_ws", "util.helper")
        assert result is not None
        added = set(result["added_callers"])
        removed = set(result["removed_callers"])
        assert "util.new_func" in added
        assert "main" in removed
        assert "main.init" in removed

    def test_callers_none_for_nonexistent(self, two_version_dbs):
        """符号在两侧都不存在时返回 None。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callers("left_ws", "right_ws", "nonexistent.func")
        assert result is None

    def test_callers_none_for_added_symbol(self, two_version_dbs):
        """符号仅在右侧存在（added）时返回 None（无法对比 caller delta）。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callers("left_ws", "right_ws", "util.new_func")
        assert result is None

    def test_callers_none_for_removed_symbol(self, two_version_dbs):
        """符号仅在左侧存在（removed）时返回 None。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v2, v1)
        result = cache.diff_callers("left_ws", "right_ws", "util.new_func")
        assert result is None

    def test_callers_for_main_unchanged_callers(self, two_version_dbs):
        """main 没有 caller（顶层函数），两侧都应无 caller 变化。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callers("left_ws", "right_ws", "main")
        assert result is not None
        assert result["added_callers"] == []
        assert result["removed_callers"] == []


# ----------------------------------------------------------------------
# diff_callees 基础测试
# ----------------------------------------------------------------------

class TestDiffCalleesBasic:
    def test_callees_unchanged(self, unchanged_dbs):
        """完全相同的版本，callee 集合应无变化。"""
        from callwarden_core import PySnapshotCache
        v1, v3 = unchanged_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v3)
        result = cache.diff_callees("left_ws", "right_ws", "main")
        assert result is not None
        assert result["added_callees"] == []
        assert result["removed_callees"] == []
        # caller 字段应为空
        assert result["added_callers"] == []
        assert result["removed_callers"] == []

    def test_callees_changed_main(self, two_version_dbs):
        """main 的 callee 集合变化
        v1: main 调用 init, helper
        v2: main 只调用 init（helper 被移除）
        removed: util.helper
        """
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callees("left_ws", "right_ws", "main")
        assert result is not None
        assert "util.helper" in result["removed_callees"]
        assert result["added_callees"] == []

    def test_callees_added_for_init(self, callee_added_dbs):
        """main.init 新增 callee new_func
        v1: main.init 调用 helper
        v5: main.init 调用 helper, new_func
        added: util.new_func
        """
        from callwarden_core import PySnapshotCache
        v1, v5 = callee_added_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v5)
        result = cache.diff_callees("left_ws", "right_ws", "main.init")
        assert result is not None
        assert "util.new_func" in result["added_callees"]
        assert result["removed_callees"] == []

    def test_callees_none_for_nonexistent(self, two_version_dbs):
        """符号在两侧都不存在时返回 None。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callees("left_ws", "right_ws", "nonexistent.func")
        assert result is None

    def test_callees_none_for_added_symbol(self, two_version_dbs):
        """符号仅在右侧存在（added）时返回 None。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callees("left_ws", "right_ws", "util.new_func")
        assert result is None

    def test_callees_for_helper_no_change(self, two_version_dbs):
        """util.helper 没有 callee（叶子函数），两侧都应无 callee 变化。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callees("left_ws", "right_ws", "util.helper")
        assert result is not None
        assert result["added_callees"] == []
        assert result["removed_callees"] == []


# ----------------------------------------------------------------------
# 错误处理
# ----------------------------------------------------------------------

class TestDiffCallersCalleesErrors:
    def test_callers_workspace_not_in_cache(self, two_version_dbs):
        """workspace 不在 cache 中应抛异常。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        cache.get_or_create("left_ws").build_and_publish(v1, "ctx", None)
        # right_ws 未注册
        with pytest.raises(Exception):
            cache.diff_callers("left_ws", "right_ws", "main")

    def test_callees_workspace_not_in_cache(self, two_version_dbs):
        """workspace 不在 cache 中应抛异常。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        cache.get_or_create("left_ws").build_and_publish(v1, "ctx", None)
        with pytest.raises(Exception):
            cache.diff_callees("left_ws", "right_ws", "main")

    def test_callers_workspace_no_snapshot(self, two_version_dbs):
        """workspace 已注册但未发布 snapshot 应抛异常。"""
        from callwarden_core import PySnapshotCache
        v1, _ = two_version_dbs
        cache = PySnapshotCache(8)
        cache.get_or_create("left_ws").build_and_publish(v1, "ctx", None)
        cache.get_or_create("right_ws")  # 注册但未 publish
        with pytest.raises(Exception):
            cache.diff_callers("left_ws", "right_ws", "main")

    def test_callees_workspace_no_snapshot(self, two_version_dbs):
        """workspace 已注册但未发布 snapshot 应抛异常。"""
        from callwarden_core import PySnapshotCache
        v1, _ = two_version_dbs
        cache = PySnapshotCache(8)
        cache.get_or_create("left_ws").build_and_publish(v1, "ctx", None)
        cache.get_or_create("right_ws")
        with pytest.raises(Exception):
            cache.diff_callees("left_ws", "right_ws", "main")


# ----------------------------------------------------------------------
# 返回结构完整性
# ----------------------------------------------------------------------

class TestDiffCallersCalleesStructure:
    def test_callers_result_has_all_fields(self, two_version_dbs):
        """diff_callers 返回的 dict 应包含所有 4 个字段。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callers("left_ws", "right_ws", "util.helper")
        assert result is not None
        for field in ["added_callers", "removed_callers",
                      "added_callees", "removed_callees"]:
            assert field in result, f"missing field: {field}"

    def test_callees_result_has_all_fields(self, two_version_dbs):
        """diff_callees 返回的 dict 应包含所有 4 个字段。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callees("left_ws", "right_ws", "main")
        assert result is not None
        for field in ["added_callers", "removed_callers",
                      "added_callees", "removed_callees"]:
            assert field in result, f"missing field: {field}"

    def test_callers_callee_fields_empty(self, two_version_dbs):
        """diff_callers 返回的 added_callees / removed_callees 应为空列表。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callers("left_ws", "right_ws", "util.helper")
        assert result is not None
        assert result["added_callees"] == []
        assert result["removed_callees"] == []

    def test_callees_caller_fields_empty(self, two_version_dbs):
        """diff_callees 返回的 added_callers / removed_callers 应为空列表。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        result = cache.diff_callees("left_ws", "right_ws", "main")
        assert result is not None
        assert result["added_callers"] == []
        assert result["removed_callers"] == []


# ----------------------------------------------------------------------
# 与 diff_symbol 的一致性
# ----------------------------------------------------------------------

class TestConsistencyWithDiffSymbol:
    def test_callers_matches_diff_symbol(self, two_version_dbs):
        """diff_callers 的结果应与 diff_symbol 中的 caller_delta 一致。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        symbol_result = cache.diff_symbol("left_ws", "right_ws", "util.helper")
        callers_result = cache.diff_callers("left_ws", "right_ws", "util.helper")
        assert callers_result is not None
        symbol_caller_delta = symbol_result["caller_delta"]
        assert set(callers_result["added_callers"]) == set(symbol_caller_delta["added_callers"])
        assert set(callers_result["removed_callers"]) == set(symbol_caller_delta["removed_callers"])

    def test_callees_matches_diff_symbol(self, two_version_dbs):
        """diff_callees 的结果应与 diff_symbol 中的 callee_delta 一致。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        symbol_result = cache.diff_symbol("left_ws", "right_ws", "main")
        callees_result = cache.diff_callees("left_ws", "right_ws", "main")
        assert callees_result is not None
        symbol_callee_delta = symbol_result["callee_delta"]
        assert set(callees_result["added_callees"]) == set(symbol_callee_delta["added_callees"])
        assert set(callees_result["removed_callees"]) == set(symbol_callee_delta["removed_callees"])

    def test_callers_plus_callees_equals_symbol_edges(self, two_version_dbs):
        """diff_callers + diff_callees 的并集应等于 diff_symbol 的全部 edge delta。"""
        from callwarden_core import PySnapshotCache
        v1, v2 = two_version_dbs
        cache = PySnapshotCache(8)
        _publish_both(cache, v1, v2)
        symbol_result = cache.diff_symbol("left_ws", "right_ws", "util.helper")
        callers_result = cache.diff_callers("left_ws", "right_ws", "util.helper")
        callees_result = cache.diff_callees("left_ws", "right_ws", "util.helper")
        assert callers_result is not None
        assert callees_result is not None

        # caller delta 一致
        symbol_caller = symbol_result["caller_delta"]
        assert set(callers_result["added_callers"]) == set(symbol_caller["added_callers"])
        assert set(callers_result["removed_callers"]) == set(symbol_caller["removed_callers"])

        # callee delta 一致
        symbol_callee = symbol_result["callee_delta"]
        assert set(callees_result["added_callees"]) == set(symbol_callee["added_callees"])
        assert set(callees_result["removed_callees"]) == set(symbol_callee["removed_callees"])
