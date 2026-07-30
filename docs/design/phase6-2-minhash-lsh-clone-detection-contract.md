# Phase 6-2 契约：MinHash/LSH clone detection 与循环算法

**Task ID**: `T-1785148066858-41a74576`（Phase 6-2，父任务 `T-1785148066857-e68483a6` — Phase 6：分析能力与可选适配器）
**状态**: contract
**日期**: 2026-07-30
**验证环境**: Windows 10（开发主机）+ WSL2（Linux E2E）+ pytest 差分测试 harness

## 1. 范围

Phase 6-2 将 token shingling、MinHash 签名生成、LSH 分桶与克隆分组核心计算迁移到 Rust。这是 Phase 6 中 CPU 密集度最高的部分（大规模符号的 token 序列哈希与签名矩阵计算），迁移到 Rust + rayon 并行化后预期收益最大。

**涉及**：
- **token shingling**：基于符号 token 序列的 k-shingle 分词
- **MinHash 签名生成**：基于哈希族（MurmurHash/FxHash）的固定长度签名矩阵
- **LSH 分桶**：band-based bucketing，将相似签名聚到同桶
- **克隆分组与 Jaccard 阈值过滤**：候选对 → 验证 → 分组
- **公开方法**：`detect_clones` / `detect_clones_to_groups` / `list_clones` / `get_clone_stats` / `clear_clones` / `_detect_clone_groups_core`
- **数据结构**：`CloneGroup` / `CloneGroupDetail`（`db/db_clone_groups.py`）

**不涉及**（在 Phase 6 其他子任务或 Python 保留层）：
- MCP 工具注册（Python 保留 `@mcp.tool()` 编排层）
- 异步任务管理（`detect_clones_async` / `get_job_status` 等在 MCP 层调度，复用 Phase 6-4 的 job 基础设施）
- CloneGroup 持久化 schema 变更（沿用现有 `clone_groups` / `clone_group_members` 表）
- 向量搜索（Phase 6-3）
- blast radius / impact 计算（Phase 6-1）

## 2. 现有资产盘点

### 2.1 Python 计算核心

| 资产 | 路径 | 说明 |
|---|---|---|
| CloneDetectionMixin | `db/db_clone_detection.py`（821 行） | 5 公开方法 + `_detect_clone_groups_core` 核心 helper；纯 Python 实现，含 numpy 可选加速路径 |
| CloneGroupMixin | `db/db_clone_groups.py`（363 行） | `CloneGroup` / `CloneGroupDetail` 数据类 + 5 公开方法（`store_clone_groups` / `clear_clone_groups` / `list_clone_groups` / `get_clone_group_detail` / `get_clone_group_stats`） |
| 归一化正则 | `db/db_clone_detection.py` `_NORMALIZE_TOKEN_RE` | 标识符/字符串/数字归一化，用于 Type-2 检测 |
| 克隆类型常量 | `db/db_clone_detection.py` `CLONE_TYPE_1/2/3` | Type-1 完全相同 / Type-2 重命名 / Type-3 微调 |

**关键观察**：
- `db_clone_detection.py` 与 `db_clone_groups.py` 均为**纯 Python 实现**，无任何 `callwarden_core` / `rust_ext` 导入
- numpy 为可选依赖（`try/except` 加载），缺失时降级到纯 Python 哈希
- LSH 参数（band 数、row 数、签名长度）以模块常量形式定义，Rust 侧需对齐

### 2.2 Rust 加速层资产

| 资产 | 路径 | 说明 |
|---|---|---|
| GraphStore | `rust_ext/src/graph.rs` | 已实现 callers/callees/search 内存索引；clone detection 不直接依赖，但符号元数据查询可复用 |
| SymbolSearchBatch | `rust_ext/src/graph.rs` | PyO3 懒批对象，符号批量检索可复用 |
| FxHashMap / rayon | `rust_ext/Cargo.toml` | 已依赖 `rustc-hash` 与 `rayon`，可直接用于签名矩阵并行计算 |

### 2.3 Rust 侧缺口

- **无 `rust_ext/src/clone_detection.rs` 模块**：token shingling / MinHash / LSH / 分组核心均未实现
- **无 PyO3 暴露**：`detect_clones_core` 等 PyO3 函数不存在
- **无 `rollback_config` entry**：`rust_clone_detection` flag 未登记

## 3. 验证矩阵

差分测试（Python baseline vs Rust 实现）共用 `differential-harness-contract.md` 的 harness 框架。所有差分测试必须在 `cw --refresh-all` 后运行，确保 GraphStore 与 SQL 数据一致。

