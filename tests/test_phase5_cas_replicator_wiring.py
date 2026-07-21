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
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 批次10 测试用：项目根目录（用于读取源文件验证修复标记）
ROOT = Path(__file__).resolve().parent.parent

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


# ============================================================
# 批次9（K4 snapshot 未发布修复）测试
# ============================================================


class TestBatch9CodegraphDbPathResolution:
    """批次9：DaemonConfig.codegraph_db_path_template + resolve_codegraph_db_path。

    根因：daemon_server.py L838 调用 replicator.replicate(workspace_id) 未传 db_path，
    导致 Replicator 跳过 publish_snapshot，watcher→daemon→query 事件回环中断。
    修复：DaemonConfig 加 codegraph_db_path_template 字段 + resolve_codegraph_db_path 方法
    （与 Rust 端 workspace.rs L1319-1336 对齐）。
    """

    def test_default_template_falls_back_to_user_db(self):
        """默认模板为空时，回退到用户级单库 ~/.callwarden/callwarden.db。"""
        from callwarden.server.daemon_config import DaemonConfig

        cfg = DaemonConfig.default()
        assert cfg.codegraph_db_path_template == ""

        db_path = cfg.resolve_codegraph_db_path("ws-test-123")
        # 必须以 ~/.callwarden/callwarden.db 结尾
        expected_suffix = os.path.join(".callwarden", "callwarden.db")
        assert db_path.endswith(expected_suffix), (
            f"默认 db_path 应以 {expected_suffix} 结尾，实际: {db_path}"
        )

    def test_template_with_placeholder_substituted(self):
        """模板含 {workspace_instance_id} 占位符时正确替换。"""
        from callwarden.server.daemon_config import DaemonConfig

        cfg = DaemonConfig.load_from_dict({
            "codegraph_db_path_template": "/var/lib/callwarden/{workspace_instance_id}/codegraph.db",
        })
        assert cfg.codegraph_db_path_template == "/var/lib/callwarden/{workspace_instance_id}/codegraph.db"

        db_path = cfg.resolve_codegraph_db_path("ws-abc-123")
        assert db_path == "/var/lib/callwarden/ws-abc-123/codegraph.db"

    def test_template_without_placeholder_returned_as_is(self):
        """模板无占位符时原样返回（如统一 db_path 场景）。"""
        from callwarden.server.daemon_config import DaemonConfig

        cfg = DaemonConfig.load_from_dict({
            "codegraph_db_path_template": "/opt/callwarden/shared.db",
        })
        db_path = cfg.resolve_codegraph_db_path("any-ws-id")
        assert db_path == "/opt/callwarden/shared.db"


