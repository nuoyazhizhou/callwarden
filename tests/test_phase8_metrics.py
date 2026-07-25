"""Phase 8.2: metrics endpoint 测试。

测试覆盖：
1. Counter：递增、label、获取值
2. Gauge：设置、递增、递减、获取值
3. Histogram：观测、统计、分位数、bucket
4. MetricsCollector：注册、记录、运行时收集、导出
5. 导出格式：Prometheus 文本格式、JSON 格式
6. 全局单例
7. 标签格式化工具
"""

import json
import time
import threading
import pytest

from server.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsCollector,
    get_metrics_collector,
    reset_metrics_collector,
    get_memory_info,
    get_cpu_info,
    get_process_info,
    _percentile,
    _format_labels,
    _unformat_labels,
    _merge_labels,
)


# ============================================================
# Counter 测试
# ============================================================


class TestCounter:
    """Counter 测试。"""

    def test_initial_value(self):
        c = Counter("test_total", "test counter")
        assert c.get() == 0.0

    def test_inc_default(self):
        c = Counter("test_total")
        c.inc()
        assert c.get() == 1.0

    def test_inc_custom_value(self):
        c = Counter("test_total")
        c.inc(5.0)
        assert c.get() == 5.0

    def test_inc_multiple(self):
        c = Counter("test_total")
        c.inc(1.0)
        c.inc(2.0)
        c.inc(3.0)
        assert c.get() == 6.0

    def test_inc_with_labels(self):
        c = Counter("requests", labels=["method", "status"])
        c.inc(labels={"method": "GET", "status": "200"})
        c.inc(labels={"method": "GET", "status": "200"})
        c.inc(labels={"method": "POST", "status": "500"})
        assert c.get(labels={"method": "GET", "status": "200"}) == 2.0
        assert c.get(labels={"method": "POST", "status": "500"}) == 1.0

    def test_get_all(self):
        c = Counter("requests", labels=["status"])
        c.inc(labels={"status": "ok"})
        c.inc(labels={"status": "ok"})
        c.inc(labels={"status": "error"})
        all_values = c.get_all()
        assert len(all_values) == 2

    def test_reset(self):
        c = Counter("test_total")
        c.inc(10.0)
        c.reset()
        assert c.get() == 0.0

    def test_thread_safety(self):
        c = Counter("test_total")
        def worker():
            for _ in range(1000):
                c.inc()
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert c.get() == 10000.0

    def test_name_and_help(self):
        c = Counter("my_counter", "help text")
        assert c.name == "my_counter"
        assert c.help_text == "help text"


# ============================================================
# Gauge 测试
# ============================================================


class TestGauge:
    """Gauge 测试。"""

    def test_initial_value(self):
        g = Gauge("memory_bytes", "memory gauge")
        assert g.get() == 0.0

    def test_set(self):
        g = Gauge("memory_bytes")
        g.set(1024.0)
        assert g.get() == 1024.0

    def test_set_overwrites(self):
        g = Gauge("memory_bytes")
        g.set(100.0)
        g.set(200.0)
        assert g.get() == 200.0

    def test_inc(self):
        g = Gauge("memory_bytes")
        g.set(100.0)
        g.inc(50.0)
        assert g.get() == 150.0

    def test_dec(self):
        g = Gauge("memory_bytes")
        g.set(100.0)
        g.dec(30.0)
        assert g.get() == 70.0

    def test_inc_negative(self):
        g = Gauge("memory_bytes")
        g.set(100.0)
        g.inc(-50.0)
        assert g.get() == 50.0

    def test_with_labels(self):
        g = Gauge("queue_size", labels=["queue"])
        g.set(10, labels={"queue": "parse"})
        g.set(20, labels={"queue": "resolve"})
        assert g.get(labels={"queue": "parse"}) == 10
        assert g.get(labels={"queue": "resolve"}) == 20

    def test_reset(self):
        g = Gauge("memory_bytes")
        g.set(100.0)
        g.reset()
        assert g.get() == 0.0

    def test_thread_safety(self):
        g = Gauge("counter")
        def worker():
            for _ in range(1000):
                g.inc(1.0)
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert g.get() == 10000.0


# ============================================================
# Histogram 测试
# ============================================================


