"""P1-5 基线脚本扫描盲区修复验证测试。

复审报告 feature-matrix-remediation-reaudit-2026-07-22.md §3 P1-5（L156-168）：
- check_baseline.py --check 声称"76 文档零不一致"但存在扫描盲区：
  1. db_files 只匹配 "N 个 db_*.py" 正向格式，抓不到 "db_*.py`（N 个文件）" 反向格式
  2. SKIP_MARKERS 含 "→"，整行跳过含 → 的行（含当前数字也被跳过）
  3. 输出用 len(doc_paths) 而非实际扫描文件数

P1-5 修复：
- db_files 增加 reverse_pattern 捕获 "db_*.py`（N 个文件）" 格式
- 移除 "→" 从 SKIP_MARKERS，改为在 → 处分段，只检查 → 之后的当前值
- scan_document_consistency 返回 (inconsistencies, scanned_count)，报告实际扫描数
"""
import os
import sys
from pathlib import Path

import pytest

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from scripts.check_baseline import (
    generate_baseline,
    scan_document_consistency,
)


# ============================================================
# 辅助：构造临时 .md 文件并扫描
# ============================================================

def _scan_lines(lines, baseline=None):
    """把 lines 写入临时 .md 文件并扫描，返回 (inconsistencies, scanned_count)。

    临时文件写入项目根目录下（tests/_p1_5_tmp_scan.md），确保
    scan_document_consistency 的 relative_to(PROJECT_ROOT) 能正常工作。
    """
    if baseline is None:
        baseline = {
            "mcp_tools": 206,
            "db_files": 40,
            "mixin_functional": 35,
            "mixin_total": 36,
            "languages": 16,
            "schema_version": 41,
            "product_version": "0.3.0",
        }

    project_root = Path(__file__).resolve().parent.parent
    tmp_path = project_root / "tests" / "_p1_5_tmp_scan.md"

    try:
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return scan_document_consistency(baseline, [tmp_path])
    finally:
        tmp_path.unlink(missing_ok=True)


# ============================================================
# P1-5 测试组 1: db_files reverse_pattern 盲区修复
# ============================================================

def test_p1_5_reverse_pattern_catches_db_files_after_format():
    """P1-5: db_files reverse_pattern 捕获 "db_*.py`（N 个文件）" 格式。

    复审报告：docs/architecture.md:49 用 `` `db_*.py`（39 个文件） `` 格式，
    旧扫描器只匹配 "N 个 db_*.py" 正向格式，抓不到这种数字在 db_*.py 之后的格式。
    """
    inconsistencies, _ = _scan_lines([
        "| 业务层 | 35 个功能 Mixin + 1 基类 | `db.py` + `db_*.py`（39 个文件） |",
    ])

    # 应检测到 db_files 不一致（期望 40，实际 39）
    db_issues = [i for i in inconsistencies if i["key"] == "db_files"]
    assert len(db_issues) == 1, (
        f"reverse_pattern 应捕获 'db_*.py`（39 个文件）' 格式，"
        f"实际检测到 {len(db_issues)} 个 db_files 问题"
    )
    assert db_issues[0]["found"] == 39
    assert db_issues[0]["expected"] == 40


def test_p1_5_reverse_pattern_catches_full_width_paren():
    """P1-5: reverse_pattern 兼容全角括号（）和半角括号 ()。"""
    # 全角括号
    inc1, _ = _scan_lines(["`db_*.py`（39 个文件）"])
    assert any(i["key"] == "db_files" and i["found"] == 39 for i in inc1)

    # 半角括号
    inc2, _ = _scan_lines(["`db_*.py` (39 files)"])
    assert any(i["key"] == "db_files" and i["found"] == 39 for i in inc2)


def test_p1_5_reverse_pattern_correct_value_not_flagged():
    """P1-5: reverse_pattern 不误报正确值（40 个文件）。"""
    inconsistencies, _ = _scan_lines([
        "| 业务层 | `db_*.py`（40 个文件） |",
    ])
    db_issues = [i for i in inconsistencies if i["key"] == "db_files"]
    assert len(db_issues) == 0, "正确值 40 不应被标记为不一致"


