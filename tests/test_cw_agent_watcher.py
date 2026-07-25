"""G9: AgentWatcher + _AgentChangeHandler 单元测试。

验证 G9 watcher 组件的文件变更检测、防抖、daemon RPC 调用。

规范：
- docs/design/enterprise-architecture-evolution.md §v8
- docs/design/parse-input-abi.md §2（canonicalize_source 是唯一输入入口）

测试覆盖：
1. AgentWatcher 初始化（构造参数 + canonicalize_fn 加载）
2. handle_file_change（mock daemon_rpc_client + canonicalize_fn）
3. handle_file_change 降级路径（canonicalize 不可用时直接读文件）
4. handle_file_delete（mock daemon_rpc_client）
5. _AgentChangeHandler 防抖（schedule_process + _process_pending）
6. supported_exts 过滤（on_modified / on_created / on_deleted / on_moved）
7. start/stop（mock watchdog Observer）
8. run_agent_watcher_loop（stop_event 设置后退出）
"""

from __future__ import annotations

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
from callwarden.server.agent_watcher import (
    AgentWatcher,
    _AgentChangeHandler,
    run_agent_watcher_loop,
    HAS_WATCHDOG,
    _get_canonicalize_fn,
)


# ============================================
# Mock 工厂
# ============================================


def _make_mock_daemon_rpc():
    """构造 mock daemon_rpc_client。"""
    rpc = MagicMock()
    rpc.call.return_value = {"status": "committed", "generation": "gen_test_001"}
    return rpc


def _make_agent_session_with_epoch(ws_id="ws_test_001", epoch=5):
    """构造已协商 epoch 的 AgentSession。"""
    session = AgentSession.create_in_memory()
    session.register_workspace(ws_id)
    session.set_epoch(ws_id, epoch)
    return session


def _make_watcher(tmp_path, canonicalize_fn=None, ws_id="ws_test_001"):
    """构造 AgentWatcher 实例（不启动 Observer）。"""
    session = _make_agent_session_with_epoch(ws_id=ws_id)
    rpc = _make_mock_daemon_rpc()
    watcher = AgentWatcher(
        agent_session=session,
        daemon_rpc_client=rpc,
        workspace_instance_id=ws_id,
        watch_dir=str(tmp_path),
        supported_exts={".py", ".rs", ".ts"},
    )
    if canonicalize_fn is not None:
        watcher._canonicalize_fn = canonicalize_fn
    return watcher, session, rpc


def _make_mock_canonicalize(canonical_bytes=b"hello canonical", content_hash="abc123"):
    """构造 mock canonicalize 函数。"""
    fn = MagicMock()
    fn.return_value = {
        "canonical_bytes": canonical_bytes,
        "content_hash": content_hash,
        "canonical_total": len(canonical_bytes),
        "raw_total": len(canonical_bytes) + 5,
        "metadata": {},
    }
    return fn


# ============================================
# 1. AgentWatcher 初始化
# ============================================


class TestAgentWatcherInit:
    """AgentWatcher 构造与初始化。"""

    def test_init_basic(self, tmp_path):
        """基础构造：参数被正确保存。"""
        watcher, session, rpc = _make_watcher(tmp_path)
        assert watcher.agent_session is session
        assert watcher.daemon_rpc_client is rpc
        assert watcher.workspace_instance_id == "ws_test_001"
        assert watcher.watch_dir == str(tmp_path)
        assert ".py" in watcher.supported_exts
        assert watcher.debounce_time == 1.0
        assert not watcher.is_running

    def test_init_loads_canonicalize_fn(self, tmp_path):
        """构造时尝试加载 canonicalize_source_py。"""
        watcher, _, _ = _make_watcher(tmp_path)
        # _canonicalize_fn 可能是 None（Rust 扩展未编译）或 callable
        if watcher._canonicalize_fn is not None:
            assert callable(watcher._canonicalize_fn)

    def test_get_canonicalize_fn_returns_callable_or_none(self):
        """_get_canonicalize_fn 返回 callable 或 None。"""
        fn = _get_canonicalize_fn()
        assert fn is None or callable(fn)


# ============================================
# 2. handle_file_change
# ============================================


