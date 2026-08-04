"""查询面：符号/调用链/注释恢复/缺陷检测/覆盖率/模块图（原 [L1/L2/L3/L9/L10]）

拆分自 server/mcp_server.py（122-557 行区间），由 register(mcp) 注册。
"""

# [L1+L2+L3] 查询类工具（get_stats 属 L1；search_symbols/get_symbol 等
# [L3] 高级调用链与模块图工具（get_call_chain_down / export_module_graph 等）

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _get_daemon_client, _get_db_path_for_daemon, get_db


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_stats() -> dict:
        """获取代码知识图谱统计信息（文件数、函数数、调用关系数等）"""
        # Phase 4.8: 优先走 daemon client（Rust snapshot），回退 SQL
        client = _get_daemon_client()
        return client.get_stats(db_path=_get_db_path_for_daemon())

    @mcp.tool()
    def search_symbols(query: str, kind: str = "", limit: int = 20) -> list:
        """搜索符号（函数、类、结构体等）

        Args:
            query: 搜索关键词（符号名模糊匹配）
            kind: 按类型过滤（fn/method/class/struct/enum/trait/interface 等）
            limit: 返回数量限制
        """
        # Phase 4.8: 优先走 daemon client
        client = _get_daemon_client()
        return client.search_symbols(query, kind=kind or None, limit=limit,
                                      db_path=_get_db_path_for_daemon())

    @mcp.tool()
    def get_symbol(qualified_name: str) -> Optional[dict]:
        """获取符号的详细信息

        Args:
            qualified_name: 符号限定名（如 crate::module::function_name）
        """
        # Phase 4.8: 优先走 daemon client
        client = _get_daemon_client()
        return client.get_symbol(qualified_name, db_path=_get_db_path_for_daemon())

    @mcp.tool()
    def get_symbol_location(name: str, file_path: str = "") -> Optional[dict]:
        """获取符号的位置信息（文件、行号、列号）

        Args:
            name: 符号名
            file_path: 可选的文件路径（用于消除重名歧义）
        """
        client = _get_daemon_client()
        return client.get_symbol_location(
            name, file_path=file_path, db_path=_get_db_path_for_daemon()
        )

    @mcp.tool()
    def get_file_symbols(file_path: str) -> list:
        """获取文件中的所有符号

        Args:
            file_path: 文件路径（相对项目根目录）
        """
        client = _get_daemon_client()
        return client.get_file_symbols(
            file_path, db_path=_get_db_path_for_daemon()
        )

    @mcp.tool()
    def get_callers(callee_name: str, qualified_name: Optional[str] = None) -> list:
        """查询指定函数的所有调用者（谁调用了它）

        P28：大规模项目推荐传入 qualified_name 避免短名跨模块误匹配

        Args:
            callee_name: 被调用的函数名（简名）
            qualified_name: 可选，完整限定名（如 module::Class::method），
                           传入时精确匹配该符号，避免多个模块同名函数误匹配
        """
        # Phase 4.8: 优先走 daemon client
        client = _get_daemon_client()
        return client.get_callers(callee_name, qualified_name,
                                   db_path=_get_db_path_for_daemon())

    @mcp.tool()
    def get_callees(caller_name: str, qualified_name: Optional[str] = None) -> list:
        """查询指定函数调用了哪些函数（它调用了谁）

        P28：大规模项目推荐传入 qualified_name 避免短名跨模块误匹配

        Args:
            caller_name: 调用者函数名（简名）
            qualified_name: 可选，完整限定名（如 module::Class::method），
                           传入时精确匹配该符号，避免多个模块同名函数误匹配
        """
        # Phase 4.8: 优先走 daemon client
        client = _get_daemon_client()
        return client.get_callees(caller_name, qualified_name,
                                    db_path=_get_db_path_for_daemon())

    @mcp.tool()
    def get_symbol_history(qualified_name: str) -> list:
        """获取符号的版本历史（所有历史版本）

        Args:
            qualified_name: 符号限定名
        """
        db = get_db()
        return db.get_symbol_history(qualified_name)

    @mcp.tool()
    def get_file_history(file_path: str) -> list:
        """获取文件的版本历史

        Args:
            file_path: 文件路径（相对项目根目录）
        """
        db = get_db()
        return db.get_file_history(file_path)

    @mcp.tool()
    def get_recent_changes(since: str = "1d") -> dict:
        """获取近期变更的文件和符号

        Args:
            since: 时间范围（如 1h/1d/1w/2024-01-01）
        """
        db = get_db()
        return db.get_recent_changes(since)

    @mcp.tool()
    def get_topological_order(limit: int = 50) -> list:
        """获取按依赖拓扑排序的符号列表（被调用最多的在前）

        Args:
            limit: 返回数量限制
        """
        # Phase 4.8: 优先走 daemon client
        client = _get_daemon_client()
        return client.get_topological_order(limit=limit,
                                              db_path=_get_db_path_for_daemon())

    @mcp.tool()
    def get_impact(qualified_name: str, max_depth: int = 10) -> dict:
        """影响面分析：向上追踪所有调用该函数的上游函数

        Args:
            qualified_name: 起始函数的限定名
            max_depth: 最大追踪深度（默认 10）
        """
        # Phase 4.8: get_impact 仍走 SQL（daemon 暂不支持 get_call_chain_up）
        db = get_db()
        return db.get_call_chain_up(qualified_name, max_depth=max_depth)

    @mcp.tool()
    def get_call_chain_down(qualified_name: str, max_depth: int = 10) -> dict:
        """调用链向下：追踪该函数调用的所有下游函数

        Args:
            qualified_name: 起始函数的限定名
            max_depth: 最大追踪深度（默认 10）
        """
        # Phase 4.8: 优先走 daemon client
        client = _get_daemon_client()
        result = client.get_call_chain_down(qualified_name, max_depth=max_depth,
                                             db_path=_get_db_path_for_daemon())
        # daemon 返回 list，MCP 接口期望 dict（兼容旧格式）
        if isinstance(result, list):
            return {"chain": result, "edges": result}
        return result

    @mcp.tool()
    def get_top_callers(limit: int = 20, kind: str = "fn", module_filter: str = "") -> list:
        """获取被调用次数最多的函数排行

        Args:
            limit: 返回数量限制（默认 20）
            kind: 符号类型（默认 fn，可选 struct/enum/trait 等）
            module_filter: 模块过滤（前缀匹配）
        """
        db = get_db()
        return db.get_top_callers(limit=limit, kind=kind, module_filter=module_filter)

    @mcp.tool()
    def get_orphan_symbols(kind: str = "fn", module_filter: str = "", limit: int = 100) -> list:
        """获取未被调用的孤立符号

        Args:
            kind: 符号类型（默认 fn，可选 struct/enum/trait 等）
            module_filter: 模块过滤（前缀匹配）
            limit: 返回数量限制（默认 100）
        """
        db = get_db()
        return db.get_orphan_symbols(kind=kind, module_filter=module_filter, limit=limit)

    @mcp.tool()
    def get_deepest_functions(limit: int = 20, module_filter: str = "", kind: str = "fn") -> list:
        """获取调用深度最深的函数排行

        Args:
            limit: 返回数量限制（默认 20）
            module_filter: 模块过滤（前缀匹配）
            kind: 符号类型（默认 fn）
        """
        db = get_db()
        return db.get_deepest_functions(limit=limit, module_filter=module_filter, kind=kind)

    @mcp.tool()
    def get_module_call_stats(limit: int = 30) -> list:
        """获取模块间调用统计

        Args:
            limit: 返回数量限制（默认 30）
        """
        db = get_db()
        return db.get_module_call_stats(limit=limit)

    @mcp.tool()
    def detect_cycles(max_depth: int = 10) -> list:
        """检测循环调用

        Args:
            max_depth: 最大追踪深度（默认 10）

        Returns:
            检测到的循环列表，每个循环是一个函数名列表
        """
        # Phase 4.8: 优先走 daemon client
        client = _get_daemon_client()
        return client.detect_cycles(max_depth=max_depth,
                                     db_path=_get_db_path_for_daemon())

    @mcp.tool()
    def get_comment_from_version(spec: str) -> Optional[dict]:
        """从历史版本中获取注释（用于 git checkout 后恢复注释）

        Args:
            spec: 版本规格（格式: 文件路径:符号名@版本号 或 文件路径:行号）
        """
        db = get_db()
        return db.get_comment_from_version(spec)

    @mcp.tool()
    def restore_comment(spec: str, preview: bool = True) -> dict:
        """恢复函数注释（从历史版本）

        Args:
            spec: 版本规格（格式: 文件路径:符号名@版本号 或 文件路径:行号）
            preview: 预览模式（只显示不写入）
        """
        db = get_db()
        return db.restore_comment(spec, preview=preview)

    @mcp.tool()
    def get_issue_summary() -> dict:
        """获取缺陷检测汇总统计"""
        db = get_db()
        return db.get_issue_summary()

    @mcp.tool()
    def find_issues(issue_type: str = "", limit: int = 30) -> list:
        """查找代码缺陷（缺注释、硬编码、unwrap 等）

        Args:
            issue_type: 缺陷类型（missing_comment/todo_fixme/unwrap_call/expect_call/panic_macro/unsafe_block/hardcoded_path/hardcoded_url/magic_number 等）
            limit: 返回数量限制
        """
        db = get_db()
        # db 层方法名为 get_function_issues，参数 issue_filter 对应 MCP 的 issue_type
        return db.get_function_issues(issue_filter=issue_type or None, limit=limit)

    @mcp.tool()
    def get_semgrep_stats() -> dict:
        """获取 Semgrep 缺陷统计（按严重程度、语言、规则分组）"""
        db = get_db()
        return db.get_semgrep_stats()

    @mcp.tool()
    def get_semgrep_findings(severity: str = "", language: str = "",
                             rule_id: str = "", limit: int = 50) -> list:
        """查询 Semgrep 发现的缺陷

        Args:
            severity: 按严重程度过滤（ERROR/WARNING/INFO）
            language: 按语言过滤（rust/typescript/python/kotlin 等）
            rule_id: 按规则 ID 过滤（模糊匹配）
            limit: 返回数量限制
        """
        db = get_db()
        return db.get_semgrep_findings(
            severity=severity, language=language,
            rule_id=rule_id, limit=limit,
        )

    @mcp.tool()
    def run_semgrep_scan(config: str = "p/default",
                         languages: list = None,
                         timeout: int = 300) -> dict:
        """运行 Semgrep 扫描并将结果存入数据库 — 同步版本

        注意：对于大型代码库，请使用 semgrep_scan_async 提交后台 job,
        避免阻塞 MCP 请求。semgrep_scan_async 提交后可用 wait_for_job
        等待完成，结果通过 get_semgrep_findings / get_semgrep_stats 查询。

        Args:
            config: Semgrep 规则配置（默认 p/default，可选 p/security/p/best-practices 等）
            languages: 限制扫描的语言列表（如 ["rust", "typescript"]），为空则扫描所有支持的语言
            timeout: 扫描超时时间（秒，默认 300）
        """
        db = get_db()
        return db.run_semgrep_and_save(
            config=config,
            languages=languages,
            timeout=timeout,
        )

    @mcp.tool()
    def scan_semgrep_incremental(base_branch: str = "main",
                                 head: str = "HEAD",
                                 config: str = "p/default",
                                 languages: list = None,
                                 timeout: int = 300) -> dict:
        """增量 Semgrep 扫描：只扫描 git diff 变更文件并清理旧 findings

        A14 修复（2026-07-20）：旧实现 scan_type 硬编码 'full'，不清理变更文件
        的 stale findings。本工具调用 db.scan_semgrep_incremental()：
        - 通过 git diff --name-only 取 base_branch...head 的变更文件
        - 扫描变更文件，scan_type='incremental' 写入 semgrep_scans
        - 删除变更文件的旧 findings，避免重复计数
        - 每条 finding 关联 scan_id，支持审计追溯

        适用场景：PR 检查、CI 流水线、代码 review 前的快速缺陷检测。

        Args:
            base_branch: 基准分支（默认 main）
            head: 目标提交（默认 HEAD）
            config: Semgrep 规则配置（默认 p/default）
            languages: 限制扫描的语言列表
            timeout: 扫描超时时间（秒，默认 300）

        Returns:
            dict: {success, scan_type, changed_files, scanned_files,
                   saved_findings, total_findings, stale_file_ids}
        """
        db = get_db()
        return db.scan_semgrep_incremental(
            base_branch=base_branch,
            head=head,
            config=config,
            languages=languages,
            timeout=timeout,
        )

    @mcp.tool()
    def get_comment_coverage(group_by: str = "module") -> dict:
        """获取注释覆盖率统计

        Args:
            group_by: 分组方式：module（按模块）、file（按文件）、kind（按类型）
        """
        db = get_db()
        return db.get_comment_coverage(group_by=group_by)

    @mcp.tool()
    def get_uncommented_symbols(kind: str = "fn",
                                module_filter: str = "",
                                limit: int = 100) -> list:
        """获取未注释的符号列表

        Args:
            kind: 符号类型（默认 fn，可选 struct/enum/trait 等）
            module_filter: 模块过滤（前缀匹配）
            limit: 返回数量限制（默认 100）
        """
        db = get_db()
        return db.get_uncommented_symbols(
            kind=kind,
            module_filter=module_filter or None,
        )[:limit]

    @mcp.tool()
    def get_call_heatmap(group_by: str = "module", top_n: int = 20) -> list:
        """获取函数调用频率热力图数据

        Args:
            group_by: 分组方式（module 或 file）
            top_n: 返回数量限制（默认 20）
        """
        db = get_db()
        return db.get_call_heatmap(group_by=group_by, top_n=top_n)

    @mcp.tool()
    def get_test_coverage() -> dict:
        """获取测试覆盖率统计（test函数分布）"""
        db = get_db()
        return db.get_test_coverage()

    @mcp.tool()
    def export_module_graph(format: str = "mermaid") -> str:
        """导出模块依赖图

        Args:
            format: 输出格式（mermaid 或 dot）
        """
        db = get_db()
        return db.export_module_graph(format=format)

    @mcp.tool()
    def restore_all_comments(preview: bool = True,
                             file_filter: str = "") -> dict:
        """批量恢复所有有注释历史的函数注释

        Args:
            preview: 预览模式（只显示不写入）
            file_filter: 只恢复指定文件的注释（文件路径前缀匹配）
        """
        db = get_db()
        return db.restore_all_comments(
            preview=preview,
            file_filter=file_filter or None,
        )
