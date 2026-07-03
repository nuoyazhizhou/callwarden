"""
db_token_savings.py
===================

Token 节省账本 Mixin。

记录每次 Agent 操作（RAG / 调用链 / 摘要 / 注释恢复）节省的 token 数，
用于宣传利器（"已为你节省 N tokens"）和优化依据（哪些操作节省最多）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional


class TokenSavingsMixin:
    """Token 节省账本 Mixin

    提供以下能力：
    - record_token_savings: 记录一次操作的 token 节省
    - get_token_savings_report: 获取节省报告（按操作类型/时间窗口汇总）
    - get_total_savings: 获取累计节省 token 数
    - estimate_and_record: 估算原始 token 数并自动记录

    依赖 token_savings_ledger 表（Schema v11）。
    """

    def record_token_savings(
        self,
        operation: str,
        original_tokens: int,
        actual_tokens: int,
        agent_task_id: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """记录一次操作的 token 节省

        Args:
            operation: 操作类型（rag_context / call_chain_summary / semantic_search / comment_restore / blast_radius）
            original_tokens: 原始 token 数（无压缩时的估算值）
            actual_tokens: 实际使用的 token 数
            agent_task_id: 关联的任务 ID（可选）
            detail: 详情字典（如涉及的符号数、文件数等）

        Returns:
            {"id": int, "tokens_saved": int, "savings_pct": float}
        """
        tokens_saved = max(0, original_tokens - actual_tokens)
        savings_pct = (tokens_saved / original_tokens * 100.0) if original_tokens > 0 else 0.0

        ws_id = None
        if hasattr(self, "_get_active_workspace_id"):
            try:
                ws_id = self._get_active_workspace_id()
            except Exception:
                ws_id = None

        now = time.time()
        cur = self.conn.execute(
            """
            INSERT INTO token_savings_ledger
                (operation, workspace_id, agent_task_id, original_tokens,
                 actual_tokens, tokens_saved, savings_pct, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation,
                ws_id,
                agent_task_id,
                original_tokens,
                actual_tokens,
                tokens_saved,
                savings_pct,
                json.dumps(detail, ensure_ascii=False) if detail else "",
                now,
            ),
        )
        self.conn.commit()

        return {
            "id": cur.lastrowid,
            "tokens_saved": tokens_saved,
            "savings_pct": round(savings_pct, 2),
        }

    def estimate_and_record(
        self,
        operation: str,
        actual_content: str,
        full_content: str = "",
        agent_task_id: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """估算原始 token 数并自动记录

        当只知道实际输出的文本内容时，用字符数/4 估算 actual_tokens，
        用 full_content（如果提供）估算 original_tokens；否则用操作类型的默认压缩比估算。

        Args:
            operation: 操作类型
            actual_content: 实际输出内容（用于估算 actual_tokens）
            full_content: 原始完整内容（用于估算 original_tokens，为空则按默认压缩比）
            agent_task_id: 关联的任务 ID
            detail: 详情字典

        Returns:
            {"id": int, "tokens_saved": int, "savings_pct": float, "original_tokens": int, "actual_tokens": int}
        """
        actual_tokens = len(actual_content) // 4

        if full_content:
            original_tokens = len(full_content) // 4
        else:
            # 按操作类型的默认压缩比估算原始 token 数
            # RAG 上下文通常压缩 5-10 倍，调用链摘要压缩 3-5 倍
            default_ratios = {
                "rag_context": 8,
                "call_chain_summary": 4,
                "semantic_search": 3,
                "comment_restore": 2,
                "blast_radius": 5,
                "review_readiness": 6,
            }
            ratio = default_ratios.get(operation, 3)
            original_tokens = actual_tokens * ratio

        result = self.record_token_savings(
            operation=operation,
            original_tokens=original_tokens,
            actual_tokens=actual_tokens,
            agent_task_id=agent_task_id,
            detail=detail,
        )
        result["original_tokens"] = original_tokens
        result["actual_tokens"] = actual_tokens
        return result

    def get_total_savings(
        self, time_window: str = "", operation_filter: str = ""
    ) -> Dict[str, Any]:
        """获取累计节省 token 数

        Args:
            time_window: 时间窗口（"7d" / "30d" / "90d" / "" 全部）
            operation_filter: 操作类型过滤

        Returns:
            {
                "total_saved": int,        # 累计节省 token 数
                "total_operations": int,   # 操作总数
                "avg_savings_pct": float,  # 平均节省百分比
                "by_operation": {...},     # 按操作类型分组
            }
        """
        params: list = []
        where_clauses: list = []

        if operation_filter:
            where_clauses.append("operation = ?")
            params.append(operation_filter)

        if time_window:
            seconds = self._parse_time_window_seconds(time_window)
            if seconds > 0:
                where_clauses.append("created_at >= ?")
                params.append(time.time() - seconds)

        where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        # 总计
        cur = self.conn.execute(
            f"""
            SELECT
                COALESCE(SUM(tokens_saved), 0) as total_saved,
                COUNT(*) as total_operations,
                COALESCE(AVG(savings_pct), 0) as avg_savings_pct
            FROM token_savings_ledger{where_sql}
            """,
            params,
        )
        row = cur.fetchone()

        # 按操作类型分组
        cur2 = self.conn.execute(
            f"""
            SELECT
                operation,
                COALESCE(SUM(tokens_saved), 0) as total_saved,
                COUNT(*) as op_count,
                COALESCE(AVG(savings_pct), 0) as avg_pct
            FROM token_savings_ledger{where_sql}
            GROUP BY operation
            ORDER BY total_saved DESC
            """ + (f" AND operation = ?" if operation_filter and not where_clauses else ""),
            params if not operation_filter else (params + [operation_filter]) if operation_filter and not where_clauses else params,
        )
        # 简化分组查询（避免 WHERE 子句重复）
        group_params = [p for p in params]
        cur2 = self.conn.execute(
            f"""
            SELECT
                operation,
                COALESCE(SUM(tokens_saved), 0) as total_saved,
                COUNT(*) as op_count,
                COALESCE(AVG(savings_pct), 0) as avg_pct
            FROM token_savings_ledger{where_sql}
            GROUP BY operation
            ORDER BY total_saved DESC
            """,
            params,
        )

        by_operation: Dict[str, Any] = {}
        for r in cur2:
            by_operation[r["operation"]] = {
                "total_saved": r["total_saved"],
                "op_count": r["op_count"],
                "avg_savings_pct": round(r["avg_pct"], 2),
            }

        return {
            "total_saved": row["total_saved"] if row else 0,
            "total_operations": row["total_operations"] if row else 0,
            "avg_savings_pct": round(row["avg_savings_pct"], 2) if row else 0.0,
            "by_operation": by_operation,
        }

    def get_token_savings_report(
        self, time_window: str = "30d"
    ) -> Dict[str, Any]:
        """获取 Token 节省报告（宣传利器 + 优化依据）

        Args:
            time_window: 时间窗口（默认 30d）

        Returns:
            {
                "time_window": str,
                "total_saved": int,
                "total_operations": int,
                "avg_savings_pct": float,
                "by_operation": {...},
                "daily_trend": [...],      # 每日节省趋势
                "headline": str,           # 宣传标题（如"已为你节省 12.3K tokens"）
            }
        """
        stats = self.get_total_savings(time_window=time_window)

        # 每日趋势
        seconds = self._parse_time_window_seconds(time_window) if time_window else 86400 * 365
        cur = self.conn.execute(
            """
            SELECT
                DATE(created_at, 'unixepoch', 'localtime') as date,
                SUM(tokens_saved) as daily_saved,
                COUNT(*) as daily_ops
            FROM token_savings_ledger
            WHERE created_at >= ?
            GROUP BY DATE(created_at, 'unixepoch', 'localtime')
            ORDER BY date ASC
            """,
            (time.time() - seconds,),
        )
        daily_trend = [
            {"date": r["date"], "saved": r["daily_saved"], "ops": r["daily_ops"]}
            for r in cur
        ]

        # 生成宣传标题
        total = stats["total_saved"]
        if total >= 1000000:
            headline = f"已为你节省 {total / 1000000:.1f}M tokens"
        elif total >= 1000:
            headline = f"已为你节省 {total / 1000:.1f}K tokens"
        else:
            headline = f"已为你节省 {total} tokens"

        return {
            "time_window": time_window,
            "total_saved": total,
            "total_operations": stats["total_operations"],
            "avg_savings_pct": stats["avg_savings_pct"],
            "by_operation": stats["by_operation"],
            "daily_trend": daily_trend,
            "headline": headline,
        }

    @staticmethod
    def _parse_time_window_seconds(time_window: str) -> int:
        """解析时间窗口字符串为秒数"""
        if not time_window:
            return 0
        w = time_window.lower().strip()
        if w.endswith("d"):
            try:
                return int(w[:-1]) * 86400
            except ValueError:
                return 0
        if w.endswith("w"):
            try:
                return int(w[:-1]) * 86400 * 7
            except ValueError:
                return 0
        if w.endswith("m"):
            try:
                return int(w[:-1]) * 86400 * 30
            except ValueError:
                return 0
        if w.endswith("y"):
            try:
                return int(w[:-1]) * 86400 * 365
            except ValueError:
                return 0
        return 0
