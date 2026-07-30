# Phase 6-3 契约：向量索引、余弦计算与测试关联

**Task ID**: `T-1785148066858-6e0c6cb9`（Phase 6-3，父任务 `T-1785148066857-e68483a6` — Phase 6：分析能力与可选适配器）
**状态**: contract
**日期**: 2026-07-30
**验证环境**: Windows 10（开发主机）+ WSL2（Linux E2E）+ pytest 差分测试 harness

## 1. 范围

Phase 6-3 将向量加载与内存索引、TopK 排序、测试关联 XML 解析的计算核心迁移到 Rust。`batch_cosine_similarity` 已在 Phase 1 完成 Rust 实现，本阶段在此基础上完成"批量余弦 → TopK 排序 → 候选符号物化"整条链路的 Rust 短路。`embed_symbol` 依赖 `sentence-transformers`（Python ML 库），保留 Python。

**涉及**：
- **batch_cosine_similarity**（已实现，`rust_ext/src/lib.rs:1574-1603`，30 行）
- **向量加载与内存索引**：`_load_all_embeddings`（全量 embedding 读取到内存）
- **semantic_search / find_similar_functions 的 TopK 排序**：得分排序 + 阈值过滤 + top_n 截断
- **测试关联：lcov/cobertura XML 解析**：`db_coverage.py` 的 `import_lcov` / `import_cobertura`
- **JUnit XML 解析 + 测试关联**：`db_tests.py` 的 `import_test_results`
- **test_impact_selection**：测试影响选择（基于 callers 反向追溯测试函数）

**不涉及**（保留 Python）：
- **embed_symbol / embed_all_symbols**：依赖 `sentence-transformers`（Python ML 库），无法在 Rust 侧重建等价模型加载与推理；保留 Python 实现
- **ask_codebase 的 RAG 上下文构建**：`_build_rag_block` / `_format_rag_context` / `_lookup_symbol_hash_by_qualified_name` / `_get_callees_for_symbol` 等编排逻辑保留 Python
- **keyword_fallback_search**：Python 文本搜索，作为 embedding 不可用时的降级路径

## 2. 现有资产盘点

### 2.1 Python 计算核心

| 资产 | 路径 | 说明 |
|---|---|---|
| VectorMixin | `db/db_vector.py`（941 行） | 5 公开方法 + 9 helper；已配置三级降级链：`callwarden_core.batch_cosine_similarity` > numpy 批量 > 纯 Python |
| CoverageMixin | `db/db_coverage.py`（566 行） | 5 公开方法 + 5 helper；`import_lcov` / `import_cobertura` / `get_coverage_for_symbol` / `find_uncovered_functions` / `test_impact_selection` |
| TestRelationMixin | `db/db_tests.py`（491 行） | 6 公开方法 + 1 helper；`build_test_relations` / `get_test_cases` / `get_tested_functions` / `get_test_coverage_summary` / `import_test_results` / `get_test_stability` |

### 2.2 VectorMixin 公开方法清单

| 方法 | 行号 | 迁移策略 |
|---|---|---|
| `embed_symbol` | 226 | **保留 Python**（sentence-transformers 依赖） |
| `embed_all_symbols` | 265 | **保留 Python**（同上） |
| `semantic_search` | 395 | **Rust 短路**（TopK 排序 + 阈值过滤） |
| `find_similar_functions` | 482 | **Rust 短路**（TopK 排序 + 阈值过滤） |
| `ask_codebase` | 589 | **保留 Python**（RAG 上下文构建） |

### 2.3 Rust 加速层资产

| 资产 | 路径 | 说明 |
|---|---|---|
| `batch_cosine_similarity` | `rust_ext/src/lib.rs:1574-1603` | **已实现**（Phase 1）；query (dim,) × matrix (N, dim) → scores (N,)；30 行 |
| numpy PyArray 互操作 | `rust_ext/src/lib.rs:1570` | 已 `use numpy::{PyArray1, PyReadonlyArray1, PyReadonlyArray2}` |

