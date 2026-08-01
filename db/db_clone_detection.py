"""
db_clone_detection.py
=====================

重复代码检测 Mixin。

基于 tree-sitter token 序列和符号内容，检测 Type-1/2/3 重复代码：
- Type-1：完全相同的符号内容（content_hash 相同，或归一化 token 序列相同）
- Type-2：重命名克隆（token 序列相同，但标识符名不同）
- Type-3：微调克隆（相似度 >= similarity_threshold，但 < 1.0）

通过 Mixin 模式集成到 CodeGraphDB 主类。
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import numpy as np
    _NUMPY_IMPORT_ERROR = None
except Exception as _exc:  # 平台原生库加载失败时不阻断 CLI/MCP 启动
    np = None
    _NUMPY_IMPORT_ERROR = _exc

from ..i18n import t


# 克隆类型常量
CLONE_TYPE_1 = 1  # 完全相同
CLONE_TYPE_2 = 2  # 重命名克隆
CLONE_TYPE_3 = 3  # 微调克隆


# 需要归一化的 token 类别（用于 Type-2 检测）
# 标识符、字符串字面量、数字字面量归一化，保留结构
_NORMALIZE_TOKEN_RE = re.compile(
    r"""
    (?P<ident>[A-Za-z_][A-Za-z0-9_]*) |     # 标识符
    (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*') |  # 字符串
    (?P<num>\d+\.?\d*)                      # 数字
    """,
    re.VERBOSE,
)


def _normalize_token_sequence(content: str) -> str:
    """把符号内容归一化为 token 序列，用于 Type-2 检测

    归一化策略：
    - 所有标识符替换为 ID
    - 所有字符串替换为 STR
    - 所有数字替换为 NUM
    - 保留关键字、运算符、标点符号
    - 移除注释和空白

    Args:
        content: 符号源代码内容

    Returns:
        归一化后的 token 序列字符串（空格分隔）
    """
    # 移除注释（Python 和 JS 风格）
    no_comments = re.sub(
        r"#.*$|//.*$|/\*.*?\*/", "", content, flags=re.MULTILINE | re.DOTALL
    )

    tokens = []
    pos = 0
    while pos < len(no_comments):
        m = _NORMALIZE_TOKEN_RE.match(no_comments, pos)
        if m:
            if m.group("ident"):
                tokens.append("ID")
            elif m.group("str"):
                tokens.append("STR")
            elif m.group("num"):
                tokens.append("NUM")
            pos = m.end()
        else:
            ch = no_comments[pos]
            if not ch.isspace():
                tokens.append(ch)
            pos += 1

    return " ".join(tokens)


def _token_hash(content: str) -> str:
    """计算归一化 token 序列的 hash

    Args:
        content: 符号源代码内容

    Returns:
        SHA-256 前 16 位的 hash 字符串
    """
    normalized = _normalize_token_sequence(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _jaccard_similarity(set_a: set, set_b: set) -> float:
    """计算两个集合的 Jaccard 相似度

    Args:
        set_a: 集合 A
        set_b: 集合 B

    Returns:
        相似度 [0, 1]，空集相似度为 0
    """
    if not set_a or not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


# ==================== MinHash + LSH（P1 优化 + Phase 7.1 稳定 hash）====================

# Phase 7.1 修复：使用稳定的 FNV-1a hash 替代 Python 内置 hash()。
# Python hash() 在不同进程间结果不同（PYTHONHASHSEED 随机化），
# 导致同一代码库在不同运行中产生不同的 MinHash 签名和 LSH 桶，
# clone 检测结果不可复现。FNV-1a 是确定性 hash，跨进程稳定。

# FNV-1a 参数（32 位）
_FNV_OFFSET_BASIS = 0x811C9DC5
_FNV_PRIME = 0x01000193


def _fnv1a_32(data: bytes) -> int:
    """FNV-1a 32 位 hash（确定性，跨进程稳定）

    Args:
        data: 字节串

    Returns:
        32 位无符号整数 hash 值
    """
    h = _FNV_OFFSET_BASIS
    for b in data:
        h ^= b
        h = (h * _FNV_PRIME) & 0xFFFFFFFF
    return h


def _stable_token_hash(token) -> int:
    """计算 token 的稳定 hash（FNV-1a，跨进程确定性）

    Phase 7.1：替代 Python 内置 hash()，确保 MinHash 签名跨进程一致。

    支持两种输入：
    - str：直接编码为 UTF-8 后 hash
    - tuple（3-gram shingle）：先转为固定格式字符串再 hash，
      确保 (a, b, c) 和 "a|b|c" 一一对应

    Args:
        token: token 字符串或 3-gram tuple

    Returns:
        32 位无符号整数
    """
    if isinstance(token, tuple):
        # 3-gram shingle：用分隔符拼接为字符串
        token = "\x1f".join(str(t) for t in token)
    elif not isinstance(token, str):
        token = str(token)
    return _fnv1a_32(token.encode("utf-8"))


# 预生成 128 个独立 hash 函数系数（模块加载时计算一次，进程内固定）。
# 使用通用 hash family: h_i(x) = (a_i * x + b_i) mod 2^32
# 每个 (a_i, b_i) 由 SHA-256 派生，保证两两不同且统计独立。
# a_i 强制为奇数，确保与 2^32 互质（避免高比特位丢失）。
# 系数截断到 32 位，确保 numpy uint64 乘法不溢出（a * bh < 2^64）。
_HASH_COEFFS: list = []
for _i in range(128):
    _h = hashlib.sha256(f"callwarden_minhash_perm_{_i}".encode("utf-8")).digest()
    _a = (int.from_bytes(_h[:4], "little") | 1) & 0xFFFFFFFF  # 32 位奇数
    _b = int.from_bytes(_h[4:8], "little") & 0xFFFFFFFF       # 32 位
    _HASH_COEFFS.append((_a, _b))

# 仅在 numpy 可用时预生成数组；平台原生库不可用不应阻断普通查询启动。
_HASH_A_NP = (
    np.array([c[0] for c in _HASH_COEFFS], dtype=np.uint64)
    if np is not None else None
)
_HASH_B_NP = (
    np.array([c[1] for c in _HASH_COEFFS], dtype=np.uint64)
    if np is not None else None
)
_MASK_32 = np.uint64(0xFFFFFFFF) if np is not None else None

# Phase 7.1：大桶保护参数
# LSH 桶中符号数超过此值时跳过该桶（降级为暴力比较的子集），
# 避免常见模式（如 return / break / continue）导致的所有符号落入同一桶。
_MAX_BUCKET_SIZE = 200


def _minhash_signature(token_set: set, num_perm: int = 128) -> tuple:
    """计算 token 集合的 MinHash 签名

    MinHash 通过对集合元素施加多个独立哈希函数，取最小值作为签名。
    两个集合 MinHash 签名在相同位置的概率 ≈ Jaccard 相似度。

    Phase 7.1 修复：使用 FNV-1a 稳定 hash 替代 Python 内置 hash()。
    - 旧实现：hash(t) 受 PYTHONHASHSEED 影响，跨进程不可复现
    - 新实现：_stable_token_hash(t) 基于 FNV-1a，确定性
    签名在跨进程、跨机器环境下完全一致，clone 检测结果可复现。

    P1 优化（核心）：numpy 向量化计算 (a*x+b) mod 2^32，替代 Python 循环。
    - 每 token 只算一次稳定 hash，得到 base_hash 数组
    - 128 个 perm 通过 numpy 广播一次计算所有 hash，再取 min
    - 实测 Android 22K 符号 MinHash 阶段 29s → ~2s（约 15x 加速）

    Args:
        token_set: token 集合
        num_perm: 哈希函数数量（签名长度），默认 128

    Returns:
        长度为 num_perm 的签名 tuple，每个元素是 32 位无符号整数。
        空集合返回全 0xFFFFFFFF。
    """
    if not token_set:
        return tuple([0xFFFFFFFF] * num_perm)

    if np is None or _HASH_A_NP is None or _HASH_B_NP is None or _MASK_32 is None:
        raise RuntimeError(
            "clone detection requires a working numpy runtime; "
            f"import failed: {_NUMPY_IMPORT_ERROR}"
        )

    # numpy 向量化：对所有 perm × 所有 token 一次性计算
    # shape: (num_perm, N)，每行是该 perm 下所有 token 的 hash
    base_hashes = np.array(
        [_stable_token_hash(t) for t in token_set], dtype=np.uint64
    )

    all_hashes = (
        _HASH_A_NP[:num_perm, None] * base_hashes[None, :]
        + _HASH_B_NP[:num_perm, None]
    ) & _MASK_32

    # 每行取 min → 签名
    signature = all_hashes.min(axis=1)
    return tuple(int(x) for x in signature)


def _lsh_buckets(signature: tuple, num_bands: int = 8, rows_per_band: int = 16) -> list:
    """将 MinHash 签名分桶（Locality-Sensitive Hashing）

    LSH 把签名分成 num_bands 个带，每带 rows_per_band 个哈希值。
    两个签名在任意一个带上完全相同，就是候选相似对。

    阈值公式：t ≈ (1/num_bands)^(1/rows_per_band)
    P1 修复参数：b=8, r=16 → t ≈ 0.88，配合 3-gram token 集合使用。
    3-gram 集合区分度高，即使 LSH 阈值 0.88 略高于检测阈值 0.8，
    也能有效筛选真正的克隆对。

    Args:
        signature: MinHash 签名（长度需 >= num_bands * rows_per_band）
        num_bands: 带数
        rows_per_band: 每带行数

    Returns:
        桶 key 列表（num_bands 个），用于分组候选对
    """
    buckets = []
    for i in range(num_bands):
        start = i * rows_per_band
        end = start + rows_per_band
        band = signature[start:end]
        # 把带内的哈希值拼接成字符串作为桶 key
        bucket_key = f"b{i}:" + ":".join(str(h) for h in band)
        buckets.append(bucket_key)
    return buckets


class CloneDetectionMixin:
    """重复代码检测 Mixin

    通过 self.conn 访问数据库连接，提供 Type-1/2/3 重复代码检测能力。
    检测结果持久化到 clone_pairs 表，支持 workspace 隔离和增量更新。

    Phase 7.0 新增：detect_clones_to_groups 把结果写入 clone_groups +
    clone_group_members（不展开成 pairs），适合后台 job 异步执行。

    Phase 6-2 wire-production: _detect_clone_groups_core 接入 Rust 短路
    （feature=rust_clone_detection）。Rust 负责重计算（MinHash/LSH/Jaccard/
    分组），Python 负责 DB 查询和 token 归一化。Rust 失败时 fail-soft
    降级到 Python 全路径。
    """

    def _detect_clone_groups_via_rust(
        self,
        symbols: List[Dict[str, Any]],
        similarity_threshold: float,
    ) -> Optional[Tuple[List[Dict[str, Any]], Dict[str, int]]]:
        """Rust 短路：调用 callwarden_core.py_detect_clones_core

        Python 负责 DB 查询 + token 归一化（_normalize_token_sequence），
        Rust 负责 3-gram 构建 + MinHash 签名 + LSH 分桶 + Jaccard 验证 +
        Type-1/2/3 分组。

        Args:
            symbols: DB 查询得到的符号列表（dict row）
            similarity_threshold: Type-3 相似度阈值

        Returns:
            (groups, stats) 或 None（Rust 不可用/失败）
        """
        try:
            import callwarden_core  # type: ignore
        except ImportError:
            return None

        # 构造 Rust 输入：List[Tuple[int, str, str, List[str]]]
        rust_input: List[Tuple[int, str, str, List[str]]] = []
        for s in symbols:
            content = s.get("content") or ""
            if not content:
                continue
            normalized = _normalize_token_sequence(content)
            th = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
            tokens = normalized.split()
            rust_input.append((
                s["id"],
                s["symbol_hash"],
                th,
                tokens,
            ))

        try:
            groups, stats = callwarden_core.py_detect_clones_core(
                rust_input, similarity_threshold
            )
            # 物化懒批对象为 list（AGENTS.md 规则 17）
            groups_list = list(groups)
            stats_dict = dict(stats)
            # 物化 groups 中的 members
            for g in groups_list:
                g["members"] = list(g["members"])
            return groups_list, stats_dict
        except Exception:
            # Rust 异常 → fail-soft 降级到 Python
            return None

    def _detect_clone_groups_core(
        self,
        file_filter: str = "",
        min_lines: int = 5,
        similarity_threshold: float = 0.8,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """检测 clone groups 的核心逻辑（不写库）

        返回：
            groups: List[Dict]，每个 dict 含：
                - clone_type: int (1/2/3)
                - token_hash: str
                - similarity: float
                - members: List[int]（symbol IDs，第一个为 representative）
            stats: Dict 含 scanned_symbols / skipped_symbols / total_groups
        """
        ws_id = self._get_active_workspace_id()
        normalized_filter = file_filter.replace("\\", "/").strip()

        # 加载候选符号
        filter_clause = ""
        sql_params: List[Any] = [ws_id, min_lines]
        if normalized_filter:
            filter_clause = "AND fi.rel_path LIKE ?"
            sql_params.append(normalized_filter + "%")

        cur = self.conn.execute(
            f"""
            SELECT s.id, s.symbol_hash, s.name, s.kind, s.start_line, s.end_line,
                   s.qualified_name, fi.rel_path as file_path,
                   sc.content, sc.signature
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ?
              AND fi.status != 'archived'
              AND s.kind IN ('fn', 'function', 'method', 'test_fn')
              AND (s.end_line - s.start_line + 1) >= ?
              {filter_clause}
            ORDER BY s.id
            """,
            sql_params,
        )

        symbols = [dict(row) for row in cur]
        scanned = len(symbols)
        skipped = 0

        # Phase 6-2 wire-production: Rust 短路（feature=rust_clone_detection）
        # rollback_config 中 feature=rust_clone_detection 置为 1 时回退 Python 全路径
        # Rust 负责重计算（MinHash/LSH/Jaccard/分组），Python 负责 DB 查询和归一化
        # Rust 失败时 fail-soft 降级到 Python 全路径
        if not self.is_feature_rolled_back("rust_clone_detection"):
            rust_result = self._detect_clone_groups_via_rust(symbols, similarity_threshold)
            if rust_result is not None:
                groups, group_stats = rust_result
                # 补全 stats 中 Python 侧统计的 skipped_symbols
                # Rust 路径下 skipped = 无 content 的符号数
                skipped = sum(1 for s in symbols if not (s.get("content") or ""))
                group_stats["skipped_symbols"] = skipped
                group_stats["scanned_symbols"] = scanned
                return groups, group_stats

        # Python 全路径（fallback 或 rollback_flag=1 时）

        # 预计算每个符号的 token_hash 和 token 集合
        sym_meta: List[Dict[str, Any]] = []
        minhash_cache: Dict[str, tuple] = {}
        for s in symbols:
            content = s.get("content") or ""
            if not content:
                skipped += 1
                continue
            lines = s["end_line"] - s["start_line"] + 1
            normalized = _normalize_token_sequence(content)
            th = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
            tokens = normalized.split()
            if len(tokens) >= 3:
                token_set = set(zip(tokens, tokens[1:], tokens[2:]))
            else:
                token_set = set(tokens)
            if th in minhash_cache:
                minhash_sig = minhash_cache[th]
            else:
                minhash_sig = _minhash_signature(token_set)
                minhash_cache[th] = minhash_sig
            sym_meta.append({
                "id": s["id"],
                "symbol_hash": s["symbol_hash"],
                "name": s["name"],
                "content": content,
                "lines": lines,
                "token_hash": th,
                "token_set": token_set,
                "minhash_sig": minhash_sig,
                "qualified_name": s["qualified_name"],
                "file_path": s["file_path"],
            })

        # 按 token_hash 和 content_hash 分组
        by_token_hash: Dict[str, List[Dict[str, Any]]] = {}
        by_content_hash: Dict[str, List[Dict[str, Any]]] = {}
        for m in sym_meta:
            by_token_hash.setdefault(m["token_hash"], []).append(m)
            by_content_hash.setdefault(m["symbol_hash"], []).append(m)

        groups: List[Dict[str, Any]] = []

        # Type-1：content_hash 相同
        for ch, group_syms in by_content_hash.items():
            if len(group_syms) < 2:
                continue
            groups.append({
                "clone_type": CLONE_TYPE_1,
                "token_hash": group_syms[0]["token_hash"],
                "similarity": 1.0,
                "members": [s["id"] for s in group_syms],
            })

        # Type-2：token_hash 相同但 content_hash 不同
        type1_token_hashes = {
            g["token_hash"] for g in groups if g["clone_type"] == CLONE_TYPE_1
        }
        for th, group_syms in by_token_hash.items():
            if len(group_syms) < 2:
                continue
            # 跳过已被 Type-1 覆盖的 token_hash（避免重复）
            if th in type1_token_hashes:
                # 仍然报告 Type-2：同 token_hash 但不同 content_hash 的子组
                # 把同 content_hash 的符号聚合，跨 content_hash 的视为 Type-2
                by_ch: Dict[str, List[Dict[str, Any]]] = {}
                for s in group_syms:
                    by_ch.setdefault(s["symbol_hash"], []).append(s)
                if len(by_ch) < 2:
                    continue
                # Type-2 组：所有成员（representative 取第一个）
                groups.append({
                    "clone_type": CLONE_TYPE_2,
                    "token_hash": th,
                    "similarity": 1.0,
                    "members": [s["id"] for s in group_syms],
                })
            else:
                groups.append({
                    "clone_type": CLONE_TYPE_2,
                    "token_hash": th,
                    "similarity": 1.0,
                    "members": [s["id"] for s in group_syms],
                })

        # Type-3：相似度 >= 阈值但 < 1.0
        # 按 token_hash 去重，每组取第一个符号作为代表参与 LSH
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
            lsh_buckets: Dict[str, List[int]] = defaultdict(list)
            for idx in lsh_rep_indices:
                m = sym_meta[idx]
                for bucket_key in _lsh_buckets(m["minhash_sig"]):
                    lsh_buckets[bucket_key].append(idx)
            # Phase 7.1：大桶保护 — 跳过过大的桶
            # 大桶（如 > 200 个符号）通常意味着常见模式（return/break）导致
            # 大量不相关符号落入同一桶，展开 O(n²) 候选对既慢又无意义。
            # 跳过这些桶可显著减少候选对数量，不影响精确率（精确 Jaccard
            # 验证会过滤掉假阳性，跳过桶只是降低召回率，但这些桶中的真阳性
            # 比例极低）。
            for indices in lsh_buckets.values():
                if len(indices) < 2:
                    continue
                if len(indices) > _MAX_BUCKET_SIZE:
                    continue
                for i in range(len(indices)):
                    for j in range(i + 1, len(indices)):
                        a, b = indices[i], indices[j]
                        pair = (a, b) if a < b else (b, a)
                        candidate_pairs.add(pair)

        # 跳过已被 Type-1/2 覆盖的代表对（同 token_hash）
        covered_token_hashes = {g["token_hash"] for g in groups}

        # Type-3 组：相似度 >= 阈值
        # 把候选对聚合为 group：相同 (token_hash_a, token_hash_b, similarity) 的归为一组
        type3_clusters: Dict[Tuple[str, str, float], List[int]] = defaultdict(list)
        for a_idx, b_idx in candidate_pairs:
            a, b = sym_meta[a_idx], sym_meta[b_idx]
            # 跳过同 token_hash（已被 Type-1/2 覆盖）
            if a["token_hash"] == b["token_hash"]:
                continue
            sim = _jaccard_similarity(a["token_set"], b["token_set"])
            if sim >= similarity_threshold and sim < 1.0:
                # 用相似度量化作为 cluster key
                sim_bucket = round(sim, 2)
                key = (a["token_hash"], b["token_hash"], sim_bucket)
                type3_clusters[key].extend([a["id"], b["id"]])

        for key, member_ids in type3_clusters.items():
            # 去重 members
            unique_members = list(dict.fromkeys(member_ids))
            if len(unique_members) < 2:
                continue
            th_a, th_b, sim_val = key
            groups.append({
                "clone_type": CLONE_TYPE_3,
                "token_hash": f"{th_a}|{th_b}",
                "similarity": sim_val,
                "members": unique_members,
            })

        stats = {
            "scanned_symbols": scanned,
            "skipped_symbols": skipped,
            "total_groups": len(groups),
            "type1_groups": sum(1 for g in groups if g["clone_type"] == CLONE_TYPE_1),
            "type2_groups": sum(1 for g in groups if g["clone_type"] == CLONE_TYPE_2),
            "type3_groups": sum(1 for g in groups if g["clone_type"] == CLONE_TYPE_3),
        }
        return groups, stats

    def detect_clones_to_groups(
        self,
        file_filter: str = "",
        min_lines: int = 5,
        similarity_threshold: float = 0.8,
        progress_callback=None,
    ) -> Dict[str, Any]:
        """检测重复代码并写入 clone_groups（不展开为 pairs）

        适合后台 job 异步执行，避免 20 万符号产生 N×N pair 爆炸。

        参数：
            progress_callback: 可选，签名为 (progress: float, message: str) -> None
                供 job_executor 上报进度

        返回：统计字典
        """
        if progress_callback:
            progress_callback(0.1, "loading symbols")

        groups, stats = self._detect_clone_groups_core(
            file_filter=file_filter,
            min_lines=min_lines,
            similarity_threshold=similarity_threshold,
        )

        if progress_callback:
            progress_callback(0.6, f"detected {len(groups)} groups")

        # 写入 clone_groups
        ws_id = self._get_active_workspace_id()

        # 清空旧的 groups（同 workspace + 同 file_filter 范围）
        # 简化：直接清空所有 groups，重新写入
        from .db_clone_groups import store_clone_groups, clear_clone_groups
        clear_clone_groups(self.conn, ws_id)

        if progress_callback:
            progress_callback(0.8, "storing groups")

        stored = store_clone_groups(self.conn, ws_id, groups)

        if progress_callback:
            progress_callback(1.0, f"stored {stored} groups")

        stats["stored_groups"] = stored
        stats["similarity_threshold"] = similarity_threshold
        stats["min_lines"] = min_lines
        return stats

    def _detect_clone_pairs_via_rust(
        self,
        symbols: List[Dict[str, Any]],
        similarity_threshold: float,
    ) -> Optional[Tuple[List[Dict[str, Any]], Dict[str, int]]]:
        """把 Rust 分组结果转换为现有 ``clone_pairs`` 契约。

        ``cw clone detect`` 的公开结果是 pair 表，而 Rust 核心 API 为了
        大规模场景返回 clone groups。Type-1/2 组需要展开为两两关系；Type-3
        组由唯一的 token-hash 代表对组成，正常情况下恰好包含两个成员。
        若 Rust 返回无法保持 pair 语义的 Type-3 超大组，则返回 None，交给
        原 Python 路径处理，避免静默扩大结果集。
        """
        rust_result = self._detect_clone_groups_via_rust(
            symbols, similarity_threshold
        )
        if rust_result is None:
            return None
        groups, stats = rust_result
        by_id = {int(s["id"]): s for s in symbols}
        pairs: List[Dict[str, Any]] = []
        seen: Set[Tuple[int, int, int]] = set()

        for group in groups:
            clone_type = int(group["clone_type"])
            members = [int(member) for member in group.get("members", [])]
            if clone_type == CLONE_TYPE_3 and len(members) != 2:
                return None
            for left in range(len(members)):
                for right in range(left + 1, len(members)):
                    a_id, b_id = members[left], members[right]
                    a, b = by_id.get(a_id), by_id.get(b_id)
                    if a is None or b is None:
                        return None
                    if clone_type == CLONE_TYPE_2 and a["symbol_hash"] == b["symbol_hash"]:
                        continue
                    key = (min(a_id, b_id), max(a_id, b_id), clone_type)
                    if key in seen:
                        continue
                    seen.add(key)
                    pairs.append({
                        "symbol_a_id": a_id,
                        "symbol_b_id": b_id,
                        "clone_type": clone_type,
                        "similarity": float(group["similarity"]),
                        "token_hash": str(group["token_hash"]),
                        "lines_a": int(a["end_line"] - a["start_line"] + 1),
                        "lines_b": int(b["end_line"] - b["start_line"] + 1),
                    })

        type3_count = sum(1 for p in pairs if p["clone_type"] == CLONE_TYPE_3)
        stats = dict(stats)
        stats["scanned_symbols"] = len(symbols)
        stats["skipped_symbols"] = sum(1 for s in symbols if not (s.get("content") or ""))
        stats["type3_pairs"] = type3_count
        return pairs, stats

    def _persist_clone_pairs_result(
        self,
        ws_id: int,
        pairs: List[Dict[str, Any]],
        scanned: int,
        skipped: int,
        type3_count: int,
        now: float,
        similarity_threshold: float,
        min_lines: int,
    ) -> Dict[str, Any]:
        """持久化 Rust/ Python 共同使用的 pair 结果。"""
        upsert_sql = """
            INSERT INTO clone_pairs
                (workspace_id, symbol_a_id, symbol_b_id, clone_type,
                 similarity, token_hash, lines_a, lines_b, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, symbol_a_id, symbol_b_id, clone_type)
            DO UPDATE SET
                similarity = excluded.similarity,
                token_hash = excluded.token_hash,
                lines_a = excluded.lines_a,
                lines_b = excluded.lines_b,
                detected_at = excluded.detected_at
        """
        batch_data = []
        for p in pairs:
            batch_data.append((
                ws_id, p["symbol_a_id"], p["symbol_b_id"], p["clone_type"],
                p["similarity"], p["token_hash"], p["lines_a"], p["lines_b"], now,
            ))
            if len(batch_data) >= 50000:
                self.conn.executemany(upsert_sql, batch_data)
                batch_data.clear()
        if batch_data:
            self.conn.executemany(upsert_sql, batch_data)
        self.conn.commit()
        type1_count = sum(1 for p in pairs if p["clone_type"] == CLONE_TYPE_1)
        type2_count = sum(1 for p in pairs if p["clone_type"] == CLONE_TYPE_2)
        return {
            "total_pairs": len(pairs),
            "type1_pairs": type1_count,
            "type2_pairs": type2_count,
            "type3_pairs": type3_count,
            "scanned_symbols": scanned,
            "skipped_symbols": skipped,
            "similarity_threshold": similarity_threshold,
            "min_lines": min_lines,
        }

    def detect_clones(
        self,
        file_filter: str = "",
        min_lines: int = 5,
        similarity_threshold: float = 0.8,
    ) -> Dict[str, Any]:
        """检测重复代码（Type-1/2/3）

        Args:
            file_filter: 文件路径前缀过滤（如 "src/core/"），空字符串扫描所有
            min_lines: 最小符号行数，低于此值的符号跳过（避免噪声）
            similarity_threshold: Type-3 相似度阈值 [0, 1]

        Returns:
            统计字典，包含 type1_pairs / type2_pairs / type3_pairs /
            total_pairs / scanned_symbols / skipped_symbols
        """
        ws_id = self._get_active_workspace_id()
        normalized_filter = file_filter.replace("\\", "/").strip()
        now = time.time()

        # 清理旧的检测结果（同 workspace + 同 file_filter 范围）
        # 注意：为支持增量更新，调用方可在调用前手动清理
        # 这里不强制清理，依赖 UNIQUE 索引做 UPSERT

        # 加载候选符号（已去重，因为 symbols.symbol_hash 关联 symbol_contents）
        # P2 优化：用 IN 子查询让优化器走 idx_symbols_kind_file covering index。
        #   旧 SQL JOIN file_instances 触发 SCAN s 全表扫描（100K=13.79ms）；
        #   IN 子查询先扫 file_instances（500 行）走 idx_file_instances_relpath，
        #   再用 BLOOM FILTER + idx_symbols_kind_file 精确定位（100K=0.46ms，30x 加速）。
        filter_subclause = ""
        sql_params: List[Any] = [ws_id]  # IN 子查询的第一个参数（workspace_id）
        if normalized_filter:
            filter_subclause = "AND rel_path LIKE ?"
            sql_params.append(normalized_filter + "%")
        sql_params.append(min_lines)  # 外层 symbols 的 min_lines

        cur = self.conn.execute(
            f"""
            SELECT s.id, s.symbol_hash, s.name, s.kind, s.start_line, s.end_line,
                   s.qualified_name, fi.rel_path as file_path,
                   sc.content, sc.signature
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            WHERE s.file_instance_id IN (
                SELECT id FROM file_instances
                WHERE workspace_id = ? AND status != 'archived' {filter_subclause}
            )
              AND s.kind IN ('fn', 'function', 'method', 'test_fn')
              AND (s.end_line - s.start_line + 1) >= ?
            ORDER BY s.id
            """,
            sql_params,
        )

        symbols = [dict(row) for row in cur]
        scanned = len(symbols)
        skipped = 0

        # 默认 clone detect 也走 Rust MinHash/LSH；rollback flag 或无法保持
        # clone_pairs 精确语义时回退下面的 Python 实现。
        if not self.is_feature_rolled_back("rust_clone_detection"):
            rust_result = self._detect_clone_pairs_via_rust(
                symbols, similarity_threshold
            )
            if rust_result is not None:
                rust_pairs, rust_stats = rust_result
                return self._persist_clone_pairs_result(
                    ws_id,
                    rust_pairs,
                    rust_stats["scanned_symbols"],
                    rust_stats["skipped_symbols"],
                    rust_stats["type3_pairs"],
                    now,
                    similarity_threshold,
                    min_lines,
                )

        # 预计算每个符号的 token_hash、token 集合和 MinHash 签名
        # P1 修复：按 token_hash 缓存 MinHash 签名，避免重复计算。
        #   同一 token_hash 的符号 token 集合完全相同，MinHash 签名也相同。
        #   42K 符号中可能只有几 K 个不同 token_hash，缓存可减少 90%+ 的 MinHash 计算。
        # P1 修复：_normalize_token_sequence 只调用一次，复用给 token_hash 和 token_set。
        #   旧实现 _token_hash 内部调用一次，token_set 提取又调用一次，重复正则开销。
        sym_meta: List[Dict[str, Any]] = []
        minhash_cache: Dict[str, tuple] = {}  # token_hash -> MinHash 签名
        for s in symbols:
            content = s.get("content") or ""
            if not content:
                skipped += 1
                continue
            lines = s["end_line"] - s["start_line"] + 1
            # P1 修复：归一化只做一次，复用给 token_hash 和 token_set
            normalized = _normalize_token_sequence(content)
            th = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
            # P1 修复：用 3-gram 集合替代 token set，保留顺序信息提高区分度。
            #   token set Jaccard 对结构相同但逻辑不同的函数误报率高
            #   （如 foo(){return x+1} 和 bar(){return y-2} 的 token set Jaccard=0.78）。
            #   3-gram Jaccard 同样场景 = 0.50，正确低于 0.8 阈值。
            tokens = normalized.split()
            if len(tokens) >= 3:
                token_set = set(zip(tokens, tokens[1:], tokens[2:]))
            else:
                token_set = set(tokens)  # 太短的用 1-gram
            # P1 修复：MinHash 签名按 token_hash 缓存
            if th in minhash_cache:
                minhash_sig = minhash_cache[th]
            else:
                minhash_sig = _minhash_signature(token_set)
                minhash_cache[th] = minhash_sig
            sym_meta.append({
                "id": s["id"],
                "symbol_hash": s["symbol_hash"],
                "name": s["name"],
                "content": content,
                "lines": lines,
                "token_hash": th,
                "token_set": token_set,
                "minhash_sig": minhash_sig,
                "qualified_name": s["qualified_name"],
                "file_path": s["file_path"],
            })

        # 按 token_hash 分组（Type-1 + Type-2 候选）
        by_token_hash: Dict[str, List[Dict[str, Any]]] = {}
        by_content_hash: Dict[str, List[Dict[str, Any]]] = {}
        for m in sym_meta:
            by_token_hash.setdefault(m["token_hash"], []).append(m)
            by_content_hash.setdefault(m["symbol_hash"], []).append(m)

        pairs: List[Dict[str, Any]] = []

        # Type-1：content_hash 相同（符号内容完全一致）
        for ch, group in by_content_hash.items():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    pairs.append({
                        "symbol_a_id": a["id"],
                        "symbol_b_id": b["id"],
                        "clone_type": CLONE_TYPE_1,
                        "similarity": 1.0,
                        "token_hash": a["token_hash"],
                        "lines_a": a["lines"],
                        "lines_b": b["lines"],
                    })

        # Type-2：token_hash 相同但 content_hash 不同（重命名克隆）
        existing_pairs = {(p["symbol_a_id"], p["symbol_b_id"]) for p in pairs}
        for th, group in by_token_hash.items():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    # 跳过已识别为 Type-1 的对
                    if (a["id"], b["id"]) in existing_pairs:
                        continue
                    # 必须是不同符号（symbol_hash 不同）
                    if a["symbol_hash"] == b["symbol_hash"]:
                        continue
                    pairs.append({
                        "symbol_a_id": a["id"],
                        "symbol_b_id": b["id"],
                        "clone_type": CLONE_TYPE_2,
                        "similarity": 1.0,
                        "token_hash": th,
                        "lines_a": a["lines"],
                        "lines_b": b["lines"],
                    })
                    existing_pairs.add((a["id"], b["id"]))

        # Type-3：相似度 >= 阈值但 < 1.0（基于 token 集合 Jaccard）
        # P1 优化：MinHash + LSH 替代 name 前缀分组
        # P1 修复：按 token_hash 去重后参与 LSH。
        #   旧实现把所有符号放入 LSH，但同一 token_hash 组的符号 token 集合完全相同，
        #   MinHash 签名也相同，全部落入同一桶导致桶爆炸（22K 符号中 17K 落入同一桶，
        #   候选对 >10 亿）。修复后每组只取一个代表参与 LSH，候选对从 O(n²) 降到 O(u²)
        #   （u = 不同 token_hash 数，远小于 n）。
        # LSH 把签名分成 8 带，每带 16 哈希，落入相同桶的为候选对
        # 候选对再用精确 Jaccard 验证，保证精确率 100%
        # 阈值 t ≈ (1/8)^(1/16) ≈ 0.88，配合 3-gram token 集合使用
        # P1 修复：小数据集（< 500 唯一 token_hash）直接暴力 O(u²) 比较，
        #   跳过 LSH。LSH 阈值 0.88 高于用户设定的低阈值（如 0.3）时会漏掉对，
        #   小数据集暴力比较既快又正确。

        # 按 token_hash 去重，每组取第一个符号作为代表
        token_hash_to_rep_idx: Dict[str, int] = {}
        for idx, m in enumerate(sym_meta):
            th = m["token_hash"]
            if th not in token_hash_to_rep_idx:
                token_hash_to_rep_idx[th] = idx

        lsh_rep_indices = list(token_hash_to_rep_idx.values())
        num_reps = len(lsh_rep_indices)

        # P1 修复：小数据集直接暴力比较，大数据集用 LSH
        BRUTEFORCE_THRESHOLD = 500
        candidate_pairs: Set = set()

        if num_reps < BRUTEFORCE_THRESHOLD:
            # 暴力 O(u²) 比较——小数据集无需 LSH
            for i in range(num_reps):
                for j in range(i + 1, num_reps):
                    candidate_pairs.add((lsh_rep_indices[i], lsh_rep_indices[j]))
        else:
            # LSH 分桶（只处理代表符号）
            lsh_buckets: Dict[str, List[int]] = defaultdict(list)
            for idx in lsh_rep_indices:
                m = sym_meta[idx]
                for bucket_key in _lsh_buckets(m["minhash_sig"]):
                    lsh_buckets[bucket_key].append(idx)

            # Phase 7.1：大桶保护 — 收集候选对（去重），跳过过大的桶
            for indices in lsh_buckets.values():
                if len(indices) < 2:
                    continue
                if len(indices) > _MAX_BUCKET_SIZE:
                    continue
                for i in range(len(indices)):
                    for j in range(i + 1, len(indices)):
                        a, b = indices[i], indices[j]
                        pair = (a, b) if a < b else (b, a)
                        candidate_pairs.add(pair)

        type3_count = 0
        for a_idx, b_idx in candidate_pairs:
            a, b = sym_meta[a_idx], sym_meta[b_idx]
            if (a["id"], b["id"]) in existing_pairs:
                continue
            sim = _jaccard_similarity(a["token_set"], b["token_set"])
            if sim >= similarity_threshold and sim < 1.0:
                # P1 修复：只报告代表对，不做组间扩展。
                #   旧实现把代表对扩展到组内所有符号两两组合，产生 14.7M 对（22K 符号），
                #   持久化耗时巨大且对用户无意义。代表对已足够标识相似组，
                #   用户可通过 Type-2 查询同组其他符号。
                pairs.append({
                    "symbol_a_id": a["id"],
                    "symbol_b_id": b["id"],
                    "clone_type": CLONE_TYPE_3,
                    "similarity": round(sim, 3),
                    "token_hash": a["token_hash"],
                    "lines_a": a["lines"],
                    "lines_b": b["lines"],
                })
                existing_pairs.add((a["id"], b["id"]))
                type3_count += 1

        # 持久化到 clone_pairs（UPSERT）
        # P1 修复：改用 executemany 批量插入，替代逐条 execute。
        #   4.7M 对逐条 INSERT 耗时 121s（每条 ~26μs），executemany 批量插入
        #   可降到 ~10s（10x+ 加速）。
        upsert_sql = """
            INSERT INTO clone_pairs
                (workspace_id, symbol_a_id, symbol_b_id, clone_type,
                 similarity, token_hash, lines_a, lines_b, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, symbol_a_id, symbol_b_id, clone_type)
            DO UPDATE SET
                similarity = excluded.similarity,
                token_hash = excluded.token_hash,
                lines_a = excluded.lines_a,
                lines_b = excluded.lines_b,
                detected_at = excluded.detected_at
        """
        # 分批 executemany（每批 50000 条，避免内存爆炸）
        BATCH_SIZE = 50000
        batch_data = []
        for p in pairs:
            batch_data.append((
                ws_id, p["symbol_a_id"], p["symbol_b_id"], p["clone_type"],
                p["similarity"], p["token_hash"], p["lines_a"], p["lines_b"], now,
            ))
            if len(batch_data) >= BATCH_SIZE:
                self.conn.executemany(upsert_sql, batch_data)
                batch_data.clear()
        if batch_data:
            self.conn.executemany(upsert_sql, batch_data)
        self.conn.commit()

        type1_count = sum(1 for p in pairs if p["clone_type"] == CLONE_TYPE_1)
        type2_count = sum(1 for p in pairs if p["clone_type"] == CLONE_TYPE_2)

        return {
            "total_pairs": len(pairs),
            "type1_pairs": type1_count,
            "type2_pairs": type2_count,
            "type3_pairs": type3_count,
            "scanned_symbols": scanned,
            "skipped_symbols": skipped,
            "similarity_threshold": similarity_threshold,
            "min_lines": min_lines,
        }

    def list_clones(
        self,
        clone_type: int = 0,
        min_similarity: float = 0.0,
        limit: int = 100,
        symbol_id: int = 0,
    ) -> List[Dict]:
        """列出检测到的克隆对

        Args:
            clone_type: 克隆类型过滤（0=全部，1/2/3 对应 Type-1/2/3）
            min_similarity: 最低相似度过滤
            limit: 返回上限
            symbol_id: 只返回涉及此符号的克隆对（0=不过滤）

        Returns:
            克隆对列表，按相似度降序，包含符号和文件信息
        """
        ws_id = self._get_active_workspace_id()
        params: List[Any] = [ws_id]
        where = ["cp.workspace_id = ?"]

        if clone_type in (CLONE_TYPE_1, CLONE_TYPE_2, CLONE_TYPE_3):
            where.append("cp.clone_type = ?")
            params.append(clone_type)
        if min_similarity > 0:
            where.append("cp.similarity >= ?")
            params.append(min_similarity)
        if symbol_id > 0:
            where.append("(cp.symbol_a_id = ? OR cp.symbol_b_id = ?)")
            params.extend([symbol_id, symbol_id])
        params.append(limit)

        cur = self.conn.execute(
            f"""
            SELECT cp.clone_type, cp.similarity, cp.token_hash,
                   cp.lines_a, cp.lines_b, cp.detected_at,
                   sa.name as symbol_a_name, sa.qualified_name as symbol_a_qualified,
                   sa.start_line as symbol_a_line,
                   sb.name as symbol_b_name, sb.qualified_name as symbol_b_qualified,
                   sb.start_line as symbol_b_line,
                   fa.rel_path as file_a, fb.rel_path as file_b
            FROM clone_pairs cp
            JOIN symbols sa ON cp.symbol_a_id = sa.id
            JOIN symbols sb ON cp.symbol_b_id = sb.id
            JOIN file_instances fa ON sa.file_instance_id = fa.id
            JOIN file_instances fb ON sb.file_instance_id = fb.id
            WHERE {" AND ".join(where)}
            ORDER BY cp.similarity DESC, cp.detected_at DESC
            LIMIT ?
            """,
            params,
        )
        return [dict(row) for row in cur]

    def get_clone_stats(self) -> Dict[str, Any]:
        """获取克隆检测统计信息

        Returns:
            统计字典，包含 total / type1 / type2 / type3 / affected_files /
            affected_symbols
        """
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN clone_type = 1 THEN 1 ELSE 0 END) as type1,
                SUM(CASE WHEN clone_type = 2 THEN 1 ELSE 0 END) as type2,
                SUM(CASE WHEN clone_type = 3 THEN 1 ELSE 0 END) as type3
            FROM clone_pairs WHERE workspace_id = ?
            """,
            (ws_id,),
        )
        row = cur.fetchone()
        if not row:
            return {"total": 0, "type1": 0, "type2": 0, "type3": 0,
                    "affected_files": 0, "affected_symbols": 0}

        cur = self.conn.execute(
            """
            SELECT COUNT(DISTINCT fi.id) as files, COUNT(DISTINCT s.id) as syms
            FROM clone_pairs cp
            JOIN symbols s ON (cp.symbol_a_id = s.id OR cp.symbol_b_id = s.id)
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE cp.workspace_id = ?
            """,
            (ws_id,),
        )
        aff = cur.fetchone()
        return {
            "total": row["total"] or 0,
            "type1": row["type1"] or 0,
            "type2": row["type2"] or 0,
            "type3": row["type3"] or 0,
            "affected_files": aff["files"] if aff else 0,
            "affected_symbols": aff["syms"] if aff else 0,
        }

    def clear_clones(self) -> int:
        """清空当前 workspace 的所有克隆检测结果

        Returns:
            被删除的记录数
        """
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            "DELETE FROM clone_pairs WHERE workspace_id = ?",
            (ws_id,),
        )
        self.conn.commit()
        return cur.rowcount
