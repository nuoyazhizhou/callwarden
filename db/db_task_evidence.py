"""
db_task_evidence.py
===================

P1 Snapshot-bound 追加式 Evidence 与 freshness Mixin（Requirements 1.2, 1.7, 6.1–6.24, 7.6–7.8）。

核心能力：
- append_evidence：不可变 append-only Evidence 追加（Req 1.7, 6.1, 6.3）
- invalidate_evidence：个体失效事件追加（Req 6.6, 6.24）
- register_verifier / revoke_verifier：Verifier_Registry + 单条撤销记录（Req 6.11–6.13, 6.20, 6.23）
- derive_freshness：查询时派生 Freshness_Status（Req 6.4–6.5, 6.12–6.15, 6.21）
  全序优先级：invalid > superseded > stale > fresh
- get/set_retention_config：保留窗口配置（Req 6.16–6.17）

设计原则：
- Evidence 追加与 gate decision 提交经 daemon 串行化点并使用 Authoritative_Clock（Req 6.19）
- 撤销采用单条记录 + 查询时派生，不逐条写入失效事件（Req 6.13, 6.20）
- 撤销不修改任何既有 payload，既有 evidence 逐字节保留（Req 6.23）
- 个体失效（payload 校验失败、引用不存在等）仍按 Req 6.6 追加个体失效事件（Req 6.24）
- 派生确定性：同一 evidence + 同一撤销记录集合 → 重复派生结果恒定（Req 6.21）
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 仓库父目录加入 sys.path
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from i18n import t
from .task_snapshot import (
    WorkspaceSnapshot,
    compute_snapshot_id,
)


# ============================================
# Freshness_Status 常量（Req 6.15, 7.2）
# ============================================

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_INVALID = "invalid"
FRESHNESS_SUPERSEDED = "superseded"
FRESHNESS_HISTORICAL_UNBOUND = "historical_unbound"
FRESHNESS_UNKNOWN = "unknown"

# 全序优先级（Req 6.15）：invalid > superseded > stale > fresh
# historical_unbound 和 unknown 是特殊状态，不参与全序
_FRESHNESS_PRIORITY: Dict[str, int] = {
    FRESHNESS_FRESH: 0,
    FRESHNESS_STALE: 1,
    FRESHNESS_SUPERSEDED: 2,
    FRESHNESS_INVALID: 3,
}

# 优先级名称（用于 Structured_Reason 中报告生效规则）
_PRIORITY_RULE_NAMES: Dict[int, str] = {
    0: "fresh_default",
    1: "stale_over_fresh",
    2: "superseded_over_stale",
    3: "invalid_over_all",
}


# ============================================
# 错误码目录（Req 1.12）
# ============================================

class EvidenceErrorCode:
    """Evidence 追加/失效/freshness 派生失败路径的稳定错误码目录。"""

    # Evidence payload hash 校验失败（Req 6.5）
    PAYLOAD_HASH_INVALID = "EVIDENCE_PAYLOAD_HASH_INVALID"
    # Evidence 引用不存在（contract/snapshot/verifier 不存在）（Req 6.5）
    REFERENCE_MISSING = "EVIDENCE_REFERENCE_MISSING"
    # Verifier 未在 Verifier_Registry 注册（Req 6.11–6.12）
    VERIFIER_NOT_REGISTERED = "EVIDENCE_VERIFIER_NOT_REGISTERED"
    # Verifier trust_status ≠ trusted（Req 6.12）
    VERIFIER_NOT_TRUSTED = "EVIDENCE_VERIFIER_NOT_TRUSTED"
    # 快照捕获失败（Req 6.9–6.10）
    SNAPSHOT_CAPTURE_FAILED = "EVIDENCE_SNAPSHOT_CAPTURE_FAILED"
    # S0/S1 快照不匹配（TOCTOU 防护，Req 6.10）
    SNAPSHOT_MISMATCH = "EVIDENCE_SNAPSHOT_MISMATCH"
    # 保留窗口配置非法（Req 6.16）
    RETENTION_WINDOW_INVALID = "EVIDENCE_RETENTION_WINDOW_INVALID"
    # 数据库写入失败
    PERSISTENCE_FAILED = "EVIDENCE_PERSISTENCE_FAILED"
    # Verifier 撤销记录重复（同一三元组已撤销，Req 6.13）
    REVOCATION_DUPLICATE = "EVIDENCE_REVOCATION_DUPLICATE"


# 警告码
class EvidenceWarningCode:
    """Evidence 非阻断警告的稳定警告码目录。"""

    # 保留窗口即将到期
    RETENTION_EXPIRING = "EVIDENCE_WARN_RETENTION_EXPIRING"


# 错误码 → i18n key
EVIDENCE_I18N_KEYS: Dict[str, str] = {
    EvidenceErrorCode.PAYLOAD_HASH_INVALID: "errors.evidence_payload_hash_invalid",
    EvidenceErrorCode.REFERENCE_MISSING: "errors.evidence_reference_missing",
    EvidenceErrorCode.VERIFIER_NOT_REGISTERED: "errors.evidence_verifier_not_registered",
    EvidenceErrorCode.VERIFIER_NOT_TRUSTED: "errors.evidence_verifier_not_trusted",
    EvidenceErrorCode.SNAPSHOT_CAPTURE_FAILED: "errors.evidence_snapshot_capture_failed",
    EvidenceErrorCode.SNAPSHOT_MISMATCH: "errors.evidence_snapshot_mismatch",
    EvidenceErrorCode.RETENTION_WINDOW_INVALID: "errors.evidence_retention_window_invalid",
    EvidenceErrorCode.PERSISTENCE_FAILED: "errors.evidence_persistence_failed",
    EvidenceErrorCode.REVOCATION_DUPLICATE: "errors.evidence_revocation_duplicate",
}

# 警告码 → i18n key
EVIDENCE_WARNING_I18N_KEYS: Dict[str, str] = {
    EvidenceWarningCode.RETENTION_EXPIRING: "warnings.evidence_retention_expiring",
}

# 双语默认文案（i18n catalog 尚未收录时使用）
_EVIDENCE_BUNDLED_DEFAULTS: Dict[str, Dict[str, str]] = {
    "errors.evidence_payload_hash_invalid": {
        "zh_CN": "Evidence payload hash 校验失败（evidence_id={evidence_id}）；判为 invalid 并排除条款满足（Requirement 6.5）。",
        "en_US": "Evidence payload hash validation failed (evidence_id={evidence_id}); classified as invalid and excluded from clause satisfaction (Requirement 6.5).",
    },
    "errors.evidence_reference_missing": {
        "zh_CN": "Evidence 引用不存在（{reference_type}={reference_value}）；判为 invalid（Requirement 6.5）。",
        "en_US": "Evidence reference does not exist ({reference_type}={reference_value}); classified as invalid (Requirement 6.5).",
    },
    "errors.evidence_verifier_not_registered": {
        "zh_CN": "Verifier 未注册（name={verifier_name}, version={verifier_version}）；Evidence 判为 invalid（Requirement 6.11–6.12）。",
        "en_US": "Verifier not registered (name={verifier_name}, version={verifier_version}); Evidence classified as invalid (Requirement 6.11–6.12).",
    },
    "errors.evidence_verifier_not_trusted": {
        "zh_CN": "Verifier 不可信（name={verifier_name}, version={verifier_version}, trust_status={trust_status}）；Evidence 判为 invalid（Requirement 6.12）。",
        "en_US": "Verifier not trusted (name={verifier_name}, version={verifier_version}, trust_status={trust_status}); Evidence classified as invalid (Requirement 6.12).",
    },
    "errors.evidence_snapshot_capture_failed": {
        "zh_CN": "快照捕获失败（原因：{reason}）；返回 unknown 并拒绝状态转换（Requirement 6.10）。",
        "en_US": "Snapshot capture failed (reason: {reason}); returned unknown and rejected state transition (Requirement 6.10).",
    },
    "errors.evidence_snapshot_mismatch": {
        "zh_CN": "S0/S1 快照不匹配（TOCTOU 防护）；追加 snapshot_changed_during_gate 事件，本轮结论按 stale 处理（Requirement 6.10）。",
        "en_US": "S0/S1 snapshot mismatch (TOCTOU protection); appended snapshot_changed_during_gate event, this round's conclusions treated as stale (Requirement 6.10).",
    },
    "errors.evidence_retention_window_invalid": {
        "zh_CN": "保留窗口非法（days={days}）；必须为正整数（Requirement 6.16）。",
        "en_US": "Invalid retention window (days={days}); must be a positive integer (Requirement 6.16).",
    },
    "errors.evidence_persistence_failed": {
        "zh_CN": "数据库写入失败（表={table}，错误：{error}）。",
        "en_US": "Database write failed (table={table}, error: {error}).",
    },
    "errors.evidence_revocation_duplicate": {
        "zh_CN": "Verifier 撤销记录重复（三元组 {verifier_name}/{verifier_version}/{verifier_config_hash} 已撤销）；不追加重复记录（Requirement 6.13）。",
        "en_US": "Verifier revocation record duplicate (triple {verifier_name}/{verifier_version}/{verifier_config_hash} already revoked); no duplicate record appended (Requirement 6.13).",
    },
    "warnings.evidence_retention_expiring": {
        "zh_CN": "Evidence 保留窗口即将到期（evidence_id={evidence_id}, 剩余 {remaining_days} 天）。",
        "en_US": "Evidence retention window expiring soon (evidence_id={evidence_id}, {remaining_days} days remaining).",
    },
}


# ============================================
# Authority gate 接入（0B：gate-first 写入口，计划 §3.3）
# ============================================

# authority 写路径未接入 gate 时的稳定错误码（0B fail-closed）。
EVIDENCE_AUTHORITY_GATE_DISABLED = "E_TASK_LOOP_CAPABILITY_DISABLED"


def _authority_gate_disabled_reason(detail: str) -> Dict[str, Any]:
    """构造 authority gate 未接入的 Structured_Reason（0B fail-closed）。

    `invalidate_evidence` / `revoke_verifier` 是 authority 写入口，按计划 §3.3
    必须在 `CapabilityMutationGate` 内（锁序 gate → authority store → task DB）
    提交，禁止存在绕过 gate 的直写入口。authority control-plane route 尚未
    接入时稳定拒绝，不回退本地 SQLite。
    """
    return {
        "success": False,
        "error": EVIDENCE_AUTHORITY_GATE_DISABLED,
        "code": EVIDENCE_AUTHORITY_GATE_DISABLED,
        "message_key": "errors.evidence_capability_disabled",
        "detail": detail,
    }


def _require_authority_gate() -> Optional[Dict[str, Any]]:
    """authority 写入口的 gate 前置校验（0B gate-first）。

    - local 模式：无 daemon gate，保留 legacy 直写语义（不阻断）。
    - enterprise/auto 模式：authority 写必须经 daemon `CapabilityMutationGate`
      串行化（锁序 gate → authority store → task DB）；0B 阶段该 control-plane
      route 未接入，稳定 fail-closed，禁止绕过 gate 直写。
    """
    from callwarden.config import get_daemon_mode

    mode = get_daemon_mode()
    if mode in ("enterprise", "auto"):
        return _authority_gate_disabled_reason(
            "authority 写路径必须经 daemon CapabilityMutationGate 提交"
            "（锁序 gate → authority store → task DB）；0B 阶段 gate 接入尚未"
            "完成，拒绝直写（fail-closed），不回退本地 SQLite"
        )
    return None


def _resolve_evidence_message(message_key: str, context: Dict[str, Any]) -> str:
    """解析 i18n 消息（与 db_task_contracts.py 同模式）。"""
    lang = t("current_locale", default="zh_CN") or "zh_CN"
    try:
        msg = t(message_key, **context)
        if msg and msg != message_key:
            return msg
    except Exception:
        pass
    defaults = _EVIDENCE_BUNDLED_DEFAULTS.get(message_key, {})
    template = defaults.get(lang) or defaults.get("zh_CN") or message_key
    try:
        return template.format(**context)
    except (KeyError, IndexError):
        return template


@dataclass
class EvidenceStructuredReason:
    """结构化失败原因（与 ContractStructuredReason 同模式，Req 1.12）。"""

    code: str
    message_key: str
    context: Dict[str, Any] = field(default_factory=dict)
    severity: str = "error"

    def message(self) -> str:
        return _resolve_evidence_message(self.message_key, self.context)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message_key": self.message_key,
            "message": self.message(),
            "context": dict(self.context),
            "severity": self.severity,
        }


def make_evidence_reason(code: str, severity: str = "error", **context: Any) -> EvidenceStructuredReason:
    """按错误码构造 EvidenceStructuredReason。"""
    if code not in EVIDENCE_I18N_KEYS:
        raise KeyError(f"未登记的 Evidence 错误码: {code}")
    return EvidenceStructuredReason(
        code=code,
        message_key=EVIDENCE_I18N_KEYS[code],
        context=dict(context),
        severity=severity,
    )


def make_evidence_warning(code: str, **context: Any) -> EvidenceStructuredReason:
    """按警告码构造非阻断 EvidenceStructuredReason（severity=warning）。"""
    if code not in EVIDENCE_WARNING_I18N_KEYS:
        raise KeyError(f"未登记的 Evidence 警告码: {code}")
    return EvidenceStructuredReason(
        code=code,
        message_key=EVIDENCE_WARNING_I18N_KEYS[code],
        context=dict(context),
        severity="warning",
    )


class EvidenceError(Exception):
    """携带 EvidenceStructuredReason 的 Evidence 操作异常。"""

    def __init__(self, reason: EvidenceStructuredReason):
        self.reason = reason
        super().__init__(f"[{reason.code}] {reason.message()}")


# ============================================
# Evidence 事件类型
# ============================================

EVIDENCE_TYPE_TEST_RUN = "test_run"
EVIDENCE_TYPE_STATIC_CHECK = "static_check"
EVIDENCE_TYPE_DIFF_MANIFEST = "diff_manifest"
EVIDENCE_TYPE_SYMBOL_CHANGE = "symbol_change"
EVIDENCE_TYPE_REVIEWER_VERDICT = "reviewer_verdict"

EVENT_TYPE_EVIDENCE_APPENDED = "evidence_appended"
EVENT_TYPE_EVIDENCE_INVALIDATED = "evidence_invalidated"


# ============================================
# Freshness 派生辅助
# ============================================

def _select_freshness_by_priority(
    candidates: List[Tuple[str, Optional[EvidenceStructuredReason]]]
) -> Tuple[str, Optional[EvidenceStructuredReason]]:
    """按全序优先级选择最终 Freshness_Status（Req 6.15）。

    Args:
        candidates: [(status, reason), ...]，每个 reason 可能为 None

    Returns:
        (selected_status, merged_reason) 其中 reason 同时报告所选状态与生效优先级
    """
    if not candidates:
        return FRESHNESS_FRESH, None

    # 过滤掉 unknown 和 historical_unbound（特殊状态，不参与全序）
    priority_candidates = [
        (s, r) for s, r in candidates if s in _FRESHNESS_PRIORITY
    ]
    if not priority_candidates:
        # 所有候选都是特殊状态，返回第一个
        return candidates[0]

    # 按优先级排序（高优先级在前）
    priority_candidates.sort(
        key=lambda x: _FRESHNESS_PRIORITY[x[0]], reverse=True
    )
    selected_status, selected_reason = priority_candidates[0]
    priority_level = _FRESHNESS_PRIORITY[selected_status]
    priority_rule = _PRIORITY_RULE_NAMES.get(priority_level, "")

    # 在 reason 中附加优先级信息
    if selected_reason:
        merged = EvidenceStructuredReason(
            code=selected_reason.code,
            message_key=selected_reason.message_key,
            context=dict(selected_reason.context),
            severity=selected_reason.severity,
        )
        merged.context["freshness_status"] = selected_status
        merged.context["priority_rule"] = priority_rule
        return selected_status, merged
    else:
        # fresh 无 reason，但需要报告优先级
        return selected_status, None


# ============================================
# TaskEvidenceMixin
# ============================================

class TaskEvidenceMixin:
    """P1 追加式 Evidence 与 freshness 派生 Mixin。

    提供 Evidence 追加、个体失效、Verifier 注册/撤销、Freshness 派生和保留窗口配置。
    所有写操作追加新记录，不修改或删除既有 payload（Req 1.7, 6.23）。
    """

    # ============================================
    # Evidence 追加（Req 1.7, 6.1, 6.3）
    # ============================================

    def append_evidence(
        self,
        task_id: str,
        contract_id: str,
        contract_revision: int,
        contract_hash: str,
        evidence_type: str,
        snapshot: WorkspaceSnapshot,
        verifier_name: str = "",
        verifier_version: str = "",
        verifier_config_hash: str = "",
        producer_identity: str = "",
        payload: Optional[Dict[str, Any]] = None,
        payload_hash: str = "",
        test_run_id: str = "",
        workspace_id: Optional[int] = None,
        lease_token: Optional[str] = None,
        fencing_counter: Optional[int] = None,
        lease_role: str = "implementer",
    ) -> Dict[str, Any]:
        """追加一条不可变 Evidence 记录（Req 1.7, 6.1, 6.3）。

        重跑 verifier 追加新记录，不替换（Req 6.3）。
        绑定 contract_id/revision/hash + snapshot + file/symbol hashes + verifier 三元组。

        P4 强化（Req 11.8-11.9）：
        - 提供 lease_token/fencing_counter 时启用受保护写路径（默认 role=implementer）
        - 过期 / token hash 不匹配 / 旧 counter 均在写入前拒绝，不改变 task data

        Args:
            task_id: 关联任务 ID
            contract_id: 契约 ID
            contract_revision: 契约 revision
            contract_hash: 契约 hash
            evidence_type: Evidence 类型（test_run/static_check/diff_manifest/symbol_change/reviewer_verdict）
            snapshot: Workspace_Snapshot 绑定
            verifier_name: Verifier 名称
            verifier_version: Verifier 版本
            verifier_config_hash: Verifier 配置摘要
            producer_identity: 生产者身份（agent/session/tool）
            payload: Evidence payload dict（JSON 序列化存储）
            payload_hash: payload 摘要（若为空则自动计算）
            test_run_id: 关联测试运行 ID（可选）
            workspace_id: 工作区 ID
            lease_token: P4 可选的 Lease raw token（提供时启用受保护写校验）
            fencing_counter: P4 可选的当前 fencing counter
            lease_role: P4 受保护写角色（默认 implementer）

        Returns:
            {success, evidence_id, event_id} 或 {success: False, error: ...}
        """
        if not task_id or not contract_id:
            return {"success": False, "error": "task_id and contract_id are required"}
        if contract_revision <= 0:
            return {"success": False, "error": "contract_revision must be positive"}
        if not contract_hash:
            return {"success": False, "error": "contract_hash is required"}

        # P4: protected mutation Lease 校验（Req 11.8-11.9）
        # Evidence 追加是受保护写；校验失败在写入前拒绝，不改变 task data。
        if lease_token is not None and fencing_counter is not None:
            ok_l, lease_reason = self.validate_lease_for_mutation(
                task_id, lease_role, lease_token, fencing_counter
            )
            if not ok_l:
                return {
                    "success": False,
                    "error": lease_reason["code"],
                    "reason": lease_reason,
                }

        # 自动计算 payload_hash（Req 6.1: payload hash）
        if not payload_hash and payload is not None:
            payload_bytes = json.dumps(
                payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            payload_hash = "sha256:" + hashlib.sha256(payload_bytes).hexdigest()

        # 生成唯一 evidence_id
        evidence_id = f"E-{uuid.uuid4().hex[:16]}"
        now = time.time()  # P1 阶段 daemon 不可用时退化为客户端时钟（Req 6.19）

        # 序列化 file_hashes 和 symbol_hashes
        file_hashes_json = json.dumps(
            snapshot.file_hashes, sort_keys=True, ensure_ascii=False
        ) if snapshot.file_hashes else ""
        symbol_hashes_json = json.dumps(
            snapshot.symbol_hashes, sort_keys=True, ensure_ascii=False
        ) if snapshot.symbol_hashes else ""

        # payload JSON
        payload_json = json.dumps(
            payload, sort_keys=True, ensure_ascii=False
        ) if payload else ""

        try:
            cur = self.conn.execute(
                """
                INSERT INTO task_evidence_events
                    (evidence_id, task_id, contract_id, contract_revision, contract_hash,
                     evidence_type, event_type,
                     commit_hash, workspace_snapshot_id, file_hashes, symbol_hashes,
                     graph_refresh_version,
                     verifier_name, verifier_version, verifier_config_hash,
                     producer_identity, produced_at, payload_hash,
                     workspace_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id, task_id, contract_id, contract_revision, contract_hash,
                    evidence_type, EVENT_TYPE_EVIDENCE_APPENDED,
                    snapshot.head_commit, snapshot.snapshot_id,
                    file_hashes_json, symbol_hashes_json,
                    snapshot.graph_refresh_version,
                    verifier_name, verifier_version, verifier_config_hash,
                    producer_identity, now, payload_hash,
                    workspace_id,
                ),
            )
            self.conn.commit()
            return {
                "success": True,
                "evidence_id": evidence_id,
                "event_id": cur.lastrowid,
                "produced_at": now,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============================================
    # 个体失效（Req 6.6, 6.24）
    # ============================================

    def invalidate_evidence(
        self,
        evidence_id: str,
        reason_code: str,
        reason_detail: str = "",
    ) -> Dict[str, Any]:
        """追加个体失效事件（Req 6.6, 6.24）。

        不修改原始 Evidence 记录，追加一条 evidence_invalidated 事件。
        个体失效场景：payload hash 校验失败、引用不存在等。

        Args:
            evidence_id: 原始 Evidence ID
            reason_code: 失效原因码（EvidenceErrorCode 之一）
            reason_detail: 失效详情

        Returns:
            {success, event_id} 或 {success: False, error: ...}
        """
        # 0B gate-first：authority 写入口须经 CapabilityMutationGate，禁止绕过 gate 直写。
        blocked = _require_authority_gate()
        if blocked is not None:
            return blocked

        if not evidence_id:
            return {"success": False, "error": "evidence_id is required"}

        # 查找原始 Evidence
        try:
            cur = self.conn.execute(
                "SELECT * FROM task_evidence_events WHERE evidence_id = ? "
                "AND event_type = ?",
                (evidence_id, EVENT_TYPE_EVIDENCE_APPENDED),
            )
            original = cur.fetchone()
            if not original:
                return {"success": False, "error": f"evidence {evidence_id} not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}

        now = time.time()
        invalidation_reason = json.dumps(
            {"code": reason_code, "detail": reason_detail},
            ensure_ascii=False,
        )
        invalidation_id = f"EINV-{uuid.uuid4().hex[:16]}"

        try:
            cur = self.conn.execute(
                """
                INSERT INTO task_evidence_events
                    (evidence_id, task_id, contract_id, contract_revision, contract_hash,
                     evidence_type, event_type,
                     commit_hash, workspace_snapshot_id, file_hashes, symbol_hashes,
                     graph_refresh_version,
                     verifier_name, verifier_version, verifier_config_hash,
                     producer_identity, produced_at, payload_hash,
                     invalidation_reason, original_evidence_ref,
                     workspace_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invalidation_id,
                    original["task_id"],
                    original["contract_id"],
                    original["contract_revision"],
                    original["contract_hash"],
                    original["evidence_type"],
                    EVENT_TYPE_EVIDENCE_INVALIDATED,
                    original["commit_hash"],
                    original["workspace_snapshot_id"],
                    original["file_hashes"],
                    original["symbol_hashes"],
                    original["graph_refresh_version"],
                    original["verifier_name"],
                    original["verifier_version"],
                    original["verifier_config_hash"],
                    original["producer_identity"],
                    now,
                    original["payload_hash"],
                    invalidation_reason,
                    evidence_id,
                    original["workspace_id"] if "workspace_id" in original.keys() else None,
                ),
            )
            self.conn.commit()
            return {
                "success": True,
                "event_id": cur.lastrowid,
                "invalidation_id": invalidation_id,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============================================
    # Verdict 提交（P1 write-path，Req 6.7-6.10）
    # ============================================

    def submit_verdict(
        self,
        task_id: str,
        contract_id: str,
        contract_revision: int,
        contract_hash: str,
        phase: str = "PRE_VERDICT",
        clause_results: Optional[Dict[str, Any]] = None,
        findings: Optional[List[Dict[str, Any]]] = None,
        overall: str = "",
        reviewer_identity: str = "",
        view_manifest_hash: str = "",
        snapshot_id: str = "",
        attestation: str = "",
        amendment_ref: str = "",
        verdict_id: str = "",
        workspace_id: Optional[int] = None,
        lease_token: Optional[str] = None,
        fencing_counter: Optional[int] = None,
        lease_role: str = "reviewer",
    ) -> Dict[str, Any]:
        """提交一条 Reviewer Verdict（P1 write-path）。

        写入 task_verdict_events，供 evaluate_evidence_gate_for_task 消费。
        追加式记录，不修改既有 payload（Req 1.7, 6.23）。

        P4 强化（Req 11.8-11.9）：
        - 提供 lease_token/fencing_counter 时启用受保护写路径（默认 role=reviewer）
        - 过期 / token hash 不匹配 / 旧 counter 均在写入前拒绝，不改变 task data

        Args:
            task_id: 关联任务 ID
            contract_id: 契约 ID
            contract_revision: 契约 revision
            contract_hash: 契约 hash
            phase: Verdict 阶段（PRE_VERDICT/POST_VERDICT 等）
            clause_results: 条款级评审结果 dict（JSON 存储）
            findings: 发现列表（JSON 存储）
            overall: 总体结论（approved/rejected/needs_changes/unclear）
            reviewer_identity: 评审者身份（agent/session marker）
            view_manifest_hash: 盲视 manifest hash（可空）
            snapshot_id: 绑定的 workspace snapshot id（可空）
            attestation: 评审者声明（可空）
            amendment_ref: 修订引用（可空）
            verdict_id: 显式 verdict id（可空，默认生成）
            workspace_id: 工作区 ID
            lease_token: P4 可选的 Lease raw token（提供时启用受保护写校验）
            fencing_counter: P4 可选的当前 fencing counter
            lease_role: P4 受保护写角色（默认 reviewer）

        Returns:
            {success, verdict_id, event_id} 或 {success: False, error: ...}
        """
        if not task_id or not contract_id:
            return {"success": False, "error": "task_id and contract_id are required"}
        if contract_revision <= 0:
            return {"success": False, "error": "contract_revision must be positive"}
        if not contract_hash:
            return {"success": False, "error": "contract_hash is required"}

        # P4: protected mutation Lease 校验（Req 11.8-11.9）
        # Verdict 提交是受保护写；校验失败在写入前拒绝，不改变 task data。
        if lease_token is not None and fencing_counter is not None:
            ok_l, lease_reason = self.validate_lease_for_mutation(
                task_id, lease_role, lease_token, fencing_counter
            )
            if not ok_l:
                return {
                    "success": False,
                    "error": lease_reason["code"],
                    "reason": lease_reason,
                }

        vid = verdict_id or f"V-{uuid.uuid4().hex[:16]}"
        now = time.time()  # P1 阶段 daemon 不可用时退化为客户端时钟（Req 6.19）

        clause_json = json.dumps(
            clause_results, sort_keys=True, ensure_ascii=False
        ) if clause_results else ""
        findings_json = json.dumps(
            findings, sort_keys=True, ensure_ascii=False
        ) if findings else ""

        try:
            cur = self.conn.execute(
                """
                INSERT INTO task_verdict_events
                    (verdict_id, task_id, contract_id, contract_revision, contract_hash,
                     phase, view_manifest_hash, snapshot_id, reviewer_identity,
                     clause_results, findings, overall, attestation, amendment_ref,
                     submitted_at, workspace_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vid, task_id, contract_id, contract_revision, contract_hash,
                    phase, view_manifest_hash, snapshot_id, reviewer_identity,
                    clause_json, findings_json, overall, attestation, amendment_ref,
                    now, workspace_id,
                ),
            )
            self.conn.commit()
            return {
                "success": True,
                "verdict_id": vid,
                "event_id": cur.lastrowid,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============================================
    # Verifier_Registry（Req 6.11–6.12）
    # ============================================

    def register_verifier(
        self,
        name: str,
        version: str,
        config_hash: str,
        registered_by: str = "",
        description: str = "",
    ) -> Dict[str, Any]:
        """注册 Verifier 到 Verifier_Registry（Req 6.11）。

        Args:
            name: Verifier 名称
            version: Verifier 版本
            config_hash: Verifier 配置摘要（sha256:...）
            registered_by: 注册者 session marker
            description: Verifier 描述

        Returns:
            {success, id, action} 或 {success: False, error: ...}
        """
        if not name or not version or not config_hash:
            return {"success": False, "error": "name, version, config_hash are required"}

        now = time.time()
        try:
            # 检查是否已有记录
            cur = self.conn.execute(
                "SELECT id, trust_status FROM verifier_registry "
                "WHERE name = ? AND version = ? AND config_hash = ?",
                (name, version, config_hash),
            )
            existing = cur.fetchone()

            if existing:
                return {
                    "success": True,
                    "id": existing["id"],
                    "action": "exists",
                    "trust_status": existing["trust_status"],
                }

            cur = self.conn.execute(
                """
                INSERT INTO verifier_registry
                    (name, version, config_hash, trust_status,
                     registration_time, registered_by, description)
                VALUES (?, ?, ?, 'trusted', ?, ?, ?)
                """,
                (name, version, config_hash, now, registered_by, description),
            )
            self.conn.commit()
            return {
                "success": True,
                "id": cur.lastrowid,
                "action": "inserted",
                "trust_status": "trusted",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_verifier_status(
        self, name: str, version: str, config_hash: str
    ) -> Optional[str]:
        """查询 Verifier 信任状态（只读，可走 MCP）。

        Returns:
            trust_status 字符串（trusted/revoked），或 None（未注册）
        """
        try:
            cur = self.conn.execute(
                "SELECT trust_status FROM verifier_registry "
                "WHERE name = ? AND version = ? AND config_hash = ?",
                (name, version, config_hash),
            )
            row = cur.fetchone()
            if not row:
                return None
            return row["trust_status"]
        except Exception:
            return None

    # ============================================
    # Verifier 撤销（Req 6.13, 6.20, 6.23）
    # ============================================

    def revoke_verifier(
        self,
        name: str,
        version: str,
        config_hash: str,
        reason: str,
        initiating_actor: str = "",
    ) -> Dict[str, Any]:
        """追加单条 Verifier_Revocation_Record（Req 6.13, 6.20, 6.23）。

        撤销不逐条写入失效事件——invalid 由查询时按三元组匹配派生（Req 6.13）。
        撤销不修改任何既有 Evidence payload（Req 6.23）。
        同一三元组只追加一条撤销记录（UNIQUE 约束）。

        Args:
            name: Verifier 名称
            version: Verifier 版本
            config_hash: Verifier 配置摘要
            reason: 撤销原因
            initiating_actor: 发起者身份

        Returns:
            {success, revocation_id} 或 {success: False, error: ...}
        """
        # 0B gate-first：authority 写入口须经 CapabilityMutationGate，禁止绕过 gate 直写。
        blocked = _require_authority_gate()
        if blocked is not None:
            return blocked

        if not name or not version or not config_hash:
            return {"success": False, "error": "name, version, config_hash are required"}
        if not reason:
            return {"success": False, "error": "reason is required"}

        now = time.time()  # Authoritative_Clock（Req 6.20）

        try:
            # 检查是否已撤销（UNIQUE 约束保证幂等）
            cur = self.conn.execute(
                "SELECT id FROM verifier_revocation_records "
                "WHERE verifier_name = ? AND verifier_version = ? AND verifier_config_hash = ?",
                (name, version, config_hash),
            )
            existing = cur.fetchone()
            if existing:
                return {
                    "success": False,
                    "error": "already revoked",
                    "reason": make_evidence_reason(
                        EvidenceErrorCode.REVOCATION_DUPLICATE,
                        verifier_name=name,
                        verifier_version=version,
                        verifier_config_hash=config_hash,
                    ).to_dict(),
                }

            cur = self.conn.execute(
                """
                INSERT INTO verifier_revocation_records
                    (verifier_name, verifier_version, verifier_config_hash,
                     revocation_reason, initiating_actor_identity, revocation_time)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, version, config_hash, reason, initiating_actor, now),
            )

            # 更新 verifier_registry 的 trust_status
            self.conn.execute(
                "UPDATE verifier_registry SET trust_status = 'revoked' "
                "WHERE name = ? AND version = ? AND config_hash = ?",
                (name, version, config_hash),
            )
            self.conn.commit()

            return {
                "success": True,
                "revocation_id": cur.lastrowid,
                "revocation_time": now,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def is_verifier_revoked(
        self, name: str, version: str, config_hash: str
    ) -> bool:
        """查询 Verifier 三元组是否已被撤销（只读，可走 MCP）。

        派生确定性保证（Req 6.21）：同一三元组 + 同一撤销记录集合 → 结果恒定。
        """
        try:
            cur = self.conn.execute(
                "SELECT 1 FROM verifier_revocation_records "
                "WHERE verifier_name = ? AND verifier_version = ? AND verifier_config_hash = ?",
                (name, version, config_hash),
            )
            return cur.fetchone() is not None
        except Exception:
            return False

    # ============================================
    # Freshness 派生（Req 6.4–6.5, 6.12–6.15, 6.21）
    # ============================================

    def derive_freshness(
        self,
        evidence_id: str,
        current_contract_revision: int,
        current_snapshot: Optional[WorkspaceSnapshot] = None,
        current_file_hashes: Optional[Dict[str, str]] = None,
        current_symbol_hashes: Optional[Dict[str, str]] = None,
        current_graph_version: str = "",
    ) -> Tuple[str, Optional[EvidenceStructuredReason]]:
        """查询时派生 Freshness_Status（Req 6.4–6.5, 6.12–6.15, 6.21）。

        全序优先级：invalid > superseded > stale > fresh（Req 6.15）。
        返回 (status, reason) 其中 reason 同时报告所选状态与生效优先级。

        派生规则：
        - invalid: payload hash 校验失败 / 引用不存在 / verifier 无条目或非 trusted / 匹配撤销记录
        - superseded: 同 contract_id 发布了更高 revision（Req 6.14）
        - stale: snapshot/file hash/symbol hash/graph version/verifier version+config 变化（Req 6.4）
        - fresh: 全部匹配
        - historical_unbound: 旧 test_runs 未绑定 contract 和 snapshot（Req 7.2）
        - unknown: 快照捕获失败（Req 6.10）

        Args:
            evidence_id: Evidence ID
            current_contract_revision: 当前契约 revision
            current_snapshot: 当前 Workspace_Snapshot（可选，用于比较）
            current_file_hashes: 当前文件 hash（可选，用于比较）
            current_symbol_hashes: 当前符号 hash（可选，用于比较）
            current_graph_version: 当前图刷新版本

        Returns:
            (status, reason) — reason 为 None 时表示 fresh 无错误
        """
        # 查找原始 Evidence
        try:
            cur = self.conn.execute(
                "SELECT * FROM task_evidence_events WHERE evidence_id = ? "
                "AND event_type = ?",
                (evidence_id, EVENT_TYPE_EVIDENCE_APPENDED),
            )
            row = cur.fetchone()
        except Exception:
            return FRESHNESS_UNKNOWN, make_evidence_reason(
                EvidenceErrorCode.PERSISTENCE_FAILED,
                table="task_evidence_events",
                error="query failed",
            )

        if not row:
            return FRESHNESS_INVALID, make_evidence_reason(
                EvidenceErrorCode.REFERENCE_MISSING,
                reference_type="evidence_id",
                reference_value=evidence_id,
            )

        candidates: List[Tuple[str, Optional[EvidenceStructuredReason]]] = []

        # 1. 检查是否已被个体失效（Req 6.5）
        try:
            inv_cur = self.conn.execute(
                "SELECT 1 FROM task_evidence_events "
                "WHERE original_evidence_ref = ? AND event_type = ?",
                (evidence_id, EVENT_TYPE_EVIDENCE_INVALIDATED),
            )
            if inv_cur.fetchone():
                candidates.append((
                    FRESHNESS_INVALID,
                    make_evidence_reason(
                        EvidenceErrorCode.PAYLOAD_HASH_INVALID,
                        evidence_id=evidence_id,
                    ),
                ))
        except Exception:
            pass

        # 2. 检查 Verifier 是否注册且可信（Req 6.11–6.12）
        v_name = row["verifier_name"]
        v_version = row["verifier_version"]
        v_config = row["verifier_config_hash"]
        if v_name and v_version and v_config:
            status = self.get_verifier_status(v_name, v_version, v_config)
            if status is None:
                candidates.append((
                    FRESHNESS_INVALID,
                    make_evidence_reason(
                        EvidenceErrorCode.VERIFIER_NOT_REGISTERED,
                        verifier_name=v_name,
                        verifier_version=v_version,
                    ),
                ))
            elif status != "trusted":
                candidates.append((
                    FRESHNESS_INVALID,
                    make_evidence_reason(
                        EvidenceErrorCode.VERIFIER_NOT_TRUSTED,
                        verifier_name=v_name,
                        verifier_version=v_version,
                        trust_status=status,
                    ),
                ))
            # 检查撤销记录（Req 6.13: 查询时派生）
            if self.is_verifier_revoked(v_name, v_version, v_config):
                candidates.append((
                    FRESHNESS_INVALID,
                    make_evidence_reason(
                        EvidenceErrorCode.VERIFIER_NOT_TRUSTED,
                        verifier_name=v_name,
                        verifier_version=v_version,
                        trust_status="revoked",
                    ),
                ))

        # 3. 检查 superseded（Req 6.14: revision 前进）
        bound_revision = row["contract_revision"]
        if current_contract_revision > bound_revision:
            candidates.append((FRESHNESS_SUPERSEDED, None))

        # 4. 检查 stale（Req 6.4: 绑定维度变化）
        stale_reasons: List[str] = []
        if current_snapshot is not None:
            # 比较 snapshot_id
            bound_snapshot_id = row["workspace_snapshot_id"]
            if bound_snapshot_id and current_snapshot.snapshot_id:
                if bound_snapshot_id != current_snapshot.snapshot_id:
                    stale_reasons.append("workspace_snapshot_id")
            # 比较文件 hash
            if current_file_hashes is not None:
                bound_file_hashes = json.loads(row["file_hashes"]) if row["file_hashes"] else {}
                for path, h in bound_file_hashes.items():
                    current_h = current_file_hashes.get(path, "")
                    if current_h and h and current_h != h:
                        stale_reasons.append(f"file_hash:{path}")
                        break
            # 比较符号 hash
            if current_symbol_hashes is not None:
                bound_symbol_hashes = json.loads(row["symbol_hashes"]) if row["symbol_hashes"] else {}
                for qn, h in bound_symbol_hashes.items():
                    current_h = current_symbol_hashes.get(qn, "")
                    if current_h and h and current_h != h:
                        stale_reasons.append(f"symbol_hash:{qn}")
                        break
            # 比较图刷新版本
            if current_graph_version and row["graph_refresh_version"]:
                if current_graph_version != row["graph_refresh_version"]:
                    stale_reasons.append("graph_refresh_version")

        if stale_reasons:
            candidates.append((FRESHNESS_STALE, None))

        # 5. 如果没有候选状态，则为 fresh
        if not candidates:
            return FRESHNESS_FRESH, None

        # 6. 按全序优先级选择（Req 6.15）
        return _select_freshness_by_priority(candidates)

    # ============================================
    # Evidence 查询
    # ============================================

    def get_evidence(self, evidence_id: str) -> Optional[Dict[str, Any]]:
        """查询单条 Evidence（只读，可走 MCP）。"""
        try:
            cur = self.conn.execute(
                "SELECT * FROM task_evidence_events "
                "WHERE evidence_id = ? AND event_type = ?",
                (evidence_id, EVENT_TYPE_EVIDENCE_APPENDED),
            )
            row = cur.fetchone()
            if not row:
                return None
            result = dict(row)
            # 反序列化 JSON 字段
            for field_name in ("file_hashes", "symbol_hashes"):
                if result.get(field_name):
                    try:
                        result[field_name] = json.loads(result[field_name])
                    except (json.JSONDecodeError, TypeError):
                        pass
            return result
        except Exception:
            return None

    def list_evidence_for_task(
        self, task_id: str, contract_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """查询任务的所有 Evidence（只读，可走 MCP）。"""
        sql = "SELECT * FROM task_evidence_events WHERE task_id = ? AND event_type = ?"
        params: list = [task_id, EVENT_TYPE_EVIDENCE_APPENDED]
        if contract_id:
            sql += " AND contract_id = ?"
            params.append(contract_id)
        sql += " ORDER BY produced_at ASC"
        try:
            cur = self.conn.execute(sql, params)
            results = []
            for row in cur.fetchall():
                item = dict(row)
                for field_name in ("file_hashes", "symbol_hashes"):
                    if item.get(field_name):
                        try:
                            item[field_name] = json.loads(item[field_name])
                        except (json.JSONDecodeError, TypeError):
                            pass
                results.append(item)
            return results
        except Exception:
            return []

    def list_evidence_for_contract(
        self, contract_id: str, contract_revision: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """查询契约的所有 Evidence（只读，可走 MCP）。"""
        sql = "SELECT * FROM task_evidence_events WHERE contract_id = ? AND event_type = ?"
        params: list = [contract_id, EVENT_TYPE_EVIDENCE_APPENDED]
        if contract_revision is not None:
            sql += " AND contract_revision = ?"
            params.append(contract_revision)
        sql += " ORDER BY produced_at ASC"
        try:
            cur = self.conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
        except Exception:
            return []

    # ============================================
    # 保留窗口配置（Req 6.16–6.17）
    # ============================================

    def get_retention_config(
        self, workspace_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """查询保留窗口配置（只读，可走 MCP）。

        优先返回 workspace 级配置，回退到 global 级（Req 6.16）。
        """
        scope = "workspace" if workspace_id is not None else "global"
        try:
            if workspace_id is not None:
                cur = self.conn.execute(
                    "SELECT * FROM evidence_retention_config "
                    "WHERE scope = 'workspace' AND workspace_id = ?",
                    (workspace_id,),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
            # 回退到 global
            cur = self.conn.execute(
                "SELECT * FROM evidence_retention_config WHERE scope = 'global'"
            )
            row = cur.fetchone()
            if row:
                return dict(row)
            # 默认值
            return {
                "scope": "global",
                "workspace_id": None,
                "retention_window_days": 365,
                "archive_location": "",
                "archive_format": "jsonl",
                "auto_archive": 0,
            }
        except Exception:
            return {
                "scope": scope,
                "retention_window_days": 365,
            }

    def set_retention_config(
        self,
        retention_window_days: int,
        workspace_id: Optional[int] = None,
        archive_location: str = "",
        archive_format: str = "jsonl",
        auto_archive: bool = False,
    ) -> Dict[str, Any]:
        """设置保留窗口配置（写操作，走 CLI）。

        Args:
            retention_window_days: 保留窗口天数（默认 365，Req 6.16）
            workspace_id: 工作区 ID（None=全局）
            archive_location: 归档位置
            archive_format: 归档格式
            auto_archive: 是否自动归档

        Returns:
            {success, id, action} 或 {success: False, error: ...}
        """
        if retention_window_days <= 0:
            return {
                "success": False,
                "error": "retention_window_days must be positive",
                "reason": make_evidence_reason(
                    EvidenceErrorCode.RETENTION_WINDOW_INVALID,
                    days=retention_window_days,
                ).to_dict(),
            }

        scope = "workspace" if workspace_id is not None else "global"
        now = time.time()

        try:
            cur = self.conn.execute(
                "SELECT id FROM evidence_retention_config "
                "WHERE scope = ? AND "
                + ("workspace_id = ?" if workspace_id is not None else "workspace_id IS NULL"),
                (scope, workspace_id) if workspace_id is not None else (scope,),
            )
            existing = cur.fetchone()

            if existing:
                self.conn.execute(
                    """
                    UPDATE evidence_retention_config
                    SET retention_window_days = ?, archive_location = ?,
                        archive_format = ?, auto_archive = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (retention_window_days, archive_location, archive_format,
                     1 if auto_archive else 0, now, existing["id"]),
                )
                self.conn.commit()
                return {"success": True, "id": existing["id"], "action": "updated"}
            else:
                cur = self.conn.execute(
                    """
                    INSERT INTO evidence_retention_config
                        (scope, workspace_id, retention_window_days,
                         archive_location, archive_format, auto_archive,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (scope, workspace_id, retention_window_days,
                     archive_location, archive_format, 1 if auto_archive else 0,
                     now, now),
                )
                self.conn.commit()
                return {"success": True, "id": cur.lastrowid, "action": "inserted"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============================================
    # Verifier_Registry 查询
    # ============================================

    def list_verifiers(
        self, trust_status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """查询 Verifier_Registry（只读，可走 MCP）。

        Args:
            trust_status: 过滤信任状态（trusted/revoked），None=全部

        Returns:
            Verifier 注册信息列表
        """
        sql = "SELECT * FROM verifier_registry"
        params: list = []
        if trust_status:
            sql += " WHERE trust_status = ?"
            params.append(trust_status)
        sql += " ORDER BY name ASC, version ASC"
        try:
            cur = self.conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
        except Exception:
            return []

    def list_revocation_records(
        self, verifier_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """查询 Verifier 撤销记录（只读，可走 MCP）。"""
        sql = "SELECT * FROM verifier_revocation_records"
        params: list = []
        if verifier_name:
            sql += " WHERE verifier_name = ?"
            params.append(verifier_name)
        sql += " ORDER BY revocation_time ASC"
        try:
            cur = self.conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
        except Exception:
            return []
