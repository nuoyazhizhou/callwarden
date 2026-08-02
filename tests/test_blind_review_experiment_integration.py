# -*- coding: utf-8 -*-
"""P0 盲评对照实验集成测试（Req 12.1, 12.4-12.8, 12.15-12.20, 12.25-12.26, 13.2-13.5）。

验证 P0 实验工具链与现有任务状态机的集成：
- 任务状态机完整流程（create → report → review → apply → reopen）
- Schema 中立性（Property 24：不新增表、不改 user_version）
- 盲视图字段约束（Control/Treatment 披露规则）
- 无效样本失效（同会话/披露泄露/snapshot 漂移）
- JSONL 追加式采集（non_product_evidence 强制）

使用临时数据库，不污染生产环境。
断言结构化状态/错误码，不只断言自然语言文本（AGENTS.md 规则 35）。
"""
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db import CodeGraphDB
from experiments.blind_review_protocol import (
    ExperimentBatch, build_default_protocol, ExperimentProtocolError,
    ExperimentErrorCode, GroupAssignment, PauseTrigger,
    Experiment_Batch_Config,
)
from experiments.blind_review_views import (
    ViewDisclosureError, ViewErrorCode,
    build_minimal_blind_view, BlindViewGroup, BlindViewPhase,
    BlindViewSourceFacts, assert_treatment_blind_purity,
)
from experiments.blind_review_jsonl import (
    ExperimentJsonlWriter, build_blind_view_record,
    build_review_metrics_record, build_invalid_sample_record,
    build_incident_record,
)
from experiments.blind_review_evaluator import (
    is_nontrivial_code_change, SampleRecord, EvaluatorError,
    EvaluatorErrorCode,
)


# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture
def temp_db(tmp_path):
    """创建临时数据库实例。"""
    db_path = str(tmp_path / "test_integration.db")
    db = CodeGraphDB(db_path=db_path)
    yield db
    db.conn.close()


@pytest.fixture
def temp_config(tmp_path):
    """创建临时实验配置。"""
    config_path = str(tmp_path / "batch_config.json")
    return Experiment_Batch_Config(path=config_path)


@pytest.fixture
def temp_jsonl(tmp_path):
    """创建临时 JSONL 路径。"""
    return str(tmp_path / "experiment.jsonl")


@pytest.fixture
def source_facts():
    """构造最小来源事实。"""
    return BlindViewSourceFacts(
        task_id="T-integ-001",
        task_title="集成测试任务",
        task_description="验证 P0 与状态机集成",
        step_targets=[{"file": "main.py", "symbol": "handle_request"}],
        change_audit_diffs=[{"diff": "+15 lines", "file": "main.py"}],
        symbol_changes=[{"name": "handle_request", "type": "modified"}],
        test_runs_status=[{"status": "passed"}],
        open_quality_findings=[],
    )


# ===================================================================
# A) 任务状态机完整流程（Req 13.2-13.5）
# ===================================================================