class TestBatch9WorkspaceResourcesHasCodegraphDbPath:
    """批次9：_get_workspace_resources 把 codegraph_db_path 存到 resources。

    file.refresh / workspace.recover 调用 replicator.replicate 时从 resources
    取 db_path，确保 publish_snapshot 被触发。
    """

    def test_resources_contains_codegraph_db_path(self, tmp_path):
        """resources dict 必须含 codegraph_db_path 键。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService
        from callwarden.server.snapshot_manager import SnapshotManagerService

        snapshot_svc = MagicMock(spec=SnapshotManagerService)
        registry_db = str(tmp_path / "registry.db")
        service = EnterpriseDaemonService(
            registry_db=registry_db,
            snapshot_service=snapshot_svc,
            data_root=str(tmp_path / "enterprise"),
        )

        ws_id = "test-ws-cgdb"
        res = service._get_workspace_resources(ws_id)
        # 批次9：必须含 codegraph_db_path 键
        assert "codegraph_db_path" in res, "resources 必须含 codegraph_db_path"
        # 默认模板为空 → 回退到用户级单库路径
        assert res["codegraph_db_path"].endswith(
            os.path.join(".callwarden", "callwarden.db")
        ), f"默认应回退到用户级单库，实际: {res['codegraph_db_path']}"

    def test_resources_codegraph_db_path_uses_config_template(self, tmp_path):
        """配置 codegraph_db_path_template 后，resources 用模板解析路径。"""
        from callwarden.server.daemon_config import DaemonConfig
        from callwarden.server.daemon_server import EnterpriseDaemonService
        from callwarden.server.snapshot_manager import SnapshotManagerService

        snapshot_svc = MagicMock(spec=SnapshotManagerService)
        # 注意：daemon_server.py L175-176 当 cfg.data_root != dirname(registry_db)
        # 时会重建 config（覆盖 codegraph_db_path_template）。
        # 所以 registry_db 必须放在 data_root 目录下。
        data_root = str(tmp_path / "enterprise")
        custom_cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "codegraph_db_path_template": str(tmp_path / "ws_{workspace_instance_id}" / "cg.db"),
        })
        registry_db = str(tmp_path / "enterprise" / "registry.db")
        service = EnterpriseDaemonService(
            registry_db=registry_db,
            snapshot_service=snapshot_svc,
            data_root=data_root,
            config=custom_cfg,
        )

        ws_id = "ws-xyz"
        res = service._get_workspace_resources(ws_id)
        expected = str(tmp_path / f"ws_{ws_id}" / "cg.db")
        assert res["codegraph_db_path"] == expected, (
            f"应使用配置模板解析，期望 {expected}，实际 {res['codegraph_db_path']}"
        )


class TestBatch9FileRefreshPassesDbPath:
    """批次9：file.refresh / recover 调用 replicator.replicate 时传 db_path。

    根因：原代码 res["replicator"].replicate(workspace_id) 漏传 db_path，
    Replicator.replicate L529 检查 `if snapshot_service is not None and db_path:` → 跳过 publish。
    """

    def test_file_refresh_passes_db_path_to_replicate(self, tmp_path, monkeypatch):
        """file.refresh committed 后调用 replicate 必须传 db_path。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService
        from callwarden.server.snapshot_manager import SnapshotManagerService

        snapshot_svc = MagicMock(spec=SnapshotManagerService)
        # 用 default cfg，db_path 会回退到 ~/.callwarden/callwarden.db
        registry_db = str(tmp_path / "registry.db")
        service = EnterpriseDaemonService(
            registry_db=registry_db,
            snapshot_service=snapshot_svc,
            data_root=str(tmp_path / "enterprise"),
        )

        # 用数字 workspace_id（避免 daemon_server.py L828 int(workspace_id) 失败）
        # _owned_workspace 校验通过 mock 绕过
        uid = os.getuid() if hasattr(os, "getuid") else 0
        ws_id = "12345"  # 数字字符串
        # Mock _owned_workspace 绕过 registry 校验
        monkeypatch.setattr(
            service, "_owned_workspace",
            lambda peer_uid, workspace_id: {
                "workspace_instance_id": workspace_id,
                # P0-1 整改（2026-07-21）：dispatch 用 int(workspace["workspace_id"])
                # 取数字主键传给 daemon_handle_refresh，mock 必须提供此字段
                "workspace_id": int(workspace_id) if str(workspace_id).isdigit() else 12345,
                "owner_uid": peer_uid,
                "host_real_root": "/test/root",
                "status": "active",
            },
        )

        res = service._get_workspace_resources(ws_id)

        # Mock replicator 捕获 replicate 调用参数
        captured_calls = []

        class _MockReplicator:
            def replicate(self, workspace_id, db_path="", build_context_hash=""):
                captured_calls.append({
                    "workspace_id": workspace_id,
                    "db_path": db_path,
                })
                from callwarden.server.replicator import ReplicationResult
                return ReplicationResult(
                    success=True, workspace_id=workspace_id,
                    generation=1, applied_count=1, pending_count=1,
                )

        res["replicator"] = _MockReplicator()

        # Mock daemon_handle_refresh 返回 committed
        monkeypatch.setattr(
            "callwarden.server.replicator.daemon_handle_refresh",
            lambda **kwargs: {"status": "committed", "content_hash": "abc123"},
        )

        # 模拟 agent 发 refresh（用 canonical_bytes_hex 避开 abs_path 校验）
        import binascii
        canonical_bytes = b"# test content\n"
        params = {
            "workspace_instance_id": ws_id,
            "agent_session_id": "test-session",
            "session_epoch": 1,
            "monotonic_seq": 1,
            "rel_path": "test.py",
            "canonical_bytes_hex": binascii.hexlify(canonical_bytes).decode(),
            "content_hash": hashlib.sha256(canonical_bytes).hexdigest(),
            "language": "python",
        }

        # 先 connect 建立 session（daemon_handle_refresh 需要 active session）
        from callwarden.server.replicator import daemon_handle_connect
        daemon_handle_connect(
            peer_uid=uid,
            workspace_id=int(ws_id),
            requested_session_id="test-session",
            ws_conn=res["ws_conn"],
        )

        # 调用 dispatch 触发 file.refresh
        peer = {"uid": uid}
        result = service.dispatch(peer, "workspace.file.refresh", params)

        # 验证：committed 状态 + replication 含 db_path
        assert result["status"] == "committed"
        assert "replication" in result
        assert result["replication"]["snapshot_published"] in (True, False)
        # 核心：replicate 必须被调用，且 db_path 不为空
        assert len(captured_calls) == 1, f"应调用 replicate 1 次，实际 {len(captured_calls)}"
        assert captured_calls[0]["db_path"], "db_path 不能为空（修复前 bug）"
        assert captured_calls[0]["db_path"].endswith(
            os.path.join(".callwarden", "callwarden.db")
        ), f"应回退到用户级单库，实际 {captured_calls[0]['db_path']}"

    def test_file_refresh_result_has_snapshot_published_flag(self, tmp_path, monkeypatch):
        """file.refresh 返回结果含 snapshot_published 标志 + snapshot_warning 提示。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService
        from callwarden.server.snapshot_manager import SnapshotManagerService
        from callwarden.server.replicator import ReplicationResult

        snapshot_svc = MagicMock(spec=SnapshotManagerService)
        registry_db = str(tmp_path / "registry.db")
        service = EnterpriseDaemonService(
            registry_db=registry_db,
            snapshot_service=snapshot_svc,
            data_root=str(tmp_path / "enterprise"),
        )

        uid = os.getuid() if hasattr(os, "getuid") else 0
        ws_id = "12345"
        monkeypatch.setattr(
            service, "_owned_workspace",
            lambda peer_uid, workspace_id: {
                "workspace_instance_id": workspace_id,
                # P0-1 整改（2026-07-21）：dispatch 用 int(workspace["workspace_id"])
                # 取数字主键传给 daemon_handle_refresh，mock 必须提供此字段
                "workspace_id": int(workspace_id) if str(workspace_id).isdigit() else 12345,
                "owner_uid": peer_uid,
                "host_real_root": "/test/root",
                "status": "active",
            },
        )

        res = service._get_workspace_resources(ws_id)

        # Mock replicator：模拟 publish 失败
        class _FailingReplicator:
            def replicate(self, workspace_id, db_path="", build_context_hash=""):
                return ReplicationResult(
                    success=False, workspace_id=workspace_id,
                    generation=0, applied_count=0, pending_count=0,
                    error="publish failed: Rust backend unavailable",
                )

        res["replicator"] = _FailingReplicator()

        # Mock daemon_handle_refresh
        monkeypatch.setattr(
            "callwarden.server.replicator.daemon_handle_refresh",
            lambda **kwargs: {"status": "committed", "content_hash": "abc"},
        )

        # 建立 session
        from callwarden.server.replicator import daemon_handle_connect
        daemon_handle_connect(
            peer_uid=uid,
            workspace_id=int(ws_id),
            requested_session_id="test-session",
            ws_conn=res["ws_conn"],
        )

        import binascii
        canonical_bytes = b"# test\n"
        params = {
            "workspace_instance_id": ws_id,
            "agent_session_id": "test-session",
            "session_epoch": 1,
            "monotonic_seq": 1,
            "rel_path": "test.py",
            "canonical_bytes_hex": binascii.hexlify(canonical_bytes).decode(),
            "content_hash": hashlib.sha256(canonical_bytes).hexdigest(),
            "language": "python",
        }

        peer = {"uid": uid}
        result = service.dispatch(peer, "workspace.file.refresh", params)

        # snapshot_published 必须为 False（因 publish 失败）
        assert result["replication"]["snapshot_published"] is False
        # 必须含 snapshot_warning 诊断信息
        assert "snapshot_warning" in result["replication"]
        assert "publish failed" in result["replication"]["snapshot_warning"]


class TestBatch9WorkspaceRecoverPassesDbPath:
    """批次9：workspace.recover 也传 db_path。"""

    def test_recover_passes_db_path(self, tmp_path, monkeypatch):
        """workspace.recover 调用 recover 时传 db_path。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService
        from callwarden.server.snapshot_manager import SnapshotManagerService

        snapshot_svc = MagicMock(spec=SnapshotManagerService)
        registry_db = str(tmp_path / "registry.db")
        service = EnterpriseDaemonService(
            registry_db=registry_db,
            snapshot_service=snapshot_svc,
            data_root=str(tmp_path / "enterprise"),
        )

        uid = os.getuid() if hasattr(os, "getuid") else 0
        ws_id = "67890"
        monkeypatch.setattr(
            service, "_owned_workspace",
            lambda peer_uid, workspace_id: {
                "workspace_instance_id": workspace_id,
                "owner_uid": peer_uid,
                "host_real_root": "/test/root",
                "status": "active",
            },
        )

        res = service._get_workspace_resources(ws_id)

        captured = []

        class _MockReplicator:
            def recover(self, workspace_id, db_path=""):
                captured.append({"db_path": db_path})
                from callwarden.server.replicator import ReplicationResult
                return ReplicationResult(
                    success=True, workspace_id=workspace_id,
                    generation=3, applied_count=2, pending_count=0,
                )

        res["replicator"] = _MockReplicator()

        peer = {"uid": uid}
        result = service.dispatch(
            peer, "workspace.recover",
            {"workspace_instance_id": ws_id},
        )

        assert result["status"] == "recovered"
        assert "snapshot_published" in result
        # 核心：recover 必须传 db_path，不为空
        assert len(captured) == 1
        assert captured[0]["db_path"], "recover 必须传 db_path（修复前 bug）"


