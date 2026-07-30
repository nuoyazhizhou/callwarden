# Phase 6-1 契约：blast radius、impact 与演化热点

**Task ID**: `T-1785148066858-a0d73ef2`（Phase 6-1，父任务 `T-1785148066857-e68483a6` — Phase 6：分析能力与可选适配器）
**状态**: contract
**日期**: 2026-07-30
**验证环境**: Windows 10（开发主机）+ WSL2（Linux E2E）+ pytest 差分测试 harness

## 1. 范围

Phase 6-1 将 blast radius、impact 分析和演化智能的计算核心迁移到 Rust，复用已有 GraphStore 的内存索引（CSR HashMap）加速图遍历。Python 层保留为 MCP 工具编排层与回滚兜底。

**涉及**：
- **blast_radius**：递归调用链遍历 + 深度控制 + 环路检测
- **cross_layer_impact**：跨文件/模块聚合
- **get_clone_aware_impact**：依赖 clone detection 结果（Phase 6-2 依赖）
- **function_change_frequency**：基于 git commit 的变更频率统计
- **defect_correlation**：缺陷关联（git log + commit message 关键词匹配 + 时间窗口）
- **hotspot_evolution**：热点演化分析
- **churn_analysis**：churn 分析

**不涉及**（在 Phase 6 其他子任务或已落地）：
- MCP 工具注册（Python 保留编排层）
- Semgrep/RAG 集成（Phase 6-4）
- 向量搜索（Phase 6-3）
- clone detection 自身实现（Phase 6-2）

## 2. 现有资产盘点

### 2.1 Python 计算核心

| 资产 | 路径 | 说明 |
|---|---|---|
| ImpactMixin | `db/db_impact.py`（975 行） | 7 公开方法：`diff_to_symbol` / `blast_radius` / `cross_layer_impact` / `review_readiness_report` / `get_vulnerability_blast_radius` / `get_clone_aware_impact` |
| EvolutionMixin | `db/db_evolution.py`（757 行） | 6 公开方法：`function_change_frequency` / `defect_correlation` / `get_defect_correlation_by_qn` / `hotspot_evolution` / `churn_analysis` / `refresh_evolution_metrics` |

### 2.2 Rust 加速层资产

| 资产 | 路径 | 说明 |
|---|---|---|
| GraphStore | `rust_ext/src/graph.rs` | 已实现 `callers` / `callees` / `search` 内存索引（CSR HashMap），可为 `blast_radius` 提供底层图遍历加速 |
| CallersBatch | `rust_ext/src/graph.rs` | PyO3 懒批对象，降低 Rust→Python 转换开销 |
| SymbolSearchBatch | `rust_ext/src/graph.rs` | 同上，符号搜索懒批 |
| PyImpactChange | `rust_ext/src/metrics.rs` | 计数器结构体，非影响分析逻辑（仅用于 metrics 聚合） |

## 3. 验证矩阵

差分测试（Python baseline vs Rust 实现）共用 `differential-harness-contract.md` 的 harness 框架。

### 3.1 D1: blast_radius 差分测试

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D1.1 | 单层调用链（depth=1） | Rust 与 Python 输出一致 | 字段级 deep diff |
| D1.2 | 多层递归（depth=3，默认） | 受影响符号集合一致 | 集合相等 |
| D1.3 | 环路调用（A→B→A） | 环路检测终止，无死循环 | visited 集合一致 |
| D1.4 | 孤立符号（无 callers） | 返回空 impact set | 退出码 0，空结果 |
| D1.5 | 跨文件调用链 | 文件/模块聚合一致 | cross_layer 字段一致 |

### 3.2 D2: cross_layer_impact 差分测试

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D2.1 | 单文件符号 | 文件级聚合 = 1 | 字段一致 |
| D2.2 | 跨多文件符号 | 文件/模块聚合分布一致 | dict 相等 |
| D2.3 | 跨模块边界 | 模块级 rollup 一致 | dict 相等 |
| D2.4 | 依赖 blast_radius 结果 | 与 D1 输入同源时结果一致 | 引用一致性 |

### 3.3 D3: defect_correlation 差分测试

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D3.1 | 含 "fix"/"bug" 关键词的 commit | 关联窗口内缺陷 commit | 列表相等 |
| D3.2 | 中文 commit message | UTF-8 解码一致 | 字段一致 |
| D3.3 | 时间窗口边界（window_commits=5） | 窗口切片一致 | 列表长度 + 内容相等 |
| D3.4 | git log parser 一致性 | Rust git log parser 与 Python 输出一致 | 行级 diff |
| D3.5 | 无 git 历史符号 | 空结果 | 退出码 0 |

### 3.4 D4: hotspot_evolution / churn_analysis 差分测试

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D4.1 | hotspot_evolution（全量） | 排序结果一致 | list[dict] 相等 |
| D4.2 | hotspot_evolution（module_filter） | 过滤后结果一致 | list[dict] 相等 |
| D4.3 | churn_analysis（90d 默认窗口） | churn 指标一致 | dict 相等 |
| D4.4 | churn_analysis（自定义 time_window） | 时间窗口切片一致 | dict 相等 |

## 4. 迁移策略

按性能收益与依赖顺序分阶段迁移：

1. **优先迁移 `blast_radius`**：复用 GraphStore 图遍历（CSR HashMap），性能收益最大（递归 callers 遍历从 SQL 逐跳查询 → 内存索引短路，预期 5x 加速）。
2. **随后迁移 `cross_layer_impact`**：依赖 `blast_radius` 输出，作为聚合层。
3. **迁移 `defect_correlation`**：依赖 git log 解析，需先实现 Rust 版 git log parser（保留与 Python 一致的编码处理）。
4. **`hotspot_evolution` / `churn_analysis` 保持 Python**：本质是聚合查询，迁移收益较低，且依赖 `refresh_evolution_metrics` 的批量物化逻辑。
5. **`get_clone_aware_impact`**：依赖 Phase 6-2 的 clone detection，本阶段仅保留 Python 入口与契约接口，待 6-2 落地后接入。

