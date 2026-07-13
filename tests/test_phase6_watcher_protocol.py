"""Phase 6 测试：Watcher 协议、事件合并调度器、Durable Staging WAL。

任务：T-1783974522648-e2d3 Steps #0-#3
"""

import json
import os
import sqlite3
import tempfile
import time

import pytest


# ============================================
# Watcher 协议测试
# ============================================


class TestWatcherProtocol:
    """协议定义与 generation 比较测试。"""

    def test_event_kind_values(self):
        from server.watcher_protocol import EventKind
        assert EventKind.MODIFY.value == "modify"
        assert EventKind.DELETE.value == "delete"
        assert EventKind.RENAME.value == "rename"

    def test_watcher_event_serialization(self):
        from server.watcher_protocol import WatcherEvent, EventKind
        event = WatcherEvent(
            workspace_instance_id="ws_1",
            agent_session_id="sess_1",
            session_epoch=5,
            monotonic_seq=10,
            rel_path="src/main.py",
            event_kind=EventKind.MODIFY,
            observed_mtime_ns=1000000,
            event_observed_mono_ns=2000000,
        )
        d = event.to_dict()
        assert d["workspace_instance_id"] == "ws_1"
        assert d["event_kind"] == "modify"
        assert d["session_epoch"] == 5
        assert d["monotonic_seq"] == 10

        # 反序列化
        event2 = WatcherEvent.from_dict(d)
        assert event2.rel_path == "src/main.py"
        assert event2.event_kind == EventKind.MODIFY
        assert event2.generation == (5, 10)

    def test_compare_generations(self):
        from server.watcher_protocol import compare_generations
        assert compare_generations("1:5", "1:5") == 0
        assert compare_generations("1:5", "1:6") == -1
        assert compare_generations("2:1", "1:100") == 1
        assert compare_generations("", "1:1") == -1
        assert compare_generations("1:1", "") == 1
        assert compare_generations("", "") == 0

    def test_is_stale_generation(self):
        from server.watcher_protocol import is_stale_generation
        assert is_stale_generation("1:5", "1:6") is True
        assert is_stale_generation("1:5", "1:5") is True
        assert is_stale_generation("1:6", "1:5") is False
        assert is_stale_generation("2:1", "1:100") is False

    def test_stage_timestamps(self):
        from server.watcher_protocol import StageTimestamps
        ts = StageTimestamps()
        ts.set_stage("0", 1000)
        ts.set_stage("1", 2000)
        ts.set_stage("5", 5000)
        durations = ts.to_durations_ms()
        assert "T0→T1 coalesce" in durations
        assert durations["T0→T1 coalesce"] == pytest.approx(0.001, abs=0.0001)

    def test_refresh_response_serialization(self):
        from server.watcher_protocol import RefreshResponse, CASResult
        resp = RefreshResponse(
            file_generation="3:7",
            snapshot_generation=42,
            cas_result=CASResult.HIT,
            coalesced_event_count=3,
            stage_durations_ms={"T0→T6 total": 1.5},
        )
        d = resp.to_dict()
        assert d["file_generation"] == "3:7"
        assert d["snapshot_generation"] == 42
        assert d["cas_result"] == "hit"
        assert d["coalesced_event_count"] == 3


# ============================================
# 事件合并测试
# ============================================


class TestEventCoalescing:
    """事件合并规则测试。"""

    def _make_event(self, kind, seq=1):
        from server.watcher_protocol import WatcherEvent, EventKind
        return WatcherEvent(
            workspace_instance_id="ws_1",
            agent_session_id="sess_1",
            session_epoch=1,
            monotonic_seq=seq,
            rel_path="test.py",
            event_kind=kind,
            canonical_bytes=b"content",
        )

    def test_modify_modify_coalesce(self):
        from server.watcher_protocol import coalesce_events, EventKind
        e1 = self._make_event(EventKind.MODIFY, seq=1)
        e2 = self._make_event(EventKind.MODIFY, seq=2)
        merged = coalesce_events(e1, e2)
        assert merged is not None
        assert merged.event_kind == EventKind.MODIFY
        assert merged.monotonic_seq == 2

    def test_create_modify_coalesce(self):
        from server.watcher_protocol import coalesce_events, EventKind
        e1 = self._make_event(EventKind.CREATE, seq=1)
        e2 = self._make_event(EventKind.MODIFY, seq=2)
        merged = coalesce_events(e1, e2)
        assert merged is not None
        assert merged.event_kind == EventKind.CREATE  # 合并为 create

    def test_delete_create_coalesce(self):
        from server.watcher_protocol import coalesce_events, EventKind
        e1 = self._make_event(EventKind.DELETE, seq=1)
        e2 = self._make_event(EventKind.CREATE, seq=2)
        merged = coalesce_events(e1, e2)
        assert merged is not None
        assert merged.event_kind == EventKind.MODIFY  # replace

    def test_modify_delete_coalesce(self):
        from server.watcher_protocol import coalesce_events, EventKind
        e1 = self._make_event(EventKind.MODIFY, seq=1)
        e2 = self._make_event(EventKind.DELETE, seq=2)
        merged = coalesce_events(e1, e2)
        assert merged is not None
        assert merged.event_kind == EventKind.DELETE

    def test_create_delete_cancel(self):
        from server.watcher_protocol import coalesce_events, EventKind
        e1 = self._make_event(EventKind.CREATE, seq=1)
        e2 = self._make_event(EventKind.DELETE, seq=2)
        merged = coalesce_events(e1, e2)
        assert merged is None  # 互相抵消


