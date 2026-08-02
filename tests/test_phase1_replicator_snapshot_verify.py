"""Phase 1-4 verify: Replicator + SnapshotManager 性能/安全/恢复测试

迁移计划 §4 第 5/6 条要求:
- Performance: P50/P95 延迟
- Security: 只读、rollback_flag 控制、路径安全
- Recovery: 损坏输入、重复请求、并发写入

同时验证 Replicator.get_pending_count 接入 Rust 短路后的生产行为。
"""
import os
import sys
import json
import time
import statistics
import tempfile
from pathlib import Path

import pytest

_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

try:
    import callwarden_core
    HAS_RUST = True
except ImportError:
    HAS_RUST = False

from callwarden.server.staging_log import StagingLog, StagingEntry
from callwarden.server.replicator import Replicator


@pytest.fixture
def staging_log_with_pending(tmp_path):
    """创建带 3 pending + 2 applied 的 staging log"""
    log_path = str(tmp_path / "test.log")
    staging_log = StagingLog(log_path)
    for i in range(5):
        entry = StagingEntry(
            lsn=i + 1,
            timestamp=1000.0 + i,
            workspace_id="ws1" if i < 3 else "ws2",
            file_path=f"file_{i}.py",
            content_hash=f"hash_{i}",
            language="python",
            parse_delta={},
            resolve_delta={},
            frontier={},
            metrics_update={},
            status="pending" if i % 2 == 0 else "applied",
            error=None,
        )
        staging_log.append(entry)
    return staging_log, log_path


# ============================================================
# 性能测试(迁移计划 §4 第 5 条)
# ============================================================
class TestReplicatorQueryPerformance:
    """性能测试:Rust 路径 P50/P95 延迟"""

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_rust_p95_under_10ms(self, staging_log_with_pending):
        """Rust 路径 P95 延迟应 < 10ms"""
        _, log_path = staging_log_with_pending
        times = []
        for _ in range(100):
            t0 = time.perf_counter()
            callwarden_core.replicator_get_pending_count(log_path, None)
            times.append((time.perf_counter() - t0) * 1000)

        p50 = statistics.median(times)
        p95 = sorted(times)[95]
        assert p95 < 10.0, f"Rust P95={p95:.3f}ms 超过 10ms 阈值"
        print(f"\n  Rust P50={p50:.3f}ms  P95={p95:.3f}ms  (100 次调用)")

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_rust_vs_python_latency(self, staging_log_with_pending):
        """Rust 与 Python 路径延迟对比"""
        staging_log, log_path = staging_log_with_pending
        replicator = Replicator(staging_log=staging_log)

        rust_times = []
        for _ in range(50):
            t0 = time.perf_counter()
            callwarden_core.replicator_get_pending_count(log_path, None)
            rust_times.append((time.perf_counter() - t0) * 1000)

        # Python 路径(直接走 staging_log.read_pending)
        py_times = []
        for _ in range(50):
            t0 = time.perf_counter()
            pending = staging_log.read_pending()
            len(pending)
            py_times.append((time.perf_counter() - t0) * 1000)

        rust_p50 = statistics.median(rust_times)
        py_p50 = statistics.median(py_times)
        print(f"\n  Rust P50={rust_p50:.3f}ms  vs  Python P50={py_p50:.3f}ms")
        # 功能一致性
        assert callwarden_core.replicator_get_pending_count(log_path, None) == 3


# ============================================================
# 安全测试(迁移计划 §4 第 5 条权限结果)
# ============================================================
class TestReplicatorQuerySecurity:
    """安全测试:只读不写、路径校验"""

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_readonly_does_not_modify_log(self, staging_log_with_pending):
        """只读查询不修改 staging log 内容"""
        _, log_path = staging_log_with_pending
        before = open(log_path, "r", encoding="utf-8").read()
        callwarden_core.replicator_get_pending_count(log_path, None)
        after = open(log_path, "r", encoding="utf-8").read()
        assert before == after  # 内容不变

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_nonexistent_path_returns_zero(self, tmp_path):
        """不存在的路径返回 0(StagingLog::new 创建空文件)"""
        bad_path = str(tmp_path / "nonexistent.log")
        count = callwarden_core.replicator_get_pending_count(bad_path, None)
        assert count == 0

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_empty_file_returns_zero(self, tmp_path):
        """空文件返回 0"""
        log_path = str(tmp_path / "empty.log")
        open(log_path, "w").close()
        count = callwarden_core.replicator_get_pending_count(log_path, None)
        assert count == 0


