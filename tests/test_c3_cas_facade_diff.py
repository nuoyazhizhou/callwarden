# -*- coding: utf-8 -*-
"""C3（Global/Local CAS 完整迁移）差分测试。

验证 Rust `callwarden_core.cas_file_generation_*` facade 与 Python
`server/replicator.py` 生产路径（daemon_handle_connect/refresh 接线后）
的 file_generations 两阶段写语义一致：

- C1 原子性：seen/committed 均为单事务（BEGIN IMMEDIATE），并发不撕裂
- C2 stale 拦截：旧代际 seen / 已 committed 的同代际重试 → 拒绝
- C3 幂等：重复 committed 同代际不产生重复行
- C4 空结果：file_generations 无该 rel_path → 自动初始化
- C5 workspace 隔离：跨 workspace 不串扰
- C6 接线：daemon_handle_connect 的 reset 走 Rust facade（ws_db_path 非空时）

对应契约：docs/design/c3-global-local-cas-complete-contract.md §5/§6
"""
import os
import sqlite3
import tempfile

import pytest

callwarden_core = pytest.importorskip("callwarden_core")

from callwarden.server.replicator import (  # noqa: E402
    _RUST_FILE_GEN_AVAILABLE,
    daemon_handle_connect,
    init_session_schema,
)