class TestAgentWatcherHandleFileChange:
    """AgentWatcher.handle_file_change 测试。"""

    def test_handle_change_with_canonicalize(self, tmp_path):
        """canonicalize_source_py 成功时使用规范化字节流。"""
        canonical = b"# canonical content\nprint('hello')\n"
        canonical_fn = _make_mock_canonicalize(
            canonical_bytes=canonical, content_hash="hash_canon_001",
        )
        watcher, session, rpc = _make_watcher(tmp_path, canonicalize_fn=canonical_fn)

        # 创建测试文件
        test_file = tmp_path / "main.py"
        test_file.write_text("print('hello')\n", encoding="utf-8")

        response = watcher.handle_file_change(str(test_file))

        # 验证 canonicalize 被调用
        canonical_fn.assert_called_once_with(str(test_file))
        # 验证 RPC 被调用
        rpc.call.assert_called_once()
        call_args = rpc.call.call_args
        assert call_args[0][0] == "workspace.file.refresh"
        params = call_args[0][1]
        assert params["workspace_instance_id"] == "ws_test_001"
        assert params["session_epoch"] == 5
        assert params["session_epoch"] == 5
        assert params["monotonic_seq"] == 1
        assert params["content_hash"] == "hash_canon_001"
        assert params["canonical_len"] == len(canonical)
        # canonical_bytes_hex 编码正确
        assert bytes.fromhex(params["canonical_bytes_hex"]) == canonical
        # rel_path 是相对 watch_dir 的路径
        assert params["rel_path"] == "main.py"
        # 响应透传
        assert response["status"] == "committed"

    def test_handle_change_seq_increments(self, tmp_path):
        """多次 handle_file_change 时 seq 递增。"""
        canonical_fn = _make_mock_canonicalize()
        watcher, _, _ = _make_watcher(tmp_path, canonicalize_fn=canonical_fn)

        test_file = tmp_path / "a.py"
        test_file.write_text("a\n", encoding="utf-8")

        watcher.handle_file_change(str(test_file))
        watcher.handle_file_change(str(test_file))
        watcher.handle_file_change(str(test_file))

        # 验证 3 次 RPC 调用，seq 分别是 1, 2, 3
        assert canonical_fn.call_count == 3
        seqs = [call.args[1]["monotonic_seq"]
               for call in watcher.daemon_rpc_client.call.call_args_list]
        assert seqs == [1, 2, 3]

    def test_handle_change_fallback_when_canonicalize_unavailable(self, tmp_path):
        """canonicalize_source_py 不可用时降级直接读文件。"""
        watcher, _, rpc = _make_watcher(tmp_path, canonicalize_fn=None)
        # 显式设为 None
        watcher._canonicalize_fn = None

        test_file = tmp_path / "fallback.py"
        # 使用 write_bytes 避免 Windows 默认换行符转换（\n → \r\n）
        content_bytes = b"print('fallback')\n"
        test_file.write_bytes(content_bytes)

        response = watcher.handle_file_change(str(test_file))

        # RPC 被调用
        rpc.call.assert_called_once()
        params = rpc.call.call_args[0][1]
        # canonical_bytes_hex 应该是文件内容的 hex
        assert bytes.fromhex(params["canonical_bytes_hex"]) == content_bytes
        # content_hash 应该是 sha256
        import hashlib
        expected_hash = hashlib.sha256(content_bytes).hexdigest()
        assert params["content_hash"] == expected_hash

    def test_handle_change_fallback_on_canonicalize_error(self, tmp_path):
        """canonicalize 抛异常时降级读文件。"""
        canonical_fn = MagicMock()
        canonical_fn.side_effect = RuntimeError("canonicalize failed")
        watcher, _, rpc = _make_watcher(tmp_path, canonicalize_fn=canonical_fn)

        test_file = tmp_path / "error.py"
        test_file.write_text("content\n", encoding="utf-8")

        response = watcher.handle_file_change(str(test_file))

        # RPC 仍然被调用（降级路径）
        rpc.call.assert_called_once()
        assert response["status"] == "committed"

    def test_handle_change_skips_deleted_file(self, tmp_path):
        """文件已被删除时跳过。"""
        canonical_fn = _make_mock_canonicalize()
        watcher, _, rpc = _make_watcher(tmp_path, canonicalize_fn=canonical_fn)

        non_existent = tmp_path / "nonexistent.py"
        response = watcher.handle_file_change(str(non_existent))

        # 不应该调用 canonicalize 或 RPC
        canonical_fn.assert_not_called()
        rpc.call.assert_not_called()
        assert response["status"] == "skipped_deleted"

    def test_handle_change_rpc_failure_raises(self, tmp_path):
        """RPC 失败时抛 AgentProtocolError。"""
        canonical_fn = _make_mock_canonicalize()
        watcher, _, rpc = _make_watcher(tmp_path, canonicalize_fn=canonical_fn)
        rpc.call.side_effect = RuntimeError("daemon unreachable")

        test_file = tmp_path / "fail.py"
        test_file.write_text("content\n", encoding="utf-8")

        from callwarden.server.agent_protocol import AgentProtocolError
        with pytest.raises(AgentProtocolError, match="refresh_failed"):
            watcher.handle_file_change(str(test_file))


# ============================================
# 3. handle_file_delete
# ============================================