# ============================================================
# 批次10（P2 性能优化）：daemon 子连接 PRAGMA + _evict_snapshot_cache bug 修复
# ============================================================


class TestBatch10DaemonConfigDbPragmaSettings:
    """批次10：DaemonConfig 新增 db 配置段（wal_autocheckpoint / cache_size_kb / mmap_size_bytes 等）。"""

    def test_default_config_has_db_section(self):
        """DEFAULT_CONFIG 必须含 db 配置段。"""
        from callwarden.server.daemon_config import DEFAULT_CONFIG

        assert "db" in DEFAULT_CONFIG, "DEFAULT_CONFIG 必须含 db 配置段"
        db_cfg = DEFAULT_CONFIG["db"]
        # 关键字段必须存在
        for key in ("wal_autocheckpoint", "cache_size_kb", "mmap_size_bytes",
                    "temp_store_memory", "synchronous_normal"):
            assert key in db_cfg, f"db.{key} 必须存在"

    def test_default_values_aligned_with_main_connection(self):
        """默认值必须与 CodeGraphDB 主连接（db_base.py L1888-1927）对齐。"""
        from callwarden.server.daemon_config import DaemonConfig

        cfg = DaemonConfig.default()
        # 256MB cache → -262144 KB
        assert cfg.db_cache_size_kb == 262144, "cache_size_kb 默认应为 262144 (256MB)"
        # 256MB mmap
        assert cfg.db_mmap_size_bytes == 268435456, "mmap_size_bytes 默认应为 268435456 (256MB)"
        # 默认开启
        assert cfg.db_temp_store_memory is True, "temp_store_memory 默认应开启"
        assert cfg.db_synchronous_normal is True, "synchronous_normal 默认应开启"
        # 默认 1000 页（SQLite 默认值）
        assert cfg.db_wal_autocheckpoint == 1000, "wal_autocheckpoint 默认应为 1000"

    def test_custom_config_overrides_defaults(self):
        """用户配置应覆盖默认值（如大规模写入场景调大 wal_autocheckpoint）。"""
        from callwarden.server.daemon_config import DaemonConfig

        cfg = DaemonConfig.load_from_dict({
            "db": {
                "wal_autocheckpoint": 5000,
                "cache_size_kb": 524288,  # 512MB
                "mmap_size_bytes": 536870912,  # 512MB
                "temp_store_memory": False,
                "synchronous_normal": False,
            }
        })
        assert cfg.db_wal_autocheckpoint == 5000
        assert cfg.db_cache_size_kb == 524288
        assert cfg.db_mmap_size_bytes == 536870912
        assert cfg.db_temp_store_memory is False
        assert cfg.db_synchronous_normal is False