### 3.1 D1: MinHash 签名生成差分测试

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D1.1 | 相同 token 序列（同符号两次取签名） | Rust 与 Python 输出签名完全一致 | 字段级 deep diff |
| D1.2 | 不同 token 序列（不同符号） | 签名不同 | 集合不等 |
| D1.3 | 归一化后相同的 Type-2 克隆 | 归一化 token 序列 → 签名一致 | 签名相等 |
| D1.4 | 空符号 / 无 token 序列 | 返回空签名或零签名 | 退出码 0，结构一致 |
| D1.5 | 签名长度（num_perm）与 Python 常量一致 | 签名维度相同 | list 长度相等 |
| D1.6 | 哈希族选择（MurmurHash vs FxHash） | 哈希函数族与 Python 对齐，避免签名发散 | 逐位置签名相等 |

### 3.2 D2: LSH 分桶差分测试

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D2.1 | 相同签名 → 相同桶 | Rust 与 Python 桶 ID 一致 | 集合相等 |
| D2.2 | 高相似签名（Jaccard ≥ 阈值） | 落入至少一个相同桶 | 候选对集合一致 |
| D2.3 | 低相似签名（Jaccard < 阈值） | 不落入相同桶 | 候选对集合为空 |
| D2.4 | band 数 / row 数与 Python 常量一致 | 分桶维度相同 | 桶数量级一致 |
| D2.5 | 边界签名（恰好处于阈值边界） | 与 Python 一致地纳入/排除 | 候选对集合相等 |

### 3.3 D3: detect_clones 端到端差分测试

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D3.1 | 同一代码库（含已知 Type-1 克隆） | Rust 与 Python 输出克隆组一致 | 字段级 deep diff |
| D3.2 | 同一代码库（含 Type-2 重命名克隆） | 克隆组一致 | 字段级 deep diff |
| D3.3 | 同一代码库（含 Type-3 微调克隆） | 克隆组一致（相似度浮点容差 1e-6） | 字段级 deep diff |
| D3.4 | 无克隆代码库 | 空结果 | 退出码 0，空列表 |
| D3.5 | `similarity_threshold` 参数变化 | 阈值过滤结果一致 | 列表相等 |
| D3.6 | `detect_clones_to_groups` 返回 `CloneGroup` 列表 | 与 Python 输出结构一致 | `to_dict()` 字段相等 |
| D3.7 | 大型代码库（10k+ 符号） | 结果一致 + 性能提升 ≥ 5x | 结果差分 + 基准测试 |

### 3.4 D4: Jaccard 相似度计算差分测试

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D4.1 | 两相同 shingle 集合 | Jaccard = 1.0 | 浮点相等（容差 1e-9） |
| D4.2 | 两不相交 shingle 集合 | Jaccard = 0.0 | 浮点相等 |
| D4.3 | 部分重叠集合 | Jaccard = |A∩B| / |A∪B| | 浮点相等 |
| D4.4 | 空集合（边界） | 与 Python 一致的默认行为 | 退出码 0，结果一致 |
| D4.5 | MinHash 估算 Jaccard vs 精确 Jaccard | 估算误差在预期范围内 | 误差 ≤ 1/√num_perm |

## 4. 迁移策略

按性能收益与依赖顺序分阶段迁移，每个 Rust 实现通过 `rollback_config` flag 控制，默认 Python，差分测试稳定后切换。

### 4.1 模块划分

作为独立 Rust 模块 `rust_ext/src/clone_detection.rs` 实现，对外暴露 `detect_clones_core` PyO3 函数。Python 侧（`CloneDetectionMixin`）保留：
- 结果持久化（写入 `clone_groups` / `clone_group_members` 表）
- `CloneGroup` / `CloneGroupDetail` 数据类封装
- MCP 工具编排（`detect_clones_async` 异步调度）
- `list_clones` / `get_clone_stats` / `clear_clones` 的查询/清理路径（这些是 DB 查询，迁移收益低）

### 4.2 性能优化手段

1. **`rustc-hash`（FxHashMap）替代 Python dict**：shingle 集合、签名矩阵、桶映射全部用 FxHashMap/FxHashSet，降低哈希开销
2. **`rayon` 并行化 MinHash 签名生成**：每个符号的签名计算独立，`par_iter` 跨符号并行
3. **签名矩阵紧凑布局**：`Vec<u64>` 连续存储，避免 Python list of int 的装箱开销
4. **LSH 分桶批处理**：所有符号的签名一次性入桶，避免 Python 逐符号循环
5. **候选对验证短路**：先 MinHash 估算 Jaccard，低于阈值的候选对直接跳过精确 shingle 集合计算

### 4.3 哈希函数族选择

