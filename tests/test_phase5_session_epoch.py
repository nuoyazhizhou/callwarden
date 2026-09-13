"""
Phase 5: Session epoch / generation CAS 协议测试

规范：docs/design/watcher-generation-state-machine.md
修复 T-1783751525743-7c76

A 桶 stale 依据（薄客户端 RPC seam）：
    ``server/replicator.py:364-384 daemon_handle_refresh`` 已 daemon authority 化——
    函数体 ``del peer_uid, workspace_id, ws_conn, cas_conn, workspace_root ...``
    （:379-380）后仅 ``return _call_daemon_rpc(_REPLICATOR_REFRESH_METHOD, params)``
    （:384；方法常量 ``mcp.replicator.daemon_handle_refresh`` 见 :52）。
    本地 SQLite 的 epoch-CAS（stale session 拒绝 / seq 去重 / 两阶段 commit）与
    parse+publish 管道均已移入 Rust daemon（``rust_ext/src/daemon/snapshot_guard.rs``
    等），Python 侧只保留 ``daemon_handle_connect`` 的本地 epoch 分配与
    ``file_generations`` 会话重置（fallback SQL 路径）。
"""

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"))

# 统一走 canonical 包路径（callwarden.server），避免与顶层 `server.*` 形成双模块
# 实例——server/_mcp_common.py:12 `from ..db import CodeGraphDB` 在顶层 server.*
# 包下会「越界相对导入」失败（attempted relative import beyond top-level package）。
from callwarden.server import replicator as R

SESSION_SCHEMA_DDL = R.SESSION_SCHEMA_DDL
ProtocolError = R.ProtocolError
daemon_handle_connect = R.daemon_handle_connect
daemon_handle_refresh = R.daemon_handle_refresh
init_session_schema = R.init_session_schema


# ============================================
# 辅助
# ============================================

def _open_db() -> sqlite3.Connection:
    """打开一个内存 SQLite 并初始化 session schema，返回 row_factory=Row 的连接。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_session_schema(conn)
    return conn


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
# TestSessionSchema —— schema 初始化
# ============================================

class TestSessionSchema:
    """session schema 初始化测试"""

    def test_session_schema_creates_tables(self):
        """init_session_schema 创建 3 张表"""
        conn = _open_db()
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = [r["name"] for r in cur.fetchall()]
        assert "agent_sessions" in names
        assert "workspace_active_session" in names
        assert "file_generations" in names

    def test_session_schema_idempotent(self):
        """重复调用 init_session_schema 不报错"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_session_schema(conn)
        init_session_schema(conn)  # 不应抛异常


# ============================================
# TestDaemonHandleConnect —— 连接握手
# ============================================

