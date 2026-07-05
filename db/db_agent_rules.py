"""
db_agent_rules.py
=================

Agent Rule Memory Mixin。

提供项目规则的候选-审核-生效-同步全生命周期：
- rule_candidate_create / rule_candidate_list / rule_candidate_accept /
  rule_candidate_reject：候选规则的创建、列出、接受、拒绝。
- rule_list：列出已生效规则。
- get_applicable_rules：按上下文（语言/文件/动作/符号类型/finding_type/模块前缀）
  返回匹配的 active 规则，供 task_next_step / work_next_job / get_symbol /
  file_symbol_content 注入。
- extract_rule_candidates_from_quality_findings：从 task_quality_findings
  聚合重复问题，生成 pending 候选规则（不自动接受）。
- rule_sync_agents_md：把 active 规则同步到 AGENTS.md 标记区，默认 dry-run。

设计原则：
1. 候选规则默认 pending，必须 accept 后才会写入 agent_rules 并参与上下文注入。
2. accept 是 idempotent 的：重复 accept 同一 candidate 不会重复创建 active rule。
3. AGENTS.md 同步只改 marker block，不触碰人工维护内容。
4. 异常不静默吞掉，向外抛出供调用方处理。
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Dict, List, Optional

from ..i18n import t


# ============================================
# 状态常量
# ============================================

# 候选规则状态
CANDIDATE_STATUS_PENDING = "pending"
CANDIDATE_STATUS_ACCEPTED = "accepted"
CANDIDATE_STATUS_REJECTED = "rejected"

# 已生效规则状态
RULE_STATUS_ACTIVE = "active"
RULE_STATUS_DEPRECATED = "deprecated"
RULE_STATUS_REMOVED = "removed"

# 严重级别（按优先级降序）
SEVERITY_ORDER = {"critical": 4, "error": 3, "warning": 2, "info": 1}
VALID_SEVERITY = frozenset(SEVERITY_ORDER.keys())


def _gen_rule_id(prefix: str) -> str:
    """生成规则唯一 ID

    格式: {prefix}-{timestamp_ms}-{random4hex}
    例如: ARC-1783253838000-a1b2

    Args:
        prefix: ID 前缀（ARC=候选 / AR=已生效 / ARSL=同步日志）

    Returns:
        形如 ARC-1783253838000-a1b2 的唯一标识
    """
    ts_ms = int(time.time() * 1000)
    rand4 = secrets.token_hex(2)
    return f"{prefix}-{ts_ms}-{rand4}"


def _serialize_scope(scope: Optional[Dict[str, Any]]) -> str:
    """序列化 scope dict 为 JSON 字符串存储

    None 或空 dict 都序列化为 '{}'，保证默认值一致。
    """
    if not scope:
        return "{}"
    try:
        return json.dumps(scope, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _deserialize_scope(raw: str) -> Dict[str, Any]:
    """反序列化 scope_json 为 dict

    空串或非法 JSON 返回空 dict。
    """
    if not raw:
        return {}
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _serialize_evidence(evidence: Optional[Dict[str, Any]]) -> str:
    """序列化 evidence dict 为 JSON 字符串存储"""
    if not evidence:
        return "{}"
    try:
        return json.dumps(evidence, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _deserialize_evidence(raw: str) -> Dict[str, Any]:
    """反序列化 evidence_json 为 dict"""
    if not raw:
        return {}
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _normalize_severity(severity: str) -> str:
    """规范化 severity 字段

    非法值回落到 'info'，避免数据库写入脏数据。
    """
    if not severity:
        return "info"
    sev_lower = severity.lower()
    return sev_lower if sev_lower in VALID_SEVERITY else "info"


class AgentRulesMixin:
    """Agent Rule Memory 功能 Mixin

    通过 self.conn 访问数据库连接，提供规则候选-审核-生效-同步全链路。
    所有规则使用 TEXT 主键：
    - ARC-xxx: 候选规则
    - AR-xxx: 已生效规则
    - ARSL-xxx: 同步日志

    状态机：
    - 候选规则: pending → accepted（写入 agent_rules）/ rejected
    - 生效规则: active → deprecated → removed
    """

    # ============================================
    # 候选规则 CRUD
    # ============================================

    def rule_candidate_create(
        self,
        title: str,
        rule_text: str,
        scope: Optional[Dict[str, Any]] = None,
        severity: str = "info",
        source: str = "manual",
        evidence: Optional[Dict[str, Any]] = None,
        confidence: float = 0.0,
    ) -> str:
        """创建候选规则

        Args:
            title: 规则标题（简短描述）
            rule_text: 规则正文（Agent 注入时会原文返回）
            scope: 规则作用域，支持 languages/file_patterns/symbol_kinds/
                   actions/finding_types/module_prefixes 字段
            severity: 严重级别（critical/error/warning/info）
            source: 来源（manual / auto_quality_findings / auto_semgrep /
                    task_review / other）
            evidence: 证据 dict（如 {"task_id": "T-xxx", "occurrences": 3}）
            confidence: 置信度（0.0-1.0），自动提取时使用

        Returns:
            新建候选规则的 ID（ARC-xxx）

        Raises:
            ValueError: title 或 rule_text 为空
        """
        if not title or not title.strip():
            raise ValueError(t("cli.messages.rule_candidate_title_required"))
        if not rule_text or not rule_text.strip():
            raise ValueError(t("cli.messages.rule_candidate_text_required"))

        candidate_id = _gen_rule_id("ARC")
        now = time.time()
        scope_json = _serialize_scope(scope)
        sev = _normalize_severity(severity)
        evidence_json = _serialize_evidence(evidence)
        # confidence 限制在 [0.0, 1.0]
        conf = max(0.0, min(1.0, float(confidence)))

        self.conn.execute(
            """
            INSERT INTO agent_rule_candidates
                (id, title, rule_text, scope_json, severity, source,
                 evidence_json, confidence, status, created_at,
                 reviewed_at, reviewer, linked_rule_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                title.strip(),
                rule_text.strip(),
                scope_json,
                sev,
                source or "manual",
                evidence_json,
                conf,
                CANDIDATE_STATUS_PENDING,
                now,
                None,
                "",
                "",
            ),
        )
        self.conn.commit()
        return candidate_id

    def rule_candidate_list(
        self,
        status: str = CANDIDATE_STATUS_PENDING,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """列出候选规则

        Args:
            status: 状态过滤（pending / accepted / rejected），
                    空串表示不过滤
            limit: 返回数量上限

        Returns:
            候选规则列表，每个元素为 dict：
            {id, title, rule_text, scope, severity, source, evidence,
             confidence, status, created_at, reviewed_at, reviewer, linked_rule_id}
            scope / evidence 已反序列化为 dict
        """
        if limit <= 0:
            return []
        if status:
            cur = self.conn.execute(
                """
                SELECT id, title, rule_text, scope_json, severity, source,
                       evidence_json, confidence, status, created_at,
                       reviewed_at, reviewer, linked_rule_id
                FROM agent_rule_candidates
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (status, limit),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT id, title, rule_text, scope_json, severity, source,
                       evidence_json, confidence, status, created_at,
                       reviewed_at, reviewer, linked_rule_id
                FROM agent_rule_candidates
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [self._row_to_candidate(dict(row)) for row in cur]

    def rule_candidate_accept(
        self,
        candidate_id: str,
        reviewer: str = "agent",
    ) -> str:
        """接受候选规则，写入 agent_rules

        流程：
        1. 校验 candidate 存在且 status=pending
        2. 在 agent_rules 中创建 active 规则
        3. 更新 candidate 状态为 accepted，记录 reviewer/reviewed_at/linked_rule_id
        4. 返回新建的 active 规则 ID

        幂等性：重复 accept 同一 candidate 会抛出 ValueError，避免重复创建。
        如果 candidate 已经 accepted，且 linked_rule_id 还存在，则返回原 rule_id。

        Args:
            candidate_id: 候选规则 ID（ARC-xxx）
            reviewer: 审核人标识

        Returns:
            新建的 active 规则 ID（AR-xxx）

        Raises:
            ValueError: candidate 不存在 / 状态非 pending / 已被 reject
        """
        if not candidate_id:
            raise ValueError(t("cli.messages.rule_candidate_id_required"))

        cur = self.conn.execute(
            """
            SELECT id, title, rule_text, scope_json, severity, source,
                   evidence_json, confidence, status, linked_rule_id
            FROM agent_rule_candidates
            WHERE id = ?
            """,
            (candidate_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(
                t("cli.messages.rule_candidate_not_found", id=candidate_id)
            )

        current_status = row["status"]
        if current_status == CANDIDATE_STATUS_REJECTED:
            raise ValueError(
                t("cli.messages.rule_candidate_already_rejected", id=candidate_id)
            )
        if current_status == CANDIDATE_STATUS_ACCEPTED:
            # 幂等：已 accepted 则返回原 linked_rule_id（若仍存在）
            linked = row["linked_rule_id"] or ""
            if linked:
                # 校验 linked rule 是否还存在
                rule_cur = self.conn.execute(
                    "SELECT id FROM agent_rules WHERE id = ?",
                    (linked,),
                )
                if rule_cur.fetchone():
                    return linked
            # linked rule 已被删除，继续走 accept 流程重建
        elif current_status != CANDIDATE_STATUS_PENDING:
            raise ValueError(
                t(
                    "cli.messages.rule_candidate_invalid_status",
                    id=candidate_id,
                    status=current_status,
                )
            )

        # 创建 active 规则
        rule_id = _gen_rule_id("AR")
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO agent_rules
                (id, title, rule_text, scope_json, severity, status,
                 source_candidate_id, evidence_json, created_at, updated_at,
                 synced_to_agents_md, sync_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                row["title"],
                row["rule_text"],
                row["scope_json"],
                row["severity"],
                RULE_STATUS_ACTIVE,
                candidate_id,
                row["evidence_json"],
                now,
                now,
                0,
                "",
            ),
        )

        # 更新 candidate 状态
        self.conn.execute(
            """
            UPDATE agent_rule_candidates
            SET status = ?, reviewed_at = ?, reviewer = ?, linked_rule_id = ?
            WHERE id = ?
            """,
            (CANDIDATE_STATUS_ACCEPTED, now, reviewer or "agent", rule_id, candidate_id),
        )
        self.conn.commit()
        return rule_id

    def rule_candidate_reject(
        self,
        candidate_id: str,
        reviewer: str = "agent",
        reason: str = "",
    ) -> bool:
        """拒绝候选规则

        流程：
        1. 校验 candidate 存在且 status=pending
        2. 更新状态为 rejected，记录 reviewer/reviewed_at
        3. reason 写入 evidence_json 的 reject_reason 字段（保留原 evidence）

        幂等性：重复 reject 已 rejected 的 candidate 返回 True，不报错。

        Args:
            candidate_id: 候选规则 ID（ARC-xxx）
            reviewer: 审核人标识
            reason: 拒绝原因（可选）

        Returns:
            True 表示拒绝成功

        Raises:
            ValueError: candidate 不存在 / 状态非 pending 且非 rejected /
                        已被 accepted
        """
        if not candidate_id:
            raise ValueError(t("cli.messages.rule_candidate_id_required"))

        cur = self.conn.execute(
            """
            SELECT id, status, evidence_json
            FROM agent_rule_candidates
            WHERE id = ?
            """,
            (candidate_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(
                t("cli.messages.rule_candidate_not_found", id=candidate_id)
            )

        current_status = row["status"]
        if current_status == CANDIDATE_STATUS_ACCEPTED:
            raise ValueError(
                t("cli.messages.rule_candidate_already_accepted", id=candidate_id)
            )
        if current_status == CANDIDATE_STATUS_REJECTED:
            # 幂等：已 rejected 直接返回成功
            return True
        if current_status != CANDIDATE_STATUS_PENDING:
            raise ValueError(
                t(
                    "cli.messages.rule_candidate_invalid_status",
                    id=candidate_id,
                    status=current_status,
                )
            )

        # 把 reason 追加到 evidence 的 reject_reason 字段
        evidence = _deserialize_evidence(row["evidence_json"])
        if reason:
            evidence["reject_reason"] = reason
        evidence_json = _serialize_evidence(evidence)

        now = time.time()
        self.conn.execute(
            """
            UPDATE agent_rule_candidates
            SET status = ?, reviewed_at = ?, reviewer = ?, evidence_json = ?
            WHERE id = ?
            """,
            (CANDIDATE_STATUS_REJECTED, now, reviewer or "agent", evidence_json, candidate_id),
        )
        self.conn.commit()
        return True

    # ============================================
    # 已生效规则查询
    # ============================================

    def rule_list(
        self,
        status: str = RULE_STATUS_ACTIVE,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """列出已生效规则

        Args:
            status: 状态过滤（active / deprecated / removed），
                    空串表示不过滤
            limit: 返回数量上限

        Returns:
            规则列表，每个元素为 dict：
            {id, title, rule_text, scope, severity, status, source_candidate_id,
             evidence, created_at, updated_at, synced_to_agents_md, sync_hash}
            scope / evidence 已反序列化为 dict
        """
        if limit <= 0:
            return []
        if status:
            cur = self.conn.execute(
                """
                SELECT id, title, rule_text, scope_json, severity, status,
                       source_candidate_id, evidence_json, created_at, updated_at,
                       synced_to_agents_md, sync_hash
                FROM agent_rules
                WHERE status = ?
                ORDER BY
                    CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'error' THEN 1
                        WHEN 'warning' THEN 2
                        WHEN 'info' THEN 3
                        ELSE 4
                    END,
                    updated_at DESC
                LIMIT ?
                """,
                (status, limit),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT id, title, rule_text, scope_json, severity, status,
                       source_candidate_id, evidence_json, created_at, updated_at,
                       synced_to_agents_md, sync_hash
                FROM agent_rules
                ORDER BY
                    CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'error' THEN 1
                        WHEN 'warning' THEN 2
                        WHEN 'info' THEN 3
                        ELSE 4
                    END,
                    updated_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [self._row_to_rule(dict(row)) for row in cur]

    # ============================================
    # 内部辅助
    # ============================================

    def _row_to_candidate(self, row: dict) -> dict:
        """把数据库行转换为对外暴露的 candidate dict

        scope_json / evidence_json 反序列化为 dict。
        """
        return {
            "id": row.get("id", ""),
            "title": row.get("title", ""),
            "rule_text": row.get("rule_text", ""),
            "scope": _deserialize_scope(row.get("scope_json", "{}")),
            "severity": row.get("severity", "info"),
            "source": row.get("source", "manual"),
            "evidence": _deserialize_evidence(row.get("evidence_json", "{}")),
            "confidence": row.get("confidence", 0.0),
            "status": row.get("status", CANDIDATE_STATUS_PENDING),
            "created_at": row.get("created_at", 0.0),
            "reviewed_at": row.get("reviewed_at"),
            "reviewer": row.get("reviewer", ""),
            "linked_rule_id": row.get("linked_rule_id", ""),
        }

    def _row_to_rule(self, row: dict) -> dict:
        """把数据库行转换为对外暴露的 rule dict

        scope_json / evidence_json 反序列化为 dict。
        """
        return {
            "id": row.get("id", ""),
            "title": row.get("title", ""),
            "rule_text": row.get("rule_text", ""),
            "scope": _deserialize_scope(row.get("scope_json", "{}")),
            "severity": row.get("severity", "info"),
            "status": row.get("status", RULE_STATUS_ACTIVE),
            "source_candidate_id": row.get("source_candidate_id", ""),
            "evidence": _deserialize_evidence(row.get("evidence_json", "{}")),
            "created_at": row.get("created_at", 0.0),
            "updated_at": row.get("updated_at", 0.0),
            "synced_to_agents_md": bool(row.get("synced_to_agents_md", 0)),
            "sync_hash": row.get("sync_hash", ""),
        }
