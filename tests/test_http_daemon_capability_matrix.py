import json
import os
from collections import Counter

EVIDENCE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..", ".trae-cn", "evidence", "http-daemon-capability-matrix.json"
)

# 以源码为真相源核对后的真实 rust_native 工具清单（tool_name -> dispatch.rs 真名）。
# 依据（H4B-M 整改 evidence log）：
# - tools_query.py 10 个 query.*（H4B-N 头部注释权威清单）
# - tools_task.py 5 个 query.issues/query.tests（M2.4/M2.5）+ 22 个 task.* 路由工具
#   （route_task_write/read HTTP 模式经 HttpDaemonRpcClient 透传，dispatch.rs 有真名分支）
# - tools_p4_lease.py 5 个 lease.*（_call_daemon_rpc 真名透传）
# - tools_workspace.py 2 个 workspace.list/workspace.activate
# - W4-1（T-1786886251769-22b94ee8-sub-1）：5 个 git 读面工具迁移 rust_native
#   （get_file_history/get_git_commits/get_commit_changes/get_git_stats/
#   get_commit_tasks，dispatch.rs query.* 真名分支）
# - W4-2（T-1786886251769-22b94ee8-sub-2）：2 个 coverage/review 读面工具迁移
#   rust_native（get_coverage_for_symbol/diff_to_symbol，dispatch.rs query.* 真名分支）
# - W4-3（T-1786886251769-22b94ee8-sub-3）：5 个 defect 读面工具迁移 rust_native
#   （defect_correlation/churn_analysis/defect_search/defect_suggest_fix/
#   get_defect_correlation，dispatch.rs query.* 真名分支；defect_learn 写面保持 python_compat）
# 该清单不是 baseline 复制，是 2026-08-14 源码核对结果；源码 RPC 路由变化时须同步。
RUST_NATIVE_EXPECTED = {
    "get_stats": "query.stats",
    "search_symbols": "query.search",
    "get_symbol": "query.symbol",
    "get_symbol_location": "query.symbol_location",
    "get_file_symbols": "query.file",
    "get_callers": "query.callers",
    "get_callees": "query.callees",
    "get_topological_order": "query.topological_order",
    "get_call_chain_down": "query.call_chain_down",
    "detect_cycles": "query.detect_cycles",
    "get_symbol_issues": "query.issues",
    "get_test_cases": "query.tests",
    "get_tested_functions": "query.tests",
    "get_test_coverage_summary": "query.tests",
    "get_test_stability": "query.tests",
    "task_create": "task.create",
    "task_next_step": "task.claim",
    "work_next_job": "task.work_next",
    "task_resolve_block": "task.reopen",
    "task_report_step": "task.report",
    "record_task_symbol_change": "task.record_symbol_change",
    "link_edit_audit_symbols": "task.link_edit_audit_symbols",
    "get_task_symbol_changes": "task.get_symbol_changes",
    "get_task_commits": "task.get_commits",
    "task_rollback": "task.rollback",
    "task_apply": "task.apply",
    "task_close": "task.close",
    "task_capture_diff": "task.capture_diff",
    "task_create_subtask": "task.create_subtask",
    "task_split": "task.split",
    "task_create_from_plan": "task.create_from_plan",
    "task_status_tree": "task.status_tree",
    "task_list": "task.list",
    "task_status": "task.status",
    "task_completion_review": "task.completion_review",
    "task_quality_findings": "task.quality_findings",
    "task_resolve_quality_finding": "task.resolve_quality_finding",
    "lease_acquire": "lease.acquire",
    "lease_renew": "lease.renew",
    "lease_release": "lease.release",
    "lease_status": "lease.status",
    "lease_list_events": "lease.list_events",
    "list_workspaces": "workspace.list",
    "get_active_workspace": "workspace.activate",
    "get_file_history": "query.file_history",
    "get_git_commits": "query.git_commits",
    "get_commit_changes": "query.git_commit_changes",
    "get_git_stats": "query.git_stats",
    "get_commit_tasks": "query.commit_tasks",
    "get_coverage_for_symbol": "query.coverage_for_symbol",
    "diff_to_symbol": "query.diff_to_symbol",
    "defect_correlation": "query.defect_correlation",
    "churn_analysis": "query.churn_analysis",
    "defect_search": "query.defect_search",
    "defect_suggest_fix": "query.defect_suggest_fix",
    "get_defect_correlation": "query.get_defect_correlation",
    "diff_branches": "query.diff_branches",
    # MCP-001（T-1787321708699-da5d8224）：get_role_view 迁移 rust_native
    "get_role_view": "role_view.get",
}


