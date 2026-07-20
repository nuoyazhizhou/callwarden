"""Phase 8.2: Daemon 运行时指标收集与导出。

设计参考：
- docs/design/enterprise-daemon-shared-snapshot-plan.md §Phase 8（metrics endpoint）
- 验收：内存、CPU、队列、错误率可观测

提供：
1. MetricsCollector：收集 daemon 运行时指标
   - 计数器（counters）：请求数、错误数、job 完成数
   - 仪表（gauges）：内存、CPU、队列大小、活跃连接
   - 直方图（histograms）：请求延迟、job 执行时间
2. 运行时指标收集：内存 RSS、CPU 时间、uptime
3. 导出格式：
   - Prometheus 文本格式（/metrics endpoint）
   - JSON 格式（API 查询）

指标命名约定：
- cw_daemon_<category>_<name>{labels} value
- 示例：cw_daemon_jobs_total{status="completed"} 42
- 示例：cw_daemon_memory_rss_bytes 134217728
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
import threading
import json
from typing import Any, Dict, Iterator, List, Optional, Tuple


# ============================================================
# 运行时指标采集
# ============================================================


def get_memory_info() -> Dict[str, int]:
    """获取当前进程的内存信息（字节）。

    Linux: 读取 /proc/self/status
    Windows: 使用 psutil（如果可用），否则返回 0
    其他: 尝试 resource 模块

    Returns:
        {"rss": int, "vms": int, "peak": int}
        单位为字节，不可用时返回 0
    """
    result = {"rss": 0, "vms": 0, "peak": 0}

    # Linux: 读取 /proc/self/status
    if sys.platform == "linux":
        try:
            with open("/proc/self/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        result["rss"] = int(line.split()[1]) * 1024  # KB -> bytes
                    elif line.startswith("VmSize:"):
                        result["vms"] = int(line.split()[1]) * 1024
                    elif line.startswith("VmPeak:"):
                        result["peak"] = int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        return result

    # 尝试 psutil（跨平台）
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        result["rss"] = getattr(mem, "rss", 0)
        result["vms"] = getattr(mem, "vms", 0)
        result["peak"] = getattr(mem, "peak", result["rss"])
    except ImportError:
        pass

    return result


def get_cpu_info() -> Dict[str, float]:
    """获取当前进程的 CPU 时间（秒）。

    Returns:
        {"user": float, "system": float, "total": float}
    """
    # 优先使用 os.times()（跨平台）
    try:
        times = os.times()
        user = times.user
        system = times.system
        return {"user": user, "system": system, "total": user + system}
    except (AttributeError, OSError):
        pass

    # 尝试 resource 模块（Unix only）
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        user = usage.ru_utime
        system = usage.ru_stime
        return {"user": user, "system": system, "total": user + system}
    except (ImportError, AttributeError):
        pass

    return {"user": 0.0, "system": 0.0, "total": 0.0}


def get_process_info() -> Dict[str, Any]:
    """获取进程基本信息。

    Returns:
        {"pid": int, "uptime": float, "thread_count": int}
    """
    return {
        "pid": os.getpid(),
        "uptime": 0.0,  # 由 MetricsCollector 填充
        "thread_count": threading.active_count(),
    }


# ============================================================
# MetricsCollector
# ============================================================


class Counter:
    """单调递增计数器。

    用于记录请求数、错误数、job 完成数等。
    """

    def __init__(self, name: str, help_text: str = "", labels: Optional[List[str]] = None):
        self.name = name
        self.help_text = help_text
        self.label_keys = labels or []
        self._values: Dict[str, float] = {}  # label_str -> value
        self._lock = threading.Lock()

    def _label_key(self, labels: Optional[Dict[str, str]] = None) -> str:
        """将 label dict 转为排序后的 key 字符串。"""
        if not labels:
            return ""
        return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

    def inc(self, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        """递增计数器。"""
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def get(self, labels: Optional[Dict[str, str]] = None) -> float:
        """获取当前值。"""
        key = self._label_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def get_all(self) -> Dict[str, float]:
        """获取所有 label 组合的值。"""
        with self._lock:
            return dict(self._values)

    def reset(self) -> None:
        """重置计数器（仅用于测试）。"""
        with self._lock:
            self._values.clear()


class Gauge:
    """仪表，值可增可减。

    用于记录内存、CPU、队列大小等。
    """

    def __init__(self, name: str, help_text: str = "", labels: Optional[List[str]] = None):
        self.name = name
        self.help_text = help_text
        self.label_keys = labels or []
        self._values: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _label_key(self, labels: Optional[Dict[str, str]] = None) -> str:
        if not labels:
            return ""
        return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

    def set(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """设置 gauge 值。"""
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = value

    def inc(self, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        """递增 gauge 值。"""
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def dec(self, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        """递减 gauge 值。"""
        self.inc(-value, labels)

    def get(self, labels: Optional[Dict[str, str]] = None) -> float:
        """获取当前值。"""
        key = self._label_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def get_all(self) -> Dict[str, float]:
        """获取所有 label 组合的值。"""
        with self._lock:
            return dict(self._values)

    def reset(self) -> None:
        """重置 gauge（仅用于测试）。"""
        with self._lock:
            self._values.clear()


class Histogram:
    """直方图，记录观测值分布。

    用于记录请求延迟、job 执行时间等。
    """

    DEFAULT_BUCKETS = [
        0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0
    ]

    def __init__(self, name: str, help_text: str = "",
                 buckets: Optional[List[float]] = None,
                 labels: Optional[List[str]] = None):
        self.name = name
        self.help_text = help_text
        self.label_keys = labels or []
        self.buckets = sorted(buckets or self.DEFAULT_BUCKETS)
        self._observations: Dict[str, List[float]] = {}  # label_str -> [values]
        self._counts: Dict[str, List[int]] = {}  # label_str -> bucket counts
        self._sums: Dict[str, float] = {}  # label_str -> sum
        self._totals: Dict[str, int] = {}  # label_str -> total count
        self._lock = threading.Lock()

    def _label_key(self, labels: Optional[Dict[str, str]] = None) -> str:
        if not labels:
            return ""
        return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

    def observe(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """记录一个观测值。"""
        key = self._label_key(labels)
        with self._lock:
            if key not in self._counts:
                self._counts[key] = [0] * len(self.buckets)
                self._sums[key] = 0.0
                self._totals[key] = 0
                self._observations[key] = []

            self._observations[key].append(value)
            self._sums[key] += value
            self._totals[key] += 1

            # 更新 bucket counts
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    self._counts[key][i] += 1
                    break

    def get_stats(self, labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """获取统计信息。"""
        key = self._label_key(labels)
        with self._lock:
            if key not in self._totals:
                return {"count": 0, "sum": 0.0, "avg": 0.0}

            total = self._totals[key]
            sum_val = self._sums[key]
            observations = list(self._observations[key])

            # 计算分位数
            sorted_obs = sorted(observations)
            p50 = _percentile(sorted_obs, 0.50)
            p95 = _percentile(sorted_obs, 0.95)
            p99 = _percentile(sorted_obs, 0.99)

            return {
                "count": total,
                "sum": sum_val,
                "avg": sum_val / total if total > 0 else 0.0,
                "p50": p50,
                "p95": p95,
                "p99": p99,
                "buckets": [
                    {"le": b, "count": self._counts[key][i]}
                    for i, b in enumerate(self.buckets)
                ],
            }

    def reset(self) -> None:
        """重置直方图（仅用于测试）。"""
        with self._lock:
            self._observations.clear()
            self._counts.clear()
            self._sums.clear()
            self._totals.clear()


def _percentile(sorted_values: List[float], p: float) -> float:
    """计算分位数。

    Args:
        sorted_values: 已排序的值列表
        p: 百分位（0.0 - 1.0）

    Returns:
        分位数值
    """
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    k = (n - 1) * p
    f = int(k)
    c = k - f
    if f + 1 < n:
        return sorted_values[f] + c * (sorted_values[f + 1] - sorted_values[f])
    return sorted_values[f]


# ============================================================
# MetricsCollector
# ============================================================


class MetricsCollector:
    """Daemon 运行时指标收集器。

    用法：
        collector = MetricsCollector()

        # 注册指标
        collector.register_counter("requests_total", "Total requests", labels=["status"])
        collector.register_gauge("memory_rss_bytes", "Memory RSS in bytes")
        collector.register_histogram("request_duration_seconds", "Request duration")

        # 记录指标
        collector.increment("requests_total", labels={"status": "ok"})
        collector.set_gauge("memory_rss_bytes", 134217728)
        collector.observe("request_duration_seconds", 0.025)

        # 导出
        collector.collect_runtime_metrics()  # 自动收集运行时指标
        prometheus_text = collector.to_prometheus()
        json_data = collector.to_json()
    """

    def __init__(self):
        self._start_time = time.time()
        self._counters: Dict[str, Counter] = {}
        self._gauges: Dict[str, Gauge] = {}
        self._histograms: Dict[str, Histogram] = {}
        self._lock = threading.Lock()

        # 注册内置指标
        self._register_builtin_metrics()

    def _register_builtin_metrics(self) -> None:
        """注册内置指标。"""
        # 运行时指标
        self.register_gauge("memory_rss_bytes", "Resident set size in bytes")
        self.register_gauge("memory_vms_bytes", "Virtual memory size in bytes")
        self.register_gauge("memory_peak_bytes", "Peak memory usage in bytes")
        self.register_gauge("cpu_user_seconds", "User CPU time in seconds")
        self.register_gauge("cpu_system_seconds", "System CPU time in seconds")
        self.register_gauge("cpu_total_seconds", "Total CPU time in seconds")
        self.register_gauge("uptime_seconds", "Process uptime in seconds")
        self.register_gauge("thread_count", "Active thread count")

        # 请求指标
        self.register_counter("requests_total", "Total requests",
                              labels=["method", "status"])
        self.register_counter("errors_total", "Total errors",
                              labels=["type"])
        # G13（2026-07-20）：daemon RPC 调用延迟直方图，供 measure_rpc 埋点
        self.register_histogram("request_duration_seconds",
                                "RPC request duration in seconds",
                                labels=["method"])

        # Job 指标
        self.register_gauge("jobs_pending", "Pending jobs count")
        self.register_gauge("jobs_running", "Running jobs count")
        self.register_gauge("jobs_completed_total", "Completed jobs count")
        self.register_gauge("jobs_failed_total", "Failed jobs count")
        self.register_counter("jobs_submitted_total", "Total jobs submitted")
        self.register_counter("jobs_cancelled_total", "Total jobs cancelled")
        self.register_histogram("job_duration_seconds", "Job execution duration",
                                labels=["handler"])

        # 连接指标
        self.register_gauge("active_connections", "Active connections count")

    def _register_metric(self, registry: Dict, name: str, instance: Any) -> None:
        with self._lock:
            registry[name] = instance

    # ----- 注册 -----

    def register_counter(self, name: str, help_text: str = "",
                         labels: Optional[List[str]] = None) -> Counter:
        """注册计数器。"""
        c = Counter(name, help_text, labels)
        self._register_metric(self._counters, name, c)
        return c

    def register_gauge(self, name: str, help_text: str = "",
                       labels: Optional[List[str]] = None) -> Gauge:
        """注册仪表。"""
        g = Gauge(name, help_text, labels)
        self._register_metric(self._gauges, name, g)
        return g

    def register_histogram(self, name: str, help_text: str = "",
                           buckets: Optional[List[float]] = None,
                           labels: Optional[List[str]] = None) -> Histogram:
        """注册直方图。"""
        h = Histogram(name, help_text, buckets, labels)
        self._register_metric(self._histograms, name, h)
        return h

    # ----- 记录 -----

    def increment(self, name: str, value: float = 1.0,
                  labels: Optional[Dict[str, str]] = None) -> None:
        """递增计数器。"""
        if name in self._counters:
            self._counters[name].inc(value, labels)

    def set_gauge(self, name: str, value: float,
                  labels: Optional[Dict[str, str]] = None) -> None:
        """设置仪表值。"""
        if name in self._gauges:
            self._gauges[name].set(value, labels)

    def inc_gauge(self, name: str, value: float = 1.0,
                  labels: Optional[Dict[str, str]] = None) -> None:
        """递增仪表值。"""
        if name in self._gauges:
            self._gauges[name].inc(value, labels)

    def dec_gauge(self, name: str, value: float = 1.0,
                  labels: Optional[Dict[str, str]] = None) -> None:
        """递减仪表值。"""
        if name in self._gauges:
            self._gauges[name].dec(value, labels)

    def observe(self, name: str, value: float,
                labels: Optional[Dict[str, str]] = None) -> None:
        """记录直方图观测值。"""
        if name in self._histograms:
            self._histograms[name].observe(value, labels)

    # ----- 收集运行时指标 -----

    def collect_runtime_metrics(self) -> None:
        """收集运行时指标（内存、CPU、uptime）。

        应定期调用（如每 10 秒）更新运行时 gauge。
        """
        # 内存
        mem = get_memory_info()
        self.set_gauge("memory_rss_bytes", mem["rss"])
        self.set_gauge("memory_vms_bytes", mem["vms"])
        self.set_gauge("memory_peak_bytes", mem["peak"])

        # CPU
        cpu = get_cpu_info()
        self.set_gauge("cpu_user_seconds", cpu["user"])
        self.set_gauge("cpu_system_seconds", cpu["system"])
        self.set_gauge("cpu_total_seconds", cpu["total"])

        # uptime
        uptime = time.time() - self._start_time
        self.set_gauge("uptime_seconds", uptime)

        # 线程数
        self.set_gauge("thread_count", threading.active_count())

    # ----- 导出 -----

    def to_prometheus(self) -> str:
        """导出为 Prometheus 文本格式。

        格式：
            # HELP <name> <help_text>
            # TYPE <name> <type>
            <name>{labels} <value>

        Returns:
            Prometheus 格式的文本
        """
        lines: List[str] = []
        self.collect_runtime_metrics()

        # Counters
        for name, counter in sorted(self._counters.items()):
            if counter.help_text:
                lines.append(f"# HELP {name} {counter.help_text}")
            lines.append(f"# TYPE {name} counter")
            for label_key, value in sorted(counter.get_all().items()):
                label_str = _format_labels(label_key)
                lines.append(f"{name}{label_str} {value}")

        # Gauges
        for name, gauge in sorted(self._gauges.items()):
            if gauge.help_text:
                lines.append(f"# HELP {name} {gauge.help_text}")
            lines.append(f"# TYPE {name} gauge")
            for label_key, value in sorted(gauge.get_all().items()):
                label_str = _format_labels(label_key)
                lines.append(f"{name}{label_str} {value}")

        # Histograms
        for name, hist in sorted(self._histograms.items()):
            if hist.help_text:
                lines.append(f"# HELP {name} {hist.help_text}")
            lines.append(f"# TYPE {name} histogram")
            for label_key in sorted(hist._totals.keys()):
                stats = hist.get_stats(_unformat_labels(label_key))
                label_prefix = _format_labels(label_key)
                # bucket 行
                for bucket in stats["buckets"]:
                    bucket_labels = _merge_labels(label_key, f"le={bucket['le']}")
                    lines.append(f"{name}_bucket{_format_labels(bucket_labels)} {bucket['count']}")
                # +Inf bucket
                inf_labels = _merge_labels(label_key, "le=+Inf")
                lines.append(f"{name}_bucket{_format_labels(inf_labels)} {stats['count']}")
                lines.append(f"{name}_sum{label_prefix} {stats['sum']}")
                lines.append(f"{name}_count{label_prefix} {stats['count']}")

        return "\n".join(lines) + "\n"

    def to_json(self) -> Dict[str, Any]:
        """导出为 JSON 格式。

        Returns:
            包含所有指标的字典
        """
        self.collect_runtime_metrics()

        result: Dict[str, Any] = {
            "timestamp": time.time(),
            "uptime": time.time() - self._start_time,
            "counters": {},
            "gauges": {},
            "histograms": {},
        }

        # Counters
        for name, counter in sorted(self._counters.items()):
            result["counters"][name] = {
                "help": counter.help_text,
                "values": counter.get_all(),
            }

        # Gauges
        for name, gauge in sorted(self._gauges.items()):
            result["gauges"][name] = {
                "help": gauge.help_text,
                "values": gauge.get_all(),
            }

        # Histograms
        for name, hist in sorted(self._histograms.items()):
            result["histograms"][name] = {
                "help": hist.help_text,
                "stats": {},
            }
            for label_key in hist._totals:
                stats = hist.get_stats(_unformat_labels(label_key))
                result["histograms"][name]["stats"][label_key] = stats

        return result

    def to_json_string(self, indent: int = 2) -> str:
        """导出为 JSON 字符串。"""
        return json.dumps(self.to_json(), indent=indent, ensure_ascii=False)

    # ----- 工具方法 -----

    def get_metric(self, name: str) -> Optional[Any]:
        """获取单个指标实例。"""
        if name in self._counters:
            return self._counters[name]
        if name in self._gauges:
            return self._gauges[name]
        if name in self._histograms:
            return self._histograms[name]
        return None

    def list_metrics(self) -> Dict[str, List[str]]:
        """列出所有指标名。"""
        return {
            "counters": sorted(self._counters.keys()),
            "gauges": sorted(self._gauges.keys()),
            "histograms": sorted(self._histograms.keys()),
        }

    def reset(self) -> None:
        """重置所有指标（仅用于测试）。"""
        for c in self._counters.values():
            c.reset()
        for g in self._gauges.values():
            g.reset()
        for h in self._histograms.values():
            h.reset()
        self._start_time = time.time()

    @property
    def start_time(self) -> float:
        """获取启动时间。"""
        return self._start_time

    @property
    def uptime(self) -> float:
        """获取运行时长（秒）。"""
        return time.time() - self._start_time


# ============================================================
# 标签格式化工具
# ============================================================


def _format_labels(label_key: str) -> str:
    """将 label_key 字符串格式化为 Prometheus 标签格式。

    "status=ok,method=query" -> '{status="ok",method="query"}'
    "" -> ""
    """
    if not label_key:
        return ""
    parts = label_key.split(",")
    formatted = []
    for part in parts:
        if "=" in part:
            k, v = part.split("=", 1)
            formatted.append(f'{k}="{v}"')
        else:
            formatted.append(part)
    return "{" + ",".join(formatted) + "}"


def _unformat_labels(label_key: str) -> Dict[str, str]:
    """将 label_key 字符串解析为 label dict。"""
    if not label_key:
        return {}
    result = {}
    for part in label_key.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            result[k] = v
    return result


def _merge_labels(label_key: str, extra: str) -> str:
    """合并 label_key 和额外的 label。"""
    if not label_key:
        return extra
    return f"{label_key},{extra}"


# ============================================================
# 全局单例
# ============================================================


_global_collector: Optional[MetricsCollector] = None
_global_lock = threading.Lock()


def get_metrics_collector() -> MetricsCollector:
    """获取全局 MetricsCollector 单例。"""
    global _global_collector
    if _global_collector is None:
        with _global_lock:
            if _global_collector is None:
                _global_collector = MetricsCollector()
    return _global_collector


def reset_metrics_collector() -> None:
    """重置全局 MetricsCollector（仅用于测试）。"""
    global _global_collector
    with _global_lock:
        _global_collector = None


# ============================================
# G13（2026-07-20 二轮评审补全）：daemon 埋点辅助工具
# ============================================


@contextlib.contextmanager
def measure_rpc(method: str) -> Iterator[None]:
    """RPC 调用埋点上下文管理器（G13 daemon metrics 修复）

    用法：
        with measure_rpc("workspace.file.refresh"):
            ...

    自动埋点：
    - requests_total{method, status="ok"|"error"}
    - request_duration_seconds{method}
    - errors_total{type="rpc_error|internal"} (仅异常时)
    - active_connections gauge (执行期间 +1，结束 -1)

    异常类型识别（避免循环 import daemon_server.DaemonRpcError）：
    - 通过 ``sys.exc_info()[0].__name__`` 字符串比对 DaemonRpcError 类名
    - DaemonRpcError → errors_total{type="rpc_error"}
    - 其他异常 → errors_total{type="internal"}（异常被 re-raise，由上层捕获）

    Args:
        method: RPC 方法名（如 "workspace.file.refresh"）

    Yields:
        None
    """
    collector = get_metrics_collector()
    start = time.time()
    collector.inc_gauge("active_connections")
    status = "ok"
    error_type = ""
    try:
        yield
    except Exception:
        status = "error"
        exc_type = sys.exc_info()[0]
        if exc_type is not None and exc_type.__name__ == "DaemonRpcError":
            error_type = "rpc_error"
        else:
            error_type = "internal"
        raise
    finally:
        collector.dec_gauge("active_connections")
        collector.increment(
            "requests_total",
            labels={"method": method, "status": status},
        )
        if error_type:
            collector.increment(
                "errors_total",
                labels={"type": error_type},
            )
        collector.observe(
            "request_duration_seconds",
            time.time() - start,
            labels={"method": method},
        )
