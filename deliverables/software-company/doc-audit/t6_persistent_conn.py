#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T6 补充：持久连接 vs 每调用新建连接的延迟对照。

目的：T6 主基准经 route_rpc 走 HTTP，p50 低但 p95/p99 尾延迟大。本脚本用同一
HttpDaemonRpcClient 实例持久复用连接重测相同方法，若尾延迟大幅下降，则证明
尾延迟根因是 HTTP 连接建立开销（而非查询本身或 daemon 调度）。
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
from statistics import mean, median

os.environ.setdefault("PYTHONUTF8", "1")
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = r"C:\git_work\callwarden"
sys.path.insert(0, os.path.dirname(_REPO_ROOT))

da = importlib.import_module("callwarden.server.daemon_autostart")
dcmod = importlib.import_module("callwarden.server.daemon_client")


def pct(s, p):
    if not s:
        return 0.0
    return s[max(0, min(len(s) - 1, int(len(s) * p / 100)))]


def stat(lat):
    s = sorted(lat)
    return {
        "n": len(s), "min_ms": round(s[0], 3), "p50_ms": round(median(lat), 3),
        "p95_ms": round(pct(s, 95), 3), "p99_ms": round(pct(s, 99), 3),
        "max_ms": round(s[-1], 3), "mean_ms": round(mean(lat), 3),
    }


def main():
    iters = 200
    warmup = 25
    # 发现 daemon endpoint + 构造持久 HTTP client
    client = None
    # HttpDaemonRpcClient 单例：复用同一 client（workspace 发现/authority 复用），
    # 对照 route_rpc 每调用新建的开销。
    try:
        client = dcmod.HttpDaemonRpcClient.get_instance()
        _ = client.call("ping", {})  # 预热发现
    except Exception as e:
        print(f"HttpDaemonRpcClient 构造失败: {e}", file=sys.stderr)

    if client is None or not hasattr(client, "call"):
        print(json.dumps({"error": "无法获得持久 client"}, ensure_ascii=False))
        return

    methods = [
        ("ping", {}),
        ("schema.version", {}),
        ("query.callers", {"callee_name": "recover", "qualified_name":
                           "server.health_check.RecoveryHandler.recover"}),
    ]
    results = {}
    for method, params in methods:
        for _ in range(warmup):
            try:
                client.call(method, dict(params))
            except Exception:
                pass
        lat, err = [], 0
        for _ in range(iters):
            t0 = time.perf_counter()
            try:
                client.call(method, dict(params))
                lat.append((time.perf_counter() - t0) * 1000.0)
            except Exception:
                err += 1
        results[method] = (stat(lat) if lat else {"error": "all failed", "errors": err})
        r = results[method]
        print(f"[持久连接] {method:<16} p50={r.get('p50_ms','-')}ms "
              f"p95={r.get('p95_ms','-')}ms p99={r.get('p99_ms','-')}ms "
              f"min={r.get('min_ms','-')}ms n={r.get('n')}")

    out = os.path.join(_HERE, "t6_persistent_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"mode": "persistent_connection", "iters": iters, "results": results},
                  f, ensure_ascii=False, indent=2)
    print(f"写入: {out}")


if __name__ == "__main__":
    main()
