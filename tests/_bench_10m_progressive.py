"""千万级符号渐进式压测：定位内存与耗时瓶颈

分 4 个规模阶梯（200万 / 500万 / 700万 / 1000万符号），
每个规模记录：
  - Stage A: GraphStore.load_from_sqlite（加载到 CSR）
  - Stage B: compute_depth_all（拓扑深度计算）
  - Stage C: 查询性能（get_callers/get_callees/search_symbols/get_topological_order）
  - Stage D: RSS 内存占用

每个阶段记录 RSS 增量 + 耗时，定位哪个阶段内存暴涨 / 耗时过长。
从最小规模开始，逐步定位瓶颈，从底层逐个优化。

用法：
    $env:PYTHONPATH = "rust_ext/target/pyinstall"
    python tests/_bench_10m_progressive.py
"""
from __future__ import annotations

import os
import sys
import time
import json
import gc
import traceback

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

_RUST_INSTALL = os.path.join(_PKG_ROOT, 'rust_ext', 'target', 'pyinstall')
if os.path.isdir(_RUST_INSTALL) and _RUST_INSTALL not in sys.path:
    sys.path.insert(0, _RUST_INSTALL)


def get_rss_mb() -> float:
    """获取当前进程 RSS（单位 MB）"""
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except ImportError:
        return 0.0


# ============================================
# 合成 DB 生成（复用 _bench_graph_store_large.py 的生成器）
# ============================================