class TestAgentWatcherHandleFileDelete:
    """AgentWatcher.handle_file_delete 测试。"""

    def test_handle_delete_calls_workspace_file_delete(self, tmp_path):
        """删除文件时发送 workspace.file.delete RPC。"""
        watcher, session, rpc = _make_watcher(tmp_path)

        deleted_file = tmp_path / "deleted.py"
        response = watcher.handle_file_delete(str(deleted_file))

        rpc.call.assert_called_once_with(
            "workspace.file.delete",
            {
                "workspace_instance_id": "ws_test_001",
                "rel_path": "deleted.py",
                "agent_session_id": session.session_id,
            },
        )

    def test_handle_delete_subdir_relpath(self, tmp_path):
        """子目录文件的 rel_path 正确计算。"""
        watcher, _, _ = _make_watcher(tmp_path)
        subdir = tmp_path / "src" / "deep"
        subdir.mkdir(parents=True)
        deleted = subdir / "module.py"
        deleted.write_text("a\n", encoding="utf-8")

        watcher.handle_file_delete(str(deleted))

        call_args = watcher.daemon_rpc_client.call.call_args
        params = call_args[0][1]
        assert params["rel_path"] == "src/deep/module.py"


# ============================================
# 4. _AgentChangeHandler 防抖
# ============================================


class TestAgentChangeHandlerDebounce:
    """_AgentChangeHandler 防抖逻辑测试。"""

    def test_handler_filters_unsupported_ext(self, tmp_path):
        """非支持扩展名的事件被过滤。"""
        watcher, _, _ = _make_watcher(tmp_path)
        handler = _AgentChangeHandler(
            agent_watcher=watcher,
            supported_exts={".py"},
            debounce_time=0.05,
        )

        # 构造 mock event
        class MockEvent:
            def __init__(self, path, is_directory=False):
                self.src_path = path
                self.is_directory = is_directory

        # .py 应该被加入 pending
        handler.on_modified(MockEvent(str(tmp_path / "a.py")))
        assert str(tmp_path / "a.py") in handler._pending_changes

        # .txt 应该被过滤
        handler._pending_changes.clear()
        handler.on_modified(MockEvent(str(tmp_path / "b.txt")))
        assert str(tmp_path / "b.txt") not in handler._pending_changes

    def test_handler_ignores_directory_events(self, tmp_path):
        """目录事件被忽略。"""
        watcher, _, _ = _make_watcher(tmp_path)
        handler = _AgentChangeHandler(
            agent_watcher=watcher,
            supported_exts={".py"},
            debounce_time=0.05,
        )

        class MockEvent:
            def __init__(self, path, is_directory=True):
                self.src_path = path
                self.is_directory = is_directory

        handler.on_modified(MockEvent(str(tmp_path / "subdir"), is_directory=True))
        assert len(handler._pending_changes) == 0

    def test_debounce_batches_multiple_changes(self, tmp_path):
        """防抖：多个变更合并为一次 _process_pending 调用。"""
        canonical_fn = _make_mock_canonicalize()
        watcher, _, rpc = _make_watcher(tmp_path, canonicalize_fn=canonical_fn)
        handler = _AgentChangeHandler(
            agent_watcher=watcher,
            supported_exts={".py"},
            debounce_time=0.1,
        )

        class MockEvent:
            def __init__(self, path, is_directory=False):
                self.src_path = path
                self.is_directory = is_directory

        # 创建 3 个文件并触发 modified 事件
        files = []
        for i in range(3):
            f = tmp_path / f"f{i}.py"
            f.write_text(f"content {i}\n", encoding="utf-8")
            files.append(f)
            handler.on_modified(MockEvent(str(f)))

        # 等待防抖触发 _process_pending
        time.sleep(0.3)

        # 验证 3 个文件都被处理（3 次 RPC 调用）
        assert rpc.call.call_count == 3

    def test_flush_pending_immediate(self, tmp_path):
        """flush_pending 立即处理所有 pending changes。"""
        canonical_fn = _make_mock_canonicalize()
        watcher, _, rpc = _make_watcher(tmp_path, canonicalize_fn=canonical_fn)
        handler = _AgentChangeHandler(
            agent_watcher=watcher,
            supported_exts={".py"},
            debounce_time=10.0,  # 长防抖，确保 flush 前不触发
        )

        class MockEvent:
            def __init__(self, path, is_directory=False):
                self.src_path = path
                self.is_directory = is_directory

        # 触发 2 个文件变更（pending 但未处理）
        f1 = tmp_path / "f1.py"
        f1.write_text("c1\n", encoding="utf-8")
        f2 = tmp_path / "f2.py"
        f2.write_text("c2\n", encoding="utf-8")
        handler.on_modified(MockEvent(str(f1)))
        handler.on_modified(MockEvent(str(f2)))
        assert len(handler._pending_changes) == 2

        # flush_pending 立即处理
        handler.flush_pending()

        # 验证 2 个 RPC 调用
        assert rpc.call.call_count == 2

    def test_shutdown_cancels_timer(self, tmp_path):
        """shutdown 取消 pending timer。"""
        watcher, _, _ = _make_watcher(tmp_path)
        handler = _AgentChangeHandler(
            agent_watcher=watcher,
            supported_exts={".py"},
            debounce_time=0.1,
        )

        class MockEvent:
            def __init__(self, path, is_directory=False):
                self.src_path = path
                self.is_directory = is_directory

        f = tmp_path / "x.py"
        f.write_text("c\n", encoding="utf-8")
        handler.on_modified(MockEvent(str(f)))
        assert handler._timer is not None

        handler.shutdown()
        # timer 被取消
        assert handler._timer is None


