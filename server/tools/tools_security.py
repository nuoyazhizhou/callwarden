"""安全与协作面：护栏/分支感知/安全编辑/跨仓库/LSP（原 [L7/L12]）

拆分自 server/mcp_server.py（3609-4583 行区间），由 register(mcp) 注册。
"""

# [L1] 分支感知图谱工具（BranchMixin）（register_branch / list_branches 等）
# [L6] Agent Rule Memory 工具（rule_candidate_* / rule_list / rule_sync_agents_md 等）
# [L7] 检查门禁工具（run_check_gate / resolve_gate_findings）
# [L12] 安全编辑/跨仓库/LSP 工具（propose_edit / detect_cross_repo_deps / lsp_* 等）

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _get_daemon_client, get_db


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
        try:
            db = get_db()
            return db.register_branch_workspace(branch_name, repo_root)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def list_branches() -> list:
        """列出所有分支工作区

        返回每个分支工作区的 id / name / root_path / created_at / symbol_count。

        Returns:
            分支工作区列表
        """
        try:
            db = get_db()
            return db.list_branch_workspaces()
        except Exception as e:
            return [{"error": str(e)}]

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
        try:
            db = get_db()
            return db.diff_branches(source_branch, target_branch)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def diff_callers(left_workspace_id: str, right_workspace_id: str,
                     qualified_name: str) -> dict:
        """对比两个 workspace 中同一符号的 caller 边集合

        基于 resolved edge delta，返回 left/right 各自独有的 caller 列表及共同 caller。

        修复 T-1783751538837-33e1: DaemonClient 已有 diff_callers 方法，但 MCP 层未暴露。

        Args:
            left_workspace_id: 左 workspace ID
            right_workspace_id: 右 workspace ID
            qualified_name: 符号限定名

        Returns:
            {"left_only": [...], "right_only": [...], "common": [...]}
            Rust 不可用时返回 {"error": "rust backend unavailable"}
        """
        try:
            client = _get_daemon_client()
            result = client.diff_callers(left_workspace_id, right_workspace_id,
                                         qualified_name)
            if result is None:
                return {"error": "rust backend unavailable"}
            return result
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def diff_callees(left_workspace_id: str, right_workspace_id: str,
                     qualified_name: str) -> dict:
        """对比两个 workspace 中同一符号的 callee 边集合

        基于 resolved edge delta，返回 left/right 各自独有的 callee 列表及共同 callee。

        修复 T-1783751538837-33e1: DaemonClient 已有 diff_callees 方法，但 MCP 层未暴露。

        Args:
            left_workspace_id: 左 workspace ID
            right_workspace_id: 右 workspace ID
            qualified_name: 符号限定名

        Returns:
            {"left_only": [...], "right_only": [...], "common": [...]}
            Rust 不可用时返回 {"error": "rust backend unavailable"}
        """
        try:
            client = _get_daemon_client()
            result = client.diff_callees(left_workspace_id, right_workspace_id,
                                         qualified_name)
            if result is None:
                return {"error": "rust backend unavailable"}
            return result
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def compare_snapshots(left_workspace_id: str, right_workspace_id: str,
                          scope_type: str = "repo", scope_value: str = "") -> dict:
        """对比两个 workspace 中指定 scope 内的所有符号差异

        同步查询：小 scope（file/module）直接返回结果。
        仓库级 scope 应先调用 count_symbols_in_scope 检查大小，超阈值时改用后台 job。

        修复 T-1783751538837-33e1: DaemonClient 已有 compare_snapshots 方法，但 MCP 层未暴露。

        Args:
            left_workspace_id: 左 workspace ID
            right_workspace_id: 右 workspace ID
            scope_type: "file" / "module" / "repo"
            scope_value: 文件路径或模块路径（repo 时忽略）

        Returns:
            {"changes": [...], "scope_type": str, "scope_value": str, "count": int}
            Rust 不可用时返回 {"error": "rust backend unavailable"}
        """
        try:
            client = _get_daemon_client()
            result = client.compare_snapshots(left_workspace_id, right_workspace_id,
                                               scope_type, scope_value)
            if result is None:
                return {"error": "rust backend unavailable"}
            return result
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def switch_branch(branch_name: str) -> dict:
        """切换活动工作区到指定分支

        切换当前活动工作区到指定分支，后续查询在该分支上下文中执行。

        Args:
            branch_name: 分支名

        Returns:
            {"branch_name": str, "workspace_id": int, "symbol_count": N}
        """
        try:
            db = get_db()
            return db.switch_branch_context(branch_name)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.merge_preview(source_branch, target_branch)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.propose_edit(
                file_path=file_path,
                new_content=new_content,
                operation=operation,
                agent_task_id=agent_task_id,
                symbol_hash=symbol_hash,
                dry_run=dry_run,
                expected_hash=expected_hash,
            )
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def propose_range_patch(file_path: str, start_line: int, end_line: int,
                            replacement: str, agent_task_id: str = "",
                            symbol_hash: str = "", dry_run: bool = False,
                            expected_hash: str = "") -> dict:
        """提交行号范围补丁，避免读写整个大文件

        行号为 1-based 闭区间。用于 Agent 只改目标函数、目标代码块或
        插入少量注释，而不需要提交完整文件内容。
        """
        try:
            db = get_db()
            return db.propose_range_patch(
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                replacement=replacement,
                agent_task_id=agent_task_id,
                symbol_hash=symbol_hash,
                dry_run=dry_run,
                expected_hash=expected_hash,
            )
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def propose_symbol_patch(file_path: str, symbol_name: str, patch: str,
                             mode: str = "replace", agent_task_id: str = "",
                             dry_run: bool = False,
                             expected_hash: str = "") -> dict:
        """提交符号级补丁，按图谱定位函数/类范围后局部改写

        mode 支持 replace / insert_before / insert_after。注释任务通常使用
        insert_before；bugfix/refactor 可使用 replace 或 range patch。
        """
        try:
            db = get_db()
            return db.propose_symbol_patch(
                file_path=file_path,
                symbol_name=symbol_name,
                patch=patch,
                mode=mode,
                agent_task_id=agent_task_id,
                dry_run=dry_run,
                expected_hash=expected_hash,
            )
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.propose_symbol_id_patch(
                symbol_id=symbol_id,
                patch=patch,
                mode=mode,
                agent_task_id=agent_task_id,
                dry_run=dry_run,
                expected_hash=expected_hash,
                expected_symbol_hash=expected_symbol_hash,
            )
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.revert_edit(audit_id=audit_id)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.get_edit_history(file_path=file_path, limit=limit)
        except Exception as e:
            return [{"error": str(e)}]

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
        try:
            db = get_db()
            return db.get_edit_stats(time_window=time_window)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.detect_cross_repo_deps(
                source_workspace=source_workspace,
                target_workspace=target_workspace,
            )
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.find_shared_symbols(
                workspace_a=workspace_a,
                workspace_b=workspace_b,
            )
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.cross_repo_impact(symbol_hash=symbol_hash, depth=depth)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.cross_repo_summary()
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.lsp_hover(file_path=file_path, line=line, character=character)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.lsp_definition(file_path=file_path, line=line, character=character)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.lsp_references(
                file_path=file_path, line=line, character=character,
                include_declaration=include_declaration,
            )
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.lsp_diagnostics(file_path=file_path)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.lsp_completion(file_path=file_path, line=line, character=character)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.lsp_check_available(language=language)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.run_check_gate(
                task_id=task_id, step_id=step_id, changed_files=changed_files
            )
        except Exception as e:
            return {"error": str(e), "passed": False}

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
        try:
            db = get_db()
            return db.resolve_gate_findings(task_id=task_id)
        except Exception as e:
            return {"error": str(e), "resolved_count": 0}

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
        try:
            db = get_db()
            cid = db.rule_candidate_create(
                title=title,
                rule_text=rule_text,
                scope=scope or {},
                severity=severity,
                source=source,
                evidence=evidence or {},
                confidence=confidence,
            )
            return {"candidate_id": cid}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def rule_candidate_list(status: str = "pending", limit: int = 50) -> dict:
        """列出候选规则

        Args:
            status: 状态过滤 pending / accepted / rejected，空串返回所有
            limit: 返回数量上限（默认 50）

        Returns:
            {"candidates": [...], "count": int}
        """
        try:
            db = get_db()
            rows = db.rule_candidate_list(status=status, limit=limit)
            return {"candidates": rows, "count": len(rows)}
        except Exception as e:
            return {"error": str(e), "candidates": [], "count": 0}

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
        try:
            db = get_db()
            rid = db.rule_candidate_accept(
                candidate_id=candidate_id, reviewer=reviewer
            )
            return {"rule_id": rid}
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            ok = db.rule_candidate_reject(
                candidate_id=candidate_id, reviewer=reviewer, reason=reason
            )
            return {"rejected": ok}
        except Exception as e:
            return {"error": str(e), "rejected": False}

    @mcp.tool()
    def rule_list(status: str = "active", limit: int = 100) -> dict:
        """列出已生效规则

        Args:
            status: 状态过滤 active / deprecated / removed，空串返回所有
            limit: 返回数量上限（默认 100）

        Returns:
            {"rules": [...], "count": int}
        """
        try:
            db = get_db()
            rules = db.rule_list(status=status, limit=limit)
            return {"rules": rules, "count": len(rules)}
        except Exception as e:
            return {"error": str(e), "rules": [], "count": 0}

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
        try:
            db = get_db()
            rules = db.get_applicable_rules(context=context, limit=limit)
            return {"rules": rules, "count": len(rules)}
        except Exception as e:
            return {"error": str(e), "rules": [], "count": 0}

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
        try:
            db = get_db()
            return db.rule_sync_agents_md(
                target_path=target_path, dry_run=dry_run, actor=actor
            )
        except Exception as e:
            return {"error": str(e), "success": False}

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
        try:
            db = get_db()
            return db.rule_insert_agents_md_block(
                target_path=target_path, actor=actor
            )
        except Exception as e:
            return {"error": str(e), "success": False}

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
        try:
            db = get_db()
            cids = db.extract_rule_candidates_from_quality_findings(
                task_id=task_id, min_occurrences=min_occurrences
            )
            return {"candidate_ids": cids, "count": len(cids)}
        except Exception as e:
            return {"error": str(e), "candidate_ids": [], "count": 0}
