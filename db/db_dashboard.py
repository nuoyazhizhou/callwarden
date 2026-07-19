"""
db_dashboard.py
===============

项目综合状态报表 Mixin 类。

聚合现有各 Mixin 的查询方法（get_stats / get_code_metrics_summary /
bootstrap_status / get_complexity_hotspots 等），输出一份项目级驾驶舱数据，
供 `cw dashboard` CLI 命令和 MCP `get_project_dashboard` 工具使用。

设计原则：
- 不重写 SQL，全部复用现有 Mixin 方法
- 昂贵操作（detect_cycles / hotspot_evolution / churn_analysis）走可选开关
- 异常隔离：某个 section 失败不影响其他 section（返回 error 字段）
- 纯只读：不触发任何写操作，WAL 模式下与 MCP Server 并发安全
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional


class DashboardMixin:
    """项目综合状态报表 Mixin

    通过 self.conn 访问数据库，组合各 Mixin 方法输出项目级驾驶舱数据。
    """

    def get_project_dashboard(
        self,
        *,
        with_cycles: bool = False,
        with_evolution: bool = False,
        quick: bool = True,
        top_n: int = 5,
    ) -> Dict[str, Any]:
        """获取项目综合状态驾驶舱

        聚合 7 个维度的项目状态：

        1. overview: workspace、git_head、last_build、db_size、db_stale
        2. code_scale: total_files、total_lines、total_symbols、function_count、by_kind、by_language
        3. code_quality: avg/max_complexity、complexity_distribution、comment_coverage、
           largest_fns_topN、complexity_hotspots_topN
        4. call_graph: total_calls、resolved_rate、cross_file_calls、cycles_count（可选）、
           orphans_count、depth_distribution
        5. task_risk: task_counts、open_findings、blocking_findings、recommended_action
        6. audit: audit_broken、active_rules、pending_candidates、latest_scan_run
        7. evolution: recent_commits、churn_30d、hotspot_topN（可选，需 git history）

        Args:
            with_cycles: 是否计算循环调用数（detect_cycles，可能慢，默认 False）
            with_evolution: 是否计算演化趋势（hotspot_evolution + churn_analysis，需 git history）
            quick: 快速模式（默认 True），跳过昂贵的 Python 圈复杂度计算
                   （get_code_metrics_summary 的 complexity_distribution + get_complexity_hotspots
                   需要为每个函数调 _compute_cyclomatic_complexity，20K 函数 ~1.8s）
                   quick=False 才计算 avg/max_complexity 和 complexity_hotspots_top
            top_n: top 风险点列表的长度（默认 5）

        Returns:
            包含上述 7 个 section 的 dict，每个 section 失败时返回 {"error": str}
        """
        result: Dict[str, Any] = {}
        ws_id = self._get_active_workspace_id()

        # 缓存 get_stats（被 code_scale 和 call_graph 共用，避免双调用 ~65ms）
        # 失败时降级为 None，由各 section 内部 try-except 自行处理或再次调用
        try:
            cached_stats = self.get_stats()
        except Exception:
            cached_stats = None

        # 1. overview
        result["overview"] = self._dashboard_overview(ws_id)

        # 2. code_scale
        result["code_scale"] = self._dashboard_code_scale(ws_id, cached_stats=cached_stats)

        # 3. code_quality
        result["code_quality"] = self._dashboard_code_quality(ws_id, top_n, quick=quick)

        # 4. call_graph
        result["call_graph"] = self._dashboard_call_graph(
            ws_id, with_cycles, top_n, cached_stats=cached_stats
        )

        # 5. task_risk
        result["task_risk"] = self._dashboard_task_risk()

        # 6. audit
        result["audit"] = self._dashboard_audit()

        # 7. evolution
        if with_evolution:
            result["evolution"] = self._dashboard_evolution(top_n)
        else:
            result["evolution"] = None

        return result

    # ------------------------------------------------------------------
    # 各 section 实现
    # ------------------------------------------------------------------

    def _dashboard_overview(self, ws_id: str) -> Dict[str, Any]:
        """概览：workspace、git_head、last_build、db_size、db_stale"""
        try:
            ws = self.get_active_workspace() if hasattr(self, "get_active_workspace") else None
            ws_name = ws["name"] if ws else "unknown"
            root_path = ws["root_path"] if ws else self.workspace_root

            # git_head
            git_head = ""
            is_git = False
            try:
                if hasattr(self, "_is_git_repo") and self._is_git_repo():
                    is_git = True
                    git_head = self._get_git_head() if hasattr(self, "_get_git_head") else ""
            except Exception:
                pass

            # last_build（最近一次 scan_run 或 last_parsed）
            last_build = 0
            try:
                cur = self.conn.execute(
                    "SELECT MAX(last_parsed) as m FROM file_instances "
                    "WHERE workspace_id = ? AND last_parsed > 0",
                    (ws_id,),
                )
                row = cur.fetchone()
                if row and row["m"]:
                    last_build = row["m"]
            except Exception:
                pass

            # db_size
            db_size = 0
            try:
                db_path = getattr(self, "db_path", None)
                if db_path and os.path.exists(db_path):
                    db_size = os.path.getsize(db_path)
            except Exception:
                pass

            # db_stale（对比最近 scan_run 的 git_head）
            db_stale = False
            latest_scan = None
            if hasattr(self, "get_latest_scan_run"):
                try:
                    latest_scan = self.get_latest_scan_run()
                    if latest_scan and is_git and git_head:
                        scan_head = latest_scan.get("git_head", "") if isinstance(latest_scan, dict) else ""
                        db_stale = bool(scan_head) and scan_head != git_head
                except Exception:
                    pass

            return {
                "workspace_name": ws_name,
                "root_path": root_path,
                "is_git_repo": is_git,
                "git_head": git_head[:12] if git_head else "",
                "last_build_ts": last_build,
                "db_size_bytes": db_size,
                "db_stale": db_stale,
                "latest_scan_run": latest_scan,
            }
        except Exception as e:
            return {"error": str(e)}

    def _dashboard_code_scale(
        self, ws_id: str, cached_stats: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """代码规模：文件、行数、符号、按 kind/language 分布"""
        try:
            stats = cached_stats if cached_stats is not None else self.get_stats()
            # 按 language 分布（从 file_instances rel_path 后缀聚合）
            by_language: Dict[str, int] = {}
            try:
                cur = self.conn.execute(
                    "SELECT substr(rel_path, instr(rel_path, '.') + 1) as ext, COUNT(*) as cnt "
                    "FROM file_instances "
                    "WHERE workspace_id = ? AND status != 'archived' AND rel_path LIKE '%.%' "
                    "GROUP BY ext ORDER BY cnt DESC",
                    (ws_id,),
                )
                for row in cur:
                    ext = row["ext"].rsplit(".", 1)[-1] if "." in row["ext"] else row["ext"]
                    by_language[ext] = by_language.get(ext, 0) + row["cnt"]
            except Exception:
                pass

            # total_lines
            total_lines = 0
            try:
                cur = self.conn.execute(
                    "SELECT SUM(total_lines) as total FROM file_instances "
                    "WHERE workspace_id = ? AND status != 'archived'",
                    (ws_id,),
                )
                row = cur.fetchone()
                if row and row["total"]:
                    total_lines = row["total"]
            except Exception:
                pass

            return {
                "total_files": stats.get("total_files", 0),
                "total_lines": total_lines,
                "total_symbols": stats.get("total_symbols", 0),
                "total_function_versions": stats.get("total_file_symbol_links", 0),
                "by_kind": stats.get("by_kind", {}),
                "by_language": by_language,
                "commented_symbols": stats.get("commented", 0),
            }
        except Exception as e:
            return {"error": str(e)}

    def _dashboard_code_quality(self, ws_id: str, top_n: int, quick: bool = True) -> Dict[str, Any]:
        """代码质量：复杂度、注释覆盖、top 风险函数

        Args:
            ws_id: workspace id
            top_n: top 列表长度
            quick: True 跳过昂贵的 Python 圈复杂度计算（默认 True）
        """
        try:
            # 未注释函数数（SQL，快）
            uncommented_fns = 0
            try:
                cur = self.conn.execute(
                    """SELECT COUNT(*) as c FROM symbols s
                       WHERE s.has_comment = 0 AND s.kind IN ('fn','test_fn','method')
                         AND s.file_instance_id IN (
                             SELECT id FROM file_instances
                             WHERE workspace_id = ? AND status != 'archived'
                         )""",
                    (ws_id,),
                )
                uncommented_fns = cur.fetchone()["c"]
            except Exception:
                pass

            # 注释覆盖率（SQL，快）
            comment_coverage_pct = 0.0
            try:
                cur = self.conn.execute(
                    """SELECT
                           COUNT(*) as total,
                           SUM(CASE WHEN s.has_comment = 1 THEN 1 ELSE 0 END) as commented
                       FROM symbols s
                       WHERE s.kind IN ('fn','function','method')
                         AND s.file_instance_id IN (
                             SELECT id FROM file_instances
                             WHERE workspace_id = ? AND status != 'archived'
                         )""",
                    (ws_id,),
                )
                row = cur.fetchone()
                total = row["total"] or 0
                commented = row["commented"] or 0
                if total > 0:
                    comment_coverage_pct = round(commented / total * 100, 1)
            except Exception:
                pass

            # top 大函数（纯 SQL ORDER BY end_line-start_line DESC LIMIT N，
            # 不走 get_largest_functions 因为它拉全表到 Python 排序，100K 上 ~300ms）
            largest_fns: List[Dict] = []
            try:
                cur = self.conn.execute(
                    """SELECT s.qualified_name, s.kind, s.start_line, s.end_line,
                              s.module_path, s.depth, fi.rel_path
                       FROM symbols s
                       JOIN file_instances fi ON s.file_instance_id = fi.id
                       WHERE fi.workspace_id = ? AND s.kind IN ('fn', 'function', 'method')
                         AND s.start_line > 0 AND s.end_line > 0
                       ORDER BY (s.end_line - s.start_line) DESC
                       LIMIT ?""",
                    (ws_id, top_n),
                )
                for row in cur:
                    line_count = row["end_line"] - row["start_line"] + 1
                    largest_fns.append({
                        "qualified_name": row["qualified_name"],
                        "file_path": row["rel_path"],
                        "start_line": row["start_line"],
                        "line_count": line_count,
                        "depth": row["depth"] if row["depth"] >= 0 else 0,
                        "module_path": row["module_path"],
                    })
            except Exception:
                pass

            if quick:
                # 快速模式：跳过 Python 圈复杂度计算
                return {
                    "avg_complexity": None,
                    "max_complexity": None,
                    "complexity_distribution": None,
                    "comment_coverage_pct": comment_coverage_pct,
                    "uncommented_fns": uncommented_fns,
                    "complexity_hotspots_top": [],
                    "largest_fns_top": largest_fns,
                    "quick_mode": True,
                }

            # 完整模式：算 avg/max/distribution + hotspots（慢，每函数一次 Python 调用）
            summary = self.get_code_metrics_summary()

            complexity_hotspots: List[Dict] = []
            try:
                if hasattr(self, "get_complexity_hotspots"):
                    complexity_hotspots = self.get_complexity_hotspots(limit=top_n)
            except Exception:
                pass

            return {
                "avg_complexity": summary.get("avg_complexity", 0),
                "max_complexity": summary.get("max_complexity", 0),
                "complexity_distribution": summary.get("complexity_distribution", {}),
                "comment_coverage_pct": summary.get("comment_coverage", 0),
                "uncommented_fns": uncommented_fns,
                "complexity_hotspots_top": complexity_hotspots,
                "largest_fns_top": largest_fns,
                "quick_mode": False,
            }
        except Exception as e:
            return {"error": str(e)}

    def _dashboard_call_graph(
        self,
        ws_id: str,
        with_cycles: bool,
        top_n: int,
        cached_stats: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """调用图：调用数、resolved rate、cycles、orphans、depth 分布"""
        try:
            stats = cached_stats if cached_stats is not None else self.get_stats()
            total_calls = stats.get("total_calls", 0)
            resolved_calls = stats.get("resolved_calls", 0)
            cross_file = stats.get("cross_file_calls", 0)
            resolve_rate = (resolved_calls / total_calls * 100) if total_calls > 0 else 0

            # cycles（可选，可能慢）
            cycles_count: Optional[int] = None
            if with_cycles:
                try:
                    if hasattr(self, "detect_cycles"):
                        cycles = self.detect_cycles()
                        cycles_count = len(cycles) if cycles else 0
                except Exception:
                    cycles_count = -1

            # orphans count（用 COUNT SQL，不拉 1万行）
            orphans_count = 0
            try:
                cur = self.conn.execute(
                    """SELECT COUNT(*) as c FROM file_symbol_versions fsv
                       JOIN file_versions fv ON fsv.file_version_id = fv.id
                       JOIN file_instances fi ON fv.file_instance_id = fi.id
                       JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
                       WHERE fi.workspace_id = ? AND fv.is_current = 1
                         AND sc.kind = 'fn'
                         AND fsv.qualified_name NOT IN (
                             SELECT DISTINCT cv.callee_qualified
                             FROM call_versions cv
                             JOIN file_versions fv2 ON cv.file_version_id = fv2.id
                             JOIN file_instances fi2 ON fv2.file_instance_id = fi2.id
                             WHERE fi2.workspace_id = ? AND fv2.is_current = 1
                               AND cv.callee_qualified != '' AND cv.caller_qualified != ''
                         )""",
                    (ws_id, ws_id),
                )
                orphans_count = cur.fetchone()["c"]
            except Exception:
                pass

            return {
                "total_calls": total_calls,
                "resolved_calls": resolved_calls,
                "resolve_rate_pct": round(resolve_rate, 1),
                "cross_file_calls": cross_file,
                "cycles_count": cycles_count,
                "orphans_count": orphans_count,
                "depth_distribution": stats.get("depth_distribution", {}),
            }
        except Exception as e:
            return {"error": str(e)}

    def _dashboard_task_risk(self) -> Dict[str, Any]:
        """任务与风险：任务状态分布、阻塞 findings、推荐下一步"""
        try:
            if not hasattr(self, "bootstrap_status"):
                return {"error": "bootstrap_status not available"}
            bootstrap = self.bootstrap_status()
            return {
                "task_counts": bootstrap.get("tasks", {}),
                "open_findings_count": bootstrap.get("open_findings_count", 0),
                "blocking_findings_count": bootstrap.get("blocking_findings_count", 0),
                "pending_rule_candidates": bootstrap.get("pending_candidates_count", 0),
                "recommended_action": bootstrap.get("recommended_next_action", ""),
            }
        except Exception as e:
            return {"error": str(e)}

    def _dashboard_audit(self) -> Dict[str, Any]:
        """审计：audit_chain 完整性、active rules、latest scan_run"""
        try:
            bootstrap = self.bootstrap_status() if hasattr(self, "bootstrap_status") else {}
            audit_verify = bootstrap.get("audit_verify", {})
            return {
                "active_rules_count": bootstrap.get("active_rules_count", 0),
                "audit_broken_count": audit_verify.get("broken_count", 0) if isinstance(audit_verify, dict) else 0,
                "audit_verified_count": audit_verify.get("verified_count", 0) if isinstance(audit_verify, dict) else 0,
                "latest_scan_run": bootstrap.get("latest_scan_run"),
            }
        except Exception as e:
            return {"error": str(e)}

    def _dashboard_evolution(self, top_n: int) -> Dict[str, Any]:
        """演化趋势：最近 commits、churn、hotspot 排名（需 git history）"""
        try:
            recent_commits: List[Dict] = []
            if hasattr(self, "get_git_commits"):
                try:
                    recent_commits = self.get_git_commits(limit=top_n)
                except Exception:
                    pass

            churn: Optional[Dict] = None
            if hasattr(self, "churn_analysis"):
                try:
                    churn = self.churn_analysis(time_window="30d")
                except Exception:
                    pass

            hotspot_top: List[Dict] = []
            if hasattr(self, "hotspot_evolution"):
                try:
                    all_hotspots = self.hotspot_evolution()
                    hotspot_top = all_hotspots[:top_n] if all_hotspots else []
                except Exception:
                    pass

            git_stats: Optional[Dict] = None
            if hasattr(self, "get_git_stats"):
                try:
                    git_stats = self.get_git_stats()
                except Exception:
                    pass

            return {
                "recent_commits": recent_commits,
                "churn_30d": churn,
                "hotspot_top": hotspot_top,
                "git_stats": git_stats,
            }
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 风险预警聚合
    # ------------------------------------------------------------------

    def get_project_risks(self, top_n: int = 5, quick: bool = True) -> List[Dict[str, Any]]:
        """获取项目风险预警列表

        扫描 5 类风险：
        - high_complexity: 圈复杂度 > 20 的函数（quick=True 时跳过，需 Python 计算）
        - oversized_function: 行数 > 500 的函数
        - blocking_findings: 严重度为 block 的 findings
        - broken_audit: audit_chain 损坏记录
        - db_stale: DB 滞后于 git HEAD

        Args:
            top_n: 每类风险最多列出多少条
            quick: True 跳过高复杂度检查（避免 Python 圈复杂度计算 ~3s），
                   False 才计算 high_complexity 风险

        Returns:
            风险列表，每条含 type/severity/detail/symbol/qualified_name 等字段
        """
        risks: List[Dict[str, Any]] = []
        ws_id = self._get_active_workspace_id()

        # 1. 高复杂度函数（complexity > 20）—— Python 算复杂度，quick 模式跳过
        if not quick:
            try:
                if hasattr(self, "get_complexity_hotspots"):
                    hotspots = self.get_complexity_hotspots(limit=top_n * 3)
                    for h in hotspots:
                        cx = h.get("complexity", 0) or h.get("cyclomatic_complexity", 0)
                        if cx and cx > 20:
                            risks.append({
                                "type": "high_complexity",
                                "severity": "high" if cx > 30 else "medium",
                                "qualified_name": h.get("qualified_name", ""),
                                "file_path": h.get("file_path", ""),
                                "detail": f"圈复杂度 {cx}（建议 < 20）",
                            })
            except Exception:
                pass

        # 2. 超大函数（行数 > 500）—— 纯 SQL ORDER BY，无需 Python 算复杂度
        try:
            cur = self.conn.execute(
                """SELECT s.qualified_name, s.start_line, s.end_line, fi.rel_path
                   FROM symbols s
                   JOIN file_instances fi ON s.file_instance_id = fi.id
                   WHERE fi.workspace_id = ? AND s.kind IN ('fn','function','method')
                     AND s.start_line > 0 AND s.end_line > 0
                     AND (s.end_line - s.start_line + 1) > 500
                   ORDER BY (s.end_line - s.start_line) DESC
                   LIMIT ?""",
                (ws_id, top_n * 3),
            )
            for row in cur:
                lines = row["end_line"] - row["start_line"] + 1
                risks.append({
                    "type": "oversized_function",
                    "severity": "high" if lines > 1000 else "medium",
                    "qualified_name": row["qualified_name"],
                    "file_path": row["rel_path"],
                    "detail": f"函数 {lines} 行（建议 < 500）",
                })
        except Exception:
            pass

        # 3. 阻塞 findings
        try:
            cur = self.conn.execute(
                "SELECT task_id, step_id, severity, message FROM task_quality_findings "
                "WHERE status = 'open' AND severity = 'block' LIMIT ?",
                (top_n,),
            )
            for row in cur:
                risks.append({
                    "type": "blocking_finding",
                    "severity": "high",
                    "task_id": row["task_id"],
                    "step_id": row["step_id"],
                    "detail": row["message"] or "阻塞 findings 未解决",
                })
        except Exception:
            pass

        # 4. audit_chain 损坏
        try:
            if hasattr(self, "verify_audit_chain"):
                audit = self.verify_audit_chain(table_name="", limit=500)
                broken = audit.get("broken_count", 0) if isinstance(audit, dict) else 0
                if broken > 0:
                    risks.append({
                        "type": "broken_audit",
                        "severity": "high",
                        "detail": f"审计链有 {broken} 条损坏记录（cw audit verify 查看）",
                    })
        except Exception:
            pass

        # 5. db_stale
        try:
            if hasattr(self, "get_latest_scan_run") and hasattr(self, "_is_git_repo") and self._is_git_repo():
                latest = self.get_latest_scan_run()
                if latest and hasattr(self, "_get_git_head"):
                    scan_head = latest.get("git_head", "") if isinstance(latest, dict) else ""
                    current_head = self._get_git_head()
                    if scan_head and current_head and scan_head != current_head:
                        risks.append({
                            "type": "db_stale",
                            "severity": "medium",
                            "detail": f"DB 滞后于 git HEAD（{scan_head[:8]} → {current_head[:8]}），建议 cw --refresh-all",
                        })
        except Exception:
            pass

        # 按严重度排序：high > medium > low
        severity_order = {"high": 0, "medium": 1, "low": 2}
        risks.sort(key=lambda r: severity_order.get(r.get("severity", "low"), 3))
        return risks[:top_n * 3]
