"""统一基准体系 v3：覆盖 4 个维度

修正点（基于 v2 + matrix 报告 + P2/P3/P4/P6 优化）：
1. SQLite 构建（schema v32，已删除 idx_calls_callee）+ 逐索引计时
2. GraphStore 加载/查询（load_from_sqlite / dump / load_from_file + 4 类查询性能）
3. GraphStore 内存占用分解（基于 stats()，定位下个优化点）
4. SQL vs GraphStore 查询对比（量化 GraphStore 加速比）

参数：与生产配置一致（cache=256MB, mmap=256MB, page=8KB, temp=MEMORY, mode=deferred）
规模：1M + 2M，各 3 次取中位数，串行运行

用法：
  python tests/_bench_unified_v3.py --symbols 1000000 --runs 3
  python tests/_bench_unified_v3.py --symbols 2000000 --runs 3
"""
import os
import sys
import json
import time
import sqlite3
import argparse
from pathlib import Path

# 复用 v2 的流式数据生成器和工具函数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bench_baseline_v2 import (
    run_single, collect_env_info, median, split_index_statements,
    gen_files_stream, gen_symbols_stream, gen_calls_stream,
)

# 加载 GraphStore（callwarden_core）
RUST_TARGET = str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall")
sys.path.insert(0, RUST_TARGET)


def _import_graphstore():
    """延迟导入 callwarden_core，避免未编译时立即失败"""
    import callwarden_core
    return callwarden_core


# ============================================
# GraphStore 基准测试
# ============================================