def generate_synthetic_db(db_path: str, target_symbols: int, seed: int = 42):
    """生成合成 SQLite 数据库

    Args:
        db_path: 数据库文件路径
        target_symbols: 目标符号数
        seed: 随机种子（可复现）
    """
    import random
    import sqlite3

    rng = random.Random(seed)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # 建表（对齐 callwarden schema）
    conn.executescript("""
        CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY,
            root_path TEXT,
            name TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            is_active INTEGER DEFAULT 0
        );
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL,
            abs_path TEXT,
            content_hash TEXT,
            total_lines INTEGER DEFAULT 0,
            language TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY,
            file_instance_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            module_path TEXT DEFAULT '',
            start_line INTEGER DEFAULT 0,
            end_line INTEGER DEFAULT 0,
            depth INTEGER DEFAULT -1,
            symbol_hash TEXT DEFAULT '',
            has_comment INTEGER DEFAULT 0,
            visibility TEXT DEFAULT '',
            content TEXT DEFAULT '',
            signature TEXT DEFAULT ''
        );
        CREATE TABLE calls (
            id INTEGER PRIMARY KEY,
            caller_id INTEGER NOT NULL,
            caller_name TEXT DEFAULT '',
            caller_qualified TEXT DEFAULT '',
            callee_name TEXT NOT NULL,
            callee_qualified TEXT DEFAULT '',
            callee_module TEXT DEFAULT '',
            callee_id INTEGER DEFAULT 0,
            call_line INTEGER DEFAULT 0,
            is_cross_file INTEGER DEFAULT 0,
            file_instance_id INTEGER DEFAULT 0
        );
        CREATE INDEX idx_symbols_qname ON symbols(qualified_name);
        CREATE INDEX idx_symbols_name ON symbols(name);
        CREATE INDEX idx_symbols_file ON symbols(file_instance_id);
        CREATE INDEX idx_calls_caller ON calls(caller_id);
        CREATE INDEX idx_calls_callee_id ON calls(callee_id);
        CREATE INDEX idx_calls_callee_name ON calls(callee_name);
    """)

    # 1. workspace
    conn.execute("INSERT INTO workspaces (id, root_path, name, is_active) VALUES (1, '/synthetic', 'synthetic', 1)")

    # 2. 文件（平均每文件 5 个符号）
    n_files = max(1, target_symbols // 5)
    n_symbols = target_symbols
    n_fns = n_symbols * 7 // 10  # 70% 函数
    n_classes = n_symbols * 2 // 10  # 20% 类
    n_structs = n_symbols - n_fns - n_classes  # 10% 结构体

    print(f"  生成 {n_files} 文件, {n_symbols} 符号 (fn={n_fns}, class={n_classes}, struct={n_structs})...")

    # 批量插入文件
    file_batch = []
    for i in range(1, n_files + 1):
        dir_idx = i // 20
        rel_path = f"src/module_{dir_idx}/file_{i}.py"
        file_batch.append((i, 1, rel_path, f"/{rel_path}", "abc123", 100, "python", "active"))
    conn.executemany(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, content_hash, total_lines, language, status) VALUES (?,?,?,?,?,?,?,?)",
        file_batch
    )

    # 批量插入符号
    sym_batch = []
    fn_ids = []  # 记录所有函数 ID（用于生成调用边）
    sym_names = []  # 记录 (id, name) 用于生成调用边

    sym_id = 0
    for i in range(n_fns):
        sym_id += 1
        fid = (i % n_files) + 1
        name = f"func_{i}"
        qname = f"module_{i // 100}.func_{i}"
        sym_batch.append((sym_id, fid, name, qname, "fn", "", 1, 10, -1, "", 0, "", "", ""))
        fn_ids.append(sym_id)
        sym_names.append((sym_id, name))
    for i in range(n_classes):
        sym_id += 1
        fid = ((n_fns + i) % n_files) + 1
        name = f"Class_{i}"
        qname = f"module_{i // 100}.Class_{i}"
        sym_batch.append((sym_id, fid, name, qname, "class", "", 1, 50, -1, "", 0, "", "", ""))
        sym_names.append((sym_id, name))
    for i in range(n_structs):
        sym_id += 1
        fid = ((n_fns + n_classes + i) % n_files) + 1
        name = f"Struct_{i}"
        qname = f"module_{i // 100}.Struct_{i}"
        sym_batch.append((sym_id, fid, name, qname, "struct", "", 1, 20, -1, "", 0, "", "", ""))

    # 分批插入符号（每批 50000）
    batch_size = 50000
    for start in range(0, len(sym_batch), batch_size):
        conn.executemany(
            "INSERT INTO symbols (id, file_instance_id, name, qualified_name, kind, module_path, start_line, end_line, depth, symbol_hash, has_comment, visibility, content, signature) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            sym_batch[start:start + batch_size]
        )
    conn.commit()
    print(f"  符号插入完成 ({n_symbols})")

    # 3. 调用边：每个函数随机调用 5-15 个其他函数
    # 30% 未解析（callee_id=0，外部库）
    n_calls_est = n_fns * 10  # 平均 10 条调用/函数
    print(f"  生成 ~{n_calls_est} 调用边...")

    call_batch = []
    call_id = 0
    for caller_id in fn_ids:
        n_callees = rng.randint(5, 15)
        for _ in range(n_callees):
            call_id += 1
            if rng.random() < 0.3:
                # 30% 未解析（外部调用）
                callee_name = f"ext_func_{rng.randint(0, 99999)}"
                callee_id = 0
                callee_qname = ""
            else:
                # 70% 已解析
                callee_idx = rng.randint(0, len(sym_names) - 1)
                callee_id, callee_name = sym_names[callee_idx]
                callee_qname = f"module_{callee_idx // 100}.{callee_name}"

            caller_name = f"func_{caller_id - 1}"
            caller_qname = f"module_{(caller_id-1) // 100}.func_{caller_id - 1}"
            caller_fid = ((caller_id - 1) % n_files) + 1
            is_cross = 1 if rng.random() < 0.4 else 0
            call_line = rng.randint(1, 100)

            call_batch.append((call_id, caller_id, caller_name, caller_qname,
                               callee_name, callee_qname, "", callee_id, call_line, is_cross, caller_fid))

        # 每 100000 条提交一次
        if len(call_batch) >= 100000:
            conn.executemany(
                "INSERT INTO calls (id, caller_id, caller_name, caller_qualified, callee_name, callee_qualified, callee_module, callee_id, call_line, is_cross_file, file_instance_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                call_batch
            )
            conn.commit()
            call_batch.clear()

    if call_batch:
        conn.executemany(
            "INSERT INTO calls (id, caller_id, caller_name, caller_qualified, callee_name, callee_qualified, callee_module, callee_id, call_line, is_cross_file, file_instance_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            call_batch
        )
    conn.commit()

    actual_calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    actual_symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    db_size = os.path.getsize(db_path)

    conn.close()
    print(f"  调用边插入完成 ({actual_calls})")
    print(f"  数据库大小: {db_size / 1024 / 1024:.1f} MB")

    return {
        "symbols": actual_symbols,
        "calls": actual_calls,
        "files": n_files,
        "db_size_mb": round(db_size / 1024 / 1024, 1),
    }