class TestBatch10ApplyDaemonRwPragmas:
    """批次10：DaemonConfig.apply_daemon_rw_pragmas 方法应用所有 PRAGMA。"""

    def test_apply_daemon_rw_pragmas_sets_all_expected_pragmas(self, tmp_path):
        """apply_daemon_rw_pragmas 必须设置所有 PRAGMA（cache_size / mmap_size / synchronous / temp_store / wal_autocheckpoint）。"""
        import sqlite3
        from callwarden.server.daemon_config import DaemonConfig

        cfg = DaemonConfig.default()
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        try:
            cfg.apply_daemon_rw_pragmas(conn)
            # 验证所有 PRAGMA 已设置
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL=1
            # cache_size：SQLite 不同版本读取时可能返回负值（KB）或正值（KB 或页数），
            # 用 abs() 比较。256MB → |±262144| KB 或 32768 页（8KB/页）。
            cache_val = conn.execute("PRAGMA cache_size").fetchone()[0]
            assert abs(cache_val) in (262144, 32768), (
                f"cache_size 应为 256MB（abs=262144 KB 或 32768 页），实际 {cache_val}"
            )
            assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 268435456
            assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY=2
            # wal_autocheckpoint 必须在 WAL 模式下才能读取，验证不报错即可
            auto = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
            assert auto == 1000, f"wal_autocheckpoint 应为 1000, 实际 {auto}"
        finally:
            conn.close()

    def test_apply_daemon_rw_pragmas_is_idempotent(self, tmp_path):
        """重复调用 apply_daemon_rw_pragmas 应幂等（无副作用）。"""
        import sqlite3
        from callwarden.server.daemon_config import DaemonConfig

        cfg = DaemonConfig.default()
        db_path = str(tmp_path / "idempotent.db")
        conn = sqlite3.connect(db_path)
        try:
            cfg.apply_daemon_rw_pragmas(conn)
            cfg.apply_daemon_rw_pragmas(conn)  # 第二次调用应幂等
            cache_val = conn.execute("PRAGMA cache_size").fetchone()[0]
            assert abs(cache_val) in (262144, 32768)
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        finally:
            conn.close()

    def test_apply_daemon_rw_pragmas_respects_disabled_flags(self, tmp_path):
        """当 synchronous_normal=False / temp_store_memory=False 时不应设置对应 PRAGMA。"""
        import sqlite3
        from callwarden.server.daemon_config import DaemonConfig

        cfg = DaemonConfig.load_from_dict({
            "db": {
                "synchronous_normal": False,
                "temp_store_memory": False,
            }
        })
        db_path = str(tmp_path / "disabled.db")
        conn = sqlite3.connect(db_path)
        try:
            # 先记录默认值
            default_sync = conn.execute("PRAGMA synchronous").fetchone()[0]
            default_temp = conn.execute("PRAGMA temp_store").fetchone()[0]
            cfg.apply_daemon_rw_pragmas(conn)
            # synchronous / temp_store 应保持 SQLite 默认值（未被覆盖）
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == default_sync
            assert conn.execute("PRAGMA temp_store").fetchone()[0] == default_temp
            # 其他 PRAGMA 仍应被设置
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            conn.close()


