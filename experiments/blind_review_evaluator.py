# -*- coding: utf-8 -*-
"""P0 blind-review 对照实验评估器（任务 1.3）。

本模块是纯函数式评估层：输入为已采集的 JSONL 记录（任务 1.2）与冻结协议（任务 1.1），
输出指标计算、成功判定、灰区标记与 fail-safe 暂停结论。不直连数据库、不修改 schema
（Property 24 / Requirement 12.1），模块级不 import db/schema。

覆盖需求：
    - 12.6  两组原始计数 → 指标（绝对计数/比例/置信区间）
    - 12.8  invalid 样本排除出效果估计与全部成功/暂停指标分子分母（仅计入 invalid 率）
    - 12.9  最小样本不足 → directional-only，不授权 P1
    - 12.10 缺陷检测成功条件（recall 相对提升或额外高风险缺陷）
    - 12.11 误报成功条件（绝对差不超过 10pp）
    - 12.12 时延成功条件（median/P90 相对增幅上限）
    - 12.13 安全与盲法成功条件（reopen/rollback 不高于 Control + verdict-before-reveal 比例）
    - 12.14 全部成功条件 + 最小样本满足 → eligible_for_p1（非 P1 已实现）
    - 12.15–12.20 暂停触发器（且仅是这六类）
    - 12.21 暂停保留记录与锁定定义（由调用方对 ExperimentBatch.pause 落库）
    - 12.22 报告输出锁定指标定义/观察窗口/绝对分子分母/比例/置信区间/每个 invalid 原因
    - 12.23 评估记录标记 non_product_evidence，不支撑 P1 hard gate
    - 12.24 暂停无法记录/执行 → fail-safe 停止新纳样
    - 12.26 非平凡 code_change 最小样本门槛（>=10 非注释源行 + 至少一条符号变化，排除格式化/生成）
    - 12.27 误报灰区（>10pp 且 <=20pp）
    - 12.28 时延灰区（median 相对增幅 >25% 且 <=50%）
    - 12.29 灰区未解决 → 排除 P1 授权；灰区不触发暂停

错误码归属：暂停/纳样守卫类（BATCH_PAUSED/PAUSE_RECORD_FAILED/BATCH_NOT_FOUND/INELIGIBLE_SAMPLE）
复用 protocol.make_reason；评估器特有语义（directional-only/灰区/暂停评估结论/输入非法）在本文件内
自建本地 reason 注册表（与 1.2 同构），不修改 1.1/1.2 文件；i18n 词条由任务 1.4 统一收录。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import NON_PRODUCT_EVIDENCE

# 暂停/纳样守卫复用 protocol 已登记错误码与 Structured_Reason（不重复造轮子，亦不改 1.1）。
from .blind_review_protocol import (
    ExperimentBatch,
    SuccessThresholds,
    PauseThresholds,
    MetricDefinition,
    ObservationWindow,
    PauseTrigger,
    GroupAssignment,
    Structured_Reason,
    make_reason,
    ExperimentErrorCode,
    ExperimentProtocolError,
)


# ---------------------------------------------------------------------------
# 本地 reason 注册表：评估器特有语义（Requirement 1.12，沿用 1.2 同构方案）
# ---------------------------------------------------------------------------


class EvaluatorErrorCode:
    """评估器失败/非成功路径的稳定错误码目录（Requirement 1.12）。

    错误码是稳定常量，不随消息文案变化；文案本地化由 i18n key 承载，二者解耦。
    暂停/纳样守卫类错误码不在此处，复用 protocol.ExperimentErrorCode。
    """

    # 12.9：有效样本不足，只能输出 directional result，不授权 P1。
    INSUFFICIENT_SAMPLE = "EXP_INSUFFICIENT_SAMPLE"
    # 12.27：误报灰区（Treatment 误报率高出 Control >10pp 且 <=20pp）。
    GRAY_ZONE_FP = "EXP_GRAY_ZONE_FP"
    # 12.28：时延灰区（Treatment median latency 相对增幅 >25% 且 <=50%）。
    GRAY_ZONE_LATENCY = "EXP_GRAY_ZONE_LATENCY"
    # 12.29：灰区未解决，排除 P1 授权（灰区本身不触发暂停）。
    GRAY_ZONE_UNRESOLVED = "EXP_GRAY_ZONE_UNRESOLVED"
    # 12.15–12.20：评估命中暂停条件（评估结论；实际暂停由调用方落库）。
    PAUSE_TRIGGERED = "EXP_PAUSE_TRIGGERED"
    # 输入记录缺失/非法，无法完成确定性评估（fail-closed，不静默 pass）。
    EVALUATION_INPUT_INVALID = "EXP_EVALUATION_INPUT_INVALID"


# 错误码 → i18n key（完整路径，位于 errors.* 命名空间）。词条由任务 1.4 写入 catalog；
# 在此之前由 _EVALUATOR_BUNDLED_DEFAULTS 提供双语回退，使 key 在两种语言下均可解析。
EVALUATOR_I18N_KEYS: Dict[str, str] = {
    EvaluatorErrorCode.INSUFFICIENT_SAMPLE: "errors.experiment_insufficient_sample",
    EvaluatorErrorCode.GRAY_ZONE_FP: "errors.experiment_gray_zone_fp",
    EvaluatorErrorCode.GRAY_ZONE_LATENCY: "errors.experiment_gray_zone_latency",
    EvaluatorErrorCode.GRAY_ZONE_UNRESOLVED: "errors.experiment_gray_zone_unresolved",
    EvaluatorErrorCode.PAUSE_TRIGGERED: "errors.experiment_pause_triggered",
    EvaluatorErrorCode.EVALUATION_INPUT_INVALID: "errors.experiment_evaluation_input_invalid",
}

# 捆绑的双语默认文案：catalog 尚未收录对应 key 时（1.4 之前）仍可按当前语言解析出可读消息。
_EVALUATOR_BUNDLED_DEFAULTS: Dict[str, Dict[str, str]] = {
    "errors.experiment_insufficient_sample": {
        "zh_CN": "实验批次 {batch_id} 有效样本不足（有效任务 {valid_tasks}/{min_valid_tasks}，非平凡 code_change {nontrivial}/{min_nontrivial}）；仅报告方向性结果，不授权 P1。",
        "en_US": "Experiment batch {batch_id} has insufficient samples (valid tasks {valid_tasks}/{min_valid_tasks}, non-trivial code_change {nontrivial}/{min_nontrivial}); reporting directional results only, P1 not authorized.",
    },
    "errors.experiment_gray_zone_fp": {
        "zh_CN": "实验批次 {batch_id} 落入误报灰区：Treatment 误报率高出 Control {diff_pp} 个百分点（>10 且 <=20）；标记未授权 P1，继续纳样并记录为灰区观察。",
        "en_US": "Experiment batch {batch_id} is in the false-positive gray zone: Treatment FP rate exceeds Control by {diff_pp} percentage points (>10 and <=20); marked not authorized for P1, admission continues and is recorded as a gray-zone observation.",
    },
    "errors.experiment_gray_zone_latency": {
        "zh_CN": "实验批次 {batch_id} 落入时延灰区：Treatment 中位 review latency 相对 Control 增幅 {increase_pct}（>25% 且 <=50%）；标记未授权 P1，继续纳样并记录为灰区观察。",
        "en_US": "Experiment batch {batch_id} is in the latency gray zone: Treatment median review latency increases by {increase_pct} relative to Control (>25% and <=50%); marked not authorized for P1, admission continues and is recorded as a gray-zone observation.",
    },
    "errors.experiment_gray_zone_unresolved": {
        "zh_CN": "实验批次 {batch_id} 存在未解决的灰区观察（{zones}）；灰区未解决前排除 P1 授权，但灰区本身不触发暂停。",
        "en_US": "Experiment batch {batch_id} has unresolved gray-zone observation(s) ({zones}); P1 authorization is excluded while unresolved, but the gray zone itself does not trigger a pause.",
    },
    "errors.experiment_pause_triggered": {
        "zh_CN": "实验批次 {batch_id} 命中暂停条件（触发器：{trigger}）；停止新样本、保留批次与阈值、恢复现有 review 流程。",
        "en_US": "Experiment batch {batch_id} hit a pause condition (trigger: {trigger}); new admission stops, batch and thresholds are retained, and the existing review flow is restored.",
    },
    "errors.experiment_evaluation_input_invalid": {
        "zh_CN": "实验评估输入非法：{detail}；fail-closed 拒绝评估，不静默通过。",
        "en_US": "Experiment evaluation input is invalid: {detail}; evaluation is rejected fail-closed and never passes silently.",
    },
}


def _resolve_evaluator_message(message_key: str, context: Dict[str, Any]) -> str:
    """把 i18n key 解析为当前语言的可读消息（Requirement 1.12）。

    解析顺序：1) 项目 i18n catalog（errors.*，任务 1.4 补齐后优先生效）；2) 本模块捆绑的
    双语默认（按当前语言，再退回 en_US）；3) key 本身。占位符用 context 填充；任何格式化
    失败都退回未格式化文本，绝不抛异常吞掉错误码。
    """
    lang = "en_US"
    catalog_text: Optional[str] = None
    try:
        # 延迟导入，避免实验包硬依赖 i18n 初始化。experiments 是 callwarden 子包，用包相对导入。
        from ..i18n import t, get_language

        lang = get_language() or "en_US"
        # t(key, default=None) 在 catalog 缺失该 key 时返回 key 本身，故用“返回值是否等于 key”判定收录。
        catalog_text = t(message_key, default=None)
    except Exception:
        catalog_text = None

    if isinstance(catalog_text, str) and catalog_text and catalog_text != message_key:
        template = catalog_text
    else:
        bundle = _EVALUATOR_BUNDLED_DEFAULTS.get(message_key)
        if bundle:
            template = bundle.get(lang) or bundle.get("en_US") or message_key
        else:
            template = message_key

    try:
        return template.format(**context)
    except (KeyError, ValueError, IndexError):
        return template


@dataclass
class EvaluatorStructuredReason:
    """评估器结构化原因（Requirement 1.12），与 protocol.Structured_Reason 同构。

    code        —— 稳定错误码（EvaluatorErrorCode 之一），不随文案变化；
    message_key —— 可在 zh_CN/en_US 解析的 i18n key；
    context     —— 占位符上下文；
    severity    —— "error" / "warning"。灰区观察为 warning（非阻断），其余失败路径为 error。
    """

    code: str
    message_key: str
    context: Dict[str, Any] = field(default_factory=dict)
    severity: str = "error"

    def message(self) -> str:
        """解析当前语言下的可读消息。"""
        return _resolve_evaluator_message(self.message_key, self.context)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message_key": self.message_key,
            "message": self.message(),
            "context": dict(self.context),
            "severity": self.severity,
        }


def make_evaluator_reason(code: str, severity: str = "error", **context: Any) -> EvaluatorStructuredReason:
    """按错误码构造 EvaluatorStructuredReason，自动绑定对应 i18n key。

    错误码必须在 EVALUATOR_I18N_KEYS 中登记，否则抛 KeyError——保证每个失败路径都有稳定
    错误码与可解析 i18n key（Requirement 1.12），而不是静默返回无码原因。
    """
    if code not in EVALUATOR_I18N_KEYS:
        raise KeyError(f"未登记的评估器错误码: {code}（必须先在 EVALUATOR_I18N_KEYS 注册）")
    return EvaluatorStructuredReason(
        code=code,
        message_key=EVALUATOR_I18N_KEYS[code],
        context=dict(context),
        severity=severity,
    )


class EvaluatorError(Exception):
    """携带 EvaluatorStructuredReason 的评估异常（输入非法等 fail-closed 路径）。"""

    def __init__(self, reason: EvaluatorStructuredReason):
        self.reason = reason
        super().__init__(f"[{reason.code}] {reason.message()}")


# ---------------------------------------------------------------------------
# 样本与指标计算（Requirement 12.6 / 12.8 / 12.22 / 12.26）
# ---------------------------------------------------------------------------


# Wilson score interval 的 z 值（95% 置信水平）。
_WILSON_Z = 1.96


def wilson_confidence_interval(successes: int, trials: int, z: float = _WILSON_Z) -> Tuple[float, float]:
    """比例的 Wilson score 置信区间（Requirement 12.22）。

    相比正态近似，Wilson 区间在小样本与极端比例（接近 0/1）下更稳健，不会越界 [0,1]。
    trials<=0 时返回 (0.0, 0.0)，避免伪造窄区间（小样本诚实报告）。
    """
    if trials <= 0:
        return (0.0, 0.0)
    p = successes / trials
    denom = 1.0 + (z * z) / trials
    center = (p + (z * z) / (2.0 * trials)) / denom
    halfwidth = (z * math.sqrt((p * (1.0 - p)) / trials + (z * z) / (4.0 * trials * trials))) / denom
    lower = max(0.0, center - halfwidth)
    upper = min(1.0, center + halfwidth)
    return (lower, upper)


def is_nontrivial_code_change(
    changed_source_lines: int,
    has_symbol_change: bool,
    is_formatting_only: bool = False,
    is_generated: bool = False,
    *,
    tracked_source_file: bool = True,
) -> bool:
    """非平凡 ``code_change`` 最小样本门槛（Requirement 12.26）。

    ``changed_source_lines`` 必须来自至少一个 tracked 源文件的非注释行变更，且
    ``task_symbol_changes`` 至少有一条符号变化。调用方无法证明文件 tracked 或为源文件
    时传 ``tracked_source_file=False``；纯格式化和生成文件始终排除。阈值是“至少 10 行”，
    因而恰好 10 行也满足要求。
    """
    if is_formatting_only or is_generated or not tracked_source_file:
        return False
    try:
        lines = int(changed_source_lines)
    except (TypeError, ValueError):
        return False
    return lines >= 10 and bool(has_symbol_change)


# 典型生成文件/构建产物后缀（Req 12.26 排除项）。源文件判定：不在该集合即视为源码。
_GENERATED_FILE_SUFFIXES = {
    ".lock", ".min.js", ".min.css", ".map", ".pyc", ".pyo", ".o", ".so",
    ".dll", ".pyd", ".exe", ".class", ".jar", ".war", ".pdf", ".png",
    ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf",
    ".eot", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".whl", ".egg",
    ".cwsnap", ".csw", ".snap", ".jsonl", ".log", ".tmp", ".bak", ".orig",
}
_GENERATED_FILE_NAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
    "pipfile.lock", "go.sum", "cargo.lock", "composer.lock", "gemfile.lock",
    "lock.json",
}
# 文档/规格类后缀（T-1785854667954 G0 归因治理）：非源码文件，不计入
# 12.26 的“非注释源码行”。markdown/restructuredtext 的正文行会被
# count_non_comment_added_lines 误算为源码行（如 migration-manifest.md 36 行），
# 文档任务（design/review profile）的文档 diff 不应判定为 nontrivial code_change。
_DOC_FILE_SUFFIXES = {
    ".md", ".rst", ".txt", ".markdown", ".adoc", ".mdown", ".rdoc",
}


def count_non_comment_added_lines(diff_text: str) -> int:
    """从统一 diff 文本统计“非注释新增行”数量（Req 12.26 的 changed_source_lines）。

    只统计 ``+`` 前缀的 hunk 内容行（排除 ``+++`` 文件头与空 ``+`` 行），并跳过
    常见注释行：``//``、``#``、``/*``、``*``、``<!--``、``--``、``\"\"\"``、``'''``。
    行首空白后判定注释。失败/非 diff 输入返回 0（不乐观解释）。
    """
    if not diff_text:
        return 0
    count = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or not line.startswith("+"):
            continue
        added = line[1:]
        stripped = added.strip()
        if not stripped:
            continue
        if stripped.startswith(("//", "#", "/*", "*", "<!--", "--", "\"\"\"", "'''")):
            continue
        # 行尾行内注释（如 code // comment）不排除：仍算源码行。
        count += 1
    return count


def is_generated_path(rel_path: str) -> bool:
    """按 Req 12.26 判定文件是否属于生成文件/构建产物/文档（应排除）。"""
    name = (rel_path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in _GENERATED_FILE_NAMES:
        return True
    for suffix in _GENERATED_FILE_SUFFIXES:
        if name.endswith(suffix):
            return True
    for suffix in _DOC_FILE_SUFFIXES:
        if name.endswith(suffix):
            return True
    return False


def nontrivial_code_change_from_change_audit(
    change_audit_diffs: Sequence[Dict[str, Any]],
    symbol_changes: Sequence[Dict[str, Any]],
) -> bool:
    """从 change_audit.diff + task_symbol_changes 自动判定 12.26 非平凡门槛。

    替代 CLI ``--nontrivial`` 手填（G0 清单：行数 + 符号数必须自动判定，不得手填）。
    ``change_audit_diffs`` 每项含 ``file_path`` 与 ``diff``；``symbol_changes`` 为
    ``task_symbol_changes`` 记录（非空即证明符号级变更，对应“至少一条符号变化”）。
    """
    has_symbol_change = bool(symbol_changes)
    if not has_symbol_change:
        return False
    for audit in change_audit_diffs:
        if not isinstance(audit, dict):
            continue
        rel_path = audit.get("file_path") or ""
        if is_generated_path(rel_path):
            continue
        diff_text = audit.get("diff") or ""
        lines = count_non_comment_added_lines(diff_text)
        if is_nontrivial_code_change(
            lines,
            has_symbol_change,
            tracked_source_file=True,  # change_audit 仅记录已跟踪文件的变更
        ):
            return True
    return False


def nontrivial_code_change_from_facts(
    changed_files: Sequence[Dict[str, Any]],
    symbol_changes: Sequence[Dict[str, Any]],
) -> bool:
    """从现有 diff/symbol facts 判定 12.26 的非平凡样本门槛。

    ``changed_files`` 中每项可提供 ``non_comment_lines``（或 ``changed_source_lines``）、
    ``tracked``、``is_source_file``、``is_formatting_only`` 和 ``is_generated``。只要有一个
    tracked 源文件满足 10 行非注释改动，并且存在至少一条符号变化即返回 True。未知字段不
    被乐观解释为通过，避免把生成文件或未跟踪文件误计入 G0 分母。
    """
    has_symbol_change = bool(symbol_changes)
    for fact in changed_files:
        if not isinstance(fact, dict):
            continue
        lines = fact.get("non_comment_lines", fact.get("changed_source_lines", 0))
        if is_nontrivial_code_change(
            lines,
            has_symbol_change,
            bool(fact.get("is_formatting_only", False)),
            bool(fact.get("is_generated", False)),
            tracked_source_file=bool(fact.get("tracked", False) and fact.get("is_source_file", True)),
        ):
            return True
    return False


@dataclass
class SampleRecord:
    """单个有效样本的解析视图（Requirement 12.6）。

    由一条 review_metrics JSONL 记录（任务 1.2 build_review_metrics_record）解析而来。
    invalid 样本不进入本结构，而由 invalid_reason_code 单独标记并排除出效果估计（12.8）。
    """

    task_id: str
    group: str  # GroupAssignment.CONTROL / TREATMENT 的取值
    verified_true_positives: int = 0
    verified_false_positives: int = 0
    verified_misses: int = 0
    review_duration_seconds: float = 0.0
    token_usage: int = 0
    reopen_events: int = 0
    post_apply_defects: int = 0
    post_apply_rollbacks: int = 0
    # 12.26 非平凡 code_change 门槛（由调用方用 is_nontrivial_code_change 预计算）。
    is_nontrivial_code_change: bool = False
    # 12.13 盲法证据：首轮 verdict 是否早于 reveal（Treatment 样本的盲法成功分子）。
    verdict_before_reveal: bool = False
    # 12.10 额外确认的高风险缺陷数（Treatment 相对 Control 的附加成功路径）。
    confirmed_high_risk_defects: int = 0
    # 12.15 critical miss 是否因最小视图遗漏必要事实（事件型暂停触发输入）。
    has_critical_miss_missing_facts: bool = False
    # 12.6：保留首轮/最终 finding 原始事实，报告不能只输出派生比例。
    first_pass_findings: int = 0
    final_findings: int = 0

    @property
    def recall_denominator(self) -> int:
        """锁定 recall 分母（Requirement 12.6：verified_misses 用于锁定 recall 分母）。"""
        return self.verified_true_positives + self.verified_misses

    @property
    def fp_denominator(self) -> int:
        """误报率分母：首轮 finding 数；旧记录缺该字段时回退到 TP+FP。"""
        return self.first_pass_findings if self.first_pass_findings > 0 else (
            self.verified_false_positives + self.verified_true_positives
        )

    @property
    def reopened_or_rolled_back(self) -> bool:
        """该样本 apply 后是否发生 reopen 或 rollback（12.13 安全分子）。"""
        return (self.reopen_events + self.post_apply_rollbacks) > 0

    @classmethod
    def from_review_metrics_record(
        cls,
        record: Dict[str, Any],
        *,
        is_nontrivial_code_change: bool = False,
        verdict_before_reveal: bool = False,
        confirmed_high_risk_defects: int = 0,
        has_critical_miss_missing_facts: bool = False,
    ) -> "SampleRecord":
        """从 review_metrics JSONL 记录构造 SampleRecord。

        非平凡门槛、verdict-before-reveal、高风险缺陷数与 critical-miss 标志由调用方
        从对应记录（task_symbol_changes / reveal_event / 人工核实）预计算后传入，
        保持本结构对原始计数记录的单一职责。缺少必需字段 → fail-closed 抛 EvaluatorError。
        """
        try:
            group = str(record["group"])
            task_id = str(record["task_id"])
        except (KeyError, TypeError) as exc:
            raise EvaluatorError(
                make_evaluator_reason(
                    EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                    detail=f"review_metrics 记录缺少 group/task_id: {exc}",
                )
            )
        if group not in (GroupAssignment.CONTROL.value, GroupAssignment.TREATMENT.value):
            raise EvaluatorError(
                make_evaluator_reason(
                    EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                    detail=f"非法 group 取值: {group!r}",
                )
            )
        def _nonnegative_int(name: str, default: int = 0) -> int:
            try:
                value = int(record.get(name, default))
            except (TypeError, ValueError) as exc:
                raise EvaluatorError(make_evaluator_reason(
                    EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                    detail=f"{name} 不是整数: {exc}"))
            if value < 0:
                raise EvaluatorError(make_evaluator_reason(
                    EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                    detail=f"{name} 不能为负数: {value}"))
            return value

        def _nonnegative_float(name: str, default: float = 0.0) -> float:
            try:
                value = float(record.get(name, default))
            except (TypeError, ValueError) as exc:
                raise EvaluatorError(make_evaluator_reason(
                    EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                    detail=f"{name} 不是数字: {exc}"))
            if not math.isfinite(value) or value < 0:
                raise EvaluatorError(make_evaluator_reason(
                    EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                    detail=f"{name} 必须是有限非负数: {value}"))
            return value

        if confirmed_high_risk_defects < 0:
            raise EvaluatorError(make_evaluator_reason(
                EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                detail=f"confirmed_high_risk_defects 不能为负数: {confirmed_high_risk_defects}"))

        return cls(
            task_id=task_id,
            group=group,
            first_pass_findings=_nonnegative_int("first_pass_findings"),
            final_findings=_nonnegative_int("final_findings"),
            verified_true_positives=_nonnegative_int("verified_true_positives"),
            verified_false_positives=_nonnegative_int("verified_false_positives"),
            verified_misses=_nonnegative_int("verified_misses"),
            review_duration_seconds=_nonnegative_float("review_duration_seconds"),
            token_usage=_nonnegative_int("token_usage"),
            reopen_events=_nonnegative_int("reopen_events"),
            post_apply_defects=_nonnegative_int("post_apply_defects"),
            post_apply_rollbacks=_nonnegative_int("post_apply_rollbacks"),
            is_nontrivial_code_change=bool(is_nontrivial_code_change),
            verdict_before_reveal=bool(verdict_before_reveal),
            confirmed_high_risk_defects=int(confirmed_high_risk_defects),
            has_critical_miss_missing_facts=bool(has_critical_miss_missing_facts),
        )


@dataclass
class GroupMetrics:
    """单组（Control 或 Treatment）的聚合指标（Requirement 12.6 / 12.22）。

    同时保留绝对计数（分子/分母）与派生比例及其置信区间，供报告输出（12.22）与
    成功/灰区/暂停判定（12.10–12.20, 12.27–12.28）消费。
    """

    group: str
    valid_n: int = 0
    # 缺陷检测（12.10）：recall = tp / (tp + misses)。
    true_positives: int = 0
    verified_misses: int = 0
    recall: float = 0.0
    recall_ci: Tuple[float, float] = (0.0, 0.0)
    confirmed_high_risk_defects: int = 0
    # 误报（12.11 / 12.16 / 12.27）：fp_rate = fp / (fp + tp)。
    false_positives: int = 0
    false_positive_rate: float = 0.0
    fp_rate_ci: Tuple[float, float] = (0.0, 0.0)
    # 时延（12.12 / 12.17 / 12.28）。
    median_latency_seconds: float = 0.0
    p90_latency_seconds: float = 0.0
    # 安全与盲法（12.13）。
    reopen_rollback_count: int = 0
    reopen_rollback_rate: float = 0.0
    reopen_rollback_ci: Tuple[float, float] = (0.0, 0.0)
    blinding_success_count: int = 0
    blinding_success_rate: float = 0.0
    blinding_ci: Tuple[float, float] = (0.0, 0.0)
    # 非平凡 code_change 样本数（12.9 / 12.26）。
    nontrivial_code_change_count: int = 0
    # apply 后缺陷/回滚原始计数（12.6）。
    post_apply_defects: int = 0
    post_apply_rollbacks: int = 0
    # 12.6 原始事实置于末尾，保持旧 positional API。
    first_pass_findings: int = 0
    final_findings: int = 0
    token_usage: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # asdict 已把 tuple 转为 list；显式还原为 [low, high] 便于阅读。
        d["recall_ci"] = list(self.recall_ci)
        d["fp_rate_ci"] = list(self.fp_rate_ci)
        d["reopen_rollback_ci"] = list(self.reopen_rollback_ci)
        d["blinding_ci"] = list(self.blinding_ci)
        return d


def _safe_rate(numerator: int, denominator: int) -> float:
    """分母为 0 时返回 0.0，避免 ZeroDivisionError（小样本诚实报告，不伪造）。"""
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _percentile(values: Sequence[float], pct: float) -> float:
    """计算百分位数（线性插值）。空序列返回 0.0。

    pct 取 0–100。P90 即 pct=90。使用 statistics.quantiles 的 inclusive 方法，
    与 median 一致地对小样本稳健。
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    # quantiles(data, n=100, method='inclusive') 返回 99 个分位点，索引 pct-1 对应 P{pct}。
    qs = statistics.quantiles(list(values), n=100, method="inclusive")
    idx = max(0, min(len(qs) - 1, int(round(pct)) - 1))
    return float(qs[idx])