def bench_graphstore(db_path: str, snap_path: str) -> dict:
    """GraphStore 加载/查询/内存基准

    Returns:
        dict with timing/memory/query_latency fields
    """
    callwarden_core = _import_graphstore()
    import psutil

    proc = psutil.Process()
    rss_before = proc.memory_info().rss / 1024 / 1024

    # 1. load_from_sqlite 计时
    store = callwarden_core.GraphStore()
    t0 = time.perf_counter()
    sym_count, edge_count = store.load_from_sqlite(db_path)
    t_load_sqlite = time.perf_counter() - t0

    rss_after_load = proc.memory_info().rss / 1024 / 1024
    stats = store.stats()
    exact_memory = store.memory_breakdown() if hasattr(store, "memory_breakdown") else None

    # 2. dump_to_file 计时
    if os.path.exists(snap_path):
        os.remove(snap_path)
    t0 = time.perf_counter()
    store.dump_to_file(snap_path)
    t_dump = time.perf_counter() - t0
    snap_size_mb = os.path.getsize(snap_path) / 1024 / 1024

    # 3. load_from_file 计时（冷启动模拟）
    store_snap = callwarden_core.GraphStore()
    t0 = time.perf_counter()
    store_snap.load_from_file(snap_path)
    t_load_file = time.perf_counter() - t0

    # 4. GraphStore 查询性能（用 store_snap，模拟从快照冷启动查询）
    conn = sqlite3.connect(db_path)

    # 取 30 个随机 qname 测 get_symbol
    qnames = [r[0] for r in conn.execute(
        "SELECT qualified_name FROM symbols WHERE qualified_name != '' ORDER BY RANDOM() LIMIT 30"
    ).fetchall()]

    # get_symbol（GraphStore vs SQL）
    t0 = time.perf_counter()
    for qn in qnames:
        _ = store_snap.get_symbol(qn)
    t_gs_get_symbol = (time.perf_counter() - t0) / len(qnames) * 1000  # ms/op

    t0 = time.perf_counter()
    for qn in qnames:
        _ = conn.execute("SELECT * FROM symbols WHERE qualified_name = ? LIMIT 1", (qn,)).fetchone()
    t_sql_get_symbol = (time.perf_counter() - t0) / len(qnames) * 1000

    # get_callers（取 10 个 callee_name）
    callee_names = [r[0] for r in conn.execute(
        "SELECT callee_name FROM calls WHERE callee_name != '' GROUP BY callee_name LIMIT 10"
    ).fetchall()]
    t0 = time.perf_counter()
    for cn in callee_names:
        _ = store_snap.get_callers(cn)
    t_gs_get_callers = (time.perf_counter() - t0) / max(len(callee_names), 1) * 1000

    t0 = time.perf_counter()
    for cn in callee_names:
        _ = conn.execute(
            "SELECT COUNT(*) FROM calls WHERE callee_name = ?", (cn,)
        ).fetchone()[0]
    t_sql_get_callers = (time.perf_counter() - t0) / max(len(callee_names), 1) * 1000

    # search_symbols（取 5 个 name）
    terms = [r[0] for r in conn.execute(
        "SELECT DISTINCT name FROM symbols WHERE kind='fn' LIMIT 5"
    ).fetchall()]
    t0 = time.perf_counter()
    for t in terms:
        _ = store_snap.search_symbols(t, None, 100)
    t_gs_search = (time.perf_counter() - t0) / max(len(terms), 1) * 1000

    t0 = time.perf_counter()
    for t in terms:
        _ = conn.execute(
            "SELECT id FROM symbols WHERE name LIKE ? LIMIT 100", (f"%{t}%",)
        ).fetchall()
    t_sql_search = (time.perf_counter() - t0) / max(len(terms), 1) * 1000

    # get_call_chain_down（depth=3）
    t0 = time.perf_counter()
    if qnames:
        chain = store_snap.get_call_chain_down(qnames[0], 3)
        t_gs_chain = (time.perf_counter() - t0) * 1000
        chain_len = len(chain)
    else:
        t_gs_chain = 0
        chain_len = 0

    # SQL CTE call_chain_down（参考用，慢，仅 1 次）
    t0 = time.perf_counter()
    if qnames:
        try:
            _ = conn.execute(
                "WITH RECURSIVE chain(callee) AS ("
                "  SELECT callee_id FROM calls WHERE caller_id = (SELECT id FROM symbols WHERE qualified_name = ? LIMIT 1)"
                "  UNION ALL"
                "  SELECT c.callee_id FROM calls c JOIN chain ON c.caller_id = chain.callee"
                "  LIMIT 1000"
                ") SELECT COUNT(*) FROM chain",
                (qnames[0],)
            ).fetchone()[0]
        except Exception:
            pass
    t_sql_chain = (time.perf_counter() - t0) * 1000

    conn.close()

    # 5. 内存分解
    rss_after_all = proc.memory_info().rss / 1024 / 1024

    # GraphStore 各组件大小（从 stats 推算字节数）
    sym_count = stats.get("symbol_count", 0)
    edge_count = stats.get("edge_count", 0)
    name_pool = stats.get("name_pool_size", 0)
    qname_pool = stats.get("qname_pool_size", 0)
    module_pool = stats.get("module_pool_size", 0)
    callee_names_pool = stats.get("callee_name_pool_size", 0)
    search_pool = stats.get("search_pool_size", 0)
    qname_index = stats.get("qname_index_size", 0)
    simple_name_index = stats.get("simple_name_index_size", 0)
    file_index = stats.get("file_index_size", 0)

    # 字节计算
    by_id_bytes = sym_count * 48  # GraphSymbol = 48 bytes (P5 repr(C))
    fwd_edges_bytes = stats.get("edge_count", 0) * 16  # CallEdge = 16 bytes
    bwd_edges_bytes = stats.get("edge_count", 0) * 8  # BackwardEdge = 8 bytes (P4)
    fwd_offsets_bytes = stats.get("forward_offsets_size", 0) * 8  # usize
    bwd_offsets_bytes = stats.get("backward_offsets_size", 0) * 8
    # by_qname_sorted_ids: Vec<u32> (P4)
    by_qname_bytes = qname_index * 4
    # FxHashMap<String, Vec<u32>>: 估算 ~48 bytes/key
    by_simple_name_bytes = simple_name_index * 48
    by_file_bytes = file_index * 48

    return {
        "timing": {
            "load_from_sqlite_s": round(t_load_sqlite, 3),
            "dump_to_file_s": round(t_dump, 3),
            "load_from_file_s": round(t_load_file, 3),
        },
        "storage": {
            "snap_size_mb": round(snap_size_mb, 1),
        },
        "memory": {
            "rss_before_mb": round(rss_before, 1),
            "rss_after_load_mb": round(rss_after_load, 1),
            "graphstore_rss_mb": round(rss_after_load - rss_before, 1),
            "rss_after_all_mb": round(rss_after_all, 1),
        },
        "query_latency_ms": {
            "gs_get_symbol": round(t_gs_get_symbol, 3),
            "sql_get_symbol": round(t_sql_get_symbol, 3),
            "gs_get_callers": round(t_gs_get_callers, 3),
            "sql_get_callers": round(t_sql_get_callers, 3),
            "gs_search_symbols": round(t_gs_search, 3),
            "sql_search_like": round(t_sql_search, 3),
            "gs_call_chain_down": round(t_gs_chain, 3),
            "sql_call_chain_cte": round(t_sql_chain, 3),
        },
        "chain_len": chain_len,
        "graphstore_stats": stats,
        "memory_breakdown_mb": ({
            "by_id (GraphSymbol × 48B)": round(by_id_bytes / 1024 / 1024, 1),
            "forward_edges (CallEdge × 16B)": round(fwd_edges_bytes / 1024 / 1024, 1),
            "backward_edges (BackwardEdge × 8B)": round(bwd_edges_bytes / 1024 / 1024, 1),
            "forward_offsets (usize × 8B)": round(fwd_offsets_bytes / 1024 / 1024, 1),
            "backward_offsets (usize × 8B)": round(bwd_offsets_bytes / 1024 / 1024, 1),
            "name_pool": round(name_pool / 1024 / 1024, 1),
            "qname_pool": round(qname_pool / 1024 / 1024, 1),
            "module_pool": round(module_pool / 1024 / 1024, 1),
            "callee_names_pool": round(callee_names_pool / 1024 / 1024, 1),
            "search_pool_lower (P2)": round(search_pool / 1024 / 1024, 1),
            "by_qname_sorted_ids (Vec<u32>)": round(by_qname_bytes / 1024 / 1024, 2),
            "by_simple_name (FxHashMap est.)": round(by_simple_name_bytes / 1024 / 1024, 1),
            "by_file (FxHashMap est.)": round(by_file_bytes / 1024 / 1024, 1),
        } if exact_memory is None else {
            key: round(value / 1024 / 1024, 2)
            for key, value in exact_memory.items()
            if key != "known_heap_total"
        }),
        "known_heap_mb": (
            round(exact_memory["known_heap_total"] / 1024 / 1024, 1)
            if exact_memory is not None else None
        ),
    }


