"""工作区与文件面：构建刷新/工作区/Git/文件/状态/符号内容/度量/健康检查

拆分自 server/mcp_server.py（558-1259 行区间），由 register(mcp) 注册。

H4B-N（T-1786590214634-9e740cdc-h4b-native-read）：HTTP daemon 原生读/查路由归类说明
- rust_native（H4A 已建路由）：list_workspaces（workspace.list）、
  get_active_workspace（workspace.activate），与 HEAD 基线一致，保留。
- python_compat / legacy_local：其余 25 个工具全部由矩阵标注为 python_compat
  或 legacy_local（file_grep/file_list 为 legacy_local）。daemon dispatch.rs 无
  workspace.build_graph/workspace.refresh_file/file.read/git.*/metrics.* 等 RPC 分支，
  建立指向不存在 RPC 的伪路由会在 HTTP 模式抛 method_not_found，违反 fail-closed
  契约；故恢复本地 SQL/文件系统执行（与 HEAD 基线一致），待 H4B-C 扩展 compat_route
  后迁移。本文件无 H4B-N assigned 的 rust_native read/query 行。
"""

# [L1] 构建刷新与工作区管理工具（build_graph / list_workspaces / set_active_workspace 等）
# [L2] 文件操作与符号内容工具（file_read / file_grep / file_symbol_content 等）
# [L4] 代码度量与健康检查工具（get_code_metrics_summary / get_code_health_check 等）
# [L8] Git 集成工具（import_git_history / get_git_commits / get_commit_changes 等）

import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _call_daemon_rpc, get_db

