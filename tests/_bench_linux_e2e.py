"""linux 真实 E2E 基准测试

用 testcode/linux 真实内核代码验证合成基准（v3）的代表性。

测量维度：
1. refresh_all（build_full_graph）真实解析耗时 + RSS
2. 真实符号/调用边分布（验证 1 符号 = 7 调用边的合成假设）
3. GraphStore load_from_sqlite / dump / load_from_file
4. GraphStore 内存分解
5. 真实查询性能（get_symbol / get_callers / search_symbols / call_chain_down）

用法：
  python tests/_bench_linux_e2e.py                    # 全量 linux（63K 文件）
  python tests/_bench_linux_e2e.py --subdir fs       # 仅 fs/ 子集
  python tests/_bench_linux_e2e.py --skip-refresh     # 跳过 refresh，只测 GraphStore
"""
import os
import sys
import json
import time
import sqlite3
import argparse
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 加载 callwarden_core
RUST_TARGET = str(PROJECT_ROOT / "rust_ext" / "target" / "pyinstall")
sys.path.insert(0, RUST_TARGET)

LINUX_ROOT = str(PROJECT_ROOT / "testcode" / "linux")


def _import_graphstore():
    """延迟导入 callwarden_core"""
    import callwarden_core
    return callwarden_core


def _get_db_path_for_workspace(root_path: str) -> str:
    """计算 workspace 对应的数据库路径（复用 config.get_project_db_path）"""
    from config import get_project_db_path
    return get_project_db_path(root_path)


def _median(values):
    """取中位数"""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def bench_refresh(db, subdir: str = None) -> dict:
    """阶段 1：refresh_all 真实解析（计时 + RSS）

    Args:
        db: CodeGraphDB 实例
        subdir: 可选子目录（如 "fs"），None 表示全量
    """
    import psutil
    proc = psutil.Process()

    rss_before = proc.memory_info().rss / 1024 / 1024

    if subdir:
        print(f"[1/5] refresh_directory: {subdir}/ ...")
        t0 = time.perf_counter()
        db.build_directory(subdir)
        t_build = time.perf_counter() - t0
    else:
        print("[1/5] build_full_graph (全量 linux) ...")
        t0 = time.perf_counter()
        db.build_full_graph(force=True)
        t_build = time.perf_counter() - t0

    rss_after = proc.memory_info().rss / 1024 / 1024

    print(f"  refresh 耗时: {t_build:.2f}s")
    print(f"  RSS: {rss_before:.1f}MB -> {rss_after:.1f}MB (峰值 {rss_after:.1f}MB)")

    return {
        "refresh_s": round(t_build, 2),
        "rss_before_mb": round(rss_before, 1),
        "rss_after_mb": round(rss_after, 1),
        "rss_peak_mb": round(rss_after, 1),  # 简化：用 after 作为峰值
        "subdir": subdir or "all",
    }


def bench_stats(db) -> dict:
    """阶段 2：真实符号/调用边分布"""
    print("[2/5] 查询真实分布 ...")

    cur = db.conn.execute("SELECT COUNT(*) as cnt FROM file_instances WHERE status != 'archived'")
    file_count = cur.fetchone()["cnt"]

    cur = db.conn.execute("SELECT COUNT(*) as cnt FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id WHERE fi.status != 'archived'")
    sym_count = cur.fetchone()["cnt"]

    cur = db.conn.execute("SELECT COUNT(*) as cnt FROM calls c JOIN symbols s ON c.caller_id = s.id JOIN file_instances fi ON s.file_instance_id = fi.id WHERE fi.status != 'archived'")
    call_count = cur.fetchone()["cnt"]

    # 符号类型分布
    cur = db.conn.execute("""
        SELECT sc.kind, COUNT(*) as cnt
        FROM symbols s
        JOIN file_instances fi ON s.file_instance_id = fi.id
        JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
        WHERE fi.status != 'archived'
        GROUP BY sc.kind ORDER BY cnt DESC LIMIT 10
    """)
    kind_dist = {row["kind"]: row["cnt"] for row in cur}

    # 每文件符号数分布
    cur = db.conn.execute("""
        SELECT AVG(sym_cnt) as avg, MAX(sym_cnt) as max, MIN(sym_cnt) as min
        FROM (
            SELECT fi.id, COUNT(s.id) as sym_cnt
            FROM file_instances fi
            LEFT JOIN symbols s ON s.file_instance_id = fi.id
            WHERE fi.status != 'archived'
            GROUP BY fi.id
        )
    """)
    sym_per_file = dict(cur.fetchone())

    calls_per_sym = call_count / sym_count if sym_count > 0 else 0

    print(f"  文件数: {file_count}")
    print(f"  符号数: {sym_count}")
    print(f"  调用边: {call_count}")
    print(f"  调用边/符号: {calls_per_sym:.2f}（合成基准假设 7.0）")
    print(f"  符号/文件: avg={sym_per_file.get('avg', 0):.1f}, max={sym_per_file.get('max', 0)}")

    return {
        "file_count": file_count,
        "symbol_count": sym_count,
        "call_count": call_count,
        "calls_per_symbol": round(calls_per_sym, 2),
        "kind_distribution": kind_dist,
        "symbols_per_file": {k: round(v, 1) if isinstance(v, float) else v for k, v in sym_per_file.items()},
    }


