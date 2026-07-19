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

from ..config import atomic_write_file, norm_path


class QueryMixin:
    """查询功能 Mixin

    通过 self.conn 访问数据库连接，提供各种查询接口。
    """

    def get_stats(self) -> Dict:
        """获取当前工作区的统计信息，包括文件、符号、调用关系和注释覆盖

        性能优化（2026-07-19）：14 次串行 COUNT → 6 次 SQL，用 SUM(CASE WHEN) 合并同表多次扫描
        - SQL 1: file_instances + symbol_contents（scalar subquery 合并）
        - SQL 2: symbols 聚合（total_symbols + commented，SUM CASE WHEN）
        - SQL 3: calls 聚合（total_calls + cross_file + resolved，SUM CASE WHEN 单次扫描替代 3 次）
        - SQL 4: file_versions 聚合（total + current + multi_version，SUM CASE WHEN + 子查询）
        - SQL 5: file_symbol_versions + call_versions 计数（UNION ALL）
        - SQL 6: by_kind + depth_distribution（GROUP BY）

        EXPLAIN 实测各 SQL 耗时（100K 符号）：
        - calls 3 次 COUNT 串行: 64ms → 1 次 SUM CASE WHEN: ~25ms（节省 39ms）
        - symbols 2 次 COUNT: 4.2ms → 1 次 SUM CASE WHEN: ~2.5ms（节省 1.7ms）

        Returns:
            包含 total_files / total_symbols / by_kind / total_calls /
            cross_file_calls / resolved_calls / commented 等键的统计字典
        """
        stats = {}
        ws_id = self._get_active_workspace_id()

        # SQL 1：file_instances COUNT + symbol_contents COUNT（独立小表，scalar subquery 合并）
        cur = self.conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM file_instances
                 WHERE workspace_id = ? AND status != 'archived') AS total_files,
                (SELECT COUNT(*) FROM symbol_contents) AS unique_symbol_contents
        """, (ws_id,))
        row = cur.fetchone()
        stats["total_files"] = row["total_files"]
        stats["unique_symbol_contents"] = row["unique_symbol_contents"]

        # SQL 2：symbols 聚合（total_symbols + commented，单次扫描替代 2 次）
        cur = self.conn.execute("""
            SELECT
                COUNT(*) AS total_symbols,
                SUM(CASE WHEN s.comment_status = 'done' THEN 1 ELSE 0 END) AS commented
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fi.status != 'archived'
        """, (ws_id,))
        row = cur.fetchone()
        stats["total_symbols"] = row["total_symbols"]
        stats["commented"] = row["commented"] or 0

        # SQL 3：calls 聚合（total_calls + cross_file + resolved，单次扫描替代 3 次）
        cur = self.conn.execute("""
            SELECT
                COUNT(*) AS total_calls,
                SUM(CASE WHEN c.is_cross_file = 1 THEN 1 ELSE 0 END) AS cross_file_calls,
                SUM(CASE WHEN c.callee_id IS NOT NULL THEN 1 ELSE 0 END) AS resolved_calls
            FROM calls c
            JOIN symbols s ON c.caller_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fi.status != 'archived'
        """, (ws_id,))
        row = cur.fetchone()
        stats["total_calls"] = row["total_calls"]
        stats["cross_file_calls"] = row["cross_file_calls"] or 0
        stats["resolved_calls"] = row["resolved_calls"] or 0

        # SQL 4：file_versions 聚合（total + current + multi_version，单次扫描 + 子查询）
        cur = self.conn.execute("""
            SELECT
                COUNT(*) AS total_file_versions,
                SUM(CASE WHEN fv.is_current = 1 THEN 1 ELSE 0 END) AS current_files,
                (SELECT COUNT(*) FROM (
                    SELECT fv2.file_instance_id FROM file_versions fv2
                    JOIN file_instances fi2 ON fv2.file_instance_id = fi2.id
                    WHERE fi2.workspace_id = ? AND fi2.status != 'archived'
                    GROUP BY fv2.file_instance_id HAVING COUNT(*) > 1
                )) AS multi_version_files
            FROM file_versions fv
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fi.status != 'archived'
        """, (ws_id, ws_id))
        row = cur.fetchone()
        stats["total_file_versions"] = row["total_file_versions"]
        stats["current_files"] = row["current_files"] or 0
        stats["multi_version_files"] = row["multi_version_files"]

        # SQL 5：file_symbol_versions + call_versions（独立小查询，UNION ALL 合并）
        cur = self.conn.execute("""
            SELECT 'fsv' AS kind, COUNT(*) AS cnt FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fi.status != 'archived'
            UNION ALL
            SELECT 'cv' AS kind, COUNT(*) AS cnt FROM call_versions cv
            JOIN file_versions fv ON cv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fi.status != 'archived'
        """, (ws_id, ws_id))
        rows = cur.fetchall()
        for r in rows:
            if r["kind"] == "fsv":
                stats["total_file_symbol_links"] = r["cnt"]
            elif r["kind"] == "cv":
                stats["total_call_versions"] = r["cnt"]

        # SQL 6：by_kind GROUP BY（用 IN 子查询让优化器选 idx_symbols_kind_file covering index）
        # EXPLAIN: SEARCH s USING COVERING INDEX idx_symbols_kind_file (ANY(kind) AND file_instance_id=?) + BLOOM FILTER
        # 100K 符号实测：26ms（JOIN）→ 6ms（IN 子查询，4.3x 加速）
        cur = self.conn.execute("""
            SELECT s.kind, COUNT(*) as cnt FROM symbols s
            WHERE s.file_instance_id IN (
                SELECT id FROM file_instances
                WHERE workspace_id = ? AND status != 'archived'
            )
            GROUP BY s.kind ORDER BY cnt DESC
        """, (ws_id,))
        stats["by_kind"] = {row["kind"]: row["cnt"] for row in cur}

        # SQL 7：depth_distribution GROUP BY（用 IN 子查询让优化器选 idx_symbols_depth_file_fn 部分索引）
        # EXPLAIN: SEARCH s USING INDEX idx_symbols_depth_file_fn (depth>?) + BLOOM FILTER
        # 100K 符号实测：42ms（JOIN）→ 15ms（IN 子查询，2.8x 加速）
        cur = self.conn.execute("""
            SELECT s.depth, COUNT(*) as cnt FROM symbols s
            WHERE s.file_instance_id IN (
                SELECT id FROM file_instances
                WHERE workspace_id = ? AND status != 'archived'
            ) AND s.kind IN ('fn', 'test_fn') AND s.depth >= 0
            GROUP BY s.depth ORDER BY s.depth
        """, (ws_id,))
        stats["depth_distribution"] = {row["depth"]: row["cnt"] for row in cur}

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
            "SELECT rel_path, mtime, last_parsed FROM file_instances WHERE workspace_id = ? AND status != 'archived'",
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

        cur = self.conn.execute("SELECT COUNT(*) as c FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id WHERE fi.workspace_id = ? AND fi.status != 'archived' AND s.has_comment = 0 AND s.kind IN ('fn','test_fn','method')", (ws_id,))
        uncommented_fns = cur.fetchone()["c"]

        # 使用 self.db_path（已按工作区 hash 自动计算），避免硬编码路径
        # 旧代码写死 ~/.callwarden/callwarden.db，但实际路径是 ~/.callwarden/{hash}/callwarden.db
        db_path = self.db_path
        db_size = 0
        if db_path and os.path.exists(db_path):
            db_size = os.path.getsize(db_path)

        last_parsed_times = []
        for row in self.conn.execute("SELECT last_parsed FROM file_instances WHERE workspace_id = ? AND status != 'archived' AND last_parsed > 0", (ws_id,)):
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


    def get_callers(self, callee_name: str, qualified_name: Optional[str] = None) -> List[Dict]:
        """查询谁调用了这个函数

        QN 自动识别：传入含分隔符（. 或 ::）的名称时自动识别为 QN，
        同时提取短名用于索引查找。QN 查不到时自动降级为短名匹配。

        显式传入 qualified_name 参数时：精确匹配，QN 查不到返回空，不降级
        （避免跨模块短名误匹配）。

        P28：大规模下短名跨模块误匹配优化
        - 默认行为（qualified_name=None）：对齐原接口，按 callee_name 短名匹配
        - 传入 qualified_name：先查 callee_id，再用 callee_id 过滤边，
          避免多个模块都有同名函数（如 init/main/handle）导致跨模块误匹配
        """
        # QN 自动识别：含分隔符的名称视为 QN，提取短名用于索引查找
        # 自动识别的 QN 允许 fallback 到短名；显式传 qualified_name 时不 fallback
        auto_qn_fallback = False
        if qualified_name is None and ("." in callee_name or "::" in callee_name):
            qualified_name = callee_name
            callee_name = callee_name.rsplit(".", 1)[-1].rsplit("::", 1)[-1]
            auto_qn_fallback = True

        # B-P7b: Rust GraphStore 短路（CSR 内存查询，O(degree+k)）
        store = self._get_graph_store()
        if store is not None and store.load_state() == "graph_ready":
            try:
                rust_callers = store.get_callers(callee_name, qualified_name)
                if rust_callers is not None:
                    if qualified_name is not None:
                        materialized = list(rust_callers)
                        if materialized:
                            return materialized
                        # QN 过滤返回空：仅当自动识别 QN 时降级到纯短名
                        if auto_qn_fallback:
                            rust_callers = store.get_callers(callee_name, None)
                            if rust_callers is not None:
                                return rust_callers
                        return []  # 显式 QN 未找到 → 返回空
                    return rust_callers
            except Exception:
                pass  # Rust 查询异常 → 降级 SQL
        # P6 注：idx_calls_callee 已删除（GraphStore 覆盖 get_callers）。
        # SQL 降级路径（callwarden_core 未安装时）WHERE callee_name=? 走全表扫描。
        # P7：传入 qualified_name 时先在当前工作区解析 symbol id，再通过紧凑整数索引查边。
        # 显式 callee_id > 0 是 SQLite 使用 partial index 的必要条件。
        if qualified_name is not None:
            ws_id = self._get_active_workspace_id()
            cur = self.conn.execute(
                """SELECT c.*, s.name as caller_name, fi.rel_path as caller_file
                   FROM calls c
                   JOIN symbols s ON c.caller_id = s.id
                   JOIN file_instances fi ON s.file_instance_id = fi.id
                   WHERE fi.workspace_id = ?
                     AND c.callee_id > 0
                     AND c.callee_id = (
                         SELECT target.id
                         FROM symbols target
                         JOIN file_instances target_fi ON target.file_instance_id = target_fi.id
                         WHERE target_fi.workspace_id = ?
                           AND target.qualified_name = ?
                         LIMIT 1
                     )
                   ORDER BY fi.rel_path, c.call_line""",
                (ws_id, ws_id, qualified_name),
            )
            result = [dict(row) for row in cur]
            if result:
                return result
            # QN 查不到 → 仅自动识别 QN 时降级短名（QN 可能未入库或已删除）
            if not auto_qn_fallback:
                return []  # 显式 QN 未找到 → 返回空，不降级
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


    def get_callees(self, caller_name: str, qualified_name: Optional[str] = None) -> List[Dict]:
        """查询这个函数调用了谁

        QN 自动识别：传入含分隔符（. 或 ::）的名称时自动识别为 QN，
        同时提取短名用于索引查找。QN 查不到时自动降级为短名匹配。

        显式传入 qualified_name 参数时：精确匹配，QN 查不到返回空，不降级
        （避免跨模块短名误匹配）。

        P28：大规模下短名跨模块误匹配优化
        - 默认行为（qualified_name=None）：对齐原接口，按 caller_name 短名匹配
        - 传入 qualified_name：直接 qname→caller_id 走 CSR，避免多候选遍历
        """
        # QN 自动识别：含分隔符的名称视为 QN，提取短名用于索引查找
        # 自动识别的 QN 允许 fallback 到短名；显式传 qualified_name 时不 fallback
        auto_qn_fallback = False
        if qualified_name is None and ("." in caller_name or "::" in caller_name):
            qualified_name = caller_name
            caller_name = caller_name.rsplit(".", 1)[-1].rsplit("::", 1)[-1]
            auto_qn_fallback = True

        # B-P7b: Rust GraphStore 短路（CSR forward 遍历，O(degree)）
        store = self._get_graph_store()
        if store is not None and store.load_state() == "graph_ready":
            try:
                rust_callees = store.get_callees(caller_name, qualified_name)
                if rust_callees is not None:
                    if qualified_name is not None:
                        materialized = list(rust_callees)
                        if materialized:
                            return materialized
                        # QN 过滤返回空：仅当自动识别 QN 时降级到纯短名
                        if auto_qn_fallback:
                            rust_callees = store.get_callees(caller_name, None)
                            if rust_callees is not None:
                                return rust_callees
                        return []  # 显式 QN 未找到 → 返回空
                    return rust_callees
            except Exception:
                pass  # Rust 查询异常 → 降级 SQL
        # P28：传入 qualified_name 时，用 qualified_name 精确定位 caller
        if qualified_name is not None:
            cur = self.conn.execute(
                """SELECT c.callee_name, c.callee_file, c.callee_qualified, c.call_line, c.is_cross_file
                   FROM calls c
                   JOIN symbols s ON c.caller_id = s.id
                   WHERE s.qualified_name = ?
                   ORDER BY c.call_line""",
                (qualified_name,),
            )
            result = [dict(row) for row in cur]
            if result:
                return result
            # QN 查不到 → 仅自动识别 QN 时降级短名
            if not auto_qn_fallback:
                return []  # 显式 QN 未找到 → 返回空，不降级
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
            "SELECT * FROM file_instances WHERE workspace_id = ? AND rel_path = ? AND status != 'archived'",
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
               WHERE fsv.qualified_name = ? AND fi.workspace_id = ? AND fi.status != 'archived'
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
               WHERE fi.workspace_id = ? AND fi.rel_path = ? AND fi.status != 'archived'
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
                   WHERE fi.workspace_id = ? AND fi.status != 'archived' AND s.name = ?""",
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
               WHERE fi.workspace_id = ? AND fi.rel_path = ? AND fi.status != 'archived'
               ORDER BY s.start_line""",
            (ws_id, rel_path),
        )
        return [dict(row) for row in cur]


    def search_symbols(self, query: str, kind: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """搜索符号

        Args:
            query: 搜索关键词（模糊匹配 qualified_name 和 name，子串匹配）
            kind: 符号类型过滤（可选）
            limit: 结果数量限制

        Returns:
            匹配的符号列表

        路由策略（F20 路由反转，2026-07-19）：
        1. FTS5 trigram 倒排索引（主路径，query >= 3 字符）：O(log N + M)
        2. Rust GraphStore memchr 子串扫描（fallback 1，query < 3 字符或 FTS5 不可用）：O(N×L)
        3. LIKE %query% 全表扫描（fallback 2，FTS5 + Rust 都不可用）：O(N)

        实测（1M 符号）：FTS5 trigram 2.354ms > Rust memchr 3.132ms > LIKE 全表
        原路由把 Rust 放在 FTS5 前面是 B-P7b Rust 短路原则的误用，本次反转修正。
        详见 [docs/architecture.md §6 查询路径设计决策](../docs/architecture.md#6-查询路径设计决策graphstore-vs-sql-路由)。
        """
        ws_id = self._get_active_workspace_id()

        # 1. FTS5 trigram 主路径（query >= 3 字符）
        # FTS5 unicode61 + trigram tokenizer 把 snake_case/camelCase/::./ 自动分词
        # 例如 user_login_handler → [user, login, handler]，搜 "login" 即可命中
        # trigram 要求 query >= 3 字符，否则抛 ValueError 触发 fallback
        try:
            fts_query = self._build_fts_query(query)
            sql = """
                SELECT DISTINCT
                    s.qualified_name,
                    s.module_path,
                    s.start_line,
                    s.end_line,
                    s.depth,
                    s.name,
                    s.kind,
                    s.signature,
                    s.has_comment,
                    fi.rel_path as file_path
                FROM symbols_fts
                JOIN symbols s ON s.id = symbols_fts.rowid
                JOIN file_instances fi ON s.file_instance_id = fi.id
                WHERE fi.workspace_id = ? AND fi.status != 'archived'
                  AND symbols_fts MATCH ?
            """
            params: List[Any] = [ws_id, fts_query]

            if kind:
                sql += " AND s.kind = ?"
                params.append(kind)

            sql += " ORDER BY s.kind, s.depth DESC, fi.rel_path, s.start_line LIMIT ?"
            params.append(limit)

            cur = self.conn.execute(sql, params)
            return [dict(row) for row in cur]
        except Exception:
            # FTS5 不可用 / query < 3 字符 / query 含特殊语法 → 进入 Rust fallback
            pass

        # 2. Rust GraphStore memchr 子串扫描（fallback 1）
        # B-P7b: Rust GraphStore 快速过滤 + SQL 字段补全
        # Rust 做子串匹配（O(N×L) 扫描预计算的小写字段，零 SQL），
        # 返回最多 limit 个匹配的 symbol id，
        # 再用 SQL 按 id IN(...) 批量取完整字段（signature/has_comment 等）
        # 注意：空 query 跳过 Rust 短路（Rust 子串匹配空串返回空，语义错误；
        # 空 query 应返回所有符号，走 LIKE 路径）
        store = self._get_graph_store() if query else None
        if store is not None:
            try:
                rust_results = store.search_symbols(query, kind, limit)
                if rust_results is not None and len(rust_results) > 0:
                    ids = [r["id"] for r in rust_results]
                    placeholders = ",".join("?" * len(ids))
                    sql = f"""
                        SELECT DISTINCT
                            s.qualified_name, s.module_path, s.start_line, s.end_line,
                            s.depth, s.name, s.kind, s.signature, s.has_comment,
                            fi.rel_path as file_path
                        FROM symbols s
                        JOIN file_instances fi ON s.file_instance_id = fi.id
                        WHERE fi.workspace_id = ? AND fi.status != 'archived'
                          AND s.id IN ({placeholders})
                        ORDER BY s.kind, s.depth DESC, fi.rel_path, s.start_line
                        LIMIT ?
                    """
                    params = [ws_id] + ids + [limit]
                    cur = self.conn.execute(sql, params)
                    return [dict(row) for row in cur]
                elif rust_results is not None:
                    # Rust 返回空列表（有结果但为空）→ 直接返回
                    return []
            except Exception:
                pass  # Rust 查询异常 → 降级 LIKE

        # 3. LIKE %query% 全表扫描（fallback 2，最终兜底）
        return self._search_symbols_like(query, kind, limit)

    @staticmethod
    def _build_fts_query(query: str) -> str:
        """把用户输入转为 FTS5 MATCH 查询（trigram tokenizer）

        trigram 做任意子串匹配：搜 "order" 命中 "processOrderItem"。
        无需加 * 前缀，直接用原始子串。

        约束：
        - query 必须 >= 3 字符（trigram 的 3-gram 要求），否则抛异常触发 LIKE 回退
        - 提取字母数字部分，过滤 FTS5 特殊字符（双引号、OR/AND/NOT 关键字）
        """
        import re
        # 提取连续的字母数字下划线 token
        tokens = re.findall(r'[A-Za-z0-9_]+', query)
        if not tokens:
            raise ValueError("empty fts query")
        # trigram 要求每个 token >= 3 字符
        valid_tokens = [t for t in tokens if len(t) >= 3]
        if not valid_tokens:
            raise ValueError("all tokens shorter than 3 chars, fallback to LIKE")
        # 用双引号包裹每个 token，避免被解释为 FTS5 关键字（OR/AND/NOT）
        return ' '.join(f'"{t}"' for t in valid_tokens)

    def _search_symbols_like(self, query: str, kind: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """LIKE 回退路径（FTS5 不可用时使用，保持原查询语义）"""
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
            WHERE fi.workspace_id = ? AND fi.status != 'archived' AND fv.is_current = 1 AND fsv.is_deleted = 0
              AND (fsv.qualified_name LIKE ? OR sc.name LIKE ?)
        """

        params: List[Any] = [ws_id, f"%{query}%", f"%{query}%"]

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
            WHERE fi.workspace_id = ? AND fi.status != 'archived' AND fv.is_current = 1 AND fsv.is_deleted = 0
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
                COALESCE(NULLIF(cv.callee_qualified, ''), cv.callee_name) as target_name,
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

        # 注入 issues（fail-soft：静态检查数据缺失时降级为空列表）
        # 只注入前 5 条 WARNING+ 问题，避免 token 爆炸；完整列表用 cw issues <QN>
        try:
            all_issues = self.get_symbol_issues(qualified_name, include_info=False)
            result["issues"] = all_issues[:5]
            result["issues_total"] = len(all_issues)
        except Exception:
            result["issues"] = []
            result["issues_total"] = 0

        # 注入 test_cases（fail-soft：测试关联数据缺失时降级为空列表）
        # 只注入前 5 条测试 case；完整列表用 cw tests <QN>
        try:
            test_summary = self.get_test_coverage_summary(qualified_name)
            result["has_tests"] = test_summary["has_tests"]
            result["test_count"] = test_summary["test_count"]
            result["test_cases"] = test_summary["tests"][:5]
        except Exception:
            result["has_tests"] = False
            result["test_count"] = 0
            result["test_cases"] = []

        # 注入 evolution_summary（fail-soft：变更-缺陷关联数据缺失时降级为空）
        # 只注入摘要（change_count / defect_count / defect_rate / recent_defects 前3条）
        try:
            result["evolution_summary"] = self.get_defect_correlation_by_qn(qualified_name)
        except Exception:
            result["evolution_summary"] = {
                "qualified_name": qualified_name,
                "change_count": 0,
                "defect_count": 0,
                "defect_rate": 0.0,
                "recent_defects": [],
            }

        return result

    def find_symbol_at_line(self, file_path: str, line: int) -> Optional[Dict]:
        """查找给定文件给定行号所属的最内层符号

        用于 cw grep 的行号→符号映射：给定一个文件路径和行号，返回包含该行的
        最小范围符号（如某行在一个嵌套方法内，优先返回内层方法，而非外层函数）。

        Args:
            file_path: 文件路径（相对或绝对均可，内部归一化为相对路径）
            line: 行号（1-based）

        Returns:
            符号字典（qualified_name/name/kind/start_line/end_line），无匹配返回 None
        """
        ws_id = self._get_active_workspace_id()
        # 归一化为相对路径（与 file_instances.rel_path 对齐）
        if os.path.isabs(file_path):
            try:
                rel_path = os.path.relpath(file_path, self.workspace_root)
            except ValueError:
                # Windows 跨盘符 relpath 报错 → 直接用原路径
                rel_path = file_path
        else:
            rel_path = file_path
        # 路径分隔符统一为正斜杠（cw 数据库约定）
        rel_path = rel_path.replace("\\", "/")

        cur = self.conn.execute(
            """SELECT s.qualified_name, s.name, s.kind, s.start_line, s.end_line
               FROM symbols s
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND fi.rel_path = ? AND fi.status != 'archived'
                 AND s.start_line <= ? AND s.end_line >= ?
               ORDER BY (s.end_line - s.start_line) ASC
               LIMIT 1""",
            (ws_id, rel_path, line, line),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def find_symbols_at_lines(self, file_path: str, lines: List[int]) -> Dict[int, Optional[Dict]]:
        """批量查找多行号所属符号（用于 cw grep 减少重复 SQL）

        一次查询文件所有符号，在 Python 端做区间匹配，避免 N 次 SQL。

        Args:
            file_path: 文件路径
            lines: 行号列表（1-based）

        Returns:
            {line: 符号字典或 None} 映射
        """
        ws_id = self._get_active_workspace_id()
        if os.path.isabs(file_path):
            try:
                rel_path = os.path.relpath(file_path, self.workspace_root)
            except ValueError:
                rel_path = file_path
        else:
            rel_path = file_path
        rel_path = rel_path.replace("\\", "/")

        # 一次性取出该文件所有函数/方法符号（kind 为 fn/method/test_fn 等"有行号范围"的符号）
        cur = self.conn.execute(
            """SELECT s.qualified_name, s.name, s.kind, s.start_line, s.end_line
               FROM symbols s
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND fi.rel_path = ? AND fi.status != 'archived'
                 AND s.start_line > 0 AND s.end_line > 0
               ORDER BY (s.end_line - s.start_line) ASC""",
            (ws_id, rel_path),
        )
        symbols = [dict(row) for row in cur]

        # 在 Python 端做行号区间匹配（按范围升序，取第一个包含的即最内层）
        result: Dict[int, Optional[Dict]] = {}
        for line in lines:
            matched = None
            for sym in symbols:
                if sym["start_line"] <= line <= sym["end_line"]:
                    matched = sym
                    break  # 已按范围升序，第一个匹配即最内层
            result[line] = matched
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
            WHERE fi.workspace_id = ? AND fi.status != 'archived' AND fv.is_current = 1 AND fsv.is_deleted = 0
              AND (sc.name = ? OR fsv.qualified_name = ?)
              AND (fi.rel_path = ? OR fi.abs_path = ? OR fi.rel_path LIKE '%' || ?)
            LIMIT 1
        """
        cur = self.conn.execute(sql, (ws_id, symbol_name, symbol_name, file_path, file_path, file_path))
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)

    def get_symbol_issues(self, qualified_name: str, include_info: bool = False) -> List[Dict]:
        """查询符号相关的静态检查问题（Semgrep findings + Guardrail findings）

        整合两类静态检查数据，让 agent 查符号时一站式看到已知缺陷/告警。
        用于 cw issues <QN> 子命令，也被 get_symbol() fail-soft 注入。

        查询路径：
        1. semgrep_findings：按 symbol_qualified 精确匹配（首选）
                         OR file_instance_id + line 范围交集（兜底，行范围落在符号内）
        2. guardrail_findings：按 file_path + symbol_hash 匹配

        Args:
            qualified_name: 符号限定名
            include_info: 是否包含 INFO 级别（默认只 WARNING+，避免噪音）

        Returns:
            issues 列表，按 severity 降序（ERROR > WARNING > INFO），每条含：
            - source: "semgrep" / "guardrail"
            - rule_id, rule_name, severity, message, start_line, end_line
            - snippet, fix（仅 semgrep）
        """
        ws_id = self._get_active_workspace_id()

        # 先拿符号的 file_instance_id / start_line / end_line / file_path / symbol_hash
        sym_sql = """
            SELECT fi.id as file_instance_id, fi.rel_path as file_path,
                   fsv.start_line, fsv.end_line, fsv.symbol_hash
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fi.status != 'archived' AND fv.is_current = 1 AND fsv.is_deleted = 0
              AND fsv.qualified_name = ?
            LIMIT 1
        """
        cur = self.conn.execute(sym_sql, (ws_id, qualified_name))
        sym_row = cur.fetchone()
        if not sym_row:
            return []
        sym = dict(sym_row)

        issues: List[Dict] = []

        # 1. semgrep_findings：优先按 symbol_qualified 精确匹配，兜底用行范围交集
        sem_sql = """
            SELECT rule_id, rule_name, severity, confidence, message,
                   start_line, end_line, snippet, fix
            FROM semgrep_findings
            WHERE file_instance_id = ?
              AND (symbol_qualified = ? OR symbol_qualified = ''
                   OR (start_line BETWEEN ? AND ? AND end_line BETWEEN ? AND ?))
        """
        params = (sym["file_instance_id"], qualified_name,
                  sym["start_line"], sym["end_line"], sym["start_line"], sym["end_line"])
        if not include_info:
            sem_sql += " AND severity != 'INFO' AND severity != 'UNKNOWN'"
        sem_sql += " ORDER BY CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, start_line"
        try:
            cur = self.conn.execute(sem_sql, params)
            for row in cur:
                d = dict(row)
                d["source"] = "semgrep"
                issues.append(d)
        except Exception:
            pass  # fail-soft：表不存在或异常时降级为空

        # 2. guardrail_findings：按 file_path + symbol_hash 匹配
        guard_sql = """
            SELECT gf.rule_id, gr.category as rule_name, gf.severity, gf.message,
                   gf.status, gf.detected_at
            FROM guardrail_findings gf
            JOIN guardrail_rules gr ON gf.rule_id = gr.rule_id
            WHERE gf.file_path = ? AND gf.symbol_hash = ?
        """
        if not include_info:
            guard_sql += " AND gf.severity != 'info'"
        guard_sql += " ORDER BY CASE gf.severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END"
        try:
            cur = self.conn.execute(guard_sql, (sym["file_path"], sym["symbol_hash"]))
            for row in cur:
                d = dict(row)
                d["source"] = "guardrail"
                d["start_line"] = 0  # guardrail_findings 无行号
                d["end_line"] = 0
                issues.append(d)
        except Exception:
            pass  # fail-soft

        return issues


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