class TestDaemonHandleConnect:
    """daemon_handle_connect 测试"""

    def test_daemon_handle_connect_assigns_epoch(self):
        """首次连接 epoch=1，第二次连接 epoch=2"""
        conn = _open_db()
        resp1 = daemon_handle_connect(peer_uid=1000, workspace_id=1,
                                      requested_session_id="s1", ws_conn=conn)
        assert resp1["session_epoch"] == 1

        resp2 = daemon_handle_connect(peer_uid=1000, workspace_id=1,
                                      requested_session_id="s2", ws_conn=conn)
        assert resp2["session_epoch"] == 2

    def test_daemon_handle_connect_revokes_old_session(self):
        """新 session 连接后，旧 session 的 revoked_at 被设置"""
        conn = _open_db()
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=conn)
        # 此时 s1 是 active，revoked_at IS NULL
        row = conn.execute(
            "SELECT revoked_at FROM agent_sessions "
            "WHERE workspace_id=1 AND session_id='s1'"
        ).fetchone()
        assert row["revoked_at"] is None

        # s2 连接 → s1 应被撤销
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s2", ws_conn=conn)
        row = conn.execute(
            "SELECT revoked_at FROM agent_sessions "
            "WHERE workspace_id=1 AND session_id='s1'"
        ).fetchone()
        assert row["revoked_at"] is not None

        # s2 仍 active
        row2 = conn.execute(
            "SELECT revoked_at FROM agent_sessions "
            "WHERE workspace_id=1 AND session_id='s2'"
        ).fetchone()
        assert row2["revoked_at"] is None

    def test_daemon_handle_connect_updates_active_session(self):
        """workspace_active_session 表更新为最新 session"""
        conn = _open_db()
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=conn)
        row = conn.execute(
            "SELECT active_session_id, active_session_epoch "
            "FROM workspace_active_session WHERE workspace_id=1"
        ).fetchone()
        assert row["active_session_id"] == "s1"
        assert row["active_session_epoch"] == 1

        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s2", ws_conn=conn)
        row = conn.execute(
            "SELECT active_session_id, active_session_epoch "
            "FROM workspace_active_session WHERE workspace_id=1"
        ).fetchone()
        assert row["active_session_id"] == "s2"
        assert row["active_session_epoch"] == 2

    def test_daemon_handle_connect_resets_file_generations_seq(self):
        """新 session 连接后，已有 file_generations 的 latest_seq 重置为 0"""
        conn = _open_db()
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=conn)
        # 手动写入一条 file_generations 记录
        conn.execute(
            "INSERT INTO file_generations (workspace_id, rel_path, latest_session_id, "
            "latest_session_epoch, latest_seq, latest_seen_generation, "
            "latest_committed_generation) VALUES (1, 'a.py', 's1', 1, 5, '1:5', '1:5')"
        )
        conn.commit()

        # s2 连接 → latest_seq 应被重置为 0
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s2", ws_conn=conn)
        row = conn.execute(
            "SELECT latest_session_id, latest_session_epoch, latest_seq, "
            "latest_seen_generation FROM file_generations "
            "WHERE workspace_id=1 AND rel_path='a.py'"
        ).fetchone()
        assert row["latest_session_id"] == "s2"
        assert row["latest_session_epoch"] == 2
        assert row["latest_seq"] == 0
        assert row["latest_seen_generation"] == ""


# ============================================
# TestDaemonHandleRefresh —— refresh 薄客户端转发契约
# ============================================

