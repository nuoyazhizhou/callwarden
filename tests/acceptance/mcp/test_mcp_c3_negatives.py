"""TP-10M C3：MCP 只读工具负向语义合同（验收套件）。

selector: tests/acceptance/mcp/test_mcp_c3_negatives.py

对只读工具喂**不存在的实体**（不存在的符号 / 文件 / workspace / id），
验收负向语义：

  - 返回**结构化业务错误**（not_found / empty / invalid_params 等）或
    **空结果**；
  - 绝不 DEFECT：不能是 method_not_found、daemon 崩溃、Python traceback、
    传输层失败。

这是「读面健壮性」验收：任何不存在实体的查询都必须 fail-closed 到结构化
业务错误，而不是崩溃或伪 DEFECT。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from mcp_wire_harness import McpWireHarness  # noqa: E402

_CONV = Path(__file__).resolve().parents[2] / "tests" / "convergence"
if str(_CONV) not in sys.path:
    sys.path.insert(0, str(_CONV))

import t2_mcp_runner as t2r  # noqa: E402

# 不存在的实体（确定性、可复现）
GHOST_QNAME = "tp10m::ghost::does_not_exist"
GHOST_NAME = "tp10m_ghost_function_xyz"
GHOST_FILE = "tp10m_ghost_dir/ghost_file.py"
GHOST_WS = "999999999"
GHOST_INSTANCE = "tp10mdeadbeef0000"

# 参与负向实测的只读工具及其「幽灵参数」（按参数名族构造）
NEGATIVE_CASES: list[tuple[str, dict]] = [
    ("get_symbol", {"qualified_name": GHOST_QNAME}),
    ("get_symbol_location", {"qualified_name": GHOST_QNAME}),
    ("get_symbol_issues", {"qualified_name": GHOST_QNAME}),
    ("get_symbol_history", {"qualified_name": GHOST_QNAME}),
    ("get_symbol_content_by_hash", {"symbol_hash": "0" * 64}),
    ("get_callers", {"callee_name": GHOST_NAME}),
    ("get_callees", {"caller_name": GHOST_NAME}),
    ("get_top_callers", {"callee_name": GHOST_NAME}),
    ("get_call_chain_down", {"qualified_name": GHOST_QNAME}),
    ("get_call_heatmap", {"qualified_name": GHOST_QNAME}),
    ("get_file_symbols", {"file_path": GHOST_FILE}),
    ("get_file_history", {"file_path": GHOST_FILE}),
    ("get_edit_history", {"file_path": GHOST_FILE}),
    ("file_read", {"file_path": GHOST_FILE}),
    ("file_grep", {"pattern": GHOST_NAME, "path": ""}),
    ("get_impact", {"symbol_name": GHOST_NAME}),
    ("get_dependency_edges", {"qualified_name": GHOST_QNAME}),
    ("get_resolved_edges", {"workspace_id": 999999,
                            "build_context_hash": "0" * 40}),
    ("get_function_metrics", {"qualified_name": GHOST_QNAME}),
    ("get_coverage_for_symbol", {"qualified_name": GHOST_QNAME}),
    ("get_comment_from_version", {"file_path": GHOST_FILE, "version": "1"}),
    ("get_build_context", {"workspace_id": 999999,
                           "build_context_hash": "0" * 40}),
    ("get_active_build_context", {"workspace_id": 999999}),
    ("list_build_contexts", {"workspace_id": 999999}),
    ("get_workspace_toolchains", {"workspace_id": 999999}),
    ("get_toolchain", {"workspace_id": 999999,
                       "toolchain_fingerprint": GHOST_NAME}),
    ("get_clone_group_detail", {"group_id": 999999}),
    ("get_clone_group_stats", {"workspace_id": 999999}),
    ("get_artifact_freshness", {"artifact_id": GHOST_NAME}),
    ("get_attestation_validity", {"attestation_id": GHOST_NAME}),
    ("get_gate_decision", {"gate_id": GHOST_NAME}),
    ("get_role_view", {"role": "tp10m-ghost-role"}),
    ("get_job_status", {"job_id": GHOST_NAME}),
    ("get_task_commits", {"task_id": GHOST_NAME}),
    ("get_symbol_commit_history", {"qualified_name": GHOST_QNAME}),
    ("get_symbol_change_tasks", {"qualified_name": GHOST_QNAME}),
    ("find_evidence", {"query": GHOST_NAME}),
    ("find_issues", {"query": GHOST_NAME}),
    ("find_similar_functions", {"qualified_name": GHOST_QNAME}),
    ("find_shared_symbols", {"symbol_name": GHOST_NAME}),
    ("find_uncovered_functions", {"workspace_id": 999999}),
    ("semantic_search", {"query": GHOST_NAME}),
    ("search_symbols", {"query": GHOST_NAME}),
    ("get_comment_coverage", {"workspace_id": 999999}),
    ("get_code_health_check", {"workspace_id": 999999}),
    ("get_code_metrics_summary", {"workspace_id": 999999}),
    ("get_complexity_hotspots", {"workspace_id": 999999}),
    ("get_coupling_analysis", {"workspace_id": 999999}),
    ("get_deepest_functions", {"workspace_id": 999999}),
    ("get_largest_functions", {"workspace_id": 999999}),
    ("get_most_coupled_functions", {"workspace_id": 999999}),
    ("get_orphan_symbols", {"workspace_id": 999999}),
    ("get_uncommented_symbols", {"workspace_id": 999999}),
    ("get_tested_functions", {"workspace_id": 999999}),
    ("get_test_stability", {"workspace_id": 999999}),
    ("get_token_savings_report", {"workspace_id": 999999}),
    ("hotspot_evolution", {"workspace_id": 999999}),
    ("evolution_frequency", {"workspace_id": 999999}),
    ("churn_analysis", {"workspace_id": 999999}),
    ("get_edit_stats", {"workspace_id": 999999}),
    ("get_git_stats", {"workspace_id": 999999}),
    ("get_module_call_stats", {"workspace_id": 999999}),
    ("get_semgrep_stats", {"workspace_id": 999999}),
    ("get_clone_stats", {"workspace_id": 999999}),
    ("get_vulnerability_blast_radius", {"symbol_name": GHOST_NAME}),
    ("get_clone_aware_impact", {"symbol_name": GHOST_NAME}),
    ("cross_layer_impact", {"symbol_name": GHOST_NAME}),
    ("cross_repo_impact", {"symbol_name": GHOST_NAME}),
    ("get_defect_correlation", {"symbol_name": GHOST_NAME}),
    ("defect_search", {"query": GHOST_NAME}),
    ("defect_stats", {"workspace_id": 999999}),
    ("get_issue_summary", {"workspace_id": 999999}),
    ("get_freshness_status", {"workspace_id": 999999}),
    ("get_summary", {"workspace_id": 999999}),
    ("get_metrics", {"workspace_id": 999999}),
    ("repo_map", {"workspace_id": 999999}),
    ("project_brief", {"workspace_id": 999999}),
    ("export_module_graph", {"workspace_id": 999999}),
    ("get_topological_order", {"workspace_id": 999999}),
    ("get_interface_providers", {"interface_name": GHOST_NAME}),
    ("get_ownership_map", {"workspace_id": 999999}),
    ("get_project_dependencies", {"workspace_id": 999999}),
    ("count_resolved_edges", {"workspace_id": 999999,
                              "build_context_hash": "0" * 40}),
    ("get_test_coverage", {"workspace_id": 999999}),
    ("get_test_coverage_summary", {"workspace_id": 999999}),
    ("get_test_cases", {"symbol_name": GHOST_NAME}),
    ("test_impact_selection", {"changed_files": [GHOST_FILE]}),
    ("get_task_symbol_changes", {"task_id": GHOST_NAME}),
    ("get_commit_changes", {"commit_sha": "0" * 40}),
    ("get_git_commits", {"workspace_id": 999999}),
    ("get_recent_changes", {"workspace_id": 999999}),
    ("merge_preview", {"workspace_id": 999999, "merge_request_id": GHOST_NAME}),
    ("review_readiness", {"task_id": GHOST_NAME}),
    ("who_to_ask", {"topic": GHOST_NAME}),
    ("ask_codebase", {"question": GHOST_NAME}),
    ("get_status", {"workspace_instance_id": GHOST_INSTANCE}),
]


@pytest.fixture(scope="module")
def harness():
    h = McpWireHarness()
    h.start()
    try:
        yield h
    finally:
        h.stop()


def _is_empty_success(r) -> bool:
    """空结果成功：交付、无错误、载荷为空（None / 空串 / 空集合）。"""
    if not r.delivered or r.isError or r.error_text:
        return False
    p = r.payload
    if p is None:
        return True
    if isinstance(p, str):
        return not p.strip()
    if isinstance(p, (list, dict, set)):
        return len(p) == 0
    return False


def test_c3_nonexistent_entities_never_defect(harness):
    """只读工具对不存在实体返回结构化业务错误或空结果，绝不 DEFECT。"""
    results = []
    defects = []
    t0 = time.time()
    for name, args in NEGATIVE_CASES:
        r = harness.call(name, dict(args))
        if not r.delivered:
            verdict = "DEFECT"
        elif r.isError or r.error_text:
            verdict = t2r._classify_error(r.error_text)
        else:
            verdict = "PASS_EMPTY" if _is_empty_success(r) else "PASS_DATA"
        entry = {"tool": name, "args": args, "verdict": verdict,
                 "delivered": r.delivered, "isError": r.isError,
                 "error_text": r.error_text[:160],
                 "exc": (f"{r.exc_type}: {r.exc_message[:160]}"
                         if not r.delivered else "")}
        results.append(entry)
        if verdict == "DEFECT":
            defects.append(entry)
    counts: dict[str, int] = {}
    for e in results:
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1
    evidence = {
        **harness.meta(),
        "suite": "TP-10M C3 只读工具负向语义",
        "cases": len(results),
        "counts": counts,
        "defect_count": len(defects),
        "defects": defects,
        "elapsed_seconds": round(time.time() - t0, 1),
        "verdict": "PASS" if not defects else "FAIL",
    }
    harness.write_report("tp10m_c3_negatives.json", evidence)
    msg = (f"C3: {len(defects)} DEFECTs of {len(results)} negative cases "
           f"(counts={counts})")
    if defects:
        msg += "\n" + "\n".join(
            f"  - {d['tool']}: {d.get('exc') or d['error_text']}"
            for d in defects[:20])
    assert not defects, msg


def test_c3_negative_cases_cover_substantial_read_surface(harness):
    """负向用例集合必须覆盖足够大的只读表面（防止用例集退化）。"""
    live = set(harness.tool_names)
    missing = [n for n, _ in NEGATIVE_CASES if n not in live]
    assert not missing, f"C3 references non-existent tools: {missing}"
    assert len(NEGATIVE_CASES) >= 60, (
        f"C3 coverage too thin: {len(NEGATIVE_CASES)} cases")


def test_c3_ghost_symbol_and_file_are_truly_absent(harness):
    """基线断言：幽灵符号/文件确实不存在（否则负向语义无法解释）。"""
    r = harness.call("search_symbols", {"query": "tp10m_ghost_function_xyz",
                                        "limit": 5})
    assert r.delivered, f"search_symbols wire failure: {r.exc_message[:120]}"
    payload = r.payload
    rows = payload if isinstance(payload, list) else (
        payload.get("results") if isinstance(payload, dict) else [])
    rows = rows or []
    names = {str(row.get("name")) for row in rows if isinstance(row, dict)}
    assert "tp10m_ghost_function_xyz" not in names, (
        f"ghost symbol unexpectedly present: {names}")
