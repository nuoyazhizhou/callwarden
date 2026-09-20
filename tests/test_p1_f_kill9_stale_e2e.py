"""P1-F Step 5: kill -9 恢复 E2E + stale session/generation 拒绝

设计文档：docs/design/rust-only-parser-cutover-plan.md §8 Phase 4 完成门

覆盖三项完成门：
    - **kill -9 恢复**：通过 Rust 侧 ParseRetryLog 持久化文件实现（Rust 集成测试
      ``rust_ext/tests/p1_f_recovery_e2e.rs`` 已覆盖完整链路），本文件验证
      Python 侧可观测的 session epoch CAS 行为
    - **stale 不覆盖**：通过 ``daemon_handle_refresh`` 验证 stale session/seq 被拒绝
    - **失败文件可定位**：通过 ``RustParserFacade.extract_diagnostics`` 验证
      Python 侧能从 parse result 推导出 failed/partial 状态

注意：
    - Rust 侧的 ``ParseRetryLog`` / ``SnapshotGuard`` / ``ParserMetrics`` 已在
      ``rust_ext/tests/p1_f_recovery_e2e.rs`` 中通过 15 个集成测试覆盖完整链路。
    - 本文件聚焦 Python 可观测行为：session epoch CAS、stale 拒绝、diagnostics 推导。
    - frozen strict mode 的强制在 ``test_p1_f_frozen_strict_mode.py`` 中覆盖。
"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"))


# ============================================
# 模块级 skip：依赖 server.replicator（含 Rust 扩展）
# ============================================

try:
    # 统一走 canonical 包路径（callwarden.server）：避免与顶层 `server.*` 形成双模块
    # 实例——server/_mcp_common.py:12 `from ..db import CodeGraphDB` 在顶层 server.*
    # 包下会「越界相对导入」失败。
    from callwarden.server import replicator as _R

    SESSION_SCHEMA_DDL = _R.SESSION_SCHEMA_DDL
    ProtocolError = _R.ProtocolError
    daemon_handle_connect = _R.daemon_handle_connect
    daemon_handle_refresh = _R.daemon_handle_refresh
    init_session_schema = _R.init_session_schema
    _REPLICATOR_AVAILABLE = True
except ImportError:
    _REPLICATOR_AVAILABLE = False

try:
    from callwarden.config import ParseMode
    from callwarden.db.rust_parser_facade import RustParserFacade
    _FACADE_AVAILABLE = True
except ImportError:
    _FACADE_AVAILABLE = False


# ============================================
# 辅助函数
# ============================================


def _open_db() -> sqlite3.Connection:
    """打开内存 SQLite 并初始化 session schema"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_session_schema(conn)
    return conn


def _refresh_msg(session_id: str, epoch: int, seq: int,
                 rel_path: str = "src/main.py") -> dict:
    """构造 refresh 消息"""
    return {
        "rel_path": rel_path,
        "agent_session_id": session_id,
        "monotonic_seq": seq,
        "session_epoch": epoch,
    }


# ============================================
# Stale session 拒绝（kill -9 后旧 session 失效）
# ============================================

@pytest.mark.skipif(not _REPLICATOR_AVAILABLE,
                    reason="server.replicator 不可用（需 Rust 扩展）")
