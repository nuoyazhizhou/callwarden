"""P31 真实大库验证：对齐度 + 性能对比。

对 testcode 下 5 个真实项目（new-api/admin/codex-lb/DeepSeek-TUI/CLIProxyAPI）验证：
1. 对齐度：Rust parse_file_lang vs Python create_parser() 的符号名集合对比
   - 每项目采样最多 100 文件
   - 统计：对齐率、漏提取数、多提取数、样本
2. 性能：Rust batch_parse_files_lang（rayon 8 线程）vs Python 串行 parse_file()
   - 全量文件
   - 输出：wall time、加速比、符号数

输出报告到 tests/_p31_validation_report.json
"""
from __future__ import annotations

import os
import sys
import time
import json
import glob

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

# 语言映射：扩展名 → (rust_lang_id, python_parser_attr)
_EXT_LANG = {
    ".py":   "python",
    ".rs":   "rust",
    ".go":   "go",
    ".java": "java",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".rb":   "ruby",
    ".php":  "php",
    ".scala": "scala",
    ".cs":   "csharp",
    ".cpp":  "cpp",
    ".cc":   "cpp",
    ".cxx":  "cpp",
    ".hpp":  "cpp",
}

# 验证目标项目
PROJECTS = [
    {"name": "new-api",       "path": "testcode/new-api",       "langs": {"go"}},
    {"name": "admin",        "path": "testcode/admin",         "langs": {"java"}},
    {"name": "codex-lb",     "path": "testcode/codex-lb",      "langs": {"python"}},
    {"name": "DeepSeek-TUI", "path": "testcode/DeepSeek-TUI",  "langs": {"rust", "typescript"}},
    {"name": "CLIProxyAPI",  "path": "testcode/CLIProxyAPI",    "langs": {"go"}},
]

ALIGN_SAMPLE_PER_PROJECT = 100  # 对齐度采样上限


def _scan_files(project_path: str, lang_filter: set):
    """扫描项目下所有支持语言的文件，返回 [(abs_path, rust_lang_id), ...]"""
    results = []
    for root, dirs, files in os.walk(project_path):
        # 跳过 .git / vendor / node_modules 等
        dirs[:] = [d for d in dirs if d not in (".git", "vendor", "node_modules", ".venv", "__pycache__")]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _EXT_LANG:
                continue
            lang_id = _EXT_LANG[ext]
            # lang_filter 指定要哪些语言（python/rust/typescript 等）
            # rust_lang_id 与 python lang 对应
            if lang_id not in lang_filter:
                continue
            abs_path = os.path.join(root, fn)
            results.append((abs_path, lang_id))
    return results


def _rust_parse_one(abs_path: str, lang_id: str):
    """Rust 单文件解析，返回符号名集合"""
    from callwarden_core import parse_file_lang
    module_path = os.path.splitext(os.path.basename(abs_path))[0]
    result = parse_file_lang(abs_path, module_path, lang_id)
    if result.get("error"):
        return None, result["error"]
    names = set(s["name"] for s in result["symbols"])
    return names, None


def _python_parse_one(abs_path: str):
    """Python 单文件解析，返回符号名集合"""
    from callwarden.parsers import create_parser
    parser = create_parser(abs_path)
    if parser is None:
        return None, "no_parser"
    module_path = os.path.splitext(os.path.basename(abs_path))[0]
    result = parser.parse_file(abs_path, module_path)
    names = set(s["name"] for s in result["symbols"])
    return names, None


