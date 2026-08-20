"""查询面：符号/调用链/注释恢复/缺陷检测/覆盖率/模块图（原 [L1/L2/L3/L9/L10]）

拆分自 server/mcp_server.py（122-557 行区间），由 register(mcp) 注册。

M2.3（T-1786529505247-9d083e54）：grep 的 daemon 入口为
DaemonClient.query_grep（enterprise/auto 走 RPC query.grep，daemon 不可用
fail-closed，不回退本地 SQLite）。MCP file_grep 工具位于 tools_workspace.py
（M2.3 所有权白名单外），不在本模块，本地 grep 由该工具/CLI 承担。

M2.4（T-1786539379174-90f74174）：query.issues 的 daemon 入口为
DaemonClient.query_issues（enterprise/auto 走 RPC query.issues 按符号查询
semgrep+guardrail findings，daemon 不可用 fail-closed，local 模式返回 None）。
本模块的 get_issue_summary / find_issues 是**全局正则缺陷扫描**
（IssueAnalyzerMixin：missing_comment/todo_fixme/unwrap_call/hardcoded_path 等），
语义与按符号查询的 query.issues 不对应，保留本地 SQL 执行；按符号查询缺陷
请使用 get_symbol_issues（tools_task.py，内部走 DaemonClient.get_symbol_issues）。

H4B-N（T-1786590214634-9e740cdc-h4b-native-read）：HTTP daemon 原生读/查路由归类说明
- rust_native（H4A 已建路由，走 _get_daemon_client）：get_stats/search_symbols/
  get_symbol/get_symbol_location/get_file_symbols/get_callers/get_callees/
  get_topological_order/get_call_chain_down/detect_cycles（10 个）
  W2-1（T-1786840097330-dec66710）：get_uncommented_symbols/get_module_call_stats/
  get_semgrep_stats 迁移 rust_native（HTTP 分支直连 HttpDaemonRpcClient 便捷方法，
  走 snapshot query_db_path 访问主库全表），共 13 个
- 本地 SQL（矩阵标 rust_native 但 daemon_rpc_method=none / 语义不映射）：
  get_issue_summary/find_issues/get_test_coverage（M2.4/M2.5 说明，保留本地执行）
- python_compat（归 H4B-compat-read，待 compat_route 扩展后迁移）：其余 16 个
  工具。dispatch.rs 无 query.symbol_history/query.top_callers/query.semgrep_findings
  等 RPC 分支，建立指向不存在 RPC 的伪路由会在 HTTP 模式抛 method_not_found，
  违反 fail-closed 契约；故恢复本地 SQL 执行并以注释标注矩阵归类。
"""

# [L1+L2+L3] 查询类工具（get_stats 属 L1；search_symbols/get_symbol 等
# [L3] 高级调用链与模块图工具（get_call_chain_down / export_module_graph 等）

import os
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _get_daemon_client, _get_db_path_for_daemon, get_db
from ...db import CodeGraphDB
from callwarden.server.daemon_client import route_worker_call
from callwarden.config import is_http_transport_enabled, norm_path

# H4C-2（T-1786716190783-ba187c88 步骤#0）：符号组只读工具接入 compat worker。
# 注意：必须用顶层 `server.compat_registry` 导入，与 compat_worker.py 保持同一
# 模块单例；若用 `callwarden.server.compat_registry` 会得到另一个模块对象，
# 注册将落到错误的 registry 单例（模块单例风险）。
from server.compat_registry import (  # noqa: E402
    SCOPE_WORKSPACE,
    CompatCallContext,
    register_compat_routes,
)