### 2.4 Rust 侧缺口

- **无向量加载内存索引**：`_load_all_embeddings` 仍是 Python 从 SQLite BLOB 读取 + `numpy.frombuffer` 还原，未走 Rust 批量 BLOB → `Vec<f32>` 路径
- **无 TopK 排序 Rust 实现**：`semantic_search` / `find_similar_functions` 的 `heapq.nlargest` / `sorted` + 阈值过滤仍为纯 Python
- **无 XML 解析 Rust 实现**：lcov / cobertura / JUnit 三类 XML 解析均为 Python `xml.etree.ElementTree`，未迁移 `quick-xml`
- **无 `rollback_config` entry**：`rust_vector_topk` / `rust_coverage_parser` / `rust_junit_parser` 三个 flag 均未登记

## 3. 验证矩阵

差分测试（Python baseline vs Rust 实现）共用 `differential-harness-contract.md` 的 harness 框架。所有差分测试必须在 `cw --refresh-all` 后运行，确保 GraphStore 与 SQL 数据一致；向量相关测试须先用 `cw embed --all` 生成 embedding。

### 3.1 D1: batch_cosine_similarity 差分测试（已通过，Phase 1 完成）

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D1.1 | 单向量 query × 单行 matrix | Rust 与 numpy/Python 输出一致 | 浮点相等（容差 1e-6） |
| D1.2 | query × N=1000 行 matrix | scores 长度 = N，逐元素一致 | 数组相等 |
| D1.3 | 零向量 query / 零向量行 | 返回零数组（无 NaN） | 无 NaN |
| D1.4 | 维度不一致（dim mismatch） | PyResult 报错，与 numpy 一致 | 异常类型一致 |

### 3.2 D2: 向量加载与 TopK 排序差分测试

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D2.1 | 全量加载 embedding（N=500） | Rust 加载结果与 `_load_all_embeddings` 一致 | `[(symbol_hash, vec)]` 列表相等 |
| D2.2 | TopK=10 排序 | Rust 与 Python `heapq.nlargest` 输出一致 | list[dict] 字段级 deep diff |
| D2.3 | 阈值过滤（threshold=0.5） | 过滤后候选集一致 | 集合相等 |
| D2.4 | 相同分数的稳定性 | Rust 与 Python 在相同分数下的排序顺序一致（symbol_hash tiebreaker） | list 顺序一致 |
| D2.5 | 空 embedding 表 | 返回空结果 | 退出码 0，空列表 |
| D2.6 | `semantic_search` 端到端 | Rust 短路与 Python baseline 输出一致 | 字段级 deep diff |
| D2.7 | `find_similar_functions` 端到端 | Rust 短路与 Python baseline 输出一致 | 字段级 deep diff |
| D2.8 | 大型 embedding 矩阵（100k+ 向量） | 结果一致 + TopK 排序性能提升 ≥ 3x | 结果差分 + 基准测试（串行取 3 次中位） |

### 3.3 D3: lcov/cobertura XML 解析差分测试

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D3.1 | 标准 lcov XML | Rust 与 Python 解析的 coverage 记录一致 | 记录集合相等 |
| D3.2 | 标准 cobertura XML | 同上 | 记录集合相等 |
| D3.3 | 空文件 / 无 coverage 数据 | 返回空记录，退出码 0 | 空列表 |
| D3.4 | 格式错误的 XML | Rust 与 Python 一致地报错（或一致地降级） | 异常类型一致 |
| D3.5 | 路径归一化（`_normalize_path`） | Rust 与 Python 路径归一化结果一致 | 字符串相等 |
| D3.6 | 大型 lcov 报告（10k+ 行） | 解析结果一致 + 性能提升 ≥ 2x | 结果差分 + 基准测试 |