def validate_alignment(project):
    """对齐度验证：采样对比 Rust vs Python 符号名集合"""
    name = project["name"]
    path = os.path.join(_PKG_ROOT, project["path"])
    lang_filter = project["langs"]

    files = _scan_files(path, lang_filter)
    # 采样
    if len(files) > ALIGN_SAMPLE_PER_PROJECT:
        step = len(files) // ALIGN_SAMPLE_PER_PROJECT
        sampled = files[::step][:ALIGN_SAMPLE_PER_PROJECT]
    else:
        sampled = files

    aligned = 0
    rust_missing_total = 0  # Python 有但 Rust 没有
    rust_extra_total = 0    # Rust 有但 Python 没有
    perfect_match = 0       # 完全一致
    samples_missing = []    # 漏提取样本
    samples_extra = []       # 多提取样本
    errors = []

    for abs_path, lang_id in sampled:
        rust_names, rust_err = _rust_parse_one(abs_path, lang_id)
        py_names, py_err = _python_parse_one(abs_path)

        if rust_err:
            errors.append({"file": abs_path, "rust_error": rust_err})
            continue
        if py_err:
            errors.append({"file": abs_path, "py_error": py_err})
            continue

        missing = py_names - rust_names   # Python 有 Rust 没有
        extra = rust_names - py_names       # Rust 有 Python 没有

        if not missing and not extra:
            perfect_match += 1
        if not missing:
            aligned += 1

        rust_missing_total += len(missing)
        rust_extra_total += len(extra)

        if missing and len(samples_missing) < 10:
            samples_missing.append({
                "file": os.path.relpath(abs_path, _PKG_ROOT),
                "missing": sorted(missing)[:10],
            })
        if extra and len(samples_extra) < 10:
            samples_extra.append({
                "file": os.path.relpath(abs_path, _PKG_ROOT),
                "extra": sorted(extra)[:10],
            })

    total = len(sampled)
    return {
        "project": name,
        "total_sampled": total,
        "perfect_match": perfect_match,
        "perfect_match_rate": round(perfect_match / total * 100, 1) if total else 0,
        "aligned_files": aligned,
        "aligned_rate": round(aligned / total * 100, 1) if total else 0,
        "rust_missing_symbols": rust_missing_total,
        "rust_extra_symbols": rust_extra_total,
        "samples_missing": samples_missing,
        "samples_extra": samples_extra,
        "errors": errors[:5],
    }


def benchmark_rust_batch(files, num_threads=8):
    """Rust 批量解析性能测试"""
    from callwarden_core import batch_parse_files_lang

    # 按语言分组（batch_parse_files_lang 一次只支持一种语言）
    by_lang = {}
    for abs_path, lang_id in files:
        by_lang.setdefault(lang_id, []).append((abs_path, os.path.splitext(os.path.basename(abs_path))[0]))

    total_symbols = 0
    total_calls = 0
    t0 = time.perf_counter()
    for lang_id, lang_files in by_lang.items():
        results = batch_parse_files_lang(lang_files, lang_id, num_threads=num_threads)
        for r in results:
            if not r.get("error"):
                total_symbols += len(r.get("symbols", []))
                total_calls += len(r.get("raw_calls", []))
    elapsed = time.perf_counter() - t0
    return elapsed, total_symbols, total_calls


def benchmark_python_serial(files):
    """Python 串行解析性能测试（基准）"""
    from callwarden.parsers import create_parser

    parser_cache = {}
    total_symbols = 0
    total_calls = 0
    t0 = time.perf_counter()
    for abs_path, _ in files:
        parser = parser_cache.get(abs_path[-3:])
        if parser is None:
            parser = create_parser(abs_path)
            if parser is None:
                continue
        # 简单缓存：按语言缓存一个实例（复用 tree cache）
        module_path = os.path.splitext(os.path.basename(abs_path))[0]
        try:
            result = parser.parse_file(abs_path, module_path)
            total_symbols += len(result.get("symbols", []))
            total_calls += len(result.get("raw_calls", []))
        except Exception:
            pass
    elapsed = time.perf_counter() - t0
    return elapsed, total_symbols, total_calls


