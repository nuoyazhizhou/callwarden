"""
db_metrics.py
=============

代码知识图谱代码度量 Mixin 类。

提供圈复杂度、耦合度、代码行数、扇入扇出等代码质量度量功能。
基于已有的符号内容、调用关系、版本历史数据计算，无需额外存储。
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from ..i18n import t


class MetricsMixin:
    """代码度量功能 Mixin

    通过 self.conn 访问数据库连接，提供代码质量度量功能。
    所有度量均实时计算，不额外存储。
    """

    # 圈复杂度关键词（多语言通用）
    _COMPLEXITY_KEYWORDS = [
        r'\bif\b', r'\belse\b', r'\bfor\b', r'\bwhile\b', r'\bmatch\b',
        r'\bcase\b', r'\bcatch\b', r'\b&&\b', r'\b\|\|\b',
        r'\btry\b', r'\bexcept\b', r'\bfinally\b',
        r'\bwhen\b', r'\bguard\b',
    ]

    def _compute_cyclomatic_complexity(self, content: str, language: str = "") -> int:
        """计算单个函数的圈复杂度

        基于控制流关键词计数，初始值为 1（函数本身至少有 1 条路径）。

        Args:
            content: 函数源码
            language: 语言标识（rust/python/typescript 等）

        Returns:
            圈复杂度值
        """
        if not content:
            return 1

        complexity = 1

        # 通用关键词匹配
        for pattern in self._COMPLEXITY_KEYWORDS:
            matches = re.findall(pattern, content)
            complexity += len(matches)

        # 三元运算符 ? : （Rust/C/Java/TypeScript）
        if language in ("rust", "c", "java", "typescript", "javascript", "go"):
            # 匹配 ? : 三元表达式（排除字符串中的）
            ternary_count = len(re.findall(r'\?\s*[^:]+\s*:', content))
            complexity += ternary_count

        # Python 特有：列表推导式生成额外路径
        if language == "python":
            comp_count = len(re.findall(r'\bfor\b.*\bin\b', content))
            complexity += comp_count

        return complexity

    def get_function_metrics(self, qualified_name: str) -> Optional[Dict[str, Any]]:
        """获取单个函数的度量信息

        Args:
            qualified_name: 函数限定名

        Returns:
            度量字典，包含圈复杂度、行数、扇入扇出、深度等
        """
        ws_id = self._get_active_workspace_id()

        # 获取符号基本信息和内容
        cur = self.conn.execute(
            """
            SELECT s.qualified_name, s.kind, s.start_line, s.end_line,
                   s.depth, s.module_path, s.signature,
                   sc.content, sc.content_hash,
                   fi.rel_path, fi.abs_path
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ? AND s.qualified_name = ?
            LIMIT 1
            """,
            (ws_id, qualified_name),
        )
        row = cur.fetchone()
        if not row:
            return None

        content = row["content"] or ""
        line_count = row["end_line"] - row["start_line"] + 1 if row["start_line"] and row["end_line"] else 0

        # 从文件实例获取语言
        lang = ""
        if row["rel_path"]:
            from .config import detect_language_from_path
            lang = detect_language_from_path(row["rel_path"]) or ""

        # 圈复杂度
        cyclomatic = self._compute_cyclomatic_complexity(content, lang)

        # 扇入：谁调用了这个函数
        cur = self.conn.execute(
            """
            SELECT COUNT(DISTINCT caller_id) as cnt
            FROM calls c
            JOIN symbols s ON c.caller_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND c.callee_qualified = ?
            """,
            (ws_id, qualified_name),
        )
        fan_in = cur.fetchone()["cnt"]

        # 扇出：这个函数调用了谁
        cur = self.conn.execute(
            """
            SELECT COUNT(DISTINCT callee_name) as cnt
            FROM calls c
            JOIN symbols s ON c.caller_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.qualified_name = ?
            """,
            (ws_id, qualified_name),
        )
        fan_out = cur.fetchone()["cnt"]

        # 复杂度评级
        if cyclomatic <= 5:
            risk_level = "低"
        elif cyclomatic <= 10:
            risk_level = "中"
        elif cyclomatic <= 20:
            risk_level = "高"
        else:
            risk_level = "极高"

        return {
            "qualified_name": qualified_name,
            "kind": row["kind"],
            "file_path": row["rel_path"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "line_count": line_count,
            "cyclomatic_complexity": cyclomatic,
            "risk_level": risk_level,
            "fan_in": fan_in,
            "fan_out": fan_out,
            "depth": row["depth"] if row["depth"] >= 0 else 0,
            "module_path": row["module_path"],
            "signature": row["signature"],
        }

    def get_complexity_hotspots(self, limit: int = 30, module_filter: str = "") -> List[Dict[str, Any]]:
        """获取圈复杂度最高的函数列表（复杂度热点）

        Args:
            limit: 返回数量限制
            module_filter: 模块路径前缀过滤

        Returns:
            按圈复杂度降序排列的函数列表
        """
        ws_id = self._get_active_workspace_id()

        sql = """
            SELECT s.qualified_name, s.kind, s.start_line, s.end_line,
                   s.depth, s.module_path, s.signature,
                   sc.content, fi.rel_path
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ? AND s.kind IN ('fn', 'function', 'method')
        """
        params: list = [ws_id]

        if module_filter:
            sql += " AND s.module_path LIKE ?"
            params.append(f"{module_filter}%")

        cur = self.conn.execute(sql, params)

        results = []
        for row in cur:
            content = row["content"] or ""
            lang = ""
            if row["rel_path"]:
                from .config import detect_language_from_path
                lang = detect_language_from_path(row["rel_path"]) or ""

            complexity = self._compute_cyclomatic_complexity(content, lang)
            line_count = row["end_line"] - row["start_line"] + 1 if row["start_line"] and row["end_line"] else 0

            results.append({
                "qualified_name": row["qualified_name"],
                "file_path": row["rel_path"],
                "start_line": row["start_line"],
                "line_count": line_count,
                "cyclomatic_complexity": complexity,
                "depth": row["depth"] if row["depth"] >= 0 else 0,
                "module_path": row["module_path"],
            })

        results.sort(key=lambda x: x["cyclomatic_complexity"], reverse=True)
        return results[:limit]

    def get_coupling_analysis(self, limit: int = 30) -> List[Dict[str, Any]]:
        """获取模块耦合度分析

        分析模块间的调用关系，计算每个模块的传入/传出耦合度。

        Args:
            limit: 返回数量限制

        Returns:
            按总耦合度降序排列的模块列表
        """
        ws_id = self._get_active_workspace_id()

        # 统计模块间调用关系
        cur = self.conn.execute(
            """
            SELECT s_caller.module_path as caller_module,
                   c.callee_module as callee_module,
                   COUNT(*) as call_count
            FROM calls c
            JOIN symbols s_caller ON c.caller_id = s_caller.id
            JOIN file_instances fi ON s_caller.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
              AND s_caller.module_path != ''
              AND c.callee_module != ''
              AND s_caller.module_path != c.callee_module
            GROUP BY s_caller.module_path, c.callee_module
            """,
            (ws_id,),
        )

        # afferent（传入）：被其他模块调用的次数
        # efferent（传出）：调用其他模块的次数
        afferent = defaultdict(int)
        efferent = defaultdict(int)
        all_modules = set()

        for row in cur:
            caller_mod = row["caller_module"]
            callee_mod = row["callee_module"]
            cnt = row["call_count"]

            efferent[caller_mod] += cnt
            afferent[callee_mod] += cnt
            all_modules.add(caller_mod)
            all_modules.add(callee_mod)

        results = []
        for mod in all_modules:
            aff = afferent[mod]
            eff = efferent[mod]
            total = aff + eff

            # 不稳定性 = 传出 / (传入 + 传出)，1=完全依赖他人，0=被他人依赖
            instability = eff / total if total > 0 else 0

            results.append({
                "module": mod,
                "afferent": aff,
                "efferent": eff,
                "total_coupling": total,
                "instability": round(instability, 2),
            })

        results.sort(key=lambda x: x["total_coupling"], reverse=True)
        return results[:limit]

    def get_code_metrics_summary(self) -> Dict[str, Any]:
        """获取代码度量汇总统计

        Returns:
            包含全局度量统计的字典
        """
        ws_id = self._get_active_workspace_id()

        # 基本统计
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM file_instances WHERE workspace_id = ?",
            (ws_id,),
        )
        file_count = cur.fetchone()["cnt"]

        cur = self.conn.execute(
            """
            SELECT COUNT(*) as cnt FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.kind IN ('fn', 'function', 'method')
            """,
            (ws_id,),
        )
        function_count = cur.fetchone()["cnt"]

        cur = self.conn.execute(
            """
            SELECT SUM(fi.total_lines) as total FROM file_instances fi
            WHERE fi.workspace_id = ?
            """,
            (ws_id,),
        )
        total_lines = cur.fetchone()["total"] or 0

        # 复杂度分布
        cur = self.conn.execute(
            """
            SELECT s.qualified_name, sc.content, fi.rel_path, s.start_line, s.end_line
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ? AND s.kind IN ('fn', 'function', 'method')
            """,
            (ws_id,),
        )

        complexity_buckets = {"低 (≤5)": 0, "中 (6-10)": 0, "高 (11-20)": 0, "极高 (>20)": 0}
        total_complexity = 0
        max_complexity = 0
        function_count_with_content = 0

        for row in cur:
            content = row["content"] or ""
            if not content:
                continue

            lang = ""
            if row["rel_path"]:
                from .config import detect_language_from_path
                lang = detect_language_from_path(row["rel_path"]) or ""

            complexity = self._compute_cyclomatic_complexity(content, lang)
            total_complexity += complexity
            max_complexity = max(max_complexity, complexity)
            function_count_with_content += 1

            if complexity <= 5:
                complexity_buckets["低 (≤5)"] += 1
            elif complexity <= 10:
                complexity_buckets["中 (6-10)"] += 1
            elif complexity <= 20:
                complexity_buckets["高 (11-20)"] += 1
            else:
                complexity_buckets["极高 (>20)"] += 1

        avg_complexity = total_complexity / function_count_with_content if function_count_with_content > 0 else 0

        # 调用关系统计
        cur = self.conn.execute(
            """
            SELECT COUNT(*) as cnt FROM calls c
            JOIN symbols s ON c.caller_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
            """,
            (ws_id,),
        )
        total_calls = cur.fetchone()["cnt"]

        # 注释覆盖率
        cur = self.conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN s.has_comment = 1 THEN 1 ELSE 0 END) as commented
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.kind IN ('fn', 'function', 'method')
            """,
            (ws_id,),
        )
        comment_row = cur.fetchone()
        comment_coverage = (comment_row["commented"] / comment_row["total"] * 100) if comment_row["total"] > 0 else 0

        return {
            "file_count": file_count,
            "function_count": function_count,
            "total_lines": total_lines,
            "total_calls": total_calls,
            "avg_complexity": round(avg_complexity, 1),
            "max_complexity": max_complexity,
            "complexity_distribution": complexity_buckets,
            "comment_coverage": round(comment_coverage, 1),
        }

    def get_largest_functions(self, limit: int = 30, module_filter: str = "") -> List[Dict[str, Any]]:
        """获取代码行数最多的函数列表

        Args:
            limit: 返回数量限制
            module_filter: 模块路径前缀过滤

        Returns:
            按行数降序排列的函数列表
        """
        ws_id = self._get_active_workspace_id()

        sql = """
            SELECT s.qualified_name, s.kind, s.start_line, s.end_line,
                   s.module_path, s.depth, fi.rel_path
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.kind IN ('fn', 'function', 'method')
        """
        params: list = [ws_id]

        if module_filter:
            sql += " AND s.module_path LIKE ?"
            params.append(f"{module_filter}%")

        cur = self.conn.execute(sql, params)

        results = []
        for row in cur:
            line_count = row["end_line"] - row["start_line"] + 1 if row["start_line"] and row["end_line"] else 0
            results.append({
                "qualified_name": row["qualified_name"],
                "file_path": row["rel_path"],
                "start_line": row["start_line"],
                "line_count": line_count,
                "depth": row["depth"] if row["depth"] >= 0 else 0,
                "module_path": row["module_path"],
            })

        results.sort(key=lambda x: x["line_count"], reverse=True)
        return results[:limit]

    def get_most_coupled_functions(self, limit: int = 30) -> List[Dict[str, Any]]:
        """获取扇入扇出总和最高的函数（耦合度最高的函数）

        Args:
            limit: 返回数量限制

        Returns:
            按总耦合度降序排列的函数列表
        """
        ws_id = self._get_active_workspace_id()

        # 扇入统计
        cur = self.conn.execute(
            """
            SELECT c.callee_qualified as name, COUNT(DISTINCT c.caller_id) as fan_in
            FROM calls c
            JOIN symbols s ON c.caller_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND c.callee_qualified != ''
            GROUP BY c.callee_qualified
            """,
            (ws_id,),
        )
        fan_in_map = {row["name"]: row["fan_in"] for row in cur}

        # 扇出统计
        cur = self.conn.execute(
            """
            SELECT s.qualified_name as name, COUNT(DISTINCT c.callee_name) as fan_out,
                   s.start_line, s.module_path, fi.rel_path
            FROM symbols s
            JOIN calls c ON c.caller_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.qualified_name != ''
            GROUP BY s.qualified_name
            """,
            (ws_id,),
        )

        results = []
        for row in cur:
            name = row["name"]
            fin = fan_in_map.get(name, 0)
            fout = row["fan_out"]
            results.append({
                "qualified_name": name,
                "file_path": row["rel_path"],
                "fan_in": fin,
                "fan_out": fout,
                "total_coupling": fin + fout,
                "module_path": row["module_path"],
            })

        results.sort(key=lambda x: x["total_coupling"], reverse=True)
        return results[:limit]

    def get_code_health_check(self, severity: str = "all") -> Dict[str, Any]:
        """代码健康检查：识别大文件、复杂函数、高耦合模块等需要重构的问题

        供 AI Agent 在修改代码前参考，避免因文件/函数过大导致
        Token 溢出、写入失败、理解困难等问题。

        Args:
            severity: 问题严重程度过滤（all / high / medium / low）

        Returns:
            健康检查报告，包含各类问题列表和总体评分
        """
        ws_id = self._get_active_workspace_id()
        issues: List[Dict[str, Any]] = []

        # 1. 大文件检查
        cur = self.conn.execute(
            """
            SELECT id, rel_path, abs_path, total_lines
            FROM file_instances
            WHERE workspace_id = ? AND total_lines > 0
            ORDER BY total_lines DESC
            LIMIT 20
            """,
            (ws_id,),
        )
        large_files = []
        for row in cur:
            lines = row["total_lines"]
            if lines >= 2000:
                sev = "high"
                advice = t("cli.messages.metrics_advice_file_huge", default="Severely oversized; strongly consider splitting by responsibility into multiple modules")
            elif lines >= 1000:
                sev = "medium"
                advice = t("cli.messages.metrics_advice_file_large", default="Large file; consider splitting")
            elif lines >= 500:
                sev = "low"
                advice = t("cli.messages.metrics_advice_file_medium", default="Consider splitting by responsibility")
            else:
                continue
            large_files.append({
                "file_path": row["rel_path"],
                "total_lines": lines,
                "severity": sev,
                "advice": advice,
            })
        issues.append({
            "category": "large_files",
            "title": t("cli.messages.metrics_title_large_files", default="Oversized files"),
            "count": len(large_files),
            "items": large_files,
        })

        # 2. 复杂函数检查
        hotspots = self.get_complexity_hotspots(limit=30)
        complex_functions = []
        for fn in hotspots:
            comp = fn["cyclomatic_complexity"]
            if comp >= 30:
                sev = "high"
                advice = t("cli.messages.metrics_advice_function_extreme", default="Extremely complex function; refactor and split before AI edits")
            elif comp >= 20:
                sev = "medium"
                advice = t("cli.messages.metrics_advice_function_complex", default="High complexity; split into smaller functions")
            elif comp >= 10:
                sev = "low"
                advice = t("cli.messages.metrics_advice_function_moderate", default="Moderate complexity; consider optimization")
            else:
                continue
            complex_functions.append({
                "qualified_name": fn["qualified_name"],
                "file_path": fn["file_path"],
                "start_line": fn["start_line"],
                "line_count": fn["line_count"],
                "cyclomatic_complexity": comp,
                "depth": fn["depth"],
                "severity": sev,
                "advice": advice,
            })
        issues.append({
            "category": "complex_functions",
            "title": t("cli.messages.metrics_title_complex_functions", default="Complex functions"),
            "count": len(complex_functions),
            "items": complex_functions,
        })

        # 3. 超长函数检查
        largest = self.get_largest_functions(limit=30)
        long_functions = []
        for fn in largest:
            lines = fn["line_count"]
            if lines >= 200:
                sev = "high"
                advice = t("cli.messages.metrics_advice_function_too_long", default="Function is too long; strongly consider splitting before AI reads or edits it")
            elif lines >= 100:
                sev = "medium"
                advice = t("cli.messages.metrics_advice_function_long", default="Long function; consider splitting")
            elif lines >= 50:
                sev = "low"
                advice = t("cli.messages.metrics_advice_function_split", default="Consider splitting")
            else:
                continue
            long_functions.append({
                "qualified_name": fn["qualified_name"],
                "file_path": fn["file_path"],
                "start_line": fn["start_line"],
                "line_count": lines,
                "depth": fn["depth"],
                "severity": sev,
                "advice": advice,
            })
        issues.append({
            "category": "long_functions",
            "title": t("cli.messages.metrics_title_long_functions", default="Long functions"),
            "count": len(long_functions),
            "items": long_functions,
        })

        # 4. 高耦合模块检查
        coupling = self.get_coupling_analysis(limit=20)
        high_coupling_modules = []
        for mod in coupling:
            inst = mod["instability"]
            if inst >= 0.9:
                sev = "high"
                advice = t("cli.messages.metrics_advice_coupling_extreme", default="Extremely unstable; heavily depends on external modules, so edits have broad impact")
            elif inst >= 0.7:
                sev = "medium"
                advice = t("cli.messages.metrics_advice_coupling_unstable", default="Unstable; depends on many external modules")
            elif mod["total_coupling"] >= 50:
                sev = "low"
                advice = t("cli.messages.metrics_advice_coupling_high", default="High coupling")
            else:
                continue
            high_coupling_modules.append({
                "module": mod["module"],
                "afferent": mod["afferent"],
                "efferent": mod["efferent"],
                "total_coupling": mod["total_coupling"],
                "instability": inst,
                "severity": sev,
                "advice": advice,
            })
        issues.append({
            "category": "high_coupling",
            "title": t("cli.messages.metrics_title_high_coupling", default="Highly coupled modules"),
            "count": len(high_coupling_modules),
            "items": high_coupling_modules,
        })

        # 按严重程度过滤
        if severity != "all":
            filtered_issues = []
            for cat in issues:
                filtered_items = [item for item in cat["items"] if item["severity"] == severity]
                if filtered_items:
                    filtered_cat = dict(cat)
                    filtered_cat["items"] = filtered_items
                    filtered_cat["count"] = len(filtered_items)
                    filtered_issues.append(filtered_cat)
            issues = filtered_issues

        # 计算总体健康评分（0-100，越高越好）
        high_count = sum(1 for cat in issues for item in cat["items"] if item["severity"] == "high")
        medium_count = sum(1 for cat in issues for item in cat["items"] if item["severity"] == "medium")
        low_count = sum(1 for cat in issues for item in cat["items"] if item["severity"] == "low")

        health_score = max(0, 100 - high_count * 5 - medium_count * 2 - low_count * 0.5)

        if health_score >= 80:
            health_level = t("cli.messages.metrics_health_good", default="good")
        elif health_score >= 60:
            health_level = t("cli.messages.metrics_health_fair", default="fair")
        elif health_score >= 40:
            health_level = t("cli.messages.metrics_health_poor", default="poor")
        else:
            health_level = t("cli.messages.metrics_health_bad", default="bad")

        return {
            "health_score": round(health_score, 1),
            "health_level": health_level,
            "high_issue_count": high_count,
            "medium_issue_count": medium_count,
            "low_issue_count": low_count,
            "total_issue_count": high_count + medium_count + low_count,
            "issues": issues,
            "agent_guidance": t("cli.messages.metrics_agent_guidance", default=(
                "Before editing code, AI agents should:\n"
                "1. Prefer small files; split large files before editing when possible\n"
                "2. Understand complex functions fully, or split them first\n"
                "3. Validate all call sites after editing highly coupled modules\n"
                "4. Refactor before editing functions over 200 lines or complexity > 30"
            )),
        }

    def check_file_health(self, file_path: str) -> Optional[Dict[str, Any]]:
        """检查单个文件的健康状态（供 Agent 修改文件前调用）

        Args:
            file_path: 文件路径

        Returns:
            文件健康报告，包含大小、复杂度、是否建议拆分等
        """
        ws_id = self._get_active_workspace_id()

        # 标准化路径（统一正斜杠）
        if not os.path.isabs(file_path):
            abs_path = os.path.join(self.workspace_root, file_path)
        else:
            abs_path = file_path
        abs_path = abs_path.replace("\\", "/")

        cur = self.conn.execute(
            """
            SELECT id, rel_path, abs_path, total_lines
            FROM file_instances
            WHERE workspace_id = ? AND abs_path = ?
            LIMIT 1
            """,
            (ws_id, abs_path),
        )
        file_row = cur.fetchone()
        if not file_row:
            return None

        file_id = file_row["id"]
        total_lines = file_row["total_lines"] or 0

        # 大文件判断
        if total_lines >= 2000:
            file_severity = "high"
            file_advice = t("cli.messages.metrics_file_advice_huge", default="File is severely oversized (2000+ lines); strongly split into modules before editing")
        elif total_lines >= 1000:
            file_severity = "medium"
            file_advice = t("cli.messages.metrics_file_advice_large", default="File is large (1000+ lines); split first or edit in small steps")
        elif total_lines >= 500:
            file_severity = "low"
            file_advice = t("cli.messages.metrics_file_advice_medium", default="File is medium sized (500+ lines); edit in focused chunks")
        else:
            file_severity = "normal"
            file_advice = t("cli.messages.metrics_file_advice_normal", default="File size is normal")

        # 获取该文件的函数列表
        cur = self.conn.execute(
            """
            SELECT s.qualified_name, s.start_line, s.end_line, s.depth,
                   s.module_path, s.signature, sc.content
            FROM symbols s
            LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            WHERE s.file_instance_id = ? AND s.kind IN ('fn', 'function', 'method')
            """,
            (file_id,),
        )

        from .config import detect_language_from_path
        lang = detect_language_from_path(file_row["rel_path"]) or ""

        function_issues = []
        max_complexity = 0
        max_lines = 0

        for row in cur:
            content = row["content"] or ""
            comp = self._compute_cyclomatic_complexity(content, lang) if content else 0
            lines = row["end_line"] - row["start_line"] + 1 if row["start_line"] and row["end_line"] else 0

            max_complexity = max(max_complexity, comp)
            max_lines = max(max_lines, lines)

            if comp >= 20 or lines >= 100:
                sev = "high" if comp >= 30 or lines >= 200 else "medium"
                function_issues.append({
                    "qualified_name": row["qualified_name"],
                    "start_line": row["start_line"],
                    "line_count": lines,
                    "cyclomatic_complexity": comp,
                    "severity": sev,
                    "advice": t("cli.messages.metrics_function_issue_advice", default="Function is too large/complex; split before editing"),
                })

        return {
            "file_path": file_row["rel_path"],
            "total_lines": total_lines,
            "max_function_complexity": max_complexity,
            "max_function_lines": max_lines,
            "file_severity": file_severity,
            "file_advice": file_advice,
            "function_issue_count": len(function_issues),
            "function_issues": function_issues[:10],
            "should_split_first": file_severity == "high" or any(f["severity"] == "high" for f in function_issues),
            "agent_warning": t("cli.messages.metrics_agent_warning", default=(
                "Warning: this file has high-severity issues. Full-file edits may cause:\n"
                "- Token overflow; the AI may not understand the whole file\n"
                "- Write failures because the file is too large to rewrite safely\n"
                "- Logic errors because complex functions are easy to break\n"
                "Recommendation: split into smaller files/functions, then edit incrementally"
            )) if file_severity == "high" or any(f["severity"] == "high" for f in function_issues) else "",
        }
