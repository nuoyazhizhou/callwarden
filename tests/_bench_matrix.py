"""
参数矩阵实验：1M 规模，筛选 P13/P14/P15 的可行子集

参数矩阵（6 组合 × 3 次中位数）：
1. (cache=64,  mmap=256,  page=4) → baseline（与 v2 基准一致）
2. (cache=256, mmap=256,  page=4) → 单测 cache_size 收益
3. (cache=64,  mmap=1024, page=4) → 单测 mmap_size 收益
4. (cache=256, mmap=1024, page=4) → cache + mmap 联合
5. (cache=512, mmap=1024, page=4) → 极限内存（验证收益递减点）
6. (cache=256, mmap=1024, page=8) → page_size 影响（空间 vs 速度）

固定：mode=deferred, temp_store=MEMORY, commit_every=10, symbols=1M
"""
import os
import sys
import json
import time
import argparse
from pathlib import Path

# 复用 v2 的 run_single 和工具函数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bench_baseline_v2 import (
    run_single, collect_env_info, median, split_index_statements
)


# ============================================
# 矩阵定义
# ============================================

MATRIX = [
    # (name, cache_size_kb, mmap_size_bytes, page_size_bytes)
    ("baseline_64_256_4",   64 * 1024,   256 * 1024 * 1024, 4096),
    ("cache_256_256_4",     256 * 1024,  256 * 1024 * 1024, 4096),
    ("mmap_64_1024_4",      64 * 1024,   1024 * 1024 * 1024, 4096),
    ("combined_256_1024_4", 256 * 1024,  1024 * 1024 * 1024, 4096),
    ("extreme_512_1024_4",  512 * 1024,  1024 * 1024 * 1024, 4096),
    ("page8_256_1024_8",    256 * 1024,  1024 * 1024 * 1024, 8192),
]


# ============================================
# 主函数
# ============================================

