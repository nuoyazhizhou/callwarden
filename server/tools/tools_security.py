"""安全与协作面：护栏/分支感知/安全编辑/跨仓库/LSP（原 [L7/L12]）

拆分自 server/mcp_server.py（3609-4583 行区间），由 register(mcp) 注册。

H4B-I（T-1786590214634-9e740cdc-h4b-index-job）：index-write/job HTTP cutover
- dispatch.rs 无任何 rules.* / security.* / index.* / job.* RPC 分支
  （DaemonStateExt 默认返回 method_not_found）。本模块曾存在两处指向不存在
  RPC 的伪路由（diff_callees/compare_snapshots 的 `security.*`），以及一处
  HTTP 模式必 AttributeError 的路径（diff_callers 直接调用 _get_daemon_client()
  返回的 HttpDaemonRpcClient——该 client 无 diff_callers 方法），已全部移除。
- 本模块工具在 HTTP 模式下 fail-closed：经 _http_unsupported() 返回结构化
  unsupported 错误，不直连本地 SQLite（不构造 CodeGraphDB，无 SQLite fallback）；
  非 HTTP（legacy）模式保持本地 get_db() / DaemonClient 执行，公开方法语义不变。
- compat_route 扩展（把 python_compat 方法注册到 H3 compat worker）由
  h4b-registry-docs（...h4b-registry-docs）承接；本任务不触碰 Rust/compat_registry。
"""

# [L1] 分支感知图谱工具（BranchMixin）（register_branch / list_branches 等）
# [L6] Agent Rule Memory 工具（rule_candidate_* / rule_list / rule_sync_agents_md 等）
# [L7] 检查门禁工具（run_check_gate / resolve_gate_findings）
# [L12] 安全编辑/跨仓库/LSP 工具（propose_edit / detect_cross_repo_deps / lsp_* 等）

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _get_daemon_client, _get_db_path_for_daemon, get_db
from ...db import CodeGraphDB
from callwarden.server.daemon_client import (
    is_http_transport_enabled,
    route_worker_call,
)

# H4C-2 第三批（T-1786747295227-49c90d68）：安全/分支/跨仓库/LSP/规则组只读工具
# 接入 compat worker。注意：必须用顶层 `server.compat_registry` 导入，与
# compat_worker.py 保持同一模块单例（模块单例风险，见 tools_query.py L41-49 注释）。
from server.compat_registry import (  # noqa: E402
    SCOPE_WORKSPACE,
    CompatCallContext,
    register_compat_routes,
)

