"""
mcp_server.py
=============

代码知识图谱 MCP 服务器。

提供 MCP 工具接口，支持多容器共享调用（通过共享数据库文件）。
部署方式：在宿主机安装一次，所有容器通过 $HOME 共享路径调用。
"""

import os
import sys
from typing import Any, Dict, Optional

# 确保可以导入 callwarden 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from mcp.server.fastmcp import FastMCP
    HAS_FASTMCP = True
except ImportError:
    HAS_FASTMCP = False

from ..db import CodeGraphDB
from ..config import PROJECT_ROOT, get_project_db_path
from ..i18n import t


_db_instance: Optional[CodeGraphDB] = None


def _get_daemon_client():
    """获取 DaemonClient 单例（Phase 4.8: 高频查询走 daemon client）。

    延迟导入避免循环依赖。
    """
    from callwarden.server.daemon_client import get_daemon_client
    return get_daemon_client()


def _get_db_path_for_daemon() -> str:
    """获取当前 workspace 的 db_path（用于 daemon 自动发布 snapshot）。"""
    db = get_db()
    return get_project_db_path(db.workspace_root)


def get_db(workspace: Optional[str] = None) -> CodeGraphDB:
    """获取数据库单例（MCP 服务是长连接，复用连接）

    Args:
        workspace: 工作区路径或名称，为空则使用默认/活动工作区
    """
    global _db_instance
    if _db_instance is None:
        if workspace and os.path.isdir(workspace):
            _db_instance = CodeGraphDB(workspace_root=workspace)
        else:
            _db_instance = CodeGraphDB()
    elif workspace:
        # 如果指定了工作区且与当前不同，切换工作区
        current_root = _db_instance.workspace_root
        if os.path.isdir(workspace):
            ws_path = os.path.abspath(workspace)
            if os.path.abspath(current_root) != ws_path:
                _db_instance.close()
                _db_instance = CodeGraphDB(workspace_root=workspace)
        else:
            # 按名称切换
            active = _db_instance.get_active_workspace()
            if not active or active.get("name") != workspace:
                _db_instance.set_active_workspace(workspace)
    return _db_instance


