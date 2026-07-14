"""
db_evolution.py
===============

代码演化智能 Mixin。

提供 function_change_frequency / defect_correlation / hotspot_evolution /
churn_analysis / refresh_evolution_metrics 等方法。
通过 Mixin 模式集成到 CodeGraphDB 主类。
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional


class EvolutionMixin:
    """代码演化智能 Mixin

    利用版本历史数据提供代码演化分析能力，包括：
    - 函数变更频率分析（function_change_frequency）
    - 函数变更与缺陷关联（defect_correlation）
    - 热点函数演化排名（hotspot_evolution）
    - 代码流失 churn 分析（churn_analysis）
    - 演化指标缓存批量刷新（refresh_evolution_metrics）

    通过 self.conn 访问数据库连接（sqlite3.Connection，row_factory=Row）。
    """

    # 时间窗口单位 -> 秒数
    _TIME_WINDOW_UNITS = {
        "d": 86400,             # 天
        "w": 7 * 86400,         # 周
        "m": 30 * 86400,        # 月（按 30 天近似）
        "y": 365 * 86400,       # 年
    }

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _parse_time_window(self, time_window: str) -> float:
        """解析时间窗口字符串为截止时间戳

        支持格式："30d"、"90d"、"1y"、"4w"、"6m"，空字符串表示无限制。

        Args:
            time_window: 时间窗口字符串

        Returns:
            截止时间戳（早于此时间的记录被过滤），返回 0.0 表示无限制
        """
        if not time_window:
            return 0.0
        m = re.match(r'^\s*(\d+)\s*([dwmy])\s*$', time_window)
        if not m:
            return 0.0
        num = int(m.group(1))
        seconds = self._TIME_WINDOW_UNITS.get(m.group(2), 0)
        if seconds <= 0:
            return 0.0
        return time.time() - num * seconds

    def _compute_change_distribution(self, timestamps: List[float]) -> Dict[str, Dict[str, int]]:
        """按天/周/月统计变更时间分布"""
        daily: Dict[str, int] = defaultdict(int)
        weekly: Dict[str, int] = defaultdict(int)
        monthly: Dict[str, int] = defaultdict(int)
        for ts in timestamps:
            lt = time.localtime(ts)
            daily[time.strftime("%Y-%m-%d", lt)] += 1
            weekly[time.strftime("%Y-W%W", lt)] += 1
            monthly[time.strftime("%Y-%m", lt)] += 1
        return {
            "daily": dict(daily),
            "weekly": dict(weekly),
            "monthly": dict(monthly),
        }

    def _compute_hotspot_scores(self, module_filter: str = "") -> List[Dict[str, Any]]:
        """计算所有函数符号的热点评分（内部共享方法）

        供 hotspot_evolution 与 refresh_evolution_metrics 复用，避免重复计算。
        评分公式：hotspot_score = change_frequency * 0.4 + defect_density * 0.3
                                  + cyclomatic_complexity * 0.3（均归一化到 0-1）
        """
        ws_id = self._get_active_workspace_id()
        now = time.time()

        # 查询所有函数符号（当前快照）
        sql = """
            SELECT s.symbol_hash, s.qualified_name, s.module_path,
                   s.start_line, s.end_line, sc.content, fi.rel_path
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ? AND s.kind IN ('fn', 'function', 'method')
              AND s.qualified_name != ''
        """
        params: list = [ws_id]
        if module_filter:
            sql += " AND s.module_path LIKE ?"
            params.append(f"{module_filter}%")

        cur = self.conn.execute(sql, params)
        symbols = cur.fetchall()
        if not symbols:
            return []

        # 批量预取变更次数（按 symbol_hash 分组，关联 file_versions 拿到时间）
        cur = self.conn.execute(
            """
            SELECT fsv.symbol_hash,
                   COUNT(DISTINCT fsv.file_version_id) as cnt,
                   MIN(fv.parsed_at) as first_seen,
                   MAX(fv.parsed_at) as last_changed
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            GROUP BY fsv.symbol_hash
            """,
        )
        change_map: Dict[str, Dict[str, Any]] = {}
        for row in cur:
            change_map[row["symbol_hash"]] = {
                "count": row["cnt"],
                "first_seen": row["first_seen"] or 0.0,
                "last_changed": row["last_changed"] or 0.0,
            }

        # 批量预取缺陷数（按 symbol_qualified 分组）
        cur = self.conn.execute(
            """
            SELECT symbol_qualified, COUNT(*) as cnt
            FROM semgrep_findings
            WHERE symbol_qualified != ''
            GROUP BY symbol_qualified
            """,
        )
        defect_map: Dict[str, int] = {row["symbol_qualified"]: row["cnt"] for row in cur}

        # MetricsMixin 的圈复杂度计算函数（若可用）
        complexity_fn = getattr(self, "_compute_cyclomatic_complexity", None)

        # 第一轮：计算每个符号的原始指标，并记录最大值用于归一化
        raw_list: List[Dict[str, Any]] = []
        max_change = 1
        max_defect = 1
        max_complexity = 1

        for row in symbols:
            symbol_hash = row["symbol_hash"]
            qualified_name = row["qualified_name"]
            change_info = change_map.get(
                symbol_hash, {"count": 0, "first_seen": 0.0, "last_changed": 0.0}
            )
            change_count = change_info["count"]
            defect_count = defect_map.get(qualified_name, 0)

            # 圈复杂度：优先用 MetricsMixin 的实现，否则用函数行数近似
            complexity = 1
            content = row["content"] or ""
            if callable(complexity_fn) and content:
                lang = ""
                if row["rel_path"]:
                    try:
                        from .config import detect_language_from_path
                        lang = detect_language_from_path(row["rel_path"]) or ""
                    except Exception:
                        lang = ""
                try:
                    complexity = complexity_fn(content, lang)
                except Exception:
                    complexity = 1
            else:
                if row["start_line"] and row["end_line"] and row["end_line"] >= row["start_line"]:
                    complexity = row["end_line"] - row["start_line"] + 1

            raw_list.append({
                "symbol_hash": symbol_hash,
                "qualified_name": qualified_name,
                "module_path": row["module_path"] or "",
                "change_count": change_count,
                "defect_count": defect_count,
                "complexity": complexity,
                "first_seen": change_info["first_seen"],
                "last_changed": change_info["last_changed"],
            })

            max_change = max(max_change, change_count)
            max_defect = max(max_defect, defect_count)
            max_complexity = max(max_complexity, complexity)

        # 第二轮：归一化并计算热点评分
        results: List[Dict[str, Any]] = []
        for raw in raw_list:
            change_freq = raw["change_count"] / max_change if max_change > 0 else 0.0
            defect_density = raw["defect_count"] / max_defect if max_defect > 0 else 0.0
            cyclo_norm = raw["complexity"] / max_complexity if max_complexity > 0 else 0.0
            hotspot_score = change_freq * 0.4 + defect_density * 0.3 + cyclo_norm * 0.3

            # 标注热点类型
            label: Optional[str] = None
            last_changed = raw["last_changed"]
            days_since = (now - last_changed) / 86400 if last_changed > 0 else float("inf")
            if raw["change_count"] > 5 and days_since <= 30:
                label = "持续热点"
            elif 3 <= raw["change_count"] <= 5 and days_since <= 7:
                label = "新兴热点"

            results.append({
                "qualified_name": raw["qualified_name"],
                "symbol_hash": raw["symbol_hash"],
                "module_path": raw["module_path"],
                "hotspot_score": round(hotspot_score, 4),
                "change_count": raw["change_count"],
                "defect_count": raw["defect_count"],
                "complexity": raw["complexity"],
                "first_seen": raw["first_seen"],
                "last_changed": raw["last_changed"],
                "label": label,
            })

        results.sort(key=lambda x: x["hotspot_score"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def function_change_frequency(self, qualified_name: str, time_window: str = "") -> Dict[str, Any]:
        """查询符号的变更频率

        通过 qualified_name 查询 file_symbol_versions 表，关联 file_versions 与
        git_commits，统计变更次数、变更时间分布、变更者列表与变更间隔趋势。

        Args:
            qualified_name: 函数限定名
            time_window: 时间窗口过滤（如 "30d"、"90d"、"1y"），空字符串表示不限

        Returns:
            变更频率统计字典，包含 change_count / first_seen / last_changed /
            changers / timeline / intervals / distribution 等字段
        """
        ws_id = self._get_active_workspace_id()
        cutoff = self._parse_time_window(time_window)

        sql = """
            SELECT fv.id as fv_id, fv.parsed_at, fv.commit_hash,
                   gc.author, gc.message
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            LEFT JOIN git_commits gc ON fv.commit_hash = gc.commit_hash
            WHERE fi.workspace_id = ? AND fsv.qualified_name = ?
        """
        params: list = [ws_id, qualified_name]
        if cutoff > 0:
            sql += " AND fv.parsed_at >= ?"
            params.append(cutoff)
        sql += " ORDER BY fv.parsed_at ASC"

        cur = self.conn.execute(sql, params)
        rows = cur.fetchall()

        # 去重同一 file_version_id（同一版本可能因符号表归档产生多行）
        seen_fv: set = set()
        timeline: List[Dict[str, Any]] = []
        changers: List[str] = []
        timestamps: List[float] = []
        for row in rows:
            fv_id = row["fv_id"]
            if fv_id in seen_fv:
                continue
            seen_fv.add(fv_id)
            ts = row["parsed_at"]
            timestamps.append(ts)
            author = row["author"] or ""
            if author and author not in changers:
                changers.append(author)
            timeline.append({
                "timestamp": ts,
                "commit_hash": row["commit_hash"] or "",
                "author": author,
                "message": row["message"] or "",
            })

        change_count = len(timestamps)
        first_seen = timestamps[0] if timestamps else 0.0
        last_changed = timestamps[-1] if timestamps else 0.0

        # 变更间隔趋势（相邻变更的时间差，单位秒）
        intervals: List[float] = []
        for i in range(1, len(timestamps)):
            intervals.append(max(0.0, timestamps[i] - timestamps[i - 1]))

        return {
            "qualified_name": qualified_name,
            "change_count": change_count,
            "first_seen": first_seen,
            "last_changed": last_changed,
            "changers": changers,
            "timeline": timeline,
            "intervals": intervals,
            "avg_interval": (sum(intervals) / len(intervals)) if intervals else 0.0,
            "distribution": self._compute_change_distribution(timestamps),
        }

    def defect_correlation(self, symbol_hash: str, window_commits: int = 5) -> Dict[str, Any]:
        """关联函数变更与缺陷

        统计符号变更后 window_commits 次提交内引入的缺陷数量。

        Args:
            symbol_hash: 符号内容 hash（即 symbol_contents.content_hash）
            window_commits: 变更后观察的提交窗口数

        Returns:
            缺陷关联统计字典，包含 total_changes / defects_after_change /
            defect_types / findings 字段
        """
        ws_id = self._get_active_workspace_id()

        # 获取符号的 qualified_name（用于匹配 semgrep_findings.symbol_qualified）
        cur = self.conn.execute(
            "SELECT content_hash, qualified_name FROM symbol_contents WHERE content_hash = ?",
            (symbol_hash,),
        )
        sym_row = cur.fetchone()
        qualified_name = sym_row["qualified_name"] if sym_row else ""

        # 找到该符号出现过的所有 file_version（变更点），按 file_instance 分组
        cur = self.conn.execute(
            """
            SELECT fv.id as fv_id, fv.file_instance_id, fv.version_num,
                   fv.content_hash, fv.parsed_at
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fsv.symbol_hash = ?
            ORDER BY fv.file_instance_id, fv.version_num ASC
            """,
            (ws_id, symbol_hash),
        )
        changes_by_file: Dict[int, List[Any]] = defaultdict(list)
        for row in cur:
            changes_by_file[row["file_instance_id"]].append(row)

        total_changes = sum(len(v) for v in changes_by_file.values())

        # 对每个变更点，查找后续 window_commits 个版本内的 semgrep_findings
        defect_findings: List[Dict[str, Any]] = []
        defect_types: Dict[str, int] = defaultdict(int)
        seen_finding_ids: set = set()

        for file_instance_id, change_versions in changes_by_file.items():
            # 获取该 file_instance 所有版本（按版本号排序），用于定位窗口
            cur = self.conn.execute(
                """
                SELECT id, version_num, content_hash, parsed_at
                FROM file_versions
                WHERE file_instance_id = ?
                ORDER BY version_num ASC
                """,
                (file_instance_id,),
            )
            all_versions = cur.fetchall()
            version_num_to_index = {
                v["version_num"]: idx for idx, v in enumerate(all_versions)
            }

            for change in change_versions:
                idx = version_num_to_index.get(change["version_num"])
                if idx is None:
                    continue
                # 窗口：从当前变更版本的下一个版本开始，取 window_commits 个版本
                window_versions = all_versions[idx + 1: idx + 1 + window_commits]
                window_hashes = [
                    v["content_hash"] for v in window_versions if v["content_hash"]
                ]
                if not window_hashes:
                    continue
                placeholders = ",".join("?" * len(window_hashes))
                cur = self.conn.execute(
                    f"""
                    SELECT id, rule_id, rule_name, severity, start_line, end_line,
                           content_hash, scanned_at, symbol_qualified, message
                    FROM semgrep_findings
                    WHERE file_instance_id = ?
                      AND content_hash IN ({placeholders})
                    """,
                    [file_instance_id] + window_hashes,
                )
                for frow in cur:
                    fid = frow["id"]
                    if fid in seen_finding_ids:
                        continue
                    seen_finding_ids.add(fid)
                    defect_findings.append({
                        "rule_id": frow["rule_id"],
                        "rule_name": frow["rule_name"],
                        "severity": frow["severity"],
                        "start_line": frow["start_line"],
                        "end_line": frow["end_line"],
                        "scanned_at": frow["scanned_at"],
                        "message": frow["message"],
                        "after_change_at": change["parsed_at"],
                    })
                    defect_types[frow["rule_id"]] += 1

        # 补充：通过 symbol_qualified 直接关联的缺陷（不局限于窗口）
        if qualified_name:
            cur = self.conn.execute(
                """
                SELECT id, rule_id, rule_name, severity, start_line, end_line,
                       content_hash, scanned_at, symbol_qualified, message
                FROM semgrep_findings
                WHERE symbol_qualified = ?
                """,
                (qualified_name,),
            )
            for frow in cur:
                fid = frow["id"]
                if fid in seen_finding_ids:
                    continue
                seen_finding_ids.add(fid)
                defect_findings.append({
                    "rule_id": frow["rule_id"],
                    "rule_name": frow["rule_name"],
                    "severity": frow["severity"],
                    "start_line": frow["start_line"],
                    "end_line": frow["end_line"],
                    "scanned_at": frow["scanned_at"],
                    "message": frow["message"],
                    "after_change_at": 0.0,
                })
                defect_types[frow["rule_id"]] += 1

        return {
            "symbol_hash": symbol_hash,
            "total_changes": total_changes,
            "defects_after_change": len(defect_findings),
            "defect_types": dict(defect_types),
            "findings": defect_findings,
        }

    def get_defect_correlation_by_qn(self, qualified_name: str, window_commits: int = 5) -> Dict[str, Any]:
        """按限定名查询符号的变更-缺陷关联（defect_correlation 的便捷封装）

        Args:
            qualified_name: 符号限定名
            window_commits: 变更后观察的提交窗口数

        Returns:
            包含 qualified_name / change_count / defect_count / defect_rate / recent_defects 的字典
        """
        ws_id = self._get_active_workspace_id()

        # 查符号的 symbol_hash
        cur = self.conn.execute(
            """SELECT fsv.symbol_hash FROM file_symbol_versions fsv
               JOIN file_versions fv ON fsv.file_version_id = fv.id
               JOIN file_instances fi ON fv.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND fv.is_current = 1 AND fsv.is_deleted = 0
                 AND fsv.qualified_name = ?
               LIMIT 1""",
            (ws_id, qualified_name),
        )
        row = cur.fetchone()
        if not row:
            return {
                "qualified_name": qualified_name,
                "change_count": 0,
                "defect_count": 0,
                "defect_rate": 0.0,
                "recent_defects": [],
            }

        symbol_hash = row["symbol_hash"]
        result = self.defect_correlation(symbol_hash, window_commits=window_commits)

        # 取最近 3 条缺陷
        findings = result.get("findings", [])
        recent_defects = [
            {
                "rule_id": f.get("rule_id", ""),
                "severity": f.get("severity", ""),
                "message": f.get("message", "")[:100],
                "start_line": f.get("start_line", 0),
            }
            for f in findings[:3]
        ]

        change_count = result.get("total_changes", 0)
        defect_count = result.get("defects_after_change", 0)
        defect_rate = (defect_count / change_count) if change_count > 0 else 0.0

        return {
            "qualified_name": qualified_name,
            "change_count": change_count,
            "defect_count": defect_count,
            "defect_rate": round(defect_rate, 3),
            "defect_types": result.get("defect_types", {}),
            "recent_defects": recent_defects,
        }

    def hotspot_evolution(self, module_filter: str = "") -> List[Dict[str, Any]]:
        """热点函数演化排名

        对每个函数符号计算 hotspot_score = change_frequency * 0.4
            + defect_density * 0.3 + cyclomatic_complexity * 0.3（均归一化到 0-1），
        标注 "持续热点" / "新兴热点"，按分数降序排列。

        - 持续热点：change_count > 5 且 last_changed 在 30 天内
        - 新兴热点：change_count 3-5 且 last_changed 在 7 天内

        Args:
            module_filter: 模块路径前缀过滤

        Returns:
            热点函数列表，按 hotspot_score 降序排列
        """
        return self._compute_hotspot_scores(module_filter)

    def churn_analysis(self, module_filter: str = "", time_window: str = "90d") -> Dict[str, Any]:
        """代码流失（churn）分析

        统计时间窗口内的变更行数、变更文件数、流失率、高频变更文件与流失趋势。
        变更行数通过相邻文件版本 total_lines 差值近似。

        Args:
            module_filter: 模块路径前缀过滤
            time_window: 时间窗口（默认 "90d"）

        Returns:
            流失分析字典，包含 churn_rate / total_churned_lines / changed_files /
            top_churned_files / trend 字段
        """
        ws_id = self._get_active_workspace_id()
        cutoff = self._parse_time_window(time_window)

        sql = """
            SELECT fv.id, fv.file_instance_id, fv.version_num, fv.total_lines,
                   fv.parsed_at, fi.rel_path, fi.module_path
            FROM file_versions fv
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
        """
        params: list = [ws_id]
        if module_filter:
            sql += " AND fi.module_path LIKE ?"
            params.append(f"{module_filter}%")
        if cutoff > 0:
            sql += " AND fv.parsed_at >= ?"
            params.append(cutoff)
        sql += " ORDER BY fv.file_instance_id, fv.version_num ASC"

        cur = self.conn.execute(sql, params)
        versions = cur.fetchall()

        # 按 file_instance 分组，计算相邻版本行数差
        by_file: Dict[int, List[Any]] = defaultdict(list)
        for row in versions:
            by_file[row["file_instance_id"]].append(row)

        total_churned_lines = 0
        changed_files = 0
        total_lines_current = 0
        file_churn_records: List[Dict[str, Any]] = []
        trend_buckets: Dict[str, int] = defaultdict(int)

        for file_instance_id, fversions in by_file.items():
            if not fversions:
                continue
            changed_files += 1
            # 当前文件行数取该文件最新版本
            total_lines_current += fversions[-1]["total_lines"] or 0

            if len(fversions) < 2:
                # 只有一个版本，无法计算流失行数
                file_churn_records.append({
                    "file_instance_id": file_instance_id,
                    "rel_path": fversions[0]["rel_path"],
                    "change_count": 1,
                    "churned_lines": 0,
                })
                continue

            file_churn = 0
            change_count = 1  # 至少有 1 次变更（首个版本）
            for i in range(1, len(fversions)):
                prev_lines = fversions[i - 1]["total_lines"] or 0
                curr_lines = fversions[i]["total_lines"] or 0
                diff = abs(curr_lines - prev_lines)
                file_churn += diff
                change_count += 1
                if diff > 0:
                    # 趋势分桶（按天）
                    bucket_key = time.strftime(
                        "%Y-%m-%d", time.localtime(fversions[i]["parsed_at"])
                    )
                    trend_buckets[bucket_key] += diff

            total_churned_lines += file_churn
            file_churn_records.append({
                "file_instance_id": file_instance_id,
                "rel_path": fversions[0]["rel_path"],
                "change_count": change_count,
                "churned_lines": file_churn,
            })

        churn_rate = (
            total_churned_lines / total_lines_current
        ) if total_lines_current > 0 else 0.0

        # 高频变更文件 Top 10（按变更次数降序）
        top_files = sorted(
            file_churn_records,
            key=lambda x: x["change_count"],
            reverse=True,
        )[:10]

        # 流失趋势（按时间排序）
        trend = [
            {"date": k, "churned_lines": v}
            for k, v in sorted(trend_buckets.items())
        ]

        return {
            "churn_rate": round(churn_rate, 4),
            "total_churned_lines": total_churned_lines,
            "changed_files": changed_files,
            "total_lines_current": total_lines_current,
            "top_churned_files": top_files,
            "trend": trend,
        }

    def refresh_evolution_metrics(self) -> int:
        """批量刷新演化指标缓存

        查询所有函数符号，计算 change_count / defect_count / hotspot_score，
        写入 evolution_metrics 表（INSERT OR REPLACE）。

        Returns:
            刷新的符号数
        """
        results = self._compute_hotspot_scores(module_filter="")
        now = time.time()

        for item in results:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO evolution_metrics
                    (symbol_hash, change_count, defect_count, hotspot_score,
                     first_seen, last_changed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["symbol_hash"],
                    item["change_count"],
                    item["defect_count"],
                    item["hotspot_score"],
                    item["first_seen"],
                    item["last_changed"],
                    now,
                ),
            )
        self.conn.commit()
        return len(results)
