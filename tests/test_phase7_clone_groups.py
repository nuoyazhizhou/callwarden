"""
Phase 7.0: Clone Groups 存储测试

测试 db_clone_groups.py 的 group 存储、查询、清理。
"""

import os
import sys
import sqlite3
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# 直接导入模块，避免 db/__init__.py 的相对导入链
import importlib.util


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # Python 3.14+ dataclass 需要
    spec.loader.exec_module(mod)
    return mod


_cg_path = Path(__file__).parent.parent / "db" / "db_clone_groups.py"
_cg_mod = _load_module("db_clone_groups", str(_cg_path))

init_clone_groups_schema = _cg_mod.init_clone_groups_schema
compute_group_hash = _cg_mod.compute_group_hash
store_clone_groups = _cg_mod.store_clone_groups
clear_clone_groups = _cg_mod.clear_clone_groups
list_clone_groups = _cg_mod.list_clone_groups
get_clone_group_members = _cg_mod.get_clone_group_members
get_clone_group_detail = _cg_mod.get_clone_group_detail
get_clone_group_stats = _cg_mod.get_clone_group_stats

CloneGroup = _cg_mod.CloneGroup


# ============================================
# Fixtures
# ============================================

@pytest.fixture
def db_conn():
    """创建一个临时 SQLite 连接，含 clone_groups schema 和依赖表"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # 创建 workspaces 表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            root_path TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL,
            is_active INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            active_task_id TEXT DEFAULT ''
        )
    """)
    conn.execute(
        "INSERT INTO workspaces (name, root_path, created_at, is_active) VALUES (?, ?, ?, 1)",
        ("test-ws", "/tmp/test", time.time()),
    )
    ws_id = conn.execute("SELECT id FROM workspaces WHERE is_active = 1").fetchone()["id"]

    # 创建 file_instances 表（symbols 外键依赖）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL,
            abs_path TEXT NOT NULL,
            current_content_hash TEXT DEFAULT '',
            mtime REAL NOT NULL,
            total_lines INTEGER DEFAULT 0,
            last_parsed REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            module_path TEXT DEFAULT '',
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
    """)
    conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, mtime) VALUES (?, ?, ?, ?)",
        (ws_id, "test.c", "/tmp/test.c", time.time()),
    )
    fi_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # 创建 symbols 表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER NOT NULL,
            symbol_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT DEFAULT '',
            start_line INTEGER DEFAULT 0,
            end_line INTEGER DEFAULT 0,
            qualified_name TEXT DEFAULT '',
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id)
        )
    """)
    # 插入 10 个符号
    for i in range(10):
        conn.execute(
            """INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, start_line, end_line, qualified_name)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (fi_id, f"hash_{i}", f"func_{i}", "fn", 10 + i * 10, 20 + i * 10, f"func_{i}"),
        )
    conn.commit()
    sym_ids = [r["id"] for r in conn.execute("SELECT id FROM symbols ORDER BY id").fetchall()]

    init_clone_groups_schema(conn)
    yield conn, ws_id, sym_ids, fi_id
    conn.close()


# ============================================
# Group hash
# ============================================

class TestGroupHash:
    def test_consistency(self):
        """相同参数产生相同 hash"""
        h1 = compute_group_hash(1, 2, "abc123", 0.8)
        h2 = compute_group_hash(1, 2, "abc123", 0.8)
        assert h1 == h2

    def test_different_workspace(self):
        h1 = compute_group_hash(1, 2, "abc", 0.8)
        h2 = compute_group_hash(2, 2, "abc", 0.8)
        assert h1 != h2

    def test_different_type(self):
        h1 = compute_group_hash(1, 1, "abc", 0.8)
        h2 = compute_group_hash(1, 2, "abc", 0.8)
        assert h1 != h2

    def test_different_token_hash(self):
        h1 = compute_group_hash(1, 2, "abc", 0.8)
        h2 = compute_group_hash(1, 2, "xyz", 0.8)
        assert h1 != h2

    def test_similarity_bucket(self):
        """相似度量化到 0.05 粒度"""
        h1 = compute_group_hash(1, 3, "abc", 0.81)
        h2 = compute_group_hash(1, 3, "abc", 0.82)
        # 0.81 → 0.8, 0.82 → 0.85（0.05 粒度）
        # 实际：0.81 * 20 = 16.2 → round=16 → 16/20=0.8
        # 0.82 * 20 = 16.4 → round=16 → 16/20=0.8
        # 这两个应该在同一 bucket
        assert h1 == h2

    def test_similarity_far_apart_different(self):
        """相差较大的相似度产生不同 hash"""
        h1 = compute_group_hash(1, 3, "abc", 0.80)
        h2 = compute_group_hash(1, 3, "abc", 0.95)
        assert h1 != h2

    def test_returns_16_chars(self):
        h = compute_group_hash(1, 1, "abc", 1.0)
        assert len(h) == 16


# ============================================
# 存储
# ============================================

class TestStoreCloneGroups:
    def test_store_single_group(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [{
            "clone_type": 1,
            "token_hash": "abc",
            "similarity": 1.0,
            "members": sym_ids[:3],
        }]
        n = store_clone_groups(conn, ws_id, groups)
        assert n == 1
        # 验证 group 记录
        row = conn.execute(
            "SELECT * FROM clone_groups WHERE workspace_id = ?", (ws_id,)
        ).fetchone()
        assert row["clone_type"] == 1
        assert row["token_hash"] == "abc"
        assert row["similarity"] == 1.0
        assert row["member_count"] == 3
        assert row["representative_symbol_id"] == sym_ids[0]

    def test_store_multiple_groups(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [
            {"clone_type": 1, "token_hash": "t1", "similarity": 1.0, "members": sym_ids[:2]},
            {"clone_type": 2, "token_hash": "t2", "similarity": 1.0, "members": sym_ids[2:4]},
            {"clone_type": 3, "token_hash": "t3", "similarity": 0.85, "members": sym_ids[4:7]},
        ]
        n = store_clone_groups(conn, ws_id, groups)
        assert n == 3
        rows = conn.execute(
            "SELECT * FROM clone_groups WHERE workspace_id = ? ORDER BY clone_type",
            (ws_id,),
        ).fetchall()
        assert len(rows) == 3

    def test_store_empty_list(self, db_conn):
        conn, ws_id, _, _ = db_conn
        assert store_clone_groups(conn, ws_id, []) == 0

    def test_store_skips_empty_members(self, db_conn):
        conn, ws_id, _, _ = db_conn
        groups = [
            {"clone_type": 1, "token_hash": "x", "similarity": 1.0, "members": []},
        ]
        assert store_clone_groups(conn, ws_id, groups) == 0

    def test_upsert_same_group_hash(self, db_conn):
        """相同 group_hash 的记录被 UPSERT"""
        conn, ws_id, sym_ids, _ = db_conn
        # Type-1 同 token_hash → 相同 group_hash
        g1 = [{"clone_type": 1, "token_hash": "abc", "similarity": 1.0, "members": sym_ids[:2]}]
        store_clone_groups(conn, ws_id, g1)

        # 第二次：相同 group_hash，不同 members
        g2 = [{"clone_type": 1, "token_hash": "abc", "similarity": 1.0, "members": sym_ids[2:5]}]
        store_clone_groups(conn, ws_id, g2)

        # 应该只有 1 个 group（UPSERT 覆盖）
        rows = conn.execute(
            "SELECT * FROM clone_groups WHERE workspace_id = ?", (ws_id,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["member_count"] == 3  # 新的 members 数量

    def test_members_replaced_on_upsert(self, db_conn):
        """UPSERT 时旧 members 被清空，写入新 members"""
        conn, ws_id, sym_ids, _ = db_conn
        g1 = [{"clone_type": 1, "token_hash": "abc", "similarity": 1.0, "members": sym_ids[:2]}]
        store_clone_groups(conn, ws_id, g1)
        group_id = conn.execute(
            "SELECT id FROM clone_groups WHERE workspace_id = ?", (ws_id,)
        ).fetchone()["id"]
        assert conn.execute(
            "SELECT COUNT(*) FROM clone_group_members WHERE group_id = ?",
            (group_id,),
        ).fetchone()[0] == 2

        # 第二次：members 不同
        g2 = [{"clone_type": 1, "token_hash": "abc", "similarity": 1.0, "members": sym_ids[3:6]}]
        store_clone_groups(conn, ws_id, g2)
        # 旧 members 已清空，新 members 是 3 个
        assert conn.execute(
            "SELECT COUNT(*) FROM clone_group_members WHERE group_id = ?",
            (group_id,),
        ).fetchone()[0] == 3


# ============================================
# 清理
# ============================================

class TestClearCloneGroups:
    def test_clear_removes_all_groups(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [
            {"clone_type": 1, "token_hash": "t1", "similarity": 1.0, "members": sym_ids[:2]},
            {"clone_type": 2, "token_hash": "t2", "similarity": 1.0, "members": sym_ids[2:4]},
        ]
        store_clone_groups(conn, ws_id, groups)
        deleted = clear_clone_groups(conn, ws_id)
        assert deleted == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM clone_groups WHERE workspace_id = ?", (ws_id,)
        ).fetchone()[0] == 0

    def test_clear_also_removes_members(self, db_conn):
        """ON DELETE CASCADE 应该把 members 也删掉"""
        conn, ws_id, sym_ids, _ = db_conn
        groups = [
            {"clone_type": 1, "token_hash": "t1", "similarity": 1.0, "members": sym_ids[:3]},
        ]
        store_clone_groups(conn, ws_id, groups)
        clear_clone_groups(conn, ws_id)
        assert conn.execute("SELECT COUNT(*) FROM clone_group_members").fetchone()[0] == 0

    def test_clear_empty_workspace(self, db_conn):
        conn, ws_id, _, _ = db_conn
        assert clear_clone_groups(conn, ws_id) == 0

    def test_clear_only_target_workspace(self, db_conn):
        """清理不影响其他 workspace 的 groups"""
        conn, ws_id, sym_ids, _ = db_conn
        # 插入第二个 workspace
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at) VALUES (?, ?, ?)",
            ("ws2", "/tmp/ws2", time.time()),
        )
        ws2_id = conn.execute("SELECT id FROM workspaces WHERE name = 'ws2'").fetchone()["id"]

        # 两个 workspace 各存一组
        groups = [{"clone_type": 1, "token_hash": "t", "similarity": 1.0, "members": sym_ids[:2]}]
        store_clone_groups(conn, ws_id, groups)
        store_clone_groups(conn, ws2_id, groups)

        clear_clone_groups(conn, ws_id)
        assert conn.execute(
            "SELECT COUNT(*) FROM clone_groups WHERE workspace_id = ?", (ws_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM clone_groups WHERE workspace_id = ?", (ws2_id,)
        ).fetchone()[0] == 1


# ============================================
# 查询
# ============================================

class TestListCloneGroups:
    def test_list_empty(self, db_conn):
        conn, ws_id, _, _ = db_conn
        assert list_clone_groups(conn, ws_id) == []

    def test_list_all(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [
            {"clone_type": 1, "token_hash": "t1", "similarity": 1.0, "members": sym_ids[:2]},
            {"clone_type": 2, "token_hash": "t2", "similarity": 1.0, "members": sym_ids[2:4]},
            {"clone_type": 3, "token_hash": "t3", "similarity": 0.85, "members": sym_ids[4:7]},
        ]
        store_clone_groups(conn, ws_id, groups)
        listed = list_clone_groups(conn, ws_id)
        assert len(listed) == 3

    def test_filter_by_type(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [
            {"clone_type": 1, "token_hash": "t1", "similarity": 1.0, "members": sym_ids[:2]},
            {"clone_type": 2, "token_hash": "t2", "similarity": 1.0, "members": sym_ids[2:4]},
            {"clone_type": 3, "token_hash": "t3", "similarity": 0.85, "members": sym_ids[4:7]},
        ]
        store_clone_groups(conn, ws_id, groups)
        only_type2 = list_clone_groups(conn, ws_id, clone_type=2)
        assert len(only_type2) == 1
        assert only_type2[0].clone_type == 2

    def test_filter_by_min_similarity(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [
            {"clone_type": 3, "token_hash": "t1", "similarity": 0.82, "members": sym_ids[:2]},
            {"clone_type": 3, "token_hash": "t2", "similarity": 0.91, "members": sym_ids[2:4]},
            {"clone_type": 3, "token_hash": "t3", "similarity": 0.95, "members": sym_ids[4:7]},
        ]
        store_clone_groups(conn, ws_id, groups)
        high_sim = list_clone_groups(conn, ws_id, min_similarity=0.9)
        assert len(high_sim) == 2

    def test_limit(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [
            {"clone_type": 1, "token_hash": f"t{i}", "similarity": 1.0, "members": sym_ids[i:i+2]}
            for i in range(5)
        ]
        store_clone_groups(conn, ws_id, groups)
        assert len(list_clone_groups(conn, ws_id, limit=3)) == 3

    def test_sorted_by_similarity_desc(self, db_conn):
        """按相似度降序"""
        conn, ws_id, sym_ids, _ = db_conn
        groups = [
            {"clone_type": 3, "token_hash": "low", "similarity": 0.82, "members": sym_ids[:2]},
            {"clone_type": 3, "token_hash": "high", "similarity": 0.95, "members": sym_ids[2:4]},
            {"clone_type": 3, "token_hash": "mid", "similarity": 0.88, "members": sym_ids[4:6]},
        ]
        store_clone_groups(conn, ws_id, groups)
        listed = list_clone_groups(conn, ws_id)
        assert listed[0].similarity >= listed[1].similarity
        assert listed[1].similarity >= listed[2].similarity


class TestGetCloneGroupMembers:
    def test_get_members(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [{"clone_type": 1, "token_hash": "t", "similarity": 1.0, "members": sym_ids[:3]}]
        store_clone_groups(conn, ws_id, groups)
        group_id = conn.execute(
            "SELECT id FROM clone_groups WHERE workspace_id = ?", (ws_id,)
        ).fetchone()["id"]
        members = get_clone_group_members(conn, group_id)
        assert len(members) == 3
        assert all("symbol_id" in m for m in members)
        assert all("name" in m for m in members)
        assert all("file_path" in m for m in members)

    def test_get_members_limit(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [{"clone_type": 1, "token_hash": "t", "similarity": 1.0, "members": sym_ids[:5]}]
        store_clone_groups(conn, ws_id, groups)
        group_id = conn.execute(
            "SELECT id FROM clone_groups WHERE workspace_id = ?", (ws_id,)
        ).fetchone()["id"]
        members = get_clone_group_members(conn, group_id, limit=2)
        assert len(members) == 2

    def test_get_members_unknown_group(self, db_conn):
        conn, ws_id, _, _ = db_conn
        assert get_clone_group_members(conn, 99999) == []


class TestGetCloneGroupDetail:
    def test_get_detail(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [{"clone_type": 1, "token_hash": "t", "similarity": 1.0, "members": sym_ids[:3]}]
        store_clone_groups(conn, ws_id, groups)
        group_id = conn.execute(
            "SELECT id FROM clone_groups WHERE workspace_id = ?", (ws_id,)
        ).fetchone()["id"]
        detail = get_clone_group_detail(conn, group_id)
        assert detail is not None
        assert detail.group.id == group_id
        assert len(detail.members) == 3

    def test_get_detail_unknown(self, db_conn):
        conn, ws_id, _, _ = db_conn
        assert get_clone_group_detail(conn, 99999) is None


class TestGetCloneGroupStats:
    def test_stats_empty(self, db_conn):
        conn, ws_id, _, _ = db_conn
        stats = get_clone_group_stats(conn, ws_id)
        assert stats["total_groups"] == 0
        assert stats["type1"] == 0
        assert stats["affected_files"] == 0
        assert stats["affected_symbols"] == 0

    def test_stats_with_groups(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [
            {"clone_type": 1, "token_hash": "t1", "similarity": 1.0, "members": sym_ids[:3]},
            {"clone_type": 2, "token_hash": "t2", "similarity": 1.0, "members": sym_ids[3:6]},
            {"clone_type": 3, "token_hash": "t3", "similarity": 0.85, "members": sym_ids[6:9]},
        ]
        store_clone_groups(conn, ws_id, groups)
        stats = get_clone_group_stats(conn, ws_id)
        assert stats["total_groups"] == 3
        assert stats["type1"] == 1
        assert stats["type2"] == 1
        assert stats["type3"] == 1
        assert stats["total_members"] == 9
        # 所有 symbols 都来自同一文件
        assert stats["affected_files"] == 1
        assert stats["affected_symbols"] == 9

    def test_stats_distinct_members(self, db_conn):
        """同一 symbol 出现在多个 group 中只算一次"""
        conn, ws_id, sym_ids, _ = db_conn
        groups = [
            {"clone_type": 1, "token_hash": "t1", "similarity": 1.0, "members": sym_ids[:5]},
            {"clone_type": 2, "token_hash": "t2", "similarity": 1.0, "members": sym_ids[3:7]},
        ]
        store_clone_groups(conn, ws_id, groups)
        stats = get_clone_group_stats(conn, ws_id)
        # sym_ids[3:5] 在两个 group 中都出现，但 distinct 计数
        assert stats["affected_symbols"] == 7  # sym_ids[:7]


# ============================================
# CloneGroup dataclass
# ============================================

class TestCloneGroupDataclass:
    def test_to_dict(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [{"clone_type": 1, "token_hash": "t", "similarity": 1.0, "members": sym_ids[:2]}]
        store_clone_groups(conn, ws_id, groups)
        listed = list_clone_groups(conn, ws_id)
        d = listed[0].to_dict()
        assert d["clone_type"] == 1
        assert d["token_hash"] == "t"
        assert d["similarity"] == 1.0

    def test_summary(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        groups = [{"clone_type": 2, "token_hash": "t", "similarity": 1.0, "members": sym_ids[:5]}]
        store_clone_groups(conn, ws_id, groups)
        listed = list_clone_groups(conn, ws_id)
        s = listed[0].summary()
        assert "CloneGroup" in s
        assert "type=2" in s
        assert "members=5" in s


# ============================================
# Schema 幂等性
# ============================================

class TestSchemaIdempotent:
    def test_init_schema_twice(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                root_path TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL,
                is_active INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                active_task_id TEXT DEFAULT ''
            )
        """)
        init_clone_groups_schema(conn)
        init_clone_groups_schema(conn)  # 第二次不报错
        conn.close()


# ============================================
# 端到端：store → list → detail → clear
# ============================================

class TestEndToEnd:
    def test_full_workflow(self, db_conn):
        conn, ws_id, sym_ids, _ = db_conn
        # 1. 存储
        groups = [
            {"clone_type": 1, "token_hash": "t1", "similarity": 1.0, "members": sym_ids[:3]},
            {"clone_type": 2, "token_hash": "t2", "similarity": 1.0, "members": sym_ids[3:6]},
            {"clone_type": 3, "token_hash": "t3", "similarity": 0.85, "members": sym_ids[6:9]},
        ]
        stored = store_clone_groups(conn, ws_id, groups)
        assert stored == 3

        # 2. 列表
        listed = list_clone_groups(conn, ws_id)
        assert len(listed) == 3

        # 3. 详情
        detail = get_clone_group_detail(conn, listed[0].id)
        assert detail is not None
        assert len(detail.members) == 3

        # 4. 统计
        stats = get_clone_group_stats(conn, ws_id)
        assert stats["total_groups"] == 3

        # 5. 清理
        deleted = clear_clone_groups(conn, ws_id)
        assert deleted == 3
        assert list_clone_groups(conn, ws_id) == []