def bench_graphstore(db_path: str, snap_path: str) -> dict:
    """阶段 3+4：GraphStore 加载/查询/内存"""
    import psutil
    callwarden_core = _import_graphstore()
    proc = psutil.Process()

    print("[3/5] GraphStore load_from_sqlite / dump / load_from_file ...")

    rss_before = proc.memory_info().rss / 1024 / 1024

    # 1. load_from_sqlite
    store = callwarden_core.GraphStore()
    t0 = time.perf_counter()
    sym_count, edge_count = store.load_from_sqlite(db_path)
    t_load_sqlite = time.perf_counter() - t0

    rss_after_load = proc.memory_info().rss / 1024 / 1024
    stats = store.stats()

    # 2. dump_to_file
    if os.path.exists(snap_path):
        os.remove(snap_path)
    t0 = time.perf_counter()
    store.dump_to_file(snap_path)
    t_dump = time.perf_counter() - t0
    snap_size_mb = os.path.getsize(snap_path) / 1024 / 1024

    # 3. load_from_file（冷启动模拟）
    store_snap = callwarden_core.GraphStore()
    t0 = time.perf_counter()
    store_snap.load_from_file(snap_path)
    t_load_file = time.perf_counter() - t0

    print(f"  load_from_sqlite: {t_load_sqlite:.2f}s ({sym_count} symbols, {edge_count} edges)")
    print(f"  dump_to_file: {t_dump:.2f}s ({snap_size_mb:.1f}MB)")
    print(f"  load_from_file: {t_load_file:.2f}s")
    print(f"  GraphStore RSS: {rss_after_load:.1f}MB")

    # 4. 内存分解
    mem = stats.get("memory", {})
    print(f"  内存分解:")
    for k, v in mem.items():
        if isinstance(v, (int, float)) and v > 1024 * 1024:
            print(f"    {k}: {v / 1024 / 1024:.1f}MB")
        elif isinstance(v, (int, float)):
            print(f"    {k}: {v}")

    # 5. 查询性能
    print("[4/5] GraphStore vs SQL 查询性能 ...")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query_latency = _bench_queries(store_snap, conn, store)

    conn.close()

    return {
        "timing": {
            "load_from_sqlite_s": round(t_load_sqlite, 2),
            "dump_to_file_s": round(t_dump, 2),
            "load_from_file_s": round(t_load_file, 2),
            "snap_size_mb": round(snap_size_mb, 1),
        },
        "memory": {
            "rss_before_mb": round(rss_before, 1),
            "rss_after_load_mb": round(rss_after_load, 1),
            "graphstore_rss_mb": round(rss_after_load - rss_before, 1),
        },
        "stats": stats,
        "query_latency_ms": query_latency,
    }


