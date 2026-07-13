"""Watcher 事件协议与 Generation 可见性规范。

任务：T-1783974522648-e2d3 Step #0
规范：enterprise-watcher-benefit-production-plan.md §3.1-§3.2

定义从文件事件到 snapshot generation 的完整协议：
- 事件格式（agent → daemon）
- Generation 比较规则
- 延迟时间点 T0-T6
- Refresh 响应格式
- 查询可见性协议
"""

import enum
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ============================================
# 事件类型
# ============================================

class EventKind(enum.Enum):
    """文件事件类型。"""
    MODIFY = "modify"
    CREATE = "create"
    DELETE = "delete"
    RENAME = "rename"
    MOVE_SRC = "move_src"
    MOVE_DST = "move_dst"


# ============================================
# Watcher 事件（agent → daemon）
# ============================================

@dataclass
class WatcherEvent:
    """agent 发送给 daemon 的单个文件事件。

    规范：enterprise-watcher-benefit-production-plan.md §3.1
    """
    workspace_instance_id: str
    agent_session_id: str
    session_epoch: int
    monotonic_seq: int
    rel_path: str
    event_kind: EventKind
    observed_mtime_ns: int = 0
    observed_raw_hash: str = ""
    event_observed_mono_ns: int = 0
    # rename/move 额外字段
    rename_from_path: str = ""
    # FD 或 canonical bytes（由 UDS 传输层填充）
    canonical_bytes: Optional[bytes] = None
    content_fd: Optional[int] = None

    @property
    def generation(self) -> tuple:
        """返回 (session_epoch, monotonic_seq) 用于比较。"""
        return (self.session_epoch, self.monotonic_seq)

    def to_dict(self) -> Dict:
        """序列化为 RPC params dict。"""
        d = {
            "workspace_instance_id": self.workspace_instance_id,
            "agent_session_id": self.agent_session_id,
            "session_epoch": self.session_epoch,
            "monotonic_seq": self.monotonic_seq,
            "rel_path": self.rel_path,
            "event_kind": self.event_kind.value,
            "observed_mtime_ns": self.observed_mtime_ns,
            "observed_raw_hash": self.observed_raw_hash,
            "event_observed_mono_ns": self.event_observed_mono_ns,
        }
        if self.rename_from_path:
            d["rename_from_path"] = self.rename_from_path
        return d

    @classmethod
    def from_dict(cls, params: Dict) -> "WatcherEvent":
        """从 RPC params dict 反序列化。"""
        kind_str = params.get("event_kind", "modify")
        try:
            kind = EventKind(kind_str)
        except ValueError:
            kind = EventKind.MODIFY
        return cls(
            workspace_instance_id=str(params.get("workspace_instance_id", "")),
            agent_session_id=str(params.get("agent_session_id", "")),
            session_epoch=int(params.get("session_epoch", 0)),
            monotonic_seq=int(params.get("monotonic_seq", 0)),
            rel_path=str(params.get("rel_path", "")),
            event_kind=kind,
            observed_mtime_ns=int(params.get("observed_mtime_ns", 0)),
            observed_raw_hash=str(params.get("observed_raw_hash", "")),
            event_observed_mono_ns=int(params.get("event_observed_mono_ns", 0)),
            rename_from_path=str(params.get("rename_from_path", "")),
        )


# ============================================
# Refresh 响应（daemon → agent）
# ============================================

class CASResult(enum.Enum):
    """CAS 查找结果。"""
    HIT = "hit"
    MISS = "miss"
    DIRTY_OVERLAY = "dirty_overlay"


@dataclass
class RefreshResponse:
    """daemon 返回给 agent 的 refresh 结果。

    规范：enterprise-watcher-benefit-production-plan.md §3.1
    """
    file_generation: str = ""           # "{epoch}:{seq}"
    snapshot_generation: int = 0        # daemon 分配的 workspace snapshot generation
    cas_result: CASResult = CASResult.MISS
    coalesced_event_count: int = 1      # 被合并的事件数
    stage_durations_ms: Dict[str, float] = field(default_factory=dict)
    status: str = "committed"           # committed / stale_seq_dropped / error
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        """序列化为 RPC result dict。"""
        return {
            "file_generation": self.file_generation,
            "snapshot_generation": self.snapshot_generation,
            "cas_result": self.cas_result.value,
            "coalesced_event_count": self.coalesced_event_count,
            "stage_durations_ms": self.stage_durations_ms,
            "status": self.status,
            "error": self.error,
        }


# ============================================
# 延迟时间点 T0-T6
# ============================================