def compute_group_metrics(group: str, samples: Sequence[SampleRecord]) -> GroupMetrics:
    """聚合单组指标（Requirement 12.6 / 12.22）。

    只统计 group 匹配的有效样本；invalid 样本应在调用方先行剔除（12.8），不进入本函数。
    所有比例附 Wilson 置信区间；latency 报告 median 与 P90 点估计 + 样本数。
    """
    own = [s for s in samples if s.group == group]
    m = GroupMetrics(group=group)
    m.valid_n = len(own)
    if not own:
        return m

    tp = sum(s.verified_true_positives for s in own)
    misses = sum(s.verified_misses for s in own)
    fp = sum(s.verified_false_positives for s in own)
    m.first_pass_findings = sum(s.first_pass_findings for s in own)
    m.final_findings = sum(s.final_findings for s in own)
    m.token_usage = sum(s.token_usage for s in own)
    m.true_positives = tp
    m.verified_misses = misses
    m.false_positives = fp
    m.confirmed_high_risk_defects = sum(s.confirmed_high_risk_defects for s in own)

    # recall = tp / (tp + misses)，分母为锁定 recall 分母（12.6）。
    recall_denom = tp + misses
    m.recall = _safe_rate(tp, recall_denom)
    m.recall_ci = wilson_confidence_interval(tp, recall_denom)

    # 误报分母按 Requirement 12.6 使用首轮 finding 数；旧 JSONL 未保存该字段时，
    # 回退到 verified TP+FP，保持既有 P0 记录可重放。
    fp_denom = m.first_pass_findings if m.first_pass_findings > 0 else fp + tp
    m.false_positive_rate = _safe_rate(fp, fp_denom)
    m.fp_rate_ci = wilson_confidence_interval(fp, fp_denom)

    # 时延：median 与 P90。
    durations = [s.review_duration_seconds for s in own]
    m.median_latency_seconds = float(statistics.median(durations))
    m.p90_latency_seconds = _percentile(durations, 90.0)

    # 安全：reopen/rollback 率（样本级：发生 reopen 或 rollback 的样本占比）。
    rr = sum(1 for s in own if s.reopened_or_rolled_back)
    m.reopen_rollback_count = rr
    m.reopen_rollback_rate = _safe_rate(rr, m.valid_n)
    m.reopen_rollback_ci = wilson_confidence_interval(rr, m.valid_n)

    # 盲法：verdict-before-reveal 比例（Treatment 样本的盲法成功分子，12.13）。
    blind = sum(1 for s in own if s.verdict_before_reveal)
    m.blinding_success_count = blind
    m.blinding_success_rate = _safe_rate(blind, m.valid_n)
    m.blinding_ci = wilson_confidence_interval(blind, m.valid_n)

    m.nontrivial_code_change_count = sum(1 for s in own if s.is_nontrivial_code_change)
    m.post_apply_defects = sum(s.post_apply_defects for s in own)
    m.post_apply_rollbacks = sum(s.post_apply_rollbacks for s in own)
    return m


