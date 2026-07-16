"""testcode 分批合并入库基线测试脚本（内存安全版）。

问题：build_full_graph 对 150 万符号需要 10-14GB 内存（一次性加载所有结果），
32GB 机器空闲仅 13GB，会 OOM。

方案：分批 build_directory，每批 50 个项目（约 12K 文件），内存 < 1GB。
最后统一做 call_resolve（从 DB 加载，分批处理）。

约束：只读 testcode，不增删改任何文件。

P28 验证模式：不强制 CW_MP_WORKERS=1，让 _detect_optimal_workers(file_count)
动态决策。加算法预检 + 内存监控，验证修复后的算法在大场景下安全。

用法：
  python tests/_perf_testcode_baseline.py [--refresh] [--batch-size N] [--algo-verify]
    --refresh      重新构建 DB（删除旧 DB 全量重建）
    --batch-size N 每批项目数（默认 50）
    --algo-verify  算法预检模式：只打印各 file_count 下的 worker 决策，不构建
    不带参数       使用已有 DB 做查询性能测试
"""
from __future__ import annotations
import os
import sys
import time
import json
import shutil
import ctypes
import threading
from typing import Optional

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

# P28 验证：不强制 CW_MP_WORKERS=1，让 _detect_optimal_workers(file_count) 动态决策
# 原强制 1 worker 会绕过算法，无法验证修复效果
# 如需回退到强制 1 worker，设置环境变量 CW_MP_WORKERS=1 即可

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_build import (
    _detect_optimal_workers,
    _get_available_memory_mb,
    _WORKER_MEM_BUDGET_MB,
    _HOST_RESERVED_MEM_MB,
    _SCALE_LARGE_FILE_THRESHOLD,
    _SCALE_VERY_LARGE_FILE_THRESHOLD,
    _SCALE_HUGE_FILE_THRESHOLD,
)
from callwarden.config import scan_subprojects

TESTCODE_ROOT = os.path.join(_PKG_ROOT, "testcode", "repos")
DB_DIR = os.path.join(_PKG_ROOT, "tests", "_testcode_db")
DB_PATH = os.path.join(DB_DIR, "callwarden.db")
BASELINE_REPORT = os.path.join(_PKG_ROOT, "tests", "_perf_testcode_baseline.json")
QUERY_REPORT = os.path.join(_PKG_ROOT, "tests", "_perf_testcode_queries.json")

_peak_memory_mb = 0
_peak_tree_memory_mb = 0
# 内存安全阈值：超过时主动 GC + 警告（不终止，因 build_directory 已分批控制）
_MEM_WARN_MB = 4000   # 4GB 警告
_MEM_CRITICAL_MB = 6000  # 6GB 临界（接近宿主机安全上限）
_MEM_SAMPLE_INTERVAL_SEC = 1.0


def _format_mb(value: Optional[int]) -> str:
    """格式化内存值，避免采样失败时误显示为 0MB。"""
    return f"{value}MB" if value is not None else "N/A"


def _get_windows_process_rss_mb(pid: int) -> Optional[int]:
    """用 Windows API 获取指定进程 WorkingSetSize。"""
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi

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

        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int

        close_handle = False
        if pid == os.getpid():
            handle = kernel32.GetCurrentProcess()
        else:
            # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ
            handle = kernel32.OpenProcess(0x1000 | 0x0010, 0, pid)
            close_handle = True
        if not handle:
            return None

        try:
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            if not ok:
                return None
            return int(counters.WorkingSetSize // (1024 * 1024))
        finally:
            if close_handle:
                kernel32.CloseHandle(handle)
    except Exception:
        return None


def _get_linux_process_rss_mb(pid: int) -> Optional[int]:
    """从 /proc 读取指定进程 RSS。"""
    try:
        with open(f"/proc/{pid}/statm", "r", encoding="utf-8") as f:
            parts = f.read().split()
        if len(parts) < 2:
            return None
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(int(parts[1]) * page_size // (1024 * 1024))
    except Exception:
        return None


def _get_process_rss_mb(pid: Optional[int] = None) -> Optional[int]:
    """获取指定进程 RSS（MB），优先 psutil，缺失时用平台原生 API。"""
    target_pid = pid or os.getpid()
    try:
        import psutil
        return int(psutil.Process(target_pid).memory_info().rss // (1024 * 1024))
    except ImportError:
        pass
    except Exception:
        return None

    if sys.platform == "win32":
        return _get_windows_process_rss_mb(target_pid)
    if sys.platform.startswith("linux"):
        return _get_linux_process_rss_mb(target_pid)
    return None


def _get_rss_mb() -> Optional[int]:
    """获取当前进程 RSS（MB）。"""
    global _peak_memory_mb
    rss = _get_process_rss_mb(os.getpid())
    if rss is not None and rss > _peak_memory_mb:
        _peak_memory_mb = rss
    return rss


def _get_child_pids_windows(root_pid: int) -> list[int]:
    """枚举 Windows 进程树子进程 PID。"""
    try:
        kernel32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_ulong),
                ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = ctypes.c_int
        kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == INVALID_HANDLE_VALUE:
            return []

        try:
            by_parent: dict[int, list[int]] = {}
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                pid = int(entry.th32ProcessID)
                parent = int(entry.th32ParentProcessID)
                by_parent.setdefault(parent, []).append(pid)
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))

            child_pids: list[int] = []
            stack = list(by_parent.get(root_pid, []))
            seen = set()
            while stack:
                pid = stack.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                child_pids.append(pid)
                stack.extend(by_parent.get(pid, []))
            return child_pids
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        return []


