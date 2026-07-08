# Call Warden 性能优化计划（P0-P5）

> 基于 [performance_report_phase2.md](performance_report_phase2.md) 识别的 5 个瓶颈
> 目标：支撑 firmware 完整 125 仓库 / 20 万符号 / 20GB 源码规模
> 日期：2026-07-08

## 1. 规模评估与架构决策

### 1.1 firmware 完整规模推算

| 指标 | 当前测试（2 仓库） | 完整规模（125 仓库） | 说明 |
|------|------------------|-------------------|------|
| 源码体积 | 9GB | 20GB | 排除 buildroot + 谷歌框架后约几百兆 |
| 文件数 | 14,293 | ~100,000+ | 排除第三方后 |
| 符号数 | 107,926 | ~200,000 | 用户估计 |
| 调用数 | 0（卡死） | ~1,000,000+ | 按 5:1 符号调用比推算 |
| DB 大小 | 275MB | ~800MB-1GB | 按 4MB/千符号推算 |

### 1.2 架构方向对比

| 方向 | 推荐度 | 适用场景 | 本项目评估 |
|------|--------|---------|-----------|
| **改进算法** | ⭐⭐⭐⭐⭐ | 算法瓶颈（O(n²) → O(n log n)） | **首选**。P0/P1 都是算法问题 |
| **Rust 重写** | ⭐⭐⭐ | 算法优化后仍不够快 | **备选**。已有 rust_ext 基础设施，算法优化后评估 |
| **分库** | ⭐⭐ | 单库过大（>10GB）或查询并发高 | **不推荐**。800MB SQLite 无压力，分库增加跨库调用解析复杂度 |

### 1.3 决策

- **SQLite 单库**：800MB-1GB 完全在 SQLite 舒适区（支持到 281TB）
- **算法优化优先**：P0/P1/P3/P5 都是算法层面，不涉及语言切换
- **Rust 重写保留**：算法优化后若 20 万符号 refresh 仍 >30 分钟，再考虑 Rust 重写调用解析核心循环
- **不分库**：workspace 隔离已够，跨库调用解析反而增加工程复杂度

## 2. 优化任务清单

### P0（致命）：调用关系解析 O(M×N) → O(M×K)