# ============================================
# 5. start / stop（mock Observer）
# ============================================


@pytest.mark.skipif(not HAS_WATCHDOG, reason="watchdog 未安装")
class TestAgentWatcherStartStop:
    """AgentWatcher.start / stop 测试（mock Observer）。"""

    def test_start_initializes_observer(self, tmp_path):
        """start 创建并启动 Observer。"""
        watcher, _, _ = _make_watcher(tmp_path)
        with patch("callwarden.server.agent_watcher.Observer") as MockObserver:
            mock_observer = MagicMock()
            MockObserver.return_value = mock_observer

            result = watcher.start()
            assert result is True
            assert watcher.is_running is True
            mock_observer.schedule.assert_called_once()
            mock_observer.start.assert_called_once()

    def test_start_idempotent(self, tmp_path):
        """重复 start 返回 True 不重启 Observer。"""
        watcher, _, _ = _make_watcher(tmp_path)
        with patch("callwarden.server.agent_watcher.Observer") as MockObserver:
            mock_observer = MagicMock()
            MockObserver.return_value = mock_observer

            watcher.start()
            result = watcher.start()  # 二次 start
            assert result is True
            # Observer 只创建一次
            assert MockObserver.call_count == 1

    def test_start_fails_on_missing_dir(self, tmp_path):
        """watch_dir 不存在时报错。"""
        watcher, _, _ = _make_watcher(tmp_path)
        watcher.watch_dir = str(tmp_path / "nonexistent_dir")

        with pytest.raises(RuntimeError, match="watch_dir 不存在"):
            watcher.start()

    def test_stop_cleans_observer(self, tmp_path):
        """stop 停止并清理 Observer。"""
        watcher, _, _ = _make_watcher(tmp_path)
        with patch("callwarden.server.agent_watcher.Observer") as MockObserver:
            mock_observer = MagicMock()
            MockObserver.return_value = mock_observer

            watcher.start()
            watcher.stop()
            assert watcher.is_running is False
            mock_observer.stop.assert_called_once()
            mock_observer.join.assert_called_once()

    def test_stop_when_not_running(self, tmp_path):
        """未启动时 stop 不报错。"""
        watcher, _, _ = _make_watcher(tmp_path)
        watcher.stop()  # 不抛异常
        assert watcher.is_running is False


# ============================================
# 6. run_agent_watcher_loop
# ============================================


@pytest.mark.skipif(not HAS_WATCHDOG, reason="watchdog 未安装")
class TestRunAgentWatcherLoop:
    """run_agent_watcher_loop 主循环测试。"""

    def test_loop_exits_on_stop_event(self, tmp_path):
        """stop_event 设置后 loop 退出。"""
        session = _make_agent_session_with_epoch()
        rpc = _make_mock_daemon_rpc()

        stop_event = threading.Event()

        # 0.5 秒后设置 stop_event
        def setter():
            time.sleep(0.5)
            stop_event.set()

        threading.Thread(target=setter, daemon=True).start()

        with patch("callwarden.server.agent_watcher.Observer") as MockObserver:
            mock_observer = MagicMock()
            MockObserver.return_value = mock_observer

            result = run_agent_watcher_loop(
                agent_session=session,
                daemon_rpc_client=rpc,
                workspace_instance_id="ws_test_001",
                watch_dir=str(tmp_path),
                supported_exts={".py"},
                stop_event=stop_event,
            )
            assert result == 0

    def test_loop_returns_2_when_watchdog_unavailable(self, tmp_path):
        """watchdog 未安装时返回 2。"""
        session = _make_agent_session_with_epoch()
        rpc = _make_mock_daemon_rpc()
        stop_event = threading.Event()

        with patch("callwarden.server.agent_watcher.HAS_WATCHDOG", False):
            result = run_agent_watcher_loop(
                agent_session=session,
                daemon_rpc_client=rpc,
                workspace_instance_id="ws_test_001",
                watch_dir=str(tmp_path),
                supported_exts={".py"},
                stop_event=stop_event,
            )
            assert result == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
