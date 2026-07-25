"""G9: AgentSession 单元测试。

验证 AgentSession 的 session_id / epoch / seq_counter 管理和持久化。

规范：
- docs/design/enterprise-architecture-evolution.md §v8
- docs/design/watcher-generation-state-machine.md §4.1（session epoch CAS）

测试覆盖：
1. create_in_memory / create_or_load（持久化 + 加载）
2. register_workspace / set_epoch / next_seq
3. epoch 重置 seq_counter
4. 未协商 epoch 时 next_seq 报错
5. 未注册 workspace 时 next_seq 报错
6. 多 workspace 独立 seq
7. 持久化 + 重新加载（epoch 清零）
8. 线程安全（多线程并发 next_seq）
9. to_dict / list_workspaces / is_active / remove_workspace
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"))

from server.agent_session import (
    AgentSession,
    DEFAULT_AGENT_SESSION_FILE,
)


# ============================================
# 1. create_in_memory 基础测试
# ============================================


class TestAgentSessionCreate:
    """AgentSession 工厂方法测试。"""

    def test_create_in_memory_default_session_id(self):
        """create_in_memory 默认生成 session_id。"""
        session = AgentSession.create_in_memory()
        assert session.session_id.startswith("agent-test-")
        assert len(session.session_id) > len("agent-test-")
        assert session.session_file == ":memory:"
        assert session.list_workspaces() == []

    def test_create_in_memory_custom_session_id(self):
        """create_in_memory 支持自定义 session_id。"""
        session = AgentSession.create_in_memory(session_id="my-agent-001")
        assert session.session_id == "my-agent-001"

    def test_create_in_memory_repr(self):
        """__repr__ 包含 session_id 和 workspace 数。"""
        session = AgentSession.create_in_memory(session_id="repr-test")
        r = repr(session)
        assert "repr-test" in r
        assert "workspaces=0" in r


# ============================================
# 2. workspace 操作 + epoch + seq
# ============================================


class TestAgentSessionWorkspaceOps:
    """AgentSession 的 workspace 级别操作。"""

    def test_register_workspace_idempotent(self):
        """register_workspace 幂等（重复注册不创建重复条目）。"""
        session = AgentSession.create_in_memory()
        ws_id = "abc123def456abcd"
        session.register_workspace(ws_id)
        session.register_workspace(ws_id)  # 重复
        assert session.list_workspaces() == [ws_id]
        assert session.get_epoch(ws_id) == 0
        assert session.get_seq(ws_id) == 0

    def test_set_epoch_resets_seq_counter(self):
        """set_epoch 重置 seq_counter=0（daemon 侧 latest_seq 也置 0）。"""
        session = AgentSession.create_in_memory()
        ws_id = "ws_test_001"
        session.register_workspace(ws_id)
        session.set_epoch(ws_id, 5)

        # 分配几个 seq
        assert session.next_seq(ws_id) == 1
        assert session.next_seq(ws_id) == 2
        assert session.next_seq(ws_id) == 3
        assert session.get_seq(ws_id) == 3

        # 重新协商 epoch → seq 重置
        session.set_epoch(ws_id, 6)
        assert session.get_epoch(ws_id) == 6
        assert session.get_seq(ws_id) == 0
        assert session.next_seq(ws_id) == 1

    def test_next_seq_monotonic(self):
        """next_seq 单调递增。"""
        session = AgentSession.create_in_memory()
        ws_id = "ws_mono"
        session.register_workspace(ws_id)
        session.set_epoch(ws_id, 1)
        for i in range(1, 11):
            assert session.next_seq(ws_id) == i

    def test_next_seq_without_epoch_raises(self):
        """未协商 epoch 时 next_seq 报错。"""
        session = AgentSession.create_in_memory()
        ws_id = "ws_no_epoch"
        session.register_workspace(ws_id)
        with pytest.raises(ValueError, match="session_epoch 未协商"):
            session.next_seq(ws_id)

    def test_next_seq_without_workspace_raises(self):
        """未注册 workspace 时 next_seq 报错。"""
        session = AgentSession.create_in_memory()
        with pytest.raises(ValueError, match="未注册"):
            session.next_seq("ws_not_registered")

    def test_is_active_after_set_epoch(self):
        """is_active 在注册+协商后返回 True。"""
        session = AgentSession.create_in_memory()
        ws_id = "ws_active"
        assert not session.is_active(ws_id)
        session.register_workspace(ws_id)
        assert not session.is_active(ws_id)  # 注册但未协商
        session.set_epoch(ws_id, 1)
        assert session.is_active(ws_id)

    def test_remove_workspace(self):
        """remove_workspace 移除后 is_active=False。"""
        session = AgentSession.create_in_memory()
        ws_id = "ws_remove"
        session.register_workspace(ws_id)
        session.set_epoch(ws_id, 1)
        assert session.remove_workspace(ws_id) is True
        assert not session.is_active(ws_id)
        assert ws_id not in session.list_workspaces()
        # 二次删除返回 False
        assert session.remove_workspace(ws_id) is False


# ============================================
# 3. 多 workspace 隔离
# ============================================


class TestAgentSessionMultiWorkspace:
    """多 workspace 独立 epoch + seq。"""

    def test_multi_workspace_independent_seq(self):
        """不同 workspace 的 seq 相互独立。"""
        session = AgentSession.create_in_memory()
        ws1 = "ws_multi_001"
        ws2 = "ws_multi_002"
        session.register_workspace(ws1)
        session.register_workspace(ws2)
        session.set_epoch(ws1, 1)
        session.set_epoch(ws2, 1)

        # 交替调用 next_seq，两个 workspace 独立递增
        assert session.next_seq(ws1) == 1
        assert session.next_seq(ws2) == 1
        assert session.next_seq(ws1) == 2
        assert session.next_seq(ws1) == 3
        assert session.next_seq(ws2) == 2

    def test_multi_workspace_independent_epoch(self):
        """不同 workspace 的 epoch 独立。"""
        session = AgentSession.create_in_memory()
        ws1 = "ws_epoch_001"
        ws2 = "ws_epoch_002"
        session.register_workspace(ws1)
        session.register_workspace(ws2)
        session.set_epoch(ws1, 10)
        session.set_epoch(ws2, 20)
        assert session.get_epoch(ws1) == 10
        assert session.get_epoch(ws2) == 20

    def test_set_epoch_for_unregistered_workspace(self):
        """set_epoch 对未注册的 workspace 会自动注册。"""
        session = AgentSession.create_in_memory()
        ws_id = "ws_auto_register"
        session.set_epoch(ws_id, 3)  # 未注册也允许 set
        assert session.is_active(ws_id)
        assert session.get_epoch(ws_id) == 3


# ============================================
# 4. 持久化 + 加载
# ============================================


class TestAgentSessionPersistence:
    """AgentSession 持久化测试。"""

    def test_create_or_load_creates_new_file(self, tmp_path):
        """create_or_load 创建新 session 文件。"""
        session_file = str(tmp_path / "session.json")
        session = AgentSession.create_or_load(session_file)
        assert os.path.isfile(session_file)
        assert session.session_id.startswith("agent-")
        # 文件内容合法 JSON
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["session_id"] == session.session_id
        assert data["version"] == 1

    def test_load_existing_session_id_preserved(self, tmp_path):
        """加载现有 session 时 session_id 保持不变。"""
        session_file = str(tmp_path / "session.json")
        # 第一次创建
        session1 = AgentSession.create_or_load(session_file)
        original_id = session1.session_id
        # 注册 workspace + 协商 epoch
        ws_id = "ws_persist_001"
        session1.register_workspace(ws_id)
        session1.set_epoch(ws_id, 5)
        session1.next_seq(ws_id)

        # 第二次加载（模拟 agent 重启）
        session2 = AgentSession.create_or_load(session_file)
        assert session2.session_id == original_id  # session_id 保持
        assert ws_id in session2.list_workspaces()
        # epoch 清零（必须重新协商）
        assert session2.get_epoch(ws_id) == 0
        assert session2.get_seq(ws_id) == 0

    def test_load_corrupted_file_creates_new(self, tmp_path):
        """文件损坏时重新创建 session。"""
        session_file = str(tmp_path / "session.json")
        # 写入损坏 JSON
        with open(session_file, "w") as f:
            f.write("{invalid json content")
        session = AgentSession.create_or_load(session_file)
        assert session.session_id.startswith("agent-")
        assert session.list_workspaces() == []

    def test_to_dict_returns_snapshot(self):
        """to_dict 返回 session 状态快照。"""
        session = AgentSession.create_in_memory(session_id="dict-test")
        ws_id = "ws_dict_001"
        session.register_workspace(ws_id)
        session.set_epoch(ws_id, 7)
        session.next_seq(ws_id)

        d = session.to_dict()
        assert d["session_id"] == "dict-test"
        assert ws_id in d["workspaces"]
        assert d["workspaces"][ws_id]["epoch"] == 7
        assert d["workspaces"][ws_id]["seq_counter"] == 1


# ============================================
# 5. 线程安全
# ============================================


class TestAgentSessionThreadSafety:
    """AgentSession 多线程并发测试。"""

    def test_concurrent_next_seq_no_duplicates(self):
        """多线程并发 next_seq 不会产生重复 seq。"""
        session = AgentSession.create_in_memory()
        ws_id = "ws_concurrent"
        session.register_workspace(ws_id)
        session.set_epoch(ws_id, 1)

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            local_results = []
            for _ in range(50):
                local_results.append(session.next_seq(ws_id))
            with results_lock:
                results.extend(local_results)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 200 个 seq 都应该唯一（无重复）
        assert len(results) == 200
        assert len(set(results)) == 200  # 无重复
        # 所有 seq 在 [1, 200] 范围内
        assert min(results) == 1
        assert max(results) == 200

    def test_concurrent_multi_workspace_isolated(self):
        """多 workspace 并发不串扰。"""
        session = AgentSession.create_in_memory()
        ws1 = "ws_concurrent_1"
        ws2 = "ws_concurrent_2"
        session.register_workspace(ws1)
        session.register_workspace(ws2)
        session.set_epoch(ws1, 1)
        session.set_epoch(ws2, 1)

        ws1_results = []
        ws2_results = []
        ws1_lock = threading.Lock()
        ws2_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker_ws1():
            barrier.wait()
            local = []
            for _ in range(30):
                local.append(session.next_seq(ws1))
            with ws1_lock:
                ws1_results.extend(local)

        def worker_ws2():
            barrier.wait()
            local = []
            for _ in range(30):
                local.append(session.next_seq(ws2))
            with ws2_lock:
                ws2_results.extend(local)

        t1 = threading.Thread(target=worker_ws1)
        t2 = threading.Thread(target=worker_ws2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 两个 workspace 独立递增到 30
        assert len(ws1_results) == 30
        assert len(ws2_results) == 30
        assert len(set(ws1_results)) == 30  # 无重复
        assert len(set(ws2_results)) == 30
        # 两个 workspace 的 seq 集合相同（都从 1 到 30）
        assert set(ws1_results) == set(ws2_results) == set(range(1, 31))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
