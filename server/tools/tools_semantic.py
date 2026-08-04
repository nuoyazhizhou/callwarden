"""语义与外部符号面：语义搜索/外部符号/GC 备份审计

拆分自 server/mcp_server.py（1260-1590 行区间），由 register(mcp) 注册。
"""

# [L2] 语义搜索工具（semantic_search / find_similar_functions / embed_*）
# [L11] 外部符号与 GC 备份审计工具（get_project_dependencies / gc_archive_list 等）

from mcp.server.fastmcp import FastMCP

from .._mcp_common import get_db
from ...i18n import t


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
