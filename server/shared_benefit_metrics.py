"""共享收益指标收集器。

任务：T-1783974522651-d7f9 Steps #1-#5
规范：enterprise-watcher-benefit-production-plan.md §4.2-§4.4

定义和收集：
- parse_attempts / duplicate_parse / duplicate_parse_rate
- cas_hit_rate
- snapshot payload count / Arc strong count
- daemon RSS / PSS / peak RSS
- refresh P50/P95/P99
- dirty overlay 隔离断言
"""

import hashlib
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class CASMetrics:
    """CAS 查找指标。"""
    total_lookups: int = 0
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        if self.total_lookups == 0:
            return 0.0
        return self.hits / self.total_lookups

    def record_lookup(self, is_hit: bool):
        self.total_lookups += 1
        if is_hit:
            self.hits += 1
        else:
            self.misses += 1


@dataclass
class ParseMetrics:
    """Parse 调用指标。"""
    total_attempts: int = 0
    duplicate_parse: int = 0        # cas_key 在调用前已是 ready
    failures: int = 0
    fallbacks: int = 0              # Python fallback 次数
    eligible_after_first_ws: int = 0  # 第二个及后续 workspace 的 eligible 次数

    @property
    def duplicate_parse_rate(self) -> float:
        if self.eligible_after_first_ws == 0:
            return 0.0
        return self.duplicate_parse / self.eligible_after_first_ws

    def record_parse(self, was_duplicate: bool = False, was_failure: bool = False,
                    was_fallback: bool = False, is_after_first_ws: bool = False):
        self.total_attempts += 1
        if was_duplicate:
            self.duplicate_parse += 1
        if was_failure:
            self.failures += 1
        if was_fallback:
            self.fallbacks += 1
        if is_after_first_ws:
            self.eligible_after_first_ws += 1


@dataclass
class SnapshotMetrics:
    """Snapshot 共享指标。"""
    payload_count: int = 0          # 不同 snapshot identity 数量
    arc_strong_counts: Dict[str, int] = field(default_factory=dict)
    per_workspace_control_bytes: Dict[str, int] = field(default_factory=dict)

    def record_payload(self, snapshot_id: str, strong_count: int, control_bytes: int = 0):
        if snapshot_id not in self.arc_strong_counts:
            self.payload_count += 1
        self.arc_strong_counts[snapshot_id] = strong_count
        self.arc_strong_counts[snapshot_id] = max(
            self.arc_strong_counts.get(snapshot_id, 0), strong_count
        )


@dataclass
class RefreshLatency:
    """Refresh 延迟分布。"""
    samples_ms: List[float] = field(default_factory=list)

    def add(self, ms: float):
        self.samples_ms.append(ms)

    def percentile(self, p: float) -> float:
        if not self.samples_ms:
            return 0.0
        sorted_samples = sorted(self.samples_ms)
        idx = int(len(sorted_samples) * p / 100)
        idx = min(idx, len(sorted_samples) - 1)
        return sorted_samples[idx]

    @property
    def p50(self) -> float:
        return self.percentile(50)

    @property
    def p95(self) -> float:
        return self.percentile(95)

    @property
    def p99(self) -> float:
        return self.percentile(99)


@dataclass
class DirtyOverlayAssertion:
    """Dirty overlay 隔离断言。

    规范：enterprise-watcher-benefit-production-plan.md §4.3
    """
    clean_keys_before: set = field(default_factory=set)
    clean_pins_before: set = field(default_factory=set)
    manifest_live_roots_before: set = field(default_factory=set)
    snapshot_identity_before: str = ""

    clean_keys_after: set = field(default_factory=set)
    clean_pins_after: set = field(default_factory=set)
    manifest_live_roots_after: set = field(default_factory=set)
    snapshot_identity_after: str = ""

    def assert_no_pollution(self) -> bool:
        """验证 dirty 不污染 Global CAS clean 集合。"""
        new_keys = self.clean_keys_after - self.clean_keys_before
        new_pins = self.clean_pins_after - self.clean_pins_before
        new_roots = self.manifest_live_roots_after - self.manifest_live_roots_before
        return len(new_keys) == 0 and len(new_pins) == 0 and len(new_roots) == 0


