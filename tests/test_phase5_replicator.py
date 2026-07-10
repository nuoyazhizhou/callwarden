"""
Phase 5.7: Replicator 合并 delta 并发布新 generation 测试
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"))

from server.staging_log import StagingLog, create_staging_entry
from server.replicator import Replicator, ReplicationResult


# ============================================
# TestReplicatorBasic —— 基础功能
# ============================================

class TestReplicatorBasic:
    """基础功能测试"""

    def test_no_pending(self, tmp_path):
        """无 pending entries 时返回空结果"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)
        replicator = Replicator(log)

        result = replicator.replicate("ws1")
        assert result.success
        assert result.pending_count == 0
        assert result.applied_count == 0

    def test_replicate_with_entries(self, tmp_path):
        """有 pending entries 时执行 replication"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # 添加 3 条 entries
        for i in range(3):
            entry = create_staging_entry(
                workspace_id="ws1",
                file_path=f"file_{i}.py",
                content_hash=f"hash_{i}",
                language="python",
                parse_delta={"symbol_delta": {"added": [f"func_{i}"]}},
            )
            log.append(entry)

        replicator = Replicator(log)
        result = replicator.replicate("ws1")

        assert result.success
        assert result.pending_count == 3
        assert result.applied_count == 3
        assert len(result.applied_lsns) == 3

        # 所有 entries 应该被标记为 applied
        pending = log.read_pending()
        assert len(pending) == 0

    def test_replicate_specific_workspace(self, tmp_path):
        """只 replicate 指定 workspace 的 entries"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # 添加不同 workspace 的 entries
        for ws in ["ws1", "ws2", "ws1"]:
            entry = create_staging_entry(
                workspace_id=ws,
                file_path=f"{ws}_file.py",
                content_hash=f"hash_{ws}",
                language="python",
            )
            log.append(entry)

        replicator = Replicator(log)
        result = replicator.replicate("ws1")

        assert result.success
        assert result.pending_count == 2  # ws1 有 2 条
        assert result.applied_count == 2

        # ws2 的 entry 应该还是 pending
        pending = log.read_pending()
        assert len(pending) == 1
        assert pending[0].workspace_id == "ws2"

    def test_repr(self, tmp_path):
        """__repr__"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)
        replicator = Replicator(log)
        repr_str = repr(replicator)
        assert "Replicator" in repr_str


# ============================================
# TestReplicatorTruncate —— 截断
# ============================================

class TestReplicatorTruncate:
    """截断测试"""

    def test_log_truncated_after_replication(self, tmp_path):
        """replication 后 log 被截断"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for i in range(5):
            entry = create_staging_entry(
                workspace_id="ws1",
                file_path=f"file_{i}.py",
                content_hash=f"hash_{i}",
                language="python",
            )
            log.append(entry)

        replicator = Replicator(log)
        result = replicator.replicate("ws1")

        assert result.applied_count == 5

        # log 应该被截断（所有 entries 已 applied）
        entries = log.read()
        assert len(entries) == 0

    def test_partial_truncate(self, tmp_path):
        """部分截断（多 workspace 混合）"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # ws1 的 entries
        for i in range(3):
            entry = create_staging_entry("ws1", f"ws1_{i}.py", f"hash1_{i}", "python")
            log.append(entry)

        # ws2 的 entries
        for i in range(2):
            entry = create_staging_entry("ws2", f"ws2_{i}.py", f"hash2_{i}", "python")
            log.append(entry)

        replicator = Replicator(log)
        replicator.replicate("ws1")

        # ws1 的 entries 被截断，ws2 保留
        entries = log.read()
        assert len(entries) == 2
        assert all(e.workspace_id == "ws2" for e in entries)


# ============================================
# TestReplicatorMerge —— Delta 合并
# ============================================

class TestReplicatorMerge:
    """Delta 合并测试"""

    def test_merge_deltas(self, tmp_path):
        """合并多个 entries 的 delta"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # 添加带 parse_delta 的 entries
        for i in range(3):
            entry = create_staging_entry(
                workspace_id="ws1",
                file_path=f"file_{i}.py",
                content_hash=f"hash_{i}",
                language="python",
                parse_delta={
                    "symbol_delta": {
                        "added": [f"func_{i}_a", f"func_{i}_b"],
                        "removed": [],
                        "changed": [],
                    },
                },
                resolve_delta={
                    "added": [{"caller": f"func_{i}_a", "callee": "target"}],
                    "removed": [],
                },
            )
            log.append(entry)

        replicator = Replicator(log)
        pending = log.read_pending()
        merged = replicator._merge_deltas(pending)

        assert merged["total_added_symbols"] == 6  # 3 entries × 2 symbols each
        assert merged["total_added_edges"] == 3
        assert len(merged["files"]) == 3

    def test_merge_empty_delta(self, tmp_path):
        """合并空 delta"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        entry = create_staging_entry("ws1", "file.py", "hash", "python")
        log.append(entry)

        replicator = Replicator(log)
        pending = log.read_pending()
        merged = replicator._merge_deltas(pending)

        assert merged["total_added_symbols"] == 0
        assert merged["total_removed_symbols"] == 0
        assert len(merged["files"]) == 1


# ============================================
# TestReplicatorRecovery —— 恢复
# ============================================

class TestReplicatorRecovery:
    """恢复测试"""

    def test_recover_pending(self, tmp_path):
        """恢复 pending entries"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # 写入 3 条 pending entries（模拟 crash 前未处理）
        for i in range(3):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        # 模拟重启：创建新 Replicator
        log2 = StagingLog(log_path)
        replicator = Replicator(log2)
        result = replicator.recover("ws1")

        assert result.success
        assert result.pending_count == 3
        assert result.applied_count == 3

    def test_recover_mixed_status(self, tmp_path):
        """恢复混合状态的 entries（有 pending 和 applied）"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # 写入 3 条，标记第 1 条为 applied
        for i in range(3):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)
        log.mark_applied(1)

        # 模拟重启
        log2 = StagingLog(log_path)
        replicator = Replicator(log2)
        result = replicator.recover("ws1")

        assert result.success
        assert result.pending_count == 2  # 只处理 pending 的
        assert result.applied_count == 2


# ============================================
# TestReplicatorResult —— 结果对象
# ============================================

class TestReplicatorResult:
    """ReplicationResult 测试"""

    def test_result_summary(self):
        """result summary"""
        result = ReplicationResult(
            success=True,
            workspace_id="ws1",
            generation=5,
            applied_lsns=[1, 2, 3],
            pending_count=3,
            applied_count=3,
        )
        summary = result.summary()
        assert "ok" in summary
        assert "ws1" in summary
        assert "gen=5" in summary
        assert "3/3" in summary

    def test_failed_result(self):
        """失败结果"""
        result = ReplicationResult(
            success=False,
            workspace_id="ws1",
            error="publish failed",
        )
        assert not result.success
        assert result.error == "publish failed"
        summary = result.summary()
        assert "failed" in summary


# ============================================
# TestReplicatorGetPending —— 获取 pending 数量
# ============================================

class TestReplicatorGetPending:
    """get_pending_count 测试"""

    def test_get_pending_all(self, tmp_path):
        """获取所有 pending 数量"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for i in range(5):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        replicator = Replicator(log)
        assert replicator.get_pending_count() == 5

    def test_get_pending_by_workspace(self, tmp_path):
        """按 workspace 获取 pending 数量"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for ws in ["ws1", "ws1", "ws2"]:
            entry = create_staging_entry(ws, f"{ws}.py", f"hash_{ws}", "python")
            log.append(entry)

        replicator = Replicator(log)
        assert replicator.get_pending_count("ws1") == 2
        assert replicator.get_pending_count("ws2") == 1
        assert replicator.get_pending_count("ws3") == 0

    def test_get_pending_after_replication(self, tmp_path):
        """replication 后 pending 数量为 0"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for i in range(3):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        replicator = Replicator(log)
        assert replicator.get_pending_count("ws1") == 3

        replicator.replicate("ws1")

        assert replicator.get_pending_count("ws1") == 0


