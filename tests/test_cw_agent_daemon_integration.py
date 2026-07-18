"""G9: Agent ↔ Daemon 集成测试（E2E）。

验证 agent_protocol.py 的 UDS 握手 + refresh RPC 流程（使用 mock daemon）。

规范：
- docs/design/enterprise-architecture-evolution.md §v8
- docs/design/watcher-generation-state-machine.md §4.1（session epoch CAS）
- docs/design/daemon-ipc-security.md §3（memfd 协议）/ §6（S10：传输路径透明）

测试覆盖：
1. user_agent_ping（mock daemon_rpc_client）
2. user_agent_connect（mock 返回 session_epoch，AgentSession.set_epoch 被调用）
3. user_agent_connect 错误路径（daemon 不可达 / 无效 epoch）
4. build_refresh_message（session_epoch + monotonic_seq 透传）
5. send_refresh_to_daemon 小文件路径（canonical_bytes_hex）
6. send_refresh_to_daemon 无 canonical_bytes 兼容路径（abs_path）
7. send_refresh_to_daemon 失败路径（RPC 异常）
8. 完整流程：connect → handle_file_change → RPC（mock daemon）
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"))

from callwarden.server.agent_session import AgentSession
from callwarden.server.agent_protocol import (
    user_agent_connect,
    user_agent_ping,
    build_refresh_message,
    send_refresh_to_daemon,
    AgentProtocolError,
    MSG_CONNECT,
    MSG_REFRESH,
    MSG_PING,
    REFRESH_LARGE_FILE_THRESHOLD,
)
from callwarden.server.agent_watcher import AgentWatcher


# ============================================
# Mock 工厂
# ============================================


def _make_mock_daemon_rpc():
    """构造 mock daemon_rpc_client。"""
    rpc = MagicMock()
    rpc.call.return_value = {"status": "committed", "generation": "gen_test_001"}
    return rpc


def _make_session_with_epoch(ws_id="ws_e2e_001", epoch=5):
    """构造已协商 epoch 的 AgentSession。"""
    session = AgentSession.create_in_memory()
    session.register_workspace(ws_id)
    session.set_epoch(ws_id, epoch)
    return session


# ============================================
# 1. user_agent_ping
# ============================================


class TestUserAgentPing:
    """user_agent_ping 测试。"""

    def test_ping_success(self):
        """daemon 可达时返回 ping 响应。"""
        rpc = _make_mock_daemon_rpc()
        rpc.call.return_value = {"status": "ok", "peer_uid": 1000, "pid": 12345}

        response = user_agent_ping(rpc)
        assert response["status"] == "ok"
        assert response["peer_uid"] == 1000
        rpc.call.assert_called_once_with("ping")

    def test_ping_daemon_unreachable_raises(self):
        """daemon 不可达时抛 AgentProtocolError。"""
        rpc = MagicMock()
        rpc.call.side_effect = ConnectionError("daemon socket not found")

        with pytest.raises(AgentProtocolError, match="daemon_unreachable"):
            user_agent_ping(rpc)


# ============================================
# 2. user_agent_connect
# ============================================


class TestUserAgentConnect:
    """user_agent_connect 握手测试。"""

    def test_connect_success(self):
        """握手成功：daemon 返回 session_epoch，AgentSession 状态更新。"""
        rpc = _make_mock_daemon_rpc()
        rpc.call.return_value = {"session_epoch": 7, "workspace_instance_id": "ws_e2e_001"}

        session = AgentSession.create_in_memory(session_id="agent-e2e-001")
        epoch = user_agent_connect(rpc, "ws_e2e_001", session)

        assert epoch == 7
        # RPC 被调用
        rpc.call.assert_called_once_with(
            "workspace.connect",
            {
                "workspace_instance_id": "ws_e2e_001",
                "agent_session_id": "agent-e2e-001",
            },
        )
        # AgentSession 状态更新
        assert session.get_epoch("ws_e2e_001") == 7
        assert session.is_active("ws_e2e_001")

    def test_connect_registers_workspace_automatically(self):
        """握手时自动注册 workspace 到 AgentSession。"""
        rpc = _make_mock_daemon_rpc()
        rpc.call.return_value = {"session_epoch": 1}

        session = AgentSession.create_in_memory()
        # workspace 未注册
        assert "ws_auto_reg" not in session.list_workspaces()
        user_agent_connect(rpc, "ws_auto_reg", session)
        # 握手后已注册
        assert "ws_auto_reg" in session.list_workspaces()
        assert session.get_epoch("ws_auto_reg") == 1

    def test_connect_rpc_failure_raises(self):
        """workspace.connect RPC 失败时抛 AgentProtocolError。"""
        rpc = MagicMock()
        rpc.call.side_effect = ConnectionError("daemon unreachable")

        session = AgentSession.create_in_memory()
        with pytest.raises(AgentProtocolError, match="connect_failed"):
            user_agent_connect(rpc, "ws_fail", session)

    def test_connect_invalid_epoch_raises(self):
        """daemon 返回非法 epoch 时抛 AgentProtocolError。"""
        rpc = _make_mock_daemon_rpc()
        rpc.call.return_value = {"session_epoch": 0}  # 0 非法

        session = AgentSession.create_in_memory()
        with pytest.raises(AgentProtocolError, match="invalid_epoch"):
            user_agent_connect(rpc, "ws_invalid_epoch", session)

    def test_connect_missing_epoch_in_response_raises(self):
        """daemon 响应缺少 session_epoch 时抛错。"""
        rpc = _make_mock_daemon_rpc()
        rpc.call.return_value = {}  # 缺少 session_epoch

        session = AgentSession.create_in_memory()
        with pytest.raises(AgentProtocolError, match="invalid_epoch"):
            user_agent_connect(rpc, "ws_missing_epoch", session)

    def test_connect_resets_seq_counter(self):
        """握手后 seq_counter 被重置为 0。"""
        rpc = _make_mock_daemon_rpc()

        session = AgentSession.create_in_memory()
        ws_id = "ws_reset_seq"
        # 预先协商 + 分配几个 seq
        session.register_workspace(ws_id)
        session.set_epoch(ws_id, 3)
        session.next_seq(ws_id)
        session.next_seq(ws_id)
        assert session.get_seq(ws_id) == 2

        # 再次握手（模拟 agent 重启）
        rpc.call.return_value = {"session_epoch": 4}
        user_agent_connect(rpc, ws_id, session)

        # epoch 更新为 4，seq 重置为 0
        assert session.get_epoch(ws_id) == 4
        assert session.get_seq(ws_id) == 0
        # 下一个 seq 是 1
        assert session.next_seq(ws_id) == 1


# ============================================
# 3. build_refresh_message
# ============================================


class TestBuildRefreshMessage:
    """build_refresh_message 测试。"""

    def test_build_message_contains_all_fields(self):
        """消息包含所有必要字段。"""
        session = _make_session_with_epoch(ws_id="ws_msg_001", epoch=10)
        msg = build_refresh_message(session, "ws_msg_001", "src/main.py")

        assert msg["workspace_instance_id"] == "ws_msg_001"
        assert msg["rel_path"] == "src/main.py"
        assert msg["agent_session_id"] == session.session_id
        assert msg["session_epoch"] == 10
        assert msg["monotonic_seq"] == 1

    def test_build_message_seq_increments(self):
        """多次构建消息时 seq 递增。"""
        session = _make_session_with_epoch(ws_id="ws_seq", epoch=1)
        seqs = [build_refresh_message(session, "ws_seq", "a.py")["monotonic_seq"]
                for _ in range(5)]
        assert seqs == [1, 2, 3, 4, 5]

    def test_build_message_session_not_active_raises(self):
        """未协商 session 时抛错。"""
        session = AgentSession.create_in_memory()
        session.register_workspace("ws_inactive")
        # 未 set_epoch
        with pytest.raises(AgentProtocolError, match="session_not_active"):
            build_refresh_message(session, "ws_inactive", "a.py")

    def test_build_message_workspace_not_registered_raises(self):
        """workspace 未注册时抛错（is_active=False）。"""
        session = AgentSession.create_in_memory()
        with pytest.raises(AgentProtocolError, match="session_not_active"):
            build_refresh_message(session, "ws_not_reg", "a.py")


# ============================================
# 4. send_refresh_to_daemon: 小文件 hex 路径
# ============================================


class TestSendRefreshSmallFile:
    """send_refresh_to_daemon 小文件路径（canonical_bytes_hex）。"""

    def test_small_file_uses_hex_path(self, tmp_path):
        """小文件走 canonical_bytes_hex 路径。"""
        rpc = _make_mock_daemon_rpc()
        session = _make_session_with_epoch(ws_id="ws_small", epoch=5)

        canonical = b"# Python file\nprint('hello')\n"
        content_hash = hashlib.sha256(canonical).hexdigest()

        response = send_refresh_to_daemon(
            daemon_rpc_client=rpc,
            agent_session=session,
            workspace_instance_id="ws_small",
            rel_path="main.py",
            abs_path=str(tmp_path / "main.py"),
            canonical_bytes=canonical,
            content_hash=content_hash,
        )

        rpc.call.assert_called_once()
        call_args = rpc.call.call_args
        assert call_args[0][0] == "workspace.file.refresh"
        params = call_args[0][1]
        assert params["workspace_instance_id"] == "ws_small"
        assert params["rel_path"] == "main.py"
        assert params["session_epoch"] == 5
        assert params["monotonic_seq"] == 1
        assert params["canonical_len"] == len(canonical)
        assert params["content_hash"] == content_hash
        assert params["canonical_bytes_hex"] == canonical.hex()
        assert response["status"] == "committed"

    def test_small_file_at_threshold_boundary(self, tmp_path):
        """恰好等于阈值时走 hex 路径。"""
        rpc = _make_mock_daemon_rpc()
        session = _make_session_with_epoch(ws_id="ws_boundary", epoch=1)

        # 构造恰好等于阈值大小的 canonical_bytes
        canonical = b"x" * REFRESH_LARGE_FILE_THRESHOLD
        content_hash = hashlib.sha256(canonical).hexdigest()

        send_refresh_to_daemon(
            daemon_rpc_client=rpc,
            agent_session=session,
            workspace_instance_id="ws_boundary",
            rel_path="big.py",
            abs_path=str(tmp_path / "big.py"),
            canonical_bytes=canonical,
            content_hash=content_hash,
        )

        # 应该走 hex 路径（<= 阈值）
        params = rpc.call.call_args[0][1]
        assert "canonical_bytes_hex" in params
        assert "canonical_len" in params


# ============================================
# 5. send_refresh_to_daemon: 无 canonical_bytes 兼容路径
# ============================================


class TestSendRefreshNoCanonicalBytes:
    """send_refresh_to_daemon 无 canonical_bytes 兼容路径。"""

    def test_no_canonical_bytes_uses_abs_path(self, tmp_path):
        """无 canonical_bytes 时 params 包含 abs_path。"""
        rpc = _make_mock_daemon_rpc()
        session = _make_session_with_epoch(ws_id="ws_abs", epoch=1)

        abs_path = str(tmp_path / "main.py")
        send_refresh_to_daemon(
            daemon_rpc_client=rpc,
            agent_session=session,
            workspace_instance_id="ws_abs",
            rel_path="main.py",
            abs_path=abs_path,
            canonical_bytes=None,
        )

        params = rpc.call.call_args[0][1]
        assert params["abs_path"] == abs_path
        # canonical_bytes_hex 不应该出现
        assert "canonical_bytes_hex" not in params


# ============================================
# 6. send_refresh_to_daemon: 失败路径
# ============================================


class TestSendRefreshFailure:
    """send_refresh_to_daemon 失败路径。"""

    def test_rpc_failure_raises_agent_protocol_error(self, tmp_path):
        """RPC 异常时抛 AgentProtocolError。"""
        rpc = MagicMock()
        rpc.call.side_effect = RuntimeError("daemon crashed")

        session = _make_session_with_epoch(ws_id="ws_fail", epoch=1)
        canonical = b"content"

        with pytest.raises(AgentProtocolError, match="refresh_failed"):
            send_refresh_to_daemon(
                daemon_rpc_client=rpc,
                agent_session=session,
                workspace_instance_id="ws_fail",
                rel_path="main.py",
                abs_path=str(tmp_path / "main.py"),
                canonical_bytes=canonical,
                content_hash=hashlib.sha256(canonical).hexdigest(),
            )


# ============================================
# 7. 消息类型常量
# ============================================


class TestMessageTypeConstants:
    """消息类型常量定义。"""

    def test_constants_distinct(self):
        """消息类型常量互不相同。"""
        assert MSG_PING != MSG_CONNECT
        assert MSG_CONNECT != MSG_REFRESH
        assert MSG_PING != MSG_REFRESH

    def test_constants_are_int(self):
        """消息类型是 int。"""
        assert isinstance(MSG_PING, int)
        assert isinstance(MSG_CONNECT, int)
        assert isinstance(MSG_REFRESH, int)


# ============================================
# 8. E2E 完整流程
# ============================================


class TestEndToEndFlow:
    """完整流程：connect → handle_file_change → RPC。"""

    def test_full_flow_with_mock_daemon(self, tmp_path):
        """完整流程：握手 → 文件变更 → refresh RPC。"""
        rpc = _make_mock_daemon_rpc()
        # workspace.connect 返回 epoch=3
        rpc.call.side_effect = [
            # ping
            {"status": "ok", "peer_uid": 1000, "pid": 12345},
            # workspace.connect
            {"session_epoch": 3, "workspace_instance_id": "ws_e2e_full"},
            # workspace.register（可能已注册）
            {"workspace_instance_id": "ws_e2e_full"},
            # workspace.file.refresh（小文件）
            {"status": "committed", "generation": "gen_001"},
        ]

        session = AgentSession.create_in_memory(session_id="agent-e2e-full")

        # 1. ping
        ping_resp = user_agent_ping(rpc)
        assert ping_resp["status"] == "ok"

        # 2. connect
        epoch = user_agent_connect(rpc, "ws_e2e_full", session)
        assert epoch == 3
        assert session.is_active("ws_e2e_full")

        # 3. workspace.register（agent 启动时调用）
        rpc.call("workspace.register", {"client_view_root": str(tmp_path)})

        # 4. 创建文件并触发 refresh
        test_file = tmp_path / "main.py"
        test_file.write_bytes(b"print('hello')\n")

        # mock canonicalize_fn（返回规范化字节流）
        canonical = b"print('hello')\n"
        canonical_fn = MagicMock()
        canonical_fn.return_value = {
            "canonical_bytes": canonical,
            "content_hash": hashlib.sha256(canonical).hexdigest(),
            "canonical_total": len(canonical),
            "raw_total": len(canonical),
            "metadata": {},
        }

        watcher = AgentWatcher(
            agent_session=session,
            daemon_rpc_client=rpc,
            workspace_instance_id="ws_e2e_full",
            watch_dir=str(tmp_path),
            supported_exts={".py"},
        )
        watcher._canonicalize_fn = canonical_fn

        response = watcher.handle_file_change(str(test_file))
        assert response["status"] == "committed"
        assert response["generation"] == "gen_001"

        # 验证 RPC 调用顺序
        methods = [call[0][0] for call in rpc.call.call_args_list]
        assert methods == ["ping", "workspace.connect", "workspace.register",
                           "workspace.file.refresh"]

    def test_full_flow_multiple_refresh_seq_increments(self, tmp_path):
        """多次 refresh 时 seq 单调递增。"""
        rpc = _make_mock_daemon_rpc()
        rpc.call.side_effect = [
            {"session_epoch": 1},  # connect
            {"status": "committed"},  # refresh 1
            {"status": "committed"},  # refresh 2
            {"status": "committed"},  # refresh 3
        ]

        session = AgentSession.create_in_memory()
        user_agent_connect(rpc, "ws_seq_e2e", session)

        # 模拟 3 次 canonicalize + refresh
        canonical_fn = MagicMock()
        canonical_fn.return_value = {
            "canonical_bytes": b"x",
            "content_hash": "a" * 64,
        }

        watcher = AgentWatcher(
            agent_session=session,
            daemon_rpc_client=rpc,
            workspace_instance_id="ws_seq_e2e",
            watch_dir=str(tmp_path),
            supported_exts={".py"},
        )
        watcher._canonicalize_fn = canonical_fn

        test_file = tmp_path / "a.py"
        test_file.write_bytes(b"x\n")

        # 3 次 refresh
        watcher.handle_file_change(str(test_file))
        watcher.handle_file_change(str(test_file))
        watcher.handle_file_change(str(test_file))

        # 验证 4 次 RPC 调用（1 connect + 3 refresh）
        assert rpc.call.call_count == 4
        # 最后 3 次 refresh 的 seq 应该是 1, 2, 3
        refresh_calls = rpc.call.call_args_list[1:]  # 跳过 connect
        seqs = [call[0][1]["monotonic_seq"] for call in refresh_calls]
        assert seqs == [1, 2, 3]


# ============================================
# 9. AgentProtocolError 结构
# ============================================


class TestAgentProtocolError:
    """AgentProtocolError 异常结构。"""

    def test_error_has_code_and_message(self):
        """AgentProtocolError 包含 code 和 message 属性。"""
        err = AgentProtocolError("test_code", "test message")
        assert err.code == "test_code"
        assert err.message == "test message"
        assert "test_code" in str(err)
        assert "test message" in str(err)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
