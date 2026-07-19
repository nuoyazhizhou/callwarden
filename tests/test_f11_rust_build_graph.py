"""F11 方案 A 验证基准：Rust 端并行构建 CSR vs Python build_full_graph

测试目的：验证 build_graph_from_c_files 的正确性，并对比两条路径的性能。

路径 A（新）：Rust rayon parse → Rust 内存构 CSR → 直接查询
路径 B（旧）：Python build_full_graph → SQLite INSERT → GraphStore.load_from_sqlite

预期：路径 A 跳过 SQLite INSERT，应在小规模下显著快于路径 B。
大规模下路径 A 仍受 parse 阶段限制（与路径 B 共享 tree-sitter），但跳过 INSERT 后总耗时显著降低。

使用方式：
    # 默认小规模（100 文件 × 10 函数 = 1000 符号）
    $env:PYTHONPATH = "rust_ext/target/pyinstall"
    python tests/test_f11_rust_build_graph.py

    # 中规模（50 文件 × 200 函数 = 10000 符号）
    $env:F11_NUM_FILES = "50"
    $env:F11_FUNCS_PER_FILE = "200"
    $env:PYTHONPATH = "rust_ext/target/pyinstall"
    python tests/test_f11_rust_build_graph.py
"""
import os
import sys
import time
import json
import shutil
import tempfile

# 确保能导入 callwarden
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db.db import CodeGraphDB


# ============================================
# 合成 C 代码生成器（链式调用，确保有可解析边）
# ============================================

def generate_synthetic_c_repo(root: str, num_files: int = 100, funcs_per_file: int = 10):
    """生成合成 C 代码库。

    每个文件包含 N 个函数，链式调用：fn_0 → fn_1 → ... → fn_{N-1}
    最后一个函数调用下一个文件的第一个函数（跨文件链）。

    Args:
        root: 目标根目录
        num_files: 文件数量
        funcs_per_file: 每个文件的函数数量

    Returns:
        总符号数
    """
    os.makedirs(root, exist_ok=True)

    for file_idx in range(num_files):
        filepath = os.path.join(root, f"mod_{file_idx:06d}.c")
        lines = []

        for fn_idx in range(funcs_per_file):
            fn_name = f"fn_{file_idx}_{fn_idx:06d}"

            if fn_idx + 1 < funcs_per_file:
                # 文件内链式调用：fn_i 调用 fn_{i+1}
                callee = f"fn_{file_idx}_{fn_idx + 1:06d}"
                lines.append(f"int {fn_name}(int x) {{")
                lines.append(f"    return {callee}(x + 1);")
                lines.append(f"}}")
                lines.append("")
            elif file_idx + 1 < num_files:
                # 最后一个函数调用下一个文件的第一个函数
                callee = f"fn_{file_idx + 1:06d}_000000"
                lines.append(f"int {fn_name}(int x) {{")
                lines.append(f"    return {callee}(x + 1);")
                lines.append(f"}}")
                lines.append("")
            else:
                # 最后一个文件的最后一个函数
                lines.append(f"int {fn_name}(int x) {{")
                lines.append(f"    return x;")
                lines.append(f"}}")
                lines.append("")

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    total_symbols = num_files * funcs_per_file
    print(f"  生成 {num_files} C 文件, {total_symbols:,} 函数")
    return total_symbols


# ============================================
# 辅助函数
# ============================================

def _get_rss_mb():
    """获取当前进程 RSS（MB）"""
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    except ImportError:
        return 0.0


# ============================================
# 基准测试
# ============================================