class TestStaleSessionRejected:
    """stale session/generation 拒绝契约（A 桶：RPC seam 迁移）

    stale 依据：`server/replicator.py:364-384 daemon_handle_refresh` 已 daemon
    authority 化——kill -9 恢复后的 session epoch CAS（stale session 拒绝 /
    stale seq 丢弃 / 两阶段 commit）现由 Rust daemon 持有
    （`rust_ext/src/daemon/snapshot_guard.rs` 等），Python 侧只做参数序列化 +
    回包透传 + 异常上抛。本地 epoch-CAS 实现已删除，故本类断言
    「转发 / 透传 / fail-closed」契约。
    """

    def _connect(self, session_id: str = "s1"):
        conn = _open_db()
        resp = daemon_handle_connect(
            peer_uid=1000, workspace_id=1,
            requested_session_id=session_id, ws_conn=conn,
        )
        return conn, resp["session_epoch"]

    def _stub(self, monkeypatch, response):
        calls = []

        def fake_rpc(method, params):
            calls.append((method, dict(params)))
            return response

        monkeypatch.setattr(_R, "_call_daemon_rpc", fake_rpc)
        return calls

    def test_old_session_rejected_after_new_connect(self, monkeypatch):
        """kill -9 恢复：S1(epoch1) 被 S2(epoch2) 接管 → daemon 拒绝 S1 refresh"""
        conn, old_epoch = self._connect("s1")
        assert old_epoch == 1

        resp2 = daemon_handle_connect(
            peer_uid=1000, workspace_id=1,
            requested_session_id="s2", ws_conn=conn,
        )
        new_epoch = resp2["session_epoch"]
        assert new_epoch == 2

        forward_calls = []

        def fake_rpc(method, params):
            forward_calls.append((method, dict(params)))
            # daemon 侧：epoch=1 已被 epoch=2 接管 → stale session 拒绝
            raise ProtocolError("stale session", code="stale_session")

        monkeypatch.setattr(_R, "_call_daemon_rpc", fake_rpc)
        with pytest.raises(ProtocolError, match="stale session"):
            daemon_handle_refresh(
                peer_uid=1000, workspace_id=1,
                msg=_refresh_msg("s1", epoch=old_epoch, seq=1),
                ws_conn=conn,
            )
        assert forward_calls[0][0] == _R._REPLICATOR_REFRESH_METHOD
        assert forward_calls[0][1]["session_epoch"] == old_epoch
        assert forward_calls[0][1]["agent_session_id"] == "s1"

    def test_stale_seq_dropped(self, monkeypatch):
        """daemon 判定 stale_seq_dropped → 原样回包（本地不再判定）"""
        conn, _ = self._connect("s1")
        calls = self._stub(monkeypatch, {"status": "stale_seq_dropped"})
        resp = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=1, seq=3),
            ws_conn=conn,
        )
        assert resp["status"] == "stale_seq_dropped"
        assert calls[0][1]["monotonic_seq"] == 3

    def test_correct_session_epoch_accepted(self, monkeypatch):
        """正确 epoch 的 refresh 转发后回包 committed，无本地 ProtocolError"""
        conn, epoch = self._connect("s1")
        self._stub(monkeypatch, {"status": "committed",
                                 "generation": f"{epoch}:1"})
        result = daemon_handle_refresh(
            peer_uid=1000, workspace_id=1,
            msg=_refresh_msg("s1", epoch=epoch, seq=1),
            ws_conn=conn,
        )
        assert result["status"] == "committed"
        assert result["generation"] == f"{epoch}:1"


# ============================================
# 失败状态推导（失败文件可定位）
# ============================================

@pytest.mark.skipif(not _FACADE_AVAILABLE,
                    reason="callwarden.db.rust_parser_facade 不可用")