### 3.4 D4: JUnit XML 解析 + 测试关联差分测试

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D4.1 | 标准 JUnit XML | Rust 与 Python 解析的 test case 记录一致 | 记录集合相等 |
| D4.2 | `import_test_results` 端到端 | 持久化的 test_results / test_relations 一致 | DB 行级 diff |
| D4.3 | `build_test_relations` 关联建立 | 关联表行数一致 | COUNT 相等 |
| D4.4 | `get_test_cases` 反查 | 返回的 test case 列表一致 | list[dict] 相等 |
| D4.5 | `test_impact_selection`（callers 反向追溯） | 受影响的测试函数集合一致 | 集合相等 |
| D4.6 | 失败/跳过状态解析 | Rust 与 Python 一致解析 failure/skipped | 字段一致 |
| D4.7 | 含中文测试名 | UTF-8 解码一致 | 字段一致 |
| D4.8 | 大型 JUnit 报告（10k+ testcase） | 解析结果一致 + 性能提升 ≥ 2x | 结果差分 + 基准测试 |

## 4. 迁移策略

按性能收益与依赖顺序分阶段迁移，每个 Rust 实现通过 `rollback_config` flag 控制，默认 Python，差分测试稳定后切换。

### 4.1 模块划分

- **`rust_ext/src/vector_topk.rs`（新模块）**：向量加载内存索引 + TopK 排序 + 阈值过滤，对外暴露 `py_vector_topk(query_vec, matrix, threshold, top_n) -> Vec<(String, f32)>`
- **`rust_ext/src/coverage_parser.rs`（新模块，可选）**：lcov / cobertura XML 解析，对外暴露 `py_parse_lcov(xml_str) -> Vec<PyDict>` / `py_parse_cobertura(xml_str) -> Vec<PyDict>`
- **`rust_ext/src/junit_parser.rs`（新模块，可选）**：JUnit XML 解析，对外暴露 `py_parse_junit(xml_str) -> Vec<PyDict>`
- Python 层（`VectorMixin` / `CoverageMixin` / `TestRelationMixin`）保留：DB 持久化、MCP 工具编排、`embed_symbol` 模型加载

### 4.2 性能优化手段

1. **rayon 并行化 TopK 排序**：得分计算已由 `batch_cosine_similarity` 完成，TopK 截断用 `select_nth_unstable` + 阈值过滤并行化
2. **BLOB 批量解码**：`_load_all_embeddings` 改为 Rust 一次性 `SELECT symbol_hash, embedding FROM symbol_embeddings` + `Vec::from_raw_parts` 解码，避免 Python 逐行 `numpy.frombuffer`
3. **`quick-xml` 流式解析**：lcov / cobertura / JUnit 报告可能达 10k+ 节点，用 `quick-xml::Reader` 流式解析，避免 `ElementTree` 全量 DOM
4. **零拷贝字符串**：XML 解析的路径/测试名用 `&str` 借用，仅在持久化时转 `String`

### 4.3 向量维度一致性

- **768 维**（jina-embeddings-v2-base-code 默认）与 **384 维**（备用小模型）共存
- Rust 侧不硬编码维度，从 `matrix.ncols()` 动态读取
- D2.1 验证两种维度下的加载与排序一致性

### 4.4 embed_symbol 保留 Python 的理由

- `sentence-transformers` 是 Python ML 库，依赖 PyTorch / ONNX Runtime
- Rust 侧无等价轻量模型加载方案（`candle` 生态尚不成熟，且需重新导出模型权重）
- 迁移收益低：`embed_symbol` 是 I/O 密集（模型推理），非 CPU 密集；Rust 化不会带来数量级加速
- 保留 Python 入口 + Rust 加速余弦计算，已达到性能目标（D1.8 大型矩阵 3x+ 加速）

## 5. 实现计划

### P0: 契约文档（当前）

1. **编写本契约文档** ✅
2. **盘点现有资产**：`VectorMixin` + `CoverageMixin` + `TestRelationMixin` + `batch_cosine_similarity` 已齐全
3. **识别缺口**：Rust 版向量加载 / TopK 排序 / XML 解析均未实现；`rollback_config` 三个 flag 未登记

