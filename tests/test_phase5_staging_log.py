"""
Phase 5.6: Staging Durable Log 测试

测试 StagingLog 的 append/read/truncate/mark_applied/mark_failed 功能，
以及 crash recovery（LSN 恢复）。
"""

import os
import json
import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from server.staging_log import StagingLog, StagingEntry, create_staging_entry


# ============================================
# TestStagingLogBasic —— 基础功能
# ============================================

class TestStagingLogBasic:
    """基础功能测试"""

    def test_create_log(self, tmp_path):
        """创建 log 文件"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)
        assert os.path.exists(log_path) is False  # 未 append 时不创建
        assert log._next_lsn == 1

    def test_append_and_read(self, tmp_path):
        """追加并读取 entry"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        entry = create_staging_entry(
            workspace_id="ws1",
            file_path="src/main.py",
            content_hash="abc123",
            language="python",
            parse_delta={"added": ["func_a"]},
        )
        lsn = log.append(entry)

        assert lsn == 1
        assert entry.lsn == 1
        assert entry.timestamp > 0

        entries = log.read()
        assert len(entries) == 1
        assert entries[0].lsn == 1
        assert entries[0].file_path == "src/main.py"
        assert entries[0].content_hash == "abc123"
        assert entries[0].language == "python"
        assert entries[0].parse_delta["added"] == ["func_a"]
        assert entries[0].status == "pending"

    def test_append_multiple(self, tmp_path):
        """追加多条 entry"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for i in range(5):
            entry = create_staging_entry(
                workspace_id="ws1",
                file_path=f"file_{i}.py",
                content_hash=f"hash_{i}",
                language="python",
            )
            lsn = log.append(entry)
            assert lsn == i + 1

        entries = log.read()
        assert len(entries) == 5
        for i, e in enumerate(entries):
            assert e.lsn == i + 1
            assert e.file_path == f"file_{i}.py"

    def test_read_since_lsn(self, tmp_path):
        """从指定 LSN 开始读取"""
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

        # 从 LSN=3 开始读取（不包含 3）
        entries = log.read(since_lsn=3)
        assert len(entries) == 2
        assert entries[0].lsn == 4
        assert entries[1].lsn == 5

    def test_repr(self, tmp_path):
        """__repr__"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)
        repr_str = repr(log)
        assert "StagingLog" in repr_str
        assert "next_lsn" in repr_str


# ============================================
# TestStagingLogStatus —— 状态管理
# ============================================

class TestStagingLogStatus:
    """状态管理测试"""

    def test_mark_applied(self, tmp_path):
        """标记 applied"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        entry = create_staging_entry("ws1", "file.py", "hash", "python")
        lsn = log.append(entry)

        log.mark_applied(lsn)

        entries = log.read()
        assert entries[0].status == "applied"

    def test_mark_failed(self, tmp_path):
        """标记 failed"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        entry = create_staging_entry("ws1", "file.py", "hash", "python")
        lsn = log.append(entry)

        log.mark_failed(lsn, "parse error")

        entries = log.read()
        assert entries[0].status == "failed"
        assert entries[0].error == "parse error"

    def test_read_pending(self, tmp_path):
        """读取 pending entries"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for i in range(3):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        # 标记第二条为 applied
        log.mark_applied(2)

        pending = log.read_pending()
        assert len(pending) == 2
        assert pending[0].lsn == 1
        assert pending[1].lsn == 3

    def test_stats(self, tmp_path):
        """统计信息"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for i in range(4):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        log.mark_applied(1)
        log.mark_applied(2)
        log.mark_failed(3, "error")

        stats = log.stats()
        assert stats["total_entries"] == 4
        assert stats["applied"] == 2
        assert stats["failed"] == 1
        assert stats["pending"] == 1
        assert stats["next_lsn"] == 5


# ============================================
# TestStagingLogTruncate —— 截断
# ============================================

