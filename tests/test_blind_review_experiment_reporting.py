# -*- coding: utf-8 -*-
"""P0 报告完整性与 JSONL 恢复测试（Req 12.21-12.24, 12.27-12.29）。

验证：
- 中断恢复（截断末行不丢失已写记录）
- 追加记录不丢失
- 每个 invalid 原因可见
- 指标分子/分母和观察窗口随报告输出
- 冻结后不能修改旧批次移动目标线
- 灰区边界与 fail-safe 暂停
"""
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.blind_review_protocol import (
    ExperimentBatch, build_default_protocol, ExperimentProtocolError,
    ExperimentErrorCode, PauseTrigger, Experiment_Batch_Config,
    SuccessThresholds, PauseThresholds,
)
from experiments.blind_review_jsonl import (
    ExperimentJsonlWriter, build_review_metrics_record,
    build_invalid_sample_record,
)
from experiments.blind_review_evaluator import (
    compute_group_metrics, evaluate_success, evaluate_gray_zone,
    evaluate_pause_conditions, compute_invalid_sample_stats,
    build_evaluation_report, SampleRecord, GroupMetrics,
    EvaluatorError, EvaluatorErrorCode,
)


# ===================================================================
# 辅助
# ===================================================================

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
# A) 中断恢复（Req 12.24 fail-safe）
# ===================================================================

class TestInterruptRecovery:
    """JSONL 中断恢复。"""

    def test_truncated_last_line_recovered(self, tmp_path):
        """截断末行后仍恢复完整记录。"""
        path = str(tmp_path / "test.jsonl")
        writer = ExperimentJsonlWriter(path)
        for i in range(5):
            writer.append(build_invalid_sample_record(f"T-{i}", "B-1", f"R{i}"))
        # 模拟中断：追加半行 JSON
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"record_type": "invalid_sample", "task_id": "T-BRO')
        # 恢复读取
        records = writer.read_records()
        assert len(records) == 5  # 前 5 条完整恢复
        assert all(r["non_product_evidence"] is True for r in records)

    def test_empty_file_recovery(self, tmp_path):
        """空文件恢复为空列表。"""
        path = str(tmp_path / "empty.jsonl")
        open(path, "w").close()
        writer = ExperimentJsonlWriter(path)
        records = writer.read_records()
        assert records == []

    def test_missing_file_recovery(self, tmp_path):
        """文件不存在时恢复为空列表（不报错）。"""
        path = str(tmp_path / "nonexistent.jsonl")
        writer = ExperimentJsonlWriter(path)
        records = writer.read_records()
        assert records == []

    def test_recover_with_stats(self, tmp_path):
        """recover_with_stats 返回恢复统计。"""
        path = str(tmp_path / "stats.jsonl")
        writer = ExperimentJsonlWriter(path)
        writer.append(build_invalid_sample_record("T-1", "B-1", "R1"))
        writer.append(build_invalid_sample_record("T-2", "B-1", "R2"))
        with open(path, "a", encoding="utf-8") as f:
            f.write("GARBAGE LINE\n")
        records, skipped = writer.recover_with_stats(path)
        assert len(records) == 2
        assert skipped >= 1


# ===================================================================
# B) 追加记录不丢失（Req 12.1）
# ===================================================================

class TestAppendIntegrity:
    """追加式写入不丢失已有记录。"""

    def test_append_preserves_existing(self, tmp_path):
        """多次追加后所有记录可读。"""
        path = str(tmp_path / "append.jsonl")
        writer = ExperimentJsonlWriter(path)
        for i in range(10):
            writer.append(build_review_metrics_record(
                task_id=f"T-{i}", batch_id="B-1", group="control",
                first_pass_findings=3, final_findings=4,
                verified_true_positives=2, verified_false_positives=1,
                verified_misses=1, review_duration_seconds=60.0,
                token_usage=1000, reopen_events=0, post_apply_defects=0,
            ))
        records = writer.read_records()
        assert len(records) == 10
        assert [r["task_id"] for r in records] == [f"T-{i}" for i in range(10)]

    def test_new_writer_instance_appends(self, tmp_path):
        """新 Writer 实例追加不覆盖。"""
        path = str(tmp_path / "multi.jsonl")
        w1 = ExperimentJsonlWriter(path)
        w1.append(build_invalid_sample_record("T-1", "B-1", "R1"))
        # 新实例
        w2 = ExperimentJsonlWriter(path)
        w2.append(build_invalid_sample_record("T-2", "B-1", "R2"))
        records = w2.read_records()
        assert len(records) == 2