@pytest.fixture()
def ws_db():
    """临时 workspace.db：init_session_schema 初始化 session 管理表。"""
    tmpdir = tempfile.mkdtemp(prefix="c3_facade_")
    db_path = os.path.join(tmpdir, "workspace.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_session_schema(conn)
    conn.close()
    yield db_path
    conn = None
    try:
        os.remove(db_path)
        os.remove(db_path + "-wal")
        os.remove(db_path + "-shm")
    except OSError:
        pass


def _row(conn, ws_id, rel_path):
    return conn.execute(
        "SELECT latest_session_id, latest_session_epoch, latest_seq, "
        "latest_seen_generation, latest_committed_generation "
        "FROM file_generations WHERE workspace_id = ? AND rel_path = ?",
        (ws_id, rel_path),
    ).fetchone()


@pytest.mark.skipif(not _RUST_FILE_GEN_AVAILABLE,
                    reason="Rust file_generations facade 不可用（旧 pyd）")
class TestCasFileGenerationFacade:
    def test_seen_first_insert(self, ws_db):
        """C4 空结果：首次 seen 自动初始化行并更新 seen 代际。"""
        assert callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 1) is True
        conn = sqlite3.connect(ws_db)
        conn.row_factory = sqlite3.Row
        row = _row(conn, 42, "a.py")
        assert row["latest_seq"] == 1
        assert row["latest_seen_generation"] == "1:1"
        assert row["latest_committed_generation"] == ""

    def test_seen_higher_seq_allowed(self, ws_db):
        """同一 session 更高 seq 正常更新。"""
        callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 1)
        assert callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 2) is True
        conn = sqlite3.connect(ws_db)
        conn.row_factory = sqlite3.Row
        assert _row(conn, 42, "a.py")["latest_seen_generation"] == "1:2"

    def test_seen_stale_lower_seq_rejected(self, ws_db):
        """C2 stale：未 committed 时更旧代际 seen 被拒绝（不覆盖 uncommitted）。"""
        callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 2)
        assert callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 1) is False
        conn = sqlite3.connect(ws_db)
        conn.row_factory = sqlite3.Row
        # 旧代际不得覆盖已有 seen
        assert _row(conn, 42, "a.py")["latest_seen_generation"] == "1:2"

    def test_committed_conditional_update(self, ws_db):
        """committed 条件更新 latest_committed_generation。"""
        callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 2)
        assert callwarden_core.cas_file_generation_committed(ws_db, 42, "a.py", 1, 2) is True
        conn = sqlite3.connect(ws_db)
        conn.row_factory = sqlite3.Row
        assert _row(conn, 42, "a.py")["latest_committed_generation"] == "1:2"

    def test_seen_idempotent_when_committed(self, ws_db):
        """C2/C3 幂等：已 committed 的同代际再 seen → False（stale_seq_dropped）。"""
        callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 2)
        callwarden_core.cas_file_generation_committed(ws_db, 42, "a.py", 1, 2)
        assert callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 2) is False

    def test_stale_committed_rejected(self, ws_db):
        """已 committed 更高代际后，更低代际 committed 返回 False。"""
        callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 2)
        callwarden_core.cas_file_generation_committed(ws_db, 42, "a.py", 1, 2)
        assert callwarden_core.cas_file_generation_committed(ws_db, 42, "a.py", 1, 1) is False
        conn = sqlite3.connect(ws_db)
        conn.row_factory = sqlite3.Row
        assert _row(conn, 42, "a.py")["latest_committed_generation"] == "1:2"

    def test_uncommit_rollback(self, ws_db):
        """uncommit 清空 committed，允许同代际重试。"""
        callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 2)
        callwarden_core.cas_file_generation_committed(ws_db, 42, "a.py", 1, 2)
        assert callwarden_core.cas_file_generation_uncommit(ws_db, 42, "a.py") is True
        conn = sqlite3.connect(ws_db)
        conn.row_factory = sqlite3.Row
        assert _row(conn, 42, "a.py")["latest_committed_generation"] == ""

    def test_reset_via_connect_wiring(self, ws_db):
        """C6 接线：daemon_handle_connect 的会话重置走 Rust facade。"""
        # 先写入旧 session 数据
        callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 5)
        callwarden_core.cas_file_generation_committed(ws_db, 42, "a.py", 1, 5)

        conn = sqlite3.connect(ws_db)
        conn.row_factory = sqlite3.Row
        # 预置旧 session（epoch=1），确保 connect 分配 epoch=2
        conn.execute(
            "INSERT INTO agent_sessions (workspace_id, session_id, session_epoch, "
            "activated_at, revoked_at, peer_uid) VALUES (42, 's1', 1, 1, NULL, 1000)")
        conn.commit()
        res = daemon_handle_connect(
            peer_uid=1000,
            workspace_id=42,
            requested_session_id="s2",
            ws_conn=conn,
            ws_db_path=ws_db,
        )
        assert res["session_epoch"] == 2
        row = _row(conn, 42, "a.py")
        # reset：归属新 session、seq 归零、seen 清空
        assert row["latest_session_id"] == "s2"
        assert row["latest_session_epoch"] == 2
        assert row["latest_seq"] == 0
        assert row["latest_seen_generation"] == ""

    def test_workspace_isolation(self, ws_db):
        """C5 workspace 隔离：跨 workspace 写不串扰。"""
        callwarden_core.cas_file_generation_seen(ws_db, 42, "a.py", "s1", 1, 3)
        callwarden_core.cas_file_generation_committed(ws_db, 42, "a.py", 1, 3)
        # 另一 workspace 的同名文件独立初始化
        assert callwarden_core.cas_file_generation_seen(ws_db, 43, "a.py", "s1", 1, 1) is True
        conn = sqlite3.connect(ws_db)
        conn.row_factory = sqlite3.Row
        assert _row(conn, 42, "a.py")["latest_committed_generation"] == "1:3"
        assert _row(conn, 43, "a.py")["latest_seq"] == 1

    def test_concurrent_seen_no_tear(self, ws_db):
        """C1 原子性：多线程并发 seen 不同代际，单事务串行化不撕裂。

        最终 latest_seq 必须等于最大 seq；低 seq 在后执行时被 stale 拦截返回
        False，但至少一个调用成功（首个非 stale）。
        """
        import threading

        results: list = []
        results_lock = threading.Lock()

        def worker(seq):
            ok = callwarden_core.cas_file_generation_seen(
                ws_db, 42, "a.py", "s1", 1, seq)
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2, 3, 4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        conn = sqlite3.connect(ws_db)
        conn.row_factory = sqlite3.Row
        row = _row(conn, 42, "a.py")
        assert row["latest_seq"] == 4, dict(row)
        assert any(results)  # 首个非 stale 至少成功一次
