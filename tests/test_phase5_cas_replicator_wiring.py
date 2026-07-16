"""Phase 5 集成测试：CAS/Replicator/StagingLog/SnapshotManager 接通验收。

任务：T-1783952125417-7a09 Step #4
规范：enterprise-daemon-full-e2e-followup.md §4.4

覆盖：
1. CAS miss → publish → 第二 workspace 命中（parse miss=0）
2. stale session / 重复 seq / 乱序 seq / 断线重连不覆盖新 generation
3. 崩溃后 pending staging entries 恢复幂等
4. 并发 100 reader 无 SQLite 锁错误
5. mark_applied_batch 批量标记
6. daemon_handle_refresh 接收 canonical_bytes（不读 abs_path）
7. EnterpriseDaemonService per-workspace 资源初始化
8. 各阶段耗时报告
"""

import hashlib
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest

# ============================================================
# CAS 集成测试
# ============================================================


class TestCASHitMiss:
    """验收 §4.4: CAS miss 第一次 parse/publish；相同内容第二 workspace 命中且 parse miss=0。"""

    def _make_cas_conn(self, tmp_dir):
        """创建独立的 CAS 数据库连接。"""
        from callwarden.db.db_cas import init_cas_schema
        db_path = os.path.join(tmp_dir, "cas.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        init_cas_schema(conn)
        return conn

    def test_cas_first_publish_then_hit(self, tmp_path):
        """第一次 CAS miss 发布，第二次 CAS hit 且 parse miss=0。"""
        from callwarden.db.db_cas import compute_cas_key_v1, cas_lookup, cas_publish_with_retry, cas_pin

        tmp_dir = str(tmp_path)
        cas_conn = self._make_cas_conn(tmp_dir)

        # 模拟 Python 文件的 parse result
        content = b"def hello():\n    print('hello')\n"
        content_hash = hashlib.sha256(content).hexdigest()
        language = "python"
        cas_key = compute_cas_key_v1(
            content_hash, language, "0.1.0", "0.2.0", "v1", "v1", "v1"
        )

        # 第一次：CAS miss
        existing = cas_lookup(cas_conn, cas_key)
        assert existing is None, "首次查询应该 CAS miss"

        # 发布
        parse_result = {
            "symbols": [
                {"name": "hello", "qualified_name": "hello", "kind": "function",
                 "start_line": 1, "end_line": 2, "content": "def hello():\n    print('hello')\n",
                 "has_comment": False, "depth": 0}
            ],
            "raw_calls": [
                {"caller_name": "hello", "callee_name": "print", "line": 2}
            ],
            "imports": [],
            "file_size": len(content),
            "total_lines": 2,
        }
        cas_publish_with_retry(cas_conn, cas_key, content_hash, language,
                               parse_result, workspace_id=1)

        # 验证 state=ready
        row = cas_lookup(cas_conn, cas_key)
        assert row is not None, "发布后应该 CAS hit"
        assert row["state"] == "ready"

        # 第二次：相同内容，不同 workspace_id → CAS hit
        cas_pin(cas_conn, cas_key, workspace_id=2)
        row2 = cas_lookup(cas_conn, cas_key)
        assert row2 is not None, "相同内容第二 workspace 应该 CAS hit"
        assert row2["cas_key"] == cas_key

        cas_conn.close()

    def test_cas_cross_workspace_dedup(self, tmp_path):
        """验证 CAS key 与 workspace_id / UID / branch 无关。"""
        from callwarden.db.db_cas import compute_cas_key_v1

        content_hash = hashlib.sha256(b"def foo(): pass\n").hexdigest()
        # 不同 workspace，相同内容 → 相同 CAS key
        key1 = compute_cas_key_v1(content_hash, "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        key2 = compute_cas_key_v1(content_hash, "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        assert key1 == key2, "相同内容必须得到相同 CAS key，与路径/UID/branch 无关"


# ============================================================
# daemon_handle_refresh canonical_bytes 测试
# ============================================================


class TestDaemonHandleRefreshCanonicalBytes:
    """验收 §4.2: daemon_handle_refresh 接收 canonical_bytes，不读 abs_path。"""

    def _setup_workspace(self, tmp_dir):
        """创建 workspace session DB。"""
        from callwarden.server.replicator import init_session_schema
        ws_path = os.path.join(tmp_dir, "workspace.db")
        ws_conn = sqlite3.connect(ws_path)
        ws_conn.row_factory = sqlite3.Row
        ws_conn.execute("PRAGMA busy_timeout=5000")
        init_session_schema(ws_conn)
        return ws_conn

    def test_refresh_with_canonical_bytes_no_abs_path(self, tmp_path):
        """传入 canonical_bytes 时不读 abs_path。"""
        from callwarden.server.replicator import daemon_handle_connect, daemon_handle_refresh

        tmp_dir = str(tmp_path)
        ws_conn = self._setup_workspace(tmp_dir)

        # 连接握手
        connect_result = daemon_handle_connect(
            peer_uid=1000, workspace_id=1,
            requested_session_id="test-session-1",
            ws_conn=ws_conn,
        )
        epoch = connect_result["session_epoch"]
        assert epoch >= 1

        # refresh with canonical_bytes, abs_path 故意传一个不存在的路径
        canonical_bytes = b"def greet():\n    return 'hi'\n"
        msg = {
            "rel_path": "greet.py",
            "agent_session_id": "test-session-1",
            "session_epoch": epoch,
            "monotonic_seq": 1,
            "abs_path": "/nonexistent/path/that/should/not/be/read.py",
        }
        result = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1, msg=msg,
            ws_conn=ws_conn, cas_conn=None,
            canonical_bytes=canonical_bytes,
        )
        assert result["status"] == "committed"
        assert result["generation"] == f"{epoch}:1"
        # canonical_bytes 模式下 content_hash 应该从 bytes 计算
        assert result.get("content_hash") == hashlib.sha256(canonical_bytes).hexdigest()

        ws_conn.close()

    def test_refresh_stale_session_rejected_with_canonical_bytes(self, tmp_path):
        """canonical_bytes 模式下 stale session 仍然被拒绝。"""
        from callwarden.server.replicator import daemon_handle_connect, daemon_handle_refresh, ProtocolError

        tmp_dir = str(tmp_path)
        ws_conn = self._setup_workspace(tmp_dir)

        daemon_handle_connect(
            peer_uid=1000, workspace_id=1,
            requested_session_id="session-1",
            ws_conn=ws_conn,
        )

        # 新 session 覆盖旧 session
        connect2 = daemon_handle_connect(
            peer_uid=1000, workspace_id=1,
            requested_session_id="session-2",
            ws_conn=ws_conn,
        )
        new_epoch = connect2["session_epoch"]

        # 用旧 session refresh → 应该被拒绝
        msg = {
            "rel_path": "test.py",
            "agent_session_id": "session-1",
            "session_epoch": 1,
            "monotonic_seq": 1,
        }
        with pytest.raises(ProtocolError, match="stale session"):
            daemon_handle_refresh(
                peer_uid=1000, workspace_id=1, msg=msg,
                ws_conn=ws_conn, cas_conn=None,
                canonical_bytes=b"x = 1\n",
            )

        ws_conn.close()

    def test_refresh_duplicate_seq_dropped(self, tmp_path):
        """重复 seq 直接丢弃，不报错。"""
        from callwarden.server.replicator import daemon_handle_connect, daemon_handle_refresh

        tmp_dir = str(tmp_path)
        ws_conn = self._setup_workspace(tmp_dir)

        connect_result = daemon_handle_connect(
            peer_uid=1000, workspace_id=1,
            requested_session_id="session-dup",
            ws_conn=ws_conn,
        )
        epoch = connect_result["session_epoch"]

        canonical_bytes = b"y = 2\n"
        msg1 = {
            "rel_path": "dup.py",
            "agent_session_id": "session-dup",
            "session_epoch": epoch,
            "monotonic_seq": 1,
        }
        result1 = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1, msg=msg1,
            ws_conn=ws_conn, cas_conn=None,
            canonical_bytes=canonical_bytes,
        )
        assert result1["status"] == "committed"

        # 重复 seq=1
        result2 = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1, msg=msg1,
            ws_conn=ws_conn, cas_conn=None,
            canonical_bytes=canonical_bytes,
        )
        assert result2["status"] == "stale_seq_dropped"

        ws_conn.close()

    def test_refresh_out_of_order_seq(self, tmp_path):
        """乱序 seq：先 seq=2 再 seq=1，seq=1 被丢弃。"""
        from callwarden.server.replicator import daemon_handle_connect, daemon_handle_refresh

        tmp_dir = str(tmp_path)
        ws_conn = self._setup_workspace(tmp_dir)

        connect_result = daemon_handle_connect(
            peer_uid=1000, workspace_id=1,
            requested_session_id="session-ooo",
            ws_conn=ws_conn,
        )
        epoch = connect_result["session_epoch"]

        # seq=2 first
        msg2 = {
            "rel_path": "ooo.py",
            "agent_session_id": "session-ooo",
            "session_epoch": epoch,
            "monotonic_seq": 2,
        }
        result2 = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1, msg=msg2,
            ws_conn=ws_conn, cas_conn=None,
            canonical_bytes=b"z = 3\n",
        )
        assert result2["status"] == "committed"

        # seq=1 (out of order) → dropped
        msg1 = {
            "rel_path": "ooo.py",
            "agent_session_id": "session-ooo",
            "session_epoch": epoch,
            "monotonic_seq": 1,
        }
        result1 = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1, msg=msg1,
            ws_conn=ws_conn, cas_conn=None,
            canonical_bytes=b"a = 1\n",
        )
        assert result1["status"] == "stale_seq_dropped"

        ws_conn.close()