class TestTaskStateMachineIntegration:
    """P0 实验不改变现有任务状态机行为。"""

    def test_full_lifecycle_create_to_review(self, temp_db):
        """task_create → report all steps → status=review。"""
        task_id = temp_db.task_create(
            title="P0 集成测试任务",
            description="验证状态机",
            steps=[
                {"action": "implement", "target_file": "test.py"},
                {"action": "verify", "target_file": ""},
            ],
        )
        assert task_id  # 非空
        # 查询步骤
        steps = [dict(r) for r in temp_db.conn.execute(
            "SELECT id, step_index, status FROM task_steps WHERE task_id=? ORDER BY step_index",
            (task_id,))]
        assert len(steps) == 2
        assert all(s["status"] == "pending" for s in steps)
        # 逐步报告
        for s in steps:
            temp_db.task_report_step(task_id, s["id"], "done", True, None)
        # 验证状态变为 review
        task = dict(temp_db.conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone())
        assert task["status"] == "review"

    def test_apply_transitions_to_applied(self, temp_db):
        """task_apply → status=applied。"""
        task_id = temp_db.task_create(
            title="apply 测试", steps=[{"action": "implement", "target_file": "x.py"}])
        steps = [dict(r) for r in temp_db.conn.execute(
            "SELECT id FROM task_steps WHERE task_id=?", (task_id,))]
        temp_db.task_report_step(task_id, steps[0]["id"], "done", True, None)
        result = temp_db.task_apply(task_id, reviewer="test-reviewer")
        assert "error" not in result or result.get("error") is None
        task = dict(temp_db.conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone())
        assert task["status"] == "applied"

    def test_reopen_transitions_to_in_progress(self, temp_db):
        """task_reopen → status=in_progress。"""
        task_id = temp_db.task_create(
            title="reopen 测试", steps=[{"action": "implement", "target_file": "x.py"}])
        steps = [dict(r) for r in temp_db.conn.execute(
            "SELECT id FROM task_steps WHERE task_id=?", (task_id,))]
        temp_db.task_report_step(task_id, steps[0]["id"], "done", True, None)
        temp_db.task_apply(task_id, reviewer="r")
        result = temp_db.task_reopen(task_id, reviewer="r", reason="需要修复")
        assert "error" not in result or result.get("error") is None
        task = dict(temp_db.conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone())
        assert task["status"] == "in_progress"

    def test_p0_experiment_does_not_alter_state_machine(self, temp_db, temp_config, temp_jsonl):
        """P0 实验操作（创建批次/纳样/记录）不改变任务状态。"""
        task_id = temp_db.task_create(
            title="P0 不干扰测试", steps=[{"action": "implement", "target_file": "x.py"}])
        # 执行 P0 实验操作
        protocol = build_default_protocol(99)
        batch = ExperimentBatch(
            batch_id="B-integ-001",
            created_at="2026-01-01T00:00:00+00:00",
            protocol=protocol,
        )
        batch.lock_protocol()
        temp_config.put_batch(batch)
        temp_config.save()
        # 写 JSONL 记录
        writer = ExperimentJsonlWriter(temp_jsonl)
        writer.append(build_invalid_sample_record(task_id, "B-integ-001", "TEST"))
        # 验证任务状态未被改变（仍为 open）
        task = dict(temp_db.conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone())
        assert task["status"] == "open"


# ===================================================================
# B) Schema 中立性（Property 24）
# ===================================================================