class TestDaemonHandleRefresh:
    """daemon_handle_refresh 薄客户端转发契约（A 桶：RPC seam 迁移）。

    stale 依据：``server/replicator.py:364-384`` 已 daemon authority 化——
    epoch-CAS 判定（stale session 拒绝 / seq 去重 / 两阶段 commit）与
    parse+publish 管道均在 Rust daemon 内执行，Python 侧只剩参数序列化 +
    回包逐字透传。故本类断言现代契约：转发 method 常量与 msg 逐字段、
    canonical_bytes → hex、legacy 形状参数不外发、回包透传、
    RPC 异常 fail-closed（不落本地 SQLite 回退）。
    """

    def _connect(self, workspace_id=1, session_id="s1"):
        conn = _open_db()
        resp = daemon_handle_connect(
            peer_uid=1000, workspace_id=workspace_id,
            requested_session_id=session_id, ws_conn=conn,
        )
        return conn, resp["session_epoch"]

    def _stub(self, monkeypatch, response):
        calls = []

        def fake_rpc(method, params):
            calls.append((method, dict(params)))
            return response

        monkeypatch.setattr(R, "_call_daemon_rpc", fake_rpc)
        return calls

    def test_forwards_method_and_msg_params(self, monkeypatch):
        """转发 method 常量 + msg 逐字段；legacy 形状参数不外发。"""
        conn, epoch = self._connect()
        calls = self._stub(monkeypatch, {"status": "committed", "generation": "1:1"})
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=epoch, seq=1),
            ws_conn=conn, cas_conn=None, workspace_root="/some/root",
        )
        assert resp == {"status": "committed", "generation": "1:1"}
        assert len(calls) == 1
        method, params = calls[0]
        assert method == R._REPLICATOR_REFRESH_METHOD
        assert method == "mcp.replicator.daemon_handle_refresh"
        assert params["rel_path"] == "src/main.py"
        assert params["agent_session_id"] == "s1"
        assert params["session_epoch"] == epoch
        assert params["monotonic_seq"] == 1
        # legacy 形状参数不再外发（daemon 不接收）
        for legacy in ("peer_uid", "workspace_id", "ws_conn", "cas_conn",
                       "workspace_root", "canonical_bytes_hex"):
            assert legacy not in params

    def test_canonical_bytes_serialized_as_hex(self, monkeypatch):
        """canonical_bytes → canonical_bytes_hex；未提供时不出现在 params。"""
        conn, epoch = self._connect()
        calls = self._stub(monkeypatch, {"status": "committed"})
        canonical = b"def foo():\n    pass\n"
        daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=epoch, seq=1),
            ws_conn=conn, canonical_bytes=canonical,
        )
        assert calls[0][1]["canonical_bytes_hex"] == canonical.hex()

        calls.clear()
        daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=epoch, seq=2),
            ws_conn=conn,
        )
        assert "canonical_bytes_hex" not in calls[0][1]

    def test_response_verbatim_passthrough(self, monkeypatch):
        """daemon 回包（含 protection 嵌套）逐字透传，不重写字段。"""
        conn, epoch = self._connect()
        resp = {
            "status": "blocked",
            "cas_state": "parse_failed",
            "protection": {
                "blocked": True,
                "reason": "parse failure",
                "cas_state": "parse_failed",
                "parse_status": "failed",
                "dirty_overlay": False,
                "allows_retry": True,
            },
        }
        self._stub(monkeypatch, resp)
        result = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=epoch, seq=1),
            ws_conn=conn,
        )
        assert result == resp
        assert result["protection"]["blocked"] is True
        assert result["protection"]["allows_retry"] is True

    def test_stale_seq_status_passthrough(self, monkeypatch):
        """daemon 判定 stale_seq_dropped → 原样回包（本地不再做去重判定）。"""
        conn, epoch = self._connect()
        self._stub(monkeypatch, {"status": "stale_seq_dropped"})
        result = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=epoch, seq=1),
            ws_conn=conn,
        )
        assert result["status"] == "stale_seq_dropped"

    def test_daemon_protocol_error_propagates_fail_closed(self, monkeypatch):
        """daemon 抛 ProtocolError → 上抛且不写本地 SQLite（无回退）。"""
        conn, epoch = self._connect()

        def boom(method, params):
            raise ProtocolError("stale session epoch", code="stale_session")

        monkeypatch.setattr(R, "_call_daemon_rpc", boom)
        with pytest.raises(ProtocolError, match="stale session"):
            daemon_handle_refresh(
                peer_uid=1000, workspace_id=1,
                msg=_refresh_msg("s1", epoch=epoch, seq=1),
                ws_conn=conn,
            )
        rows = conn.execute("SELECT * FROM file_generations").fetchall()
        assert rows == []

    def test_rpc_unavailable_propagates(self, monkeypatch):
        """daemon 不可用（非 ProtocolError）同样上抛，不回退本地实现。"""
        conn, epoch = self._connect()

        def boom(method, params):
            raise RuntimeError("daemon unavailable")

        monkeypatch.setattr(R, "_call_daemon_rpc", boom)
        with pytest.raises(RuntimeError, match="daemon unavailable"):
            daemon_handle_refresh(
                peer_uid=1000, workspace_id=1,
                msg=_refresh_msg("s1", epoch=epoch, seq=1),
                ws_conn=conn,
            )


# ============================================
# TestConcurrentSameWorkspace —— 并发不变量
# ============================================

