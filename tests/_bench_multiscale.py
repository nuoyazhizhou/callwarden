"""多规模性能基准脚本（1K / 10K / 100K 符号阶梯）

用途：
    全量回归 + 性能基准验证任务的一部分。
    跑 1K / 10K / 100K 三档符号规模，输出 markdown 报告。

执行：
    python tests/_bench_multiscale.py

输出：
    - 控制台实时打印
    - tests/_bench_multiscale_report.md 报告文件

测量指标：
    1. build_full_graph 总耗时（parse + resolve + depth + FTS + GC）
    2. get_stats 延迟
    3. search_symbols 延迟（FTS5 路径）
    4. get_callers 延迟（Rust GraphStore CSR）
    5. get_callees 延迟
    6. get_call_chain_up 延迟（BFS）
    7. blast_radius 延迟
    8. detect_clones 延迟（限定 file_filter 避免 O(N^2)）
    9. db 文件大小
    10. 峰值 RSS
"""
import os
import sys
import time
import json
import shutil
import tempfile
import platform
import subprocess

# 确保能导入 callwarden
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db.db import CodeGraphDB


# ============================================
# 合成代码生成器（复用 test_perf_10m_symbols.py 的策略）
# ============================================

def generate_synthetic_repo(root: str, num_files: int, funcs_per_file: int) -> int:
    """生成合成 Python 代码库。

    每个文件内函数链式调用（fn_0 → fn_1 → ... → fn_M），
    跨文件链式调用（file_0::fn_0 → file_1::fn_0 → ...）。
    """
    os.makedirs(root, exist_ok=True)

    for file_idx in range(num_files):
        filepath = os.path.join(root, f"mod_{file_idx:06d}.py")
        lines = []

        for fn_idx in range(funcs_per_file):
            fn_name = f"fn_{file_idx}_{fn_idx:06d}"

            if fn_idx + 1 < funcs_per_file:
                callee = f"fn_{file_idx}_{fn_idx + 1:06d}"
                lines.append(f"def {fn_name}(x):")
                lines.append(f"    return {callee}(x + 1)")
                lines.append("")
            elif file_idx + 1 < num_files:
                callee = f"fn_{file_idx + 1:06d}_000000"
                lines.append(f"def {fn_name}(x):")
                lines.append(f"    return {callee}(x + 1)")
                lines.append("")
            else:
                lines.append(f"def {fn_name}(x):")
                lines.append(f"    return x")
                lines.append("")

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    total_symbols = num_files * funcs_per_file
    return total_symbols


# ============================================
# 辅助函数
# ============================================

def _format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _get_rss_mb() -> float:
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    except ImportError:
        return 0.0


def _setup_db(repo_path: str, db_path: str) -> CodeGraphDB:
    db = CodeGraphDB(db_path, workspace_root=repo_path)
    ws_id = db.register_workspace("bench", repo_path, "多规模基准测试工作区")
    db.set_active_workspace(ws_id)
    return db


def _db_file_size(db_path: str) -> int:
    if not os.path.exists(db_path):
        return 0
    return os.path.getsize(db_path)


# ============================================
# 单规模测试
# ============================================

