# Call Warden Phase 2 性能验证报告

> 任务: T-1783441015097-cdfe 千万级符号性能验证
> 日期: 2026-07-08
> 环境: Windows 10, Python 3.14.3, SQLite 3.50.4, 8 核 CPU

## 1. 测试数据

使用 4 个真实仓库，按规模从小到大测试：

| 仓库 | 语言 | 源码行数 | 文件数 | 说明 |
|------|------|---------|--------|------|
| admin | Java | 257K | 1758 | 云端管理后台（Maven 多模块） |
| android | Java/Kotlin | 898K | 5105 | Android App |
| ios_muzoplayer | ObjC/Swift/C | 1.58M | 3760 | iOS App（排除 third 后） |
| firmware | C/C++ | 34M | 14293 | 固件主仓库（排除第三方库后） |

## 2. 测试结果汇总

### 2.1 refresh-all 性能

| 仓库 | 文件数 | 符号数 | 调用数 | refresh(s) | DB(MB) |
|------|--------|--------|--------|-----------|--------|
| admin | 1758 | 20936 | 84669 | 223.5 | 83 |
| android | 5105 | 50776 | 190777 | 450.0 | 224 |
| ios_muzoplayer | 3760 | 2884 | 8845 | 35.6 | 22 |
| firmware | 14293 | 107926 | 0* | >1800** | 275 |

\* firmware 调用关系未写入（Step 4/5 超时）
\*\* firmware refresh 30 分钟未完成，强制停止

### 2.2 核心查询性能

| 仓库 | search(s) | get_callers(s) | get_callees(s) | topo(s) | clone_detect(s) | clone_pairs |
|------|-----------|----------------|----------------|---------|-----------------|-------------|
| admin | 0.066 | 0.000 | 0.000 | 0.000 | 2.856 | 90,057 |
| android | 0.203 | 0.000 | 0.000 | 0.076 | 42.263 | 1,032,083 |
| ios_muzoplayer | 0.005 | 0.000 | 0.000 | 0.003 | 0.286 | 3,273 |
| firmware | N/A | N/A | N/A | N/A | N/A | N/A |

### 2.3 关键发现

1. **admin（2万符号）**: refresh 3.7min，全部查询亚秒级，性能良好
2. **android（5万符号）**: refresh 7.5min，查询亚秒级，clone detect 42s（1M 对）
3. **ios_muzoplayer（3K符号）**: ObjC 解析器覆盖有限，符号数偏少
4. **firmware（10万符号）**: refresh 卡在 Step 4/5（调用关系解析），30 分钟未完成

## 3. 性能瓶颈分析

### P0（致命）: 调用关系解析 O(M×N)

