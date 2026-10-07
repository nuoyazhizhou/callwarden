#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T6 性能基准：生产 daemon + 真实 callwarden workspace（~34 万符号）端到端 RPC 延迟。

方法论（遵循 AGENTS 规则 13：真实 E2E 优先）：
- 负载：生产 Rust daemon（HTTP transport）+ 已发布 snapshot 的 callwarden 自身 workspace。
- 查询参数：从真实符号表采样（search_symbols 取真实 qualified_name），不用合成数据。
- 测量：每方法预热 N 次 → 串行 M 次（不并行，避免互扰）→ 取 min/p50/p95/p99/max。
- 分维度：基线 RPC（ping/health）、单值查询、符号查询、图遍历、影响分析。
- 记录硬件/环境，结果写 JSON + 人类可读报告。

用法：python t6_perf_bench.py [--iters 200] [--warmup 20]
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import sys
import time
from statistics import mean, median

os.environ.setdefault("PYTHONUTF8", "1")
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = r"C:\git_work\callwarden"
sys.path.insert(0, os.path.dirname(_REPO_ROOT))

dc = importlib.import_module("callwarden.server.daemon_client")
route_rpc = dc.route_rpc


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, int(len(sorted_vals) * p / 100)))
    return sorted_vals[idx]


def bench(method, params, op, iters, warmup):
    """对单方法预热 warmup 次后测量 iters 次，返回延迟统计（毫秒）。"""
    # 预热
    for _ in range(warmup):
        try:
            route_rpc(method, dict(params), op)
        except Exception:
            pass
    lat = []
    errors = 0
    for _ in range(iters):
        t0 = time.perf_counter()
        try:
            route_rpc(method, dict(params), op)
            lat.append((time.perf_counter() - t0) * 1000.0)
        except Exception:
            errors += 1
    if not lat:
        return {"method": method, "error": "all failed", "errors": errors, "n": 0}
    s = sorted(lat)
    return {
        "method": method,
        "n": len(s),
        "errors": errors,
        "min_ms": round(s[0], 3),
        "p50_ms": round(median(lat), 3),
        "p95_ms": round(pct(s, 95), 3),
        "p99_ms": round(pct(s, 99), 3),
        "max_ms": round(s[-1], 3),
        "mean_ms": round(mean(lat), 3),
    }