def _bench_queries(store_snap, conn, store_load) -> dict:
    """查询性能对比"""
    result = {}

    # 取 30 个随机 qname 测 get_symbol
    qnames = [r[0] for r in conn.execute(
        "SELECT qualified_name FROM symbols WHERE qualified_name != '' ORDER BY RANDOM() LIMIT 30"
    ).fetchall()]

    if qnames:
        # GraphStore get_symbol
        t0 = time.perf_counter()
        for qn in qnames:
            _ = store_snap.get_symbol(qn)
        t_gs = (time.perf_counter() - t0) / len(qnames) * 1000

        # SQL get_symbol
        t0 = time.perf_counter()
        for qn in qnames:
            _ = conn.execute("SELECT * FROM symbols WHERE qualified_name = ? LIMIT 1", (qn,)).fetchone()
        t_sql = (time.perf_counter() - t0) / len(qnames) * 1000

        result["get_symbol"] = {"gs_ms": round(t_gs, 4), "sql_ms": round(t_sql, 4)}
        print(f"  get_symbol: GS={t_gs:.4f}ms SQL={t_sql:.4f}ms")

    # get_callers（取 10 个 callee_name）
    callee_names = [r[0] for r in conn.execute(
        "SELECT DISTINCT callee_qualified FROM call_versions WHERE callee_qualified != '' LIMIT 10"
    ).fetchall()]
    # 如果 call_versions 表不存在，降级到 calls.callee_qualified
    if not callee_names:
        try:
            callee_names = [r[0] for r in conn.execute(
                "SELECT DISTINCT callee_qualified FROM calls WHERE callee_qualified != '' LIMIT 10"
            ).fetchall()]
        except Exception:
            callee_names = []

    if callee_names:
        # GraphStore get_callers（用 load 版本，有 backward_edges）
        t0 = time.perf_counter()
        for cn in callee_names:
            _ = store_load.get_callers(cn)
        t_gs = (time.perf_counter() - t0) / len(callee_names) * 1000

        # SQL get_callers（可能全表扫描，超时降级）
        t0 = time.perf_counter()
        for cn in callee_names:
            try:
                _ = conn.execute(
                    "SELECT DISTINCT cv.caller_qualified FROM call_versions cv "
                    "JOIN file_versions fv ON cv.file_version_id = fv.id "
                    "JOIN file_instances fi ON fv.file_instance_id = fi.id "
                    "WHERE fi.workspace_id = ? AND fv.is_current = 1 AND cv.callee_qualified = ?",
                    (1, cn)
                ).fetchall()
            except Exception:
                try:
                    _ = conn.execute(
                        "SELECT DISTINCT c.caller_qualified FROM calls c "
                        "JOIN symbols s ON c.caller_id = s.id "
                        "JOIN file_instances fi ON s.file_instance_id = fi.id "
                        "WHERE fi.workspace_id = ? AND c.callee_qualified = ?",
                        (1, cn)
                    ).fetchall()
                except Exception:
                    pass
        t_sql = (time.perf_counter() - t0) / len(callee_names) * 1000

        result["get_callers"] = {"gs_ms": round(t_gs, 4), "sql_ms": round(t_sql, 4)}
        print(f"  get_callers: GS={t_gs:.4f}ms SQL={t_sql:.4f}ms")

    # search_symbols
    search_terms = ["init", "read", "write", "alloc", "free"]
    t0 = time.perf_counter()
    for term in search_terms:
        _ = store_snap.search_symbols(term, 20)
    t_gs = (time.perf_counter() - t0) / len(search_terms) * 1000

    t0 = time.perf_counter()
    for term in search_terms:
        _ = conn.execute(
            "SELECT qualified_name FROM symbols WHERE qualified_name LIKE ? LIMIT 20",
            (f"%{term}%",)
        ).fetchall()
    t_sql = (time.perf_counter() - t0) / len(search_terms) * 1000

    result["search_symbols"] = {"gs_ms": round(t_gs, 4), "sql_ms": round(t_sql, 4)}
    print(f"  search_symbols: GS={t_gs:.4f}ms SQL={t_sql:.4f}ms")

    # call_chain_down
    if qnames:
        qname = qnames[0]
        t0 = time.perf_counter()
        _ = store_snap.get_call_chain_down(qname, 5)
        t_gs = (time.perf_counter() - t0) * 1000

        result["call_chain_down"] = {"gs_ms": round(t_gs, 4)}
        print(f"  call_chain_down({qname[:40]}): GS={t_gs:.4f}ms")

    return result


def main():
    parser = argparse.ArgumentParser(description="linux 真实 E2E 基准测试")
    parser.add_argument("--subdir", default=None,
                        help="仅测试子目录（如 fs/mm/kernel），默认全量")
    parser.add_argument("--skip-refresh", action="store_true",
                        help="跳过 refresh，仅测量 GraphStore（需先跑过一次 refresh）")
    parser.add_argument("--output", default=None,
                        help="报告 JSON 输出路径")
    args = parser.parse_args()

    from callwarden.db.db import CodeGraphDB

    # 确保 workspace 存在
    print(f"=== linux E2E 基准测试 ===")
    print(f"linux root: {LINUX_ROOT}")
    if args.subdir:
        print(f"子目录: {args.subdir}/")

    db = CodeGraphDB(workspace_root=LINUX_ROOT)

    # workspace 由 _init_workspace 自动注册，直接设置 active
    ws_name = "linux-e2e" if not args.subdir else f"linux-{args.subdir}"
    print(f"\n[0/5] workspace: {ws_name}")
    try:
        db.set_active_workspace(ws_name)
    except Exception:
        # 如果自动注册的 name 不同，尝试用 root_path 注册
        ws_id = db.register_workspace(ws_name, LINUX_ROOT, "linux kernel E2E test")
        db.set_active_workspace(ws_name)
    print(f"  workspace_root: {db.workspace_root}")

    # 获取数据库路径
    db_path = _get_db_path_for_workspace(LINUX_ROOT)
    snap_path = db_path + ".snap"
    print(f"  db_path: {db_path}")

    if not args.skip_refresh:
        # 阶段 1：refresh
        refresh_result = bench_refresh(db, args.subdir)
    else:
        print("[1/5] 跳过 refresh（--skip-refresh）")
        refresh_result = {"skipped": True}

    # 阶段 2：真实分布
    stats_result = bench_stats(db)

    # 阶段 3+4：GraphStore
    graphstore_result = bench_graphstore(db_path, snap_path)

    # 阶段 5：汇总报告
    print("\n[5/5] 生成报告 ...")
    report = {
        "test_type": "linux_e2e",
        "linux_root": LINUX_ROOT,
        "subdir": args.subdir,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "refresh": refresh_result,
        "distribution": stats_result,
        "graphstore": graphstore_result,
    }

    # 对比 v3 合成基准
    v3_comparison = _compare_with_v3(stats_result, graphstore_result)
    report["v3_comparison"] = v3_comparison

    # 输出
    output_path = args.output or str(Path(__file__).parent / f"_linux_e2e_{args.subdir or 'all'}_report.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n报告已保存: {output_path}")

    # 打印对比摘要
    _print_comparison_summary(report)


