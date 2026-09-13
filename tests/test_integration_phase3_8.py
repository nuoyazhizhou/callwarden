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

stale 依据（daemon authority / HTTP thin-client 迁移）
====================================================
- **A 类（被测模块已是 daemon 薄客户端，改 mock RPC seam）**
  * `server/replicator.py:364-384 daemon_handle_refresh`：签名仅为兼容形状，实际
    只做参数序列化后 `_call_daemon_rpc("mcp.replicator.daemon_handle_refresh",
    params)`；session epoch / stale seq / CAS 两阶段 / generation 推进全部在 Rust
    daemon。旧用例「用本地 ws_conn/cas_conn 断言 epoch、file_generations 表」已过期。
    （`daemon_handle_connect` 仍为本地实现，其断言保留。）
  * `server/schema_migrator.py:1-6/146-202`：`migrate_daemon_dbs` 由 daemon 经
    `mcp.schema_migrator.apply_migrations` 执行，旧用例「本地建 registry 表」已过期。
- **仍本地，保留断言**：`server/backup_restore.py` `BackupManager` /
  `RestoreManager` 的 Python fallback（`_RUST_BACKUP_MANAGER_AVAILABLE=False`，
  本地文件系统复制/恢复）；`db/db_daemon.py:15-61 init_daemon_schema` 仍提供本地
  registry DDL，用于为本地 fallback 组件准备 schema。
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
from callwarden.server.replicator import (
    ProtocolError,
    daemon_handle_connect,
    daemon_handle_refresh,
    init_session_schema,
)
from callwarden.server.query_budget import (
    QueryBudget,
    default_budget,
    shallow_budget,
)
from callwarden.server.daemon_config import DaemonConfig
from callwarden.server.daemon_client import DaemonUnavailableError
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.schema_migrator import migrate_daemon_dbs
from callwarden.server.backup_restore import BackupManager, RestoreManager


# daemon RPC method 命名空间（生产侧见 server/replicator.py:52 /
# server/schema_migrator.py:19-22）
REPLICATOR_REFRESH_METHOD = "mcp.replicator.daemon_handle_refresh"
SCHEMA_APPLY_METHOD = "mcp.schema_migrator.apply_migrations"


class _RpcRecorder:
    """daemon RPC 替身：记录 (method, params) 并回放预设 payload / 抛预设异常。

    用于在被测模块已成为 daemon 薄客户端后，断言「路由到正确 RPC + 参数正确 +
    回包透传 + 失败 fail-closed（不回退本地）」这一现代契约。
    """

    def __init__(self, reply=None, raise_exc=None):
        self.calls = []
        self._reply = reply
        self._raise = raise_exc

    def reply(self, payload):
        self._reply = payload
        return self

    def raise_with(self, exc):
        self._raise = exc
        return self

    def __call__(self, method, params):
        self.calls.append((method, dict(params or {})))
        if self._raise is not None:
            raise self._raise
        return self._reply

    @property
    def methods(self):
        return [m for m, _ in self.calls]

    def last_params(self):
        return self.calls[-1][1] if self.calls else None


def _init_local_registry(cfg: DaemonConfig) -> None:
    """建立本地 registry schema（仅服务仍为本地实现的 fallback 组件）。

    daemon authority 迁移后 ``migrate_daemon_dbs`` 不再在本地建表；而
    ``BackupManager`` / ``RestoreManager`` 的 Python fallback 仍直接复制/恢复
    本地 DB 文件，故此处复用 ``db/db_daemon.init_daemon_schema`` 的本地 DDL。
    """
    from callwarden.db.db_daemon import init_daemon_schema

    conn = sqlite3.connect(cfg.registry_db_path)
    try:
        init_daemon_schema(conn)
    finally:
        conn.close()



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


@pytest.fixture
def replicator_daemon(monkeypatch):
    """把 `server.replicator._call_daemon_rpc` 替换为记录型替身（A 类 mock seam）。"""
    rec = _RpcRecorder()
    monkeypatch.setattr("callwarden.server.replicator._call_daemon_rpc", rec)
    return rec


