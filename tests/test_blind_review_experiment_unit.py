# -*- coding: utf-8 -*-
"""P0 盲评对照实验单元测试（Requirement 12.2–12.29）。

覆盖：分层随机可复现、协议锁定、无效样本排除、最小样本与非平凡门槛、
全部成功阈值、每个灰区边界、每个暂停阈值边界。

断言结构化状态/错误码，不只断言自然语言文本（AGENTS.md 规则 35）。
使用精确二进制分数避免 IEEE754 边界脆弱性。
"""
import sys
import os
import json
import pytest

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.blind_review_protocol import (
    ExperimentBatch, BatchProtocol, build_default_protocol,
    ExperimentProtocolError, ExperimentErrorCode,
    GroupAssignment, PauseTrigger, ToggleScope, ToggleValue,
    Experiment_Batch_Config, SuccessThresholds, PauseThresholds,
)
from experiments.blind_review_views import (
    ViewDisclosureError, ViewErrorCode,
    build_minimal_blind_view, build_verdict_change_record,
    BlindViewGroup, BlindViewPhase, BlindViewSourceFacts,
    assert_treatment_blind_purity, find_prior_review_contamination,
    VERDICT_CHANGE_REASONS,
)
from experiments.blind_review_jsonl import (
    ExperimentJsonlWriter, build_invalid_sample_record,
    build_review_metrics_record, build_incident_record,
    build_reveal_event_record, canonical_incident_record_type, write_evidence_bundle,
)
from experiments.blind_review_evaluator import (
    EvaluatorError, EvaluatorErrorCode,
    wilson_confidence_interval, is_nontrivial_code_change,
    SampleRecord, GroupMetrics, compute_group_metrics,
    evaluate_success, evaluate_gray_zone, evaluate_pause_conditions,
    compute_invalid_sample_stats, relative_change, validate_blind_view_records,
)
from experiments.admission_scope import ScopeContractError, validate_scope_contract
from experiments.truth_source_sync import (
    TruthSourceSyncError,
    validate_truth_source_change_set,
    validate_truth_sources,
)


# ===================================================================
# 辅助工具
# ===================================================================


def test_scope_contract_requires_declared_implementation_evidence():
    source = BlindViewSourceFacts(
        task_id="T-scope-1",
        change_audit_diffs=[{"file_path": "docs/design/plan.md"}],
    )
    with pytest.raises(ScopeContractError) as exc:
        validate_scope_contract(source, {
            "task_id": "T-scope-1",
            "profile": "code_change",
            "required_paths": ["rust_ext/src/lib.rs"],
            "allowed_paths": ["rust_ext/**", "tests/**"],
        })
    assert exc.value.code == "EXP_SCOPE_REQUIRED_PATH_MISSING"


def test_scope_contract_rejects_non_object_json():
    source = BlindViewSourceFacts(task_id="T-scope-invalid", change_audit_diffs=[{"file_path": "a.py"}])
    with pytest.raises(ScopeContractError) as exc:
        validate_scope_contract(source, [])
    assert exc.value.code == "EXP_SCOPE_CONTRACT_INVALID"


def test_code_change_scope_requires_a_tracked_implementation_path():
    """配置、文档或未跟踪源码不能替代真正的实现证据。"""
    source = BlindViewSourceFacts(
        task_id="T-scope-source",
        change_audit_diffs=[
            {"file_path": "docs/design/plan.md"},
            {"file_path": "pyproject.toml"},
        ],
    )
    with pytest.raises(ScopeContractError) as exc:
        validate_scope_contract(source, {
            "task_id": "T-scope-source",
            "profile": "code_change",
            "required_paths": ["pyproject.toml"],
            "allowed_paths": ["docs/**", "pyproject.toml"],
        })
    assert exc.value.code == "EXP_SCOPE_CODE_EVIDENCE_MISSING"

    source.change_audit_diffs.append({"file_path": "experiments/admission_scope.py"})
    with pytest.raises(ScopeContractError) as exc:
        validate_scope_contract(source, {
            "task_id": "T-scope-source",
            "profile": "code_change",
            "required_paths": ["pyproject.toml"],
            "allowed_paths": ["docs/**", "pyproject.toml", "experiments/**"],
        }, tracked_paths=["experiments/admission_scope.py"])
    assert exc.value.code == "EXP_SCOPE_IMPLEMENTATION_PATH_MISSING"

    with pytest.raises(ScopeContractError) as exc:
        validate_scope_contract(source, {
            "task_id": "T-scope-source",
            "profile": "code_change",
            "required_paths": ["experiments/admission_scope.py"],
            "allowed_paths": ["docs/**", "pyproject.toml", "experiments/**"],
        }, tracked_paths=[])
    assert exc.value.code == "EXP_SCOPE_SOURCE_NOT_TRACKED"


def test_scope_contract_rejects_scope_drift_and_accepts_declared_paths():
    source = BlindViewSourceFacts(
        task_id="T-scope-2",
        change_audit_diffs=[
            {"file_path": "release/inspect_pyinstaller_bundle.py"},
            {"file_path": "rust_ext/src/lib.rs"},
        ],
    )
    with pytest.raises(ScopeContractError) as exc:
        validate_scope_contract(source, {
            "task_id": "T-scope-2",
            "profile": "review",
            "required_paths": ["release/inspect_pyinstaller_bundle.py"],
            "allowed_paths": ["release/**", "tests/**"],
        })
    assert exc.value.code == "EXP_SCOPE_OUTSIDE_ALLOWED_PATHS"

    result = validate_scope_contract(source, {
        "task_id": "T-scope-2",
        "profile": "review",
        "required_paths": ["release/inspect_pyinstaller_bundle.py"],
        "allowed_paths": ["release/**", "rust_ext/**"],
    })
    assert result["source_file_count"] == 2


def test_inspector_scope_contract_rejects_missing_required_workflow_path():
    """artifact inspector 样本缺 workflow 验收时不得纳入盲视图。"""
    source = BlindViewSourceFacts(
        task_id="T-inspector-scope",
        change_audit_diffs=[
            {"file_path": "release/inspect_pyinstaller_bundle.py"},
            {"file_path": "tests/test_release_bundle_inspector.py"},
        ],
    )
    with pytest.raises(ScopeContractError) as exc:
        validate_scope_contract(source, _artifact_inspector_scope_contract())
    assert exc.value.code == "EXP_SCOPE_REQUIRED_PATH_MISSING"
    assert ".github/workflows/" in exc.value.detail


def test_inspector_scope_contract_rejects_rust_scope_drift():
    """inspector/验收任务混入 Rust 改造时必须 fail-closed。"""
    source = BlindViewSourceFacts(
        task_id="T-inspector-scope",
        change_audit_diffs=[
            {"file_path": "release/inspect_pyinstaller_bundle.py"},
            {"file_path": ".github/workflows/release.yml"},
            {"file_path": "tests/test_release_bundle_inspector.py"},
            {"file_path": "rust_ext/src/lib.rs"},
        ],
    )
    with pytest.raises(ScopeContractError) as exc:
        validate_scope_contract(source, _artifact_inspector_scope_contract())
    assert exc.value.code == "EXP_SCOPE_OUTSIDE_ALLOWED_PATHS"
    assert "rust_ext/src/lib.rs" in exc.value.detail


