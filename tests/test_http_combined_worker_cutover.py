"""H4C-2+3 合并：符号+任务 read-only 工具接入 worker 的真实进程门测试（收敛版）。

维护任务 T-1788871227327-45c94bd8（2026-09-08 pytest 测试债清理）把本文件
对齐到 Rust-authority 现状。当前稳定契约（已由本文件单向断言锁定）：
- **python_compat 层已全量退役**（P0-COMPAT-v3 系列卡
  T-1788963104058-fdb2e848 / T-1788963104879-2e9e6270 / T-1788963106520-907544c8
  把剩余组全部迁 rust_native 并清零白名单）：运行时 compat registry = 0、
  RUST_COMPAT_ROUTE 静态声明 = 0、http_server.rs COMPAT_ROUTE_WHITELIST = 0。
  全部 239 工具经 rust_native handler 服务；历史分组常量（SYMBOL_METHODS /
  TASK_METHODS / ...）仅作"不应再注册"的负向断言输入保留。
- 对齐门语义相应收敛为"空表恒等"：registry == 静态表 == 白名单 == 空集，
  validate_against_rust_route 必须 aligned（无 missing/extra/mismatch）。
- 真实进程门 TestRealDaemonCombinedWorkerCutover：隔离 daemon + 生产
  HttpDaemonRpcClient，经 workspace.register + snapshot.publish 建立
  workspace authority 后，覆盖 rust_native 只读方法正向/负向（compat worker
  冷启动预热已随 worker 退役删除）。
- 工具函数层逐函数 HTTP 路由 / 写语义 fail-closed 断言已由维护期同源文件
  接管：test_http_native_read_cutover.py（tools_query native 直读 / 本地保持）、
  test_http_compat_worker_batch.py（route_worker_call 白名单语义 + 真实 daemon
  worker batch）、test_http_unsupported_error_cutover.py（collab/p2/p3/p4 只读
  路由 + 写语义 fail-closed）。本文件不再维护已退役的 route_worker_call mock
  层断言（T03 收敛后工具函数体已薄壳化 route_rpc，无裸 get_db 绕过面）。

归类依据：http-daemon-mvp-compatibility-contract.md §3.3 worker 契约。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 装配导入：import compat_worker 即触发其内部对 tools_query / tools_task 的
# 装配 import，工具模块模块级 register_compat_routes 随之注册到 registry 单例。
import server.compat_worker as _compat_worker_asm  # noqa: E402,F401
from server import compat_registry as reg  # noqa: E402
from callwarden.server.daemon_client import (  # noqa: E402
    DaemonRemoteError,
    DaemonUnavailableError,
    E_HTTP_REQUEST_TIMEOUT,
    HttpDaemonRpcClient,
)

# 注册计数口径（T-1788871227327-45c94bd8 triage 收敛；P0-COMPAT-v3 终态更新）：
# python_compat 层全量退役后三张声明表全部清零——
#   runtime registry = 0 = http_server.rs COMPAT_ROUTE_WHITELIST =
#   RUST_COMPAT_ROUTE 静态声明（空表恒等，validate_against_rust_route aligned）。
# 历史"漂移记账"清单（NATIVE_MIGRATED_RESIDUAL / RUST_WHITELIST_DEAD_ENTRIES）
# 相应收敛：残留声明已随静态表清零一并清理，仅作负向断言输入保留。
EXPECTED_TOTAL = 0
EXPECTED_STATIC_TOTAL = 0
EXPECTED_RUST_WHITELIST_TOTAL = 0

# 历史 native 迁移残留清单（P0-COMPAT-v3 前的 11 项声明漂移）。
# 静态表清零后这些方法在 registry / 静态表 / Rust 白名单均无声明，
# python 侧 compat_route() 返回 None（fail-closed）——本清单用于断言
# "曾残留的方法不得回潮"。
NATIVE_MIGRATED_RESIDUAL = [
    "check_action_identity",
    "check_session_separation",
    "detect_cycle",
    "find_evidence",
    "get_action_identity",
    "get_artifact_freshness",
    "get_dependency_edges",
    "get_freshness_status",
    "get_gate_decision",
    "get_interface_providers",
    "validate_revision_dependencies",
]

# 历史白名单死条目（MCP-002/003/004），随白名单清零一并移除；
# 保留作负向断言输入（不得回潮）。
RUST_WHITELIST_DEAD_ENTRIES = [
    "find_evidence",
    "get_freshness_status",
    "get_gate_decision",
]

# 完全移出 python_compat 声明/注册的方法（registry / 静态表 / Rust 白名单均无）。
REMOVED_UNREGISTERED = [
    # W2-1/W4-x 迁出符号组：get_uncommented_symbols 等已由 W2-1 系列记账，下列为
    # 本文件历史组中迁出的符号组方法（不再出现在任何声明表）。
    "get_top_callers",
    "get_orphan_symbols",
    "get_deepest_functions",
    "get_comment_coverage",
    "get_call_heatmap",
    # W4-2：get_coverage_for_symbol / diff_to_symbol；W4-3：defect_correlation /
    # churn_analysis / defect_search / defect_suggest_fix 等迁出摘要组。
    "find_uncovered_functions",
    # W3-1：rules 组 build_context/toolchain 相关全部迁出。
    "list_toolchains",
    "get_toolchain",
    "get_workspace_toolchains",
    # MCP-001：collab get_role_view 迁出并已从静态表移除。
    "get_role_view",
]

# 符号组（tools_query 模块级 register_compat_routes 注册）= 8（W2-1..W4-x 迁出
# get_top_callers / get_orphan_symbols / get_deepest_functions /
# get_comment_coverage / get_call_heatmap，见 REMOVED_UNREGISTERED）。
SYMBOL_METHODS = [
    "get_symbol_history",
    "get_recent_changes",
    "get_impact",
    "get_comment_from_version",
    "get_issue_summary",
    "find_issues",
    "get_test_coverage",
    "export_module_graph",
]

# 任务组（tools_task 模块级 register_compat_routes 注册）= 8（W3-2/W4-3 迁出
# get_job_status/list_jobs/wait_for_job/get_defect_correlation 后仍注册的方法）。
TASK_METHODS = [
    "get_symbol_change_tasks",
    "audit_verify_chain",
    "list_audit_signing_keys",
    "bootstrap_status",
    "list_clones",
    "list_clone_groups",
    "get_clone_group_detail",
    "task_plan_template",
]

# 摘要/演化/护栏/缺陷组（tools_summary 注册）= 19（W2-3..W4-3 迁出 defect_stats /
# get_coverage_for_symbol / diff_to_symbol / defect_correlation / churn_analysis /
# defect_search / defect_suggest_fix / find_uncovered_functions，见
# REMOVED_UNREGISTERED 与上方记账；defect_learn 写面保留 python_compat）。
SUMMARY_METHODS = [
    "get_summary",
    "project_brief",
    "repo_map",
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
    "review_readiness",
    "cross_layer_impact",
    "evolution_frequency",
    "hotspot_evolution",
    "defect_learn",
]

# 语义/外部符号组（tools_semantic 注册）= 5（未再迁移，仍全部注册）。
SEMANTIC_METHODS = [
    "semantic_search",
    "find_similar_functions",
    "get_symbol_commit_history",
    "parse_codeowners",
    "get_project_dependencies",
]

# security 组（tools_security 注册）= 15（W2-3 get_edit_stats、W4-4 diff_branches
# 迁出后仍注册的分支/编辑历史/跨仓库/LSP/规则查询组只读方法）。
SECURITY_METHODS = [
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
    "rule_candidate_list",
    "rule_list",
    "get_applicable_rules",
]

# rules 组已全部迁出 python_compat（W3-1 起 list_toolchains / get_toolchain /
# get_workspace_toolchains 不再注册、不再声明，见 REMOVED_UNREGISTERED）。
RULES_METHODS = [
    "list_toolchains",
    "get_toolchain",
    "get_workspace_toolchains",
]

# collab 组已全部迁出 python_compat：get_role_view（MCP-001）已从静态表移除；
# find_evidence / get_freshness_status / get_gate_decision（MCP-002/3/4）仍残留在
# 静态表与 Rust 白名单（声明漂移死条目，见 NATIVE_MIGRATED_RESIDUAL /
# RUST_WHITELIST_DEAD_ENTRIES），native-first handler 先命中、运行时无害。
COLLAB_METHODS = [
    "get_role_view",
    "find_evidence",
    "get_freshness_status",
    "get_gate_decision",
]

# p2 依赖图/环检测组已全部迁出 python_compat（5 个方法不再注册；仍残留在静态表
# → NATIVE_MIGRATED_RESIDUAL 记账，compat_route()/is_compat_method 均 False）。
P2_METHODS = [
    "get_artifact_freshness",
    "get_interface_providers",
    "detect_cycle",
    "validate_revision_dependencies",
    "get_dependency_edges",
]

# p3 身份/证明组（tools_p3_identity 注册）= 2（get_action_identity /
# check_action_identity / check_session_separation 已迁 native，残留于静态表）。
P3_METHODS = [
    "get_attestation_validity",
    "list_attestation_revocations",
]

# p4 租约组（tools_p4_lease 注册）= 1（assignment_show 仍注册）。
P4_METHODS = [
    "assignment_show",
]

# 写语义工具（用户决策 Q3：不接入，保持 _http_unsupported fail-closed）+
# governance_write 任务工具（task_create/next/report/apply/close 等）。
# 均不得出现在 registry / RUST_COMPAT_ROUTE。
# H4C-4（T-1786745594007-b2d57524）：补全 13 个写语义工具（含 restore_comment /
# rotate_audit_signing_key / clear_clones / cancel_job / embed_symbols_async）。
WRITE_SEMANTICS_METHODS = [
    "run_semgrep_scan",
    "scan_semgrep_incremental",
    "detect_clones",
    "detect_clones_async",
    "semgrep_scan_async",
    "rule_seed_bootstrap",
    "cleanup_agent_rule_sync_log",
    "restore_comment",
    "restore_all_comments",
    "rotate_audit_signing_key",
    "clear_clones",
    "cancel_job",
    "embed_symbols_async",
    "task_create",
    "task_next",
    "task_report",
    "task_apply",
    "task_close",
    # H4C-2 第二批（T-1786747295213-64204cce）：summary/semantic 写语义/治理工具
    # （generate_summary 矩阵标注 read_only 属异常，实为写语义，不接入 worker）。
    "generate_summary",
    "import_coverage",
    "guardrail_add_rule",
    "record_token_savings",
    "embed_symbols",
    "embed_single_symbol",
    "import_codeowners",
    "import_git_blame",
    "import_project_dependencies",
    "prune_external_symbols",
    "gc_retention",
    "gc_policy_get",
    "gc_policy_set",
    "gc_archive_list",
    "gc_archive_inspect",
    "gc_audit_list",
    "gc_audit_get",
    "gc_archive_import",
    # H4C-2 第三批（T-1786747295227-49c90d68）：security 组写语义/治理工具
    # （register_branch/switch_branch 有 set_active_workspace 写副作用；
    # propose_*/revert_edit 编辑审计；run_check_gate/resolve_gate_findings 门槛；
    # rule_*/extract_* 规则管理；detect_cross_repo_deps 写依赖图）。
    "register_branch",
    "switch_branch",
    "propose_edit",
    "propose_range_patch",
    "propose_symbol_patch",
    "propose_symbol_id_patch",
    "revert_edit",
    "detect_cross_repo_deps",
    "run_check_gate",
    "resolve_gate_findings",
    "rule_candidate_create",
    "rule_candidate_accept",
    "rule_candidate_reject",
    "rule_sync_agents_md",
    "rule_insert_agents_md_block",
    "extract_rule_candidates_from_quality_findings",
    # high-risk 未接入（依赖 Rust 内存 GraphStore，db 层无 SQL 等价实现，
    # worker 只读 SQLite 连接无法承载，维持 fail-closed）。
    "diff_callers",
    "diff_callees",
    "compare_snapshots",
    # rules 组 get_metrics 不依赖 db.conn（走 daemon RPC / 本地 MetricsCollector），
    # 不接入 worker，维持 fail-closed。
    "get_metrics",
    # H4C-2 第三批（T-1786747295227-b876fddf）：collab/p2/p3/p4 写语义/治理工具
    # （submit_verdict/append_evidence 写 gate/evidence；p2 5 项写依赖图与接口
    # 选择；p3 record/register 写身份证明；p4 assignment_create/revoke 写任务
    # 分配。worker 只读连接无法承载，维持 fail-closed；lease_* 5 项为
    # rust_native 不经 python compat registry，天然不在本清单）。
    "submit_verdict",
    "append_evidence",
    "import_envelope_dependencies",
    "record_artifact_identity",
    "publish_interface",
    "select_interface_provider",
    "build_hard_dependency_edges",
    "record_action_identity",
    "register_attestation_revocation",
    "assignment_create",
    "assignment_revoke",
]


# ============================================================
# 1. 单元层：装配导入后的 registry 状态
# ============================================================


class TestCombinedRegistry:
    """装配导入后的 registry 状态 + 两端声明漂移单向对齐门（Rust-authority 收敛）。

    维护任务 T-1788871227327-45c94bd8 triage 结论：源码侧三张声明表（registry /
    http_server.rs COMPAT_ROUTE_WHITELIST / RUST_COMPAT_ROUTE 静态）已漂移且
    在 FORBIDDEN 边界内不可收敛；按用户决策把严格相等对齐门改为"运行时有意义"的
    单向正确性断言（runtime 注册面 ⊆ 白名单 ∩ 静态表、op 全程一致），漂移残留
    由 NATIVE_MIGRATED_RESIDUAL / RUST_WHITELIST_DEAD_ENTRIES 精确记账。
    """

    def test_total_registered(self):
        registry = reg.get_compat_registry()
        # P0-COMPAT-v3 终态：python_compat 层全量退役，registry 必须为空
        assert len(registry) == EXPECTED_TOTAL, (
            f"registry 方法数应 = {EXPECTED_TOTAL}（compat 层已全量退役），"
            f"实际 {len(registry)}——若非零说明有方法回潮 compat 注册，须核查"
        )
        # 空表恒等：registry == RUST_COMPAT_ROUTE 静态声明 == 空集
        assert set(registry.methods()) == set(reg.RUST_COMPAT_ROUTE) == set(), (
            "compat 退役后 registry 与静态声明表均应为空集"
        )

    def _assert_group_unregistered(self, group_methods, group_name):
        """P0-COMPAT-v3 终态：该组方法已全部迁 rust_native，python 侧 fail-closed。"""
        registry = reg.get_compat_registry()
        for m in group_methods:
            assert not registry.is_compat_method(m), f"{group_name}工具 {m} 不应再注册"
            assert registry.operation_class(m) is None
            assert reg.compat_route(m) is None

    def test_symbol_group_unregistered(self):
        assert len(SYMBOL_METHODS) == 8
        self._assert_group_unregistered(SYMBOL_METHODS, "符号")

    def test_task_group_unregistered(self):
        assert len(TASK_METHODS) == 8
        self._assert_group_unregistered(TASK_METHODS, "任务")

    def test_summary_group_unregistered(self):
        assert len(SUMMARY_METHODS) == 19
        self._assert_group_unregistered(SUMMARY_METHODS, "摘要")

    def test_semantic_group_unregistered(self):
        assert len(SEMANTIC_METHODS) == 5
        self._assert_group_unregistered(SEMANTIC_METHODS, "语义")

    def test_security_group_unregistered(self):
        assert len(SECURITY_METHODS) == 15
        self._assert_group_unregistered(SECURITY_METHODS, "security")

    def test_p3_group_unregistered(self):
        assert len(P3_METHODS) == 2
        self._assert_group_unregistered(P3_METHODS, "p3")

    def test_p4_group_unregistered(self):
        assert len(P4_METHODS) == 1
        self._assert_group_unregistered(P4_METHODS, "p4")

    def test_rules_collab_p2_groups_not_registered(self):
        """rules / collab / p2 三组已全部迁出 python_compat：registry 与路由均不可见。"""
        registry = reg.get_compat_registry()
        for m in RULES_METHODS + COLLAB_METHODS + P2_METHODS:
            assert not registry.is_compat_method(m), f"{m} 不应再注册"
            assert registry.operation_class(m) is None
            assert reg.compat_route(m) is None

    def test_validate_against_rust_route_reports_declaration_drift(self):
        """两端对齐门（P0-COMPAT-v3 终态）：三表清零后必须完全 aligned——
        无 missing（历史 11 项残留声明已清理）、无 extra、无 mismatch；
        任何非空项即声明表回潮，须核查迁移回归。
        """
        assert len(reg.RUST_COMPAT_ROUTE) == EXPECTED_STATIC_TOTAL, (
            f"RUST_COMPAT_ROUTE 静态声明应 = {EXPECTED_STATIC_TOTAL}（已清零），"
            f"实际 {len(reg.RUST_COMPAT_ROUTE)}"
        )
        result = reg.validate_against_rust_route()
        assert result["extra"] == [], f"runtime 注册面超出静态声明: {result}"
        assert result["mismatch"] == {}, f"operation_class 不一致: {result}"
        assert result["missing"] == [], f"静态声明残留未清理: {result}"
        assert result["aligned"] is True, result

    def test_rust_whitelist_source_sync_one_way(self):
        """单向对齐门（P0-COMPAT-v3 终态）：http_server.rs COMPAT_ROUTE_WHITELIST
        已清零（T-1788963104058-fdb2e848），与 Python 静态表/registry 空表恒等。
        若白名单非空，多出项必须精确等于历史死条目之外的声明——当前契约下
        任何非空白名单条目都视为声明回潮，直接失败。
        """
        rs_path = _REPO_ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs"
        text = rs_path.read_text(encoding="utf-8")
        m = re.search(
            r"const COMPAT_ROUTE_WHITELIST:\s*&\[\(&str, &str\)\]\s*=\s*&\[(.*?)\];",
            text,
            re.S,
        )
        assert m is not None, "http_server.rs 找不到 COMPAT_ROUTE_WHITELIST 常量"
        rust_map = dict(
            re.findall(r'\(\s*"([a-z0-9_.]+)"\s*,\s*"([a-z_]+)"\s*\)', m.group(1))
        )
        assert len(rust_map) == EXPECTED_RUST_WHITELIST_TOTAL, (
            f"Rust 白名单应含 {EXPECTED_RUST_WHITELIST_TOTAL} 个方法（已清零），"
            f"实际 {len(rust_map)}: {sorted(rust_map)}"
        )
        registry = reg.get_compat_registry()
        registered = set(registry.methods())
        # 空表恒等：runtime 注册面 == 白名单 == 静态表 == 空集
        assert registered == set(rust_map) == set(reg.RUST_COMPAT_ROUTE) == set()

    def test_native_migrated_residuals_are_inert(self):
        """历史 11 项残留（P0-COMPAT-v3 后）：声明已随静态表清零清理，
        python 侧 fail-closed——不得以任何形式回潮（注册/声明/路由）。
        """
        registry = reg.get_compat_registry()
        for m in NATIVE_MIGRATED_RESIDUAL:
            assert m not in reg.RUST_COMPAT_ROUTE, f"残留 {m} 不得回潮静态声明表"
            assert not registry.is_compat_method(m), f"native 已迁移方法 {m} 不应注册"
            assert reg.compat_route(m) is None, f"{m} python 侧路由应为 None"

    def test_fully_removed_unregistered(self):
        """完全移出 python_compat（registry / 静态表 均无）的方法 fail-closed。"""
        registry = reg.get_compat_registry()
        for m in REMOVED_UNREGISTERED:
            assert not registry.is_compat_method(m), f"{m} 不应注册"
            assert m not in reg.RUST_COMPAT_ROUTE, f"{m} 不应残留在静态表"
            assert reg.compat_route(m) is None, f"{m} python 侧路由应为 None"

    def test_write_semantics_fail_closed(self):
        """写语义 + governance_write 工具不接入：registry 与路由均不可见。"""
        registry = reg.get_compat_registry()
        for m in WRITE_SEMANTICS_METHODS:
            assert not registry.is_compat_method(m), f"写语义工具 {m} 不应注册"
            assert reg.compat_route(m) is None, f"写语义工具 {m} 不应有 compat 路由"

    def test_legacy_h4c1_default_methods_native_migrated(self):
        # INT-001 起默认 registry 为空：stats_top_files 迁 rust_native；
        # get_uncommented_symbols 已 W2-1 迁 rust_native。均不再由 python 侧
        # 注册/声明，兼容 `_build_default_registry` 空默认语义。
        registry = reg.get_compat_registry()
        for m in ("stats_top_files", "get_uncommented_symbols"):
            assert not registry.is_compat_method(m), f"{m} 不应注册"
            assert m not in reg.RUST_COMPAT_ROUTE, f"{m} 不应残留在静态表"
            assert reg.compat_route(m) is None


# ============================================================
# 2. 真实进程门：隔离 daemon + 生产 HttpDaemonRpcClient
# ============================================================

# 覆盖符号组（get_recent_changes，T-1788871227327-45c94bd8 起）与任务组
# （task_plan_template）正向路径所需的表：workspaces（_bind_readonly_db 解析
# workspace_root）+ file_instances + file_versions（符号组正向 get_recent_changes
# 的 minimal 空表：无行 → changed_files/changed_functions 空列表）
# + symbols（security 组 list_branches JOIN 子查询需表存在）
# + semgrep_findings/jobs（历史保留种子）
# + agent_rule_candidates/agent_rules（security 组规则查询只读种子）。
COMBINED_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    root_path TEXT DEFAULT '',
    is_active INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS file_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    status TEXT DEFAULT 'pending'
);
-- T-1788871227327-45c94bd8：符号组正向改用 get_recent_changes（读
-- file_versions JOIN file_instances），minimal 空表（无行 → 空列表）。
CREATE TABLE IF NOT EXISTS file_versions (
    -- 列集对齐 get_recent_changes native 查询面（fv.content_hash/mtime/
    -- total_lines/is_deleted/commit_hash，T-1788871227327-45c94bd8）。
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    version_num INTEGER DEFAULT 1,
    content_hash TEXT DEFAULT '',
    mtime REAL DEFAULT 0,
    total_lines INTEGER DEFAULT 0,
    parsed_at REAL DEFAULT 0,
    is_current INTEGER DEFAULT 1,
    is_deleted INTEGER DEFAULT 0,
    commit_hash TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS semgrep_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    content_hash TEXT DEFAULT '',
    rule_id TEXT NOT NULL,
    rule_name TEXT DEFAULT '',
    message TEXT DEFAULT '',
    severity TEXT DEFAULT 'INFO',
    confidence TEXT DEFAULT 'UNKNOWN',
    language TEXT DEFAULT '',
    start_line INTEGER DEFAULT 0,
    end_line INTEGER DEFAULT 0,
    snippet TEXT DEFAULT '',
    fix TEXT DEFAULT '',
    symbol_id INTEGER DEFAULT 0,
    symbol_qualified TEXT DEFAULT '',
    scanned_at REAL DEFAULT 0,
    scan_id INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    workspace_id INTEGER NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    progress REAL DEFAULT 0.0,
    message TEXT DEFAULT '',
    params TEXT DEFAULT '{}',
    result_summary TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    cancel_requested INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL DEFAULT 0,
    finished_at REAL DEFAULT 0
);
-- security 组 list_branches：list_branch_workspaces 子查询 JOIN symbols（需表存在）
CREATE TABLE IF NOT EXISTS symbols (
    -- 列集镜像 rust_ext/src/daemon/cas_merge.rs 生产 symbols 表
    -- （T-1788871227327-45c94bd8：snapshot.publish build_and_publish 的
    -- symbols 预置查询需要 module_path/start_line/end_line/depth 等，
    -- 旧 worker 时代精简列集不再满足 native 权威路径）。
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    visibility TEXT DEFAULT 'private',
    start_line INTEGER NOT NULL DEFAULT 0,
    end_line INTEGER NOT NULL DEFAULT 0,
    start_col INTEGER DEFAULT 0,
    end_col INTEGER DEFAULT 0,
    signature TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    comment_status TEXT DEFAULT 'pending',
    module_path TEXT DEFAULT '',
    qualified_name TEXT DEFAULT '',
    depth INTEGER DEFAULT -1
);
-- snapshot.publish build_and_publish 的 calls 预置查询需要
-- （列集镜像 rust_ext/src/daemon/cas_merge.rs 生产 calls 表的查询面）。
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id INTEGER NOT NULL,
    callee_id INTEGER NOT NULL,
    callee_name TEXT DEFAULT '',
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0
);
-- security 组规则查询（整改 T-1786747295227-49c90d68）：rule_candidate_list 查
-- agent_rule_candidates、rule_list / get_applicable_rules 查 agent_rules。
-- 全局表（无 workspace_id），表结构镜像 db/schema.py。
CREATE TABLE IF NOT EXISTS agent_rule_candidates (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    scope_json TEXT DEFAULT '{}',
    severity TEXT DEFAULT 'info',
    source TEXT DEFAULT 'manual',
    evidence_json TEXT DEFAULT '{}',
    confidence REAL DEFAULT 0.0,
    status TEXT DEFAULT 'pending',
    created_at REAL NOT NULL,
    reviewed_at REAL,
    reviewer TEXT DEFAULT '',
    linked_rule_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agent_rules (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    scope_json TEXT DEFAULT '{}',
    severity TEXT DEFAULT 'info',
    status TEXT DEFAULT 'active',
    source_candidate_id TEXT DEFAULT '',
    evidence_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    synced_to_agents_md INTEGER DEFAULT 0,
    sync_hash TEXT DEFAULT ''
);
"""


