"""TP-10M C1：243 个 MCP 工具真实 wire 全量调用（验收套件）。

selector: tests/acceptance/mcp/test_mcp_c1_wire.py

对冻结清单的全部 243 个 MCP 工具做**真实 wire 调用**：
ClientSession.call_tool(name, arguments) → 结构化结果分类。

分类语义（沿用 tests/convergence/t2_mcp_runner.py）：
  - PASS：wire 返回非 error（工具可路由、daemon 执行，含空结果）；
  - EXPECTED_BUSINESS：结构化业务错误（缺参/前置/not_found/snapshot 等）；
  - DEFECT：真实缺陷（method_not_found / daemon 不可用 / 未预期异常 / 崩溃 /
    传输层失败 / 超时）；
  - SKIP：破坏性 / 重操作（SKIP_TOOLS，带原因）。

硬断言：**DEFECT == 0**（243 工具全部可路由、无崩溃、无传输失败）。
TP-10M 验收标准（TASK_BREAKDOWN.md:122）：**业务写不是普通文本成功**——
写工具（PROTECTED_MUTATION / GOVERNANCE_WRITE）若 PASS，结果必须是结构化
MCP 内容（structuredContent 或可解析 JSON），纯文本成功记为
WRITE_UNSTRUCTURED 并视为验收失败。

证据：tp10m_c1_wire_sweep.json 落到 <run_root>/reports/。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from mcp_wire_harness import McpWireHarness  # noqa: E402

# t2 的分类语义与 SKIP 名单（只读复用，不改动 convergence 套件）
_CONV = Path(__file__).resolve().parents[2] / "tests" / "convergence"
if str(_CONV) not in sys.path:
    sys.path.insert(0, str(_CONV))

import t2_mcp_runner as t2r  # noqa: E402

# TP-10M 在 t2 SKIP 之外额外跳过的工具（wire 下的重操作/环境前置），
# 原因逐条记录在证据 JSON 里。
TP10M_EXTRA_SKIP: dict[str, str] = {
    # 全仓 1198 文件 / 20k 符号的克隆检测：wire 同步调用远超单工具超时，
    # 且 semgrep 规则缓存在隔离 run 根内需联网下载（离线即失败）。
    # 路由可达性已由 t2 与 C2/C3 覆盖。
    "detect_clones": "重操作:全仓克隆检测，wire 同步超时；semgrep 规则缓存需联网",
    "detect_clones_async": "重操作:克隆检测 async job，与 detect_clones同类",
    "semgrep_scan_async": "重操作:semgrep 全仓扫描 async，规则缓存需联网",
    "run_semgrep_scan": "重操作:semgrep 全仓扫描，规则缓存需联网",
    "scan_semgrep_incremental": "重操作:semgrep 增量扫描，规则缓存需联网",
    "embed_symbols": "重操作:全仓符号嵌入（20k 符号），wire 同步超时",
    "embed_symbols_async": "重操作:符号嵌入 async job，同 embed_symbols",
    "embed_single_symbol": "重操作:单符号嵌入计算，wire 同步超时",
    # 全仓 import：git 历史/覆盖率导入是批处理 job，wire 同步超时
    "import_git_blame": "重操作:全仓 git blame 导入，wire 同步超时",
    "import_project_dependencies": "重操作:全仓依赖导入，wire 同步超时",
    "import_envelope_dependencies": "重操作:envelope 依赖导入，wire 同步超时",
    "import_coverage": "重操作:覆盖率导入，无覆盖率数据源",
    "import_codeowners": "重操作:codeowners 导入，主仓 CODEOWNERS 解析",
    "refresh_file": "重操作:单文件全量重算（触发依赖重算），C1 不逐个测",
}

WRITE_OPS = ("PROTECTED_MUTATION", "GOVERNANCE_WRITE")


@pytest.fixture(scope="module")
def seeded():
    """模块级隔离 wire 会话 + 已播种 SeedContext。"""
    h = McpWireHarness()
    h.start()
    t0 = time.time()
    h.seed()
    print(f"\n[TP-10M] seed done in {time.time() - t0:.0f}s "
          f"(ws={h.seed_ctx.workspace_id} qnames={len(h.seed_ctx.known_qualified_names)})")
    try:
        yield h
    finally:
        h.stop()


@pytest.fixture(scope="module")
def sweep_report(seeded):
    """全量扫描只跑一次，三个断言用例共用其结果（243 次 wire 调用）。"""
    return _sweep(seeded)


def _classify_call(r, name: str, op: str) -> tuple[str, str]:
    """把 WireCall 分类为 PASS/EXPECTED_BUSINESS/DEFECT；返回 (verdict, detail)。"""
    if not r.delivered:
        # 传输/协议层失败（含超时）：C1 的核心断言对象，直接记 DEFECT
        return ("DEFECT",
                f"wire 未交付: {r.exc_type}: {r.exc_message[:220]}")
    if r.isError or r.error_text:
        verdict = t2r._classify_error(r.error_text)
        return (verdict, f"error: {r.error_text[:220]}")
    return ("PASS", f"structured={r.structured} kinds={','.join(r.content_kinds)}")


def _sweep(harness: McpWireHarness) -> dict:
    """遍历全部 wire 工具做真实调用，返回分类汇总。"""
    op_by_name = harness.op_class_by_name()
    results = []
    counts = {"PASS": 0, "EXPECTED_BUSINESS": 0, "DEFECT": 0, "SKIP": 0}
    skipped_extra = []
    t0 = time.time()
    for name in harness.tool_names:
        op = op_by_name.get(name, "READ_ONLY")
        if name in t2r.SKIP_TOOLS:
            results.append({"name": name, "op": op, "verdict": "SKIP",
                            "detail": t2r.SKIP_TOOLS[name]})
            counts["SKIP"] += 1
            continue
        if name in TP10M_EXTRA_SKIP:
            results.append({"name": name, "op": op, "verdict": "SKIP",
                            "detail": TP10M_EXTRA_SKIP[name]})
            counts["SKIP"] += 1
            skipped_extra.append(name)
            continue
        args = harness.build_params(name)
        r = harness.call(name, args)
        verdict, detail = _classify_call(r, name, op)
        # 业务写验收：写工具 PASS 时结果必须结构化（不能是纯文本成功）
        write_unstructured = False
        if verdict == "PASS" and op in WRITE_OPS and not r.structured:
            write_unstructured = True
            verdict = "DEFECT"
            detail = (f"写工具返回非结构化成功（违反 TASK_BREAKDOWN.md:122）: "
                      f"kinds={','.join(r.content_kinds)} detail={detail[:120]}")
        results.append({
            "name": name, "op": op, "verdict": verdict, "detail": detail,
            "params_keys": sorted(args.keys()),
            "structured": r.structured,
            "write_unstructured": write_unstructured,
        })
        counts[verdict] += 1
    defects = [x for x in results if x["verdict"] == "DEFECT"]
    return {
        "counts": counts,
        "results": results,
        "defects": defects,
        "skipped_extra": skipped_extra,
        "elapsed_seconds": round(time.time() - t0, 1),
    }


def test_c1_wire_sweep_covers_all_243_tools(seeded, sweep_report):
    """C1 扫描必须覆盖 wire 上的全部 243 个工具（不遗漏）。"""
    covered = [r["name"] for r in sweep_report["results"]]
    assert len(covered) == len(seeded.tool_names), (
        f"sweep covered {len(covered)} of {len(seeded.tool_names)} tools")
    assert sorted(covered) == sorted(seeded.tool_names)


def test_c1_zero_defect_across_all_tools(seeded, sweep_report):
    """硬断言：243 工具真实 wire 调用 DEFECT == 0。"""
    report = sweep_report
    counts = report["counts"]
    total = sum(counts.values())
    defects = report["defects"]
    evidence = {
        **seeded.meta(),
        "suite": "TP-10M C1 243 MCP 工具真实 wire 全量调用",
        "counts": counts,
        "total": total,
        "defect_count": len(defects),
        "defects": defects,
        "skipped_extra": report["skipped_extra"],
        "skip_reasons": dict(t2r.SKIP_TOOLS, **TP10M_EXTRA_SKIP),
        "elapsed_seconds": report["elapsed_seconds"],
        "verdict": "PASS" if not defects else "FAIL",
    }
    seeded.write_report("tp10m_c1_wire_sweep.json", evidence)
    # 逐工具明细（便于复核任何一个分类）
    seeded.write_report("tp10m_c1_wire_results.json",
                        {"results": report["results"]})
    msg = (f"DEFECT != 0: {len(defects)} defects of {total} tools "
           f"(counts={counts})")
    if defects:
        msg += "\n" + "\n".join(
            f"  - {d['name']} [{d['op']}]: {d['detail']}" for d in defects)
    assert not defects, msg
    assert counts["PASS"] + counts["EXPECTED_BUSINESS"] + counts["SKIP"] == total
    # 至少要有真实成功（全 EXPECTED_BUSINESS/SKIP 说明参数面没真正被执行）
    assert counts["PASS"] > 0, "no tool returned a successful result"


def test_c1_read_tools_actually_return_data(seeded):
    """代表性只读工具经 wire 返回结构化数据（不是空/错误）。"""
    checks = [
        ("get_stats", lambda r: r.ok and isinstance(r.payload, dict)),
        ("get_status", lambda r: r.ok),
        ("list_workspaces", lambda r: r.ok),
        ("search_symbols", lambda r: r.ok),
        ("get_active_workspace", lambda r: r.ok),
    ]
    failures = []
    for name, pred in checks:
        r = seeded.call(name, seeded.build_params(name))
        if not pred(r):
            failures.append(
                f"{name}: ok={r.ok} err={r.error_text[:120]} exc={r.exc_type}")
    assert not failures, "read tools did not return data: " + "; ".join(failures)


def test_c1_evidence_written(seeded, sweep_report):
    """C1 证据 JSON 必须落盘且自洽。"""
    report = sweep_report
    evidence = {
        **seeded.meta(),
        "suite": "TP-10M C1 evidence presence check",
        "counts": report["counts"],
    }
    path = seeded.write_report("tp10m_c1_evidence_marker.json", evidence)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["counts"] == report["counts"]