def relative_change(treatment_value: float, control_value: float) -> float:
    """相对变化 (treatment - control) / control（Requirement 12.10 / 12.12 的相对口径）。

    control_value<=0 时：treatment_value>0 返回 +inf（视为无限增幅，fail-closed 不放过），
    二者皆 0 返回 0.0。避免除零并保守判定。
    """
    if control_value <= 0:
        if treatment_value > 0:
            return math.inf
        return 0.0
    return (treatment_value - control_value) / control_value


# ---------------------------------------------------------------------------
# 成功判定（Requirement 12.9–12.14）
# ---------------------------------------------------------------------------


@dataclass
class SuccessEvaluation:
    """成功判定结论（Requirement 12.9–12.14）。

    directional_only=True 表示样本不足，仅报告方向、不授权 P1（12.9）。
    eligible_for_p1=True 表示最小样本与全部成功条件满足，批次具备 P1 决策资格——
    这是决策资格而非 P1 已实现（12.14 / 12.23）。
    """

    directional_only: bool = False
    min_sample_satisfied: bool = False
    defect_detection_satisfied: bool = False
    false_positive_satisfied: bool = False
    latency_satisfied: bool = False
    safety_blinding_satisfied: bool = False
    eligible_for_p1: bool = False
    # 各条件的结构化说明（含未满足原因），供报告与 CLI 输出。
    reasons: List[Dict[str, Any]] = field(default_factory=list)
    # 样本不足时的结构化原因（12.9）。
    insufficient_reason: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "directional_only": self.directional_only,
            "min_sample_satisfied": self.min_sample_satisfied,
            "defect_detection_satisfied": self.defect_detection_satisfied,
            "false_positive_satisfied": self.false_positive_satisfied,
            "latency_satisfied": self.latency_satisfied,
            "safety_blinding_satisfied": self.safety_blinding_satisfied,
            "eligible_for_p1": self.eligible_for_p1,
            "reasons": list(self.reasons),
            "insufficient_reason": self.insufficient_reason,
        }


