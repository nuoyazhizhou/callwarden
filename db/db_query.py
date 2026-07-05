"""
db_query.py
==========

代码知识图谱查询 Mixin 类。

提供符号查询、文件查询、状态统计、模块图导出等功能。
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from ..config import atomic_write_file


class QueryMixin:
    """查询功能 Mixin

    通过 self.conn 访问数据库连接，提供各种查询接口。
    """

    def get_stats(self) -> Dict:
        """获取当前工作区的统计信息，包括文件、符号、调用关系和注释覆盖

        Returns:
            包含 total_files / total_symbols / by_kind / total_calls /
            cross_file_calls / resolved_calls / commented 等键的统计字典
        """
        stats = {}
        ws_id = self._get_active_workspace_id()

        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM file_instances WHERE workspace_id = ?",
            (ws_id,),
        )
        stats["total_files"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
        """, (ws_id,))
        stats["total_symbols"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("""
            SELECT s.kind, COUNT(*) as cnt FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
            GROUP BY s.kind ORDER BY cnt DESC
        """, (ws_id,))
        stats["by_kind"] = {row["kind"]: row["cnt"] for row in cur}

        cur = self.conn.execute("SELECT COUNT(*) as cnt FROM calls")
        stats["total_calls"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("SELECT COUNT(*) as cnt FROM calls WHERE is_cross_file = 1")
        stats["cross_file_calls"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("SELECT COUNT(*) as cnt FROM calls WHERE callee_id IS NOT NULL")
        stats["resolved_calls"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.comment_status = 'done'
        """, (ws_id,))
        stats["commented"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("""
            SELECT s.depth, COUNT(*) as cnt FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.kind IN ('fn', 'test_fn') AND s.depth >= 0
            GROUP BY s.depth ORDER BY s.depth
        """, (ws_id,))
        stats["depth_distribution"] = {row["depth"]: row["cnt"] for row in cur}

        cur = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM file_versions fv
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
        """, (ws_id,))
        stats["total_file_versions"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM file_versions fv
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fv.is_current = 1
        """, (ws_id,))
        stats["current_files"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("SELECT COUNT(*) as cnt FROM symbol_contents")
        stats["unique_symbol_contents"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
        """, (ws_id,))
        stats["total_file_symbol_links"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM call_versions cv
            JOIN file_versions fv ON cv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
        """, (ws_id,))
        stats["total_call_versions"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM (
                SELECT fv.file_instance_id FROM file_versions fv
                JOIN file_instances fi ON fv.file_instance_id = fi.id
                WHERE fi.workspace_id = ?
                GROUP BY fv.file_instance_id HAVING COUNT(*) > 1
            )
        """, (ws_id,))
        stats["multi_version_files"] = cur.fetchone()["cnt"]

        return stats


    def get_status(self) -> Dict:
        """获取代码图谱状态概览（用于 cg status 命令）"""
        import time as _time
        ws_id = self._get_active_workspace_id()
        ws = self.get_active_workspace()
        stats = self.get_stats()

        now = _time.time()
        scanned_files = self._scan_supported_files()
        current_set = set(scanned_files)

        cur = self.conn.execute(
            "SELECT rel_path, mtime, last_parsed FROM file_instances WHERE workspace_id = ?",
            (ws_id,),
        )
        db_files = {}
        stale_files = []
        deleted_files = []
        for row in cur:
            db_files[row["rel_path"]] = dict(row)
            if row["rel_path"] not in current_set:
                deleted_files.append(row["rel_path"])
            else:
                abs_p = os.path.join(self.workspace_root, row["rel_path"])
                try:
                    disk_mtime = os.path.getmtime(abs_p)
                    if abs(disk_mtime - row["mtime"]) > 0.001:
                        stale_files.append(row["rel_path"])
                except OSError:
                    stale_files.append(row["rel_path"])

        new_files = [f for f in current_set if f not in db_files]

        cur = self.conn.execute("SELECT COUNT(*) as c FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id WHERE fi.workspace_id = ? AND s.has_comment = 0 AND s.kind IN ('fn','test_fn','method')", (ws_id,))
        uncommented_fns = cur.fetchone()["c"]

        # 使用 self.db_path（已按工作区 hash 自动计算），避免硬编码路径
        # 旧代码写死 ~/.callwarden/callwarden.db，但实际路径是 ~/.callwarden/{hash}/callwarden.db
        db_path = self.db_path
        db_size = 0
        if db_path and os.path.exists(db_path):
            db_size = os.path.getsize(db_path)

        last_parsed_times = []
        for row in self.conn.execute("SELECT last_parsed FROM file_instances WHERE workspace_id = ? AND last_parsed > 0", (ws_id,)):
            last_parsed_times.append(row["last_parsed"])
        last_build = max(last_parsed_times) if last_parsed_times else 0

        lang_dist = {}
        for row in self.conn.execute("""
            SELECT substr(fi.rel_path, instr(fi.rel_path, '.') + 1) as ext, COUNT(*) as cnt
            FROM file_instances fi WHERE fi.workspace_id = ? AND fi.rel_path LIKE '%.%'
            GROUP BY ext ORDER BY cnt DESC
        """, (ws_id,)):
            ext = row["ext"].rsplit(".", 1)[-1] if "." in row["ext"] else row["ext"]
            lang_dist[ext] = lang_dist.get(ext, 0) + row["cnt"]

        total_calls = stats.get("total_calls", 0)
        resolved_calls = stats.get("resolved_calls", 0)
        resolve_rate = (resolved_calls / total_calls * 100) if total_calls > 0 else 0

        return {
            "workspace": {
                "name": ws["name"] if ws else "unknown",
                "root": ws["root_path"] if ws else self.workspace_root,
                "db_size": db_size,
            },
            "files": {
                "tracked": stats.get("current_files", 0),
                "on_disk": len(current_set),
                "new": len(new_files),
                "stale": len(stale_files),
                "deleted": len(deleted_files),
                "new_files": new_files[:10],
                "stale_files": stale_files[:10],
                "deleted_files": deleted_files[:10],
                "by_language": lang_dist,
            },
            "symbols": {
                "total": stats.get("total_symbols", 0),
                "by_kind": stats.get("by_kind", {}),
                "uncommented_fns": uncommented_fns,
            },
            "calls": {
                "total": total_calls,
                "resolved": resolved_calls,
                "cross_file": stats.get("cross_file_calls", 0),
                "resolve_rate": round(resolve_rate, 1),
            },
            "depth": stats.get("depth_distribution", {}),
            "last_build": last_build,
            "needs_rebuild": len(new_files) + len(stale_files) > 0,
        }


    def get_topological_order(self, limit: int = 100) -> List[Dict]:
        """按拓扑深度排序（depth 小的在前 = 底层函数在前）"""
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            """SELECT s.*, fi.rel_path, fi.abs_path
               FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND s.kind = 'fn'
               ORDER BY s.depth ASC, s.start_line ASC
               LIMIT ?""",
            (ws_id, limit),
        )
        return [dict(row) for row in cur]


    def get_callers(self, callee_name: str) -> List[Dict]:
        """查询谁调用了这个函数"""
        cur = self.conn.execute(
            """SELECT c.*, s.name as caller_name, fi.rel_path as caller_file
               FROM calls c
               JOIN symbols s ON c.caller_id = s.id
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE c.callee_name = ?
               ORDER BY fi.rel_path, c.call_line""",
            (callee_name,),
        )
        return [dict(row) for row in cur]


    def get_callees(self, caller_name: str) -> List[Dict]:
        """查询这个函数调用了谁"""
        cur = self.conn.execute(
            """SELECT c.callee_name, c.callee_file, c.callee_qualified, c.call_line, c.is_cross_file
               FROM calls c
               JOIN symbols s ON c.caller_id = s.id
               WHERE s.name = ?
               ORDER BY c.call_line""",
            (caller_name,),
        )
        return [dict(row) for row in cur]


    def get_file_by_path(self, file_path: str) -> Optional[Dict]:
        """通过路径获取文件实例信息"""
        ws_id = self._get_active_workspace_id()
        rel_path = norm_path(file_path)
        cur = self.conn.execute(
            "SELECT * FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
            (ws_id, rel_path),
        )
        row = cur.fetchone()
        return dict(row) if row else None


    def get_symbol_history(self, qualified_name: str) -> List[Dict]:
        """查看某个符号的所有历史版本（按时间排序）"""
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            """SELECT 
               fsv.symbol_hash,
               fsv.qualified_name,
               fsv.start_line,
               fsv.end_line,
               fsv.module_path,
               fsv.is_deleted,
               fv.version_num,
               fv.parsed_at,
               fv.mtime,
               fv.is_current,
               fi.rel_path as file_path
               FROM file_symbol_versions fsv
               JOIN file_versions fv ON fsv.file_version_id = fv.id
               JOIN file_instances fi ON fv.file_instance_id = fi.id
               WHERE fsv.qualified_name = ? AND fi.workspace_id = ?
               ORDER BY fv.parsed_at DESC""",
            (qualified_name, ws_id),
        )
        return [dict(row) for row in cur]


    def get_file_history(self, file_path: str) -> List[Dict]:
        """查看某个文件的所有历史版本（按时间排序）"""
        ws_id = self._get_active_workspace_id()
        rel_path = norm_path(os.path.relpath(file_path, self.workspace_root)) if os.path.isabs(file_path) else file_path
        cur = self.conn.execute(
            """SELECT fv.*, fi.rel_path
               FROM file_versions fv
               JOIN file_instances fi ON fv.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND fi.rel_path = ?
               ORDER BY fv.version_num DESC""",
            (ws_id, rel_path),
        )
        return [dict(row) for row in cur]


    def get_symbol_content_by_hash(self, content_hash: str) -> Optional[Dict]:
        """通过 hash 获取函数内容"""
        cur = self.conn.execute(
            "SELECT * FROM symbol_contents WHERE content_hash = ?",
            (content_hash,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


    def _parse_since(self, since: str) -> float:
        """解析时间字符串（1h, 30m, 1d, 2h30m）返回秒数"""
        total = 0
        import re
        matches = re.findall(r'(\d+)([hdms])', since)
        for num, unit in matches:
            num = int(num)
            if unit == 'd':
                total += num * 86400
            elif unit == 'h':
                total += num * 3600
            elif unit == 'm':
                total += num * 60
            elif unit == 's':
                total += num
        return total


    def get_recent_changes(self, since: str) -> Dict:
        """查看最近变化的文件和函数"""
        ws_id = self._get_active_workspace_id()
        seconds = self._parse_since(since)
        cutoff = time.time() - seconds

        cur = self.conn.execute(
            """SELECT fv.*, fi.rel_path
               FROM file_versions fv
               JOIN file_instances fi ON fv.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND fv.parsed_at > ?
               ORDER BY fv.parsed_at DESC""",
            (ws_id, cutoff),
        )
        changed_files = [dict(row) for row in cur]

        changed_functions = []
        for fv in changed_files:
            if fv["version_num"] <= 1:
                continue

            cur = self.conn.execute(
                """SELECT qualified_name, symbol_hash
                   FROM file_symbol_versions
                   WHERE file_version_id = (
                       SELECT id FROM file_versions 
                       WHERE file_instance_id = ? AND version_num = ?
                   )""",
                (fv["file_instance_id"], fv["version_num"] - 1),
            )
            prev_hashes = {row["qualified_name"]: row["symbol_hash"] for row in cur}

            cur = self.conn.execute(
                """SELECT qualified_name, symbol_hash, start_line, is_deleted
                   FROM file_symbol_versions
                   WHERE file_version_id = ?""",
                (fv["id"],),
            )
            curr_hashes = {row["qualified_name"]: (row["symbol_hash"], row["start_line"], row["is_deleted"]) for row in cur}

            all_names = set(prev_hashes.keys()) | set(curr_hashes.keys())
            for name in all_names:
                prev_h = prev_hashes.get(name)
                curr_info = curr_hashes.get(name)
                curr_h = curr_info[0] if curr_info else None
                is_deleted = curr_info[2] if curr_info else 0

                if prev_h != curr_h:
                    change_type = ""
                    if not prev_h and not is_deleted:
                        change_type = "新增"
                    elif is_deleted or not curr_h:
                        change_type = "删除"
                    else:
                        change_type = "修改"

                    line = curr_info[1] if curr_info else 0
                    changed_functions.append({
                        "qualified_name": name,
                        "file_path": fv["rel_path"],
                        "change_type": change_type,
                        "version": fv["version_num"],
                        "parsed_at": fv["parsed_at"],
                        "line": line,
                        "prev_hash": prev_h or "",
                        "curr_hash": curr_h or "",
                    })

        return {
            "changed_files": changed_files,
            "changed_functions": changed_functions,
            "since_seconds": seconds,
        }


    def get_symbol_location(self, name: str, file_path: Optional[str] = None) -> Optional[Dict]:
        """查询符号位置"""
        ws_id = self._get_active_workspace_id()
        if file_path:
            rel_path = os.path.relpath(file_path, self.workspace_root)
            cur = self.conn.execute(
                """SELECT s.*, fi.rel_path, fi.abs_path 
                   FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id
                   WHERE fi.workspace_id = ? AND s.name = ? AND fi.rel_path = ?""",
                (ws_id, name, rel_path),
            )
        else:
            cur = self.conn.execute(
                """SELECT s.*, fi.rel_path, fi.abs_path 
                   FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id
                   WHERE fi.workspace_id = ? AND s.name = ?""",
                (ws_id, name),
            )
        row = cur.fetchone()
        return dict(row) if row else None


    def get_file_symbols(self, file_path: str) -> List[Dict]:
        """获取文件内所有符号"""
        ws_id = self._get_active_workspace_id()
        rel_path = os.path.relpath(file_path, self.workspace_root) if os.path.isabs(file_path) else file_path
        cur = self.conn.execute(
            """SELECT s.* FROM symbols s 
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND fi.rel_path = ?
               ORDER BY s.start_line""",
            (ws_id, rel_path),
        )
        return [dict(row) for row in cur]


    def search_symbols(self, query: str, kind: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """搜索符号

        Args:
            query: 搜索关键词（模糊匹配 qualified_name 和 name）
            kind: 符号类型过滤（可选）
            limit: 结果数量限制

        Returns:
            匹配的符号列表
        """
        ws_id = self._get_active_workspace_id()
        sql = """
            SELECT DISTINCT
                fsv.qualified_name,
                fsv.module_path,
                fsv.start_line,
                fsv.end_line,
                fsv.depth,
                sc.name,
                sc.kind,
                sc.signature,
                sc.has_comment,
                fi.rel_path as file_path
            FROM file_symbol_versions fsv
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fv.is_current = 1 AND fsv.is_deleted = 0
              AND (fsv.qualified_name LIKE ? OR sc.name LIKE ?)
        """

        params = [ws_id, f"%{query}%", f"%{query}%"]

        if kind:
            sql += " AND sc.kind = ?"
            params.append(kind)

        sql += " ORDER BY sc.kind, fsv.depth DESC, fi.rel_path, fsv.start_line LIMIT ?"
        params.append(limit)

        cur = self.conn.execute(sql, params)
        return [dict(row) for row in cur]


    def get_symbol(self, qualified_name: str) -> Optional[Dict]:
        """获取符号详情（包括调用关系）

        Args:
            qualified_name: 符号的限定名

        Returns:
            符号详情字典，包含基本信息、调用的函数、被谁调用
        """
        ws_id = self._get_active_workspace_id()
        sql = """
            SELECT DISTINCT
                fsv.qualified_name,
                fsv.module_path,
                fsv.start_line,
                fsv.end_line,
                fsv.depth,
                sc.name,
                sc.kind,
                sc.signature,
                sc.has_comment,
                sc.comment_content,
                sc.content_hash,
                fi.rel_path as file_path,
                fi.abs_path
            FROM file_symbol_versions fsv
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fv.is_current = 1 AND fsv.is_deleted = 0
              AND fsv.qualified_name = ?
            LIMIT 1
        """

        cur = self.conn.execute(sql, (ws_id, qualified_name))
        row = cur.fetchone()
        if not row:
            return None

        result = dict(row)

        out_sql = """
            SELECT DISTINCT
                cv.callee_qualified as target_name,
                cv.callee_module as target_module,
                cv.callee_file as target_file,
                cv.call_line
            FROM call_versions cv
            JOIN file_versions fv ON cv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fv.is_current = 1
              AND cv.caller_qualified = ?
            ORDER BY cv.callee_qualified
        """

        cur = self.conn.execute(out_sql, (ws_id, qualified_name))
        result["calls_out"] = [dict(row) for row in cur]

        in_sql = """
            SELECT DISTINCT
                cv.caller_qualified as caller_name,
                cv.caller_hash,
                cv.call_line,
                fi.rel_path as caller_file
            FROM call_versions cv
            JOIN file_versions fv ON cv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fv.is_current = 1
              AND cv.callee_qualified = ?
            ORDER BY cv.caller_qualified
        """

        cur = self.conn.execute(in_sql, (ws_id, qualified_name))
        result["called_by"] = [dict(row) for row in cur]

        # 注入 applicable_rules（fail-soft：无 AgentRulesMixin 或异常时降级为空列表）
        # 上下文：qualified_name / file_path / kind / 推断 language
        if hasattr(self, "get_applicable_rules_for_symbol"):
            try:
                result["applicable_rules"] = self.get_applicable_rules_for_symbol(
                    qualified_name=result.get("qualified_name", qualified_name),
                    file_path=result.get("file_path", ""),
                    kind=result.get("kind", ""),
                    limit=5,
                )
            except Exception:
                result["applicable_rules"] = []
        else:
            # 兼容未启用 AgentRulesMixin 的部署
            result["applicable_rules"] = []

        return result

    def get_symbol_by_name_and_file(self, symbol_name: str, file_path: str) -> Optional[Dict]:
        """通过符号名和文件名获取符号详情

        Args:
            symbol_name: 符号名称（短名或限定名）
            file_path: 文件路径（相对或绝对）

        Returns:
            符号详情字典
        """
        ws_id = self._get_active_workspace_id()
        sql = """
            SELECT
                fsv.qualified_name,
                fsv.module_path,
                fsv.start_line,
                fsv.end_line,
                fsv.depth,
                sc.name,
                sc.kind,
                sc.signature,
                sc.content_hash,
                fi.rel_path as file,
                fi.abs_path
            FROM file_symbol_versions fsv
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fv.is_current = 1 AND fsv.is_deleted = 0
              AND (sc.name = ? OR fsv.qualified_name = ?)
              AND (fi.rel_path = ? OR fi.abs_path = ? OR fi.rel_path LIKE '%' || ?)
            LIMIT 1
        """
        cur = self.conn.execute(sql, (ws_id, symbol_name, symbol_name, file_path, file_path, file_path))
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)


    def export_module_graph(self, format: str = "mermaid", output_file: str = "") -> str:
        """导出模块依赖图

        Args:
            format: 输出格式（mermaid 或 dot）
            output_file: 输出文件路径，为空则返回字符串

        Returns:
            依赖图内容
        """
        ws_id = self._get_active_workspace_id()
        module_stats = defaultdict(lambda: {
            "call_count": 0,
            "callers": set(),
            "callees": set(),
        })

        sql = """
            SELECT 
                cv.caller_qualified,
                cv.callee_qualified,
                COUNT(*) as call_count
            FROM call_versions cv
            JOIN file_versions fv ON cv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fv.is_current = 1
              AND cv.caller_qualified != ''
              AND cv.callee_qualified != ''
              AND cv.caller_qualified LIKE '%::%'
              AND cv.callee_qualified LIKE '%::%'
            GROUP BY cv.caller_qualified, cv.callee_qualified
        """
        cur = self.conn.execute(sql, (ws_id,))

        all_modules = set()

        def get_top_module(name):
            """从限定名中提取顶级模块（取前 2-3 段）"""
            parts = name.split("::")
            if len(parts) >= 3:
                return "::".join(parts[:3])
            elif len(parts) >= 2:
                return "::".join(parts[:2])
            else:
                return name

        for row in cur:
            caller_mod = get_top_module(row["caller_qualified"])
            callee_mod = get_top_module(row["callee_qualified"])

            if caller_mod != callee_mod:
                key = (caller_mod, callee_mod)
                module_stats[key]["call_count"] += row["call_count"]
                all_modules.add(caller_mod)
                all_modules.add(callee_mod)

        if format == "mermaid":
            lines = ["flowchart TD"]
            for mod in sorted(all_modules):
                safe_name = mod.replace("::", "_").replace("-", "_")
                lines.append(f"    {safe_name}[\"{mod}\"]")

            lines.append("")
            for (caller, callee), stats in sorted(module_stats.items(), key=lambda x: x[1]["call_count"], reverse=True):
                caller_safe = caller.replace("::", "_").replace("-", "_")
                callee_safe = callee.replace("::", "_").replace("-", "_")
                weight = min(stats["call_count"], 10)
                arrow = "-->" if weight < 5 else f"-- \"{stats['call_count']}\" -->"
                lines.append(f"    {caller_safe}{arrow}{callee_safe}")

            content = "\n".join(lines) + "\n"

        elif format == "dot":
            lines = ["digraph module_dependencies {"]
            lines.append("    rankdir=LR;")
            lines.append("    node [shape=box, style=filled, fillcolor=lightblue];")
            lines.append("")

            for mod in sorted(all_modules):
                safe_name = mod.replace("::", "_").replace("-", "_")
                lines.append(f'    {safe_name} [label="{mod}"];')

            lines.append("")
            for (caller, callee), stats in sorted(module_stats.items(), key=lambda x: x[1]["call_count"], reverse=True):
                caller_safe = caller.replace("::", "_").replace("-", "_")
                callee_safe = callee.replace("::", "_").replace("-", "_")
                penwidth = max(1, min(stats["call_count"] / 10, 5))
                lines.append(f'    {caller_safe} -> {callee_safe} [label="{stats["call_count"]}", penwidth={penwidth:.1f}];')

            lines.append("}")
            content = "\n".join(lines) + "\n"

        else:
            raise ValueError(f"不支持的格式: {format}")

        if output_file:
            # SEC-001：原子写入，避免半写入状态
            atomic_write_file(output_file, content)
            return output_file

        return content
