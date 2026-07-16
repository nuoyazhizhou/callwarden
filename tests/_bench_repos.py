"""批量基准测试：扫描 testcode/repos 下所有项目，找出解析问题

统计每个项目的：
- files_scanned / files_parsed / files_unchanged / files_failed
- symbols / calls / elapsed
- 失败文件列表（如果有，从 stdout 捕获）

输出 JSON 报告到 tests/_bench_report.json
"""
from __future__ import annotations
import os, sys, time, json, tempfile, shutil, io, re

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

os.environ.setdefault("CW_MP_WORKERS", "4")

from callwarden.config import scan_subprojects
from callwarden.db.db import CodeGraphDB

REPOS_DIR = os.path.join(_PKG_ROOT, "testcode", "repos")
REPORT_PATH = os.path.join(_PKG_ROOT, "tests", "_bench_report.json")


def _run_build(db, name, root):
    """执行 build_full_graph 并捕获 stdout（找失败文件）"""
    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        ws = db.register_workspace(name, root)
        db.set_active_workspace(ws)
        t0 = time.perf_counter()
        db.build_full_graph(force=False)
        elapsed = time.perf_counter() - t0
        log = buf.getvalue()
    finally:
        sys.stdout = old_stdout
    return elapsed, log


def _debug_one(root):
    """调试单个项目：打印 workspace_root 和扫描结果"""
    import tempfile, os
    db = CodeGraphDB(db_path=os.path.join(tempfile.gettempdir(), "_debug.db"), workspace_root=root)
    print(f"  workspace_root = {db.workspace_root}")
    files = db._scan_supported_files()
    print(f"  _scan_supported_files() -> {len(files)} files")
    if files:
        for f in files[:5]:
            print(f"    {f}")
    db.close()
    os.unlink(os.path.join(tempfile.gettempdir(), "_debug.db"))


def _debug_run(root, name):
    """调试 _run_build 方式"""
    import tempfile, os
    db = CodeGraphDB(db_path=os.path.join(tempfile.gettempdir(), "_debug2.db"), workspace_root=root)
    print(f"  before register: workspace_root = {db.workspace_root}")
    ws = db.register_workspace(name, root)
    print(f"  register_workspace returned id={ws}")
    db.set_active_workspace(ws)
    print(f"  after set_active: workspace_root = {db.workspace_root}")
    files = db._scan_supported_files()
    print(f"  _scan_supported_files() -> {len(files)} files")
    db.close()
    os.unlink(os.path.join(tempfile.gettempdir(), "_debug2.db"))


def _debug_build(root, name):
    """调试 _run_build + build_full_graph"""
    import tempfile, os
    db = CodeGraphDB(db_path=os.path.join(tempfile.gettempdir(), "_debug3.db"), workspace_root=root)
    elapsed, log = _run_build(db, name, root)
    stats = db.get_stats()
    t = getattr(db, "_stage_timings", {})
    print(f"  elapsed={elapsed:.2f}s")
    print(f"  stats: files={stats.get('file_count',0)} symbols={stats.get('symbol_count',0)}")
    print(f"  timings: files_total={t.get('files_total','N/A')} parsed={t.get('files_parsed','N/A')} unchanged={t.get('files_unchanged','N/A')}")
    print(f"  log (first 500 chars):\n{log[:500]}")
    db.close()
    os.unlink(os.path.join(tempfile.gettempdir(), "_debug3.db"))


