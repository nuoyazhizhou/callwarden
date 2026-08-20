"""摘要与演化分析面：代码摘要/RepoMap/覆盖率智能/所有权/影响/演化/缺陷知识库

拆分自 server/mcp_server.py（3051-3608 行区间），由 register(mcp) 注册。

H4B-C（T-1786590214634-9e740cdc-h4b-compat-read）：compatibility read HTTP cutover
- H0 capability registry（.trae-cn/evidence/http-daemon-capability-matrix.json）：
  本模块 31 个工具全部 backend=python_compat、daemon_rpc_method=none。
- dispatch.rs 无任何 summary.* / guardrail.* / defect.* 等 RPC 分支（DaemonStateExt
  trait 默认返回 method_not_found）。因此 HTTP 模式下禁止调用 _call_daemon_rpc
  指向不存在的 RPC——伪路由会在 HTTP 模式抛 method_not_found，违反 fail-closed 契约。
- 本模块工具在 HTTP 模式下 fail-closed：经 _http_unsupported() 返回结构化
  unsupported 错误，不直连本地 SQLite（不构造 CodeGraphDB，无 SQLite fallback）；
  非 HTTP（legacy）模式保持本地 get_db() 执行，公开方法语义不变。
- compat_route 扩展（把 python_compat 方法注册到 H3 compat worker）由
  h4b-registry-docs（...h4b-registry-docs）承接；本任务不触碰 Rust/compat_registry。
"""

# [L2] 代码摘要 + Repo Map 工具（generate_summary / get_summary / project_brief / repo_map）
# [L4] 演化智能工具（evolution_frequency / defect_correlation / hotspot_evolution 等）
# [L7] 安全护栏工具（guardrail_scan / guardrail_check_edit / guardrail_list_rules 等）
# [L9] 缺陷知识库与变更影响工具（defect_search / blast_radius / ask_codebase 等）
# [L10] 覆盖率智能与所有权分析工具（import_coverage / who_to_ask / get_ownership_map 等）

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _get_daemon_client, _get_db_path_for_daemon, get_db
from ...db import CodeGraphDB
from callwarden.server.daemon_client import (
    is_http_transport_enabled,
    route_worker_call,
)

# H4C-2 第二批（T-1786747295213-64204cce）：摘要/演化/护栏/缺陷组只读工具接入
# compat worker。注意：必须用顶层 `server.compat_registry` 导入，与
# compat_worker.py 保持同一模块单例（模块单例风险，见 tools_query.py L41-49 注释）。
from server.compat_registry import (  # noqa: E402
    SCOPE_WORKSPACE,
    CompatCallContext,
    register_compat_routes,
)