def _seed_combined_db(home_dir: Path, root_path: str) -> str:
    """在隔离 USERPROFILE 下建 worker 可读的种子库，返回 db 路径。

    - workspaces 注册 workspace_id=1（root_path 指向隔离临时目录）；
    - file_instances 1 条（workspace 1, src/app.py）；
    - file_versions 空表（T-1788871227327-45c94bd8 起符号组正向
      get_recent_changes 无行 → 返回空变更列表）；
    - semgrep_findings / jobs 种子（历史保留，非当前正向断言依赖）；
    - symbols 空表（security 组 list_branches JOIN 子查询需表存在）；
    - agent_rule_candidates / agent_rules 各 1 条（security 组规则查询正向）。
    """
    db_file = home_dir / ".callwarden" / "callwarden.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript(COMBINED_SCHEMA)
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, is_active) VALUES (1, 'seed-repo', ?, 1)",
            (root_path,),
        )
        cur = conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, status) VALUES (1, 'src/app.py', 'parsed')"
        )
        fi_id = cur.lastrowid
        conn.execute(
            """INSERT INTO semgrep_findings
               (file_instance_id, content_hash, rule_id, rule_name, message,
                severity, confidence, language, start_line, end_line,
                snippet, fix, symbol_id, symbol_qualified, scanned_at, scan_id)
               VALUES (?, '', 'rule-no-else-return', 'no-else-return',
                       'simplify if-else', 'ERROR', 'HIGH', 'python',
                       10, 12, '', '', 0, 'app.run', 0, 0)""",
            (fi_id,),
        )
        conn.execute(
            """INSERT INTO jobs
               (job_id, workspace_id, job_type, status, progress, message,
                params, result_summary, error, cancel_requested, created_at,
                started_at, finished_at)
               VALUES ('J-seed-1', 1, 'clone_detect', 'completed', 1.0, 'done',
                       '{}', '{}', '', 0, 1000.0, 1000.0, 2000.0)"""
        )
        # security 组规则查询种子（整改 T-1786747295227-49c90d68）：
        # 1 条 pending 候选（rule_candidate_list 正向）+ 1 条 active 空 scope 全局
        # 规则（rule_list / get_applicable_rules 正向，空 scope 匹配任意上下文）。
        conn.execute(
            """INSERT INTO agent_rule_candidates
               (id, title, rule_text, scope_json, severity, source, evidence_json,
                confidence, status, created_at, reviewed_at, reviewer, linked_rule_id)
               VALUES ('ARC-seed-1', 'seed candidate', 'do not use bare except',
                       '{}', 'warning', 'manual', '{}', 0.8, 'pending',
                       1000.0, NULL, '', '')"""
        )
        conn.execute(
            """INSERT INTO agent_rules
               (id, title, rule_text, scope_json, severity, status,
                source_candidate_id, evidence_json, created_at, updated_at,
                synced_to_agents_md, sync_hash)
               VALUES ('AR-seed-1', 'seed rule', 'use explicit error handling',
                       '{}', 'info', 'active', 'ARC-seed-1', '{}',
                       1000.0, 1000.0, 0, '')"""
        )
        conn.commit()
    finally:
        conn.close()
    return str(db_file)