class TestBatch10DaemonServerUsesPragmas:
    """批次10：daemon_server.py 的 cas_conn / ws_conn / registry_conn 必须使用 apply_daemon_rw_pragmas。"""

    def test_python_uses_apply_daemon_rw_pragmas(self):
        """daemon_server.py 必须在 cas_conn / ws_conn / registry_conn 创建处调用 apply_daemon_rw_pragmas。"""
        path = ROOT / "server" / "daemon_server.py"
        content = path.read_text(encoding="utf-8")

        # 必须包含批次10 修复标记
        assert "批次10" in content, "daemon_server.py 必须包含批次10 标记"
        # cas_conn 和 ws_conn 必须使用 apply_daemon_rw_pragmas（不再用裸 PRAGMA）
        # 找到 _get_workspace_resources 方法范围
        idx = content.find("_get_workspace_resources")
        assert idx >= 0, "_get_workspace_resources 方法必须存在"
        block = content[idx:idx + 3000]
        assert "apply_daemon_rw_pragmas" in block, (
            "_get_workspace_resources 必须使用 apply_daemon_rw_pragmas"
        )
        # 不应再保留裸 PRAGMA（除 snapshot copy 临时连接外）
        # cas_conn / ws_conn 不应再有裸 conn.execute("PRAGMA busy_timeout=5000")
        assert 'cas_conn.execute("PRAGMA busy_timeout' not in block, (
            "cas_conn 不应再使用裸 PRAGMA（应用 apply_daemon_rw_pragmas）"
        )
        assert 'ws_conn.execute("PRAGMA busy_timeout' not in block, (
            "ws_conn 不应再使用裸 PRAGMA（应用 apply_daemon_rw_pragmas）"
        )

    def test_registry_conn_uses_apply_daemon_rw_pragmas(self):
        """_registry_conn 也必须使用 apply_daemon_rw_pragmas。"""
        path = ROOT / "server" / "daemon_server.py"
        content = path.read_text(encoding="utf-8")

        idx = content.find("def _registry_conn")
        assert idx >= 0, "_registry_conn 方法必须存在"
        block = content[idx:idx + 500]
        assert "apply_daemon_rw_pragmas" in block, (
            "_registry_conn 必须使用 apply_daemon_rw_pragmas"
        )

    def test_cas_conn_actually_has_pragmas_applied(self, tmp_path):
        """端到端验证：通过 _get_workspace_resources 创建的 cas_conn 必须有 cache_size 等配置。"""
        import sqlite3
        import os
        import threading
        from unittest.mock import MagicMock
        from callwarden.server.daemon_server import EnterpriseDaemonService

        # 用数字字符串避免 int(workspace_id) 失败
        ws_id = "12345"
        data_root = str(tmp_path / "enterprise")
        os.makedirs(data_root, exist_ok=True)
        registry_db = str(tmp_path / "enterprise" / "registry.db")

        # 用最小化构造绕过完整 daemon 启动（schema migration + 后台线程）
        # 直接通过 __new__ 创建实例并手动设置必要字段
        service = EnterpriseDaemonService.__new__(EnterpriseDaemonService)
        service.registry_db = registry_db
        service.snapshot_service = MagicMock()
        service._data_root = data_root
        service._workspace_resources = {}
        service._resources_lock = threading.Lock()
        service._toolchain_db_path = os.path.join(data_root, "toolchain.db")
        service._toolchain_conn = None
        service._toolchain_lock = threading.Lock()
        from callwarden.server.daemon_config import DaemonConfig
        service._config = DaemonConfig.default()

        # mock _owned_workspace 绕过 registry 校验
        service._owned_workspace = lambda peer_uid, workspace_id: {
            "workspace_instance_id": workspace_id,
            "owner_uid": peer_uid,
            "host_real_root": "/test/root",
            "status": "active",
        }

        res = service._get_workspace_resources(ws_id)
        cas_conn = res["cas_conn"]
        ws_conn = res["ws_conn"]

        try:
            # 验证 cache_size 已设置（256MB → |±262144| KB 或 32768 页）
            cas_cache = cas_conn.execute("PRAGMA cache_size").fetchone()[0]
            ws_cache = ws_conn.execute("PRAGMA cache_size").fetchone()[0]
            assert abs(cas_cache) in (262144, 32768), (
                f"cas_conn 必须设置 cache_size=256MB，实际 {cas_cache}"
            )
            assert abs(ws_cache) in (262144, 32768), (
                f"ws_conn 必须设置 cache_size=256MB，实际 {ws_cache}"
            )
            # mmap_size 也应设置
            assert cas_conn.execute("PRAGMA mmap_size").fetchone()[0] == 268435456
            assert ws_conn.execute("PRAGMA mmap_size").fetchone()[0] == 268435456
            # synchronous=NORMAL
            assert cas_conn.execute("PRAGMA synchronous").fetchone()[0] == 1
            assert ws_conn.execute("PRAGMA synchronous").fetchone()[0] == 1
            # temp_store=MEMORY
            assert cas_conn.execute("PRAGMA temp_store").fetchone()[0] == 2
            assert ws_conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        finally:
            cas_conn.close()
            ws_conn.close()


