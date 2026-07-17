"""
call_chain.py
=============

调用链分析 Mixin 类。

提供调用链追踪、调用统计、孤立符号检测、循环调用检测等功能。
"""

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set


class CallChainMixin:
    """调用链分析 Mixin

    通过 self.conn 访问数据库连接，提供调用链相关的查询方法。
    """

    def get_call_chain_up(self, qualified_name: str, max_depth: int = 10) -> Dict:
        """向上追踪调用链（找出所有调用该函数的上游函数）

        Args:
            qualified_name: 起始函数的限定名
            max_depth: 最大追踪深度

        Returns:
            调用链结构，包含层级、调用者信息
        """
        ws_id = self._get_active_workspace_id()
        visited = set()  # 避免循环
        levels = []  # 每层的调用者列表

        current_level = {qualified_name}
        visited.add(qualified_name)

        for depth in range(max_depth):
            next_level = set()
            level_callers = []

            # 批量优化：对当前层的所有 callee 一次性查询 callers（避免每节点单独查询）
            # SQLite IN 子句占位符有上限，按 500 一批分块
            callee_list = [c for c in current_level if c]
            batch_size = 500
            for i in range(0, len(callee_list), batch_size):
                chunk = callee_list[i:i + batch_size]
                placeholders = ",".join("?" * len(chunk))
                sql = f"""
                    SELECT DISTINCT cv.caller_qualified as caller_name,
                                    cv.callee_qualified as callee_name
                    FROM call_versions cv
                    JOIN file_versions fv ON cv.file_version_id = fv.id
                    JOIN file_instances fi ON fv.file_instance_id = fi.id
                    WHERE fi.workspace_id = ?
                      AND fv.is_current = 1
                      AND cv.callee_qualified IN ({placeholders})
                      AND cv.caller_qualified != ''
                """
                cur = self.conn.execute(sql, [ws_id] + chunk)
                for row in cur:
                    caller = row["caller_name"]
                    callee = row["callee_name"]
                    if caller and caller not in visited:
                        visited.add(caller)
                        next_level.add(caller)
                        level_callers.append({
                            "caller": caller,
                            "target": callee,
                            "depth": depth + 1,
                        })

            if not level_callers:
                break

            levels.append({
                "depth": depth + 1,
                "count": len(level_callers),
                "callers": level_callers,
            })

            current_level = next_level
            if not current_level:
                break

        return {
            "start": qualified_name,
            "max_depth_reached": len(levels),
            "total_upstream": len(visited) - 1,  # 减去起始节点
            "levels": levels,
            "all_upstream": list(visited - {qualified_name}),
        }

    def get_call_chain_down(self, qualified_name: str, max_depth: int = 10) -> Dict:
        """向下追踪调用链（找出该函数调用的所有下游函数）

        Args:
            qualified_name: 起始函数的限定名
            max_depth: 最大追踪深度

        Returns:
            调用链结构，包含层级、被调用者信息
        """
        ws_id = self._get_active_workspace_id()
        visited = set()
        levels = []

        current_level = {qualified_name}
        visited.add(qualified_name)

        for depth in range(max_depth):
            next_level = set()
            level_callees = []

            # 批量优化：对当前层的所有 caller 一次性查询 callees（避免每节点单独查询）
            caller_list = [c for c in current_level if c]
            batch_size = 500
            for i in range(0, len(caller_list), batch_size):
                chunk = caller_list[i:i + batch_size]
                placeholders = ",".join("?" * len(chunk))
                sql = f"""
                    SELECT DISTINCT cv.callee_qualified as callee_name,
                                    cv.caller_qualified as caller_name
                    FROM call_versions cv
                    JOIN file_versions fv ON cv.file_version_id = fv.id
                    JOIN file_instances fi ON fv.file_instance_id = fi.id
                    WHERE fi.workspace_id = ?
                      AND fv.is_current = 1
                      AND cv.caller_qualified IN ({placeholders})
                      AND cv.callee_qualified != ''
                """
                cur = self.conn.execute(sql, [ws_id] + chunk)
                for row in cur:
                    callee = row["callee_name"]
                    caller = row["caller_name"]
                    if callee and callee not in visited:
                        visited.add(callee)
                        next_level.add(callee)
                        level_callees.append({
                            "callee": callee,
                            "caller": caller,
                            "depth": depth + 1,
                        })

            if not level_callees:
                break

            levels.append({
                "depth": depth + 1,
                "count": len(level_callees),
                "callees": level_callees,
            })

            current_level = next_level
            if not current_level:
                break

        return {
            "start": qualified_name,
            "max_depth_reached": len(levels),
            "total_downstream": len(visited) - 1,
            "levels": levels,
            "all_downstream": list(visited - {qualified_name}),
        }

    def get_top_callers(self, limit: int = 20, kind: str = "fn", module_filter: str = "") -> List[Dict]:
        """获取被调用次数最多的函数排行

        Args:
            limit: 返回数量限制
            kind: 符号类型（默认 fn）
            module_filter: 模块过滤（前缀匹配）

        Returns:
            按被调用次数降序排列的符号列表
        """
        ws_id = self._get_active_workspace_id()
        # 统计每个函数被多少个不同的调用者调用
        sql = """
            SELECT
                cv.callee_qualified as qualified_name,
                COUNT(DISTINCT cv.caller_qualified) as caller_count,
                COUNT(*) as call_count
            FROM call_versions cv
            JOIN file_versions fv ON cv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
              AND fv.is_current = 1
              AND cv.callee_qualified != ''
              AND cv.caller_qualified != ''
        """

        params = [ws_id]
        if module_filter:
            sql += " AND cv.callee_qualified LIKE ?"
            params.append(module_filter + "%")

        sql += " GROUP BY cv.callee_qualified ORDER BY caller_count DESC LIMIT ?"
        params.append(limit)

        cur = self.conn.execute(sql, params)
        results = []
        for row in cur:
            results.append({
                "qualified_name": row["qualified_name"],
                "caller_count": row["caller_count"],  # 不同调用者数量
                "call_count": row["call_count"],  # 总调用次数
            })
        return results

    def get_orphan_symbols(self, kind: str = "fn", module_filter: str = "", limit: int = 100) -> List[Dict]:
        """获取未被调用的孤立函数

        Args:
            kind: 符号类型（默认 fn）
            module_filter: 模块过滤（前缀匹配）
            limit: 返回数量限制

        Returns:
            未被任何函数调用的符号列表
        """
        ws_id = self._get_active_workspace_id()
        # 找出所有当前版本的符号，然后排除有被调用记录的
        sql = """
            SELECT DISTINCT fsv.qualified_name, fsv.module_path, sc.name, sc.kind
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ?
              AND fv.is_current = 1
              AND sc.kind = ?
              AND fsv.qualified_name NOT IN (
                  SELECT DISTINCT cv.callee_qualified
                  FROM call_versions cv
                  JOIN file_versions fv2 ON cv.file_version_id = fv2.id
                  JOIN file_instances fi2 ON fv2.file_instance_id = fi2.id
                  WHERE fi2.workspace_id = ?
                    AND fv2.is_current = 1
                    AND cv.callee_qualified != ''
                    AND cv.caller_qualified != ''
              )
        """

        params = [ws_id, kind, ws_id]
        if module_filter:
            sql += " AND fsv.module_path LIKE ?"
            params.append(module_filter + "%")

        sql += " ORDER BY fsv.module_path, fsv.qualified_name LIMIT ?"
        params.append(limit)

        cur = self.conn.execute(sql, params)
        results = []
        for row in cur:
            results.append({
                "qualified_name": row["qualified_name"],
                "module_path": row["module_path"],
                "name": row["name"],
                "kind": row["kind"],
            })
        return results

    def get_deepest_functions(self, limit: int = 20, module_filter: str = "", kind: str = "fn") -> List[Dict]:
        """获取调用深度最深的函数排行

        Args:
            limit: 返回数量限制
            module_filter: 模块过滤（前缀匹配）
            kind: 符号类型（默认 fn）

        Returns:
            按深度降序排列的符号列表
        """
        ws_id = self._get_active_workspace_id()
        sql = """
            SELECT DISTINCT fsv.qualified_name, fsv.module_path, fsv.depth, sc.kind
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ?
              AND fv.is_current = 1
              AND sc.kind = ?
              AND fsv.depth >= 0
        """

        params = [ws_id, kind]
        if module_filter:
            sql += " AND fsv.module_path LIKE ?"
            params.append(module_filter + "%")

        sql += " ORDER BY fsv.depth DESC, fsv.qualified_name LIMIT ?"
        params.append(limit)

        cur = self.conn.execute(sql, params)
        results = []
        for row in cur:
            results.append({
                "qualified_name": row["qualified_name"],
                "module_path": row["module_path"],
                "depth": row["depth"],
                "kind": row["kind"],
            })
        return results

    def get_module_call_stats(self, limit: int = 30) -> List[Dict]:
        """获取模块间调用统计

        Args:
            limit: 返回数量限制

        Returns:
            按调用次数降序排列的模块调用对列表
        """
        ws_id = self._get_active_workspace_id()
        sql = """
            SELECT
                cv.caller_qualified,
                cv.callee_qualified,
                COUNT(*) as call_count,
                COUNT(DISTINCT cv.caller_qualified) as unique_caller_count,
                COUNT(DISTINCT cv.callee_qualified) as unique_callee_count
            FROM call_versions cv
            JOIN file_versions fv ON cv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
              AND fv.is_current = 1
              AND cv.caller_qualified != ''
              AND cv.callee_qualified != ''
              AND cv.caller_qualified LIKE '%::%'
              AND cv.callee_qualified LIKE '%::%'
            GROUP BY cv.caller_qualified, cv.callee_qualified
        """

        cur = self.conn.execute(sql, (ws_id,))

        # 在 Python 中聚合模块级统计
        module_stats = defaultdict(lambda: {
            "call_count": 0,
            "callers": set(),
            "callees": set(),
        })

        for row in cur:
            caller = row["caller_qualified"]
            callee = row["callee_qualified"]

            # 提取顶级模块（前两级，如 lib::core）
            def get_top_module(name):
                """从 qualified_name 中提取顶级模块（前 2-3 级路径）"""
                parts = name.split("::")
                if len(parts) >= 3:
                    return "::".join(parts[:3])  # lib::core::xxx
                elif len(parts) >= 2:
                    return "::".join(parts[:2])  # lib::core
                else:
                    return name

            caller_mod = get_top_module(caller)
            callee_mod = get_top_module(callee)

            if caller_mod != callee_mod:
                key = (caller_mod, callee_mod)
                module_stats[key]["call_count"] += row["call_count"]
                module_stats[key]["callers"].add(caller)
                module_stats[key]["callees"].add(callee)

        # 转换为列表并排序
        results = []
        for (caller_mod, callee_mod), stats in module_stats.items():
            results.append({
                "caller_module": caller_mod,
                "callee_module": callee_mod,
                "call_count": stats["call_count"],
                "unique_caller_count": len(stats["callers"]),
                "unique_callee_count": len(stats["callees"]),
            })

        results.sort(key=lambda x: x["call_count"], reverse=True)
        return results[:limit]

    def detect_cycles(self, max_depth: int = 10) -> List[List[str]]:
        """检测循环调用

        使用 DFS 遍历调用图，检测所有循环依赖

        Args:
            max_depth: 最大追踪深度

        Returns:
            检测到的循环列表，每个循环是一个函数名列表
        """
        ws_id = self._get_active_workspace_id()
        # 获取所有调用关系
        sql = """
            SELECT DISTINCT cv.caller_qualified, cv.callee_qualified
            FROM call_versions cv
            JOIN file_versions fv ON cv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
              AND fv.is_current = 1
              AND cv.caller_qualified != ''
              AND cv.callee_qualified != ''
        """
        cur = self.conn.execute(sql, (ws_id,))

        # 构建邻接表
        adj = defaultdict(set)
        nodes = set()
        for row in cur:
            caller = row["caller_qualified"]
            callee = row["callee_qualified"]
            adj[caller].add(callee)
            nodes.add(caller)
            nodes.add(callee)

        # DFS 检测环
        cycles = []
        visited = set()
        path_stack = []
        path_set = set()

        def dfs(node, depth):
            """从指定节点开始深度优先搜索，检测调用图中的循环依赖"""
            if depth > max_depth:
                return

            if node in path_set:
                # 找到一个环
                cycle_start = path_stack.index(node)
                cycle = path_stack[cycle_start:] + [node]
                # 去重：将环标准化（以最小元素开头）
                min_idx = cycle[:-1].index(min(cycle[:-1]))
                normalized_cycle = cycle[min_idx:-1] + cycle[:min_idx] + [cycle[min_idx]]
                cycle_tuple = tuple(normalized_cycle)
                if cycle_tuple not in {tuple(c) for c in cycles}:
                    cycles.append(normalized_cycle)
                return

            if node in visited:
                return

            visited.add(node)
            path_stack.append(node)
            path_set.add(node)

            for neighbor in adj.get(node, set()):
                dfs(neighbor, depth + 1)

            path_stack.pop()
            path_set.remove(node)

        # 从每个节点开始 DFS
        for node in sorted(nodes):
            if node not in visited:
                dfs(node, 0)
                # 重置 visited，因为环可能从任何节点开始
                visited = set()
                if len(cycles) >= 50:  # 限制最多检测 50 个环
                    break

        return cycles