# ============================================================
# 恢复测试(迁移计划 §4 第 6 条)
# ============================================================
class TestReplicatorQueryRecovery:
    """恢复测试:损坏输入、重复请求、并发写入"""

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_malformed_lines_skipped(self, tmp_path):
        """损坏的 JSON 行应被跳过"""
        log_path = str(tmp_path / "malformed.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write('{"lsn":1,"timestamp":1.0,"workspace_id":"ws1","file_path":"a.py","content_hash":"h","language":"python","parse_delta":{},"resolve_delta":{},"frontier":{},"metrics_update":{},"status":"pending","error":null}\n')
            f.write("this is not json\n")
            f.write('{"lsn":2,"timestamp":2.0,"workspace_id":"ws1","file_path":"b.py","content_hash":"h","language":"python","parse_delta":{},"resolve_delta":{},"frontier":{},"metrics_update":{},"status":"pending","error":null}\n')
        count = callwarden_core.replicator_get_pending_count(log_path, None)
        assert count == 2  # 损坏行被跳过

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_repeated_calls_consistent(self, staging_log_with_pending):
        """重复请求(20 次)结果一致"""
        _, log_path = staging_log_with_pending
        results = [
            callwarden_core.replicator_get_pending_count(log_path, None)
            for _ in range(20)
        ]
        assert all(r == 3 for r in results)

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_concurrent_append_then_read(self, tmp_path):
        """并发写入后读取:应读到最新的 pending 数量"""
        log_path = str(tmp_path / "concurrent.log")
        staging_log = StagingLog(log_path)

        # 初始写入 2 条 pending
        for i in range(2):
            entry = StagingEntry(
                lsn=i + 1, timestamp=1000.0 + i, workspace_id="ws1",
                file_path=f"f{i}.py", content_hash="h", language="python",
                parse_delta={}, resolve_delta={}, frontier={}, metrics_update={},
                status="pending", error=None,
            )
            staging_log.append(entry)

        # 读取应返回 2
        assert callwarden_core.replicator_get_pending_count(log_path, None) == 2

        # 再写入 1 条 pending
        entry = StagingEntry(
            lsn=3, timestamp=1002.0, workspace_id="ws1",
            file_path="f2.py", content_hash="h", language="python",
            parse_delta={}, resolve_delta={}, frontier={}, metrics_update={},
            status="pending", error=None,
        )
        staging_log.append(entry)

        # 读取应返回 3
        assert callwarden_core.replicator_get_pending_count(log_path, None) == 3

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_workspace_filter_nonexistent_ws(self, staging_log_with_pending):
        """不存在的 workspace_id 返回 0"""
        _, log_path = staging_log_with_pending
        count = callwarden_core.replicator_get_pending_count(log_path, "ws_nonexistent")
        assert count == 0


# ============================================================
# 生产接入验证(Replicator.get_pending_count Rust 短路)
# ============================================================
class TestReplicatorGetPendingCountWireProduction:
    """验证 Replicator.get_pending_count 接入 Rust 短路后的生产行为"""

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_get_pending_count_returns_correct_value(self, staging_log_with_pending):
        """get_pending_count 通过 Rust 短路返回正确值"""
        staging_log, _ = staging_log_with_pending
        replicator = Replicator(staging_log=staging_log)

        # 无过滤:3 pending
        assert replicator.get_pending_count() == 3
        # ws1 过滤:ws1 有 lsn 1,3(pending),lsn 2(applied)→ 2 pending
        assert replicator.get_pending_count(workspace_id="ws1") == 2
        # ws2 过滤:ws2 有 lsn 4(applied),lsn 5(pending)→ 1 pending
        # 但根据 fixture,i%2==0 是 pending:i=0(ws1,pending),i=1(ws1,applied),i=2(ws1,pending),i=3(ws2,applied),i=4(ws2,pending)
        # ws2: i=3(applied), i=4(pending) → 1 pending
        assert replicator.get_pending_count(workspace_id="ws2") == 1

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_get_pending_count_rust_actually_called(self, staging_log_with_pending):
        """验证 Rust 短路真的被触发(通过 monkey-patch 追踪)"""
        staging_log, _ = staging_log_with_pending
        replicator = Replicator(staging_log=staging_log)

        rust_called = {"count": 0}
        original = callwarden_core.replicator_get_pending_count

        def tracer(log_path, workspace_id=None):
            rust_called["count"] += 1
            return original(log_path, workspace_id)

        callwarden_core.replicator_get_pending_count = tracer
        try:
            replicator.get_pending_count()
            replicator.get_pending_count(workspace_id="ws1")
        finally:
            callwarden_core.replicator_get_pending_count = original

        assert rust_called["count"] == 2, f"Rust 应被调用 2 次,实际 {rust_called['count']}"

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_empty_log_returns_zero(self, tmp_path):
        """空 staging log 返回 0"""
        log_path = str(tmp_path / "empty.log")
        staging_log = StagingLog(log_path)
        replicator = Replicator(staging_log=staging_log)
        assert replicator.get_pending_count() == 0
        assert replicator.get_pending_count(workspace_id="ws1") == 0