# ============================================
# TestReplicatorEndToEnd —— 端到端测试
# ============================================

class TestReplicatorEndToEnd:
    """端到端测试"""

    def test_full_lifecycle(self, tmp_path):
        """完整生命周期"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # 1. 写入 entries
        for i in range(3):
            entry = create_staging_entry(
                workspace_id="ws1",
                file_path=f"file_{i}.py",
                content_hash=f"hash_{i}",
                language="python",
                parse_delta={"symbol_delta": {"added": [f"func_{i}"]}},
            )
            log.append(entry)

        # 2. Replicate
        replicator = Replicator(log)
        result = replicator.replicate("ws1")

        assert result.success
        assert result.applied_count == 3
        assert result.pending_count == 3

        # 3. 验证 log 被截断
        assert replicator.get_pending_count("ws1") == 0

        # 4. 再次 replicate（无 pending）
        result2 = replicator.replicate("ws1")
        assert result2.success
        assert result2.pending_count == 0
        assert result2.applied_count == 0

    def test_multiple_replications(self, tmp_path):
        """多次 replication"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        replicator = Replicator(log)

        # 第一批
        for i in range(2):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        result1 = replicator.replicate("ws1")
        assert result1.applied_count == 2

        # 第二批
        for i in range(3):
            entry = create_staging_entry("ws1", f"file_{i+2}.py", f"hash_{i+2}", "python")
            log.append(entry)

        result2 = replicator.replicate("ws1")
        assert result2.applied_count == 3

        # log 应只有第二批之前的残留（实际上都被截断了）
        assert replicator.get_pending_count("ws1") == 0

    def test_recover_after_crash(self, tmp_path):
        """crash 后恢复"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # 写入 entries，不处理（模拟 crash）
        for i in range(5):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        # 模拟重启
        log2 = StagingLog(log_path)
        replicator = Replicator(log2)

        # 恢复
        result = replicator.recover("ws1")
        assert result.success
        assert result.applied_count == 5

        # 验证所有 entries 已 applied
        assert replicator.get_pending_count("ws1") == 0