def evaluate_success(
    control: GroupMetrics,
    treatment: GroupMetrics,
    thresholds: SuccessThresholds,
    valid_task_count: int,
    nontrivial_code_change_count: int,
    batch_id: str = "",
    *,
    gray_zone: Optional["GrayZoneEvaluation"] = None,
) -> SuccessEvaluation:
    """评估全部成功条件（Requirement 12.9–12.14）。

    Args:
        control / treatment: 两组聚合指标（compute_group_metrics 输出）。
        thresholds: 冻结的成功阈值（随批次锁定）。
        valid_task_count: 有效任务总数（两组 valid_n 之和，invalid 已排除）。
        nontrivial_code_change_count: 非平凡 code_change 任务总数（两组之和）。
        batch_id: 用于结构化原因上下文。

    最小样本不足（12.9）→ directional_only=True 且 eligible_for_p1=False，附
    INSUFFICIENT_SAMPLE 原因；此时仍计算各条件供方向性参考，但不授权 P1。
    """
    result = SuccessEvaluation()

    # 12.9 最小样本。
    min_ok = (
        valid_task_count >= thresholds.min_valid_tasks
        and nontrivial_code_change_count >= thresholds.min_nontrivial_code_change_tasks
    )
    result.min_sample_satisfied = min_ok
    if not min_ok:
        result.directional_only = True
        result.insufficient_reason = make_evaluator_reason(
            EvaluatorErrorCode.INSUFFICIENT_SAMPLE,
            severity="warning",
            batch_id=batch_id,
            valid_tasks=valid_task_count,
            min_valid_tasks=thresholds.min_valid_tasks,
            nontrivial=nontrivial_code_change_count,
            min_nontrivial=thresholds.min_nontrivial_code_change_tasks,
        ).to_dict()

    # 12.10 缺陷检测：recall 相对 Control 提升 >= 阈值（相对变化，非百分点），
    # 或额外确认高风险缺陷 >= 阈值且 critical miss 未增加。
    recall_improvement = relative_change(treatment.recall, control.recall)
    recall_path = recall_improvement >= thresholds.recall_relative_improvement_min
    extra_defects_path = (
        treatment.confirmed_high_risk_defects >= thresholds.additional_high_risk_defects_min
        and treatment.verified_misses <= control.verified_misses
    )
    result.defect_detection_satisfied = bool(recall_path or extra_defects_path)
    result.reasons.append({
        "condition": "defect_detection",
        "satisfied": result.defect_detection_satisfied,
        "recall_relative_improvement": recall_improvement,
        "recall_relative_improvement_min": thresholds.recall_relative_improvement_min,
        "confirmed_high_risk_defects": treatment.confirmed_high_risk_defects,
        "additional_high_risk_defects_min": thresholds.additional_high_risk_defects_min,
    })

    # 12.11 误报：Treatment fp_rate - Control fp_rate（绝对差）<= 阈值。
    fp_abs_diff = treatment.false_positive_rate - control.false_positive_rate
    result.false_positive_satisfied = fp_abs_diff <= thresholds.false_positive_rate_abs_diff_max
    result.reasons.append({
        "condition": "false_positive",
        "satisfied": result.false_positive_satisfied,
        "fp_abs_diff": fp_abs_diff,
        "false_positive_rate_abs_diff_max": thresholds.false_positive_rate_abs_diff_max,
    })

    # 12.12 时延：median 与 P90 相对 Control 增幅上限（相对变化）。
    median_increase = relative_change(treatment.median_latency_seconds, control.median_latency_seconds)
    p90_increase = relative_change(treatment.p90_latency_seconds, control.p90_latency_seconds)
    result.latency_satisfied = (
        median_increase <= thresholds.median_latency_relative_increase_max
        and p90_increase <= thresholds.p90_latency_relative_increase_max
    )
    result.reasons.append({
        "condition": "latency",
        "satisfied": result.latency_satisfied,
        "median_relative_increase": median_increase,
        "median_latency_relative_increase_max": thresholds.median_latency_relative_increase_max,
        "p90_relative_increase": p90_increase,
        "p90_latency_relative_increase_max": thresholds.p90_latency_relative_increase_max,
    })

    # 12.13 安全与盲法：reopen/rollback 率不高于 Control（差 <= 阈值，默认 0.0 即不高于），
    # 且 verdict-before-reveal 比例 >= 阈值。
    rr_diff = treatment.reopen_rollback_rate - control.reopen_rollback_rate
    safety_ok = rr_diff <= thresholds.reopen_rollback_rate_above_control_max
    blinding_ok = treatment.blinding_success_rate >= thresholds.blinding_verdict_before_reveal_min
    result.safety_blinding_satisfied = bool(safety_ok and blinding_ok)
    result.reasons.append({
        "condition": "safety_blinding",
        "satisfied": result.safety_blinding_satisfied,
        "reopen_rollback_rate_diff": rr_diff,
        "reopen_rollback_rate_above_control_max": thresholds.reopen_rollback_rate_above_control_max,
        "blinding_success_rate": treatment.blinding_success_rate,
        "blinding_verdict_before_reveal_min": thresholds.blinding_verdict_before_reveal_min,
    })

    # 12.14 资格：最小样本 + 全部成功条件；灰区未解决或暂停时不得授权 P1。
    result.eligible_for_p1 = (
        min_ok
        and result.defect_detection_satisfied
        and result.false_positive_satisfied
        and result.latency_satisfied
        and result.safety_blinding_satisfied
        and (gray_zone is None or gray_zone.authorized_for_p1)
    )
    if gray_zone is not None and not gray_zone.authorized_for_p1:
        result.reasons.append(make_evaluator_reason(
            EvaluatorErrorCode.GRAY_ZONE_UNRESOLVED,
            severity="warning",
            batch_id=batch_id,
            zones=", ".join(
                name for name, active in (
                    ("false_positive", gray_zone.fp_gray_zone),
                    ("latency", gray_zone.latency_gray_zone),
                ) if active
            ) or "unresolved",
        ).to_dict())
    return result


