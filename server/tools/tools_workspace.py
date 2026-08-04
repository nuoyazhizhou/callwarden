"""工作区与文件面：构建刷新/工作区/Git/文件/状态/符号内容/度量/健康检查

拆分自 server/mcp_server.py（558-1259 行区间），由 register(mcp) 注册。
"""

# [L1] 构建刷新与工作区管理工具（build_graph / list_workspaces / set_active_workspace 等）
# [L2] 文件操作与符号内容工具（file_read / file_grep / file_symbol_content 等）
# [L4] 代码度量与健康检查工具（get_code_metrics_summary / get_code_health_check 等）
# [L8] Git 集成工具（import_git_history / get_git_commits / get_commit_changes 等）

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import get_db


def register(mcp: FastMCP) -> None:
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
            result = {
                "path": abs_path,
                "total_lines": total,
                "offset": offset,
                "limit": limit,
                "content": content,
            }

            # L4: include_context=True 时合并返回符号上下文
            if include_context:
                rel_path = os.path.relpath(abs_path, ws_root).replace("\\", "/")
                try:
                    symbols = db.get_file_symbols(rel_path)
                    # 精简符号列表字段（只返回 Agent 需要的关键字段）
                    result["symbols"] = [
                        {
                            "name": s.get("name", ""),
                            "qualified_name": s.get("qualified_name", ""),
                            "kind": s.get("kind", ""),
                            "start_line": s.get("start_line", 0),
                            "end_line": s.get("end_line", 0),
                        }
                        for s in symbols
                    ]

                    # 为每个函数/方法符号附加 callers/callees 摘要（top 3）
                    # 限制最多 20 个符号避免响应过大
                    fn_kinds = ("fn", "function", "method", "test_fn")
                    symbol_contexts = []
                    for sym in symbols[:20]:
                        if sym.get("kind") not in fn_kinds:
                            continue
                        sym_name = sym.get("name", "")
                        sym_qn = sym.get("qualified_name", "") or None
                        if not sym_name:
                            continue
                        ctx = {
                            "symbol": sym_name,
                            "qualified_name": sym.get("qualified_name", ""),
                            "start_line": sym.get("start_line", 0),
                            "end_line": sym.get("end_line", 0),
                        }
                        # 调用方 top 3
                        try:
                            callers = db.get_callers(sym_name, sym_qn)
                            ctx["callers_total"] = len(callers or [])
                            ctx["callers"] = [
                                {
                                    "caller": c.get("caller_name", ""),
                                    "file": c.get("caller_file", ""),
                                }
                                for c in (callers or [])[:3]
                            ]
                        except Exception:
                            ctx["callers"] = []
                            ctx["callers_total"] = 0
                        # 被调用方 top 3
                        try:
                            callees = db.get_callees(sym_name, sym_qn)
                            ctx["callees_total"] = len(callees or [])
                            ctx["callees"] = [
                                {
                                    "callee": c.get("callee_name", ""),
                                    "module": c.get("callee_module", ""),
                                }
                                for c in (callees or [])[:3]
                            ]
                        except Exception:
                            ctx["callees"] = []
                            ctx["callees_total"] = 0
                        symbol_contexts.append(ctx)
                    result["symbol_contexts"] = symbol_contexts
                except Exception as e:
                    result["symbol_contexts_error"] = str(e)

            return result
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