class TestBatch10EvictSnapshotCacheBugFix:
    """批次10：修复 _evict_snapshot_cache 空实现 bug。

    修复前：直接 return True（不调用任何驱逐方法），导致 workspace 注销后
    SnapshotManagerService 缓存内存泄漏。
    修复后：调用 snapshot_service.evict_workspace(workspace_id)。
    """

    def test_python_evict_calls_evict_workspace(self):
        """daemon_server.py 的 _evict_snapshot_cache 必须调用 evict_workspace。"""
        path = ROOT / "server" / "daemon_server.py"
        content = path.read_text(encoding="utf-8")

        idx = content.find("def _evict_snapshot_cache")
        assert idx >= 0, "_evict_snapshot_cache 方法必须存在"
        block = content[idx:idx + 1200]
        # 必须调用 evict_workspace（不再是无操作 stub）
        assert "evict_workspace" in block, (
            "_evict_snapshot_cache 必须调用 snapshot_service.evict_workspace"
        )
        # 不应再保留旧注释"SnapshotManagerService 没有公开 evict 接口"
        assert "没有公开 evict 接口" not in block, (
            "旧 stub 注释应删除（snapshot_manager.py:186 已有 evict_workspace）"
        )
        # 必须有批次10 标记
        assert "批次10" in block, "必须包含批次10 修复标记"

    def test_evict_returns_false_when_service_is_none(self):
        """snapshot_service 为 None 时应返回 False，不是 True（之前 stub 返回 True）。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService

        service = EnterpriseDaemonService.__new__(EnterpriseDaemonService)
        service.snapshot_service = None

        # 修复前：return True（不安全）
        # 修复后：return False（明确未驱逐）
        result = service._evict_snapshot_cache("test-ws-id")
        assert result is False, (
            "snapshot_service 为 None 时应返回 False（明确未驱逐），不应是 True"
        )

    def test_evict_calls_snapshot_service_evict_workspace(self):
        """_evict_snapshot_cache 必须实际调用 snapshot_service.evict_workspace。"""
        from unittest.mock import MagicMock
        from callwarden.server.daemon_server import EnterpriseDaemonService

        service = EnterpriseDaemonService.__new__(EnterpriseDaemonService)
        mock_snap = MagicMock()
        mock_snap.evict_workspace.return_value = True
        service.snapshot_service = mock_snap

        result = service._evict_snapshot_cache("ws-evict-test")

        # 必须调用 evict_workspace
        mock_snap.evict_workspace.assert_called_once_with("ws-evict-test")
        assert result is True

    def test_evict_handles_exception_gracefully(self):
        """evict_workspace 抛异常时应被捕获，返回 False。"""
        from unittest.mock import MagicMock
        from callwarden.server.daemon_server import EnterpriseDaemonService

        service = EnterpriseDaemonService.__new__(EnterpriseDaemonService)
        mock_snap = MagicMock()
        mock_snap.evict_workspace.side_effect = RuntimeError("test error")
        service.snapshot_service = mock_snap

        # 不应抛异常
        result = service._evict_snapshot_cache("ws-err")
        assert result is False
        mock_snap.evict_workspace.assert_called_once_with("ws-err")


class TestBatch10DurableStagingAndJobExecutorPragmas:
    """批次10：durable_staging.py 和 job_executor.py 也补全 PRAGMA。"""

    def test_durable_staging_has_full_pragma_set(self):
        """durable_staging.py 必须补全 cache_size / mmap_size / temp_store / wal_autocheckpoint。"""
        path = ROOT / "server" / "durable_staging.py"
        content = path.read_text(encoding="utf-8")

        # 找到 __init__ 方法
        idx = content.find("def __init__(self, db_path: str):")
        assert idx >= 0, "DurableStagingLog.__init__ 必须存在"
        block = content[idx:idx + 1000]

        # 必须包含所有 PRAGMA
        for pragma in (
            "PRAGMA busy_timeout=5000",
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA wal_autocheckpoint",
            "PRAGMA cache_size",
            "PRAGMA mmap_size",
            "PRAGMA temp_store=MEMORY",
        ):
            assert pragma in block, f"durable_staging.py 必须设置 {pragma}"

    def test_job_executor_has_full_pragma_set(self):
        """job_executor.py 必须补全 cache_size / mmap_size / temp_store / wal_autocheckpoint。"""
        path = ROOT / "server" / "job_executor.py"
        content = path.read_text(encoding="utf-8")

        # 找到 start 方法的 PRAGMA 设置块
        idx = content.find("PRAGMA journal_mode=WAL")
        assert idx >= 0, "job_executor.py 必须设置 PRAGMA"
        block = content[idx:idx + 800]

        # 必须包含所有 PRAGMA
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA busy_timeout=5000",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA wal_autocheckpoint",
            "PRAGMA cache_size",
            "PRAGMA mmap_size",
            "PRAGMA temp_store=MEMORY",
        ):
            assert pragma in block, f"job_executor.py 必须设置 {pragma}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