from ..daemon_client import route_rpc as _route


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def generate_summary(qualified_name: str, summary: str, model: str = "manual") -> dict:
        """为函数生成/保存摘要

        Args:
            qualified_name: 函数限定名
            summary: 摘要文本
            model: 生成模型标识

        Returns:
            保存结果
        """
        return _route('summary.generate', {"qualified_name": qualified_name, "summary": summary, "model": model}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_summary(qualified_name: str) -> Optional[dict]:
        """获取函数的当前摘要

        Args:
            qualified_name: 函数限定名

        Returns:
            摘要信息
        """
        return _route('get_summary', {"qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def project_brief() -> dict:
        """获取项目简报

        包含项目类型、入口、模块职责、热点函数、健康评分等。
        适合 Agent 首次接触项目时调用。

        Returns:
            项目简报字典
        """
        return _route('project_brief', {}, 'READ_ONLY')

    @mcp.tool()
    def repo_map(format: str = "text") -> str:
        """生成仓库模块依赖图

        Args:
            format: 输出格式（text 或 mermaid）

        Returns:
            依赖图内容
        """
        return _route('repo_map', {"format": format}, 'READ_ONLY')

    @mcp.tool()
    def import_coverage(file_path: str, format: str = "lcov") -> dict:
        """导入覆盖率报告

        Args:
            file_path: 覆盖率报告文件路径
            format: 格式（lcov 或 cobertura）

        Returns:
            导入统计
        """
        _res = _route('task.job_submit', {**{"file_path": file_path, "format": format}, "job_type": "coverage", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def get_coverage_for_symbol(qualified_name: str) -> Optional[dict]:
        """获取函数的代码覆盖率

        Args:
            qualified_name: 函数限定名

        Returns:
            覆盖率信息（总行数、覆盖行数、百分比、未覆盖行列表）
        """
        return _route('query.coverage_for_symbol', {"qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def find_uncovered_functions(module_filter: str = "", threshold: int = 50) -> list:
        """查找覆盖率低于阈值的函数

        Args:
            module_filter: 模块路径前缀过滤
            threshold: 覆盖率阈值百分比（默认 50）

        Returns:
            低覆盖率函数列表
        """
        return _route('find_uncovered_functions', {"module_filter": module_filter, "threshold": threshold}, 'READ_ONLY')

    @mcp.tool()
    def test_impact_selection(qualified_name: str) -> list:
        """测试影响选择：改了某函数后需要运行哪些测试

        Args:
            qualified_name: 被修改的函数限定名

        Returns:
            需要运行的测试函数列表
        """
        return _route('test_impact_selection', {"qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def who_to_ask(file_path: str) -> Optional[dict]:
        """查询文件负责人（谁最了解这个文件）

        综合 CODEOWNERS 和 git blame 信息。

        Args:
            file_path: 文件路径

        Returns:
            负责人信息（owner, source, confidence, last_commit_author）
        """
        return _route('who_to_ask', {"file_path": file_path}, 'READ_ONLY')

    @mcp.tool()
    def get_ownership_map(module_filter: str = "") -> list:
        """获取模块所有权映射

        Args:
            module_filter: 模块路径前缀过滤

        Returns:
            按模块分组的所有权映射
        """
        return _route('get_ownership_map', {"module_filter": module_filter}, 'READ_ONLY')

    @mcp.tool()
    def guardrail_scan(file_filter: str = "") -> list:
        """对指定文件运行安全规则扫描

        扫描 DB / API / Incident 三类可阻断规则，识别代码中的安全风险。

        Args:
            file_filter: 文件路径前缀过滤（为空则扫描全部文件）

        Returns:
            findings 列表，每项包含 finding_id / category / severity / file_path 等字段
        """
        return _route('guardrail_scan', {"file_filter": file_filter}, 'READ_ONLY')

    @mcp.tool()
    def guardrail_check_edit(file_path: str, proposed_change: str = "") -> dict:
        """编辑前阻断式检查

        在修改文件前调用，根据规则决定是否阻断本次编辑。

        Args:
            file_path: 目标文件路径
            proposed_change: 拟议变更内容（可选，用于上下文相关检查）

        Returns:
            {"decision": "block"/"warn"/"pass", "findings": [...], "message": "..."}
        """
        return _route('guardrail_check_edit', {"file_path": file_path, "proposed_change": proposed_change}, 'READ_ONLY')

    @mcp.tool()
    def guardrail_list_rules(category_filter: str = "") -> list:
        """列出已注册的安全规则

        Args:
            category_filter: 按类别过滤（如 db / api / incident）

        Returns:
            规则列表
        """
        return _route('guardrail_list_rules', {"category_filter": category_filter}, 'READ_ONLY')

    @mcp.tool()
    def guardrail_add_rule(category: str, pattern: str,
                           severity: str = "warn", action: str = "warn",
                           description: str = "") -> dict:
        """添加自定义安全规则

        Args:
            category: 规则类别（如 db / api / incident）
            pattern: 匹配模式（正则或关键字）
            severity: 严重程度（warn / error / block）
            action: 触发动作（warn / block）
            description: 规则描述

        Returns:
            添加结果
        """
        return _route('guardrail.add_rule', {"category": category, "pattern": pattern, "severity": severity, "action": action, "description": description}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def blast_radius(symbol_hash: str, depth: int = 3) -> dict:
        """计算变更影响半径

        以指定符号为起点，沿调用链向上游扩散，计算受影响的调用者数量与范围。

        Args:
            symbol_hash: 起始符号的内容哈希
            depth: 扩散深度（默认 3）

        Returns:
            影响半径报告
        """
        return _route('blast_radius', {"symbol_hash": symbol_hash, "depth": depth}, 'READ_ONLY')

    @mcp.tool()
    def ask_codebase(question: str, top_k: int = 5, include_callers: int = 2, include_callees: int = 1, max_tokens: int = 4000) -> dict:
        """RAG 管道：基于调用链增强的代码库问答上下文组装

        与普通语义搜索的差异：组装完整的 RAG 上下文，包含种子函数 + 调用方 + 被调用方 + 摘要。
        向量索引不可用时自动回退到关键词匹配。

        Args:
            question: 自然语言问题
            top_k: 种子函数数量
            include_callers: 每个种子包含的调用方数量
            include_callees: 每个种子包含的被调用方数量
            max_tokens: 上下文最大 token 数

        Returns:
            rag_context (完整 RAG 上下文文本) + context_blocks + estimated_tokens
        """
        return _route('ask_codebase', {"question": question, "top_k": top_k, "include_callers": include_callers, "include_callees": include_callees, "max_tokens": max_tokens}, 'READ_ONLY')

    @mcp.tool()
    def record_token_savings(operation: str, original_tokens: int, actual_tokens: int, agent_task_id: str = "", detail: str = "") -> dict:
        """记录一次操作的 token 节省（用于账本和优化分析）

        Args:
            operation: 操作类型（rag_context / call_chain_summary / semantic_search / comment_restore / blast_radius）
            original_tokens: 原始 token 数（无压缩时的估算值）
            actual_tokens: 实际使用的 token 数
            agent_task_id: 关联的任务 ID（可选）
            detail: 详情 JSON 字符串（可选）

        Returns:
            {"id": int, "tokens_saved": int, "savings_pct": float}
        """
        return _route('edit.record_token_savings', {"operation": operation, "original_tokens": original_tokens, "actual_tokens": actual_tokens, "agent_task_id": agent_task_id, "detail": detail}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_token_savings_report(time_window: str = "30d") -> dict:
        """获取 Token 节省报告（宣传利器 + 优化依据）

        Args:
            time_window: 时间窗口（7d / 30d / 90d / "" 全部）

        Returns:
            total_saved + total_operations + avg_savings_pct + by_operation + daily_trend + headline
        """
        return _route('get_token_savings_report', {"time_window": time_window}, 'READ_ONLY')

    @mcp.tool()
    def get_vulnerability_blast_radius(finding_id: int = 0, severity_filter: str = "", depth: int = 3) -> dict:
        """计算漏洞的爆炸半径（Semgrep 发现 × 调用链反向传播 = 安全影响面）

        全行业空白特性：将 Semgrep findings 与调用图结合，回答"这个漏洞能影响多少下游调用方"。
        - finding_id 指定单个漏洞（为 0 则扫描所有匹配 severity_filter 的漏洞）
        - severity_filter: ERROR/WARN/INFO（为空则不过滤）
        - depth: 调用图反向遍历深度（默认 3 层）

        返回：风险等级 + 每个漏洞的影响树 + 受影响符号汇总 + 高风险调用方
        """
        return _route('get_vulnerability_blast_radius', {"finding_id": finding_id, "severity_filter": severity_filter, "depth": depth}, 'READ_ONLY')

    @mcp.tool()
    def get_clone_aware_impact(qualified_name: str, depth: int = 3) -> dict:
        """克隆感知的变更影响分析（H11）

        在 blast_radius 基础上联动 clone_pairs：源符号的克隆代码变更也会影响相同调用方，
        因此影响半径应包含克隆符号的影响。

        Args:
            qualified_name: 源符号限定名
            depth: BFS 遍历深度（默认 3）

        返回：源符号信息 + 原始影响半径 + 克隆列表 + 每个克隆的影响半径 + 合并后影响总数
        """
        return _route('get_clone_aware_impact', {"qualified_name": qualified_name, "depth": depth}, 'READ_ONLY')

    @mcp.tool()
    def diff_to_symbol(diff_text: str) -> list:
        """将 git diff 映射到受影响符号

        解析 diff 文本，定位每个变更对应的符号（函数/方法/类）。

        Args:
            diff_text: git diff 文本内容

        Returns:
            受影响符号列表
        """
        return _route('query.diff_to_symbol', {"diff_text": diff_text}, 'READ_ONLY')

    @mcp.tool()
    def review_readiness(symbol_hash: str) -> dict:
        """审查就绪报告

        评估指定符号是否满足代码审查就绪条件（测试覆盖、注释完整、无遗留缺陷等）。

        Args:
            symbol_hash: 符号内容哈希

        Returns:
            审查就绪报告
        """
        return _route('review_readiness', {"symbol_hash": symbol_hash}, 'READ_ONLY')

    @mcp.tool()
    def cross_layer_impact(symbol_hash: str) -> dict:
        """跨层影响分析

        分析符号变更对其他层（如 API / DB / UI）的跨层影响。

        Args:
            symbol_hash: 符号内容哈希

        Returns:
            跨层影响报告
        """
        return _route('cross_layer_impact', {"symbol_hash": symbol_hash}, 'READ_ONLY')

    @mcp.tool()
    def evolution_frequency(qualified_name: str, time_window: str = "") -> dict:
        """函数变更频率分析

        统计指定函数在时间窗口内的变更次数、变更作者、关联 commit 等。

        Args:
            qualified_name: 函数限定名
            time_window: 时间窗口（如 30d / 90d / 1y，为空则全量）

        Returns:
            变更频率报告
        """
        return _route('evolution_frequency', {"qualified_name": qualified_name, "time_window": time_window}, 'READ_ONLY')

    @mcp.tool()
    def defect_correlation(symbol_hash: str, window_commits: int = 5) -> dict:
        """缺陷关联分析

        分析指定符号在最近 N 次 commit 内的变更与缺陷修复的关联性。

        Args:
            symbol_hash: 符号内容哈希
            window_commits: 关联窗口 commit 数（默认 5）

        Returns:
            缺陷关联报告

        W4-3（T-1786886251769-22b94ee8-sub-3）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.defect_correlation，
        经 snapshot query_db_path 访问主库，注入权威 workspace_instance_id
        做 workspace 隔离）；local/legacy 模式保留原路由语义（local 走本地
        db 回退，enterprise/auto 走 compat worker）。
        """
        return _route('query.defect_correlation', {"symbol_hash": symbol_hash, "window_commits": window_commits}, 'READ_ONLY')

    @mcp.tool()
    def hotspot_evolution(module_filter: str = "") -> list:
        """热点函数演化

        识别近期变更最频繁的热点函数，辅助聚焦审查与重构资源。

        Args:
            module_filter: 模块路径前缀过滤

        Returns:
            热点函数演化列表
        """
        return _route('hotspot_evolution', {"module_filter": module_filter}, 'READ_ONLY')

    @mcp.tool()
    def churn_analysis(module_filter: str = "", time_window: str = "90d") -> dict:
        """代码流失分析

        统计指定时间窗口内的代码流失量（新增/删除/修改行数）。

        Args:
            module_filter: 模块路径前缀过滤
            time_window: 时间窗口（默认 90d）

        Returns:
            代码流失报告

        W4-3（T-1786886251769-22b94ee8-sub-3）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.churn_analysis，
        经 snapshot query_db_path 访问主库，注入权威 workspace_instance_id
        做 workspace 隔离）；local/legacy 模式保留原路由语义（local 走本地
        db 回退，enterprise/auto 走 compat worker）。
        """
        return _route('query.churn_analysis', {"module_filter": module_filter, "time_window": time_window}, 'READ_ONLY')

    @mcp.tool()
    def defect_search(category: str = "", severity_filter: str = "") -> list:
        """缺陷模式搜索

        按类别/严重度搜索缺陷知识库中已积累的缺陷模式。

        Args:
            category: 类别过滤（前缀匹配，如 "sec" 匹配 "security"）
            severity_filter: 严重度过滤（精确匹配，如 error / warning / info）

        Returns:
            缺陷模式列表

        W4-3（T-1786886251769-22b94ee8-sub-3）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.defect_search，
        经 snapshot query_db_path 访问主库，注入权威 workspace_instance_id
        仅用于 ACL——defect_patterns 无 workspace_id 列，为全局视图，与
        Python db 层语义一致）；local/legacy 模式保留原路由语义（local 走
        本地 db 回退，enterprise/auto 走 compat worker）。
        """
        return _route('query.defect_search', {"category": category, "severity_filter": severity_filter}, 'READ_ONLY')

    @mcp.tool()
    def defect_suggest_fix(symbol_hash: str, finding_id: int = 0) -> dict:
        """修复建议

        基于缺陷知识库为指定符号/缺陷推荐修复方案。

        Args:
            symbol_hash: 符号内容哈希
            finding_id: 关联缺陷 ID（可选，默认 0）

        Returns:
            修复建议报告

        W4-3（T-1786886251769-22b94ee8-sub-3）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.defect_suggest_fix，
        经 snapshot query_db_path 访问主库，注入权威 workspace_instance_id
        做 workspace 隔离）；local/legacy 模式保留原路由语义（local 走本地
        db 回退，enterprise/auto 走 compat worker）。
        """
        return _route('query.defect_suggest_fix', {"symbol_hash": symbol_hash, "finding_id": finding_id}, 'READ_ONLY')

    @mcp.tool()
    def defect_learn(fix_commit_hash: str) -> dict:
        """从修复中学习

        解析指定修复 commit，提取缺陷模式并入库，供后续推荐使用。

        Args:
            fix_commit_hash: 修复 commit 的哈希值

        Returns:
            学习结果报告
        """
        return _route('defect_learn', {"fix_commit_hash": fix_commit_hash}, 'READ_ONLY')

    @mcp.tool()
    def defect_stats() -> dict:
        """缺陷知识库统计

        返回缺陷知识库的整体统计信息（模式总数、分类分布、严重度分布等）。

        W2-3（T-1786840097331-fd01a3f8）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native defect.stats，经 snapshot
        query_db_path 访问主库 defect_patterns/defect_fixes，注入权威
        workspace_instance_id 仅用于 ACL——defect 两表无 workspace_id 列，
        统计为全局视图，与 Python db 层语义一致）；local/legacy 模式保留原
        路由语义（local 走本地 db 回退，enterprise/auto 走 compat worker）。

        Returns:
            统计字典
        """
        return _route('defect.stats', {}, 'READ_ONLY')


# ============================================================
# H4C-2 第二批（T-1786747295213-64204cce）：摘要/演化/护栏/缺陷组只读工具 worker handler
# ============================================================
# 接入说明（用户三项决策，见任务描述）：
# - handler 定义在工具模块内，由 compat_worker.handle_frame 通用派发按 registry
#   分发；本模块被 worker 装配 import 后模块级注册随之执行；
# - 轻量只读绑定：object.__new__(CodeGraphDB) 绕过 __init__（含 PRAGMA WAL /
#   schema 迁移 / workspace 注册等写副作用），注入 ctx.conn（worker 的
#   mode=ro 只读连接）+ ctx.workspace_id 后复用 db 层查询方法；
# - 写语义工具不接入（fail-closed）：generate_summary（db.generate_summary 保存
#   摘要，矩阵标注 read_only 属异常已修正为 governance_write）、import_coverage /
#   guardrail_add_rule / record_token_savings（governance_write，不接入 worker）。
_SUMMARY_COMPAT_SCOPE = SCOPE_WORKSPACE  # 矩阵 workspace_scoped


def _bind_readonly_db(ctx: CompatCallContext) -> CodeGraphDB:
    """轻量只读绑定：绕过 CodeGraphDB.__init__，注入 worker 只读连接与显式 workspace。

    与 tools_query.py 同款：ctx.conn 由 compat_worker 用 `file:{db_path}?mode=ro`
    打开（read_only 契约）；active_workspace 注入 ctx.workspace_id，db 层查询基于
    `_get_active_workspace_id()` 过滤；workspace_root 从 workspaces 表解析
    （who_to_ask / parse_codeowners 等文件路径依赖）。
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


def _h_get_summary(ctx: CompatCallContext) -> Any:
    """worker handler：获取函数当前摘要（只读）"""
    return _bind_readonly_db(ctx).get_summary(ctx.params.get("qualified_name", ""))


def _h_project_brief(ctx: CompatCallContext) -> Any:
    """worker handler：项目简报（只读）"""
    return _bind_readonly_db(ctx).project_brief()


def _h_repo_map(ctx: CompatCallContext) -> Any:
    """worker handler：仓库模块依赖图（只读）"""
    return _bind_readonly_db(ctx).repo_map(format=ctx.params.get("format", "text"))


def _h_get_coverage_for_symbol(ctx: CompatCallContext) -> Any:
    """worker handler：函数代码覆盖率（只读）"""
    return _bind_readonly_db(ctx).get_coverage_for_symbol(ctx.params.get("qualified_name", ""))


def _h_find_uncovered_functions(ctx: CompatCallContext) -> Any:
    """worker handler：低覆盖率函数（只读）"""
    return _bind_readonly_db(ctx).find_uncovered_functions(
        module_filter=ctx.params.get("module_filter", ""),
        threshold=ctx.params.get("threshold", 50),
    )


def _h_test_impact_selection(ctx: CompatCallContext) -> Any:
    """worker handler：测试影响选择（只读）"""
    return _bind_readonly_db(ctx).test_impact_selection(ctx.params.get("qualified_name", ""))


def _h_who_to_ask(ctx: CompatCallContext) -> Any:
    """worker handler：文件负责人查询（只读）"""
    return _bind_readonly_db(ctx).who_to_ask(file_path=ctx.params.get("file_path", ""))


def _h_get_ownership_map(ctx: CompatCallContext) -> Any:
    """worker handler：模块所有权映射（只读）"""
    return _bind_readonly_db(ctx).get_ownership_map(
        module_filter=ctx.params.get("module_filter", "")
    )


def _h_guardrail_scan(ctx: CompatCallContext) -> Any:
    """worker handler：安全规则扫描（只读）"""
    return _bind_readonly_db(ctx).scan_guardrails(ctx.params.get("file_filter", ""))


def _h_guardrail_check_edit(ctx: CompatCallContext) -> Any:
    """worker handler：编辑前阻断式检查（只读）"""
    return _bind_readonly_db(ctx).check_before_edit(
        ctx.params.get("file_path", ""),
        ctx.params.get("proposed_change", ""),
    )


def _h_guardrail_list_rules(ctx: CompatCallContext) -> Any:
    """worker handler：列出安全规则（只读）"""
    return _bind_readonly_db(ctx).guardrail_list_rules(ctx.params.get("category_filter", ""))


def _h_blast_radius(ctx: CompatCallContext) -> Any:
    """worker handler：变更影响半径（只读）"""
    return _bind_readonly_db(ctx).blast_radius(
        ctx.params.get("symbol_hash", ""),
        ctx.params.get("depth", 3),
    )


def _h_ask_codebase(ctx: CompatCallContext) -> Any:
    """worker handler：RAG 代码库问答上下文（只读）"""
    return _bind_readonly_db(ctx).ask_codebase(
        ctx.params.get("question", ""),
        ctx.params.get("top_k", 5),
        ctx.params.get("include_callers", 2),
        ctx.params.get("include_callees", 1),
        ctx.params.get("max_tokens", 4000),
    )


def _h_get_token_savings_report(ctx: CompatCallContext) -> Any:
    """worker handler：Token 节省报告（只读）"""
    return _bind_readonly_db(ctx).get_token_savings_report(ctx.params.get("time_window", "30d"))


def _h_get_vulnerability_blast_radius(ctx: CompatCallContext) -> Any:
    """worker handler：漏洞爆炸半径（只读）"""
    return _bind_readonly_db(ctx).get_vulnerability_blast_radius(
        ctx.params.get("finding_id", 0),
        ctx.params.get("severity_filter", ""),
        ctx.params.get("depth", 3),
    )


def _h_get_clone_aware_impact(ctx: CompatCallContext) -> Any:
    """worker handler：克隆感知变更影响（只读）"""
    return _bind_readonly_db(ctx).get_clone_aware_impact(
        ctx.params.get("qualified_name", ""),
        ctx.params.get("depth", 3),
    )


def _h_review_readiness(ctx: CompatCallContext) -> Any:
    """worker handler：审查就绪报告（只读）"""
    return _bind_readonly_db(ctx).review_readiness_report(ctx.params.get("symbol_hash", ""))


def _h_cross_layer_impact(ctx: CompatCallContext) -> Any:
    """worker handler：跨层影响分析（只读）"""
    return _bind_readonly_db(ctx).cross_layer_impact(ctx.params.get("symbol_hash", ""))


def _h_evolution_frequency(ctx: CompatCallContext) -> Any:
    """worker handler：函数变更频率分析（只读）"""
    return _bind_readonly_db(ctx).function_change_frequency(
        ctx.params.get("qualified_name", ""),
        ctx.params.get("time_window", ""),
    )


def _h_hotspot_evolution(ctx: CompatCallContext) -> Any:
    """worker handler：热点函数演化（只读）"""
    return _bind_readonly_db(ctx).hotspot_evolution(ctx.params.get("module_filter", ""))


def _h_defect_learn(ctx: CompatCallContext) -> Any:
    """worker handler：从修复中学习（只读）"""
    return _bind_readonly_db(ctx).learn_defect_from_fix(ctx.params.get("fix_commit_hash", ""))


# 摘要/演化/护栏/缺陷组只读白名单（20 个）：跳过 generate_summary（写语义，
# fail-closed）与 import_coverage / guardrail_add_rule / record_token_savings
# （governance_write，不接入 worker）。defect_stats 已 W2-3 迁移 rust_native
# （T-1786840097331-fd01a3f8），从本白名单移除（原 27 项）；
# get_coverage_for_symbol / diff_to_symbol 已 W4-2 迁移 rust_native
# （T-1786886251769-22b94ee8-sub-2），从本白名单移除（原 26 项）；
# defect_correlation / churn_analysis / defect_search / defect_suggest_fix
# 已 W4-3 迁移 rust_native（T-1786886251769-22b94ee8-sub-3），从本白名单移除
# （原 24 项）；defect_learn 为写面（INSERT defect_fixes/defect_patterns），
# 保持 python_compat（W4-3 决策，见 ledger §9.24）。
# review_readiness 依赖 blast_radius 与 cross_layer_impact（均未迁移），
# 保持 python_compat（W4-2 决策，见 ledger §9.23）。
_SUMMARY_READ_ONLY_METHODS: Dict[str, Any] = {
    "get_summary": _h_get_summary,
    "project_brief": _h_project_brief,
    "repo_map": _h_repo_map,
    "find_uncovered_functions": _h_find_uncovered_functions,
    "test_impact_selection": _h_test_impact_selection,
    "who_to_ask": _h_who_to_ask,
    "get_ownership_map": _h_get_ownership_map,
    "guardrail_scan": _h_guardrail_scan,
    "guardrail_check_edit": _h_guardrail_check_edit,
    "guardrail_list_rules": _h_guardrail_list_rules,
    "blast_radius": _h_blast_radius,
    "ask_codebase": _h_ask_codebase,
    "get_token_savings_report": _h_get_token_savings_report,
    "get_vulnerability_blast_radius": _h_get_vulnerability_blast_radius,
    "get_clone_aware_impact": _h_get_clone_aware_impact,
    "review_readiness": _h_review_readiness,
    "cross_layer_impact": _h_cross_layer_impact,
    "evolution_frequency": _h_evolution_frequency,
    "hotspot_evolution": _h_hotspot_evolution,
    "defect_learn": _h_defect_learn,
}

# 模块级注册：worker 装配 import 本模块时执行，注册到 compat_registry 单例并
# 同步 RUST_COMPAT_ROUTE（Rust 侧 http_server.rs 白名单在步骤#2 同步）。
register_compat_routes(
    _SUMMARY_READ_ONLY_METHODS,
    workspace_scope=_SUMMARY_COMPAT_SCOPE,
    description="H4C-2 第二批摘要/演化/护栏/缺陷组只读工具（20 个，T-1786747295213-64204cce 步骤#0；defect_stats 已 W2-3 迁移 rust_native；get_coverage_for_symbol / diff_to_symbol 已 W4-2 迁移 rust_native，T-1786886251769-22b94ee8-sub-2；defect_correlation / churn_analysis / defect_search / defect_suggest_fix 已 W4-3 迁移 rust_native，T-1786886251769-22b94ee8-sub-3）",
)
