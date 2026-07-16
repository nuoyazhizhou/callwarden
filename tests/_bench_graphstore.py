"""
GraphStore P1: 1M 规模实际加载和查询性能基准

目标：
1. 测量 load_from_sqlite 耗时（10M 外推依据）
2. 测量 GraphStore 查询性能 vs SQL 查询性能对比
3. 测量内存占用
4. 找出优化空间

前置条件：
- 先用 _bench_baseline_v2.py 生成 1M 规模数据库（已有 _bench_v2_1m.db）
- 或新建一个 1M 数据库
"""
import os
import sys
import time
import json
import psutil
import sqlite3
from pathlib import Path

# 复用 v2 的数据生成和建表逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bench_baseline_v2 import (
    gen_symbols_stream, gen_calls_stream, split_index_statements,
    collect_env_info
)

# 加载正式 schema
import importlib.util
_schema_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "db", "schema.py")
_spec = importlib.util.spec_from_file_location("callwarden_schema", _schema_path)
_schema_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_schema_mod)
SCHEMA_TABLES_SQL = _schema_mod.SCHEMA_TABLES_SQL
SCHEMA_INDEXES_SQL = _schema_mod.SCHEMA_INDEXES_SQL


def build_test_db(db_path: str, target_symbols: int = 1000000,
                  cache_size_kb: int = 262144, page_size: int = 8192):
    """构建测试数据库（使用 P13+P15 最优参数）"""
    if os.path.exists(db_path):
        # 检查是否已有足够数据
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            if count >= target_symbols * 0.9:
                print(f"  数据库已存在，{count:,} 符号，跳过构建")
                return
        except:
            pass
        finally:
            conn.close()

    print(f"  构建测试数据库：{target_symbols:,} 符号")
    for suffix in ["", "-wal", "-shm"]:
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA page_size=8192")  # P15: 必须在 WAL 之前
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA cache_size=-{cache_size_kb}")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    # 建表（无索引）
    conn.executescript(SCHEMA_TABLES_SQL)
    conn.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at, is_active, description) "
        "VALUES (1, 'synthetic', '/synthetic', ?, 1, '压测')",
        (time.time(),)
    )
    conn.commit()

    # 先插入 file_instances（GraphStore 加载时 JOIN file_instances，必须有数据）
    n_files = max(1, target_symbols // 5)
    print(f"  插入 {n_files:,} file_instances...")
    file_batch = []
    for i in range(1, n_files + 1):
        dir_idx = i // 20
        rel_path = f"src/module_{dir_idx}/file_{i}.py"
        file_batch.append((
            i,                      # id
            1,                      # workspace_id
            rel_path,               # rel_path
            f"/{rel_path}",         # abs_path
            f"hash_{i:08x}",       # current_content_hash
            float(i),               # mtime
            100,                    # total_lines
            0.0,                    # last_parsed
            "active",               # status
            f"module_{dir_idx}",    # module_path
        ))
    conn.executemany(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, total_lines, last_parsed, status, module_path) VALUES (?,?,?,?,?,?,?,?,?,?)",
        file_batch
    )
    conn.commit()

    # 流式入库 symbols
    sym_insert_sql = (
        "INSERT INTO symbols (id, file_instance_id, symbol_hash, name, kind, visibility, "
        "start_line, end_line, start_col, end_col, signature, has_comment, comment_status, "
        "module_path, qualified_name, depth) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )

    BATCH = 50000
    sym_id_offset = 0
    for i, batch in enumerate(gen_symbols_stream(target_symbols, n_files, BATCH)):
        conn.executemany(sym_insert_sql, batch)
        if (i + 1) % 10 == 0:
            conn.commit()
    conn.commit()

    # 调用边
    call_insert_sql = (
        "INSERT INTO calls (id, caller_id, caller_name, caller_module, callee_name, "
        "callee_module, callee_qualified, callee_file, callee_id, call_line, is_cross_file) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    )
    BATCH_CALL = 100000
    for i, batch in enumerate(gen_calls_stream(target_symbols, BATCH_CALL)):
        conn.executemany(call_insert_sql, batch)
        if (i + 1) % 10 == 0:
            conn.commit()
    conn.commit()

    # 建索引
    conn.executescript(SCHEMA_INDEXES_SQL)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()

    sym_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    call_count = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    print(f"  完成：{sym_count:,} 符号, {call_count:,} 调用边")

    conn.close()


def bench_graphstore_load(db_path: str, runs: int = 3) -> dict:
    """测试 GraphStore 加载性能"""
    try:
        from callwarden_core import GraphStore
    except ImportError:
        return {"error": "callwarden_core 未安装"}

    load_times = []
    stats = None

    for run in range(runs):
        store = GraphStore()
        t0 = time.perf_counter()
        store.load_from_sqlite(db_path)
        t_load = time.perf_counter() - t0

        if run == 0:
            stats = store.stats()
            # 测量内存
            proc = psutil.Process()
            rss_mb = proc.memory_info().rss / 1024 / 1024

        load_times.append(t_load)
        print(f"  Run {run+1}: load={t_load:.2f}s")
        del store

    load_median = sorted(load_times)[len(load_times) // 2]
    return {
        "load_times_s": [round(t, 2) for t in load_times],
        "load_median_s": round(load_median, 2),
        "stats": stats,
        "peak_rss_mb": round(rss_mb, 1) if stats else 0,
    }


def bench_graphstore_queries(db_path: str, n_queries: int = 100) -> dict:
    """测试 GraphStore 查询性能 vs SQL 查询性能"""
    try:
        from callwarden_core import GraphStore
    except ImportError:
        return {"error": "callwarden_core 未安装"}

    # 先建一个 SQL 连接用于对比
    sql_conn = sqlite3.connect(db_path)
    sql_conn.execute("PRAGMA cache_size=-262144")
    sql_conn.execute("PRAGMA temp_store=MEMORY")

    # 加载 GraphStore
    store = GraphStore()
    t0 = time.perf_counter()
    store.load_from_sqlite(db_path)
    load_time = time.perf_counter() - t0
    print(f"  GraphStore 加载：{load_time:.2f}s")

    # 随机选 100 个 callee_name 作为查询样本
    cur = sql_conn.execute(
        "SELECT callee_name FROM calls WHERE callee_name != '' "
        "GROUP BY callee_name ORDER BY RANDOM() LIMIT ?",
        (n_queries,)
    )
    callee_names = [r[0] for r in cur.fetchall()]

    cur = sql_conn.execute(
        "SELECT qualified_name FROM symbols WHERE qualified_name != '' "
        "ORDER BY RANDOM() LIMIT ?",
        (n_queries,)
    )
    qnames = [r[0] for r in cur.fetchall()]

    cur = sql_conn.execute(
        "SELECT DISTINCT name FROM symbols WHERE kind = 'fn' LIMIT ?",
        (n_queries,)
    )
    search_terms = [r[0] for r in cur.fetchall()]

    # === 1. get_callers 性能对比（公平对比：都获取完整数据）===
    # GraphStore
    t0 = time.perf_counter()
    gs_results = 0
    for name in callee_names:
        batch = store.get_callers(name)
        # 实际访问数据，触发懒转换
        if hasattr(batch, 'caller_ids'):
            gs_results += len(batch.caller_ids)
        elif hasattr(batch, 'caller_names'):
            gs_results += len(batch.caller_names)
    t_gs_callers = time.perf_counter() - t0

    # SQL（获取完整 caller_ids，不是 COUNT）
    t0 = time.perf_counter()
    sql_results = 0
    for name in callee_names:
        cur = sql_conn.execute(
            "SELECT caller_id FROM calls WHERE callee_name = ?", (name,)
        )
        sql_results += len(cur.fetchall())
    t_sql_callers = time.perf_counter() - t0

    # === 2. get_callees 性能对比 ===
    # GraphStore
    t0 = time.perf_counter()
    for qname in qnames:
        try:
            store.get_callees(qname)
        except:
            pass
    t_gs_callees = time.perf_counter() - t0

    # SQL
    t0 = time.perf_counter()
    for qname in qnames:
        cur = sql_conn.execute(
            "SELECT c.* FROM calls c JOIN symbols s ON c.caller_id = s.id "
            "WHERE s.qualified_name = ?", (qname,)
        )
        cur.fetchall()
    t_sql_callees = time.perf_counter() - t0

    # === 3. search_symbols 性能对比 ===
    # GraphStore
    t0 = time.perf_counter()
    for term in search_terms:
        try:
            store.search_symbols(term, "", 100)
        except:
            pass
    t_gs_search = time.perf_counter() - t0

    # SQL
    t0 = time.perf_counter()
    for term in search_terms:
        cur = sql_conn.execute(
            "SELECT * FROM symbols WHERE name LIKE ? LIMIT 100",
            (f"%{term}%",)
        )
        cur.fetchall()
    t_sql_search = time.perf_counter() - t0

    # === 4. get_symbol 性能对比 ===
    # GraphStore
    t0 = time.perf_counter()
    for qname in qnames:
        store.get_symbol(qname)
    t_gs_get = time.perf_counter() - t0

    # SQL
    t0 = time.perf_counter()
    for qname in qnames:
        cur = sql_conn.execute(
            "SELECT * FROM symbols WHERE qualified_name = ? LIMIT 1", (qname,)
        )
        cur.fetchone()
    t_sql_get = time.perf_counter() - t0

    # === 5. 图遍历性能对比（GraphStore 的真正优势领域）===
    # get_call_chain_down（BFS 遍历，depth=5）
    gs_chain_results = 0
    t0 = time.perf_counter()
    for qname in qnames[:20]:  # 只取前 20 个，避免太慢
        try:
            result = store.get_call_chain_down(qname, 5)
            gs_chain_results += len(result) if result else 0
        except:
            pass
    t_gs_chain = time.perf_counter() - t0

    # SQL 递归 CTE（公平对比）
    sql_chain_results = 0
    t0 = time.perf_counter()
    for qname in qnames[:20]:
        cur = sql_conn.execute(
            "WITH RECURSIVE chain AS ( "
            "  SELECT c.callee_name, c.callee_id, 1 as depth "
            "  FROM calls c JOIN symbols s ON c.caller_id = s.id "
            "  WHERE s.qualified_name = ? "
            "  UNION ALL "
            "  SELECT c2.callee_name, c2.callee_id, ch.depth + 1 "
            "  FROM calls c2 JOIN chain ch ON c2.caller_id = ch.callee_id "
            "  WHERE ch.depth < 5 "
            ") SELECT COUNT(*) FROM chain",
            (qname,)
        )
        sql_chain_results += cur.fetchone()[0]
    t_sql_chain = time.perf_counter() - t0

    # === 6. 批量查询（100 个 callee 一次性）===
    # GraphStore 批量
    t0 = time.perf_counter()
    for name in callee_names:
        batch = store.get_callers(name)
        _ = batch.caller_ids if hasattr(batch, 'caller_ids') else 0
    t_gs_batch = time.perf_counter() - t0

    # SQL 批量（用 IN 子句一次性查）
    t0 = time.perf_counter()
    placeholders = ",".join("?" * len(callee_names))
    cur = sql_conn.execute(
        f"SELECT callee_name, COUNT(*) FROM calls "
        f"WHERE callee_name IN ({placeholders}) "
        f"GROUP BY callee_name",
        callee_names
    )
    cur.fetchall()
    t_sql_batch = time.perf_counter() - t0

    # 结果汇总
    proc = psutil.Process()
    rss_mb = proc.memory_info().rss / 1024 / 1024

    result = {
        "n_queries": n_queries,
        "load_time_s": round(load_time, 2),
        "graphstore": {
            "get_callers_total_ms": round(t_gs_callers * 1000, 2),
            "get_callers_avg_ms": round(t_gs_callers / n_queries * 1000, 3),
            "get_callees_total_ms": round(t_gs_callees * 1000, 2),
            "get_callees_avg_ms": round(t_gs_callees / n_queries * 1000, 3),
            "search_symbols_total_ms": round(t_gs_search * 1000, 2),
            "search_symbols_avg_ms": round(t_gs_search / n_queries * 1000, 3),
            "get_symbol_total_ms": round(t_gs_get * 1000, 2),
            "get_symbol_avg_ms": round(t_gs_get / n_queries * 1000, 3),
            "call_chain_total_ms": round(t_gs_chain * 1000, 2),
            "call_chain_avg_ms": round(t_gs_chain / 20 * 1000, 3),
            "batch_callers_total_ms": round(t_gs_batch * 1000, 2),
        },
        "sql": {
            "get_callers_total_ms": round(t_sql_callers * 1000, 2),
            "get_callers_avg_ms": round(t_sql_callers / n_queries * 1000, 3),
            "get_callees_total_ms": round(t_sql_callees * 1000, 2),
            "get_callees_avg_ms": round(t_sql_callees / n_queries * 1000, 3),
            "search_symbols_total_ms": round(t_sql_search * 1000, 2),
            "search_symbols_avg_ms": round(t_sql_search / n_queries * 1000, 3),
            "get_symbol_total_ms": round(t_sql_get * 1000, 2),
            "get_symbol_avg_ms": round(t_sql_get / n_queries * 1000, 3),
            "call_chain_total_ms": round(t_sql_chain * 1000, 2),
            "call_chain_avg_ms": round(t_sql_chain / 20 * 1000, 3),
            "batch_callers_total_ms": round(t_sql_batch * 1000, 2),
        },
        "speedup": {
            "get_callers": round(t_sql_callers / t_gs_callers, 2) if t_gs_callers > 0 else 0,
            "get_callees": round(t_sql_callees / t_gs_callees, 2) if t_gs_callees > 0 else 0,
            "search_symbols": round(t_sql_search / t_gs_search, 2) if t_gs_search > 0 else 0,
            "get_symbol": round(t_sql_get / t_gs_get, 2) if t_gs_get > 0 else 0,
            "call_chain": round(t_sql_chain / t_gs_chain, 2) if t_gs_chain > 0 else 0,
            "batch_callers": round(t_sql_batch / t_gs_batch, 2) if t_gs_batch > 0 else 0,
        },
        "peak_rss_mb": round(rss_mb, 1),
    }

    sql_conn.close()
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="GraphStore P1: 1M 规模性能基准")
    parser.add_argument("--symbols", type=int, default=1000000, help="目标符号数")
    parser.add_argument("--db", type=str, default="", help="数据库路径")
    parser.add_argument("--queries", type=int, default=100, help="每种查询测试次数")
    parser.add_argument("--runs", type=int, default=3, help="加载测试重复次数")
    parser.add_argument("--skip-build", action="store_true", help="跳过数据库构建")
    parser.add_argument("--skip-load", action="store_true", help="跳过加载测试")
    parser.add_argument("--skip-query", action="store_true", help="跳过查询测试")
    args = parser.parse_args()

    env = collect_env_info()
    db_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = args.db or os.path.join(db_dir, f"_graphstore_{args.symbols // 1000000}m.db")

    print(f"\n{'='*70}")
    print(f"GraphStore P1: {args.symbols:,} 符号规模性能基准")
    print(f"{'='*70}")
    print(f"环境：SQLite {env['sqlite_version']}, Python {env['python_version']}")
    print(f"      RAM {env.get('ram_total_gb', '?')}GB, available {env.get('ram_available_mb', '?')}MB")
    print(f"数据库：{db_path}")
    print()

    # 1. 构建测试数据库（如需要）
    if not args.skip_build:
        print("[1/3] 构建测试数据库...")
        build_test_db(db_path, args.symbols)
    else:
        print("[1/3] 跳过数据库构建")

    # 2. 测试 GraphStore 加载性能
    load_result = {}
    if not args.skip_load:
        print(f"\n[2/3] 测试 GraphStore 加载性能（{args.runs} 次）...")
        load_result = bench_graphstore_load(db_path, args.runs)
        print(f"  中位数加载时间：{load_result.get('load_median_s', 0)}s")
        if load_result.get("stats"):
            print(f"  GraphStore stats：{load_result['stats']}")
        print(f"  峰值 RSS：{load_result.get('peak_rss_mb', 0)} MB")
    else:
        print("\n[2/3] 跳过加载测试")

    # 3. 测试查询性能
    query_result = {}
    if not args.skip_query:
        print(f"\n[3/3] 测试查询性能（{args.queries} 次/查询类型）...")
        query_result = bench_graphstore_queries(db_path, args.queries)

        print(f"\n{'='*70}")
        print(f"查询性能对比（{args.queries} 次，图遍历 20 次）")
        print(f"{'='*70}")
        print(f"{'查询类型':<20} {'GraphStore':>15} {'SQL':>15} {'加速比':>10}")
        print(f"{'─'*65}")
        for q in ["get_callers", "get_callees", "search_symbols", "get_symbol",
                  "call_chain", "batch_callers"]:
            gs = query_result["graphstore"].get(f"{q}_avg_ms",
                                                 query_result["graphstore"].get(f"{q}_total_ms", 0))
            sql = query_result["sql"].get(f"{q}_avg_ms",
                                          query_result["sql"].get(f"{q}_total_ms", 0))
            sp = query_result["speedup"].get(q, 0)
            print(f"  {q:<18} {gs:>12.3f}ms {sql:>12.3f}ms {sp:>8.2f}x")

        print(f"\n  加载时间：{query_result['load_time_s']}s")
        print(f"  峰值 RSS：{query_result['peak_rss_mb']} MB")
    else:
        print("\n[3/3] 跳过查询测试")

    # 保存报告
    report = {
        "env": env,
        "params": {"symbols": args.symbols, "queries": args.queries, "runs": args.runs},
        "load": load_result,
        "query": query_result,
    }
    report_path = os.path.join(db_dir, f"_graphstore_{args.symbols // 1000000}m_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n报告已保存：{report_path}")


if __name__ == "__main__":
    main()