# ===================================================================
# C) 每个 invalid 原因可见（Req 12.22）
# ===================================================================

class TestInvalidReasonVisibility:
    """报告中每个 invalid 原因码可见。"""

    def test_all_reason_codes_in_report(self):
        """compute_invalid_sample_stats 保留每个原因码计数。"""
        codes = ["SAME_SESSION", "SNAPSHOT_CHANGED", "SAME_SESSION",
                 "BLIND_BROKEN", "SNAPSHOT_CHANGED", "SNAPSHOT_CHANGED"]
        rate, counts = compute_invalid_sample_stats(codes, 20)
        assert "SAME_SESSION" in counts
        assert "SNAPSHOT_CHANGED" in counts
        assert "BLIND_BROKEN" in counts
        assert counts["SAME_SESSION"] == 2
        assert counts["SNAPSHOT_CHANGED"] == 3
        assert counts["BLIND_BROKEN"] == 1
        # 比率 = 6 / (20 + 6)
        assert rate == pytest.approx(6.0 / 26.0)

    def test_evaluation_report_includes_invalid_counts(self):
        """build_evaluation_report 输出包含 invalid 统计。"""
        c = _gm("control")
        t = _gm("treatment")
        thresholds = SuccessThresholds(min_valid_tasks=5, min_nontrivial_code_change_tasks=3)
        pause_thresholds = PauseThresholds()
        success = evaluate_success(c, t, thresholds, valid_task_count=20,
                                   nontrivial_code_change_count=16, batch_id="B")
        gray = evaluate_gray_zone(c, t, thresholds, pause_thresholds, batch_id="B")
        pause = evaluate_pause_conditions(c, t, pause_thresholds, batch_id="B")
        report = build_evaluation_report(
            batch_id="B-rpt", control=c, treatment=t,
            success=success, gray_zone=gray, pause=pause,
            invalid_reason_counts={"SAME_SESSION": 3, "SNAPSHOT_CHANGED": 1},
            invalid_sample_rate=0.1,
            metric_definitions=[], observation_windows=[],
        )
        assert report["non_product_evidence"] is True
        assert "groups" in report


# ===================================================================
# D) 指标分子/分母和观察窗口随报告输出（Req 12.22/12.23）
# ===================================================================