def test_inspector_only_scope_contract_is_admissible():
    """完整 inspector、workflow、验收测试的最小变更可纳样。"""
    source = BlindViewSourceFacts(
        task_id="T-inspector-scope",
        change_audit_diffs=[
            {"file_path": "release/inspect_pyinstaller_bundle.py"},
            {"file_path": ".github/workflows/release.yml"},
            {"file_path": "tests/test_release_bundle_inspector.py"},
            {"file_path": "docs/design/rust-only-parser-size-report.md"},
        ],
    )
    result = validate_scope_contract(
        source, _artifact_inspector_scope_contract(),
        tracked_paths=[
            "release/inspect_pyinstaller_bundle.py",
            "tests/test_release_bundle_inspector.py",
        ],
    )
    assert result["changed_paths"] == [
        ".github/workflows/release.yml",
        "docs/design/rust-only-parser-size-report.md",
        "release/inspect_pyinstaller_bundle.py",
        "tests/test_release_bundle_inspector.py",
    ]


def _artifact_inspector_scope_contract():
    """与 docs/design/artifact-inspector-scope-contract.json 同构的测试契约。"""
    return {
        "task_id": "T-inspector-scope",
        "profile": "code_change",
        "required_paths": [
            "release/inspect_pyinstaller_bundle.py",
            ".github/workflows/",
            "tests/test_release_bundle_inspector.py",
        ],
        "allowed_paths": [
            "release/",
            ".github/workflows/",
            "tests/test_release_bundle_inspector.py",
            "docs/design/",
        ],
    }


def test_truth_source_sync_requires_all_three_documents(tmp_path):
    (tmp_path / "docs/design").mkdir(parents=True)
    for relative in (
        "docs/design/requirements.md",
        "docs/design/multi-llm-contract-driven-collaboration-design.md",
    ):
        (tmp_path / relative).write_text("G0 P0", encoding="utf-8")
    with pytest.raises(TruthSourceSyncError) as exc:
        validate_truth_sources(tmp_path, ["G0", "P0"])
    assert exc.value.code == "EXP_TRUTH_SOURCE_MISSING"


def test_truth_source_sync_rejects_partial_and_accepts_complete_set(tmp_path):
    (tmp_path / "docs/design").mkdir(parents=True)
    for relative in (
        "docs/design/requirements.md",
        "docs/design/multi-llm-contract-driven-collaboration-design.md",
        "docs/design/tasks.md",
    ):
        (tmp_path / relative).write_text("G0 P0", encoding="utf-8")
    result = validate_truth_sources(tmp_path, ["G0", "P0"])
    assert result["paths"][-1] == "docs/design/tasks.md"


def test_truth_source_sync_rejects_agents_only_change_set():
    """任务声称同步三份规格时，单改 AGENTS.md 不能算完成。"""
    with pytest.raises(TruthSourceSyncError) as exc:
        validate_truth_source_change_set(
            ["AGENTS.md"],
            ["docs/design/requirements.md", "docs/design/multi-llm-contract-driven-collaboration-design.md",
             "docs/design/tasks.md"],
            ["docs/design/requirements.md", "docs/design/multi-llm-contract-driven-collaboration-design.md",
             "docs/design/tasks.md"],
        )
    assert exc.value.code == "EXP_TRUTH_SOURCE_CHANGE_MISSING"


def test_truth_source_sync_requires_complete_contract_and_change_set():
    """required/allowed 与实际审计路径都覆盖三份规格才通过。"""
    paths = [
        "docs/design/requirements.md",
        "docs/design/multi-llm-contract-driven-collaboration-design.md",
        "docs/design/tasks.md",
    ]
    result = validate_truth_source_change_set(paths, paths, paths)
    assert result["truth_source_paths"] == paths

    with pytest.raises(TruthSourceSyncError) as exc:
        validate_truth_source_change_set(paths, paths, paths + ["AGENTS.md"])
    assert exc.value.code == "EXP_TRUTH_SOURCE_AGENTS_SUBSTITUTE_FORBIDDEN"

def _make_batch(seed=42, locked=True):
    """创建测试用批次。"""
    protocol = build_default_protocol(seed)
    batch = ExperimentBatch(
        batch_id="B-test-001",
        created_at="2026-01-01T00:00:00+00:00",
        protocol=protocol,
    )
    if locked:
        batch.lock_protocol()
    return batch


def _make_source_facts(task_id="T-test"):
    """构造最小来源事实。"""
    return BlindViewSourceFacts(
        task_id=task_id,
        task_title="测试任务",
        task_description="测试描述",
        step_targets=[{"file": "file_a.py", "symbol": "func_a"}],
        change_audit_diffs=[{"diff": "+1 line"}],
        symbol_changes=[{"name": "func_a", "type": "modified"}],
        test_runs_status=[{"status": "passed"}],
        open_quality_findings=[],
    )


def _gm(group, n=10, tp=8, misses=2, fp=2, latency=100.0,
        reopen=0, blinding=10, nontrivial=8):
    """快速构造 GroupMetrics。"""
    recall = tp / (tp + misses) if (tp + misses) > 0 else 0.0
    fp_rate = fp / (fp + tp) if (fp + tp) > 0 else 0.0
    reopen_rate = reopen / n if n > 0 else 0.0
    blinding_rate = blinding / n if n > 0 else 0.0
    return GroupMetrics(
        group=group, valid_n=n,
        true_positives=tp, verified_misses=misses,
        recall=recall, recall_ci=(0.0, 1.0),
        confirmed_high_risk_defects=0,
        false_positives=fp, false_positive_rate=fp_rate, fp_rate_ci=(0.0, 1.0),
        median_latency_seconds=latency, p90_latency_seconds=latency * 1.5,
        reopen_rollback_count=reopen, reopen_rollback_rate=reopen_rate,
        reopen_rollback_ci=(0.0, 1.0),
        blinding_success_count=blinding, blinding_success_rate=blinding_rate,
        blinding_ci=(0.0, 1.0),
        nontrivial_code_change_count=nontrivial,
        post_apply_defects=0, post_apply_rollbacks=0,
    )


# ===================================================================
# A) 分层随机可复现（Req 12.3）
# ===================================================================

