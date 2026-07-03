"""
db_summary.py
=============

代码摘要与项目简报 Mixin 类。
提供 AI 摘要生成、版本化存储、项目简报和仓库地图功能。

依赖：
- self.conn：sqlite3.Connection（由 CodeGraphBase 提供）
- self._get_active_workspace_id()：获取活动工作区 ID
- 复用 MetricsMixin 的 get_code_metrics_summary / get_code_health_check / get_complexity_hotspots
- 复用 QueryMixin 的 get_status
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


class SummaryMixin:
    """代码摘要功能 Mixin

    通过 self.conn 访问数据库连接，提供 AI 摘要的版本化存储、
    项目简报（高层概览）和仓库地图（模块依赖图）功能。
    """

    def generate_summary(self, qualified_name: str, summary: str, model: str = "manual") -> dict:
        """为函数生成/保存摘要（手动传入摘要文本）

        采用版本化存储：同一符号的旧摘要会被标记为 is_current=0，
        新摘要 version 自增。symbol_summaries.id 由 AUTOINCREMENT 自动生成。

        Args:
            qualified_name: 函数限定名
            summary: 摘要文本
            model: 摘要生成模型标识（默认 manual 表示手写）

        Returns:
            包含 symbol_hash、summary、version 的字典；未找到符号时返回 error
        """
        ws_id = self._get_active_workspace_id()

        # 查找 symbol_hash
        cur = self.conn.execute(
            """SELECT s.symbol_hash FROM symbols s
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND s.qualified_name = ?""",
            (ws_id, qualified_name),
        )
        row = cur.fetchone()
        if not row:
            return {"error": f"未找到函数: {qualified_name}"}
        symbol_hash = row["symbol_hash"]

        # 旧摘要标记为非当前
        self.conn.execute(
            "UPDATE symbol_summaries SET is_current = 0 WHERE symbol_hash = ?",
            (symbol_hash,),
        )

        # 获取下一个版本号
        cur = self.conn.execute(
            "SELECT MAX(version) as v FROM symbol_summaries WHERE symbol_hash = ?",
            (symbol_hash,),
        )
        next_version = (cur.fetchone()["v"] or 0) + 1

        # 插入新摘要（id 由 AUTOINCREMENT 自动生成，不显式指定）
        self.conn.execute(
            """INSERT INTO symbol_summaries
               (symbol_hash, summary, model, version, is_current, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (symbol_hash, summary, model, next_version, time.time()),
        )
        self.conn.commit()

        return {"symbol_hash": symbol_hash, "summary": summary, "version": next_version}

    def get_summary(self, qualified_name: str) -> Optional[dict]:
        """获取函数的当前摘要

        Args:
            qualified_name: 函数限定名

        Returns:
            摘要字典（包含 qualified_name、summary、model、version），无则返回 None
        """
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            """SELECT ss.summary, ss.model, ss.version, ss.created_at,
                      s.qualified_name, fi.rel_path
               FROM symbol_summaries ss
               JOIN symbols s ON ss.symbol_hash = s.symbol_hash
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND s.qualified_name = ? AND ss.is_current = 1""",
            (ws_id, qualified_name),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "qualified_name": row["qualified_name"],
            "summary": row["summary"],
            "model": row["model"],
            "version": row["version"],
        }

    def project_brief(self) -> dict:
        """生成项目简报

        汇总项目类型、文件/函数/行数、模块列表、复杂度热点、健康评分等
        高层信息，供 AI Agent 快速了解项目全貌。

        复用已有方法（若不存在则跳过对应数据）：
        - QueryMixin.get_status
        - MetricsMixin.get_code_metrics_summary / get_code_health_check / get_complexity_hotspots

        Returns:
            项目简报字典
        """
        # 复用已有方法（若不存在则返回空）
        status = self.get_status() if hasattr(self, "get_status") else {}
        metrics = self.get_code_metrics_summary() if hasattr(self, "get_code_metrics_summary") else {}
        health = self.get_code_health_check("high") if hasattr(self, "get_code_health_check") else {}

        ws_id = self._get_active_workspace_id()

        # 判断项目类型：按文件扩展名分布统计（不依赖 status["files"] 结构）
        cur = self.conn.execute(
            """SELECT rel_path FROM file_instances
               WHERE workspace_id = ? AND rel_path LIKE '%.%'""",
            (ws_id,),
        )
        ext_counts: Dict[str, int] = {}
        for row in cur:
            ext = row["rel_path"].rsplit(".", 1)[-1].lower()
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

        if ext_counts:
            # 取出现次数最多的扩展名作为主语言
            top_ext = max(ext_counts.items(), key=lambda x: x[1])[0]
            lang_map = {
                "rs": "Rust",
                "py": "Python",
                "ts": "TypeScript",
                "tsx": "TypeScript",
                "js": "JavaScript",
                "jsx": "JavaScript",
                "go": "Go",
                "java": "Java",
                "c": "C",
                "cpp": "C++",
                "h": "C/C++",
            }
            project_type = lang_map.get(top_ext, f"Multi-language (.{top_ext})")
        else:
            project_type = "Unknown"

        # 获取模块列表（按函数数量降序，最多 20 个）
        cur = self.conn.execute(
            """SELECT DISTINCT s.module_path, COUNT(*) as fn_count
               FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND s.module_path != ''
                 AND s.kind IN ('fn', 'function', 'method')
               GROUP BY s.module_path
               ORDER BY fn_count DESC
               LIMIT 20""",
            (ws_id,),
        )
        modules = [
            {"module": row["module_path"], "function_count": row["fn_count"]}
            for row in cur
        ]

        # 获取热点函数（复用 MetricsMixin）
        hotspots = (
            self.get_complexity_hotspots(limit=10)
            if hasattr(self, "get_complexity_hotspots")
            else []
        )

        # 文件数优先取 metrics，再回退到 status["files"]["tracked"]
        file_count = metrics.get("file_count", 0)
        if not file_count and isinstance(status.get("files"), dict):
            file_count = status["files"].get("tracked", 0)

        return {
            "project_type": project_type,
            "file_count": file_count,
            "function_count": metrics.get("function_count", 0),
            "total_lines": metrics.get("total_lines", 0),
            "modules": modules,
            "hot_functions": hotspots,
            "health_score": health.get("health_score", 0),
            "health_level": health.get("health_level", "未知"),
            "avg_complexity": metrics.get("avg_complexity", 0),
            "comment_coverage": metrics.get("comment_coverage", 0),
        }

    def repo_map(self, format: str = "text") -> str:
        """生成仓库模块依赖地图

        基于模块间调用关系（跨模块调用）构建依赖图。

        Args:
            format: 输出格式，"text" 纯文本列表，"mermaid" Mermaid 图表语法

        Returns:
            仓库地图字符串
        """
        ws_id = self._get_active_workspace_id()

        # 获取模块间跨模块调用关系
        cur = self.conn.execute(
            """SELECT s_caller.module_path as caller,
                      c.callee_module as callee,
                      COUNT(*) as cnt
               FROM calls c
               JOIN symbols s_caller ON c.caller_id = s_caller.id
               JOIN file_instances fi ON s_caller.file_instance_id = fi.id
               WHERE fi.workspace_id = ?
                 AND s_caller.module_path != ''
                 AND c.callee_module != ''
                 AND s_caller.module_path != c.callee_module
               GROUP BY s_caller.module_path, c.callee_module
               ORDER BY cnt DESC""",
            (ws_id,),
        )
        edges = [(row["caller"], row["callee"], row["cnt"]) for row in cur]

        if format == "mermaid":
            lines = ["graph TD"]
            for caller, callee, cnt in edges:
                # Mermaid 节点 ID 不能含点号，用下划线替换
                safe_caller = caller.replace(".", "_")
                safe_callee = callee.replace(".", "_")
                lines.append(f"    {safe_caller} -->|{cnt}| {safe_callee}")
            return "\n".join(lines)
        else:
            lines = ["仓库模块依赖图:", ""]
            for caller, callee, cnt in edges:
                lines.append(f"  {caller} → {callee} ({cnt} 次调用)")
            return "\n".join(lines)