from ..daemon_client import route_rpc as _route


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def register_branch(branch_name: str, repo_root: str = "") -> dict:
        """注册分支工作区

        将分支注册为独立 workspace（name=分支名），用于分支感知图谱。
        同一仓库的多个分支通过 root_path 追加 "#分支名" 保证唯一性。
        若分支已存在则直接返回，不重复创建。

        Args:
            branch_name: 分支名（如 "main" / "feature-x"）
            repo_root: 仓库物理根路径，为空则使用默认工作区根路径

        Returns:
            {"workspace_id": int, "branch_name": str, "is_new": bool}
        """
        return _route('admin.branch_register', {"branch_name": branch_name, "repo_root": repo_root}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def list_branches() -> list:
        """列出所有分支工作区

        返回每个分支工作区的 id / name / root_path / created_at / symbol_count。

        Returns:
            分支工作区列表
        """
        return _route('list_branches', {}, 'READ_ONLY')

    @mcp.tool()
    def diff_branches(source_branch: str, target_branch: str) -> dict:
        """比较两个分支的符号差异

        通过比较两个 workspace 的 symbols 表（按 qualified_name 对比 symbol_hash）：
        - added: target 有但 source 没有
        - removed: source 有但 target 没有
        - modified: 两边都有但 symbol_hash 不同
        - unchanged_count: 两边都有且 hash 相同的符号数

        Args:
            source_branch: 源分支名
            target_branch: 目标分支名

        Returns:
            符号差异字典
        """
        return _route('query.diff_branches', {"source_branch": source_branch, "target_branch": target_branch}, 'READ_ONLY')

    @mcp.tool()
    def diff_callers(left_workspace_id: str, right_workspace_id: str,
                     qualified_name: str) -> dict:
        """对比两个 workspace 中同一符号的 caller 边集合

        基于 resolved edge delta，返回 left/right 各自独有的 caller 列表及共同 caller。

        修复 T-1783751538837-33e1: DaemonClient 已有 diff_callers 方法，但 MCP 层未暴露。
        H4B-I: HTTP 模式 fail-closed（HttpDaemonRpcClient 无 diff_callers 方法，
        直接调用会 AttributeError），仅 legacy 模式走 DaemonClient。

        Args:
            left_workspace_id: 左 workspace ID
            right_workspace_id: 右 workspace ID
            qualified_name: 符号限定名

        Returns:
            {"left_only": [...], "right_only": [...], "common": [...]}
            Rust 不可用时返回 {"error": "rust backend unavailable"}
        """
        return _route('query.diff_callers', {"left_workspace_id": left_workspace_id, "right_workspace_id": right_workspace_id, "qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def diff_callees(left_workspace_id: str, right_workspace_id: str,
                     qualified_name: str) -> dict:
        """对比两个 workspace 中同一符号的 callee 边集合

        基于 resolved edge delta，返回 left/right 各自独有的 callee 列表及共同 callee。

        修复 T-1783751538837-33e1: DaemonClient 已有 diff_callees 方法，但 MCP 层未暴露。
        H4B-I: 移除指向不存在 RPC 的 `security.diff_callees` 伪路由（HTTP 模式
        method_not_found），改为 fail-closed；legacy 模式保持 DaemonClient 执行。

        Args:
            left_workspace_id: 左 workspace ID
            right_workspace_id: 右 workspace ID
            qualified_name: 符号限定名

        Returns:
            {"left_only": [...], "right_only": [...], "common": [...]}
            Rust 不可用时返回 {"error": "rust backend unavailable"}
        """
        return _route('query.diff_callees', {"left_workspace_id": left_workspace_id, "right_workspace_id": right_workspace_id, "qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def compare_snapshots(left_workspace_id: str, right_workspace_id: str,
                          scope_type: str = "repo", scope_value: str = "") -> dict:
        """对比两个 workspace 中指定 scope 内的所有符号差异

        同步查询：小 scope（file/module）直接返回结果。
        仓库级 scope 应先调用 count_symbols_in_scope 检查大小，超阈值时改用后台 job。

        修复 T-1783751538837-33e1: DaemonClient 已有 compare_snapshots 方法，但 MCP 层未暴露。
        H4B-I: 移除指向不存在 RPC 的 `security.compare_snapshots` 伪路由（HTTP 模式
        method_not_found），改为 fail-closed；legacy 模式保持 DaemonClient 执行。

        Args:
            left_workspace_id: 左 workspace ID
            right_workspace_id: 右 workspace ID
            scope_type: "file" / "module" / "repo"
            scope_value: 文件路径或模块路径（repo 时忽略）

        Returns:
            {"changes": [...], "scope_type": str, "scope_value": str, "count": int}
            Rust 不可用时返回 {"error": "rust backend unavailable"}
        """
        return _route('admin.snapshot_compare', {"left_workspace_id": left_workspace_id, "right_workspace_id": right_workspace_id, "scope_type": scope_type, "scope_value": scope_value}, 'READ_ONLY')

    @mcp.tool()
    def switch_branch(branch_name: str) -> dict:
        """切换活动工作区到指定分支

        切换当前活动工作区到指定分支，后续查询在该分支上下文中执行。

        Args:
            branch_name: 分支名

        Returns:
            {"branch_name": str, "workspace_id": int, "symbol_count": N}
        """
        return _route('admin.branch_switch', {"branch_name": branch_name}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def merge_preview(source_branch: str, target_branch: str) -> dict:
        """合并预览：分析 source 分支变更对 target 分支的影响

        基于 diff_branches 结果，对 added/modified 符号调用 blast_radius，
        汇总受影响符号数、影响层级和风险等级。

        风险等级：
        - affected_symbols > 20 → high
        - affected_symbols > 5  → medium
        - 其余                   → low

        Args:
            source_branch: 源分支名
            target_branch: 目标分支名

        Returns:
            {"affected_symbols": N, "impact_layers": [...], "risk_level": "low/medium/high"}
        """
        return _route('merge_preview', {"source_branch": source_branch, "target_branch": target_branch}, 'READ_ONLY')

    @mcp.tool()
    def propose_edit(file_path: str, new_content: str, operation: str = "edit",
                     agent_task_id: str = "", symbol_hash: str = "",
                     dry_run: bool = False, expected_hash: str = "") -> dict:
        """提交安全编辑请求（Agent OS 核心能力）

        执行流程：
        1. 计算编辑前文件的 SHA-256 hash（file_hash_before）
        2. 计算编辑后内容的 SHA-256 hash（file_hash_after）
        3. 生成 diff 摘要（新增/删除行数）
        4. 写入 file_edit_audit 表（status=pending）
        5. 如果 dry_run=True，返回预览结果不实际写入
        6. 如果 dry_run=False，原子写入文件（先写临时文件再 rename）
        7. 更新 audit 记录 status=applied, applied_at=now

        Args:
            file_path: 文件路径（相对 workspace_root 或绝对路径）
            new_content: 编辑后的完整内容
            operation: 操作类型（edit / create / delete）
            agent_task_id: 关联的任务 ID（可选）
            symbol_hash: 关联的符号 hash（可选）
            dry_run: 是否仅预览（True 不实际写入文件）
            expected_hash: 编辑前期望文件 hash（可选，用于并发保护）

        Returns:
            {
                "audit_id": int,
                "file_path": str,
                "file_hash_before": str,
                "file_hash_after": str,
                "diff_summary": str,
                "status": "applied"/"preview"/"failed",
                "success": bool,
                "error": str  # 失败时存在
            }
        """
        return _route('edit.propose', {"file_path": file_path, "new_content": new_content, "operation": operation, "agent_task_id": agent_task_id, "symbol_hash": symbol_hash, "dry_run": dry_run, "expected_hash": expected_hash}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def propose_range_patch(file_path: str, start_line: int, end_line: int,
                            replacement: str, agent_task_id: str = "",
                            symbol_hash: str = "", dry_run: bool = False,
                            expected_hash: str = "") -> dict:
        """提交行号范围补丁，避免读写整个大文件

        行号为 1-based 闭区间。用于 Agent 只改目标函数、目标代码块或
        插入少量注释，而不需要提交完整文件内容。
        """
        return _route('edit.propose_range_patch', {"file_path": file_path, "start_line": start_line, "end_line": end_line, "replacement": replacement, "agent_task_id": agent_task_id, "symbol_hash": symbol_hash, "dry_run": dry_run, "expected_hash": expected_hash}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def propose_symbol_patch(file_path: str, symbol_name: str, patch: str,
                             mode: str = "replace", agent_task_id: str = "",
                             dry_run: bool = False,
                             expected_hash: str = "") -> dict:
        """提交符号级补丁，按图谱定位函数/类范围后局部改写

        mode 支持 replace / insert_before / insert_after。注释任务通常使用
        insert_before；bugfix/refactor 可使用 replace 或 range patch。
        """
        return _route('edit.propose_symbol_patch', {"file_path": file_path, "symbol_name": symbol_name, "patch": patch, "mode": mode, "agent_task_id": agent_task_id, "dry_run": dry_run, "expected_hash": expected_hash}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def propose_symbol_id_patch(symbol_id: int, patch: str,
                                mode: str = "replace", agent_task_id: str = "",
                                dry_run: bool = False, expected_hash: str = "",
                                expected_symbol_hash: str = "") -> dict:
        """提交基于 symbol_id 的精确符号补丁

        使用 symbols.id 定位当前符号快照，并在写入前校验文件 hash
        与符号 hash。工具内部会执行 Before-Edit Contract，block 时拒绝写入。
        mode 支持 replace / insert_before / insert_after。
        """
        return _route('edit.propose_symbol_id_patch', {"symbol_id": symbol_id, "patch": patch, "mode": mode, "agent_task_id": agent_task_id, "dry_run": dry_run, "expected_hash": expected_hash, "expected_symbol_hash": expected_symbol_hash}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def revert_edit(audit_id: int) -> dict:
        """回滚编辑（标记审计状态为 reverted）

        注意：审计表不存储完整文件内容，本方法仅标记审计状态。
        实际内容回滚需要依赖 git checkout 或外部备份机制。

        Args:
            audit_id: 审计记录 ID

        Returns:
            {
                "audit_id": int,
                "status": "reverted",
                "message": str,
                "file_path": str,
                "file_hash_before": str
            }
        """
        return _route('edit.revert', {"audit_id": audit_id}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_edit_history(file_path: str = "", limit: int = 20) -> list:
        """查询文件编辑历史

        按文件路径过滤或返回全部编辑记录，按 created_at 倒序排列。

        Args:
            file_path: 文件路径过滤（为空则返回全部）
            limit: 返回数量限制（默认 20）

        Returns:
            审计记录列表，每条记录含 id / file_path / operation / status /
            file_hash_before / file_hash_after / diff_summary / agent_task_id /
            created_at / applied_at / reverted_at
        """
        return _route('get_edit_history', {"file_path": file_path, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_edit_stats(time_window: str = "30d") -> dict:
        """获取文件编辑统计

        统计指定时间窗口内的编辑情况：
        - 总编辑数 / 成功数 / 回滚数 / 失败数
        - 按操作类型分组（edit / create / delete）
        - 回滚率（reverted / applied）

        Args:
            time_window: 时间窗口（如 7d / 30d / 90d / "" 全部）

        Returns:
            {
                "time_window": str,
                "total": int,
                "by_status": {"applied": N, "reverted": N, "failed": N, "pending": N},
                "by_operation": {"edit": N, "create": N, "delete": N},
                "revert_rate": float
            }
        """
        return _route('edit.stats', {"time_window": time_window}, 'READ_ONLY')

    @mcp.tool()
    def detect_cross_repo_deps(source_workspace: str, target_workspace: str = "") -> dict:
        """检测跨仓库依赖关系

        通过扫描源仓库中所有符号的 import 语句，匹配目标仓库的符号名，
        识别跨仓库依赖。结果持久化到 cross_repo_deps 表。

        Args:
            source_workspace: 源仓库名称
            target_workspace: 目标仓库名称（为空则扫描所有其他仓库）

        Returns:
            {
                "source_workspace": str,
                "detected_deps": [
                    {
                        "target_workspace": str,
                        "dependency_type": "import",
                        "source_symbol": str,
                        "target_symbol": str,
                        "evidence": str,
                        "confidence": float
                    }
                ],
                "total_deps": int
            }
        """
        _res = _route('task.job_submit', {**{"source_workspace": source_workspace, "target_workspace": target_workspace}, "job_type": "cross_repo_deps", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def find_shared_symbols(workspace_a: str = "", workspace_b: str = "") -> dict:
        """查找跨仓库共享符号（相同 content_hash 的函数）

        利用 symbol_contents 表的 content_hash 去重特性，
        识别两个仓库中实现完全相同的函数（共享代码 / 复制粘贴）。

        Args:
            workspace_a: 仓库名称 A（为空则扫描所有 workspace）
            workspace_b: 仓库名称 B（为空则 A 与所有其他仓库对比）

        Returns:
            {
                "total_shared": int,
                "shared_symbols": [
                    {
                        "content_hash": str,
                        "workspace_a": str, "workspace_b": str,
                        "qualified_name_a": str, "qualified_name_b": str,
                        "file_a": str, "file_b": str
                    }
                ]
            }
        """
        return _route('find_shared_symbols', {"workspace_a": workspace_a, "workspace_b": workspace_b}, 'READ_ONLY')

    @mcp.tool()
    def cross_repo_impact(symbol_hash: str, depth: int = 2) -> dict:
        """跨仓库影响分析

        给定一个符号，分析它的变更会影响哪些其他仓库。
        联合 cross_repo_deps 表 + blast_radius 进行多层级传播分析。

        Args:
            symbol_hash: 变更符号的 hash（可从 search_symbols / get_symbol 获取）
            depth: 影响传播深度（默认 2）

        Returns:
            {
                "source_symbol": str,
                "source_workspace": str,
                "local_impacted_count": int,
                "impacted_repos": [
                    {"workspace": str, "impacted_symbols": [...],
                     "dependency_type": str, "confidence": float}
                ],
                "total_impacted_repos": int,
                "risk_level": "low/medium/high"
            }
        """
        return _route('cross_repo_impact', {"symbol_hash": symbol_hash, "depth": depth}, 'READ_ONLY')

    @mcp.tool()
    def cross_repo_summary() -> dict:
        """跨仓库分析总览

        返回所有已注册仓库的统计信息：仓库列表、跨仓库依赖数、共享符号数、
        依赖类型分布。

        Returns:
            {
                "total_repos": int,
                "repos": [{"name": str, "root_path": str, "symbol_count": int}],
                "total_cross_deps": int,
                "total_shared_symbols": int,
                "deps_by_type": {"import": N, "call": N, "shared_symbol": N}
            }
        """
        return _route('cross_repo_summary', {}, 'READ_ONLY')

    @mcp.tool()
    def lsp_hover(file_path: str, line: int, character: int) -> dict:
        """获取符号的 hover 信息（类型签名、文档注释等）

        通过 LSP 协议查询符号的悬浮信息，补充 tree-sitter 静态分析。
        支持 Python / TypeScript / Go / Rust（需对应 LSP 服务器已安装）。

        Args:
            file_path: 文件绝对路径
            line: 行号（0-based）
            character: 列号（0-based）

        Returns:
            {
                "file_path": str, "line": int, "character": int,
                "contents": str, "available": bool
            }
        """
        return _route('lsp_hover', {"file_path": file_path, "line": line, "character": character}, 'READ_ONLY')

    @mcp.tool()
    def lsp_definition(file_path: str, line: int, character: int) -> dict:
        """跳转到定义

        通过 LSP 协议查询符号定义位置（跨文件跳转）。

        Args:
            file_path: 文件路径
            line: 行号（0-based）
            character: 列号（0-based）

        Returns:
            {
                "definitions": [{"uri": str, "file_path": str, "line": int, "character": int}],
                "available": bool
            }
        """
        return _route('lsp_definition', {"file_path": file_path, "line": line, "character": character}, 'READ_ONLY')

    @mcp.tool()
    def lsp_references(
        file_path: str,
        line: int,
        character: int,
        include_declaration: bool = True,
    ) -> dict:
        """查找符号的所有引用

        通过 LSP 协议查找符号在项目中的所有引用位置。
        比 calls 表的静态分析更准确（LSP 理解语义）。

        Args:
            file_path: 文件路径
            line: 行号（0-based）
            character: 列号（0-based）
            include_declaration: 是否包含定义本身（默认 True）

        Returns:
            {
                "references": [{"uri": str, "file_path": str, "line": int, "character": int}],
                "total": int, "available": bool
            }
        """
        return _route('lsp_references', {"file_path": file_path, "line": line, "character": character, "include_declaration": include_declaration}, 'READ_ONLY')

    @mcp.tool()
    def lsp_diagnostics(file_path: str) -> dict:
        """获取文件诊断信息（错误、警告）

        通过 LSP 服务器获取文件的实时诊断（编译错误、类型错误、lint 警告等）。
        比 Semgrep 静态规则更精准（LSP 由编译器驱动）。

        Args:
            file_path: 文件路径

        Returns:
            {
                "file_path": str,
                "diagnostics": [
                    {"line": int, "character": int, "message": str,
                     "severity": int, "source": str}
                ],
                "total": int, "available": bool
            }
        """
        return _route('lsp_diagnostics', {"file_path": file_path}, 'READ_ONLY')

    @mcp.tool()
    def lsp_completion(file_path: str, line: int, character: int) -> dict:
        """获取代码补全建议

        通过 LSP 协议获取指定位置的补全建议（方法、字段、变量等）。
        可用于 Agent 生成代码时的智能补全。

        Args:
            file_path: 文件路径
            line: 行号（0-based）
            character: 列号（0-based）

        Returns:
            {
                "completions": [{"label": str, "kind": int, "detail": str}],
                "total": int, "available": bool
            }
        """
        return _route('lsp_completion', {"file_path": file_path, "line": line, "character": character}, 'READ_ONLY')

    @mcp.tool()
    def lsp_check_available(language: str = "") -> dict:
        """检查 LSP 服务器是否可用

        检查指定语言（或所有支持语言）的 LSP 服务器是否已安装且可启动。
        Agent 在调用其他 lsp_* 工具前应先调用此方法确认可用性。

        Args:
            language: 语言（python/typescript/go/rust），为空则检查所有

        Returns:
            {
                "available_servers": {"python": true, "typescript": false, ...},
                "total_available": int
            }
        """
        return _route('lsp_check_available', {"language": language}, 'READ_ONLY')

    @mcp.tool()
    def run_check_gate(task_id: str, step_id: str, changed_files: list) -> dict:
        """手动触发检查门禁（F6）

        对变更文件运行语法检查 + Semgrep 扫描。通常由 task_report_step
        在步骤成功且有文件变更时自动触发，此工具用于手动补检或复查。

        检查失败会自动在任务中插入 fix_gate_failure 步骤，Agent 必须修复
        后才能继续。结果同时写入 guardrail_findings 表。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            changed_files: 变更的文件路径列表

        Returns:
            {
                "passed": bool,
                "checks_run": ["syntax", "semgrep"],
                "findings": [...],
                "fix_required": bool,
                "summary": "..."
            }
        """
        return _route('gate.run_check', {"task_id": task_id, "step_id": step_id, "changed_files": changed_files}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def resolve_gate_findings(task_id: str) -> dict:
        """标记任务的门禁发现为已解决（F6）

        Agent 修复缺陷后调用此工具，将该任务关联文件上的所有 open 状态
        guardrail_findings 标记为 resolved。

        Args:
            task_id: 任务 ID

        Returns:
            {"resolved_count": int, "task_id": str}
        """
        return _route('gate.resolve_findings', {"task_id": task_id}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def rule_candidate_create(
        title: str,
        rule_text: str,
        scope: Optional[dict] = None,
        severity: str = "info",
        source: str = "manual",
        evidence: Optional[dict] = None,
        confidence: float = 0.0,
    ) -> dict:
        """创建候选规则（pending 状态）

        Agent 在 task 执行过程中观察到的规则候选需要走审核流程：
        创建 → 审核（accept/reject）→ 写入 agent_rules 生效。

        Args:
            title: 规则标题（简短描述）
            rule_text: 规则正文（Agent 注入时会原文返回）
            scope: 作用域 dict，支持 languages / file_patterns /
                symbol_kinds / actions / finding_types / module_prefixes
            severity: 严重级别 critical / error / warning / info
            source: 来源 manual / auto_quality_findings / auto_semgrep /
                task_review / other
            evidence: 证据 dict（如 task_id、occurrences 等）
            confidence: 置信度 0.0-1.0

        Returns:
            {"candidate_id": "ARC-xxx"}
        """
        return _route('rule.candidate_create', {"title": title, "rule_text": rule_text, "scope": scope, "severity": severity, "source": source, "evidence": evidence, "confidence": confidence}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def rule_candidate_list(status: str = "pending", limit: int = 50) -> dict:
        """列出候选规则

        Args:
            status: 状态过滤 pending / accepted / rejected，空串返回所有
            limit: 返回数量上限（默认 50）

        Returns:
            {"candidates": [...], "count": int}
        """
        return _route('rule_candidate_list', {"status": status, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def rule_candidate_accept(
        candidate_id: str,
        reviewer: str = "agent",
    ) -> dict:
        """接受候选规则，写入 agent_rules（active）

        幂等：重复 accept 已 accepted 的 candidate 会返回原 linked_rule_id。

        Args:
            candidate_id: 候选规则 ID（ARC-xxx）
            reviewer: 审核人标识

        Returns:
            {"rule_id": "AR-xxx"} 或 {"error": ...}
        """
        return _route('rule.candidate_accept', {"candidate_id": candidate_id, "reviewer": reviewer}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def rule_candidate_reject(
        candidate_id: str,
        reviewer: str = "agent",
        reason: str = "",
    ) -> dict:
        """拒绝候选规则

        Args:
            candidate_id: 候选规则 ID（ARC-xxx）
            reviewer: 审核人标识
            reason: 拒绝原因（可选）

        Returns:
            {"rejected": bool}
        """
        return _route('rule.candidate_reject', {"candidate_id": candidate_id, "reviewer": reviewer, "reason": reason}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def rule_list(status: str = "active", limit: int = 100) -> dict:
        """列出已生效规则

        Args:
            status: 状态过滤 active / deprecated / removed，空串返回所有
            limit: 返回数量上限（默认 100）

        Returns:
            {"rules": [...], "count": int}
        """
        return _route('rule_list', {"status": status, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_applicable_rules(
        context: dict,
        limit: int = 10,
    ) -> dict:
        """按上下文返回匹配的 active 规则

        scope 匹配规则：
        - 空 scope = 全局匹配
        - 同字段内多值 OR 匹配（如 languages: [python, rust]）
        - 不同字段间 AND 匹配
        - file_patterns 支持 glob；module_prefixes 前缀匹配

        排序：severity 优先级 → 命中字段数 → updated_at 倒序

        Args:
            context: 上下文 dict，支持字段 languages / file_patterns /
                symbol_kinds / actions / finding_types / module_prefixes
            limit: 返回数量上限（默认 10）

        Returns:
            {"rules": [...], "count": int}
        """
        return _route('get_applicable_rules', {"context": context, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def rule_sync_agents_md(
        target_path: str = "AGENTS.md",
        dry_run: bool = True,
        actor: str = "agent",
    ) -> dict:
        """把 active 规则同步到 AGENTS.md 标记区

        安全策略：
        - dry_run=True（默认）只返回 preview，不写文件
        - apply 模式只替换 CALLWARDEN_RULES_START/END 之间内容，
          不触碰人工维护区域
        - 标记区不存在时返回 error + suggested_block
        - 写入后记录 agent_rule_sync_log 并标记规则 synced_to_agents_md=1

        Args:
            target_path: AGENTS.md 文件路径（相对 workspace 或绝对）
            dry_run: True=只返回 preview，False=实际写入
            actor: 操作者标识

        Returns:
            {"success": bool, "rule_count": int, "preview": str, ...}
        """
        return _route('rule.sync_agents_md', {"target_path": target_path, "dry_run": dry_run, "actor": actor}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def rule_insert_agents_md_block(
        target_path: str = "AGENTS.md",
        actor: str = "agent",
    ) -> dict:
        """在 AGENTS.md 末尾插入 Call Warden 规则标记块

        当标记区不存在时调用此方法插入空标记块，之后
        rule_sync_agents_md 才能正常工作。重复插入会返回失败。

        Args:
            target_path: AGENTS.md 文件路径
            actor: 操作者标识

        Returns:
            {"success": bool, "target_path": str, "message": str}
        """
        return _route('rule.insert_agents_md_block', {"target_path": target_path, "actor": actor}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def extract_rule_candidates_from_quality_findings(
        task_id: str = "",
        min_occurrences: int = 2,
    ) -> dict:
        """从 task_quality_findings 聚合重复问题生成候选规则

        聚合维度：(finding_type, severity, source)
        - 同一聚合键 count >= min_occurrences 时生成 1 个 pending 候选
        - 去重：同一聚合键已有 pending 候选时跳过
        - evidence 保存 finding_ids（最多 10 条）和 occurrences
        - confidence = min(1.0, occurrences/10)

        Args:
            task_id: 任务 ID，空串则全库扫描
            min_occurrences: 阈值（默认 2）

        Returns:
            {"candidate_ids": [...], "count": int}
        """
        return _route('rule.extract_candidates', {"task_id": task_id, "min_occurrences": min_occurrences}, 'PROTECTED_MUTATION')


# ============================================================
# H4C-2 第三批（T-1786747295227-49c90d68）：分支/编辑历史/跨仓库/LSP/规则查询
# 只读工具 worker handler（index 组，步骤#0 + 整改）
# ============================================================
# 接入说明（遵循 tools_summary.py 已收口模式）：
# - handler 定义在工具模块内，由 compat_worker.handle_frame 通用派发按 registry
#   分发；本模块被 worker 装配 import 后模块级注册随之执行；
# - 轻量只读绑定：object.__new__(CodeGraphDB) 绕过 __init__（含 PRAGMA WAL /
#   schema 迁移 / workspace 注册等写副作用），注入 ctx.conn（worker 的
#   mode=ro 只读连接）+ ctx.workspace_id 后复用 db 层查询方法；
# - 写语义工具不接入（fail-closed）：register_branch / switch_branch /
#   propose_edit / propose_range_patch / propose_symbol_patch /
#   propose_symbol_id_patch / revert_edit / detect_cross_repo_deps /
#   run_check_gate / resolve_gate_findings / rule_candidate_create /
#   rule_candidate_accept / rule_candidate_reject / rule_sync_agents_md /
#   rule_insert_agents_md_block / extract_rule_candidates_from_quality_findings
#   （governance_write，不接入 worker）；
# - 高风险核验项（diff_callers/diff_callees/compare_snapshots）：db 层（db_branch
#   等 Mixin）无基于 SQL 的等价实现，依赖 Rust 内存 GraphStore（DaemonClient
#   self._svc._cache），worker 只读 SQLite 连接无法承载 → 不接入，维持 fail-closed
#   （见工具函数体内注释与任务 step#0 result）。
_SECURITY_COMPAT_SCOPE = SCOPE_WORKSPACE  # 矩阵 workspace_scoped


def _bind_readonly_db(ctx: CompatCallContext) -> CodeGraphDB:
    """轻量只读绑定：绕过 CodeGraphDB.__init__，注入 worker 只读连接与显式 workspace。

    与 tools_query.py / tools_summary.py 同款：ctx.conn 由 compat_worker 用
    `file:{db_path}?mode=ro` 打开（read_only 契约）；active_workspace 注入
    ctx.workspace_id，db 层查询基于 `_get_active_workspace_id()` 过滤；
    workspace_root 从 workspaces 表解析（跨仓库/LSP 等文件路径依赖）。
    """
    db = object.__new__(CodeGraphDB)
    db.conn = ctx.conn
    db.active_workspace = {"id": ctx.workspace_id} if ctx.workspace_id else None
    db.workspace_root = None
    if ctx.workspace_id is not None:
        try:
            row = ctx.conn.execute(
                "SELECT root_path FROM workspaces WHERE id = ?",
                (ctx.workspace_id,),
            ).fetchone()
            if row is not None:
                db.workspace_root = row["root_path"]
        except Exception:
            db.workspace_root = None
    return db


def _h_list_branches(ctx: CompatCallContext) -> Any:
    """worker handler：列出所有分支工作区（只读）"""
    return _bind_readonly_db(ctx).list_branch_workspaces()


def _h_merge_preview(ctx: CompatCallContext) -> Any:
    """worker handler：合并预览（只读等价版）。

    db.merge_preview 内部调用 switch_branch_context → set_active_workspace
    （UPDATE workspaces 写副作用），worker 只读连接无法承载。本 handler 跳过
    switch，直接基于注入的 workspace 计算：diff_branches（纯 SELECT）+ 逐个
    blast_radius（Rust fail-soft 降级 SQL BFS，只读），聚合逻辑与 db 层一致。
    影响半径在 worker 当前 workspace 上下文计算（不切换 target 分支），
    语义等价于原实现的近似（原实现切换后亦仅用于 blast_radius 的 workspace 过滤）。
    """
    db = _bind_readonly_db(ctx)
    diff = db.diff_branches(
        ctx.params.get("source_branch", ""),
        ctx.params.get("target_branch", ""),
    )
    if isinstance(diff, dict) and "error" in diff:
        return diff

    # 收集需要分析的符号 hash（target 侧的 added 和 modified）
    target_hashes: list = []
    for item in diff.get("added", []) if isinstance(diff, dict) else []:
        h = item.get("symbol_hash", "")
        if h:
            target_hashes.append(h)
    for item in diff.get("modified", []) if isinstance(diff, dict) else []:
        h = item.get("target_hash", "")
        if h:
            target_hashes.append(h)

    # 逐个调用 blast_radius，去重收集受影响符号（与原实现一致，单符号失败不中断）
    seen_hashes: set = set()
    all_impacted: set = set()
    impact_layers: list = []

    for symbol_hash in target_hashes:
        if symbol_hash in seen_hashes:
            continue
        seen_hashes.add(symbol_hash)
        try:
            br = db.blast_radius(symbol_hash, depth=3)
        except Exception:
            continue

        for layer in br.get("layers", []):
            for sym in layer.get("symbols", []):
                h = sym.get("symbol_hash", "")
                if h:
                    all_impacted.add(h)

        impact_layers.append({
            "source_symbol": br.get("source_symbol", ""),
            "source_hash": symbol_hash,
            "total_impacted": br.get("total_impacted", 0),
            "by_layer": br.get("by_layer", {}),
        })

    affected_count = len(all_impacted)

    # 风险等级评估（与原实现一致）
    if affected_count > 20:
        risk_level = "high"
    elif affected_count > 5:
        risk_level = "medium"
    else:
        risk_level = "low"

    return {
        "affected_symbols": affected_count,
        "impact_layers": impact_layers,
        "risk_level": risk_level,
    }


def _h_get_edit_history(ctx: CompatCallContext) -> Any:
    """worker handler：查询文件编辑历史（只读）"""
    return _bind_readonly_db(ctx).get_edit_history(
        ctx.params.get("file_path", ""),
        ctx.params.get("limit", 20),
    )


def _h_find_shared_symbols(ctx: CompatCallContext) -> Any:
    """worker handler：查找跨仓库共享符号（只读）"""
    return _bind_readonly_db(ctx).find_shared_symbols(
        ctx.params.get("workspace_a", ""),
        ctx.params.get("workspace_b", ""),
    )


def _h_cross_repo_impact(ctx: CompatCallContext) -> Any:
    """worker handler：跨仓库影响分析（只读）

    db.cross_repo_impact 内部 blast_radius 有 hasattr+try-except 保护，且
    blast_radius 本体 Rust fail-soft 降级 SQL BFS（只读），可承载。
    """
    return _bind_readonly_db(ctx).cross_repo_impact(
        ctx.params.get("symbol_hash", ""),
        ctx.params.get("depth", 2),
    )


def _h_cross_repo_summary(ctx: CompatCallContext) -> Any:
    """worker handler：跨仓库分析总览（只读）"""
    return _bind_readonly_db(ctx).cross_repo_summary()


def _h_lsp_hover(ctx: CompatCallContext) -> Any:
    """worker handler：LSP hover 信息（只读）"""
    return _bind_readonly_db(ctx).lsp_hover(
        ctx.params.get("file_path", ""),
        ctx.params.get("line", 0),
        ctx.params.get("character", 0),
    )


def _h_lsp_definition(ctx: CompatCallContext) -> Any:
    """worker handler：LSP 跳转定义（只读）"""
    return _bind_readonly_db(ctx).lsp_definition(
        ctx.params.get("file_path", ""),
        ctx.params.get("line", 0),
        ctx.params.get("character", 0),
    )


def _h_lsp_references(ctx: CompatCallContext) -> Any:
    """worker handler：LSP 查找引用（只读）"""
    return _bind_readonly_db(ctx).lsp_references(
        ctx.params.get("file_path", ""),
        ctx.params.get("line", 0),
        ctx.params.get("character", 0),
        ctx.params.get("include_declaration", True),
    )


def _h_lsp_diagnostics(ctx: CompatCallContext) -> Any:
    """worker handler：LSP 文件诊断（只读）"""
    return _bind_readonly_db(ctx).lsp_diagnostics(
        ctx.params.get("file_path", ""),
    )


def _h_lsp_completion(ctx: CompatCallContext) -> Any:
    """worker handler：LSP 代码补全（只读）"""
    return _bind_readonly_db(ctx).lsp_completion(
        ctx.params.get("file_path", ""),
        ctx.params.get("line", 0),
        ctx.params.get("character", 0),
    )


def _h_lsp_check_available(ctx: CompatCallContext) -> Any:
    """worker handler：检查 LSP 服务器可用性（只读）"""
    return _bind_readonly_db(ctx).lsp_check_available(
        ctx.params.get("language", ""),
    )


def _h_rule_candidate_list(ctx: CompatCallContext) -> Any:
    """worker handler：列出候选规则（只读，查 agent_rule_candidates）"""
    db = _bind_readonly_db(ctx)
    rows = db.rule_candidate_list(
        status=ctx.params.get("status", "pending"),
        limit=ctx.params.get("limit", 50),
    )
    return {"candidates": rows, "count": len(rows)}


def _h_rule_list(ctx: CompatCallContext) -> Any:
    """worker handler：列出已生效规则（只读，查 agent_rules）"""
    db = _bind_readonly_db(ctx)
    rules = db.rule_list(
        status=ctx.params.get("status", "active"),
        limit=ctx.params.get("limit", 100),
    )
    return {"rules": rules, "count": len(rules)}


def _h_get_applicable_rules(ctx: CompatCallContext) -> Any:
    """worker handler：按上下文返回匹配的 active 规则（只读，查 agent_rules 后内存匹配）"""
    db = _bind_readonly_db(ctx)
    rules = db.get_applicable_rules(
        context=ctx.params.get("context", {}),
        limit=ctx.params.get("limit", 10),
    )
    return {"rules": rules, "count": len(rules)}


# 分支/编辑历史/跨仓库/LSP + 规则查询只读白名单（16 个，get_edit_stats 已
# W2-3 T-1786840097331-fd01a3f8 迁移 rust_native 移除）：写语义工具
# （register_branch / switch_branch / propose_* / revert_edit /
# detect_cross_repo_deps / run_check_gate / resolve_gate_findings /
# rule_candidate_create/accept/reject / rule_sync_agents_md /
# rule_insert_agents_md_block / extract_rule_candidates_from_quality_findings）
# 与高风险核验项（diff_callers / diff_callees / compare_snapshots）不接入，fail-closed。
# 规则查询工具（rule_candidate_list / rule_list / get_applicable_rules）纯 SELECT
# 只读，本整改（T-1786747295227-49c90d68）接入 worker。
_SECURITY_READ_ONLY_METHODS: Dict[str, Any] = {
    "list_branches": _h_list_branches,
    "merge_preview": _h_merge_preview,
    "get_edit_history": _h_get_edit_history,
    "find_shared_symbols": _h_find_shared_symbols,
    "cross_repo_impact": _h_cross_repo_impact,
    "cross_repo_summary": _h_cross_repo_summary,
    "lsp_hover": _h_lsp_hover,
    "lsp_definition": _h_lsp_definition,
    "lsp_references": _h_lsp_references,
    "lsp_diagnostics": _h_lsp_diagnostics,
    "lsp_completion": _h_lsp_completion,
    "lsp_check_available": _h_lsp_check_available,
    "rule_candidate_list": _h_rule_candidate_list,
    "rule_list": _h_rule_list,
    "get_applicable_rules": _h_get_applicable_rules,
}

# 模块级注册：worker 装配 import 本模块时执行，注册到 compat_registry 单例并
# 同步 RUST_COMPAT_ROUTE（Rust 侧 http_server.rs 白名单在步骤#2 同步）。
# W4-4（T-1786886251769-22b94ee8-sub-4）：diff_branches 迁移 rust_native，
# 从本注册表移除（15 个，16->15）。
register_compat_routes(
    _SECURITY_READ_ONLY_METHODS,
    workspace_scope=_SECURITY_COMPAT_SCOPE,
    description="H4C-2 第三批分支/编辑历史/跨仓库/LSP/规则查询组只读工具（15 个，T-1786747295227-49c90d68 整改；get_edit_stats 已 W2-3 T-1786840097331-fd01a3f8 迁移 rust_native；diff_branches 已 W4-4 T-1786886251769-22b94ee8-sub-4 迁移 rust_native）",
)