class TestStagingLogTruncate:
    """截断测试"""

    def test_truncate_basic(self, tmp_path):
        """基础截断"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for i in range(5):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        # 截断到 LSN=3（包含）
        log.truncate(3)

        entries = log.read()
        assert len(entries) == 2
        assert entries[0].lsn == 4
        assert entries[1].lsn == 5

    def test_truncate_all(self, tmp_path):
        """截断全部"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for i in range(3):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        log.truncate(3)

        entries = log.read()
        assert len(entries) == 0

    def test_truncate_none(self, tmp_path):
        """截断不存在的 LSN（无影响）"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for i in range(3):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        log.truncate(0)  # 不截断任何 entry

        entries = log.read()
        assert len(entries) == 3

    def test_compact_applied_all(self, tmp_path):
        """compact_applied 删除所有 applied entries"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for i in range(4):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        log.mark_applied(1)
        log.mark_applied(3)

        log.compact_applied()

        entries = log.read()
        assert len(entries) == 2
        assert entries[0].lsn == 2
        assert entries[1].lsn == 4

    def test_compact_applied_by_workspace(self, tmp_path):
        """compact_applied 按 workspace 过滤"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # ws1: LSN 1, 2, 3
        for i in range(3):
            entry = create_staging_entry("ws1", f"ws1_{i}.py", f"h1_{i}", "python")
            log.append(entry)
        # ws2: LSN 4, 5
        for i in range(2):
            entry = create_staging_entry("ws2", f"ws2_{i}.py", f"h2_{i}", "python")
            log.append(entry)

        # 标记 ws1 的 LSN 1, 2 和 ws2 的 LSN 4 为 applied
        log.mark_applied(1)
        log.mark_applied(2)
        log.mark_applied(4)

        # 只 compact ws1 的 applied
        log.compact_applied("ws1")

        entries = log.read()
        # ws1: 只剩 LSN 3（1, 2 被删除）
        # ws2: LSN 4 (applied), 5 (pending) 都保留
        assert len(entries) == 3
        lsns = [e.lsn for e in entries]
        assert 3 in lsns
        assert 4 in lsns  # ws2 的 applied 保留
        assert 5 in lsns


# ============================================
# TestStagingLogRecovery —— 崩溃恢复
# ============================================

class TestStagingLogRecovery:
    """崩溃恢复测试"""

    def test_lsn_recovery(self, tmp_path):
        """重启后 LSN 恢复"""
        log_path = str(tmp_path / "staging.log")
        log1 = StagingLog(log_path)

        for i in range(3):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log1.append(entry)

        assert log1._next_lsn == 4

        # 模拟重启：创建新的 StagingLog 实例
        log2 = StagingLog(log_path)
        assert log2._next_lsn == 4

        # 新追加的 entry 应从 LSN=4 开始
        entry = create_staging_entry("ws1", "new.py", "new_hash", "python")
        lsn = log2.append(entry)
        assert lsn == 4

    def test_corrupted_line_recovery(self, tmp_path):
        """损坏行恢复"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # 写入正常 entry
        entry = create_staging_entry("ws1", "file.py", "hash", "python")
        log.append(entry)

        # 手动写入损坏的行
        with open(log_path, "a", encoding="utf-8") as f:
            f.write('{"invalid": json}\n')  # 不是有效的 JSON
            f.write('{"lsn": 2, "timestamp": 0, "workspace_id": "ws1", "file_path": "ok.py", "content_hash": "h2", "language": "python"}\n')

        # 重新打开 log，应跳过损坏行
        log2 = StagingLog(log_path)
        entries = log2.read()
        # 正常 entry + 1 条有效的手动写入 = 2
        assert len(entries) == 2
        assert entries[0].lsn == 1
        assert entries[1].lsn == 2

    def test_empty_log_recovery(self, tmp_path):
        """空 log 恢复"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)
        assert log._next_lsn == 1

        # 重新打开
        log2 = StagingLog(log_path)
        assert log2._next_lsn == 1


# ============================================
# TestStagingEntry —— StagingEntry 数据结构
# ============================================

class TestStagingEntry:
    """StagingEntry 测试"""

    def test_create_entry(self):
        """创建 entry"""
        entry = StagingEntry(
            lsn=1,
            timestamp=1234567890.0,
            workspace_id="ws1",
            file_path="test.py",
            content_hash="abc123",
            language="python",
        )
        assert entry.lsn == 1
        assert entry.status == "pending"
        assert entry.error is None

    def test_entry_serialization(self):
        """entry 序列化/反序列化"""
        entry = StagingEntry(
            lsn=1,
            timestamp=1234567890.0,
            workspace_id="ws1",
            file_path="test.py",
            content_hash="abc123",
            language="python",
            parse_delta={"added": ["func_a"]},
            resolve_delta={"added": [{"caller": "a", "callee": "b"}]},
            frontier={"directly_affected": ["func_a"]},
            metrics_update={"depth_changes": []},
        )

        # 序列化为 JSON line
        line = entry.to_json_line()
        assert isinstance(line, str)

        # 反序列化
        entry2 = StagingEntry.from_json_line(line)
        assert entry2.lsn == 1
        assert entry2.workspace_id == "ws1"
        assert entry2.file_path == "test.py"
        assert entry2.parse_delta["added"] == ["func_a"]
        assert entry2.resolve_delta["added"][0]["caller"] == "a"
        assert entry2.frontier["directly_affected"] == ["func_a"]

    def test_entry_summary(self):
        """entry summary"""
        entry = StagingEntry(
            lsn=1,
            timestamp=1234567890.0,
            workspace_id="ws1",
            file_path="test.py",
            content_hash="abc123",
            language="python",
        )
        summary = entry.summary()
        assert "StagingEntry" in summary
        assert "lsn=1" in summary
        assert "test.py" in summary

    def test_create_staging_entry_helper(self):
        """create_staging_entry 辅助函数"""
        entry = create_staging_entry(
            workspace_id="ws1",
            file_path="test.py",
            content_hash="hash",
            language="python",
        )
        assert entry.lsn == 0  # 未 append
        assert entry.status == "pending"
        assert entry.parse_delta == {}
        assert entry.resolve_delta == {}


# ============================================
# TestStagingLogEndToEnd —— 端到端测试
# ============================================

class TestStagingLogEndToEnd:
    """端到端测试"""

    def test_full_lifecycle(self, tmp_path):
        """完整生命周期：append → read → mark_applied → truncate"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # 1. 追加 3 条 entry
        lsns = []
        for i in range(3):
            entry = create_staging_entry(
                workspace_id="ws1",
                file_path=f"file_{i}.py",
                content_hash=f"hash_{i}",
                language="python",
                parse_delta={"added": [f"func_{i}"]},
            )
            lsns.append(log.append(entry))

        # 2. 读取所有 pending
        pending = log.read_pending()
        assert len(pending) == 3

        # 3. 标记前两条为 applied
        log.mark_applied(lsns[0])
        log.mark_applied(lsns[1])

        # 4. 读取 pending（只剩 1 条）
        pending = log.read_pending()
        assert len(pending) == 1
        assert pending[0].lsn == lsns[2]

        # 5. 截断已应用的 entries
        log.truncate(lsns[1])

        # 6. 验证截断后的状态
        entries = log.read()
        assert len(entries) == 1
        assert entries[0].lsn == lsns[2]

    def test_multiple_workspaces(self, tmp_path):
        """多 workspace 场景"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        for ws in ["ws1", "ws2", "ws3"]:
            entry = create_staging_entry(
                workspace_id=ws,
                file_path=f"{ws}/main.py",
                content_hash=f"hash_{ws}",
                language="python",
            )
            log.append(entry)

        entries = log.read()
        assert len(entries) == 3
        workspaces = [e.workspace_id for e in entries]
        assert "ws1" in workspaces
        assert "ws2" in workspaces
        assert "ws3" in workspaces

    def test_crash_recovery_scenario(self, tmp_path):
        """crash 恢复场景"""
        log_path = str(tmp_path / "staging.log")
        log = StagingLog(log_path)

        # 写入 3 条 entry
        for i in range(3):
            entry = create_staging_entry("ws1", f"file_{i}.py", f"hash_{i}", "python")
            log.append(entry)

        # 标记第 2 条为 applied（模拟 Replicator 已应用）
        log.mark_applied(2)

        # 模拟 crash + 重启
        log2 = StagingLog(log_path)

        # 恢复后应有 3 条 entry，第 2 条状态为 applied
        entries = log2.read()
        assert len(entries) == 3
        assert entries[1].status == "applied"
        assert entries[0].status == "pending"
        assert entries[2].status == "pending"

        # 新 entry 应从 LSN=4 开始
        entry = create_staging_entry("ws1", "new.py", "new_hash", "python")
        lsn = log2.append(entry)
        assert lsn == 4