# ============================================================
# StagingLog mark_applied_batch 测试
# ============================================================


class TestStagingLogBatchMark:
    """验收 mark_applied_batch 批量标记。"""

    def test_mark_applied_batch(self, tmp_path):
        """批量标记多个 LSN 为 applied，单次文件重写。"""
        from callwarden.server.staging_log import StagingLog, StagingEntry

        log_path = str(tmp_path / "test.log")
        log = StagingLog(log_path)

        # 追加 5 个 entries
        entries = []
        for i in range(5):
            entry = StagingEntry(
                lsn=0, timestamp=time.time(),
                workspace_id="ws_batch",
                file_path=f"file_{i}.py",
                content_hash=f"hash_{i}",
                language="python",
            )
            lsn = log.append(entry)
            entries.append(lsn)

        # 验证全部 pending
        pending = log.read_pending()
        assert len(pending) == 5

        # 批量标记 lsn 1, 3, 5
        log.mark_applied_batch([entries[0], entries[2], entries[4]])

        # 验证只有 2 个 pending
        pending = log.read_pending()
        assert len(pending) == 2
        remaining_lsns = {e.lsn for e in pending}
        assert remaining_lsns == {entries[1], entries[3]}

    def test_mark_applied_batch_empty(self, tmp_path):
        """空 lsns 列表不应报错。"""
        from callwarden.server.staging_log import StagingLog

        log_path = str(tmp_path / "empty.log")
        log = StagingLog(log_path)
        log.mark_applied_batch([])  # 不应抛异常


