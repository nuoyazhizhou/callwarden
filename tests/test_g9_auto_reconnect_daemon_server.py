"""G9 auto-reconnect：daemon_server.py code 透传单元测试。

验证 daemon_server.py 的 workspace.file.refresh dispatch 在捕获
replicator.ProtocolError 时，将 ProtocolError.code 作为 DaemonRpcError.code
透传给 client（agent 端据此决定是否触发 auto-reconnect）。

测试通过 patch replicator.daemon_handle_refresh 让它抛特定 code 的
ProtocolError，验证 dispatch 返回的 DaemonRpcError.code 与原 code 一致。

规范：docs/design/daemon-deploy-runbook.md §9.7.3
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(
    0,
    str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"),
)


def _make_daemon_service(tmp_path):
    """构造一个 EnterpriseDaemonService 实例（不启动 UDS server）。"""
    from callwarden.server.daemon_server import EnterpriseDaemonService
    from callwarden.server.snapshot_manager import SnapshotManagerService
    snapshot_service = SnapshotManagerService(max_workspaces=8)
    return EnterpriseDaemonService(
        registry_db=str(tmp_path / "registry.db"),
        snapshot_service=snapshot_service,
    )


def _peer():
    """构造 peer credential 字典。"""
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return {"pid": os.getpid(), "uid": uid, "gid": uid}


def _dispatch_refresh(daemon_service, peer, ws_id, params_extra=None):
    """调用 workspace.file.refresh dispatch，绕过 _owned_workspace 校验。

    daemon_server.py 第 502 行 `workspace_id=int(workspace_id)` 要求 workspace_id
    是数字字符串，但 register_workspace 返回 sha256 hex。测试中直接 patch
    _owned_workspace 和 _get_workspace_resources，使用数字 workspace_id。
    """
    params = {
        "workspace_instance_id": ws_id,
        "rel_path": "main.py",
        "agent_session_id": "s1",
        "session_epoch": 1,
        "monotonic_seq": 1,
        "canonical_bytes_hex": b"x".hex(),
    }
    if params_extra:
        params.update(params_extra)

    # patch _owned_workspace 返回 mock workspace dict
    mock_workspace = MagicMock()
    with patch.object(
        daemon_service, "_owned_workspace", return_value=mock_workspace,
    ):
        return daemon_service.dispatch(peer, "workspace.file.refresh", params)


# ============================================
# daemon_server.py workspace.file.refresh code 透传
# ============================================


class TestDaemonServerRefreshCodePropagation:
    """daemon_server.py workspace.file.refresh 错误码透传测试。

    场景：daemon_handle_refresh 抛 replicator.ProtocolError，
    dispatch 应识别该异常并将 code 透传为 DaemonRpcError.code。
    """

    @pytest.fixture
    def daemon_service(self, tmp_path):
        return _make_daemon_service(tmp_path)

    @pytest.fixture
    def mock_resources(self, daemon_service):
        """patch _get_workspace_resources 返回 mock 资源，
        避免 daemon_server.py 第 502 行 int(workspace_id) 失败。"""
        mock_ws_conn = MagicMock()
        mock_cas_conn = MagicMock()
        mock_staging_log = MagicMock()
        mock_replicator = MagicMock()
        mock_resources = {
            "ws_conn": mock_ws_conn,
            "cas_conn": mock_cas_conn,
            "staging_log": mock_staging_log,
            "replicator": mock_replicator,
        }
        with patch.object(
            daemon_service, "_get_workspace_resources", return_value=mock_resources,
        ):
            yield mock_resources

    def test_session_not_active_propagates_as_code(
        self, daemon_service, mock_resources,
    ):
        """daemon_handle_refresh 抛 ProtocolError(session_not_active) →
        dispatch 抛 DaemonRpcError(code=session_not_active)。"""
        from callwarden.server.daemon_server import DaemonRpcError
        from callwarden.server.replicator import ProtocolError

        peer = _peer()

        def _raise_session_not_active(**kwargs):
            raise ProtocolError(
                "no active session for workspace 1",
                code="session_not_active",
            )

        # daemon_server.py 中通过 from callwarden.server.replicator import
        # daemon_handle_refresh 局部导入，需 patch 源模块
        with patch(
            "callwarden.server.replicator.daemon_handle_refresh",
            side_effect=_raise_session_not_active,
        ):
            with pytest.raises(DaemonRpcError) as exc_info:
                _dispatch_refresh(daemon_service, peer, ws_id="1")
        # 关键断言：code 透传为 session_not_active（而非默认 refresh_failed）
        assert exc_info.value.code == "session_not_active"
        assert "no active session" in exc_info.value.message

    def test_stale_session_propagates_as_code(
        self, daemon_service, mock_resources,
    ):
        """daemon_handle_refresh 抛 ProtocolError(stale_session) →
        dispatch 抛 DaemonRpcError(code=stale_session)。"""
        from callwarden.server.daemon_server import DaemonRpcError
        from callwarden.server.replicator import ProtocolError

        peer = _peer()

        def _raise_stale_session(**kwargs):
            raise ProtocolError(
                "stale session rejected: incoming=old:1 active=new:2",
                code="stale_session",
            )

        with patch(
            "callwarden.server.replicator.daemon_handle_refresh",
            side_effect=_raise_stale_session,
        ):
            with pytest.raises(DaemonRpcError) as exc_info:
                _dispatch_refresh(daemon_service, peer, ws_id="1")
        assert exc_info.value.code == "stale_session"

    def test_stale_manifest_commit_propagates_as_code(
        self, daemon_service, mock_resources,
    ):
        """stale_manifest_commit code 也应透传（虽不触发 auto-reconnect）。"""
        from callwarden.server.daemon_server import DaemonRpcError
        from callwarden.server.replicator import ProtocolError

        peer = _peer()

        def _raise_stale_manifest(**kwargs):
            raise ProtocolError(
                "stale manifest commit for main.py",
                code="stale_manifest_commit",
            )

        with patch(
            "callwarden.server.replicator.daemon_handle_refresh",
            side_effect=_raise_stale_manifest,
        ):
            with pytest.raises(DaemonRpcError) as exc_info:
                _dispatch_refresh(daemon_service, peer, ws_id="1")
        assert exc_info.value.code == "stale_manifest_commit"

    def test_default_protocol_error_code_uses_protocol_error(
        self, daemon_service, mock_resources,
    ):
        """ProtocolError 不传 code 时 code='protocol_error'，
        dispatch 也应透传此值而非 refresh_failed。"""
        from callwarden.server.daemon_server import DaemonRpcError
        from callwarden.server.replicator import ProtocolError

        peer = _peer()

        def _raise_default(**kwargs):
            # 不传 code，默认 'protocol_error'
            raise ProtocolError("some protocol error")

        with patch(
            "callwarden.server.replicator.daemon_handle_refresh",
            side_effect=_raise_default,
        ):
            with pytest.raises(DaemonRpcError) as exc_info:
                _dispatch_refresh(daemon_service, peer, ws_id="1")
        # 不传 code 时 ProtocolError.code='protocol_error'
        assert exc_info.value.code == "protocol_error"

    def test_non_protocol_error_falls_back_to_refresh_failed(
        self, daemon_service, mock_resources,
    ):
        """非 ProtocolError（如 ValueError）→ code 默认 refresh_failed。"""
        from callwarden.server.daemon_server import DaemonRpcError

        peer = _peer()

        def _raise_value_error(**kwargs):
            raise ValueError("daemon internal error")

        with patch(
            "callwarden.server.replicator.daemon_handle_refresh",
            side_effect=_raise_value_error,
        ):
            with pytest.raises(DaemonRpcError) as exc_info:
                _dispatch_refresh(daemon_service, peer, ws_id="1")
        assert exc_info.value.code == "refresh_failed"

    def test_committed_response_unaffected_by_error_branch(
        self, daemon_service, mock_resources,
    ):
        """正常 committed 路径不受新 except 影响。"""
        peer = _peer()

        # 让 mock_replicator.replicate 返回带 generation/applied_count/duration_ms 的对象
        mock_replicate_result = MagicMock(
            generation="1:1", applied_count=1, duration_ms=1,
        )
        mock_resources["replicator"].replicate.return_value = mock_replicate_result

        def _committed_response(**kwargs):
            return {"status": "committed", "generation": "1:1", "content_hash": "abc"}

        with patch(
            "callwarden.server.replicator.daemon_handle_refresh",
            side_effect=_committed_response,
        ):
            result = _dispatch_refresh(daemon_service, peer, ws_id="1")
        assert result["status"] == "committed"
        assert result["generation"] == "1:1"
        # replication 字段也存在
        assert "replication" in result
        assert result["replication"]["generation"] == "1:1"


# ============================================
# G9/G34 批次7：daemon_server.py canonical_bytes 提取链路
# 验证 hex/b64/abs_path 三个分支的正确性与安全校验
# ============================================


class TestDaemonServerRefreshCanonicalBytesExtraction:
    """daemon_server.py workspace.file.refresh canonical_bytes 提取测试。

    G9/G34 批次7：Python daemon 同步 Rust 端协议——
    优先级：FD > canonical_bytes_hex > canonical_bytes_b64 > abs_path。

    G10 批次7：常规文件 FD 路径补全校验（owner UID + 大小上限 + content_hash）。
    K2 批次7：abs_path 路径逃逸防护（必须落在 host_real_root 内）。
    """

    @pytest.fixture
    def daemon_service(self, tmp_path):
        return _make_daemon_service(tmp_path)

    @pytest.fixture
    def mock_resources(self, daemon_service):
        """patch _get_workspace_resources 返回 mock 资源。"""
        mock_ws_conn = MagicMock()
        mock_cas_conn = MagicMock()
        mock_staging_log = MagicMock()
        mock_replicator = MagicMock()
        mock_replicate_result = MagicMock(
            generation="1:1", applied_count=1, duration_ms=1,
        )
        mock_replicator.replicate.return_value = mock_replicate_result
        mock_resources = {
            "ws_conn": mock_ws_conn,
            "cas_conn": mock_cas_conn,
            "staging_log": mock_staging_log,
            "replicator": mock_replicator,
        }
        with patch.object(
            daemon_service, "_get_workspace_resources", return_value=mock_resources,
        ):
            yield mock_resources

    def test_hex_decode_success(self, daemon_service, mock_resources):
        """canonical_bytes_hex 成功解码并传给 daemon_handle_refresh。"""
        from callwarden.server.replicator import daemon_handle_refresh

        canonical = b"# Python source\nprint('hello')\n"
        hex_str = canonical.hex()
        captured_kwargs = {}

        def _capture(**kwargs):
            captured_kwargs.update(kwargs)
            return {
                "status": "committed",
                "generation": "1:1",
                "content_hash": "abc",
            }

        peer = _peer()
        mock_workspace = MagicMock()
        with patch(
            "callwarden.server.replicator.daemon_handle_refresh",
            side_effect=_capture,
        ), patch.object(
            daemon_service, "_owned_workspace", return_value=mock_workspace,
        ):
            result = _dispatch_refresh(
                daemon_service, peer, ws_id="1",
                params_extra={"canonical_bytes_hex": hex_str},
            )

        assert result["status"] == "committed"
        # canonical_bytes 已正确解码并传递给 daemon_handle_refresh
        assert captured_kwargs.get("canonical_bytes") == canonical

    def test_hex_decode_invalid_raises(self, daemon_service, mock_resources):
        """无效 hex 字符串触发 hex_decode_failed 错误码。"""
        from callwarden.server.daemon_server import DaemonRpcError

        peer = _peer()
        mock_workspace = MagicMock()
        with patch.object(
            daemon_service, "_owned_workspace", return_value=mock_workspace,
        ):
            with pytest.raises(DaemonRpcError) as exc_info:
                _dispatch_refresh(
                    daemon_service, peer, ws_id="1",
                    params_extra={"canonical_bytes_hex": "not-valid-hex!@#$"},
                )
        assert exc_info.value.code == "hex_decode_failed"

    def test_b64_legacy_path_still_works(self, daemon_service, mock_resources):
        """canonical_bytes_b64 兼容路径仍能正常解码。

        不使用 _dispatch_refresh（其默认注入 hex 字段），直接构造只含 b64 的
        params 验证 b64 兼容路径。
        """
        import base64

        canonical = b"legacy b64 content"
        b64_str = base64.b64encode(canonical).decode("ascii")
        captured_kwargs = {}

        def _capture(**kwargs):
            captured_kwargs.update(kwargs)
            return {"status": "committed", "generation": "1:1"}

        peer = _peer()
        mock_workspace = MagicMock()
        params = {
            "workspace_instance_id": "1",
            "rel_path": "main.py",
            "agent_session_id": "s1",
            "session_epoch": 1,
            "monotonic_seq": 1,
            "canonical_bytes_b64": b64_str,
        }
        with patch(
            "callwarden.server.replicator.daemon_handle_refresh",
            side_effect=_capture,
        ), patch.object(
            daemon_service, "_owned_workspace", return_value=mock_workspace,
        ):
            result = daemon_service.dispatch(
                peer, "workspace.file.refresh", params,
            )

        assert result["status"] == "committed"
        assert captured_kwargs.get("canonical_bytes") == canonical

    def test_abs_path_path_escape_rejected(self, daemon_service, mock_resources, tmp_path):
        """abs_path 落在 host_real_root 外被拒绝（path_escape）。"""
        from callwarden.server.daemon_server import DaemonRpcError

        peer = _peer()
        # host_real_root 设为 tmp_path
        mock_workspace = {
            "host_real_root": str(tmp_path),
            "snapshot_id": None,
        }
        # _validate_owned_path 会用 os.path.realpath 解析，逃逸路径在 tmp_path 外
        escape_path = str(tmp_path.parent / "outside_root.py")

        with patch.object(
            daemon_service, "_owned_workspace", return_value=mock_workspace,
        ), patch.object(
            daemon_service, "_validate_owned_path",
            return_value=os.path.realpath(escape_path),
        ):
            with pytest.raises(DaemonRpcError) as exc_info:
                params = {
                    "workspace_instance_id": "1",
                    "rel_path": "main.py",
                    "agent_session_id": "s1",
                    "session_epoch": 1,
                    "monotonic_seq": 1,
                    "abs_path": escape_path,
                }
                daemon_service.dispatch(peer, "workspace.file.refresh", params)
        assert exc_info.value.code == "path_escape"

    def test_fd_path_owner_mismatch_rejected(self, daemon_service, mock_resources, tmp_path):
        """常规文件 FD owner UID 不匹配被拒绝（fd_owner_mismatch）。

        G10 批次7：常规文件 FD 无 seal 保护，必须校验 owner UID 防跨用户攻击。
        """
        from callwarden.server.daemon_server import DaemonRpcError

        # 创建临时文件作为 FD
        tmp_file = tmp_path / "fd_content.bin"
        tmp_file.write_bytes(b"test content")
        fd = os.open(str(tmp_file), os.O_RDONLY)

        try:
            peer = _peer()
            # peer uid 是当前进程 uid；mock fstat 返回不同 uid
            fake_stat = MagicMock()
            fake_stat.st_uid = peer["uid"] + 999  # 不同 uid
            fake_stat.st_size = 12

            mock_workspace = MagicMock()
            with patch.object(
                daemon_service, "_owned_workspace", return_value=mock_workspace,
            ), patch("os.fstat", return_value=fake_stat):
                with patch(
                    "callwarden.server.ipc_transport.is_memfd",
                    return_value=False,
                ):
                    params = {
                        "workspace_instance_id": "1",
                        "rel_path": "main.py",
                        "agent_session_id": "s1",
                        "session_epoch": 1,
                        "monotonic_seq": 1,
                        "canonical_len": 12,
                        "content_hash": "",
                    }
                    with pytest.raises(DaemonRpcError) as exc_info:
                        daemon_service.dispatch(
                            peer, "workspace.file.refresh", params,
                            received_fds=[fd],
                        )
            assert exc_info.value.code == "fd_owner_mismatch"
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def test_fd_path_hash_mismatch_rejected(self, daemon_service, mock_resources, tmp_path):
        """常规文件 FD content_hash 不匹配被拒绝（fd_hash_mismatch）。

        G10 批次7：客户端提供 content_hash 时必须校验 sha256。
        """
        import hashlib

        from callwarden.server.daemon_server import DaemonRpcError

        canonical = b"actual file content"
        tmp_file = tmp_path / "fd_content.bin"
        tmp_file.write_bytes(canonical)
        fd = os.open(str(tmp_file), os.O_RDONLY)

        try:
            peer = _peer()
            fake_stat = MagicMock()
            fake_stat.st_uid = peer["uid"]  # owner 匹配
            fake_stat.st_size = len(canonical)

            # 提供错误的 content_hash
            wrong_hash = "0" * 64

            mock_workspace = MagicMock()
            with patch.object(
                daemon_service, "_owned_workspace", return_value=mock_workspace,
            ), patch("os.fstat", return_value=fake_stat):
                with patch(
                    "callwarden.server.ipc_transport.is_memfd",
                    return_value=False,
                ):
                    params = {
                        "workspace_instance_id": "1",
                        "rel_path": "main.py",
                        "agent_session_id": "s1",
                        "session_epoch": 1,
                        "monotonic_seq": 1,
                        "canonical_len": len(canonical),
                        "content_hash": wrong_hash,
                    }
                    with pytest.raises(DaemonRpcError) as exc_info:
                        daemon_service.dispatch(
                            peer, "workspace.file.refresh", params,
                            received_fds=[fd],
                        )
            assert exc_info.value.code == "fd_hash_mismatch"
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def test_fd_path_size_mismatch_rejected(self, daemon_service, mock_resources, tmp_path):
        """常规文件 FD canonical_len 不匹配被拒绝（fd_size_mismatch）。

        G10 批次7：canonical_len 与实际 FD 大小不一致视为篡改。
        """
        from callwarden.server.daemon_server import DaemonRpcError

        canonical = b"size mismatch test"
        tmp_file = tmp_path / "fd_content.bin"
        tmp_file.write_bytes(canonical)
        fd = os.open(str(tmp_file), os.O_RDONLY)

        try:
            peer = _peer()
            fake_stat = MagicMock()
            fake_stat.st_uid = peer["uid"]  # owner 匹配
            fake_stat.st_size = len(canonical)
            fake_stat.st_mode = 0o100644  # 常规文件

            mock_workspace = MagicMock()
            with patch.object(
                daemon_service, "_owned_workspace", return_value=mock_workspace,
            ), patch("os.fstat", return_value=fake_stat):
                with patch(
                    "callwarden.server.ipc_transport.is_memfd",
                    return_value=False,
                ):
                    params = {
                        "workspace_instance_id": "1",
                        "rel_path": "main.py",
                        "agent_session_id": "s1",
                        "session_epoch": 1,
                        "monotonic_seq": 1,
                        "canonical_len": len(canonical) + 100,  # 故意不匹配
                        "content_hash": "",
                    }
                    with pytest.raises(DaemonRpcError) as exc_info:
                        daemon_service.dispatch(
                            peer, "workspace.file.refresh", params,
                            received_fds=[fd],
                        )
            assert exc_info.value.code == "fd_size_mismatch"
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