def _compare_with_v3(stats_result, graphstore_result) -> dict:
    """对比 v3 合成基准"""
    sym_count = stats_result.get("symbol_count", 0)
    call_count = stats_result.get("call_count", 0)
    calls_per_sym = stats_result.get("calls_per_symbol", 0)

    # 找到最接近的 v3 规模
    if sym_count < 500000:
        v3_scale = "1M"
        v3_storage_build = 73.17
        v3_load_sqlite = 10.78
        v3_load_file = 3.69
        v3_rss = 727.2
    elif sym_count < 1500000:
        v3_scale = "1M"
        v3_storage_build = 73.17
        v3_load_sqlite = 10.78
        v3_load_file = 3.69
        v3_rss = 727.2
    else:
        v3_scale = "2M"
        v3_storage_build = 123.21
        v3_load_sqlite = 16.21
        v3_load_file = 6.48
        v3_rss = 1465.1

    gs_timing = graphstore_result.get("timing", {})
    gs_mem = graphstore_result.get("memory", {})

    return {
        "v3_scale": v3_scale,
        "calls_per_symbol": {
            "linux_real": calls_per_sym,
            "v3_synthetic": 7.0,
            "deviation_pct": round((calls_per_sym - 7.0) / 7.0 * 100, 1),
        },
        "graphstore_rss": {
            "linux_real_mb": gs_mem.get("graphstore_rss_mb", 0),
            f"v3_{v3_scale}_mb": v3_rss,
        },
        "load_from_sqlite": {
            "linux_real_s": gs_timing.get("load_from_sqlite_s", 0),
            f"v3_{v3_scale}_s": v3_load_sqlite,
        },
        "load_from_file": {
            "linux_real_s": gs_timing.get("load_from_file_s", 0),
            f"v3_{v3_scale}_s": v3_load_file,
        },
    }


def _print_comparison_summary(report: dict):
    """打印对比摘要"""
    print("\n" + "=" * 60)
    print("linux E2E vs v3 合成基准 对比摘要")
    print("=" * 60)

    dist = report.get("distribution", {})
    gs = report.get("graphstore", {})
    cmp = report.get("v3_comparison", {})

    print(f"\n规模:")
    print(f"  文件数: {dist.get('file_count', 0)}")
    print(f"  符号数: {dist.get('symbol_count', 0)}")
    print(f"  调用边: {dist.get('call_count', 0)}")

    calls_cmp = cmp.get("calls_per_symbol", {})
    print(f"\n调用边/符号:")
    print(f"  linux 真实: {calls_cmp.get('linux_real', 0)}")
    print(f"  v3 合成:   {calls_cmp.get('v3_synthetic', 0)}")
    print(f"  偏差: {calls_cmp.get('deviation_pct', 0)}%")

    gs_timing = gs.get("timing", {})
    gs_mem = gs.get("memory", {})
    print(f"\nGraphStore 性能:")
    print(f"  load_from_sqlite: {gs_timing.get('load_from_sqlite_s', 0)}s (v3 {cmp.get('load_from_sqlite', {}).get('v3_1M_s', 'N/A')}s)")
    print(f"  load_from_file:   {gs_timing.get('load_from_file_s', 0)}s (v3 {cmp.get('load_from_file', {}).get('v3_1M_s', 'N/A')}s)")
    print(f"  GraphStore RSS:   {gs_mem.get('graphstore_rss_mb', 0)}MB")

    print(f"\n符号类型分布:")
    for kind, cnt in list(dist.get("kind_distribution", {}).items())[:5]:
        print(f"  {kind}: {cnt}")


if __name__ == "__main__":
    main()