class TestConcurrentSameWorkspace:
    """同一 workspace 并发连接测试（本地 epoch 表）+ refresh 转发"""

    def test_second_connect_revokes_first(self, monkeypatch):
        """S1 connect → S2 connect revoke S1；refresh 由 daemon 判定"""
        conn = _open_db()
        resp1 = daemon_handle_connect(peer_uid=1000, workspace_id=1,
                                      requested_session_id="s1", ws_conn=conn)
        assert resp1["session_epoch"] == 1

        resp2 = daemon_handle_connect(peer_uid=1000, workspace_id=1,
                                      requested_session_id="s2", ws_conn=conn)
        assert resp2["session_epoch"] == 2

        # S1 在本地权威表被撤销（stale 判定本身已下沉 daemon）
        row = conn.execute(
            "SELECT revoked_at FROM agent_sessions "
            "WHERE workspace_id=1 AND session_id='s1'"
        ).fetchone()
        assert row["revoked_at"] is not None

        calls = []
        monkeypatch.setattr(
            R, "_call_daemon_rpc",
            lambda m, p: calls.append((m, dict(p))) or {"status": "committed",
                                                       "generation": "2:1"},
        )
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s2", epoch=2, seq=1),
            ws_conn=conn,
        )
        assert resp["status"] == "committed"
        assert resp["generation"] == "2:1"
        assert calls[0][0] == R._REPLICATOR_REFRESH_METHOD
        assert calls[0][1]["agent_session_id"] == "s2"
        assert calls[0][1]["session_epoch"] == 2

    def test_concurrent_threaded_same_workspace(self, monkeypatch):
        """多线程并发：连接被 barrier 串行化（epoch 不重复），S1 写入由 daemon 拒绝

        规范 §7 test_concurrent_same_workspace_rejected。stale 判定已下沉 daemon：
        本测试断言 Python 侧把 S1 的 refresh 原样转发，且 daemon 抛出的
        ProtocolError 上抛、无本地回退。
        """
        conn = _open_db()
        barrier_both_connected = threading.Barrier(2)
        errors = []
        forwarded = []

        def fake_rpc(method, params):
            forwarded.append((method, dict(params)))
            # daemon 侧：epoch=1 已被 epoch=2 接管 → stale session 拒绝
            if params.get("session_epoch") == 1:
                raise ProtocolError("stale session", code="stale_session")
            return {"status": "committed", "generation": "2:1"}

        monkeypatch.setattr(R, "_call_daemon_rpc", fake_rpc)

        def s1_worker():
            try:
                resp = daemon_handle_connect(peer_uid=1000, workspace_id=1,
                                             requested_session_id="s1", ws_conn=conn)
                assert resp["session_epoch"] == 1
                barrier_both_connected.wait(timeout=5)
                try:
                    daemon_handle_refresh(
                        peer_uid=1000, workspace_id=1,
                        msg=_refresh_msg("s1", epoch=1, seq=1),
                        ws_conn=conn,
                    )
                    errors.append("S1 epoch=1 写入应被 daemon 拒绝（已被 S2 revoke）")
                except ProtocolError:
                    pass  # 预期被拒绝
            except Exception as e:
                errors.append(f"S1 异常: {e}")

        def s2_worker():
            try:
                barrier_both_connected.wait(timeout=5)
                resp = daemon_handle_connect(peer_uid=1000, workspace_id=1,
                                             requested_session_id="s2", ws_conn=conn)
                assert resp["session_epoch"] == 2
            except Exception as e:
                errors.append(f"S2 异常: {e}")

        t1 = threading.Thread(target=s1_worker)
        t2 = threading.Thread(target=s2_worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert errors == [], f"并发写不变量被破坏: {errors}"
        assert forwarded, "refresh 未转发到 daemon"
        assert forwarded[0][0] == R._REPLICATOR_REFRESH_METHOD
        assert forwarded[0][1]["agent_session_id"] == "s1"


# ============================================
# TestSessionEpochMonotonicity —— epoch 单调性
# ============================================

class TestSessionEpochMonotonicity:
    """session_epoch 单调递增不变量（W1）"""

    def test_epoch_monotonic_increase(self):
        """多次连接的 epoch 严格递增"""
        conn = _open_db()
        epochs = []
        for i in range(5):
            resp = daemon_handle_connect(
                peer_uid=1000, workspace_id=1,
                requested_session_id=f"s{i}", ws_conn=conn,
            )
            epochs.append(resp["session_epoch"])
        assert epochs == [1, 2, 3, 4, 5]

    def test_epoch_per_workspace_independent(self):
        """不同 workspace 的 epoch 互相独立"""
        conn = _open_db()
        # ws1 第一次连接
        r1 = daemon_handle_connect(peer_uid=1000, workspace_id=1,
                                   requested_session_id="s1", ws_conn=conn)
        assert r1["session_epoch"] == 1
        # ws2 第一次连接（epoch 也是 1）
        r2 = daemon_handle_connect(peer_uid=1000, workspace_id=2,
                                   requested_session_id="s2", ws_conn=conn)
        assert r2["session_epoch"] == 1
        # ws1 第二次连接 → epoch=2
        r3 = daemon_handle_connect(peer_uid=1000, workspace_id=1,
                                   requested_session_id="s3", ws_conn=conn)
        assert r3["session_epoch"] == 2
        # ws2 仍是 epoch=1 active
        row = conn.execute(
            "SELECT active_session_epoch FROM workspace_active_session "
            "WHERE workspace_id=2"
        ).fetchone()
        assert row["active_session_epoch"] == 1


# ============================================
# TestFileGenerationsDedup —— file_generations 去重 / 会话重置
# ============================================

class TestFileGenerationsDedup:
    """file_generations 会话重置（本地）+ refresh 转发路由"""

    def test_distinct_files_forwarded_separately(self, monkeypatch):
        """不同文件的 refresh 各自携带独立 rel_path/seq 转发 daemon。"""
        conn = _open_db()
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=conn)
        calls = []
        monkeypatch.setattr(
            R, "_call_daemon_rpc",
            lambda m, p: calls.append((m, dict(p))) or {"status": "committed"},
        )

        daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="a.py"),
            ws_conn=conn,
        )
        daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=2, rel_path="b.py"),
            ws_conn=conn,
        )

        assert [(c[1]["rel_path"], c[1]["monotonic_seq"]) for c in calls] == [
            ("a.py", 1), ("b.py", 2),
        ]

    def test_new_session_resets_all_files(self, monkeypatch):
        """新 session 连接后，所有 file_generations 的 latest_seq 都重置"""
        conn = _open_db()
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=conn)

        # refresh 已不写本地库（daemon authority），故直接种入两行
        conn.execute(
            "INSERT INTO file_generations (workspace_id, rel_path, latest_session_id, "
            "latest_session_epoch, latest_seq, latest_seen_generation, "
            "latest_committed_generation) "
            "VALUES (1, 'a.py', 's1', 1, 7, '1:7', '1:7')"
        )
        conn.execute(
            "INSERT INTO file_generations (workspace_id, rel_path, latest_session_id, "
            "latest_session_epoch, latest_seq, latest_seen_generation, "
            "latest_committed_generation) "
            "VALUES (1, 'b.py', 's1', 1, 3, '1:3', '1:3')"
        )
        conn.commit()

        # S2 连接 → 全量重置为 latest_seq=0
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s2", ws_conn=conn)

        rows = conn.execute(
            "SELECT rel_path, latest_seq, latest_session_id, latest_seen_generation "
            "FROM file_generations WHERE workspace_id=1 ORDER BY rel_path"
        ).fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row["latest_seq"] == 0
            assert row["latest_session_id"] == "s2"
            assert row["latest_seen_generation"] == ""

        # S2 从 seq=1 重新开始 → 转发 daemon（epoch=2）
        calls = []
        monkeypatch.setattr(
            R, "_call_daemon_rpc",
            lambda m, p: calls.append((m, dict(p))) or {"status": "committed",
                                                       "generation": "2:1"},
        )
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s2", epoch=2, seq=1, rel_path="a.py"),
            ws_conn=conn,
        )
        assert resp["status"] == "committed"
        assert resp["generation"] == "2:1"
        assert calls[0][1]["session_epoch"] == 2


