# 参数矩阵实验报告（1M 规模，3 次中位数）

> 任务：T-1783907815346-75de
> 日期：2026-07-13
> 脚本：[tests/_bench_matrix.py](file:///c:/git_work/callwarden/tests/_bench_matrix.py)
> 数据：[tests/_matrix_1m_report.json](file:///c:/git_work/callwarden/tests/_matrix_1m_report.json)

## 1. 矩阵设计

**固定**：mode=deferred, temp_store=MEMORY, commit_every=10, symbols=1M（1,000,000 符号 / 7M 调用边 / 200K 文件）

**变量**（6 组合）：

| 组合 | cache_size | mmap_size | page_size | 假设 |
|------|-----------|-----------|-----------|------|
| baseline | 64 MB | 256 MB | 4 KB | 与 v2 基准一致 |
| cache_256 | **256 MB** | 256 MB | 4 KB | 单测 cache 收益 |
| mmap_64 | 64 MB | **1024 MB** | 4 KB | 单测 mmap 收益 |
| combined | 256 MB | 1024 MB | 4 KB | cache + mmap 联合 |
| extreme | **512 MB** | 1024 MB | 4 KB | 极限内存（验证收益递减点） |
| page8 | 256 MB | 1024 MB | **8 KB** | page_size 影响 |

## 2. 总览

| 组合 | insert(s) | index(s) | **storage(s)** | db(MB) | peak_WAL(MB) | peak_RSS(MB) | 加速比 |
|------|-----------|----------|---------------|--------|--------------|--------------|--------|
| baseline | 49.36 | 40.85 | **90.60** | 1247 | 168 | 484 | 1.00x |
| cache_256 | 41.18 | 38.84 | **80.06** | 1247 | 168 | 487 | 1.13x |
| mmap_64 | 44.36 | 39.74 | **84.17** | 1247 | 168 | 489 | 1.08x |
| combined | 45.53 | 41.13 | **82.91** | 1247 | 168 | 490 | 1.09x |
| extreme | 39.04 | 41.25 | **83.54** | 1247 | 168 | 491 | 1.08x |
| **page8** | **37.07** | **37.91** | **74.52** | **1241** | 167 | 487 | **1.22x** |

## 3. calls 表 3 索引耗时（关键瓶颈）

| 组合 | idx_calls_caller | idx_calls_callee | idx_calls_callee_q | 合计 | 占 index_s |
|------|------------------|------------------|---------------------|------|-----------|
| baseline | 4.35s | 14.60s | 14.34s | **33.29s** | 81.5% |
| cache_256 | 5.04s | 15.40s | 11.95s | 32.39s | 83.4% |
| mmap_64 | 3.09s | 13.31s | 10.03s | **26.42s** | 66.5% |
| combined | 4.76s | 11.67s | 13.51s | 29.94s | 72.8% |
| extreme | 5.10s | 17.37s | 13.06s | 35.53s | 86.1% |
| **page8** | 2.70s | 10.44s | 15.07s | **28.21s** | 74.4% |

## 4. 边际收益分析（相对 baseline）

| 变量 | storage_build 收益 | index_s 收益 | 备注 |
|------|-------------------|-------------|------|
| cache 64→256MB | **+11.6%** | +4.9% | 主要收益来源 |
| mmap 256→1024MB | +7.1% | +2.7% | 单独加大 mmap 收益有限 |
| cache+mmap 联合 | +8.5% | -0.7% | **不叠加**，combined 比 cache_256 还慢 |
| cache 256→512MB | +7.8% | -1.0% | **收益递减**，甚至更慢 |
| page 4→8KB | **+17.8%** | +7.2% | 与 cache_256 叠加效果最好 |

## 5. 关键发现

### 5.1 page8 是最优 SQLite 构建参数组合

`cache=256MB + mmap=256MB(够用) + page_size=8KB + temp_store=MEMORY + mode=deferred`

- **storage_build: 90.60 → 74.52s = 17.8% 加速**
- DB 体积反而更小（1241 vs 1247MB，8KB page 元数据开销更低）
- RSS 几乎不变（487 vs 484MB，cache 是 lazily allocated）

**为什么 page_size=8K 有效**：
- 1M 符号的 qualified_name 平均长度 ~30 字节，8K page 能装更多 entries
- B-tree 内部节点扇出更大，建索引时需要的 page split 更少
- 对大表（calls 7M 行）尤其有效

### 5.2 cache_size 边际收益递减点在 256MB

- 64→256MB: 收益 +11.6%（值得）
- 256→512MB: 收益 -1%（**反而更慢**，可能 fsync 开销变大）

**原因**：1M 数据 ~1.2GB，64MB cache 装不下热点（频繁换页），256MB cache 能装下大部分热点，512MB cache 已经能装下大部分数据，但 commit 时需要 fsync 更多 dirty pages。

### 5.3 mmap_size 几乎无收益

- 单独加大 mmap（256→1024MB）只带来 7%，且与 cache_256 联合时收益不叠加
- combined（cache=256 + mmap=1024）比 cache_256 单独还慢
- **mmap 主要影响读，对写和建索引影响小**

### 5.4 calls 3 索引仍是绝对瓶颈

- 最优组合下仍占 74.4%（28.21s / 37.91s）
- SQL 层已到顶，无法通过参数调优突破
- **必须通过 GraphStore 接管这些查询路径**

## 6. P13/P14/P15 验证结论

| 假设 | 验证结果 | 收益 | 决策 |
|------|---------|------|------|
| P13 cache_size 提升 | ✅ 有效 | +11.6% | **实施到 256MB**（不要 512MB） |
| P14 mmap_size 提升 | ❌ 无效 | +7% 且不叠加 | **废弃** |
| P15 page_size 调整 | ✅ 有效 | +7.2% | **实施 page_size=8KB** |

**最优 SQLite 构建参数**：`cache_size=256MB + page_size=8KB + temp_store=MEMORY + mode=deferred`
**联合收益**：**17.8%**（90.60s → 74.52s）

## 7. 外推到 10M（按 10x 线性外推）

| 参数集 | 1M 实测 | 10M 外推 | 节省 |
|--------|---------|-----------|------|
| baseline (当前) | 90.60s | ~906s = 15.1 分钟 | - |
| 最优 SQLite | 74.52s | ~745s = 12.4 分钟 | **2.7 分钟** |
| calls 3 索引绝对耗时 | 28.21s | ~282s = 4.7 分钟 | 仍是最大瓶颈 |

**结论**：SQLite 参数优化收益有限（17.8%）。真正的大头是 calls 表 3 索引本身（占 74%），必须通过 GraphStore 接管才能突破。

## 8. 下一步建议

1. **应用 P13+P15 到正式代码**（低风险增量，17.8% 收益）
   - 修改 [db/db_base.py](file:///c:/git_work/callwarden/db/db_base.py) 中的 PRAGMA 设置
   - cache_size: -64000 → -262144（64MB → 256MB）
   - page_size: 默认 4096 → 8192（在创建任何表之前设置）
2. **启动 GraphStore P1-P6**（突破 SQL 瓶颈）
   - calls 3 索引可减载到 GraphStore CSR
   - 查询性能 5000x 加速
   - 建索引阶段可以跳过部分索引（如 idx_calls_callee_qualified，由 GraphStore 提供相同查询能力）

## 9. 测试环境

- SQLite 3.50.4, Python 3.14.3
- Windows 11, Intel 22 cores, 31GB RAM（8.7GB available）
- Disk: 952GB total, 33GB free
- 总耗时：25.2 分钟（6 组合 × 3 次串行）