# ============================================
# 主函数
# ============================================

def main():
    parser = argparse.ArgumentParser(description="统一基准体系 v3：4 维度覆盖")
    parser.add_argument("--symbols", type=int, default=1000000, help="目标符号数")
    parser.add_argument("--runs", type=int, default=3, help="运行次数（取中位数）")
    parser.add_argument("--label", type=str, default="", help="标签（如 1M/2M）")
    parser.add_argument("--db-dir", type=str, default="", help="数据库目录")
    parser.add_argument("--skip-graphstore", action="store_true", help="跳过 GraphStore 测试")
    parser.add_argument("--skip-sqlite", action="store_true", help="跳过 SQLite 构建测试")
    args = parser.parse_args()

    env = collect_env_info()
    db_dir = args.db_dir or os.path.dirname(os.path.abspath(__file__))
    label = args.label or f"{args.symbols // 1000000}M"

    # 生产参数（与 db_base.py 一致）
    CACHE_KB = 256 * 1024      # 256 MB
    MMAP = 256 * 1024 * 1024   # 256 MB
    PAGE = 8192                # 8 KB
    TEMP = "MEMORY"

    print(f"\n{'='*72}")
    print(f"统一基准体系 v3 — 规模 {label}")
    print(f"{'='*72}")
    print(f"环境：SQLite {env['sqlite_version']}, Python {env['python_version']}")
    print(f"      RAM {env.get('ram_total_gb', '?')}GB, available {env.get('ram_available_mb', '?')}MB")
    print(f"      Disk free {env.get('disk_free_gb', '?')}GB")
    print(f"参数：cache=256MB, mmap=256MB, page=8KB, temp=MEMORY, mode=deferred")
    print(f"规模：{args.symbols:,} 符号 × {args.runs} 次")
    print()

    all_runs = []
    start_time = time.time()

    for run_idx in range(args.runs):
        print(f"\n{'─'*72}")
        print(f"Run {run_idx + 1}/{args.runs}")
        print(f"{'─'*72}")

        db_path = os.path.join(db_dir, f"_unified_{label}_r{run_idx}.db")
        snap_path = os.path.join(db_dir, f"_unified_{label}_r{run_idx}.cwsnap")

        # ---- SQLite 构建 ----
        sqlite_result = None
        if not args.skip_sqlite:
            print("\n[1/2] SQLite 构建（含逐索引计时）...")
            t_start = time.time()
            sqlite_result = run_single(
                db_path, args.symbols,
                mode="deferred",
                commit_every=10,
                cache_size_kb=CACHE_KB,
                mmap_size=MMAP,
                page_size=PAGE,
                temp_store=TEMP,
                per_index_timing=True,
            )
            print(f"  storage_build: {sqlite_result['timing']['storage_build_s']}s")
            print(f"    schema={sqlite_result['timing']['schema_s']}s, "
                  f"insert={sqlite_result['timing']['insert_s']}s, "
                  f"index={sqlite_result['timing']['index_s']}s, "
                  f"checkpoint={sqlite_result['timing']['checkpoint_s']}s")
            print(f"  db={sqlite_result['storage']['db_mb']}MB, "
                  f"peak_wal={sqlite_result['storage']['peak_wal_mb']}MB, "
                  f"peak_rss={sqlite_result['memory']['peak_rss_mb']}MB")
            # 打印 3 个最慢索引
            idx_list = sqlite_result.get("index_timings") or []
            slowest = sorted(idx_list, key=lambda x: -x.get("time_s", 0))[:5]
            if slowest:
                print(f"  最慢 5 个索引:")
                for idx in slowest:
                    print(f"    {idx['name']}: {idx['time_s']}s")
            print(f"  SQLite 阶段耗时：{time.time() - t_start:.1f}s")

        # ---- GraphStore 基准 ----
        gs_result = None
        if not args.skip_graphstore:
            print("\n[2/2] GraphStore 加载/查询/内存基准...")
            t_start = time.time()
            try:
                gs_result = bench_graphstore(db_path, snap_path)
                t = gs_result["timing"]
                m = gs_result["memory"]
                q = gs_result["query_latency_ms"]
                print(f"  load_from_sqlite: {t['load_from_sqlite_s']}s")
                print(f"  dump_to_file: {t['dump_to_file_s']}s "
                      f"(snap={gs_result['storage']['snap_size_mb']}MB)")
                print(f"  load_from_file: {t['load_from_file_s']}s")
                print(f"  GraphStore RSS: {m['graphstore_rss_mb']}MB")
                print(f"  查询延迟（ms/op）:")
                print(f"    get_symbol:    GS={q['gs_get_symbol']:.3f}  SQL={q['sql_get_symbol']:.3f}  "
                      f"加速={q['sql_get_symbol']/max(q['gs_get_symbol'],0.001):.1f}x")
                print(f"    get_callers:   GS={q['gs_get_callers']:.3f}  SQL={q['sql_get_callers']:.3f}  "
                      f"加速={q['sql_get_callers']/max(q['gs_get_callers'],0.001):.1f}x")
                print(f"    search:        GS={q['gs_search_symbols']:.3f}  SQL={q['sql_search_like']:.3f}  "
                      f"加速={q['sql_search_like']/max(q['gs_search_symbols'],0.001):.1f}x")
                print(f"    call_chain:    GS={q['gs_call_chain_down']:.3f}  SQL={q['sql_call_chain_cte']:.3f}  "
                      f"加速={q['sql_call_chain_cte']/max(q['gs_call_chain_down'],0.001):.1f}x")
                # 内存分解 Top 5
                mb = gs_result["memory_breakdown_mb"]
                top5 = sorted(mb.items(), key=lambda x: -x[1])[:5]
                print(f"  内存 Top 5:")
                for k, v in top5:
                    print(f"    {k}: {v}MB")
                print(f"  GraphStore 阶段耗时：{time.time() - t_start:.1f}s")
            except Exception as e:
                print(f"  GraphStore 测试失败：{e}")
                import traceback
                traceback.print_exc()

        all_runs.append({
            "run": run_idx + 1,
            "sqlite": sqlite_result,
            "graphstore": gs_result,
        })

        # 清理中间 db（保留最后一轮用于后续验证）
        if run_idx < args.runs - 1:
            for p in [db_path, db_path + "-wal", db_path + "-shm", snap_path]:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

    # ---- 中位数汇总 ----
    print(f"\n\n{'='*72}")
    print(f"汇总（{args.runs} 次中位数）— 规模 {label}")
    print(f"{'='*72}")

    def med(key, path=None):
        vals = []
        for r in all_runs:
            obj = r[path] if path else r
            if obj is None:
                continue
            v = obj
            for k in key.split("."):
                v = v.get(k, {}) if isinstance(v, dict) else None
                if v is None:
                    break
            if v is not None and isinstance(v, (int, float)):
                vals.append(v)
        return median(vals) if vals else 0

    # SQLite 汇总
    if not args.skip_sqlite:
        print(f"\nSQLite 构建：")
        print(f"  schema_s:      {med('timing.schema_s', 'sqlite'):.2f}s")
        print(f"  insert_s:      {med('timing.insert_s', 'sqlite'):.2f}s")
        print(f"  index_s:       {med('timing.index_s', 'sqlite'):.2f}s")
        print(f"  checkpoint_s:  {med('timing.checkpoint_s', 'sqlite'):.2f}s")
        print(f"  storage_build: {med('timing.storage_build_s', 'sqlite'):.2f}s")
        print(f"  db_size:       {med('storage.db_mb', 'sqlite'):.1f}MB")
        print(f"  peak_wal:     {med('storage.peak_wal_mb', 'sqlite'):.1f}MB")
        print(f"  peak_rss:     {med('memory.peak_rss_mb', 'sqlite'):.1f}MB")

    # GraphStore 汇总
    if not args.skip_graphstore:
        print(f"\nGraphStore：")
        print(f"  load_from_sqlite: {med('timing.load_from_sqlite_s', 'graphstore'):.2f}s")
        print(f"  dump_to_file:     {med('timing.dump_to_file_s', 'graphstore'):.2f}s")
        print(f"  load_from_file:   {med('timing.load_from_file_s', 'graphstore'):.2f}s")
        print(f"  snap_size:        {med('storage.snap_size_mb', 'graphstore'):.1f}MB")
        print(f"  GraphStore RSS:   {med('memory.graphstore_rss_mb', 'graphstore'):.1f}MB")

        print(f"\n查询延迟（ms/op 中位数）:")
        print(f"  {'查询':<22} {'GraphStore':>12} {'SQL':>12} {'加速比':>10}")
        print(f"  {'-'*58}")
        for gs_key, sql_key, qname in [
            ("gs_get_symbol", "sql_get_symbol", "get_symbol"),
            ("gs_get_callers", "sql_get_callers", "get_callers"),
            ("gs_search_symbols", "sql_search_like", "search_symbols"),
            ("gs_call_chain_down", "sql_call_chain_cte", "call_chain_down"),
        ]:
            gs_v = med(f"query_latency_ms.{gs_key}", "graphstore")
            sql_v = med(f"query_latency_ms.{sql_key}", "graphstore")
            speedup = sql_v / max(gs_v, 0.001)
            print(f"  {qname:<22} {gs_v:>10.3f}ms {sql_v:>10.3f}ms {speedup:>8.1f}x")

    # 保存报告
    report = {
        "env": env,
        "params": {
            "label": label,
            "symbols": args.symbols,
            "runs": args.runs,
            "cache_mb": 256, "mmap_mb": 256, "page_size": 8192,
            "temp_store": "MEMORY", "mode": "deferred",
            "schema_version": 33,  # P7 使用 callee_id 部分索引
        },
        "all_runs": all_runs,
        "total_elapsed_s": round(time.time() - start_time, 1),
    }
    report_path = os.path.join(db_dir, f"_unified_v3_{label}_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n报告已保存：{report_path}")
    print(f"总耗时：{(time.time() - start_time)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