# ============================================
# TestDaemonParsePublishPipeline —— parse + CAS publish 管道（已下沉 daemon）
# ============================================

class TestDaemonParsePublishPipeline:
    """parse + CAS publish 管道已在 daemon 侧（A 桶 stale 依据）。

    stale 依据：``server/replicator.py:410 _daemon_parse_and_publish`` 已非
    ``daemon_handle_refresh`` 的调用路径（:364-384 只做参数序列化 + RPC）；
    re-canonicalize / re-hash / Rust parse / CAS publish /
    失败 generation 保护均在 Rust daemon（``rust_ext/src/daemon/snapshot_guard.rs``）。
    故本类断言薄客户端把 daemon 的 ``cas_state`` 回包透传，且不接收本地
    ``cas_conn`` / ``workspace_root`` 形状参数。
    """

    def _connect(self, session_id="s1"):
        conn = _open_db()
        resp = daemon_handle_connect(peer_uid=1000, workspace_id=1,
                                     requested_session_id=session_id, ws_conn=conn)
        return conn, resp["session_epoch"]

    def _stub(self, monkeypatch, response):
        calls = []
        monkeypatch.setattr(
            R, "_call_daemon_rpc",
            lambda m, p: calls.append((m, dict(p))) or response,
        )
        return calls

    def test_cas_state_passthrough_when_daemon_skips_cas(self, monkeypatch):
        """daemon 返回 no_cas_conn → cas_state 原样透传（本地不再判定）。"""
        conn, _ = self._connect()
        calls = self._stub(monkeypatch, {"status": "committed", "cas_state": "no_cas_conn"})
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="test.py"),
            ws_conn=conn,
            cas_conn=None,
        )
        assert resp["status"] == "committed"
        assert resp["cas_state"] == "no_cas_conn"
        assert "cas_conn" not in calls[0][1]

    def test_abs_path_from_msg_forwarded(self, monkeypatch):
        """msg 携带 abs_path 时原样转发给 daemon。"""
        conn, _ = self._connect()
        calls = self._stub(monkeypatch, {"status": "committed"})
        msg = _refresh_msg("s1", epoch=1, seq=1, rel_path="test.py")
        msg["abs_path"] = "/nonexistent/path/test.py"
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=msg,
            ws_conn=conn,
            cas_conn=None,
        )
        assert resp["status"] == "committed"
        assert calls[0][1]["abs_path"] == "/nonexistent/path/test.py"

    def test_workspace_root_argument_not_forwarded(self, monkeypatch):
        """legacy workspace_root 形状参数不再外发（abs_path 由 daemon 推导）。"""
        conn, _ = self._connect()
        calls = self._stub(monkeypatch, {"status": "committed"})
        daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="src/main.py"),
            ws_conn=conn,
            cas_conn=None,
            workspace_root="/some/root",
        )
        assert "workspace_root" not in calls[0][1]

    def test_unsupported_language_cas_state_passthrough(self, monkeypatch):
        """daemon 判定 unsupported_language → cas_state 原样透传。"""
        conn, _ = self._connect()
        self._stub(monkeypatch, {"status": "committed",
                                 "cas_state": "unsupported_language"})
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="README.unknown"),
            ws_conn=conn,
            cas_conn=None,
        )
        assert resp["status"] == "committed"
        assert resp["cas_state"] == "unsupported_language"

    def test_cas_conn_is_ignored_by_thin_client(self, monkeypatch):
        """显式传入本地 cas_conn 也不改变转发参数（薄客户端不读本地 CAS）。"""
        cas_conn = sqlite3.connect(":memory:", check_same_thread=False)
        cas_conn.row_factory = sqlite3.Row
        conn, _ = self._connect()
        calls = self._stub(monkeypatch, {"status": "committed", "cas_key": "k",
                                        "cas_state": "ready_published"})
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=1, rel_path="test.py"),
            ws_conn=conn,
            cas_conn=cas_conn,
        )
        assert resp["cas_key"] == "k"
        assert resp["cas_state"] == "ready_published"
        assert "cas_conn" not in calls[0][1]
        cas_conn.close()


