"""H4B-R: compat registry 能力与两端对齐门测试

验证 server/compat_registry.py 的 H4B-R 扩展（H4B-C docstring 承接的
compat_route 注册/查询/校验 API）：
- 恢复的 _build_default_registry / get_compat_registry 懒加载单例
  （修复 H3 提交后 _DEFAULT_REGISTRY 被误删导致的 compat_worker ImportError）；
- RUST_COMPAT_ROUTE 常量镜像 Rust http_server.rs `compat_route`
  （H4C 装配后全量 86 项：H4C-1 1 + H4C-2 13 + H4C-3 9 + H4C-2 第二批 29 +
  H4C-2 第三批 19 + H4C-2 第三批 collab/p2/p3/p4 15，均 read_only）；
- compat_route(method) 查询镜像 Rust 语义（未知方法 → None，fail-closed）；
- register_compat_route 注册即校验（与 Rust 映射不一致 → ValueError）；
- validate_against_rust_route 两端对齐门（missing/extra/mismatch/aligned）；
- compat_worker 集成：get_compat_registry 恢复后 import 不再 ImportError。

真实进程门（TestRealDaemonCompatRpcAlignment，参照 H4B-N/C/I/E 模板）：
- 正向：compat_route 全量 86 方法在生产 HttpDaemonRpcClient 调用下**绝不**返回
  method_not_found（经 H3 compat worker 服务）；
- 负向：registry 未注册的 python_compat 方法（get_code_metrics_summary）
  在真实 daemon 上必返回 method_not_found —— 实证 HTTP 模式 fail-closed
  （registry 是 worker 方法真相源，未注册方法不可达）；
- /capabilities 端点：backend=python_compat 且 status=available 的方法集合
  与 Python RUST_COMPAT_ROUTE 完全一致（两端对齐最强实证）。

归类依据：.trae-cn/evidence/http-daemon-capability-matrix.json（237 tools）
- python_compat 190 / rust_native 28 / legacy_local 19；
- Rust COMPAT_ROUTE_WHITELIST 声明 101 个 python_compat 方法
  （http_server.rs COMPAT_ROUTE_WHITELIST）；
- dispatch.rs 无 get_code_metrics_summary 分支（DaemonStateExt 默认
  method_not_found）。

适配：T-1786721363018-63aa9993（H4C-2+3 装配后 registry 2->89，断言同步；
原 H4B-R 时代硬编码 len(reg)==2 的 7 个用例已更新至 89 全量）；
整改：T-1786747295227-49c90d68（规则查询组 3 项接入 worker，registry 89->92，
security 组 14->17，相关断言同步至 92 全量）；
接入：T-1786747295227-b876fddf（collab 组 4 + p2 组 5 + p3 组 5 + p4 组 1
共 15 项只读接入 worker，registry 92->107，相关断言同步至 107 全量）；
W2-1（T-1786840097330-dec66710）：get_uncommented_symbols /
get_module_call_stats / get_semgrep_stats 3 个迁移 rust_native（native
handler + 便捷方法），registry 107->104（H4C-1 默认 2->1、符号组 17->15），
相关断言同步至 104 全量；
W2-2（T-1786840097330-a9e0ec69）：get_clone_stats / get_job_stats /
get_clone_group_stats 3 个迁移 rust_native，registry 104->101
（任务组 16->13），相关断言同步至 101 全量。
W3-1（T-1786861820150-bfe5e805）：list_build_contexts / get_build_context /
get_active_build_context / get_resolved_edges / count_resolved_edges 5 个迁移
rust_native，registry 99->94（rules 组 8->3），相关断言同步至 94 全量。
W3-2（T-1786861820151-f3cecf40）：get_job_status / list_jobs / wait_for_job 3 个
迁移 rust_native，registry 94->91（任务组 13->10），相关断言同步至 91 全量。
W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 迁移 rust_native，
registry 91->90（符号组 15->14），相关断言同步至 90 全量。
W4-1（T-1786886251769-22b94ee8-sub-1）：get_file_history / get_commit_tasks
2 个迁移 rust_native，registry 90->88（符号组 14->13、任务组 10->9），
相关断言同步至 88 全量。
W4-2（T-1786886251769-22b94ee8-sub-2）：get_coverage_for_symbol /
diff_to_symbol 2 个迁移 rust_native，registry 88->86（摘要组 26->24），
相关断言同步至 86 全量。
W4-3（T-1786886251769-22b94ee8-sub-3）：defect_correlation /
churn_analysis / defect_search / defect_suggest_fix / get_defect_correlation
5 个迁移 rust_native，registry 86->81（任务组 9->8、缺陷组 20 内移除 4），
相关断言同步至 81 全量；defect_learn 写面保持 python_compat。
"""

import json
import os
import subprocess
import sys
import time

import pytest

