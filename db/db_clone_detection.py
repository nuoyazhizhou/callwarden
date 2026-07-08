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


# ==================== MinHash + LSH（P1 优化）====================


def _minhash_signature(token_set: set, num_perm: int = 128) -> tuple:
    """计算 token 集合的 MinHash 签名

    MinHash 通过对集合元素施加多个独立哈希函数，取最小值作为签名。
    两个集合 MinHash 签名在相同位置的概率 ≈ Jaccard 相似度。

    Args:
        token_set: token 集合
        num_perm: 哈希函数数量（签名长度），默认 128

    Returns:
        长度为 num_perm 的签名 tuple，每个元素是 32 位无符号整数。
        空集合返回全 0xFFFFFFFF。

    Note:
        签名依赖 Python hash()，受 PYTHONHASHSEED 影响，但同一进程内一致。
        detect_clones 是单次调用，满足此约束。
    """
    if not token_set:
        return tuple([0xFFFFFFFF] * num_perm)

    signature = [0xFFFFFFFF] * num_perm
    for token in token_set:
        base = hash(token)
        for i in range(num_perm):
            # 线性变换生成独立哈希：h_i = (base * (i+1) + offset * i) mod 2^32
            h = (base * (i + 1) + 0x9E3779B9 * i) & 0xFFFFFFFF
            if h < signature[i]:
                signature[i] = h
    return tuple(signature)


def _lsh_buckets(signature: tuple, num_bands: int = 32, rows_per_band: int = 4) -> list:
    """将 MinHash 签名分桶（Locality-Sensitive Hashing）

    LSH 把签名分成 num_bands 个带，每带 rows_per_band 个哈希值。
    两个签名在任意一个带上完全相同，就是候选相似对。

    阈值公式：t ≈ (1/num_bands)^(1/rows_per_band)
    默认参数：b=32, r=4 → t ≈ 0.42，低于 0.8 阈值，保证召回率。

    Args:
        signature: MinHash 签名
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
    """

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

        # 预计算每个符号的 token_hash、token 集合和 MinHash 签名
        sym_meta: List[Dict[str, Any]] = []
        for s in symbols:
            content = s.get("content") or ""
            if not content:
                skipped += 1
                continue
            lines = s["end_line"] - s["start_line"] + 1
            th = _token_hash(content)
            # Type-3 相似度用 token set（去重，简化 Jaccard 计算）
            token_set = set(_normalize_token_sequence(content).split())
            # P1 优化：MinHash 签名用于 LSH 候选筛选
            minhash_sig = _minhash_signature(token_set)
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
        # LSH 把签名分成 32 带，每带 4 哈希，落入相同桶的为候选对
        # 候选对再用精确 Jaccard 验证，保证精确率 100%
        # 阈值 t ≈ (1/32)^(1/4) ≈ 0.42，低于 0.8，保证召回率 ≥ 95%
        lsh_buckets: Dict[str, List[int]] = defaultdict(list)
        for idx, m in enumerate(sym_meta):
            for bucket_key in _lsh_buckets(m["minhash_sig"]):
                lsh_buckets[bucket_key].append(idx)

        # 收集候选对（去重）
        candidate_pairs: Set = set()
        for indices in lsh_buckets.values():
            if len(indices) < 2:
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
        for p in pairs:
            self.conn.execute(
                """
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
                """,
                (ws_id, p["symbol_a_id"], p["symbol_b_id"], p["clone_type"],
                 p["similarity"], p["token_hash"], p["lines_a"], p["lines_b"], now),
            )
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
    ) -> List[Dict]:
        """列出检测到的克隆对

        Args:
            clone_type: 克隆类型过滤（0=全部，1/2/3 对应 Type-1/2/3）
            min_similarity: 最低相似度过滤
            limit: 返回上限

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
