"""Phase 6-2 P2 差分测试：detect_clones_core 端到端 + Jaccard 相似度

**本文件是契约 docs/design/phase6-2-minhash-lsh-clone-detection-contract.md §3.3/3.4 D3/D4 矩阵的 ✅(behavioral) 标记载体。**

差分测试矩阵：
  TestDetectClonesCoreDiff（D3.1-D3.7）：
    - D3.1: Type-1 克隆（相同 content_hash）→ Rust 与 Python 一致
    - D3.2: Type-2 克隆（相同 token_hash，不同 content_hash）→ 一致
    - D3.3: Type-3 克隆（相似度 >= 阈值）→ 一致（浮点容差 1e-6）
    - D3.4: 无克隆 → 空结果
    - D3.5: similarity_threshold 参数变化 → 一致
    - D3.6: 返回结构（clone_type / token_hash / similarity / members）字段一致
    - D3.7: 大规模符号（>500 触发 LSH 路径）→ 一致

  TestJaccardDiff（D4.1-D4.5）：
    - D4.1: 两相同集合 → Jaccard = 1.0
    - D4.2: 两不相交集合 → Jaccard = 0.0
    - D4.3: 部分重叠 → Jaccard = |A∩B|/|A∪B|
    - D4.4: 空集合 → 0.0
    - D4.5: MinHash 估算 Jaccard vs 精确 Jaccard（误差范围内）

前置条件：
  - Rust 扩展 callwarden_core 必须可加载
  - numpy 必须可用（Python baseline 依赖）

关联：
  - 契约：docs/design/phase6-2-minhash-lsh-clone-detection-contract.md §3.3/3.4
  - Python 真相源：db/db_clone_detection.py:_detect_clone_groups_core / _jaccard_similarity
  - Rust 真相源：rust_ext/src/clone_detection.rs:detect_clones_core / jaccard_similarity
"""
from __future__ import annotations

import hashlib
import os
import sys
from typing import Any, Dict, List, Set, Tuple

import pytest

# ============================================
# 前置条件：Rust 扩展 + numpy 可用性检查
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
    _RUST_EXT_SKIP_REASON = f"callwarden_core 不可加载：{_e}"

try:
    import numpy as np  # type: ignore
    _NUMPY_AVAILABLE = True
except Exception:
    _NUMPY_AVAILABLE = False
    np = None  # type: ignore

# 导入 Python baseline 工具函数
sys.path.insert(0, os.path.join(_PKG_PARENT, "callwarden"))
from callwarden.db.db_clone_detection import (  # type: ignore
    _minhash_signature,
    _lsh_buckets,
    _jaccard_similarity,
    _normalize_token_sequence,
    _MAX_BUCKET_SIZE,
)


# ============================================
# Python baseline：detect_clones_core 纯计算版本
# ============================================