from ..daemon_client import route_rpc as _route


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def build_graph() -> bool:
        """完整构建代码知识图谱（全量扫描）"""
        return _route('workspace.build_graph', {}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def refresh_file(file_path: str) -> bool:
        """刷新单个文件（增量更新）

        Args:
            file_path: 文件路径（相对或绝对路径）
        """
        return _route('workspace.file.refresh_file', {"file_path": file_path}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def list_workspaces() -> list:
        """列出所有工作区

        HTTP 模式（H6，CW_DAEMON_TRANSPORT=http）：经 RPC workspace.list
        （无参，Rust handler `handle_workspace_list` 按 peer uid 返回
        daemon_workspaces 行数组，无需 workspace_instance_id）。返回 daemon
        视图（每行字段：workspace_id/workspace_instance_id/snapshot_id/
        owner_uid/git_remote_url/git_head_commit_sha/client_view_root/
        host_real_root/toolchain_fingerprint/registered_at/last_active_at/
        status）。与 legacy db.list_workspaces()（workspaces 表行：
        id/name/root_path/created_at/is_active/description/active_task_id）
        字段差异做最小兼容映射：每行 client_view_root→root_path、name 用
        os.path.basename(client_view_root) 兜底；daemon 行无 is_active/
        created_at/description 字段。
        """
        return _route('workspace.list', {}, 'READ_ONLY')

    @mcp.tool()
    def register_workspace(name: str, root_path: str, description: str = "") -> int:
        """注册新工作区

        Args:
            name: 工作区名称（唯一）
            root_path: 工作区根目录绝对路径
            description: 描述

        HTTP 模式（H6，CW_DAEMON_TRANSPORT=http）：SQLite workspaces 表为
        真相源（先 db.register_workspace，name/root_path 重复时幂等返回已有
        id），再经 HttpDaemonRpcClient.workspace_register 同步 daemon 注册表
        （daemon_workspaces 是读面 workspace.list/status 的数据源，必须同步
        否则读面不可见）。daemon 不可用 → DaemonUnavailableError（fail-closed，
        禁止静默回退纯 SQL 造成双表分裂）；重试即自愈（register 两侧幂等）。
        """
        return _route('workspace.register', {"name": name, "client_view_root": root_path, "description": description}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def set_active_workspace(workspace_id_or_name: str) -> bool:
        """设置活动工作区

        Args:
            workspace_id_or_name: 工作区 ID（数字字符串）或名称

        HTTP 模式（H6）：SQLite workspaces 表为真相源（先
        db.set_active_workspace 更新 is_active，workspace 不存在返回 False），
        再经 HttpDaemonRpcClient.workspace_activate 同步 daemon 注册表状态。
        daemon 不可用 → DaemonUnavailableError（fail-closed，不静默成功）。
        """
        return _route('workspace.activate', {"workspace_id_or_name": workspace_id_or_name}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def delete_workspace(workspace_id_or_name: str) -> bool:
        """删除工作区（级联删除所有实例和版本）

        Args:
            workspace_id_or_name: 工作区 ID（数字字符串）或名称

        HTTP 模式（H6）：SQLite workspaces 表为真相源（先硬删，workspace
        不存在返回 False），再经 HttpDaemonRpcClient.workspace_remove 同步
        daemon 注册表（Rust remove 为 archive 软删语义，读面 owned ACL 已
        排除 archived 行）。daemon 不可用 → DaemonUnavailableError
        （fail-closed）；此时 SQLite 已删、daemon 行保留，重试会因 SQLite
        行不存在返回 False（不重复删），daemon 残留行由后续同 root 注册
        INSERT OR REPLACE 覆盖或 workspace_remove 归档自愈。
        """
        return _route('workspace.remove', {"workspace_id_or_name": workspace_id_or_name}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_active_workspace() -> Optional[dict]:
        """获取当前活动工作区信息

        HTTP 模式（H6，CW_DAEMON_TRANSPORT=http）：经 HttpDaemonRpcClient
        workspace_status 便捷方法（W1-1，T-1786808777378-bbcbf059）查询——
        便捷方法先 _ensure_remote_snapshot(db_path) 注册当前 workspace 拿权威
        workspace_instance_id 后调 workspace.status（Rust native 读方法，
        read_only + owned ACL：owner_uid 匹配且非 archived；缺注入时 Rust 强制
        require_str_param 返回 invalid_params——即修复前调 workspace.activate {}
        的缺陷）。"当前活动工作区"在 HTTP 模式下即当前配置 workspace 的
        daemon 视图（daemon_workspaces 行）。返回结构透传 daemon 行
        （workspace_id/workspace_instance_id/snapshot_id/owner_uid/
        git_remote_url/git_head_commit_sha/client_view_root/host_real_root/
        toolchain_fingerprint/registered_at/last_active_at/status），并做与
        legacy workspaces 表行（id/name/root_path/created_at/is_active/
        description/active_task_id）的最小兼容映射：client_view_root→root_path、
        host_real_root 保留、name 用 os.path.basename(client_view_root) 兜底；
        daemon 行无 is_active/created_at/description 字段（HTTP 模式以单
        workspace 语义替代 legacy is_active）。
        """
        return _route('workspace.status', {}, 'READ_ONLY')

    @mcp.tool()
    def file_read(file_path: str, offset: int = 0, limit: int = 200, include_context: bool = False) -> Optional[dict]:
        """读取文件内容（可选附带符号上下文）

        Agent 通过此工具读取文件，替代 IDE 内置 Read 工具。
        支持行号偏移和行数限制，避免一次性返回过大内容。

        L4 赋能：include_context=True 时合并返回文件中的符号列表 + 每个函数符号的
        调用方/被调用方摘要（top 3），减少 Agent 3+N 次 MCP 往返为 1 次。

        Args:
            file_path: 文件路径（相对工作区根目录或绝对路径）
            offset: 起始行号（从 0 开始，默认 0）
            limit: 读取行数（默认 200）
            include_context: 是否附带符号上下文（符号列表+调用方/被调用方摘要，默认 False）

        Returns:
            dict: {path, total_lines, offset, limit, content}，include_context=True 时
                  额外返回 {symbols, symbol_contexts}，文件不存在返回 None
        """
        return _route('workspace.file.read', {"file_path": file_path, "offset": offset, "limit": limit, "include_context": include_context}, 'READ_ONLY')

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
        return _route('workspace.file.grep', {"pattern": pattern, "path": path, "glob": glob, "output_mode": output_mode, "head_limit": head_limit}, 'READ_ONLY')

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
        return _route('workspace.file.list', {"path": path, "glob": glob}, 'READ_ONLY')

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
        return _route('workspace.file.symbol_content', {"file_path": file_path, "symbol_name": symbol_name}, 'READ_ONLY')

    @mcp.tool()
    def import_git_history(max_commits: int = 100) -> dict:
        """导入 Git 历史记录到数据库

        W4-4（T-1786886251769-22b94ee8-sub-4）：写面通道决策——本工具是
        governance_write（INSERT OR IGNORE git_commits）+ 依赖 git 子进程
        （workspace_root 下 .git + `git log`），不适合 rust_native 迁移；
        HTTP 模式 fail-closed（明确写面不支持声明，见 ledger §9.25），
        local/legacy 模式保持本地 SQL 语义（代码见下）。

        Args:
            max_commits: 最大导入 commit 数量（默认 100）

        Returns:
            导入结果，包含成功状态和导入数量
        """
        _res = _route('task.job_submit', {**{"max_commits": max_commits}, "job_type": "git_history", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def get_git_commits(limit: int = 20, offset: int = 0) -> list:
        """获取 Git commit 列表

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.git_commits，经
        snapshot query_db_path 访问主库 git_commits，注入权威
        workspace_instance_id）；local/legacy 模式保留本地 SQL 语义。

        Args:
            limit: 返回数量限制（默认 20）
            offset: 偏移量（默认 0）

        Returns:
            commit 列表，按时间倒序排列
        """
        return _route('query.git_commits', {"limit": limit, "offset": offset}, 'READ_ONLY')

    @mcp.tool()
    def get_commit_changes(commit_hash: str) -> dict:
        """获取指定 commit 的变更详情

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.git_commit_changes，
        经 snapshot query_db_path 访问主库 git_commits + git_file_changes，
        注入权威 workspace_instance_id）；local/legacy 模式保留本地 SQL 语义。

        Args:
            commit_hash: commit 哈希值

        Returns:
            commit 详情和变更文件列表
        """
        return _route('query.git_commit_changes', {"commit_hash": commit_hash}, 'READ_ONLY')

    @mcp.tool()
    def get_git_stats() -> dict:
        """获取 Git 集成统计信息

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.git_stats，经
        snapshot query_db_path 访问主库 git_commits + git_file_changes，
        注入权威 workspace_instance_id）；local/legacy 模式保留本地 SQL 语义。

        Returns:
            Git 相关统计数据（commit 数、文件变更数、变更类型分布）
        """
        return _route('query.git_stats', {}, 'READ_ONLY')

    @mcp.tool()
    def get_status() -> dict:
        """获取代码图谱完整状态概览

        包含工作区信息、文件分布、语言分布、符号分布、
        调用关系统计、注释覆盖率、缺陷统计等全面信息。

        Returns:
            完整的状态概览字典
        """
        return _route('query.status', {}, 'READ_ONLY')

    @mcp.tool()
    def remove_file(file_path: str) -> bool:
        """从图谱中移除指定文件（标记为删除，保留历史）

        Args:
            file_path: 文件的绝对路径或相对工作区路径

        Returns:
            是否成功移除
        """
        return _route('workspace.file.remove', {"file_path": file_path}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def build_directory(dir_path: str) -> dict:
        """构建指定目录的代码图谱

        Args:
            dir_path: 目录的绝对路径

        Returns:
            构建结果统计
        """
        return _route('workspace.build_directory', {"dir_path": dir_path}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_symbol_content_by_hash(content_hash: str) -> Optional[dict]:
        """根据内容哈希获取符号的完整内容

        Args:
            content_hash: 符号内容的 SHA-256 哈希

        Returns:
            符号内容详情，包含完整代码
        """
        return _route('query.symbol_content_by_hash', {"content_hash": content_hash}, 'READ_ONLY')

    @mcp.tool()
    def get_code_metrics_summary() -> dict:
        """获取代码度量汇总统计

        包含文件数、函数数、总代码行、调用关系数、
        平均/最高圈复杂度、复杂度分布、注释覆盖率等。

        Returns:
            全局度量统计字典
        """
        return _route('query.metrics_summary', {}, 'READ_ONLY')

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
        return _route('query.complexity_hotspots', {"limit": limit, "module_filter": module_filter}, 'READ_ONLY')

    @mcp.tool()
    def get_coupling_analysis(limit: int = 30) -> list:
        """获取模块耦合度分析

        分析模块间调用关系，计算每个模块的传入/传出耦合度和不稳定性。

        Args:
            limit: 返回数量限制（默认 30）

        Returns:
            按总耦合度降序排列的模块列表
        """
        return _route('query.coupling_analysis', {"limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_function_metrics(qualified_name: str) -> Optional[dict]:
        """获取单个函数的度量详情

        Args:
            qualified_name: 函数限定名

        Returns:
            度量字典，包含圈复杂度、行数、扇入扇出、深度等
        """
        return _route('query.function_metrics', {"qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def get_largest_functions(limit: int = 20, module_filter: str = "") -> list:
        """获取代码行数最多的函数

        Args:
            limit: 返回数量限制（默认 20）
            module_filter: 模块路径前缀过滤

        Returns:
            按行数降序排列的函数列表
        """
        return _route('query.largest_functions', {"limit": limit, "module_filter": module_filter}, 'READ_ONLY')

    @mcp.tool()
    def get_most_coupled_functions(limit: int = 20) -> list:
        """获取耦合度最高的函数（扇入+扇出最大）

        Args:
            limit: 返回数量限制（默认 20）

        Returns:
            按总耦合度降序排列的函数列表
        """
        return _route('query.most_coupled_functions', {"limit": limit}, 'READ_ONLY')

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
        return _route('query.code_health', {"severity": severity}, 'READ_ONLY')

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
        return _route('workspace.file.health', {"file_path": file_path}, 'READ_ONLY')
