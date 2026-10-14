"""TP-11P 性能域验收：隔离 daemon 上的测量 + 结构不变量。

🔴 顺序场景套件（按定义顺序执行，共享 module 级 env）：
- P1 冷全量构建（220 文件合成语料）：测量完备性——ok/scanned/symbols/
  elapsed/RSS 全部可观测，非空非零；
- P2 增量优势与正确性：单文件 refresh 必须显著低于全量构建（结构比
  <1/4），且新符号即时可读（正确性不因提速牺牲）；
- P3 RSS 护栏：全量构建后工作集 < 2 GiB、增量 < 1 GiB；增量刷新后
  工作集增量 < 256 MiB（防每次 refresh 泄漏）；
- P4 廉价 RPC 延迟分布：workspace.status/file.read/file.health 各 30 次，
  median < 1s、p95 < 2s（护栏级，非 SLO）；
- P5 重复性 sanity：再次全量构建两次，第 3 次耗时 < 第 1 次 ×3。

🔴 SLO 纪律（TESTING_PLAN §8）：硬件/corpus/profile 预算未冻结 → 本域
**不宣称达到任何 SLO**。上述阈值全是宽裕护栏（抓病态回归：快路径失效、
内存失控、延迟爆炸），不是性能目标。基线测量值由 fixture 落
`reports/tp11p_baseline.json`，供后续按固定硬件/corpus 冻结预算后
转为正式断言。

首跑基线（2026-10-14，220 文件合成语料=200 py + 10 rs + 10 js，
本机 win32；仅存档，非 SLO）：
- 冷全量构建 7.44s / 1220 符号 / RSS 25.9 MiB；warm 重建 5.04s
- 单文件增量刷新 0.21s（=冷构建 2.8%，护栏 25% 有 9× 余量）；
  第 2/3 次全量 5.45s / 4.97s（无渐进退化）
- 5 次增量刷新后 RSS 增量 +256 KiB（无泄漏迹象）
- 廉价 RPC median 2.2–3.3ms / p95 ≤ 16ms（护栏 1000/2000ms）

计时全部客户端侧 perf_counter（daemon 无服务端计时面）；RSS 走 WMI
WorkingSetSize（按 manifest/Popen 自洽 PID，查询失败不伪造）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pytest  # noqa: E402

from performance_harness import (  # noqa: E402
    EXTRA_JS_FILES,
    EXTRA_RS_FILES,
    DEFAULT_PY_FILES,
    DEFAULT_PY_PACKAGES,
    PerfEnv,
    summarize,
)

# 护栏级阈值（非 SLO，见模块 docstring）
RSS_CEILING_KB = 2 * 1024 * 1024          # 2 GiB 全量构建后工作集天花板
RSS_DELTA_KB = 1024 * 1024                # 1 GiB 构建增量天花板
RSS_INCREMENTAL_DELTA_KB = 256 * 1024     # 256 MiB 增量刷新增量天花板
LATENCY_MEDIAN_S = 1.0                    # 廉价 RPC median 护栏
LATENCY_P95_S = 2.0                       # 廉价 RPC p95 护栏
INCREMENTAL_RATIO = 4.0                   # refresh 必须 < cold_build / 4
REPEAT_FACTOR = 3.0                       # 第 3 次 cold < 第 1 次 ×3

EXPECTED_FILES = (DEFAULT_PY_FILES + EXTRA_RS_FILES + EXTRA_JS_FILES)


@pytest.fixture(scope="module")
def env() -> PerfEnv:
    probe = PerfEnv()
    PerfEnv.wipe_run_root(probe.run_root)
    e = PerfEnv()
    e.start_daemon()
    e.setup_workspace()
    files = e.generate_corpus()
    assert len(files) == EXPECTED_FILES, (
        f"语料文件数应={EXPECTED_FILES}，实际={len(files)}")
    e.metrics["corpus"] = {
        "files": len(files), "py": DEFAULT_PY_FILES,
        "rs": EXTRA_RS_FILES, "js": EXTRA_JS_FILES,
        "packages": DEFAULT_PY_PACKAGES,
    }
    try:
        yield e
    finally:
        e.write_baseline()
        e.stop_daemon()


# ---------------------------------------------------------------- P1 冷构建

def test_p1_cold_full_build_measurements_complete(env: PerfEnv):
    r, dt = env.direct_build_graph()
    assert r.ok, f"冷全量建图失败：{r!r}"
    res = r.result or {}
    assert res.get("scanned", 0) >= EXPECTED_FILES, (
        f"scanned 应覆盖全部合成语料（≥{EXPECTED_FILES}）：{res}")
    assert res.get("symbols", 0) >= 1000, (
        f"符号数应 ≥1000（200 模块 × 6 函数 + 多语言）：{res}")
    assert dt > 0, "冷构建耗时必须可观测"

    rss_after = env.daemon_rss_kb()
    assert rss_after is not None and rss_after > 0, (
        "构建后 daemon RSS 必须可观测（WMI 查询失败不得伪造通过）")

    env.metrics["p1_cold_build"] = {
        "elapsed_s": dt, "scanned": res.get("scanned"),
        "inserted": res.get("inserted"), "symbols": res.get("symbols"),
        "rss_after_kb": rss_after,
    }


# ---------------------------------------------------------------- P2 增量优势

def test_p2_incremental_refresh_beats_full_build_and_stays_correct(
        env: PerfEnv):
    cold = env.metrics.get("p1_cold_build", {}).get("elapsed_s")
    assert cold and cold > 0, "P1 必须先测得冷构建耗时"

    # 改一个文件：新增一个函数（内容变化 → 走解析路径）
    target = env.workspace_dir / "src" / "pkg00" / "m0000.py"
    original = target.read_text(encoding="utf-8")
    target.write_text(original + "\n\ndef tp11p_added_fn(z):\n    return z + 1\n",
                      encoding="utf-8")

    r, dt = env.timed_rpc("workspace.file.refresh_file", {
        "workspace_instance_id": env.workspace_instance_id,
        "file_path": str(target)})
    assert r.ok, f"单文件增量刷新失败：{r!r}"
    res = r.result or {}
    assert res.get("symbols", 0) >= 7, (
        f"刷新后该文件应含 7 个函数符号：{res}")

    # 正确性：新函数即时可读（读面走 live DB，不经快照门）
    rd = env.rpc("workspace.file.read", {
        "workspace_instance_id": env.workspace_instance_id,
        "file_path": str(target)})
    assert rd.ok and "tp11p_added_fn" in (rd.result or {}).get("content", ""), (
        f"增量刷新后新函数应可读：{rd!r}")

    # 结构性增量优势：refresh << cold
    assert dt < cold / INCREMENTAL_RATIO, (
        f"单文件刷新 {dt:.3f}s 未显著低于冷构建 {cold:.3f}s "
        f"（结构比 1/{INCREMENTAL_RATIO:.0f}）——增量快路径疑似失效")

    # warm 全量重建（语料已含 1 处编辑）：不应病态变慢
    r2, dt2 = env.direct_build_graph()
    assert r2.ok, f"warm 重建失败：{r2!r}"
    assert dt2 < cold * REPEAT_FACTOR, (
        f"warm 重建 {dt2:.3f}s 病态慢于冷构建 {cold:.3f}s")

    env.metrics["p2_incremental"] = {
        "refresh_elapsed_s": dt, "cold_build_s": cold,
        "ratio": dt / cold, "warm_rebuild_s": dt2,
        "symbols_after_refresh": res.get("symbols"),
    }


# ---------------------------------------------------------------- P3 RSS 护栏

def test_p3_rss_guardrails(env: PerfEnv):
    rss = env.daemon_rss_kb()
    assert rss is not None, "RSS 必须可观测"
    assert rss < RSS_CEILING_KB, (
        f"全量构建后 daemon 工作集 {rss/1024:.1f} MiB 超过护栏 "
        f"{RSS_CEILING_KB/1024:.0f} MiB（内存失控信号）")

    cold = env.metrics.get("p1_cold_build", {}).get("elapsed_s", 0)
    rss_before = env.metrics.get("p1_cold_build", {}).get("rss_after_kb")

    # 连续 5 次增量刷新，RSS 增量必须受控（防每次 refresh 泄漏）
    probe_files = sorted((env.workspace_dir / "src" / "pkg01").glob("m*.py"))[:5]
    assert len(probe_files) == 5, (
        f"RSS 探针应取到 5 个现存文件，实际={len(probe_files)}")
    for i, f in enumerate(probe_files):
        content = f.read_text(encoding="utf-8")
        f.write_text(content + f"\n# tp11p rss probe {i}\n", encoding="utf-8")
        r, _ = env.timed_rpc("workspace.file.refresh_file", {
            "workspace_instance_id": env.workspace_instance_id,
            "file_path": str(f)})
        assert r.ok, f"RSS 探针第 {i} 次刷新失败：{r!r}"

    rss_after_incr = env.daemon_rss_kb()
    assert rss_after_incr is not None, "增量后 RSS 必须可观测"
    delta = rss_after_incr - (rss_before or rss_after_incr)
    assert rss_after_incr < RSS_CEILING_KB, (
        f"增量刷新后工作集 {rss_after_incr/1024:.1f} MiB 超护栏")
    assert delta < RSS_INCREMENTAL_DELTA_KB, (
        f"5 次增量刷新 RSS 增量 {delta/1024:.1f} MiB 超护栏 "
        f"{RSS_INCREMENTAL_DELTA_KB/1024:.0f} MiB（泄漏信号）")

    env.metrics["p3_rss"] = {
        "rss_after_build_kb": rss, "rss_after_incremental_kb": rss_after_incr,
        "incremental_delta_kb": delta,
        "cold_build_s": cold,
    }


# ---------------------------------------------------------------- P4 廉价 RPC 延迟

def test_p4_cheap_rpc_latency_distribution(env: PerfEnv):
    seed_file = env.workspace_dir / "src" / "pkg02" / "m0010.py"
    samples: dict = {}
    for name, method, params_fn in (
        ("workspace.status", "workspace.status",
         lambda: {"workspace_instance_id": env.workspace_instance_id}),
        ("file.read", "workspace.file.read",
         lambda: {"workspace_instance_id": env.workspace_instance_id,
                  "file_path": str(seed_file)}),
        ("file.health", "workspace.file.health",
         lambda: {"workspace_instance_id": env.workspace_instance_id,
                  "file_path": str(seed_file)}),
    ):
        s = env.latency_samples(method, params_fn, n=30)
        assert all(x > 0 for x in s), f"{name} 存在非正耗时样本"
        stats = summarize(s)
        samples[name] = stats
        assert stats["median"] < LATENCY_MEDIAN_S, (
            f"{name} median {stats['median']*1000:.1f}ms 超护栏 "
            f"{LATENCY_MEDIAN_S*1000:.0f}ms")
        assert stats["p95"] < LATENCY_P95_S, (
            f"{name} p95 {stats['p95']*1000:.1f}ms 超护栏 "
            f"{LATENCY_P95_S*1000:.0f}ms")

    env.metrics["p4_latency"] = samples


# ---------------------------------------------------------------- P5 重复性

def test_p5_repeat_build_sanity(env: PerfEnv):
    cold = env.metrics.get("p1_cold_build", {}).get("elapsed_s")
    assert cold and cold > 0, "P1 必须先测得冷构建耗时"
    times = []
    for i in range(2):
        r, dt = env.direct_build_graph()
        assert r.ok, f"第 {i + 2} 次全量建图失败：{r!r}"
        times.append(dt)
    third = times[-1]
    assert third < cold * REPEAT_FACTOR, (
        f"第 3 次全量建图 {third:.3f}s 病态慢于首次 {cold:.3f}s "
        f"（×{REPEAT_FACTOR:.0f} 护栏）——渐进退化信号")
    env.metrics["p5_repeat"] = {
        "first_cold_s": cold, "second_s": times[0], "third_s": third,
    }