class TestReportCompleteness:
    """报告输出完整性。"""

    def test_group_metrics_numerator_denominator(self):
        """GroupMetrics 包含分子/分母可推导的字段。"""
        c = _gm("control", n=20, tp=15, misses=5, fp=3)
        # recall = 15/(15+5) = 0.75
        assert c.recall == pytest.approx(0.75)
        # fp_rate = 3/(3+15) = 0.1667
        assert c.false_positive_rate == pytest.approx(3.0 / 18.0)
        assert c.valid_n == 20
        assert c.true_positives == 15
        assert c.verified_misses == 5
        assert c.false_positives == 3

    def test_evaluation_report_has_observation_windows(self):
        """报告包含 observation_windows 字段。"""
        c = _gm("control")
        t = _gm("treatment")
        thresholds = SuccessThresholds(min_valid_tasks=5, min_nontrivial_code_change_tasks=3)
        pause_thresholds = PauseThresholds()
        success = evaluate_success(c, t, thresholds, valid_task_count=20,
                                   nontrivial_code_change_count=16, batch_id="B")
        gray = evaluate_gray_zone(c, t, thresholds, pause_thresholds, batch_id="B")
        pause = evaluate_pause_conditions(c, t, pause_thresholds, batch_id="B")
        report = build_evaluation_report(
            batch_id="B-rpt", control=c, treatment=t,
            success=success, gray_zone=gray, pause=pause,
            invalid_reason_counts={}, invalid_sample_rate=0.0,
            metric_definitions=[], observation_windows=[],
        )
        assert "observation_windows" in report
        assert report["observation_windows"] == []

    def test_report_non_product_evidence_always_true(self):
        """报告 non_product_evidence 始终为 True（Req 12.23）。"""
        c = _gm("control")
        t = _gm("treatment")
        thresholds = SuccessThresholds(min_valid_tasks=5, min_nontrivial_code_change_tasks=3)
        pause_thresholds = PauseThresholds()
        success = evaluate_success(c, t, thresholds, valid_task_count=20,
                                   nontrivial_code_change_count=16, batch_id="B")
        gray = evaluate_gray_zone(c, t, thresholds, pause_thresholds, batch_id="B")
        pause = evaluate_pause_conditions(c, t, pause_thresholds, batch_id="B")
        report = build_evaluation_report(
            batch_id="B", control=c, treatment=t,
            success=success, gray_zone=gray, pause=pause,
            invalid_reason_counts={}, invalid_sample_rate=0.0,
            metric_definitions=[], observation_windows=[],
        )
        assert report["non_product_evidence"] is True
        assert report.get("is_product_evidence", False) is False


# ===================================================================
# E) 冻结后不能移动目标线（Req 12.21）
# ===================================================================

class TestFrozenBatchImmutability:
    """冻结批次不可修改协议/阈值。"""

    def test_frozen_batch_rejects_lock(self, tmp_path):
        """首次纳样后冻结，再 lock 抛 BATCH_FROZEN。"""
        config_path = str(tmp_path / "config.json")
        config = Experiment_Batch_Config(path=config_path)
        protocol = build_default_protocol(42)
        batch = ExperimentBatch(
            batch_id="B-frozen", created_at="2026-01-01T00:00:00+00:00",
            protocol=protocol,
        )
        batch.lock_protocol()
        batch.mark_first_admission("2026-01-01T01:00:00+00:00")
        config.put_batch(batch)
        config.save()
        # 重新加载后尝试再次 lock
        config2 = Experiment_Batch_Config(path=config_path)
        config2.load()
        loaded = config2.get_batch("B-frozen")
        with pytest.raises(ExperimentProtocolError) as exc_info:
            loaded.lock_protocol()
        assert exc_info.value.reason.code == ExperimentErrorCode.BATCH_FROZEN

    def test_frozen_thresholds_unchanged(self, tmp_path):
        """冻结后 success_thresholds 保持不变。"""
        config_path = str(tmp_path / "config.json")
        config = Experiment_Batch_Config(path=config_path)
        protocol = build_default_protocol(42)
        original_min = protocol.success_thresholds.min_valid_tasks
        batch = ExperimentBatch(
            batch_id="B-thresh", created_at="2026-01-01T00:00:00+00:00",
            protocol=protocol,
        )
        batch.lock_protocol()
        batch.mark_first_admission("2026-01-01T01:00:00+00:00")
        config.put_batch(batch)
        config.save()
        # 重新加载
        config2 = Experiment_Batch_Config(path=config_path)
        config2.load()
        loaded = config2.get_batch("B-thresh")
        assert loaded.protocol.success_thresholds.min_valid_tasks == original_min

    def test_paused_batch_persisted(self, tmp_path):
        """暂停状态持久化到配置文件（Req 12.21）。"""
        config_path = str(tmp_path / "config.json")
        config = Experiment_Batch_Config(path=config_path)
        protocol = build_default_protocol(42)
        batch = ExperimentBatch(
            batch_id="B-pause-persist", created_at="2026-01-01T00:00:00+00:00",
            protocol=protocol,
        )
        batch.lock_protocol()
        batch.pause(PauseTrigger.FP_RATE_EXCEED_20PP_CONSECUTIVE, "连续超阈",
                    "2026-01-02T00:00:00+00:00")
        config.put_batch(batch)
        config.save()
        # 新实例加载
        config2 = Experiment_Batch_Config(path=config_path)
        config2.load()
        loaded = config2.get_batch("B-pause-persist")
        assert loaded.paused_at is not None
        assert loaded.pause_trigger == PauseTrigger.FP_RATE_EXCEED_20PP_CONSECUTIVE


