"""Phase 6-1 P3 差分测试：defect_correlation Rust 实现与 Python 一致性验证

**本文件对应 Phase 6-1 P3（defect_correlation Rust 迁移）的 D3 差分矩阵。**

差分测试矩阵（D3.1 - D3.8）：
  TestDefectCorrelationDiff：
    - D3.1: 单文件单变更点 + 窗口内有 2 个 findings
    - D3.2: 单文件多变更点（3 个变更点，窗口=2）
    - D3.3: 多文件变更（2 个文件，各 1 个变更点）
    - D3.4: 窗口边界（window_commits=1，只有紧邻下一个版本的 findings 被匹配）
    - D3.5: 去重验证（同一 finding 被多个窗口匹配只计一次）
    - D3.6: 直接关联缺陷（direct_findings 不在窗口内但通过 qualified_name 关联）
    - D3.7: 空变更（changes_by_file 为空，total_changes=0）
    - D3.8: 空 findings（findings_by_file_hash 为空，defects_after_change=0）

预期差异：无
  - Rust 与 Python 在窗口切片 + finding 匹配 + 去重 + 聚合逻辑上完全对齐
  - after_change_at 在 Python baseline 中统一为 0.0（与 Rust 输出一致）

前置条件：
  - Rust 扩展 callwarden_core 必须可加载
  - 如果不可加载，本测试套件会显式 skip 并给出修复指引

关联：
  - Python 真相源：db/db_evolution.py:EvolutionMixin.defect_correlation (L311-L447)
  - Rust 真相源：rust_ext/src/impact.rs:defect_correlation_core + py_defect_correlation
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Tuple

import pytest

# ============================================
# 前置条件：Rust 扩展可用性检查
# ============================================

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

_RUST_EXT_AVAILABLE = False
_RUST_EXT_SKIP_REASON = ""
try:
    import callwarden_core  # type: ignore
    _RUST_EXT_AVAILABLE = True
except ImportError as _e:
    _RUST_EXT_SKIP_REASON = (
        f"callwarden_core 不可加载：{_e}。"
        "请先运行 `maturin develop --manifest-path rust_ext/Cargo.toml --release` "
        "或 `pip install --force-reinstall rust_ext/target/wheels/callwarden_core-*.whl`。"
    )


# ============================================
# Python baseline: defect_correlation 纯函数实现
# ============================================
# 从 db/db_evolution.py::defect_correlation (L311-L447) 提取的核心逻辑
# 与 Rust 入参完全一致：所有数据均由调用方预查询后传入

# 类型别名（对齐 Rust 函数签名）
# changes_by_file: List[Tuple[int, List[int]]]               → (file_instance_id, [version_num, ...])
# all_versions_by_file: List[Tuple[int, List[Tuple[int, str]]]]  → (file_instance_id, [(version_num, content_hash), ...])
# findings_by_file_hash: List[Tuple[int, str, List[Tuple]]]   → (file_id, content_hash, [(id, rule_id, rule_name, severity, start_line, end_line, scanned_at, message), ...])
# direct_findings: List[Tuple[int, str, str, str, int, int, float, str]]
FindingTuple = Tuple[int, str, str, str, int, int, float, str]


def _py_defect_correlation(
    changes_by_file: List[Tuple[int, List[int]]],
    all_versions_by_file: List[Tuple[int, List[Tuple[int, str]]]],
    findings_by_file_hash: List[Tuple[int, str, List[FindingTuple]]],
    direct_findings: List[FindingTuple],
    window_commits: int,
) -> Dict[str, Any]:
    """Python baseline: defect_correlation 核心逻辑

    对齐 db/db_evolution.py:EvolutionMixin.defect_correlation 的 Python 全路径 fallback。
    所有 SQL 查询结果由调用方预查询后传入（与 Rust 入参完全一致）。
    """
    # 转换为 dict 便于查找（对齐 Python 原实现的 dict 语义）
    all_versions_map: Dict[int, List[Tuple[int, str]]] = dict(all_versions_by_file)
    findings_map: Dict[Tuple[int, str], List[FindingTuple]] = {}
    for fid, ch, findings in findings_by_file_hash:
        findings_map[(fid, ch)] = findings

    # 对齐 Python: total_changes = sum(len(v) for v in changes_by_file.values())
    total_changes = sum(len(v[1]) for v in changes_by_file)

    defect_findings: List[Dict[str, Any]] = []
    defect_types: Dict[str, int] = {}
    seen_finding_ids: set = set()

    # 遍历变更点（对齐 Python: for file_instance_id, change_versions in changes_by_file.items():）
    for file_instance_id, change_versions in changes_by_file:
        all_versions = all_versions_map.get(file_instance_id, [])
        version_num_to_index = {v[0]: idx for idx, v in enumerate(all_versions)}

        for change_version_num in change_versions:
            idx = version_num_to_index.get(change_version_num)
            if idx is None:
                continue
            # 窗口切片：从当前变更版本的下一个版本开始，取 window_commits 个版本
            window_versions = all_versions[idx + 1: idx + 1 + window_commits]
            window_hashes = [v[1] for v in window_versions if v[1]]
            for wh in window_hashes:
                key = (file_instance_id, wh)
                for f in findings_map.get(key, []):
                    fid = f[0]  # finding id
                    if fid in seen_finding_ids:
                        continue
                    seen_finding_ids.add(fid)
                    defect_findings.append({
                        "rule_id": f[1],
                        "rule_name": f[2],
                        "severity": f[3],
                        "start_line": f[4],
                        "end_line": f[5],
                        "scanned_at": f[6],
                        "message": f[7],
                        "after_change_at": 0.0,
                    })
                    defect_types[f[1]] = defect_types.get(f[1], 0) + 1

    # 补充：通过 symbol_qualified 直接关联的缺陷（不局限于窗口）
    # 对齐 Python: for frow in cur:（直接关联查询）
    for f in direct_findings:
        fid = f[0]
        if fid in seen_finding_ids:
            continue
        seen_finding_ids.add(fid)
        defect_findings.append({
            "rule_id": f[1],
            "rule_name": f[2],
            "severity": f[3],
            "start_line": f[4],
            "end_line": f[5],
            "scanned_at": f[6],
            "message": f[7],
            "after_change_at": 0.0,
        })
        defect_types[f[1]] = defect_types.get(f[1], 0) + 1

    return {
        "total_changes": total_changes,
        "defects_after_change": len(defect_findings),
        "defect_types": defect_types,
        "findings": defect_findings,
    }


# ============================================
# 归一化对比工具
# ============================================

def _normalize(result: Dict[str, Any]) -> Dict[str, Any]:
    """归一化结果用于对比

    - total_changes / defects_after_change：直接比较标量
    - defect_types：转为 sorted dict（对齐 Rust 按 key 排序）
    - findings：转为 sorted tuple 列表（忽略顺序差异）
    """
    findings = sorted([tuple(sorted(d.items())) for d in result.get("findings", [])])
    return {
        "total_changes": result.get("total_changes", 0),
        "defects_after_change": result.get("defects_after_change", 0),
        "defect_types": dict(sorted(result.get("defect_types", {}).items())),
        "findings": findings,
    }


def _assert_defect_correlation_equal(py_result: Dict[str, Any],
                                      rust_result: Dict[str, Any]) -> None:
    """断言 Python baseline 与 Rust 输出完全一致"""
    py_norm = _normalize(py_result)
    rust_norm = _normalize(rust_result)
    for key in ("total_changes", "defects_after_change", "defect_types", "findings"):
        assert py_norm[key] == rust_norm[key], (
            f"field '{key}' mismatch:\n"
            f"  py={py_norm[key]}\n  rust={rust_norm[key]}"
        )


# ============================================
# 辅助函数：构造 finding 元组
# ============================================

def _finding(
    fid: int, rule_id: str, rule_name: str, severity: str,
    start_line: int, end_line: int, scanned_at: float, message: str,
) -> FindingTuple:
    """构造 finding 元组（对齐 Rust FindingInfo 字段顺序）"""
    return (fid, rule_id, rule_name, severity, start_line, end_line, scanned_at, message)


# ============================================
# D3.1: 单文件单变更点 + 窗口内有 2 个 findings
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestDefectCorrelationDiffD3_1:
    """D3.1: 单文件单变更点 + 窗口内有 2 个 findings

    文件 1 在版本 1 变更，后续窗口 [v2, v3, v4] 内有两个版本的 findings，
    预期 defects_after_change=2
    """

    def test_d3_1_single_change_two_findings(self):
        changes_by_file: List[Tuple[int, List[int]]] = [(1, [1])]
        all_versions_by_file: List[Tuple[int, List[Tuple[int, str]]]] = [
            (1, [
                (1, "hash_v1"),
                (2, "hash_v2"),
                (3, "hash_v3"),
                (4, "hash_v4"),
            ]),
        ]
        findings_by_file_hash: List[Tuple[int, str, List[FindingTuple]]] = [
            (1, "hash_v2", [_finding(100, "rule_a", "Rule A", "high", 10, 20, 1000.0, "a")]),
            (1, "hash_v3", [_finding(101, "rule_b", "Rule B", "low", 30, 40, 2000.0, "b")]),
        ]
        direct_findings: List[FindingTuple] = []
        window_commits = 3

        py_result = _py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )
        rust_result = callwarden_core.py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )

        _assert_defect_correlation_equal(py_result, rust_result)

        # 具体断言
        assert rust_result["total_changes"] == 1
        assert rust_result["defects_after_change"] == 2
        assert len(rust_result["findings"]) == 2
        rule_ids = {f["rule_id"] for f in rust_result["findings"]}
        assert rule_ids == {"rule_a", "rule_b"}


# ============================================
# D3.2: 单文件多变更点（3 个变更点，窗口=2）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestDefectCorrelationDiffD3_2:
    """D3.2: 单文件多变更点

    文件 1 有 3 个变更点（版本 1, 3, 5），window_commits=2，
    每个变更点的窗口内有不同数量的 findings
    """

    def test_d3_2_multiple_change_points(self):
        changes_by_file: List[Tuple[int, List[int]]] = [(1, [1, 3, 5])]
        all_versions_by_file: List[Tuple[int, List[Tuple[int, str]]]] = [
            (1, [
                (1, "h1"),
                (2, "h2"),  # v1 的窗口 [v2, v3]
                (3, "h3"),
                (4, "h4"),  # v3 的窗口 [v4, v5]
                (5, "h5"),
                (6, "h6"),  # v5 的窗口 [v6, v7]
                (7, "h7"),
            ]),
        ]
        findings_by_file_hash: List[Tuple[int, str, List[FindingTuple]]] = [
            (1, "h2", [_finding(1, "r1", "R1", "low", 1, 2, 1.0, "v1-window-1")]),
            (1, "h4", [_finding(2, "r2", "R2", "low", 1, 2, 2.0, "v3-window-1")]),
            (1, "h6", [_finding(3, "r3", "R3", "low", 1, 2, 3.0, "v5-window-1")]),
        ]
        direct_findings: List[FindingTuple] = []
        window_commits = 2

        py_result = _py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )
        rust_result = callwarden_core.py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )

        _assert_defect_correlation_equal(py_result, rust_result)

        # 具体断言
        assert rust_result["total_changes"] == 3
        assert rust_result["defects_after_change"] == 3
        assert len(rust_result["findings"]) == 3


# ============================================
# D3.3: 多文件变更（2 个文件，各 1 个变更点）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestDefectCorrelationDiffD3_3:
    """D3.3: 多文件变更

    2 个文件各 1 个变更点，每个变更点窗口内各有 1 个 finding，
    预期 defects_after_change=2
    """

    def test_d3_3_multi_file_changes(self):
        changes_by_file: List[Tuple[int, List[int]]] = [
            (1, [1]),  # 文件 1 在版本 1 变更
            (2, [1]),  # 文件 2 在版本 1 变更
        ]
        all_versions_by_file: List[Tuple[int, List[Tuple[int, str]]]] = [
            (1, [(1, "f1_h1"), (2, "f1_h2"), (3, "f1_h3")]),
            (2, [(1, "f2_h1"), (2, "f2_h2"), (3, "f2_h3")]),
        ]
        findings_by_file_hash: List[Tuple[int, str, List[FindingTuple]]] = [
            (1, "f1_h2", [_finding(10, "rA", "RA", "med", 5, 10, 100.0, "file1")]),
            (2, "f2_h2", [_finding(20, "rB", "RB", "low", 15, 25, 200.0, "file2")]),
        ]
        direct_findings: List[FindingTuple] = []
        window_commits = 2

        py_result = _py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )
        rust_result = callwarden_core.py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )

        _assert_defect_correlation_equal(py_result, rust_result)

        # 具体断言
        assert rust_result["total_changes"] == 2
        assert rust_result["defects_after_change"] == 2
        rule_ids = {f["rule_id"] for f in rust_result["findings"]}
        assert rule_ids == {"rA", "rB"}


# ============================================
# D3.4: 窗口边界（window_commits=1）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestDefectCorrelationDiffD3_4:
    """D3.4: 窗口边界

    window_commits=1，只有紧邻变更版本下一个版本的 findings 被匹配，
    再后面的版本不在窗口内
    """

    def test_d3_4_window_boundary(self):
        changes_by_file: List[Tuple[int, List[int]]] = [(1, [1])]
        all_versions_by_file: List[Tuple[int, List[Tuple[int, str]]]] = [
            (1, [
                (1, "h1"),
                (2, "h2"),  # 在窗口内（紧邻下一个）
                (3, "h3"),  # 不在窗口内（window_commits=1）
                (4, "h4"),  # 不在窗口内
            ]),
        ]
        findings_by_file_hash: List[Tuple[int, str, List[FindingTuple]]] = [
            (1, "h2", [_finding(1, "r_in", "RIn", "low", 1, 2, 1.0, "in-window")]),
            (1, "h3", [_finding(2, "r_out", "ROut", "low", 1, 2, 2.0, "out-of-window")]),
            (1, "h4", [_finding(3, "r_far", "RFar", "low", 1, 2, 3.0, "far-out")]),
        ]
        direct_findings: List[FindingTuple] = []
        window_commits = 1

        py_result = _py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )
        rust_result = callwarden_core.py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )

        _assert_defect_correlation_equal(py_result, rust_result)

        # 具体断言：只有 h2 的 finding 被匹配
        assert rust_result["defects_after_change"] == 1
        assert rust_result["findings"][0]["rule_id"] == "r_in"


# ============================================
# D3.5: 去重验证（同一 finding 被多个窗口匹配只计一次）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestDefectCorrelationDiffD3_5:
    """D3.5: 去重验证

    同一个 finding id 出现在多个版本的 findings 中（被多个窗口匹配），
    预期只计一次
    """

    def test_d3_5_dedup_same_finding_id(self):
        changes_by_file: List[Tuple[int, List[int]]] = [(1, [1, 2])]
        all_versions_by_file: List[Tuple[int, List[Tuple[int, str]]]] = [
            (1, [
                (1, "h1"),
                (2, "h2"),  # v1 窗口的下一个版本
                (3, "h3"),  # v2 窗口的下一个版本
            ]),
        ]
        # h2 和 h3 都包含同一 finding id=200，应去重
        findings_by_file_hash: List[Tuple[int, str, List[FindingTuple]]] = [
            (1, "h2", [_finding(200, "r_dup", "RDup", "med", 1, 5, 100.0, "dup")]),
            (1, "h3", [_finding(200, "r_dup", "RDup", "med", 1, 5, 100.0, "dup")]),
        ]
        direct_findings: List[FindingTuple] = []
        window_commits = 2

        py_result = _py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )
        rust_result = callwarden_core.py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )

        _assert_defect_correlation_equal(py_result, rust_result)

        # 具体断言：去重后只剩 1 个
        assert rust_result["defects_after_change"] == 1
        assert len(rust_result["findings"]) == 1
        assert rust_result["findings"][0]["rule_id"] == "r_dup"
        # defect_types 计数应为 1
        assert rust_result["defect_types"]["r_dup"] == 1


# ============================================
# D3.6: 直接关联缺陷（direct_findings 不在窗口内）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestDefectCorrelationDiffD3_6:
    """D3.6: 直接关联缺陷

    direct_findings 不在窗口内（findings_by_file_hash 为空），
    但通过 symbol_qualified 直接关联，预期被收集到结果中
    """

    def test_d3_6_direct_findings(self):
        changes_by_file: List[Tuple[int, List[int]]] = [(1, [1])]
        all_versions_by_file: List[Tuple[int, List[Tuple[int, str]]]] = [
            (1, [(1, "h1"), (2, "h2")]),
        ]
        # findings_by_file_hash 为空（窗口内无 findings）
        findings_by_file_hash: List[Tuple[int, str, List[FindingTuple]]] = []
        # 直接关联的缺陷（通过 symbol_qualified 查询得到）
        direct_findings: List[FindingTuple] = [
            _finding(300, "direct_rule", "Direct", "high", 1, 10, 500.0, "direct-qn"),
            _finding(301, "direct_rule2", "Direct2", "low", 20, 30, 600.0, "direct-qn2"),
        ]
        window_commits = 2

        py_result = _py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )
        rust_result = callwarden_core.py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )

        _assert_defect_correlation_equal(py_result, rust_result)

        # 具体断言
        assert rust_result["defects_after_change"] == 2
        assert len(rust_result["findings"]) == 2
        rule_ids = {f["rule_id"] for f in rust_result["findings"]}
        assert rule_ids == {"direct_rule", "direct_rule2"}
        # after_change_at 应为 0.0
        for f in rust_result["findings"]:
            assert f["after_change_at"] == 0.0


# ============================================
# D3.7: 空变更（changes_by_file 为空）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestDefectCorrelationDiffD3_7:
    """D3.7: 空变更

    changes_by_file 为空列表，预期 total_changes=0，
    defects_after_change=0（无变更点，无窗口，无 findings）
    """

    def test_d3_7_empty_changes(self):
        changes_by_file: List[Tuple[int, List[int]]] = []
        all_versions_by_file: List[Tuple[int, List[Tuple[int, str]]]] = []
        findings_by_file_hash: List[Tuple[int, str, List[FindingTuple]]] = []
        direct_findings: List[FindingTuple] = []
        window_commits = 5

        py_result = _py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )
        rust_result = callwarden_core.py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )

        _assert_defect_correlation_equal(py_result, rust_result)

        # 具体断言
        assert rust_result["total_changes"] == 0
        assert rust_result["defects_after_change"] == 0
        assert len(rust_result["findings"]) == 0
        assert rust_result["defect_types"] == {}


# ============================================
# D3.8: 空 findings（findings_by_file_hash 为空）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestDefectCorrelationDiffD3_8:
    """D3.8: 空 findings

    findings_by_file_hash 为空（窗口内无任何 finding），
    但有变更点，预期 total_changes>0 但 defects_after_change=0
    """

    def test_d3_8_empty_findings(self):
        changes_by_file: List[Tuple[int, List[int]]] = [(1, [1, 2])]
        all_versions_by_file: List[Tuple[int, List[Tuple[int, str]]]] = [
            (1, [(1, "h1"), (2, "h2"), (3, "h3")]),
        ]
        # findings_by_file_hash 为空（无任何 semgrep findings）
        findings_by_file_hash: List[Tuple[int, str, List[FindingTuple]]] = []
        direct_findings: List[FindingTuple] = []
        window_commits = 5

        py_result = _py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )
        rust_result = callwarden_core.py_defect_correlation(
            changes_by_file, all_versions_by_file,
            findings_by_file_hash, direct_findings, window_commits,
        )

        _assert_defect_correlation_equal(py_result, rust_result)

        # 具体断言
        assert rust_result["total_changes"] == 2
        assert rust_result["defects_after_change"] == 0
        assert len(rust_result["findings"]) == 0
        assert rust_result["defect_types"] == {}