### P1: 向量加载 + TopK 排序 Rust 实现 + 差分测试

1. **在 `rust_ext/src/vector_topk.rs` 实现 `_load_all_embeddings` Rust 版**：BLOB → `Vec<f32>` 批量解码
2. **实现 TopK 排序 + 阈值过滤**：`select_nth_unstable` + rayon 并行
3. **PyO3 暴露**：`py_vector_topk(query_vec, matrix, threshold, top_n) -> Vec<(String, f32)>`
4. **D2 差分测试**：D2.1–D2.8 全通过
5. **`rollback_config` 登记 `rust_vector_topk`**

### P2: semantic_search / find_similar_functions wire-production

1. **`VectorMixin.semantic_search` 接入 Rust 短路**：feature flag 默认 Python
2. **`VectorMixin.find_similar_functions` 接入 Rust 短路**：同上
3. **D2.6 + D2.7 端到端差分回归**：与 Python baseline 字段级一致
4. **大型 embedding 矩阵性能基准**：100k+ 向量场景 Rust vs Python，确认 ≥ 3x 加速（AGENTS.md 规则 13：串行运行取 3 次中位，记录硬件型号）

### P3: 测试关联 XML 解析（可选迁移）

1. **在 `rust_ext/src/coverage_parser.rs` 实现 lcov/cobertura 解析**：`quick-xml` 流式
2. **在 `rust_ext/src/junit_parser.rs` 实现 JUnit 解析**：同上
3. **PyO3 暴露**：`py_parse_lcov` / `py_parse_cobertura` / `py_parse_junit`
4. **D3 + D4 差分测试**：D3.1–D3.6 + D4.1–D4.8 全通过
5. **`rollback_config` 登记 `rust_coverage_parser` / `rust_junit_parser`**
6. **若收益评估不达标（< 2x 加速），P3 可降级为保留 Python**，仅在 P4 收尾时记录决策

### P4: wire-production + verify + review

1. **wire-production**：Python `CoverageMixin` / `TestRelationMixin` 接入 Rust 短路（feature flag 默认 Python，差分稳定后切 Rust）
2. **差分回归**：D1–D4 全套通过
3. **migration-manifest.md §46 Review 清单**填写
4. **close Phase 6-3 任务**

## 6. 验收标准

1. **D1 batch_cosine_similarity 差分测试**：D1.1–D1.4 全通过（Phase 1 已完成，本阶段回归确认无回归）
2. **D2 向量加载与 TopK 排序差分测试**：D2.1–D2.8 全通过
3. **D3 lcov/cobertura XML 解析差分测试**：D3.1–D3.6 全通过（若 P3 执行）
4. **D4 JUnit XML 解析 + 测试关联差分测试**：D4.1–D4.8 全通过（若 P3 执行）
5. **rollback_config 登记**：`rust_vector_topk` 必登记；`rust_coverage_parser` / `rust_junit_parser` 视 P3 执行情况登记
6. **大型 embedding 矩阵性能**：100k+ 向量场景 TopK 排序性能提升 ≥ 3x（中位数，串行运行取 3 次中位，记录硬件型号）
7. **migration-manifest.md §46 Review 清单完整**
8. **Phase 6-3 任务状态机完成 + closed**
9. **文档同步**（AGENTS.md 规则 22）：本阶段不新增 MCP 工具/CLI 子命令/Mixin/语言，无需更新 `mcp_tools.md` / `cli_reference.md`；但 `migration-manifest.md` + `docs/architecture.md`（如涉及 Rust 模块清单）须同步

## 7. 风险与注意事项

### 7.1 AGENTS.md 强制规则