from ..daemon_client import route_rpc as _route


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_stats() -> dict:
        """获取代码知识图谱统计信息（文件数、函数数、调用关系数等）"""
        return _route('query.stats', {}, 'READ_ONLY')

    @mcp.tool()
    def search_symbols(query: str, kind: str = "", limit: int = 20) -> list:
        """搜索符号（函数、类、结构体等）

        Args:
            query: 搜索关键词（符号名模糊匹配）
            kind: 按类型过滤（fn/method/class/struct/enum/trait/interface 等）
            limit: 返回数量限制
        """
        return _route('query.search', {"query": query, "kind": kind, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_symbol(qualified_name: str) -> Optional[dict]:
        """获取符号的详细信息

        M2.2（T-1786526643663-594ee010）：路由到 DaemonClient.get_symbol，
        enterprise/auto 模式走 daemon RPC query.symbol；daemon 不可用时
        fail-closed（仅 local 模式回退本地 SQL）。

        Args:
            qualified_name: 符号限定名（如 crate::module::function_name）
        """
        return _route('query.symbol', {"qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def get_symbol_location(name: str, file_path: str = "") -> Optional[dict]:
        """获取符号的位置信息（文件、行号、列号）

        Args:
            name: 符号名
            file_path: 可选的文件路径（用于消除重名歧义）
        """
        return _route('query.symbol_location', {"name": name, "file_path": file_path}, 'READ_ONLY')

    @mcp.tool()
    def get_file_symbols(file_path: str) -> list:
        """获取文件中的所有符号

        M2.1（T-1786519351240-73127ab4）：路由到 DaemonClient.get_file_symbols，
        enterprise/auto 模式走 daemon RPC query.file；daemon 不可用时 fail-closed
        （仅 local 模式回退本地 SQL）。

        Args:
            file_path: 文件路径（相对项目根目录）
        """
        return _route('query.file', {"file_path": file_path}, 'READ_ONLY')

    @mcp.tool()
    def get_callers(callee_name: str, qualified_name: Optional[str] = None) -> list:
        """查询指定函数的所有调用者（谁调用了它）

        P28：大规模项目推荐传入 qualified_name 避免短名跨模块误匹配

        Args:
            callee_name: 被调用的函数名（简名）
            qualified_name: 可选，完整限定名（如 module::Class::method），
                           传入时精确匹配该符号，避免多个模块同名函数误匹配
        """
        return _route('query.callers', {"callee_name": callee_name, "qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def get_callees(caller_name: str, qualified_name: Optional[str] = None) -> list:
        """查询指定函数调用了哪些函数（它调用了谁）

        P28：大规模项目推荐传入 qualified_name 避免短名跨模块误匹配

        Args:
            caller_name: 调用者函数名（简名）
            qualified_name: 可选，完整限定名（如 module::Class::method），
                           传入时精确匹配该符号，避免多个模块同名函数误匹配
        """
        return _route('query.callees', {"caller_name": caller_name, "qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def get_symbol_history(qualified_name: str) -> list:
        """获取符号的版本历史（所有历史版本）

        Args:
            qualified_name: 符号限定名
        """
        return _route('get_symbol_history', {"qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def get_file_history(file_path: str) -> list:
        """获取文件的版本历史

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.file_history，经
        snapshot query_db_path 访问主库 file_versions JOIN file_instances，
        注入权威 workspace_instance_id）；绝对路径在 Python 工具层规范化为
        rel_path（复刻 db 层 `os.path.relpath(file_path, workspace_root)`，
        workspaces.root_path 为真相源）。local/legacy 模式保留原路由语义
        （local 走本地 db 回退，enterprise/auto 走 compat worker）。

        Args:
            file_path: 文件路径（相对或绝对路径）
        """
        return _route('query.file_history', {"file_path": file_path}, 'READ_ONLY')

    @mcp.tool()
    def get_recent_changes(since: str = "1d") -> dict:
        """获取近期变更的文件和符号

        Args:
            since: 时间范围（如 1h/1d/1w/2024-01-01）
        """
        return _route('get_recent_changes', {"since": since}, 'READ_ONLY')

    @mcp.tool()
    def get_topological_order(limit: int = 50) -> list:
        """获取按依赖拓扑排序的符号列表（被调用最多的在前）

        Args:
            limit: 返回数量限制
        """
        return _route('query.topological_order', {"limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_impact(qualified_name: str, max_depth: int = 10) -> dict:
        """影响面分析：向上追踪所有调用该函数的上游函数

        Args:
            qualified_name: 起始函数的限定名
            max_depth: 最大追踪深度（默认 10）
        """
        return _route('get_impact', {"qualified_name": qualified_name, "max_depth": max_depth}, 'READ_ONLY')

    @mcp.tool()
    def get_call_chain_down(qualified_name: str, max_depth: int = 10) -> dict:
        """调用链向下：追踪该函数调用的所有下游函数

        Args:
            qualified_name: 起始函数的限定名
            max_depth: 最大追踪深度（默认 10）
        """
        return _route('query.call_chain_down', {"qualified_name": qualified_name, "max_depth": max_depth}, 'READ_ONLY')

    @mcp.tool()
    def get_top_callers(limit: int = 20, kind: str = "fn", module_filter: str = "") -> list:
        """获取被调用次数最多的函数排行

        Args:
            limit: 返回数量限制（默认 20）
            kind: 符号类型（默认 fn，可选 struct/enum/trait 等）
            module_filter: 模块过滤（前缀匹配）
        """
        return _route('get_top_callers', {"limit": limit, "kind": kind, "module_filter": module_filter}, 'READ_ONLY')

    @mcp.tool()
    def get_orphan_symbols(kind: str = "fn", module_filter: str = "", limit: int = 100) -> list:
        """获取未被调用的孤立符号

        Args:
            kind: 符号类型（默认 fn，可选 struct/enum/trait 等）
            module_filter: 模块过滤（前缀匹配）
            limit: 返回数量限制（默认 100）
        """
        return _route('get_orphan_symbols', {"kind": kind, "module_filter": module_filter, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_deepest_functions(limit: int = 20, module_filter: str = "", kind: str = "fn") -> list:
        """获取调用深度最深的函数排行

        Args:
            limit: 返回数量限制（默认 20）
            module_filter: 模块过滤（前缀匹配）
            kind: 符号类型（默认 fn）
        """
        return _route('get_deepest_functions', {"limit": limit, "module_filter": module_filter, "kind": kind}, 'READ_ONLY')

    @mcp.tool()
    def get_module_call_stats(limit: int = 30) -> list:
        """获取模块间调用统计

        W2-1（T-1786840097330-dec66710）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.module_call_stats，
        经 snapshot query_db_path 访问主库 call_versions，注入权威
        workspace_instance_id）；local/legacy 模式保留原路由语义
        （local 走本地 db 回退，enterprise/auto 走 compat worker）。

        Args:
            limit: 返回数量限制（默认 30）
        """
        return _route('query.module_call_stats', {"limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def detect_cycles(max_depth: int = 10) -> list:
        """检测循环调用

        Args:
            max_depth: 最大追踪深度（默认 10）

        Returns:
            检测到的循环列表，每个循环是一个函数名列表
        """
        return _route('query.detect_cycles', {"max_depth": max_depth}, 'READ_ONLY')

    @mcp.tool()
    def get_comment_from_version(spec: str) -> Optional[dict]:
        """从历史版本中获取注释（用于 git checkout 后恢复注释）

        Args:
            spec: 版本规格（格式: 文件路径:符号名@版本号 或 文件路径:行号）
        """
        return _route('get_comment_from_version', {"spec": spec}, 'READ_ONLY')

    @mcp.tool()
    def restore_comment(spec: str, preview: bool = True) -> dict:
        """恢复函数注释（从历史版本）

        Args:
            spec: 版本规格（格式: 文件路径:符号名@版本号 或 文件路径:行号）
            preview: 预览模式（只显示不写入）
        """
        return _route('edit.restore_comment', {"spec": spec, "preview": preview}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_issue_summary() -> dict:
        """获取缺陷检测汇总统计

        M2.4（T-1786539379174-90f74174）：本工具为全局正则缺陷扫描（本地 SQL），
        与按符号查询的 daemon RPC query.issues 语义不对应，不迁移 daemon；
        query.issues 的 daemon 入口是 DaemonClient.query_issues（按符号查询）。
        """
        return _route('get_issue_summary', {}, 'READ_ONLY')

    @mcp.tool()
    def find_issues(issue_type: str = "", limit: int = 30) -> list:
        """查找代码缺陷（缺注释、硬编码、unwrap 等）

        M2.4（T-1786539379174-90f74174）：本工具为全局正则缺陷扫描（本地 SQL），
        与按符号查询的 daemon RPC query.issues 语义不对应，不迁移 daemon；
        query.issues 的 daemon 入口是 DaemonClient.query_issues（按符号查询）。

        Args:
            issue_type: 缺陷类型（missing_comment/todo_fixme/unwrap_call/expect_call/panic_macro/unsafe_block/hardcoded_path/hardcoded_url/magic_number 等）
            limit: 返回数量限制
        """
        return _route('find_issues', {"issue_type": issue_type, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_semgrep_stats() -> dict:
        """获取 Semgrep 缺陷统计（按严重程度、语言、规则分组）

        W2-1（T-1786840097330-dec66710）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.semgrep_stats，
        经 snapshot query_db_path 访问主库 semgrep_findings，注入权威
        workspace_instance_id）；local/legacy 模式保留原路由语义
        （local 走本地 db 回退，enterprise/auto 走 compat worker）。
        """
        return _route('query.semgrep_stats', {}, 'READ_ONLY')

    @mcp.tool()
    def get_semgrep_findings(severity: str = "", language: str = "",
                             rule_id: str = "", limit: int = 50) -> list:
        """查询 Semgrep 发现的缺陷

        W3-3（T-1786861820151-deb64c48）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.semgrep_findings，
        经 snapshot query_db_path 访问主库 semgrep_findings JOIN
        file_instances，注入权威 workspace_instance_id）；local/legacy 模式
        保留原路由语义（local 走本地 db 回退，enterprise/auto 走 compat
        worker，经 _SYMBOL_READ_ONLY_METHODS 白名单外的仅 legacy/local
        回退路径）。

        Args:
            severity: 按严重程度过滤（ERROR/WARNING/INFO）
            language: 按语言过滤（rust/typescript/python/kotlin 等）
            rule_id: 按规则 ID 过滤（模糊匹配）
            limit: 返回数量限制
        """
        return _route('query.semgrep_findings', {"severity": severity, "language": language, "rule_id": rule_id, "limit": limit}, 'READ_ONLY')

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
        _res = _route('task.job_submit', {**{"config": config, "languages": languages, "timeout": timeout}, "job_type": "semgrep_scan", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

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
        _res = _route('task.job_submit', {**{"base_branch": base_branch, "head": head, "config": config, "languages": languages, "timeout": timeout}, "job_type": "semgrep_incremental", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def get_comment_coverage(group_by: str = "module") -> dict:
        """获取注释覆盖率统计

        Args:
            group_by: 分组方式：module（按模块）、file（按文件）、kind（按类型）
        """
        return _route('get_comment_coverage', {"group_by": group_by}, 'READ_ONLY')

    @mcp.tool()
    def get_uncommented_symbols(kind: str = "fn",
                                module_filter: str = "",
                                limit: int = 100) -> list:
        """获取未注释的符号列表

        W2-1（T-1786840097330-dec66710）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.uncommented_symbols，
        经 snapshot query_db_path 访问主库 file_symbol_versions/symbol_contents，
        注入权威 workspace_instance_id）；local/legacy 模式保留原路由语义
        （local 走本地 db 回退，enterprise/auto 走 compat worker）。

        Args:
            kind: 符号类型（默认 fn，可选 struct/enum/trait 等）
            module_filter: 模块过滤（前缀匹配）
            limit: 返回数量限制（默认 100）
        """
        return _route('query.uncommented_symbols', {"kind": kind, "module_filter": module_filter, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_call_heatmap(group_by: str = "module", top_n: int = 20) -> list:
        """获取函数调用频率热力图数据

        Args:
            group_by: 分组方式（module 或 file）
            top_n: 返回数量限制（默认 20）
        """
        return _route('get_call_heatmap', {"group_by": group_by, "top_n": top_n}, 'READ_ONLY')

    @mcp.tool()
    def get_test_coverage() -> dict:
        """获取测试覆盖率统计（test函数分布）

        M2.5（T-1786584287058-7f712ff4）：本工具为无参全项目测试率统计
        （本地 SQL），与按符号查询的 daemon RPC query.tests 语义不对应，
        不迁移 daemon（遵循 M2.4 get_issue_summary 先例）；
        query.tests 的 daemon 入口是 DaemonClient.query_tests（按符号查询）。
        """
        return _route('get_test_coverage', {}, 'READ_ONLY')

    @mcp.tool()
    def export_module_graph(format: str = "mermaid") -> str:
        """导出模块依赖图

        Args:
            format: 输出格式（mermaid 或 dot）
        """
        return _route('export_module_graph', {"format": format}, 'READ_ONLY')

    @mcp.tool()
    def restore_all_comments(preview: bool = True,
                             file_filter: str = "") -> dict:
        """批量恢复所有有注释历史的函数注释

        Args:
            preview: 预览模式（只显示不写入）
            file_filter: 只恢复指定文件的注释（文件路径前缀匹配）
        """
        return _route('edit.restore_all_comments', {"preview": preview, "file_filter": file_filter}, 'PROTECTED_MUTATION')


# ============================================================
# H4C-2（T-1786716190783-ba187c88 步骤#0）：符号组只读工具 worker handler
# ============================================================
# 接入说明（用户三项决策，见任务描述）：
# - handler 定义在工具模块内，由 compat_worker.handle_frame 通用派发按 registry
#   分发；本模块被 worker 装配 import 后模块级注册随之执行；
# - 轻量只读绑定：object.__new__(CodeGraphDB) 绕过 __init__（含 PRAGMA WAL /
#   schema 迁移 / workspace 注册等写副作用），注入 ctx.conn（worker 的
#   mode=ro 只读连接）+ ctx.workspace_id 后复用 db 层查询方法；
# - workspace_root 从用户级库 workspaces 表解析（get_file_history 依赖）；
# - 写语义工具不接入（fail-closed）：run_semgrep_scan / scan_semgrep_incremental
#   （扫描并保存 findings，属写语义，矩阵分类待收尾修正）；
# - get_uncommented_symbols 已在 H4C-1 默认 registry 注册，跳过避免重复 ValueError。

_SYMBOL_COMPAT_SCOPE = SCOPE_WORKSPACE  # 矩阵 workspace_scoped


def _bind_readonly_db(ctx: CompatCallContext) -> CodeGraphDB:
    """轻量只读绑定：绕过 CodeGraphDB.__init__，注入 worker 只读连接与显式 workspace。

    - ctx.conn 由 compat_worker 用 `file:{db_path}?mode=ro` 打开（read_only 契约）；
    - active_workspace 注入 ctx.workspace_id，db 层查询基于
      `_get_active_workspace_id()` 过滤，不查询 active workspace；
    - workspace_root 从 ctx.conn 对应库的 workspaces 表解析（get_file_history 等
      方法依赖 os.path.relpath(file_path, self.workspace_root)）。
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


def _h_get_symbol_history(ctx: CompatCallContext) -> Any:
    """worker handler：符号版本历史（只读）"""
    return _bind_readonly_db(ctx).get_symbol_history(ctx.params.get("qualified_name", ""))


def _h_get_recent_changes(ctx: CompatCallContext) -> Any:
    """worker handler：近期变更（只读）"""
    return _bind_readonly_db(ctx).get_recent_changes(
        since=ctx.params.get("since", "1d")
    )


def _h_get_impact(ctx: CompatCallContext) -> Any:
    """worker handler：影响面分析（只读）"""
    return _bind_readonly_db(ctx).get_call_chain_up(
        ctx.params.get("qualified_name", ""),
        max_depth=ctx.params.get("max_depth", 10),
    )


def _h_get_top_callers(ctx: CompatCallContext) -> Any:
    """worker handler：被调用最多函数排行（只读）"""
    return _bind_readonly_db(ctx).get_top_callers(
        limit=ctx.params.get("limit", 20),
        kind=ctx.params.get("kind", "fn"),
        module_filter=ctx.params.get("module_filter", ""),
    )


def _h_get_orphan_symbols(ctx: CompatCallContext) -> Any:
    """worker handler：孤立符号（只读）"""
    return _bind_readonly_db(ctx).get_orphan_symbols(
        kind=ctx.params.get("kind", "fn"),
        module_filter=ctx.params.get("module_filter", ""),
        limit=ctx.params.get("limit", 100),
    )


def _h_get_deepest_functions(ctx: CompatCallContext) -> Any:
    """worker handler：调用深度最深函数排行（只读）"""
    return _bind_readonly_db(ctx).get_deepest_functions(
        limit=ctx.params.get("limit", 20),
        module_filter=ctx.params.get("module_filter", ""),
        kind=ctx.params.get("kind", "fn"),
    )


def _h_get_comment_from_version(ctx: CompatCallContext) -> Any:
    """worker handler：从历史版本获取注释（只读）"""
    return _bind_readonly_db(ctx).get_comment_from_version(
        ctx.params.get("spec", "")
    )


def _h_get_issue_summary(ctx: CompatCallContext) -> Any:
    """worker handler：缺陷检测汇总（只读）"""
    return _bind_readonly_db(ctx).get_issue_summary()


def _h_find_issues(ctx: CompatCallContext) -> Any:
    """worker handler：查找代码缺陷（只读）"""
    return _bind_readonly_db(ctx).get_function_issues(
        issue_filter=ctx.params.get("issue_type", "") or None,
        limit=ctx.params.get("limit", 30),
    )


def _h_get_comment_coverage(ctx: CompatCallContext) -> Any:
    """worker handler：注释覆盖率统计（只读）"""
    return _bind_readonly_db(ctx).get_comment_coverage(
        group_by=ctx.params.get("group_by", "module")
    )


def _h_get_call_heatmap(ctx: CompatCallContext) -> Any:
    """worker handler：调用频率热力图（只读）"""
    return _bind_readonly_db(ctx).get_call_heatmap(
        group_by=ctx.params.get("group_by", "module"),
        top_n=ctx.params.get("top_n", 20),
    )


def _h_get_test_coverage(ctx: CompatCallContext) -> Any:
    """worker handler：测试覆盖率统计（只读）"""
    return _bind_readonly_db(ctx).get_test_coverage()


def _h_export_module_graph(ctx: CompatCallContext) -> Any:
    """worker handler：导出模块依赖图（只读）"""
    return _bind_readonly_db(ctx).export_module_graph(
        format=ctx.params.get("format", "mermaid")
    )


# 符号组只读白名单（13 个）：跳过 run_semgrep_scan / scan_semgrep_incremental
# （写语义，fail-closed）；get_uncommented_symbols / get_module_call_stats /
# get_semgrep_stats 已 W2-1 迁移 rust_native（T-1786840097330-dec66710）、
# get_semgrep_findings 已 W3-3 迁移 rust_native（T-1786861820151-deb64c48）、
# get_file_history 已 W4-1 迁移 rust_native（T-1786886251769-22b94ee8-sub-1），
# 工具层函数体在 HTTP 模式直连 HttpDaemonRpcClient 便捷方法，见各定义处。
_SYMBOL_READ_ONLY_METHODS: Dict[str, Any] = {
    "get_symbol_history": _h_get_symbol_history,
    "get_recent_changes": _h_get_recent_changes,
    "get_impact": _h_get_impact,
    "get_top_callers": _h_get_top_callers,
    "get_orphan_symbols": _h_get_orphan_symbols,
    "get_deepest_functions": _h_get_deepest_functions,
    "get_comment_from_version": _h_get_comment_from_version,
    "get_issue_summary": _h_get_issue_summary,
    "find_issues": _h_find_issues,
    "get_comment_coverage": _h_get_comment_coverage,
    "get_call_heatmap": _h_get_call_heatmap,
    "get_test_coverage": _h_get_test_coverage,
    "export_module_graph": _h_export_module_graph,
}

# 模块级注册：worker 装配 import 本模块时执行，注册到 compat_registry 单例并
# 同步 RUST_COMPAT_ROUTE（Rust 侧 http_server.rs 白名单在步骤#2 同步）。
register_compat_routes(
    _SYMBOL_READ_ONLY_METHODS,
    workspace_scope=_SYMBOL_COMPAT_SCOPE,
    description="H4C-2 符号组只读工具（13 个，T-1786716190783-ba187c88 步骤#0；"
                "3 个 stats 已 W2-1 迁移 rust_native，get_semgrep_findings "
                "已 W3-3 迁移 rust_native，get_file_history 已 W4-1 迁移 rust_native）",
)