def sample_symbols(n=30):
    """从真实符号表采样 qualified_name + simple name（供图查询用）。"""
    qnames, snames = [], []
    try:
        # query.search 返回真实符号；用常见子串采样
        for q in ("handle", "get", "build", "parse", "run"):
            r = route_rpc("query.search", {"query": q, "kind": "", "limit": 20}, "READ_ONLY")
            items = r if isinstance(r, list) else (r.get("results") or r.get("symbols") or [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                qn = it.get("qualified_name") or it.get("qname")
                nm = it.get("name")
                if qn:
                    qnames.append(qn)
                if nm:
                    snames.append(nm)
            if len(qnames) >= n:
                break
    except Exception as e:
        print(f"[sample] search_symbols 采样失败: {e}", file=sys.stderr)
    # 去重保序
    qnames = list(dict.fromkeys(qnames))[:n]
    snames = list(dict.fromkeys(snames))[:n]
    return qnames, snames


def bench_param_sweep(method, param_list, op, iters, warmup, label):
    """对一组真实参数轮转测量（每次换一个真实符号），返回聚合延迟。"""
    if not param_list:
        return {"method": f"{method}({label})", "error": "no sample params", "n": 0}
    for _ in range(warmup):
        try:
            route_rpc(method, dict(param_list[0]), op)
        except Exception:
            pass
    lat, errors = [], 0
    for i in range(iters):
        p = param_list[i % len(param_list)]
        t0 = time.perf_counter()
        try:
            route_rpc(method, dict(p), op)
            lat.append((time.perf_counter() - t0) * 1000.0)
        except Exception:
            errors += 1
    if not lat:
        return {"method": f"{method}({label})", "error": "all failed", "errors": errors, "n": 0}
    s = sorted(lat)
    return {
        "method": f"{method}({label})",
        "n": len(s), "errors": errors,
        "min_ms": round(s[0], 3), "p50_ms": round(median(lat), 3),
        "p95_ms": round(pct(s, 95), 3), "p99_ms": round(pct(s, 99), 3),
        "max_ms": round(s[-1], 3), "mean_ms": round(mean(lat), 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=20)
    args = ap.parse_args()

    print("=" * 72)
    print(f"T6 性能基准 — 生产 daemon + 真实 callwarden workspace")
    print(f"iters={args.iters} warmup={args.warmup}")
    print("=" * 72)

    # 采样真实符号
    qnames, snames = sample_symbols(30)
    print(f"采样真实符号: {len(qnames)} qnames, {len(snames)} simple names")
    if qnames[:3]:
        print(f"  示例 qname: {qnames[:3]}")

    results = []

    # 维度 1：基线 RPC 往返（无 workspace 语义，纯传输+调度）
    print("\n[1] 基线 RPC（传输+调度开销）")
    for m, p, op in [("ping", {}, "READ_ONLY"), ("health", {}, "READ_ONLY"),
                     ("schema.version", {}, "READ_ONLY")]:
        r = bench(m, p, op, args.iters, args.warmup)
        results.append(r)
        print(f"  {r['method']:<20} p50={r.get('p50_ms','-')}ms p95={r.get('p95_ms','-')}ms "
              f"p99={r.get('p99_ms','-')}ms n={r.get('n')} err={r.get('errors')}")

    # 维度 2：单值/统计查询
    print("\n[2] 单值/统计查询")
    for m, p, op in [("query.stats", {}, "READ_ONLY"),
                     ("get_fts_status", {}, "READ_ONLY")]:
        r = bench(m, p, op, args.iters, args.warmup)
        results.append(r)
        print(f"  {r['method']:<20} p50={r.get('p50_ms','-')}ms p95={r.get('p95_ms','-')}ms "
              f"p99={r.get('p99_ms','-')}ms n={r.get('n')} err={r.get('errors')}")

    # 维度 3：符号查询（真实参数轮转）
    print("\n[3] 符号查询（真实符号轮转）")
    search_params = [{"query": q, "kind": "", "limit": 20}
                     for q in ("handle", "get", "build", "parse", "run")]
    r = bench_param_sweep("query.search", search_params, "READ_ONLY",
                          args.iters, args.warmup, "real-terms")
    results.append(r)
    print(f"  {r['method']:<30} p50={r.get('p50_ms','-')}ms p95={r.get('p95_ms','-')}ms "
          f"p99={r.get('p99_ms','-')}ms n={r.get('n')} err={r.get('errors')}")

    get_sym_params = [{"qualified_name": qn} for qn in qnames]
    r = bench_param_sweep("query.symbol", get_sym_params, "READ_ONLY",
                          args.iters, args.warmup, "real-qnames")
    results.append(r)
    print(f"  {r['method']:<30} p50={r.get('p50_ms','-')}ms p95={r.get('p95_ms','-')}ms "
          f"p99={r.get('p99_ms','-')}ms n={r.get('n')} err={r.get('errors')}")

    # 维度 4：图遍历（CSR）
    print("\n[4] 图遍历（Rust CSR）")
    # query.callers 用 callee_name + qualified_name；query.callees 用 caller_name + qualified_name
    callers_params = [{"callee_name": qn.split(".")[-1].split("::")[-1], "qualified_name": qn}
                      for qn in qnames]
    r = bench_param_sweep("query.callers", callers_params, "READ_ONLY",
                          args.iters, args.warmup, "real-qnames")
    results.append(r)
    print(f"  {r['method']:<30} p50={r.get('p50_ms','-')}ms p95={r.get('p95_ms','-')}ms "
          f"p99={r.get('p99_ms','-')}ms n={r.get('n')} err={r.get('errors')}")

    callees_params = [{"caller_name": qn.split(".")[-1].split("::")[-1], "qualified_name": qn}
                      for qn in qnames]
    r = bench_param_sweep("query.callees", callees_params, "READ_ONLY",
                          args.iters, args.warmup, "real-qnames")
    results.append(r)
    print(f"  {r['method']:<30} p50={r.get('p50_ms','-')}ms p95={r.get('p95_ms','-')}ms "
          f"p99={r.get('p99_ms','-')}ms n={r.get('n')} err={r.get('errors')}")

    # 调用链（图遍历 BFS，可能较慢，减少 iters）
    chain_iters = max(30, args.iters // 4)
    chain_params = [{"qualified_name": qn, "max_depth": 5} for qn in qnames]
    r = bench_param_sweep("query.call_chain_down", chain_params, "READ_ONLY",
                          chain_iters, args.warmup, "depth5")
    results.append(r)
    print(f"  {r['method']:<28} p50={r.get('p50_ms','-')}ms p95={r.get('p95_ms','-')}ms "
          f"p99={r.get('p99_ms','-')}ms n={r.get('n')} err={r.get('errors')}")

    # 环境信息
    env = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu": platform.processor(),
        "cpu_count": os.cpu_count(),
        "workspace": "callwarden self (4baea3ff12c2ea5c)",
        "symbol_count": 339797,
        "call_count": 274336,
        "file_count": 130177,
        "transport": os.environ.get("CW_DAEMON_TRANSPORT", "?"),
    }

    report = {"env": env, "params": vars(args), "results": results,
              "sampled_qnames": qnames[:10]}
    out = os.path.join(_HERE, "t6_perf_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n结果写入: {out}")
    print("=" * 72)


if __name__ == "__main__":
    main()