@dataclass
class StageTimestamps:
    """记录 refresh 管道的各阶段时间点（单调时钟纳秒）。

    规范：enterprise-watcher-benefit-production-plan.md §3.2

    T0: watcher 收到内核事件
    T1: 防抖/合并完成并进入发送队列
    T2: daemon 完成接收、ACL、字节限制与 generation seen CAS
    T3: canonicalize、hash、CAS lookup/parse 完成
    T4: delta durable，manifest/projection 条件提交完成
    T5: SnapshotManager 发布新 ArcSwap generation
    T6: 第一条带目标 generation 的查询返回新符号/调用边
    """
    t0_event_received_ns: int = 0
    t1_coalesce_complete_ns: int = 0
    t2_acl_and_seen_cas_ns: int = 0
    t3_parse_complete_ns: int = 0
    t4_manifest_committed_ns: int = 0
    t5_snapshot_published_ns: int = 0
    t6_query_visible_ns: int = 0

    def set_stage(self, stage: str, ns: int = 0):
        """设置指定阶段的时间点。ns=0 时使用当前单调时钟。"""
        if ns == 0:
            ns = time.monotonic_ns()
        attr = f"t{stage}_ns" if stage.isdigit() else f"t{stage}"
        # 映射 stage 名到字段名
        stage_map = {
            "0": "t0_event_received_ns",
            "1": "t1_coalesce_complete_ns",
            "2": "t2_acl_and_seen_cas_ns",
            "3": "t3_parse_complete_ns",
            "4": "t4_manifest_committed_ns",
            "5": "t5_snapshot_published_ns",
            "6": "t6_query_visible_ns",
        }
        field_name = stage_map.get(stage, attr)
        if hasattr(self, field_name):
            setattr(self, field_name, ns)

    def to_durations_ms(self) -> Dict[str, float]:
        """计算各阶段耗时（毫秒）。"""
        durations = {}
        stages = [
            ("t0_event_received_ns", "t1_coalesce_complete_ns", "T0→T1 coalesce"),
            ("t1_coalesce_complete_ns", "t2_acl_and_seen_cas_ns", "T1→T2 ACL+CAS"),
            ("t2_acl_and_seen_cas_ns", "t3_parse_complete_ns", "T2→T3 parse"),
            ("t3_parse_complete_ns", "t4_manifest_committed_ns", "T3→T4 manifest"),
            ("t4_manifest_committed_ns", "t5_snapshot_published_ns", "T4→T5 snapshot"),
            ("t5_snapshot_published_ns", "t6_query_visible_ns", "T5→T6 query"),
        ]
        for start_attr, end_attr, label in stages:
            start = getattr(self, start_attr, 0)
            end = getattr(self, end_attr, 0)
            if start > 0 and end > 0:
                durations[label] = (end - start) / 1_000_000
        # 总耗时
        if self.t0_event_received_ns > 0 and self.t6_query_visible_ns > 0:
            durations["T0→T6 total"] = (
                (self.t6_query_visible_ns - self.t0_event_received_ns) / 1_000_000
            )
        elif self.t0_event_received_ns > 0 and self.t5_snapshot_published_ns > 0:
            durations["T0→T5 total"] = (
                (self.t5_snapshot_published_ns - self.t0_event_received_ns) / 1_000_000
            )
        return durations


# ============================================
# Generation 比较
# ============================================

def compare_generations(gen_a: str, gen_b: str) -> int:
    """比较两个 generation 字符串 "{epoch}:{seq}"。

    返回：
        -1 如果 gen_a < gen_b
         0 如果 gen_a == gen_b
         1 如果 gen_a > gen_b
    """
    if not gen_a and not gen_b:
        return 0
    if not gen_a:
        return -1
    if not gen_b:
        return 1

    try:
        epoch_a, seq_a = gen_a.split(":")
        epoch_b, seq_b = gen_b.split(":")
        ea, sa = int(epoch_a), int(seq_a)
        eb, sb = int(epoch_b), int(seq_b)
    except (ValueError, IndexError):
        return 0

    if ea != eb:
        return -1 if ea < eb else 1
    if sa != sb:
        return -1 if sa < sb else 1
    return 0


def is_stale_generation(incoming: str, current: str) -> bool:
    """检查 incoming generation 是否已过时（<= current）。"""
    return compare_generations(incoming, current) <= 0


# ============================================
# 事件合并规则
# ============================================

# 合并矩阵：(前一个事件, 后一个事件) → 合并结果
# 规范：enterprise-watcher-benefit-production-plan.md §3.3
COALESCE_RULES = {
    (EventKind.MODIFY, EventKind.MODIFY): EventKind.MODIFY,
    (EventKind.CREATE, EventKind.MODIFY): EventKind.CREATE,
    (EventKind.DELETE, EventKind.CREATE): EventKind.MODIFY,  # replace
    (EventKind.MODIFY, EventKind.DELETE): EventKind.DELETE,
    (EventKind.CREATE, EventKind.DELETE): None,  # 互相抵消
}


def coalesce_events(prev: WatcherEvent, curr: WatcherEvent) -> Optional[WatcherEvent]:
    """按合并规则合并两个同路径事件。

    返回合并后的事件，或 None（如果互相抵消）。
    """
    key = (prev.event_kind, curr.event_kind)
    result_kind = COALESCE_RULES.get(key)

    if result_kind is None:
        # 互相抵消（create + delete）
        return None

    if result_kind is not None:
        # 使用合并后的类型，保留最新内容
        merged = WatcherEvent(
            workspace_instance_id=curr.workspace_instance_id,
            agent_session_id=curr.agent_session_id,
            session_epoch=curr.session_epoch,
            monotonic_seq=curr.monotonic_seq,
            rel_path=curr.rel_path,
            event_kind=result_kind,
            observed_mtime_ns=curr.observed_mtime_ns,
            observed_raw_hash=curr.observed_raw_hash,
            event_observed_mono_ns=prev.event_observed_mono_ns,  # 保留最早时间
            canonical_bytes=curr.canonical_bytes,
            content_fd=curr.content_fd,
        )
        return merged

    # 无合并规则：返回最新事件
    return curr