# ---------------------------------------------------------------------------
# 灰区标记（Requirement 12.27–12.29）
# ---------------------------------------------------------------------------


@dataclass
class GrayZoneEvaluation:
    """灰区评估结论（Requirement 12.27–12.29）。

    gray_zone=True 表示落入灰区：标记批次未授权 P1、继续纳样并记录为灰区观察。
    灰区本身不触发暂停，暂停触发器仍只有 12.15–12.20（12.29）。
    """

    gray_zone: bool = False
    fp_gray_zone: bool = False
    latency_gray_zone: bool = False
    gray_zone_unresolved: bool = False
    authorized_for_p1: bool = True
    # 灰区观察的结构化原因（warning，非阻断）。
    observations: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gray_zone": self.gray_zone,
            "fp_gray_zone": self.fp_gray_zone,
            "latency_gray_zone": self.latency_gray_zone,
            "gray_zone_unresolved": self.gray_zone_unresolved,
            "authorized_for_p1": self.authorized_for_p1,
            "observations": list(self.observations),
        }


def evaluate_gray_zone(
    control: GroupMetrics,
    treatment: GroupMetrics,
    thresholds: SuccessThresholds,
    pause_thresholds: PauseThresholds,
    batch_id: str = "",
    *,
    resolved: bool = False,
) -> GrayZoneEvaluation:
    """评估灰区（Requirement 12.27–12.29）。

    误报灰区（12.27）：Treatment fp_rate 高出 Control > false_positive_rate_abs_diff_max（10pp）
    且 <= fp_exceed_control_abs_diff_pause（20pp），按绝对差。
    时延灰区（12.28）：median 相对增幅 > median_latency_relative_increase_max（25%）
    且 <= median_latency_relative_increase_pause（50%）。
    落入任一灰区 → authorized_for_p1=False、继续纳样、记录灰区观察；灰区不触发暂停。
    """
    result = GrayZoneEvaluation()

    fp_abs_diff = treatment.false_positive_rate - control.false_positive_rate
    if thresholds.false_positive_rate_abs_diff_max < fp_abs_diff <= pause_thresholds.fp_exceed_control_abs_diff_pause:
        result.fp_gray_zone = True
        result.observations.append(
            make_evaluator_reason(
                EvaluatorErrorCode.GRAY_ZONE_FP,
                severity="warning",
                batch_id=batch_id,
                diff_pp=round(fp_abs_diff * 100.0, 2),
            ).to_dict()
        )

    median_increase = relative_change(treatment.median_latency_seconds, control.median_latency_seconds)
    if thresholds.median_latency_relative_increase_max < median_increase <= pause_thresholds.median_latency_relative_increase_pause:
        result.latency_gray_zone = True
        result.observations.append(
            make_evaluator_reason(
                EvaluatorErrorCode.GRAY_ZONE_LATENCY,
                severity="warning",
                batch_id=batch_id,
                increase_pct=f"{round(median_increase * 100.0, 2)}%",
            ).to_dict()
        )

    result.gray_zone = result.fp_gray_zone or result.latency_gray_zone
    # 新观察默认未解决；只有调用方明确提供后续解决事实才可恢复 P1 资格。
    result.gray_zone_unresolved = result.gray_zone and not resolved
    result.authorized_for_p1 = not result.gray_zone_unresolved
    return result