@pytest.fixture
def schema_daemon(monkeypatch):
    """把 `server.schema_migrator._call_daemon_rpc` 替换为记录型替身（A 类 mock seam）。"""
    rec = _RpcRecorder()
    monkeypatch.setattr("callwarden.server.schema_migrator._call_daemon_rpc", rec)
    return rec


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
        from callwarden.server.snapshot_manager import SnapshotManagerService
        SnapshotManagerService.reset_instance()
        try:
            svc1 = SnapshotManagerService.get_instance()
            svc2 = SnapshotManagerService.get_instance()
            assert svc1 is svc2, "get_instance 应返回同一单例"
        finally:
            SnapshotManagerService.reset_instance()

    def test_publish_snapshot_returns_dict_or_none(self, tmp_path):
        """publish_snapshot 在 Rust 可用时返回 dict，空 DB 抛 RuntimeError（预期行为）"""
        from callwarden.server.snapshot_manager import SnapshotManagerService
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

    def test_daemon_refresh_valid_epoch_committed(self, tmp_ws_conn, replicator_daemon):
        """A 类：refresh 路由到 daemon RPC，参数序列化正确并透传 committed 回包。"""
        replicator_daemon.reply({
            "status": "committed", "generation": "1:1", "cas_state": "no_cas_conn",
        })
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="test.py"),
            ws_conn=tmp_ws_conn,
            cas_conn=None,
        )
        assert resp["status"] == "committed"
        assert resp["generation"] == "1:1"
        assert resp["cas_state"] == "no_cas_conn"
        assert replicator_daemon.methods == [REPLICATOR_REFRESH_METHOD]
        params = replicator_daemon.last_params()
        assert params["rel_path"] == "test.py"
        assert params["agent_session_id"] == "s1"
        assert params["monotonic_seq"] == 1
        assert params["session_epoch"] == 1

    def test_daemon_refresh_stale_epoch_rejected(self, tmp_ws_conn, replicator_daemon):
        """A 类：stale epoch 由 daemon 拒绝，Python 侧透传 DaemonRemoteError。"""
        replicator_daemon.raise_with(
            DaemonRemoteError("stale_session", "stale session epoch")
        )
        with pytest.raises(DaemonRemoteError) as ei:
            daemon_handle_refresh(
                peer_uid=1000, workspace_id=1,
                msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="test.py"),
                ws_conn=tmp_ws_conn,
                cas_conn=None,
            )
        assert ei.value.code == "stale_session"
        assert replicator_daemon.methods == [REPLICATOR_REFRESH_METHOD]

    def test_daemon_refresh_stale_seq_dropped(self, tmp_ws_conn, replicator_daemon):
        """A 类：同 epoch 内 stale seq 由 daemon 判定，回包透传 stale_seq_dropped。"""
        replicator_daemon.reply({"status": "stale_seq_dropped"})
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="test.py"),
            ws_conn=tmp_ws_conn,
            cas_conn=None,
        )
        assert resp["status"] == "stale_seq_dropped"

    def test_daemon_refresh_updates_file_generations(self, tmp_ws_conn, replicator_daemon):
        """A 类：generation 推进在 daemon 侧；Python 只透传回包字段。"""
        replicator_daemon.reply({
            "status": "committed",
            "generation": "1:1",
            "latest_seen_generation": "1:1",
            "latest_committed_generation": "1:1",
        })
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="src/main.py"),
            ws_conn=tmp_ws_conn,
            cas_conn=None,
        )
        assert resp["latest_seen_generation"] == "1:1"
        assert resp["latest_committed_generation"] == "1:1"
        assert replicator_daemon.last_params()["rel_path"] == "src/main.py"

    def test_daemon_refresh_end_to_end_with_cas(self, tmp_ws_conn, replicator_daemon):
        """A 类：端到端参数序列化——canonical_bytes 转 hex，daemon 回包透传。"""
        replicator_daemon.reply({
            "status": "committed", "generation": "1:1",
            "cas_state": "ready_published", "cas_key": "k1",
        })
        canonical = b'{"rel_path":"test.py"}'
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="test.py"),
            ws_conn=tmp_ws_conn,
            cas_conn=None,
            canonical_bytes=canonical,
        )
        assert resp["status"] == "committed"
        assert resp["cas_state"] == "ready_published"
        assert resp["cas_key"] == "k1"
        params = replicator_daemon.last_params()
        assert params["canonical_bytes_hex"] == canonical.hex()

    def test_daemon_refresh_unsupported_language_skips_cas(self, tmp_ws_conn, replicator_daemon):
        """A 类：不支持扩展名由 daemon 判定 unsupported_language，回包透传。"""
        replicator_daemon.reply({
            "status": "committed", "cas_state": "unsupported_language",
        })
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="README.unknown"),
            ws_conn=tmp_ws_conn,
            cas_conn=None,
        )
        assert resp["status"] == "committed"
        assert resp["cas_state"] == "unsupported_language"

    def test_daemon_refresh_unavailable_is_fail_closed(self, tmp_ws_conn, monkeypatch):
        """A 类 fail-closed：daemon 不可用抛错，绝不回退本地写 file_generations。"""
        def _boom(method, params):
            raise DaemonUnavailableError("E_HTTP_DAEMON_UNAVAILABLE")

        monkeypatch.setattr("callwarden.server.replicator._call_daemon_rpc", _boom)
        with pytest.raises(DaemonUnavailableError):
            daemon_handle_refresh(
                peer_uid=1000, workspace_id=1,
                msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="test.py"),
                ws_conn=tmp_ws_conn,
                cas_conn=None,
            )
        count = tmp_ws_conn.execute(
            "SELECT COUNT(*) FROM file_generations"
        ).fetchone()[0]
        assert count == 0, "daemon 不可用时不得在本地写入 file_generations"


