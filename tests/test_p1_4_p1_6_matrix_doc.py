"""P1-4 / P1-6 复审整改文档验证测试。

复审报告 feature-matrix-code-reaudit-2026-07-21.md §P1-4（QueryBudget 不是通用 daemon 查询预算）
+ §P1-6（Python daemon 与 Rust system daemon 能力被混为一谈）。

P1-4 / P1-6 是文档化任务（不修复代码缺陷，只做能力区分文档化）：
- 矩阵从 ✅ 回退为 🟡 后已正确反映实际状态
- 本批次补充详细缺口清单到 implementation-status.md §六
- G29 标题收紧为「Frontier budget（仅 compute_frontier_with_budget，通用 query budget 未实现）」
- G28 从 ✅ 改为 🟡（SnapshotManagerService budget 接入残缺）
- G13/G14/G15 添加 P1-6 能力区分标注

测试只验证文档同步性，不验证代码（因为 P1-4/P1-6 未修复代码）。
"""

import os
import sys
from pathlib import Path

import pytest

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# P1-4: G29 标题收紧 + G28 状态回退 + implementation-status §六
# ============================================================

def test_p1_4_g29_title_restricted_to_frontier_budget():
    """P1-4: G29 标题已收紧为「Frontier budget」，明确范围。"""
    matrix = ROOT / "_feature_matrix.md"
    content = matrix.read_text(encoding="utf-8")

    # 找到 G29 行
    lines = content.splitlines()
    g29_line = next((line for line in lines if line.startswith("| G29 |")), None)
    assert g29_line is not None, "G29 行必须存在"

    # 标题必须收紧（不再泛称 QueryBudget 限制）
    assert "Frontier budget" in g29_line, (
        f"G29 标题必须收紧为「Frontier budget」，实际：{g29_line}"
    )
    # 必须明确「通用 query budget 未实现」
    assert "通用 query budget 未实现" in g29_line, (
        f"G29 必须明确标注「通用 query budget 未实现」，实际：{g29_line}"
    )


def test_p1_4_g29_status_is_yellow_reaudit():
    """P1-4: G29 状态为 🟡 复审整改（批次33）。"""
    matrix = ROOT / "_feature_matrix.md"
    content = matrix.read_text(encoding="utf-8")

    lines = content.splitlines()
    g29_line = next((line for line in lines if line.startswith("| G29 |")), None)
    assert g29_line is not None
    assert "🟡 复审整改（2026-07-21 批次33）" in g29_line, (
        f"G29 状态应为 🟡 复审整改（批次33），实际：{g29_line}"
    )


def test_p1_4_g28_status_downgraded_to_yellow():
    """P1-4: G28 状态从 ✅ 降级为 🟡（SnapshotManagerService budget 接入残缺）。"""
    matrix = ROOT / "_feature_matrix.md"
    content = matrix.read_text(encoding="utf-8")

    lines = content.splitlines()
    g28_line = next((line for line in lines if line.startswith("| G28 |")), None)
    assert g28_line is not None
    assert "🟡 复审整改（2026-07-21 批次33）" in g28_line, (
        f"G28 状态应为 🟡 复审整改（批次33），实际：{g28_line}"
    )
    # 必须提到 budget 接入残缺
    assert "budget 接入残缺" in g28_line or "budget" in g28_line.lower(), (
        f"G28 必须提到 budget 接入残缺，实际：{g28_line}"
    )


def test_p1_4_implementation_status_has_section_six():
    """P1-4: implementation-status.md 包含 §六 复审整改章节。"""
    status = ROOT / "docs" / "design" / "implementation-status.md"
    content = status.read_text(encoding="utf-8")

    assert "## 六、复审整改（2026-07-21 批次33：P1-4 + P1-6）" in content, (
        "implementation-status.md 必须包含 §六 复审整改章节"
    )


def test_p1_4_implementation_status_documents_query_budget_gap():
    """P1-4: implementation-status.md §六详细记录 QueryBudget 缺口。"""
    status = ROOT / "docs" / "design" / "implementation-status.md"
    content = status.read_text(encoding="utf-8")

    # 必须包含 6.1-6.6 子章节（标题中的 SnapshotManagerService 有反引号包裹）
    assert "6.1 QueryBudget 定义位置" in content
    assert "6.2 QueryBudget 的消费点" in content
    assert "6.3 Daemon 查询 RPC 的 budget 接入状态" in content
    assert "6.4 Python" in content and "budget 接入残缺详情" in content
    assert "6.5 MCP 工具层完全不暴露 timeout/truncate" in content
    assert "6.6 P1-4 剩余工作" in content


