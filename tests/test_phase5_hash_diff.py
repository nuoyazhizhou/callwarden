"""Phase 5.2 单元测试：Changed File Hash Diff

测试覆盖：
- HashDiffStore 创建与配置
- compute_file_hash：SHA-256 计算
- diff_events：
  - Created 事件 → Added（首次见到文件）
  - Modified 事件，内容未变 → 忽略（假阳性过滤）
  - Modified 事件，内容改变 → Modified
  - Removed 事件 → Removed
  - 多文件批量 diff
- register_hash / get_hash / tracked_count / clear / snapshot
- 与 DebouncedFileWatcher 集成测试
"""
import os
import time
import pytest

import sys
_pyinstall = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rust_ext", "target", "pyinstall"
)
if os.path.isdir(_pyinstall):
    sys.path.insert(0, _pyinstall)

try:
    from callwarden_core import PyHashDiffStore, PyDebouncedFileWatcher
    HAS_RUST = True
except ImportError:
    HAS_RUST = False

pytestmark = pytest.mark.skipif(not HAS_RUST, reason="callwarden_core Rust 扩展未构建")


class TestHashDiffStoreBasic:
    """基础创建与配置测试"""

    def test_create_empty(self):
        """创建空的 hash diff store"""
        s = PyHashDiffStore()
        assert s.tracked_count() == 0
        assert repr(s) == "PyHashDiffStore(tracked=0)"

    def test_register_and_get_hash(self):
        """注册并获取 hash"""
        s = PyHashDiffStore()
        s.register_hash("/test/file.py", "abc123")
        assert s.get_hash("/test/file.py") == "abc123"
        assert s.tracked_count() == 1

    def test_get_hash_not_found(self):
        """获取未注册的 hash 返回 None"""
        s = PyHashDiffStore()
        assert s.get_hash("/nonexistent.py") is None

    def test_clear(self):
        """清空所有 hash"""
        s = PyHashDiffStore()
        s.register_hash("/a.py", "h1")
        s.register_hash("/b.py", "h2")
        assert s.tracked_count() == 2
        s.clear()
        assert s.tracked_count() == 0

    def test_snapshot(self):
        """snapshot 返回所有 tracked 文件"""
        s = PyHashDiffStore()
        s.register_hash("/a.py", "hash_a")
        s.register_hash("/b.py", "hash_b")
        snap = s.snapshot()
        assert len(snap) == 2
        snap_dict = dict(snap)
        assert snap_dict["/a.py"] == "hash_a"
        assert snap_dict["/b.py"] == "hash_b"


class TestDiffEventsAdded:
    """新增文件检测"""

    def test_created_event_new_file(self, tmp_path):
        """Created 事件 + 文件不存在于 store → Added"""
        f = tmp_path / "new.py"
        f.write_text("x = 1\n")

        s = PyHashDiffStore()
        events = [("created", str(f), int(time.time() * 1000))]
        changes = s.diff_events(events)

        assert len(changes) == 1
        assert changes[0]["kind"] == "added"
        assert changes[0]["content_hash"] is not None
        assert changes[0]["previous_hash"] is None
        assert s.tracked_count() == 1

    def test_modified_event_first_time(self, tmp_path):
        """Modified 事件 + 首次见到 → Added"""
        f = tmp_path / "first.py"
        f.write_text("y = 2\n")

        s = PyHashDiffStore()
        events = [("modified", str(f), int(time.time() * 1000))]
        changes = s.diff_events(events)

        assert len(changes) == 1
        assert changes[0]["kind"] == "added"


class TestDiffEventsUnchanged:
    """假阳性过滤测试"""

    def test_modified_unchanged_dropped(self, tmp_path):
        """Modified 事件 + 内容未变 → 忽略"""
        f = tmp_path / "same.py"
        f.write_text("content stays same\n")

        s = PyHashDiffStore()
        # 第一次注册
        events1 = [("created", str(f), 1000)]
        changes1 = s.diff_events(events1)
        assert len(changes1) == 1

        # 第二次 Modified，内容相同
        events2 = [("modified", str(f), 2000)]
        changes2 = s.diff_events(events2)
        assert len(changes2) == 0  # 假阳性被过滤

    def test_multiple_unchanged_dropped(self, tmp_path):
        """多次 Modified 内容不变 → 始终忽略"""
        f = tmp_path / "stable.py"
        f.write_text("stable content\n")

        s = PyHashDiffStore()
        s.diff_events([("created", str(f), 1000)])

        # 连续 3 次 Modified，内容不变
        for i in range(3):
            changes = s.diff_events([("modified", str(f), 2000 + i)])
            assert len(changes) == 0


