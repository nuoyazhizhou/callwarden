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
    """daemon_handle_refresh 真实调用：C6 保护门控在 merge 之前、committed 段之前。"""

    def _setup(self, tmp_path):
        from callwarden.server.replicator import (
            daemon_handle_connect, init_session_schema,
        )
        from callwarden.db.db_cas import init_cas_schema

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
        row = ws_conn.execute(
            "SELECT latest_committed_generation FROM file_generations "
            "WHERE workspace_id=7 AND rel_path='a.py'"
        ).fetchone()
        return None if row is None else row["latest_committed_generation"]

    def test_parse_failure_blocked_does_not_commit(self, tmp_path, monkeypatch):
        """验收点 5：parse_failed → blocked，latest_committed_generation 不推进。"""
        from callwarden.server import replicator as R

        ws_conn, cas_conn, epoch = self._setup(tmp_path)

        def fake_publish(**kwargs):
            return {
                "cas_state": "parse_failed",
                "cas_key": "k-fail",
                "content_hash": "h1",
                "language": "python",
            }

        monkeypatch.setattr(R, "_daemon_parse_and_publish", fake_publish)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/a.py"),
            ws_conn=ws_conn, cas_conn=cas_conn,
            canonical_bytes=b"def a():\n    pass\n",
        )
        assert result["status"] == "blocked"
        assert result["cas_state"] == "parse_failed"
        protection = result["protection"]
        assert protection["blocked"] is True
        assert protection["parse_status"] == "failed"
        assert protection["allows_retry"] is True
        # committed 段未执行 → latest_committed_generation 为空
        assert self._committed_generation(ws_conn) == ""

        ws_conn.close()
        cas_conn.close()

    def test_publish_failure_blocked(self, tmp_path, monkeypatch):
        """验收点 5：publish_failed → blocked。"""
        from callwarden.server import replicator as R

        ws_conn, cas_conn, epoch = self._setup(tmp_path)

        def fake_publish(**kwargs):
            return {
                "cas_state": "publish_failed",
                "cas_key": "k-fail",
                "content_hash": "h1",
            }

        monkeypatch.setattr(R, "_daemon_parse_and_publish", fake_publish)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/a.py"),
            ws_conn=ws_conn, cas_conn=cas_conn,
            canonical_bytes=b"def a():\n    pass\n",
        )
        assert result["status"] == "blocked"
        assert result["protection"]["parse_status"] == "failed"
        assert self._committed_generation(ws_conn) == ""

        ws_conn.close()
        cas_conn.close()

    def test_partial_blocked_does_not_replace_snapshot(self, tmp_path, monkeypatch):
        """验收点 7：partial_published → blocked（不替换上一代 snapshot）。"""
        from callwarden.server import replicator as R

        ws_conn, cas_conn, epoch = self._setup(tmp_path)

        def fake_publish(**kwargs):
            return {"cas_state": "partial_published", "cas_key": "k-p", "content_hash": "h2"}

        monkeypatch.setattr(R, "_daemon_parse_and_publish", fake_publish)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/a.py"),
            ws_conn=ws_conn, cas_conn=cas_conn,
            canonical_bytes=b"def a():\n    pass\n",
        )
        assert result["status"] == "blocked"
        assert result["protection"]["parse_status"] == "partial"
        assert self._committed_generation(ws_conn) == ""

        ws_conn.close()
        cas_conn.close()

    def test_unsupported_blocked(self, tmp_path, monkeypatch):
        """unsupported_language（真实 CAS 主链）→ blocked。"""
        from callwarden.server import replicator as R

        ws_conn, cas_conn, epoch = self._setup(tmp_path)

        def fake_publish(**kwargs):
            return {"cas_state": "unsupported_language", "cas_key": "k-u", "content_hash": "h3"}

        monkeypatch.setattr(R, "_daemon_parse_and_publish", fake_publish)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.unknown", "/work/a.unknown"),
            ws_conn=ws_conn, cas_conn=cas_conn,
        )
        assert result["status"] == "blocked"
        assert result["protection"]["parse_status"] == "unsupported"

        ws_conn.close()
        cas_conn.close()

    def test_dirty_overlay_blocked_even_if_ready(self, tmp_path, monkeypatch):
        """dirty overlay 路径 → blocked，即使 cas_state=ready_published。"""
        from callwarden.server import replicator as R

        ws_conn, cas_conn, epoch = self._setup(tmp_path)

        def fake_publish(**kwargs):
            return {"cas_state": "ready_published", "cas_key": "k-ok", "content_hash": "h4"}

        monkeypatch.setattr(R, "_daemon_parse_and_publish", fake_publish)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/.git/a.py"),
            ws_conn=ws_conn, cas_conn=cas_conn,
            canonical_bytes=b"def a():\n    pass\n",
        )
        assert result["status"] == "blocked"
        assert result["protection"]["dirty_overlay"] is True
        assert self._committed_generation(ws_conn) == ""

        ws_conn.close()
        cas_conn.close()

    def test_ready_state_commits(self, tmp_path, monkeypatch):
        """回归：ready_published → committed，latest_committed_generation 推进。"""
        from callwarden.server import replicator as R

        ws_conn, cas_conn, epoch = self._setup(tmp_path)

        def fake_publish(**kwargs):
            return {"cas_state": "ready_published", "cas_key": "k-ok", "content_hash": "h5"}

        monkeypatch.setattr(R, "_daemon_parse_and_publish", fake_publish)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/a.py"),
            ws_conn=ws_conn, cas_conn=cas_conn,
            canonical_bytes=b"def a():\n    pass\n",
        )
        assert result["status"] == "committed"
        assert self._committed_generation(ws_conn) == f"{epoch}:1"

        ws_conn.close()
        cas_conn.close()

    def test_no_cas_conn_skips_protection(self, tmp_path, monkeypatch):
        """回归：cas_conn=None（无 CAS 主链）→ 不启用保护，committed。"""
        from callwarden.server import replicator as R

        ws_conn, cas_conn, epoch = self._setup(tmp_path)
        cas_conn.close()

        def fake_publish(**kwargs):
            return {"cas_state": "no_cas_conn", "cas_key": "", "content_hash": "h6"}

        monkeypatch.setattr(R, "_daemon_parse_and_publish", fake_publish)

        result = R.daemon_handle_refresh(
            peer_uid=1000, workspace_id=7,
            msg=self._make_msg(epoch, 1, "a.py", "/work/a.py"),
            ws_conn=ws_conn, cas_conn=None,
            canonical_bytes=b"def a():\n    pass\n",
        )
        # no_cas_conn 在 Python 侧不启用保护（镜像 Rust cas_store=None → 不保护）
        assert result["status"] == "committed"

        ws_conn.close()


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