def test_p1_4_implementation_status_mentions_field_mismatch():
    """P1-4: 文档必须记录 Rust/Python QueryBudget 字段不对齐。"""
    status = ROOT / "docs" / "design" / "implementation-status.md"
    content = status.read_text(encoding="utf-8")

    # Rust 3 字段 vs Python 5 字段
    assert "max_depth / max_nodes / timeout_ms（3 字段）" in content, (
        "必须记录 Rust QueryBudget 是 3 字段"
    )
    assert "max_results / frontier_limit（5 字段" in content, (
        "必须记录 Python QueryBudget 多 max_results/frontier_limit 共 5 字段"
    )


def test_p1_4_implementation_status_mentions_missing_python_rpcs():
    """P1-4: 文档必须记录 Python daemon 缺 3 个查询 RPC。"""
    status = ROOT / "docs" / "design" / "implementation-status.md"
    content = status.read_text(encoding="utf-8")

    # Python daemon 缺 call_chain_down / topological_order / detect_cycles
    assert "❌ **未实现**" in content, (
        "必须标记 Python daemon 未实现的查询 RPC"
    )


def test_p1_4_implementation_status_mentions_visit_node_missing():
    """P1-4: 文档必须记录 SnapshotManagerService 不调用 b.visit_node()。"""
    status = ROOT / "docs" / "design" / "implementation-status.md"
    content = status.read_text(encoding="utf-8")

    assert "从不调用 `b.visit_node()`" in content, (
        "必须记录 query_call_chain_down / query_detect_cycles 不调用 b.visit_node()"
    )


# ============================================================
# P1-6: G13/G14/G15 状态 + daemon 能力区分矩阵
# ============================================================

def test_p1_6_g13_status_mentions_p1_6_remediation():
    """P1-6: G13 添加 P1-6 复审整改标注。"""
    matrix = ROOT / "_feature_matrix.md"
    content = matrix.read_text(encoding="utf-8")

    lines = content.splitlines()
    g13_line = next((line for line in lines if line.startswith("| G13 |")), None)
    assert g13_line is not None
    assert "P1-6 复审整改（2026-07-21 批次33）" in g13_line, (
        f"G13 必须包含 P1-6 复审整改标注，实际：{g13_line}"
    )


def test_p1_6_g14_status_mentions_p1_6_remediation():
    """P1-6: G14 添加 P1-6 复审整改标注。"""
    matrix = ROOT / "_feature_matrix.md"
    content = matrix.read_text(encoding="utf-8")

    lines = content.splitlines()
    g14_line = next((line for line in lines if line.startswith("| G14 |")), None)
    assert g14_line is not None
    assert "P1-6 复审整改（2026-07-21 批次33）" in g14_line


def test_p1_6_g15_status_mentions_p1_6_remediation():
    """P1-6: G15 添加 P1-6 复审整改标注。"""
    matrix = ROOT / "_feature_matrix.md"
    content = matrix.read_text(encoding="utf-8")

    lines = content.splitlines()
    g15_line = next((line for line in lines if line.startswith("| G15 |")), None)
    assert g15_line is not None
    assert "P1-6 复审整改（2026-07-21 批次33）" in g15_line


def test_p1_6_implementation_status_has_daemon_capability_matrix():
    """P1-6: implementation-status.md 包含 daemon 能力区分矩阵。"""
    status = ROOT / "docs" / "design" / "implementation-status.md"
    content = status.read_text(encoding="utf-8")

    assert "6.7 daemon 能力区分矩阵" in content, (
        "必须包含 §6.7 daemon 能力区分矩阵"
    )
    assert "6.8 P1-6 结论" in content, "必须包含 §6.8 P1-6 结论"


def test_p1_6_daemon_matrix_lists_python_vs_rust_capabilities():
    """P1-6: daemon 能力区分矩阵必须列出 Python vs Rust 各项能力。"""
    status = ROOT / "docs" / "design" / "implementation-status.md"
    content = status.read_text(encoding="utf-8")

    # 必须列出关键能力对比
    assert "Metrics 收集器（G13）" in content
    assert "Health Check（G14）" in content
    assert "Schema Migrator（G15）" in content
    assert "Replicator CAS→Manifest→Snapshot（G11）" in content
    assert "memfd 六重校验（G10）" in content

    # 必须标注「Python 已实现，Rust 未对齐」
    assert "Python 已实现，Rust 未对齐" in content, (
        "必须明确标注 Python 已实现但 Rust 未对齐的能力"
    )


def test_p1_6_conclusion_states_python_not_proof_of_rust():
    """P1-6: 结论必须明确「不能用 Python 单例证明 Rust 服务具备相同能力」。"""
    status = ROOT / "docs" / "design" / "implementation-status.md"
    content = status.read_text(encoding="utf-8")

    assert "不能用 Python 单例证明 Rust 服务具备相同能力" in content, (
        "P1-6 结论必须明确不能用 Python 单例证明 Rust 服务"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
