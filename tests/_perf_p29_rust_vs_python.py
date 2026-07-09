"""P29: Rust parse vs Python 多进程 parse benchmark。

对比指标：
1. 速度（parse 时间）
2. 内存峰值（RSS）
3. 结果一致性（符号数、调用数）

测试数据：testcode/repos 下的真实 C 文件
"""
from __future__ import annotations

import os
import sys
import time
import json
import ctypes
import tempfile
import shutil

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

REPORT_PATH = os.path.join(_PKG_ROOT, "tests", "_perf_p29_rust_vs_python.json")


def _get_rss_mb():
    """获取当前进程 RSS（MB），优先用 psutil（跨平台可靠）。"""
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        return mem.rss // (1024 * 1024), mem.peak_wset // (1024 * 1024)
    except ImportError:
        pass

    # Windows ctypes fallback
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # GetCurrentProcess 返回伪句柄 -1，需要用真实句柄
        h_process = kernel32.GetCurrentProcess()
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong),
                        ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        ctypes.windll.psapi.GetProcessMemoryInfo(h_process,
                                                  ctypes.byref(counters),
                                                  counters.cb)
        return counters.WorkingSetSize // (1024 * 1024), counters.PeakWorkingSetSize // (1024 * 1024)
    except Exception:
        return 0, 0


def _collect_c_files(root_dir, max_files=2000):
    """收集 testcode 下的真实 C 文件"""
    files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # 跳过第三方库目录
        dirnames[:] = [d for d in dirnames if d not in (
            "node_modules", "vendor", "third_party", ".git", "target", "build",
            "dist", "__pycache__", ".venv", "venv", "deps", "libs"
        )]
        for f in filenames:
            if f.endswith((".c", ".h")):
                files.append(os.path.join(dirpath, f))
                if len(files) >= max_files:
                    return files
    return files


def benchmark_rust_parse(c_files, num_threads=8):
    """Rust batch_parse_c_files benchmark"""
    from callwarden_core import batch_parse_c_files

    # 资源文件预过滤
    from callwarden.db.db_build import _is_resource_file
    filtered = []
    for path in c_files:
        is_res, _ = _is_resource_file(path)
        if not is_res:
            filtered.append(path)

    rust_args = [(path, "") for path in filtered]

    print(f"  [rust] {len(rust_args)} files, threads={num_threads}")
    rss_before, _ = _get_rss_mb()

    t0 = time.perf_counter()
    results = batch_parse_c_files(rust_args, num_threads=num_threads)
    elapsed = time.perf_counter() - t0

    rss_after, rss_peak = _get_rss_mb()

    # 统计结果
    total_symbols = sum(len(r.get("symbols", [])) for r in results)
    total_calls = sum(len(r.get("raw_calls", [])) for r in results)
    total_lines = sum(r.get("total_lines", 0) for r in results)
    errors = sum(1 for r in results if r.get("error"))

    print(f"  [rust] elapsed: {elapsed:.2f}s")
    print(f"  [rust] symbols: {total_symbols:,}, calls: {total_calls:,}, lines: {total_lines:,}")
    print(f"  [rust] errors: {errors}")
    print(f"  [rust] RSS: before={rss_before}MB, after={rss_after}MB, peak={rss_peak}MB, delta={rss_after-rss_before}MB")

    return {
        "engine": "rust",
        "files": len(rust_args),
        "elapsed_sec": round(elapsed, 2),
        "symbols": total_symbols,
        "calls": total_calls,
        "lines": total_lines,
        "errors": errors,
        "rss_before_mb": rss_before,
        "rss_after_mb": rss_after,
        "rss_peak_mb": rss_peak,
        "rss_delta_mb": rss_after - rss_before,
        "threads": num_threads,
    }