- **MurmurHash3**（首选）：与 Python `mmh3` 库（若可用）或纯 Python fallback 输出一致；需在 Rust 侧使用 `murmurhash3` crate 并与 Python baseline 对齐种子
- **FxHash（备选）**：更快但分布略弱；仅用于 LSH 桶 ID 计算，不用于 MinHash 签名
- **差分测试锁定**：D1.6 验证哈希族选择，确保 Rust 与 Python 签名逐位置相等

### 4.4 LSH 参数对齐

| 参数 | Python 位置 | Rust 对齐方式 |
|---|---|---|
| `num_perm`（签名长度） | `db_clone_detection.py` 模块常量 | Rust 模块 `const NUM_PERM` |
| `num_bands`（band 数） | 同上 | Rust 模块 `const NUM_BANDS` |
| `rows_per_band`（每 band 行数） | 同上 | Rust 模块 `const ROWS_PER_BAND` |
| `similarity_threshold` | `detect_clones` 参数 | PyO3 函数参数透传 |

## 5. 实现计划

### P0: 契约文档（当前）

1. **编写本契约文档** ✅
2. **盘点现有资产**：Python `CloneDetectionMixin` + `CloneGroupMixin` 已齐全；Rust 侧无对应模块
3. **识别缺口**：Rust 版 token shingling / MinHash / LSH / 分组核心均未实现

### P1: MinHash 签名 + LSH 分桶 Rust 实现 + 单元测试

1. **在 `rust_ext/src/clone_detection.rs` 实现 token shingling**：k-shingle 分词，与 Python `_normalize_tokens` 顺序一致
2. **实现 MinHash 签名生成**：MurmurHash3 哈希族 + `num_perm` 长度签名
3. **实现 LSH 分桶**：band-based bucketing，桶 ID 哈希与 Python 一致
4. **PyO3 暴露**：`py_minhash_signature(tokens) -> Vec<u64>` / `py_lsh_buckets(signatures) -> HashMap`
5. **D1 + D2 差分测试**：D1.1–D1.6 + D2.1–D2.5 全通过
6. **`rollback_config` 登记 `rust_clone_detection`（阶段 1：仅签名/分桶）**

### P2: detect_clones_core 端到端实现 + 差分测试

1. **实现 Jaccard 精确计算 + MinHash 估算**：D4 差分测试
2. **实现候选对验证 + 克隆分组核心**：`detect_clones_core(symbols, threshold) -> Vec<CloneGroupRaw>`
3. **PyO3 暴露**：`py_detect_clones_core(symbol_hashes, threshold) -> PyDict`
4. **D3 + D4 差分测试**：D3.1–D3.7 + D4.1–D4.5 全通过
5. **`rollback_config` 升级 `rust_clone_detection`（阶段 2：完整 detect_clones）**

### P3: CloneGroup 持久化对接（Python 侧）

1. **`CloneDetectionMixin.detect_clones` 接入 Rust 短路**：feature flag 默认 Python
2. **Rust 输出转换为 `CloneGroup` 数据类**：字段映射一致
3. **`store_clone_groups` 持久化路径不变**：沿用现有 schema
4. **`detect_clones_to_groups` 端到端验证**：返回 `CloneGroup` 列表与 Python baseline 一致
5. **`list_clones` / `get_clone_stats` / `clear_clones` 保持 Python**：DB 查询路径，迁移收益低

### P4: wire-production + verify + review

1. **wire-production**：Python `CloneDetectionMixin` 全面接入 Rust 短路（feature flag 默认 Python，差分稳定后切 Rust）
2. **差分回归**：D1–D4 全套通过
3. **大型代码库性能基准**：10k+ 符号场景 Rust vs Python，确认 ≥ 5x 加速
4. **migration-manifest.md §45 Review 清单**填写
5. **close Phase 6-2 任务**
6. **回填 Phase 6-1 `get_clone_aware_impact` Rust 短路**（依赖本阶段 `clone_groups` 持久化结果）

## 6. 验收标准

1. **D1 MinHash 签名差分测试**：D1.1–D1.6 全通过
2. **D2 LSH 分桶差分测试**：D2.1–D2.5 全通过
3. **D3 detect_clones 端到端差分测试**：D3.1–D3.7 全通过
4. **D4 Jaccard 相似度差分测试**：D4.1–D4.5 全通过
5. **rollback_config 登记**：`rust_clone_detection` entry 完整（含两阶段升级记录）
6. **大型代码库性能**：10k+ 符号场景 detect_clones 端到端性能提升 ≥ 5x（中位数，串行运行取 3 次中位）
7. **migration-manifest.md §45 Review 清单完整**
8. **Phase 6-2 任务状态机完成 + closed**
9. **文档同步**（AGENTS.md 规则 22）：本阶段不新增 MCP 工具/CLI 子命令/Mixin/语言，无需更新 mcp_tools.md / cli_reference.md；但 `migration-manifest.md` + `docs/architecture.md`（如涉及 Rust 模块清单）须同步

