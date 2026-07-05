"""
db_task_quality.py
==================

任务质量门禁（Task Quality Gate）Mixin。

承载任务完成门禁发现，区别于通用 guardrail_findings：
把 Semgrep、复杂度、调用链一致性、scope violation、i18n 硬编码等
质量问题挂到 task/step 上，使 open error/block finding 阻止任务进入 done。

事实层由 task_quality_findings 表（v21 schema）表达；
本模块只提供记录/查询/解决/阻断判断/修复步骤插入五个核心方法。

状态规则（与 plan 文档一致）：
- severity: info / warn / error / block
- status:   open / resolved / wontfix
- 严重度 info/warn：记录但不阻塞，task_status 显示 warn
- 严重度 error/block：step blocked，自动插入 fix_quality_gate_failure 步骤
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..i18n import t


# 阻塞性严重度（open 状态时阻止 step 进入 done）
BLOCKING_SEVERITIES = frozenset({"error", "block"})


class TaskQualityMixin:
    """任务质量门禁 Mixin

    提供任务完成门禁的记录、查询、解决、阻断判断、修复步骤插入能力。
    与 TaskMixin（任务状态机）协作：task_report_step 在决定 step 是否 done 前
    应调用 task_has_blocking_findings 判断是否存在阻塞 finding。
    """

    def record_task_quality_finding(
        self,
        task_id: str,
        step_id: str = "",
        finding_type: str = "",
        severity: str = "warn",
        message: str = "",
        evidence: Any = None,
        source: str = "",
    ) -> int:
        """写入一条 task_quality_findings 记录

        Args:
            task_id: 关联的任务 ID（必填）
            step_id: 关联的步骤 ID（可选，任务级 finding 留空）
            finding_type: 发现类型（semgrep / file_health / call_chain / scope / i18n / manual）
            severity: 严重度（info / warn / error / block），默认 warn
            message: 发现描述（必填）
            evidence: 证据数据（dict / list / str），自动 JSON 序列化
            source: 来源标识（semgrep / file_health / call_chain / scope / i18n / manual）

        Returns:
            finding_id（成功时 >0；失败时返回 0）
        """
        if not task_id:
            return 0
        if not message:
            return 0

        # 标准化严重度
        if severity not in ("info", "warn", "error", "block"):
            severity = "warn"

        # 序列化 evidence
        if evidence is None:
            evidence_str = ""
        elif isinstance(evidence, str):
            evidence_str = evidence
        else:
            try:
                evidence_str = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                evidence_str = str(evidence)

        ws_id = self._get_active_workspace_id() if hasattr(self, "_get_active_workspace_id") else None
        now = time.time()

        try:
            cur = self.conn.execute(
                """
                INSERT INTO task_quality_findings
                    (workspace_id, task_id, step_id, finding_type, severity, status,
                     message, evidence, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ws_id, task_id, step_id, finding_type, severity, "open",
                 message, evidence_str, source, now),
            )
            self.conn.commit()
            return cur.lastrowid
        except Exception:
            return 0

    def get_task_quality_findings(
        self,
        task_id: str,
        status: str = "open",
        severity: str = "",
    ) -> List[Dict[str, Any]]:
        """查询任务质量发现

        Args:
            task_id: 任务 ID（必填）
            status: 状态过滤（open / resolved / wontfix / all），默认 open
            severity: 严重度过滤（info / warn / error / block），默认不过滤

        Returns:
            finding 列表，按 created_at 升序排列（旧的先处理）
        """
        if not task_id:
            return []

        sql = "SELECT * FROM task_quality_findings WHERE task_id = ?"
        params: List[Any] = [task_id]

        if status and status != "all":
            sql += " AND status = ?"
            params.append(status)

        if severity:
            sql += " AND severity = ?"
            params.append(severity)

        sql += " ORDER BY created_at ASC"

        try:
            cur = self.conn.execute(sql, params)
            rows = cur.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []

    def resolve_task_quality_finding(
        self,
        finding_id: int,
        resolution: str = "fixed",
        resolved_by: str = "agent",
    ) -> Dict[str, Any]:
        """解决或豁免单条 finding

        Args:
            finding_id: finding ID
            resolution: 解决方式（fixed / wontfix / false_positive）
            resolved_by: 解决者标识（agent / human / system）

        Returns:
            {success, finding_id, status, resolution, resolved_at}
        """
        if not finding_id:
            return {"success": False, "error": t(
                "cli.messages.task_quality_finding_id_required",
                default="finding_id is required")}

        # 映射 resolution 到 status
        status_map = {
            "fixed": "resolved",
            "wontfix": "wontfix",
            "false_positive": "wontfix",
        }
        new_status = status_map.get(resolution, "resolved")
        now = time.time()

        try:
            cur = self.conn.execute(
                "SELECT id FROM task_quality_findings WHERE id = ?",
                (finding_id,),
            )
            if not cur.fetchone():
                return {"success": False, "error": t(
                    "cli.messages.task_quality_finding_not_found",
                    default="finding not found", id=finding_id)}

            self.conn.execute(
                """
                UPDATE task_quality_findings
                SET status = ?, resolved_at = ?, resolved_by = ?
                WHERE id = ?
                """,
                (new_status, now, resolved_by, finding_id),
            )
            self.conn.commit()
            return {
                "success": True,
                "finding_id": finding_id,
                "status": new_status,
                "resolution": resolution,
                "resolved_at": now,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def task_has_blocking_findings(self, task_id: str) -> bool:
        """判断任务是否存在 open 状态的 error/block finding

        用于 task_report_step 决定 step 是否可以进入 done。
        当返回 True 时，应阻止 step 进入 done，并自动插入修复步骤。

        Args:
            task_id: 任务 ID

        Returns:
            True 表示存在阻塞 finding
        """
        if not task_id:
            return False

        try:
            cur = self.conn.execute(
                """
                SELECT COUNT(*) as cnt
                FROM task_quality_findings
                WHERE task_id = ?
                  AND status = 'open'
                  AND severity IN ('error', 'block')
                """,
                (task_id,),
            )
            row = cur.fetchone()
            return row["cnt"] > 0
        except Exception:
            return False

    def insert_fix_quality_gate_step(
        self,
        task_id: str,
        source_step_id: str,
        findings: List[Dict[str, Any]],
    ) -> str:
        """为质量门禁失败自动插入修复步骤

        在 task 下插入一个新的 step（action="fix_quality_gate_failure"），
        关联触发修复的源 step 和阻塞 finding 列表。

        Args:
            task_id: 任务 ID
            source_step_id: 触发修复的源步骤 ID
            findings: 阻塞 finding 列表（dict 至少包含 id / severity / message / finding_type）

        Returns:
            新建的 step_id（失败时返回空字符串）
        """
        if not task_id:
            return ""

        from .db_tasks import _gen_step_id

        now = time.time()
        step_id = _gen_step_id()

        # 构造 check_items：每个 finding 一条修复提示
        check_lines: List[str] = []
        for f in findings:
            sev = f.get("severity", "warn")
            ftype = f.get("finding_type", "")
            msg = f.get("message", "")
            check_lines.append(f"[{sev}] {ftype}: {msg}")
        check_items = "\n".join(check_lines) if check_lines else ""

        # 序列化 findings 摘要存入 result（便于 Agent 阅读）
        findings_summary = json.dumps(
            [
                {
                    "id": f.get("id"),
                    "severity": f.get("severity"),
                    "finding_type": f.get("finding_type"),
                    "message": f.get("message"),
                    "source": f.get("source", ""),
                }
                for f in findings
            ],
            ensure_ascii=False,
        )

        # 计算新 step 的 step_index（追加到末尾）
        try:
            cur = self.conn.execute(
                "SELECT COALESCE(MAX(step_index), -1) as max_idx FROM task_steps WHERE task_id = ?",
                (task_id,),
            )
            step_index = cur.fetchone()["max_idx"] + 1
        except Exception:
            step_index = 0

        try:
            self.conn.execute(
                """
                INSERT INTO task_steps
                    (id, task_id, step_index, action, target_file, target_symbol,
                     check_items, status, result, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id,
                    task_id,
                    step_index,
                    "fix_quality_gate_failure",
                    "",
                    source_step_id,
                    check_items,
                    "pending",
                    findings_summary,
                    now,
                    None,
                ),
            )
            self.conn.commit()
            return step_id
        except Exception:
            return ""