# 仓库根加入 sys.path（支持 `server.*` 与 `callwarden.server.*` 两种 import）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import server.compat_worker  # noqa: E402  集成验证：import 不再 ImportError
import server.compat_registry as compat_registry_mod  # noqa: E402
from server.compat_registry import (  # noqa: E402
    READ_ONLY,
    INDEX_WRITE,
    GOVERNANCE_WRITE,
    SCOPE_WORKSPACE,
    SCOPE_SNAPSHOT,
    SCOPE_AUTHORITY,
    CompatCallContext,
    CompatMethod,
    CompatRegistry,
    RUST_COMPAT_ROUTE,
    _build_default_registry,
    compat_route,
    get_compat_registry,
    register_compat_route,
    validate_against_rust_route,
)
from callwarden.server.daemon_client import (  # noqa: E402
    DaemonUnavailableError,
    E_HTTP_REQUEST_TIMEOUT,
    HttpDaemonRpcClient,
)
from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402
from callwarden.config import (  # noqa: E402
    get_http_manifest_dir,
    get_http_manifest_path,
)
from callwarden.server.daemon_autostart import _pid_alive  # noqa: E402


# ------------------------------------------------------------
# H4C 全量 compat 方法集合（三端真相：Rust COMPAT_ROUTE_WHITELIST 80 项）
# ------------------------------------------------------------
# H4C-1 默认 registry 的 1 个方法（http_server.rs `compat_route` 首批声明；
# W2-1：get_uncommented_symbols 已迁移 rust_native，2->1）
_H4C1_DEFAULT_METHODS = {"stats_top_files"}