# ---------------------------------------------------------------------------
# fail-safe 暂停状态机（Requirement 12.15–12.21 / 12.24）
# ---------------------------------------------------------------------------


@dataclass
class PauseEvaluation:
    """暂停评估结论（Requirement 12.15–12.20）。

    should_pause=True 时 trigger 为命中的 PauseTrigger（且仅是这六类）。调用方据此对
    ExperimentBatch.pause(trigger, reason, client_clock_time) 落库（12.21）；若无法记录，
    改用 halt_admission_fail_safe()（12.24）并返回 PAUSE_RECORD_FAILED。
    """

    should_pause: bool = False
    trigger: Optional[PauseTrigger] = None
    # 命中暂停的结构化原因（error）。
    reason: Optional[Dict[str, Any]] = None
    # 各触发器的评估明细，供报告。
    details: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "should_pause": self.should_pause,
            "trigger": (self.trigger.value if self.trigger else None),
            "reason": self.reason,
            "details": list(self.details),
        }


def evaluate_pause_conditions(
    control: GroupMetrics,
    treatment: GroupMetrics,
    pause_thresholds: PauseThresholds,
    *,
    invalid_sample_rate: float = 0.0,
    critical_miss_missing_facts: bool = False,
    disclosure_incident: bool = False,
    integrity_incident: bool = False,
    consecutive_fp_exceed_count: int = 0,
    consecutive_latency_weeks: int = 0,
    snapshot_drift_unattributable_rate: float = 0.0,
    batch_id: str = "",
) -> PauseEvaluation:
    """评估六个暂停触发器（Requirement 12.15–12.20），命中任一即 should_pause=True。

    事件型触发器（12.15 critical miss / 12.18 披露事件 / 12.20 完整性事件）由调用方以布尔传入。
    阈值型触发器：
        - 12.16 连续 consecutive_fp_exceed_count >= fp_consecutive_samples 个 Treatment 样本
          误报率超 Control fp_exceed_control_abs_diff_pause（20pp，绝对差）。
        - 12.17 median latency 连续 consecutive_latency_weeks >= median_latency_consecutive_weeks 周
          增幅 > median_latency_relative_increase_pause（50%），或 invalid_sample_rate > invalid_sample_rate_pause（30%）。
        - 12.19 snapshot_drift_unattributable_rate > snapshot_drift_unattributable_pause（20%）。

    灰区（12.27–12.29）不是暂停触发器，本函数不评估灰区。
    触发器按 12.15→12.20 顺序检查，返回首个命中者（确定性）。
    """
    result = PauseEvaluation()

    def _hit(trigger: PauseTrigger, detail: Dict[str, Any]) -> None:
        detail = dict(detail)
        detail["trigger"] = trigger.value
        result.details.append(detail)
        if not result.should_pause:
            result.should_pause = True
            result.trigger = trigger
            result.reason = make_evaluator_reason(
                EvaluatorErrorCode.PAUSE_TRIGGERED,
                severity="error",
                batch_id=batch_id,
                trigger=trigger.value,
            ).to_dict()

    # 12.15 critical miss（事件型）。
    if critical_miss_missing_facts:
        _hit(PauseTrigger.CRITICAL_MISS_MISSING_FACTS, {
            "condition": "critical_miss_missing_facts",
            "description": "Treatment 出现 Control 未出现的 critical miss，根因是最小视图遗漏必要事实",
        })

    # 12.16 连续误报超 20pp。
    fp_exceed = (treatment.false_positive_rate - control.false_positive_rate) > pause_thresholds.fp_exceed_control_abs_diff_pause
    if fp_exceed and consecutive_fp_exceed_count >= pause_thresholds.fp_consecutive_samples:
        _hit(PauseTrigger.FP_RATE_EXCEED_20PP_CONSECUTIVE, {
            "condition": "fp_rate_exceed_20pp_consecutive",
            "consecutive_fp_exceed_count": consecutive_fp_exceed_count,
            "fp_consecutive_samples": pause_thresholds.fp_consecutive_samples,
            "fp_abs_diff": treatment.false_positive_rate - control.false_positive_rate,
        })
    else:
        result.details.append({
            "trigger": PauseTrigger.FP_RATE_EXCEED_20PP_CONSECUTIVE.value,
            "condition": "fp_rate_exceed_20pp_consecutive",
            "hit": False,
            "fp_exceed": fp_exceed,
            "consecutive_fp_exceed_count": consecutive_fp_exceed_count,
        })

    # 12.17 时延连续两周增幅 >50%，或 invalid 率 >30%。
    median_increase = relative_change(treatment.median_latency_seconds, control.median_latency_seconds)
    latency_hit = (
        median_increase > pause_thresholds.median_latency_relative_increase_pause
        and consecutive_latency_weeks >= pause_thresholds.median_latency_consecutive_weeks
    )
    invalid_hit = invalid_sample_rate > pause_thresholds.invalid_sample_rate_pause
    if latency_hit or invalid_hit:
        _hit(PauseTrigger.LATENCY_OR_INVALID_RATE, {
            "condition": "latency_or_invalid_rate",
            "median_relative_increase": median_increase,
            "consecutive_latency_weeks": consecutive_latency_weeks,
            "invalid_sample_rate": invalid_sample_rate,
            "latency_hit": latency_hit,
            "invalid_hit": invalid_hit,
        })
    else:
        result.details.append({
            "trigger": PauseTrigger.LATENCY_OR_INVALID_RATE.value,
            "condition": "latency_or_invalid_rate",
            "hit": False,
            "median_relative_increase": median_increase,
            "invalid_sample_rate": invalid_sample_rate,
        })

    # 12.18 披露事件（事件型）。
    if disclosure_incident:
        _hit(PauseTrigger.DISCLOSURE_INCIDENT, {
            "condition": "disclosure_incident",
            "description": "Implementer_Notes、既有 verdict 或敏感推理被泄露到 Treatment blind view",
        })

    # 12.19 snapshot 漂移不可归因 >20%。
    if snapshot_drift_unattributable_rate > pause_thresholds.snapshot_drift_unattributable_pause:
        _hit(PauseTrigger.SNAPSHOT_DRIFT_UNATTRIBUTABLE, {
            "condition": "snapshot_drift_unattributable",
            "snapshot_drift_unattributable_rate": snapshot_drift_unattributable_rate,
            "snapshot_drift_unattributable_pause": pause_thresholds.snapshot_drift_unattributable_pause,
        })
    else:
        result.details.append({
            "trigger": PauseTrigger.SNAPSHOT_DRIFT_UNATTRIBUTABLE.value,
            "condition": "snapshot_drift_unattributable",
            "hit": False,
            "snapshot_drift_unattributable_rate": snapshot_drift_unattributable_rate,
        })

    # 12.20 完整性事件（事件型）。
    if integrity_incident:
        _hit(PauseTrigger.FABRICATED_INDEPENDENCE_OR_EVIDENCE, {
            "condition": "fabricated_independence_or_evidence",
            "description": "实验流程诱导伪造独立性或伪造证据",
        })

    return result