class TestJoinPath:
    """_join_path 辅助函数"""

    def test_join_path_basic(self):
        from callwarden.server.replicator import _join_path
        assert _join_path("/root", "src/main.py") == "/root/src/main.py"

    def test_join_path_trailing_slash(self):
        from callwarden.server.replicator import _join_path
        assert _join_path("/root/", "src/main.py") == "/root/src/main.py"

    def test_join_path_leading_slash_in_rel(self):
        from callwarden.server.replicator import _join_path
        assert _join_path("/root", "/src/main.py") == "/root/src/main.py"

    def test_join_path_windows_backslash(self):
        from callwarden.server.replicator import _join_path
        assert _join_path("C:\\root", "src\\main.py") == "c:/root/src/main.py"

    def test_join_path_empty_root(self):
        from callwarden.server.replicator import _join_path
        assert _join_path("", "src/main.py") == "src/main.py"


# ============================================
# G9 auto-reconnect：ProtocolError.code 字段透传
# ============================================


class TestProtocolErrorCodeField:
    """G9 auto-reconnect 前提：ProtocolError 携带语义化 code 字段。

    daemon_server.py 识别 replicator.ProtocolError 后透传 code 作为
    DaemonRpcError.code → DaemonRemoteError.code → AgentProtocolError.code，
    agent_watcher 据此决定是否触发 auto-reconnect。
    """

    def test_no_active_session_error_propagates_with_code(self, monkeypatch):
        """daemon 返回 session_not_active → ProtocolError 上抛且 code 保留。"""
        conn = _open_db()

        def boom(method, params):
            raise ProtocolError("no active session for workspace 1",
                                code="session_not_active")

        monkeypatch.setattr(R, "_call_daemon_rpc", boom)
        with pytest.raises(ProtocolError) as exc_info:
            daemon_handle_refresh(
                peer_uid=1000, workspace_id=1,
                msg=_refresh_msg("s1", epoch=1, seq=1),
                ws_conn=conn,
            )
        assert exc_info.value.code == "session_not_active"
        assert "no active session" in exc_info.value.message

    def test_stale_session_error_propagates_with_code(self, monkeypatch):
        """daemon 返回 stale_session → ProtocolError 上抛且 code 保留。"""
        conn = _open_db()
        daemon_handle_connect(peer_uid=1000, workspace_id=1,
                              requested_session_id="s1", ws_conn=conn)

        def boom(method, params):
            raise ProtocolError("stale session epoch", code="stale_session")

        monkeypatch.setattr(R, "_call_daemon_rpc", boom)
        with pytest.raises(ProtocolError) as exc_info:
            daemon_handle_refresh(
                peer_uid=1000, workspace_id=1,
                msg=_refresh_msg("s1", epoch=1, seq=1),
                ws_conn=conn,
            )
        assert exc_info.value.code == "stale_session"

    def test_stale_manifest_commit_error_carries_code(self):
        """ProtocolError 显式构造 stale_manifest_commit code（构造性验证）。

        stale manifest commit 判定已下沉 daemon（Rust 侧 CAS 第二阶段），
        Python 侧只负责把 code 透传给上层；此处直接断言 ProtocolError
        支持 stale_manifest_commit code 字段，覆盖序列化/透传路径。
        """
        err = ProtocolError(
            "stale manifest commit for main.py",
            code="stale_manifest_commit",
        )
        assert err.code == "stale_manifest_commit"
        assert "stale manifest commit" in err.message
        # 注意：此 code 不属于 auto-reconnect 触发集合
        # agent_watcher._handle_single_change 只对 session_not_active/stale_session 触发重连
        assert err.code not in ("session_not_active", "stale_session")

    def test_protocol_error_default_code(self):
        """ProtocolError 不传 code 时默认 'protocol_error'（向后兼容）。"""
        err = ProtocolError("test error")
        assert err.code == "protocol_error"
        assert err.message == "test error"

    def test_protocol_error_explicit_code(self):
        """ProtocolError 显式传 code 时保留。"""
        err = ProtocolError("msg", code="custom_code")
        assert err.code == "custom_code"
        assert err.message == "msg"

    def test_protocol_error_str_format(self):
        """ProtocolError 的 str 表示包含 message（向后兼容 match=... 测试）。"""
        err = ProtocolError("no active session for workspace 1",
                            code="session_not_active")
        # pytest.raises(ProtocolError, match="no active session") 依赖 str 表示
        assert "no active session" in str(err)