class TestStratifiedRandom:
    """分层随机分组确定性与可复现。"""

    def test_same_seed_same_strata_same_group(self):
        """同 seed 同 strata_key → 同分组。"""
        p1 = build_default_protocol(42)
        p2 = build_default_protocol(42)
        a1 = p1.assign_group("task-abc")
        a2 = p2.assign_group("task-abc")
        assert a1 == a2

    def test_different_strata_may_differ(self):
        """不同 strata_key → 分组可能不同（统计意义上）。"""
        p = build_default_protocol(42)
        groups = {p.assign_group(f"key-{i}") for i in range(100)}
        # 100 个 key 应至少出现两种分组
        assert len(groups) == 2

    def test_assignment_is_enum(self):
        """分组结果为 GroupAssignment 枚举。"""
        p = build_default_protocol(7)
        a = p.assign_group("x")
        assert isinstance(a, GroupAssignment)
        assert a in (GroupAssignment.CONTROL, GroupAssignment.TREATMENT)

    def test_different_seed_may_differ(self):
        """不同 seed 对同一 key 可能给出不同分组。"""
        results = set()
        for seed in range(50):
            p = build_default_protocol(seed)
            results.add(p.assign_group("fixed-key"))
        assert len(results) == 2

    def test_paired_mode_forces_opposite_groups_for_same_strata(self):
        """paired 模式下同一 strata 的两个槽位必然一控一治。"""
        p = build_default_protocol(1599586622, assignment_mode="paired_v2")
        slot0 = p.assign_group("profile=code_change|risk=high", pair_slot=0, pair_id="pair-001")
        slot1 = p.assign_group("profile=code_change|risk=high", pair_slot=1, pair_id="pair-001")
        assert {slot0, slot1} == {GroupAssignment.CONTROL, GroupAssignment.TREATMENT}
        assert p.assign_group("profile=code_change|risk=high", pair_slot=0, pair_id="pair-001") == slot0

    def test_paired_mode_requires_pair_slot(self):
        """paired 模式拒绝缺失或非法槽位，避免回退到同组哈希。"""
        p = build_default_protocol(42, assignment_mode="paired_v2")
        with pytest.raises(ExperimentProtocolError):
            p.assign_group("same-strata", pair_slot=0)
        with pytest.raises(ExperimentProtocolError):
            p.assign_group("same-strata", pair_slot=2, pair_id="pair-001")
        with pytest.raises(ExperimentProtocolError):
            p.assign_group("same-strata", pair_slot=0, pair_id="")

    def test_legacy_hash_protocol_serialization_stays_compatible(self):
        """旧 hash 协议不因新增 paired 字段改变序列化指纹。"""
        legacy = build_default_protocol(42).to_dict()
        paired = build_default_protocol(42, assignment_mode="paired_v2").to_dict()
        assert "assignment_mode" not in legacy
        assert paired["assignment_mode"] == "paired_v2"

    def test_legacy_paired_protocol_remains_replayable(self):
        """旧批次 paired 语义不被 paired_v2 的 pair_id 改写。"""
        p = build_default_protocol(42, assignment_mode="paired")
        assert p.assign_group("same-strata", pair_slot=0) != p.assign_group("same-strata", pair_slot=1)


# ===================================================================
# B) 协议锁定（Req 12.3）
# ===================================================================

class TestProtocolLock:
    """协议锁定与冻结。"""

    def test_lock_returns_fingerprint(self):
        """lock_protocol 返回非空 fingerprint。"""
        batch = _make_batch(locked=False)
        fp = batch.lock_protocol()
        assert isinstance(fp, str)
        assert len(fp) > 0

    def test_locked_admission_allowed(self):
        """锁定后 ensure_admission_allowed 通过。"""
        batch = _make_batch(locked=True)
        batch.ensure_admission_allowed()  # 不抛异常

    def test_unlocked_admission_rejected(self):
        """未锁定时 ensure_admission_allowed 抛 PROTOCOL_NOT_LOCKED。"""
        batch = _make_batch(locked=False)
        with pytest.raises(ExperimentProtocolError) as exc_info:
            batch.ensure_admission_allowed()
        assert exc_info.value.reason.code == ExperimentErrorCode.PROTOCOL_NOT_LOCKED

    def test_frozen_after_first_admission(self):
        """首次纳样后冻结，再 lock 抛 BATCH_FROZEN。"""
        batch = _make_batch(locked=True)
        batch.mark_first_admission("2026-01-01T01:00:00+00:00")
        with pytest.raises(ExperimentProtocolError) as exc_info:
            batch.lock_protocol()
        assert exc_info.value.reason.code == ExperimentErrorCode.BATCH_FROZEN

    def test_paused_admission_rejected(self):
        """暂停后 ensure_admission_allowed 抛 BATCH_PAUSED。"""
        batch = _make_batch(locked=True)
        batch.pause(PauseTrigger.DISCLOSURE_INCIDENT, "test", "2026-01-01T02:00:00+00:00")
        with pytest.raises(ExperimentProtocolError) as exc_info:
            batch.ensure_admission_allowed()
        assert exc_info.value.reason.code == ExperimentErrorCode.BATCH_PAUSED


# ===================================================================
# C) 无效样本排除（Req 12.8/12.22）
# ===================================================================

class TestInvalidSample:
    """无效样本记录与统计。"""

    def test_invalid_sample_record_has_reason(self):
        """build_invalid_sample_record 保留 reason_code。"""
        rec = build_invalid_sample_record("T-1", "B-1", "SAME_SESSION", "detail")
        assert rec["invalid_reason_code"] == "SAME_SESSION"
        assert rec["non_product_evidence"] is True

    def test_compute_invalid_sample_stats(self):
        """compute_invalid_sample_stats 正确计算比率。"""
        codes = ["SAME_SESSION", "SAME_SESSION", "SNAPSHOT_CHANGED"]
        rate, counts = compute_invalid_sample_stats(codes, 10)
        # 分母 = valid + invalid = 10 + 3 = 13
        assert rate == pytest.approx(3.0 / 13.0)
        assert counts["SAME_SESSION"] == 2
        assert counts["SNAPSHOT_CHANGED"] == 1

    def test_invalid_rate_zero_when_no_invalid(self):
        """无无效样本时比率为 0。"""
        rate, counts = compute_invalid_sample_stats([], 20)
        assert rate == 0.0
        assert counts == {}

    def test_sample_record_fail_closed_missing_group(self):
        """SampleRecord.from_review_metrics_record 对缺失 group fail-closed。"""
        rec = {"task_id": "T-1", "batch_id": "B-1"}  # 缺 group
        with pytest.raises(EvaluatorError) as exc_info:
            SampleRecord.from_review_metrics_record(rec)
        assert exc_info.value.reason.code == EvaluatorErrorCode.EVALUATION_INPUT_INVALID


# ===================================================================
# D) 最小样本与非平凡门槛（Req 12.9/12.26）
# ===================================================================

