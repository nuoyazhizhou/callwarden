#!/usr/bin/env python3
"""P1 分阶段计时：定位 clone detect 瓶颈（匹配新代码逻辑）。

用法：python -u tests/_bench_clone_profile.py
"""
import os
import sys
import time
import hashlib
from collections import defaultdict

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def main():
    print("P1 Clone Detect 分阶段计时（新逻辑）", flush=True)

    from callwarden.db.db import CodeGraphDB
    from callwarden.db.db_clone_detection import (
        _normalize_token_sequence, _minhash_signature,
        _lsh_buckets, _jaccard_similarity,
    )

    db_path = os.path.join(_project_root, "tests", "_perf_db", "android", "callwarden.db")
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}", flush=True)
        return

    android_root = r"C:\git_work\callwarden\testcode\android"
    db = CodeGraphDB(db_path=db_path, workspace_root=android_root)
    db.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    ws_id = db._get_active_workspace_id()
    print(f"workspace_id={ws_id}", flush=True)

    # === 阶段 1：SQL 加载符号 ===
    t0 = time.time()
    cur = db.conn.execute(
        """
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
        ORDER BY s.id
        """,
        (ws_id, 5),
    )
    symbols = [dict(row) for row in cur]
    t1 = time.time()
    print(f"阶段1 SQL加载: {t1-t0:.3f}s ({len(symbols)} 符号)", flush=True)

    # === 阶段 2：token 归一化 + token_hash + token_set + MinHash（带缓存）===
    t0 = time.time()
    sym_meta = []
    minhash_cache = {}  # token_hash -> MinHash 签名
    total_tokens = 0
    for s in symbols:
        content = s.get("content") or ""
        if not content:
            continue
        lines = s["end_line"] - s["start_line"] + 1
        # 归一化只做一次
        normalized = _normalize_token_sequence(content)
        th = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        # P1 修复：用 3-gram 集合替代 token set
        tokens = normalized.split()
        if len(tokens) >= 3:
            token_set = set(zip(tokens, tokens[1:], tokens[2:]))
        else:
            token_set = set(tokens)
        total_tokens += len(token_set)
        # MinHash 按 token_hash 缓存
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
    t1 = time.time()
    unique_th = len(minhash_cache)
    print(f"阶段2 token+minhash: {t1-t0:.3f}s ({len(sym_meta)} 符号, {unique_th} 唯一token_hash)", flush=True)
    print(f"  avg {total_tokens/max(len(sym_meta),1):.1f} tokens/sym, MinHash计算 {unique_th} 次（缓存命中率 {(len(sym_meta)-unique_th)/max(len(sym_meta),1)*100:.1f}%）", flush=True)

    # === 阶段 3：Type-1 + Type-2 分组 ===
    t0 = time.time()
    by_token_hash = {}
    by_content_hash = {}
    for m in sym_meta:
        by_token_hash.setdefault(m["token_hash"], []).append(m)
        by_content_hash.setdefault(m["symbol_hash"], []).append(m)

    pairs = []
    for ch, group in by_content_hash.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                pairs.append((group[i]["id"], group[j]["id"], 1))

    existing_pairs = {(p[0], p[1]) for p in pairs}
    type2_count = 0
    for th, group in by_token_hash.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if (a["id"], b["id"]) in existing_pairs:
                    continue
                if a["symbol_hash"] == b["symbol_hash"]:
                    continue
                pairs.append((a["id"], b["id"], 2))
                existing_pairs.add((a["id"], b["id"]))
                type2_count += 1
    t1 = time.time()
    print(f"阶段3 Type1+2: {t1-t0:.3f}s (Type-1+2: {len(pairs)} 对)", flush=True)

    # === 阶段 4：按 token_hash 去重 + LSH 分桶 ===
    t0 = time.time()
    token_hash_to_rep_idx = {}
    for idx, m in enumerate(sym_meta):
        th = m["token_hash"]
        if th not in token_hash_to_rep_idx:
            token_hash_to_rep_idx[th] = idx

    lsh_rep_indices = list(token_hash_to_rep_idx.values())
    print(f"  LSH 输入: {len(lsh_rep_indices)} 代表 (从 {len(sym_meta)} 符号去重)", flush=True)

    lsh_buckets = defaultdict(list)
    for idx in lsh_rep_indices:
        m = sym_meta[idx]
        for bucket_key in _lsh_buckets(m["minhash_sig"]):
            lsh_buckets[bucket_key].append(idx)

    bucket_sizes = sorted([len(v) for v in lsh_buckets.values() if len(v) >= 2], reverse=True)
    print(f"  LSH 桶总数: {len(lsh_buckets)}", flush=True)
    print(f"  多元素桶: {len(bucket_sizes)}", flush=True)
    if bucket_sizes:
        print(f"  最大桶: {bucket_sizes[0]}, top10: {bucket_sizes[:10]}", flush=True)
        big_buckets = [s for s in bucket_sizes if s > 100]
        print(f"  >100 元素的桶: {len(big_buckets)} 个", flush=True)
        total_candidate_upper = sum(s*(s-1)//2 for s in bucket_sizes)
        print(f"  候选对上限（所有桶 C(n,2) 之和）: {total_candidate_upper}", flush=True)
    t1 = time.time()
    print(f"阶段4 LSH分桶: {t1-t0:.3f}s", flush=True)

    # === 阶段 5：候选对收集 ===
    t0 = time.time()
    candidate_pairs = set()
    for indices in lsh_buckets.values():
        if len(indices) < 2:
            continue
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                a, b = indices[i], indices[j]
                pair = (a, b) if a < b else (b, a)
                candidate_pairs.add(pair)
    t1 = time.time()
    print(f"阶段5 候选对收集: {t1-t0:.3f}s ({len(candidate_pairs)} 候选对)", flush=True)

    # === 阶段 6：Jaccard 验证（无组扩展）===
    t0 = time.time()
    type3_count = 0
    for a_idx, b_idx in candidate_pairs:
        a, b = sym_meta[a_idx], sym_meta[b_idx]
        if (a["id"], b["id"]) in existing_pairs:
            continue
        sim = _jaccard_similarity(a["token_set"], b["token_set"])
        if sim >= 0.8 and sim < 1.0:
            type3_count += 1
    t1 = time.time()
    print(f"阶段6 Jaccard验证: {t1-t0:.3f}s ({type3_count} Type-3 对)", flush=True)

    print(f"\n总 Type-1+2: {len(pairs)}, Type-3: {type3_count}", flush=True)
    db.close()


if __name__ == "__main__":
    main()