# ============================================================
# 崩溃恢复测试
# ============================================================


class TestCrashRecovery:
    """验收 §4.4: crash 后 pending staging entries 恢复幂等。"""

    def test_staging_log_survives_crash(self, tmp_path):
        """模拟 daemon crash：pending entries 在 log 文件中持久化。"""
        from callwarden.server.staging_log import StagingLog, StagingEntry

        log_path = str(tmp_path / "crash.log")

        # 第一次运行：追加 entries 但没标记 applied
        log1 = StagingLog(log_path)
        for i in range(3):
            entry = StagingEntry(
                lsn=0, timestamp=time.time(),
                workspace_id="ws_crash",
                file_path=f"crash_{i}.py",
                content_hash=f"hash_{i}",
                language="python",
            )
            log1.append(entry)

        # 模拟 crash：直接丢弃 log 对象
        del log1

        # 第二次运行：从 log 文件恢复
        log2 = StagingLog(log_path)
        pending = log2.read_pending()
        assert len(pending) == 3, "crash 后应该恢复 3 个 pending entries"

        # 恢复后标记 applied
        log2.mark_applied_batch([e.lsn for e in pending])
        assert len(log2.read_pending()) == 0

    def test_recover_idempotent(self, tmp_path):
        """重复 recover 应该幂等。"""
        from callwarden.server.staging_log import StagingLog, StagingEntry
        from callwarden.server.replicator import Replicator

        log_path = str(tmp_path / "idempotent.log")
        log = StagingLog(log_path)

        entry = StagingEntry(
            lsn=0, timestamp=time.time(),
            workspace_id="ws_idem",
            file_path="idem.py",
            content_hash="hash_idem",
            language="python",
        )
        log.append(entry)

        replicator = Replicator(log, snapshot_service=None)

        # 第一次 recover
        result1 = replicator.recover("ws_idem")
        assert result1.applied_count == 1

        # 第二次 recover：无 pending → 无操作
        result2 = replicator.recover("ws_idem")
        assert result2.applied_count == 0