class TestSampleThresholds:
    """最小样本量与非平凡 code change 门槛。"""

    def test_min_sample_not_satisfied(self):
        """valid_n < min_valid_tasks → min_sample_satisfied=False。"""
        c = _gm("control", n=5)
        t = _gm("treatment", n=5)
        thresholds = SuccessThresholds(min_valid_tasks=30, min_nontrivial_code_change_tasks=10)
        result = evaluate_success(c, t, thresholds, valid_task_count=10,
                                  nontrivial_code_change_count=5, batch_id="B")
        assert result.min_sample_satisfied is False
        assert result.directional_only is True

    def test_min_sample_satisfied(self):
        """valid_n >= min → min_sample_satisfied=True。"""
        c = _gm("control", n=20)
        t = _gm("treatment", n=20)
        thresholds = SuccessThresholds(min_valid_tasks=30, min_nontrivial_code_change_tasks=10)
        result = evaluate_success(c, t, thresholds, valid_task_count=40,
                                  nontrivial_code_change_count=20, batch_id="B")
        assert result.min_sample_satisfied is True

    def test_nontrivial_code_change_boundary(self):
        """is_nontrivial_code_change: >=10 行 AND 符号变更。"""
        assert is_nontrivial_code_change(10, True) is True
        assert is_nontrivial_code_change(9, True) is False
        assert is_nontrivial_code_change(10, False) is False
        assert is_nontrivial_code_change(100, True, is_formatting_only=True) is False
        assert is_nontrivial_code_change(100, True, is_generated=True) is False

    def test_insufficient_reason_present(self):
        """样本不足时 insufficient_reason 非空。"""
        c = _gm("control", n=2)
        t = _gm("treatment", n=2)
        thresholds = SuccessThresholds(min_valid_tasks=30)
        result = evaluate_success(c, t, thresholds, valid_task_count=4,
                                  nontrivial_code_change_count=2, batch_id="B")
        assert result.insufficient_reason is not None


# ===================================================================
# E) 全部成功阈值（Req 12.10-12.14）
# ===================================================================

class TestSuccessThresholds:
    """成功判定各阈值边界。"""

    def _eval(self, control, treatment):
        thresholds = SuccessThresholds(
            min_valid_tasks=10, min_nontrivial_code_change_tasks=5,
            recall_relative_improvement_min=0.15,
            false_positive_rate_abs_diff_max=0.1,
            median_latency_relative_increase_max=0.25,
            reopen_rollback_rate_above_control_max=0.0,
            blinding_verdict_before_reveal_min=0.9,
        )
        return evaluate_success(control, treatment, thresholds,
                                valid_task_count=40, nontrivial_code_change_count=20,
                                batch_id="B")

    def test_recall_improvement_pass(self):
        """recall 相对改善 >= 15% → eligible。"""
        # control recall=0.5, treatment recall=0.625 → 相对改善=0.25 >= 0.15
        c = _gm("control", tp=4, misses=4, fp=0, latency=100.0, blinding=10)
        t = _gm("treatment", tp=5, misses=3, fp=0, latency=100.0, blinding=10)
        result = self._eval(c, t)
        assert result.eligible_for_p1 is True

    def test_recall_improvement_fail(self):
        """recall 相对改善 < 15% → not eligible。"""
        # control recall=0.8, treatment recall=0.8 → 改善=0
        c = _gm("control", tp=8, misses=2, fp=0, latency=100.0, blinding=10)
        t = _gm("treatment", tp=8, misses=2, fp=0, latency=100.0, blinding=10)
        result = self._eval(c, t)
        assert result.eligible_for_p1 is False

    def test_empty_recall_denominator_is_not_zero_effect(self):
        """Treatment 无 TP/miss 时 12.10 必须报告不可估计，而不是 -1.0。"""
        control = _gm("control", tp=8, misses=2, fp=0, latency=100.0, blinding=10)
        treatment = _gm("treatment", tp=0, misses=0, fp=0, latency=100.0, blinding=10)
        result = self._eval(control, treatment)
        defect = next(r for r in result.reasons if r.get("condition") == "defect_detection")
        assert result.defect_detection_satisfied is False
        assert defect["recall_defined"] is False
        assert defect["recall_relative_improvement"] is None
        assert any(r.get("code") == EvaluatorErrorCode.DEFECT_COVERAGE_INSUFFICIENT
                   for r in result.reasons)

    def test_fp_abs_diff_pass(self):
        """FP 绝对差 <= 10pp → pass。"""
        # control fp_rate=0.25 (2/8), treatment fp_rate=0.3125 (2/6.4→用精确)
        # 用精确二进制：control fp=2,tp=8 → 0.2; treatment fp=2,tp=8 → 0.2; diff=0
        c = _gm("control", tp=8, misses=2, fp=2, latency=100.0, blinding=10)
        t = _gm("treatment", tp=8, misses=0, fp=2, latency=100.0, blinding=10)
        result = self._eval(c, t)
        # fp_rate control=2/10=0.2, treatment=2/10=0.2, diff=0 <= 0.1
        assert result.false_positive_satisfied is True

    def test_fp_abs_diff_fail(self):
        """FP 绝对差 > 10pp → fail。"""
        # control fp_rate=0.0, treatment fp_rate=0.125 (1/8)
        c = _gm("control", tp=8, misses=2, fp=0, latency=100.0, blinding=10)
        t = _gm("treatment", tp=7, misses=1, fp=1, latency=100.0, blinding=10)
        # treatment fp_rate = 1/8 = 0.125 > 0.1
        result = self._eval(c, t)
        assert result.false_positive_satisfied is False

    def test_latency_pass(self):
        """时延相对增幅 <= 25% → pass。"""
        c = _gm("control", tp=8, misses=0, fp=0, latency=100.0, blinding=10)
        t = _gm("treatment", tp=8, misses=0, fp=0, latency=125.0, blinding=10)
        result = self._eval(c, t)
        assert result.latency_satisfied is True

    def test_latency_fail(self):
        """时延相对增幅 > 25% → fail。"""
        c = _gm("control", tp=8, misses=0, fp=0, latency=100.0, blinding=10)
        t = _gm("treatment", tp=8, misses=0, fp=0, latency=126.0, blinding=10)
        result = self._eval(c, t)
        assert result.latency_satisfied is False

    def test_blinding_pass(self):
        """盲法成功率 >= 90% → pass。"""
        c = _gm("control", tp=8, misses=2, fp=0, latency=100.0, blinding=10)
        t = _gm("treatment", tp=8, misses=0, fp=0, latency=100.0, blinding=9)
        # blinding_rate = 9/10 = 0.9 >= 0.9
        result = self._eval(c, t)
        assert result.safety_blinding_satisfied is True

    def test_blinding_fail(self):
        """盲法成功率 < 90% → fail。"""
        c = _gm("control", tp=8, misses=2, fp=0, latency=100.0, blinding=10)
        t = _gm("treatment", tp=8, misses=0, fp=0, latency=100.0, blinding=8)
        # blinding_rate = 8/10 = 0.8 < 0.9
        result = self._eval(c, t)
        assert result.safety_blinding_satisfied is False


# ===================================================================
# F) 每个灰区边界（Req 12.27-12.29）
# ===================================================================