def run_scale(num_files: int, funcs_per_file: int, tmpdir: str) -> dict:
    """跑一个规模档位，返回 metrics dict。"""
    scale_name = f"{num_files * funcs_per_file // 1000}K"
    print(f"\n{'='*60}")
    print(f"  规模: {scale_name} 符号 ({num_files} 文件 × {funcs_per_file} 函数/文件)")
    print(f"{'='*60}")

    repo_dir = os.path.join(tmpdir, f"repo_{scale_name}")
    db_path = os.path.join(tmpdir, f"db_{scale_name}.db")

    metrics: dict = {
        "scale": scale_name,
        "num_files": num_files,
        "funcs_per_file": funcs_per_file,
        "target_symbols": num_files * funcs_per_file,
    }

    # 0. 生成合成代码
    t0 = time.time()
    total = generate_synthetic_repo(repo_dir, num_files, funcs_per_file)
    gen_t = time.time() - t0
    print(f"[0] 生成合成代码: {total:,} 符号, 耗时 {gen_t:.2f}s")
    metrics["gen_time_s"] = round(gen_t, 2)

    # 1. build_full_graph
    rss_before = _get_rss_mb()
    print(f"[1] build_full_graph 开始（起始 RSS {rss_before:.0f}MB）...")
    db = _setup_db(repo_dir, db_path)
    t0 = time.time()
    db.build_full_graph()
    build_t = time.time() - t0
    rss_after = _get_rss_mb()
    print(f"    耗时 {build_t:.2f}s, RSS {rss_before:.0f}→{rss_after:.0f}MB")
    metrics["build_time_s"] = round(build_t, 2)
    metrics["rss_before_mb"] = round(rss_before, 0)
    metrics["rss_after_mb"] = round(rss_after, 0)

    # 2. get_stats
    t0 = time.time()
    stats = db.get_stats()
    stats_t = time.time() - t0
    symbol_count = stats.get("total_symbols", 0) if isinstance(stats, dict) else 0
    call_count = stats.get("total_calls", 0) if isinstance(stats, dict) else 0
    resolved_calls = stats.get("resolved_calls", 0) if isinstance(stats, dict) else 0
    cross_file_calls = stats.get("cross_file_calls", 0) if isinstance(stats, dict) else 0
    print(f"[2] get_stats: {stats_t*1000:.1f}ms, 符号 {symbol_count:,}, 调用 {call_count:,}, 已解析 {resolved_calls:,}, 跨文件 {cross_file_calls:,}")
    metrics["get_stats_ms"] = round(stats_t * 1000, 1)
    metrics["actual_symbols"] = symbol_count
    metrics["actual_calls"] = call_count

    # 3. search_symbols（FTS5 路径）
    t0 = time.time()
    results = db.search_symbols("fn_0_000005")
    search_t = time.time() - t0
    print(f"[3] search_symbols: {search_t*1000:.2f}ms, 结果 {len(results) if results else 0}")
    metrics["search_symbols_ms"] = round(search_t * 1000, 2)

    # 4. get_callers（Rust GraphStore CSR）
    # 首次查询：含 _get_graph_store + wait_for_calls_ready 开销
    t0 = time.time()
    callers = db.get_callers("fn_0_000006")
    callers_t = time.time() - t0
    print(f"[4] get_callers (cold): {callers_t*1000:.3f}ms, 结果 {len(callers) if callers else 0}")
    metrics["get_callers_ms"] = round(callers_t * 1000, 3)
    # Warm 查询：直接走 Rust CSR，反映真实使用场景
    t0 = time.time()
    callers_warm = db.get_callers("fn_0_000006")
    callers_warm_t = time.time() - t0
    print(f"[4b] get_callers (warm): {callers_warm_t*1000:.3f}ms, 结果 {len(callers_warm) if callers_warm else 0}")
    metrics["get_callers_warm_ms"] = round(callers_warm_t * 1000, 3)

    # 5. get_callees
    t0 = time.time()
    callees = db.get_callees("fn_0_000005")
    callees_t = time.time() - t0
    print(f"[5] get_callees: {callees_t*1000:.3f}ms, 结果 {len(callees) if callees else 0}")
    metrics["get_callees_ms"] = round(callees_t * 1000, 3)

    # 6. get_call_chain_up BFS（深度 5）
    t0 = time.time()
    try:
        chain_up = db.get_call_chain_up("fn_0_000005", max_depth=5)
        chain_up_t = time.time() - t0
        chain_up_len = len(chain_up) if chain_up else 0
    except Exception as e:
        chain_up_t = time.time() - t0
        chain_up_len = -1
        print(f"    [warn] get_call_chain_up 异常: {e}")
    print(f"[6] get_call_chain_up(d=5): {chain_up_t*1000:.2f}ms, 节点 {chain_up_len}")
    metrics["call_chain_up_ms"] = round(chain_up_t * 1000, 2)
    metrics["chain_up_nodes"] = chain_up_len

    # 7. blast_radius（需 symbol_hash，先 SQL 拿一个真实 hash）
    t0 = time.time()
    try:
        # 从 symbols 表取一个 symbol_hash
        cur = db.conn.execute("SELECT s.symbol_hash FROM symbols s LIMIT 1")
        row = cur.fetchone()
        if row:
            sym_hash = row[0]
            t_lookup = time.time()
            br = db.blast_radius(sym_hash) if hasattr(db, "blast_radius") else None
            t_br_done = time.time()
            # blast_radius 返回 dict: {"impacted_symbols": [...], "total": N, ...}
            if isinstance(br, dict):
                br_count = br.get("total", len(br.get("impacted_symbols", [])))
            elif isinstance(br, list):
                br_count = len(br)
            else:
                br_count = 0
            print(f"[7] blast_radius: lookup={int((t_lookup-t0)*1000)}ms, blast={int((t_br_done-t_lookup)*1000)}ms, total={int((t_br_done-t0)*1000)}ms, 影响 {br_count}")
            br_t = t_br_done - t0
        else:
            br_t = time.time() - t0
            br_count = 0
    except Exception as e:
        br_t = time.time() - t0
        br_count = -1
        print(f"    [warn] blast_radius 异常: {e}")
    metrics["blast_radius_ms"] = round(br_t * 1000, 2)
    metrics["blast_radius_count"] = br_count

    # 8. detect_clones（file_filter 是字符串，限定文件前缀）
    t0 = time.time()
    try:
        clones = db.detect_clones(file_filter="mod_00000") if hasattr(db, "detect_clones") else None
        clones_t = time.time() - t0
        # 返回 dict: {"clone_groups": [...], "total": N}
        if isinstance(clones, dict):
            clones_count = clones.get("total", len(clones.get("clone_groups", [])))
        elif isinstance(clones, list):
            clones_count = len(clones)
        else:
            clones_count = 0
    except Exception as e:
        clones_t = time.time() - t0
        clones_count = -1
        print(f"    [warn] detect_clones 异常: {e}")
    print(f"[8] detect_clones(filter='mod_00000'): {clones_t*1000:.2f}ms, 对 {clones_count}")
    metrics["detect_clones_ms"] = round(clones_t * 1000, 2)
    metrics["clones_count"] = clones_count

    # 9. db 文件大小
    db_size = _db_file_size(db_path)
    print(f"[9] DB 文件大小: {_format_bytes(db_size)}")
    metrics["db_size_mb"] = round(db_size / 1024 / 1024, 2)

    # 10. 峰值 RSS
    metrics["peak_rss_mb"] = round(rss_after, 0)

    db.close()
    shutil.rmtree(repo_dir, ignore_errors=True)
    try:
        os.remove(db_path)
        # 清理 SQLite 附属文件
        for ext in ("-wal", "-shm"):
            p = db_path + ext
            if os.path.exists(p):
                os.remove(p)
    except OSError:
        pass

    return metrics