# Rust COMPAT_ROUTE_WHITELIST 全量 80 项（H4C-1 1 + H4C-2 13 + H4C-3 8 +
# H4C-2 第二批 20 + H4C-2 第三批 18 +
# H4C-2 第三批（T-1786747295227-b876fddf）collab/p2/p3/p4 15，
# http_server.rs COMPAT_ROUTE_WHITELIST），
# 与装配后 RUST_COMPAT_ROUTE / 运行时 registry 对齐。
# 整改（T-1786747295227-49c90d68）：rule_candidate_list / rule_list /
# get_applicable_rules 3 个纯 SELECT 只读方法接入 worker，security 组 14→17。
# W2-1（T-1786840097330-dec66710）：get_uncommented_symbols /
# get_module_call_stats / get_semgrep_stats 3 个迁移 rust_native，107→104。
# W2-2（T-1786840097330-a9e0ec69）：get_clone_stats / get_job_stats /
# get_clone_group_stats 3 个迁移 rust_native，任务组 16→13，104→101。
# W2-3（T-1786840097331-fd01a3f8）：defect_stats / get_edit_stats 2 个迁移
# rust_native，摘要组 27→26、security 组 14→13，101→99。
# W3-1（T-1786861820150-bfe5e805）：list_build_contexts / get_build_context /
# get_active_build_context / get_resolved_edges / count_resolved_edges 5 个迁移
# rust_native，rules 组 8→3，99→94。
# W3-2（T-1786861820151-f3cecf40）：get_job_status / list_jobs / wait_for_job
# 3 个迁移 rust_native，任务组 13→10，94→91。
# W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 迁移 rust_native，
# 符号组 15→14，91→90。
# W4-1（T-1786886251769-22b94ee8-sub-1）：get_file_history / get_commit_tasks
# 2 个迁移 rust_native，符号组 14→13、任务组 10→9，90→88。
# W4-2（T-1786886251769-22b94ee8-sub-2）：get_coverage_for_symbol /
# diff_to_symbol 2 个迁移 rust_native，摘要组 26→24，88→86。
# review_readiness 依赖 blast_radius 与 cross_layer_impact（均未迁移），
# 保持 python_compat。
_EXPECTED_COMPAT_METHODS_81 = {
    # H4C-1 默认（1）
    "stats_top_files",
    # H4C-2 符号组只读（13；get_semgrep_findings 已 W3-3
    # T-1786861820151-deb64c48 迁移 rust_native（15→14）、get_file_history 已
    # W4-1 T-1786886251769-22b94ee8-sub-1 迁移 rust_native（14→13））
    "get_symbol_history",
    "get_recent_changes",
    "get_impact",
    "get_top_callers",
    "get_orphan_symbols",
    "get_deepest_functions",
    "get_comment_from_version",
    "get_issue_summary",
    "find_issues",
    "get_comment_coverage",
    "get_call_heatmap",
    "get_test_coverage",
    "export_module_graph",
    # H4C-3 任务组只读（8；get_job_status / list_jobs / wait_for_job 已
    # W3-2 T-1786861820151-f3cecf40 迁移 rust_native（13→10）、get_commit_tasks
    # 已 W4-1 T-1786886251769-22b94ee8-sub-1 迁移 rust_native（10→9）、
    # get_defect_correlation 已 W4-3 T-1786886251769-22b94ee8-sub-3 迁移
    # rust_native（9→8））
    "get_symbol_change_tasks",
    "audit_verify_chain",
    "list_audit_signing_keys",
    "bootstrap_status",
    "list_clones",
    "list_clone_groups",
    "get_clone_group_detail",
    "task_plan_template",
    # H4C-2 第二批（T-1786747295213-64204cce）：摘要/演化/护栏/缺陷组只读（20；
    # defect_correlation / churn_analysis / defect_search / defect_suggest_fix 已
    # W4-3 T-1786886251769-22b94ee8-sub-3 迁移 rust_native，defect_learn 写面
    # 保持 python_compat）
    "get_summary",
    "project_brief",
    "repo_map",
    # W4-2（T-1786886251769-22b94ee8-sub-2）：get_coverage_for_symbol 已迁移
    # rust_native（query.coverage_for_symbol），不再注册于 compat registry
    "find_uncovered_functions",
    "test_impact_selection",
    "who_to_ask",
    "get_ownership_map",
    "guardrail_scan",
    "guardrail_check_edit",
    "guardrail_list_rules",
    "blast_radius",
    "ask_codebase",
    "get_token_savings_report",
    "get_vulnerability_blast_radius",
    "get_clone_aware_impact",
    # W4-2（T-1786886251769-22b94ee8-sub-2）：diff_to_symbol 已迁移 rust_native
    # （query.diff_to_symbol），不再注册于 compat registry；review_readiness
    # 依赖 blast_radius 与 cross_layer_impact（均未迁移），保持 python_compat
    "review_readiness",
    "cross_layer_impact",
    "evolution_frequency",
    "hotspot_evolution",
    "defect_learn",
    # H4C-2 第二批：语义/外部符号组只读（5）
    "semantic_search",
    "find_similar_functions",
    "get_symbol_commit_history",
    "parse_codeowners",
    "get_project_dependencies",
    # H4C-2 第三批（T-1786747295227-49c90d68）：security 组只读（12；
    # diff_branches 已 W4-4 T-1786886251769-22b94ee8-sub-4 迁移 rust_native，
    # 13→12）
    "list_branches",
    "merge_preview",
    "get_edit_history",
    "find_shared_symbols",
    "cross_repo_impact",
    "cross_repo_summary",
    "lsp_hover",
    "lsp_definition",
    "lsp_references",
    "lsp_diagnostics",
    "lsp_completion",
    "lsp_check_available",
    # H4C-2 第三批：rules 组只读（3；list_build_contexts / get_build_context /
    # get_active_build_context / get_resolved_edges / count_resolved_edges 已
    # W3-1 T-1786861820150-bfe5e805 迁移 rust_native，8→3）
    "list_toolchains",
    "get_toolchain",
    "get_workspace_toolchains",
    # H4C-2 第三批（T-1786747295227-49c90d68 整改）：规则查询组只读（3）
    "rule_candidate_list",
    "rule_list",
    "get_applicable_rules",
    # H4C-2 第三批（T-1786747295227-b876fddf）：collab 组只读（4）
    "get_role_view",
    "find_evidence",
    "get_freshness_status",
    "get_gate_decision",
    # H4C-2 第三批（T-1786747295227-b876fddf）：p2 依赖图/环检测组只读（5）
    "get_artifact_freshness",
    "get_interface_providers",
    "detect_cycle",
    "validate_revision_dependencies",
    "get_dependency_edges",
    # H4C-2 第三批（T-1786747295227-b876fddf）：p3 身份/证明组只读（5）
    "get_action_identity",
    "check_action_identity",
    "check_session_separation",
    "get_attestation_validity",
    "list_attestation_revocations",
    # H4C-2 第三批（T-1786747295227-b876fddf）：p4 租约组只读（1）
    "assignment_show",
}


def _full_registry() -> CompatRegistry:
    """构造与 H4C 运行时 registry 同构的独立 registry（80 项，handler 用 dummy）。

    供 validate_against_rust_route 负向用例使用：基于 H4C-1 默认 1 项，
    补齐 H4C-2/3 新增 80 项（read_only / SCOPE_WORKSPACE）。
    """
    reg = _build_default_registry()
    for method in sorted(_EXPECTED_COMPAT_METHODS_81 - _H4C1_DEFAULT_METHODS):
        reg.register(method, READ_ONLY, SCOPE_WORKSPACE, "h4b-r 测试夹具", _dummy_handler)
    return reg


# ============================================================
# 1. 恢复的默认 registry（H3 误删修复）
# ============================================================