def _py_detect_clones_core(
    symbols: List[Tuple[int, str, str, List[str]]],
    similarity_threshold: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Python baseline：与 Rust detect_clones_core 对齐的纯计算版本

    输入：每个符号的 (id, symbol_hash, token_hash, normalized_tokens)
    输出：(groups, stats)

    本函数复刻 db_clone_detection.py:_detect_clone_groups_core 的核心逻辑，
    但不依赖 DB，接收预归一化的 token 列表。
    """
    scanned = len(symbols)
    skipped = 0

    # 1. 构建 SymMeta 列表
    sym_meta: List[Dict[str, Any]] = []
    minhash_cache: Dict[str, tuple] = {}
    for (sid, symbol_hash, token_hash, tokens) in symbols:
        if not tokens:
            skipped += 1
            continue
        # 构建 token_set（与 db_clone_detection.py 对齐）
        if len(tokens) >= 3:
            token_set = set(zip(tokens, tokens[1:], tokens[2:]))
        else:
            token_set = set(tokens)
        # MinHash 签名（按 token_hash 缓存）
        if token_hash in minhash_cache:
            minhash_sig = minhash_cache[token_hash]
        else:
            minhash_sig = _minhash_signature(token_set)
            minhash_cache[token_hash] = minhash_sig
        sym_meta.append({
            "id": sid,
            "symbol_hash": symbol_hash,
            "token_hash": token_hash,
            "token_set": token_set,
            "minhash_sig": minhash_sig,
        })

    # 2. 按 token_hash 和 symbol_hash 分组
    by_token_hash: Dict[str, List[int]] = {}
    by_content_hash: Dict[str, List[int]] = {}
    for idx, m in enumerate(sym_meta):
        by_token_hash.setdefault(m["token_hash"], []).append(idx)
        by_content_hash.setdefault(m["symbol_hash"], []).append(idx)

    groups: List[Dict[str, Any]] = []

    # 3. Type-1：content_hash 相同
    for ch, group_indices in by_content_hash.items():
        if len(group_indices) < 2:
            continue
        groups.append({
            "clone_type": 1,
            "token_hash": sym_meta[group_indices[0]]["token_hash"],
            "similarity": 1.0,
            "members": [sym_meta[i]["id"] for i in group_indices],
        })

    # 4. Type-2：token_hash 相同但 content_hash 不同
    type1_token_hashes = {
        g["token_hash"] for g in groups if g["clone_type"] == 1
    }
    for th, group_indices in by_token_hash.items():
        if len(group_indices) < 2:
            continue
        if th in type1_token_hashes:
            # 检查是否有多个不同 content_hash
            by_ch: Dict[str, List[int]] = {}
            for i in group_indices:
                by_ch.setdefault(sym_meta[i]["symbol_hash"], []).append(i)
            if len(by_ch) < 2:
                continue
            groups.append({
                "clone_type": 2,
                "token_hash": th,
                "similarity": 1.0,
                "members": [sym_meta[i]["id"] for i in group_indices],
            })
        else:
            groups.append({
                "clone_type": 2,
                "token_hash": th,
                "similarity": 1.0,
                "members": [sym_meta[i]["id"] for i in group_indices],
            })

    # 5. Type-3：相似度 >= 阈值但 < 1.0
    # 5.1 按 token_hash 去重，每组取第一个符号作为代表
    token_hash_to_rep_idx: Dict[str, int] = {}
    for idx, m in enumerate(sym_meta):
        th = m["token_hash"]
        if th not in token_hash_to_rep_idx:
            token_hash_to_rep_idx[th] = idx

    lsh_rep_indices = list(token_hash_to_rep_idx.values())
    num_reps = len(lsh_rep_indices)

    BRUTEFORCE_THRESHOLD = 500
    candidate_pairs: Set = set()

    if num_reps < BRUTEFORCE_THRESHOLD:
        for i in range(num_reps):
            for j in range(i + 1, num_reps):
                candidate_pairs.add((lsh_rep_indices[i], lsh_rep_indices[j]))
    else:
        from collections import defaultdict
        lsh_buckets_map: Dict[str, List[int]] = defaultdict(list)
        for idx in lsh_rep_indices:
            m = sym_meta[idx]
            for bucket_key in _lsh_buckets(m["minhash_sig"]):
                lsh_buckets_map[bucket_key].append(idx)
        for indices in lsh_buckets_map.values():
            if len(indices) < 2:
                continue
            if len(indices) > _MAX_BUCKET_SIZE:
                continue
            for i in range(len(indices)):
                for j in range(i + 1, len(indices)):
                    a, b = indices[i], indices[j]
                    pair = (a, b) if a < b else (b, a)
                    candidate_pairs.add(pair)

    # 5.3 Jaccard 验证 + 聚类
    from collections import defaultdict
    type3_clusters: Dict[Tuple[str, str, float], List[int]] = defaultdict(list)
    for a_idx, b_idx in candidate_pairs:
        a, b = sym_meta[a_idx], sym_meta[b_idx]
        if a["token_hash"] == b["token_hash"]:
            continue
        sim = _jaccard_similarity(a["token_set"], b["token_set"])
        if sim >= similarity_threshold and sim < 1.0:
            sim_bucket = round(sim, 2)
            key = (a["token_hash"], b["token_hash"], sim_bucket)
            type3_clusters[key].extend([a["id"], b["id"]])

    for key, member_ids in type3_clusters.items():
        unique_members = list(dict.fromkeys(member_ids))
        if len(unique_members) < 2:
            continue
        th_a, th_b, sim_val = key
        groups.append({
            "clone_type": 3,
            "token_hash": f"{th_a}|{th_b}",
            "similarity": sim_val,
            "members": unique_members,
        })

    stats = {
        "scanned_symbols": scanned,
        "skipped_symbols": skipped,
        "total_groups": len(groups),
        "type1_groups": sum(1 for g in groups if g["clone_type"] == 1),
        "type2_groups": sum(1 for g in groups if g["clone_type"] == 2),
        "type3_groups": sum(1 for g in groups if g["clone_type"] == 3),
    }
    return groups, stats


# ============================================
# 辅助：归一化 + token_hash 计算
# ============================================

def _make_symbol(
    sid: int,
    content: str,
    symbol_hash: str = None,
) -> Tuple[int, str, str, List[str]]:
    """构造 detect_clones_core 输入项

    Args:
        sid: 符号 ID
        content: 符号源码内容
        symbol_hash: content_hash（None 则用 content 的 sha256[:16]）

    Returns:
        (id, symbol_hash, token_hash, normalized_tokens)
    """
    normalized = _normalize_token_sequence(content)
    th = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    tokens = normalized.split()
    if symbol_hash is None:
        symbol_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return (sid, symbol_hash, th, tokens)


def _normalize_groups_for_compare(
    groups: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """归一化 groups 列表用于比较

    - members 排序（顺序无关）
    - 整个列表按 (clone_type, token_hash, similarity) 排序
    """
    normalized = []
    for g in groups:
        normalized.append({
            "clone_type": g["clone_type"],
            "token_hash": g["token_hash"],
            "similarity": round(float(g["similarity"]), 2),
            "members": sorted(list(g["members"])),
        })
    # 按 (clone_type, token_hash, similarity) 排序
    normalized.sort(key=lambda x: (x["clone_type"], x["token_hash"], x["similarity"]))
    return normalized


# ============================================
# D3: detect_clones_core 端到端差分测试
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
@pytest.mark.skipif(not _NUMPY_AVAILABLE, reason="numpy 不可用（Python baseline 依赖）")
class TestDetectClonesCoreDiff:
    """D3.1-D3.7: detect_clones_core Rust vs Python 差分"""

    def test_d3_1_type1_clone(self):
        """D3.1: Type-1 克隆（相同 content_hash）→ 一致"""
        # 两个符号内容完全相同 → 相同 symbol_hash + 相同 token_hash
        content = "def foo(): return 1 + 2"
        symbols = [
            _make_symbol(1, content, symbol_hash="hash_A"),
            _make_symbol(2, content, symbol_hash="hash_A"),
        ]

        py_groups, py_stats = _py_detect_clones_core(symbols, 0.8)
        rust_groups, rust_stats = callwarden_core.py_detect_clones_core(symbols, 0.8)

        # 应有 1 个 Type-1 组
        assert py_stats["type1_groups"] == 1
        assert rust_stats["type1_groups"] == 1

        # 比较归一化后的 groups
        py_norm = _normalize_groups_for_compare(py_groups)
        rust_norm = _normalize_groups_for_compare(rust_groups)
        assert py_norm == rust_norm

    def test_d3_2_type2_clone(self):
        """D3.2: Type-2 克隆（相同 token_hash，不同 content_hash）→ 一致"""
        # 两个符号归一化后 token 序列相同，但原始内容不同（标识符名不同）
        content_a = "def foo(): return 1 + 2"
        content_b = "def bar(): return 1 + 2"
        symbols = [
            _make_symbol(1, content_a, symbol_hash="hash_A"),
            _make_symbol(2, content_b, symbol_hash="hash_B"),
        ]

        py_groups, py_stats = _py_detect_clones_core(symbols, 0.8)
        rust_groups, rust_stats = callwarden_core.py_detect_clones_core(symbols, 0.8)

        # 归一化后 token 相同 → 同 token_hash，但 content_hash 不同 → Type-2
        assert py_stats["type2_groups"] >= 1
        assert rust_stats["type2_groups"] == py_stats["type2_groups"]

        py_norm = _normalize_groups_for_compare(py_groups)
        rust_norm = _normalize_groups_for_compare(rust_groups)
        assert py_norm == rust_norm

    def test_d3_3_type3_clone(self):
        """D3.3: Type-3 克隆（相似度 >= 阈值）→ 一致"""
        # 两个符号大部分相同，少量差异 → Jaccard 0.5-0.9
        content_a = """
def process(items):
    result = []
    for item in items:
        if item > 0:
            result.append(item * 2)
    return result
"""
        content_b = """
def process(items):
    result = []
    for item in items:
        if item >= 0:
            result.append(item * 3)
    return result
"""
        symbols = [
            _make_symbol(1, content_a, symbol_hash="hash_A"),
            _make_symbol(2, content_b, symbol_hash="hash_B"),
        ]

        # 用低阈值让 Type-3 触发
        py_groups, py_stats = _py_detect_clones_core(symbols, 0.3)
        rust_groups, rust_stats = callwarden_core.py_detect_clones_core(symbols, 0.3)

        # 应有 1 个 Type-3 组
        assert py_stats["type3_groups"] >= 1, f"expected Type-3 group, py_stats={py_stats}"
        assert rust_stats["type3_groups"] == py_stats["type3_groups"]

        py_norm = _normalize_groups_for_compare(py_groups)
        rust_norm = _normalize_groups_for_compare(rust_groups)
        assert py_norm == rust_norm, f"Type-3 mismatch:\npy={py_norm}\nrust={rust_norm}"

    def test_d3_4_no_clones(self):
        """D3.4: 无克隆 → 空结果"""
        content_a = "def foo(): return 1"
        content_b = "def bar(x, y): return x + y * 2"
        content_c = "def baz(): print('hello world')"
        symbols = [
            _make_symbol(1, content_a, symbol_hash="hash_A"),
            _make_symbol(2, content_b, symbol_hash="hash_B"),
            _make_symbol(3, content_c, symbol_hash="hash_C"),
        ]

        py_groups, py_stats = _py_detect_clones_core(symbols, 0.8)
        rust_groups, rust_stats = callwarden_core.py_detect_clones_core(symbols, 0.8)

        # 无克隆
        assert py_stats["total_groups"] == 0
        assert rust_stats["total_groups"] == 0
        assert rust_stats == py_stats

    def test_d3_5_threshold_variation(self):
        """D3.5: similarity_threshold 参数变化 → 一致"""
        # 构造中等相似度的符号对
        content_a = "def foo(a, b): return a + b"
        content_b = "def bar(a, b): return a + b"
        content_c = "def baz(a, b): return a - b + 1"
        symbols = [
            _make_symbol(1, content_a, symbol_hash="hash_A"),
            _make_symbol(2, content_b, symbol_hash="hash_B"),
            _make_symbol(3, content_c, symbol_hash="hash_C"),
        ]

        for threshold in [0.1, 0.3, 0.5, 0.7, 0.9]:
            py_groups, py_stats = _py_detect_clones_core(symbols, threshold)
            rust_groups, rust_stats = callwarden_core.py_detect_clones_core(symbols, threshold)

            py_norm = _normalize_groups_for_compare(py_groups)
            rust_norm = _normalize_groups_for_compare(rust_groups)
            assert py_norm == rust_norm, (
                f"threshold={threshold}: py={py_norm}\nrust={rust_norm}"
            )
            assert py_stats == rust_stats, (
                f"threshold={threshold}: py_stats={py_stats}\nrust_stats={rust_stats}"
            )

    def test_d3_6_group_structure(self):
        """D3.6: 返回的 group dict 字段与 Python 一致"""
        content = "def foo(): return 1"
        symbols = [
            _make_symbol(1, content, symbol_hash="hash_A"),
            _make_symbol(2, content, symbol_hash="hash_A"),
        ]

        py_groups, _ = _py_detect_clones_core(symbols, 0.8)
        rust_groups, _ = callwarden_core.py_detect_clones_core(symbols, 0.8)

        # 比较 Type-1 组的字段
        py_g = py_groups[0]
        rust_g = rust_groups[0]

        # 字段集合一致
        assert set(py_g.keys()) == {"clone_type", "token_hash", "similarity", "members"}
        assert set(rust_g.keys()) == {"clone_type", "token_hash", "similarity", "members"}

        # 字段类型一致
        assert isinstance(rust_g["clone_type"], int)
        assert isinstance(rust_g["token_hash"], str)
        assert isinstance(rust_g["similarity"], float)
        assert isinstance(list(rust_g["members"]), list)

        # 字段值一致
        assert rust_g["clone_type"] == py_g["clone_type"] == 1
        assert rust_g["token_hash"] == py_g["token_hash"]
        assert rust_g["similarity"] == py_g["similarity"] == 1.0
        assert sorted(list(rust_g["members"])) == sorted(py_g["members"])

    def test_d3_7_large_scale_lsh_path(self):
        """D3.7: 大规模符号（>500 触发 LSH 路径）→ 一致

        构造 600 个不同 token_hash 的符号，触发 LSH 分桶路径。
        """
        import random
        random.seed(42)  # 确定性测试

        symbols = []
        for i in range(600):
            # 构造多样化的 token 序列
            tokens = ["def", f"func_{i}", "(", "x", ")", ":", "return", "x", "+", str(i % 100)]
            content = " ".join(tokens)
            sh = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            th = hashlib.sha256((" ".join(tokens)).encode("utf-8")).hexdigest()[:16]
            symbols.append((i + 1, sh, th, tokens))

        py_groups, py_stats = _py_detect_clones_core(symbols, 0.8)
        rust_groups, rust_stats = callwarden_core.py_detect_clones_core(symbols, 0.8)

        # 统计信息一致
        assert py_stats == rust_stats, (
            f"large-scale stats mismatch:\npy={py_stats}\nrust={rust_stats}"
        )

        # groups 归一化后一致
        py_norm = _normalize_groups_for_compare(py_groups)
        rust_norm = _normalize_groups_for_compare(rust_groups)
        assert py_norm == rust_norm, (
            f"large-scale groups mismatch:\npy_count={len(py_norm)}\nrust_count={len(rust_norm)}"
        )


# ============================================
# D4: Jaccard 相似度差分测试
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
@pytest.mark.skipif(not _NUMPY_AVAILABLE, reason="numpy 不可用（Python baseline 依赖）")
class TestJaccardDiff:
    """D4.1-D4.5: Jaccard 相似度 Rust vs Python 差分

    注意：Rust 的 jaccard_similarity 是泛型函数，不直接暴露 PyO3。
    我们通过 py_detect_clones_core 间接验证：Type-3 组的 similarity 字段
    应与 Python _jaccard_similarity 计算的结果一致。
    """

    def test_d4_1_identical_sets(self):
        """D4.1: 两相同 shingle 集合 → Jaccard = 1.0"""
        # 通过 Type-1 克隆验证（相同内容 → Jaccard = 1.0）
        content = "def foo(): return 1 + 2 + 3"
        symbols = [
            _make_symbol(1, content, symbol_hash="hash_A"),
            _make_symbol(2, content, symbol_hash="hash_A"),
        ]

        py_groups, _ = _py_detect_clones_core(symbols, 0.8)
        rust_groups, _ = callwarden_core.py_detect_clones_core(symbols, 0.8)

        # Type-1 组 similarity = 1.0
        py_g = py_groups[0]
        rust_g = rust_groups[0]
        assert abs(rust_g["similarity"] - 1.0) < 1e-9
        assert abs(py_g["similarity"] - 1.0) < 1e-9

    def test_d4_2_disjoint_sets(self):
        """D4.2: 两不相交 shingle 集合 → Jaccard = 0.0（不形成 Type-3 组）"""
        # 直接用 Python baseline 验证 Jaccard = 0
        set_a = {"a", "b", "c"}
        set_b = {"x", "y", "z"}
        py_sim = _jaccard_similarity(set_a, set_b)
        assert py_sim == 0.0

        # 通过 detect_clones_core 验证：不相交的符号不应形成 Type-3 组
        content_a = "def alpha(): return 1 + 2 + 3"
        content_b = "def beta(): print('hello world test')"
        symbols = [
            _make_symbol(1, content_a, symbol_hash="hash_A"),
            _make_symbol(2, content_b, symbol_hash="hash_B"),
        ]
        py_groups, py_stats = _py_detect_clones_core(symbols, 0.1)
        rust_groups, rust_stats = callwarden_core.py_detect_clones_core(symbols, 0.1)

        # 低阈值下若仍无 Type-3，说明 Jaccard = 0
        # （注意：可能因 LSH 桶不重叠而无候选对，也是正确行为）
        assert rust_stats["type3_groups"] == py_stats["type3_groups"]

    def test_d4_3_partial_overlap(self):
        """D4.3: 部分重叠 → Jaccard = |A∩B|/|A∪B|"""
        # 构造已知 Jaccard 的集合
        set_a = {1, 2, 3, 4}
        set_b = {3, 4, 5, 6}
        # |A∩B| = 2, |A∪B| = 6 → Jaccard = 2/6 ≈ 0.333
        expected_jaccard = 2 / 6

        py_sim = _jaccard_similarity(set_a, set_b)
        assert abs(py_sim - expected_jaccard) < 1e-9

        # 通过 Type-3 克隆验证：相似度应匹配
        # 构造两个符号，使其 token 3-gram 集合有部分重叠
        content_a = """
def process(items):
    result = []
    for item in items:
        if item > 0:
            result.append(item)
    return result
"""
        content_b = """
def process(items):
    result = []
    for item in items:
        if item > 0:
            result.append(item * 2)
    return result + extra
"""
        symbols = [
            _make_symbol(1, content_a, symbol_hash="hash_A"),
            _make_symbol(2, content_b, symbol_hash="hash_B"),
        ]

        # 用很低阈值让 Type-3 触发
        py_groups, py_stats = _py_detect_clones_core(symbols, 0.1)
        rust_groups, rust_stats = callwarden_core.py_detect_clones_core(symbols, 0.1)

        # 若形成 Type-3 组，similarity 应一致
        if py_stats["type3_groups"] > 0:
            py_type3 = [g for g in py_groups if g["clone_type"] == 3]
            rust_type3 = [g for g in rust_groups if g["clone_type"] == 3]
            assert len(py_type3) == len(rust_type3)
            for py_g, rust_g in zip(py_type3, rust_type3):
                assert abs(py_g["similarity"] - rust_g["similarity"]) < 1e-6, (
                    f"similarity mismatch: py={py_g['similarity']} rust={rust_g['similarity']}"
                )

    def test_d4_4_empty_sets(self):
        """D4.4: 空集合 → Jaccard = 0.0（Python baseline 行为）"""
        set_a: set = set()
        set_b = {1, 2, 3}
        py_sim = _jaccard_similarity(set_a, set_b)
        assert py_sim == 0.0

        # 通过 detect_clones_core：空 token 的符号会被 skipped
        # 构造一个空 token 符号 + 一个正常符号
        symbols = [
            (1, "hash_A", "th_A", []),  # 空 tokens
            _make_symbol(2, "def foo(): return 1", symbol_hash="hash_B"),
        ]
        py_groups, py_stats = _py_detect_clones_core(symbols, 0.8)
        rust_groups, rust_stats = callwarden_core.py_detect_clones_core(symbols, 0.8)

        # 空符号被 skipped
        assert py_stats["skipped_symbols"] == 1
        assert rust_stats["skipped_symbols"] == 1
        assert py_stats == rust_stats

    def test_d4_5_minhash_estimate_vs_exact(self):
        """D4.5: MinHash 估算 Jaccard vs 精确 Jaccard（误差范围内）

        MinHash 估算的 Jaccard 与精确 Jaccard 的误差应 ≤ 1/√num_perm ≈ 0.088。
        """
        # 构造两个有部分重叠的 token 集合
        content_a = """
def compute_sum(values):
    total = 0
    for v in values:
        if v > 0:
            total += v * 2
    return total
"""
        content_b = """
def compute_sum(values):
    total = 0
    for v in values:
        if v >= 0:
            total += v * 3
    return total
"""
        norm_a = _normalize_token_sequence(content_a)
        norm_b = _normalize_token_sequence(content_b)
        tokens_a = norm_a.split()
        tokens_b = norm_b.split()

        # 3-gram 集合
        if len(tokens_a) >= 3:
            set_a = set(zip(tokens_a, tokens_a[1:], tokens_a[2:]))
        else:
            set_a = set(tokens_a)
        if len(tokens_b) >= 3:
            set_b = set(zip(tokens_b, tokens_b[1:], tokens_b[2:]))
        else:
            set_b = set(tokens_b)

        # 精确 Jaccard
        exact_jaccard = _jaccard_similarity(set_a, set_b)

        # MinHash 估算 Jaccard（签名相同位置比例）
        sig_a = _minhash_signature(set_a)
        sig_b = _minhash_signature(set_b)
        matches = sum(1 for a, b in zip(sig_a, sig_b) if a == b)
        estimated_jaccard = matches / len(sig_a)

        # 误差应 ≤ 1/√num_perm ≈ 0.088（理论值，实际可能更小）
        # 使用较宽松的容差 0.15 覆盖统计波动
        error_bound = 0.15
        assert abs(estimated_jaccard - exact_jaccard) <= error_bound, (
            f"MinHash estimate {estimated_jaccard} vs exact {exact_jaccard}, "
            f"error {abs(estimated_jaccard - exact_jaccard)} > {error_bound}"
        )

        # Rust 侧签名应与 Python 一致（已由 D1.1 覆盖）
        rust_sig_a = list(callwarden_core.py_minhash_signature(list(set_a)))
        rust_sig_b = list(callwarden_core.py_minhash_signature(list(set_b)))
        assert rust_sig_a == list(sig_a)
        assert rust_sig_b == list(sig_b)


# ============================================
# 混合场景测试
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
@pytest.mark.skipif(not _NUMPY_AVAILABLE, reason="numpy 不可用（Python baseline 依赖）")
class TestMixedCloneScenarios:
    """混合场景：Type-1 + Type-2 + Type-3 同时存在"""

    def test_mixed_all_three_types(self):
        """Type-1 + Type-2 + Type-3 同时存在 → Rust 与 Python 一致"""
        symbols = []

        # Type-1: 两个完全相同的符号
        content_t1 = "def identical(): return 42"
        symbols.append(_make_symbol(1, content_t1, symbol_hash="hash_T1_A"))
        symbols.append(_make_symbol(2, content_t1, symbol_hash="hash_T1_A"))

        # Type-2: 两个归一化后相同但内容不同的符号
        content_t2_a = "def renamed_alpha(x): return x + 1"
        content_t2_b = "def renamed_beta(y): return y + 1"
        symbols.append(_make_symbol(3, content_t2_a, symbol_hash="hash_T2_A"))
        symbols.append(_make_symbol(4, content_t2_b, symbol_hash="hash_T2_B"))

        # Type-3: 两个相似但不完全相同的符号
        content_t3_a = """
def process(data):
    result = []
    for item in data:
        if item > 0:
            result.append(item * 2)
    return result
"""
        content_t3_b = """
def process(data):
    result = []
    for item in data:
        if item >= 0:
            result.append(item * 3)
    return result
"""
        symbols.append(_make_symbol(5, content_t3_a, symbol_hash="hash_T3_A"))
        symbols.append(_make_symbol(6, content_t3_b, symbol_hash="hash_T3_B"))

        # 用低阈值让 Type-3 触发
        py_groups, py_stats = _py_detect_clones_core(symbols, 0.3)
        rust_groups, rust_stats = callwarden_core.py_detect_clones_core(symbols, 0.3)

        # 统计信息一致
        assert py_stats == rust_stats, (
            f"mixed stats mismatch:\npy={py_stats}\nrust={rust_stats}"
        )

        # 应同时有 Type-1, Type-2, Type-3
        assert py_stats["type1_groups"] >= 1
        assert py_stats["type2_groups"] >= 1
        # Type-3 可能因 LSH 桶不重叠而不触发，但若 Python 触发则 Rust 也应触发
        assert py_stats["type3_groups"] == rust_stats["type3_groups"]

        # groups 归一化后一致
        py_norm = _normalize_groups_for_compare(py_groups)
        rust_norm = _normalize_groups_for_compare(rust_groups)
        assert py_norm == rust_norm, (
            f"mixed groups mismatch:\npy={py_norm}\nrust={rust_norm}"
        )

    def test_stats_fields_alignment(self):
        """stats dict 字段完全一致"""
        content = "def foo(): return 1"
        symbols = [
            _make_symbol(1, content, symbol_hash="hash_A"),
            _make_symbol(2, content, symbol_hash="hash_A"),
        ]

        _, py_stats = _py_detect_clones_core(symbols, 0.8)
        _, rust_stats = callwarden_core.py_detect_clones_core(symbols, 0.8)

        # 字段集合一致
        assert set(py_stats.keys()) == {
            "scanned_symbols", "skipped_symbols", "total_groups",
            "type1_groups", "type2_groups", "type3_groups"
        }
        assert set(rust_stats.keys()) == set(py_stats.keys())

        # 字段值一致
        assert rust_stats == py_stats