class TestGrayZone:
    """灰区边界测试。"""

    def _eval_gray(self, control, treatment):
        thresholds = SuccessThresholds(
            min_valid_tasks=10, min_nontrivial_code_change_tasks=5,
            false_positive_rate_abs_diff_max=0.1,
            median_latency_relative_increase_max=0.25,
        )
        pause_thresholds = PauseThresholds(
            fp_exceed_control_abs_diff_pause=0.2,
            median_latency_relative_increase_pause=0.5,
        )
        return evaluate_gray_zone(control, treatment, thresholds, pause_thresholds, batch_id="B")

    def test_fp_gray_zone_inside(self):
        """FP diff 在 (10pp, 20pp] → 灰区。"""
        # control fp_rate=0.25, treatment fp_rate=0.375 → diff=0.125 (12.5pp)
        c = _gm("control", tp=6, misses=2, fp=2)  # fp_rate=2/8=0.25
        t = _gm("treatment", tp=5, misses=2, fp=3)  # fp_rate=3/8=0.375
        result = self._eval_gray(c, t)
        assert result.fp_gray_zone is True
        assert result.gray_zone is True
        assert result.authorized_for_p1 is False

    def test_fp_below_gray_zone(self):
        """FP diff <= 10pp → 非灰区。"""
        # control fp_rate=0.25, treatment fp_rate=0.3125 → diff=0.0625
        c = _gm("control", tp=6, misses=2, fp=2)  # 0.25
        t = _gm("treatment", tp=5, misses=2, fp=2)  # 2/7≈0.286 → diff≈0.036
        result = self._eval_gray(c, t)
        assert result.fp_gray_zone is False

    def test_fp_above_gray_zone(self):
        """FP diff > 20pp → 非灰区（触发暂停而非灰区）。"""
        # control fp_rate=0.0, treatment fp_rate=0.25 → diff=0.25 > 0.20
        c = _gm("control", tp=8, misses=2, fp=0)
        t = _gm("treatment", tp=6, misses=2, fp=2)  # 2/8=0.25
        result = self._eval_gray(c, t)
        assert result.fp_gray_zone is False

    def test_latency_gray_zone_inside(self):
        """时延增幅在 (25%, 50%] → 灰区。"""
        c = _gm("control", tp=8, misses=2, fp=0, latency=100.0)
        t = _gm("treatment", tp=8, misses=2, fp=0, latency=137.5)  # 37.5% increase
        result = self._eval_gray(c, t)
        assert result.latency_gray_zone is True
        assert result.gray_zone is True

    def test_latency_below_gray_zone(self):
        """时延增幅 <= 25% → 非灰区。"""
        c = _gm("control", tp=8, misses=2, fp=0, latency=100.0)
        t = _gm("treatment", tp=8, misses=2, fp=0, latency=125.0)  # 25% exactly
        result = self._eval_gray(c, t)
        assert result.latency_gray_zone is False

    def test_latency_above_gray_zone(self):
        """时延增幅 > 50% → 非灰区（触发暂停）。"""
        c = _gm("control", tp=8, misses=2, fp=0, latency=100.0)
        t = _gm("treatment", tp=8, misses=2, fp=0, latency=151.0)  # 51%
        result = self._eval_gray(c, t)
        assert result.latency_gray_zone is False


# ===================================================================
# G) 每个暂停阈值边界（Req 12.15-12.20）
# ===================================================================

class TestPauseConditions:
    """暂停条件边界测试。"""

    def _eval_pause(self, **kwargs):
        # 使用 treatment FP 率远超 control（>20pp）且时延远超（>50%）的指标，
        # 确保 consecutive 计数能触发暂停
        c = _gm("control", tp=8, misses=2, fp=0, latency=100.0)
        t = _gm("treatment", tp=6, misses=2, fp=4, latency=200.0)
        # control fp_rate=0/8=0, treatment fp_rate=4/10=0.4, diff=0.4 > 0.2
        # latency increase = (200-100)/100 = 1.0 > 0.5
        pause_thresholds = PauseThresholds(
            fp_exceed_control_abs_diff_pause=0.2,
            fp_consecutive_samples=10,
            median_latency_relative_increase_pause=0.5,
            median_latency_consecutive_weeks=2,
            invalid_sample_rate_pause=0.3,
            snapshot_drift_unattributable_pause=0.2,
        )
        return evaluate_pause_conditions(c, t, pause_thresholds, batch_id="B", **kwargs)

    def test_critical_miss(self):
        """Req 12.15: critical miss → 暂停。"""
        result = self._eval_pause(critical_miss_missing_facts=True)
        assert result.should_pause is True
        assert result.trigger == PauseTrigger.CRITICAL_MISS_MISSING_FACTS

    def test_no_critical_miss(self):
        """无 critical miss → 不暂停。"""
        result = self._eval_pause(critical_miss_missing_facts=False)
        assert result.should_pause is False

    def test_fp_consecutive_exceed(self):
        """Req 12.16: FP 连续超阈 >= 10 → 暂停。"""
        result = self._eval_pause(consecutive_fp_exceed_count=10)
        assert result.should_pause is True
        assert result.trigger == PauseTrigger.FP_RATE_EXCEED_20PP_CONSECUTIVE

    def test_fp_consecutive_below(self):
        """FP 连续超阈 < 10 → 不暂停。"""
        result = self._eval_pause(consecutive_fp_exceed_count=9)
        assert result.should_pause is False

    def test_latency_consecutive_weeks(self):
        """Req 12.17: 时延连续 >= 2 周 → 暂停。"""
        result = self._eval_pause(consecutive_latency_weeks=2)
        assert result.should_pause is True
        assert result.trigger == PauseTrigger.LATENCY_OR_INVALID_RATE

    def test_invalid_rate_exceed(self):
        """Req 12.17: 无效率 > 30% → 暂停。"""
        result = self._eval_pause(invalid_sample_rate=0.31)
        assert result.should_pause is True
        assert result.trigger == PauseTrigger.LATENCY_OR_INVALID_RATE

    def test_invalid_rate_at_boundary(self):
        """无效率 = 30% → 不暂停（> 0.30 才触发）。"""
        result = self._eval_pause(invalid_sample_rate=0.30)
        assert result.should_pause is False

    def test_disclosure_incident(self):
        """Req 12.18: 披露事件 → 暂停。"""
        result = self._eval_pause(disclosure_incident=True)
        assert result.should_pause is True
        assert result.trigger == PauseTrigger.DISCLOSURE_INCIDENT

    def test_snapshot_drift(self):
        """Req 12.19: 快照漂移 > 20% → 暂停。"""
        result = self._eval_pause(snapshot_drift_unattributable_rate=0.21)
        assert result.should_pause is True
        assert result.trigger == PauseTrigger.SNAPSHOT_DRIFT_UNATTRIBUTABLE

    def test_snapshot_drift_at_boundary(self):
        """快照漂移 = 20% → 不暂停（> 0.20 才触发）。"""
        result = self._eval_pause(snapshot_drift_unattributable_rate=0.20)
        assert result.should_pause is False

    def test_integrity_incident(self):
        """Req 12.20: 完整性事件 → 暂停。"""
        result = self._eval_pause(integrity_incident=True)
        assert result.should_pause is True
        assert result.trigger == PauseTrigger.FABRICATED_INDEPENDENCE_OR_EVIDENCE

    def test_deterministic_first_hit_order(self):
        """多条件同时满足时，按确定性首次命中顺序。"""
        result = self._eval_pause(
            critical_miss_missing_facts=True,
            disclosure_incident=True,
            integrity_incident=True,
        )
        # CRITICAL_MISS 优先级最高
        assert result.trigger == PauseTrigger.CRITICAL_MISS_MISSING_FACTS