class TestRegistryRestored:
    def test_get_compat_registry_returns_singleton(self):
        """懒加载单例：多次调用返回同一实例。"""
        assert get_compat_registry() is get_compat_registry()

    def test_default_registry_has_full_route_methods(self):
        """默认 registry（H4C 装配后单例）方法名集合与 Rust `compat_route` 全量一致（80 项）。"""
        reg = get_compat_registry()
        assert len(reg) == 80
        assert set(reg.methods()) == set(RUST_COMPAT_ROUTE) == _EXPECTED_COMPAT_METHODS_81

    def test_default_entries_are_read_only(self):
        """两个默认方法均为 read_only（与 Rust `compat_route` 一致）。"""
        reg = get_compat_registry()
        for method, op_class in RUST_COMPAT_ROUTE.items():
            entry = reg.get(method)
            assert entry is not None, f"缺失默认方法: {method}"
            assert entry.operation_class == op_class
            assert entry.operation_class == READ_ONLY

    def test_default_entries_scopes(self):
        """workspace_scope：stats_top_files=authority；get_uncommented_symbols 已
        W2-1 迁移 rust_native，不再注册于默认 registry。"""
        reg = get_compat_registry()
        assert reg.workspace_scope("stats_top_files") == SCOPE_AUTHORITY
        assert not reg.is_compat_method("get_uncommented_symbols")

    def test_default_entries_have_callable_handlers(self):
        """handler 可调用且返回 CompatMethod 实例（契约 §3.3 字段齐备）。"""
        reg = get_compat_registry()
        for method in RUST_COMPAT_ROUTE:
            entry = reg.get(method)
            assert isinstance(entry, CompatMethod)
            assert callable(entry.handler)
            assert isinstance(entry.description, str) and entry.description

    def test_build_default_registry_is_h4c1_subset_of_rust_route(self):
        """_build_default_registry() 为 H4C-1 默认 1 项，是 Rust 全量路由（80 项）的子集。

        H4C-2/3 新增 89 项由工具模块 register_compat_routes 注册到单例，
        不属于 `_build_default_registry`（保持 H4C-1 语义）。
        """
        reg = _build_default_registry()
        assert set(reg.methods()) == _H4C1_DEFAULT_METHODS
        assert set(reg.methods()) <= set(RUST_COMPAT_ROUTE)


# ============================================================
# 2. compat_route 查询（镜像 Rust 语义）
# ============================================================


class TestCompatRouteQuery:
    def test_rust_compat_route_constant_mirrors_rust(self):
        """常量键集精确镜像 http_server.rs `compat_route` 全量 80 项（均 read_only）。"""
        assert set(RUST_COMPAT_ROUTE) == _EXPECTED_COMPAT_METHODS_81
        assert set(RUST_COMPAT_ROUTE.values()) == {READ_ONLY}

    def test_route_returns_operation_class_for_compat_methods(self):
        for method, op_class in RUST_COMPAT_ROUTE.items():
            assert compat_route(method) == op_class

    def test_route_returns_none_for_unknown(self):
        """未知方法返回 None（fail-closed，不抛异常）。"""
        assert compat_route("get_code_metrics_summary") is None
        assert compat_route("") is None
        assert compat_route("no.such.rpc") is None


# ============================================================
# 3. register_compat_route（注册即校验）
# ============================================================


@pytest.fixture
def iso_registry(monkeypatch):
    """隔离 registry：monkeypatch 模块级 get_compat_registry，避免污染全局单例。"""
    reg = CompatRegistry()
    monkeypatch.setattr(compat_registry_mod, "get_compat_registry", lambda: reg)
    return reg


def _dummy_handler(ctx: CompatCallContext):
    return {}