def _get_child_pids_linux(root_pid: int) -> list[int]:
    """枚举 Linux /proc 进程树子进程 PID。"""
    try:
        by_parent: dict[int, list[int]] = {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
                    parent = None
                    for line in f:
                        if line.startswith("PPid:"):
                            parent = int(line.split()[1])
                            break
                if parent is not None:
                    by_parent.setdefault(parent, []).append(pid)
            except Exception:
                continue

        child_pids: list[int] = []
        stack = list(by_parent.get(root_pid, []))
        seen = set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            child_pids.append(pid)
            stack.extend(by_parent.get(pid, []))
        return child_pids
    except Exception:
        return []


def _get_process_tree_rss_mb() -> Optional[int]:
    """获取当前进程及子进程 RSS 总和，用于观察 ProcessPool worker 峰值。"""
    global _peak_tree_memory_mb
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        total = proc.memory_info().rss
        for child in proc.children(recursive=True):
            try:
                total += child.memory_info().rss
            except Exception:
                continue
        tree_rss = int(total // (1024 * 1024))
    except ImportError:
        if sys.platform == "win32":
            child_pids = _get_child_pids_windows(os.getpid())
        elif sys.platform.startswith("linux"):
            child_pids = _get_child_pids_linux(os.getpid())
        else:
            child_pids = []

        values = []
        for pid in [os.getpid()] + child_pids:
            rss = _get_process_rss_mb(pid)
            if rss is not None:
                values.append(rss)
        if not values:
            return None
        tree_rss = sum(values)
    except Exception:
        return None

    if tree_rss > _peak_tree_memory_mb:
        _peak_tree_memory_mb = tree_rss
    return tree_rss


class _MemoryMonitor:
    """后台采样 batch 期间进程树峰值，避免只在 worker 退出后看内存。"""

    def __init__(self, interval_sec: float = _MEM_SAMPLE_INTERVAL_SEC):
        self.interval_sec = interval_sec
        self.peak_rss_mb: Optional[int] = None
        self.peak_tree_rss_mb: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _sample_once(self) -> None:
        rss = _get_rss_mb()
        tree_rss = _get_process_tree_rss_mb()
        if rss is not None:
            self.peak_rss_mb = max(self.peak_rss_mb or 0, rss)
        if tree_rss is not None:
            self.peak_tree_rss_mb = max(self.peak_tree_rss_mb or 0, tree_rss)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_sec):
            self._sample_once()

    def __enter__(self):
        self._sample_once()
        self._thread = threading.Thread(target=self._run, name="cw-mem-monitor", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._sample_once()
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._sample_once()


def verify_algorithm():
    """P28 算法预检：打印各 file_count 下的 worker 决策 + 当前系统资源。

    验证 _detect_optimal_workers 在不同数据规模下的行为，确认大场景下安全。
    """
    print("=" * 60)
    print("[P28 算法预检] _detect_optimal_workers 行为验证")
    print("=" * 60)

    # 当前系统资源
    avail = _get_available_memory_mb()
    cpu = os.cpu_count() or 4
    print(f"\n系统资源：")
    print(f"  CPU 核心数:        {cpu}")
    print(f"  可用内存:          {avail:.0f} MB" if avail else "  可用内存:          无法检测")
    print(f"  当前进程 RSS:      {_format_mb(_get_rss_mb())}")
    print(f"  进程树 RSS:        {_format_mb(_get_process_tree_rss_mb())}")
    print(f"\n算法参数：")
    print(f"  worker 预算:       {_WORKER_MEM_BUDGET_MB} MB/worker（含 AST 峰值）")
    print(f"  保留内存:          {_HOST_RESERVED_MEM_MB} MB（给宿主机）")
    print(f"  规模阈值:          {_SCALE_LARGE_FILE_THRESHOLD} / "
          f"{_SCALE_VERY_LARGE_FILE_THRESHOLD} / "
          f"{_SCALE_HUGE_FILE_THRESHOLD} 文件")

    # 各 file_count 下的 worker 决策
    print(f"\n各 file_count 下的 worker 决策：")
    print(f"  {'file_count':>12} | {'workers':>8} | {'scale_cap':>10} | 说明")
    print(f"  {'-'*12} | {'-'*8} | {'-'*10} | {'-'*30}")

    test_cases = [
        (0, "默认（不启用规模因子）"),
        (1000, "小项目"),
        (5000, "中等项目"),
        (_SCALE_LARGE_FILE_THRESHOLD, "10K 阈值（scale_cap=2）"),
        (_SCALE_LARGE_FILE_THRESHOLD + 1, "刚过 10K 阈值"),
        (20000, "20K 文件"),
        (_SCALE_VERY_LARGE_FILE_THRESHOLD, "50K 阈值（scale_cap=1）"),
        (_SCALE_VERY_LARGE_FILE_THRESHOLD + 1, "刚过 50K 阈值"),
        (100000, "100K 文件（接近 1M 符号）"),
        (_SCALE_HUGE_FILE_THRESHOLD, "200K 阈值（强制 1 worker）"),
        (150000, "150K 文件（原崩溃场景）"),
    ]

    for fc, desc in test_cases:
        workers = _detect_optimal_workers(fc)
        if fc >= _SCALE_HUGE_FILE_THRESHOLD:
            cap = "1 (强制)"
        elif fc >= _SCALE_VERY_LARGE_FILE_THRESHOLD:
            cap = "1"
        elif fc >= _SCALE_LARGE_FILE_THRESHOLD:
            cap = "2"
        else:
            cap = "无限制"
        print(f"  {fc:>12,} | {workers:>8} | {cap:>10} | {desc}")

    # 回归验证：原崩溃场景必须返回 1
    print(f"\n[P28 回归验证] 原崩溃场景（150K 文件 + 32GB 机器空闲 13GB）：")
    crash_workers = _detect_optimal_workers(150000)
    status = "PASS" if crash_workers == 1 else "FAIL"
    print(f"  _detect_optimal_workers(150000) = {crash_workers}  [{status}]")
    if crash_workers == 1:
        print(f"  原崩溃场景（4 worker）现已修复，强制 1 worker")
    else:
        print(f"  警告：算法未正确限制大场景 worker 数！")

    print("=" * 60)
    print()


def build_baseline_db(batch_size=50):
    """分批构建基线 DB：每批 batch_size 个项目。

    约束：只读 testcode，不增删改任何文件。
    P28 验证：不强制 CW_MP_WORKERS，让 _detect_optimal_workers 动态决策。
    """
    print(f"[baseline] 分批构建 testcode/repos 基线 DB")
    print(f"  root:       {TESTCODE_ROOT}")
    print(f"  DB:         {DB_PATH}")
    print(f"  batch_size: {batch_size} 项目/批")
    print(f"  workers:    动态（P28 算法决策，不强制 1）")
    print(f"  [mem] 初始 RSS: {_format_mb(_get_rss_mb())} "
          f"tree={_format_mb(_get_process_tree_rss_mb())}")
    print()

    # 清理旧 DB
    if os.path.exists(DB_DIR):
        shutil.rmtree(DB_DIR)
    os.makedirs(DB_DIR, exist_ok=True)

    # 扫描子项目
    print(f"[1/4] 扫描子项目...")
    t0 = time.perf_counter()
    projects = scan_subprojects(TESTCODE_ROOT, max_depth=2)
    print(f"  发现 {len(projects)} 个子项目 ({time.perf_counter() - t0:.1f}s)")
    by_lang = {}
    for p in projects:
        lang = p.get("lang", "unknown")
        by_lang[lang] = by_lang.get(lang, 0) + 1
    print(f"  语言分布: {by_lang}")
    print()

    # 创建 DB + 注册 workspace
    print(f"[2/4] 初始化 DB...")
    db = CodeGraphDB(db_path=DB_PATH, workspace_root=TESTCODE_ROOT)
    ws_id = db.register_workspace("testcode_repos_baseline", TESTCODE_ROOT,
                                   "testcode/repos 合并入库基线")
    db.set_active_workspace(ws_id)
    print(f"  workspace_id: {ws_id}")
    print()

    # 分批构建
    print(f"[3/4] 分批构建（每批 {batch_size} 个项目，动态 worker）...")
    total_batches = (len(projects) + batch_size - 1) // batch_size
    t_total = time.perf_counter()
    total_files = 0
    total_symbols = 0
    # P28: 记录每批的 worker 决策和内存，用于验证算法
    batch_records = []

    for batch_idx in range(total_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, len(projects))
        batch_projects = projects[start:end]

        # 收集这批项目的根目录
        batch_dirs = set()
        for p in batch_projects:
            root = p.get("root", "")
            if root and os.path.isdir(root):
                batch_dirs.add(root)

        if not batch_dirs:
            continue

        # P28: 预估本批文件数（实际扫描后可能不同，用于算法决策参考）
        # build_directory 内部会扫描每个目录的文件数并传给 _detect_optimal_workers
        # 这里只记录动态算法的决策结果（从 build_directory 的 cprint 输出捕获）
        t_batch = time.perf_counter()
        # 对每个目录调用 build_directory（增量追加到同一个 DB）
        with _MemoryMonitor() as mem:
            for d in sorted(batch_dirs):
                try:
                    # build_directory 是相对路径或绝对路径
                    rel = os.path.relpath(d, TESTCODE_ROOT)
                    db.build_directory(rel)
                except Exception as e:
                    print(f"    ERROR {d}: {e}", flush=True)

        batch_elapsed = time.perf_counter() - t_batch
        stats = db.get_stats()
        batch_files = stats.get("total_files", 0)
        batch_symbols = stats.get("total_symbols", 0)
        rss = _get_rss_mb()
        tree_rss = _get_process_tree_rss_mb()
        peak_tree_rss = mem.peak_tree_rss_mb if mem.peak_tree_rss_mb is not None else tree_rss
        avail_after = _get_available_memory_mb()

        total_files = batch_files
        total_symbols = batch_symbols

        # P28: 推算本批新增文件数（用于验证算法决策）
        if batch_records:
            prev_files = batch_records[-1]["cumulative_files"]
        else:
            prev_files = 0
        batch_new_files = batch_files - prev_files

        # 算法决策验证：对本批 file_count 调用 _detect_optimal_workers
        algo_workers = _detect_optimal_workers(batch_new_files)
        avail_str = f"{avail_after:.0f}MB" if avail_after else "N/A"
        print(f"  batch {batch_idx+1}/{total_batches}: projects {start+1}-{end} "
              f"new_files={batch_new_files:,} cum_files={batch_files:,} "
              f"sym={batch_symbols:,} "
              f"{batch_elapsed:.1f}s RSS={_format_mb(rss)} "
              f"treeRSS={_format_mb(tree_rss)} peakTree={_format_mb(peak_tree_rss)} "
              f"avail={avail_str} "
              f"algo_workers({batch_new_files})={algo_workers}", flush=True)

        batch_records.append({
            "batch": batch_idx + 1,
            "new_files": batch_new_files,
            "cumulative_files": batch_files,
            "symbols": batch_symbols,
            "elapsed_sec": round(batch_elapsed, 2),
            "rss_mb": rss,
            "tree_rss_mb": tree_rss,
            "peak_tree_rss_mb": peak_tree_rss,
            "avail_mem_mb": round(avail_after, 0) if avail_after else None,
            "algo_workers": algo_workers,
        })

        # 内存安全检查：超过警告阈值，强制 GC
        warn_mem = peak_tree_rss if peak_tree_rss is not None else rss
        if warn_mem is not None and warn_mem > _MEM_WARN_MB:
            import gc
            gc.collect()
            rss_after = _get_rss_mb()
            tree_after = _get_process_tree_rss_mb()
            print(f"    [mem] GC: RSS {_format_mb(rss)} -> {_format_mb(rss_after)}, "
                  f"tree {_format_mb(tree_rss)} -> {_format_mb(tree_after)} "
                  f"(超 {_MEM_WARN_MB}MB 警告阈值)", flush=True)
            # 临界阈值：打印严重警告（不终止，因 build_directory 已分批隔离）
            critical_mem = tree_after if tree_after is not None else rss_after
            if critical_mem is not None and critical_mem > _MEM_CRITICAL_MB:
                print(f"    [mem] CRITICAL: RSS {critical_mem}MB 超 {_MEM_CRITICAL_MB}MB 临界！"
                      f"宿主机可能风险，建议减少 batch_size", flush=True)

    total_elapsed = time.perf_counter() - t_total

    # 最终统计
    stats = db.get_stats()
    timings = getattr(db, "_stage_timings", {}) or {}
    db_size = os.path.getsize(DB_PATH) / (1024 * 1024)
    final_rss = _get_rss_mb()
    final_tree_rss = _get_process_tree_rss_mb()

    print(f"\n  构建完成: {total_elapsed:.1f}s ({total_elapsed/60:.1f}min)")
    print(f"  [mem] 最终 RSS: {_format_mb(final_rss)} (peak: {_format_mb(_peak_memory_mb)})")
    print(f"  [mem] 最终 tree RSS: {_format_mb(final_tree_rss)} "
          f"(peak: {_format_mb(_peak_tree_memory_mb)})")
    print(f"  DB size:  {db_size:.2f} MB")
    print(f"  symbols:  {stats.get('total_symbols', 0):,}")
    print(f"  calls:    {stats.get('total_calls', 0):,}")
    print(f"  files:    {stats.get('total_files', 0):,}")
    print()

    # P28 验证总结：算法决策分布 + 内存峰值
    if batch_records:
        max_rss = max((r["rss_mb"] or 0) for r in batch_records)
        max_tree_rss = max((r["peak_tree_rss_mb"] or 0) for r in batch_records)
        max_new = max(r["new_files"] for r in batch_records)
        workers_dist = {}
        for r in batch_records:
            w = r["algo_workers"]
            workers_dist[w] = workers_dist.get(w, 0) + 1
        print(f"[P28 算法验证总结]")
        print(f"  最大单批文件数:    {max_new:,}")
        print(f"  最大 RSS:          {_format_mb(max_rss)} (峰值 {_format_mb(_peak_memory_mb)})")
        print(f"  最大进程树 RSS:    {_format_mb(max_tree_rss)} "
              f"(峰值 {_format_mb(_peak_tree_memory_mb)})")
        print(f"  算法决策分布:      {workers_dist}")
        peak_for_check = _peak_tree_memory_mb or _peak_memory_mb
        print(f"  内存安全:          {'PASS' if peak_for_check < _MEM_CRITICAL_MB else 'WARN'} "
              f"(进程树峰值 {_format_mb(peak_for_check)} / 临界 {_MEM_CRITICAL_MB}MB)")
        print()

    db.close()

    # 保存基线报告
    report = {
        "label": "testcode_repos_baseline",
        "root": TESTCODE_ROOT,
        "db_path": DB_PATH,
        "db_size_mb": round(db_size, 2),
        "build_elapsed_sec": round(total_elapsed, 2),
        "peak_memory_mb": _peak_memory_mb,
        "stats": {
            "symbols": stats.get("total_symbols", 0),
            "calls": stats.get("total_calls", 0),
            "files": stats.get("total_files", 0),
        },
        "subprojects": {
            "total": len(projects),
            "by_lang": by_lang,
        },
        "batch_size": batch_size,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "p28_verification": {
            "batch_records": batch_records,
            "peak_memory_mb": _peak_memory_mb,
            "mem_critical_threshold_mb": _MEM_CRITICAL_MB,
            "max_batch_files": max((r["new_files"] for r in batch_records), default=0),
        },
    }

    with open(BASELINE_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[4/4] 基线报告已保存: {BASELINE_REPORT}")
    print()

    return report


def run_query_tests():
    """在已有 DB 上运行查询性能测试。"""
    if not os.path.exists(DB_PATH):
        print(f"[error] DB 不存在: {DB_PATH}")
        print(f"        请先运行: python tests/_perf_testcode_baseline.py --refresh")
        return None

    print(f"[query] 在基线 DB 上运行查询性能测试")
    print(f"  DB: {DB_PATH}")

    db = CodeGraphDB(db_path=DB_PATH, workspace_root=TESTCODE_ROOT)
    db_size = os.path.getsize(DB_PATH) / (1024 * 1024)
    print(f"  DB size: {db_size:.2f} MB")

    results = {"queries": {}, "db_size_mb": round(db_size, 2)}

    stats = db.get_stats()
    results["stats"] = {
        "symbols": stats.get("total_symbols", 0),
        "calls": stats.get("total_calls", 0),
        "files": stats.get("total_files", 0),
    }
    print(f"  symbols: {results['stats']['symbols']:,}")
    print(f"  calls:   {results['stats']['calls']:,}")
    print(f"  files:   {results['stats']['files']:,}")
    print()

    # 1. search_symbols
    print(f"[1/5] search_symbols...")
    for term in ["main", "test", "handle", "create", "get"]:
        try:
            t0 = time.perf_counter()
            r = db.search_symbols(term, limit=50)
            elapsed = time.perf_counter() - t0
            print(f"  search '{term}': {elapsed:.4f}s, {len(r)} 结果")
            results["queries"][f"search_{term}"] = {
                "elapsed": round(elapsed, 4),
                "result_count": len(r),
            }
        except Exception as e:
            print(f"  search '{term}' 失败: {e}")
            results["queries"][f"search_{term}"] = {"error": str(e)[:200]}

    # 2. get_callers
    print(f"\n[2/5] get_callers (短名)...")
    try:
        cur = db.conn.execute(
            "SELECT callee_name, COUNT(*) as c FROM calls "
            "GROUP BY callee_name ORDER BY c DESC LIMIT 5"
        )
        top_callees = [(row[0], row[1]) for row in cur.fetchall()]
        print(f"  top callees: {top_callees[:5]}")
    except Exception as e:
        top_callees = []
        print(f"  查询 top callees 失败: {e}")

    for callee_name, count in top_callees[:3]:
        try:
            t0 = time.perf_counter()
            callers = db.get_callers(callee_name)
            elapsed = time.perf_counter() - t0
            print(f"  get_callers '{callee_name}': {elapsed:.4f}s, {len(callers)} 调用者")
            results["queries"][f"get_callers_{callee_name}"] = {
                "callee_name": callee_name,
                "expected_count": count,
                "elapsed": round(elapsed, 4),
                "result_count": len(callers),
            }
        except Exception as e:
            print(f"  get_callers '{callee_name}' 失败: {e}")
            results["queries"][f"get_callers_{callee_name}"] = {"error": str(e)[:200]}

    # 3. get_callees
    print(f"\n[3/5] get_callees (短名)...")
    try:
        cur = db.conn.execute(
            "SELECT caller_name, COUNT(*) as c FROM calls "
            "GROUP BY caller_name ORDER BY c DESC LIMIT 5"
        )
        top_callers = [(row[0], row[1]) for row in cur.fetchall()]
        print(f"  top callers: {top_callers[:5]}")
    except Exception as e:
        top_callers = []
        print(f"  查询 top callers 失败: {e}")

    for caller_name, count in top_callers[:3]:
        try:
            t0 = time.perf_counter()
            callees = db.get_callees(caller_name)
            elapsed = time.perf_counter() - t0
            print(f"  get_callees '{caller_name}': {elapsed:.4f}s, {len(callees)} 被调用者")
            results["queries"][f"get_callees_{caller_name}"] = {
                "caller_name": caller_name,
                "expected_count": count,
                "elapsed": round(elapsed, 4),
                "result_count": len(callees),
            }
        except Exception as e:
            print(f"  get_callees '{caller_name}' 失败: {e}")
            results["queries"][f"get_callees_{caller_name}"] = {"error": str(e)[:200]}

    # 4. get_topological_order
    print(f"\n[4/5] get_topological_order...")
    try:
        t0 = time.perf_counter()
        topo = db.get_topological_order(limit=5000)
        elapsed = time.perf_counter() - t0
        print(f"  topo: {elapsed:.4f}s, {len(topo)} 符号")
        results["queries"]["topological_order"] = {
            "elapsed": round(elapsed, 4),
            "result_count": len(topo),
        }
    except Exception as e:
        print(f"  topo 失败: {e}")
        results["queries"]["topological_order"] = {"error": str(e)[:200]}

    # 5. get_impact
    print(f"\n[5/5] get_impact (变更影响)...")
    if top_callers:
        target = top_callers[0][0]
        try:
            cur = db.conn.execute(
                "SELECT id FROM symbols WHERE name = ? LIMIT 1", (target,)
            )
            row = cur.fetchone()
            if row:
                sym_id = row[0]
                t0 = time.perf_counter()
                impact = db.get_impact(sym_id)
                elapsed = time.perf_counter() - t0
                print(f"  get_impact '{target}' (id={sym_id}): {elapsed:.4f}s, {len(impact)} 影响符号")
                results["queries"]["get_impact"] = {
                    "target": target,
                    "symbol_id": sym_id,
                    "elapsed": round(elapsed, 4),
                    "result_count": len(impact),
                }
        except Exception as e:
            print(f"  get_impact '{target}' 失败: {e}")
            results["queries"]["get_impact"] = {"error": str(e)[:200]}

    db.close()

    with open(QUERY_REPORT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n查询测试报告已保存: {QUERY_REPORT}")

    return results


def main():
    batch_size = 50
    if "--batch-size" in sys.argv:
        idx = sys.argv.index("--batch-size")
        if idx + 1 < len(sys.argv):
            batch_size = int(sys.argv[idx + 1])

    # P28: --algo-verify 只跑算法预检，不构建（快速验证）
    if "--algo-verify" in sys.argv:
        verify_algorithm()
        return

    # P28: --incremental 增量构建（不删旧 DB，已构建的跳过）
    if "--incremental" in sys.argv:
        if not os.path.exists(DB_PATH):
            print(f"[error] DB 不存在: {DB_PATH}")
            print(f"        请先运行: python tests/_perf_testcode_baseline.py --refresh")
            return
        # 增量构建：不删 DB，直接构建（build_directory 会跳过已解析文件）
        _build_incremental(batch_size=batch_size)
        return

    if "--refresh" in sys.argv:
        # 先跑算法预检，再全量构建
        verify_algorithm()
        build_baseline_db(batch_size=batch_size)
        print()
        print("=" * 60)
        print("[query tests] 在新建基线 DB 上运行查询测试...")
        print("=" * 60)
        run_query_tests()
    else:
        run_query_tests()


def _build_incremental(batch_size=50):
    """P28 增量构建：不删旧 DB，继续构建剩余项目。

    用于在已有 batch 1-4 基础上继续，验证动态算法在剩余批次的安全性。
    """
    print(f"[baseline] 增量构建（不删旧 DB，继续剩余批次）")
    print(f"  DB:         {DB_PATH}")
    print(f"  batch_size: {batch_size} 项目/批")
    print(f"  workers:    动态（P28 算法决策）")
    print(f"  [mem] 初始 RSS: {_format_mb(_get_rss_mb())} "
          f"tree={_format_mb(_get_process_tree_rss_mb())}")
    print()

    # 先跑算法预检
    verify_algorithm()

    # 扫描子项目
    print(f"[1/3] 扫描子项目...")
    projects = scan_subprojects(TESTCODE_ROOT, max_depth=2)
    print(f"  发现 {len(projects)} 个子项目")
    print()

    # 打开已有 DB（不重建）
    print(f"[2/3] 打开已有 DB（增量模式）...")
    db = CodeGraphDB(db_path=DB_PATH, workspace_root=TESTCODE_ROOT)
    stats_before = db.get_stats()
    print(f"  已有: files={stats_before.get('total_files', 0):,} "
          f"sym={stats_before.get('total_symbols', 0):,} "
          f"calls={stats_before.get('total_calls', 0):,}")
    print()

    # 分批构建（复用 build_baseline_db 的循环逻辑，但不删 DB）
    print(f"[3/3] 增量分批构建（每批 {batch_size} 个项目）...")
    total_batches = (len(projects) + batch_size - 1) // batch_size
    t_total = time.perf_counter()
    batch_records = []
    files_before = stats_before.get("total_files", 0)

    for batch_idx in range(total_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, len(projects))
        batch_projects = projects[start:end]
        batch_dirs = set()
        for p in batch_projects:
            root = p.get("root", "")
            if root and os.path.isdir(root):
                batch_dirs.add(root)
        if not batch_dirs:
            continue

        t_batch = time.perf_counter()
        with _MemoryMonitor() as mem:
            for d in sorted(batch_dirs):
                try:
                    rel = os.path.relpath(d, TESTCODE_ROOT)
                    db.build_directory(rel)
                except Exception as e:
                    print(f"    ERROR {d}: {e}", flush=True)

        batch_elapsed = time.perf_counter() - t_batch
        stats = db.get_stats()
        batch_files = stats.get("total_files", 0)
        batch_symbols = stats.get("total_symbols", 0)
        rss = _get_rss_mb()
        tree_rss = _get_process_tree_rss_mb()
        peak_tree_rss = mem.peak_tree_rss_mb if mem.peak_tree_rss_mb is not None else tree_rss
        avail_after = _get_available_memory_mb()

        if batch_records:
            prev_files = batch_records[-1]["cumulative_files"]
        else:
            prev_files = files_before
        batch_new_files = batch_files - prev_files
        algo_workers = _detect_optimal_workers(batch_new_files)
        avail_str = f"{avail_after:.0f}MB" if avail_after else "N/A"

        print(f"  batch {batch_idx+1}/{total_batches}: projects {start+1}-{end} "
              f"new_files={batch_new_files:,} cum_files={batch_files:,} "
              f"sym={batch_symbols:,} "
              f"{batch_elapsed:.1f}s RSS={_format_mb(rss)} "
              f"treeRSS={_format_mb(tree_rss)} peakTree={_format_mb(peak_tree_rss)} "
              f"avail={avail_str} "
              f"algo_workers({batch_new_files})={algo_workers}", flush=True)

        batch_records.append({
            "batch": batch_idx + 1,
            "new_files": batch_new_files,
            "cumulative_files": batch_files,
            "symbols": batch_symbols,
            "elapsed_sec": round(batch_elapsed, 2),
            "rss_mb": rss,
            "tree_rss_mb": tree_rss,
            "peak_tree_rss_mb": peak_tree_rss,
            "avail_mem_mb": round(avail_after, 0) if avail_after else None,
            "algo_workers": algo_workers,
        })

        warn_mem = peak_tree_rss if peak_tree_rss is not None else rss
        if warn_mem is not None and warn_mem > _MEM_WARN_MB:
            import gc
            gc.collect()
            rss_after = _get_rss_mb()
            tree_after = _get_process_tree_rss_mb()
            print(f"    [mem] GC: RSS {_format_mb(rss)} -> {_format_mb(rss_after)}, "
                  f"tree {_format_mb(tree_rss)} -> {_format_mb(tree_after)} "
                  f"(超 {_MEM_WARN_MB}MB 警告阈值)", flush=True)
            critical_mem = tree_after if tree_after is not None else rss_after
            if critical_mem is not None and critical_mem > _MEM_CRITICAL_MB:
                print(f"    [mem] CRITICAL: RSS {critical_mem}MB 超 {_MEM_CRITICAL_MB}MB 临界！",
                      flush=True)

    total_elapsed = time.perf_counter() - t_total
    stats = db.get_stats()
    db_size = os.path.getsize(DB_PATH) / (1024 * 1024)
    final_rss = _get_rss_mb()
    final_tree_rss = _get_process_tree_rss_mb()

    print(f"\n  增量构建完成: {total_elapsed:.1f}s ({total_elapsed/60:.1f}min)")
    print(f"  [mem] 最终 RSS: {_format_mb(final_rss)} (peak: {_format_mb(_peak_memory_mb)})")
    print(f"  [mem] 最终 tree RSS: {_format_mb(final_tree_rss)} "
          f"(peak: {_format_mb(_peak_tree_memory_mb)})")
    print(f"  DB size:  {db_size:.2f} MB")
    print(f"  symbols:  {stats.get('total_symbols', 0):,}")
    print(f"  calls:    {stats.get('total_calls', 0):,}")
    print(f"  files:    {stats.get('total_files', 0):,}")

    if batch_records:
        max_rss = max((r["rss_mb"] or 0) for r in batch_records)
        max_tree_rss = max((r["peak_tree_rss_mb"] or 0) for r in batch_records)
        max_new = max(r["new_files"] for r in batch_records)
        workers_dist = {}
        for r in batch_records:
            w = r["algo_workers"]
            workers_dist[w] = workers_dist.get(w, 0) + 1
        print(f"\n[P28 算法验证总结]")
        print(f"  最大单批文件数:    {max_new:,}")
        print(f"  最大 RSS:          {_format_mb(max_rss)} (峰值 {_format_mb(_peak_memory_mb)})")
        print(f"  最大进程树 RSS:    {_format_mb(max_tree_rss)} "
              f"(峰值 {_format_mb(_peak_tree_memory_mb)})")
        print(f"  算法决策分布:      {workers_dist}")
        peak_for_check = _peak_tree_memory_mb or _peak_memory_mb
        print(f"  内存安全:          {'PASS' if peak_for_check < _MEM_CRITICAL_MB else 'WARN'} "
              f"(进程树峰值 {_format_mb(peak_for_check)} / 临界 {_MEM_CRITICAL_MB}MB)")
    print()

    db.close()

    # 保存增量报告
    report = {
        "label": "testcode_repos_baseline_incremental",
        "db_path": DB_PATH,
        "db_size_mb": round(db_size, 2),
        "build_elapsed_sec": round(total_elapsed, 2),
        "peak_memory_mb": _peak_memory_mb,
        "peak_tree_memory_mb": _peak_tree_memory_mb,
        "stats_before": {
            "files": files_before,
        },
        "stats_after": {
            "symbols": stats.get("total_symbols", 0),
            "calls": stats.get("total_calls", 0),
            "files": stats.get("total_files", 0),
        },
        "p28_verification": {
            "batch_records": batch_records,
            "peak_memory_mb": _peak_memory_mb,
            "peak_tree_memory_mb": _peak_tree_memory_mb,
            "mem_critical_threshold_mb": _MEM_CRITICAL_MB,
            "max_batch_files": max((r["new_files"] for r in batch_records), default=0),
        },
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(BASELINE_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"增量报告已保存: {BASELINE_REPORT}")


if __name__ == "__main__":
    main()
