"""compatibility registry——声明兼容方法的 operation_class 与实现映射。

契约：docs/design/http-daemon-mvp-compatibility-contract.md §3.3 / §5
- 每个兼容方法必须声明 read_only / index_write / governance_write 之一；
- MVP 禁止 governance_write（worker 收到该 operation_class 直接拒绝）；
- backend=python_compat 的方法由 daemon 内部 adapter 路由到本 registry，
  worker 不接受 MCP/CLI 直接连接；
- worker 只能使用 daemon 注入的显式 context（workspace_id 等），
  不得查询/选择 active workspace，不得接受客户端传入 db_path。

H4B-R 扩展（compat_route 对齐）：
- 本文件是 worker 侧方法真相源；Rust 侧 rust_ext/src/daemon/http_server.rs
  的 `compat_route` 是路由入口，二者必须保持一致（见 RUST_COMPAT_ROUTE /
  register_compat_route / validate_against_rust_route）。
- 模块级 compat_route(method) 镜像 Rust 路由语义（返回 operation_class）；
  register_compat_route 注册即校验；validate_against_rust_route 提供两端对齐门。

H4C-1 扩展（批量 read_only 注册）：
- CompatRegistry.register_read_only_batch 支持一次注册一批 read_only 方法
  （符号/任务组白名单）；模块级 register_compat_routes 在批量注册的同时同步
  RUST_COMPAT_ROUTE（声明 Rust 侧 compat_route 白名单已实现），使两端对齐门
  覆盖批量注册；未知方法 compat_route() 返回 None 的 fail-closed 语义不变。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

# operation_class 常量（契约 §3.3）
READ_ONLY = "read_only"
INDEX_WRITE = "index_write"
GOVERNANCE_WRITE = "governance_write"
OPERATION_CLASSES = (READ_ONLY, INDEX_WRITE, GOVERNANCE_WRITE)

# workspace_scope 常量（契约 §5 capability registry 字段）
SCOPE_NONE = "none"
SCOPE_WORKSPACE = "workspace"
SCOPE_SNAPSHOT = "snapshot"
SCOPE_AUTHORITY = "authority"


@dataclass(frozen=True)
class CompatCallContext:
    """一次兼容调用的显式上下文（由 daemon 注入，worker 不得自行扩展）。

    - db_path 由 worker 通过 authority 配置解析（get_project_db_path），
      客户端或 frame 传入的路径一律拒绝；
    - workspace_id 是用户级数据库中的整数工作区 id，用于 SQL 过滤；
    - workspace_instance_id 是字符串实例标识，用于 traceability。
    """

    request_id: str
    method: str
    params: Dict[str, Any]
    workspace_instance_id: str
    workspace_id: Optional[int]
    operation_class: str
    deadline: Optional[float]  # epoch ms（daemon 端负责强制超时）
    db_path: str  # 由 authority 配置解析的用户级数据库路径
    conn: Optional[sqlite3.Connection] = field(default=None, repr=False)  # read_only 方法专用只读连接


@dataclass(frozen=True)
class CompatMethod:
    """兼容方法注册条目。"""

    method: str
    operation_class: str
    workspace_scope: str
    description: str
    handler: Callable[[CompatCallContext], Any]


class CompatRegistry:
    """可扩展的兼容方法注册表（fail-closed：未知方法返回 None）。"""

    def __init__(self) -> None:
        self._methods: Dict[str, CompatMethod] = {}

    def register(
        self,
        method: str,
        operation_class: str,
        workspace_scope: str,
        description: str,
        handler: Callable[[CompatCallContext], Any],
    ) -> None:
        if not method or not isinstance(method, str):
            raise ValueError("method 必须是非空字符串")
        if operation_class not in OPERATION_CLASSES:
            raise ValueError(f"非法 operation_class: {operation_class!r}（可选 {OPERATION_CLASSES}）")
        if operation_class == GOVERNANCE_WRITE:
            raise ValueError("MVP 禁止 governance_write 兼容方法")
        if method in self._methods:
            raise ValueError(f"重复注册兼容方法: {method}")
        self._methods[method] = CompatMethod(
            method=method,
            operation_class=operation_class,
            workspace_scope=workspace_scope,
            description=description,
            handler=handler,
        )

    def register_read_only_batch(
        self,
        methods: Dict[str, Callable[[CompatCallContext], Any]],
        workspace_scope: str,
        description: str,
    ) -> None:
        """批量注册 read_only 兼容方法（H4C-1：符号/任务组 read_only 白名单）。

        所有方法统一 operation_class=read_only；逐个复用 `register` 的完整校验
        （非法 scope、governance_write、重复方法均拒绝），保证与单方法注册
        的 fail-closed 约束一致。
        """
        if not methods:
            raise ValueError("methods 不能为空")
        if workspace_scope not in (
            SCOPE_WORKSPACE,
            SCOPE_SNAPSHOT,
            SCOPE_AUTHORITY,
        ):
            raise ValueError(
                f"非法 workspace_scope: {workspace_scope!r}"
                f"（可选 {SCOPE_WORKSPACE}/{SCOPE_SNAPSHOT}/{SCOPE_AUTHORITY}）"
            )
        for method, handler in methods.items():
            self.register(method, READ_ONLY, workspace_scope, description, handler)

    def get(self, method: str) -> Optional[CompatMethod]:
        return self._methods.get(method)

    def is_compat_method(self, method: str) -> bool:
        return method in self._methods

    def operation_class(self, method: str) -> Optional[str]:
        entry = self._methods.get(method)
        return entry.operation_class if entry else None

    def workspace_scope(self, method: str) -> Optional[str]:
        entry = self._methods.get(method)
        return entry.workspace_scope if entry else None

    def methods(self) -> Dict[str, CompatMethod]:
        return dict(self._methods)

    def __len__(self) -> int:
        return len(self._methods)


# ---------------------------------------------------------------
# 默认方法实现（全部 read_only；写方法需由后续 phase 显式注册）
# ---------------------------------------------------------------


def _coerce_limit(value: Any) -> int:
    """limit 参数归一化：必须为 1..500 的整数，否则抛 ValueError（→ E_COMPAT_EXECUTION_ERROR）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"limit 必须是整数: {value!r}")
    if not (1 <= n <= 500):
        raise ValueError("limit 必须在 1..500 之间")
    return n


