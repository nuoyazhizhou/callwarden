"""server/degraded_mode.py 单元测试。

覆盖 Req 14.27–14.30, 14.34–14.37。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.degraded_mode import (
    GOVERNANCE_WRITE_OPS,
    INDEX_WRITE_OPS,
    DegradedRoutingDecision,
    OperationClass,
    OperationComponent,
    StructuredReason,
    can_advance_state,
    classify_operation,
    components,
    produces_evidence_or_gate,
    route_degraded,
)


# ---------------------------------------------------------------------------
# Req 14.27, 14.34: class(op) 三分类，恒定不变
# ---------------------------------------------------------------------------


class TestClassifyOperation:
    """操作分类恒定、可判定、可重放。"""

    def test_governance_write_ops(self):
        """Protected_Mutation 全集归类为 Governance_Write。"""
        for op in GOVERNANCE_WRITE_OPS:
            assert classify_operation(op) == OperationClass.GOVERNANCE_WRITE

    def test_index_write_ops(self):
        """索引写操作归类为 Index_Write。"""
        for op in INDEX_WRITE_OPS:
            assert classify_operation(op) == OperationClass.INDEX_WRITE

    def test_read_only_default(self):
        """未知操作默认归类为 read_only。"""
        assert classify_operation("query.symbols") == OperationClass.READ_ONLY
        assert classify_operation("get_callers") == OperationClass.READ_ONLY
        assert classify_operation("unknown.method") == OperationClass.READ_ONLY

    def test_classification_is_constant(self):
        """同一操作多次分类结果恒定 [Req 14.34]。"""
        for _ in range(10):
            assert classify_operation("verdict.submit") == OperationClass.GOVERNANCE_WRITE
            assert classify_operation("graph.refresh") == OperationClass.INDEX_WRITE
            assert classify_operation("query.symbols") == OperationClass.READ_ONLY

    def test_governance_set_matches_protected_mutations(self):
        """Governance_Write 集合与 Rust PROTECTED_MUTATION_METHODS 一致。"""
        expected = {
            "snapshot.publish", "workspace.file.refresh", "workspace.recover",
            "backup", "restore",
            "verdict.submit", "reveal.submit", "evidence.append",
            "gate.decide", "task.apply", "task.close",
            "lease.acquire", "lease.release", "lease.extend",
        }
        assert GOVERNANCE_WRITE_OPS == expected


# ---------------------------------------------------------------------------
# Req 14.35: components(op) 拆分
# ---------------------------------------------------------------------------


class TestComponents:
    """跨类操作按组成部分分级。"""

    def test_single_class_op_returns_one_component(self):
        """单一类别操作返回单元素列表。"""
        comps = components("query.symbols")
        assert len(comps) == 1
        assert comps[0].op_class == OperationClass.READ_ONLY

    def test_cross_class_op_returns_multiple(self):
        """跨类操作返回多元素列表。"""
        comps = components("workspace.file.refresh")
        assert len(comps) == 2
        classes = {c.op_class for c in comps}
        assert OperationClass.INDEX_WRITE in classes
        assert OperationClass.GOVERNANCE_WRITE in classes

    def test_snapshot_publish_is_cross_class(self):
        """snapshot.publish 是跨类操作。"""
        comps = components("snapshot.publish")
        assert len(comps) == 2

    def test_governance_only_op_single_component(self):
        """纯 Governance 操作返回单元素。"""
        comps = components("verdict.submit")
        assert len(comps) == 1
        assert comps[0].op_class == OperationClass.GOVERNANCE_WRITE


# ---------------------------------------------------------------------------
# Req 14.28: read_only 直连只读
# ---------------------------------------------------------------------------


class TestRouteReadOnly:
    """Degraded_Mode 下只读操作直连执行。"""

    def test_read_only_allowed(self):
        decision = route_degraded("query.symbols", "/tmp/sock", "linux")
        assert decision.allowed is True
        assert decision.mode == "direct_read"
        assert decision.op_class == OperationClass.READ_ONLY
        assert decision.reason is None


# ---------------------------------------------------------------------------
# Req 14.29: Index_Write 直连写入
# ---------------------------------------------------------------------------


class TestRouteIndexWrite:
    """Degraded_Mode 下 Index_Write 直连写入。"""

    def test_index_write_allowed(self):
        decision = route_degraded("graph.refresh", "/tmp/sock", "linux")
        assert decision.allowed is True
        assert decision.mode == "direct_write"
        assert decision.op_class == OperationClass.INDEX_WRITE


# ---------------------------------------------------------------------------
# Req 14.30: Governance_Write fail closed
# ---------------------------------------------------------------------------


class TestRouteGovernanceWrite:
    """Degraded_Mode 下 Governance_Write fail closed。"""

    def test_governance_write_rejected(self):
        decision = route_degraded("verdict.submit", "/tmp/sock", "linux")
        assert decision.allowed is False
        assert decision.mode == "fail_closed"
        assert decision.reason is not None
        assert decision.reason.code == "E_GOVERNANCE_WRITE_DEGRADED"
        assert decision.reason.message_key == "error.governance_write_degraded"

    def test_recovery_guidance_is_platform_specific(self):
        """恢复指引给出平台具体的 daemon 拉起命令 [Req 14.30]。"""
        # Linux
        d = route_degraded("task.apply", "/run/cw.sock", "linux")
        assert "systemctl --user start" in d.reason.recovery_guidance
        assert "/run/cw.sock" in d.reason.recovery_guidance

        # macOS
        d = route_degraded("task.apply", "/tmp/cw.sock", "macos")
        assert "launchctl start" in d.reason.recovery_guidance

        # Windows
        d = route_degraded("task.apply", r"\\.\pipe\cw", "windows")
        assert "cw_daemon.exe" in d.reason.recovery_guidance

    def test_recovery_not_generic_db_busy(self):
        """恢复指引不是泛化的"数据库正忙" [Req 14.30]。"""
        d = route_degraded("gate.decide", "/tmp/sock", "linux")
        assert "数据库正忙" not in d.reason.recovery_guidance
        assert "database is locked" not in d.reason.recovery_guidance


# ---------------------------------------------------------------------------
# Req 14.35: 跨类操作部分执行
# ---------------------------------------------------------------------------


class TestCrossClassRouting:
    """跨类操作按组成部分分级。"""

    def test_cross_class_identifies_components(self):
        """Structured_Reason 标识已执行/被拒组成部分 [Req 14.37]。"""
        decision = route_degraded("workspace.file.refresh", "/tmp/sock", "linux")
        assert decision.allowed is False
        assert len(decision.executed_components) > 0
        assert len(decision.rejected_components) > 0
        assert "index_refresh" in decision.executed_components
        assert "governance_record" in decision.rejected_components

    def test_cross_class_reason_has_components(self):
        decision = route_degraded("snapshot.publish", "/tmp/sock", "linux")
        assert decision.reason is not None
        assert "index_publish" in decision.reason.executed_components
        assert "governance_seal" in decision.reason.rejected_components


# ---------------------------------------------------------------------------
# Req 14.36: 状态推进约束
# ---------------------------------------------------------------------------


class TestStateAdvancement:
    """状态推进只挂在 Governance_Write 成功路径上。"""

    def test_degraded_index_write_no_state_advance(self):
        """Degraded_Mode 下 Index_Write 不推进状态。"""
        assert can_advance_state("graph.refresh", degraded=True) is False

    def test_degraded_governance_write_no_state_advance(self):
        """Degraded_Mode 下 Governance_Write 不推进状态（fail closed）。"""
        assert can_advance_state("verdict.submit", degraded=True) is False

    def test_normal_mode_allows_state_advance(self):
        """正常模式下按原有逻辑推进。"""
        assert can_advance_state("task.apply", degraded=False) is True

    def test_degraded_no_evidence_or_gate(self):
        """Degraded_Mode 下不产生 Evidence 与 gate decision [Req 14.36]。"""
        assert produces_evidence_or_gate("gate.decide", degraded=True) is False
        assert produces_evidence_or_gate("evidence.append", degraded=True) is False

    def test_normal_mode_produces_evidence(self):
        """正常模式下 Governance_Write 产生 Evidence。"""
        assert produces_evidence_or_gate("gate.decide", degraded=False) is True
        assert produces_evidence_or_gate("evidence.append", degraded=False) is True


# ---------------------------------------------------------------------------
# StructuredReason 序列化
# ---------------------------------------------------------------------------


class TestStructuredReason:
    """StructuredReason 结构完整性。"""

    def test_to_dict(self):
        reason = StructuredReason(
            code="E_TEST",
            message_key="error.test",
            recovery_guidance="run: cw_daemon",
            executed_components=["a"],
            rejected_components=["b"],
            context={"k": "v"},
        )
        d = reason.to_dict()
        assert d["code"] == "E_TEST"
        assert d["message_key"] == "error.test"
        assert d["recovery_guidance"] == "run: cw_daemon"
        assert d["executed_components"] == ["a"]
        assert d["rejected_components"] == ["b"]
        assert d["context"] == {"k": "v"}