def _load_evidence():
    with open(EVIDENCE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_total_tools():
    data = _load_evidence()
    assert data["metadata"]["total_tools"] == 237
    assert len(data["tools"]) == 237


def test_all_tools_have_http_route():
    data = _load_evidence()
    for t in data["tools"]:
        route = t.get("http_route", "")
        assert route, "Tool %s has empty http_route" % t["tool_name"]
        assert route.startswith("/mcp/tools/"), (
            "Tool %s route %s does not start with /mcp/tools/" % (t["tool_name"], route)
        )


def test_no_unknown_route():
    data = _load_evidence()
    unknown = [t["tool_name"] for t in data["tools"]
               if t.get("http_route", "unknown") == "unknown"]
    assert len(unknown) == 0, "Tools with unknown routes: %s" % unknown


def test_all_tools_have_backend():
    data = _load_evidence()
    for t in data["tools"]:
        assert t.get("backend"), "Tool %s missing backend" % t["tool_name"]
        assert t["backend"] in ("legacy_local", "python_compat", "rust_native"), (
            "Tool %s has unknown backend: %s" % (t["tool_name"], t["backend"])
        )


def test_all_tools_have_operation_class():
    data = _load_evidence()
    valid_ops = ("read_only", "index_write", "governance_write")
    for t in data["tools"]:
        assert t.get("operation_class"), "Tool %s missing operation_class" % t["tool_name"]
        assert t["operation_class"] in valid_ops, (
            "Tool %s has invalid operation_class: %s" % (t["tool_name"], t["operation_class"])
        )


def test_all_tools_have_workspace_scope():
    data = _load_evidence()
    valid_scopes = ("workspace_scoped", "workspace_admin")
    for t in data["tools"]:
        assert t.get("workspace_scope"), "Tool %s missing workspace_scope" % t["tool_name"]
        assert t["workspace_scope"] in valid_scopes, (
            "Tool %s has invalid workspace_scope: %s" % (t["tool_name"], t["workspace_scope"])
        )


def test_all_tools_have_current_status():
    data = _load_evidence()
    for t in data["tools"]:
        assert t.get("current_status"), "Tool %s missing current_status" % t["tool_name"]


def test_metadata_aggregation_matches_tools():
    data = _load_evidence()
    tools = data["tools"]
    actual_backends = Counter(t["backend"] for t in tools)
    actual_ops = Counter(t["operation_class"] for t in tools)
    actual_scopes = Counter(t["workspace_scope"] for t in tools)
    meta = data["metadata"]["aggregation"]
    # Counter 归一化：两边的 0 计数键（legacy_local）均不参与相等比较
    assert actual_backends == Counter(meta["backend_distribution"])
    assert actual_ops == Counter(meta["operation_class_distribution"])
    assert actual_scopes == Counter(meta["workspace_scope_distribution"])


def test_http_route_coverage_matches():
    data = _load_evidence()
    tools = data["tools"]
    meta = data["metadata"]["http_route_coverage"]
    actual_with_route = sum(1 for t in tools if t.get("http_route", "unknown") != "unknown")
    actual_unknown = sum(1 for t in tools if t.get("http_route", "unknown") == "unknown")
    assert meta["with_route"] == actual_with_route
    assert meta["unknown_route"] == actual_unknown
    assert meta["total"] == len(tools)


def test_backend_distribution_matches_source_truth():
    """backend 分布与源码核对后的真实分布一致（非 baseline 复制）。

    2026-08-14 H4B-M 整改：移除对 baseline 19/190/28 的硬编码固化
    （baseline 直接复制导致 13 个无真实 RPC 的工具被标 rust_native）。
    真实分布以 RUST_NATIVE_EXPECTED（源码核对结果）为准。
    """
    data = _load_evidence()
    dist = data["metadata"]["aggregation"]["backend_distribution"]
    # MCP-001（T-1787321708699-da5d8224）：get_role_view 迁移 rust_native
    assert dist["rust_native"] == len(RUST_NATIVE_EXPECTED) == 58
    assert dist["python_compat"] == 179
    # legacy_local 分类已废止：所有工具按源码真实归类（真实 RPC -> rust_native，其余 -> python_compat）
    assert dist["legacy_local"] == 0
    # 与 tools 行聚合一致（防 metadata 与矩阵行脱节）；Counter 归一化忽略 0 计数键
    tools = data["tools"]
    assert Counter(t["backend"] for t in tools) == Counter(dist)


def test_rust_native_set_matches_source_truth():
    """矩阵中 backend=rust_native 的工具集合必须与源码核对白名单完全一致。

    防止两类失真：
    - 虚标：无真实 RPC 的工具被标 rust_native（如 find_issues/get_metrics/diff_callers）
    - 漏标：真实 RPC 工具未标 rust_native（如 lease.*/task.*/workspace.list）
    """
    data = _load_evidence()
    actual = {t["tool_name"]: t.get("daemon_rpc_method") for t in data["tools"]
              if t["backend"] == "rust_native"}
    assert actual == RUST_NATIVE_EXPECTED, (
        "rust_native 集合与源码白名单不一致\n  矩阵=%s\n  白名单=%s" % (
            sorted(actual), sorted(RUST_NATIVE_EXPECTED))
    )


def test_rust_native_requires_real_rpc():
    """交叉验证：rust_native 必 daemon_rpc_method≠none/unknown 且 rust_handler 指向 dispatch.rs。

    backend=rust_native 表示真实走 daemon RPC（dispatch.rs 有真名分支），
    daemon_rpc_method 与 rust_handler 必须为真名，禁止 none/unknown。
    """
    data = _load_evidence()
    for t in data["tools"]:
        if t["backend"] != "rust_native":
            continue
        rpc = t.get("daemon_rpc_method")
        handler = t.get("rust_handler")
        assert rpc and rpc not in ("none", "unknown"), (
            "rust_native %s 缺真实 daemon_rpc_method: %r" % (t["tool_name"], rpc))
        assert handler and handler.startswith("rust_ext/src/daemon/dispatch.rs::"), (
            "rust_native %s rust_handler 未指向 dispatch.rs: %r" % (t["tool_name"], handler))


def test_no_unknown_rpc_or_handler():
    """矩阵中不允许残留 unknown rpc/handler（diff_*/compare_snapshots 已清理）。"""
    data = _load_evidence()
    bad = [t["tool_name"] for t in data["tools"]
           if t.get("daemon_rpc_method") == "unknown" or t.get("rust_handler") == "unknown"]
    assert bad == [], "存在 unknown rpc/handler: %s" % bad


def test_python_compat_has_no_rust_handler():
    """python_compat 工具不得指向 rust handler（本地执行/fail-closed，无 Rust 路径）。"""
    data = _load_evidence()
    for t in data["tools"]:
        if t["backend"] == "python_compat":
            assert t.get("rust_handler") == "none", (
                "python_compat %s 不应有 rust_handler: %r" % (t["tool_name"], t.get("rust_handler")))


def test_legacy_local_retired():
    """legacy_local 分类已废止（按源码真实归类，成员并入 rust_native/python_compat）。"""
    data = _load_evidence()
    legacy = [t["tool_name"] for t in data["tools"] if t["backend"] == "legacy_local"]
    assert legacy == [], "legacy_local 仍有残留: %s" % legacy


def test_no_duplicate_tool_names():
    data = _load_evidence()
    names = [t["tool_name"] for t in data["tools"]]
    assert len(names) == len(set(names)), "Duplicate tool names found"


def test_evidence_file_exists():
    assert os.path.exists(EVIDENCE_PATH), "Evidence file not found: %s" % EVIDENCE_PATH
    assert os.path.getsize(EVIDENCE_PATH) > 0, "Evidence file is empty"
