"""Phase 5 Step 0 单元测试：Rust notify crate 监听 workspace roots

测试范围：
- PyFileWatcher 创建和配置
- start / stop 生命周期
- poll_events 拉取文件变更事件
- 扩展名过滤
- 默认扩展名列表
"""

import os
import time
import tempfile
from pathlib import Path

import pytest

# 跳过条件：callwarden_core 未安装时跳过
callwarden_core = pytest.importorskip("callwarden_core")


class TestPyFileWatcherBasic:
    def test_create_watcher(self, tmp_path):
        """创建 watcher 实例。"""
        from callwarden_core import PyFileWatcher
        watcher = PyFileWatcher(str(tmp_path))
        assert watcher.root() == str(tmp_path)
        assert watcher.is_running() is False
        watcher.stop()

    def test_create_with_custom_extensions(self, tmp_path):
        """使用自定义扩展名创建。"""
        from callwarden_core import PyFileWatcher
        watcher = PyFileWatcher(str(tmp_path), ["py", "rs"])
        exts = watcher.extensions()
        assert "py" in exts
        assert "rs" in exts
        assert len(exts) == 2

    def test_default_extensions(self, tmp_path):
        """默认扩展名应包含常见语言。"""
        from callwarden_core import PyFileWatcher
        watcher = PyFileWatcher(str(tmp_path))
        exts = watcher.extensions()
        # 至少包含 rs/py/ts/js/go
        for expected in ["rs", "py", "ts", "js", "go"]:
            assert expected in exts, f"missing extension: {expected}"


class TestPyFileWatcherLifecycle:
    def test_start_and_stop(self, tmp_path):
        """start 后 is_running=True，stop 后 False。"""
        from callwarden_core import PyFileWatcher
        watcher = PyFileWatcher(str(tmp_path))
        watcher.start()
        assert watcher.is_running() is True
        watcher.stop()
        assert watcher.is_running() is False

    def test_start_idempotent(self, tmp_path):
        """重复 start 不报错。"""
        from callwarden_core import PyFileWatcher
        watcher = PyFileWatcher(str(tmp_path))
        watcher.start()
        watcher.start()  # 不应报错
        assert watcher.is_running() is True
        watcher.stop()

    def test_stop_idempotent(self, tmp_path):
        """未 start 直接 stop 不报错。"""
        from callwarden_core import PyFileWatcher
        watcher = PyFileWatcher(str(tmp_path))
        watcher.stop()  # 不应报错
        assert watcher.is_running() is False


class TestPyFileWatcherEvents:
    def test_poll_empty_events(self, tmp_path):
        """无变更时 poll_events 返回空列表。"""
        from callwarden_core import PyFileWatcher
        watcher = PyFileWatcher(str(tmp_path))
        watcher.start()
        events = watcher.poll_events()
        assert events == []
        watcher.stop()

    def test_detect_file_creation(self, tmp_path):
        """检测到新文件创建事件。"""
        from callwarden_core import PyFileWatcher
        watcher = PyFileWatcher(str(tmp_path), ["py"])
        watcher.start()
        # 等待 watcher 初始化
        time.sleep(0.3)
        # 创建文件
        test_file = tmp_path / "test_file.py"
        test_file.write_text("# test")
        # 等待事件到达
        time.sleep(0.5)
        events = watcher.poll_events()
        assert len(events) > 0
        # 至少有一个 created 事件
        kinds = [e["kind"] for e in events]
        assert "created" in kinds or "modified" in kinds
        # 路径包含 test_file.py
        paths = [e["path"] for e in events]
        assert any("test_file.py" in p for p in paths)
        watcher.stop()

    def test_extension_filter(self, tmp_path):
        """不支持扩展名的文件不产生事件。"""
        from callwarden_core import PyFileWatcher
        watcher = PyFileWatcher(str(tmp_path), ["py"])
        watcher.start()
        time.sleep(0.3)
        # 创建 .txt 文件（不在支持列表中）
        (tmp_path / "test.txt").write_text("test")
        time.sleep(0.5)
        events = watcher.poll_events()
        # 不应有 txt 文件的事件
        txt_events = [e for e in events if "test.txt" in e["path"]]
        assert len(txt_events) == 0
        watcher.stop()

    def test_detect_file_modification(self, tmp_path):
        """检测到文件修改事件。"""
        from callwarden_core import PyFileWatcher
        # 先创建文件
        test_file = tmp_path / "mod_test.py"
        test_file.write_text("# original")
        watcher = PyFileWatcher(str(tmp_path), ["py"])
        watcher.start()
        time.sleep(0.3)
        # 修改文件
        test_file.write_text("# modified")
        time.sleep(0.5)
        events = watcher.poll_events()
        assert len(events) > 0
        paths = [e["path"] for e in events]
        assert any("mod_test.py" in p for p in paths)
        watcher.stop()

    def test_event_has_timestamp(self, tmp_path):
        """事件包含 timestamp_ms 字段。"""
        from callwarden_core import PyFileWatcher
        watcher = PyFileWatcher(str(tmp_path), ["py"])
        watcher.start()
        time.sleep(0.3)
        (tmp_path / "ts_test.py").write_text("# test")
        time.sleep(0.5)
        events = watcher.poll_events()
        if events:
            assert "timestamp_ms" in events[0]
            assert events[0]["timestamp_ms"] > 0
        watcher.stop()


class TestPyFileWatcherNestedDirs:
    def test_recursive_watch(self, tmp_path):
        """递归监听子目录中的文件变更。"""
        from callwarden_core import PyFileWatcher
        # 创建子目录
        sub_dir = tmp_path / "subdir"
        sub_dir.mkdir()
        watcher = PyFileWatcher(str(tmp_path), ["py"])
        watcher.start()
        time.sleep(0.3)
        # 在子目录创建文件
        (sub_dir / "nested.py").write_text("# nested")
        time.sleep(0.5)
        events = watcher.poll_events()
        paths = [e["path"] for e in events]
        assert any("nested.py" in p for p in paths)
        watcher.stop()