**迁移原则**：每个 Rust 实现必须通过 `rollback_config` flag 控制，默认 Python，差分测试稳定后切换。

## 5. 实现计划

### P0: 契约文档（当前）

1. **编写本契约文档** ✅
2. **盘点现有资产**：Python 计算核心 + Rust GraphStore 已齐全
3. **识别缺口**：Rust 版 blast_radius / cross_layer_impact / defect_correlation 尚未实现

### P1: blast_radius Rust 实现 + 差分测试

1. **在 `rust_ext/src/` 实现 `blast_radius`**：复用 GraphStore callers 索引递归遍历
2. **实现环路检测**：visited set 与 Python 一致
3. **PyO3 暴露**：`py_blast_radius(symbol_hash, depth) -> PyDict`
4. **D1 差分测试**：D1.1–D1.5 全通过
5. **`rollback_config` 登记 `rust_blast_radius`**

### P2: cross_layer_impact Rust 实现

1. **在 Rust 层实现跨文件/模块聚合**：基于 blast_radius 输出
2. **PyO3 暴露**：`py_cross_layer_impact(symbol_hash) -> PyDict`
3. **D2 差分测试**：D2.1–D2.4 全通过
4. **`rollback_config` 登记 `rust_cross_layer_impact`**

### P3: defect_correlation Rust 实现（含 git log parser）

1. **实现 Rust 版 git log parser**：与 Python 输出一致（含 UTF-8 中文处理）
2. **实现关键词匹配 + 时间窗口**：与 Python `defect_correlation` 一致
3. **PyO3 暴露**：`py_defect_correlation(symbol_hash, window_commits) -> PyDict`
4. **D3 差分测试**：D3.1–D3.5 全通过
5. **`rollback_config` 登记 `rust_defect_correlation`**

### P4: wire-production + verify + review

1. **wire-production**：Python ImpactMixin/EvolutionMixin 接入 Rust 短路（feature flag 默认 Python）
2. **差分回归**：D1–D4 全套通过
3. **migration-manifest.md §44 Review 清单**填写
4. **close Phase 6-1 任务**

## 6. 验收标准

1. **D1 blast_radius 差分测试**：D1.1–D1.5 全通过
2. **D2 cross_layer_impact 差分测试**：D2.1–D2.4 全通过
3. **D3 defect_correlation 差分测试**：D3.1–D3.5 全通过
4. **D4 hotspot/churn 差分测试**：D4.1–D4.4 全通过（验证保持 Python 的实现不回归）
5. **rollback_config 登记**：`rust_blast_radius` / `rust_cross_layer_impact` / `rust_defect_correlation` 三条 entry 完整
6. **migration-manifest.md §44 Review 清单完整**
7. **Phase 6-1 任务状态机完成 + closed**

## 7. 风险与注意事项

### 7.1 AGENTS.md 强制规则

- **规则 17**：Rust 懒批对象必须在服务边界物化（`CallersBatch` / `SymbolSearchBatch` 在 MCP/daemon 边界需 `list(...)`）
- **规则 22**：代码变更必须同步更新文档（本阶段涉及 `docs/mcp_tools.md` / `migration-manifest.md` / `docs/architecture.md`）
- **规则 8**：图遍历走 Rust 内存索引（CSR HashMap）5x 加速，符合 B-P7b 设计原则
- **规则 1**：提交前必须全量刷新数据库

### 7.2 技术风险

1. **递归图遍历的环路检测**：必须与 Python `visited` set 语义一致，避免 Rust 实现漏检或重复访问导致结果发散。
2. **git log 解析的编码处理**：中文 commit message 在 Windows GBK 默认编码下易触发 `UnicodeDecodeError`（参考 AGENTS.md 规则 26）。Rust 版必须显式 UTF-8 解码，与 Python `subprocess` 输出处理一致。
3. **GraphStore 内存索引与 SQL 查询结果的一致性**：GraphStore 由 db_build 物化，与 db_query 的 SQL 结果可能因刷新时序不同步；差分测试必须在 `cw --refresh-all` 后运行。
4. **PyO3 懒批边界物化**：blast_radius 返回嵌套 dict，不可将 `CallersBatch` 直接交给 JSON 序列化。
5. **defect_correlation 的 commit message 关键词匹配**：Python 用正则，Rust 需用等价 regex crate，注意大小写/Unicode 类别一致性。

## 8. 与其他 Phase 6 子任务的关系

| 子任务 | 交付物 | Phase 6-1 关系 |
|---|---|---|
| **6-1** | **blast radius / impact / 演化热点 Rust 迁移** | **本契约** |
| 6-2 | MinHash/LSH clone detection | `get_clone_aware_impact` 依赖 6-2 输出；本阶段保留 Python 入口 |
| 6-3 | 向量搜索（sqlite-vec vec0 虚拟表） | 无依赖（演化智能不依赖向量） |
| 6-4 | MCP 工具 / Semgrep / RAG 集成 | Python 保留 MCP 编排层，6-4 在其上注册新工具 |

## 9. 下一步

Phase 6-1 完成后，推进 **Phase 6-2**（MinHash/LSH clone detection）：
- 实现 clone 检测后回填 `get_clone_aware_impact` 的 Rust 短路
- 差分测试覆盖 clone + impact 联合场景
- 后续 Phase 6-3 / 6-4 按计划推进