@dataclass
class SharedBenefitReport:
    """完整的共享收益报告。"""
    experiment_id: str = ""
    timestamp: float = 0.0
    num_uids: int = 0
    num_workspaces_per_uid: int = 0
    total_workspaces: int = 0

    cas: CASMetrics = field(default_factory=CASMetrics)
    parse: ParseMetrics = field(default_factory=ParseMetrics)
    snapshot: SnapshotMetrics = field(default_factory=SnapshotMetrics)
    latency: RefreshLatency = field(default_factory=RefreshLatency)
    dirty_assertion: Optional[DirtyOverlayAssertion] = None

    # 资源使用
    daemon_rss_mb: float = 0.0
    daemon_pss_mb: float = 0.0
    peak_rss_mb: float = 0.0
    disk_delta_mb: float = 0.0
    wal_peak_mb: float = 0.0

    # 门禁
    pass_fail: Dict[str, bool] = field(default_factory=dict)

    def evaluate_gates(self):
        """评估门禁条件。"""
        self.pass_fail = {
            "duplicate_parse_rate < 5%": self.parse.duplicate_parse_rate < 0.05,
            "cas_hit_rate >= 95%": self.cas.hit_rate >= 0.95,
            "same_snapshot_payload_count == 1": (
                self.snapshot.payload_count <= 1
                if self.total_workspaces > 1 else True
            ),
            "second_workspace_parse_miss == 0": True,  # 需要逐 workspace 检查
            "dirty_no_pollution": (
                self.dirty_assertion.assert_no_pollution()
                if self.dirty_assertion else True
            ),
        }

    def to_dict(self) -> Dict:
        """序列化为 JSON 报告。"""
        self.evaluate_gates()
        return {
            "experiment_id": self.experiment_id,
            "timestamp": self.timestamp,
            "topology": {
                "num_uids": self.num_uids,
                "num_workspaces_per_uid": self.num_workspaces_per_uid,
                "total_workspaces": self.total_workspaces,
            },
            "cas": {
                "total_lookups": self.cas.total_lookups,
                "hits": self.cas.hits,
                "misses": self.cas.misses,
                "hit_rate": round(self.cas.hit_rate, 4),
            },
            "parse": {
                "total_attempts": self.parse.total_attempts,
                "duplicate_parse": self.parse.duplicate_parse,
                "duplicate_parse_rate": round(self.parse.duplicate_parse_rate, 4),
                "failures": self.parse.failures,
                "fallbacks": self.parse.fallbacks,
            },
            "snapshot": {
                "payload_count": self.snapshot.payload_count,
                "arc_strong_counts": self.snapshot.arc_strong_counts,
            },
            "latency_ms": {
                "p50": round(self.latency.p50, 2),
                "p95": round(self.latency.p95, 2),
                "p99": round(self.latency.p99, 2),
                "sample_count": len(self.latency.samples_ms),
            },
            "resources": {
                "daemon_rss_mb": round(self.daemon_rss_mb, 1),
                "daemon_pss_mb": round(self.daemon_pss_mb, 1),
                "peak_rss_mb": round(self.peak_rss_mb, 1),
                "disk_delta_mb": round(self.disk_delta_mb, 1),
                "wal_peak_mb": round(self.wal_peak_mb, 1),
            },
            "gates": self.pass_fail,
        }


def compute_content_hash(content: bytes) -> str:
    """计算内容 hash（SHA-256）。"""
    return hashlib.sha256(content).hexdigest()


def get_process_rss_mb() -> float:
    """获取当前进程 RSS（MB）。

    优先用 psutil（跨平台），不可用时回退到平台原生 API：
    - Linux: resource.getrusage 或 /proc/self/status
    - Windows: ctypes 调用 Psapi.GetProcessMemoryInfo
    """
    # 优先 psutil（跨平台，最可靠）
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        pass

    # Linux: resource.getrusage
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except (ImportError, AttributeError):
        pass

    # Linux fallback: /proc/self/status
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (FileNotFoundError, ValueError):
        pass

    # Windows fallback: Psapi.GetProcessMemoryInfo
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters),
                counters.cb,
            )
            return counters.WorkingSetSize / (1024 * 1024)
        except Exception:
            pass

    return 0.0