# ============================================================
# P1-5 测试组 2: → 分段检查（不再整行跳过）
# ============================================================

def test_p1_5_arrow_segment_checks_post_arrow_value():
    """P1-5: 含 → 的行不再被整行跳过，→ 之后的当前值仍被检查。

    复审报告：_feature_matrix.md:362 含 "23→33 个 Mixin 类（39 个 db_*.py 文件"，
    因 → 在 SKIP_MARKERS 中，整行被跳过，33/39 两个错误数字未被检测到。
    """
    # → 之后的 Mixin 数错误（35 写成 33）
    inc1, _ = _scan_lines([
        'Mixin 模块数 23→33 个 Mixin 类（40 个 db_*.py 文件）',
    ])
    mixin_issues = [i for i in inc1 if i["key"] == "mixin_functional"]
    assert len(mixin_issues) == 1, (
        f"→ 之后的 Mixin 数 33（应 35）应被检测到，实际: {len(mixin_issues)} 个问题"
    )
    assert mixin_issues[0]["found"] == 33

    # → 之后的 db_files 数错误（40 写成 39）
    inc2, _ = _scan_lines([
        'Mixin 模块数 23→35 个 Mixin 类（39 个 db_*.py 文件）',
    ])
    db_issues = [i for i in inc2 if i["key"] == "db_files"]
    assert len(db_issues) == 1, (
        f"→ 之后的 db_files 数 39（应 40）应被检测到，实际: {len(db_issues)} 个问题"
    )
    assert db_issues[0]["found"] == 39


def test_p1_5_arrow_segment_correct_values_pass():
    """P1-5: → 之后正确值不误报。"""
    inconsistencies, _ = _scan_lines([
        'Mixin 模块数 23→35 个 Mixin 类（40 个 db_*.py 文件）',
    ])
    assert len(inconsistencies) == 0, (
        f"→ 之后正确值 35/40 不应被标记，实际: {len(inconsistencies)} 个问题"
    )


def test_p1_5_version_evolution_still_skipped():
    """P1-5: 明确的版本演化行（vN →）仍被跳过。"""
    # v3 → v9 这种版本演化行应被跳过
    inconsistencies, _ = _scan_lines([
        "Schema 从 v3 → v9 演化",
    ])
    assert len(inconsistencies) == 0, "版本演化行（vN →）应被跳过"


# ============================================================
# P1-5 测试组 3: 实际扫描文件数
# ============================================================

def test_p1_5_scan_returns_scanned_count():
    """P1-5: scan_document_consistency 返回 (inconsistencies, scanned_count)。

    旧实现只返回 inconsistencies，main() 用 len(doc_paths) 报告总数，
    导致"76 文档一致"声明包含跳过的文件。
    """
    # 用项目内实际 .md 文件扫描
    project_root = Path(__file__).resolve().parent.parent
    doc_paths = list(project_root.glob("*.md"))
    doc_paths.extend(project_root.glob("docs/**/*.md"))
    if not doc_paths:
        pytest.skip("项目内无 .md 文件")

    baseline = generate_baseline()
    result = scan_document_consistency(baseline, doc_paths)

    # 新返回值是 tuple (inconsistencies, scanned_count)
    assert isinstance(result, tuple), "scan_document_consistency 应返回 tuple"
    assert len(result) == 2, "返回值应为 (inconsistencies, scanned_count)"
    inconsistencies, scanned = result
    assert isinstance(inconsistencies, list)
    assert isinstance(scanned, int)

    # scanned_count 应 <= len(doc_paths)（因为跳过了历史/审计文档）
    assert scanned <= len(doc_paths), (
        f"scanned_count ({scanned}) 应 <= 传入文件数 ({len(doc_paths)})"
    )
    # scanned_count 应 > 0（至少扫描了一些文件）
    assert scanned > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