# ============================================
# Refresh 调度器测试
# ============================================


class TestRefreshScheduler:
    """事件合并调度器测试。"""

    def _make_event(self, ws_id, path, kind_str="modify", seq=1):
        from server.watcher_protocol import WatcherEvent, EventKind
        return WatcherEvent(
            workspace_instance_id=ws_id,
            agent_session_id="sess_1",
            session_epoch=1,
            monotonic_seq=seq,
            rel_path=path,
            event_kind=EventKind(kind_str),
            canonical_bytes=b"content",
        )

    def test_submit_and_coalesce(self):
        from server.refresh_scheduler import RefreshScheduler, SchedulerConfig

        results = []
        def on_batch(ws_id, events, needs_reconcile):
            results.append((ws_id, events, needs_reconcile))

        config = SchedulerConfig(batch_quiet_window_ms=50)
        scheduler = RefreshScheduler(config=config, on_batch_ready=on_batch)

        # 提交两个 modify 事件到同一路径
        e1 = self._make_event("ws_1", "test.py", "modify", seq=1)
        e2 = self._make_event("ws_1", "test.py", "modify", seq=2)
        assert scheduler.submit(e1) is True
        assert scheduler.submit(e2) is True

        # 等待 batch
        scheduler.force_flush("ws_1")
        assert len(results) == 1
        ws_id, events, needs_reconcile = results[0]
        assert ws_id == "ws_1"
        assert len(events) == 1  # 合并为 1 个事件
        assert events[0][1] == 2  # coalesced_count = 2
        assert needs_reconcile is False

        scheduler.shutdown()

    def test_submit_different_paths(self):
        from server.refresh_scheduler import RefreshScheduler, SchedulerConfig

        results = []
        def on_batch(ws_id, events, needs_reconcile):
            results.append((ws_id, events))

        config = SchedulerConfig(batch_quiet_window_ms=50)
        scheduler = RefreshScheduler(config=config, on_batch_ready=on_batch)

        e1 = self._make_event("ws_1", "a.py", "modify", seq=1)
        e2 = self._make_event("ws_1", "b.py", "modify", seq=2)
        scheduler.submit(e1)
        scheduler.submit(e2)
        scheduler.force_flush("ws_1")

        assert len(results) == 1
        _, events = results[0]
        assert len(events) == 2  # 两个不同路径不合并

        scheduler.shutdown()

    def test_queue_overflow(self):
        from server.refresh_scheduler import RefreshScheduler, SchedulerConfig

        config = SchedulerConfig(max_queue_entries=2, batch_quiet_window_ms=5000)
        scheduler = RefreshScheduler(config=config)

        e1 = self._make_event("ws_1", "a.py", "modify", seq=1)
        e2 = self._make_event("ws_1", "b.py", "modify", seq=2)
        e3 = self._make_event("ws_1", "c.py", "modify", seq=3)

        assert scheduler.submit(e1) is True
        assert scheduler.submit(e2) is True
        assert scheduler.submit(e3) is False  # 队列满

        stats = scheduler.get_queue_stats()
        assert "ws_1" in stats["needs_reconcile"]

        scheduler.shutdown()

    def test_create_delete_cancel(self):
        from server.refresh_scheduler import RefreshScheduler, SchedulerConfig

        results = []
        def on_batch(ws_id, events, needs_reconcile):
            results.append(events)

        config = SchedulerConfig(batch_quiet_window_ms=50)
        scheduler = RefreshScheduler(config=config, on_batch_ready=on_batch)

        e1 = self._make_event("ws_1", "test.py", "create", seq=1)
        e2 = self._make_event("ws_1", "test.py", "delete", seq=2)
        scheduler.submit(e1)
        scheduler.submit(e2)
        scheduler.force_flush("ws_1")

        # create + delete 互相抵消
        assert len(results) == 0 or len(results[0]) == 0

        scheduler.shutdown()


# ============================================
# Durable Staging WAL 测试
# ============================================