# ============================================
# 主入口
# ============================================

def main():
    print(f"Call Warden 多规模性能基准")
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"平台: {platform.platform()}")
    print(f"Python: {platform.python_version()}")
    print(f"CPU: {platform.processor() or 'unknown'}")

    # 三档规模
    scales = [
        (10, 100, "1K"),     # 1,000 符号
        (100, 100, "10K"),   # 10,000 符号
        (500, 200, "100K"),  # 100,000 符号
    ]

    tmpdir = tempfile.mkdtemp(prefix="cw_bench_")
    print(f"工作目录: {tmpdir}")

    all_metrics = []
    for num_files, funcs_per_file, name in scales:
        try:
            m = run_scale(num_files, funcs_per_file, tmpdir)
            all_metrics.append(m)
        except Exception as e:
            print(f"[ERROR] 规模 {name} 失败: {e}")
            import traceback
            traceback.print_exc()
            all_metrics.append({
                "scale": name,
                "error": str(e),
            })

    # 清理
    shutil.rmtree(tmpdir, ignore_errors=True)

    # 生成 markdown 报告
    report_path = os.path.join(os.path.dirname(__file__), "_bench_multiscale_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# Call Warden 多规模性能基准报告\n\n")
        f.write(f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- 平台: {platform.platform()}\n")
        f.write(f"- Python: {platform.python_version()}\n")
        f.write(f"- CPU: {platform.processor() or 'unknown'}\n\n")

        f.write("## 多规模阶梯结果\n\n")
        f.write("| 指标 | 1K | 10K | 100K |\n")
        f.write("|------|-----|------|-------|\n")

        # 横向对比表
        m_by_scale = {m["scale"]: m for m in all_metrics if "error" not in m}
        keys = [
            ("build_time_s", "build_full_graph (s)"),
            ("get_stats_ms", "get_stats (ms)"),
            ("search_symbols_ms", "search_symbols (ms)"),
            ("get_callers_ms", "get_callers cold (ms)"),
            ("get_callers_warm_ms", "get_callers warm (ms)"),
            ("get_callees_ms", "get_callees (ms)"),
            ("call_chain_up_ms", "call_chain_up d=5 (ms)"),
            ("blast_radius_ms", "blast_radius (ms)"),
            ("detect_clones_ms", "detect_clones (ms)"),
            ("db_size_mb", "DB size (MB)"),
            ("peak_rss_mb", "Peak RSS (MB)"),
            ("actual_symbols", "actual symbols"),
            ("actual_calls", "actual calls"),
        ]
        for key, label in keys:
            row = f"| {label} "
            for s in ["1K", "10K", "100K"]:
                v = m_by_scale.get(s, {}).get(key, "N/A")
                if isinstance(v, float):
                    row += f"| {v:.2f} "
                else:
                    row += f"| {v} "
            row += "|\n"
            f.write(row)

        f.write("\n## 详细数据\n\n")
        for m in all_metrics:
            f.write(f"### {m.get('scale', 'unknown')}\n\n")
            if "error" in m:
                f.write(f"**失败**: {m['error']}\n\n")
                continue
            f.write("```json\n")
            f.write(json.dumps(m, indent=2, ensure_ascii=False, default=str))
            f.write("\n```\n\n")

        # 规模增长分析
        f.write("## 规模增长分析\n\n")
        if all(len(m_by_scale.get(s, {})) > 0 for s in ["1K", "10K", "100K"]):
            build_1k = m_by_scale["1K"].get("build_time_s", 0)
            build_10k = m_by_scale["10K"].get("build_time_s", 0)
            build_100k = m_by_scale["100K"].get("build_time_s", 0)
            if build_1k > 0:
                f.write(f"- build 1K → 10K（10x 规模）: {build_10k/build_1k:.1f}x 耗时增长\n")
            if build_10k > 0:
                f.write(f"- build 10K → 100K（10x 规模）: {build_100k/build_10k:.1f}x 耗时增长\n")
            if build_1k > 0:
                f.write(f"- build 1K → 100K（100x 规模）: {build_100k/build_1k:.1f}x 耗时增长\n")
            f.write("\n")
            f.write("- 10x 规模预期 10x 耗时（线性），<15x 算合理\n")
            f.write("- 100x 规模预期 100x 耗时（线性），<150x 算合理，>200x 说明有 O(n^2) 退化\n")

    print(f"\n报告已生成: {report_path}")
    print(f"\n{'='*60}")
    print("多规模性能基准完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