class TestFailureStatesDiagnostics:
    """parse 失败状态推导测试

    场景：Rust parser 返回带 error 的 result，Python 侧通过
    ``RustParserFacade.extract_diagnostics`` 推导出 failed 状态，
    确保失败文件可被定位。
    """

    def test_extract_diagnostics_failed_from_error(self):
        """顶层 error 字段 → status=failed"""
        result = {"error": "parse_failed: syntax error at line 5"}
        diag = RustParserFacade.extract_diagnostics(result)
        assert diag["status"] == "failed"
        assert diag["fatal_parse_error"] == "parse_failed: syntax error at line 5"
        assert diag["partial_parse"] is False

    def test_extract_diagnostics_ok_from_clean_result(self):
        """无 error 且无 syntax/unsupported → status=ok"""
        result = {
            "symbols": [],
            "calls": [],
            "imports": [],
            "parse_errors": [],
            "unsupported_constructs": [],
        }
        diag = RustParserFacade.extract_diagnostics(result)
        assert diag["status"] == "ok"
        assert diag["syntax_error_count"] == 0
        assert diag["unsupported_construct_count"] == 0

    def test_extract_diagnostics_partial_from_syntax_errors(self):
        """有 syntax error → status=partial"""
        result = {
            "parse_errors": ["line 5: unexpected token", "line 10: missing semicolon"],
            "unsupported_constructs": [],
        }
        diag = RustParserFacade.extract_diagnostics(result)
        assert diag["status"] == "partial"
        assert diag["syntax_error_count"] == 2
        assert diag["partial_parse"] is True

    def test_extract_diagnostics_partial_from_unsupported(self):
        """有 unsupported construct → status=partial"""
        result = {
            "parse_errors": [],
            "unsupported_constructs": ["macro_rules! not supported"],
        }
        diag = RustParserFacade.extract_diagnostics(result)
        assert diag["status"] == "partial"
        assert diag["unsupported_construct_count"] == 1

    def test_extract_diagnostics_empty_result(self):
        """空 result → status=failed"""
        diag = RustParserFacade.extract_diagnostics({})
        assert diag["status"] == "failed"
        assert "empty result" in diag["fatal_parse_error"]

    def test_extract_diagnostics_failed_does_not_overwrite(self):
        """failed 状态不应替换上一代 snapshot（设计 §5.3）

        验证：failed 状态的 parse result 通过 extract_diagnostics 推导后，
        status=failed，调用方应据此跳过 snapshot 替换。
        """
        result = {"error": "canonicalize_failed: file not found"}
        diag = RustParserFacade.extract_diagnostics(result)
        assert diag["status"] == "failed"
        # 调用方应根据 status=failed 跳过 snapshot 替换（设计 §5.3）
        # 不替换上一代可查询 snapshot


# ============================================
# ParseMode 环境校验（rust-strict 默认）
# ============================================

@pytest.mark.skipif(not _FACADE_AVAILABLE,
                    reason="callwarden.db.rust_parser_facade 不可用")