## 7. 风险与注意事项

### 7.1 AGENTS.md 强制规则

- **规则 17**：Rust 懒批对象必须在服务边界物化 — `detect_clones_core` 返回的候选对/分组若使用懒批，MCP/daemon 边界须 `list(...)`
- **规则 22**：代码变更必须同步更新文档（本阶段涉及 `migration-manifest.md`，Rust 模块清单若有则同步 `docs/architecture.md`）
- **规则 8**：多行查询走 Rust 短路 — clone detection 候选对验证符合此原则
- **规则 1**：提交前必须全量刷新数据库 — 差分测试前 `cw --refresh-all`
- **规则 13**：合成数据压测 ≠ 真实 E2E — 性能基准须用真实代码库（如 callwarden 自身或开源项目），不可用 `generate_data()` 合成

### 7.2 技术风险

1. **token 分词器与 Python 一致性**：Python 侧基于 tree-sitter AST 遍历顺序生成 token 序列，Rust 侧需复用相同遍历顺序（前序/后序/中序）。若 Rust 侧直接从 GraphStore 取符号内容而非重新解析 AST，需确保 token 提取逻辑与 Python `_normalize_tokens` 逐 token 对齐。
2. **哈希函数族选择**：MurmurHash3 与 FxHash 输出不同；若 Python 用 `hashlib`（SHA/MD5）而 Rust 用 MurmurHash3，签名将完全发散。必须 D1.6 锁定哈希族一致性，或在差分测试中做哈希族无关的等价验证（如比较 Jaccard 估算而非逐位置签名）。
3. **LSH 参数对齐**：`num_perm` / `num_bands` / `rows_per_band` 必须与 Python 模块常量逐一对齐，否则候选对集合不一致。
4. **内存占用控制**：大型代码库（10k+ 符号）的 MinHash 签名矩阵为 `num_perm × num_symbols × 8 bytes`，10k 符号 × 128 perm × 8B ≈ 10MB，可控；但 shingle 集合若全量缓存可能爆炸，需流式处理或 LRU 驱逐。
5. **浮点相似度容差**：Type-3 克隆的 `similarity` 浮点字段在 Rust（f64）与 Python（float）间可能有 ULP 级差异，差分测试须用容差 1e-6 而非精确相等。
6. **rayon 线程安全**：MinHash 签名生成并行化时，签名矩阵写入须用 `par_iter_mut` 或预分配 `Vec` + 索引写入，避免 `Mutex` 锁竞争。
7. **numpy 加速路径兼容**：Python 侧有 numpy 可选加速路径，差分测试须覆盖 numpy 启用与禁用两种 baseline，确保 Rust 输出与两者均一致。

### 7.3 本地验证局限

- Windows 开发环境无法验证 Linux 专属场景（无）
- 大型代码库性能基准须在开发主机串行运行，避免后台 watcher / MCP Server 干扰
- rayon 线程数在 CI 与本地可能不同，性能基准须记录硬件型号与线程数（AGENTS.md 规则 13）

## 8. 与其他 Phase 6 子任务的关系

| 子任务 | 交付物 | Phase 6-2 关系 |
|---|---|---|
| 6-1 | blast radius / impact / 演化热点 Rust 迁移 | `get_clone_aware_impact` 依赖本阶段 `clone_groups` 结果；6-1 已保留 Python 入口，本阶段 P4 回填 Rust 短路 |
| **6-2** | **MinHash/LSH clone detection Rust 迁移** | **本契约** |
| 6-3 | 向量搜索（sqlite-vec vec0 虚拟表） | 无依赖（clone detection 基于 token shingling + MinHash，不依赖向量嵌入） |
| 6-4 | MCP 工具 / Semgrep / RAG 集成 | Python 保留 MCP 编排层；`detect_clones_async` 异步调度复用 6-4 的 job 基础设施 |

## 9. 下一步

Phase 6-2 完成后，推进 **Phase 6-3**（向量搜索 — sqlite-vec vec0 虚拟表）：
- 实现 vec0 虚拟表落地（替换当前 BLOB + 余弦相似度方案）
- 差分测试覆盖 `semantic_search` / `find_similar_functions` / `embed_symbols`
- 后续 Phase 6-4 按计划推进 MCP 工具与 Semgrep/RAG 集成
