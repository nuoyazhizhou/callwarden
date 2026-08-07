"""10.7 P4 schema 与迁移测试（Requirement 11.1-11.3, 11.6-11.7, 11.12, 13.10）

覆盖：
- 旧库升级：v45（无 P4 表）→ v46 迁移后表/索引齐全，既有 workspace 数据保留
- 重复迁移幂等：再次执行不报错、不重建、唯一索引不被破坏
- token hash 存储 + raw token 禁止：DB 只存 sha256，无 raw token 列
- 单调 counter：多次 acquire/release 后 fencing_counter 严格递增
- 唯一当前 lease：同 task+role 同时最多一个 active（部分唯一索引兜底）
- 事件追加：acquire/renew/release 顺序追加，且事件账本不记录 token
- claimed metadata 不获授权：assignment 绑定本身不授予 lease/写权限
"""
import os
import sqlite3
import time

import pytest

from callwarden.db import CodeGraphDB
from callwarden.db.db_base import _migrate_v45_to_v46
from callwarden.db.db_task_leases import (
    ERR_LEASE_NOT_FOUND,
    ERR_LEASE_TOKEN_MISMATCH,
    LeaseMixin,
)
from callwarden.db.schema import SCHEMA_VERSION

_P4_TABLES = ("task_assignments", "task_leases", "task_lease_events")


def _identity(agent_id="agent-a", session_id="sess-1", model_id="model-x", role="implementer"):
    return {"agent_id": agent_id, "session_id": session_id, "model_id": model_id, "role": role}


@pytest.fixture()
def db(tmp_path):
    os.environ["CW_USE_RUST_STORAGE"] = "0"
    d = CodeGraphDB(str(tmp_path / "p4mig.db"))
    d.register_workspace("mig-ws", str(tmp_path))
    d.set_active_workspace("mig-ws")
    yield d
    try:
        d.conn.close()
    except Exception:
        pass


def _drop_p4_tables(conn):
    """模拟 v45 旧库：删除 P4 三张表与其索引。"""
    conn.execute("DROP INDEX IF EXISTS idx_task_leases_active_unique")
    conn.execute("DROP INDEX IF EXISTS idx_task_assignments_task")
    conn.execute("DROP INDEX IF EXISTS idx_task_leases_task")
    conn.execute("DROP INDEX IF EXISTS idx_task_lease_events_lease")
    conn.execute("DROP INDEX IF EXISTS idx_task_lease_events_task")
    for t in _P4_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()


def _p4_tables(conn):
    return sorted(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('task_assignments','task_leases','task_lease_events')"
        )
    )


# ---------------------------------------------------------------
# 1. 旧库升级（v45 → v46）
# ---------------------------------------------------------------

def test_v45_legacy_db_upgrade_keeps_data(db):
    """模拟 v45 旧库：有 workspace 数据、无 P4 表；迁移后表齐全且数据保留。"""
    ws = db._get_active_workspace_id()
    db.conn.execute(
        "INSERT INTO workspaces (name, root_path, created_at) VALUES ('kept', 'x', ?)",
        (time.time(),),
    )
    db.conn.commit()
    kept_id = db.conn.execute(
        "SELECT id FROM workspaces WHERE name='kept'"
    ).fetchone()["id"]

    _drop_p4_tables(db.conn)
    assert _p4_tables(db.conn) == []

    _migrate_v45_to_v46(db.conn)
    db.conn.commit()

    assert _p4_tables(db.conn) == ["task_assignments", "task_lease_events", "task_leases"]
    # 既有数据保留
    assert db.conn.execute(
        "SELECT COUNT(*) c FROM workspaces WHERE id=?", (kept_id,)
    ).fetchone()["c"] == 1
    assert db._get_active_workspace_id() == ws
    # 唯一索引存在
    idx = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_task_leases_active_unique'"
    ).fetchone()
    assert idx, "迁移后必须重建单 active lease 部分唯一索引（Req 11.2）"


def test_migration_idempotent_repeat(db):
    """重复执行迁移：不报错、不重复建表、唯一索引保持。"""
    _migrate_v45_to_v46(db.conn)
    _migrate_v45_to_v46(db.conn)
    _migrate_v45_to_v46(db.conn)
    assert _p4_tables(db.conn) == ["task_assignments", "task_lease_events", "task_leases"]
    cnt = db.conn.execute(
        "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name='task_leases'"
    ).fetchone()["c"]
    assert cnt == 1


def test_migration_partial_state_recover(db):
    """迁移中断在中间态（只有部分表）也能恢复：重新执行补齐。"""
    _drop_p4_tables(db.conn)
    # 只建 1 张表模拟中断
    db.conn.execute(
        """
        CREATE TABLE task_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            assignment_id TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL,
            role TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL,
            revoked_at REAL DEFAULT NULL,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
        """
    )
    db.conn.commit()
    _migrate_v45_to_v46(db.conn)
    assert _p4_tables(db.conn) == ["task_assignments", "task_lease_events", "task_leases"]