# ===================================================================
# H) Wilson 置信区间与辅助函数
# ===================================================================

class TestWilsonCI:
    """Wilson 置信区间。"""

    def test_zero_trials(self):
        """trials <= 0 → (0.0, 0.0)。"""
        assert wilson_confidence_interval(0, 0) == (0.0, 0.0)

    def test_perfect_success(self):
        """全部成功 → 上界接近 1。"""
        lo, hi = wilson_confidence_interval(10, 10)
        assert hi > 0.9
        assert lo > 0.5

    def test_bounded(self):
        """结果在 [0, 1] 内。"""
        lo, hi = wilson_confidence_interval(3, 10)
        assert 0.0 <= lo <= hi <= 1.0


class TestRelativeChange:
    """relative_change 辅助函数。"""

    def test_normal(self):
        assert relative_change(1.5, 1.0) == pytest.approx(0.5)

    def test_control_zero_treatment_positive(self):
        assert relative_change(1.0, 0.0) == float("inf")

    def test_control_zero_treatment_zero(self):
        assert relative_change(0.0, 0.0) == 0.0


# ===================================================================
# I) 盲视图披露纯度（Req 12.4/12.5/12.18）
# ===================================================================

class TestBlindViewPurity:
    """盲视图披露纯度。"""

    def test_control_includes_notes(self):
        """Control 首轮即披露 Implementer_Notes（Req 12.4）。"""
        source = _make_source_facts()
        view = build_minimal_blind_view(
            task_id="T-1", source=source,
            group=BlindViewGroup.CONTROL, phase=BlindViewPhase.PRE_VERDICT,
            implementer_notes="笔记内容",
        )
        assert view.implementer_notes_included is True

    def test_treatment_pre_verdict_excludes_notes(self):
        """Treatment PRE_VERDICT 排除 Implementer_Notes（Req 12.5）。"""
        source = _make_source_facts()
        view = build_minimal_blind_view(
            task_id="T-1", source=source,
            group=BlindViewGroup.TREATMENT, phase=BlindViewPhase.PRE_VERDICT,
        )
        assert view.implementer_notes_included is False

    def test_treatment_pre_verdict_with_notes_raises(self):
        """Treatment PRE_VERDICT 传入 notes → 披露泄露（Req 12.18）。"""
        source = _make_source_facts()
        with pytest.raises(ViewDisclosureError) as exc_info:
            build_minimal_blind_view(
                task_id="T-1", source=source,
                group=BlindViewGroup.TREATMENT, phase=BlindViewPhase.PRE_VERDICT,
                implementer_notes="不应出现",
            )
        assert exc_info.value.reason.code == ViewErrorCode.DISCLOSURE_VIOLATION

    def test_treatment_post_reveal_includes_notes(self):
        """Treatment POST_REVEAL 揭示 Notes（Req 12.7）。"""
        source = _make_source_facts()
        view = build_minimal_blind_view(
            task_id="T-1", source=source,
            group=BlindViewGroup.TREATMENT, phase=BlindViewPhase.POST_REVEAL,
            implementer_notes="揭示内容", first_verdict_sealed=True,
        )
        assert view.implementer_notes_included is True

    def test_prior_independent_review_report_blocks_admission(self):
        """历史独立复审报告进入 change_audit 时必须在纳样前 fail-closed。"""
        source = _make_source_facts()
        source.change_audit_diffs = [{
            "file_path": "docs/design/rust-only-parser-independent-review-2026-07-26.md",
            "diff": "结论：拒绝 apply/close，存在 5 个 P0 + 4 个 P1",
        }]
        assert find_prior_review_contamination(source) is not None
        with pytest.raises(ViewDisclosureError) as exc_info:
            build_minimal_blind_view(
                task_id="T-review-contaminated", source=source,
                group=BlindViewGroup.TREATMENT,
                phase=BlindViewPhase.PRE_VERDICT,
            )
        assert exc_info.value.reason.code == ViewErrorCode.PRIOR_REVIEW_CONTAMINATION

    def test_prior_reviewer_verdict_in_quality_finding_blocks_admission(self):
        """质量发现中的历史 Reviewer verdict 不能成为新的 blind payload。"""
        source = _make_source_facts()
        source.open_quality_findings = [{
            "source": "task_review",
            "message": "Previous reviewer verdict: rejected apply because P1 remains open",
        }]
        with pytest.raises(ViewDisclosureError) as exc_info:
            build_minimal_blind_view(
                task_id="T-review-finding", source=source,
                group=BlindViewGroup.CONTROL,
                phase=BlindViewPhase.PRE_VERDICT,
                implementer_notes="实现说明",
            )
        assert exc_info.value.reason.code == ViewErrorCode.PRIOR_REVIEW_CONTAMINATION

    def test_normal_p0_p1_task_does_not_trigger_contamination_gate(self):
        """普通 P0/P1 实现任务不能被污染门禁误杀。"""
        source = _make_source_facts()
        source.task_title = "修复 P0/P1 阻塞问题"
        source.task_description = "完成 Rust daemon 的安全边界和发布链修复。"
        source.change_audit_diffs = [{
            "file_path": "rust_ext/src/daemon/dispatch.rs",
            "diff": "修复 P0/P1 的权限校验和错误返回。",
        }]
        assert find_prior_review_contamination(source) is None
        view = build_minimal_blind_view(
            task_id="T-normal-p0-p1", source=source,
            group=BlindViewGroup.TREATMENT,
            phase=BlindViewPhase.PRE_VERDICT,
        )
        assert view.implementer_notes_included is False


class TestRevealEventNotes:
    """reveal 事件保存可审计的 post-reveal Implementer_Notes（Req 12.7 / 12.13）。"""

    def test_reveal_without_notes_backward_compatible(self):
        """不传 notes 的旧调用方仍得到顺序记录（兼容）。"""
        rec = build_reveal_event_record(
            task_id="T-1", batch_id="B-1", first_verdict_sealed=True)
        assert rec["record_type"] == "reveal_event"
        assert rec["first_verdict_sealed_before_reveal"] is True
        assert "implementer_notes" not in rec
        assert rec["non_product_evidence"] is True

    def test_reveal_with_notes_stores_auditable_source(self):
        """传 notes 时写入可审计揭示内容（Req 12.7 留痕）。"""
        rec = build_reveal_event_record(
            task_id="T-1", batch_id="B-1", first_verdict_sealed=True,
            implementer_notes="真实实现说明：修改 db/db_tasks.py 接线。",
        )
        assert rec["implementer_notes_included"] is True
        assert "db/db_tasks.py" in rec["implementer_notes"]
        assert rec["first_verdict_sealed_before_reveal"] is True

    def test_reveal_blank_notes_fail_closed(self):
        """显式空白 notes → 拒绝（缺少可审计揭示来源）。"""
        with pytest.raises(ViewDisclosureError) as exc_info:
            build_reveal_event_record(
                task_id="T-1", batch_id="B-1", first_verdict_sealed=True,
                implementer_notes="   \n  ",
            )
        assert exc_info.value.reason.code == ViewErrorCode.VIEW_SOURCE_MISSING

    def test_reveal_notes_with_prohibited_field_fail_closed(self):
        """notes 携带禁止披露字段 → 拒绝（Req 12.7 / 13.6）。

        ``_find_prohibited_field`` 对嵌套 dict/list 中的禁止键 fail-closed；
        纯字符串内容中的同名子串不属于结构化禁止字段（与视图层行为一致）。
        """
        with pytest.raises(ViewDisclosureError) as exc_info:
            build_reveal_event_record(
                task_id="T-1", batch_id="B-1", first_verdict_sealed=True,
                implementer_notes={"body": "正常内容",
                                   "hidden_reasoning_history": "不应写入"},
            )
        assert exc_info.value.reason.code == ViewErrorCode.DISCLOSURE_VIOLATION