# ============================================
# Phase 8: Schema Migration + Backup/Restore 集成
# ============================================


def _phase8_cfg(tmp_path) -> DaemonConfig:
    data_root = str(tmp_path / "data")
    os.makedirs(data_root, exist_ok=True)
    return DaemonConfig.load_from_dict({
        "data_root": data_root,
        # 显式指向不存在路径，避免默认 audit_log_path 命中宿主机已有文件导致
        # migrate_daemon_dbs 额外触发一次 audit RPC（使断言非确定）。
        "security": {
            "admin_uids": [0, 1000],
            "audit_log_path": str(tmp_path / "no_such_audit.db"),
        },
    })


class TestPhase8SchemaMigrationIntegration:
    """Phase 8 Schema Migration 端到端（A 类：daemon 薄客户端）

    stale 依据：`server/schema_migrator.py:1-6/183-202 migrate_daemon_dbs`
    现仅做参数序列化后经 `mcp.schema_migrator.apply_migrations` 请求 daemon，
    DDL/迁移版本决策/历史全部在 Rust daemon，失败不回退 Python SQLite。
    旧用例「在本地 registry DB 上建 daemon_workspaces 表」已过期，改为断言
    RPC 路由/参数/回包透传 + fail-closed（本地不得落盘）。
    """

    def test_migrate_daemon_dbs_routes_apply_rpc(self, tmp_path, schema_daemon):
        """migrate_daemon_dbs 应路由到 apply_migrations RPC 并透传结果"""
        cfg = _phase8_cfg(tmp_path)
        schema_daemon.reply({
            "db_path": cfg.registry_db_path,
            "from_version": 0,
            "to_version": 3,
            "applied": [1, 2, 3],
            "skipped": [],
            "failed": None,
            "error": None,
        })
        results = migrate_daemon_dbs(cfg)

        assert "registry" in results, "应有 registry 迁移结果"
        assert results["registry"].status == "migrated"
        assert results["registry"].applied == [1, 2, 3]
        # 路由到正确 RPC method + 正确 params
        assert schema_daemon.methods == [SCHEMA_APPLY_METHOD]
        params = schema_daemon.last_params()
        assert params["db_path"] == cfg.registry_db_path
        assert params["migration_set"] == "registry"

    def test_migrate_daemon_dbs_idempotent(self, tmp_path, schema_daemon):
        """二次 migrate 仍幂等经 RPC；每次都是 daemon 端决策"""
        cfg = _phase8_cfg(tmp_path)
        schema_daemon.reply({
            "db_path": cfg.registry_db_path,
            "from_version": 3,
            "to_version": 3,
            "applied": [],
            "skipped": [1, 2, 3],
            "failed": None,
            "error": None,
        })
        results1 = migrate_daemon_dbs(cfg)
        results2 = migrate_daemon_dbs(cfg)
        assert results1["registry"].status == "up_to_date"
        assert results2["registry"].status == "up_to_date"
        assert schema_daemon.methods == [SCHEMA_APPLY_METHOD, SCHEMA_APPLY_METHOD]

    def test_migrate_does_not_write_local_registry(self, tmp_path, schema_daemon):
        """fail-closed：daemon 迁移不得在 Python 侧建本地 registry DB"""
        cfg = _phase8_cfg(tmp_path)
        schema_daemon.reply({
            "db_path": cfg.registry_db_path,
            "from_version": 0,
            "to_version": 3,
            "applied": [1, 2, 3],
            "skipped": [],
            "failed": None,
            "error": None,
        })
        migrate_daemon_dbs(cfg)

        assert not os.path.isfile(cfg.registry_db_path), \
            "daemon 迁移后 Python 侧不应创建本地 registry DB"

    def test_migrate_unavailable_is_fail_closed(self, tmp_path, schema_daemon):
        """daemon 不可用时应抛错且不落本地 DB"""
        cfg = _phase8_cfg(tmp_path)
        schema_daemon.raise_with(
            DaemonUnavailableError("mcp.schema_migrator.apply_migrations")
        )
        with pytest.raises(DaemonUnavailableError):
            migrate_daemon_dbs(cfg)
        assert not os.path.isfile(cfg.registry_db_path), \
            "daemon 不可用时不得回退到本地 SQLite"


class TestPhase8BackupRestoreIntegration:
    """Phase 8 Backup/Restore 端到端往返（B 类：备份/恢复仍为本地 fallback）

    stale 依据：`server/backup_restore.py` 的 `BackupManager` / `RestoreManager`
    在 `_RUST_BACKUP_MANAGER_AVAILABLE=False` 时仍直接复制/恢复本地 DB 文件，
    故这里不再调用已 RPC 化的 `migrate_daemon_dbs`（会 fail-closed），改用
    `db/db_daemon.init_daemon_schema` 直接准备本地 registry schema。
    """

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
        _init_local_registry(cfg)

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
        _init_local_registry(cfg)

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