**位置**：[db/db_build.py:798-808](../db/db_build.py#L798-L808) 策略 2 后缀匹配 + [db/db_build.py:842-849](../db/db_build.py#L842-L849) 策略 4

**问题**：
```python
# 策略 2：对每个未解析调用遍历全部符号
suffix = f".{callee_module}.{callee_name}"
for qname in all_symbols_map:  # ← O(N) 遍历
    if qname.endswith(suffix):
```

**影响**：firmware 107K 符号卡死 30+ 分钟。20 万符号预计 6 小时+。

**优化方案**：构建后缀反向索引
```python
# 预处理：构建后缀 → [qualified_name, ...] 索引
suffix_index: Dict[str, List[str]] = defaultdict(list)
for qname in all_symbols_map:
    norm = qname.replace("::", ".")
    parts = norm.split(".")
    # 为每个可能的后缀建立索引
    for i in range(len(parts)):
        suffix = ".".join(parts[i:])
        suffix_index[suffix].append(qname)

# 查询：O(K)，K 为后缀匹配候选数（通常 ≤ 5）
candidates = suffix_index.get(f"{callee_module}.{callee_name}", [])
```

**预期收益**：O(M×N) → O(M×K)，K≈5，**100 倍+**。20 万符号 refresh 预计 <10 分钟。

**测试**：`tests/test_perf_p0_suffix_index.py`
- 验证后缀索引正确性
- 验证调用解析结果与原算法一致
- 性能对比（100K 符号场景）

---

### P3（高）：策略 5 逐条 DB 查询 → 批量加载内存

**位置**：[db/db_build.py:852-876](../db/db_build.py#L852-L876)

**问题**：
```python
# 对每个未解析调用，执行一次 DB 查询
cur = self.conn.execute(
    "SELECT ... FROM external_symbols WHERE qualified_name = ?",
    (test_qname,)
)
```

**影响**：M 个未解析调用 = M 次 DB 查询。firmware 第三方库多，查询频繁。

**优化方案**：批量加载 external_symbols 到内存 dict
```python
# 预处理：一次性加载全部 external_symbols
ext_by_qname: Dict[str, Dict] = {}
ext_by_name: Dict[str, List[Dict]] = defaultdict(list)
for row in self.conn.execute("SELECT * FROM external_symbols"):
    ext_by_qname[row["qualified_name"]] = dict(row)
    ext_by_name[row["symbol_name"]].append(dict(row))

# 查询：内存查找，O(1)
ext_sym = ext_by_qname.get(test_qname)
```

**预期收益**：M 次 DB 查询 → 1 次批量加载 + M 次内存查找，**5-10 倍**。

**测试**：`tests/test_perf_p3_batch_external.py`

---

### P5（低）：_build_depth 逐个 UPDATE → executemany

**位置**：[db/db_build.py:1656-1660](../db/db_build.py#L1656-L1660)

**问题**：
```python
for fn_id, depth in depth_cache.items():
    self.conn.execute(
        "UPDATE symbols SET depth = ? WHERE id = ?",
        (depth, fn_id),
    )
```

**影响**：10 万函数 = 10 万次 UPDATE，事务开销大。

**优化方案**：
```python
updates = [(depth, fn_id) for fn_id, depth in depth_cache.items()]
self.conn.executemany(
    "UPDATE symbols SET depth = ? WHERE id = ?",
    updates,
)
```

**预期收益**：10 万次 execute → 1 次 executemany，**2-3 倍**。

**测试**：`tests/test_perf_p5_executemany.py`

---

### P1（高）：克隆检测 O(n²) → MinHash+LSH

**位置**：[db/db_clone_detection.py:254-280](../db/db_clone_detection.py#L254-L280)

**问题**：
```python
for prefix, group in by_name_prefix.items():
    for i in range(len(group)):
        for j in range(i + 1, len(group)):  # ← O(n²)
            sim = _jaccard_similarity(a["token_set"], b["token_set"])
```

**影响**：android 22K 符号 → 42s, 1M 对。20 万符号预计 >1 小时。

**优化方案**：MinHash + LSH 索引
```python
# 1. 为每个符号计算 MinHash 签名（k=128 个哈希函数）
def minhash_signature(token_set: Set[str], k: int = 128) -> List[int]:
    sig = [float('inf')] * k
    for token in token_set:
        for i in range(k):
            h = hash((i, token)) & 0xFFFFFFFF
            if h < sig[i]:
                sig[i] = h
    return sig

# 2. LSH 分桶：把 MinHash 签名分成 b 个带，每带 r 个哈希
#    落入相同桶的候选对进入精确比较
# 3. 仅对候选对计算精确 Jaccard
```

**预期收益**：O(n²) → O(n × k)，k=128，**50 倍+**。20 万符号 clone detect 预计 <2 分钟。

**测试**：`tests/test_perf_p1_lsh.py`
- 验证召回率 ≥ 95%
- 验证精确率 = 100%（LSH 只做候选筛选，最终用精确 Jaccard）
- 性能对比

---

### P2（中）：search LIKE → FTS5 全文索引

**位置**：[db/db_query.py:499](../db/db_query.py#L499)

**问题**：
```python
AND (fsv.qualified_name LIKE ? OR sc.name LIKE ?)
# 前缀通配符 %query% 无法用 B-tree 索引
```

**影响**：当前规模可接受（admin 0.066s），千万级会恶化。

**优化方案**：SQLite FTS5 虚拟表
```sql
-- 1. 创建 FTS5 虚拟表
CREATE VIRTUAL TABLE symbols_fts USING fts5(
    qualified_name, name, content='symbols', content_rowid='id'
);

-- 2. 填充索引
INSERT INTO symbols_fts(qualified_name, name)
SELECT qualified_name, name FROM symbols;

-- 3. 查询
SELECT * FROM symbols_fts WHERE symbols_fts MATCH ?
```

**预期收益**：LIKE 全表 → FTS5 倒排索引，**10-50 倍**。

**测试**：`tests/test_perf_p2_fts5.py`

---

## 3. 实施顺序

| 阶段 | 任务 | 优先级 | 依赖 | 预期耗时 |
|------|------|--------|------|---------|
| 1 | P0 后缀反向索引 | 致命 | 无 | 中 |
| 2 | P3 批量加载 external_symbols | 高 | 无 | 小 |
| 3 | P5 executemany | 低 | 无 | 小 |
| 4 | P1 MinHash+LSH | 高 | 无 | 大 |
| 5 | P2 FTS5 | 中 | 无 | 中 |
| 6 | 回归测试 + 性能对比验证 | 高 | P0-P5 | 中 |

**顺序逻辑**：
1. P0 最致命（导致 firmware 无法完成），必须最先修复
2. P3/P5 简单且与 P0 在同一文件，顺手做
3. P1 复杂度高，单独实施
4. P2 当前性能可接受，最后做
5. 全部完成后回归测试，对比优化前后性能

## 4. 验证标准

### 4.1 功能验证

- 所有现有测试通过（`pytest tests/ -x`）
- 调用解析结果与优化前一致（P0）
- 克隆检测结果召回率 ≥ 95%、精确率 100%（P1）
- search 结果与优化前一致（P2）

### 4.2 性能验证

用 `_perf_test.py` 重新测试 4 个仓库，对比优化前后：

| 仓库 | 指标 | 优化前 | 目标 |
|------|------|--------|------|
| admin | refresh | 223.5s | <120s |
| android | refresh | 450s | <240s |
| firmware | refresh | >1800s（卡死） | <600s |
| android | clone detect | 42.3s | <5s |
| admin | clone detect | 2.86s | <0.5s |

### 4.3 大规模验证

修复 P0 后，用 firmware **不排除第三方库**重新测试，验证 20 万+ 符号场景。

## 5. 风险与回退

- **P0 风险**：后缀索引内存占用增加（estimated 2-3x symbol count）。20 万符号约 200MB 内存，可接受。
- **P1 风险**：MinHash 召回率可能低于 100%。通过 LSH 候选 + 精确 Jaccard 二次验证保证精确率。
- **P2 风险**：FTS5 需要额外维护索引表，增加 DB 大小。预计 +10-20%。
- **回退方案**：每个优化独立 commit，可单独 revert。

## 6. 后续规划（本次不实施）

- **Rust 重写**：若算法优化后 20 万符号 refresh 仍 >30 分钟，考虑用 Rust 重写 `_build_call_graph_multi_lang` 核心循环
- **并行化**：调用关系解析可按文件分片并行（当前解析阶段已并行，调用解析阶段未并行）
- **增量优化**：AST 缓存激活（T-1783441015098-d577）减少重复解析
