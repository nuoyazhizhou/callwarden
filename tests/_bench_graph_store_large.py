"""B-P7a: GraphStore 大规模性能基准

生成不同规模的合成 SQLite 数据库（10K / 50K / 100K / 500K / 1M 符号），
测试 Rust GraphStore 的：
1. 加载时间（load_from_sqlite）
2. CSR 构建时间（含 forward/backward 排序 + 偏移数组）
3. 查询性能（get_callers / get_callees / search_symbols / get_topological_order）
4. 对比 Python SQL 查询性能

合成数据特征：
- 符号：fn / class / struct 混合，按文件分组
- 调用边：每个函数随机调用 5-15 个其他函数（模拟真实调用密度）
- 30% 边为未解析（callee_id=0，外部库调用）
- 文件数 = 符号数 / 5（平均每文件 5 个符号）

用法：
    $env:PYTHONPATH = "rust_ext/target/pyinstall"
    python tests/_bench_graph_store_large.py
"""
from __future__ import annotations

import os
import sys
import time
import json
import random
import sqlite3
import tempfile
import gc

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)


# ============================================
# 合成数据生成
# ============================================

def generate_synthetic_db(db_path: str, target_symbols: int, seed: int = 42):
    """生成合成 SQLite 数据库

    Args:
        db_path: 数据库文件路径
        target_symbols: 目标符号数
        seed: 随机种子（可复现）
    """
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
# 性能测试
# ============================================

def _connect_readonly(db_path: str) -> sqlite3.Connection:
    """以 immutable=1 只读模式连接"""
    normalized = db_path.replace('\\', '/')
    uri = f"file:///{normalized}?immutable=1"
    return sqlite3.connect(uri, uri=True)


def bench_rust_load(db_path: str) -> dict:
    """测试 Rust GraphStore 加载性能"""
    from callwarden_core import GraphStore
    store = GraphStore()

    gc.collect()
    t0 = time.perf_counter()
    n_sym, n_edge = store.load_from_sqlite(db_path)
    elapsed = time.perf_counter() - t0

    return {
        "load_time_ms": round(elapsed * 1000, 1),
        "symbols": n_sym,
        "edges": n_edge,
    }


def bench_rust_queries(db_path: str, n_iterations: int = 20) -> dict:
    """测试 Rust GraphStore 查询性能"""
    from callwarden_core import GraphStore
    store = GraphStore()
    store.load_from_sqlite(db_path)

    # 获取统计
    stats = store.stats()
    n_symbols = stats["symbol_count"]
    n_edges = stats["edge_count"]

    # 准备测试数据：取 20 个有调用者的函数名
    conn = _connect_readonly(db_path)
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
    conn.close()

    results = {}

    # 1. get_callers (批量 x20)
    gc.collect()
    t0 = time.perf_counter()
    total_results = 0
    for _ in range(n_iterations):
        for name in test_callee_names:
            callers = store.get_callers(name)
            total_results += len(callers)
    elapsed = time.perf_counter() - t0
    results["get_callers_x20_avg_us"] = round(elapsed * 1000000 / n_iterations, 1)

    # 2. get_callees (批量 x20)
    gc.collect()
    t0 = time.perf_counter()
    for _ in range(n_iterations):
        for name in test_caller_names:
            callees = store.get_callees(name)
            total_results += len(callees)
    elapsed = time.perf_counter() - t0
    results["get_callees_x20_avg_us"] = round(elapsed * 1000000 / n_iterations, 1)

    # 3. search_symbols (批量 x5)
    search_queries = ["func", "Class", "Struct", "module", "test"]
    gc.collect()
    t0 = time.perf_counter()
    for _ in range(5):
        for q in search_queries:
            results_list = store.search_symbols(q, None, 50)
    elapsed = time.perf_counter() - t0
    results["search_symbols_x5_avg_ms"] = round(elapsed * 1000 / 5, 2)

    # 4. get_topological_order (1 次，大规模计算)
    gc.collect()
    t0 = time.perf_counter()
    topo = store.get_topological_order()
    elapsed = time.perf_counter() - t0
    results["topo_order_ms"] = round(elapsed * 1000, 1)
    results["topo_order_count"] = len(topo)

    return results


def bench_python_queries(db_path: str, n_iterations: int = 20) -> dict:
    """测试 Python SQL 查询性能（基准）"""
    conn = _connect_readonly(db_path)

    # 准备测试数据
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

    results = {}

    # 1. get_callers (SQL JOIN)
    gc.collect()
    t0 = time.perf_counter()
    for _ in range(n_iterations):
        for name in test_callee_names:
            cur = conn.execute(
                "SELECT s.name, s.qualified_name, fi.rel_path, s.module_path, c.call_line, c.is_cross_file "
                "FROM calls c "
                "JOIN symbols s ON c.caller_id = s.id "
                "JOIN file_instances fi ON s.file_instance_id = fi.id "
                "WHERE c.callee_name = ?",
                (name,)
            )
            list(cur.fetchall())
    elapsed = time.perf_counter() - t0
    results["get_callers_x20_avg_us"] = round(elapsed * 1000000 / n_iterations, 1)

    # 2. get_callees (SQL JOIN)
    gc.collect()
    t0 = time.perf_counter()
    for _ in range(n_iterations):
        for name in test_caller_names:
            cur = conn.execute(
                "SELECT c.callee_name, c.callee_qualified, fi.rel_path, c.call_line, c.is_cross_file "
                "FROM calls c "
                "JOIN symbols s ON c.caller_id = s.id "
                "JOIN file_instances fi ON s.file_instance_id = fi.id "
                "WHERE s.name = ?",
                (name,)
            )
            list(cur.fetchall())
    elapsed = time.perf_counter() - t0
    results["get_callees_x20_avg_us"] = round(elapsed * 1000000 / n_iterations, 1)

    # 3. search_symbols (SQL LIKE)
    search_queries = ["func", "Class", "Struct", "module", "test"]
    gc.collect()
    t0 = time.perf_counter()
    for _ in range(5):
        for q in search_queries:
            cur = conn.execute(
                "SELECT name, qualified_name, kind FROM symbols "
                "WHERE name LIKE ? OR qualified_name LIKE ? LIMIT 50",
                (f"%{q}%", f"%{q}%")
            )
            list(cur.fetchall())
    elapsed = time.perf_counter() - t0
    results["search_symbols_x5_avg_ms"] = round(elapsed * 1000 / 5, 2)

    conn.close()
    return results