def main():
    print("=" * 70)
    print("P31 真实大库验证：对齐度 + 性能对比")
    print("=" * 70)

    # 检查 Rust 扩展
    try:
        from callwarden_core import supported_languages
        langs = supported_languages()
        print(f"Rust 扩展可用，支持语言: {langs}")
    except ImportError:
        print("ERROR: callwarden_core 未安装，无法验证")
        return

    # === 1. 对齐度验证 ===
    print(f"\n{'─'*70}")
    print("阶段 1: 对齐度验证（采样对比 Rust vs Python 符号集合）")
    print(f"{'─'*70}")

    alignment_results = []
    for project in PROJECTS:
        path = os.path.join(_PKG_ROOT, project["path"])
        if not os.path.isdir(path):
            print(f"  跳过 {project['name']}（目录不存在）")
            continue
        print(f"\n  验证 {project['name']} ...", end=" ", flush=True)
        t0 = time.perf_counter()
        result = validate_alignment(project)
        t_elapsed = time.perf_counter() - t0
        print(f"采样 {result['total_sampled']} 文件，"
              f"完美匹配 {result['perfect_match']} ({result['perfect_match_rate']}%)，"
              f"对齐 {result['aligned_rate']}%，"
              f"漏 {result['rust_missing_symbols']}，多 {result['rust_extra_symbols']}"
              f"  [{t_elapsed:.1f}s]")
        if result["samples_missing"]:
            print(f"    漏提取样本（前 3）:")
            for s in result["samples_missing"][:3]:
                print(f"      {s['file']}: {s['missing']}")
        if result["samples_extra"]:
            print(f"    多提取样本（前 3）:")
            for s in result["samples_extra"][:3]:
                print(f"      {s['file']}: {s['extra']}")
        alignment_results.append(result)

    # === 2. 性能对比 ===
    print(f"\n{'─'*70}")
    print("阶段 2: 性能对比（Rust rayon 8 线程 vs Python 串行）")
    print(f"{'─'*70}")

    perf_results = []
    for project in PROJECTS:
        path = os.path.join(_PKG_ROOT, project["path"])
        if not os.path.isdir(path):
            continue
        files = _scan_files(path, project["langs"])
        if not files:
            continue

        print(f"\n  {project['name']} ({len(files)} 文件):")

        # Rust 批量
        print(f"    Rust batch (8 threads) ...", end=" ", flush=True)
        rust_time, rust_sym, rust_calls = benchmark_rust_batch(files, num_threads=8)
        print(f"{rust_time:.2f}s, {rust_sym} symbols, {rust_calls} calls")

        # Python 串行
        print(f"    Python serial         ...", end=" ", flush=True)
        py_time, py_sym, py_calls = benchmark_python_serial(files)
        print(f"{py_time:.2f}s, {py_sym} symbols, {py_calls} calls")

        speedup = py_time / rust_time if rust_time > 0 else 0
        sym_diff = rust_sym - py_sym
        print(f"    加速比: {speedup:.2f}x | 符号差: Rust-Python = {sym_diff:+d}")

        perf_results.append({
            "project": project["name"],
            "files": len(files),
            "rust": {"time_sec": round(rust_time, 2), "symbols": rust_sym, "calls": rust_calls},
            "python": {"time_sec": round(py_time, 2), "symbols": py_sym, "calls": py_calls},
            "speedup": round(speedup, 2),
            "symbol_diff": sym_diff,
        })

    # === 汇总 ===
    print(f"\n{'='*70}")
    print("汇总")
    print(f"{'='*70}")

    # 对齐度汇总
    if alignment_results:
        total_sampled = sum(r["total_sampled"] for r in alignment_results)
        total_perfect = sum(r["perfect_match"] for r in alignment_results)
        total_missing = sum(r["rust_missing_symbols"] for r in alignment_results)
        total_extra = sum(r["rust_extra_symbols"] for r in alignment_results)
        print(f"\n对齐度:")
        print(f"  采样文件: {total_sampled}")
        print(f"  完美匹配: {total_perfect} ({total_perfect/total_sampled*100:.1f}%)")
        print(f"  Rust 漏提取符号总数: {total_missing}")
        print(f"  Rust 多提取符号总数: {total_extra}")

    # 性能汇总
    if perf_results:
        total_files = sum(r["files"] for r in perf_results)
        total_rust_time = sum(r["rust"]["time_sec"] for r in perf_results)
        total_py_time = sum(r["python"]["time_sec"] for r in perf_results)
        total_rust_sym = sum(r["rust"]["symbols"] for r in perf_results)
        total_py_sym = sum(r["python"]["symbols"] for r in perf_results)
        overall_speedup = total_py_time / total_rust_time if total_rust_time > 0 else 0
        print(f"\n性能:")
        print(f"  总文件: {total_files}")
        print(f"  Rust 总耗时: {total_rust_time:.1f}s ({total_rust_sym} symbols)")
        print(f"  Python 总耗时: {total_py_time:.1f}s ({total_py_sym} symbols)")
        print(f"  总加速比: {overall_speedup:.2f}x")
        print(f"  符号差: Rust-Python = {total_rust_sym - total_py_sym:+d}")

    # 保存报告
    report = {
        "alignment": alignment_results,
        "performance": perf_results,
    }
    report_path = os.path.join(_PKG_ROOT, "tests", "_p31_validation_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {report_path}")


if __name__ == "__main__":
    main()
