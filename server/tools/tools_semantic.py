"""语义与外部符号面：语义搜索/外部符号/GC 备份审计

拆分自 server/mcp_server.py（1260-1590 行区间），由 register(mcp) 注册。

H4B-C（T-1786590214634-9e740cdc-h4b-compat-read）：compatibility read HTTP cutover
- H0 capability registry（.trae-cn/evidence/http-daemon-capability-matrix.json）：
  本模块 19 个工具全部 backend=python_compat、daemon_rpc_method=none。
- dispatch.rs 无任何 semantic.* / ownership.* / gc_* 等 RPC 分支（DaemonStateExt
  trait 默认返回 method_not_found）。因此 HTTP 模式下禁止调用 _call_daemon_rpc
  指向不存在的 RPC——伪路由会在 HTTP 模式抛 method_not_found，违反 fail-closed 契约。
- 本模块工具在 HTTP 模式下 fail-closed：经 _http_unsupported() 返回结构化
  unsupported 错误，不直连本地 SQLite（不构造 CodeGraphDB，无 SQLite fallback）；
  非 HTTP（legacy）模式保持本地 get_db() 执行，公开方法语义不变。
- compat_route 扩展（把 python_compat 方法注册到 H3 compat worker）由
  h4b-registry-docs（...h4b-registry-docs）承接；本任务不触碰 Rust/compat_registry。
"""

# [L2] 语义搜索工具（semantic_search / find_similar_functions / embed_*）
# [L11] 外部符号与 GC 备份审计工具（get_project_dependencies / gc_archive_list 等）

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import get_db
from ...db import CodeGraphDB
from ...i18n import t
from callwarden.server.daemon_client import route_worker_call

# H4C-2 第二批（T-1786747295213-64204cce）：语义/外部符号组只读工具接入 compat
# worker。注意：必须用顶层 `server.compat_registry` 导入，与 compat_worker.py
# 保持同一模块单例（模块单例风险，见 tools_query.py L41-49 注释）。
from server.compat_registry import (  # noqa: E402
    SCOPE_WORKSPACE,
    CompatCallContext,
    register_compat_routes,
)