def benchmark_python_parse(c_files, num_workers=8):
    """Python ProcessPoolExecutor benchmark"""
    from callwarden.db.db_build import _parse_file_worker, _is_resource_file

    # 资源文件预过滤
    filtered = []
    for path in c_files:
        is_res, _ = _is_resource_file(path)
        if not is_res:
            filtered.append(path)

    # 构造 worker 参数：(rel_path, abs_path, lang, module_path, file_instance_id)
    mp_args = [(os.path.basename(path), path, "c", "", 0) for path in filtered]

    print(f"  [python] {len(mp_args)} files, workers={num_workers}")
    rss_before, _ = _get_rss_mb()

    from concurrent.futures import ProcessPoolExecutor
    import pickle
    os.environ["CW_MP_WORKERS"] = str(num_workers)

    t0 = time.perf_counter()
    total_symbols = 0
    total_calls = 0
    total_lines = 0
    errors = 0

    try:
        with ProcessPoolExecutor(max_workers=num_workers) as pool:
            results = list(pool.map(_parse_file_worker, mp_args, chunksize=4))

        for status, rel_path, payload in results:
            if status == "ok" and isinstance(payload, dict):
                total_symbols += len(payload.get("symbols", []))
                total_calls += len(payload.get("raw_calls", []))
                total_lines += payload.get("total_lines", 0)
            elif status == "fail":
                errors += 1
    except Exception as e:
        print(f"  [python] ERROR: {e}")
        errors = len(mp_args)

    elapsed = time.perf_counter() - t0
    rss_after, rss_peak = _get_rss_mb()

    print(f"  [python] elapsed: {elapsed:.2f}s")
    print(f"  [python] symbols: {total_symbols:,}, calls: {total_calls:,}, lines: {total_lines:,}")
    print(f"  [python] errors: {errors}")
    print(f"  [python] RSS: before={rss_before}MB, after={rss_after}MB, peak={rss_peak}MB, delta={rss_after-rss_before}MB")

    return {
        "engine": "python",
        "files": len(mp_args),
        "elapsed_sec": round(elapsed, 2),
        "symbols": total_symbols,
        "calls": total_calls,
        "lines": total_lines,
        "errors": errors,
        "rss_before_mb": rss_before,
        "rss_after_mb": rss_after,
        "rss_peak_mb": rss_peak,
        "rss_delta_mb": rss_after - rss_before,
        "workers": num_workers,
    }


def main():
    testcode_root = os.path.join(_PKG_ROOT, "testcode", "repos")
    if not os.path.isdir(testcode_root):
        print(f"[error] testcode/repos 不存在: {testcode_root}")
        return

    print("=" * 70)
    print("P29: Rust parse vs Python 多进程 parse benchmark")
    print("=" * 70)

    # 收集 C 文件
    print(f"\n[1/3] 收集 testcode/repos 下的 C 文件...")
    c_files = _collect_c_files(testcode_root, max_files=2000)
    print(f"  发现 {len(c_files)} 个 C 文件")

    if len(c_files) < 50:
        print(f"  [warning] C 文件太少（{len(c_files)}），benchmark 意义不大")
        return

    # 1. Rust benchmark
    print(f"\n[2/3] Rust batch_parse_c_files benchmark...")
    rust_result = benchmark_rust_parse(c_files, num_threads=8)

    # 清理内存
    import gc
    gc.collect()

    # 2. Python benchmark
    print(f"\n[3/3] Python ProcessPoolExecutor benchmark...")
    py_result = benchmark_python_parse(c_files, num_workers=4)

    # 对比
    print(f"\n{'=' * 70}")
    print("对比结果")
    print(f"{'=' * 70}")
    print(f"{'指标':<20} {'Rust':<20} {'Python':<20} {'差异':<20}")
    print(f"{'-' * 80}")
    print(f"{'文件数':<20} {rust_result['files']:<20} {py_result['files']:<20}")
    print(f"{'耗时(s)':<20} {rust_result['elapsed_sec']:<20} {py_result['elapsed_sec']:<20} "
          f"{rust_result['elapsed_sec'] - py_result['elapsed_sec']:+.2f}")
    print(f"{'符号数':<20} {rust_result['symbols']:<20} {py_result['symbols']:<20}")
    print(f"{'调用数':<20} {rust_result['calls']:<20} {py_result['calls']:<20}")
    print(f"{'RSS 峰值(MB)':<20} {rust_result['rss_peak_mb']:<20} {py_result['rss_peak_mb']:<20} "
          f"{rust_result['rss_peak_mb'] - py_result['rss_peak_mb']:+d}")
    print(f"{'RSS 增量(MB)':<20} {rust_result['rss_delta_mb']:<20} {py_result['rss_delta_mb']:<20} "
          f"{rust_result['rss_delta_mb'] - py_result['rss_delta_mb']:+d}")

    # 保存报告
    report = {
        "label": "p29_rust_vs_python_parse",
        "testcode_root": testcode_root,
        "rust": rust_result,
        "python": py_result,
        "comparison": {
            "elapsed_ratio": round(rust_result["elapsed_sec"] / max(0.01, py_result["elapsed_sec"]), 2),
            "rss_peak_diff": rust_result["rss_peak_mb"] - py_result["rss_peak_mb"],
            "rss_delta_diff": rust_result["rss_delta_mb"] - py_result["rss_delta_mb"],
        },
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {REPORT_PATH}")


if __name__ == "__main__":
    main()