- **规则 17**：Rust 懒批对象必须在服务边界物化 — TopK 排序返回的 `Vec<(String, f32)>` 若包装为懒批，MCP/daemon 边界须 `list(...)`
- **规则 22**：代码变更必须同步更新文档（本阶段涉及 `migration-manifest.md`，Rust 模块清单若有则同步 `docs/architecture.md`）
- **规则 8**：多行查询走 Rust 短路 — 向量加载与 TopK 排序符合此原则
- **规则 1**：提交前必须全量刷新数据库 — 差分测试前 `cw --refresh-all` + `cw embed --all`
- **规则 13**：合成数据压测 ≠ 真实 E2E — 性能基准须用真实代码库 embedding，不可用合成向量

### 7.2 技术风险

1. **向量维度一致性**（768 维 vs 384 维）：Rust 侧须动态读取维度，不可硬编码。D2.1 验证两种维度下的加载与排序一致性。
2. **TopK 排序的稳定性**：相同分数的排序顺序在 Rust（`select_nth_unstable` 不稳定排序）与 Python（`sorted` 稳定排序）间可能不同；须用 `symbol_hash` 作为 tiebreaker 显式对齐。D2.4 验证。
3. **XML 解析的边界情况**：空文件、格式错误、命名空间、CDATA、中文测试名等边界情况须在 D3.4 / D4.7 覆盖。`quick-xml` 与 `ElementTree` 在命名空间处理上语义可能不同，须显式对齐。
4. **sentence-transformers 模型版本兼容性**：`embed_symbol` 保留 Python，但模型升级可能导致新 embedding 与历史 embedding 维度/语义不一致；Rust 侧不感知此变化，须在 `embed_all_symbols` 入口校验维度。
5. **BLOB 解码的字节序与 dtype**：Python 侧用 `numpy.float32 + tobytes` 存储，Rust 侧须用 `f32::from_le_bytes`（x86/ARM 均为小端），不可用 `from_be_bytes`。D2.1 验证。
6. **PyO3 懒批边界物化**：`semantic_search` 返回嵌套 dict + 可能的懒批得分列表，MCP 边界须 `list(...)`（AGENTS.md 规则 17）。
7. **rayon 线程安全**：TopK 排序并行化时，得分数组写入须用 `par_iter_mut` 或预分配 `Vec` + 索引写入，避免 `Mutex` 锁竞争。

### 7.3 本地验证局限

- Windows 开发环境无法验证 Linux 专属场景（无）
- 大型 embedding 矩阵性能基准须在开发主机串行运行，避免后台 watcher / MCP Server 干扰
- rayon 线程数在 CI 与本地可能不同，性能基准须记录硬件型号与线程数（AGENTS.md 规则 13）
- `sentence-transformers` 模型下载需网络，离线环境须预缓存模型

## 8. 与其他 Phase 6 子任务的关系

| 子任务 | 交付物 | Phase 6-3 关系 |
|---|---|---|
| 6-1 | blast radius / impact / 演化热点 Rust 迁移 | **无直接依赖**（演化智能不依赖向量） |
| 6-2 | MinHash/LSH clone detection | **无直接依赖**（clone detection 基于 token shingling + MinHash，不依赖向量嵌入） |
| **6-3** | **向量索引、余弦计算与测试关联 Rust 迁移** | **本契约** |
| 6-4 | MCP 工具 / Semgrep / RAG 集成 | Python 保留 MCP 编排 + `ask_codebase` 的 RAG 上下文构建；6-4 在其上注册新工具 |

## 9. 下一步

Phase 6-3 完成后，推进 **Phase 6-4**（MCP 工具 / Semgrep / RAG 集成）：
- 在本阶段 Rust 短路稳定后，6-4 注册新的 MCP 工具（如 `vector_search_v2`）
- `ask_codebase` 的 RAG 上下文构建保留 Python 编排，但可调用本阶段的 Rust TopK 短路加速候选检索
- 后续视 sqlite-vec vec0 虚拟表落地情况，评估是否替换当前 BLOB + 全量扫描方案（大型代码库 100k+ 符号的近似最近邻搜索）