def bench_rust_path(repo: str, num_threads: int = 4):
    """路径 A：Rust rayon parse → Rust 内存构 CSR → 直接查询

    Returns:
        dict: 测试结果
    """
    print(f"\n{'='*60}")
    print("路径 A: Rust 端并行构建 CSR（跳过 SQLite INSERT）")
    print(f"{'='*60}")

    # 收集所有 .c 文件
    c_files = []
    for fname in sorted(os.listdir(repo)):
        if fname.endswith(".c"):
            abs_path = os.path.join(repo, fname)
            module_path = fname.replace(".c", "")
            c_files.append((abs_path, module_path))

    print(f"  文件数: {len(c_files)}")

    # 1. build_graph_from_c_files（包含 parse + resolve + CSR 构建）
    rss_before = _get_rss_mb()
    t0 = time.perf_counter()
    try:
        from callwarden_core import build_graph_from_c_files
        store, sym_count, edge_count = build_graph_from_c_files(c_files, num_threads=num_threads)
    except ImportError as e:
        print(f"  [FAIL] Rust 扩展不可用: {e}")
        return {"error": str(e)}
    build_elapsed = time.perf_counter() - t0
    rss_after = _get_rss_mb()

    print(f"  build_graph_from_c_files: {build_elapsed:.3f}s")
    print(f"  符号数: {sym_count}")
    print(f"  调用边: {edge_count}")
    print(f"  RSS: {rss_before:.1f} → {rss_after:.1f} MB (+{rss_after - rss_before:.1f} MB)")

    # 2. 查询性能（直接走 Rust CSR，零 SQLite）
    target_qname = "mod_000000.fn_0_000000"

    # get_callers
    t0 = time.perf_counter()
    callers = store.get_callers("fn_0_000001")
    callers_t = time.perf_counter() - t0
    print(f"  get_callers('fn_0_000001'): {callers_t:.4f}s, 结果数: {len(callers) if callers else 0}")

    # search_symbols
    t0 = time.perf_counter()
    results = store.search_symbols("fn_0")
    search_t = time.perf_counter() - t0
    print(f"  search_symbols('fn_0'): {search_t:.4f}s, 结果数: {len(results) if results else 0}")

    # get_stats
    t0 = time.perf_counter()
    stats = store.stats_rust() if hasattr(store, 'stats_rust') else None
    stats_t = time.perf_counter() - t0
    print(f"  stats_rust: {stats_t:.4f}s")

    # dump_to_file（验证持久化路径）
    snap_path = os.path.join(repo, "_test_rust_path.cwsnap")
    t0 = time.perf_counter()
    try:
        store.dump_to_file(snap_path)
        dump_t = time.perf_counter() - t0
        snap_size_mb = os.path.getsize(snap_path) / (1024 * 1024)
        print(f"  dump_to_file: {dump_t:.3f}s, 大小: {snap_size_mb:.2f} MB")
    except Exception as e:
        dump_t = 0
        snap_size_mb = 0
        print(f"  dump_to_file 失败: {e}")
    if os.path.exists(snap_path):
        os.remove(snap_path)

    return {
        "path": "rust",
        "files": len(c_files),
        "build_s": round(build_elapsed, 3),
        "symbols": sym_count,
        "edges": edge_count,
        "rss_before_mb": round(rss_before, 1),
        "rss_after_mb": round(rss_after, 1),
        "rss_delta_mb": round(rss_after - rss_before, 1),
        "get_callers_s": round(callers_t, 4),
        "search_s": round(search_t, 4),
        "stats_s": round(stats_t, 4),
        "dump_s": round(dump_t, 3),
        "snap_size_mb": round(snap_size_mb, 2),
    }