class TestHistogram:
    """Histogram 测试。"""

    def test_observe_single(self):
        h = Histogram("duration_seconds")
        h.observe(0.1)
        stats = h.get_stats()
        assert stats["count"] == 1
        assert stats["sum"] == 0.1
        assert stats["avg"] == 0.1

    def test_observe_multiple(self):
        h = Histogram("duration_seconds")
        h.observe(0.1)
        h.observe(0.2)
        h.observe(0.3)
        stats = h.get_stats()
        assert stats["count"] == 3
        assert stats["sum"] == pytest.approx(0.6)
        assert stats["avg"] == pytest.approx(0.2)

    def test_percentiles(self):
        h = Histogram("duration_seconds")
        for v in range(1, 101):
            h.observe(float(v) / 100)
        stats = h.get_stats()
        # p50 应该接近 0.5
        assert 0.4 <= stats["p50"] <= 0.6
        # p95 应该接近 0.95
        assert 0.85 <= stats["p95"] <= 1.0
        # p99 应该接近 0.99
        assert 0.90 <= stats["p99"] <= 1.0

    def test_buckets(self):
        h = Histogram("duration_seconds", buckets=[0.1, 0.5, 1.0])
        h.observe(0.05)
        h.observe(0.2)
        h.observe(0.6)
        h.observe(1.5)
        stats = h.get_stats()
        assert len(stats["buckets"]) == 3
        # bucket le=0.1 应该包含 0.05
        assert stats["buckets"][0]["le"] == 0.1
        assert stats["buckets"][0]["count"] == 1

    def test_with_labels(self):
        h = Histogram("duration_seconds", labels=["handler"])
        h.observe(0.1, labels={"handler": "clone_detect"})
        h.observe(0.2, labels={"handler": "clone_detect"})
        h.observe(0.5, labels={"handler": "vector_embed"})

        stats_clone = h.get_stats(labels={"handler": "clone_detect"})
        stats_embed = h.get_stats(labels={"handler": "vector_embed"})
        assert stats_clone["count"] == 2
        assert stats_embed["count"] == 1

    def test_empty_stats(self):
        h = Histogram("duration_seconds")
        stats = h.get_stats()
        assert stats["count"] == 0
        assert stats["sum"] == 0.0

    def test_reset(self):
        h = Histogram("duration_seconds")
        h.observe(0.1)
        h.reset()
        stats = h.get_stats()
        assert stats["count"] == 0


# ============================================================
# _percentile 函数测试
# ============================================================


