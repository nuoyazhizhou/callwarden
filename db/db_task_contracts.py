"""
db_task_contracts.py
====================

P1 版本化 Canonical Envelope Mixin（Task Contracts Mixin）。

实现 Requirements 1.1 / 2.1–2.11 / 5.4 / 7.4 / 7.9 / 7.11–7.16：
- Envelope_Parser / Envelope_Printer：结构化 Envelope 与 UTF-8 Canonical 序列化
- Contract_Hash：sha256(canonical_envelope_without_hash)，排除 hash 自身与纯展示字段
- profile 校验：research / design / code_change / high_risk / review 各自必填字段
- declarative / executable clause 分类：executable 缺字段降级为 declarative
- publish_revision：revision 单调递增，写入 task_contract_revisions 表
- 空 Allowed_Edit_Scope 三分支（Requirements 7.11–7.14）：
  * code_change / high_risk 空 scope → 拒绝发布，保留上一已接受 revision
  * research / design / review 空 scope → 记 unscoped，返回非阻断 Structured_Warning
  * 存量任务无 target → scope_migration_pending
- Structured_Warning（Requirements 7.15–7.17）：稳定警告码 + i18n key，非阻断
- Structured_Reason（Requirement 1.12）：稳定错误码 + i18n key，fail closed

i18n 归属：错误/警告码的 i18n 词条由任务 4.10 统一收录到 i18n/zh_CN.json 与
i18n/en_US.json；在此之前由本模块捆绑的双语默认解析（同 experiments/blind_review_views 模式）。

所有权：本文件为任务 4.2 唯一可修改文件。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..i18n import t


# ---------------------------------------------------------------------------
# 常量：profile 与 scope 分类
# ---------------------------------------------------------------------------

# 5 个合法 profile（Requirements 5.6–5.10 / 设计 §6.3）
VALID_PROFILES: Tuple[str, ...] = (
    "research",
    "design",
    "code_change",
    "high_risk",
    "review",
)

# 空 scope 时拒绝发布的 profile（Requirement 7.11）
SCOPE_REQUIRED_PROFILES: Tuple[str, ...] = ("code_change", "high_risk")

# 空 scope 时记 unscoped 的 profile（Requirement 7.12）
UNSCOPED_PROFILES: Tuple[str, ...] = ("research", "design", "review")

# scope 分类标签
SCOPE_LABEL_EXPLICIT = "explicit"
SCOPE_LABEL_UNSCOPED = "unscoped"
SCOPE_LABEL_MIGRATION_PENDING = "scope_migration_pending"

# 纯展示字段：不参与 Contract_Hash 计算（Requirement 2.8）
# contract_hash 自身也排除（Requirement 2.8 明确排除 hash 自身）
# created_at / created_by 是发布元数据，不是契约语义字段
_PRESENTATION_FIELDS: Tuple[str, ...] = ("contract_hash", "created_at", "created_by")

# executable clause 必填字段（Requirement 2.10）
EXECUTABLE_CLAUSE_REQUIRED_FIELDS: Tuple[str, ...] = (
    "clause_id",
    "subject",
    "operator",
    "expected",
    "verifier",
    "freshness",
    "severity",
)


# ---------------------------------------------------------------------------
# 错误码与 i18n key 目录（Requirement 1.12）
# ---------------------------------------------------------------------------


class ContractErrorCode:
    """P1 Envelope 发布/解析失败路径的稳定错误码目录（Requirement 1.12）。

    错误码是稳定常量，**不随消息文案变化**；文案本地化由 i18n key 承载。
    本目录与 i18n catalog 的 errors.contract_* 命名空间对应。
    """

    # Envelope grammar 错误：字段缺失、类型错误、结构非法（Req 2.2）
    ENVELOPE_GRAMMAR = "CONTRACT_ENVELOPE_GRAMMAR"
    # profile 非法：不在 5 个合法 profile 中，或在 Profile_Policy_Matrix 之外（Req 2.2, 5.11）
    INVALID_PROFILE = "CONTRACT_INVALID_PROFILE"
    # executable clause 缺字段（Req 2.10）
    EXECUTABLE_CLAUSE_INCOMPLETE = "CONTRACT_EXECUTABLE_CLAUSE_INCOMPLETE"
    # revision 非单调递增：新 revision 不大于已发布最大 revision（Req 2.7）
    REVISION_NOT_MONOTONIC = "CONTRACT_REVISION_NOT_MONOTONIC"
    # contract_hash 校验失败：revision 相同但 hash 不同，或 hash 相同但 revision 异常（Req 2.9）
    HASH_REVISION_MISMATCH = "CONTRACT_HASH_REVISION_MISMATCH"
    # 空 Allowed_Edit_Scope 拒绝发布：code_change/high_risk profile（Req 7.11）
    EMPTY_SCOPE_REJECTED = "CONTRACT_EMPTY_SCOPE_REJECTED"
    # verifier 身份缺失或不可信（Req 6.11–6.12）
    VERIFIER_NOT_TRUSTED = "CONTRACT_VERIFIER_NOT_TRUSTED"
    # freshness 规则缺失或非法（Req 2.10）
    FRESHNESS_RULE_INVALID = "CONTRACT_FRESHNESS_RULE_INVALID"
    # 数据库写入失败
    PERSISTENCE_FAILED = "CONTRACT_PERSISTENCE_FAILED"


# 警告码（与错误码同目录，但语义为非阻断）
class ContractWarningCode:
    """P1 Envelope 发布非阻断警告的稳定警告码目录（Requirements 7.15–7.17）。"""

    # research/design/review profile 空 scope 记 unscoped（Req 7.15）
    UNSCOPED_PUBLICATION = "CONTRACT_WARN_UNSCOPED_PUBLICATION"


# 错误码 → i18n key（完整路径，位于 errors.* 命名空间）
CONTRACT_I18N_KEYS: Dict[str, str] = {
    ContractErrorCode.ENVELOPE_GRAMMAR: "errors.contract_envelope_grammar",
    ContractErrorCode.INVALID_PROFILE: "errors.contract_invalid_profile",
    ContractErrorCode.EXECUTABLE_CLAUSE_INCOMPLETE: "errors.contract_executable_clause_incomplete",
    ContractErrorCode.REVISION_NOT_MONOTONIC: "errors.contract_revision_not_monotonic",
    ContractErrorCode.HASH_REVISION_MISMATCH: "errors.contract_hash_revision_mismatch",
    ContractErrorCode.EMPTY_SCOPE_REJECTED: "errors.contract_empty_scope_rejected",
    ContractErrorCode.VERIFIER_NOT_TRUSTED: "errors.contract_verifier_not_trusted",
    ContractErrorCode.FRESHNESS_RULE_INVALID: "errors.contract_freshness_rule_invalid",
    ContractErrorCode.PERSISTENCE_FAILED: "errors.contract_persistence_failed",
}

# 警告码 → i18n key（位于 warnings.* 命名空间）
CONTRACT_WARNING_I18N_KEYS: Dict[str, str] = {
    ContractWarningCode.UNSCOPED_PUBLICATION: "warnings.contract_unscoped_publication",
}

# 捆绑的双语默认文案：i18n catalog 尚未收录对应 key 时（4.10 之前），
# 仍可按当前语言解析。文案变化不改变错误码/警告码（Requirement 1.12）。
_CONTRACT_BUNDLED_DEFAULTS: Dict[str, Dict[str, str]] = {
    "errors.contract_envelope_grammar": {
        "zh_CN": "Envelope 语法错误（字段：{field}，原因：{reason}）；解析失败并拒绝发布（Requirement 2.2）。",
        "en_US": "Envelope grammar error (field: {field}, reason: {reason}); parsing failed and publication rejected (Requirement 2.2).",
    },
    "errors.contract_invalid_profile": {
        "zh_CN": "非法 profile '{profile}'；不在 Profile_Policy_Matrix 中，拒绝评估（Requirement 5.11）。",
        "en_US": "Invalid profile '{profile}'; not in Profile_Policy_Matrix, evaluation rejected (Requirement 5.11).",
    },
    "errors.contract_executable_clause_incomplete": {
        "zh_CN": "executable clause '{clause_id}' 缺少必填字段 '{missing_field}'；降级为 declarative 并排除自动 PASS 生成（Requirement 2.10）。",
        "en_US": "Executable clause '{clause_id}' missing required field '{missing_field}'; downgraded to declarative and excluded from automatic PASS generation (Requirement 2.10).",
    },
    "errors.contract_revision_not_monotonic": {
        "zh_CN": "revision {new_revision} 不大于已发布最大 revision {max_revision}（contract_id={contract_id}）；拒绝发布并保留上一已接受 revision（Requirement 2.7）。",
        "en_US": "Revision {new_revision} is not greater than the maximum published revision {max_revision} (contract_id={contract_id}); publication rejected and previous accepted revision preserved (Requirement 2.7).",
    },
    "errors.contract_hash_revision_mismatch": {
        "zh_CN": "contract_hash 与 revision 关系异常（contract_id={contract_id}, revision={revision}）；记录判为 invalid 并阻断条款满足（Requirement 2.9）。",
        "en_US": "Contract hash and revision relation is abnormal (contract_id={contract_id}, revision={revision}); record classified as invalid and clause satisfaction blocked (Requirement 2.9).",
    },
    "errors.contract_empty_scope_rejected": {
        "zh_CN": "profile '{profile}' 的 Allowed_Edit_Scope 为空集（无 file 且无 symbol）；拒绝发布并保留上一已接受 revision（Requirement 7.11）。",
        "en_US": "Allowed_Edit_Scope for profile '{profile}' is an empty set (no file and no symbol); publication rejected and previous accepted revision preserved (Requirement 7.11).",
    },
    "errors.contract_verifier_not_trusted": {
        "zh_CN": "verifier '{name}' version '{version}' config_hash '{config_hash}' 不在 Verifier_Registry 中或 trust_status 非 trusted；Evidence 判为 invalid（Requirement 6.12）。",
        "en_US": "Verifier '{name}' version '{version}' config_hash '{config_hash}' is not in Verifier_Registry or trust_status is not trusted; Evidence classified as invalid (Requirement 6.12).",
    },
    "errors.contract_freshness_rule_invalid": {
        "zh_CN": "clause '{clause_id}' 的 freshness 规则 '{freshness}' 非法或缺失；降级为 declarative（Requirement 2.10）。",
        "en_US": "Freshness rule '{freshness}' of clause '{clause_id}' is invalid or missing; downgraded to declarative (Requirement 2.10).",
    },
    "errors.contract_persistence_failed": {
        "zh_CN": "Envelope 持久化失败（contract_id={contract_id}, revision={revision}）：{detail}。",
        "en_US": "Envelope persistence failed (contract_id={contract_id}, revision={revision}): {detail}.",
    },
    "warnings.contract_unscoped_publication": {
        "zh_CN": "Envelope scope 为 unscoped（profile={profile}）；任何磁盘文件改动都会让 Completion_Gate 与 Apply_Gate 按 Requirements 1.6 与 7.12 阻断该任务。在 task step 上声明 target_file 或 target_symbol 即可建立显式 scope。",
        "en_US": "Envelope scope is unscoped (profile={profile}); any on-disk file change will make Completion_Gate and Apply_Gate block the task under Requirements 1.6 and 7.12. Declaring target_file or target_symbol on a task step establishes an explicit scope.",
    },
}


def _resolve_contract_message(message_key: str, context: Dict[str, Any]) -> str:
    """把 i18n key 解析为当前语言的可读消息（Requirement 1.12）。

    解析顺序：1) 项目 i18n catalog（任务 4.10 补齐后优先生效）；
    2) 本模块捆绑的双语默认（按当前语言，再退回 en_US）；3) key 本身。
    """
    lang = "en_US"
    catalog_text: Optional[str] = None
    try:
        from ..i18n import get_language

        lang = get_language() or "en_US"
        catalog_text = t(message_key, default=None)
    except Exception:
        catalog_text = None

    if isinstance(catalog_text, str) and catalog_text and catalog_text != message_key:
        template = catalog_text
    else:
        bundle = _CONTRACT_BUNDLED_DEFAULTS.get(message_key)
        if bundle:
            template = bundle.get(lang) or bundle.get("en_US") or message_key
        else:
            template = message_key

    try:
        return template.format(**context)
    except (KeyError, ValueError, IndexError):
        return template


@dataclass
class ContractStructuredReason:
    """结构化失败原因（Requirement 1.12）。

    code        —— 稳定错误码（ContractErrorCode 之一），不随文案变化；
    message_key —— 可在 zh_CN/en_US 解析的 i18n key；
    context     —— 占位符上下文；
    severity    —— "error"（失败路径，伴随拒绝）/ "warning"（非阻断提示）。
    """

    code: str
    message_key: str
    context: Dict[str, Any] = field(default_factory=dict)
    severity: str = "error"

    def message(self) -> str:
        """解析当前语言下的可读消息。"""
        return _resolve_contract_message(self.message_key, self.context)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message_key": self.message_key,
            "message": self.message(),
            "context": dict(self.context),
            "severity": self.severity,
        }


def make_contract_reason(code: str, severity: str = "error", **context: Any) -> ContractStructuredReason:
    """按错误码构造 ContractStructuredReason，自动绑定对应 i18n key。"""
    if code not in CONTRACT_I18N_KEYS:
        raise KeyError(f"未登记的契约错误码: {code}（必须先在 CONTRACT_I18N_KEYS 注册）")
    return ContractStructuredReason(
        code=code,
        message_key=CONTRACT_I18N_KEYS[code],
        context=dict(context),
        severity=severity,
    )


def make_contract_warning(code: str, **context: Any) -> ContractStructuredReason:
    """按警告码构造非阻断 ContractStructuredReason（severity=warning）。"""
    if code not in CONTRACT_WARNING_I18N_KEYS:
        raise KeyError(f"未登记的契约警告码: {code}（必须先在 CONTRACT_WARNING_I18N_KEYS 注册）")
    return ContractStructuredReason(
        code=code,
        message_key=CONTRACT_WARNING_I18N_KEYS[code],
        context=dict(context),
        severity="warning",
    )


class ContractPublicationError(Exception):
    """携带 ContractStructuredReason 的 Envelope 发布异常（fail closed）。"""

    def __init__(self, reason: ContractStructuredReason):
        self.reason = reason
        super().__init__(f"[{reason.code}] {reason.message()}")


# ---------------------------------------------------------------------------
# Envelope 数据结构
# ---------------------------------------------------------------------------


@dataclass
class AllowedEditScope:
    """Allowed_Edit_Scope：从 task_steps.target_file/target_symbol 派生的编辑范围。

    Requirement 7.4：files 与 symbols 从 task_steps 派生，并在 Envelope 中记录
    contract-time source 值。generated_from 记录来源字段名。
    """

    files: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    generated_from: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """scope 是否为空集（无 file 且无 symbol）。"""
        return len(self.files) == 0 and len(self.symbols) == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "files": list(self.files),
            "symbols": list(self.symbols),
            "generated_from": list(self.generated_from),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AllowedEditScope":
        if data is None:
            return cls()
        if isinstance(data, dict):
            return cls(
                files=list(data.get("files", []) or []),
                symbols=list(data.get("symbols", []) or []),
                generated_from=list(data.get("generated_from", []) or []),
            )
        raise ValueError(f"AllowedEditScope.from_dict 期望 dict，实际 {type(data)}")


@dataclass
class AcceptanceClause:
    """验收条款：declarative 或 executable（Requirement 2.10–2.11）。

    executable clause 必须含 clause_id/subject/operator/expected/verifier/freshness/severity；
    缺任一字段则降级为 declarative（Requirement 2.11），不参与自动 PASS 生成。
    """

    clause_id: str = ""
    kind: str = "declarative"  # "declarative" / "executable"
    statement: str = ""  # declarative 用
    subject: str = ""  # executable 用
    operator: str = ""  # executable 用
    expected: Any = None  # executable 用
    verifier: Dict[str, str] = field(default_factory=dict)  # {name, version, config_hash}
    freshness: str = ""  # executable 用
    severity: str = "block"  # "block" / "warn"
    # 保留原始字段以便 round-trip
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "clause_id": self.clause_id,
            "kind": self.kind,
            "severity": self.severity,
        }
        if self.kind == "declarative":
            result["statement"] = self.statement
        else:
            result["subject"] = self.subject
            result["operator"] = self.operator
            result["expected"] = self.expected
            result["verifier"] = dict(self.verifier)
            result["freshness"] = self.freshness
        # extra 字段按 key 排序追加（保证 canonical 稳定）
        for k in sorted(self.extra.keys()):
            result[k] = self.extra[k]
        return result

    @classmethod
    def from_dict(cls, data: Any) -> "AcceptanceClause":
        if not isinstance(data, dict):
            raise ValueError(f"AcceptanceClause.from_dict 期望 dict，实际 {type(data)}")
        clause_id = str(data.get("clause_id", ""))
        kind = str(data.get("kind", "declarative"))
        severity = str(data.get("severity", "block"))
        statement = str(data.get("statement", ""))
        subject = str(data.get("subject", ""))
        operator = str(data.get("operator", ""))
        expected = data.get("expected")
        verifier = dict(data.get("verifier", {}) or {})
        freshness = str(data.get("freshness", ""))
        # 收集非标准字段
        known_keys = {
            "clause_id", "kind", "severity", "statement", "subject",
            "operator", "expected", "verifier", "freshness",
        }
        extra = {k: v for k, v in data.items() if k not in known_keys}
        return cls(
            clause_id=clause_id, kind=kind, severity=severity, statement=statement,
            subject=subject, operator=operator, expected=expected,
            verifier=verifier, freshness=freshness, extra=extra,
        )


@dataclass
class Envelope:
    """Canonical Envelope（Requirements 2.1–2.11）。

    结构化 Envelope，可通过 to_canonical_bytes 序列化为 UTF-8 Canonical 字节，
    通过 from_dict 从解析后的 dict 构造。
    """

    contract_id: str = ""
    revision: int = 0
    contract_hash: str = ""  # 发布时计算，不参与 hash 输入
    profile: str = ""
    created_at: float = 0.0  # 纯展示字段，不参与 hash
    created_by: str = ""  # 纯展示字段，不参与 hash
    objective: Dict[str, Any] = field(default_factory=dict)
    interfaces: Dict[str, Any] = field(default_factory=dict)
    allowed_edit_scope: AllowedEditScope = field(default_factory=AllowedEditScope)
    acceptance_clauses: List[AcceptanceClause] = field(default_factory=list)
    risks: List[Dict[str, Any]] = field(default_factory=list)
    rollback: Dict[str, Any] = field(default_factory=dict)
    dependencies: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_hash: bool = True) -> Dict[str, Any]:
        """转换为 dict。include_hash=False 时排除 contract_hash（用于 hash 计算）。"""
        result: Dict[str, Any] = {
            "contract_id": self.contract_id,
            "revision": self.revision,
            "profile": self.profile,
            "objective": _normalize_dict_for_canonical(self.objective),
            "interfaces": _normalize_dict_for_canonical(self.interfaces),
            "allowed_edit_scope": self.allowed_edit_scope.to_dict(),
            "acceptance_clauses": [
                _normalize_for_canonical(c.to_dict()) for c in self.acceptance_clauses
            ],
            "risks": [_normalize_for_canonical(r) for r in self.risks],
            "rollback": _normalize_dict_for_canonical(self.rollback),
            "dependencies": _normalize_dict_for_canonical(self.dependencies),
        }
        if include_hash and self.contract_hash:
            result["contract_hash"] = self.contract_hash
        if self.created_at:
            result["created_at"] = self.created_at
        if self.created_by:
            result["created_by"] = self.created_by
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Envelope":
        """从 dict 构造 Envelope（Envelope_Parser，Requirement 2.1）。"""
        if not isinstance(data, dict):
            raise ValueError(f"Envelope.from_dict 期望 dict，实际 {type(data)}")
        return cls(
            contract_id=str(data.get("contract_id", "")),
            revision=int(data.get("revision", 0)),
            contract_hash=str(data.get("contract_hash", "")),
            profile=str(data.get("profile", "")),
            created_at=float(data.get("created_at", 0.0)),
            created_by=str(data.get("created_by", "")),
            objective=dict(data.get("objective", {}) or {}),
            interfaces=dict(data.get("interfaces", {}) or {}),
            allowed_edit_scope=AllowedEditScope.from_dict(data.get("allowed_edit_scope")),
            acceptance_clauses=[
                AcceptanceClause.from_dict(c) for c in (data.get("acceptance_clauses") or [])
            ],
            risks=list(data.get("risks", []) or []),
            rollback=dict(data.get("rollback", {}) or {}),
            dependencies=dict(data.get("dependencies", {}) or {}),
        )


# ---------------------------------------------------------------------------
# Canonical 序列化（Requirement 2.3–2.5）
# ---------------------------------------------------------------------------


def _normalize_path(p: str) -> str:
    """路径规范化：统一为正斜杠（Requirement 2.3 normalized paths）。"""
    if not isinstance(p, str):
        return p
    return p.replace("\\", "/")


def _normalize_for_canonical(value: Any) -> Any:
    """递归规范化值用于 Canonical 序列化。

    - dict：key 排序，递归规范化值
    - list：元素递归规范化后按 canonical json 排序（稳定数组排序，Req 2.3）
    - str：路径规范化（正斜杠）
    - 其他：原样返回

    注意：list 的稳定排序用 canonical json 字符串作为排序键，确保任意嵌套结构
    都有确定性顺序（Requirement 2.3 deterministic array ordering rules）。
    """
    if isinstance(value, dict):
        return {k: _normalize_for_canonical(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        normalized_items = [_normalize_for_canonical(v) for v in value]
        # 按 canonical json 字符串排序，确保嵌套结构也有确定性顺序
        try:
            normalized_items.sort(
                key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            )
        except (TypeError, ValueError):
            # 不可排序的元素（如混合类型）保持原序
            pass
        return normalized_items
    if isinstance(value, str):
        return _normalize_path(value)
    return value


def _normalize_dict_for_canonical(value: Dict[str, Any]) -> Dict[str, Any]:
    """dict 专用规范化包装。"""
    return _normalize_for_canonical(value) if isinstance(value, dict) else value


def envelope_to_canonical_bytes(envelope: Envelope, exclude_hash: bool = True) -> bytes:
    """Envelope_Printer：序列化为 UTF-8 Canonical 字节（Requirement 2.3）。

    - sort_keys=True：确定性字段排序
    - ensure_ascii=False：UTF-8 稳定序列化
    - separators=(',', ':')：最紧凑形式，消除空白差异
    - exclude_hash=True：排除 contract_hash 自身（Requirement 2.8）
    - 排除纯展示字段 created_at/created_by（Requirement 2.8）

    Requirement 2.4：print → parse → print → parse 产生两个语义等价的
    structured Envelope 与两个相同的 canonical 字节序列。
    """
    data = envelope.to_dict(include_hash=not exclude_hash)
    # 排除纯展示字段（Requirement 2.8）
    for f in _PRESENTATION_FIELDS:
        if f == "contract_hash" and exclude_hash:
            data.pop(f, None)
        elif f in ("created_at", "created_by"):
            data.pop(f, None)
    normalized = _normalize_for_canonical(data)
    return json.dumps(
        normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def compute_contract_hash(envelope: Envelope) -> str:
    """计算 Contract_Hash（Requirement 2.8）。

    sha256(canonical_envelope_without_hash)：
    - 排除 contract_hash 自身
    - 排除纯展示字段 created_at / created_by
    - 字段排序、路径规范化、数组稳定排序
    """
    canonical_bytes = envelope_to_canonical_bytes(envelope, exclude_hash=True)
    return "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()


# ---------------------------------------------------------------------------
# Profile 校验（Requirements 5.4, 5.6–5.10）
# ---------------------------------------------------------------------------


# 每个 profile 的必填 Envelope 字段（Requirements 5.6–5.10 / 设计 §6.3）
_PROFILE_REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    "research": ("objective", "interfaces"),  # 问题、来源边界、结论、不确定性
    "design": ("objective", "interfaces", "risks", "rollback"),  # 目标/非目标、接口、权衡、风险、迁移/回滚
    "code_change": ("objective", "interfaces", "allowed_edit_scope", "acceptance_clauses"),
    "high_risk": ("objective", "interfaces", "allowed_edit_scope", "acceptance_clauses", "risks", "rollback"),
    "review": ("objective", "acceptance_clauses"),  # 被审对象、review scope、verdict 格式
}


def validate_profile(envelope: Envelope) -> Optional[ContractStructuredReason]:
    """校验 profile 合法性与必填字段（Requirements 5.4, 5.11）。

    返回 None 表示通过；返回 ContractStructuredReason 表示失败（fail closed）。
    """
    if envelope.profile not in VALID_PROFILES:
        return make_contract_reason(
            ContractErrorCode.INVALID_PROFILE, profile=envelope.profile
        )
    required = _PROFILE_REQUIRED_FIELDS.get(envelope.profile, ())
    for field_name in required:
        value = getattr(envelope, field_name, None)
        if value is None:
            return make_contract_reason(
                ContractErrorCode.ENVELOPE_GRAMMAR,
                field=field_name,
                reason=f"profile '{envelope.profile}' requires '{field_name}' but it is None",
            )
        # 检查空值
        if isinstance(value, (list, dict, str)) and not value:
            return make_contract_reason(
                ContractErrorCode.ENVELOPE_GRAMMAR,
                field=field_name,
                reason=f"profile '{envelope.profile}' requires non-empty '{field_name}'",
            )
    return None


# ---------------------------------------------------------------------------
# Clause 分类校验（Requirements 2.10–2.11）
# ---------------------------------------------------------------------------


def classify_clause(clause: AcceptanceClause) -> Tuple[AcceptanceClause, Optional[ContractStructuredReason]]:
    """分类 clause 为 declarative 或 executable（Requirements 2.10–2.11）。

    若 clause 声明为 executable 但缺少必填字段，降级为 declarative 并返回
    非阻断 reason（用于审计记录）。注意：降级不是拒绝发布，只是排除自动 PASS 生成。

    返回 (possibly_modified_clause, reason_or_none)。
    """
    if clause.kind != "executable":
        return clause, None

    # 检查 executable 必填字段（Requirement 2.10）
    for field_name in EXECUTABLE_CLAUSE_REQUIRED_FIELDS:
        value = getattr(clause, field_name, None)
        if field_name == "verifier":
            if not clause.verifier or not clause.verifier.get("name"):
                # 降级为 declarative（Requirement 2.11）
                downgraded = AcceptanceClause(
                    clause_id=clause.clause_id, kind="declarative",
                    statement=clause.statement or clause.subject,
                    severity=clause.severity, extra=clause.extra,
                )
                return downgraded, make_contract_reason(
                    ContractErrorCode.EXECUTABLE_CLAUSE_INCOMPLETE,
                    clause_id=clause.clause_id,
                    missing_field="verifier",
                )
        elif not value:
            # 降级为 declarative（Requirement 2.11）
            downgraded = AcceptanceClause(
                clause_id=clause.clause_id, kind="declarative",
                statement=clause.statement or clause.subject,
                severity=clause.severity, extra=clause.extra,
            )
            return downgraded, make_contract_reason(
                ContractErrorCode.EXECUTABLE_CLAUSE_INCOMPLETE,
                clause_id=clause.clause_id,
                missing_field=field_name,
            )

    return clause, None


def classify_all_clauses(envelope: Envelope) -> Tuple[List[AcceptanceClause], List[ContractStructuredReason]]:
    """对 Envelope 所有 clause 执行分类，返回 (classified_clauses, downgrade_reasons)。

    降级 reason 是非阻断的审计记录（Requirement 2.11），不阻止发布。
    """
    classified: List[AcceptanceClause] = []
    reasons: List[ContractStructuredReason] = []
    for clause in envelope.acceptance_clauses:
        c, r = classify_clause(clause)
        classified.append(c)
        if r is not None:
            reasons.append(r)
    return classified, reasons


# ---------------------------------------------------------------------------
# 空 Allowed_Edit_Scope 三分支（Requirements 7.11–7.14）
# ---------------------------------------------------------------------------


def derive_scope_from_steps(
    conn: sqlite3.Connection, task_id: str
) -> AllowedEditScope:
    """从 task_steps.target_file/target_symbol 派生 Allowed_Edit_Scope（Requirement 7.4）。

    返回的 scope 记录 generated_from 来源字段。
    """
    scope = AllowedEditScope(generated_from=["task_steps.target_file", "task_steps.target_symbol"])
    try:
        cur = conn.execute(
            "SELECT DISTINCT target_file FROM task_steps WHERE task_id=? AND target_file IS NOT NULL AND target_file != ''",
            (task_id,),
        )
        scope.files = [row[0] for row in cur.fetchall() if row[0]]
    except sqlite3.OperationalError:
        # task_steps 表可能不存在 target_file 列
        pass
    try:
        cur = conn.execute(
            "SELECT DISTINCT target_symbol FROM task_steps WHERE task_id=? AND target_symbol IS NOT NULL AND target_symbol != ''",
            (task_id,),
        )
        scope.symbols = [row[0] for row in cur.fetchall() if row[0]]
    except sqlite3.OperationalError:
        pass
    return scope


def classify_scope(
    envelope: Envelope, existing_steps_have_target: bool
) -> Tuple[str, Optional[ContractStructuredReason], Optional[ContractStructuredReason]]:
    """空 Allowed_Edit_Scope 三分支分类（Requirements 7.11–7.14）。

    返回 (scope_label, reject_reason, warning)：
    - scope_label: "explicit" / "unscoped" / "scope_migration_pending"
    - reject_reason: 非 None 时拒绝发布（code_change/high_risk 空 scope，7.11）
    - warning: 非 None 时非阻断警告（research/design/review 空 scope，7.15）

    三分支优先级（scope 为空集时）：
    1. code_change / high_risk → 拒绝发布，保留上一已接受 revision（7.11）
    2. research / design / review + 既有 step 无 target → scope_migration_pending（7.13）；
       发布期不拒绝，但记录标签并返回非阻断警告；Apply_Gate 在 7.14 拒绝 task_apply
    3. research / design / review + 既有 step 有 target → unscoped（7.12）；
       发布期不拒绝，返回非阻断警告（7.15）
    """
    scope = envelope.allowed_edit_scope

    # 非空 scope：显式
    if not scope.is_empty():
        return SCOPE_LABEL_EXPLICIT, None, None

    # 空 scope 分支
    if envelope.profile in SCOPE_REQUIRED_PROFILES:
        # 分支 1：code_change / high_risk 空 scope → 拒绝发布（7.11）
        return SCOPE_LABEL_UNSCOPED, make_contract_reason(
            ContractErrorCode.EMPTY_SCOPE_REJECTED, profile=envelope.profile
        ), None

    if envelope.profile in UNSCOPED_PROFILES:
        # 非阻断警告（7.15）：适用于 research/design/review 空 scope
        warning = make_contract_warning(
            ContractWarningCode.UNSCOPED_PUBLICATION, profile=envelope.profile
        )
        if not existing_steps_have_target:
            # 分支 2：既有 step 无 target → scope_migration_pending（7.13）
            # 发布期不拒绝，但 Apply_Gate 在 7.14 拒绝 task_apply
            return SCOPE_LABEL_MIGRATION_PENDING, None, warning
        # 分支 3：既有 step 有 target 但派生 scope 为空 → unscoped（7.12）
        return SCOPE_LABEL_UNSCOPED, None, warning

    # 不应到达（profile 已校验）
    return SCOPE_LABEL_UNSCOPED, None, None


# ---------------------------------------------------------------------------
# Revision 发布（Requirements 2.6–2.9）
# ---------------------------------------------------------------------------


def get_max_published_revision(conn: sqlite3.Connection, contract_id: str) -> int:
    """获取 contract_id 的已发布最大 revision（Requirement 2.7）。

    无记录时返回 0。
    """
    try:
        cur = conn.execute(
            "SELECT MAX(revision) FROM task_contract_revisions WHERE contract_id=?",
            (contract_id,),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0


def get_current_envelope(conn: sqlite3.Connection, contract_id: str) -> Optional[Envelope]:
    """获取 contract_id 的当前（最大 revision）Envelope。"""
    try:
        cur = conn.execute(
            "SELECT envelope_payload, revision, contract_hash, profile, task_id, created_at, created_by "
            "FROM task_contract_revisions WHERE contract_id=? ORDER BY revision DESC LIMIT 1",
            (contract_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        envelope = Envelope.from_dict(payload)
        envelope.revision = int(row[1])
        envelope.contract_hash = str(row[2])
        envelope.profile = str(row[3])
        envelope.created_at = float(row[5])
        envelope.created_by = str(row[6])
        return envelope
    except (sqlite3.OperationalError, json.JSONDecodeError):
        return None


def _existing_steps_have_target(conn: sqlite3.Connection, task_id: str) -> bool:
    """检查任务既有 step 是否携带 target_file 或 target_symbol（Requirement 7.13）。"""
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM task_steps WHERE task_id=? AND "
            "(target_file IS NOT NULL AND target_file != '' OR "
            " target_symbol IS NOT NULL AND target_symbol != '')",
            (task_id,),
        )
        return int(cur.fetchone()[0]) > 0
    except sqlite3.OperationalError:
        return False


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


class TaskContractsMixin:
    """P1 版本化 Canonical Envelope Mixin。

    提供 Envelope 的解析、规范化、hash 计算、profile 校验、clause 分类、
    revision 发布与空 scope 三分支处理。所有写操作通过 CLI 调用（避免与
    MCP 长连接撞锁），只读查询可走 MCP。

    依赖 4.1 已创建的 task_contract_revisions 表（schema v43）。
    """

    def parse_envelope(self, data: Dict[str, Any]) -> Envelope:
        """Envelope_Parser：从 dict 解析为结构化 Envelope（Requirement 2.1）。

        若违反 grammar 或 profile，抛出 ContractPublicationError（Req 2.2）。
        """
        envelope = Envelope.from_dict(data)

        # grammar 基础校验
        if not envelope.contract_id:
            raise ContractPublicationError(make_contract_reason(
                ContractErrorCode.ENVELOPE_GRAMMAR,
                field="contract_id",
                reason="contract_id is required",
            ))
        if envelope.revision <= 0:
            raise ContractPublicationError(make_contract_reason(
                ContractErrorCode.ENVELOPE_GRAMMAR,
                field="revision",
                reason="revision must be a positive integer",
            ))

        # profile 校验（fail closed）
        profile_reason = validate_profile(envelope)
        if profile_reason is not None:
            raise ContractPublicationError(profile_reason)

        # clause 分类（降级非阻断，但记录审计 reason）
        classified, downgrade_reasons = classify_all_clauses(envelope)
        envelope.acceptance_clauses = classified
        # 降级 reason 附加到 envelope 的 extra 信息（非阻断，供审计）
        if downgrade_reasons:
            envelope.dependencies = dict(envelope.dependencies or {})
            envelope.dependencies["_clause_downgrades"] = [r.to_dict() for r in downgrade_reasons]

        return envelope

    def print_envelope(self, envelope: Envelope, exclude_hash: bool = False) -> str:
        """Envelope_Printer：输出 UTF-8 Canonical 字符串（Requirement 2.3）。

        exclude_hash=True 时排除 contract_hash（用于 hash 计算输入）。
        """
        return envelope_to_canonical_bytes(envelope, exclude_hash=exclude_hash).decode("utf-8")

    def compute_envelope_hash(self, envelope: Envelope) -> str:
        """计算 Contract_Hash（Requirement 2.8）。

        sha256(canonical_envelope_without_hash)，排除 hash 自身与纯展示字段。
        """
        return compute_contract_hash(envelope)

    def verify_hash_revision_consistency(
        self, envelope: Envelope
    ) -> Optional[ContractStructuredReason]:
        """校验 hash 与 revision 一致性（Requirement 2.9）。

        若 revision 相同但 hash 不同，或 hash 相同但 revision 关系异常，
        返回 Structured_Reason（fail closed）。
        """
        if not envelope.contract_hash:
            return None  # 未设置 hash，由发布时计算

        recomputed = compute_contract_hash(envelope)
        if envelope.contract_hash != recomputed:
            return make_contract_reason(
                ContractErrorCode.HASH_REVISION_MISMATCH,
                contract_id=envelope.contract_id,
                revision=envelope.revision,
            )
        return None

    def publish_envelope_revision(
        self,
        envelope: Envelope,
        task_id: str,
        workspace_id: Optional[int] = None,
        created_by: str = "",
    ) -> Tuple[Envelope, List[ContractStructuredReason]]:
        """发布 Envelope Revision（Requirements 2.6–2.9, 7.11–7.17）。

        流程：
        1. 校验 profile（fail closed）
        2. 校验 revision 单调递增（fail closed）
        3. clause 分类（降级非阻断）
        4. 空 scope 三分支分类
        5. 计算 contract_hash
        6. 写入 task_contract_revisions 表（不可变，append-only）

        返回 (envelope_with_hash, warnings)：
        - envelope_with_hash：已设置 contract_hash 的 Envelope
        - warnings：非阻断警告列表（如 unscoped 警告）

        失败时抛出 ContractPublicationError（fail closed）。
        """
        conn = self.conn  # CodeGraphDB 的连接

        # 1. profile 校验
        profile_reason = validate_profile(envelope)
        if profile_reason is not None:
            raise ContractPublicationError(profile_reason)

        # 2. revision 单调递增校验（Requirement 2.7）
        max_revision = get_max_published_revision(conn, envelope.contract_id)
        if envelope.revision <= max_revision:
            raise ContractPublicationError(make_contract_reason(
                ContractErrorCode.REVISION_NOT_MONOTONIC,
                new_revision=envelope.revision,
                max_revision=max_revision,
                contract_id=envelope.contract_id,
            ))

        # 3. clause 分类
        classified, downgrade_reasons = classify_all_clauses(envelope)
        envelope.acceptance_clauses = classified

        # 4. 空 scope 三分支
        existing_has_target = _existing_steps_have_target(conn, task_id)
        scope_label, reject_reason, warning = classify_scope(envelope, existing_has_target)

        if reject_reason is not None:
            # code_change/high_risk 空 scope：拒绝发布，保留上一已接受 revision（7.11）
            raise ContractPublicationError(reject_reason)

        # scope_migration_pending：发布期不拒绝，但记录标签
        # （Apply_Gate 在 7.14 拒绝 task_apply；本方法只记录，不拒绝）
        if scope_label == SCOPE_LABEL_MIGRATION_PENDING:
            envelope.dependencies = dict(envelope.dependencies or {})
            envelope.dependencies["_scope_label"] = SCOPE_LABEL_MIGRATION_PENDING
        elif scope_label == SCOPE_LABEL_UNSCOPED:
            envelope.dependencies = dict(envelope.dependencies or {})
            envelope.dependencies["_scope_label"] = SCOPE_LABEL_UNSCOPED

        # 记录降级 reason（非阻断审计）
        warnings: List[ContractStructuredReason] = list(downgrade_reasons)
        if warning is not None:
            warnings.append(warning)

        # 5. 计算 contract_hash
        envelope.contract_hash = compute_contract_hash(envelope)
        envelope.created_at = time.time()
        envelope.created_by = created_by

        # 6. 写入 task_contract_revisions（不可变，append-only）
        envelope_payload = json.dumps(envelope.to_dict(include_hash=True), sort_keys=True, ensure_ascii=False)
        try:
            conn.execute(
                """
                INSERT INTO task_contract_revisions
                    (contract_id, revision, contract_hash, profile, task_id,
                     workspace_id, envelope_payload, created_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.contract_id,
                    envelope.revision,
                    envelope.contract_hash,
                    envelope.profile,
                    task_id,
                    workspace_id,
                    envelope_payload,
                    envelope.created_at,
                    envelope.created_by,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            # UNIQUE(contract_id, revision) 冲突
            raise ContractPublicationError(make_contract_reason(
                ContractErrorCode.REVISION_NOT_MONOTONIC,
                new_revision=envelope.revision,
                max_revision=max_revision,
                contract_id=envelope.contract_id,
            )) from e
        except sqlite3.Error as e:
            raise ContractPublicationError(make_contract_reason(
                ContractErrorCode.PERSISTENCE_FAILED,
                contract_id=envelope.contract_id,
                revision=envelope.revision,
                detail=str(e),
            )) from e

        return envelope, warnings

    def get_current_envelope_for_task(self, task_id: str) -> Optional[Envelope]:
        """获取任务当前绑定的 Envelope（最大 revision）。"""
        conn = self.conn
        try:
            cur = conn.execute(
                "SELECT contract_id FROM task_contract_revisions WHERE task_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return get_current_envelope(conn, str(row[0]))
        except sqlite3.OperationalError:
            return None

    def get_envelope_revisions(self, contract_id: str) -> List[Dict[str, Any]]:
        """获取 contract_id 的所有 revision 记录（按 revision 升序）。"""
        conn = self.conn
        try:
            cur = conn.execute(
                "SELECT revision, contract_hash, profile, task_id, created_at, created_by "
                "FROM task_contract_revisions WHERE contract_id=? ORDER BY revision ASC",
                (contract_id,),
            )
            return [
                {
                    "revision": int(row[0]),
                    "contract_hash": str(row[1]),
                    "profile": str(row[2]),
                    "task_id": str(row[3]),
                    "created_at": float(row[4]),
                    "created_by": str(row[5]),
                }
                for row in cur.fetchall()
            ]
        except sqlite3.OperationalError:
            return []

    def derive_allowed_edit_scope(self, task_id: str) -> AllowedEditScope:
        """从 task_steps 派生 Allowed_Edit_Scope（Requirement 7.4）。"""
        return derive_scope_from_steps(self.conn, task_id)
