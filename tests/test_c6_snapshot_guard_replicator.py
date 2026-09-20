"""C6（S2）测试：Python daemon 失败 generation 保护（snapshot_guard 镜像）。

任务：T-1785590602456-0cac3cab-sub-3（S2：Snapshot publish/load 统一）
规范：c5-replicator-snapshot-disaster-recovery-contract.md §3 C6 + §7
对齐参考：rust_ext/src/daemon/snapshot_guard.rs（evaluate_generation_protection）

S2 验收点（本文件覆盖）：
- 验收点 5：任一步失败时，latest_committed_generation 不得推进。
- 验收点 7：不允许 partial snapshot 被查询命中（partial_published → blocked）。

覆盖：
1. _is_dirty_overlay_path 各模式（.git / .callwarden / .bak / ~ / .orig / .rej）
2. _evaluate_generation_protection 状态分类（failed / unsupported / stale / partial / success）
3. daemon_handle_refresh blocked 行为（parse_failed → blocked，committed 不推进）
4. daemon_handle_refresh ready 状态 → committed（回归）
5. dirty overlay 路径 → blocked（即使 cas_state=ready_published）
6. cas_conn=None（无 CAS 主链）→ 不启用保护，committed（回归）
7. daemon_server handler blocked 分支（snapshot_published=False + warning，不 replicate）
"""

import hashlib
import os
import sqlite3
from unittest.mock import MagicMock

import pytest

# ============================================================
# 单元：_is_dirty_overlay_path
# ============================================================


class TestIsDirtyOverlayPath:
    """镜像 Rust snapshot_guard.rs::is_dirty_overlay 的路径分类。"""

    def _fn(self):
        from callwarden.server.replicator import _is_dirty_overlay_path
        return _is_dirty_overlay_path

    def test_normal_path_not_dirty(self):
        fn = self._fn()
        assert fn("/work/src/main.py", "src/main.py") is False

    def test_git_dir_rejected(self):
        fn = self._fn()
        assert fn("/work/.git/config", ".git/config") is True
        assert fn("/work/.git/HEAD", "HEAD") is True  # abs_path 命中

    def test_callwarden_dir_rejected(self):
        fn = self._fn()
        assert fn("/work/.callwarden/tmp.db", ".callwarden/tmp.db") is True
        assert fn("/home/u/.callwarden/callwarden.db", "") is True

    def test_temp_prefix_rejected(self):
        fn = self._fn()
        assert fn("/work/.callwarden-tmp-123/x.py", ".callwarden-tmp-123/x.py") is True

    def test_suffix_rejected(self):
        fn = self._fn()
        assert fn("/work/foo.py~", "foo.py~") is True
        assert fn("/work/foo.bak", "foo.bak") is True
        assert fn("/work/foo.orig", "foo.orig") is True
        assert fn("/work/foo.rej", "foo.rej") is True

    def test_rel_path_only(self):
        fn = self._fn()
        # rel_path 命中即可
        assert fn("/tmp/anything", ".git/HEAD") is True
        assert fn("", ".callwarden/x.db") is True


# ============================================================
# 单元：_evaluate_generation_protection
# ============================================================


class TestEvaluateGenerationProtection:
    """镜像 Rust snapshot_guard.rs::evaluate_generation_protection 状态分类。"""

    def _fn(self):
        from callwarden.server.replicator import _evaluate_generation_protection
        return _evaluate_generation_protection

    def test_success_states_not_blocked(self):
        fn = self._fn()
        for state in ("ready_published", "ready_cache_hit", ""):
            r = fn(state, "/work/a.py", "a.py")
            assert r["blocked"] is False, state
            assert r["parse_status"] == "ok"

    def test_parse_failure_blocked_allows_retry(self):
        fn = self._fn()
        for state in ("parse_failed", "canonicalize_failed", "publish_failed",
                      "cas_lookup_failed", "no_abs_path", "no_cas_conn"):
            r = fn(state, "/work/a.py", "a.py")
            assert r["blocked"] is True, state
            assert r["parse_status"] == "failed", state
            assert r["allows_retry"] is True, state

    def test_unsupported_blocked_no_retry(self):
        fn = self._fn()
        r = fn("unsupported_language", "/work/a.unknown", "a.unknown")
        assert r["blocked"] is True
        assert r["parse_status"] == "unsupported"
        assert r["allows_retry"] is False

    def test_stale_blocked_no_retry(self):
        fn = self._fn()
        for state in ("stale_seq_dropped", "stale_generation"):
            r = fn(state, "/work/a.py", "a.py")
            assert r["blocked"] is True, state
            assert r["parse_status"] == "stale", state
            assert r["allows_retry"] is False, state

    def test_partial_blocked_no_retry(self):
        fn = self._fn()
        r = fn("partial_published", "/work/a.py", "a.py")
        assert r["blocked"] is True
        assert r["parse_status"] == "partial"
        assert r["allows_retry"] is False

    def test_dirty_overlay_takes_priority_over_success(self):
        fn = self._fn()
        r = fn("ready_published", "/work/.git/config", ".git/config")
        assert r["blocked"] is True
        assert r["dirty_overlay"] is True
        assert r["parse_status"] == "stale"