class TestPercentile:
    """_percentile 函数测试。"""

    def test_empty(self):
        assert _percentile([], 0.5) == 0.0

    def test_single_value(self):
        assert _percentile([1.0], 0.5) == 1.0
        assert _percentile([1.0], 0.0) == 1.0
        assert _percentile([1.0], 1.0) == 1.0

    def test_two_values(self):
        assert _percentile([1.0, 2.0], 0.5) == 1.5
        assert _percentile([1.0, 2.0], 0.0) == 1.0
        assert _percentile([1.0, 2.0], 1.0) == 2.0

    def test_multiple_values(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(values, 0.0) == 1.0
        assert _percentile(values, 1.0) == 5.0
        # p50 应该是 3.0
        assert _percentile(values, 0.5) == 3.0

    def test_unsorted_input(self):
        # _percentile 假设输入已排序
        values = [5.0, 1.0, 3.0, 2.0, 4.0]
        sorted_vals = sorted(values)
        assert _percentile(sorted_vals, 0.5) == 3.0


# ============================================================
# 标签格式化测试
# ============================================================


class TestFormatLabels:
    """_format_labels 函数测试。"""

    def test_empty(self):
        assert _format_labels("") == ""

    def test_single_label(self):
        result = _format_labels("status=ok")
        assert 'status="ok"' in result
        assert result.startswith("{")
        assert result.endswith("}")

    def test_multiple_labels(self):
        result = _format_labels("method=GET,status=200")
        assert 'method="GET"' in result
        assert 'status="200"' in result


class TestUnformatLabels:
    """_unformat_labels 函数测试。"""

    def test_empty(self):
        assert _unformat_labels("") == {}

    def test_single_label(self):
        result = _unformat_labels("status=ok")
        assert result == {"status": "ok"}

    def test_multiple_labels(self):
        result = _unformat_labels("method=GET,status=200")
        assert result == {"method": "GET", "status": "200"}


class TestMergeLabels:
    """_merge_labels 函数测试。"""

    def test_empty_base(self):
        assert _merge_labels("", "le=0.1") == "le=0.1"

    def test_non_empty_base(self):
        result = _merge_labels("status=ok", "le=0.1")
        assert "status=ok" in result
        assert "le=0.1" in result


# ============================================================
# 运行时指标采集测试
# ============================================================


class TestGetMemoryInfo:
    """get_memory_info 函数测试。"""

    def test_returns_dict(self):
        mem = get_memory_info()
        assert isinstance(mem, dict)
        assert "rss" in mem
        assert "vms" in mem
        assert "peak" in mem

    def test_values_non_negative(self):
        mem = get_memory_info()
        # rss 可能为 0（平台不支持），但不应为负
        assert mem["rss"] >= 0
        assert mem["vms"] >= 0
        assert mem["peak"] >= 0


class TestGetCpuInfo:
    """get_cpu_info 函数测试。"""

    def test_returns_dict(self):
        cpu = get_cpu_info()
        assert isinstance(cpu, dict)
        assert "user" in cpu
        assert "system" in cpu
        assert "total" in cpu

    def test_total_equals_user_plus_system(self):
        cpu = get_cpu_info()
        assert cpu["total"] == pytest.approx(cpu["user"] + cpu["system"])

    def test_values_non_negative(self):
        cpu = get_cpu_info()
        assert cpu["user"] >= 0.0
        assert cpu["system"] >= 0.0


class TestGetProcessInfo:
    """get_process_info 函数测试。"""

    def test_returns_dict(self):
        info = get_process_info()
        assert isinstance(info, dict)
        assert "pid" in info
        assert "uptime" in info
        assert "thread_count" in info

    def test_pid_is_current_process(self):
        import os
        info = get_process_info()
        assert info["pid"] == os.getpid()

    def test_thread_count_positive(self):
        info = get_process_info()
        assert info["thread_count"] > 0


# ============================================================
# MetricsCollector 测试
# ============================================================


class TestMetricsCollectorRegister:
    """MetricsCollector 注册指标测试。"""

    def test_register_counter(self):
        mc = MetricsCollector()
        c = mc.register_counter("custom_counter", "custom counter")
        assert isinstance(c, Counter)
        assert mc.get_metric("custom_counter") is c

    def test_register_gauge(self):
        mc = MetricsCollector()
        g = mc.register_gauge("custom_gauge", "custom gauge")
        assert isinstance(g, Gauge)
        assert mc.get_metric("custom_gauge") is g

    def test_register_histogram(self):
        mc = MetricsCollector()
        h = mc.register_histogram("custom_hist", "custom histogram")
        assert isinstance(h, Histogram)
        assert mc.get_metric("custom_hist") is h

    def test_builtin_metrics_registered(self):
        mc = MetricsCollector()
        assert mc.get_metric("memory_rss_bytes") is not None
        assert mc.get_metric("cpu_user_seconds") is not None
        assert mc.get_metric("uptime_seconds") is not None
        assert mc.get_metric("requests_total") is not None
        assert mc.get_metric("jobs_pending") is not None

    def test_get_metric_not_found(self):
        mc = MetricsCollector()
        assert mc.get_metric("nonexistent") is None

    def test_list_metrics(self):
        mc = MetricsCollector()
        mc.register_counter("custom_counter")
        mc.register_gauge("custom_gauge")
        mc.register_histogram("custom_hist")
        listed = mc.list_metrics()
        assert "custom_counter" in listed["counters"]
        assert "custom_gauge" in listed["gauges"]
        assert "custom_hist" in listed["histograms"]


class TestMetricsCollectorRecord:
    """MetricsCollector 记录指标测试。"""

    def test_increment(self):
        mc = MetricsCollector()
        mc.register_counter("test_counter")
        mc.increment("test_counter")
        assert mc.get_metric("test_counter").get() == 1.0

    def test_increment_with_labels(self):
        mc = MetricsCollector()
        mc.register_counter("test_counter", labels=["status"])
        mc.increment("test_counter", labels={"status": "ok"})
        mc.increment("test_counter", labels={"status": "ok"})
        mc.increment("test_counter", labels={"status": "error"})
        c = mc.get_metric("test_counter")
        assert c.get(labels={"status": "ok"}) == 2.0
        assert c.get(labels={"status": "error"}) == 1.0

    def test_increment_nonexistent(self):
        mc = MetricsCollector()
        # 不应抛异常
        mc.increment("nonexistent")

    def test_set_gauge(self):
        mc = MetricsCollector()
        mc.register_gauge("test_gauge")
        mc.set_gauge("test_gauge", 42.0)
        assert mc.get_metric("test_gauge").get() == 42.0

    def test_inc_gauge(self):
        mc = MetricsCollector()
        mc.register_gauge("test_gauge")
        mc.set_gauge("test_gauge", 100.0)
        mc.inc_gauge("test_gauge", 10.0)
        assert mc.get_metric("test_gauge").get() == 110.0

    def test_dec_gauge(self):
        mc = MetricsCollector()
        mc.register_gauge("test_gauge")
        mc.set_gauge("test_gauge", 100.0)
        mc.dec_gauge("test_gauge", 30.0)
        assert mc.get_metric("test_gauge").get() == 70.0

    def test_observe(self):
        mc = MetricsCollector()
        mc.register_histogram("test_hist")
        mc.observe("test_hist", 0.5)
        stats = mc.get_metric("test_hist").get_stats()
        assert stats["count"] == 1
        assert stats["sum"] == 0.5


class TestMetricsCollectorRuntime:
    """MetricsCollector 运行时指标收集测试。"""

    def test_collect_runtime_metrics(self):
        mc = MetricsCollector()
        mc.collect_runtime_metrics()
        # 运行时指标应该被更新
        assert mc.get_metric("memory_rss_bytes").get() >= 0
        assert mc.get_metric("cpu_user_seconds").get() >= 0.0
        assert mc.get_metric("uptime_seconds").get() >= 0.0
        assert mc.get_metric("thread_count").get() >= 1

    def test_uptime_increases(self):
        mc = MetricsCollector()
        time.sleep(0.1)
        mc.collect_runtime_metrics()
        assert mc.uptime >= 0.1

    def test_start_time(self):
        mc = MetricsCollector()
        assert mc.start_time > 0
        assert mc.start_time <= time.time()

    def test_uptime_property(self):
        mc = MetricsCollector()
        before = mc.uptime
        time.sleep(0.05)
        after = mc.uptime
        assert after > before


class TestMetricsCollectorExport:
    """MetricsCollector 导出测试。"""

    def test_to_prometheus_returns_string(self):
        mc = MetricsCollector()
        text = mc.to_prometheus()
        assert isinstance(text, str)
        assert len(text) > 0

    def test_to_prometheus_has_help(self):
        mc = MetricsCollector()
        text = mc.to_prometheus()
        assert "# HELP" in text

    def test_to_prometheus_has_type(self):
        mc = MetricsCollector()
        text = mc.to_prometheus()
        assert "# TYPE" in text

    def test_to_prometheus_has_builtin_metrics(self):
        mc = MetricsCollector()
        text = mc.to_prometheus()
        assert "memory_rss_bytes" in text
        assert "cpu_user_seconds" in text
        assert "uptime_seconds" in text

    def test_to_prometheus_includes_counter_values(self):
        mc = MetricsCollector()
        mc.register_counter("custom_total", "custom counter")
        mc.increment("custom_total", 5.0)
        text = mc.to_prometheus()
        assert "custom_total" in text
        assert "5" in text

    def test_to_prometheus_includes_gauge_values(self):
        mc = MetricsCollector()
        mc.register_gauge("custom_gauge", "custom gauge")
        mc.set_gauge("custom_gauge", 123.0)
        text = mc.to_prometheus()
        assert "custom_gauge" in text
        assert "123" in text

    def test_to_prometheus_includes_histogram(self):
        mc = MetricsCollector()
        mc.register_histogram("custom_hist", "custom histogram")
        mc.observe("custom_hist", 0.5)
        text = mc.to_prometheus()
        assert "custom_hist" in text
        assert "custom_hist_bucket" in text
        assert "custom_hist_sum" in text
        assert "custom_hist_count" in text

    def test_to_prometheus_counter_type(self):
        mc = MetricsCollector()
        mc.register_counter("my_counter", "test")
        text = mc.to_prometheus()
        assert "# TYPE my_counter counter" in text

    def test_to_prometheus_gauge_type(self):
        mc = MetricsCollector()
        mc.register_gauge("my_gauge", "test")
        text = mc.to_prometheus()
        assert "# TYPE my_gauge gauge" in text

    def test_to_prometheus_histogram_type(self):
        mc = MetricsCollector()
        mc.register_histogram("my_hist", "test")
        text = mc.to_prometheus()
        assert "# TYPE my_hist histogram" in text

    def test_to_json_returns_dict(self):
        mc = MetricsCollector()
        data = mc.to_json()
        assert isinstance(data, dict)
        assert "counters" in data
        assert "gauges" in data
        assert "histograms" in data

    def test_to_json_has_timestamp(self):
        mc = MetricsCollector()
        data = mc.to_json()
        assert "timestamp" in data
        assert data["timestamp"] > 0

    def test_to_json_has_uptime(self):
        mc = MetricsCollector()
        data = mc.to_json()
        assert "uptime" in data
        assert data["uptime"] >= 0.0

    def test_to_json_includes_counters(self):
        mc = MetricsCollector()
        mc.register_counter("custom_total", "custom counter")
        mc.increment("custom_total", 3.0)
        data = mc.to_json()
        assert "custom_total" in data["counters"]
        assert data["counters"]["custom_total"]["values"][""] == 3.0

    def test_to_json_includes_gauges(self):
        mc = MetricsCollector()
        mc.register_gauge("custom_gauge", "custom gauge")
        mc.set_gauge("custom_gauge", 99.0)
        data = mc.to_json()
        assert "custom_gauge" in data["gauges"]
        assert data["gauges"]["custom_gauge"]["values"][""] == 99.0

    def test_to_json_includes_histograms(self):
        mc = MetricsCollector()
        mc.register_histogram("custom_hist", "custom histogram")
        mc.observe("custom_hist", 0.5)
        data = mc.to_json()
        assert "custom_hist" in data["histograms"]
        assert "" in data["histograms"]["custom_hist"]["stats"]

    def test_to_json_string(self):
        mc = MetricsCollector()
        s = mc.to_json_string()
        assert isinstance(s, str)
        # 应该是合法的 JSON
        parsed = json.loads(s)
        assert "counters" in parsed


class TestMetricsCollectorReset:
    """MetricsCollector.reset() 测试。"""

    def test_reset_clears_counters(self):
        mc = MetricsCollector()
        mc.increment("requests_total")
        mc.reset()
        assert mc.get_metric("requests_total").get() == 0.0

    def test_reset_clears_gauges(self):
        mc = MetricsCollector()
        mc.set_gauge("memory_rss_bytes", 1000)
        mc.reset()
        assert mc.get_metric("memory_rss_bytes").get() == 0.0

    def test_reset_clears_histograms(self):
        mc = MetricsCollector()
        mc.observe("job_duration_seconds", 0.5)
        mc.reset()
        stats = mc.get_metric("job_duration_seconds").get_stats()
        assert stats["count"] == 0

    def test_reset_resets_start_time(self):
        mc = MetricsCollector()
        old_start = mc.start_time
        time.sleep(0.01)
        mc.reset()
        assert mc.start_time >= old_start


# ============================================================
# 全局单例测试
# ============================================================


class TestGlobalCollector:
    """全局 MetricsCollector 单例测试。"""

    def test_get_metrics_collector_returns_instance(self):
        reset_metrics_collector()
        mc = get_metrics_collector()
        assert isinstance(mc, MetricsCollector)

    def test_get_metrics_collector_singleton(self):
        reset_metrics_collector()
        mc1 = get_metrics_collector()
        mc2 = get_metrics_collector()
        assert mc1 is mc2

    def test_reset_metrics_collector(self):
        mc1 = get_metrics_collector()
        reset_metrics_collector()
        mc2 = get_metrics_collector()
        assert mc1 is not mc2

    def test_global_collector_has_builtin_metrics(self):
        reset_metrics_collector()
        mc = get_metrics_collector()
        assert mc.get_metric("memory_rss_bytes") is not None
        assert mc.get_metric("requests_total") is not None
        assert mc.get_metric("jobs_pending") is not None

    def test_global_collector_thread_safe(self):
        reset_metrics_collector()
        results = []

        def worker():
            mc = get_metrics_collector()
            results.append(id(mc))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 所有线程应该拿到同一个实例
        assert len(set(results)) == 1
