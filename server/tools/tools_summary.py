"""摘要与演化分析面：代码摘要/RepoMap/覆盖率智能/所有权/影响/演化/缺陷知识库

拆分自 server/mcp_server.py（3051-3608 行区间），由 register(mcp) 注册。
"""

# [L2] 代码摘要 + Repo Map 工具（generate_summary / get_summary / project_brief / repo_map）
# [L4] 演化智能工具（evolution_frequency / defect_correlation / hotspot_evolution 等）
# [L7] 安全护栏工具（guardrail_scan / guardrail_check_edit / guardrail_list_rules 等）
# [L9] 缺陷知识库与变更影响工具（defect_search / blast_radius / ask_codebase 等）
# [L10] 覆盖率智能与所有权分析工具（import_coverage / who_to_ask / get_ownership_map 等）

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import get_db


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
        db = get_db()
        return db.generate_summary(qualified_name=qualified_name, summary=summary, model=model)

    @mcp.tool()
    def get_summary(qualified_name: str) -> Optional[dict]:
        """获取函数的当前摘要

        Args:
            qualified_name: 函数限定名

        Returns:
            摘要信息
        """
        db = get_db()
        return db.get_summary(qualified_name=qualified_name)

    @mcp.tool()
    def project_brief() -> dict:
        """获取项目简报

        包含项目类型、入口、模块职责、热点函数、健康评分等。
        适合 Agent 首次接触项目时调用。

        Returns:
            项目简报字典
        """
        db = get_db()
        return db.project_brief()

    @mcp.tool()
    def repo_map(format: str = "text") -> str:
        """生成仓库模块依赖图

        Args:
            format: 输出格式（text 或 mermaid）

        Returns:
            依赖图内容
        """
        db = get_db()
        return db.repo_map(format=format)

    @mcp.tool()
    def import_coverage(file_path: str, format: str = "lcov") -> dict:
        """导入覆盖率报告

        Args:
            file_path: 覆盖率报告文件路径
            format: 格式（lcov 或 cobertura）

        Returns:
            导入统计
        """
        db = get_db()
        if format == "cobertura":
            return db.import_cobertura(file_path)
        return db.import_lcov(file_path)

    @mcp.tool()
    def get_coverage_for_symbol(qualified_name: str) -> Optional[dict]:
        """获取函数的代码覆盖率

        Args:
            qualified_name: 函数限定名

        Returns:
            覆盖率信息（总行数、覆盖行数、百分比、未覆盖行列表）
        """
        db = get_db()
        return db.get_coverage_for_symbol(qualified_name=qualified_name)

    @mcp.tool()
    def find_uncovered_functions(module_filter: str = "", threshold: int = 50) -> list:
        """查找覆盖率低于阈值的函数

        Args:
            module_filter: 模块路径前缀过滤
            threshold: 覆盖率阈值百分比（默认 50）

        Returns:
            低覆盖率函数列表
        """
        db = get_db()
        return db.find_uncovered_functions(module_filter=module_filter, threshold=threshold)

    @mcp.tool()
    def test_impact_selection(qualified_name: str) -> list:
        """测试影响选择：改了某函数后需要运行哪些测试

        Args:
            qualified_name: 被修改的函数限定名

        Returns:
            需要运行的测试函数列表
        """
        db = get_db()
        return db.test_impact_selection(qualified_name=qualified_name)

    @mcp.tool()
    def who_to_ask(file_path: str) -> Optional[dict]:
        """查询文件负责人（谁最了解这个文件）

        综合 CODEOWNERS 和 git blame 信息。

        Args:
            file_path: 文件路径

        Returns:
            负责人信息（owner, source, confidence, last_commit_author）
        """
        db = get_db()
        return db.who_to_ask(file_path=file_path)

    @mcp.tool()
    def get_ownership_map(module_filter: str = "") -> list:
        """获取模块所有权映射

        Args:
            module_filter: 模块路径前缀过滤

        Returns:
            按模块分组的所有权映射
        """
        db = get_db()
        return db.get_ownership_map(module_filter=module_filter)

    @mcp.tool()
    def guardrail_scan(file_filter: str = "") -> list:
        """对指定文件运行安全规则扫描

        扫描 DB / API / Incident 三类可阻断规则，识别代码中的安全风险。

        Args:
            file_filter: 文件路径前缀过滤（为空则扫描全部文件）

        Returns:
            findings 列表，每项包含 finding_id / category / severity / file_path 等字段
        """
        try:
            db = get_db()
            return db.scan_guardrails(file_filter)
        except Exception as e:
            return [{"error": str(e)}]

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
        try:
            db = get_db()
            return db.check_before_edit(file_path, proposed_change)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def guardrail_list_rules(category_filter: str = "") -> list:
        """列出已注册的安全规则

        Args:
            category_filter: 按类别过滤（如 db / api / incident）

        Returns:
            规则列表
        """
        try:
            db = get_db()
            return db.guardrail_list_rules(category_filter)
        except Exception as e:
            return [{"error": str(e)}]

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
        try:
            db = get_db()
            return db.guardrail_add_rule(category, pattern, severity, action, description)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.blast_radius(symbol_hash, depth)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.ask_codebase(question, top_k, include_callers, include_callees, max_tokens)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            import json as _json
            db = get_db()
            detail_dict = _json.loads(detail) if detail else None
            return db.record_token_savings(operation, original_tokens, actual_tokens, agent_task_id, detail_dict)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_token_savings_report(time_window: str = "30d") -> dict:
        """获取 Token 节省报告（宣传利器 + 优化依据）

        Args:
            time_window: 时间窗口（7d / 30d / 90d / "" 全部）

        Returns:
            total_saved + total_operations + avg_savings_pct + by_operation + daily_trend + headline
        """
        try:
            db = get_db()
            return db.get_token_savings_report(time_window)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_vulnerability_blast_radius(finding_id: int = 0, severity_filter: str = "", depth: int = 3) -> dict:
        """计算漏洞的爆炸半径（Semgrep 发现 × 调用链反向传播 = 安全影响面）

        全行业空白特性：将 Semgrep findings 与调用图结合，回答"这个漏洞能影响多少下游调用方"。
        - finding_id 指定单个漏洞（为 0 则扫描所有匹配 severity_filter 的漏洞）
        - severity_filter: ERROR/WARN/INFO（为空则不过滤）
        - depth: 调用图反向遍历深度（默认 3 层）

        返回：风险等级 + 每个漏洞的影响树 + 受影响符号汇总 + 高风险调用方
        """
        try:
            db = get_db()
            return db.get_vulnerability_blast_radius(finding_id, severity_filter, depth)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.get_clone_aware_impact(qualified_name, depth)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def diff_to_symbol(diff_text: str) -> list:
        """将 git diff 映射到受影响符号

        解析 diff 文本，定位每个变更对应的符号（函数/方法/类）。

        Args:
            diff_text: git diff 文本内容

        Returns:
            受影响符号列表
        """
        try:
            db = get_db()
            return db.diff_to_symbol(diff_text)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def review_readiness(symbol_hash: str) -> dict:
        """审查就绪报告

        评估指定符号是否满足代码审查就绪条件（测试覆盖、注释完整、无遗留缺陷等）。

        Args:
            symbol_hash: 符号内容哈希

        Returns:
            审查就绪报告
        """
        try:
            db = get_db()
            return db.review_readiness_report(symbol_hash)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def cross_layer_impact(symbol_hash: str) -> dict:
        """跨层影响分析

        分析符号变更对其他层（如 API / DB / UI）的跨层影响。

        Args:
            symbol_hash: 符号内容哈希

        Returns:
            跨层影响报告
        """
        try:
            db = get_db()
            return db.cross_layer_impact(symbol_hash)
        except Exception as e:
            return {"error": str(e)}

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
        try:
            db = get_db()
            return db.function_change_frequency(qualified_name, time_window)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def defect_correlation(symbol_hash: str, window_commits: int = 5) -> dict:
        """缺陷关联分析

        分析指定符号在最近 N 次 commit 内的变更与缺陷修复的关联性。

        Args:
            symbol_hash: 符号内容哈希
            window_commits: 关联窗口 commit 数（默认 5）

        Returns:
            缺陷关联报告
        """
        try:
            db = get_db()
            return db.defect_correlation(symbol_hash, window_commits)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def hotspot_evolution(module_filter: str = "") -> list:
        """热点函数演化

        识别近期变更最频繁的热点函数，辅助聚焦审查与重构资源。

        Args:
            module_filter: 模块路径前缀过滤

        Returns:
            热点函数演化列表
        """
        try:
            db = get_db()
            return db.hotspot_evolution(module_filter)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def churn_analysis(module_filter: str = "", time_window: str = "90d") -> dict:
        """代码流失分析

        统计指定时间窗口内的代码流失量（新增/删除/修改行数）。

        Args:
            module_filter: 模块路径前缀过滤
            time_window: 时间窗口（默认 90d）

        Returns:
            代码流失报告
        """
        try:
            db = get_db()
            return db.churn_analysis(module_filter, time_window)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def defect_search(category: str = "", severity_filter: str = "") -> list:
        """缺陷模式搜索

        按类别/严重度搜索缺陷知识库中已积累的缺陷模式。

        Args:
            category: 类别过滤（前缀匹配，如 "sec" 匹配 "security"）
            severity_filter: 严重度过滤（精确匹配，如 error / warning / info）

        Returns:
            缺陷模式列表
        """
        try:
            db = get_db()
            return db.defect_pattern_search(category, severity_filter)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def defect_suggest_fix(symbol_hash: str, finding_id: int = 0) -> dict:
        """修复建议

        基于缺陷知识库为指定符号/缺陷推荐修复方案。

        Args:
            symbol_hash: 符号内容哈希
            finding_id: 关联缺陷 ID（可选，默认 0）

        Returns:
            修复建议报告
        """
        try:
            db = get_db()
            return db.suggest_fix(symbol_hash, finding_id)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def defect_learn(fix_commit_hash: str) -> dict:
        """从修复中学习

        解析指定修复 commit，提取缺陷模式并入库，供后续推荐使用。

        Args:
            fix_commit_hash: 修复 commit 的哈希值

        Returns:
            学习结果报告
        """
        try:
            db = get_db()
            return db.learn_defect_from_fix(fix_commit_hash)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def defect_stats() -> dict:
        """缺陷知识库统计

        返回缺陷知识库的整体统计信息（模式总数、分类分布、严重度分布等）。

        Returns:
            统计字典
        """
        try:
            db = get_db()
            return db.defect_stats()
        except Exception as e:
            return {"error": str(e)}