def _find_daemon_binary():
    """定位 current-HEAD 构建的 cw-daemon 二进制（与 H4C-1 真实进程门同源）。"""
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


def _wait_manifest(data_root, proc, timeout=10.0):
    """等待隔离 daemon 发布 authority-scoped manifest（仅接受 pid 匹配当前进程）。

    H6 修复（9d6ca63，2026-08-15）后 manifest 固定写 `USERPROFILE/.callwarden/`
    （http_manifest_dir），隔离 daemon 的 USERPROFILE = data_root/userhome，
    故轮询 data_root/userhome/.callwarden；data_root 根目录不再有 manifest。
    """
    manifest_dir = os.path.join(data_root, "userhome", ".callwarden")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if os.path.isdir(manifest_dir):
            for f in os.listdir(manifest_dir):
                if f.startswith("http-daemon.") and f.endswith(".manifest.json"):
                    p = os.path.join(manifest_dir, f)
                    try:
                        m = json.loads(open(p, encoding="utf-8").read())
                    except (OSError, ValueError):
                        continue
                    if m.get("pid") == proc.pid:
                        return m
        time.sleep(0.2)
    return None


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


def _spawn_isolated_daemon(bin_path, data_root, http_bind):
    """启动隔离 daemon（临时 task DB / registry / 管道 / USERPROFILE）。"""
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    # compat worker 使用与 daemon 同版本的 Python 解释器
    env["CW_COMPAT_PYTHON"] = sys.executable
    home_dir = Path(data_root) / "userhome"
    home_dir.mkdir(parents=True, exist_ok=True)
    # H6：manifest 固定写 USERPROFILE/.callwarden，须先建目录否则 daemon 发布失败
    (home_dir / ".callwarden").mkdir(parents=True, exist_ok=True)
    env["USERPROFILE"] = str(home_dir)
    proc = subprocess.Popen(
        [bin_path, "--http-bind=" + http_bind],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


class TestRealDaemonCombinedWorkerCutover:
    """真实进程门：隔离 daemon + 生产 HttpDaemonRpcClient 覆盖合并接入。

    覆盖（派发单步骤#3）：
    - 符号组正向：get_recent_changes 经 worker 返回种子库空变更数据
      （T-1788871227327-45c94bd8：get_top_callers 已 S2 迁移 rust_native
      不再走 worker，minimal 种子库无符号版本数据（file_versions 空表），
      正向改用仍走 worker 的 get_recent_changes 验证符号组 worker 路由：
      空表 → changed_files/changed_functions 空列表）；
    - 任务组正向：task_plan_template 经 worker 返回模板字符串
      （W2-2 T-1786840097330-a9e0ec69：get_job_stats 已迁移 rust_native，
      正向改用仍走 worker 的 list_jobs；
      W3-2 T-1786861820151-f3cecf40：list_jobs 已迁移 rust_native，
      正向改用仍走 worker 的 task_plan_template）；
    - 负向：未知方法 → method_not_found 结构化错误（绝不泄漏为成功）。
    任一断言失败即整体失败；不得 skip。
    """

    @pytest.fixture
    def daemon_bin(self):
        bin_path = _find_daemon_binary()
        if bin_path is None:
            pytest.fail(
                "cw-daemon 二进制不可用（H4C-2+3 真实进程门不得 skip："
                "需先 cargo build --manifest-path rust_ext/Cargo.toml --bin cw-daemon）"
            )
        return bin_path

    def _spawn_with_client(self, daemon_bin, tmp_path):
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        root_path = str(tmp_path / "seed-repo")
        # workspace.register 校验 client_view_root 真实存在（path_not_found 门），
        # 旧 worker 直读 DB 不需要该目录存在；native authority 路径必须建出。
        os.makedirs(root_path, exist_ok=True)
        _seed_combined_db(Path(data_root) / "userhome", root_path)
        proc = _spawn_isolated_daemon(daemon_bin, data_root, "127.0.0.1:0")
        manifest = _wait_manifest(data_root, proc)
        if manifest is None:
            _terminate(proc)
            pytest.fail("隔离 daemon 未发布 manifest")
        client = HttpDaemonRpcClient(
            endpoint=manifest["endpoint"],
            verify_health=False,
            timeout=5.0,
        )
        # T-1788871227327-45c94bd8（P0-COMPAT-v3 终态）：compat worker 已退役，
        # 只读方法经 rust_native handler 服务，且要求 workspace authority——
        # 先 workspace.register 建立实例，再把种子库以 snapshot.publish 发布为
        # 查询快照；后续调用全部携带 workspace_instance_id。
        # （历史整改 4 的 worker 冷启动预热随 worker 退役一并删除。）
        try:
            ws = client.call("workspace.register", {"client_view_root": root_path})
            inst = ws["workspace_instance_id"]
            client.call("snapshot.publish", {
                "workspace_instance_id": inst,
                "build_context_hash": "combined-worker-cutover-seed",
                "db_path": str(Path(data_root) / "userhome" / ".callwarden" / "callwarden.db"),
            })
        except Exception as e:
            _terminate(proc)
            pytest.fail(f"workspace.register/snapshot.publish 失败: {type(e).__name__}: {e}")
        return proc, client, inst

    def test_symbol_group_worker_positive(self, daemon_bin, tmp_path):
        """符号组正向：get_recent_changes 返回种子库空变更数据。

        T-1788871227327-45c94bd8（P0-COMPAT-v3 终态）：compat worker 已退役，
        get_recent_changes 由 rust_native handler 经 snapshot 查询服务。
        minimal 种子库无符号版本数据（file_versions 空表），本用例验证
        符号组正向路径：native 路由成功返回空变更 dict（changed_files /
        changed_functions 空列表、含 since_seconds），非 method_not_found、
        非业务错误。
        """
        proc, client, inst = self._spawn_with_client(daemon_bin, tmp_path)
        try:
            result = client.call(
                "get_recent_changes",
                {"workspace_id": 1, "workspace_instance_id": inst,
                 "deadline_ms": 10000},
            )
            assert isinstance(result, dict), (
                f"get_recent_changes 应返回 dict，实际 {type(result)}: {result!r}"
            )
            assert result.get("changed_files") == [], (
                f"符号组正向应经 worker 返回种子库空变更列表: {result}"
            )
            assert result.get("changed_functions") == []
            assert "since_seconds" in result
        finally:
            _terminate(proc)

    def test_task_group_worker_positive(self, daemon_bin, tmp_path):
        """任务组正向：task_plan_template 返回计划模板字符串。

        W2-2（T-1786840097330-a9e0ec69）：get_job_stats 已迁移 rust_native，
        任务组正向改用仍走 worker 的 list_jobs；
        W3-2（T-1786861820151-f3cecf40）：list_jobs 已迁移 rust_native，
        任务组正向改用仍走 worker 的 task_plan_template（纯模板返回、
        无表依赖，worker 端 _h_task_plan_template 必然可服务）。
        P0-COMPAT-v3 终态：worker 已退役，task_plan_template 由 rust_native
        handler 服务（行为契约不变，路由面更新）。
        """
        proc, client, inst = self._spawn_with_client(daemon_bin, tmp_path)
        try:
            result = client.call(
                "task_plan_template",
                {"workspace_id": 1, "workspace_instance_id": inst,
                 "deadline_ms": 10000},
            )
            assert isinstance(result, str) and "Root task title" in result, (
                f"任务组正向应返回计划模板字符串: {result!r}"
            )
        finally:
            _terminate(proc)

    def test_unknown_method_negative_method_not_found(self, daemon_bin, tmp_path):
        """负向：未知方法 → method_not_found 结构化错误（fail-closed，不泄漏成功）。"""
        proc, client, inst = self._spawn_with_client(daemon_bin, tmp_path)
        try:
            with pytest.raises(DaemonRemoteError) as ei:
                client.call("no.such.tool",
                            {"workspace_id": 1, "workspace_instance_id": inst})
            assert ei.value.code == "method_not_found", (
                f"未知方法应返回 method_not_found，实际 {ei.value.code}: {ei.value.message}"
            )
        finally:
            _terminate(proc)

    def test_security_rules_group_worker_positive(self, daemon_bin, tmp_path):
        """H4C-2 第三批：security/rules 组正向返回种子库真实数据。

        - list_branches → list_branch_workspaces（workspaces 表 + symbols 子查询）；
        - rule_candidate_list / rule_list / get_applicable_rules（整改
          T-1786747295227-49c90d68）：db_agent_rules 纯 SELECT 正向返回种子库
          候选/规则真实数据。
        （T-1788871227327-45c94bd8：list_toolchains 已 S2 迁移 rust_native，
        原 toolchains 断言块删除；P0-COMPAT-v3 终态：以上方法全部经
        rust_native + snapshot 查询服务，调用携带 workspace_instance_id。）
        """
        proc, client, inst = self._spawn_with_client(daemon_bin, tmp_path)
        try:
            branches = client.call(
                "list_branches",
                {"workspace_id": 1, "workspace_instance_id": inst,
                 "deadline_ms": 10000},
            )
            assert branches is not None
            assert len(branches) == 1, f"security 组正向应返回种子库 workspaces: {branches}"
            assert branches[0]["name"] == "seed-repo"
            assert branches[0]["is_active"] == 1
            assert branches[0]["symbol_count"] == 0

            # 整改（T-1786747295227-49c90d68）：3 个规则查询只读方法正向
            cands = client.call(
                "rule_candidate_list",
                {"workspace_id": 1, "workspace_instance_id": inst,
                 "status": "pending", "limit": 50, "deadline_ms": 10000},
            )
            assert cands is not None
            assert cands["count"] == 1, f"rule_candidate_list 应返回种子库候选: {cands}"
            assert cands["candidates"][0]["id"] == "ARC-seed-1"

            rules = client.call(
                "rule_list",
                {"workspace_id": 1, "workspace_instance_id": inst,
                 "status": "active", "limit": 100, "deadline_ms": 10000},
            )
            assert rules is not None
            assert rules["count"] == 1, f"rule_list 应返回种子库规则: {rules}"
            assert rules["rules"][0]["id"] == "AR-seed-1"

            applicable = client.call(
                "get_applicable_rules",
                {"workspace_id": 1, "workspace_instance_id": inst,
                 "context": {"language": "python"}, "limit": 10, "deadline_ms": 10000},
            )
            assert applicable is not None
            assert applicable["count"] == 1, (
                f"get_applicable_rules 空 scope 全局规则应匹配任意上下文: {applicable}"
            )
            assert applicable["rules"][0]["id"] == "AR-seed-1"
            assert applicable["rules"][0]["matched_scope"] == ["global"]
        finally:
            _terminate(proc)


# ============================================================
# 3. 工具层路由 / 写语义 fail-closed（维护期退役段，已由同源文件接管）
# ============================================================
# T-1788871227327-45c94bd8：route_worker_call mock 层断言与
# E_HTTP_COMPAT_UNSUPPORTED 结构 dict 契约已退役（T03 收敛后工具函数体薄壳化
# route_rpc，写语义不再返回该结构）。对应验证由维护期同源文件接管：
# test_http_native_read_cutover.py / test_http_compat_worker_batch.py /
# test_http_unsupported_error_cutover.py，见模块 docstring。