class TestSchemaNeutrality:
    """P0 实验不新增表、不改 schema 版本。"""

    def test_user_version_unchanged(self, temp_db):
        """实验前后 PRAGMA user_version 不变。"""
        before = temp_db.conn.execute("PRAGMA user_version").fetchone()[0]
        # 执行 P0 操作（纯文件/内存，不碰 DB schema）
        protocol = build_default_protocol(42)
        batch = ExperimentBatch(
            batch_id="B-schema-001",
            created_at="2026-01-01T00:00:00+00:00",
            protocol=protocol,
        )
        batch.lock_protocol()
        after = temp_db.conn.execute("PRAGMA user_version").fetchone()[0]
        assert before == after

    def test_no_new_tables(self, temp_db):
        """实验前后 sqlite_master 表集合不变。"""
        before = {r[0] for r in temp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        # P0 操作
        _ = build_default_protocol(7)
        _ = Experiment_Batch_Config(path=os.path.join(
            os.path.dirname(temp_db.conn.execute("PRAGMA database_list").fetchone()[2]),
            "test_config.json"))
        after = {r[0] for r in temp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert before == after

    def test_blind_view_uses_existing_fields_only(self, source_facts):
        """Minimal_Blind_View 只使用现有任务字段。"""
        view = build_minimal_blind_view(
            task_id="T-integ-001",
            source=source_facts,
            group=BlindViewGroup.CONTROL,
            phase=BlindViewPhase.PRE_VERDICT,
        )
        # 视图字段应来自 source_facts 的现有字段
        assert view.task_id == "T-integ-001"
        # 不应引入新的 DB 列或表
        assert hasattr(view, "disclosed_fields")
        assert hasattr(view, "excluded_fields")


# ===================================================================
# C) 盲视图字段约束（Req 12.4/12.5/12.25）
# ===================================================================

class TestBlindViewFieldConstraints:
    """Control/Treatment 披露规则集成验证。"""

    def test_control_discloses_notes(self, source_facts):
        """Control 首轮即披露 Implementer_Notes（Req 12.4）。"""
        view = build_minimal_blind_view(
            task_id="T-integ-001", source=source_facts,
            group=BlindViewGroup.CONTROL, phase=BlindViewPhase.PRE_VERDICT,
            implementer_notes="实现笔记",
        )
        assert view.implementer_notes_included is True

    def test_treatment_excludes_notes_pre_verdict(self, source_facts):
        """Treatment PRE_VERDICT 排除 Notes（Req 12.5）。"""
        view = build_minimal_blind_view(
            task_id="T-integ-001", source=source_facts,
            group=BlindViewGroup.TREATMENT, phase=BlindViewPhase.PRE_VERDICT,
        )
        assert view.implementer_notes_included is False

    def test_disclosure_list_present(self, source_facts):
        """披露清单标注 experiment_disclosure_list（Req 12.25）。"""
        view = build_minimal_blind_view(
            task_id="T-integ-001", source=source_facts,
            group=BlindViewGroup.CONTROL, phase=BlindViewPhase.PRE_VERDICT,
        )
        # blind_view_record 应包含披露清单
        record = build_blind_view_record(view=view, batch_id="B-integ")
        assert record["non_product_evidence"] is True
        assert "disclosed_fields" in record or "view" in record

    def test_treatment_purity_assertion(self, source_facts):
        """assert_treatment_blind_purity 通过（无泄露）。"""
        view = build_minimal_blind_view(
            task_id="T-integ-001", source=source_facts,
            group=BlindViewGroup.TREATMENT, phase=BlindViewPhase.PRE_VERDICT,
        )
        # 不应抛异常
        assert_treatment_blind_purity(view)


# ===================================================================
# D) 无效样本失效（Req 12.8/12.15-12.20）
# ===================================================================

class TestInvalidSampleIntegration:
    """无效样本场景集成验证。"""

    def test_disclosure_violation_triggers_error(self, source_facts):
        """Treatment PRE_VERDICT 传入 notes → 披露泄露错误（Req 12.18）。"""
        with pytest.raises(ViewDisclosureError) as exc_info:
            build_minimal_blind_view(
                task_id="T-integ-001", source=source_facts,
                group=BlindViewGroup.TREATMENT, phase=BlindViewPhase.PRE_VERDICT,
                implementer_notes="不应出现",
            )
        assert exc_info.value.reason.code == ViewErrorCode.DISCLOSURE_VIOLATION

    def test_disclosure_incident_record(self, temp_jsonl):
        """披露事件记录写入 JSONL（Req 12.18）。"""
        writer = ExperimentJsonlWriter(temp_jsonl)
        rec = build_incident_record(
            task_id="T-integ-001", batch_id="B-integ",
            incident_type="disclosure", reason_code="FIELD_LEAK",
            reason_detail="treatment 视图包含 notes",
        )
        writer.append(rec)
        records = writer.read_records()
        assert len(records) == 1
        assert records[0]["record_type"] == "disclosure"
        assert records[0]["reason_code"] == "FIELD_LEAK"
        assert records[0]["non_product_evidence"] is True

    def test_integrity_incident_record(self, temp_jsonl):
        """完整性事件记录（Req 12.20）。"""
        writer = ExperimentJsonlWriter(temp_jsonl)
        rec = build_incident_record(
            task_id="T-integ-002", batch_id="B-integ",
            incident_type="integrity", reason_code="FABRICATED_EVIDENCE",
        )
        writer.append(rec)
        records = writer.read_records()
        assert records[0]["record_type"] == "integrity"
        assert records[0]["reason_code"] == "FABRICATED_EVIDENCE"

    def test_same_session_invalid_sample(self, temp_jsonl):
        """同会话样本标记为无效（Req 12.8）。"""
        writer = ExperimentJsonlWriter(temp_jsonl)
        rec = build_invalid_sample_record(
            task_id="T-integ-003", batch_id="B-integ",
            reason_code="SAME_SESSION",
            reason_detail="reviewer 与 implementer 同会话",
        )
        writer.append(rec)
        records = writer.read_records()
        assert records[0]["invalid_reason_code"] == "SAME_SESSION"

    def test_snapshot_drift_invalid_sample(self, temp_jsonl):
        """snapshot 漂移样本标记为无效（Req 12.8/12.19）。"""
        writer = ExperimentJsonlWriter(temp_jsonl)
        rec = build_invalid_sample_record(
            task_id="T-integ-004", batch_id="B-integ",
            reason_code="SNAPSHOT_CHANGED",
            reason_detail="代码在 review 期间发生变更",
        )
        writer.append(rec)
        records = writer.read_records()
        assert records[0]["invalid_reason_code"] == "SNAPSHOT_CHANGED"

    def test_pause_on_disclosure(self, temp_config):
        """披露事件触发批次暂停（Req 12.18/12.21）。"""
        protocol = build_default_protocol(42)
        batch = ExperimentBatch(
            batch_id="B-pause-integ",
            created_at="2026-01-01T00:00:00+00:00",
            protocol=protocol,
        )
        batch.lock_protocol()
        temp_config.put_batch(batch)
        temp_config.save()
        # 暂停
        batch.pause(PauseTrigger.DISCLOSURE_INCIDENT, "字段泄露",
                    "2026-01-01T01:00:00+00:00")
        temp_config.put_batch(batch)
        temp_config.save()
        # 验证暂停后纳样被拒绝
        loaded = temp_config.get_batch("B-pause-integ")
        assert loaded.paused_at is not None
        with pytest.raises(ExperimentProtocolError) as exc_info:
            loaded.ensure_admission_allowed()
        assert exc_info.value.reason.code == ExperimentErrorCode.BATCH_PAUSED


# ===================================================================
# E) JSONL 追加式采集（Req 12.1/12.26）
# ===================================================================

class TestJsonlIntegration:
    """JSONL 采集与 non_product_evidence 强制。"""

    def test_metrics_record_non_product_evidence(self, temp_jsonl):
        """review 指标记录强制 non_product_evidence=True（Req 12.1）。"""
        writer = ExperimentJsonlWriter(temp_jsonl)
        rec = build_review_metrics_record(
            task_id="T-integ-005", batch_id="B-integ", group="control",
            first_pass_findings=5, final_findings=7,
            verified_true_positives=4, verified_false_positives=1,
            verified_misses=2, review_duration_seconds=180.0,
            token_usage=5000, reopen_events=0, post_apply_defects=0,
        )
        writer.append(rec)
        records = writer.read_records()
        assert records[0]["non_product_evidence"] is True

    def test_nontrivial_code_change_integration(self):
        """非平凡 code change 判定集成（Req 12.26）。"""
        # >=10 行 AND 符号变更 → 非平凡
        assert is_nontrivial_code_change(15, True) is True
        # <10 行 → 平凡
        assert is_nontrivial_code_change(5, True) is False
        # 无符号变更 → 平凡
        assert is_nontrivial_code_change(20, False) is False
        # 仅格式化 → 平凡
        assert is_nontrivial_code_change(50, True, is_formatting_only=True) is False

    def test_multiple_records_append(self, temp_jsonl):
        """多条记录追加不覆盖（Req 12.1 追加式）。"""
        writer = ExperimentJsonlWriter(temp_jsonl)
        for i in range(5):
            writer.append(build_invalid_sample_record(
                f"T-{i}", "B-integ", f"REASON_{i}"))
        records = writer.read_records()
        assert len(records) == 5
        assert [r["task_id"] for r in records] == [f"T-{i}" for i in range(5)]

    def test_blind_view_record_from_view(self, source_facts, temp_jsonl):
        """从 MinimalBlindView 构建 JSONL 记录（Req 12.1/12.25）。"""
        view = build_minimal_blind_view(
            task_id="T-integ-001", source=source_facts,
            group=BlindViewGroup.CONTROL, phase=BlindViewPhase.PRE_VERDICT,
            implementer_notes="笔记",
        )
        record = build_blind_view_record(view=view, batch_id="B-integ")
        writer = ExperimentJsonlWriter(temp_jsonl)
        writer.append(record)
        records = writer.read_records()
        assert len(records) == 1
        assert records[0]["non_product_evidence"] is True
        assert records[0]["record_type"] == "blind_view"