def _stats_top_files(ctx: CompatCallContext) -> Dict[str, Any]:
    """返回符号数 Top N 文件及注释覆盖（authority 范围：按注入 workspace_id 过滤）。"""
    if ctx.conn is None:
        raise RuntimeError("read_only 方法缺少只读连接")
    if ctx.workspace_id is None:
        raise ValueError("缺少 workspace_id")
    limit = _coerce_limit(ctx.params.get("limit", 10))
    rows = ctx.conn.execute(
        """
        SELECT fi.rel_path,
               COUNT(s.id) AS symbol_count,
               COALESCE(SUM(s.has_comment), 0) AS commented_count
        FROM symbols s
        JOIN file_instances fi ON fi.id = s.file_instance_id
        WHERE fi.workspace_id = ?1 AND fi.status != 'archived'
        GROUP BY fi.id, fi.rel_path
        ORDER BY symbol_count DESC
        LIMIT ?2
        """,
        (ctx.workspace_id, limit),
    ).fetchall()
    files = []
    for r in rows:
        symbol_count = r[1]
        commented_count = r[2]
        files.append(
            {
                "rel_path": r[0],
                "symbol_count": symbol_count,
                "commented_count": commented_count,
                "comment_coverage": round(commented_count / symbol_count, 4) if symbol_count else 0.0,
            }
        )
    return {"count": len(files), "files": files}


# ---------------------------------------------------------------
# 默认 registry（恢复 H3 误删的注册器；与 Rust `compat_route` 保持一致）
# ---------------------------------------------------------------


def _build_default_registry() -> CompatRegistry:
    """构造默认兼容 registry（与 daemon capability registry 的 python_compat 行保持一致）。

    当前注册的 1 个方法即 Rust 侧 http_server.rs `compat_route` 声明的 H4C-1
    python_compat 路由（stats_top_files，read_only；get_uncommented_symbols 已
    W2-1 迁移 rust_native，T-1786840097330-dec66710）。
    """
    reg = CompatRegistry()
    reg.register(
        "stats_top_files",
        READ_ONLY,
        SCOPE_AUTHORITY,
        "返回符号数 Top N 文件及注释覆盖统计",
        _stats_top_files,
    )
    return reg