# ============================================
# 主流程
# ============================================

SCALES = [
    {"name": "10K", "symbols": 10000},
    {"name": "50K", "symbols": 50000},
    {"name": "100K", "symbols": 100000},
    {"name": "500K", "symbols": 500000},
]


def main():
    print("=" * 75)
    print("B-P7a: GraphStore 大规模性能基准")
    print("=" * 75)

    # 检查 Rust 扩展
    try:
        from callwarden_core import GraphStore
        print("Rust 扩展可用")
    except ImportError:
        print("ERROR: callwarden_core 未安装")
        return

    tmpdir = tempfile.mkdtemp(prefix="cw_bench_large_")
    print(f"临时目录: {tmpdir}")

    all_results = []

    for scale in SCALES:
        scale_name = scale["name"]
        n_symbols = scale["symbols"]

        print(f"\n{'─' * 75}")
        print(f"规模: {scale_name} ({n_symbols:,} 符号)")
        print(f"{'─' * 75}")

        # 1. 生成合成数据库
        db_path = os.path.join(tmpdir, f"synth_{scale_name}.db")
        print(f"  生成合成数据...")
        gen_info = generate_synthetic_db(db_path, n_symbols)

        # 2. Rust 加载性能
        print(f"  Rust GraphStore 加载...")
        rust_load = bench_rust_load(db_path)
        print(f"    加载时间: {rust_load['load_time_ms']:.1f}ms ({rust_load['symbols']:,} 符号, {rust_load['edges']:,} 边)")

        # 3. Rust 查询性能
        print(f"  Rust 查询性能...")
        rust_query = bench_rust_queries(db_path)
        print(f"    get_callers  x20: {rust_query['get_callers_x20_avg_us']:,.0f} us avg")
        print(f"    get_callees  x20: {rust_query['get_callees_x20_avg_us']:,.0f} us avg")
        print(f"    search_symbols x5: {rust_query['search_symbols_x5_avg_ms']:.2f} ms avg")
        print(f"    topo_order:    {rust_query['topo_order_ms']:.1f} ms ({rust_query['topo_order_count']:,} nodes)")

        # 4. Python SQL 查询性能（基准）
        print(f"  Python SQL 查询性能...")
        py_query = bench_python_queries(db_path)
        print(f"    get_callers  x20: {py_query['get_callers_x20_avg_us']:,.0f} us avg")
        print(f"    get_callees  x20: {py_query['get_callees_x20_avg_us']:,.0f} us avg")
        print(f"    search_symbols x5: {py_query['search_symbols_x5_avg_ms']:.2f} ms avg")

        # 5. 加速比
        callers_speedup = py_query["get_callers_x20_avg_us"] / rust_query["get_callers_x20_avg_us"] if rust_query["get_callers_x20_avg_us"] > 0 else 0
        callees_speedup = py_query["get_callees_x20_avg_us"] / rust_query["get_callees_x20_avg_us"] if rust_query["get_callees_x20_avg_us"] > 0 else 0
        search_speedup = py_query["search_symbols_x5_avg_ms"] / rust_query["search_symbols_x5_avg_ms"] if rust_query["search_symbols_x5_avg_ms"] > 0 else 0

        print(f"  加速比:")
        print(f"    get_callers:    {callers_speedup:.2f}x")
        print(f"    get_callees:    {callees_speedup:.2f}x")
        print(f"    search_symbols: {search_speedup:.2f}x")

        all_results.append({
            "scale": scale_name,
            "target_symbols": n_symbols,
            "data": gen_info,
            "rust_load": rust_load,
            "rust_query": rust_query,
            "python_query": py_query,
            "speedup": {
                "get_callers": round(callers_speedup, 2),
                "get_callees": round(callees_speedup, 2),
                "search_symbols": round(search_speedup, 2),
            }
        })

        # 清理大数据库文件
        if os.path.exists(db_path):
            os.remove(db_path)
        for ext in ['-wal', '-shm']:
            p = db_path + ext
            if os.path.exists(p):
                os.remove(p)

    # === 汇总 ===
    print(f"\n{'=' * 75}")
    print("汇总")
    print(f"{'=' * 75}")
    print(f"\n{'规模':>6} | {'符号':>8} | {'边':>10} | {'加载(ms)':>10} | {'callers加速':>12} | {'callees加速':>12} | {'search加速':>12}")
    print("-" * 95)
    for r in all_results:
        print(f"{r['scale']:>6} | {r['rust_load']['symbols']:>8,} | {r['rust_load']['edges']:>10,} | {r['rust_load']['load_time_ms']:>10.1f} | {r['speedup']['get_callers']:>11.2f}x | {r['speedup']['get_callees']:>11.2f}x | {r['speedup']['search_symbols']:>11.2f}x")

    # 保存报告
    report_path = os.path.join(_PKG_ROOT, "tests", "_bench_graph_store_large_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {report_path}")


if __name__ == "__main__":
    main()