def main():
    parser = argparse.ArgumentParser(description="参数矩阵实验：1M 规模")
    parser.add_argument("--symbols", type=int, default=1000000, help="目标符号数（默认 1M）")
    parser.add_argument("--runs", type=int, default=3, help="每组运行次数（默认 3，取中位数）")
    parser.add_argument("--db-dir", type=str, default="", help="数据库目录")
    parser.add_argument("--only", type=str, default="", help="只跑指定 name（逗号分隔）")
    args = parser.parse_args()

    env = collect_env_info()
    db_dir = args.db_dir or os.path.dirname(os.path.abspath(__file__))

    # 筛选要跑的组合
    matrix = MATRIX
    if args.only:
        names = set(args.only.split(","))
        matrix = [m for m in MATRIX if m[0] in names]

    print(f"\n{'='*70}")
    print(f"参数矩阵实验")
    print(f"{'='*70}")
    print(f"环境：SQLite {env['sqlite_version']}, Python {env['python_version']}")
    print(f"      RAM {env.get('ram_total_gb', '?')}GB, available {env.get('ram_available_mb', '?')}MB")
    print(f"      Disk free {env.get('disk_free_gb', '?')}GB")
    print(f"\n矩阵（{len(matrix)} 组合 × {args.runs} 次）：")
    for name, cache, mmap, page in matrix:
        print(f"  - {name:<25} cache={cache//1024}MB, mmap={mmap//1024//1024}MB, page={page}")
    print()

    all_results = {}
    start_time = time.time()

    for idx, (name, cache_kb, mmap_bytes, page_size) in enumerate(matrix):
        print(f"\n{'─'*70}")
        print(f"[{idx+1}/{len(matrix)}] 组合：{name}")
        print(f"  cache={cache_kb//1024}MB, mmap={mmap_bytes//1024//1024}MB, page={page_size}")
        print(f"  预计耗时：~{args.runs * 75}s")
        print(f"{'─'*70}")

        runs = []
        for run_idx in range(args.runs):
            db_path = os.path.join(db_dir, f"_matrix_{name}_r{run_idx}.db")
            print(f"\n  Run {run_idx + 1}/{args.runs}...")
            t_start = time.time()
            # 最后一轮做逐索引计时
            per_index = (run_idx == args.runs - 1)
            result = run_single(
                db_path, args.symbols,
                mode="deferred",
                commit_every=10,
                cache_size_kb=cache_kb,
                mmap_size=mmap_bytes,
                page_size=page_size,
                temp_store="MEMORY",
                per_index_timing=per_index,
            )
            elapsed = time.time() - t_start
            t = result["timing"]
            print(f"    storage_build={t['storage_build_s']:.2f}s "
                  f"(insert={t['insert_s']:.2f}s, index={t['index_s']:.2f}s)")
            print(f"    db={result['storage']['db_mb']:.1f}MB, "
                  f"peak_wal={result['storage']['peak_wal_mb']:.1f}MB, "
                  f"peak_rss={result['memory']['peak_rss_mb']:.1f}MB")
            print(f"    本次耗时：{elapsed:.1f}s")
            runs.append(result)
            # 清理中间 db（保留最后一轮用于查询验证）
            if run_idx < args.runs - 1:
                for suffix in ["", "-wal", "-shm"]:
                    p = db_path + suffix
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass

        # 计算中位数
        medians = {
            "schema_s": median([r["timing"]["schema_s"] for r in runs]),
            "insert_s": median([r["timing"]["insert_s"] for r in runs]),
            "index_s": median([r["timing"]["index_s"] for r in runs]),
            "checkpoint_s": median([r["timing"]["checkpoint_s"] for r in runs]),
            "storage_build_s": median([r["timing"]["storage_build_s"] for r in runs]),
            "end_to_end_s": median([r["timing"]["end_to_end_s"] for r in runs]),
            "peak_rss_mb": median([r["memory"]["peak_rss_mb"] for r in runs]),
            "peak_wal_mb": median([r["storage"]["peak_wal_mb"] for r in runs]),
            "db_mb": median([r["storage"]["db_mb"] for r in runs]),
            "query_latency": runs[-1]["query_latency"],
            "index_timings": runs[-1].get("index_timings"),
        }
        all_results[name] = {
            "params": {
                "cache_size_mb": cache_kb // 1024,
                "mmap_size_mb": mmap_bytes // 1024 // 1024,
                "page_size": page_size,
                "temp_store": "MEMORY",
                "mode": "deferred",
            },
            "runs": runs,
            "median": medians,
        }

        # 实时打印进度
        elapsed_total = time.time() - start_time
        remaining = (len(matrix) - idx - 1) * args.runs * 75
        print(f"\n  [进度] 已完成 {idx+1}/{len(matrix)} 组合，"
              f"已耗时 {elapsed_total/60:.1f}min，预计剩余 {remaining/60:.1f}min")

        # 保存最后一轮的 db 用于后续验证（清理其他文件）
        # 已在上面清理

    # 清理最后一轮 db 文件
    for name, _, _, _ in matrix:
        db_path = os.path.join(db_dir, f"_matrix_{name}_r{args.runs - 1}.db")
        for suffix in ["", "-wal", "-shm"]:
            p = db_path + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    # ============================================
    # 汇总分析
    # ============================================
    print(f"\n\n{'='*70}")
    print(f"参数矩阵实验汇总（{args.runs} 次中位数）")
    print(f"{'='*70}")

    baseline_name = MATRIX[0][0]
    baseline_storage = all_results[baseline_name]["median"]["storage_build_s"]
    baseline_index = all_results[baseline_name]["median"]["index_s"]
    baseline_insert = all_results[baseline_name]["median"]["insert_s"]

    # 边际收益表
    print(f"\n{'组合':<25} {'cache':>8} {'mmap':>8} {'page':>6} "
          f"{'insert':>8} {'index':>8} {'storage':>8} {'db':>8} {'wal':>6} {'rss':>6} "
          f"{'加速比':>8}")
    print(f"{'─'*110}")
    for name, cache_kb, mmap_bytes, page_size in matrix:
        if name not in all_results:
            continue
        m = all_results[name]["median"]
        cache_mb = cache_kb // 1024
        mmap_mb = mmap_bytes // 1024 // 1024
        speedup = baseline_storage / m["storage_build_s"] if m["storage_build_s"] > 0 else 0
        print(f"  {name:<23} {cache_mb:>6}MB {mmap_mb:>6}MB {page_size:>4}B "
              f"{m['insert_s']:>6.2f}s {m['index_s']:>6.2f}s {m['storage_build_s']:>6.2f}s "
              f"{m['db_mb']:>6.1f}M {m['peak_wal_mb']:>4.0f}M {m['peak_rss_mb']:>4.0f}M "
              f"{speedup:>6.2f}x")

    # 查询延迟对比
    print(f"\n查询延迟：")
    print(f"  {'组合':<25} {'cold_qname':>12} {'cold_callee':>12} "
          f"{'hot_qname':>12} {'hot_callee':>12}")
    print(f"  {'─'*80}")
    for name, _, _, _ in matrix:
        if name not in all_results:
            continue
        ql = all_results[name]["median"]["query_latency"]
        print(f"  {name:<23} {ql['cold_qualified_name_ms']:>10.2f}ms "
              f"{ql['cold_callee_name_ms']:>10.2f}ms "
              f"{ql['hot_qualified_name_ms']:>10.2f}ms "
              f"{ql['hot_callee_name_ms']:>10.2f}ms")

    # calls 表 3 个索引耗时对比（关键发现验证）
    print(f"\ncalls 表 3 个索引耗时对比（关键瓶颈）：")
    print(f"  {'组合':<25} {'idx_calls_caller':>18} {'idx_calls_callee':>18} "
          f"{'idx_calls_callee_q':>20} {'3 索引合计':>14} {'占 index_s':>10}")
    print(f"  {'─'*110}")
    for name, _, _, _ in matrix:
        if name not in all_results:
            continue
        m = all_results[name]["median"]
        idx_list = m.get("index_timings") or []
        # 找出 3 个 calls 索引
        caller_t = callee_t = callee_q_t = 0
        for t in idx_list:
            if "idx_calls_caller" in t["name"]:
                caller_t = t["time_s"]
            elif "idx_calls_callee_" in t["name"] and "qualified" in t["name"]:
                callee_q_t = t["time_s"]
            elif t["name"] == "INDEX idx_calls_callee":
                callee_t = t["time_s"]
        total_3 = caller_t + callee_t + callee_q_t
        pct = total_3 / m["index_s"] * 100 if m["index_s"] > 0 else 0
        print(f"  {name:<23} {caller_t:>16.2f}s {callee_t:>16.2f}s "
              f"{callee_q_t:>18.2f}s {total_3:>12.2f}s {pct:>8.1f}%")

    # 边际收益分析
    print(f"\n边际收益分析（相对 baseline）：")
    print(f"  {'变量':<25} {'storage_build':>15} {'收益':>10} {'index_s':>10} {'收益':>10}")
    print(f"  {'─'*75}")
    b = all_results[baseline_name]["median"]
    for name, _, _, _ in matrix:
        if name == baseline_name or name not in all_results:
            continue
        m = all_results[name]["median"]
        gain_storage = (b["storage_build_s"] - m["storage_build_s"]) / b["storage_build_s"] * 100
        gain_index = (b["index_s"] - m["index_s"]) / b["index_s"] * 100 if b["index_s"] > 0 else 0
        print(f"  {name:<23} {m['storage_build_s']:>13.2f}s {gain_storage:>+8.1f}% "
              f"{m['index_s']:>8.2f}s {gain_index:>+8.1f}%")

    # 保存报告
    report = {
        "env": env,
        "params": {
            "symbols": args.symbols,
            "runs": args.runs,
            "matrix_size": len(matrix),
        },
        "matrix": [{"name": n, "cache_mb": c//1024, "mmap_mb": m//1024//1024, "page_size": p}
                   for n, c, m, p in matrix],
        "results": all_results,
        "summary": {
            "baseline_storage_build_s": baseline_storage,
            "baseline_index_s": baseline_index,
            "baseline_insert_s": baseline_insert,
            "best_combination": min(all_results.items(),
                                    key=lambda x: x[1]["median"]["storage_build_s"])[0],
        },
    }
    report_path = os.path.join(db_dir, f"_matrix_{args.symbols // 1000000}m_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n报告已保存：{report_path}")

    total_elapsed = time.time() - start_time
    print(f"\n总耗时：{total_elapsed/60:.1f} 分钟")


if __name__ == "__main__":
    main()