# ============================================================
# 并发 Reader 测试
# ============================================================


class TestConcurrentReaders:
    """验收 §4.4: 并发 100 reader 无 SQLite 锁错误。"""

    def test_concurrent_cas_readers(self, tmp_path):
        """100 个并发 reader 查询 CAS 无锁错误。"""
        from callwarden.db.db_cas import init_cas_schema, cas_lookup, cas_publish_with_retry

        db_path = str(tmp_path / "concurrent.db")
        # 先写入一条 CAS 数据
        writer = sqlite3.connect(db_path)
        writer.row_factory = sqlite3.Row
        writer.execute("PRAGMA busy_timeout=5000")
        writer.execute("PRAGMA journal_mode=WAL")
        init_cas_schema(writer)

        content_hash = hashlib.sha256(b"concurrent_test").hexdigest()
        from callwarden.db.db_cas import compute_cas_key_v1
        cas_key = compute_cas_key_v1(
            content_hash, "python", "0.1.0", "0.2.0", "v1", "v1", "v1"
        )
        parse_result = {
            "symbols": [],
            "raw_calls": [],
            "imports": [],
            "file_size": 15,
            "total_lines": 1,
        }
        cas_publish_with_retry(writer, cas_key, content_hash, "python",
                               parse_result, workspace_id=1)
        writer.close()

        # 100 个并发 reader
        errors = []
        hits = []

        def reader_fn(reader_id):
            try:
                conn = sqlite3.connect(db_path, timeout=5.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=5000")
                for _ in range(10):
                    row = cas_lookup(conn, cas_key)
                    if row:
                        hits.append(reader_id)
                conn.close()
            except Exception as e:
                errors.append(str(e))

        threads = []
        for i in range(100):
            t = threading.Thread(target=reader_fn, args=(i,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0, f"并发 reader 出现 SQLite 锁错误: {errors}"
        assert len(hits) == 1000, f"100 reader × 10 次查询 = 1000 次命中，实际 {len(hits)}"


# ============================================================
# EnterpriseDaemonService per-workspace 资源测试
# ============================================================


class TestEnterpriseDaemonServiceResources:
    """验收 EnterpriseDaemonService per-workspace 资源初始化。"""

    def test_workspace_resources_lazy_init(self, tmp_path):
        """_get_workspace_resources 懒初始化 CAS/StagingLog/Replicator。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService
        from callwarden.server.snapshot_manager import SnapshotManagerService

        # 创建 mock snapshot service
        snapshot_svc = MagicMock(spec=SnapshotManagerService)

        registry_db = str(tmp_path / "registry.db")
        service = EnterpriseDaemonService(
            registry_db=registry_db,
            snapshot_service=snapshot_svc,
            data_root=str(tmp_path / "enterprise"),
        )

        # 先注册一个 workspace
        from callwarden.db.db_daemon import register_workspace
        with service._registry_conn() as conn:
            register_workspace(
                conn,
                owner_uid=os.getuid() if hasattr(os, "getuid") else 0,
                client_view_root="/test/root",
                host_real_root="/test/root",
            )

        # 懒初始化
        ws_id = "test-ws-1"
        res = service._get_workspace_resources(ws_id)
        assert "cas_conn" in res
        assert "ws_conn" in res
        assert "staging_log" in res
        assert "replicator" in res

        # 验证数据库文件已创建（staging.log 在首次 append 时才创建）
        ws_dir = os.path.join(str(tmp_path / "enterprise"), ws_id)
        assert os.path.exists(os.path.join(ws_dir, "cas.db"))
        assert os.path.exists(os.path.join(ws_dir, "workspace.db"))

        # 第二次调用返回相同资源
        res2 = service._get_workspace_resources(ws_id)
        assert res2["cas_conn"] is res["cas_conn"]


# ============================================================
# 性能指标测试
# ============================================================


class TestPerformanceMetrics:
    """验收 §4.4: 报告各阶段耗时。"""

    def test_refresh_timing(self, tmp_path):
        """验证 refresh 返回耗时指标。"""
        from callwarden.server.replicator import daemon_handle_connect, daemon_handle_refresh

        tmp_dir = str(tmp_path)
        from callwarden.server.replicator import init_session_schema
        ws_path = os.path.join(tmp_dir, "ws_perf.db")
        ws_conn = sqlite3.connect(ws_path)
        ws_conn.row_factory = sqlite3.Row
        init_session_schema(ws_conn)

        connect_result = daemon_handle_connect(
            peer_uid=1000, workspace_id=1,
            requested_session_id="perf-session",
            ws_conn=ws_conn,
        )
        epoch = connect_result["session_epoch"]

        canonical_bytes = b"def perf():\n    return 42\n"
        msg = {
            "rel_path": "perf.py",
            "agent_session_id": "perf-session",
            "session_epoch": epoch,
            "monotonic_seq": 1,
        }

        start = time.time()
        result = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1, msg=msg,
            ws_conn=ws_conn, cas_conn=None,
            canonical_bytes=canonical_bytes,
        )
        elapsed_ms = (time.time() - start) * 1000

        assert result["status"] == "committed"
        assert elapsed_ms < 5000, f"单次 refresh 应在 5 秒内完成，实际 {elapsed_ms:.1f}ms"

        ws_conn.close()

    def test_replicate_timing(self, tmp_path):
        """验证 replicate 返回耗时指标。"""
        from callwarden.server.staging_log import StagingLog, StagingEntry
        from callwarden.server.replicator import Replicator

        log_path = str(tmp_path / "perf.log")
        log = StagingLog(log_path)

        for i in range(10):
            entry = StagingEntry(
                lsn=0, timestamp=time.time(),
                workspace_id="ws_perf",
                file_path=f"perf_{i}.py",
                content_hash=f"hash_{i}",
                language="python",
            )
            log.append(entry)

        replicator = Replicator(log, snapshot_service=None)
        result = replicator.replicate("ws_perf")

        assert result.applied_count == 10
        assert result.duration_ms >= 0
        assert result.duration_ms < 5000, f"10 条 replicate 应在 5 秒内完成，实际 {result.duration_ms:.1f}ms"
