"""批次4 Rust 扩展接线验证测试（M4/M5/M6/M8/L7）。

验证 _feature_matrix.md 中批次4 条目的 Rust 端接线（workspace.rs committed 路径）：

- M4 parse delta：workspace.rs handle_workspace_file_refresh committed 路径调用
  DeltaComputer::compute_parse_delta，结果写入 StagingEntry.parse_delta
- M5 frontier：FrontierComputer::compute_frontier_with_budget（store=None 退化 +
  QueryBudget::default）写入 entry.frontier
- M6 metrics：MetricsComputer::compute_local_update（impact_depth=2）写入 entry.metrics_update
- M8 watcher：rust_ext/src/watcher.rs FileWatcher（notify crate）+ lib.rs PyO3 导出
  PyFileWatcher / PyDebouncedFileWatcher
- L7 RSS：server/shared_benefit_metrics.py 存在（CASMetrics/ParseMetrics/SnapshotMetrics）

测试策略：源码字符串匹配（不依赖 cargo build，Windows 也能跑）。
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUST_EXT = os.path.join(ROOT, "rust_ext", "src")
DAEMON_DIR = os.path.join(RUST_EXT, "daemon")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestM4ParseDeltaWiring:
    """M4: parse delta 接入 workspace.rs committed 路径。"""

    def test_m4_workspace_calls_compute_parse_delta(self):
        src = _read(os.path.join(DAEMON_DIR, "workspace.rs"))
        assert "DeltaComputer::compute_parse_delta" in src, (
            "M4: workspace.rs 未调用 DeltaComputer::compute_parse_delta"
        )

    def test_m4_parse_delta_written_to_entry(self):
        src = _read(os.path.join(DAEMON_DIR, "workspace.rs"))
        assert "entry.parse_delta = pd;" in src, (
            "M4: 未将 parse_delta 结果写入 entry.parse_delta"
        )

    def test_m4_delta_module_registered(self):
        src = _read(os.path.join(RUST_EXT, "lib.rs"))
        assert "mod delta;" in src, "M4: lib.rs 未注册 delta 模块"


class TestM5FrontierWiring:
    """M5: frontier 接入 workspace.rs committed 路径。"""

    def test_m5_workspace_calls_compute_frontier_with_budget(self):
        src = _read(os.path.join(DAEMON_DIR, "workspace.rs"))
        assert "FrontierComputer::compute_frontier_with_budget" in src, (
            "M5: workspace.rs 未调用 FrontierComputer::compute_frontier_with_budget"
        )

    def test_m5_frontier_written_to_entry(self):
        src = _read(os.path.join(DAEMON_DIR, "workspace.rs"))
        assert "entry.frontier = fd;" in src, (
            "M5: 未将 frontier 结果写入 entry.frontier"
        )

    def test_m5_frontier_module_registered(self):
        src = _read(os.path.join(RUST_EXT, "lib.rs"))
        assert "mod frontier;" in src, "M5: lib.rs 未注册 frontier 模块"
        assert "frontier::compute_frontier_with_budget" in src, (
            "M5: lib.rs 未导出 frontier::compute_frontier_with_budget"
        )


class TestM6MetricsWiring:
    """M6: metrics update 接入 workspace.rs committed 路径。"""

    def test_m6_workspace_calls_compute_local_update(self):
        src = _read(os.path.join(DAEMON_DIR, "workspace.rs"))
        assert "MetricsComputer::compute_local_update" in src, (
            "M6: workspace.rs 未调用 MetricsComputer::compute_local_update"
        )

    def test_m6_metrics_written_to_entry(self):
        src = _read(os.path.join(DAEMON_DIR, "workspace.rs"))
        assert "entry.metrics_update = md;" in src, (
            "M6: 未将 metrics 结果写入 entry.metrics_update"
        )

    def test_m6_metrics_module_registered(self):
        src = _read(os.path.join(RUST_EXT, "lib.rs"))
        assert "mod metrics;" in src, "M6: lib.rs 未注册 metrics 模块"
        assert "metrics::compute_local_update" in src, (
            "M6: lib.rs 未导出 metrics::compute_local_update"
        )


class TestM8WatcherWiring:
    """M8: 文件监听 watcher.rs 模块 + PyO3 导出。"""

    def test_m8_watcher_module_exists(self):
        path = os.path.join(RUST_EXT, "watcher.rs")
        assert os.path.exists(path), "M8: rust_ext/src/watcher.rs 不存在"

    def test_m8_watcher_module_registered(self):
        src = _read(os.path.join(RUST_EXT, "lib.rs"))
        assert "pub mod watcher;" in src, "M8: lib.rs 未注册 watcher 模块"
        assert "watcher::PyFileWatcher" in src, (
            "M8: lib.rs 未导出 PyFileWatcher"
        )
        assert "watcher::PyDebouncedFileWatcher" in src, (
            "M8: lib.rs 未导出 PyDebouncedFileWatcher"
        )

    def test_m8_watcher_uses_notify(self):
        src = _read(os.path.join(RUST_EXT, "watcher.rs"))
        assert "use notify::" in src, "M8: watcher.rs 未依赖 notify crate"


class TestL7SharedMetrics:
    """L7: server 侧共享指标模块存在。"""

    def test_l7_shared_benefit_metrics_exists(self):
        path = os.path.join(ROOT, "server", "shared_benefit_metrics.py")
        assert os.path.exists(path), "L7: server/shared_benefit_metrics.py 不存在"

    def test_l7_has_metrics_classes(self):
        src = _read(os.path.join(ROOT, "server", "shared_benefit_metrics.py"))
        assert "CASMetrics" in src, "L7: 缺少 CASMetrics"
        assert "ParseMetrics" in src, "L7: 缺少 ParseMetrics"
        assert "SnapshotMetrics" in src, "L7: 缺少 SnapshotMetrics"