**位置**: [db/db_build.py:798-808](../db/db_build.py#L798-L808) 策略 2 后缀匹配

**代码**:
```python
# 策略 2：通过 import 映射 module 后匹配
if not callee_qname and callee_module:
    suffix = f".{callee_module}.{callee_name}"
    for qname in all_symbols_map:  # ← O(N) 遍历全部符号
        if qname.endswith(suffix):
            ...
```

**同样问题**: [db/db_build.py:842-849](../db/db_build.py#L842-L849) 策略 4 同文件简名匹配也是 O(N) 遍历。

**影响**: firmware 14286 文件 / 107926 符号，假设每文件 20 个调用 = 285720 调用，50% 需要后缀匹配 = 142860 × 107926 ≈ **154 亿次操作**，导致 30 分钟无法完成。

**优化方向**:
- 构建后缀反向索引: `suffix_to_qualified = {".module.name": [qualified_name, ...]}`
- 或用 SQLite FTS5 对 qualified_name 建全文索引
- 预期效果: O(M×N) → O(M×K)，K 为后缀匹配候选数（通常 ≤ 5）

### P1（高）: 克隆检测 O(n²)

**位置**: [db/db_clone_detection.py:254-280](../db/db_clone_detection.py#L254-L280) Type-3 检测

**代码**:
```python
by_name_prefix: Dict[str, List] = {}
for m in sym_meta:
    prefix = m["name"][:3]
    by_name_prefix.setdefault(prefix, []).append(m)

for prefix, group in by_name_prefix.items():
    for i in range(len(group)):
        for j in range(i + 1, len(group)):  # ← O(n²) 双重循环
            sim = _jaccard_similarity(a["token_set"], b["token_set"])
```

**影响**: android 22849 符号 → 42.3s, 1,032,083 对。按此趋势，10 万符号将需要 **>15 分钟**。

**优化方向**:
- 使用 LSH（Locality-Sensitive Hashing）或 MinHash 替代全对比
- 或用 token_hash 分桶，仅在同桶内比较
- 预期效果: O(n²) → O(n × k)，k 为桶大小

### P2（中）: search LIKE 全表扫描

**位置**: [db/db_query.py:499](../db/db_query.py#L499)

**代码**:
```python
AND (fsv.qualified_name LIKE ? OR sc.name LIKE ?)
# 参数: f"%{query}%" — 前缀通配符无法用 B-tree 索引
```

**影响**: admin 0.066s, android 0.203s — 当前规模可接受，但千万级符号会显著恶化。

**优化方向**:
- 使用 SQLite FTS5 对 qualified_name + name 建全文索引
- 或用 n-gram 索引支持模糊搜索

### P3（中）: 策略 5 逐条 DB 查询

**位置**: [db/db_build.py:852-876](../db/db_build.py#L852-L876)

**代码**:
```python
# 对每个未解析调用，执行一次 DB 查询
cur = self.conn.execute(
    "SELECT ... FROM external_symbols WHERE qualified_name = ?",
    (test_qname,)
)
```

**影响**: M 个未解析调用 = M 次 DB 查询。firmware 第三方库多，external_symbols 查询频繁。

**优化方向**: 批量加载 external_symbols 到内存 dict，后续查询走内存。

### P5（低）: _build_depth 逐个 UPDATE

**位置**: [db/db_build.py:1656-1660](../db/db_build.py#L1656-L1660)

**代码**:
```python
for fn_id, depth in depth_cache.items():
    self.conn.execute(
        "UPDATE symbols SET depth = ? WHERE id = ?",
        (depth, fn_id),
    )
```

**影响**: 10 万函数 = 10 万次 UPDATE，事务开销大。

**优化方向**: 用 `executemany` 批量更新。

## 4. 性能趋势分析

基于 admin/android 数据外推：

| 符号数 | refresh 预估 | clone 预估 | 说明 |
|--------|-------------|-----------|------|
| 20K | 3.7min | 2.9s | admin 实测 |
| 50K | 7.5min | 42s | android 实测 |
| 100K | >30min* | ~3min | firmware 实测（卡死） |
| 500K | 不可行** | >1h | 理论推算 |
| 1M+ | 不可行** | 不可行 | 千万级目标 |

\* refresh 卡在调用关系解析，非解析阶段
\*\* O(M×N) 瓶颈导致线性恶化

**结论**: 当前架构在 5 万符号以下性能可接受，10 万符号出现致命瓶颈，无法支撑千万级目标。

## 5. 优化优先级

| 优先级 | 瓶颈 | 优化方案 | 预期收益 | 对应任务 |
|--------|------|---------|---------|---------|
| P0 | 调用解析 O(M×N) | 后缀反向索引 | 100倍+ | T-1783441015098-26d9 |
| P1 | 克隆检测 O(n²) | LSH/MinHash | 50倍+ | T-1783441015098-26d9 |
| P2 | search LIKE | FTS5 全文索引 | 10倍 | 待规划 |
| P3 | 逐条 DB 查询 | 批量加载内存 | 5倍 | 待规划 |
| P5 | 逐个 UPDATE | executemany | 2倍 | 待规划 |

## 6. DB 文件大小分析

| 仓库 | 符号数 | DB(MB) | MB/千符号 | 说明 |
|------|--------|--------|----------|------|
| admin | 20936 | 83 | 3.96 | Java 符号签名长 |
| android | 50776 | 224 | 4.41 | 含 Kotlin |
| ios_muzoplayer | 2884 | 22 | 7.61 | 符号少，开销大 |
| firmware | 107926 | 275 | 2.55 | C 符号短 |

**结论**: 平均约 4 MB/千符号，千万级符号预计 DB 约 40GB（SQLite 单文件可支持，但查询性能需优化）。

## 7. 测试局限性

1. **firmware 第三方库未完全排除**: `middleware/utilib/thirdParty/` 仍包含 ffmpeg/caps/x42-plugins 等
2. **ios_muzoplayer ObjC 解析器覆盖有限**: 3760 文件仅提取 2884 符号
3. **未达千万级**: 4 个仓库合计约 18 万符号，远未达到千万级目标
4. **Windows 长路径限制**: ios_muzoplayer 的 xcframework 路径超 260 字符

## 8. 下一步建议

1. **立即修复 P0**: 实现后缀反向索引，使 firmware refresh 可在 10 分钟内完成
2. **扩展测试**: 修复 P0 后重新测试 firmware（不排除第三方库），验证百万级符号性能
3. **实现 P1 LSH**: 使克隆检测支持 10 万+ 符号
4. **规划千万级**: P0+P1 修复后，合并所有仓库 + 不排除第三方库，目标 500K-1M 符号
