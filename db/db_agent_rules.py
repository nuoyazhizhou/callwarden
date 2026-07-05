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
import os
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
    # 自动提取候选规则（Phase 5）
    # ============================================

    def extract_rule_candidates_from_quality_findings(
        self,
        task_id: str = "",
        min_occurrences: int = 2,
    ) -> List[str]:
        """从 task_quality_findings 聚合重复问题，生成 pending 候选规则

        设计目标：把任务完成门禁中反复出现的同类问题沉淀为项目规则候选，
        让人审后变成可注入的 active 规则。

        聚合维度：(finding_type, severity, source)
        - 同一组合下出现次数 >= min_occurrences 才生成候选
        - evidence 记录来源 finding_ids（最多 10 条）和 occurrences 总数
        - 生成的候选规则默认 pending，必须 accept 才会写入 agent_rules
        - 同一聚合键已有 pending 候选时跳过（避免重复生成）

        Args:
            task_id: 限定从指定任务提取；空串表示全库扫描
            min_occurrences: 触发阈值，默认 2

        Returns:
            新建的候选规则 ID 列表（ARC-xxx）
        """
        if min_occurrences < 1:
            min_occurrences = 1

        # 聚合查询：(finding_type, severity, source) → count + sample_message + finding_ids
        if task_id:
            cur = self.conn.execute(
                """
                SELECT
                    finding_type,
                    severity,
                    source,
                    COUNT(*) as occurrences,
                    MIN(message) as sample_message,
                    GROUP_CONCAT(id) as finding_ids
                FROM task_quality_findings
                WHERE task_id = ?
                GROUP BY finding_type, severity, source
                HAVING occurrences >= ?
                ORDER BY occurrences DESC
                """,
                (task_id, min_occurrences),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT
                    finding_type,
                    severity,
                    source,
                    COUNT(*) as occurrences,
                    MIN(message) as sample_message,
                    GROUP_CONCAT(id) as finding_ids
                FROM task_quality_findings
                GROUP BY finding_type, severity, source
                HAVING occurrences >= ?
                ORDER BY occurrences DESC
                """,
                (min_occurrences,),
            )

        created_ids: List[str] = []
        for row in cur:
            finding_type = row["finding_type"] or "unknown"
            severity_raw = row["severity"] or "info"
            source_raw = row["source"] or "task_quality"
            occurrences = int(row["occurrences"] or 0)
            sample = row["sample_message"] or ""
            finding_ids_str = row["finding_ids"] or ""

            # finding_ids 是 "1,2,3" 格式，转成 list[int]，最多取 10 条
            try:
                finding_ids: List[int] = [
                    int(x) for x in finding_ids_str.split(",") if x.strip()
                ][:10]
            except ValueError:
                finding_ids = []

            # 跳过已有 pending 候选的聚合键（避免重复生成）
            # 用 title 做去重键：finding_type + severity 简短标识
            dedup_title = t(
                "cli.messages.rule_candidate_auto_title",
                default="自动沉淀: {finding_type} ({severity})",
                finding_type=finding_type,
                severity=severity_raw,
            )
            existing = self.conn.execute(
                """
                SELECT 1 FROM agent_rule_candidates
                WHERE title = ? AND status = ?
                LIMIT 1
                """,
                (dedup_title, CANDIDATE_STATUS_PENDING),
            ).fetchone()
            if existing:
                continue

            # 生成候选规则正文：基于样例 message 和 finding_type
            rule_text = t(
                "cli.messages.rule_candidate_auto_text",
                default=(
                    "在任务执行中重复出现 {finding_type} 类型问题（{occurrences} 次）。"
                    "样例: {sample}"
                ),
                finding_type=finding_type,
                occurrences=occurrences,
                sample=sample,
            )

            # 自动提取的规则用 finding_types 作为作用域
            scope = {"finding_types": [finding_type]}

            # evidence 保存来源 finding_ids 和 occurrences
            evidence = {
                "source": "task_quality_findings",
                "finding_ids": finding_ids,
                "occurrences": occurrences,
                "task_id": task_id or "",
                "sample_message": sample,
            }

            # severity 从 finding 的 severity 映射到规则 severity
            # task_quality_findings 用 warn/error，规则用 warning/error/info
            sev_map = {"error": "error", "warn": "warning", "warning": "warning"}
            rule_severity = sev_map.get(severity_raw.lower(), "info")

            cid = self.rule_candidate_create(
                title=dedup_title,
                rule_text=rule_text,
                scope=scope,
                severity=rule_severity,
                source="auto_quality_findings",
                evidence=evidence,
                confidence=min(1.0, occurrences / 10.0),
            )
            created_ids.append(cid)

        return created_ids

    # ============================================
    # 适用规则匹配（Phase 2）
    # ============================================

    def get_applicable_rules(
        self,
        context: Dict[str, Any],
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """根据上下文返回匹配的 active 规则

        上下文字段（均为可选，缺失字段视为不参与匹配）：
        - language: str，规则语言（如 "python"）
        - file_path: str，文件相对路径（如 "cli/main.py"）
        - symbol_kind: str，符号类型（如 "function"/"method"/"class"）
        - action: str，动作类型（如 "edit"/"fix"/"review"）
        - finding_type: str，发现类型（如 "i18n"/"semgrep"/"signature"）
        - module_prefix: str，模块前缀（如 "cli." 或 "server."）
        - task_id: str，任务 ID（用于按 evidence.task_id 匹配，但不影响 scope）

        匹配规则（参见 docs/design/agent-rule-memory-plan.md）：
        1. 空 scope 视为全局规则，匹配所有上下文。
        2. 同一字段内是 OR：scope.languages=["python","go"] 匹配 language=python 或 go。
        3. 不同字段之间是 AND：必须所有出现的字段都命中才算匹配。
        4. file_patterns 支持 glob（如 "cli/*.py"）。
        5. module_prefixes 是前缀匹配（"cli." 匹配 "cli.main"）。

        排序：severity 优先级 + 匹配精度（命中字段数）+ updated_at 倒序。

        Args:
            context: 上下文 dict
            limit: 返回数量上限

        Returns:
            匹配的规则列表，每个元素为 dict：
            {id, title, rule_text, scope, severity, status, source_candidate_id,
             evidence, created_at, updated_at, synced_to_agents_md, sync_hash,
             matched_scope}
            matched_scope 是命中的字段标签列表（如 ["language:python", "action:edit"]），
            便于 Agent 在日志/调试中看到为什么这条规则被选中。
        """
        if limit <= 0:
            return []

        # 一次查询所有 active 规则，再在内存中做匹配
        # 限制扫描上限为 500，避免极端情况扫描过多
        cur = self.conn.execute(
            """
            SELECT id, title, rule_text, scope_json, severity, status,
                   source_candidate_id, evidence_json, created_at, updated_at,
                   synced_to_agents_md, sync_hash
            FROM agent_rules
            WHERE status = ?
            ORDER BY updated_at DESC
            LIMIT 500
            """,
            (RULE_STATUS_ACTIVE,),
        )
        all_rules = [dict(row) for row in cur]

        matched: List[Dict[str, Any]] = []
        for rule_row in all_rules:
            scope = _deserialize_scope(rule_row.get("scope_json", "{}"))
            # 空 scope 视为全局规则
            if not scope:
                rule_dict = self._row_to_rule(rule_row)
                rule_dict["matched_scope"] = ["global"]
                matched.append(rule_dict)
                continue

            matched_labels, ok = self._match_scope(scope, context)
            if ok:
                rule_dict = self._row_to_rule(rule_row)
                rule_dict["matched_scope"] = matched_labels
                matched.append(rule_dict)

        # 排序：severity 优先级 → 匹配精度（命中字段数倒序）→ updated_at 倒序
        matched.sort(
            key=lambda r: (
                -SEVERITY_ORDER.get(r["severity"], 0),
                -len(r.get("matched_scope", [])),
                -r.get("updated_at", 0.0),
            )
        )

        return matched[:limit]

    def _match_scope(
        self,
        scope: Dict[str, Any],
        context: Dict[str, Any],
    ) -> tuple:
        """匹配单个 scope dict 与上下文

        Args:
            scope: 规则作用域 dict
            context: 上下文 dict

        Returns:
            (matched_labels, ok) 元组：
            - matched_labels: 命中的字段标签列表（如 ["language:python"]）
            - ok: True 表示所有出现的字段都命中
        """
        import fnmatch

        labels: List[str] = []
        # 1. languages
        scope_langs = scope.get("languages") or []
        if scope_langs:
            ctx_lang = (context.get("language") or "").lower()
            if not ctx_lang or ctx_lang not in [s.lower() for s in scope_langs]:
                return ([], False)
            labels.append(f"language:{ctx_lang}")

        # 2. file_patterns（glob）
        scope_patterns = scope.get("file_patterns") or []
        if scope_patterns:
            ctx_file = context.get("file_path") or ""
            if not ctx_file:
                return ([], False)
            if not any(fnmatch.fnmatch(ctx_file, pat) for pat in scope_patterns):
                return ([], False)
            labels.append(f"file:{ctx_file}")

        # 3. symbol_kinds
        scope_kinds = scope.get("symbol_kinds") or []
        if scope_kinds:
            ctx_kind = (context.get("symbol_kind") or "").lower()
            if not ctx_kind or ctx_kind not in [s.lower() for s in scope_kinds]:
                return ([], False)
            labels.append(f"symbol_kind:{ctx_kind}")

        # 4. actions
        scope_actions = scope.get("actions") or []
        if scope_actions:
            ctx_action = (context.get("action") or "").lower()
            if not ctx_action or ctx_action not in [s.lower() for s in scope_actions]:
                return ([], False)
            labels.append(f"action:{ctx_action}")

        # 5. finding_types
        scope_findings = scope.get("finding_types") or []
        if scope_findings:
            ctx_ftype = (context.get("finding_type") or "").lower()
            if not ctx_ftype or ctx_ftype not in [s.lower() for s in scope_findings]:
                return ([], False)
            labels.append(f"finding_type:{ctx_ftype}")

        # 6. module_prefixes（前缀匹配）
        scope_prefixes = scope.get("module_prefixes") or []
        if scope_prefixes:
            ctx_module = context.get("module_prefix") or ""
            if not ctx_module or not any(
                ctx_module.startswith(p) for p in scope_prefixes
            ):
                return ([], False)
            labels.append(f"module:{ctx_module}")

        return (labels, True)

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

    # ============================================
    # 符号/文件入口注入辅助
    # ============================================

    # 语言扩展名映射表（与 db_tasks._build_rule_context_for_step 保持一致）
    _EXT_LANG_MAP = {
        ".py": "python",
        ".rs": "rust",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".java": "java",
        ".kt": "kotlin",
        ".c": "c",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cs": "csharp",
        ".rb": "ruby",
        ".php": "php",
        ".swift": "swift",
        ".scala": "scala",
    }

    def build_rule_context_for_symbol(
        self,
        qualified_name: str = "",
        file_path: str = "",
        kind: str = "",
    ) -> Dict[str, Any]:
        """根据符号信息构造规则匹配上下文

        供 get_symbol / file_symbol_content 等函数级入口复用：
        - 从 file_path 扩展名推断 language
        - 从 qualified_name 推断 module_prefix（取 . 或 :: 分隔后的前缀）
        - kind 直接作为 symbol_kind

        Args:
            qualified_name: 符号限定名（如 "cli.main.handle" / "mod::Sub::fn"）
            file_path: 文件相对路径
            kind: 符号类型（function/method/class 等）

        Returns:
            上下文 dict，可传入 get_applicable_rules
        """
        context: Dict[str, Any] = {}
        if file_path:
            context["file_path"] = file_path
            _, ext = os.path.splitext(file_path)
            lang = self._EXT_LANG_MAP.get(ext.lower())
            if lang:
                context["language"] = lang

        if kind:
            # symbol_kind 规范化为小写，与 _match_scope 中的比较保持一致
            context["symbol_kind"] = kind.lower()

        if qualified_name:
            # 从限定名推断 module_prefix（取最后一段之前的部分）
            if "::" in qualified_name:
                prefix = qualified_name.rsplit("::", 1)[0]
            elif "." in qualified_name:
                prefix = qualified_name.rsplit(".", 1)[0]
            else:
                prefix = ""
            if prefix:
                context["module_prefix"] = prefix

        return context

    def get_applicable_rules_for_symbol(
        self,
        qualified_name: str = "",
        file_path: str = "",
        kind: str = "",
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """根据符号信息返回匹配的 active 规则

        封装 build_rule_context_for_symbol + get_applicable_rules，
        供 get_symbol / file_symbol_content 等函数级入口直接调用。

        fail-soft：任何异常都返回空列表，不影响符号读取主流程。

        Args:
            qualified_name: 符号限定名
            file_path: 文件相对路径
            kind: 符号类型
            limit: 返回数量上限

        Returns:
            适用规则列表，每条含 id/title/rule_text/severity/matched_scope
        """
        try:
            context = self.build_rule_context_for_symbol(
                qualified_name=qualified_name,
                file_path=file_path,
                kind=kind,
            )
            rules = self.get_applicable_rules(context, limit=limit)
            # 精简字段，避免返回过大
            return [
                {
                    "id": r.get("id", ""),
                    "title": r.get("title", ""),
                    "rule_text": r.get("rule_text", ""),
                    "severity": r.get("severity", "info"),
                    "matched_scope": r.get("matched_scope", []),
                }
                for r in rules
            ]
        except Exception:
            return []
