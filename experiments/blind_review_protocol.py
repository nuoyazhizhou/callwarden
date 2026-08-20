"""
blind_review_protocol —— P0 实验批次与冻结协议模型。

本模块是 P0 blind-review 对照实验工具链的模型与配置层（任务 1.1），负责：

1. 协议模型（Requirement 12.3）：批次配置、纳入/排除规则、分层维度、随机种子、
   指标分子/分母、观察窗口、成功/暂停阈值与 invalid 原因。
2. 冻结不变量（Requirement 12.3 / 12.21）：首次纳样前必须锁定协议；首次纳样后
   禁止原地修改协议，投影/采样/指标/窗口规则变化必须产生新批次。
3. 非产品 Evidence 标记（Requirement 12.1 / 12.23 / Property 24）：全部记录标记
   ``non_product_evidence``，只使用文件/JSONL，不新建表、不改 schema。
4. P0 Stage_Toggle 存储（Requirement 13.18 / 13.21 / Property 30）：daemon 配置存储
   不可用期间，``Experiment_Batch_Config`` 以本地文件承载 P0 开关，支持
   global/workspace/task 三级作用域，每次变更记录发起者 session marker 与客户端
   时钟时间，不引入 schema 变更；P0 解析不读取任何 P1–P4 开关取值。
5. Structured_Reason（Requirement 1.12）：每个失败路径返回稳定错误码 + 可在
   zh_CN/en_US 解析的 i18n key，错误码不随文案变化。

边界（重要）：
    - 本模块**不**实现 Minimal_Blind_View 投影与 JSONL 样本采集（任务 1.2）。
    - 本模块**不**实现指标计算、成功判定、灰区标记与暂停状态机的求值（任务 1.3）；
      本模块只提供阈值*数据*、批次状态迁移与暂停/纳样守卫，供 1.3 调用。
    - 灰区（Requirement 12.27–12.29）整体归任务 1.3，本模块不定义灰区阈值。
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

# 非产品 Evidence 标记（Requirement 12.23）。本模块写出的任何持久化记录都携带该标记。
NON_PRODUCT_EVIDENCE = "non_product_evidence"

# Experiment_Batch_Config 文件格式版本（文件级，非数据库 schema；Requirement 12.1 无 schema 变更）。
_CONFIG_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 枚举与常量
# ---------------------------------------------------------------------------


class BatchStatus(str, enum.Enum):
    """实验批次生命周期状态。

    created   —— 批次已建立，协议尚可修改；
    locked    —— 协议已冻结，等待首次纳样（Requirement 12.3：首次纳样前锁定）；
    admitting —— 已发生首次纳样，协议不可再改（Requirement 12.3 / 12.21）；
    paused    —— 触发暂停条件，保留记录与锁定定义，恢复现有 review 流程（12.21）；
    ended     —— 批次结束（样本达标或决策完成）。
    """

    CREATED = "created"
    LOCKED = "locked"
    ADMITTING = "admitting"
    PAUSED = "paused"
    ENDED = "ended"


class GroupAssignment(str, enum.Enum):
    """分层随机分组结果（Requirement 12.3）。"""

    CONTROL = "control"
    TREATMENT = "treatment"


class StratificationDimension(str, enum.Enum):
    """分层随机分配的维度（Requirement 12.3 / 设计 §14 P0 第 2 条）。

    分组程序按 profile、风险、diff 大小、语言与 reviewer/model pair 分层。
    """

    PROFILE = "profile"
    RISK = "risk"
    DIFF_SIZE = "diff_size"
    LANGUAGE = "language"
    REVIEWER_MODEL_PAIR = "reviewer_model_pair"


# 默认分层维度集合（设计 §14 P0）。
DEFAULT_STRATIFICATION_DIMENSIONS: Tuple[StratificationDimension, ...] = (
    StratificationDimension.PROFILE,
    StratificationDimension.RISK,
    StratificationDimension.DIFF_SIZE,
    StratificationDimension.LANGUAGE,
    StratificationDimension.REVIEWER_MODEL_PAIR,
)


class InvalidSampleReason(str, enum.Enum):
    """invalid 样本原因（Requirement 12.8）。

    invalid 样本保留原因，并从效果估计与全部成功/暂停指标的分子分母中排除，
    仅计入 invalid 样本率。
    """

    BLIND_CANNOT_BE_MAINTAINED = "blind_cannot_be_maintained"
    WORKSPACE_SNAPSHOT_CHANGED_DURING_REVIEW = "workspace_snapshot_changed_during_review"
    REVIEWER_IMPLEMENTER_SESSION_MATCH = "reviewer_implementer_session_match"


class PauseTrigger(str, enum.Enum):
    """暂停触发器（Requirement 12.15–12.20）。

    暂停触发器是且仅是这六类；灰区观察（12.27–12.29）不触发暂停（归任务 1.3）。
    """

    CRITICAL_MISS_MISSING_FACTS = "critical_miss_missing_facts"  # 12.15
    FP_RATE_EXCEED_20PP_CONSECUTIVE = "fp_rate_exceed_20pp_consecutive"  # 12.16
    LATENCY_OR_INVALID_RATE = "latency_or_invalid_rate"  # 12.17
    DISCLOSURE_INCIDENT = "disclosure_incident"  # 12.18
    SNAPSHOT_DRIFT_UNATTRIBUTABLE = "snapshot_drift_unattributable"  # 12.19
    FABRICATED_INDEPENDENCE_OR_EVIDENCE = "fabricated_independence_or_evidence"  # 12.20


class MetricKind(str, enum.Enum):
    """指标种类（Requirement 12.3 / 12.6 / 设计 §14 P0 成功指标）。"""

    RECALL = "recall"  # verified blocker/defect recall
    HIGH_RISK_DEFECTS = "high_risk_defects"  # 额外确认的高风险缺陷数
    FALSE_POSITIVE_RATE = "false_positive_rate"
    MEDIAN_LATENCY = "median_latency"
    P90_LATENCY = "p90_latency"
    REOPEN_ROLLBACK_RATE = "reopen_rollback_rate"
    BLINDING_SUCCESS = "blinding_success"  # verdict-before-reveal 比例
    INVALID_SAMPLE_RATE = "invalid_sample_rate"


class ToggleScope(str, enum.Enum):
    """Stage_Toggle 作用域（Requirement 13.18 / 13.3）。"""

    GLOBAL = "global"
    WORKSPACE = "workspace"
    TASK = "task"


class ToggleValue(str, enum.Enum):
    """Stage_Toggle 取值。全局默认关闭（Requirement 13.12）。"""

    ENABLED = "enabled"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Structured_Reason 与错误码（Requirement 1.12）
# ---------------------------------------------------------------------------


class ExperimentErrorCode:
    """P0 实验失败路径的稳定错误码目录（Requirement 1.12）。

    错误码是稳定常量，**不随消息文案变化**；同一错误码在任何版本都指向同一语义。
    文案本地化由 i18n key 承载，二者解耦。
    """

    # 首次纳样后试图原地修改已冻结协议（Requirement 12.3 / 12.21）。
    BATCH_FROZEN = "EXP_BATCH_FROZEN"
    # 协议尚未锁定就试图纳样（Requirement 12.3：首次纳样前必须锁定）。
    PROTOCOL_NOT_LOCKED = "EXP_PROTOCOL_NOT_LOCKED"
    # 批次已暂停，fail-safe 拒绝新纳样（Requirement 12.21 / 12.24）。
    BATCH_PAUSED = "EXP_BATCH_PAUSED"
    # 暂停动作无法记录/执行，fail-safe 停止新纳样（Requirement 12.24）。
    PAUSE_RECORD_FAILED = "EXP_PAUSE_RECORD_FAILED"
    # 批次不存在。
    BATCH_NOT_FOUND = "EXP_BATCH_NOT_FOUND"
    # 样本不满足纳入规则或命中排除规则（Requirement 12.2）。
    INELIGIBLE_SAMPLE = "EXP_INELIGIBLE_SAMPLE"
    # Stage_Toggle 作用域或取值非法（Requirement 13.18）。
    INVALID_TOGGLE = "EXP_INVALID_TOGGLE"
    # 分组模式或 pair slot 非法。
    INVALID_PROTOCOL = "EXP_INVALID_PROTOCOL"
    # Experiment_Batch_Config 读写失败。
    CONFIG_IO = "EXP_CONFIG_IO"
    # 试图让 P0 解析依赖 P1–P4 开关（Requirement 13.21 / 13.17，防御性）。
    P0_MUST_NOT_READ_P1_P4 = "EXP_P0_MUST_NOT_READ_P1_P4"


# 错误码 → i18n key（完整路径，位于 errors.* 命名空间）。
# 词条由任务 1.4 写入 i18n/zh_CN.json 与 i18n/en_US.json；在此之前由
# _BUNDLED_DEFAULTS 提供双语回退，使 key 在两种语言下均可解析（经用户确认的归属方案）。
EXPERIMENT_I18N_KEYS: Dict[str, str] = {
    ExperimentErrorCode.BATCH_FROZEN: "errors.experiment_batch_frozen",
    ExperimentErrorCode.PROTOCOL_NOT_LOCKED: "errors.experiment_protocol_not_locked",
    ExperimentErrorCode.BATCH_PAUSED: "errors.experiment_batch_paused",
    ExperimentErrorCode.PAUSE_RECORD_FAILED: "errors.experiment_pause_record_failed",
    ExperimentErrorCode.BATCH_NOT_FOUND: "errors.experiment_batch_not_found",
    ExperimentErrorCode.INELIGIBLE_SAMPLE: "errors.experiment_ineligible_sample",
    ExperimentErrorCode.INVALID_TOGGLE: "errors.experiment_invalid_toggle",
    ExperimentErrorCode.INVALID_PROTOCOL: "errors.experiment_invalid_protocol",
    ExperimentErrorCode.CONFIG_IO: "errors.experiment_config_io",
    ExperimentErrorCode.P0_MUST_NOT_READ_P1_P4: "errors.experiment_p0_must_not_read_p1_p4",
}

# 捆绑的双语默认文案：i18n catalog 尚未收录对应 key 时（1.4 之前），
# 仍可按当前语言解析出可读消息。key 为 EXPERIMENT_I18N_KEYS 中的完整路径。
_BUNDLED_DEFAULTS: Dict[str, Dict[str, str]] = {
    "errors.experiment_batch_frozen": {
        "zh_CN": "实验批次 {batch_id} 已在首次纳样后冻结，禁止原地修改协议；规则变化必须产生新批次。",
        "en_US": "Experiment batch {batch_id} is frozen after first sample admission; in-place protocol changes are forbidden. Rule changes require a new batch.",
    },
    "errors.experiment_protocol_not_locked": {
        "zh_CN": "实验批次 {batch_id} 的协议尚未锁定，首次纳样前必须先冻结协议。",
        "en_US": "Protocol of experiment batch {batch_id} is not locked; it must be frozen before the first sample admission.",
    },
    "errors.experiment_batch_paused": {
        "zh_CN": "实验批次 {batch_id} 已暂停（触发器：{trigger}），fail-safe 拒绝新纳样；恢复现有 review 流程。",
        "en_US": "Experiment batch {batch_id} is paused (trigger: {trigger}); new admission is rejected fail-safe. The existing review flow is restored.",
    },
    "errors.experiment_pause_record_failed": {
        "zh_CN": "实验批次 {batch_id} 的安全暂停无法记录或执行；fail-safe 停止新纳样，直到暂停机制恢复。",
        "en_US": "The safety pause for experiment batch {batch_id} cannot be recorded or executed; new admission is stopped fail-safe until the pause mechanism is restored.",
    },
    "errors.experiment_batch_not_found": {
        "zh_CN": "未找到实验批次 {batch_id}。",
        "en_US": "Experiment batch {batch_id} not found.",
    },
    "errors.experiment_ineligible_sample": {
        "zh_CN": "任务 {task_id} 不满足纳入规则或命中排除规则：{reason}。",
        "en_US": "Task {task_id} is ineligible: {reason}.",
    },
    "errors.experiment_invalid_toggle": {
        "zh_CN": "非法的 P0 Stage_Toggle 作用域或取值：{detail}。",
        "en_US": "Invalid P0 Stage_Toggle scope or value: {detail}.",
    },
    "errors.experiment_invalid_protocol": {
        "zh_CN": "实验协议参数非法：{detail}。",
        "en_US": "Experiment protocol parameter is invalid: {detail}.",
    },
    "errors.experiment_config_io": {
        "zh_CN": "Experiment_Batch_Config 读写失败：{detail}。",
        "en_US": "Failed to read or write Experiment_Batch_Config: {detail}.",
    },
    "errors.experiment_p0_must_not_read_p1_p4": {
        "zh_CN": "P0 Stage_Toggle 解析不得读取任何 P1–P4 开关取值（Requirement 13.21）。",
        "en_US": "P0 Stage_Toggle resolution must not read any P1-P4 toggle value (Requirement 13.21).",
    },
}


def _resolve_message(message_key: str, context: Dict[str, Any]) -> str:
    """把 i18n key 解析为当前语言的可读消息。

    解析顺序（Requirement 1.12：key 须在 zh_CN 与 en_US 均可解析）：
    1. 项目 i18n catalog（``errors.*``）——任务 1.4 补齐词条后优先生效；
    2. 本模块捆绑的双语默认（按当前语言，再退回 en_US）；
    3. key 本身。

    占位符用 ``context`` 填充；任何格式化失败都退回未格式化文本，绝不抛异常吞掉错误码。
    """
    lang = "en_US"
    catalog_text: Optional[str] = None
    try:
        # 延迟导入，避免实验包硬依赖 i18n 初始化。experiments 是 callwarden 的子包
        # （经 callwarden.experiments 导入），故用包相对导入；若误用顶层绝对导入
        # （from i18n import ...）在生产环境会 ImportError 并被下方 except 静默吞掉，
        # 导致 catalog 永远不被咨询（违背 Req 1.12 目录优先解析）。
        from ..i18n import t, get_language

        lang = get_language() or "en_US"
        # 先尝试 catalog。注意：t(key, default=None) 在 catalog 缺失该 key 时会
        # 返回 key 本身（而非 None），因此用“返回值是否等于 key”判定是否收录。
        catalog_text = t(message_key, default=None)
    except Exception:
        catalog_text = None

    # catalog 真实词条优先（1.4 补齐后生效）；返回值等于 key 本身视为未收录。
    if isinstance(catalog_text, str) and catalog_text and catalog_text != message_key:
        template = catalog_text
    else:
        bundle = _BUNDLED_DEFAULTS.get(message_key)
        if bundle:
            template = bundle.get(lang) or bundle.get("en_US") or message_key
        else:
            template = message_key

    try:
        return template.format(**context)
    except (KeyError, ValueError, IndexError):
        return template


@dataclass
class Structured_Reason:
    """结构化失败原因（Requirement 1.12）。

    code        —— 稳定错误码（ExperimentErrorCode 之一），不随文案变化；
    message_key —— 可在 zh_CN/en_US 解析的 i18n key；
    context     —— 占位符上下文（如 batch_id、trigger）；
    severity    —— "error" / "warning"；失败路径为 error。

    本对象可序列化为 dict 随实验记录留存（标记 non_product_evidence 由调用方附加）。
    """

    code: str
    message_key: str
    context: Dict[str, Any] = field(default_factory=dict)
    severity: str = "error"

    def message(self) -> str:
        """解析当前语言下的可读消息。"""
        return _resolve_message(self.message_key, self.context)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message_key": self.message_key,
            "message": self.message(),
            "context": dict(self.context),
            "severity": self.severity,
        }


def make_reason(code: str, severity: str = "error", **context: Any) -> Structured_Reason:
    """按错误码构造 Structured_Reason，自动绑定对应 i18n key。

    错误码必须在 EXPERIMENT_I18N_KEYS 中登记，否则抛 KeyError——
    这保证每个失败路径都有稳定错误码与可解析 i18n key（Requirement 1.12），
    而不是静默返回无码原因。
    """
    if code not in EXPERIMENT_I18N_KEYS:
        raise KeyError(f"未登记的实验错误码: {code}（必须先在 EXPERIMENT_I18N_KEYS 注册）")
    return Structured_Reason(
        code=code,
        message_key=EXPERIMENT_I18N_KEYS[code],
        context=dict(context),
        severity=severity,
    )


class ExperimentProtocolError(Exception):
    """携带 Structured_Reason 的实验协议异常。

    失败路径不静默吞异常（项目代码规范）；调用方可从 ``reason`` 取稳定错误码与 i18n key。
    """

    def __init__(self, reason: Structured_Reason):
        self.reason = reason
        super().__init__(f"[{reason.code}] {reason.message()}")


# ---------------------------------------------------------------------------
# 协议模型：指标定义 / 观察窗口 / 阈值 / 纳入规则
# ---------------------------------------------------------------------------


@dataclass
class ObservationWindow:
    """指标观察窗口（Requirement 12.3 / 12.6 / 12.22）。

    窗口随批次锁定，报告时与指标定义一同输出。
    """

    name: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ObservationWindow":
        return cls(name=d["name"], description=d["description"])


@dataclass
class MetricDefinition:
    """单个指标的锁定定义（Requirement 12.3 / 12.6 / 12.22）。

    明确分子与分母的含义，以及该指标使用的观察窗口。报告时须连同绝对分子/分母、
    比例、置信区间与每个 invalid 原因一同输出（求值在任务 1.3）。
    """

    kind: MetricKind
    name: str
    numerator_description: str
    denominator_description: str
    observation_window: str  # 引用 ObservationWindow.name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "numerator_description": self.numerator_description,
            "denominator_description": self.denominator_description,
            "observation_window": self.observation_window,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MetricDefinition":
        return cls(
            kind=MetricKind(d["kind"]),
            name=d["name"],
            numerator_description=d["numerator_description"],
            denominator_description=d["denominator_description"],
            observation_window=d["observation_window"],
        )


@dataclass
class SuccessThresholds:
    """成功阈值（Requirement 12.9–12.13，随批次冻结；设计 §14 P0 成功指标）。

    满足全部成功条件且达到最小样本，批次才 eligible_for_p1（求值在任务 1.3）。
    阈值是*数据*，由本模块冻结进批次；是否达成由 1.3 计算。
    """

    # 12.9 最小样本：有效任务数与非平凡 code_change 任务数下限。
    min_valid_tasks: int = 30
    min_nontrivial_code_change_tasks: int = 10
    # 12.10 缺陷检测：recall 相对 Control 提升下限，或额外确认高风险缺陷数下限。
    recall_relative_improvement_min: float = 0.15
    additional_high_risk_defects_min: int = 2
    # 12.11 误报：Treatment 误报率相对 Control 的绝对差上限（10 个百分点）。
    false_positive_rate_abs_diff_max: float = 0.10
    # 12.12 时延：中位与 P90 review latency 相对 Control 的增幅上限。
    median_latency_relative_increase_max: float = 0.25
    p90_latency_relative_increase_max: float = 0.50
    # 12.13 安全与盲法：apply 后 reopen/rollback 率不高于 Control；
    # 且可证明 verdict-before-reveal 的 Treatment 样本比例下限。
    reopen_rollback_rate_above_control_max: float = 0.0
    blinding_verdict_before_reveal_min: float = 0.90

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SuccessThresholds":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class PauseThresholds:
    """暂停阈值（Requirement 12.15–12.20，随批次冻结；设计 §14 P0 停止指标）。

    暂停触发器是且仅是 PauseTrigger 的六类；灰区（12.27–12.29）不触发暂停（归 1.3）。
    """

    # 12.16：连续 N 个 Treatment 样本误报率超过 Control 的绝对差下限。
    fp_exceed_control_abs_diff_pause: float = 0.20
    fp_consecutive_samples: int = 10
    # 12.17：中位时延相对 Control 增幅下限 + 连续周数；或 invalid 样本率上限。
    median_latency_relative_increase_pause: float = 0.50
    median_latency_consecutive_weeks: int = 2
    invalid_sample_rate_pause: float = 0.30
    # 12.19：snapshot 漂移导致不可归因样本比例上限。
    snapshot_drift_unattributable_pause: float = 0.20
    # 12.15 / 12.18 / 12.20 为事件型触发器（无阈值），由 PauseTrigger 表达。

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PauseThresholds":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class InclusionRules:
    """纳入/排除规则（Requirement 12.2，随批次冻结）。

    纳入：profile ∈ {design, code_change, review} 且存在可复核 diff 或设计变更。
    排除：紧急人工直改、纯机械格式化、无法构造最小 blind view。
    """

    included_profiles: Tuple[str, ...] = ("design", "code_change", "review")
    require_reviewable_change: bool = True
    exclude_emergency_direct_edit: bool = True
    exclude_mechanical_formatting: bool = True
    exclude_cannot_produce_blind_view: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "included_profiles": list(self.included_profiles),
            "require_reviewable_change": self.require_reviewable_change,
            "exclude_emergency_direct_edit": self.exclude_emergency_direct_edit,
            "exclude_mechanical_formatting": self.exclude_mechanical_formatting,
            "exclude_cannot_produce_blind_view": self.exclude_cannot_produce_blind_view,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InclusionRules":
        return cls(
            included_profiles=tuple(d.get("included_profiles", ("design", "code_change", "review"))),
            require_reviewable_change=d.get("require_reviewable_change", True),
            exclude_emergency_direct_edit=d.get("exclude_emergency_direct_edit", True),
            exclude_mechanical_formatting=d.get("exclude_mechanical_formatting", True),
            exclude_cannot_produce_blind_view=d.get("exclude_cannot_produce_blind_view", True),
        )

    def evaluate(
        self,
        profile: str,
        has_reviewable_change: bool,
        is_emergency_direct_edit: bool = False,
        is_mechanical_formatting: bool = False,
        can_produce_blind_view: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        """对单个任务做确定性纳入判定。

        返回 (eligible, exclusion_reason)。eligible 为 False 时给出排除原因，
        供任务 1.2/1.3 记录与上报（失败路径返回 Structured_Reason）。
        """
        if profile not in self.included_profiles:
            return False, f"profile '{profile}' 不在纳入集合 {list(self.included_profiles)}"
        if self.require_reviewable_change and not has_reviewable_change:
            return False, "缺少可复核 diff 或设计变更"
        if self.exclude_emergency_direct_edit and is_emergency_direct_edit:
            return False, "紧急人工直改被排除"
        if self.exclude_mechanical_formatting and is_mechanical_formatting:
            return False, "纯机械格式化被排除"
        if self.exclude_cannot_produce_blind_view and not can_produce_blind_view:
            return False, "无法构造最小 blind view 被排除"
        return True, None


# ---------------------------------------------------------------------------
# 协议与批次：冻结不变量
# ---------------------------------------------------------------------------


@dataclass
class BatchProtocol:
    """锁定的实验协议（Requirement 12.3）。

    首次纳样前必须冻结；冻结后逐字节不可变。包含分组程序所需的全部要素：
    纳入/排除规则、分层维度、随机种子、指标分子/分母、观察窗口、成功/暂停阈值，
    以及识别 invalid 样本的原因集合。
    """

    inclusion_rules: InclusionRules
    stratification_dimensions: Tuple[StratificationDimension, ...]
    random_seed: int
    metric_definitions: Tuple[MetricDefinition, ...]
    observation_windows: Tuple[ObservationWindow, ...]
    success_thresholds: SuccessThresholds
    pause_thresholds: PauseThresholds
    invalid_reasons: Tuple[InvalidSampleReason, ...]
    # 旧协议使用 hash；后继 G0 批次可显式启用 paired，让同一 strata 的
    # pair_slot=0/1 必然分到相反组别。
    assignment_mode: str = "hash"

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "inclusion_rules": self.inclusion_rules.to_dict(),
            "stratification_dimensions": [d.value for d in self.stratification_dimensions],
            "random_seed": self.random_seed,
            "metric_definitions": [m.to_dict() for m in self.metric_definitions],
            "observation_windows": [w.to_dict() for w in self.observation_windows],
            "success_thresholds": self.success_thresholds.to_dict(),
            "pause_thresholds": self.pause_thresholds.to_dict(),
            "invalid_reasons": [r.value for r in self.invalid_reasons],
        }
        # 不把默认值写入旧协议，保持历史 fingerprint 和重放兼容。
        if self.assignment_mode != "hash":
            payload["assignment_mode"] = self.assignment_mode
        return payload

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BatchProtocol":
        return cls(
            inclusion_rules=InclusionRules.from_dict(d["inclusion_rules"]),
            stratification_dimensions=tuple(
                StratificationDimension(x) for x in d["stratification_dimensions"]
            ),
            random_seed=int(d["random_seed"]),
            metric_definitions=tuple(MetricDefinition.from_dict(m) for m in d["metric_definitions"]),
            observation_windows=tuple(ObservationWindow.from_dict(w) for w in d["observation_windows"]),
            success_thresholds=SuccessThresholds.from_dict(d["success_thresholds"]),
            pause_thresholds=PauseThresholds.from_dict(d["pause_thresholds"]),
            invalid_reasons=tuple(InvalidSampleReason(r) for r in d["invalid_reasons"]),
            assignment_mode=d.get("assignment_mode", "hash"),
        )

    def fingerprint(self) -> str:
        """协议的稳定内容指纹。

        用于检测“规则是否变化”：冻结后若试图替换协议，比较指纹即可判定是否需要
        新批次（Requirement 12.21）。规范化 JSON 序列化保证同一协议指纹恒定。
        """
        canonical = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def assign_group(
        self,
        strata_key: str,
        pair_slot: Optional[int] = None,
        pair_id: Optional[str] = None,
    ) -> GroupAssignment:
        """确定性分层随机分组（Requirement 12.3）。

        以冻结的 random_seed 与分层键（profile/risk/diff size/language/reviewer-model
        pair 的组合）做 SHA-256，按最低位奇偶映射到 Control/Treatment。
        使用 hashlib 而非内置 hash()，保证跨进程、跨运行可复现（任务 1.5 验证）。
        """
        if self.assignment_mode not in {"hash", "paired", "paired_v2"}:
            raise ExperimentProtocolError(
                make_reason(ExperimentErrorCode.INVALID_PROTOCOL,
                            detail=f"unsupported assignment_mode: {self.assignment_mode}"))
        if self.assignment_mode in {"paired", "paired_v2"}:
            if pair_slot not in (0, 1):
                raise ExperimentProtocolError(
                    make_reason(ExperimentErrorCode.INVALID_PROTOCOL,
                                detail="paired assignment requires pair_slot 0 or 1"))
            if self.assignment_mode == "paired" and pair_id is not None:
                raise ExperimentProtocolError(
                    make_reason(ExperimentErrorCode.INVALID_PROTOCOL,
                                detail="paired uses legacy strata assignment; use paired_v2 for pair_id"))
            if self.assignment_mode == "paired_v2" and (not pair_id or not str(pair_id).strip()):
                raise ExperimentProtocolError(
                    make_reason(ExperimentErrorCode.INVALID_PROTOCOL,
                                detail="paired assignment requires a unique pair_id"))
            assignment_key = strata_key if self.assignment_mode == "paired" else f"{strata_key}:{pair_id}"
            digest = hashlib.sha256(
                f"{self.random_seed}:{assignment_key}".encode("utf-8")
            ).digest()
            base = digest[0] & 1
            # slot 0 保留确定性基线，slot 1 强制取反，形成真正的 pair。
            return GroupAssignment.CONTROL if (base ^ pair_slot) == 0 else GroupAssignment.TREATMENT
        if pair_slot is not None or pair_id is not None:
            raise ExperimentProtocolError(
                make_reason(ExperimentErrorCode.INVALID_PROTOCOL,
                            detail="hash assignment does not accept pair_slot or pair_id"))
        digest = hashlib.sha256(f"{self.random_seed}:{strata_key}".encode("utf-8")).digest()
        return GroupAssignment.CONTROL if (digest[0] & 1) == 0 else GroupAssignment.TREATMENT


@dataclass
class ExperimentBatch:
    """实验批次（Requirement 12.3 / 12.21 / 12.23）。

    冻结不变量：
        - ``status == created`` 时协议可改；
        - ``lock_protocol()`` 后进入 locked，协议快照被冻结；
        - ``mark_first_admission()`` 后进入 admitting，协议**不可再改**；
        - 规则变化（投影/采样/指标/窗口）必须 ``spawn_successor()`` 产生新批次。

    暂停（12.21）：``pause()`` 保留记录与锁定定义；暂停后同样要求新批次才能改规则。
    全部记录标记 non_product_evidence（12.23），不得支撑 P1 hard gate。
    """

    batch_id: str
    created_at: str  # 客户端时钟时间（ISO-8601）；P0 独占期无 Authoritative_Clock
    protocol: BatchProtocol
    status: BatchStatus = BatchStatus.CREATED
    frozen_protocol_fingerprint: Optional[str] = None
    first_sample_admitted_at: Optional[str] = None
    paused_at: Optional[str] = None
    pause_trigger: Optional[PauseTrigger] = None
    pause_reason: Optional[str] = None
    ended_at: Optional[str] = None
    predecessor_batch_id: Optional[str] = None
    # 审计：暂停/纳样守卫无法记录时的 fail-safe 标志（Requirement 12.24）。
    admission_halted: bool = False

    # --- 冻结不变量 -----------------------------------------------------

    def ensure_protocol_mutable(self) -> None:
        """协议可修改的前提检查；已纳样则抛 ExperimentProtocolError（12.3 / 12.21）。"""
        if self.first_sample_admitted_at is not None or self.status not in (
            BatchStatus.CREATED,
        ):
            raise ExperimentProtocolError(
                make_reason(ExperimentErrorCode.BATCH_FROZEN, batch_id=self.batch_id)
            )

    def lock_protocol(self) -> str:
        """冻结协议（Requirement 12.3：首次纳样前锁定分组程序）。

        返回冻结指纹。仅在 created 状态可锁定；锁定后进入 locked。
        """
        if self.status != BatchStatus.CREATED:
            raise ExperimentProtocolError(
                make_reason(ExperimentErrorCode.BATCH_FROZEN, batch_id=self.batch_id)
            )
        self.frozen_protocol_fingerprint = self.protocol.fingerprint()
        self.status = BatchStatus.LOCKED
        return self.frozen_protocol_fingerprint

    def ensure_admission_allowed(self) -> None:
        """纳样前提检查（Requirement 12.3 / 12.21 / 12.24）。

        - 协议未锁定（仍在 created）→ 拒绝（须先 lock_protocol）；
        - 已暂停或 fail-safe 停止 → 拒绝。
        """
        if self.status == BatchStatus.CREATED:
            raise ExperimentProtocolError(
                make_reason(ExperimentErrorCode.PROTOCOL_NOT_LOCKED, batch_id=self.batch_id)
            )
        if self.admission_halted:
            raise ExperimentProtocolError(
                make_reason(ExperimentErrorCode.PAUSE_RECORD_FAILED, batch_id=self.batch_id)
            )
        if self.status == BatchStatus.PAUSED:
            raise ExperimentProtocolError(
                make_reason(
                    ExperimentErrorCode.BATCH_PAUSED,
                    batch_id=self.batch_id,
                    trigger=(self.pause_trigger.value if self.pause_trigger else "unknown"),
                )
            )

    def mark_first_admission(self, client_clock_time: str) -> None:
        """记录首次纳样（Requirement 12.3 / 12.21）。

        必须先锁定协议；此后协议不可再改。client_clock_time 为客户端时钟时间
        （P0 独占期无 Authoritative_Clock，Requirement 13.18）。
        """
        self.ensure_admission_allowed()
        if self.first_sample_admitted_at is None:
            self.first_sample_admitted_at = client_clock_time
        self.status = BatchStatus.ADMITTING

    def pause(self, trigger: PauseTrigger, reason: str, client_clock_time: str) -> None:
        """暂停批次（Requirement 12.21）。

        保留批次记录与锁定的指标定义/分母/观察窗/阈值；暂停后规则变化须新批次。
        暂停动作本身必须被记录；若调用方无法持久化本状态，应改用
        ``halt_admission_fail_safe()``（Requirement 12.24）。
        """
        self.status = BatchStatus.PAUSED
        self.paused_at = client_clock_time
        self.pause_trigger = trigger
        self.pause_reason = reason

    def halt_admission_fail_safe(self) -> None:
        """fail-safe：暂停无法记录/执行时停止新纳样（Requirement 12.24）。"""
        self.admission_halted = True

    def end(self, client_clock_time: str) -> None:
        """结束批次。"""
        self.status = BatchStatus.ENDED
        self.ended_at = client_clock_time

    def spawn_successor(self, new_batch_id: str, new_protocol: BatchProtocol, client_clock_time: str) -> "ExperimentBatch":
        """规则变化后产生新批次（Requirement 12.21）。

        不在原批次中移动目标线：新批次携带新协议与指向前驱的 predecessor_batch_id。
        """
        return ExperimentBatch(
            batch_id=new_batch_id,
            created_at=client_clock_time,
            protocol=new_protocol,
            status=BatchStatus.CREATED,
            predecessor_batch_id=self.batch_id,
        )

    # --- 序列化 ---------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            NON_PRODUCT_EVIDENCE: True,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "protocol": self.protocol.to_dict(),
            "status": self.status.value,
            "frozen_protocol_fingerprint": self.frozen_protocol_fingerprint,
            "first_sample_admitted_at": self.first_sample_admitted_at,
            "paused_at": self.paused_at,
            "pause_trigger": (self.pause_trigger.value if self.pause_trigger else None),
            "pause_reason": self.pause_reason,
            "ended_at": self.ended_at,
            "predecessor_batch_id": self.predecessor_batch_id,
            "admission_halted": self.admission_halted,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExperimentBatch":
        return cls(
            batch_id=d["batch_id"],
            created_at=d["created_at"],
            protocol=BatchProtocol.from_dict(d["protocol"]),
            status=BatchStatus(d.get("status", "created")),
            frozen_protocol_fingerprint=d.get("frozen_protocol_fingerprint"),
            first_sample_admitted_at=d.get("first_sample_admitted_at"),
            paused_at=d.get("paused_at"),
            pause_trigger=(PauseTrigger(d["pause_trigger"]) if d.get("pause_trigger") else None),
            pause_reason=d.get("pause_reason"),
            ended_at=d.get("ended_at"),
            predecessor_batch_id=d.get("predecessor_batch_id"),
            admission_halted=d.get("admission_halted", False),
        )


# ---------------------------------------------------------------------------
# 默认协议组件（设计 §14 P0）
# ---------------------------------------------------------------------------


def default_observation_windows() -> Tuple[ObservationWindow, ...]:
    """默认观察窗口集合（Requirement 12.3 / 12.6）。"""
    return (
        ObservationWindow(
            name="review_phase",
            description="从 review 开始到首轮 verdict 封存的时长窗口（用于 latency 与盲法指标）。",
        ),
        ObservationWindow(
            name="post_apply_defect",
            description="task_apply 之后观察 reopen/rollback 与缺陷的窗口（用于安全指标）。",
        ),
    )


def default_metric_definitions() -> Tuple[MetricDefinition, ...]:
    """默认指标定义集合（Requirement 12.3 / 12.6 / 设计 §14 P0）。

    每个指标都明确分子/分母与所用观察窗口；求值在任务 1.3。
    """
    return (
        MetricDefinition(
            kind=MetricKind.RECALL,
            name="verified_blocker_defect_recall",
            numerator_description="经确认的 true positive（verified blocker/defect）数量。",
            denominator_description="锁定 recall 分母：verified true positive + verified miss。",
            observation_window="review_phase",
        ),
        MetricDefinition(
            kind=MetricKind.HIGH_RISK_DEFECTS,
            name="additional_confirmed_high_risk_defects",
            numerator_description="Treatment 相对 Control 额外发现且经确认的高风险缺陷数。",
            denominator_description="不适用（计数指标，以 Control 高风险缺陷数为基线）。",
            observation_window="review_phase",
        ),
        MetricDefinition(
            kind=MetricKind.FALSE_POSITIVE_RATE,
            name="false_positive_rate",
            numerator_description="经确认的 false positive 数量。",
            denominator_description="全部首轮 finding 数量（false positive + true positive）。",
            observation_window="review_phase",
        ),
        MetricDefinition(
            kind=MetricKind.MEDIAN_LATENCY,
            name="median_review_latency",
            numerator_description="review 时长样本（按组取中位数）。",
            denominator_description="有效样本数（用于中位数计算）。",
            observation_window="review_phase",
        ),
        MetricDefinition(
            kind=MetricKind.P90_LATENCY,
            name="p90_review_latency",
            numerator_description="review 时长样本（按组取 P90）。",
            denominator_description="有效样本数（用于 P90 计算）。",
            observation_window="review_phase",
        ),
        MetricDefinition(
            kind=MetricKind.REOPEN_ROLLBACK_RATE,
            name="post_apply_reopen_rollback_rate",
            numerator_description="apply 后发生 reopen 或 rollback 的任务数。",
            denominator_description="已 apply 的任务数。",
            observation_window="post_apply_defect",
        ),
        MetricDefinition(
            kind=MetricKind.BLINDING_SUCCESS,
            name="verdict_before_reveal_proportion",
            numerator_description="可证明首轮 verdict 早于 reveal 且 blind view 不含 Implementer_Notes 的 Treatment 样本数。",
            denominator_description="全部 Treatment 样本数。",
            observation_window="review_phase",
        ),
        MetricDefinition(
            kind=MetricKind.INVALID_SAMPLE_RATE,
            name="invalid_sample_rate",
            numerator_description="标记为 invalid 的样本数（按 InvalidSampleReason 分类）。",
            denominator_description="全部已纳样样本数。",
            observation_window="review_phase",
        ),
    )


def default_success_thresholds() -> SuccessThresholds:
    """默认成功阈值（Requirement 12.9–12.13）。"""
    return SuccessThresholds()


def default_pause_thresholds() -> PauseThresholds:
    """默认暂停阈值（Requirement 12.15–12.20）。"""
    return PauseThresholds()


def default_inclusion_rules() -> InclusionRules:
    """默认纳入/排除规则（Requirement 12.2）。"""
    return InclusionRules()


def default_invalid_reasons() -> Tuple[InvalidSampleReason, ...]:
    """默认 invalid 原因集合（Requirement 12.8）。"""
    return tuple(InvalidSampleReason)


def build_default_protocol(random_seed: int, assignment_mode: str = "hash") -> BatchProtocol:
    """用默认组件构造一个完整协议（Requirement 12.3）。

    random_seed 由调用方给定并冻结进批次，保证分组可复现。
    """
    return BatchProtocol(
        inclusion_rules=default_inclusion_rules(),
        stratification_dimensions=DEFAULT_STRATIFICATION_DIMENSIONS,
        random_seed=random_seed,
        metric_definitions=default_metric_definitions(),
        observation_windows=default_observation_windows(),
        success_thresholds=default_success_thresholds(),
        pause_thresholds=default_pause_thresholds(),
        invalid_reasons=default_invalid_reasons(),
        assignment_mode=assignment_mode,
    )


# ---------------------------------------------------------------------------
# Stage_Toggle 存储：Experiment_Batch_Config（Requirement 13.18 / 13.21 / Property 30）
# ---------------------------------------------------------------------------


@dataclass
class StageToggleChange:
    """一次 P0 Stage_Toggle 变更的审计记录（Requirement 13.18）。

    P0 独占期没有 Authoritative_Clock，只记录客户端时钟时间——这是事实陈述
    （Requirement 13.18 / 设计 §13.3.1），不是精度让步。发起者用 session marker 标识。
    """

    scope: ToggleScope
    scope_key: Optional[str]  # workspace/task 作用域的标识；global 为 None
    value: ToggleValue
    session_marker: str
    client_clock_time: str
    kind: str = "change"  # change / migration（迁移由 D0 任务 3.x 使用）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope.value,
            "scope_key": self.scope_key,
            "value": self.value.value,
            "session_marker": self.session_marker,
            "client_clock_time": self.client_clock_time,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StageToggleChange":
        return cls(
            scope=ToggleScope(d["scope"]),
            scope_key=d.get("scope_key"),
            value=ToggleValue(d["value"]),
            session_marker=d["session_marker"],
            client_clock_time=d["client_clock_time"],
            kind=d.get("kind", "change"),
        )


def _default_config_path() -> str:
    """Experiment_Batch_Config 默认路径。

    与 config.CALLWARDEN_DIR（~/.callwarden）约定一致，置于其下 experiments/ 子目录。
    为避免实验包反向依赖重量级 config 模块，这里直接展开用户主目录。
    """
    return os.path.join(os.path.expanduser("~"), ".callwarden", "experiments", "batch_config.json")


def resolve_stage_toggle(
    task_value: Optional[ToggleValue],
    workspace_value: Optional[ToggleValue],
    global_value: Optional[ToggleValue],
) -> ToggleValue:
    """P0 Stage_Toggle 三级作用域解析（Requirement 13.12 / 13.18 / 13.21）。

    优先级 task > workspace > global；某级缺值继承更宽作用域；全局默认关闭。
    本函数**只**接受 P0 的三级取值，签名上就不存在 P1–P4 入参，结构上保证
    P0 解析不读取任何 P1–P4 开关（Requirement 13.21 / Property 30）。
    """
    if task_value is not None:
        return task_value
    if workspace_value is not None:
        return workspace_value
    if global_value is not None:
        return global_value
    return ToggleValue.DISABLED


class Experiment_Batch_Config:
    """P0 实验工具链的本地文件配置（Requirement 13.18 / 13.21 / Property 30）。

    职责：
    1. 承载 P0 Stage_Toggle（daemon 配置存储不可用期间），支持 global/workspace/task
       三级作用域；每次变更记录发起者 session marker 与客户端时钟时间；无 schema 变更。
    2. 持久化已冻结的 ExperimentBatch 协议记录，使“首次纳样后禁止原地改协议”
       与“规则变化必须新批次”跨进程可执行（Requirement 12.3 / 12.21）。

    全部持久化内容标记 non_product_evidence（Requirement 12.23）。
    写入采用临时文件 + 原子替换，避免中断产生半写文件（任务 1.7 恢复测试依赖）。

    本配置**不**存储 P1–P4 开关：P0 独占期这些阶段尚未交付（Requirement 13.18 /
    设计 §13.3.1 表中“P1–P4 Stage_Toggle：不存在”）。
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or _default_config_path()
        # 内存状态：三级 P0 开关取值 + 变更审计日志 + 冻结批次表。
        self._toggles: Dict[str, Dict[str, ToggleValue]] = {
            ToggleScope.GLOBAL.value: {},   # {"" : value}
            ToggleScope.WORKSPACE.value: {},  # {workspace_id: value}
            ToggleScope.TASK.value: {},       # {task_id: value}
        }
        self._toggle_change_log: List[StageToggleChange] = []
        self._batches: Dict[str, ExperimentBatch] = {}
        self._loaded = False

    # --- 持久化 ---------------------------------------------------------

    def load(self) -> "Experiment_Batch_Config":
        """从文件加载；文件不存在则初始化为空（不视为错误，Requirement 13.18）。"""
        if not os.path.exists(self.path):
            self._loaded = True
            return self
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise ExperimentProtocolError(
                make_reason(ExperimentErrorCode.CONFIG_IO, detail=str(e))
            )
        self._toggles = {
            ToggleScope.GLOBAL.value: {
                k: ToggleValue(v) for k, v in data.get("p0_stage_toggle", {}).get("global", {}).items()
            },
            ToggleScope.WORKSPACE.value: {
                k: ToggleValue(v) for k, v in data.get("p0_stage_toggle", {}).get("workspace", {}).items()
            },
            ToggleScope.TASK.value: {
                k: ToggleValue(v) for k, v in data.get("p0_stage_toggle", {}).get("task", {}).items()
            },
        }
        self._toggle_change_log = [
            StageToggleChange.from_dict(c) for c in data.get("toggle_change_log", [])
        ]
        self._batches = {
            b["batch_id"]: ExperimentBatch.from_dict(b) for b in data.get("batches", [])
        }
        self._loaded = True
        return self

    def save(self) -> None:
        """原子写回文件（临时文件 + os.replace）。"""
        payload = {
            NON_PRODUCT_EVIDENCE: True,
            "schema_version": _CONFIG_SCHEMA_VERSION,
            "p0_stage_toggle": {
                ToggleScope.GLOBAL.value: {k: v.value for k, v in self._toggles[ToggleScope.GLOBAL.value].items()},
                ToggleScope.WORKSPACE.value: {k: v.value for k, v in self._toggles[ToggleScope.WORKSPACE.value].items()},
                ToggleScope.TASK.value: {k: v.value for k, v in self._toggles[ToggleScope.TASK.value].items()},
            },
            "toggle_change_log": [c.to_dict() for c in self._toggle_change_log],
            "batches": [b.to_dict() for b in self._batches.values()],
        }
        directory = os.path.dirname(self.path)
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".batch_config.", suffix=".tmp", dir=directory or ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.path)
            except BaseException:
                # 清理临时文件，但不吞异常——向上抛 Structured_Reason。
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except ExperimentProtocolError:
            raise
        except OSError as e:
            raise ExperimentProtocolError(
                make_reason(ExperimentErrorCode.CONFIG_IO, detail=str(e))
            )

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # --- P0 Stage_Toggle（Requirement 13.18 / 13.21） --------------------

    def set_p0_toggle(
        self,
        scope: ToggleScope,
        value: ToggleValue,
        session_marker: str,
        client_clock_time: str,
        scope_key: Optional[str] = None,
    ) -> StageToggleChange:
        """设置某作用域的 P0 开关并追加审计记录（Requirement 13.18）。

        - global 作用域 scope_key 必须为 None；workspace/task 必须提供 scope_key。
        - 每次变更追加一条 StageToggleChange（session marker + 客户端时钟时间）。
        - 只接受 P0 开关；本方法签名不含阶段参数，结构上不可能写入 P1–P4。
        变更后调用方应 ``save()`` 持久化。
        """
        if scope == ToggleScope.GLOBAL:
            if scope_key is not None:
                raise ExperimentProtocolError(
                    make_reason(ExperimentErrorCode.INVALID_TOGGLE, detail="global 作用域不应携带 scope_key")
                )
            effective_key = ""
        else:
            if not scope_key:
                raise ExperimentProtocolError(
                    make_reason(
                        ExperimentErrorCode.INVALID_TOGGLE,
                        detail=f"{scope.value} 作用域必须提供 scope_key",
                    )
                )
            effective_key = scope_key

        self._ensure_loaded()
        self._toggles[scope.value][effective_key] = value
        change = StageToggleChange(
            scope=scope,
            scope_key=(scope_key if scope != ToggleScope.GLOBAL else None),
            value=value,
            session_marker=session_marker,
            client_clock_time=client_clock_time,
            kind="change",
        )
        self._toggle_change_log.append(change)
        return change

    def get_p0_toggle(self, scope: ToggleScope, scope_key: Optional[str] = None) -> Optional[ToggleValue]:
        """读取某作用域已记录的 P0 开关取值；未记录返回 None（继承更宽作用域）。"""
        self._ensure_loaded()
        if scope == ToggleScope.GLOBAL:
            return self._toggles[ToggleScope.GLOBAL.value].get("")
        if not scope_key:
            raise ExperimentProtocolError(
                make_reason(ExperimentErrorCode.INVALID_TOGGLE, detail=f"{scope.value} 作用域必须提供 scope_key")
            )
        return self._toggles[scope.value].get(scope_key)

    def resolve_p0_toggle(self, task_id: Optional[str] = None, workspace_id: Optional[str] = None) -> ToggleValue:
        """解析任务的 P0 生效开关（Requirement 13.12 / 13.21）。

        task > workspace > global，缺值继承，全局默认关闭。
        解析**不读取**任何 P1–P4 开关——本配置根本不存储 P1–P4（Requirement 13.18）。
        """
        self._ensure_loaded()
        task_value = self.get_p0_toggle(ToggleScope.TASK, task_id) if task_id else None
        workspace_value = self.get_p0_toggle(ToggleScope.WORKSPACE, workspace_id) if workspace_id else None
        global_value = self.get_p0_toggle(ToggleScope.GLOBAL)
        return resolve_stage_toggle(task_value, workspace_value, global_value)

    @property
    def toggle_change_log(self) -> List[StageToggleChange]:
        """P0 开关变更审计日志（只读副本）。"""
        self._ensure_loaded()
        return list(self._toggle_change_log)

    # --- 冻结批次持久化（Requirement 12.3 / 12.21） ----------------------

    def put_batch(self, batch: ExperimentBatch) -> None:
        """登记/更新一个批次记录（标记 non_product_evidence）。"""
        self._ensure_loaded()
        self._batches[batch.batch_id] = batch

    def get_batch(self, batch_id: str) -> ExperimentBatch:
        """取批次；不存在抛 Structured_Reason。"""
        self._ensure_loaded()
        batch = self._batches.get(batch_id)
        if batch is None:
            raise ExperimentProtocolError(
                make_reason(ExperimentErrorCode.BATCH_NOT_FOUND, batch_id=batch_id)
            )
        return batch

    def list_batches(self) -> List[ExperimentBatch]:
        self._ensure_loaded()
        return list(self._batches.values())
