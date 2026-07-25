"""集成测试：Phase 3-8 端到端联调

覆盖新功能在真实链路下的联调：
- Phase 3: CAS 协议（publish + lookup + key 确定性 + 真实 Rust parser 集成）
- Phase 4: Snapshot Manager + QueryBudget 集成
- Phase 5: Daemon IPC（connect + refresh + epoch 校验 + stale seq + end-to-end CAS）
- Phase 8: Schema Migration + Backup/Restore 往返

设计原则：
- 自包含：所有测试用 tmp_path / tempfile，不依赖外部项目
- fail-soft：Rust 扩展不可用时降级验证，不 fail 整个测试
- 真实链路：每一步都通过实际 API 调用，模拟真实 daemon 工作流

关联父任务：T-1783698949011-2740（Enterprise Daemon Shared Snapshot）
"""
import os
import sys
import json
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

# ============================================
# Rust 扩展加载（与 test_phase5_canonicalize.py 相同的路径配置）
# ============================================

_pyinstall = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rust_ext", "target", "pyinstall"
)
if os.path.isdir(_pyinstall):
    sys.path.insert(0, _pyinstall)

from callwarden.db.db_cas import (
    init_cas_schema,
    compute_cas_key_v1,
    cas_lookup,
    cas_publish,
    cas_publish_with_retry,
    cas_pin,
)
from server.replicator import (
    ProtocolError,
    daemon_handle_connect,
    daemon_handle_refresh,
    init_session_schema,
)
from server.query_budget import (
    QueryBudget,
    default_budget,
    shallow_budget,
)
from server.daemon_config import DaemonConfig
from server.schema_migrator import migrate_daemon_dbs
from server.backup_restore import BackupManager, RestoreManager


# ============================================
# 共享 fixture
# ============================================


@pytest.fixture
def tmp_cas_conn():
    """创建内存 CAS DB 并初始化 schema。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_cas_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def tmp_ws_conn():
    """创建内存 workspace DB 并初始化 session schema。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_session_schema(conn)
    yield conn
    conn.close()


def _make_parse_result(symbols=None, raw_calls=None, imports=None):
    """构造一个简单的 parse_result（用于 CAS publish 测试）。"""
    return {
        "symbols": symbols or [],
        "raw_calls": raw_calls or [],
        "imports": imports or [],
        "file_size": 100,
        "total_lines": 5,
    }


def _refresh_msg(session_id: str, epoch: int, seq: int,
                 rel_path: str = "src/main.py") -> dict:
    """构造一条 refresh 消息。"""
    return {
        "rel_path": rel_path,
        "agent_session_id": session_id,
        "monotonic_seq": seq,
        "session_epoch": epoch,
    }


# ============================================
# Phase 3: CAS 协议集成测试
# ============================================