class TestParseModeDefault:
    """ParseMode 默认值与环境校验测试

    场景：生产环境默认 rust-strict，frozen build 强制 rust-strict。
    详细的 frozen build 强制逻辑在 test_p1_f_frozen_strict_mode.py 中覆盖。
    """

    def test_default_mode_is_rust_strict(self):
        """未设置 CW_PARSE_MODE 时默认 rust-strict"""
        old = os.environ.pop("CW_PARSE_MODE", None)
        try:
            mode = ParseMode.get_active_mode()
            assert mode == ParseMode.RUST_STRICT
        finally:
            if old is not None:
                os.environ["CW_PARSE_MODE"] = old

    def test_unknown_mode_raises(self):
        """未知 CW_PARSE_MODE 抛 ValueError"""
        old = os.environ.get("CW_PARSE_MODE")
        os.environ["CW_PARSE_MODE"] = "unknown-mode"
        try:
            with pytest.raises(ValueError, match="未知 CW_PARSE_MODE"):
                ParseMode.get_active_mode()
        finally:
            if old is None:
                os.environ.pop("CW_PARSE_MODE", None)
            else:
                os.environ["CW_PARSE_MODE"] = old

    def test_is_rust_disabled_in_strict_mode(self):
        """rust-strict 模式下 is_rust_disabled 始终返回 False"""
        old = os.environ.get("CW_PARSE_MODE")
        old_disable = os.environ.get("CW_DISABLE_RUST_PARSE")
        os.environ["CW_PARSE_MODE"] = ParseMode.RUST_STRICT
        os.environ["CW_DISABLE_RUST_PARSE"] = "1"
        try:
            assert not RustParserFacade.is_rust_disabled(), \
                "rust-strict 模式下不允许禁用 Rust parser"
        finally:
            if old is None:
                os.environ.pop("CW_PARSE_MODE", None)
            else:
                os.environ["CW_PARSE_MODE"] = old
            if old_disable is None:
                os.environ.pop("CW_DISABLE_RUST_PARSE", None)
            else:
                os.environ["CW_DISABLE_RUST_PARSE"] = old_disable

    def test_shadow_mode_allows_python_reference(self):
        """shadow 模式允许调用 Python reference parser"""
        old = os.environ.get("CW_PARSE_MODE")
        os.environ["CW_PARSE_MODE"] = ParseMode.SHADOW
        try:
            assert ParseMode.allows_python_reference()
        finally:
            if old is None:
                os.environ.pop("CW_PARSE_MODE", None)
            else:
                os.environ["CW_PARSE_MODE"] = old

    def test_python_reference_mode_allows_python_reference(self):
        """python-reference 模式允许调用 Python reference parser"""
        old = os.environ.get("CW_PARSE_MODE")
        os.environ["CW_PARSE_MODE"] = ParseMode.PYTHON_REFERENCE
        try:
            assert ParseMode.allows_python_reference()
        finally:
            if old is None:
                os.environ.pop("CW_PARSE_MODE", None)
            else:
                os.environ["CW_PARSE_MODE"] = old

    def test_rust_strict_mode_disallows_python_reference(self):
        """rust-strict 模式禁止调用 Python reference parser"""
        old = os.environ.get("CW_PARSE_MODE")
        os.environ["CW_PARSE_MODE"] = ParseMode.RUST_STRICT
        try:
            assert not ParseMode.allows_python_reference()
        finally:
            if old is None:
                os.environ.pop("CW_PARSE_MODE", None)
            else:
                os.environ["CW_PARSE_MODE"] = old

    def test_shadow_diagnostics_path_only_in_shadow_mode(self):
        """shadow diagnostics 路径只在 shadow 模式返回"""
        old = os.environ.get("CW_PARSE_MODE")
        os.environ["CW_PARSE_MODE"] = ParseMode.RUST_STRICT
        try:
            assert ParseMode.shadow_diagnostics_path() is None
        finally:
            if old is None:
                os.environ.pop("CW_PARSE_MODE", None)
            else:
                os.environ["CW_PARSE_MODE"] = old

        os.environ["CW_PARSE_MODE"] = ParseMode.SHADOW
        try:
            path = ParseMode.shadow_diagnostics_path()
            assert path is not None
            assert "shadow_diagnostics" in path
        finally:
            if old is None:
                os.environ.pop("CW_PARSE_MODE", None)
            else:
                os.environ["CW_PARSE_MODE"] = old


# ============================================
# generation_metadata 推导（失败文件可定位）
# ============================================

@pytest.mark.skipif(not _FACADE_AVAILABLE,
                    reason="callwarden.db.rust_parser_facade 不可用")
class TestGenerationMetadata:
    """generation metadata 推导测试

    场景：parse result 携带 content_hash / parser_abi，用于 CAS key 隔离
    和跨版本兼容判断。失败文件的 generation metadata 仍应可推导，
    便于 durable log 记录失败上下文。
    """

    def test_generation_metadata_from_parse_result(self):
        """从 parse result 推导 generation metadata"""
        result = {
            "content_hash": "abc123",
            "total_lines": 100,
            "canonical_total": 1024,
            "raw_total": 1100,
            "metadata": {"encoding": "utf-8"},
        }
        meta = RustParserFacade.generation_metadata(result)
        assert meta["content_hash"] == "abc123"
        assert meta["total_lines"] == 100
        assert meta["canonical_total"] == 1024
        assert meta["raw_total"] == 1100
        assert meta["metadata"] == {"encoding": "utf-8"}
        # parser_abi 来自 core_version，可能为 "unknown"（Rust 扩展不可用时）
        assert "parser_abi" in meta

    def test_generation_metadata_from_empty_result(self):
        """空 result 也能推导 metadata（失败文件可定位）"""
        meta = RustParserFacade.generation_metadata({})
        assert meta["content_hash"] == ""
        assert meta["total_lines"] == 0
        assert "parser_abi" in meta
