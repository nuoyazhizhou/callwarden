"""P28: worker 内存安全算法单元测试。

测试 _detect_optimal_workers 在不同数据规模和内存场景下的行为，
确保不会再次因算法缺陷把宿主机搞崩。

背景：原算法 worker 预算 150MB（只算 parser 加载，未算 AST 峰值）+ 保留 2GB，
4 worker 模式下主进程内存从 9GB 涨到 14.2GB，把 32GB 宿主机搞崩。
P28 修复后：预算 800MB + 保留 4GB + 新增数据规模因子（file_count 维度）。
"""
import os
import unittest
from unittest.mock import patch

from callwarden.db.db_build import (
    _detect_optimal_workers,
    _WORKER_MEM_BUDGET_MB,
    _HOST_RESERVED_MEM_MB,
    _MAX_WORKERS_CAP,
    _MIN_WORKERS,
    _SCALE_LARGE_FILE_THRESHOLD,
    _SCALE_VERY_LARGE_FILE_THRESHOLD,
    _SCALE_HUGE_FILE_THRESHOLD,
)


class TestDetectOptimalWorkersConstants(unittest.TestCase):
    """P28 修复后的常量值验证"""

    def test_worker_mem_budget_raised(self):
        """worker 预算从 150MB 提到 800MB（含 AST 峰值）"""
        self.assertEqual(_WORKER_MEM_BUDGET_MB, 800)

    def test_host_reserved_mem_raised(self):
        """保留内存从 2GB 提到 4GB"""
        self.assertEqual(_HOST_RESERVED_MEM_MB, 4096)

    def test_file_count_thresholds(self):
        """数据规模阈值常量"""
        self.assertEqual(_SCALE_LARGE_FILE_THRESHOLD, 10000)
        self.assertEqual(_SCALE_VERY_LARGE_FILE_THRESHOLD, 50000)
        self.assertEqual(_SCALE_HUGE_FILE_THRESHOLD, 200000)


class TestDetectOptimalWorkersScaleFactor(unittest.TestCase):
    """数据规模因子（P28 新增维度）测试"""

    def _detect(self, file_count, available_mb=30000.0, cpu_count=8):
        """统一 mock 入口：内存充足 + 指定 CPU 核数，便于测规模因子"""
        with patch("callwarden.db.db_build._get_available_memory_mb",
                   return_value=available_mb), \
             patch.object(os, "cpu_count", return_value=cpu_count):
            return _detect_optimal_workers(file_count)

    def test_file_count_zero_backward_compatible(self):
        """file_count=0（默认）应向后兼容，不启用规模因子"""
        workers = self._detect(0)
        # 8 核 → cpu_based=7, mem=32GB→32, scale=8 → min(7,32,8,8)=7
        self.assertEqual(workers, 7)

    def test_small_file_count_no_scale_limit(self):
        """< 10K 文件不启用规模因子"""
        workers = self._detect(5000)
        self.assertGreaterEqual(workers, 4)
        self.assertLessEqual(workers, _MAX_WORKERS_CAP)

    def test_large_file_count_caps_at_2(self):
        """>= 10K 文件，scale_cap=2，worker 数最多 2"""
        workers = self._detect(_SCALE_LARGE_FILE_THRESHOLD, cpu_count=16)
        self.assertLessEqual(workers, 2)
        self.assertGreaterEqual(workers, _MIN_WORKERS)

    def test_very_large_file_count_caps_at_1(self):
        """>= 50K 文件，scale_cap=1，worker 数强制为 1"""
        workers = self._detect(_SCALE_VERY_LARGE_FILE_THRESHOLD, cpu_count=32)
        self.assertEqual(workers, 1)

    def test_huge_file_count_caps_at_1(self):
        """>= 200K 文件，scale_cap=1，worker 数强制为 1"""
        workers = self._detect(_SCALE_HUGE_FILE_THRESHOLD, available_mb=64000.0,
                                cpu_count=64)
        self.assertEqual(workers, 1)

    def test_threshold_boundary_large(self):
        """10K 阈值边界：阈值-1 不限制，阈值 限制为 2"""
        w_below = self._detect(_SCALE_LARGE_FILE_THRESHOLD - 1)
        w_at = self._detect(_SCALE_LARGE_FILE_THRESHOLD)
        self.assertGreater(w_below, w_at)
        self.assertLessEqual(w_at, 2)

    def test_threshold_boundary_very_large(self):
        """50K 阈值边界：阈值-1 最多 2，阈值 强制 1"""
        w_below = self._detect(_SCALE_VERY_LARGE_FILE_THRESHOLD - 1, cpu_count=32)
        w_at = self._detect(_SCALE_VERY_LARGE_FILE_THRESHOLD, cpu_count=32)
        self.assertGreater(w_below, w_at)
        self.assertEqual(w_at, 1)

    def test_scale_cap_dominates_over_memory(self):
        """数据规模因子优先于内存维度（即使内存充足也降 worker）"""
        # 64GB 可用 + 64 核 + 200K 文件，仍应降到 1
        workers = self._detect(_SCALE_HUGE_FILE_THRESHOLD, available_mb=64000.0,
                                cpu_count=64)
        self.assertEqual(workers, 1)