from ..daemon_client import route_rpc as _route


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def semantic_search(query: str, top_k: int = 5) -> list:
        """语义搜索：用自然语言查找相关函数

        Args:
            query: 自然语言查询（如"处理用户认证的函数"）
            top_k: 返回结果数量（默认 5）

        Returns:
            匹配函数列表，含 qualified_name、file_path、相似度分数
        """
        return _route('semantic_search', {"query": query, "top_k": top_k}, 'READ_ONLY')

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
        return _route('find_similar_functions', {"qualified_name": qualified_name, "threshold": threshold, "top_k": top_k}, 'READ_ONLY')

    @mcp.tool()
    def embed_symbols(force: bool = False) -> dict:
        """为所有函数生成向量嵌入（首次使用前需执行）

        Args:
            force: 是否强制重新嵌入已有函数

        Returns:
            嵌入统计（总数、成功、跳过、失败）
        """
        _res = _route('task.job_submit', {**{"force": force}, "job_type": "embed", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def embed_single_symbol(symbol_hash: str) -> dict:
        """为单个函数生成向量嵌入

        Args:
            symbol_hash: 函数的内容哈希

        Returns:
            {"success": bool, "symbol_hash": str, "message": str}
        """
        _res = _route('task.job_submit', {**{"symbol_hash": symbol_hash}, "job_type": "embed_single", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

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
        return _route('get_symbol_commit_history', {"symbol_hash": symbol_hash, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def parse_codeowners(file_path: str = "") -> list:
        """解析 CODEOWNERS 文件，返回所有权规则列表（不写入数据库）

        用于预览 CODEOWNERS 文件内容，不执行导入。

        Args:
            file_path: CODEOWNERS 文件路径（为空则使用默认路径 .github/CODEOWNERS）

        Returns:
            所有权规则列表（pattern / owners）
        """
        return _route('parse_codeowners', {"file_path": file_path}, 'READ_ONLY')

    @mcp.tool()
    def import_codeowners() -> dict:
        """从 CODEOWNERS 文件导入所有权到数据库

        解析 .github/CODEOWNERS 或 CODEOWNERS 文件，将路径模式与所有者关联写入 file_owners 表。

        Returns:
            导入统计（规则数 / 已关联文件数 / 未匹配文件数）
        """
        _res = _route('task.job_submit', {**{}, "job_type": "codeowners", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def import_git_blame() -> dict:
        """从 git log 导入每个文件最近一次提交者信息作为所有权补充

        对每个 file_instance 执行 git log -1 获取最近提交者，写入 file_owners 表（source=git_blame）。
        适用于没有 CODEOWNERS 文件的项目，或作为 CODEOWNERS 的补充。

        Returns:
            导入统计（文件数 / 成功 / 失败）
        """
        _res = _route('task.job_submit', {**{}, "job_type": "git_blame", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def get_project_dependencies(languages: list = None) -> dict:
        """读取项目直接依赖清单，不展开传递依赖"""
        return _route('get_project_dependencies', {"languages": languages}, 'READ_ONLY')

    @mcp.tool()
    def import_project_dependencies() -> dict:
        """导入项目直接依赖的第一层外部符号"""
        _res = _route('task.job_submit', {**{}, "job_type": "project_deps", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def prune_external_symbols(keep_project_deps: bool = True,
                               package_names: list = None,
                               vacuum: bool = False) -> dict:
        """清理外部符号；可保留项目直接依赖并可 VACUUM 释放空间"""
        _res = _route('task.job_submit', {**{"keep_project_deps": keep_project_deps, "package_names": package_names, "vacuum": vacuum}, "job_type": "prune_external", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

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
        return _route('admin.gc_retention', {"older_than_days": older_than_days, "keep_versions": keep_versions, "include_external": include_external, "external_stale_days": external_stale_days, "dry_run": dry_run, "backup": backup, "vacuum": vacuum, "save_policy": save_policy}, 'READ_ONLY')

    @mcp.tool()
    def gc_policy_get() -> dict:
        """读取当前 workspace 的 GC retention 策略"""
        return _route('admin.gc_policy_get', {}, 'READ_ONLY')

    @mcp.tool()
    def gc_policy_set(older_than_days: int = None,
                      keep_versions: int = None,
                      include_external: bool = None,
                      external_stale_days: int = None,
                      backup_enabled: bool = None,
                      vacuum_enabled: bool = None) -> dict:
        """更新当前 workspace 的 GC retention 策略"""
        return _route('admin.gc_policy_set', {"older_than_days": older_than_days, "keep_versions": keep_versions, "include_external": include_external, "external_stale_days": external_stale_days, "backup_enabled": backup_enabled, "vacuum_enabled": vacuum_enabled}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def gc_archive_list(limit: int = 20) -> list:
        """列出当前数据库目录下的 gc_archives/*.db.gz 备份文件

        Args:
            limit: 最多返回多少条（默认 20，按 mtime 倒序）

        Returns:
            备份文件列表，每条含 path/name/size/mtime/reason
        """
        return _route('admin.gc_archive_list', {"limit": limit}, 'READ_ONLY')

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
        return _route('admin.gc_archive_inspect', {"path": path}, 'READ_ONLY')

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
        return _route('admin.gc_audit_list', {"limit": limit, "operation": operation}, 'READ_ONLY')

    @mcp.tool()
    def gc_audit_get(audit_id: int) -> dict:
        """查询单条 GC 审计记录详情

        Args:
            audit_id: gc_runs.id

        Returns:
            审计记录 dict（含反序列化的 JSON 字段），不存在返回空 dict
        """
        return _route('admin.gc_audit_get', {"audit_id": audit_id}, 'READ_ONLY')

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
        return _route('admin.gc_archive_import', {"path": path, "file_path": file_path, "package_name": package_name, "dry_run": dry_run}, 'PROTECTED_MUTATION')


# ============================================================
# H4C-2 第二批（T-1786747295213-64204cce）：语义/外部符号组只读工具 worker handler
# ============================================================
# 接入说明（用户三项决策，见任务描述）：
# - handler 定义在工具模块内，由 compat_worker.handle_frame 通用派发按 registry
#   分发；本模块被 worker 装配 import 后模块级注册随之执行；
# - 轻量只读绑定：object.__new__(CodeGraphDB) 绕过 __init__，注入 ctx.conn（worker
#   mode=ro 只读连接）+ ctx.workspace_id 后复用 db 层查询方法（与 tools_query.py 同款）；
# - 写/index_write 工具不接入（fail-closed）：embed_symbols / embed_single_symbol
#   （index_write）、import_codeowners / import_git_blame / import_project_dependencies /
#   prune_external_symbols / gc_*（governance_write，矩阵已标，不接入 worker）。
_SEMANTIC_COMPAT_SCOPE = SCOPE_WORKSPACE  # 矩阵 workspace_scoped


def _bind_readonly_db(ctx: CompatCallContext) -> CodeGraphDB:
    """轻量只读绑定：绕过 CodeGraphDB.__init__，注入 worker 只读连接与显式 workspace。

    与 tools_query.py 同款：ctx.conn 由 compat_worker 用 `file:{db_path}?mode=ro`
    打开（read_only 契约）；active_workspace 注入 ctx.workspace_id，db 层查询基于
    `_get_active_workspace_id()` 过滤；workspace_root 从 workspaces 表解析
    （parse_codeowners / get_project_dependencies 等文件路径依赖）。
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


def _h_semantic_search(ctx: CompatCallContext) -> Any:
    """worker handler：语义搜索（只读）"""
    return _bind_readonly_db(ctx).semantic_search(
        query=ctx.params.get("query", ""),
        top_k=ctx.params.get("top_k", 5),
    )


def _h_find_similar_functions(ctx: CompatCallContext) -> Any:
    """worker handler：查找语义相似函数（只读）"""
    return _bind_readonly_db(ctx).find_similar_functions(
        qualified_name=ctx.params.get("qualified_name", ""),
        threshold=ctx.params.get("threshold", 0.8),
        top_k=ctx.params.get("top_k", 20),
    )


def _h_get_symbol_commit_history(ctx: CompatCallContext) -> Any:
    """worker handler：符号 Git 变更历史（只读）"""
    return _bind_readonly_db(ctx).get_symbol_commit_history(
        ctx.params.get("symbol_hash", ""),
        ctx.params.get("limit", 20),
    )


def _h_parse_codeowners(ctx: CompatCallContext) -> Any:
    """worker handler：解析 CODEOWNERS（只读，不写入数据库）"""
    return _bind_readonly_db(ctx).parse_codeowners(
        ctx.params.get("file_path") or None
    )


def _h_get_project_dependencies(ctx: CompatCallContext) -> Any:
    """worker handler：项目直接依赖清单（只读）"""
    return _bind_readonly_db(ctx).get_project_dependencies(
        languages=ctx.params.get("languages")
    )


# 语义/外部符号组只读白名单（5 个）：跳过 embed_symbols / embed_single_symbol
# （index_write）与 import_codeowners / import_git_blame / import_project_dependencies /
# prune_external_symbols / gc_*（governance_write，不接入 worker）。
_SEMANTIC_READ_ONLY_METHODS: Dict[str, Any] = {
    "semantic_search": _h_semantic_search,
    "find_similar_functions": _h_find_similar_functions,
    "get_symbol_commit_history": _h_get_symbol_commit_history,
    "parse_codeowners": _h_parse_codeowners,
    "get_project_dependencies": _h_get_project_dependencies,
}

# 模块级注册：worker 装配 import 本模块时执行，注册到 compat_registry 单例并
# 同步 RUST_COMPAT_ROUTE（Rust 侧 http_server.rs 白名单在步骤#2 同步）。
register_compat_routes(
    _SEMANTIC_READ_ONLY_METHODS,
    workspace_scope=_SEMANTIC_COMPAT_SCOPE,
    description="H4C-2 第二批语义/外部符号组只读工具（5 个，T-1786747295213-64204cce 步骤#1）",
)