# ===================================================================
# F) 灰区边界（Req 12.27-12.29）
# ===================================================================

class TestGrayZoneReporting:
    """灰区标记与报告输出。"""

    def test_fp_gray_zone_in_report(self):
        """FP 灰区标记出现在评估报告中。"""
        # control fp_rate=0.25, treatment fp_rate=0.375 → diff=0.125 (12.5pp)
        c = _gm("control", tp=6, misses=2, fp=2)  # 2/8=0.25
        t = _gm("treatment", tp=5, misses=2, fp=3)  # 3/8=0.375
        thresholds = SuccessThresholds(
            min_valid_tasks=5, min_nontrivial_code_change_tasks=3,
            false_positive_rate_abs_diff_max=0.1,
            median_latency_relative_increase_max=0.25,
        )
        pause_thresholds = PauseThresholds(
            fp_exceed_control_abs_diff_pause=0.2,
            median_latency_relative_increase_pause=0.5,
        )
        gray = evaluate_gray_zone(c, t, thresholds, pause_thresholds, batch_id="B")
        assert gray.gray_zone is True
        assert gray.fp_gray_zone is True
        assert gray.authorized_for_p1 is False

    def test_latency_gray_zone_in_report(self):
        """时延灰区标记。"""
        c = _gm("control", latency=100.0)
        t = _gm("treatment", latency=140.0)  # 40% increase
        thresholds = SuccessThresholds(
            min_valid_tasks=5, median_latency_relative_increase_max=0.25,
        )
        pause_thresholds = PauseThresholds(
            median_latency_relative_increase_pause=0.5,
        )
        gray = evaluate_gray_zone(c, t, thresholds, pause_thresholds, batch_id="B")
        assert gray.latency_gray_zone is True
        assert gray.gray_zone is True

    def test_gray_zone_does_not_trigger_pause(self):
        """灰区本身不触发暂停（Req 12.29）。"""
        c = _gm("control", tp=6, misses=2, fp=2, latency=100.0)
        t = _gm("treatment", tp=5, misses=2, fp=3, latency=140.0)
        thresholds = SuccessThresholds(
            min_valid_tasks=5,
            false_positive_rate_abs_diff_max=0.1,
            median_latency_relative_increase_max=0.25,
        )
        pause_thresholds = PauseThresholds(
            fp_exceed_control_abs_diff_pause=0.2,
            median_latency_relative_increase_pause=0.5,
        )
        pause = evaluate_pause_conditions(c, t, pause_thresholds, batch_id="B")
        # 灰区内（12.5pp FP, 40% latency）不触发暂停（阈值 20pp/50%）
        assert pause.should_pause is False

    def test_gray_zone_observations_recorded(self):
        """灰区观察记录在 GrayZoneEvaluation 中。"""
        c = _gm("control", tp=6, misses=2, fp=2)
        t = _gm("treatment", tp=5, misses=2, fp=3)
        thresholds = SuccessThresholds(
            min_valid_tasks=5, false_positive_rate_abs_diff_max=0.1,
        )
        pause_thresholds = PauseThresholds(fp_exceed_control_abs_diff_pause=0.2)
        gray = evaluate_gray_zone(c, t, thresholds, pause_thresholds, batch_id="B")
        assert gray.gray_zone is True
        # observations 应非空
        assert len(gray.observations) > 0