class TestPhase3CASIntegration:
    """Phase 3 CAS 端到端：publish → lookup → key 确定性 → Rust parser 集成"""

    def test_cas_publish_then_lookup_hit(self, tmp_cas_conn):
        """publish 后 lookup 应命中，state='ready'"""
        cas_key = compute_cas_key_v1("hash1", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        parse_result = _make_parse_result(
            symbols=[{"name": "foo", "content": "def foo(): pass",
                      "kind": "function", "qualified_name": "foo",
                      "start_line": 1, "end_line": 1}]
        )
        cas_publish(tmp_cas_conn, cas_key, "hash1", "python", parse_result)

        result = cas_lookup(tmp_cas_conn, cas_key)
        assert result is not None, "CAS lookup 应命中"
        assert result["state"] == "ready"
        assert result["content_hash"] == "hash1"
        assert result["language"] == "python"

    def test_cas_publish_writes_symbols_and_calls(self, tmp_cas_conn):
        """publish 后符号和 raw_calls 都写入对应表"""
        cas_key = compute_cas_key_v1("hash2", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        parse_result = _make_parse_result(
            symbols=[
                {"name": "func1", "content": "def func1(): pass", "kind": "function",
                 "qualified_name": "func1", "start_line": 1, "end_line": 1},
                {"name": "func2", "content": "def func2(): pass", "kind": "function",
                 "qualified_name": "func2", "start_line": 3, "end_line": 3},
            ],
            raw_calls=[{"caller_name": "func1", "callee_name": "func2", "line": 5}],
        )
        cas_publish(tmp_cas_conn, cas_key, "hash2", "python", parse_result)

        symbols = tmp_cas_conn.execute(
            "SELECT * FROM cas_symbols WHERE cas_key = ?", (cas_key,)
        ).fetchall()
        assert len(symbols) == 2, f"应有 2 个符号，实际: {len(symbols)}"

        calls = tmp_cas_conn.execute(
            "SELECT * FROM cas_raw_calls WHERE cas_key = ?", (cas_key,)
        ).fetchall()
        assert len(calls) == 1, f"应有 1 条 raw_call，实际: {len(calls)}"

    def test_cas_key_deterministic_for_same_inputs(self):
        """相同输入产生相同 CAS key（内容寻址核心不变量）"""
        key1 = compute_cas_key_v1("hash1", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        key2 = compute_cas_key_v1("hash1", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        assert key1 == key2, "相同输入应产生相同 key"

    def test_cas_key_differs_for_different_content(self):
        """不同 content_hash 产生不同 CAS key"""
        key1 = compute_cas_key_v1("hash1", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        key2 = compute_cas_key_v1("hash2", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        assert key1 != key2, "不同 content_hash 应产生不同 key"

    def test_cas_publish_with_retry_idempotent(self, tmp_cas_conn):
        """cas_publish_with_retry 二次调用不报错（已 ready 时只补 pin）"""
        cas_key = compute_cas_key_v1("hash3", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        parse_result = _make_parse_result()

        # 第一次发布
        cas_publish_with_retry(tmp_cas_conn, cas_key, "hash3", "python",
                              parse_result, workspace_id=1, max_retries=3)
        assert cas_lookup(tmp_cas_conn, cas_key) is not None

        # 第二次发布（应幂等，不报错，只补 pin）
        cas_publish_with_retry(tmp_cas_conn, cas_key, "hash3", "python",
                              parse_result, workspace_id=1, max_retries=3)
        assert cas_lookup(tmp_cas_conn, cas_key) is not None

        # pin 应存在
        pin = tmp_cas_conn.execute(
            "SELECT * FROM cas_pending_refs WHERE cas_key = ? AND workspace_id = 1",
            (cas_key,)
        ).fetchone()
        assert pin is not None, "二次 publish 后 pin 应存在"

    def test_cas_publish_with_real_rust_parser(self, tmp_cas_conn):
        """使用真实 Rust parser 解析 Python 文件并发布到 CAS"""
        try:
            from callwarden_core import parse_file_lang
        except ImportError:
            pytest.skip("Rust 扩展不可用，跳过真实 parser 集成")

        # 写一个真实的 Python 文件
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
            f.write("def add(a, b):\n    return a + b\n")
            tmp_path = f.name
        try:
            parse_result = parse_file_lang(tmp_path, "", "python")
            assert parse_result is not None, "Rust parser 应返回结果"

            # 发布到 CAS
            import hashlib
            with open(tmp_path, "rb") as raw_f:
                content_hash = hashlib.sha256(raw_f.read()).hexdigest()
            cas_key = compute_cas_key_v1(content_hash, "python", "0.1.0",
                                         "0.2.0", "v1", "v1", "v1")
            cas_publish(tmp_cas_conn, cas_key, content_hash, "python", parse_result)

            # 验证 CAS 命中
            result = cas_lookup(tmp_cas_conn, cas_key)
            assert result is not None, "真实 parser 发布后应能 lookup"
            assert result["state"] == "ready"
            assert result["language"] == "python"
        finally:
            os.unlink(tmp_path)


# ============================================
# Phase 4: Snapshot Manager + QueryBudget 集成
# ============================================


class TestPhase4QueryBudgetIntegration:
    """Phase 4 QueryBudget 端到端：截断 + 节点预算 + 超时"""

    def test_truncate_results_caps_at_max_results(self):
        """truncate_results 截断到 max_results"""
        budget = QueryBudget(max_results=3)
        results = list(range(10))
        truncated = budget.truncate_results(results)
        assert len(truncated) == 3
        assert truncated == [0, 1, 2]

    def test_truncate_results_no_change_when_under_limit(self):
        """结果数 < max_results 时不截断"""
        budget = QueryBudget(max_results=10)
        results = [1, 2, 3]
        truncated = budget.truncate_results(results)
        assert truncated == [1, 2, 3]

    def test_visit_node_enforces_max_nodes(self):
        """visit_node 超过 max_nodes 后返回 False"""
        budget = QueryBudget(max_nodes=3)
        budget.start()
        assert budget.visit_node() is True  # 1
        assert budget.visit_node() is True  # 2
        assert budget.visit_node() is True  # 3
        assert budget.visit_node() is False  # 4 → 超限
        assert budget.exhausted is True
        assert "max_nodes" in budget.exhausted_reason

    def test_visit_node_enforces_timeout(self):
        """visit_node 超时后返回 False"""
        budget = QueryBudget(timeout_ms=1)  # 1ms 超时
        budget.start()
        time.sleep(0.01)  # 等待 10ms 超时
        assert budget.visit_node() is False
        assert budget.exhausted is True
        assert "timeout" in budget.exhausted_reason

    def test_shallow_budget_has_restricted_limits(self):
        """shallow_budget 的限制比 default 更紧"""
        shallow = shallow_budget()
        default = default_budget()
        assert shallow.max_nodes < default.max_nodes
        assert shallow.max_depth < default.max_depth
        assert shallow.timeout_ms < default.timeout_ms


class TestPhase4SnapshotManagerIntegration:
    """Phase 4 SnapshotManagerService 单例 + publish 集成"""

    def test_snapshot_manager_singleton(self):
        """get_instance 返回同一实例"""
        from server.snapshot_manager import SnapshotManagerService
        SnapshotManagerService.reset_instance()
        try:
            svc1 = SnapshotManagerService.get_instance()
            svc2 = SnapshotManagerService.get_instance()
            assert svc1 is svc2, "get_instance 应返回同一单例"
        finally:
            SnapshotManagerService.reset_instance()

    def test_publish_snapshot_returns_dict_or_none(self, tmp_path):
        """publish_snapshot 在 Rust 可用时返回 dict，空 DB 抛 RuntimeError（预期行为）"""
        from server.snapshot_manager import SnapshotManagerService
        SnapshotManagerService.reset_instance()
        try:
            svc = SnapshotManagerService.get_instance()
            if not svc.rust_available:
                # Rust 不可用时 publish_snapshot 返回 None
                result = svc.publish_snapshot(
                    workspace_instance_id="ws_test",
                    db_path=str(tmp_path / "test.db"),
                    build_context_hash="",
                )
                assert result is None, "Rust 不可用时应返回 None"
            else:
                # Rust 可用但空 DB 无业务表时应抛 RuntimeError（预期）
                # Rust build_and_publish 先查 file_instances，空 DB 会报
                # "prepare file_instances query failed: no such table: file_instances"
                db_path = str(tmp_path / "test.db")
                conn = sqlite3.connect(db_path)
                conn.close()
                with pytest.raises(RuntimeError, match="no such table"):
                    svc.publish_snapshot(
                        workspace_instance_id="ws_test",
                        db_path=db_path,
                        build_context_hash="",
                    )
        finally:
            SnapshotManagerService.reset_instance()


# ============================================
# Phase 5: Daemon IPC 集成测试
# ============================================


class TestPhase5DaemonIPCIntegration:
    """Phase 5 Daemon IPC 端到端：connect → refresh → epoch 校验 → CAS"""

    def test_daemon_connect_assigns_increasing_epoch(self, tmp_ws_conn):
        """两次 connect 分配单调递增的 epoch"""
        resp1 = daemon_handle_connect(
            peer_uid=1000, workspace_id=1,
            requested_session_id="s1", ws_conn=tmp_ws_conn
        )
        assert resp1["session_epoch"] == 1

        resp2 = daemon_handle_connect(
            peer_uid=1000, workspace_id=1,
            requested_session_id="s2", ws_conn=tmp_ws_conn
        )
        assert resp2["session_epoch"] == 2

    def test_daemon_connect_revokes_old_session(self, tmp_ws_conn):
        """新 session 连接后旧 session 被撤销"""
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=tmp_ws_conn)
        row = tmp_ws_conn.execute(
            "SELECT revoked_at FROM agent_sessions "
            "WHERE workspace_id=1 AND session_id='s1'"
        ).fetchone()
        assert row["revoked_at"] is None

        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s2", ws_conn=tmp_ws_conn)
        row = tmp_ws_conn.execute(
            "SELECT revoked_at FROM agent_sessions "
            "WHERE workspace_id=1 AND session_id='s1'"
        ).fetchone()
        assert row["revoked_at"] is not None, "s1 应被撤销"

    def test_daemon_refresh_valid_epoch_committed(self, tmp_ws_conn):
        """valid epoch 的 refresh 应返回 committed"""
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=tmp_ws_conn)
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="test.py"),
            ws_conn=tmp_ws_conn,
            cas_conn=None,
        )
        assert resp["status"] == "committed"
        assert resp["generation"] == "1:1"
        # cas_conn=None 时应有 cas_state 字段
        assert "cas_state" in resp

    def test_daemon_refresh_stale_epoch_rejected(self, tmp_ws_conn):
        """stale epoch 的 refresh 应抛 ProtocolError"""
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=tmp_ws_conn)
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s2", ws_conn=tmp_ws_conn)
        # s1 的 epoch=1 已被 s2 的 epoch=2 取代
        with pytest.raises(ProtocolError, match="stale session"):
            daemon_handle_refresh(
                peer_uid=1000, workspace_id=1,
                msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="test.py"),
                ws_conn=tmp_ws_conn,
                cas_conn=None,
            )

    def test_daemon_refresh_stale_seq_dropped(self, tmp_ws_conn):
        """同 epoch 内 stale seq 的 refresh 返回 stale_seq_dropped"""
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=tmp_ws_conn)
        # seq=1 先到
        daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="test.py"),
            ws_conn=tmp_ws_conn,
            cas_conn=None,
        )
        # seq=1 再次到达（stale）→ 应被丢弃
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="test.py"),
            ws_conn=tmp_ws_conn,
            cas_conn=None,
        )
        assert resp["status"] == "stale_seq_dropped"

    def test_daemon_refresh_updates_file_generations(self, tmp_ws_conn):
        """refresh 后 file_generations 表正确更新 seen/committed generation"""
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=tmp_ws_conn)
        daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="src/main.py"),
            ws_conn=tmp_ws_conn,
            cas_conn=None,
        )
        row = tmp_ws_conn.execute(
            "SELECT latest_session_epoch, latest_seq, "
            "latest_seen_generation, latest_committed_generation "
            "FROM file_generations WHERE workspace_id=1 AND rel_path='src/main.py'"
        ).fetchone()
        assert row is not None, "file_generations 应有记录"
        assert row["latest_session_epoch"] == 1
        assert row["latest_seq"] == 1
        assert row["latest_seen_generation"] == "1:1"
        assert row["latest_committed_generation"] == "1:1"

    def test_daemon_refresh_end_to_end_with_cas(self, tmp_ws_conn, tmp_cas_conn):
        """端到端：connect → refresh 真实 .py 文件 → 验证 CAS 发布"""
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=tmp_ws_conn)

        # 写一个真实的 Python 文件
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
            f.write("def add(a, b):\n    return a + b\n")
            tmp_path = f.name
        try:
            msg = _refresh_msg("s1", epoch=1, seq=1, rel_path="test.py")
            msg["abs_path"] = tmp_path
            resp = daemon_handle_refresh(
                peer_uid=1000, workspace_id=1,
                msg=msg,
                ws_conn=tmp_ws_conn,
                cas_conn=tmp_cas_conn,
            )
            assert resp["status"] == "committed"
            assert "cas_key" in resp
            assert "cas_state" in resp
            # cas_state 应该是 ready_published / ready_cache_hit / parse_failed 之一
            # （取决于 Rust parser 是否可用）
            assert resp["cas_state"] in (
                "ready_published", "ready_cache_hit",
                "parse_failed", "no_cas_conn",
                "canonicalize_failed", "unsupported_language",
                "cas_module_unavailable",
            ), f"未预期的 cas_state: {resp['cas_state']}"

            # 若 Rust 可用并成功发布，验证 CAS 表有记录
            if resp["cas_state"] in ("ready_published", "ready_cache_hit"):
                cas_key = resp["cas_key"]
                cas_row = cas_lookup(tmp_cas_conn, cas_key)
                assert cas_row is not None, "CAS 表应有 ready 记录"
                assert cas_row["state"] == "ready"
                assert cas_row["language"] == "python"

            # 验证 file_generations 已 committed
            gen_row = tmp_ws_conn.execute(
                "SELECT latest_seen_generation, latest_committed_generation "
                "FROM file_generations WHERE workspace_id=1 AND rel_path='test.py'"
            ).fetchone()
            assert gen_row is not None
            assert gen_row["latest_seen_generation"] == "1:1"
            assert gen_row["latest_committed_generation"] == "1:1"
        finally:
            os.unlink(tmp_path)

    def test_daemon_refresh_unsupported_language_skips_cas(self, tmp_ws_conn):
        """不支持的文件扩展名 → cas_state=unsupported_language"""
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=tmp_ws_conn)
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="README.unknown"),
            ws_conn=tmp_ws_conn,
            cas_conn=None,
        )
        assert resp["status"] == "committed"
        assert resp["cas_state"] == "unsupported_language"