# ============================================================
# 集成：daemon_handle_refresh blocked / committed
# ============================================================


class TestDaemonHandleRefreshGenerationProtection:
    """daemon_handle_refresh 薄客户端转发契约：C6 保护门控已下沉 daemon。

    stale 依据（A 桶：薄客户端 RPC seam）：生产已 daemon authority 化。
    `server/replicator.py:364-384 daemon_handle_refresh` 现为纯薄客户端——
    函数体 `del peer_uid, workspace_id, ws_conn, cas_conn, ...`（:379-380）后仅
    `return _call_daemon_rpc(_REPLICATOR_REFRESH_METHOD, params)`（:384，方法常量
    `mcp.replicator.daemon_handle_refresh` 见 :52）。C6 保护门控 / merge / committed
    段（`latest_committed_generation` 推进）均在 Rust daemon
    （`rust_ext/src/daemon/snapshot_guard.rs`）内执行，本地 `_daemon_parse_and_publish`
    已非调用路径（成为兼容死代码）。故本类改为 mock 模块级 `R._call_daemon_rpc`，
    断言「转发正确 method+params（含 canonical_bytes_hex）、回包逐字透传、本地
    ws_conn 不被写入（无本地提交回退）」这一现代契约。
    """

    def _setup(self, tmp_path):
        from callwarden.server.replicator import (
            daemon_handle_connect, init_session_schema,
        )
        from callwarden.server.cas_schema import init_cas_schema

        ws_db = os.path.join(str(tmp_path), "workspace.db")
        cas_db = os.path.join(str(tmp_path), "cas.db")

        ws_conn = sqlite3.connect(ws_db)
        ws_conn.row_factory = sqlite3.Row
        init_session_schema(ws_conn)

        cas_conn = sqlite3.connect(cas_db)
        cas_conn.row_factory = sqlite3.Row
        init_cas_schema(cas_conn)

        conn_result = daemon_handle_connect(
            peer_uid=1000, workspace_id=7,
            requested_session_id="sess-c6",
            ws_conn=ws_conn,
        )
        return ws_conn, cas_conn, conn_result["session_epoch"]

    def _make_msg(self, epoch, seq, rel_path, abs_path):
        return {
            "agent_session_id": "sess-c6",
            "session_epoch": epoch,
            "monotonic_seq": seq,
            "rel_path": rel_path,
            "abs_path": abs_path,
        }

    def _committed_generation(self, ws_conn):
        """本地 ws_conn 的 committed generation；无行（薄客户端从不本地提交）归一为 ""。"""
        row = ws_conn.execute(
            "SELECT latest_committed_generation FROM file_generations "
            "WHERE workspace_id=7 AND rel_path='a.py'"
        ).fetchone()
        return "" if row is None else row["latest_committed_generation"]

    def _stub_daemon(self, monkeypatch, response):
        """安装 RPC seam 替身：记录 (method, params) 并按 method 回放 daemon 回包。"""
        from callwarden.server import replicator as R

        calls = []

        def fake_rpc(method, params):
            calls.append((method, dict(params)))
            return response

        monkeypatch.setattr(R, "_call_daemon_rpc", fake_rpc)
        return R, calls

    def test_parse_failure_blocked_does_not_commit(self, tmp_path, monkeypatch):
        """验收点 5：daemon 判定 parse_failed → blocked，回包透传且无本地提交。"""
        ws_conn, cas_conn, epoch = self._setup(tmp_path)
        canonical = b"def a():\n    pass\n"
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
        R, calls = self._stub_daemon(monkeypatch, resp)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/a.py"),
            ws_conn=ws_conn, cas_conn=cas_conn,
            canonical_bytes=canonical,
        )
        assert result == resp
        assert calls[0][0] == R._REPLICATOR_REFRESH_METHOD
        assert calls[0][1]["canonical_bytes_hex"] == canonical.hex()
        assert calls[0][1]["rel_path"] == "a.py"
        assert calls[0][1]["agent_session_id"] == "sess-c6"
        assert result["status"] == "blocked"
        assert result["cas_state"] == "parse_failed"
        assert result["protection"]["blocked"] is True
        assert result["protection"]["parse_status"] == "failed"
        assert result["protection"]["allows_retry"] is True
        # 薄客户端不写本地 DB：committed 段只存在于 daemon
        assert self._committed_generation(ws_conn) == ""

        ws_conn.close()
        cas_conn.close()

    def test_publish_failure_blocked(self, tmp_path, monkeypatch):
        """验收点 5：daemon 判定 publish_failed → blocked，回包透传。"""
        ws_conn, cas_conn, epoch = self._setup(tmp_path)
        resp = {
            "status": "blocked",
            "cas_state": "publish_failed",
            "protection": {
                "blocked": True,
                "reason": "publish failure",
                "cas_state": "publish_failed",
                "parse_status": "failed",
                "dirty_overlay": False,
                "allows_retry": True,
            },
        }
        R, calls = self._stub_daemon(monkeypatch, resp)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/a.py"),
            ws_conn=ws_conn, cas_conn=cas_conn,
            canonical_bytes=b"def a():\n    pass\n",
        )
        assert result == resp
        assert calls[0][0] == R._REPLICATOR_REFRESH_METHOD
        assert result["status"] == "blocked"
        assert result["protection"]["parse_status"] == "failed"
        assert self._committed_generation(ws_conn) == ""

        ws_conn.close()
        cas_conn.close()

    def test_partial_blocked_does_not_replace_snapshot(self, tmp_path, monkeypatch):
        """验收点 7：daemon 判定 partial_published → blocked（不替换上一代 snapshot）。"""
        ws_conn, cas_conn, epoch = self._setup(tmp_path)
        resp = {
            "status": "blocked",
            "cas_state": "partial_published",
            "protection": {
                "blocked": True,
                "reason": "partial parse",
                "cas_state": "partial_published",
                "parse_status": "partial",
                "dirty_overlay": False,
                "allows_retry": False,
            },
        }
        R, calls = self._stub_daemon(monkeypatch, resp)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/a.py"),
            ws_conn=ws_conn, cas_conn=cas_conn,
            canonical_bytes=b"def a():\n    pass\n",
        )
        assert result == resp
        assert calls[0][0] == R._REPLICATOR_REFRESH_METHOD
        assert result["status"] == "blocked"
        assert result["protection"]["parse_status"] == "partial"
        assert self._committed_generation(ws_conn) == ""

        ws_conn.close()
        cas_conn.close()

    def test_unsupported_blocked(self, tmp_path, monkeypatch):
        """daemon 判定 unsupported_language → blocked，回包透传。"""
        ws_conn, cas_conn, epoch = self._setup(tmp_path)
        resp = {
            "status": "blocked",
            "cas_state": "unsupported_language",
            "protection": {
                "blocked": True,
                "reason": "unsupported language",
                "cas_state": "unsupported_language",
                "parse_status": "unsupported",
                "dirty_overlay": False,
                "allows_retry": False,
            },
        }
        R, calls = self._stub_daemon(monkeypatch, resp)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.unknown", "/work/a.unknown"),
            ws_conn=ws_conn, cas_conn=cas_conn,
        )
        assert result == resp
        assert calls[0][0] == R._REPLICATOR_REFRESH_METHOD
        assert result["status"] == "blocked"
        assert result["protection"]["parse_status"] == "unsupported"

        ws_conn.close()
        cas_conn.close()

    def test_dirty_overlay_blocked_even_if_ready(self, tmp_path, monkeypatch):
        """dirty overlay 路径 → daemon 判定 blocked，即使 cas_state=ready_published。"""
        ws_conn, cas_conn, epoch = self._setup(tmp_path)
        resp = {
            "status": "blocked",
            "cas_state": "ready_published",
            "protection": {
                "blocked": True,
                "reason": "dirty overlay rejected",
                "cas_state": "ready_published",
                "parse_status": "stale",
                "dirty_overlay": True,
                "allows_retry": False,
            },
        }
        R, calls = self._stub_daemon(monkeypatch, resp)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/.git/a.py"),
            ws_conn=ws_conn, cas_conn=cas_conn,
            canonical_bytes=b"def a():\n    pass\n",
        )
        assert result == resp
        assert calls[0][0] == R._REPLICATOR_REFRESH_METHOD
        assert result["status"] == "blocked"
        assert result["protection"]["dirty_overlay"] is True
        assert self._committed_generation(ws_conn) == ""

        ws_conn.close()
        cas_conn.close()

    def test_ready_state_commits(self, tmp_path, monkeypatch):
        """回归：daemon 判定 ready_published → committed，回包透传。

        注：`latest_committed_generation` 的推进发生在 daemon 侧（权威库），
        Python 薄客户端不再写本地 ws_conn，故此处只断言回包透传 + 本地无副作用。
        """
        ws_conn, cas_conn, epoch = self._setup(tmp_path)
        resp = {
            "status": "committed",
            "cas_state": "ready_published",
            "protection": {
                "blocked": False,
                "reason": "",
                "cas_state": "ready_published",
                "parse_status": "ok",
                "dirty_overlay": False,
                "allows_retry": False,
            },
        }
        R, calls = self._stub_daemon(monkeypatch, resp)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/a.py"),
            ws_conn=ws_conn, cas_conn=cas_conn,
            canonical_bytes=b"def a():\n    pass\n",
        )
        assert result == resp
        assert calls[0][0] == R._REPLICATOR_REFRESH_METHOD
        assert result["status"] == "committed"
        assert self._committed_generation(ws_conn) == ""

        ws_conn.close()
        cas_conn.close()

    def test_no_cas_conn_skips_protection(self, tmp_path, monkeypatch):
        """回归：cas_conn=None（无 CAS 主链）→ daemon 判定 committed，回包透传。"""
        ws_conn, cas_conn, epoch = self._setup(tmp_path)
        cas_conn.close()
        resp = {
            "status": "committed",
            "cas_state": "",
            "protection": {
                "blocked": False,
                "reason": "",
                "cas_state": "",
                "parse_status": "ok",
                "dirty_overlay": False,
                "allows_retry": False,
            },
        }
        R, calls = self._stub_daemon(monkeypatch, resp)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/a.py"),
            ws_conn=ws_conn, cas_conn=None,
            canonical_bytes=b"def a():\n    pass\n",
        )
        assert result == resp
        assert calls[0][0] == R._REPLICATOR_REFRESH_METHOD
        assert result["status"] == "committed"

        ws_conn.close()

    def test_daemon_unavailable_fail_closed_no_local_fallback(self, tmp_path, monkeypatch):
        """fail-closed：daemon RPC 失败时异常透传，绝不回退本地解析/提交。"""
        from callwarden.server import replicator as R
        from callwarden.server.daemon_protocol import DaemonRemoteError

        ws_conn, cas_conn, epoch = self._setup(tmp_path)

        def boom(method, params):
            raise DaemonRemoteError("E_HTTP_MANIFEST_MISSING", "fail-closed")

        monkeypatch.setattr(R, "_call_daemon_rpc", boom)

        with pytest.raises(DaemonRemoteError):
            R.daemon_handle_refresh(
                peer_uid=1000, workspace_id=7,
                msg=self._make_msg(epoch, 1, "a.py", "/work/a.py"),
                ws_conn=ws_conn, cas_conn=cas_conn,
                canonical_bytes=b"def a():\n    pass\n",
            )
        # 未回退本地：本地无任何 committed 提交
        assert self._committed_generation(ws_conn) == ""

        ws_conn.close()
        cas_conn.close()


