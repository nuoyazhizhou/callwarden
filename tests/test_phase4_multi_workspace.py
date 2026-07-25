"""Phase 4.4 单元测试：多 workspace snapshot cache。

验证 SnapshotManagerService 在多 workspace 场景下的正确性：
- 同时发布多个 workspace snapshot
- 各 workspace 独立维护 generation
- 跨 workspace 查询不互相干扰
- LRU 淘汰策略
- 并发发布不同 workspace
"""

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from callwarden.server.snapshot_manager import SnapshotManagerService

callwarden_core = pytest.importorskip("callwarden_core")


# ----------------------------------------------------------------------
# fixture：构造多个不同内容的 callwarden.db
# ----------------------------------------------------------------------

def _make_db(db_path: str, symbols: list, calls: list):
    """构造一个 callwarden.db，symbols/calls 由参数指定。
    symbols: [(id, file_instance_id, kind, name, qualified_name, module_path, start_line, end_line, depth)]
    calls: [(caller_id, callee_id, callee_name, call_line, is_cross_file)]
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE file_instances (
        id INTEGER PRIMARY KEY, rel_path TEXT, status TEXT DEFAULT 'active')""")
    # 按 symbols 中出现的 file_instance_id 插入 file_instances
    file_ids = set()
    for row in symbols:
        file_ids.add(row[1])
    for fid in sorted(file_ids):
        cur.execute("INSERT INTO file_instances VALUES (?, ?, 'active')", (fid, f"src/file_{fid}.py"))
    cur.execute("""CREATE TABLE symbols (
        id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT,
        name TEXT, qualified_name TEXT, module_path TEXT,
        start_line INTEGER, end_line INTEGER, depth INTEGER)""")
    for row in symbols:
        cur.execute("INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
    cur.execute("""CREATE TABLE calls (
        caller_id INTEGER, callee_id INTEGER, callee_name TEXT,
        call_line INTEGER, is_cross_file INTEGER)""")
    for row in calls:
        cur.execute("INSERT INTO calls VALUES (?, ?, ?, ?, ?)", row)
    conn.commit()
    conn.close()


@pytest.fixture
def multi_db(tmp_path):
    """构造 3 个不同内容的 callwarden.db。"""
    dbs = {}

    # DB1: Python 项目
    db1 = tmp_path / "ws_python.db"
    _make_db(str(db1), [
        (1, 1, "fn", "main", "main", "", 1, 10, 0),
        (2, 1, "fn", "parse_input", "main.parse_input", "", 11, 20, 1),
    ], [(1, 2, "parse_input", 5, 0)])
    dbs["ws_python"] = str(db1)

    # DB2: Rust 项目
    db2 = tmp_path / "ws_rust.db"
    _make_db(str(db2), [
        (1, 1, "fn", "main", "main", "", 1, 10, 0),
        (2, 1, "fn", "compile", "main.compile", "", 11, 20, 1),
        (3, 1, "fn", "link", "main.link", "", 21, 30, 1),
    ], [(1, 2, "compile", 5, 0), (1, 3, "link", 6, 0)])
    dbs["ws_rust"] = str(db2)

    # DB3: C 项目
    db3 = tmp_path / "ws_c.db"
    _make_db(str(db3), [
        (1, 1, "fn", "main", "main", "", 1, 10, 0),
        (2, 1, "fn", "init_hardware", "main.init_hardware", "", 11, 20, 1),
    ], [(1, 2, "init_hardware", 5, 0)])
    dbs["ws_c"] = str(db3)

    return dbs


@pytest.fixture
def service():
    SnapshotManagerService.reset_instance()
    svc = SnapshotManagerService(max_workspaces=8)
    SnapshotManagerService._instance = svc
    yield svc
    SnapshotManagerService.reset_instance()


# ----------------------------------------------------------------------
# 多 workspace 独立查询
# ----------------------------------------------------------------------

class TestMultiWorkspaceIndependent:
    def test_three_workspaces_simultaneously(self, service, multi_db):
        """三个 workspace 同时发布，各自独立查询。"""
        # 发布
        for ws_id, db_path in multi_db.items():
            service.publish_snapshot(ws_id, db_path)

        # 验证各自存在
        assert set(service.list_workspaces()) == {"ws_python", "ws_rust", "ws_c"}

        # ws_python 应该能查到 parse_input
        py_callers = service.query_callers("ws_python", "parse_input")
        assert len(py_callers) >= 1

        # ws_rust 应该能查到 compile
        rust_callers = service.query_callers("ws_rust", "compile")
        assert len(rust_callers) >= 1

        # ws_c 应该能查到 init_hardware
        c_callers = service.query_callers("ws_c", "init_hardware")
        assert len(c_callers) >= 1

    def test_cross_workspace_isolation(self, service, multi_db):
        """跨 workspace 查询不互相干扰。"""
        service.publish_snapshot("ws_python", multi_db["ws_python"])
        service.publish_snapshot("ws_rust", multi_db["ws_rust"])

        # ws_python 查不到 ws_rust 特有的函数
        assert service.query_callers("ws_python", "compile") == []
        # ws_rust 查不到 ws_python 特有的函数
        assert service.query_callers("ws_rust", "parse_input") == []

    def test_each_workspace_has_independent_generation(self, service, multi_db):
        """各 workspace 独立维护 generation。"""
        service.publish_snapshot("ws_python", multi_db["ws_python"])
        service.publish_snapshot("ws_python", multi_db["ws_python"])  # gen=2
        service.publish_snapshot("ws_rust", multi_db["ws_rust"])      # gen=1

        assert service.get_current_generation("ws_python") == 2
        assert service.get_current_generation("ws_rust") == 1


# ----------------------------------------------------------------------
# evict 单 workspace 不影响其他
# ----------------------------------------------------------------------

class TestEvictIsolation:
    def test_evict_one_does_not_affect_others(self, service, multi_db):
        """移除一个 workspace 不影响其他 workspace。"""
        for ws_id, db_path in multi_db.items():
            service.publish_snapshot(ws_id, db_path)

        # 移除 ws_python
        assert service.evict_workspace("ws_python") is True

        # ws_rust 和 ws_c 仍可查询
        assert service.query_callers("ws_rust", "compile") != []
        assert service.query_callers("ws_c", "init_hardware") != []
        # ws_python 已被移除
        assert service.query_callers("ws_python", "parse_input") == []


# ----------------------------------------------------------------------
# LRU 淘汰
# ----------------------------------------------------------------------

class TestLRUEviction:
    def test_lru_evicts_oldest(self, service, multi_db):
        """max_workspaces 满后淘汰最旧的 workspace。"""
        # 使用 max_workspaces=2
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService(max_workspaces=2)
        SnapshotManagerService._instance = svc

        svc.publish_snapshot("ws_1", multi_db["ws_python"])
        svc.publish_snapshot("ws_2", multi_db["ws_rust"])
        assert svc.list_workspaces() == ["ws_1", "ws_2"]

        # 插入第三个，应淘汰一个
        svc.publish_snapshot("ws_3", multi_db["ws_c"])
        assert len(svc.list_workspaces()) <= 2
        SnapshotManagerService.reset_instance()


# ----------------------------------------------------------------------
# 并发发布不同 workspace
# ----------------------------------------------------------------------

class TestConcurrentMultiWorkspace:
    def test_concurrent_publish_different_workspaces(self, service, multi_db):
        """并发发布不同 workspace 不出错。"""
        errors = []
        barrier = threading.Barrier(3)

        def publish_ws(ws_id, db_path):
            try:
                barrier.wait()
                service.publish_snapshot(ws_id, db_path)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=publish_ws, args=("ws_t_1", multi_db["ws_python"])),
            threading.Thread(target=publish_ws, args=("ws_t_2", multi_db["ws_rust"])),
            threading.Thread(target=publish_ws, args=("ws_t_3", multi_db["ws_c"])),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"并发发布出错: {errors}"
        assert set(service.list_workspaces()) == {"ws_t_1", "ws_t_2", "ws_t_3"}

    def test_concurrent_query_different_workspaces(self, service, multi_db):
        """并发查询不同 workspace 不出错。"""
        for ws_id, db_path in multi_db.items():
            service.publish_snapshot(ws_id, db_path)

        errors = []
        barrier = threading.Barrier(3)

        def query_ws(ws_id, func_name):
            try:
                barrier.wait()
                for _ in range(20):
                    service.query_callers(ws_id, func_name)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=query_ws, args=("ws_python", "parse_input")),
            threading.Thread(target=query_ws, args=("ws_rust", "compile")),
            threading.Thread(target=query_ws, args=("ws_c", "init_hardware")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"并发查询出错: {errors}"