def admit_sample(
    batch: ExperimentBatch,
    *,
    client_clock_time: str,
    sample_eligible: bool = True,
    persist_batch: Optional[Any] = None,
) -> ExperimentBatch:
    """以 fail-safe 方式执行一次纳样状态转换（Requirements 12.3/12.21/12.24）。

    先检查批次是否允许纳样，再执行首次纳样；若调用方提供的持久化回调失败，立即
    设置 ``admission_halted`` 并抛出稳定的 ``PAUSE_RECORD_FAILED``，拒绝后续纳样。
    ``persist_batch`` 只负责把已变更的文件配置持久化，评估器不触碰数据库/schema。
    """
    if not sample_eligible:
        raise ExperimentProtocolError(make_reason(
            ExperimentErrorCode.INELIGIBLE_SAMPLE,
            task_id="",
            reason="sample_failed_inclusion_rules",
        ))
    batch.ensure_admission_allowed()
    try:
        batch.mark_first_admission(client_clock_time)
        if persist_batch is not None:
            persist_batch(batch)
    except Exception as exc:
        # 任何状态落盘失败都不能继续纳样；这是 12.24 的 fail-safe 默认拒绝。
        batch.halt_admission_fail_safe()
        if isinstance(exc, ExperimentProtocolError) and exc.reason.code == ExperimentErrorCode.PAUSE_RECORD_FAILED:
            raise
        raise _pause_record_failed(batch.batch_id) from exc
    return batch