class TestDetectOptimalWorkersMemory(unittest.TestCase):
    """内存维度测试"""

    def _detect(self, available_mb, file_count=100, cpu_count=8):
        with patch("callwarden.db.db_build._get_available_memory_mb",
                   return_value=available_mb), \
             patch.object(os, "cpu_count", return_value=cpu_count):
            return _detect_optimal_workers(file_count)

    def test_low_memory_caps_at_1(self):
        """可用 5GB，减 4GB 保留剩 1GB，按 800MB/worker 算 = 1"""
        workers = self._detect(5000.0)
        self.assertEqual(workers, 1)

    def test_very_low_memory_caps_at_1(self):
        """可用 < 保留量，worker 数强制 1"""
        workers = self._detect(3000.0)  # 3GB < 4GB 保留
        self.assertEqual(workers, _MIN_WORKERS)

    def test_zero_available_memory(self):
        """可用内存为 0"""
        workers = self._detect(0.0)
        self.assertEqual(workers, _MIN_WORKERS)

    def test_memory_none_falls_back_to_cpu(self):
        """无法检测内存时（返回 None），按 CPU 维度计算"""
        with patch("callwarden.db.db_build._get_available_memory_mb",
                   return_value=None), \
             patch.object(os, "cpu_count", return_value=8):
            workers = _detect_optimal_workers(1000)
            # 8 核 → cpu_based=7, mem=8（默认）, scale=8 → min(7,8,8,8)=7
            self.assertEqual(workers, 7)


class TestP28Regression(unittest.TestCase):
    """P28 回归测试：模拟原 4 worker 崩溃场景"""

    def test_original_crash_scenario_now_returns_1(self):
        """模拟原崩溃场景：32GB 机器空闲 13GB + 150K 文件

        原算法（4 worker 崩溃）：
        - worker 预算 150MB → 13GB-2GB=11GB / 150 = 73 → cap 到 8
        - 4 worker 启动后主进程结果持有 1.5M 符号 → 10-14GB → 崩溃

        P28 修复后：
        - 150K 文件 >= 50K 阈值 → scale_cap=1
        - 即使内存维度算出更多，scale_cap 强制降到 1
        """
        with patch("callwarden.db.db_build._get_available_memory_mb",
                   return_value=13000.0), \
             patch.object(os, "cpu_count", return_value=16):
            workers = _detect_optimal_workers(150000)  # 150K 文件
            self.assertEqual(workers, 1,
                             "150K 文件应强制 1 worker，避免主进程结果持有内存爆炸")

    def test_1m_symbols_scenario_safe(self):
        """1M 符号场景（约 100K 文件）应强制 1 worker"""
        with patch("callwarden.db.db_build._get_available_memory_mb",
                   return_value=13000.0), \
             patch.object(os, "cpu_count", return_value=16):
            workers = _detect_optimal_workers(100000)  # 100K 文件
            self.assertEqual(workers, 1,
                             "100K 文件应强制 1 worker")

    def test_moderate_scenario_still_parallel(self):
        """中等规模（5K 文件）仍可并行，不被过度限制"""
        with patch("callwarden.db.db_build._get_available_memory_mb",
                   return_value=16000.0), \
             patch.object(os, "cpu_count", return_value=8):
            workers = _detect_optimal_workers(5000)
            # 5K < 10K 阈值，无规模限制；8 核 → cpu_based=7
            # 16GB-4GB=12GB / 800MB = 15 → cap 到 8
            # min(7, 8, 8, 8) = 7
            self.assertGreaterEqual(workers, 4,
                                    "5K 文件 + 16GB 可用应允许 4+ worker 并行")


if __name__ == "__main__":
    unittest.main()
