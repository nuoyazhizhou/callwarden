"""
experiments —— Call Warden P0 blind-review 对照实验工具链（非产品 Evidence）。

本包实现 Requirement 12 与 Requirement 13 的 P0 部分：在不改 schema 的前提下，
用可审计的对照实验验证 blind-first review 的收益与代价。本包全部记录都标记为
``non_product_evidence``，**不得**被宣称是产品级 Evidence，也**不得**用于支撑
P1 hard gate（Requirement 12.23）。

阶段定位（Requirement 13.1 / 13.6–13.8）：
    - P0 是本包唯一已启用的能力；P1–P4 一律表述为 planned / unavailable，
      而不是 currently implemented。
    - 能力范围限定为契约驱动的任务协同；不包含通用项目管理、实时 Agent 聊天、
      共享隐藏推理历史、中央多 Agent 调度器、任意自然语言证明，也不以 LLM verdict
      替代确定性验证。
    - P2 未启用：不声称复杂 DAG 调度与硬依赖保证。
    - P4 未启用：不声称安全 Lease、assignment 强制、自动派发与抢占。

schema 中立（Requirement 12.1 / Property 24）：
    本包只使用文件 / JSONL 记录实验数据，不新建表、不修改 ``db/schema.py`` 或
    ``db/db_base.py``。Minimal_Blind_View 只由现有字段构成（具体投影由 1.2 实现）。

子模块：
    - ``blind_review_protocol``：实验批次与冻结协议模型、纳入/排除规则、分层维度、
      随机种子、指标分子/分母、观察窗口、成功/暂停阈值、invalid 原因，以及 daemon
      配置存储不可用期间承载 P0 Stage_Toggle 的 ``Experiment_Batch_Config``
      （Requirement 13.18 / 13.21 / Property 30）。

后续子模块（由 wave 1/2 的任务 1.2、1.3 新增，本文件不导入以免提前耦合）：
    - ``blind_review_views`` / ``blind_review_jsonl``：最小披露投影与追加式 JSONL 采集。
    - ``blind_review_evaluator``：评估、成功判定、灰区标记与 fail-safe 暂停状态机。
"""

# 包级常量：本包产出的所有记录都是非产品 Evidence（Requirement 12.23）。
# 任何序列化记录都应携带该标记，且不得被 P1 hard gate 引用。
NON_PRODUCT_EVIDENCE = "non_product_evidence"

# 本包当前仅实现 P0。P1–P4 一律为 planned / unavailable（Requirement 13.1）。
IMPLEMENTED_STAGES = ("P0",)
PLANNED_STAGES = ("P1", "P2", "P3", "P4")

from .blind_review_protocol import (  # noqa: E402
    # 枚举与常量
    BatchStatus,
    GroupAssignment,
    StratificationDimension,
    InvalidSampleReason,
    PauseTrigger,
    MetricKind,
    ToggleScope,
    ToggleValue,
    # 错误码与 i18n 键（Requirement 1.12）
    ExperimentErrorCode,
    EXPERIMENT_I18N_KEYS,
    Structured_Reason,
    make_reason,
    # 协议模型
    MetricDefinition,
    ObservationWindow,
    SuccessThresholds,
    PauseThresholds,
    InclusionRules,
    BatchProtocol,
    ExperimentBatch,
    # Stage_Toggle 存储（Requirement 13.18 / 13.21）
    StageToggleChange,
    Experiment_Batch_Config,
    resolve_stage_toggle,
    # 默认阈值（设计 §14 P0）
    default_success_thresholds,
    default_pause_thresholds,
    default_metric_definitions,
    default_inclusion_rules,
)

__all__ = [
    "NON_PRODUCT_EVIDENCE",
    "IMPLEMENTED_STAGES",
    "PLANNED_STAGES",
    "BatchStatus",
    "GroupAssignment",
    "StratificationDimension",
    "InvalidSampleReason",
    "PauseTrigger",
    "MetricKind",
    "ToggleScope",
    "ToggleValue",
    "ExperimentErrorCode",
    "EXPERIMENT_I18N_KEYS",
    "Structured_Reason",
    "make_reason",
    "MetricDefinition",
    "ObservationWindow",
    "SuccessThresholds",
    "PauseThresholds",
    "InclusionRules",
    "BatchProtocol",
    "ExperimentBatch",
    "StageToggleChange",
    "Experiment_Batch_Config",
    "resolve_stage_toggle",
    "default_success_thresholds",
    "default_pause_thresholds",
    "default_metric_definitions",
    "default_inclusion_rules",
]
