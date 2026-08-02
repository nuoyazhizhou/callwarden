"""
blind_review_views —— P0 Control/Treatment 最小披露投影（Minimal_Blind_View）。

本模块实现 Requirement 12.4 / 12.5 / 12.7 / 12.8 / 12.18 / 12.20 / 12.25 与
Requirement 13.2–13.6 的视图侧职责，以及设计 §7.5 最小披露矩阵与 §14 P0 实验边界：

- Control：Reviewer 在首轮 verdict 前即可见契约事实、代码事实与 Implementer_Notes（Req 12.4）。
- Treatment：首轮 verdict 封存前**仅**披露 Minimal_Blind_View（Req 12.5）；封存后再揭示
  Implementer_Notes，并记录 reveal 前后 verdict 是否改变及结构化原因（Req 12.7）。
- Minimal_Blind_View 只由现有字段构成（Req 12.25 / Property 24）：任务标题/描述、
  ``task_steps`` 的 target_file/target_symbol、``change_audit`` diff、``task_symbol_changes``、
  既有 ``test_runs`` 状态、open ``task_quality_findings``；同时记录披露字段清单与排除字段清单，
  并标注为**实验披露清单**（experiment_disclosure_list）而非 View_Manifest。
- 任何组/阶段都**禁止**披露隐藏推理历史、既有 reviewer verdict、reviewer 草稿等（Req 13.6、设计 7.5）。
- 盲法无法保持（reviewer 与 implementer 会话标记相同、review 期间 snapshot 漂移）→ 无效样本（Req 12.8）。
- Treatment 盲视图若泄露 Implementer_Notes/既有 verdict/敏感推理 → 披露事件（Req 12.18）。

schema 中立（Property 24 / Req 12.1）：本模块只用 stdlib，模块级**不** import db/schema；
``collect_source_facts_from_db`` 通过 duck-type 的 ``db.conn`` 做只读 SELECT，不写库、不建表。

错误码归属（经用户确认）：1.2 失败路径需要的新错误码（披露泄露 / 无效样本 / 完整性事件 /
来源缺失）在**本文件内**自建本地 reason 注册表，复用 Structured_Reason 的数据结构形状但**不**
修改 1.1 的 blind_review_protocol.py；i18n 词条由任务 1.4 统一收录，此前由本地双语默认解析。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# 包级常量：本模块产出的所有记录都是非产品 Evidence（Requirement 12.23）。
from . import NON_PRODUCT_EVIDENCE


# ---------------------------------------------------------------------------
# 本地 Structured_Reason 注册表（Requirement 1.12；错误码归属见模块 docstring）
# ---------------------------------------------------------------------------


class ViewErrorCode:
    """P0 视图/采集失败路径的稳定错误码目录（Requirement 1.12）。

    错误码是稳定常量，**不随消息文案变化**；文案本地化由 i18n key 承载，二者解耦。
    本目录与 blind_review_protocol.ExperimentErrorCode 同构但相互独立，避免修改 1.1 文件。
    """

    # Treatment 盲视图泄露 Implementer_Notes / 既有 verdict / 敏感推理（Requirement 12.18）。
    DISCLOSURE_VIOLATION = "EXP_DISCLOSURE_VIOLATION"
    # 盲法无法保持：会话标记相同或 review 期间 snapshot 漂移（Requirement 12.8）。
    INVALID_SAMPLE = "EXP_INVALID_SAMPLE"
    # 实验诱导伪造独立性或伪造证据（Requirement 12.20）。
    INTEGRITY_INCIDENT = "EXP_INTEGRITY_INCIDENT"
    # 必需来源字段缺失，无法构造最小 blind view（Requirement 12.2 排除条件）。
    VIEW_SOURCE_MISSING = "EXP_VIEW_SOURCE_MISSING"


# 错误码 → i18n key（完整路径，位于 errors.* 命名空间）。
# 词条由任务 1.4 写入 i18n/zh_CN.json 与 i18n/en_US.json；在此之前由
# _VIEW_BUNDLED_DEFAULTS 提供双语回退，使 key 在两种语言下均可解析。
VIEW_I18N_KEYS: Dict[str, str] = {
    ViewErrorCode.DISCLOSURE_VIOLATION: "errors.experiment_disclosure_violation",
    ViewErrorCode.INVALID_SAMPLE: "errors.experiment_invalid_sample",
    ViewErrorCode.INTEGRITY_INCIDENT: "errors.experiment_integrity_incident",
    ViewErrorCode.VIEW_SOURCE_MISSING: "errors.experiment_view_source_missing",
}

# 捆绑的双语默认文案：i18n catalog 尚未收录对应 key 时（1.4 之前），仍可按当前语言解析。
_VIEW_BUNDLED_DEFAULTS: Dict[str, Dict[str, str]] = {
    "errors.experiment_disclosure_violation": {
        "zh_CN": "Treatment 盲视图披露了禁止字段 '{field}'（任务 {task_id}）；触发披露事件并暂停批次（Requirement 12.18）。",
        "en_US": "A Treatment blind view disclosed prohibited field '{field}' (task {task_id}); a disclosure incident is triggered and the batch is paused (Requirement 12.18).",
    },
    "errors.experiment_invalid_sample": {
        "zh_CN": "任务 {task_id} 的盲法无法保持（原因：{reason}）；标记为无效样本并排除出效果估计（Requirement 12.8）。",
        "en_US": "Blind conditions cannot be maintained for task {task_id} (reason: {reason}); marked as an invalid sample and excluded from effect estimation (Requirement 12.8).",
    },
    "errors.experiment_integrity_incident": {
        "zh_CN": "任务 {task_id} 检测到完整性事件（原因：{reason}）；暂停批次并记录（Requirement 12.20）。",
        "en_US": "An integrity incident was detected for task {task_id} (reason: {reason}); the batch is paused and recorded (Requirement 12.20).",
    },
    "errors.experiment_view_source_missing": {
        "zh_CN": "任务 {task_id} 缺少构造最小 blind view 所需的来源字段 '{field}'；无法纳样（Requirement 12.2）。",
        "en_US": "Task {task_id} is missing source field '{field}' required to build a minimum blind view; it cannot be admitted (Requirement 12.2).",
    },
}


def _resolve_view_message(message_key: str, context: Dict[str, Any]) -> str:
    """把 i18n key 解析为当前语言的可读消息（Requirement 1.12）。

    解析顺序：1) 项目 i18n catalog（``errors.*``，任务 1.4 补齐后优先生效）；
    2) 本模块捆绑的双语默认（按当前语言，再退回 en_US）；3) key 本身。
    占位符用 ``context`` 填充；任何格式化失败都退回未格式化文本，绝不抛异常吞掉错误码。
    """
    lang = "en_US"
    catalog_text: Optional[str] = None
    try:
        # 延迟导入，避免实验包硬依赖 i18n 初始化。experiments 是 callwarden 子包，
        # 故用包相对导入；顶层绝对导入在生产环境会 ImportError 并被下方 except 静默吞掉。
        from ..i18n import t, get_language

        lang = get_language() or "en_US"
        # t(key, default=None) 在 catalog 缺失该 key 时返回 key 本身，故以“是否等于 key”判定收录。
        catalog_text = t(message_key, default=None)
    except Exception:
        catalog_text = None

    if isinstance(catalog_text, str) and catalog_text and catalog_text != message_key:
        template = catalog_text
    else:
        bundle = _VIEW_BUNDLED_DEFAULTS.get(message_key)
        if bundle:
            template = bundle.get(lang) or bundle.get("en_US") or message_key
        else:
            template = message_key

    try:
        return template.format(**context)
    except (KeyError, ValueError, IndexError):
        return template


@dataclass
class ViewStructuredReason:
    """结构化失败原因（Requirement 1.12），与 protocol.Structured_Reason 同构。

    code        —— 稳定错误码（ViewErrorCode 之一），不随文案变化；
    message_key —— 可在 zh_CN/en_US 解析的 i18n key；
    context     —— 占位符上下文（如 task_id、field、reason）；
    severity    —— "error" / "warning"；失败路径为 error。

    本对象可序列化为 dict 随实验记录留存（non_product_evidence 标记由调用方附加）。
    """

    code: str
    message_key: str
    context: Dict[str, Any] = field(default_factory=dict)
    severity: str = "error"

    def message(self) -> str:
        """解析当前语言下的可读消息。"""
        return _resolve_view_message(self.message_key, self.context)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message_key": self.message_key,
            "message": self.message(),
            "context": dict(self.context),
            "severity": self.severity,
        }


def make_view_reason(code: str, severity: str = "error", **context: Any) -> ViewStructuredReason:
    """按错误码构造 ViewStructuredReason，自动绑定对应 i18n key。

    错误码必须在 VIEW_I18N_KEYS 中登记，否则抛 KeyError——保证每个失败路径都有
    稳定错误码与可解析 i18n key（Requirement 1.12），而不是静默返回无码原因。
    """
    if code not in VIEW_I18N_KEYS:
        raise KeyError(f"未登记的视图错误码: {code}（必须先在 VIEW_I18N_KEYS 注册）")
    return ViewStructuredReason(
        code=code,
        message_key=VIEW_I18N_KEYS[code],
        context=dict(context),
        severity=severity,
    )


class ViewDisclosureError(Exception):
    """携带 ViewStructuredReason 的视图披露异常。

    失败路径不静默吞异常（项目代码规范）；调用方可从 ``reason`` 取稳定错误码与 i18n key。
    """

    def __init__(self, reason: ViewStructuredReason):
        self.reason = reason
        super().__init__(f"[{reason.code}] {reason.message()}")


# ---------------------------------------------------------------------------
# 披露字段常量（Requirement 12.25 / 设计 7.5 最小披露矩阵）
# ---------------------------------------------------------------------------


class BlindViewGroup(Enum):
    """实验分组（Requirement 12.2 / 设计 §14 P0）。"""

    CONTROL = "control"        # Reviewer 首轮 verdict 前可见 Implementer_Notes
    TREATMENT = "treatment"    # Reviewer 首轮 verdict 封存前不可见 Implementer_Notes


class BlindViewPhase(Enum):
    """披露阶段（设计 §7.6 pre_reveal / post_reveal 的 P0 对应）。"""

    PRE_VERDICT = "pre_verdict"      # 首轮 verdict 封存前
    POST_REVEAL = "post_reveal"      # 首轮 verdict 封存后揭示 Implementer_Notes


# Minimal_Blind_View 允许披露的字段（allowlist，仅由现有字段构成，Req 12.25）。
# 这是 Treatment 盲视图与 Control 视图共享的“代码/契约事实”字段集。
MINIMAL_BLIND_VIEW_FIELDS: List[str] = [
    "task_title",              # tasks.title
    "task_description",        # tasks.description
    "step_targets",            # task_steps.target_file / target_symbol
    "change_audit_diffs",      # change_audit.diff
    "symbol_changes",          # task_symbol_changes 记录
    "test_runs_status",        # 既有 test_runs 状态
    "open_quality_findings",   # open task_quality_findings
]

# Implementer_Notes 是条件披露字段：Control 始终披露；Treatment 仅 post-reveal 披露。
IMPLEMENTER_NOTES_FIELD = "implementer_notes"

# 任何组/阶段都**禁止**披露的字段（Req 13.6、设计 7.5：不传隐藏推理历史；Req 4.1 类比）。
# 这些字段永远进入 excluded_fields，永不进入 payload。
PROHIBITED_FIELDS: List[str] = [
    "hidden_reasoning_history",     # 隐藏推理历史（思维链）
    "prior_reviewer_verdicts",      # 既有 reviewer verdict
    "reviewer_drafts",              # reviewer 草稿
    "confidence_statements",        # 置信度陈述
    "suggested_review_focus",       # 建议审核重点
]

# 披露清单标注：实验披露清单，**不是** View_Manifest（Req 12.25）。
DISCLOSURE_LABEL = "experiment_disclosure_list"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class BlindViewSourceFacts:
    """构造 Minimal_Blind_View 所需的 6 组现有字段来源（Req 12.25）。

    全部为纯数据（list/dict/str），不携带任何 db 类型，保证视图逻辑可脱离数据库测试，
    且模块级不依赖 schema（Property 24）。``collect_source_facts_from_db`` 负责只读填充。
    """

    task_id: str
    task_title: str = ""
    task_description: str = ""
    step_targets: List[Dict[str, Any]] = field(default_factory=list)
    change_audit_diffs: List[Dict[str, Any]] = field(default_factory=list)
    symbol_changes: List[Dict[str, Any]] = field(default_factory=list)
    test_runs_status: List[Dict[str, Any]] = field(default_factory=list)
    open_quality_findings: List[Dict[str, Any]] = field(default_factory=list)

    def source_field(self, name: str) -> Any:
        """按 MINIMAL_BLIND_VIEW_FIELDS 字段名取对应来源值。"""
        return getattr(self, name)


@dataclass
class MinimalBlindView:
    """P0 最小盲评视图（Req 12.25）。

    它只由现有字段构成，携带披露字段清单与排除字段清单，并标注为实验披露清单
    （experiment_disclosure_list）而非 View_Manifest；全部记录标记 non_product_evidence。
    """

    task_id: str
    group: BlindViewGroup
    phase: BlindViewPhase
    disclosed_fields: List[str]
    excluded_fields: List[str]
    payload: Dict[str, Any]
    implementer_notes_included: bool
    disclosure_label: str = DISCLOSURE_LABEL
    non_product_evidence: bool = True
    built_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "group": self.group.value,
            "phase": self.phase.value,
            "disclosed_fields": list(self.disclosed_fields),
            "excluded_fields": list(self.excluded_fields),
            "payload": dict(self.payload),
            "implementer_notes_included": self.implementer_notes_included,
            "disclosure_label": self.disclosure_label,
            # 显式标记非产品 Evidence（Req 12.23）；且本清单不是 View_Manifest（Req 12.25）。
            "non_product_evidence": self.non_product_evidence,
            "is_view_manifest": False,
            "built_at": self.built_at,
        }


# ---------------------------------------------------------------------------
# 核心投影逻辑
# ---------------------------------------------------------------------------


def _has_notes(notes: Any) -> bool:
    """判断 Implementer_Notes 是否非空（None/空串/空容器视为无）。"""
    if notes is None:
        return False
    if isinstance(notes, (str, list, dict, tuple, set)):
        return len(notes) > 0
    return True


def build_minimal_blind_view(
    task_id: str,
    source: BlindViewSourceFacts,
    group: BlindViewGroup,
    phase: BlindViewPhase,
    implementer_notes: Any = None,
    first_verdict_sealed: bool = False,
    reviewer_session_marker: Optional[str] = None,
    implementer_session_marker: Optional[str] = None,
    snapshot_changed_during_review: bool = False,
) -> MinimalBlindView:
    """按组别与披露阶段从现有字段构造 Minimal_Blind_View。

    Args:
        task_id: 任务 ID。
        source: 6 组现有字段来源（BlindViewSourceFacts）。
        group: CONTROL / TREATMENT。
        phase: PRE_VERDICT / POST_REVEAL。
        implementer_notes: 实现者说明（结构化字段；禁止包含隐藏推理历史）。
        first_verdict_sealed: 首轮 Blind_First_Pass_Verdict 是否已封存。Treatment 仅在
            封存后（POST_REVEAL）才揭示 Implementer_Notes（Req 12.5 / 12.7）。
        reviewer_session_marker / implementer_session_marker: 会话标记；二者相同则盲法
            无法保持 → 无效样本（Req 12.8）。
        snapshot_changed_during_review: review 期间 Workspace_Snapshot 是否漂移；是则
            无效样本（Req 12.8）。

    Returns:
        MinimalBlindView（标记 non_product_evidence，披露清单标注 experiment_disclosure_list）。

    Raises:
        ViewDisclosureError: 无效样本（INVALID_SAMPLE）、Treatment 盲视图泄露禁止字段
            （DISCLOSURE_VIOLATION）、或必需来源缺失（VIEW_SOURCE_MISSING）。fail-closed，
            不静默吞异常。
    """
    if not task_id:
        raise ViewDisclosureError(make_view_reason(
            ViewErrorCode.VIEW_SOURCE_MISSING, task_id=task_id, field="task_id"))

    # 无效样本检测（Req 12.8）：盲法无法保持即排除，不混入效果估计。
    if (reviewer_session_marker and implementer_session_marker
            and reviewer_session_marker == implementer_session_marker):
        raise ViewDisclosureError(make_view_reason(
            ViewErrorCode.INVALID_SAMPLE, task_id=task_id, reason="session_markers_match"))
    if snapshot_changed_during_review:
        raise ViewDisclosureError(make_view_reason(
            ViewErrorCode.INVALID_SAMPLE, task_id=task_id, reason="snapshot_drift_during_review"))

    # 必需来源缺失则无法构造最小 blind view（Req 12.2 排除条件），fail-closed。
    # task_title 与 task_description 是任务级最小事实，必须可取得（允许空串但属性须存在）。
    for required in ("task_title", "task_description"):
        if not hasattr(source, required):
            raise ViewDisclosureError(make_view_reason(
                ViewErrorCode.VIEW_SOURCE_MISSING, task_id=task_id, field=required))

    # 由现有字段构成 payload（Req 12.25 的 allowlist 字段）。
    payload: Dict[str, Any] = {}
    for fname in MINIMAL_BLIND_VIEW_FIELDS:
        payload[fname] = source.source_field(fname)
    disclosed_fields: List[str] = list(MINIMAL_BLIND_VIEW_FIELDS)

    # Implementer_Notes 条件披露：
    # - Control：首轮 verdict 前即披露（Req 12.4）。
    # - Treatment PRE_VERDICT（未封存）：排除 Implementer_Notes（Req 12.5）；若调用方仍传入
    #   非空 notes，则构成披露泄露（Req 12.18）→ fail-closed。
    # - Treatment POST_REVEAL（已封存）：揭示 Implementer_Notes（Req 12.7）。
    notes_included = False
    excluded_fields: List[str] = list(PROHIBITED_FIELDS)  # 禁止字段恒排除
    if group == BlindViewGroup.CONTROL:
        if _has_notes(implementer_notes):
            payload[IMPLEMENTER_NOTES_FIELD] = implementer_notes
            notes_included = True
        disclosed_fields.append(IMPLEMENTER_NOTES_FIELD)  # Control 披露清单含 notes 槽位
    else:  # TREATMENT
        if phase == BlindViewPhase.POST_REVEAL and first_verdict_sealed:
            if _has_notes(implementer_notes):
                payload[IMPLEMENTER_NOTES_FIELD] = implementer_notes
                notes_included = True
            disclosed_fields.append(IMPLEMENTER_NOTES_FIELD)
        else:
            # 首轮 verdict 封存前：盲视图不得含 Implementer_Notes。
            excluded_fields.append(IMPLEMENTER_NOTES_FIELD)
            if _has_notes(implementer_notes):
                # 调用方试图在 Treatment 盲视图注入 Implementer_Notes → 披露事件（Req 12.18）。
                raise ViewDisclosureError(make_view_reason(
                    ViewErrorCode.DISCLOSURE_VIOLATION,
                    task_id=task_id, field=IMPLEMENTER_NOTES_FIELD))

    # 禁止字段绝不进入 payload（防御性兜底，Req 13.6）。
    for prohibited in PROHIBITED_FIELDS:
        payload.pop(prohibited, None)

    return MinimalBlindView(
        task_id=task_id,
        group=group,
        phase=phase,
        disclosed_fields=disclosed_fields,
        excluded_fields=excluded_fields,
        payload=payload,
        implementer_notes_included=notes_included,
    )


def assert_treatment_blind_purity(view: MinimalBlindView) -> None:
    """审计 Treatment 首轮盲视图的纯度（Req 12.18 / 12.5）。

    校验：组别为 Treatment、阶段为 PRE_VERDICT 的视图，payload 不得包含任何禁止字段，
    且 implementer_notes_included 必须为 False。任一违反即抛 DISCLOSURE_VIOLATION。
    可作为独立审计入口，对已构造视图做事后核查。

    Raises:
        ViewDisclosureError: 盲视图泄露禁止字段或 Implementer_Notes。
    """
    if view.group != BlindViewGroup.TREATMENT:
        return  # 纯度约束只针对 Treatment
    if view.phase != BlindViewPhase.PRE_VERDICT:
        return  # post-reveal 允许揭示 Implementer_Notes
    for prohibited in PROHIBITED_FIELDS:
        if prohibited in view.payload:
            raise ViewDisclosureError(make_view_reason(
                ViewErrorCode.DISCLOSURE_VIOLATION,
                task_id=view.task_id, field=prohibited))
    if view.implementer_notes_included or IMPLEMENTER_NOTES_FIELD in view.payload:
        raise ViewDisclosureError(make_view_reason(
            ViewErrorCode.DISCLOSURE_VIOLATION,
            task_id=view.task_id, field=IMPLEMENTER_NOTES_FIELD))


# verdict 改变原因码（Req 12.7：记录 reveal 前后 verdict 是否改变及结构化原因）。
# 这是“变更原因”而非错误码，故不走 ViewErrorCode；但同样禁止携带隐藏推理历史。
VERDICT_CHANGE_REASONS: List[str] = [
    "no_change",                    # reveal 后结论未变
    "new_fact",                     # 揭示后发现新事实
    "corrected_misunderstanding",   # 纠正了先前误解
]


def build_verdict_change_record(
    task_id: str,
    batch_id: str,
    verdict_changed: bool,
    change_reason_code: str = "no_change",
    structured_reason: Optional[Dict[str, Any]] = None,
    client_clock_time: Optional[float] = None,
    hidden_reasoning: Any = None,
) -> Dict[str, Any]:
    """构造 Treatment reveal 前后 verdict 变更记录（Req 12.7）。

    记录 verdict 是否改变及结构化原因，**显式不记录隐藏推理历史**：若调用方传入
    ``hidden_reasoning``（非空），直接拒绝（fail-closed），以保证盲评记录不含思维链
    （Req 12.7 / 13.6）。

    Args:
        task_id / batch_id: 任务与批次标识。
        verdict_changed: reveal 后 verdict 是否改变。
        change_reason_code: VERDICT_CHANGE_REASONS 之一。
        structured_reason: 结构化变更原因（可选，dict）；不得包含隐藏推理。
        client_clock_time: 客户端时钟时间（P0 无 Authoritative_Clock）。
        hidden_reasoning: 必须为空；非空即拒绝。

    Returns:
        non_product_evidence 记录 dict。

    Raises:
        ViewDisclosureError: change_reason_code 非法，或试图记录隐藏推理历史。
    """
    if _has_notes(hidden_reasoning):
        # Req 12.7 / 13.6：禁止记录隐藏推理历史。
        raise ViewDisclosureError(make_view_reason(
            ViewErrorCode.DISCLOSURE_VIOLATION,
            task_id=task_id, field="hidden_reasoning_history"))
    if change_reason_code not in VERDICT_CHANGE_REASONS:
        raise ViewDisclosureError(make_view_reason(
            ViewErrorCode.INVALID_SAMPLE,
            task_id=task_id, reason=f"unknown_verdict_change_reason:{change_reason_code}"))
    # structured_reason 内也不得携带隐藏推理键（防御性）。
    if isinstance(structured_reason, dict) and _has_notes(structured_reason.get("hidden_reasoning")):
        raise ViewDisclosureError(make_view_reason(
            ViewErrorCode.DISCLOSURE_VIOLATION,
            task_id=task_id, field="hidden_reasoning_history"))

    return {
        "record_type": "verdict_change",
        "task_id": task_id,
        "batch_id": batch_id,
        "verdict_changed": bool(verdict_changed),
        "change_reason_code": change_reason_code,
        "structured_reason": dict(structured_reason) if isinstance(structured_reason, dict) else None,
        "client_clock_time": client_clock_time if client_clock_time is not None else time.time(),
        # 显式标记非产品 Evidence（Req 12.23）。
        "non_product_evidence": True,
        NON_PRODUCT_EVIDENCE: True,
    }


# ---------------------------------------------------------------------------
# 只读来源导出（复用现有状态机 / change_audit / task_symbol_changes / findings / test_runs）
# ---------------------------------------------------------------------------


def _rows_to_dicts(cur: Any) -> List[Dict[str, Any]]:
    """把游标结果转为 dict 列表（sqlite3.Row 支持 keys()）。"""
    out: List[Dict[str, Any]] = []
    for row in cur:
        try:
            out.append({k: row[k] for k in row.keys()})
        except Exception:
            # 非 Row 连接（极少见）退化为 tuple，不做列名映射。
            out.append({"value": tuple(row)})
    return out


def collect_source_facts_from_db(db: Any, task_id: str) -> BlindViewSourceFacts:
    """从现有表只读导出构造 Minimal_Blind_View 所需的 6 组事实（Req 12.1 / 13.2–13.5）。

    仅执行 SELECT（只读导出），不写库、不建表、不改 schema（Property 24）。``db`` 为
    duck-type 对象，需具备 ``conn``（sqlite3 连接）。各查询独立容错：某表缺失或查询失败
    时该组事实退化为空列表，但若任务本身不存在（tasks 查不到）则视为来源缺失，fail-closed。

    test_runs 表无 task_id 列，按“变更文件集合”匹配 test_file，导出相关既有测试状态。

    Args:
        db: 具备 ``conn`` 属性的数据库对象（如 CodeGraphDB）。
        task_id: 任务 ID。

    Returns:
        BlindViewSourceFacts。

    Raises:
        ViewDisclosureError: 任务不存在（VIEW_SOURCE_MISSING）。
    """
    conn = getattr(db, "conn", None)
    if conn is None:
        raise ViewDisclosureError(make_view_reason(
            ViewErrorCode.VIEW_SOURCE_MISSING, task_id=task_id, field="db.conn"))

    # 任务级事实（必须存在）。
    try:
        cur = conn.execute(
            "SELECT id, title, description FROM tasks WHERE id = ?", (task_id,))
        task_row = cur.fetchone()
    except Exception as exc:  # 查询失败不静默吞，转为来源缺失（fail-closed）。
        raise ViewDisclosureError(make_view_reason(
            ViewErrorCode.VIEW_SOURCE_MISSING, task_id=task_id, field=f"tasks:{exc}"))
    if not task_row:
        raise ViewDisclosureError(make_view_reason(
            ViewErrorCode.VIEW_SOURCE_MISSING, task_id=task_id, field="task_row"))

    facts = BlindViewSourceFacts(
        task_id=task_id,
        task_title=task_row["title"] or "",
        task_description=task_row["description"] or "",
    )

    # task_steps 的 target_file / target_symbol。
    try:
        cur = conn.execute(
            "SELECT id, step_index, action, target_file, target_symbol "
            "FROM task_steps WHERE task_id = ? ORDER BY step_index", (task_id,))
        facts.step_targets = _rows_to_dicts(cur)
    except Exception:
        facts.step_targets = []

    # change_audit diff。
    changed_files: List[str] = []
    try:
        cur = conn.execute(
            "SELECT id, step_id, file_path, hash_before, hash_after, diff, author, timestamp "
            "FROM change_audit WHERE task_id = ? ORDER BY timestamp", (task_id,))
        facts.change_audit_diffs = _rows_to_dicts(cur)
        changed_files = sorted({r.get("file_path") for r in facts.change_audit_diffs if r.get("file_path")})
    except Exception:
        facts.change_audit_diffs = []

    # task_symbol_changes 记录。
    try:
        cur = conn.execute(
            "SELECT id, step_id, file_path, qualified_name, symbol_name, change_type, source "
            "FROM task_symbol_changes WHERE task_id = ? ORDER BY created_at", (task_id,))
        facts.symbol_changes = _rows_to_dicts(cur)
    except Exception:
        facts.symbol_changes = []

    # 既有 test_runs 状态：test_runs 无 task_id，按变更文件匹配 test_file。
    try:
        if changed_files:
            placeholders = ",".join("?" for _ in changed_files)
            cur = conn.execute(
                "SELECT id, test_name, test_file, status, duration_ms, run_at "
                f"FROM test_runs WHERE test_file IN ({placeholders}) ORDER BY run_at DESC",
                tuple(changed_files))
            facts.test_runs_status = _rows_to_dicts(cur)
        else:
            facts.test_runs_status = []
    except Exception:
        facts.test_runs_status = []

    # open task_quality_findings。
    try:
        cur = conn.execute(
            "SELECT id, step_id, finding_type, severity, status, message, source "
            "FROM task_quality_findings WHERE task_id = ? AND status = 'open' ORDER BY created_at",
            (task_id,))
        facts.open_quality_findings = _rows_to_dicts(cur)
    except Exception:
        facts.open_quality_findings = []

    return facts