# ============================================
# 渐进式压测
# ============================================

# 支持命令行参数指定规模：python _bench_10m_progressive.py 2M
# 不指定则跑全部 4 个规模
import argparse
_parser = argparse.ArgumentParser()
_parser.add_argument('scale', nargs='?', default='all',
                     help='指定规模: 2M/5M/7M/10M/all')
_args, _ = _parser.parse_known_args()

_ALL_SCALES = [
    {"name": "2M", "symbols": 2_000_000, "label": "200万"},
    {"name": "5M", "symbols": 5_000_000, "label": "500万"},
    {"name": "7M", "symbols": 7_000_000, "label": "700万"},
    {"name": "10M", "symbols": 10_000_000, "label": "1000万"},
]
if _args.scale == 'all':
    SCALES = _ALL_SCALES
else:
    SCALES = [s for s in _ALL_SCALES if s['name'] == _args.scale]
    if not SCALES:
        print(f"未知规模: {_args.scale}，可选: 2M/5M/7M/10M/all")
        sys.exit(1)


def bench_scale(scale: dict, tmpdir: str) -> dict:
    """测试单个规模"""
    scale_name = scale["name"]
    n_symbols = scale["symbols"]
    label = scale["label"]

    print(f"\n{'=' * 75}")
    print(f"  规模: {label} ({scale_name}, 目标 {n_symbols:,} 符号)")
    print(f"{'=' * 75}")

    result = {
        "scale": scale_name,
        "label": label,
        "target_symbols": n_symbols,
    }

    # ---- 0. 生成合成 DB ----
    db_path = os.path.join(tmpdir, f"synth_{scale_name}.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    for ext in ['-wal', '-shm']:
        p = db_path + ext
        if os.path.exists(p):
            os.remove(p)

    print(f"\n[0/4] 生成合成数据库...")
    rss0 = get_rss_mb()
    t0 = time.perf_counter()
    gen_info = generate_synthetic_db(db_path, n_symbols)
    gen_time = time.perf_counter() - t0
    rss1 = get_rss_mb()
    print(f"  生成耗时: {gen_time:.2f}s | RSS: {rss0:.1f} → {rss1:.1f} MB (+{rss1 - rss0:.1f})")
    print(f"  实际符号: {gen_info['symbols']:,} | 调用边: {gen_info['calls']:,} | DB: {gen_info['db_size_mb']} MB")
    result["gen"] = {**gen_info, "gen_time_sec": round(gen_time, 2)}

    # 检查 Rust 扩展
    try:
        from callwarden_core import GraphStore
    except ImportError:
        print("ERROR: callwarden_core 未安装，跳过 Rust 测试")
        result["error"] = "Rust 扩展未安装"
        return result

    # ---- Stage A: GraphStore.load_from_sqlite ----
    print(f"\n[A/4] GraphStore.load_from_sqlite（加载到 CSR）...")
    gc.collect()
    rss_before = get_rss_mb()
    t0 = time.perf_counter()
    store = GraphStore()
    try:
        n_sym, n_edge = store.load_from_sqlite(db_path)
        load_time = time.perf_counter() - t0
        rss_after = get_rss_mb()
        rss_delta = rss_after - rss_before
        print(f"  加载耗时: {load_time:.2f}s")
        print(f"  符号数: {n_sym:,} | 边数: {n_edge:,}")
        print(f"  RSS: {rss_before:.1f} → {rss_after:.1f} MB (+{rss_delta:.1f} MB)")
        print(f"  内存效率: {rss_delta / max(n_sym, 1) * 1024:.2f} KB/符号")
        result["stage_a_load"] = {
            "load_time_sec": round(load_time, 3),
            "symbols_loaded": n_sym,
            "edges_loaded": n_edge,
            "rss_before_mb": round(rss_before, 1),
            "rss_after_mb": round(rss_after, 1),
            "rss_delta_mb": round(rss_delta, 1),
            "kb_per_symbol": round(rss_delta / max(n_sym, 1) * 1024, 3),
        }
    except Exception as e:
        print(f"  [失败] load_from_sqlite: {e}")
        traceback.print_exc()
        result["stage_a_load"] = {"error": str(e)}
        return result

    # ---- Stage B: compute_depth_all ----
    print(f"\n[B/4] compute_depth_all（拓扑深度计算）...")
    gc.collect()
    rss_before = get_rss_mb()
    t0 = time.perf_counter()
    try:
        depth_updates = store.compute_depth_all()
        depth_time = time.perf_counter() - t0
        rss_after = get_rss_mb()
        rss_delta = rss_after - rss_before
        print(f"  depth 耗时: {depth_time:.2f}s")
        print(f"  depth 更新数: {len(depth_updates):,}")
        print(f"  RSS: {rss_before:.1f} → {rss_after:.1f} MB (+{rss_delta:.1f} MB)")
        result["stage_b_depth"] = {
            "depth_time_sec": round(depth_time, 3),
            "updates_count": len(depth_updates),
            "rss_before_mb": round(rss_before, 1),
            "rss_after_mb": round(rss_after, 1),
            "rss_delta_mb": round(rss_delta, 1),
        }
    except Exception as e:
        print(f"  [失败] compute_depth_all: {e}")
        traceback.print_exc()
        result["stage_b_depth"] = {"error": str(e)}

    # ---- Stage C: 查询性能 ----
    print(f"\n[C/4] 查询性能测试...")
    import sqlite3
    conn = sqlite3.connect(db_path)

    # 准备测试数据：取 20 个有调用者的函数名
    rows = conn.execute(
        "SELECT callee_name FROM calls WHERE callee_name != '' "
        "GROUP BY callee_name ORDER BY count(*) DESC LIMIT 20"
    ).fetchall()
    test_callee_names = [r[0] for r in rows]

    rows = conn.execute(
        "SELECT s.name FROM calls c JOIN symbols s ON c.caller_id = s.id "
        "GROUP BY s.name ORDER BY count(*) DESC LIMIT 20"
    ).fetchall()
    test_caller_names = [r[0] for r in rows]

    query_results = {}

    # C1. get_callers (批量 x20)
    gc.collect()
    t0 = time.perf_counter()
    total_results = 0
    for _ in range(20):
        for name in test_callee_names:
            callers = store.get_callers(name)
            total_results += len(callers)
    elapsed = time.perf_counter() - t0
    query_results["get_callers_x20_avg_us"] = round(elapsed * 1000000 / 20, 1)
    print(f"  get_callers    x20: {query_results['get_callers_x20_avg_us']:>12,.0f} us avg  (结果数: {total_results})")

    # C2. get_callees (批量 x20)
    gc.collect()
    t0 = time.perf_counter()
    total_results = 0
    for _ in range(20):
        for name in test_caller_names:
            callees = store.get_callees(name)
            total_results += len(callees)
    elapsed = time.perf_counter() - t0
    query_results["get_callees_x20_avg_us"] = round(elapsed * 1000000 / 20, 1)
    print(f"  get_callees    x20: {query_results['get_callees_x20_avg_us']:>12,.0f} us avg  (结果数: {total_results})")

    # C3. search_symbols (批量 x5)
    search_queries = ["func", "Class", "Struct", "module", "test"]
    gc.collect()
    t0 = time.perf_counter()
    for _ in range(5):
        for q in search_queries:
            results_list = store.search_symbols(q, None, 50)
    elapsed = time.perf_counter() - t0
    query_results["search_symbols_x5_avg_ms"] = round(elapsed * 1000 / 5, 2)
    print(f"  search_symbols x5: {query_results['search_symbols_x5_avg_ms']:>12,.2f} ms avg")

    # C4. get_topological_order (1 次，大规模计算)
    gc.collect()
    t0 = time.perf_counter()
    topo = store.get_topological_order()
    elapsed = time.perf_counter() - t0
    query_results["topo_order_sec"] = round(elapsed, 3)
    query_results["topo_order_count"] = len(topo)
    print(f"  topo_order     x1: {query_results['topo_order_sec']:>12.3f} s     ({len(topo):,} 节点)")

    conn.close()
    result["stage_c_queries"] = query_results

    # ---- Stage D: RSS 内存占用汇总 ----
    gc.collect()
    rss_final = get_rss_mb()
    db_size = os.path.getsize(db_path) / (1024 * 1024)
    print(f"\n[D/4] 内存占用汇总")
    print(f"  最终 RSS: {rss_final:.1f} MB")
    print(f"  DB 大小:  {db_size:.1f} MB")
    print(f"  内存/符号: {rss_final / max(gen_info['symbols'], 1) * 1024:.2f} KB")
    result["stage_d_memory"] = {
        "rss_final_mb": round(rss_final, 1),
        "db_size_mb": round(db_size, 1),
        "kb_per_symbol": round(rss_final / max(gen_info['symbols'], 1) * 1024, 3),
    }

    # 清理
    del store
    gc.collect()
    if os.path.exists(db_path):
        os.remove(db_path)
    for ext in ['-wal', '-shm']:
        p = db_path + ext
        if os.path.exists(p):
            os.remove(p)

    return result


# ============================================
# 主流程
# ============================================

def main():
    import tempfile

    print("=" * 75)
    print("  千万级符号渐进式压测（200万 / 500万 / 700万 / 1000万）")
    print("=" * 75)
    print(f"  起始 RSS: {get_rss_mb():.1f} MB")

    # 检查 Rust 扩展
    try:
        from callwarden_core import GraphStore
        print("  Rust 扩展: 可用")
    except ImportError:
        print("ERROR: callwarden_core 未安装")
        return

    tmpdir = tempfile.mkdtemp(prefix="cw_10m_")
    print(f"  临时目录: {tmpdir}")

    all_results = []
    for scale in SCALES:
        try:
            result = bench_scale(scale, tmpdir)
            all_results.append(result)
        except Exception as e:
            print(f"\n[严重错误] 规模 {scale['name']} 测试失败: {e}")
            traceback.print_exc()
            all_results.append({
                "scale": scale["name"],
                "label": scale["label"],
                "target_symbols": scale["symbols"],
                "error": str(e),
            })
            # 如果某个规模失败，停止后续更大规模的测试
            print(f"\n停止后续测试（{scale['name']} 失败）")
            break

    # ---- 汇总 ----
    print(f"\n\n{'=' * 75}")
    print("  汇总报告")
    print(f"{'=' * 75}")

    print(f"\n{'规模':>6} | {'符号':>10} | {'边':>12} | {'加载(s)':>8} | {'加载RSS(MB)':>12} | {'depth(s)':>9} | {'depthRSS(MB)':>12} | {'topo(s)':>8}")
    print("-" * 110)
    for r in all_results:
        if "error" in r:
            print(f"{r['scale']:>6} | ERROR: {r['error'][:80]}")
            continue
        gen = r.get("gen", {})
        load = r.get("stage_a_load", {})
        depth = r.get("stage_b_depth", {})
        queries = r.get("stage_c_queries", {})
        print(f"{r['scale']:>6} | {gen.get('symbols', 0):>10,} | {gen.get('calls', 0):>12,} | "
              f"{load.get('load_time_sec', 0):>8.2f} | {load.get('rss_delta_mb', 0):>12.1f} | "
              f"{depth.get('depth_time_sec', 0):>9.2f} | {depth.get('rss_delta_mb', 0):>12.1f} | "
              f"{queries.get('topo_order_sec', 0):>8.3f}")

    # 保存报告
    report_path = os.path.join(_PKG_ROOT, "tests", "_bench_10m_progressive_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n报告已保存: {report_path}")

    # 清理临时目录
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n临时目录已清理: {tmpdir}")
    print(f"最终 RSS: {get_rss_mb():.1f} MB")


if __name__ == "__main__":
    main()