def main():
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    projects = scan_subprojects(REPOS_DIR, max_depth=1)
    if limit > 0:
        projects = projects[:limit]
    print(f"扫描到 {len(projects)} 个项目\n", flush=True)

    tmp_dir = tempfile.mkdtemp(prefix="cw_bench_")
    results = []
    problems = []

    for idx, p in enumerate(projects, 1):
        name = p["name"]
        lang = p["lang"]
        root = p["root"]

        db_path = os.path.join(tmp_dir, f"p{idx:04d}.db")
        db = CodeGraphDB(db_path=db_path, workspace_root=root)

        try:
            elapsed, log = _run_build(db, name, root)

            stats = db.get_stats()
            files = stats.get("total_files", 0)
            symbols = stats.get("total_symbols", 0)
            calls = stats.get("total_calls", 0)

            timings = getattr(db, "_stage_timings", {}) or {}
            files_total = timings.get("files_total", 0)
            parsed = timings.get("files_parsed", 0)
            unchanged = timings.get("files_unchanged", 0)
            # P23.7: 优先用 _stage_timings 的 files_failed/files_skipped
            failed = timings.get("files_failed", -1)
            if failed < 0:
                # 兼容旧版本：files_total - parsed - unchanged（会误算 skipped）
                failed = max(0, files_total - parsed - unchanged)
            skipped = timings.get("files_skipped", 0)

            # 从日志提取失败文件路径
            failed_paths = []
            if failed > 0 or "失败" in log or "fail" in log.lower():
                for line in log.splitlines():
                    if "失败" in line or "fail" in line.lower():
                        failed_paths.append(line.strip()[:120])

            result = {
                "idx": idx,
                "name": name,
                "lang": lang,
                "elapsed_sec": round(elapsed, 2),
                "files": files,
                "symbols": symbols,
                "calls": calls,
                "parsed": parsed,
                "unchanged": unchanged,
                "failed": failed,
            }
            results.append(result)

            if failed > 0 or failed_paths:
                result["failed_paths"] = failed_paths[:10]
                problems.append(result)

            if idx % 100 == 0 or idx <= 3:
                print(f"  [{idx}/{len(projects)}] {name:40s} files={files:5d} sym={symbols:6d} {elapsed:.1f}s fail={failed}", flush=True)

        except Exception as e:
            err = str(e)[:200]
            print(f"  [{idx}/{len(projects)}] ERROR {name}: {err}")
            problems.append({
                "idx": idx, "name": name, "lang": lang,
                "error": err, "elapsed_sec": 0,
            })
        finally:
            db.close()

    # 汇总
    total = len(results)
    total_files = sum(r.get("files", 0) for r in results)
    total_symbols = sum(r.get("symbols", 0) for r in results)
    total_calls = sum(r.get("calls", 0) for r in results)
    total_elapsed = sum(r.get("elapsed_sec", 0) for r in results)
    total_failed = sum(r.get("failed", 0) for r in results)
    with_problems = len(problems)

    print(f"\n{'='*60}")
    print(f"汇总:")
    print(f"  项目数: {total}")
    print(f"  总文件: {total_files}")
    print(f"  总符号: {total_symbols}")
    print(f"  总调用: {total_calls}")
    print(f"  总耗时: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")
    print(f"  解析失败文件数: {total_failed}")
    print(f"  有问题的项目数: {with_problems}")
    print(f"{'='*60}")

    if problems:
        print(f"\n有问题的项目（前 30 个）:")
        for p in problems[:30]:
            if "error" in p:
                print(f"  [{p['idx']:4d}] ERROR  {p['name']:40s} {p['error'][:80]}")
            else:
                print(f"  [{p['idx']:4d}] fail={p['failed']:3d}  {p['name']:40s} lang={p['lang']}")
                for ff in p.get("failed_paths", [])[:3]:
                    print(f"          {ff}")

    # 按语言汇总
    by_lang = {}
    for r in results:
        l = r["lang"]
        if l not in by_lang:
            by_lang[l] = {"count": 0, "files": 0, "symbols": 0, "elapsed": 0, "failed": 0}
        by_lang[l]["count"] += 1
        by_lang[l]["files"] += r.get("files", 0)
        by_lang[l]["symbols"] += r.get("symbols", 0)
        by_lang[l]["elapsed"] += r.get("elapsed_sec", 0)
        by_lang[l]["failed"] += r.get("failed", 0)

    print(f"\n按语言汇总:")
    print(f"  {'lang':12s} {'count':>6s} {'files':>8s} {'symbols':>8s} {'failed':>6s} {'elapsed':>8s}")
    for l, d in sorted(by_lang.items(), key=lambda x: -x[1]["count"]):
        print(f"  {l:12s} {d['count']:6d} {d['files']:8d} {d['symbols']:8d} {d['failed']:6d} {d['elapsed']:7.0f}s")

    # 保存报告
    report = {
        "summary": {
            "total_projects": total,
            "total_files": total_files,
            "total_symbols": total_symbols,
            "total_calls": total_calls,
            "total_elapsed_sec": round(total_elapsed, 1),
            "total_failed_files": total_failed,
            "projects_with_problems": with_problems,
        },
        "by_lang": by_lang,
        "problems": problems,
        "all_results": results,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {REPORT_PATH}")

    shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