class TestDiffEventsModified:
    """内容修改检测"""

    def test_modified_content_changed(self, tmp_path):
        """Modified 事件 + 内容改变 → Modified"""
        f = tmp_path / "change.py"
        f.write_text("original\n")

        s = PyHashDiffStore()
        s.diff_events([("created", str(f), 1000)])

        # 修改内容
        f.write_text("modified\n")
        changes = s.diff_events([("modified", str(f), 2000)])

        assert len(changes) == 1
        assert changes[0]["kind"] == "modified"
        assert changes[0]["content_hash"] is not None
        assert changes[0]["previous_hash"] is not None
        assert changes[0]["content_hash"] != changes[0]["previous_hash"]


class TestDiffEventsRemoved:
    """文件删除检测"""

    def test_removed_event(self, tmp_path):
        """Removed 事件 + 之前有记录 → Removed"""
        f = tmp_path / "del.py"
        f.write_text("to be deleted\n")

        s = PyHashDiffStore()
        s.diff_events([("created", str(f), 1000)])

        os.remove(str(f))
        changes = s.diff_events([("removed", str(f), 2000)])

        assert len(changes) == 1
        assert changes[0]["kind"] == "removed"
        assert changes[0]["content_hash"] is None
        assert changes[0]["previous_hash"] is not None
        assert s.tracked_count() == 0

    def test_removed_without_record(self, tmp_path):
        """Removed 事件 + 之前无记录 → 忽略"""
        s = PyHashDiffStore()
        changes = s.diff_events([("removed", str(tmp_path / "never.py"), 1000)])
        assert len(changes) == 0


class TestDiffEventsBatch:
    """批量 diff 测试"""

    def test_multiple_files_batch(self, tmp_path):
        """多文件批量 diff"""
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("a = 1\n")
        f2.write_text("b = 2\n")

        s = PyHashDiffStore()
        events = [
            ("created", str(f1), 1000),
            ("created", str(f2), 1000),
        ]
        changes = s.diff_events(events)
        assert len(changes) == 2
        assert s.tracked_count() == 2

        # 修改 f1
        f1.write_text("a = 10\n")
        changes2 = s.diff_events([("modified", str(f1), 2000)])
        assert len(changes2) == 1
        assert changes2[0]["kind"] == "modified"

    def test_mixed_events_batch(self, tmp_path):
        """混合事件类型批量处理"""
        f_add = tmp_path / "add.py"
        f_mod = tmp_path / "mod.py"
        f_del = tmp_path / "del.py"

        f_add.write_text("new\n")
        f_mod.write_text("old\n")
        f_del.write_text("to delete\n")

        s = PyHashDiffStore()
        # 先注册 mod 和 del
        s.diff_events([
            ("created", str(f_mod), 1000),
            ("created", str(f_del), 1000),
        ])

        # 批量处理：add + mod（改变内容）+ del（删除文件）
        f_mod.write_text("new content\n")
        os.remove(str(f_del))

        changes = s.diff_events([
            ("created", str(f_add), 2000),
            ("modified", str(f_mod), 2000),
            ("removed", str(f_del), 2000),
        ])

        kinds = sorted(c["kind"] for c in changes)
        assert "added" in kinds
        assert "modified" in kinds
        assert "removed" in kinds


class TestHashDiffIntegration:
    """与 DebouncedFileWatcher 集成测试"""

    def test_watcher_to_hash_diff(self, tmp_path):
        """完整管道：watcher 事件 → hash diff → 过滤假阳性"""
        f = tmp_path / "integration.py"
        f.write_text("x = 1\n")

        watcher = PyDebouncedFileWatcher(str(tmp_path), debounce_ms=100)
        store = PyHashDiffStore()
        watcher.start()
        try:
            time.sleep(0.3)
            events = watcher.flush()

            # 转换为 hash diff 需要的格式
            event_tuples = [(e["kind"], e["path"], e["timestamp_ms"]) for e in events]
            if event_tuples:
                changes = store.diff_events(event_tuples)
                # 应该至少检测到 integration.py 的变更
                paths = [c["path"] for c in changes]
                assert any("integration.py" in p for p in paths)
        finally:
            watcher.stop()

    def test_watcher_false_positive_filtered(self, tmp_path):
        """touch 不改内容 → hash diff 过滤假阳性"""
        import hashlib
        f = tmp_path / "touch.py"
        f.write_text("content\n")

        # 手动注册文件 hash（模拟已索引状态）
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        store = PyHashDiffStore()
        store.register_hash(str(f), h)

        watcher = PyDebouncedFileWatcher(str(tmp_path), debounce_ms=100)
        watcher.start()
        try:
            # touch（不改内容）
            os.utime(str(f), None)
            time.sleep(0.3)
            events = watcher.flush()
            event_tuples = [(e["kind"], e["path"], e["timestamp_ms"]) for e in events]

            # hash diff 应过滤掉假阳性（内容未变）
            if event_tuples:
                changes = store.diff_events(event_tuples)
                touch_changes = [c for c in changes if "touch.py" in c["path"]]
                # 内容未变，不应出现在 changes 中
                assert len(touch_changes) == 0
        finally:
            watcher.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