def create_mcp_server():
    """创建 MCP 服务器实例"""
    if not HAS_FASTMCP:
        print(t("cli.messages.mcp_server_fastmcp_not_installed"), file=sys.stderr)
        sys.exit(1)

    mcp = FastMCP("callwarden", dependencies=["callwarden"])

    # =================================================================
    # Call Warden MCP 工具 — 12 大类索引（C8 Step #6）
    # =================================================================
    #
    # 工具命名前缀约定：
    #   get_X      — 查询单个对象详情
    #   list_X     — 列表查询
    #   search_X   — 模糊搜索
    #   find_X     — 条件查找
    #   create_X / delete_X / update_X — CRUD 写操作
    #   import_X / export_X — 导入导出
    #   detect_X / analyze_X — 分析类
    #   其余动词前缀（task_ / gc_ / rule_ / audit_ / lsp_ / guardrail_ /
    #   file_ / defect_ / embed_ 等）作为模块前缀保留
    #
    # 12 大类分组（按 .cli_audit.md §2 设计，详细审计见 .mcp_audit.md）：
    #
    #   [1]  Workspace & Database       — 工作区管理、数据库刷新、状态概览、分支感知
    #   [2]  Query & Search             — 符号查询、搜索、文件读取、语义搜索、摘要
    #   [3]  Call Chain Analysis        — 调用链、拓扑、循环、孤儿、模块图、热力图
    #   [4]  Code Health & Metrics      — 复杂度、耦合、度量、健康检查、演化、热点、流失
    #   [5]  Task Orchestration         — 任务创建/认领/上报/回滚/审批/关闭、capture-diff
    #   [6]  Agent Rule Memory          — 规则候选/审核/生效/同步/提取/清理
    #   [7]  Audit & Bootstrap          — 审计链验证、密钥轮换、自举健康、检查门禁、安全护栏
    #   [8]  Git Integration            — git 历史、commit、变更、blame、符号历史
    #   [9]  Semgrep & Defects          — Semgrep 扫描、缺陷检测、缺陷知识库、影响半径
    #   [10] Coverage & Ownership       — 注释覆盖、测试覆盖、CODEOWNERS、所有权映射
    #   [11] GC                         — 归档、恢复、清理、策略、备份、审计
    #   [12] Diagnostics                — clone 检测、LSP、安全编辑、跨仓库分析
    #
    # =================================================================

    # ----------------------------------------------------------------
    # [L1+L2+L3] 查询类工具（get_stats 属 L1；search_symbols/get_symbol 等
    #             属 L2；get_topological_order 属 L3）
    # ----------------------------------------------------------------

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
        db = get_db()
        return db.get_symbol_location(name, file_path=file_path or None)

    @mcp.tool()
    def get_file_symbols(file_path: str) -> list:
        """获取文件中的所有符号

        Args:
            file_path: 文件路径（相对项目根目录）
        """
        db = get_db()
        return db.get_file_symbols(file_path)

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

    # ----------------------------------------------------------------
    # [L3] 高级调用链分析工具（get_impact / get_call_chain_down / detect_cycles 等）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L10] 注释恢复工具（get_comment_from_version / restore_comment 等）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L9] 缺陷检测工具（find_issues / get_semgrep_* / run_semgrep_scan 等）
    # ----------------------------------------------------------------

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
        return db.find_issues(issue_type=issue_type or None, limit=limit)

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
        """运行 Semgrep 扫描并将结果存入数据库

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

    # ----------------------------------------------------------------
    # [L10] 覆盖率分析工具（get_comment_coverage / get_test_coverage 等）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L3+L10] 模块图 & 批量工具（export_module_graph 属 L3；restore_all_comments 属 L10）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L1] 构建 & 刷新工具（build_graph / refresh_file）
    # ----------------------------------------------------------------

    @mcp.tool()
    def build_graph() -> bool:
        """完整构建代码知识图谱（全量扫描）"""
        db = get_db()
        db.build_full_graph()
        return True

    @mcp.tool()
    def refresh_file(file_path: str) -> bool:
        """刷新单个文件（增量更新）

        Args:
            file_path: 文件路径（相对或绝对路径）
        """
        db = get_db()
        db.refresh_file(file_path)
        return True

    # ----------------------------------------------------------------
    # [L1] 工作区管理工具（list_workspaces / register_workspace / set_active_workspace 等）
    # ----------------------------------------------------------------

    @mcp.tool()
    def list_workspaces() -> list:
        """列出所有工作区"""
        db = get_db()
        return db.list_workspaces()

    @mcp.tool()
    def register_workspace(name: str, root_path: str, description: str = "") -> int:
        """注册新工作区

        Args:
            name: 工作区名称（唯一）
            root_path: 工作区根目录绝对路径
            description: 描述
        """
        db = get_db()
        return db.register_workspace(name, root_path, description)

    @mcp.tool()
    def set_active_workspace(workspace_id_or_name: str) -> bool:
        """设置活动工作区

        Args:
            workspace_id_or_name: 工作区 ID（数字字符串）或名称
        """
        db = get_db()
        # 尝试转换为 int（ID）
        try:
            ws_id = int(workspace_id_or_name)
            return db.set_active_workspace(ws_id)
        except ValueError:
            return db.set_active_workspace(workspace_id_or_name)

    @mcp.tool()
    def delete_workspace(workspace_id_or_name: str) -> bool:
        """删除工作区（级联删除所有实例和版本）

        Args:
            workspace_id_or_name: 工作区 ID（数字字符串）或名称
        """
        db = get_db()
        # 尝试转换为 int（ID）
        try:
            ws_id = int(workspace_id_or_name)
            return db.delete_workspace(ws_id)
        except ValueError:
            return db.delete_workspace(workspace_id_or_name)

    @mcp.tool()
    def get_active_workspace() -> Optional[dict]:
        """获取当前活动工作区信息"""
        db = get_db()
        return db.get_active_workspace()

    # ----------------------------------------------------------------
    # [L2] 文件操作工具（Agent 通过 MCP 读取代码，替代内置 Read/Grep 工具）
    # ----------------------------------------------------------------

    @mcp.tool()
    def file_read(file_path: str, offset: int = 0, limit: int = 200) -> Optional[dict]:
        """读取文件内容

        Agent 通过此工具读取文件，替代 IDE 内置 Read 工具。
        支持行号偏移和行数限制，避免一次性返回过大内容。

        Args:
            file_path: 文件路径（相对工作区根目录或绝对路径）
            offset: 起始行号（从 0 开始，默认 0）
            limit: 读取行数（默认 200）

        Returns:
            dict: {path, total_lines, offset, limit, content}，文件不存在返回 None
        """
        db = get_db()
        ws_root = db.workspace_root

        # 解析路径
        if os.path.isabs(file_path):
            abs_path = file_path
        else:
            abs_path = os.path.join(ws_root, file_path)

        # 安全检查：必须在工作区内
        abs_path = os.path.abspath(abs_path)
        if not abs_path.startswith(os.path.abspath(ws_root)):
            return {"error": "file path outside workspace"}

        if not os.path.isfile(abs_path):
            return None

        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            total = len(lines)
            end = min(offset + limit, total)
            content = "".join(lines[offset:end])
            return {
                "path": abs_path,
                "total_lines": total,
                "offset": offset,
                "limit": limit,
                "content": content,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def file_grep(pattern: str, path: str = "", glob: str = "", output_mode: str = "files_with_matches", head_limit: int = 50) -> dict:
        """在工作区内搜索文件内容（ripgrep 风格）

        Agent 通过此工具搜索代码内容，替代 IDE 内置 Grep 工具。
        支持正则表达式、glob 过滤、多种输出模式。

        Args:
            pattern: 搜索模式（支持正则表达式）
            path: 搜索起始路径（相对或绝对，默认为工作区根）
            glob: 文件 glob 过滤（如 "*.py"、"**/*.ts"）
            output_mode: 输出模式（files_with_matches / content / count）
            head_limit: 最大返回结果数（默认 50）

        Returns:
            dict: {results, count}
        """
        import re

        db = get_db()
        ws_root = db.workspace_root

        # 解析路径
        if path and os.path.isabs(path):
            search_root = path
        elif path:
            search_root = os.path.join(ws_root, path)
        else:
            search_root = ws_root

        search_root = os.path.abspath(search_root)
        if not search_root.startswith(os.path.abspath(ws_root)):
            return {"error": "search path outside workspace", "results": [], "count": 0}

        if not os.path.isdir(search_root):
            return {"error": "search path not a directory", "results": [], "count": 0}

        try:
            regex = re.compile(pattern, re.MULTILINE)
        except re.error as e:
            return {"error": f"invalid regex: {e}", "results": [], "count": 0}

        # 编译 glob
        import fnmatch

        results = []
        count = 0

        for root, dirs, files in os.walk(search_root):
            # 跳过常见忽略目录
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build")]

            for fname in files:
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, ws_root)

                # glob 过滤
                if glob and not fnmatch.fnmatch(rel_path, glob):
                    # 也试试 basename 匹配
                    if not fnmatch.fnmatch(fname, glob):
                        continue

                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception:
                    continue

                if output_mode == "files_with_matches":
                    if regex.search(content):
                        count += 1
                        if len(results) < head_limit:
                            results.append(rel_path)
                elif output_mode == "count":
                    matches = regex.findall(content)
                    if matches:
                        c = len(matches)
                        count += c
                        if len(results) < head_limit:
                            results.append({"file": rel_path, "count": c})
                elif output_mode == "content":
                    lines = content.split("\n")
                    for i, line in enumerate(lines):
                        if regex.search(line):
                            count += 1
                            if len(results) < head_limit:
                                results.append({
                                    "file": rel_path,
                                    "line": i + 1,
                                    "content": line.rstrip(),
                                })
                else:
                    return {"error": f"invalid output_mode: {output_mode}", "results": [], "count": 0}

                if count > head_limit * 10:
                    # 够多了，停止扫描
                    break
            if count > head_limit * 10:
                break

        return {
            "results": results,
            "count": count,
            "truncated": count > head_limit,
        }

    @mcp.tool()
    def file_list(path: str = "", glob: str = "") -> list:
        """列出目录下的文件

        Agent 通过此工具浏览目录结构，替代 IDE 内置 Glob/LS 工具。

        Args:
            path: 目录路径（相对或绝对，默认为工作区根）
            glob: 文件 glob 过滤（如 "*.py"）

        Returns:
            文件/目录列表
        """
        import fnmatch

        db = get_db()
        ws_root = db.workspace_root

        if path and os.path.isabs(path):
            dir_path = path
        elif path:
            dir_path = os.path.join(ws_root, path)
        else:
            dir_path = ws_root

        dir_path = os.path.abspath(dir_path)
        if not dir_path.startswith(os.path.abspath(ws_root)):
            return []

        if not os.path.isdir(dir_path):
            return []

        try:
            entries = []
            for name in sorted(os.listdir(dir_path)):
                if name.startswith("."):
                    continue
                full = os.path.join(dir_path, name)
                is_dir = os.path.isdir(full)
                entry_type = "dir" if is_dir else "file"
                rel = os.path.relpath(full, ws_root)

                if glob and not is_dir:
                    if not fnmatch.fnmatch(name, glob):
                        continue

                entries.append({
                    "name": name,
                    "path": rel,
                    "type": entry_type,
                })
            return entries
        except Exception:
            return []

    @mcp.tool()
    def file_symbol_content(file_path: str, symbol_name: str) -> Optional[dict]:
        """读取文件中指定符号的源码内容

        结合数据库中的符号位置信息，精确读取函数/类/方法的源码，
        比 file_read 更高效，Agent 无需计算行号范围。

        Args:
            file_path: 文件路径（相对或绝对）
            symbol_name: 符号名称

        Returns:
            dict: {symbol_name, file, start_line, end_line, content}
        """
        db = get_db()
        ws_root = db.workspace_root

        # 先从数据库找符号
        sym = db.get_symbol_by_name_and_file(symbol_name, file_path)
        if not sym:
            return None

        # 读取文件内容
        abs_path = os.path.join(ws_root, sym.get("file", "")) if not os.path.isabs(sym.get("file", "")) else sym.get("file")
        if not os.path.isfile(abs_path):
            return None

        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            start = sym.get("start_line", 1) - 1
            end = sym.get("end_line", len(lines))
            start = max(0, start)
            end = min(len(lines), end)
            content = "".join(lines[start:end])

            # 注入 applicable_rules（fail-soft：无 AgentRulesMixin 或异常时降级为空列表）
            # 上下文：file_path / qualified_name / kind / 推断 language / action=read
            applicable_rules: list = []
            try:
                if hasattr(db, "get_applicable_rules_for_symbol"):
                    # 先尝试 action=read 维度匹配；如果没匹配到，再退化为符号维度匹配
                    # 这里直接构造带 action 的上下文，让 scope 含 actions 的规则也能命中
                    ctx = db.build_rule_context_for_symbol(
                        qualified_name=sym.get("qualified_name", ""),
                        file_path=sym.get("file", ""),
                        kind=sym.get("symbol_type", "") or sym.get("kind", ""),
                    )
                    ctx["action"] = "read"
                    rules_raw = db.get_applicable_rules(ctx, limit=5)
                    applicable_rules = [
                        {
                            "id": r.get("id", ""),
                            "title": r.get("title", ""),
                            "rule_text": r.get("rule_text", ""),
                            "severity": r.get("severity", "info"),
                            "matched_scope": r.get("matched_scope", []),
                        }
                        for r in rules_raw
                    ]
            except Exception:
                applicable_rules = []

            return {
                "symbol_name": symbol_name,
                "symbol_type": sym.get("symbol_type", ""),
                "qualified_name": sym.get("qualified_name", ""),
                "file": sym.get("file", ""),
                "start_line": sym.get("start_line", 0),
                "end_line": sym.get("end_line", 0),
                "content": content,
                "applicable_rules": applicable_rules,
            }
        except Exception as e:
            return {"error": str(e)}

    # ----------------------------------------------------------------
    # [L8] Git 集成工具（import_git_history / get_git_commits / get_commit_changes 等）
    # ----------------------------------------------------------------

    @mcp.tool()
    def import_git_history(max_commits: int = 100) -> dict:
        """导入 Git 历史记录到数据库

        Args:
            max_commits: 最大导入 commit 数量（默认 100）

        Returns:
            导入结果，包含成功状态和导入数量
        """
        db = get_db()
        return db.import_git_history(max_commits=max_commits)

    @mcp.tool()
    def get_git_commits(limit: int = 20, offset: int = 0) -> list:
        """获取 Git commit 列表

        Args:
            limit: 返回数量限制（默认 20）
            offset: 偏移量（默认 0）

        Returns:
            commit 列表，按时间倒序排列
        """
        db = get_db()
        return db.get_git_commits(limit=limit, offset=offset)

    @mcp.tool()
    def get_commit_changes(commit_hash: str) -> dict:
        """获取指定 commit 的变更详情

        Args:
            commit_hash: commit 哈希值

        Returns:
            commit 详情和变更文件列表
        """
        db = get_db()
        return db.get_commit_changes(commit_hash)

    @mcp.tool()
    def get_git_stats() -> dict:
        """获取 Git 集成统计信息

        Returns:
            Git 相关统计数据（commit 数、文件变更数、变更类型分布）
        """
        db = get_db()
        return db.get_git_stats()

    # ----------------------------------------------------------------
    # [L1] 状态与概览工具（get_status）
    # ----------------------------------------------------------------

    @mcp.tool()
    def get_status() -> dict:
        """获取代码图谱完整状态概览

        包含工作区信息、文件分布、语言分布、符号分布、
        调用关系统计、注释覆盖率、缺陷统计等全面信息。

        Returns:
            完整的状态概览字典
        """
        db = get_db()
        return db.get_status()

    # ----------------------------------------------------------------
    # [L1] 文件操作工具（remove_file / build_directory）
    # ----------------------------------------------------------------

    @mcp.tool()
    def remove_file(file_path: str) -> bool:
        """从图谱中移除指定文件（标记为删除，保留历史）

        Args:
            file_path: 文件的绝对路径或相对工作区路径

        Returns:
            是否成功移除
        """
        db = get_db()
        return db.remove_file(file_path)

    @mcp.tool()
    def build_directory(dir_path: str) -> dict:
        """构建指定目录的代码图谱

        Args:
            dir_path: 目录的绝对路径

        Returns:
            构建结果统计
        """
        db = get_db()
        return db.build_directory(dir_path)

    # ----------------------------------------------------------------
    # [L2] 符号内容工具（get_symbol_content_by_hash）
    # ----------------------------------------------------------------

    @mcp.tool()
    def get_symbol_content_by_hash(content_hash: str) -> Optional[dict]:
        """根据内容哈希获取符号的完整内容

        Args:
            content_hash: 符号内容的 SHA-256 哈希

        Returns:
            符号内容详情，包含完整代码
        """
        db = get_db()
        return db.get_symbol_content_by_hash(content_hash)

    # ----------------------------------------------------------------
    # [L4] 代码度量工具（get_code_metrics_summary / get_complexity_hotspots 等）
    # ----------------------------------------------------------------

    @mcp.tool()
    def get_code_metrics_summary() -> dict:
        """获取代码度量汇总统计

        包含文件数、函数数、总代码行、调用关系数、
        平均/最高圈复杂度、复杂度分布、注释覆盖率等。

        Returns:
            全局度量统计字典
        """
        db = get_db()
        return db.get_code_metrics_summary()

    @mcp.tool()
    def get_complexity_hotspots(limit: int = 20, module_filter: str = "") -> list:
        """获取圈复杂度最高的函数（复杂度热点）

        用于识别需要重构的复杂函数。

        Args:
            limit: 返回数量限制（默认 20）
            module_filter: 模块路径前缀过滤

        Returns:
            按圈复杂度降序排列的函数列表
        """
        db = get_db()
        return db.get_complexity_hotspots(limit=limit, module_filter=module_filter)

    @mcp.tool()
    def get_coupling_analysis(limit: int = 30) -> list:
        """获取模块耦合度分析

        分析模块间调用关系，计算每个模块的传入/传出耦合度和不稳定性。

        Args:
            limit: 返回数量限制（默认 30）

        Returns:
            按总耦合度降序排列的模块列表
        """
        db = get_db()
        return db.get_coupling_analysis(limit=limit)

    @mcp.tool()
    def get_function_metrics(qualified_name: str) -> Optional[dict]:
        """获取单个函数的度量详情

        Args:
            qualified_name: 函数限定名

        Returns:
            度量字典，包含圈复杂度、行数、扇入扇出、深度等
        """
        db = get_db()
        return db.get_function_metrics(qualified_name)

    @mcp.tool()
    def get_largest_functions(limit: int = 20, module_filter: str = "") -> list:
        """获取代码行数最多的函数

        Args:
            limit: 返回数量限制（默认 20）
            module_filter: 模块路径前缀过滤

        Returns:
            按行数降序排列的函数列表
        """
        db = get_db()
        return db.get_largest_functions(limit=limit, module_filter=module_filter)

    @mcp.tool()
    def get_most_coupled_functions(limit: int = 20) -> list:
        """获取耦合度最高的函数（扇入+扇出最大）

        Args:
            limit: 返回数量限制（默认 20）

        Returns:
            按总耦合度降序排列的函数列表
        """
        db = get_db()
        return db.get_most_coupled_functions(limit=limit)

    # ----------------------------------------------------------------
    # [L4] 代码健康检查工具（Agent 必读）（get_code_health_check / check_file_health）
    # ----------------------------------------------------------------

    @mcp.tool()
    def get_code_health_check(severity: str = "all") -> dict:
        """代码健康检查：识别大文件、复杂函数、高耦合模块等问题

        ⚠️  AI Agent 修改代码前强烈建议先调用此工具！

        检查内容：
        - 过大文件（>500/1000/2000 行）
        - 复杂函数（圈复杂度 >10/20/30）
        - 超长函数（>50/100/200 行）
        - 高耦合模块（不稳定性 >0.7/0.9）

        使用建议：
        1. 优先修改小文件，大文件修改前先考虑拆分
        2. 复杂函数修改前先理解完整逻辑，或先拆分成小函数
        3. 如果一个函数超过 200 行或复杂度 >30，建议先重构再修改

        Args:
            severity: 过滤严重程度（all / high / medium / low）

        Returns:
            健康检查报告，包含评分、问题分类列表和 Agent 指导
        """
        db = get_db()
        return db.get_code_health_check(severity=severity)

    @mcp.tool()
    def check_file_health(file_path: str) -> Optional[dict]:
        """检查单个文件的健康状态（Agent 修改文件前必调用）

        ⚠️  重要：在读取或修改任何文件之前，先调用此工具检查文件健康状态！

        如果返回 should_split_first = true，说明文件或其中函数过大，
        直接全量修改可能导致：
        - Token 溢出，AI 无法完整理解
        - 写入失败，文件过大难以一次性写回
        - 逻辑错误，复杂函数修改容易引入 bug

        建议：先拆分成小文件/小函数，再逐步修改。

        Args:
            file_path: 文件的绝对路径或相对工作区路径

        Returns:
            文件健康报告，包含大小、复杂度、是否建议先拆分等
        """
        db = get_db()
        return db.check_file_health(file_path)

    # ----------------------------------------------------------------
    # [L2] 语义搜索工具（semantic_search / find_similar_functions / embed_*）
    # ----------------------------------------------------------------

    @mcp.tool()
    def semantic_search(query: str, top_k: int = 5) -> list:
        """语义搜索：用自然语言查找相关函数

        Args:
            query: 自然语言查询（如"处理用户认证的函数"）
            top_k: 返回结果数量（默认 5）

        Returns:
            匹配函数列表，含 qualified_name、file_path、相似度分数
        """
        db = get_db()
        return db.semantic_search(query=query, top_k=top_k)

    @mcp.tool()
    def find_similar_functions(qualified_name: str, threshold: float = 0.8, top_k: int = 20) -> list:
        """查找与指定函数语义相似的其他函数

        用于发现重复代码、隐式依赖和可合并函数。

        Args:
            qualified_name: 函数限定名
            threshold: 相似度阈值（默认 0.8）
            top_k: 返回数量限制

        Returns:
            相似函数列表
        """
        db = get_db()
        return db.find_similar_functions(qualified_name=qualified_name, threshold=threshold, top_k=top_k)

    @mcp.tool()
    def embed_symbols(force: bool = False) -> dict:
        """为所有函数生成向量嵌入（首次使用前需执行）

        Args:
            force: 是否强制重新嵌入已有函数

        Returns:
            嵌入统计（总数、成功、跳过、失败）
        """
        db = get_db()
        return db.embed_all_symbols(force=force)

    @mcp.tool()
    def embed_single_symbol(symbol_hash: str) -> dict:
        """为单个函数生成向量嵌入

        Args:
            symbol_hash: 函数的内容哈希

        Returns:
            {"success": bool, "symbol_hash": str, "message": str}
        """
        try:
            db = get_db()
            ok = db.embed_symbol(symbol_hash)
            message_key = "cli.messages.embed_single_success" if ok else "cli.messages.embed_single_failed"
            default = "Embedding generated" if ok else "Embedding failed (symbol not found or vector service unavailable)"
            return {"success": ok, "symbol_hash": symbol_hash, "message": t(message_key, default=default)}
        except Exception as e:
            return {"success": False, "symbol_hash": symbol_hash, "message": str(e)}

    @mcp.tool()
    def get_symbol_commit_history(symbol_hash: str, limit: int = 20) -> list:
        """获取符号的 Git 变更历史

        查询 git_symbol_changes 表，返回该符号的所有 commit 变更记录。

        Args:
            symbol_hash: 符号内容哈希
            limit: 返回数量限制

        Returns:
            变更历史列表（commit_hash / timestamp / author / message / change_type）
        """
        try:
            db = get_db()
            return db.get_symbol_commit_history(symbol_hash, limit)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def parse_codeowners(file_path: str = "") -> list:
        """解析 CODEOWNERS 文件，返回所有权规则列表（不写入数据库）

        用于预览 CODEOWNERS 文件内容，不执行导入。

        Args:
            file_path: CODEOWNERS 文件路径（为空则使用默认路径 .github/CODEOWNERS）

        Returns:
            所有权规则列表（pattern / owners）
        """
        try:
            db = get_db()
            return db.parse_codeowners(file_path if file_path else None)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def import_codeowners() -> dict:
        """从 CODEOWNERS 文件导入所有权到数据库

        解析 .github/CODEOWNERS 或 CODEOWNERS 文件，将路径模式与所有者关联写入 file_owners 表。

        Returns:
            导入统计（规则数 / 已关联文件数 / 未匹配文件数）
        """
        try:
            db = get_db()
            return db.import_ownership_from_codeowners()
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def import_git_blame() -> dict:
        """从 git log 导入每个文件最近一次提交者信息作为所有权补充

        对每个 file_instance 执行 git log -1 获取最近提交者，写入 file_owners 表（source=git_blame）。
        适用于没有 CODEOWNERS 文件的项目，或作为 CODEOWNERS 的补充。

        Returns:
            导入统计（文件数 / 成功 / 失败）
        """
        try:
            db = get_db()
            return db.import_ownership_from_git_blame()
        except Exception as e:
            return {"error": str(e)}

    # ----------------------------------------------------------------
    # [L11] 外部符号工具（ExternalMixin）（get_project_dependencies / import_project_dependencies 等）
    # ----------------------------------------------------------------

    @mcp.tool()
    def get_project_dependencies(languages: list = None) -> dict:
        """读取项目直接依赖清单，不展开传递依赖"""
        try:
            db = get_db()
            return db.get_project_dependencies(languages=languages)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def import_project_dependencies() -> dict:
        """导入项目直接依赖的第一层外部符号"""
        try:
            db = get_db()
            created = db.import_project_dependencies()
            return {"created": created}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def prune_external_symbols(keep_project_deps: bool = True,
                               package_names: list = None,
                               vacuum: bool = False) -> dict:
        """清理外部符号；可保留项目直接依赖并可 VACUUM 释放空间"""
        try:
            db = get_db()
            return db.prune_external_symbols(
                keep_project_deps=keep_project_deps,
                package_names=package_names,
                vacuum=vacuum,
            )
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def gc_retention(older_than_days: int = None,
                     keep_versions: int = None,
                     include_external: bool = None,
                     external_stale_days: int = None,
                     dry_run: bool = True,
                     backup: bool = None,
                     vacuum: bool = None,
                     save_policy: bool = False) -> dict:
        """按冷热策略清理旧版本/外部符号；默认只预演，应用前压缩备份整库"""
        try:
            db = get_db()
            return db.gc_retention(
                older_than_days=older_than_days,
                keep_versions=keep_versions,
                include_external=include_external,
                external_stale_days=external_stale_days,
                dry_run=dry_run,
                backup=backup,
                vacuum=vacuum,
                save_policy=save_policy,
            )
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def gc_policy_get() -> dict:
        """读取当前 workspace 的 GC retention 策略"""
        try:
            db = get_db()
            return db.get_gc_policy()
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def gc_policy_set(older_than_days: int = None,
                      keep_versions: int = None,
                      include_external: bool = None,
                      external_stale_days: int = None,
                      backup_enabled: bool = None,
                      vacuum_enabled: bool = None) -> dict:
        """更新当前 workspace 的 GC retention 策略"""
        try:
            db = get_db()
            return db.set_gc_policy(
                older_than_days=older_than_days,
                keep_versions=keep_versions,
                include_external=include_external,
                external_stale_days=external_stale_days,
                backup_enabled=backup_enabled,
                vacuum_enabled=vacuum_enabled,
            )
        except Exception as e:
            return {"error": str(e)}

    # ----------------------------------------------------------------
    # [L11] GC 备份与审计工具（v20 新增）（gc_archive_list / gc_audit_list 等）
    # ----------------------------------------------------------------

    @mcp.tool()
    def gc_archive_list(limit: int = 20) -> list:
        """列出当前数据库目录下的 gc_archives/*.db.gz 备份文件

        Args:
            limit: 最多返回多少条（默认 20，按 mtime 倒序）

        Returns:
            备份文件列表，每条含 path/name/size/mtime/reason
        """
        try:
            db = get_db()
            return db.gc_archive_list(limit=limit)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def gc_archive_inspect(path: str) -> dict:
        """检查 GC 备份文件内容（只读模式，不解压到磁盘永久位置）

        Args:
            path: 备份文件路径（.db.gz，支持相对 gc_archives 目录的简写）

        Returns:
            备份文件元信息与内容摘要，含 schema_version/tables 列表/
            workspace_count/file_version_count/symbol_count/call_count/
            gc_runs_count/archived_files_count
        """
        try:
            db = get_db()
            return db.gc_archive_inspect(path=path)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def gc_audit_list(limit: int = 20, operation: str = None) -> list:
        """查询 GC 审计历史记录

        Args:
            limit: 最多返回多少条（默认 20，钳制到 [1, 500]）
            operation: 按操作类型过滤（retention/archive/purge）；None 表示不过滤

        Returns:
            审计记录列表，按 started_at 倒序，每条含 id/operation/dry_run/
            policy_json/candidate_counts/deleted_counts/backup_path/backup_size/
            started_at/completed_at/status/error/operator
        """
        try:
            db = get_db()
            return db.gc_audit_list(limit=limit, operation=operation)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def gc_audit_get(audit_id: int) -> dict:
        """查询单条 GC 审计记录详情

        Args:
            audit_id: gc_runs.id

        Returns:
            审计记录 dict（含反序列化的 JSON 字段），不存在返回空 dict
        """
        try:
            db = get_db()
            result = db.gc_audit_get(audit_id=audit_id)
            return result if result is not None else {}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def gc_archive_import(path: str, file_path: str = "",
                         package_name: str = "", dry_run: bool = True) -> dict:
        """从 GC 备份文件导入历史数据到当前库

        设计原则：只 INSERT OR IGNORE，绝不覆盖现有事实（当前库优先）。

        Args:
            path: 备份文件路径（.db.gz，支持相对 gc_archives 目录的简写）
            file_path: 要导入的文件相对路径（如 'src/a.py'）；
                       file_path 与 package_name 至少指定一个
            package_name: 要导入的外部包名
            dry_run: True=只统计不实际导入（默认）

        Returns:
            {
                "path": str, "dry_run": bool, "target": str, "target_value": str,
                "imported": dict, "skipped": dict, "candidate": dict, "errors": list
            }
        """
        try:
            db = get_db()
            return db.gc_archive_import(
                path=path, file_path=file_path,
                package_name=package_name, dry_run=dry_run,
            )
        except Exception as e:
            return {"error": str(e)}

    # ----------------------------------------------------------------
    # [L5] 任务驱动编排工具（task_create / task_next_step / work_next_job 等）
    # ----------------------------------------------------------------

    @mcp.tool()
    def task_create(title: str, description: str = "", steps: list = None, creator: str = "agent") -> str:
        """创建任务并返回 task_id

        Agent 通过此工具创建有步骤的任务，然后通过 task_next_step 逐步执行。

        Args:
            title: 任务标题
            description: 任务描述
            steps: 步骤列表，每个元素含 action/target_file/target_symbol/check_items
            creator: 创建者标识

        Returns:
            task_id
        """
        db = get_db()
        return db.task_create(title=title, description=description, steps=steps, creator=creator)

    @mcp.tool()
    def task_next_step(task_id: str) -> Optional[dict]:
        """领取任务的下一个待执行步骤

        Agent 必须通过此工具领取步骤，不能自由决定下一步操作。
        返回步骤详情（文件、操作、检查项），Agent 只能执行这一步。

        Before-Edit Contract：当步骤为编辑类操作时，系统自动调用护栏检查。
        - 若返回 guardrail_alert（decision=block）：步骤状态为 blocked，
          Agent 必须先处理告警，再调用 task_resolve_block 恢复步骤。
        - 若返回 guardrail_warning（decision=warn）：步骤可执行，但需关注告警。
        - 否则正常执行。

        Args:
            task_id: 任务 ID

        Returns:
            步骤详情，如果没有待执行步骤则返回 None
        """
        db = get_db()
        return db.task_next_step(task_id=task_id)

    @mcp.tool()
    def work_next_job(task_id: str) -> Optional[dict]:
        """领取下一项 Agent 工作，并返回完成它所需的最小上下文

        这是 Agent 优先入口：相比手动 read/grep/plan，本工具返回目标、
        符号源码、调用上下文、文件健康、允许编辑范围、推荐 patch 工具
        和完成后汇报方式。
        """
        db = get_db()
        return db.work_next_job(task_id=task_id)

    @mcp.tool()
    def task_resolve_block(task_id: str, step_id: str, resolution: str = "ack") -> Optional[dict]:
        """处理 blocked 步骤的护栏告警，恢复为 pending 以便重新领取

        当 task_next_step 返回 guardrail_alert（block 级别）时，Agent 处理告警后
        调用此方法将步骤从 blocked 恢复为 pending，之后可再次 task_next_step 领取。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            resolution: 处理方式（ack/override/fix_applied）

        Returns:
            更新后的步骤详情，若步骤不存在或非 blocked 状态则返回 None
        """
        db = get_db()
        return db.task_resolve_block(task_id=task_id, step_id=step_id, resolution=resolution)

    @mcp.tool()
    def task_report_step(task_id: str, step_id: str, result: str = "", success: bool = True, changes: list = None) -> Optional[dict]:
        """回报步骤执行结果

        如果失败，系统会自动插入"修复缺陷"步骤，Agent 无法跳过。
        如果成功且无更多步骤，任务状态变为 review。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            result: 执行结果描述
            success: 是否成功
            changes: 变更记录列表

        Returns:
            下一步步骤信息（如果有）
        """
        db = get_db()
        return db.task_report_step(task_id=task_id, step_id=step_id, result=result, success=success, changes=changes)

    @mcp.tool()
    def record_task_symbol_change(task_id: str, file_path: str, step_id: str = "",
                                  edit_audit_id: int = 0, change_audit_id: str = "",
                                  qualified_name: str = "", symbol_name: str = "",
                                  symbol_hash_before: str = "", symbol_hash_after: str = "",
                                  change_type: str = "modified", source: str = "manual",
                                  metadata: dict = None) -> dict:
        """记录任务/步骤到文件或符号版本变化的归因"""
        try:
            db = get_db()
            return db.record_task_symbol_change(
                task_id=task_id,
                file_path=file_path,
                step_id=step_id,
                edit_audit_id=edit_audit_id,
                change_audit_id=change_audit_id,
                qualified_name=qualified_name,
                symbol_name=symbol_name,
                symbol_hash_before=symbol_hash_before,
                symbol_hash_after=symbol_hash_after,
                change_type=change_type,
                source=source,
                metadata=metadata or {},
            )
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def link_edit_audit_symbols(audit_id: int, step_id: str = "") -> dict:
        """刷新图谱后，将某次 edit_audit 的 before/after 文件版本映射到符号变化"""
        try:
            db = get_db()
            return db.link_edit_audit_symbols(audit_id=audit_id, step_id=step_id)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_task_symbol_changes(task_id: str, step_id: str = "", file_path: str = "", limit: int = 100) -> list:
        """查询任务或步骤归因到的文件/符号变化"""
        try:
            db = get_db()
            return db.get_task_symbol_changes(task_id=task_id, step_id=step_id, file_path=file_path, limit=limit)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_symbol_change_tasks(symbol_hash: str = "", qualified_name: str = "", limit: int = 50) -> list:
        """反查某个符号版本或符号名由哪些任务改变过"""
        try:
            db = get_db()
            return db.get_symbol_change_tasks(symbol_hash=symbol_hash, qualified_name=qualified_name, limit=limit)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def task_rollback(task_id: str, change_id: str = None, reason: str = "") -> dict:
        """回滚任务中的变更

        Args:
            task_id: 任务 ID
            change_id: 变更 ID（可选，不指定则回滚最后一个变更）
            reason: 回滚原因

        Returns:
            回滚结果
        """
        db = get_db()
        return db.task_rollback(task_id=task_id, change_id=change_id, reason=reason)

    @mcp.tool()
    def task_apply(task_id: str, reviewer: str = "reviewer") -> dict:
        """审核通过：将任务状态从 review 改为 applied

        设计原则：写代码的 Agent 不能自己 applied，必须由其他会话的
        LLM 审核通过后调用此工具。只有 status=review 的任务才能 apply。

        Args:
            task_id: 任务 ID
            reviewer: 审核人标识

        Returns:
            包含 task_id、status、applied_at 的字典；失败时包含 error 字段
        """
        db = get_db()
        try:
            return db.task_apply(task_id=task_id, reviewer=reviewer)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def task_close(task_id: str, reviewer: str = "reviewer") -> dict:
        """关闭任务：将任务状态从 applied 改为 closed

        设计原则：写代码的 Agent 不能自己 closed，必须由其他会话的
        LLM 审核关闭后调用此工具。只有 status=applied 的任务才能 close。

        Args:
            task_id: 任务 ID
            reviewer: 审核人标识

        Returns:
            包含 task_id、status、closed_at 的字典；失败时包含 error 字段
        """
        db = get_db()
        try:
            return db.task_close(task_id=task_id, reviewer=reviewer)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def task_capture_diff(
        task_id: str,
        step_id: str = "",
        base: str = "",
        dry_run: bool = True,
    ) -> dict:
        """捕获外部 Agent 真实文件改动到 task/change/symbol/audit 闭环

        用于把外部 Agent（非 Call Warden MCP）在文件系统中留下的真实改动
        归因到指定 task/step，并触发质量审查。这是自举闭环的核心入口。

        流程：
        1. 调用 get_workspace_changes_since 检测变更文件
        2. dry-run=True：只返回计划不写库
        3. dry-run=False（apply 模式）：
           - 写 workspace_scan_runs（status=running -> completed）
           - 每个变更文件写 change_audit（含 hash_before/hash_after）
           - 签名审计记录 sign_audit_record（best-effort，失败不阻塞）
           - 关联 task_symbol_changes（best-effort，失败不阻塞）
           - 调用 run_task_completion_review 收集 quality findings
           - 根据 quality_decision 决定 next_action

        Args:
            task_id: 关联任务 ID
            step_id: 关联步骤 ID（可选）
            base: 基线 commit（空串自动取最近一次 scan baseline 的 git_head）
            dry_run: True 只返回计划不写库，默认 True

        Returns:
            {
                "task_id": str,
                "step_id": str,
                "dry_run": bool,
                "scan_id": int,        # apply 模式才有
                "changed_files": [...],
                "linked_symbols": [...],
                "quality_findings": [...],
                "quality_decision": "pass" | "warn" | "block" | "",
                "next_action": "review" | "fix" | "commit" | "noop" | "",
            }
        """
        db = get_db()
        try:
            return db.task_capture_diff(
                task_id=task_id,
                step_id=step_id,
                base=base,
                dry_run=dry_run,
            )
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def audit_verify_chain(table_name: str = "", limit: int = 1000) -> dict:
        """验证审计签名链连续性与签名匹配

        检查 audit_chain 表中每条记录：
        1. record_signature 是否匹配重新计算的签名
        2. prev_signature 是否匹配上一条记录的 record_signature
        3. 首条记录的 prev_signature 是否为空串

        用于检测直接改库导致的篡改。

        Args:
            table_name: 指定表名时只验证该表的链；为空时验证全部
            limit: 最多验证的记录数，默认 1000

        Returns:
            {
                "table_name": str,       # 验证的表名（空串表示全部）
                "total_count": int,      # 验证的记录总数
                "verified_count": int,   # 通过验证的记录数
                "broken_count": int,     # 不通过的记录数
                "broken_records": [     # 不通过的记录列表
                    {"id": int, "table_name": str, "record_id": str, "reasons": [str]}
                ],
                "security_level": str,  # "hmac" 或 "hash_only"
            }
        """
        db = get_db()
        try:
            return db.verify_audit_chain(table_name=table_name, limit=limit)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def rotate_audit_signing_key(key_id: str, key_secret: str = "") -> dict:
        """轮换审计签名密钥（C7）

        流程：
        1. 将当前 active 密钥置为 inactive（is_active=0）
        2. 插入新密钥记录（is_active=1）

        轮换后：
        - 新的 sign_audit_record 调用使用新密钥签名（signing_key_id = key_id）
        - 旧记录保持原签名不变（signing_key_id 不变）
        - verify_audit_chain 按 signing_key_id 查找对应密钥验证

        Args:
            key_id: 新密钥标识（唯一，如 "key-2026-07"）
            key_secret: 新密钥内容（用于 HMAC 计算）；为空时自动生成 32 字节随机密钥

        Returns:
            {
                "success": True,
                "key_id": str,           # 新密钥标识
                "rotated_at": float,     # 轮换时间戳
                "previous_key_id": str,  # 前一个 active 密钥的 key_id（无则为空串）
            }
            失败时：{"success": False, "error": str}
        """
        db = get_db()
        try:
            # key_secret 为空时自动生成 32 字节随机密钥（hex 编码，64 字符）
            if not key_secret:
                import secrets as _secrets
                key_secret = _secrets.token_hex(32)
            return db.rotate_signing_key(
                new_key_id=key_id,
                new_key_secret=key_secret,
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def list_audit_signing_keys() -> list:
        """列出所有签名密钥轮换记录（C7）

        按 rotated_at 倒序返回，每项含 key_id/rotated_at/is_active，
        不返回 key_secret 以避免泄露密钥内容。

        Returns:
            [
                {"key_id": str, "rotated_at": float, "is_active": int},
                ...
            ]
            失败时：[{"error": str}]
        """
        db = get_db()
        try:
            return db.list_signing_keys()
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def bootstrap_status() -> dict:
        """返回自举健康状态摘要

        汇总以下信息，帮助判断当前自举闭环是否健康：

        1. db_stale：DB 是否滞后（最近一次 scan_run 的 git_head 与当前 HEAD 不一致）
        2. active_rules_count：已生效的 agent_rules 数量
        3. pending_candidates_count：待审核的 rule candidates 数量
        4. open_findings_count：open 状态的 quality findings 数量
        5. blocking_findings_count：block 严重度的 quality findings 数量
        6. audit_verify：audit_chain 验证结果摘要
        7. latest_scan_run：最近一次 workspace_scan_runs 记录
        8. tasks：按状态分组的任务计数（open / in_progress / review / applied）
        9. recommended_next_action：推荐下一条命令

        Returns:
            {
                "db_stale": bool,
                "current_head": str,
                "active_rules_count": int,
                "pending_candidates_count": int,
                "open_findings_count": int,
                "blocking_findings_count": int,
                "audit_verify": {
                    "total_count": int,
                    "verified_count": int,
                    "broken_count": int,
                    "security_level": str,
                },
                "latest_scan_run": {...} | None,
                "tasks": {"open": int, "in_progress": int, "review": int, "applied": int},
                "recommended_next_action": str,
            }
        """
        db = get_db()
        try:
            return db.bootstrap_status()
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def detect_clones(
        file_filter: str = "",
        min_lines: int = 5,
        similarity_threshold: float = 0.8,
    ) -> dict:
        """检测重复代码（Type-1/2/3 克隆）

        检测范围：
        - Type-1：完全相同的符号内容（content_hash 相同）
        - Type-2：重命名克隆（token 序列相同，标识符名不同）
        - Type-3：微调克隆（token 集合 Jaccard 相似度 >= similarity_threshold）

        结果持久化到 clone_pairs 表（UPSERT），支持重复执行。

        Args:
            file_filter: 文件路径前缀过滤（如 "src/core/"），空字符串扫描所有
            min_lines: 最小符号行数，低于此值的符号跳过（默认 5）
            similarity_threshold: Type-3 相似度阈值 [0,1]（默认 0.8）

        Returns:
            {
                "total_pairs": int,
                "type1_pairs": int,
                "type2_pairs": int,
                "type3_pairs": int,
                "scanned_symbols": int,
                "skipped_symbols": int,
                "similarity_threshold": float,
                "min_lines": int,
            }
        """
        db = get_db()
        try:
            return db.detect_clones(
                file_filter=file_filter,
                min_lines=min_lines,
                similarity_threshold=similarity_threshold,
            )
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def list_clones(
        clone_type: int = 0,
        min_similarity: float = 0.0,
        limit: int = 100,
    ) -> list:
        """列出检测到的克隆对

        Args:
            clone_type: 克隆类型过滤（0=全部，1/2/3 对应 Type-N）
            min_similarity: 最低相似度过滤（默认 0.0）
            limit: 返回上限（默认 100）

        Returns:
            克隆对列表，按相似度降序，每项包含：
            {
                "clone_type": int,
                "similarity": float,
                "token_hash": str,
                "lines_a": int, "lines_b": int,
                "detected_at": float,
                "symbol_a_name": str, "symbol_a_qualified": str,
                "symbol_a_line": int,
                "symbol_b_name": str, "symbol_b_qualified": str,
                "symbol_b_line": int,
                "file_a": str, "file_b": str,
            }
        """
        db = get_db()
        try:
            return db.list_clones(
                clone_type=clone_type,
                min_similarity=min_similarity,
                limit=limit,
            )
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_clone_stats() -> dict:
        """获取克隆检测统计信息

        Returns:
            {
                "total": int,
                "type1": int, "type2": int, "type3": int,
                "affected_files": int,
                "affected_symbols": int,
            }
        """
        db = get_db()
        try:
            return db.get_clone_stats()
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def clear_clones() -> dict:
        """清空当前 workspace 的所有克隆检测结果

        Returns:
            {"deleted": int} 被删除的记录数
        """
        db = get_db()
        try:
            deleted = db.clear_clones()
            return {"deleted": deleted}
        except Exception as e:
            return {"error": str(e)}

    # ========================================
    # Phase 7.0: Heavy Jobs 后台化
    # ========================================

    @mcp.tool()
    def detect_clones_async(
        file_filter: str = "",
        min_lines: int = 5,
        similarity_threshold: float = 0.8,
    ) -> dict:
        """异步检测重复代码（后台 job，不阻塞 MCP 请求）

        把 clone detect 提交为后台 job，存 clone groups（不展开 pairs）。
        适合 20 万符号级别的代码库，避免同步执行导致 MCP 请求超时。

        Args:
            file_filter: 文件路径前缀过滤（如 "src/core/"），空字符串扫描所有
            min_lines: 最小符号行数（默认 5）
            similarity_threshold: Type-3 相似度阈值 [0,1]（默认 0.8）

        Returns:
            {
                "job_id": str,         # 任务 ID
                "status": "pending",    # 初始状态
                "job_type": "clone_detect",
                "message": "submitted",
            }
        """
        db = get_db()
        try:
            from callwarden.server.job_executor_singleton import get_job_executor
            executor = get_job_executor(db.db_path, db.workspace_root)
            params = {
                "file_filter": file_filter,
                "min_lines": min_lines,
                "similarity_threshold": similarity_threshold,
            }
            ws_id = db._get_active_workspace_id()
            job = executor.submit("clone_detect", params, workspace_id=ws_id)
            return {
                "job_id": job.job_id,
                "status": job.status,
                "job_type": job.job_type,
                "message": "submitted",
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_job_status(job_id: str) -> dict:
        """查询后台任务状态

        Args:
            job_id: 任务 ID（如 "J-1783698970719-3a4b"）

        Returns:
            {
                "job_id": str,
                "job_type": str,
                "status": str,         # pending/running/completed/cancelled/failed
                "progress": float,     # 0.0 ~ 1.0
                "message": str,
                "result_summary": dict,
                "error": str,
                "created_at": float,
                "started_at": float,
                "finished_at": float,
            }
        """
        db = get_db()
        try:
            job = db.get_job(job_id)
            if not job:
                return {"error": f"job not found: {job_id}"}
            return job.to_dict()
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def cancel_job(job_id: str) -> dict:
        """请求取消后台任务

        行为：
        - pending 状态：直接标记为 cancelled
        - running 状态：设置 cancel_requested，executor 轮询后退出
        - 终态：无操作

        Args:
            job_id: 任务 ID

        Returns:
            {"cancelled": bool, "job_id": str}
        """
        db = get_db()
        try:
            ok = db.cancel_job(job_id)
            return {"cancelled": ok, "job_id": job_id}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def list_jobs(
        job_type: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list:
        """列出后台任务

        Args:
            job_type: 任务类型过滤（"" = 全部，如 "clone_detect"）
            status: 状态过滤（"" = 全部，如 "running"）
            limit: 返回上限（默认 100）

        Returns:
            任务列表，按 created_at 降序
        """
        db = get_db()
        try:
            jobs = db.list_jobs(
                job_type=job_type or None,
                status=status or None,
                limit=limit,
            )
            return [j.to_dict() for j in jobs]
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_job_stats() -> dict:
        """获取任务统计信息

        Returns:
            {
                "pending": int, "running": int,
                "completed": int, "cancelled": int, "failed": int,
                "total": int,
            }
        """
        db = get_db()
        try:
            return db.get_job_stats()
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def list_clone_groups(
        clone_type: int = 0,
        min_similarity: float = 0.0,
        limit: int = 100,
    ) -> list:
        """列出 clone groups（Phase 7.0 新增）

        读取 detect_clones_async 的结果。每组含 representative + member_count，
        不展开成 pairs，避免 N×N 爆炸。

        Args:
            clone_type: 0=全部，1/2/3 对应 Type-N
            min_similarity: 最低相似度过滤
            limit: 返回上限（默认 100）

        Returns:
            clone group 列表，按相似度降序
        """
        db = get_db()
        try:
            groups = db.list_clone_groups(
                clone_type=clone_type,
                min_similarity=min_similarity,
                limit=limit,
            )
            return [g.to_dict() for g in groups]
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_clone_group_detail(
        group_id: int,
        members_limit: int = 100,
    ) -> dict:
        """获取 clone group 详情（含成员符号）

        Args:
            group_id: group ID
            members_limit: 成员返回上限（默认 100）

        Returns:
            {
                "group": {...},
                "members": [{"symbol_id", "name", "qualified_name",
                            "file_path", "start_line"}, ...]
            }
        """
        db = get_db()
        try:
            detail = db.get_clone_group_detail(group_id, members_limit)
            if not detail:
                return {"error": f"group not found: {group_id}"}
            return {
                "group": detail.group.to_dict(),
                "members": detail.members,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_clone_group_stats() -> dict:
        """获取 clone groups 统计信息

        Returns:
            {
                "total_groups": int, "type1": int, "type2": int, "type3": int,
                "total_members": int,
                "affected_files": int, "affected_symbols": int,
            }
        """
        db = get_db()
        try:
            return db.get_clone_group_stats()
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def embed_symbols_async(
        batch_size: int = 32,
        force: bool = False,
    ) -> dict:
        """异步嵌入向量（后台 job，不阻塞 MCP 请求）

        Phase 7.2：把 vector embedding 提交为后台 job。
        增量模式（force=False）只嵌入尚未有嵌入的符号，适合 20 万符号级别的代码库，
        避免同步执行导致 MCP 请求超时。

        Args:
            batch_size: 每批处理数量（默认 32）
            force: True 时强制重新嵌入所有符号（默认 False，增量模式）

        Returns:
            {
                "job_id": str,          # 任务 ID
                "status": "pending",     # 初始状态
                "job_type": "vector_embed",
                "message": "submitted",
            }
        """
        db = get_db()
        try:
            from callwarden.server.job_executor_singleton import get_job_executor
            executor = get_job_executor(db.db_path, db.workspace_root)
            params = {"batch_size": batch_size, "force": force}
            ws_id = db._get_active_workspace_id()
            job = executor.submit("vector_embed", params, workspace_id=ws_id)
            return {
                "job_id": job.job_id,
                "status": job.status,
                "job_type": job.job_type,
                "message": "submitted",
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def semgrep_scan_async(
        config: str = "p/default",
        languages: list = None,
        timeout: int = 300,
    ) -> dict:
        """异步运行 Semgrep 扫描（后台 job，不阻塞 MCP 请求）

        Phase 7.3：把 Semgrep CLI 扫描提交为后台 job。
        Semgrep 作为 bounded external process 执行（有 timeout 限制），
        适合大型代码库，避免同步执行导致 MCP 请求超时。

        Args:
            config: Semgrep 规则配置（默认 p/default，可选 p/security 等）
            languages: 限制扫描的语言列表（如 ["python", "rust"]），为空则扫描所有
            timeout: Semgrep CLI 超时秒数（默认 300）

        Returns:
            {
                "job_id": str,
                "status": "pending",
                "job_type": "semgrep_scan",
                "message": "submitted",
            }
        """
        db = get_db()
        try:
            from callwarden.server.job_executor_singleton import get_job_executor
            executor = get_job_executor(db.db_path, db.workspace_root)
            params = {"config": config, "languages": languages, "timeout": timeout}
            ws_id = db._get_active_workspace_id()
            job = executor.submit("semgrep_scan", params, workspace_id=ws_id)
            return {
                "job_id": job.job_id,
                "status": job.status,
                "job_type": job.job_type,
                "message": "submitted",
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def rule_seed_bootstrap(dry_run: bool = True) -> dict:
        """种子化内置自举 active rules

        把内置的 5 条 bootstrap 规则写入 agent_rules（status=active），
        让规则注入不再空转。规则覆盖：
        - i18n 强制（warning）
        - 提交前刷新代码图谱（critical）
        - 大任务必须拆分（warning）
        - 任务完成必须运行 completion review（critical）
        - 外部编辑后必须运行 task capture-diff（warning）

        幂等性：通过固定 ID（AR-bootstrap-*）实现，重复 seed 不会重复创建。
        已存在且无变化 → skip；已存在但内容变化 → update；不存在 → create。

        Args:
            dry_run: True 只返回计划不写库，默认 True

        Returns:
            {
                "dry_run": bool,
                "total": int,           # 内置规则总数（5）
                "created": int,          # 新建数量
                "updated": int,          # 更新数量
                "skipped": int,          # 跳过数量
                "rules": [               # 每条规则的执行结果
                    {"id": str, "title": str, "action": "create"|"update"|"skip"}
                ],
            }
        """
        db = get_db()
        try:
            return db.rule_seed_bootstrap(dry_run=dry_run)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def cleanup_agent_rule_sync_log(
        older_than_days: int = 90,
        keep_latest: int = 100,
        dry_run: bool = True,
    ) -> dict:
        """清理 agent_rule_sync_log 表中的旧记录，防止无限增长（C6 GC）

        策略（同时满足才删除）：
        1. created_at 早于 older_than_days 天前
        2. 不在最近 keep_latest 条记录内（按 created_at 倒序）

        默认 dry-run（只预估不删除），需传 dry_run=False 才真正执行 DELETE。
        fail-soft：任何异常都封装为 {"success": False, "error": ...}，不抛出。

        Args:
            older_than_days: 超过多少天的记录进入候选（默认 90）
            keep_latest: 保留最近多少条记录不删除（默认 100）
            dry_run: True 只预演不删除（默认 True），False 真正执行删除

        Returns:
            {
                "success": bool,
                "dry_run": bool,
                "deleted_count": int,      # dry_run 时为预估值，apply 时为实删数
                "remaining_count": int,
                "total_before": int,       # 清理前总记录数
                "older_than_days": int,
                "keep_latest": int,
                "error": str,              # 仅 success=False 时存在
            }
        """
        db = get_db()
        try:
            return db.cleanup_sync_log(
                older_than_days=older_than_days,
                keep_latest=keep_latest,
                dry_run=dry_run,
            )
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def task_create_subtask(parent_task_id: str, title: str, description: str = "", steps: list = None, creator: str = "agent") -> str:
        """在父任务下创建子任务

        当任务过大时，可将其拆分为多个子任务。子任务完成后，
        系统自动推进父任务状态，避免 Agent 遗漏任务或遗忘上下文。

        Args:
            parent_task_id: 父任务 ID
            title: 子任务标题
            description: 子任务描述
            steps: 子任务步骤列表
            creator: 创建者标识

        Returns:
            新建子任务的 task_id
        """
        db = get_db()
        return db.task_create_subtask(
            parent_task_id=parent_task_id,
            title=title,
            description=description,
            steps=steps,
            creator=creator,
        )

    @mcp.tool()
    def task_split(task_id: str, subtasks: list) -> list:
        """将大任务拆分为多个子任务

        当任务步骤过多或单个步骤描述过长时，使用此工具自动拆分。
        原任务的自身步骤保留为汇总/验证步骤，具体工作由子任务完成。
        task_next_step 会自动深度优先下钻到最底层子任务执行，
        确保 Agent 永远聚焦在具体可执行的小任务上，不会遗漏。

        Args:
            task_id: 要拆分的父任务 ID
            subtasks: 子任务定义列表，每个元素含 title/description/steps

        Returns:
            新建子任务的 ID 列表
        """
        db = get_db()
        return db.task_split(task_id=task_id, subtasks=subtasks)

    @mcp.tool()
    def task_status_tree(task_id: str) -> Optional[dict]:
        """获取任务树详情（含子任务树和进度）

        返回完整的任务树结构，包括每层的进度百分比、
        子任务列表、自身步骤状态。用于 Agent 了解整体进展，
        避免因子任务过多而迷失方向。

        Args:
            task_id: 根任务 ID

        Returns:
            任务树 dict（含 progress、steps、subtasks 递归结构）
        """
        db = get_db()
        return db.task_status_tree(task_id=task_id)

    @mcp.tool()
    def task_create_from_plan(title: str, plan_md: str, description: str = "") -> str:
        """从 Markdown 任务计划自动创建父子任务树

        Agent 只需传入任务标题和 Markdown 格式的计划，系统会自动：
        - 解析 # / ## / ### 标题层级为任务层级
        - 解析 - [ ] 列表项为任务步骤
        - 自动生成完整的父子任务树并入库
        - task_next_step 会自动深度优先下钻执行

        推荐格式：
        ```
        # 一级标题 = 根任务说明
        ## 子任务1标题
        - 步骤1描述
        - 步骤2描述
        ## 子任务2标题
        - 步骤1描述
        ```

        Args:
            title: 根任务标题
            plan_md: Markdown 格式的任务计划
            description: 根任务补充描述（可选）

        Returns:
            根任务 ID
        """
        db = get_db()
        return db.task_create_from_plan(
            title=title,
            plan_md=plan_md,
            description=description,
        )

    @mcp.tool()
    def task_plan_template() -> str:
        """获取 task_create_from_plan 的标准格式模板

        Agent 在调用 task_create_from_plan 之前，先获取此模板，
        按模板格式填写任务计划，确保解析器正确识别。

        Returns:
            Markdown 格式的模板字符串（含格式说明）
        """
        db = get_db()
        return db.task_plan_template()

    @mcp.tool()
    def task_list(status_filter: str = None, limit: int = 20) -> list:
        """列出任务

        Args:
            status_filter: 状态过滤（open/in_progress/review/applied/closed/reverted）
            limit: 返回数量限制

        Returns:
            任务列表
        """
        db = get_db()
        return db.task_list(status_filter=status_filter, limit=limit)

    @mcp.tool()
    def task_status(task_id: str) -> Optional[dict]:
        """获取任务详情和所有步骤

        Args:
            task_id: 任务 ID

        Returns:
            任务详情和步骤列表
        """
        db = get_db()
        return db.task_status(task_id=task_id)

    @mcp.tool()
    def task_completion_review(task_id: str, step_id: str = "") -> dict:
        """运行任务完成质量审查

        触发任务质量门禁：自动清理该 step 旧的 check_gate 发现，
        调用 run_check_gate（语法/Semgrep），并运行 5 个扩展检查器：
        scope/symbol_attribution/file_health/i18n_hardcoded/signature_mismatch。

        根据 open 状态的发现严重度给出决策：
        - pass: 无发现
        - warn: 仅有 info/warn（允许完成但记录）
        - block: 存在 error/block（阻塞完成，需修复后重审）

        Agent 在 task_report_step 之前或之后均可调用此工具主动复查。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID（可选，任务级审查留空）

        Returns:
            {decision, findings, summary, counts, check_gate_result}
            decision ∈ {"pass", "warn", "block"}
        """
        db = get_db()
        return db.run_task_completion_review(task_id=task_id, step_id=step_id)

    @mcp.tool()
    def task_quality_findings(task_id: str, status: str = "open", severity: str = "") -> list:
        """查询任务质量门禁发现

        返回 task_quality_findings 表中匹配过滤条件的记录，
        按 created_at 升序（旧的先处理）。

        Args:
            task_id: 任务 ID
            status: 状态过滤（open/resolved/wontfix/all），默认 open
            severity: 严重度过滤（info/warn/error/block），默认不过滤

        Returns:
            finding 列表，每项含 id/task_id/step_id/finding_type/severity/
            status/message/evidence/source/created_at/resolved_at/resolved_by
        """
        db = get_db()
        return db.get_task_quality_findings(
            task_id=task_id, status=status, severity=severity
        )

    @mcp.tool()
    def task_resolve_quality_finding(
        finding_id: int,
        resolution: str = "fixed",
        resolved_by: str = "agent",
    ) -> dict:
        """解决或豁免单条任务质量门禁发现

        将 finding 状态从 open 推进到 resolved 或 wontfix，
        记录解决者和解决时间。error/block 级别的发现被解决后，
        该 step 的阻塞状态才会解除（task_completion_review 会重新评估）。

        Args:
            finding_id: finding ID
            resolution: 解决方式
                - fixed: 已修复
                - wontfix: 暂不修复（接受风险）
                - false_positive: 误报
            resolved_by: 解决者标识（agent/human/system）

        Returns:
            {success, finding_id, status, resolution, resolved_at}
            失败时返回 {success: False, error: ...}
        """
        db = get_db()
        return db.resolve_task_quality_finding(
            finding_id=finding_id,
            resolution=resolution,
            resolved_by=resolved_by,
        )

    # ----------------------------------------------------------------
    # [L2] 代码摘要 + Repo Map 工具（generate_summary / get_summary / project_brief / repo_map）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L10] 覆盖率智能工具（import_coverage / get_coverage_for_symbol / find_uncovered_functions 等）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L10] 所有权分析工具（who_to_ask / get_ownership_map）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L7] 安全护栏工具（GuardrailMixin）（guardrail_scan / guardrail_check_edit 等）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L9+L2] 变更影响工具（ImpactMixin）（blast_radius 属 L9；ask_codebase /
    #         record_token_savings / get_token_savings_report 属 L2）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L4] 演化智能工具（EvolutionMixin）（evolution_frequency / defect_correlation 等）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L9] 缺陷知识库工具（DefectKbMixin）（defect_search / defect_suggest_fix 等）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L1] 分支感知图谱工具（BranchMixin）（register_branch / list_branches 等）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L12] 安全文件编辑工具（EditSafetyMixin，Agent OS 核心）（propose_edit / revert_edit 等）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L12] 跨仓库分析工具（Cross-Repo Analysis）（detect_cross_repo_deps 等）
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    # [L12] LSP 集成工具（Language Server Protocol）（lsp_hover / lsp_definition 等）
    # ----------------------------------------------------------------

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

    # ---- [L7] 检查门禁（F6）（run_check_gate / resolve_gate_findings）----

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

    # ---- [L6] Agent Rule Memory（候选-审核-生效-同步）（rule_candidate_* / rule_list 等）----

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

    return mcp


def _auto_sync_agents_md() -> Dict[str, Any]:
    """启动时自动同步 AGENTS.md（fail-soft，不阻断启动）

    把当前 active 的 Agent Rule Memory 同步到 AGENTS.md 标记区，
    让无 MCP 的 Agent 也能从 AGENTS.md 看到已生效规则。

    安全策略：
    - 同步失败不阻断 MCP Server 启动（fail-soft）
    - 使用 dry_run=False 实际写入文件，并记录 agent_rule_sync_log
    - 标记区不存在时静默跳过（不插入标记块，避免改写用户文件）
    - 所有输出走 stderr，不污染 stdio 协议

    Returns:
        dict: 同步结果摘要（含 success / rule_count / error 等字段）
    """
    try:
        db = get_db()
        result = db.rule_sync_agents_md(
            target_path="AGENTS.md",
            dry_run=False,
            actor="mcp_server_startup",
        )
        return result
    except Exception as exc:
        # fail-soft：任何异常都不阻断启动，仅记录错误
        return {
            "success": False,
            "dry_run": False,
            "target_path": "AGENTS.md",
            "rule_count": 0,
            "rule_ids": [],
            "before_hash": "",
            "after_hash": "",
            "error": str(exc),
        }


def _print_auto_sync_summary(result: Dict[str, Any]) -> None:
    """打印 AGENTS.md 自动同步摘要到 stderr

    MCP Server 使用 stdio 传输协议，所有日志必须走 stderr，
    否则会污染协议输出导致 client 解析失败。

    Args:
        result: _auto_sync_agents_md() 返回的结果字典
    """
    if result.get("success"):
        count = result.get("rule_count", 0)
        print(
            t(
                "cli.messages.agents_md_auto_sync_success",
                count=count,
                default=f"[Auto Sync] AGENTS.md 已同步，共 {count} 条规则",
            ),
            file=sys.stderr,
        )
    else:
        error = result.get("error", "")
        # 标记区不存在时给出更友好的提示
        if "marker" in error.lower() or "not found" in error.lower():
            print(
                t(
                    "cli.messages.agents_md_auto_sync_no_marker",
                    default="[Auto Sync] AGENTS.md 标记区不存在，跳过同步。请先运行 `cw rule insert-block` 插入标记块。",
                ),
                file=sys.stderr,
            )
        else:
            print(
                t(
                    "cli.messages.agents_md_auto_sync_skipped",
                    error=error,
                    default=f"[Auto Sync] AGENTS.md 同步跳过：{error}",
                ),
                file=sys.stderr,
            )


def main():
    """MCP 服务器入口

    启动流程：
    1. create_mcp_server() 创建服务器实例并注册所有 MCP 工具
    2. _auto_sync_agents_md() 自动同步 AGENTS.md（fail-soft，不阻断启动）
    3. server.run() 启动 stdio 传输
    """
    server = create_mcp_server()
    # 启动时自动同步 AGENTS.md（C2 新增）
    sync_result = _auto_sync_agents_md()
    _print_auto_sync_summary(sync_result)
    server.run()


if __name__ == "__main__":
    main()