class TestDurableStagingLog:
    """SQLite WAL staging log 测试。"""

    def test_append_and_read(self, tmp_path):
        from server.durable_staging import DurableStagingLog

        db_path = str(tmp_path / "staging.db")
        log = DurableStagingLog(db_path)

        lsn1 = log.append("ws_1", "a.py", 1, 1, "modify", content_hash="hash1")
        lsn2 = log.append("ws_1", "b.py", 1, 2, "create", content_hash="hash2")
        assert lsn1 > 0
        assert lsn2 > lsn1

        pending = log.read_pending()
        assert len(pending) == 2
        assert pending[0].rel_path == "a.py"
        assert pending[1].rel_path == "b.py"

        log.close()

    def test_state_transitions(self, tmp_path):
        from server.durable_staging import DurableStagingLog

        db_path = str(tmp_path / "staging.db")
        log = DurableStagingLog(db_path)

        lsn = log.append("ws_1", "a.py", 1, 1, "modify")

        # pending → applying
        assert log.transition(lsn, "applying") is True
        pending = log.read_pending()
        assert len(pending) == 1
        assert pending[0].state == "applying"

        # applying → applied
        assert log.transition(lsn, "applied", generation=42) is True
        pending = log.read_pending()
        assert len(pending) == 0  # applied 不在 pending 中

        log.close()

    def test_invalid_transition(self, tmp_path):
        from server.durable_staging import DurableStagingLog

        db_path = str(tmp_path / "staging.db")
        log = DurableStagingLog(db_path)

        lsn = log.append("ws_1", "a.py", 1, 1, "modify")

        # pending → applied (跳过 applying) 不允许
        assert log.transition(lsn, "applied") is False

        log.close()

    def test_transition_to_failed(self, tmp_path):
        from server.durable_staging import DurableStagingLog

        db_path = str(tmp_path / "staging.db")
        log = DurableStagingLog(db_path)

        lsn = log.append("ws_1", "a.py", 1, 1, "modify")
        assert log.transition(lsn, "failed", error="parse error") is True

        all_entries = log.read_all(state="failed")
        assert len(all_entries) == 1
        assert all_entries[0].error == "parse error"

        log.close()

    def test_compact_applied(self, tmp_path):
        from server.durable_staging import DurableStagingLog

        db_path = str(tmp_path / "staging.db")
        log = DurableStagingLog(db_path)

        # 追加 5 条并全部 apply
        lsns = []
        for i in range(5):
            lsn = log.append("ws_1", f"file_{i}.py", 1, i + 1, "modify")
            lsns.append(lsn)

        for lsn in lsns:
            log.transition(lsn, "applying")
            log.transition(lsn, "applied", generation=1)

        # compact 保留最近 2 条
        log.compact_applied(keep_last_n=2)

        all_applied = log.read_all(state="applied")
        assert len(all_applied) == 2

        log.close()

    def test_recovery(self, tmp_path):
        from server.durable_staging import DurableStagingLog

        db_path = str(tmp_path / "staging.db")

        # 第一次运行：追加 entries
        log1 = DurableStagingLog(db_path)
        lsn1 = log1.append("ws_1", "a.py", 1, 1, "modify")
        lsn2 = log1.append("ws_1", "b.py", 1, 2, "modify")
        log1.transition(lsn1, "applying")
        log1.close()

        # 模拟 crash：lsn1 处于 applying，lsn2 处于 pending
        log2 = DurableStagingLog(db_path)
        to_recover = log2.recover()
        assert len(to_recover) == 2
        # lsn1 是 applying，lsn2 是 pending
        states = {e.lsn: e.state for e in to_recover}
        assert states[lsn1] == "applying"
        assert states[lsn2] == "pending"

        log2.close()

    def test_stats(self, tmp_path):
        from server.durable_staging import DurableStagingLog

        db_path = str(tmp_path / "staging.db")
        log = DurableStagingLog(db_path)

        log.append("ws_1", "a.py", 1, 1, "modify")
        lsn2 = log.append("ws_1", "b.py", 1, 2, "modify")
        log.transition(lsn2, "applying")

        stats = log.stats()
        assert stats["counts"]["pending"] == 1
        assert stats["counts"]["applying"] == 1
        assert stats["total"] == 2
        assert stats["max_lsn"] == lsn2

        log.close()

    def test_workspace_filter(self, tmp_path):
        from server.durable_staging import DurableStagingLog

        db_path = str(tmp_path / "staging.db")
        log = DurableStagingLog(db_path)

        log.append("ws_1", "a.py", 1, 1, "modify")
        log.append("ws_2", "b.py", 1, 1, "modify")
        log.append("ws_1", "c.py", 1, 2, "modify")

        ws1_pending = log.read_pending(workspace_id="ws_1")
        assert len(ws1_pending) == 2

        ws2_pending = log.read_pending(workspace_id="ws_2")
        assert len(ws2_pending) == 1

        log.close()