# ============================================================
# handler 层：workspace.file.refresh blocked 分支
# ============================================================


class TestDaemonServerBlockedBranch:
    """daemon_server workspace.file.refresh 对 blocked 结果的处理。"""

    def _make_service(self, tmp_path, monkeypatch, refresh_result):
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
        ws_id = "54321"
        monkeypatch.setattr(
            service, "_owned_workspace",
            lambda peer_uid, workspace_id: {
                "workspace_instance_id": workspace_id,
                "workspace_id": 54321,
                "owner_uid": peer_uid,
                "host_real_root": "/test/root",
                "status": "active",
            },
        )

        # mock replicator.daemon_handle_refresh（handler 内 from-import 生效）
        from callwarden.server import replicator as R
        monkeypatch.setattr(R, "daemon_handle_refresh", lambda **kw: refresh_result)

        return service, uid, ws_id

    def test_blocked_returns_snapshot_published_false(self, tmp_path, monkeypatch):
        """blocked → snapshot_published=False + snapshot_warning，不 replicate。"""
        service, uid, ws_id = self._make_service(
            tmp_path, monkeypatch,
            refresh_result={
                "status": "blocked",
                "generation": "1:1",
                "cas_state": "parse_failed",
                "protection": {
                    "blocked": True,
                    "reason": "parse failure (设计 §5.3 failed): cas_state=parse_failed",
                    "parse_status": "failed",
                    "allows_retry": True,
                    "dirty_overlay": False,
                },
            },
        )
        res = service._get_workspace_resources(ws_id)

        captured = []
        orig_append = res["staging_log"].append
        res["staging_log"].append = lambda entry: captured.append(entry)

        class _NoReplicateReplicator:
            def replicate(self, *a, **kw):
                raise AssertionError("blocked 状态不得调用 replicate")

        res["replicator"] = _NoReplicateReplicator()

        import binascii
        canonical_bytes = b"def a():\n    pass\n"
        params = {
            "workspace_instance_id": ws_id,
            "agent_session_id": "sess-blocked",
            "session_epoch": 1,
            "monotonic_seq": 1,
            "rel_path": "a.py",
            "canonical_bytes_hex": binascii.hexlify(canonical_bytes).decode(),
            "content_hash": hashlib.sha256(canonical_bytes).hexdigest(),
            "language": "python",
        }
        peer = {"uid": uid}
        result = service.dispatch(peer, "workspace.file.refresh", params)

        assert result["status"] == "blocked"
        repl_map = result["replication"]
        assert repl_map["snapshot_published"] is False
        assert "snapshot_warning" in repl_map
        assert "generation 保护拦截" in repl_map["snapshot_warning"]
        assert repl_map["protection"]["blocked"] is True
        # staging 未追加、replicate 未调用
        assert captured == []

    def test_blocked_dirty_overlay_no_staging(self, tmp_path, monkeypatch):
        """dirty overlay blocked → snapshot_published=False，无 staging entry。"""
        service, uid, ws_id = self._make_service(
            tmp_path, monkeypatch,
            refresh_result={
                "status": "blocked",
                "generation": "1:1",
                "cas_state": "ready_published",
                "protection": {
                    "blocked": True,
                    "reason": "dirty overlay rejected (设计 §9.3): rel_path=.git/config",
                    "parse_status": "stale",
                    "allows_retry": False,
                    "dirty_overlay": True,
                },
            },
        )
        res = service._get_workspace_resources(ws_id)

        captured = []
        res["staging_log"].append = lambda entry: captured.append(entry)

        class _NoReplicateReplicator:
            def replicate(self, *a, **kw):
                raise AssertionError("blocked 状态不得调用 replicate")

        res["replicator"] = _NoReplicateReplicator()

        import binascii
        canonical_bytes = b"# config\n"
        params = {
            "workspace_instance_id": ws_id,
            "agent_session_id": "sess-blocked",
            "session_epoch": 1,
            "monotonic_seq": 1,
            "rel_path": ".git/config",
            "canonical_bytes_hex": binascii.hexlify(canonical_bytes).decode(),
            "content_hash": hashlib.sha256(canonical_bytes).hexdigest(),
            "language": "",
        }
        peer = {"uid": uid}
        result = service.dispatch(peer, "workspace.file.refresh", params)

        assert result["status"] == "blocked"
        assert result["replication"]["snapshot_published"] is False
        assert result["replication"]["protection"]["dirty_overlay"] is True
        assert captured == []