# ============================================
# Phase 8: Schema Migration + Backup/Restore 集成
# ============================================


class TestPhase8SchemaMigrationIntegration:
    """Phase 8 Schema Migration 端到端"""

    def test_migrate_daemon_dbs_fresh_success(self, tmp_path):
        """在全新 DB 上执行 migrate_daemon_dbs 应成功"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {"admin_uids": [0, 1000]},
        })
        results = migrate_daemon_dbs(cfg)
        assert "registry" in results, "应有 registry 迁移结果"
        assert results["registry"].status != "failed", \
            f"registry 迁移应成功: {results['registry']}"

    def test_migrate_daemon_dbs_idempotent(self, tmp_path):
        """二次 migrate 不报错（幂等）"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {"admin_uids": [0, 1000]},
        })
        # 第一次迁移
        results1 = migrate_daemon_dbs(cfg)
        assert results1["registry"].status != "failed"
        # 第二次迁移（幂等）
        results2 = migrate_daemon_dbs(cfg)
        assert results2["registry"].status != "failed", "二次迁移应幂等成功"

    def test_migrate_creates_registry_tables(self, tmp_path):
        """迁移后 registry DB 应有 daemon_workspaces 表"""
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {"admin_uids": [0, 1000]},
        })
        migrate_daemon_dbs(cfg)

        conn = sqlite3.connect(cfg.registry_db_path)
        conn.row_factory = sqlite3.Row
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "daemon_workspaces" in tables, \
            f"registry DB 应有 daemon_workspaces 表，实际: {tables}"