def bench_python_path(repo: str):
    """路径 B：Python build_full_graph → SQLite INSERT → GraphStore.load_from_sqlite

    Returns:
        dict: 测试结果
    """
    print(f"\n{'='*60}")
    print("路径 B: Python build_full_graph（SQLite INSERT + GraphStore.load_from_sqlite）")
    print(f"{'='*60}")

    db_path = os.path.join(repo, "callwarden.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    for ext in ["-wal", "-shm"]:
        p = db_path + ext
        if os.path.exists(p):
            os.remove(p)

    db = CodeGraphDB(db_path, workspace_root=repo)
    ws_id = db.register_workspace("bench-py", repo, "Python 路径基准测试")
    db.set_active_workspace(ws_id)

    # 1. build_full_graph（parse + INSERT + resolve + depth）
    rss_before = _get_rss_mb()
    t0 = time.perf_counter()
    db.build_full_graph()
    build_elapsed = time.perf_counter() - t0
    rss_after = _get_rss_mb()

    # 获取 stage_timings
    stage_t = getattr(db, '_stage_timings', {})

    # 统计符号和调用数
    ws_id_now = db._get_active_workspace_id()
    cur = db.conn.execute(
        "SELECT COUNT(*) FROM symbols s "
        "JOIN file_instances fi ON s.file_instance_id = fi.id "
        "WHERE fi.workspace_id = ?", (ws_id_now,))
    sym_count = cur.fetchone()[0]

    cur = db.conn.execute("SELECT COUNT(*) FROM calls")
    edge_count = cur.fetchone()[0]

    db_size_mb = 0.0
    if os.path.exists(db_path):
        db_size_mb = os.path.getsize(db_path) / (1024 * 1024)

    print(f"  build_full_graph: {build_elapsed:.3f}s")
    print(f"  符号数: {sym_count}")
    print(f"  调用边: {edge_count}")
    print(f"  db_size: {db_size_mb:.2f} MB")
    print(f"  RSS: {rss_before:.1f} → {rss_after:.1f} MB (+{rss_after - rss_before:.1f} MB)")
    if stage_t:
        print(f"  阶段分解:")
        for k in ["register", "parse", "symbol_write", "call_resolve_write", "depth", "commit"]:
            v = stage_t.get(k, 0)
            if isinstance(v, (int, float)) and v > 0.001:
                print(f"    {k}: {v:.3f}s")

    # 2. 查询性能（走 Python SQL，对比 Rust CSR 路径）
    t0 = time.perf_counter()
    callers = db.get_callers("fn_0_000001")
    callers_t = time.perf_counter() - t0
    print(f"  get_callers('fn_0_000001'): {callers_t:.4f}s, 结果数: {len(callers) if callers else 0}")

    t0 = time.perf_counter()
    results = db.search_symbols("fn_0")
    search_t = time.perf_counter() - t0
    print(f"  search_symbols('fn_0'): {search_t:.4f}s, 结果数: {len(results) if results else 0}")

    t0 = time.perf_counter()
    stats = db.get_stats()
    stats_t = time.perf_counter() - t0
    print(f"  get_stats: {stats_t:.4f}s")

    db.close()

    return {
        "path": "python",
        "build_s": round(build_elapsed, 3),
        "symbols": sym_count,
        "edges": edge_count,
        "db_size_mb": round(db_size_mb, 2),
        "rss_before_mb": round(rss_before, 1),
        "rss_after_mb": round(rss_after, 1),
        "rss_delta_mb": round(rss_after - rss_before, 1),
        "get_callers_s": round(callers_t, 4),
        "search_s": round(search_t, 4),
        "stats_s": round(stats_t, 4),
        "stage_timings": {k: v for k, v in stage_t.items()
                          if isinstance(v, (int, float)) and v > 0.001},
    }


def main():
    """主入口：对比两条路径"""
    # 规模配置
    num_files = int(os.environ.get("F11_NUM_FILES", "100"))
    funcs_per_file = int(os.environ.get("F11_FUNCS_PER_FILE", "10"))
    num_threads = int(os.environ.get("F11_THREADS", "4"))

    print(f"F11 方案 A 验证基准")
    print(f"规模: {num_files} 文件 × {funcs_per_file} 函数 = {num_files * funcs_per_file:,} 符号")
    print(f"线程数: {num_threads}")

    # 准备合成 C 代码
    tmpdir = tempfile.mkdtemp(prefix="cw_f11_")
    repo = os.path.join(tmpdir, "synth_repo")
    print(f"\n[0] 生成合成 C 代码...")
    generate_synthetic_c_repo(repo, num_files, funcs_per_file)

    # 路径 A: Rust
    rust_result = bench_rust_path(repo, num_threads=num_threads)

    # 路径 B: Python（用相同的 repo，但需要删除 db）
    py_result = bench_python_path(repo)

    # 对比汇总
    print(f"\n{'='*60}")
    print("对比汇总")
    print(f"{'='*60}")
    if "error" in rust_result:
        print("Rust 路径失败，无法对比")
        print(f"  Rust: {rust_result}")
        print(f"  Python: {py_result}")
    else:
        print(f"{'指标':<20} {'Rust (A)':<15} {'Python (B)':<15} {'比值 A/B':<10}")
        print("-" * 60)
        def _fmt(v, default="-"):
            return f"{v:.3f}" if isinstance(v, (int, float)) else default

        build_a = rust_result["build_s"]
        build_b = py_result["build_s"]
        print(f"{'build (s)':<20} {build_a:<15.3f} {build_b:<15.3f} {build_a/build_b:.2f}x")
        print(f"{'get_callers (s)':<20} {rust_result['get_callers_s']:<15.4f} {py_result['get_callers_s']:<15.4f} {rust_result['get_callers_s']/max(py_result['get_callers_s'], 0.0001):.2f}x")
        print(f"{'search (s)':<20} {rust_result['search_s']:<15.4f} {py_result['search_s']:<15.4f} {rust_result['search_s']/max(py_result['search_s'], 0.0001):.2f}x")
        print(f"{'RSS delta (MB)':<20} {rust_result['rss_delta_mb']:<15.1f} {py_result['rss_delta_mb']:<15.1f} -")

        if build_a < build_b:
            speedup = build_b / build_a
            print(f"\n[结论] Rust 路径在 build 阶段快 {speedup:.2f}x")
        else:
            speedup = build_a / build_b
            print(f"\n[结论] Rust 路径在 build 阶段慢 {speedup:.2f}x（可能受小规模固定开销影响）")

    # 写入报告
    report = {
        "config": {
            "num_files": num_files,
            "funcs_per_file": funcs_per_file,
            "target_symbols": num_files * funcs_per_file,
            "num_threads": num_threads,
        },
        "rust_path": rust_result,
        "python_path": py_result,
    }
    report_path = os.path.join(os.path.dirname(__file__), "_f11_bench_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n报告已写入: {report_path}")

    # 清理
    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