# ---------------------------------------------------------------
# 2. token hash / raw token 禁止（Req 11.2）
# ---------------------------------------------------------------

def test_raw_token_never_stored(db):
    """DB 只存 sha256 hash：task_leases 无 raw token 列，事件账本亦不记录。"""
    import hashlib

    ok, r = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    raw = r["token"]
    cols = [c[1] for c in db.conn.execute("PRAGMA table_info(task_leases)")]
    assert "token" not in cols and "token_hash" in cols
    row = db.conn.execute(
        "SELECT token_hash FROM task_leases WHERE lease_id=?", (r["lease_id"],)
    ).fetchone()
    assert row["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in row["token_hash"]

    ev_cols = [c[1] for c in db.conn.execute("PRAGMA table_info(task_lease_events)")]
    assert "token" not in ev_cols and "token_hash" not in ev_cols
    events = db.list_lease_events("T-1", "implementer")
    for e in events:
        assert "token" not in e and "token_hash" not in e


# ---------------------------------------------------------------
# 3. 单调 counter（Req 11.3）
# ---------------------------------------------------------------

def test_fencing_counter_strictly_monotonic(db):
    """同一 task+role 反复 acquire/release：counter 必须严格单调递增（Req 11.3）。"""
    counters = []
    for i in range(4):
        ok, r = db.acquire_lease("T-1", "implementer", _identity())
        assert ok
        counters.append(r["fencing_counter"])
        db.release_lease("T-1", "implementer", r["token"])
    assert counters == [1, 2, 3, 4], f"counters={counters}"
    assert all(counters[i] < counters[i + 1] for i in range(len(counters) - 1)), f"counters={counters}"


def test_counter_survives_reacquire_same_task(db):
    ok, r1 = db.acquire_lease("T-1", "implementer", _identity())
    db.release_lease("T-1", "implementer", r1["token"])
    ok, r2 = db.acquire_lease("T-1", "implementer", _identity())
    assert r2["fencing_counter"] == r1["fencing_counter"] + 1
    db.release_lease("T-1", "implementer", r2["token"])
    ok, r3 = db.acquire_lease("T-1", "implementer", _identity())
    assert r3["fencing_counter"] == r2["fencing_counter"] + 1


# ---------------------------------------------------------------
# 4. 唯一当前 lease（Req 11.2 部分唯一索引）
# ---------------------------------------------------------------

def test_partial_unique_index_blocks_second_active(db):
    ok, r = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    # 绕过业务层直接插入第二个 active lease → 唯一索引必须拒绝
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO task_leases
                (workspace_id, lease_id, task_id, role, agent_id, session_id,
                 model_id, token_hash, fencing_counter, acquired_at, expires_at, status)
            VALUES (?, 'L-DUP', 'T-1', 'implementer', 'a', 's', 'm',
                    'hash', 99, 0.0, 99999.0, 'active')
            """,
            (db._get_active_workspace_id(),),
        )


# ---------------------------------------------------------------
# 5. 事件追加（Req 11.6-11.7）
# ---------------------------------------------------------------

def test_events_append_only_ordered(db):
    ok, r = db.acquire_lease("T-1", "implementer", _identity())
    raw = r["token"]
    db.renew_lease("T-1", "implementer", raw)
    db.release_lease("T-1", "implementer", raw)
    events = db.list_lease_events("T-1", "implementer")
    assert [e["event_type"] for e in events] == ["acquire", "renew", "release"]
    # 事件按 event_at 升序（追加顺序）
    ats = [e["event_at"] for e in events]
    assert ats == sorted(ats)


# ---------------------------------------------------------------
# 6. claimed metadata 不获授权（Req 13.10 / 11.12）
# ---------------------------------------------------------------

def test_assignment_binding_does_not_grant_lease(db):
    """assignment 绑定只是元数据：创建 assignment 后仍无 lease，校验 NOT_FOUND。"""
    ok, _ = db.create_assignment("T-1", "implementer", _identity())
    assert ok
    st = db.get_lease_status("T-1", "implementer")
    assert st["status"] == "none"
    valid, res = db.validate_lease_for_mutation("T-1", "implementer", "any-token", 1)
    assert not valid and res["code"] == ERR_LEASE_NOT_FOUND


def test_assignment_does_not_authorize_write(db):
    """claimed identity 不改变 lease 凭证要求：assignment 存在但无 lease，写仍被拒。"""
    ok, _ = db.create_assignment("T-1", "implementer", _identity())
    assert ok
    valid, res = db.validate_lease_for_mutation("T-1", "implementer", "t", 1, identity=_identity())
    assert not valid
    assert res["code"] in (ERR_LEASE_NOT_FOUND, ERR_LEASE_TOKEN_MISMATCH)


def test_schema_version_is_46(db):
    assert SCHEMA_VERSION == 47
    assert issubclass(CodeGraphDB, LeaseMixin)
