"""
coverage.py
===========

覆盖率统计和热力图分析 Mixin 类。

提供注释覆盖率、未注释符号列表、函数调用频率热力图、测试覆盖率等统计分析功能。
"""

from typing import Any, Dict, List, Optional


class CoverageMixin:
    """覆盖率统计和热力图分析 Mixin 类。

    提供代码注释覆盖率、测试覆盖率、调用热力图等统计分析功能。
    需要与包含 self.conn 数据库连接的主类一起使用。
    """

    def get_comment_coverage(self, group_by: str = "module") -> Dict:
        """获取注释覆盖率统计

        Args:
            group_by: 分组方式：module（按模块）、file（按文件）、kind（按类型）

        Returns:
            覆盖率统计结果
        """
        ws_id = self._get_active_workspace_id()
        # 查询当前版本的所有符号及其注释状态
        # 通过 file_symbol_versions + file_versions(is_current=1) + symbol_contents
        # 使用 DISTINCT 避免同一文件同一符号的重复记录
        query = """
            SELECT 
                sc.kind,
                sc.has_comment,
                COUNT(DISTINCT fsv.qualified_name || '@' || fi.rel_path) as cnt
            FROM file_symbol_versions fsv
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fv.is_current = 1
            GROUP BY sc.kind, sc.has_comment
            ORDER BY sc.kind, sc.has_comment
        """

        cur = self.conn.execute(query, (ws_id,))
        rows = cur.fetchall()

        # 汇总按类型
        by_kind = {}
        total_commented = 0
        total_all = 0
        for row in rows:
            kind = row["kind"]
            if kind not in by_kind:
                by_kind[kind] = {"total": 0, "commented": 0}
            by_kind[kind]["total"] += row["cnt"]
            total_all += row["cnt"]
            if row["has_comment"]:
                by_kind[kind]["commented"] += row["cnt"]
                total_commented += row["cnt"]

        result = {
            "total": total_all,
            "commented": total_commented,
            "coverage": round(total_commented / total_all * 100, 1) if total_all > 0 else 0,
            "by_kind": by_kind,
        }

        # 按模块分组
        if group_by == "module" or group_by == "file":
            module_query = """
                SELECT 
                    fsv.module_path,
                    fi.rel_path,
                    sc.kind,
                    sc.has_comment,
                    COUNT(DISTINCT fsv.qualified_name || '@' || fi.rel_path) as cnt
                FROM file_symbol_versions fsv
                JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
                JOIN file_versions fv ON fsv.file_version_id = fv.id
                JOIN file_instances fi ON fv.file_instance_id = fi.id
                WHERE fi.workspace_id = ? AND fv.is_current = 1
                GROUP BY fsv.module_path, fi.rel_path, sc.kind, sc.has_comment
                ORDER BY fsv.module_path
            """

            cur = self.conn.execute(module_query, (ws_id,))
            module_rows = cur.fetchall()

            modules = {}
            for row in module_rows:
                key = row["rel_path"] if group_by == "file" else row["module_path"]
                if not key:
                    key = row["rel_path"]
                if key not in modules:
                    modules[key] = {"total": 0, "commented": 0, "by_kind": {}}
                modules[key]["total"] += row["cnt"]
                if row["has_comment"]:
                    modules[key]["commented"] += row["cnt"]
                kind = row["kind"]
                if kind not in modules[key]["by_kind"]:
                    modules[key]["by_kind"][kind] = {"total": 0, "commented": 0}
                modules[key]["by_kind"][kind]["total"] += row["cnt"]
                if row["has_comment"]:
                    modules[key]["by_kind"][kind]["commented"] += row["cnt"]

            # 计算覆盖率
            for key in modules:
                m = modules[key]
                m["coverage"] = round(m["commented"] / m["total"] * 100, 1) if m["total"] > 0 else 0

            result["by_module"] = modules if group_by == "module" else None
            result["by_file"] = modules if group_by == "file" else None

        return result

    def get_uncommented_symbols(self, kind: str = "fn", module_filter: Optional[str] = None) -> List[Dict]:
        """获取未注释的符号列表

        Args:
            kind: 符号类型（fn, struct, enum 等）
            module_filter: 模块过滤（前缀匹配）

        Returns:
            未注释符号列表（已去重）
        """
        ws_id = self._get_active_workspace_id()
        # 使用子查询去重（同一文件同一符号只取一条）
        query = """
            SELECT 
                fsv.qualified_name,
                fsv.module_path,
                fsv.start_line,
                fsv.end_line,
                fsv.depth,
                sc.name,
                sc.kind,
                sc.signature,
                fi.rel_path as file_path
            FROM (
                SELECT 
                    fsv_inner.*,
                    ROW_NUMBER() OVER (PARTITION BY fsv_inner.qualified_name, fi.rel_path ORDER BY fsv_inner.id DESC) as rn
                FROM file_symbol_versions fsv_inner
                JOIN file_versions fv_inner ON fsv_inner.file_version_id = fv_inner.id
                JOIN file_instances fi ON fv_inner.file_instance_id = fi.id
                WHERE fi.workspace_id = ? AND fv_inner.is_current = 1
            ) fsv
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fsv.rn = 1
              AND sc.has_comment = 0
              AND sc.kind = ?
        """

        params = [ws_id, ws_id, kind]

        if module_filter:
            query += " AND fsv.module_path LIKE ?"
            params.append(module_filter + "%")

        query += " ORDER BY fsv.depth DESC, fi.rel_path, fsv.start_line"

        cur = self.conn.execute(query, params)
        return [dict(row) for row in cur]

    def get_call_heatmap(self, group_by: str = "module", top_n: int = 20) -> List[Dict]:
        """获取函数调用频率热力图数据

        Args:
            group_by: 分组方式（module 或 file）
            top_n: 返回数量限制

        Returns:
            按调用频率排序的分组列表
        """
        ws_id = self._get_active_workspace_id()
        if group_by == "module":
            # 按模块统计：该模块的函数被其他模块调用的总次数
            sql = """
                SELECT 
                    fsv.module_path,
                    COUNT(*) as total_calls_in,
                    COUNT(DISTINCT cv.caller_qualified) as unique_callers,
                    COUNT(DISTINCT cv.callee_qualified) as unique_callees
                FROM call_versions cv
                JOIN file_versions fv ON cv.file_version_id = fv.id
                JOIN file_instances fi ON fv.file_instance_id = fi.id
                JOIN file_symbol_versions fsv ON fsv.qualified_name = cv.callee_qualified AND fsv.file_version_id = fv.id
                WHERE fi.workspace_id = ? AND fv.is_current = 1
                  AND cv.callee_qualified != ''
                  AND fsv.module_path != ''
                GROUP BY fsv.module_path
                ORDER BY total_calls_in DESC
                LIMIT ?
            """
            cur = self.conn.execute(sql, (ws_id, top_n * 2,))

            results = []
            for row in cur:
                results.append({
                    "group": row["module_path"],
                    "total_calls": row["total_calls_in"],
                    "unique_callers": row["unique_callers"],
                    "unique_callees": row["unique_callees"],
                })
            return results[:top_n]

        elif group_by == "file":
            # 按文件统计
            sql = """
                SELECT 
                    fi.rel_path as file_path,
                    COUNT(*) as total_calls_in,
                    COUNT(DISTINCT cv.caller_qualified) as unique_callers,
                    COUNT(DISTINCT cv.callee_qualified) as unique_callees
                FROM call_versions cv
                JOIN file_versions fv ON cv.file_version_id = fv.id
                JOIN file_symbol_versions fsv ON fsv.qualified_name = cv.callee_qualified AND fsv.file_version_id = fv.id
                JOIN file_instances fi ON fv.file_instance_id = fi.id
                WHERE fi.workspace_id = ? AND fv.is_current = 1
                  AND cv.callee_qualified != ''
                GROUP BY fi.rel_path
                ORDER BY total_calls_in DESC
                LIMIT ?
            """
            cur = self.conn.execute(sql, (ws_id, top_n,))

            results = []
            for row in cur:
                results.append({
                    "group": row["file_path"],
                    "total_calls": row["total_calls_in"],
                    "unique_callers": row["unique_callers"],
                    "unique_callees": row["unique_callees"],
                })
            return results

        else:
            raise ValueError(f"不支持的分组方式: {group_by}")

    def get_test_coverage(self) -> Dict:
        """获取测试覆盖率统计

        Returns:
            测试覆盖率统计数据
        """
        ws_id = self._get_active_workspace_id()
        # 统计总数
        sql_total = """
            SELECT COUNT(DISTINCT fsv.qualified_name) as count
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ? AND fv.is_current = 1 AND sc.kind = 'fn'
        """
        cur = self.conn.execute(sql_total, (ws_id,))
        total_fns = cur.fetchone()["count"]

        # 统计 test 函数（模块路径包含 tests 或函数名以 test_ 开头）
        sql_test = """
            SELECT COUNT(DISTINCT fsv.qualified_name) as count
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ? AND fv.is_current = 1 
              AND sc.kind = 'fn'
              AND (fsv.module_path LIKE '%::tests' OR sc.name LIKE 'test_%')
        """
        cur = self.conn.execute(sql_test, (ws_id,))
        test_fns = cur.fetchone()["count"]

        # 按模块统计 test 函数分布
        sql_by_module = """
            SELECT 
                fsv.module_path,
                COUNT(DISTINCT fsv.qualified_name) as test_count,
                SUM(CASE WHEN fsv.module_path LIKE '%::tests' OR sc.name LIKE 'test_%' THEN 1 ELSE 0 END) as test_count2
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ? AND fv.is_current = 1 AND sc.kind = 'fn'
            GROUP BY fsv.module_path
            HAVING test_count > 0 OR test_count2 > 0
            ORDER BY test_count2 DESC, test_count DESC
        """
        cur = self.conn.execute(sql_by_module, (ws_id,))
        test_by_module = []
        for row in cur:
            count = max(row["test_count"], row["test_count2"])
            if count > 0:
                test_by_module.append({
                    "module": row["module_path"] or "(unknown)",
                    "test_count": count,
                })

        # 有测试的模块数
        modules_with_tests = len(test_by_module)

        # 总模块数
        sql_total_modules = """
            SELECT COUNT(DISTINCT fsv.module_path) as count
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ? AND fv.is_current = 1 AND sc.kind = 'fn' AND fsv.module_path != ''
        """
        cur = self.conn.execute(sql_total_modules, (ws_id,))
        total_modules = cur.fetchone()["count"]

        return {
            "total_functions": total_fns,
            "test_functions": test_fns,
            "test_ratio": round(test_fns / total_fns * 100, 2) if total_fns > 0 else 0,
            "total_modules": total_modules,
            "modules_with_tests": modules_with_tests,
            "module_coverage": round(modules_with_tests / total_modules * 100, 2) if total_modules > 0 else 0,
            "test_by_module": test_by_module,
        }