class TestPhase8BackupRestoreIntegration:
    """Phase 8 Backup/Restore 端到端往返"""

    def test_backup_full_creates_backup_dir(self, tmp_path):
        """backup_full 创建备份目录和文件"""
        data_root = str(tmp_path / "data")
        backup_root = str(tmp_path / "backups")
        os.makedirs(data_root, exist_ok=True)
        os.makedirs(backup_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {"admin_uids": [0, 1000]},
        })
        migrate_daemon_dbs(cfg)

        mgr = BackupManager(cfg, backup_root=backup_root)
        result = mgr.backup_full(backup_id="B-test-001")
        assert result["backup_id"] == "B-test-001"
        assert result["backup_type"] == "full"

        backup_dir = os.path.join(backup_root, "B-test-001")
        assert os.path.isdir(backup_dir), "备份目录应存在"
        # registry.db 应被备份
        assert os.path.isfile(os.path.join(backup_dir, "registry.db")), \
            "registry.db 应被备份"
        # backup_meta.json 应存在
        assert os.path.isfile(os.path.join(backup_dir, "backup_meta.json")), \
            "backup_meta.json 应存在"

    def test_backup_restore_roundtrip(self, tmp_path):
        """备份 → 修改 → 恢复 → 验证数据一致"""
        data_root = str(tmp_path / "data")
        backup_root = str(tmp_path / "backups")
        os.makedirs(data_root, exist_ok=True)
        os.makedirs(backup_root, exist_ok=True)
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "security": {"admin_uids": [0, 1000]},
        })
        migrate_daemon_dbs(cfg)

        # 1. 插入原始数据
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute("""
            INSERT OR REPLACE INTO daemon_workspaces
            (workspace_instance_id, snapshot_id, owner_uid, git_remote_url,
             git_head_commit_sha, client_view_root, host_real_root,
             toolchain_fingerprint, registered_at, last_active_at, status)
            VALUES ('ws-orig', 'snap-orig', 1000, 'origin',
                    'abc123', '/view', '/host', 'tc-fp', ?, ?, 'active')
        """, (time.time(), time.time()))
        conn.commit()
        conn.close()

        # 2. 备份
        backup_mgr = BackupManager(cfg, backup_root=backup_root)
        backup_result = backup_mgr.backup_full(backup_id="B-roundtrip")
        assert backup_result["backup_id"] == "B-roundtrip"

        # 3. 修改 registry（模拟故障/误操作）
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.execute(
            "UPDATE daemon_workspaces SET status='deleted' "
            "WHERE workspace_instance_id='ws-orig'"
        )
        conn.commit()
        conn.close()

        # 验证修改已生效
        conn = sqlite3.connect(cfg.registry_db_path)
        row = conn.execute(
            "SELECT status FROM daemon_workspaces "
            "WHERE workspace_instance_id='ws-orig'"
        ).fetchone()
        assert row[0] == "deleted", "修改后应为 deleted"
        conn.close()

        # 4. 恢复
        restore_mgr = RestoreManager(cfg, backup_root=backup_root)
        restore_result = restore_mgr.restore("B-roundtrip")
        assert restore_result["status"] == "success", \
            f"恢复应成功: {restore_result}"

        # 5. 验证数据已恢复
        conn = sqlite3.connect(cfg.registry_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, owner_uid, git_remote_url "
            "FROM daemon_workspaces WHERE workspace_instance_id='ws-orig'"
        ).fetchone()
        assert row is not None, "恢复后 ws-orig 应存在"
        assert row["status"] == "active", "恢复后应为 active"
        assert row["owner_uid"] == 1000
        assert row["git_remote_url"] == "origin"
        conn.close()