class TestMetricEvidenceV2:
    """v2 指标必须区分真实 token 与无法采集，证据包绑定同一 JSONL。"""

    def test_real_token_source_is_recorded(self):
        rec = build_review_metrics_record(
            task_id="T-1", batch_id="B-1", group="control",
            first_pass_findings=0, final_findings=0,
            verified_true_positives=0, verified_false_positives=0,
            verified_misses=0, review_duration_seconds=1.0,
            token_usage=123, token_usage_source="real",
            reopen_events=0, post_apply_defects=0,
        )
        assert rec["token_usage"] == 123
        assert rec["token_usage_source"] == "real"
        assert rec["token_usage_unavailable_reason"] is None

    def test_unavailable_token_source_is_explicit(self):
        rec = build_review_metrics_record(
            task_id="T-1", batch_id="B-1", group="control",
            first_pass_findings=0, final_findings=0,
            verified_true_positives=0, verified_false_positives=0,
            verified_misses=0, review_duration_seconds=1.0,
            token_usage=None, token_usage_source="unavailable",
            token_usage_unavailable_reason="provider did not expose usage",
            reopen_events=0, post_apply_defects=0,
        )
        assert rec["token_usage"] is None
        assert rec["token_usage_source"] == "unavailable"

    def test_estimated_token_value_is_not_a_supported_source(self):
        with pytest.raises(ValueError):
            build_review_metrics_record(
                task_id="T-1", batch_id="B-1", group="control",
                first_pass_findings=0, final_findings=0,
                verified_true_positives=0, verified_false_positives=0,
                verified_misses=0, review_duration_seconds=1.0,
                token_usage=123, token_usage_source="estimated",
                reopen_events=0, post_apply_defects=0,
            )

    def test_incident_record_type_aliases_are_canonicalized(self):
        """兼容历史 short incident 类型，并统一新写入的长类型。"""
        assert canonical_incident_record_type("disclosure") == "disclosure_incident"
        assert canonical_incident_record_type("disclosure_incident") == "disclosure_incident"
        assert canonical_incident_record_type("integrity") == "integrity_incident"
        assert canonical_incident_record_type("integrity_incident") == "integrity_incident"

    def test_evidence_bundle_requires_jsonl_parent_and_binds_hashes(self, tmp_path):
        jsonl_path = tmp_path / "B-1.jsonl"
        writer = ExperimentJsonlWriter(str(jsonl_path))
        writer.append({"record_type": "blind_view", "task_id": "T-1", "batch_id": "B-1"})
        report = {"batch_id": "B-1", "eligible_for_p1": False}
        bundle = write_evidence_bundle(
            jsonl_path=str(jsonl_path), artifacts_dir=str(tmp_path),
            batch_id="B-1", report=report,
        )
        assert bundle["jsonl"]["sha256"] == ExperimentJsonlWriter.file_sha256(str(jsonl_path))
        assert bundle["report_sha256"] == ExperimentJsonlWriter.file_sha256(bundle["report_path"])
        assert os.path.exists(bundle["manifest_path"])

        with pytest.raises(ValueError):
            write_evidence_bundle(
                jsonl_path=str(jsonl_path), artifacts_dir=str(tmp_path / "other"),
                batch_id="B-1", report=report,
            )

    def test_final_reviewer_bundle_allows_review_records_without_mutating_jsonl(self, tmp_path):
        """交接包只接受 blind_view；评审完成后 final 包可绑定同一份 JSONL。"""
        reviewer_home = tmp_path / "reviewer"
        artifacts = reviewer_home / ".callwarden" / "experiments"
        blind_dir = artifacts / "blind_package_B-1"
        blind_dir.mkdir(parents=True)
        (blind_dir / "T-1.md").write_text("blind", encoding="utf-8")
        (artifacts / "batch_config.json").write_text("{}", encoding="utf-8")
        (artifacts / "manifest_B-1.json").write_text("{}", encoding="utf-8")
        jsonl_path = artifacts / "B-1.jsonl"
        writer = ExperimentJsonlWriter(str(jsonl_path))
        writer.append({"record_type": "blind_view", "task_id": "T-1", "batch_id": "B-1"})
        report = {"batch_id": "B-1", "eligible_for_p1": False}

        handoff = write_evidence_bundle(
            jsonl_path=str(jsonl_path), artifacts_dir=str(artifacts),
            batch_id="B-1", report=report, reviewer_home=str(reviewer_home),
        )
        assert handoff["jsonl"]["record_count"] == 1

        writer.append({"record_type": "review_metrics", "task_id": "T-1", "batch_id": "B-1"})
        with pytest.raises(ValueError, match="blind_view records only"):
            write_evidence_bundle(
                jsonl_path=str(jsonl_path), artifacts_dir=str(artifacts),
                batch_id="B-1", report=report, reviewer_home=str(reviewer_home),
            )

        before = ExperimentJsonlWriter.file_sha256(str(jsonl_path))
        final = write_evidence_bundle(
            jsonl_path=str(jsonl_path), artifacts_dir=str(artifacts),
            batch_id="B-1", report=report, reviewer_home=str(reviewer_home),
            reviewer_phase="final",
        )
        after = ExperimentJsonlWriter.file_sha256(str(jsonl_path))
        assert before == after
        manifest = json.loads(open(final["manifest_path"], encoding="utf-8").read())
        assert manifest["reviewer_phase"] == "final"
        assert manifest["review_started"] is True
        assert manifest["review_record_count"] == 1


