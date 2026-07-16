"""Refresh 事件合并调度器。

任务：T-1783974522648-e2d3 Step #2
规范：enterprise-watcher-benefit-production-plan.md §3.3

按 (workspace_id, rel_path) 保存最新事件，支持：
- modify+modify / create+modify / delete+create / modify+delete 合并
- checkout/repo sync 事件风暴 + reconcile barrier
- 队列条数/字节限制与背压
"""

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from callwarden.server.watcher_protocol import (
    EventKind,
    WatcherEvent,
    coalesce_events,
)

logger = logging.getLogger(__name__)


@dataclass
class SchedulerConfig:
    """调度器配置参数。"""
    quiet_window_ms: int = 250          # 单文件安静窗口
    batch_quiet_window_ms: int = 500    # workspace batch 安静窗口
    max_wait_ms: int = 2000             # 最大等待时间
    max_batch_files: int = 1000         # 单 batch 最大文件数
    max_queue_entries: int = 10000      # 全局队列最大条数
    max_queue_bytes: int = 256 * 1024 * 1024  # 全局队列最大字节（256MB）
    reconcile_event_threshold: int = 50  # 短窗内超过此数触发 reconcile


@dataclass
class PendingEvent:
    """队列中的待处理事件。"""
    event: WatcherEvent
    coalesced_count: int = 1
    enqueue_time_ns: int = 0
    content_size: int = 0


@dataclass
class BatchResult:
    """单次 batch 处理结果。"""
    workspace_id: str
    file_count: int = 0
    coalesced_total: int = 0
    needs_reconcile: bool = False
    duration_ms: float = 0.0


class RefreshScheduler:
    """事件合并调度器——latest-wins + reconcile barrier。

    线程安全：通过 threading.Lock 保护内部状态。
    回调：on_batch_ready(workspace_id, events) 由调度器在 batch 就绪时调用。
    """

    def __init__(
        self,
        config: Optional[SchedulerConfig] = None,
        on_batch_ready: Optional[Callable] = None,
    ):
        self.config = config or SchedulerConfig()
        self._on_batch_ready = on_batch_ready
        self._lock = threading.Lock()

        # (workspace_id, rel_path) → PendingEvent
        self._pending: Dict[Tuple[str, str], PendingEvent] = {}
        # workspace_id → 最近事件时间
        self._ws_last_event_ns: Dict[str, int] = {}
        # workspace_id → 短窗内事件计数
        self._ws_event_window: Dict[str, List[int]] = defaultdict(list)

        # 全局队列统计
        self._total_entries = 0
        self._total_bytes = 0
        self._needs_reconcile: set = set()  # 需要 reconcile 的 workspace

        # 定时器
        self._batch_timer: Optional[threading.Timer] = None

    def submit(self, event: WatcherEvent) -> bool:
        """提交一个文件事件到调度器。

        返回 True 表示事件已入队，False 表示队列已满（背压）。
        """
        now_ns = time.monotonic_ns()
        content_size = len(event.canonical_bytes) if event.canonical_bytes else 0

        with self._lock:
            # 检查队列限制
            if (self._total_entries >= self.config.max_queue_entries or
                    self._total_bytes + content_size > self.config.max_queue_bytes):
                # 队列满：设置 reconcile 标记，丢弃可重建的内存事件
                self._needs_reconcile.add(event.workspace_instance_id)
                logger.warning(
                    "queue overflow for ws=%s, setting needs_reconcile",
                    event.workspace_instance_id,
                )
                return False

            key = (event.workspace_instance_id, event.rel_path)
            existing = self._pending.get(key)

            if existing is not None:
                # 合并事件
                merged = coalesce_events(existing.event, event)
                if merged is None:
                    # 互相抵消（create + delete）
                    del self._pending[key]
                    self._total_entries -= 1
                    self._total_bytes -= existing.content_size
                else:
                    existing.event = merged
                    existing.coalesced_count += 1
                    existing.content_size = content_size
            else:
                # 新事件
                self._pending[key] = PendingEvent(
                    event=event,
                    coalesced_count=1,
                    enqueue_time_ns=now_ns,
                    content_size=content_size,
                )
                self._total_entries += 1
                self._total_bytes += content_size

            # 更新 workspace 事件窗口
            ws_id = event.workspace_instance_id
            self._ws_last_event_ns[ws_id] = now_ns
            window = self._ws_event_window[ws_id]
            window.append(now_ns)
            # 清理 2 秒外的事件计数
            cutoff = now_ns - 2_000_000_000
            self._ws_event_window[ws_id] = [
                t for t in window if t > cutoff
            ]
            # 检测事件风暴
            if len(self._ws_event_window[ws_id]) >= self.config.reconcile_event_threshold:
                self._needs_reconcile.add(ws_id)

            # 重置 batch 定时器
            self._reset_batch_timer(ws_id)

        return True

    def _reset_batch_timer(self, ws_id: str):
        """重置或启动 batch 定时器。"""
        if self._batch_timer is not None:
            self._batch_timer.cancel()

        delay_sec = self.config.batch_quiet_window_ms / 1000.0
        self._batch_timer = threading.Timer(delay_sec, self._flush_batch, args=[ws_id])
        self._batch_timer.daemon = True
        self._batch_timer.start()

    def _flush_batch(self, ws_id: str):
        """刷新指定 workspace 的 batch。"""
        events: List[Tuple[WatcherEvent, int]] = []
        needs_reconcile = False

        with self._lock:
            # 收集该 workspace 的所有 pending 事件
            keys_to_remove = []
            for key, pending in self._pending.items():
                if key[0] == ws_id:
                    events.append((pending.event, pending.coalesced_count))
                    keys_to_remove.append(key)
                    self._total_entries -= 1
                    self._total_bytes -= pending.content_size

            for key in keys_to_remove:
                del self._pending[key]

            # 检查 reconcile 标记
            if ws_id in self._needs_reconcile:
                needs_reconcile = True
                self._needs_reconcile.discard(ws_id)

            # 清理事件窗口
            self._ws_event_window.pop(ws_id, None)
            self._ws_last_event_ns.pop(ws_id, None)

        # 检查 batch 大小限制
        if len(events) > self.config.max_batch_files:
            logger.warning(
                "batch size %d exceeds max %d, setting needs_reconcile",
                len(events), self.config.max_batch_files,
            )
            needs_reconcile = True

        # 调用回调
        if self._on_batch_ready and events:
            try:
                self._on_batch_ready(ws_id, events, needs_reconcile)
            except Exception as e:
                logger.error("batch callback failed for ws=%s: %s", ws_id, e)

    def force_flush(self, ws_id: Optional[str] = None):
        """强制刷新 batch（用于测试或 reconcile barrier）。"""
        if self._batch_timer is not None:
            self._batch_timer.cancel()
            self._batch_timer = None

        if ws_id:
            self._flush_batch(ws_id)
        else:
            # 刷新所有 workspace
            ws_ids = set(key[0] for key in self._pending.keys())
            for wid in ws_ids:
                self._flush_batch(wid)

    def get_queue_stats(self) -> Dict:
        """获取当前队列统计。"""
        with self._lock:
            return {
                "total_entries": self._total_entries,
                "total_bytes": self._total_bytes,
                "pending_workspaces": len(set(k[0] for k in self._pending.keys())),
                "needs_reconcile": list(self._needs_reconcile),
                "max_entries": self.config.max_queue_entries,
                "max_bytes": self.config.max_queue_bytes,
            }

    def shutdown(self):
        """停止调度器。"""
        if self._batch_timer is not None:
            self._batch_timer.cancel()
            self._batch_timer = None