_DEFAULT_REGISTRY: Optional[CompatRegistry] = None


def get_compat_registry() -> CompatRegistry:
    """返回进程级默认兼容 registry（懒加载单例）。"""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = _build_default_registry()
    return _DEFAULT_REGISTRY


# ---------------------------------------------------------------
# H4B-R：compat_route 注册/查询/校验 API
# ---------------------------------------------------------------

# Rust 侧 http_server.rs `compat_route()` 的硬编码顶层方法名 → operation_class
# 映射。该映射是 HTTP 路由入口的唯一真相；Python 默认 registry 必须与其对齐。
# 本表由 `deliverables/software-company/tool_migration_matrix.json` 中
# target_backend=python_compat 的 79 个工具生成（T01 收敛架构，scripts/
# gen_route_matrix.py 维护）；`register_compat_routes` 的批量注册会确认这些
# 方法（幂等），两端对齐门 validate_against_rust_route 覆盖全部 79 项。
RUST_COMPAT_ROUTE: Dict[str, str] = {
    # 内部 worker 方法（非 MCP 工具，不在 239 矩阵内；H4C-1 默认注册）
    "stats_top_files": READ_ONLY,
    "ask_codebase": READ_ONLY,
    "assignment_show": READ_ONLY,
    "audit_verify_chain": READ_ONLY,
    "blast_radius": READ_ONLY,
    "bootstrap_status": READ_ONLY,
    "check_action_identity": READ_ONLY,
    "check_session_separation": READ_ONLY,
    "cross_layer_impact": READ_ONLY,
    "cross_repo_impact": READ_ONLY,
    "cross_repo_summary": READ_ONLY,
    "defect_learn": READ_ONLY,
    "detect_cycle": READ_ONLY,
    "evolution_frequency": READ_ONLY,
    "export_module_graph": READ_ONLY,
    "find_evidence": READ_ONLY,
    "find_issues": READ_ONLY,
    "find_shared_symbols": READ_ONLY,
    "find_similar_functions": READ_ONLY,
    "get_action_identity": READ_ONLY,
    "get_applicable_rules": READ_ONLY,
    "get_artifact_freshness": READ_ONLY,
    "get_attestation_validity": READ_ONLY,
    "get_clone_aware_impact": READ_ONLY,
    "get_clone_group_detail": READ_ONLY,
    "get_comment_from_version": READ_ONLY,
    "get_dependency_edges": READ_ONLY,
    "get_edit_history": READ_ONLY,
    "get_freshness_status": READ_ONLY,
    "get_gate_decision": READ_ONLY,
    "get_impact": READ_ONLY,
    "get_interface_providers": READ_ONLY,
    "get_issue_summary": READ_ONLY,
    "get_ownership_map": READ_ONLY,
    "get_project_dependencies": READ_ONLY,
    "get_recent_changes": READ_ONLY,
    # MCP-001（T-1787321708699-da5d8224）：get_role_view 迁移 rust_native，移除 compat 注册
    "get_summary": READ_ONLY,
    "get_symbol_change_tasks": READ_ONLY,
    "get_symbol_commit_history": READ_ONLY,
    "get_symbol_history": READ_ONLY,
    "get_test_coverage": READ_ONLY,
    "get_token_savings_report": READ_ONLY,
    "get_vulnerability_blast_radius": READ_ONLY,
    "guardrail_check_edit": READ_ONLY,
    "guardrail_list_rules": READ_ONLY,
    "guardrail_scan": READ_ONLY,
    "hotspot_evolution": READ_ONLY,
    "list_attestation_revocations": READ_ONLY,
    "list_audit_signing_keys": READ_ONLY,
    "list_branches": READ_ONLY,
    "list_clone_groups": READ_ONLY,
    "list_clones": READ_ONLY,
    "lsp_check_available": READ_ONLY,
    "lsp_completion": READ_ONLY,
    "lsp_definition": READ_ONLY,
    "lsp_diagnostics": READ_ONLY,
    "lsp_hover": READ_ONLY,
    "lsp_references": READ_ONLY,
    "merge_preview": READ_ONLY,
    "parse_codeowners": READ_ONLY,
    "project_brief": READ_ONLY,
    "repo_map": READ_ONLY,
    "review_readiness": READ_ONLY,
    "rule_candidate_list": READ_ONLY,
    "rule_list": READ_ONLY,
    "semantic_search": READ_ONLY,
    "task_plan_template": READ_ONLY,
    "test_impact_selection": READ_ONLY,
    "validate_revision_dependencies": READ_ONLY,
    "who_to_ask": READ_ONLY,
}