class TestBlindViewIntegrity:
    """指标记录必须能回溯到真正不同的两组首轮视图。"""

    _DISCLOSED = ["task_title", "task_description", "step_targets", "change_audit_diffs",
                  "symbol_changes", "test_runs_status", "open_quality_findings"]
    _EXCLUDED = ["hidden_reasoning_history", "prior_reviewer_verdicts", "reviewer_drafts",
                 "confidence_statements", "suggested_review_focus"]

    def test_control_notes_and_treatment_blind_pass(self):
        records = [
            {"record_type": "blind_view", "task_id": "C", "batch_id": "B",
             "phase": "pre_verdict", "group": "control",
             "disclosed_fields": self._DISCLOSED + ["implementer_notes"],
             "excluded_fields": self._EXCLUDED,
             "implementer_notes_included": True,
             "disclosure_label": "experiment_disclosure_list", "is_view_manifest": False,
             "payload": {"implementer_notes": "实现说明"}},
            {"record_type": "blind_view", "task_id": "T", "batch_id": "B",
             "phase": "pre_verdict", "group": "treatment",
             "disclosed_fields": self._DISCLOSED,
             "excluded_fields": self._EXCLUDED + ["implementer_notes"],
             "implementer_notes_included": False,
             "disclosure_label": "experiment_disclosure_list", "is_view_manifest": False,
             "payload": {}},
        ]
        result = validate_blind_view_records(
            records, [("C", "control"), ("T", "treatment")], expected_batch_id="B")
        assert result["passed"] is True
        assert result["control_notes_count"] == 1
        assert result["treatment_blind_count"] == 1

    def test_missing_control_notes_blocks_comparison(self):
        records = [
            {"record_type": "blind_view", "task_id": "C", "batch_id": "B",
             "phase": "pre_verdict", "group": "control",
             "disclosed_fields": self._DISCLOSED + ["implementer_notes"],
             "excluded_fields": self._EXCLUDED,
             "implementer_notes_included": False,
             "disclosure_label": "experiment_disclosure_list", "is_view_manifest": False,
             "payload": {}},
            {"record_type": "blind_view", "task_id": "T", "batch_id": "B",
             "phase": "pre_verdict", "group": "treatment",
             "disclosed_fields": self._DISCLOSED,
             "excluded_fields": self._EXCLUDED + ["implementer_notes"],
             "implementer_notes_included": False,
             "disclosure_label": "experiment_disclosure_list", "is_view_manifest": False,
             "payload": {}},
        ]
        result = validate_blind_view_records(
            records, [("C", "control"), ("T", "treatment")], expected_batch_id="B")
        assert result["passed"] is False
        assert any(r["condition"] == "control_notes_missing" for r in result["reasons"])

    def test_whitespace_control_notes_blocks_comparison(self):
        records = [
            {"record_type": "blind_view", "task_id": "C", "batch_id": "B",
             "phase": "pre_verdict", "group": "control",
             "disclosed_fields": ["task_title", "task_description", "step_targets",
                                  "change_audit_diffs", "symbol_changes", "test_runs_status",
                                  "open_quality_findings", "implementer_notes"],
             "excluded_fields": ["hidden_reasoning_history", "prior_reviewer_verdicts",
                                 "reviewer_drafts", "confidence_statements", "suggested_review_focus"],
             "payload": {"implementer_notes": "   "},
             "implementer_notes_included": True,
             "disclosure_label": "experiment_disclosure_list", "is_view_manifest": False},
        ]
        result = validate_blind_view_records(records, [("C", "control")], expected_batch_id="B")
        assert result["passed"] is False
        assert any(r["condition"] == "control_notes_missing" for r in result["reasons"])

    def test_cross_batch_or_disclosure_list_is_rejected(self):
        records = [
            {"record_type": "blind_view", "task_id": "T", "batch_id": "other",
             "phase": "pre_verdict", "group": "treatment",
             "disclosed_fields": [], "excluded_fields": [], "payload": {},
             "implementer_notes_included": False,
             "disclosure_label": "wrong", "is_view_manifest": True},
        ]
        result = validate_blind_view_records(records, [("T", "treatment")], expected_batch_id="B")
        assert result["passed"] is False
        conditions = {r["condition"] for r in result["reasons"]}
        assert {"view_batch_mismatch", "disclosure_list_mismatch",
                "disclosure_label_mismatch", "view_manifest_flag_invalid"} <= conditions

    def test_duplicate_or_missing_pre_verdict_view_is_rejected(self):
        base = {
            "record_type": "blind_view", "task_id": "C", "batch_id": "B",
            "phase": "pre_verdict", "group": "control",
            "disclosed_fields": self._DISCLOSED + ["implementer_notes"],
            "excluded_fields": self._EXCLUDED,
            "implementer_notes_included": True,
            "disclosure_label": "experiment_disclosure_list", "is_view_manifest": False,
            "payload": {"implementer_notes": "notes"},
        }
        duplicate = validate_blind_view_records(
            [base, dict(base)], [("C", "control")], expected_batch_id="B")
        assert duplicate["passed"] is False
        assert any(r["condition"] == "view_provenance"
                   and r["pre_verdict_view_count"] == 2 for r in duplicate["reasons"])
        missing = validate_blind_view_records([], [("C", "control")], expected_batch_id="B")
        assert missing["passed"] is False
        assert any(r["condition"] == "view_provenance" for r in missing["reasons"])

    def test_verdict_change_record_valid_reasons(self):
        """verdict 变更记录接受有效原因码。"""
        for reason in VERDICT_CHANGE_REASONS:
            rec = build_verdict_change_record("T-1", "B-1", True, reason)
            assert rec["change_reason_code"] == reason
            assert rec["non_product_evidence"] is True

    def test_verdict_change_record_invalid_reason(self):
        """verdict 变更记录拒绝无效原因码。"""
        with pytest.raises(ViewDisclosureError) as exc_info:
            build_verdict_change_record("T-1", "B-1", True, "INVALID_CODE")
        assert exc_info.value.reason.code == ViewErrorCode.INVALID_SAMPLE

    def test_verdict_change_record_rejects_hidden_reasoning(self):
        """verdict 变更记录拒绝 hidden_reasoning（Req 13.6）。"""
        with pytest.raises(ViewDisclosureError) as exc_info:
            build_verdict_change_record("T-1", "B-1", True, "no_change",
                                        hidden_reasoning="secret")
        assert exc_info.value.reason.code == ViewErrorCode.DISCLOSURE_VIOLATION


# ===================================================================
# J) JSONL 写入与恢复（Req 12.1）
# ===================================================================

class TestJsonlWriter:
    """JSONL 追加与恢复。"""

    def test_append_and_read(self, tmp_path):
        """追加后可读回。"""
        path = str(tmp_path / "test.jsonl")
        writer = ExperimentJsonlWriter(path)
        rec = build_invalid_sample_record("T-1", "B-1", "TEST_REASON")
        writer.append(rec)
        records = writer.read_records()
        assert len(records) == 1
        assert records[0]["invalid_reason_code"] == "TEST_REASON"

    def test_recover_tolerates_truncated_last_line(self, tmp_path):
        """恢复容忍末行残缺。"""
        path = str(tmp_path / "test.jsonl")
        writer = ExperimentJsonlWriter(path)
        writer.append(build_invalid_sample_record("T-1", "B-1", "R1"))
        writer.append(build_invalid_sample_record("T-2", "B-1", "R2"))
        # 手动截断末行
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"incomplete": tru')
        records = writer.read_records()
        # 应恢复前 2 条完整记录
        assert len(records) == 2

    def test_non_product_evidence_forced(self, tmp_path):
        """写入时强制 non_product_evidence=True。"""
        path = str(tmp_path / "test.jsonl")
        writer = ExperimentJsonlWriter(path)
        rec = {"record_type": "test", "non_product_evidence": False}
        writer.append(rec)
        records = writer.read_records()
        assert records[0]["non_product_evidence"] is True