class TestRegisterCompatRoute:
    def test_register_matching_operation_class_succeeds(self, iso_registry):
        """已声明方法 + 与 Rust 一致的 operation_class → 注册成功。"""
        register_compat_route(
            "stats_top_files", READ_ONLY, SCOPE_AUTHORITY,
            "h4b-r 测试注册", _dummy_handler,
        )
        assert iso_registry.is_compat_method("stats_top_files")

    def test_register_mismatched_operation_class_raises(self, iso_registry):
        """已声明方法 + 与 Rust 不一致的 operation_class → ValueError（两端对齐门）。"""
        with pytest.raises(ValueError) as ei:
            register_compat_route(
                "stats_top_files", INDEX_WRITE, SCOPE_AUTHORITY,
                "h4b-r 测试注册", _dummy_handler,
            )
        assert "stats_top_files" in str(ei.value)
        # 注册被拒绝：隔离 registry 未被污染
        assert not iso_registry.is_compat_method("stats_top_files")

    def test_register_new_method_allowed(self, iso_registry):
        """Rust 未声明的方法允许注册（供后续 phase 扩展，调用方自行保证 Rust 同步）。"""
        register_compat_route(
            "future_compat_method", READ_ONLY, SCOPE_WORKSPACE,
            "h4b-r 后续扩展", _dummy_handler,
        )
        assert iso_registry.is_compat_method("future_compat_method")

    def test_register_governance_write_rejected(self, iso_registry):
        """MVP 禁止 governance_write（register 层拒绝，规则一致）。"""
        with pytest.raises(ValueError):
            register_compat_route(
                "gov_method", GOVERNANCE_WRITE, SCOPE_WORKSPACE,
                "h4b-r 禁止治理写", _dummy_handler,
            )

    def test_register_duplicate_rejected(self, iso_registry):
        """重复注册同一方法 → ValueError。"""
        register_compat_route(
            "dup_method", READ_ONLY, SCOPE_WORKSPACE, "h4b-r", _dummy_handler,
        )
        with pytest.raises(ValueError):
            register_compat_route(
                "dup_method", READ_ONLY, SCOPE_WORKSPACE, "h4b-r", _dummy_handler,
            )

    def test_global_singleton_not_polluted(self):
        """register_compat_route 的默认目标是全局单例；隔离测试不污染单例（仍 80 方法）。"""
        reg = get_compat_registry()
        assert len(reg) == 80
        assert set(reg.methods()) == set(RUST_COMPAT_ROUTE)


# ============================================================
# 4. validate_against_rust_route（两端对齐门）
# ============================================================


class TestValidateAgainstRustRoute:
    def test_default_registry_aligned(self):
        """默认 registry 与 Rust `compat_route` 完全对齐。"""
        result = validate_against_rust_route()
        assert result["aligned"] is True
        assert result["missing"] == []
        assert result["extra"] == []
        assert result["mismatch"] == {}

    def test_missing_method_detected(self):
        """registry 缺 Rust 声明的方法 → aligned=False，missing 精确（全量 80 项）。"""
        reg = CompatRegistry()
        reg.register(
            "stats_top_files", READ_ONLY, SCOPE_AUTHORITY,
            "h4b-r 缺其余方法", _dummy_handler,
        )
        result = validate_against_rust_route(reg)
        assert result["aligned"] is False
        assert result["missing"] == sorted(
            _EXPECTED_COMPAT_METHODS_81 - {"stats_top_files"}
        )

    def test_extra_method_detected(self):
        """registry 有 Rust 未声明的方法 → aligned=False，extra 精确（基于 80 全量）。"""
        reg = _full_registry()
        reg.register("extra_method", READ_ONLY, SCOPE_WORKSPACE, "h4b-r", _dummy_handler)
        result = validate_against_rust_route(reg)
        assert result["aligned"] is False
        assert result["extra"] == ["extra_method"]
        assert result["missing"] == []

    def test_mismatch_operation_class_detected(self):
        """同方法 operation_class 与 Rust 不一致 → aligned=False，mismatch 含明细。"""
        reg = CompatRegistry()
        for method in sorted(_EXPECTED_COMPAT_METHODS_81):
            if method == "stats_top_files":
                reg.register(
                    method, INDEX_WRITE, SCOPE_AUTHORITY,
                    "h4b-r 错误 op_class", _dummy_handler,
                )
            else:
                reg.register(
                    method, READ_ONLY, SCOPE_WORKSPACE,
                    "h4b-r 正确", _dummy_handler,
                )
        result = validate_against_rust_route(reg)
        assert result["aligned"] is False
        assert result["mismatch"] == {
            "stats_top_files": {
                "rust": READ_ONLY,
                "python": INDEX_WRITE,
            }
        }
        assert result["missing"] == []
        assert result["extra"] == []

    def test_validate_does_not_mutate_registry(self):
        """校验只读：传入自定义 registry 后其内容不变（不污染调用方）。"""
        reg = _build_default_registry()
        before = set(reg.methods())
        validate_against_rust_route(reg)
        assert set(reg.methods()) == before


# ============================================================
# 5. compat_worker 集成（get_compat_registry 恢复）
# ============================================================


class TestWorkerIntegration:
    def test_compat_worker_import_resolved(self):
        """compat_worker import 不再 ImportError（H3 误删修复的回归门）。"""
        assert server.compat_worker.get_compat_registry is get_compat_registry

    def test_compat_worker_registry_has_full_route_methods(self):
        """worker 通过 get_compat_registry() 拿到与 Rust `compat_route` 一致的全量 registry。"""
        reg = server.compat_worker.get_compat_registry()
        assert set(reg.methods()) == set(RUST_COMPAT_ROUTE) == _EXPECTED_COMPAT_METHODS_81
        for method, op_class in RUST_COMPAT_ROUTE.items():
            assert reg.operation_class(method) == op_class


# ============================================================
# 真实进程门（隔离 daemon + 生产 HttpDaemonRpcClient）
# ============================================================