def apply_pause_to_batch(
    batch: ExperimentBatch,
    pause: PauseEvaluation,
    client_clock_time: str,
    *,
    persist_pause: bool = True,
) -> None:
    """把暂停结论落到批次（Requirement 12.21 / 12.24）。

    persist_pause=True（默认）：调用 batch.pause(trigger, reason, client_clock_time)，保留批次
    记录与锁定的指标定义/分母/观察窗/阈值（12.21），暂停后规则变化须新批次。
    persist_pause=False 或 pause 无 trigger：模拟“暂停无法记录/执行”，调用
    batch.halt_admission_fail_safe()（12.24 fail-safe 停止新纳样），并抛
    protocol.ExperimentProtocolError(PAUSE_RECORD_FAILED)——不静默吞掉。
    """
    if not pause.should_pause or pause.trigger is None:
        return
    reason_text = (pause.reason or {}).get("message") or pause.trigger.value
    if not persist_pause:
        batch.halt_admission_fail_safe()
        raise _pause_record_failed(batch.batch_id)
    try:
        batch.pause(pause.trigger, reason_text, client_clock_time)
    except Exception as exc:
        # 暂停状态无法写入时仍必须停止新纳样，不能让记录失败变成继续运行。
        batch.halt_admission_fail_safe()
        raise _pause_record_failed(batch.batch_id) from exc


def _pause_record_failed(batch_id: str):
    """构造 PAUSE_RECORD_FAILED 异常（复用 protocol 已登记错误码，Requirement 12.24）。"""
    from .blind_review_protocol import ExperimentProtocolError

    return ExperimentProtocolError(
        make_reason(ExperimentErrorCode.PAUSE_RECORD_FAILED, batch_id=batch_id)
    )


# ---------------------------------------------------------------------------
# invalid 样本率与原因统计（Requirement 12.8 / 12.22）
# ---------------------------------------------------------------------------


def compute_invalid_sample_stats(
    invalid_reason_codes: Sequence[str],
    valid_sample_count: int,
) -> Tuple[float, Dict[str, int]]:
    """计算 invalid 样本率与每个 invalid 原因计数（Requirement 12.8 / 12.22）。

    invalid 样本率 = invalid 数 / (invalid 数 + valid 数)。invalid 样本排除出效果估计与
    全部成功/暂停指标的分子分母，仅计入本比率（12.8）。每个 invalid 原因都可见（12.22）。
    """
    if valid_sample_count < 0:
        raise EvaluatorError(make_evaluator_reason(
            EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
            detail=f"valid_sample_count 不能为负数: {valid_sample_count}"))
    if any(not isinstance(code, str) or not code for code in invalid_reason_codes):
        raise EvaluatorError(make_evaluator_reason(
            EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
            detail="invalid 样本必须携带非空 reason code"))
    counts: Dict[str, int] = {}
    for code in invalid_reason_codes:
        counts[code] = counts.get(code, 0) + 1
    invalid_n = len(invalid_reason_codes)
    total = invalid_n + valid_sample_count
    rate = _safe_rate(invalid_n, total)
    return rate, counts


# ---------------------------------------------------------------------------
# 报告聚合（Requirement 12.22 / 12.23）
# ---------------------------------------------------------------------------


def build_evaluation_report(
    batch_id: str,
    control: GroupMetrics,
    treatment: GroupMetrics,
    success: SuccessEvaluation,
    gray_zone: GrayZoneEvaluation,
    pause: PauseEvaluation,
    invalid_reason_counts: Dict[str, int],
    invalid_sample_rate: float,
    metric_definitions: Sequence[MetricDefinition],
    observation_windows: Sequence[ObservationWindow],
    *,
    valid_task_count: int = 0,
    nontrivial_code_change_count: int = 0,
    client_clock_time: Optional[float] = None,
) -> Dict[str, Any]:
    """聚合完整评估报告（Requirement 12.22 / 12.23）。

    输出每个锁定指标定义与观察窗口、两组绝对分子/分母 + 比例 + 置信区间、每个 invalid 原因
    计数、directional_only 标志、eligible_for_p1、灰区观察、暂停状态。全部记录标记
    non_product_evidence=True 且 is_product_evidence=False——不得支撑 P1 hard gate（12.23），
    eligible_for_p1 仅表示 P1 决策资格而非 P1 已实现（12.14）。
    """
    import time as _time

    # 灰区未解决、暂停或 directional-only 任一存在时均不得输出可授权的 P1 资格。
    report_eligible_for_p1 = bool(
        success.eligible_for_p1
        and gray_zone.authorized_for_p1
        and not pause.should_pause
        and not success.directional_only
    )

    return {
        "record_type": "evaluation_report",
        "batch_id": batch_id,
        "client_clock_time": client_clock_time if client_clock_time is not None else _time.time(),
        # 12.22：锁定指标定义与观察窗口随报告输出。
        "metric_definitions": [m.to_dict() for m in metric_definitions],
        "observation_windows": [w.to_dict() for w in observation_windows],
        # 12.22：两组绝对计数/比例/置信区间。
        "groups": {
            GroupAssignment.CONTROL.value: control.to_dict(),
            GroupAssignment.TREATMENT.value: treatment.to_dict(),
        },
        "valid_task_count": valid_task_count,
        "nontrivial_code_change_count": nontrivial_code_change_count,
        # 12.8 / 12.22：invalid 样本率与每个原因。
        "invalid_sample_rate": invalid_sample_rate,
        "invalid_reason_counts": dict(invalid_reason_counts),
        # 12.9–12.14 / 12.27–12.29 / 12.15–12.20 结论。
        "success": success.to_dict(),
        "gray_zone": gray_zone.to_dict(),
        "pause": pause.to_dict(),
        "directional_only": success.directional_only,
        "eligible_for_p1": report_eligible_for_p1,
        "authorization_blockers": {
            "gray_zone_unresolved": gray_zone.gray_zone_unresolved,
            "pause_triggered": pause.should_pause,
            "insufficient_sample": success.directional_only,
        },
        # 12.23：实验记录非产品 Evidence，不支撑 P1 hard gate。
        "is_product_evidence": False,
        "non_product_evidence": True,
        NON_PRODUCT_EVIDENCE: True,
    }