def compat_route(method: str) -> Optional[str]:
    """镜像 Rust `compat_route` 语义：返回 method 的 operation_class，非 compat 返回 None。

    查询基于 Python 默认 registry（worker 侧方法真相源），与 Rust 侧路由保持一致；
    未知方法返回 None（fail-closed，不抛异常）。
    """
    entry = get_compat_registry().get(method)
    return entry.operation_class if entry else None


def register_compat_route(
    method: str,
    operation_class: str,
    workspace_scope: str,
    description: str,
    handler: Callable[[CompatCallContext], Any],
) -> None:
    """向默认 registry 注册 compat_route 方法，注册即校验与 Rust 映射一致。

    若 method 已声明于 RUST_COMPAT_ROUTE，operation_class 必须与之相同，
    否则抛 ValueError（两端对齐门）；未声明的方法允许注册（供后续 phase
    扩展），但调用方需自行保证 Rust 侧 http_server.rs `compat_route` 同步。
    """
    expected = RUST_COMPAT_ROUTE.get(method)
    if expected is not None and operation_class != expected:
        raise ValueError(
            f"compat_route 与 Rust 映射不一致: {method} 期望 {expected!r}，实际 {operation_class!r}"
        )
    get_compat_registry().register(
        method,
        operation_class,
        workspace_scope,
        description,
        handler,
    )


def register_compat_routes(
    methods: Dict[str, Callable[[CompatCallContext], Any]],
    workspace_scope: str,
    description: str,
) -> None:
    """批量注册 read_only 兼容方法并同步 Rust 白名单声明（H4C-1 基建）。

    与单方法 `register_compat_route` 的分工：
    - 单方法注册声明于 `RUST_COMPAT_ROUTE` 的方法时校验 operation_class 一致；
    - 本批量入口面向符号/任务组 read_only 白名单：一次注册一批 read_only
      方法，并自动把方法名同步到 `RUST_COMPAT_ROUTE`（声明 Rust 侧
      http_server.rs `compat_route` 白名单已实现），使
      `validate_against_rust_route` 两端对齐门覆盖批量注册；
    - 调用方（H4C-2/3 工具层接入）仍必须同步 Rust 侧 http_server.rs
      `compat_route` 白名单与 `build_capability_registry` python_compat 行；
    - `compat_route()` 对未知方法返回 None 的 fail-closed 语义不变。
    """
    get_compat_registry().register_read_only_batch(methods, workspace_scope, description)
    for method in methods:
        RUST_COMPAT_ROUTE[method] = READ_ONLY


def validate_against_rust_route(
    registry: Optional[CompatRegistry] = None,
) -> Dict[str, Any]:
    """校验 registry 与 Rust `compat_route` 映射完全对齐（两端对齐门）。

    返回结构化结果（不抛异常，便于测试断言）：
      - aligned: bool           方法名集合与 operation_class 均一致
      - missing: List[str]     Rust 有而 registry 无的方法
      - extra: List[str]       registry 有而 Rust 未声明的方法
      - mismatch: Dict[str, ...] operation_class 不一致的方法
    """
    reg = registry or get_compat_registry()
    rust_methods = set(RUST_COMPAT_ROUTE)
    py_methods = set(reg.methods())
    missing = sorted(rust_methods - py_methods)
    extra = sorted(py_methods - rust_methods)
    mismatch = {
        m: {"rust": RUST_COMPAT_ROUTE[m], "python": reg.operation_class(m)}
        for m in sorted(rust_methods & py_methods)
        if reg.operation_class(m) != RUST_COMPAT_ROUTE[m]
    }
    return {
        "aligned": not (missing or extra or mismatch),
        "missing": missing,
        "extra": extra,
        "mismatch": mismatch,
    }