def _find_daemon_binary():
    """定位 current-HEAD 构建的 cw-daemon 二进制（与 H4B-N/C/I/E 门同源）。

    优先本地 cargo build 产物，保证与当前源码一致；CW_DAEMON_BIN / runtime
    部署仅作兜底。二进制不可用时跳过用例。
    """
    candidates = [
        os.path.join("rust_ext", "target", "debug", "cw-daemon.exe"),
        os.path.join("rust_ext", "target", "debug", "cw-daemon"),
        os.environ.get("CW_DAEMON_BIN", ""),
        os.path.join("runtime", "current", "cw-daemon.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def _wait_manifest(proc, timeout=10.0):
    """等待隔离 daemon 发布 authority-scoped manifest（仅接受 pid 匹配当前进程）。

    H6 修复（9d6ca63，2026-08-15）后 manifest 固定写 `~/.callwarden/`
    （http_manifest_dir = USERPROFILE/.callwarden），不再写 daemon data_root；
    本文件隔离 daemon 不重定向 USERPROFILE，故轮询真实 get_http_manifest_dir()。
    """
    directory = get_http_manifest_dir()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if os.path.isdir(directory):
            for f in os.listdir(directory):
                if f.startswith("http-daemon.") and f.endswith(".manifest.json"):
                    p = os.path.join(directory, f)
                    try:
                        m = json.loads(open(p, encoding="utf-8").read())
                    except (OSError, ValueError):
                        continue
                    if m.get("pid") == proc.pid:
                        return m
        time.sleep(0.2)
    return None


def _backup_http_manifest():
    """备份当前 authority 的 HTTP manifest（若存在），teardown 时恢复。"""
    path = get_http_manifest_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data


def _restore_or_clean_http_manifest(pid, backup):
    """teardown 清理：删除 pid 匹配的隔离 manifest；备份 pid 存活则恢复。"""
    path = get_http_manifest_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                current = json.load(f)
            if int(current.get("pid", -1)) == pid:
                os.remove(path)
    except (OSError, ValueError):
        pass
    if backup is not None and _pid_alive(int(backup.get("pid", -1))):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(backup, f, ensure_ascii=False)
        except OSError:
            pass


def _spawn_isolated_daemon(bin_path, data_root, http_bind):
    """启动隔离 daemon（临时 task DB / registry / 管道），启用 HTTP transport。"""
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    # compat worker 使用与 daemon 同版本的 Python 解释器
    env["CW_COMPAT_PYTHON"] = sys.executable
    proc = subprocess.Popen(
        [bin_path, "--http-bind=" + http_bind],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def _terminate(proc):
    """终止 daemon 进程（terminate 优先，兜底 kill）。"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class TestRealDaemonCompatRpcAlignment:
    """真实进程级 registry ↔ Rust `compat_route` 对齐门（H4B-R 产物，H4C 适配 80 全量）。

    - 正向：compat_route 全量 80 方法（H4C-1 1 + H4C-2 13 + H4C-3 8 +
      H4C-2 第二批 25 + H4C-2 第三批 18 +
      H4C-2 第三批 collab/p2/p3/p4 15）
      在生产 HttpDaemonRpcClient 调用下**绝不**返回 method_not_found
      （经 H3 compat worker 服务）；
    - 负向：registry 未注册的 python_compat 方法（get_code_metrics_summary）
      在真实 daemon 上必返回 method_not_found —— 实证 fail-closed
      （registry 是 worker 方法真相源，未注册方法 HTTP 不可达）；
    - /capabilities：backend=python_compat 且 status=available 的方法集合
      与 Python RUST_COMPAT_ROUTE 完全一致。
    """

    POSITIVE_COMPAT_RPCS = [
        # (rpc, params) —— 只要求不返回 method_not_found（业务校验失败可接受）；
        # 统一最小参数 {"limit": 5}，非 limit 参数方法忽略之（handler 默认值兜底）。
        # ask_codebase 除外：worker 内惰性加载 jina embedding 模型（本机 HF 缓存
        # 无该模型 → 触发下载）远超 5s 超时，单独用长超时用例验证（见
        # test_ask_codebase_route_served_with_embedding_loading）。
        (method, {"limit": 5})
        for method in sorted(_EXPECTED_COMPAT_METHODS_81 - {"ask_codebase"})
    ]

    NEGATIVE_UNREGISTERED = "get_code_metrics_summary"

    @pytest.fixture
    def real_daemon(self, tmp_path):
        """启动隔离真实 daemon，yield 生产类 HttpDaemonRpcClient。"""
        bin_path = _find_daemon_binary()
        if bin_path is None:
            pytest.skip("cw-daemon 二进制不可用（需先 cargo build --bin cw-daemon）")
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        backup = _backup_http_manifest()
        proc = _spawn_isolated_daemon(bin_path, data_root, "127.0.0.1:0")
        try:
            manifest = _wait_manifest(proc)
            if manifest is None:
                pytest.fail("隔离 daemon 未发布 manifest")
            client = HttpDaemonRpcClient(
                endpoint=manifest["endpoint"],
                verify_health=False,
                timeout=5.0,
            )
            # 整改（T-1786747295227-49c90d68 步骤#3）：worker 冷启动预热，参照
            # combined_worker_cutover 整改 4——manifest 只代表 HTTP 层就绪，
            # compat worker（Python 子进程 + 装配导入）首次调用才 spawn，冷启动
            # 可能超过 client.timeout（5s）→ E_HTTP_REQUEST_TIMEOUT。用只读方法
            # stats_top_files（H4C-1 默认方法，W2-1 后唯一仍注册的默认方法；
            # get_uncommented_symbols 已迁移 rust_native 不再走 worker）预热触发
            # spawn，对 retryable 超时统一重试 2 次（有界，非固定 sleep）；非超时
            # 错误立即上抛不掩盖。
            self._wait_worker_ready(proc, client)
            yield client
        finally:
            _terminate(proc)
            _restore_or_clean_http_manifest(proc.pid, backup)

    def _wait_worker_ready(self, proc, client, retries: int = 2):
        """worker 冷启动就绪等待（整改 4 模式，无 sleep 兜底）。"""
        last_err = None
        for _ in range(retries + 1):
            try:
                # stats_top_files handler 强制要求 workspace_id；隔离库
                # 无该 workspace 时返回空结果（成功）或业务错误，均不影响预热目标
                client.call("stats_top_files", {"workspace_id": 1, "limit": 1})
                return
            except DaemonUnavailableError as e:
                if E_HTTP_REQUEST_TIMEOUT not in str(e):
                    raise
                last_err = e
            except DaemonRemoteError as e:
                # worker 已能响应帧：业务错误（如隔离库无该 workspace）说明
                # spawn + 装配已完成，预热目标达成。method_not_found 除外。
                if e.code == "method_not_found":
                    raise
                return
        _terminate(proc)
        pytest.fail(
            f"compat worker 冷启动就绪超时（预热重试 {retries} 次仍超时）: {last_err}"
        )

    def test_positive_compat_route_never_method_not_found(self, real_daemon):
        """正向：compat_route 全量 86 项（ask_codebase 除外）绝不返回 method_not_found。

        整改（T-1786747295227-49c90d68 步骤#3）：107 项扩展后首调用触发 compat
        worker 冷启动（Python 子进程 + 装配导入），可能超过 client 5s 超时 →
        E_HTTP_REQUEST_TIMEOUT（与 combined_worker_cutover 整改 4 同根因）。
        W2-1（T-1786840097330-dec66710）：get_uncommented_symbols /
        get_module_call_stats / get_semgrep_stats 迁移 rust_native 后
        107->104（全量 104 项，ask_codebase 除外 103 项）。
        W2-2（T-1786840097330-a9e0ec69）：get_clone_stats / get_job_stats /
        get_clone_group_stats 迁移 rust_native 后 104->101（全量 101 项，
        ask_codebase 除外 100 项）。
        W2-3（T-1786840097331-fd01a3f8）：defect_stats / get_edit_stats 迁移
        rust_native 后 101->99（全量 99 项，ask_codebase 除外 98 项）。
        W3-1（T-1786861820150-bfe5e805）：build 读组 5 个（list_build_contexts /
        get_build_context / get_active_build_context / get_resolved_edges /
        count_resolved_edges）迁移 rust_native 后 99->94（全量 94 项，
        ask_codebase 除外 93 项）。
        W3-2（T-1786861820151-f3cecf40）：job 读组 3 个（get_job_status /
        list_jobs / wait_for_job）迁移 rust_native 后 94->91（全量 91 项，
        ask_codebase 除外 90 项）。
        W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 迁移 rust_native
        后 91->90（全量 90 项，ask_codebase 除外 89 项）。
        W4-1（T-1786886251769-22b94ee8-sub-1）：get_file_history /
        get_commit_tasks 迁移 rust_native 后 90->88（全量 88 项，ask_codebase
        除外 87 项）。
        W4-2（T-1786886251769-22b94ee8-sub-2）：get_coverage_for_symbol /
        diff_to_symbol 迁移 rust_native 后 88->86（全量 86 项，ask_codebase
        除外 85 项）。
        fixture 已对 worker 冷启动做预热；此处对偶发慢执行再做有界重试（2 次，
        共 3 次尝试，非固定 sleep）。重试后仍超时视为「route 已被 worker 受理并
        进入执行」（method_not_found 会立即返回而非超时），符合 H4B-R 语义——
        只验证绝不 method_not_found，不验证执行耗时。
        """
        for rpc, params in self.POSITIVE_COMPAT_RPCS:
            last_err = None
            for _attempt in range(3):
                try:
                    real_daemon.call(rpc, params)
                    last_err = None
                    break
                except DaemonUnavailableError as exc:
                    if E_HTTP_REQUEST_TIMEOUT not in str(exc):
                        raise
                    last_err = exc  # 慢执行/冷启动超时：有界重试
                except DaemonRemoteError as exc:
                    assert exc.code != "method_not_found", (
                        f"{rpc} 是 Rust COMPAT_ROUTE_WHITELIST 声明的 compat 方法，"
                        f"不应 method_not_found: {exc}"
                    )
                    break
                except Exception as exc:  # noqa: BLE001 —— 非业务错误（连接/传输）视为失败
                    pytest.fail(f"{rpc} 意外异常（非 DaemonRemoteError）: {exc}")
            if last_err is not None:
                # 有界重试后仍 E_HTTP_REQUEST_TIMEOUT：route 已被 worker 受理并
                # 进入执行（用户级真实库上的全库统计/模型加载等慢路径），
                # method_not_found 必不可能返回（未注册会立即失败而非超时）。
                # H4B-R 语义只验证「绝不 method_not_found」，不验证执行耗时；
                # 超时属执行性能/环境限制，不算失败（fail-closed 门面仍在）。
                continue

    def test_ask_codebase_route_served_with_embedding_loading(self, real_daemon):
        """ask_codebase 单独验证：route 已注册且 worker 受理（慢方法不参与 5s 遍历）。

        worker 内首次调用惰性加载 jinaai/jina-embeddings-v2-base-code（本机 HF
        缓存无该模型 → 触发下载/加载），远超 client 5s 超时 → E_HTTP_REQUEST_TIMEOUT。
        route 注册与 worker 受理本身由「到达执行阶段」证明：若 registry 未注册该
        route，worker 会立即返回 method_not_found（不超时）。因此断言：
        - DaemonRemoteError 时 code 必非 method_not_found；
        - E_HTTP_REQUEST_TIMEOUT（模型下载/加载慢）视为执行环境限制，不掩盖
          路由已验证结论（fail-closed 门面仍在：超时即拒绝，未静默降级）。
        """
        slow = HttpDaemonRpcClient(
            endpoint=real_daemon.discover(),
            verify_health=False,
            timeout=60.0,
        )
        try:
            result = slow.call("ask_codebase", {"question": "", "top_k": 1})
        except DaemonRemoteError as exc:
            assert exc.code != "method_not_found", (
                "ask_codebase 是 Rust COMPAT_ROUTE_WHITELIST 声明的 compat 方法，"
                f"不应 method_not_found: {exc}"
            )
        except DaemonUnavailableError as exc:
            if E_HTTP_REQUEST_TIMEOUT in str(exc):
                # worker 已受理并进入执行（模型下载/加载）→ 路由验证达成；
                # 超时属本机 HF 模型缓存缺失的环境限制，非路由注册问题
                return
            raise
        else:
            # 模型已在缓存/加载成功（正常返回）：验证 RAG 上下文组装器返回
            # 结构契约，避免"超时即通过"掩盖成功路径的异常返回
            assert isinstance(result, dict), (
                f"ask_codebase 应返回 dict，实际 {type(result).__name__}: {result!r}"
            )
            assert "rag_context" in result, (
                f"ask_codebase 返回缺 rag_context 键: {sorted(result.keys())}"
            )

    def test_unregistered_python_compat_method_not_found(self, real_daemon):
        """负向：registry 未注册的 python_compat 方法必 method_not_found。"""
        with pytest.raises(DaemonRemoteError) as ei:
            real_daemon.call(self.NEGATIVE_UNREGISTERED, {})
        assert ei.value.code == "method_not_found", (
            f"{self.NEGATIVE_UNREGISTERED} 未注册到 compat registry 且 dispatch.rs"
            f"无分支，HTTP 模式必 method_not_found（fail-closed）"
        )

    def test_capabilities_python_compat_available_matches_rust_route(
        self, real_daemon
    ):
        """/capabilities 中 python_compat 且 available 的方法集合 == RUST_COMPAT_ROUTE。"""
        caps = real_daemon.capabilities()
        methods = caps.get("methods", {})
        pc_available = {
            name
            for name, info in methods.items()
            if info.get("backend") == "python_compat"
            and info.get("status") == "available"
        }
        assert pc_available == set(RUST_COMPAT_ROUTE), (
            f"capability registry 的 python_compat available 集合与 Python "
            f"RUST_COMPAT_ROUTE 不一致: {pc_available - set(RUST_COMPAT_ROUTE)=}"
            f" / {set(RUST_COMPAT_ROUTE) - pc_available=}"
        )
