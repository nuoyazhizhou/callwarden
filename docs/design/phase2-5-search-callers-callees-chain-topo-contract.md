# Phase 2-5 契约：搜索、callers/callees、call-chain 与拓扑

> **范围**：为已存在的 Rust GraphStore 查询方法建立正式差分测试，并接入剩余未接线的函数。
>
> **背景**：与 Phase 2-1~2-4（写入路径下沉）不同，Phase 2-5 是**读取路径**。
> Rust `GraphStore`（`rust_ext/src/graph.rs`）早已实现全部核心查询方法，
> 并通过 B-P7b 短路模式部分接入生产（`get_callers`/`get_callees`/`search_symbols`/`detect_cycles`）。
> Phase 2-5 的主要工作是：
> 1. 为已接线的函数建立正式差分测试
> 2. 接入未接线的函数（`get_topological_order`、`get_call_chain_up/down`）
> 3. 处理语义不一致（`get_topological_order` 的 ORDER BY depth vs Kahn 算法）

## 1. Python 真相源盘点

### 1.1 已接入 Rust 短路的函数

| 函数 | Python 文件:行 | Rust 方法 | 状态 |
|---|---|---|---|
| `get_callers` | `db/db_query.py:273` | `GraphStore.get_callers` (graph.rs:652) | ✅ B-P7b 短路 |
| `get_callees` | `db/db_query.py:363` | `GraphStore.get_callees` (graph.rs:712) | ✅ B-P7b 短路 |
| `search_symbols` | `db/db_query.py:622` | `GraphStore.search_symbols` (graph.rs:793) | ✅ B-P7b 短路（fallback 1） |
| `detect_cycles` | `analyzers/call_chain.py:382` | `GraphStore.detect_cycles` (graph.rs:985) | ✅ B-P7b 短路 |

### 1.2 未接入 Rust 短路的函数

| 函数 | Python 文件:行 | Rust 方法 | 语义差异 |
|---|---|---|---|
| `get_topological_order` | `db/db_query.py:260` | `GraphStore.get_topological_order` (graph.rs:929) | Python: `ORDER BY depth ASC`；Rust: Kahn 算法 |
| `get_call_chain_up` | `analyzers/call_chain.py:20` | 无直接对应 | Python: BFS on `call_versions` 表 |
| `get_call_chain_down` | `analyzers/call_chain.py:93` | `GraphStore.get_call_chain_down` (graph.rs:858) | Python: BFS on `call_versions` 表含 `is_current=1`；Rust: BFS on CSR 内存图 |

### 1.3 不在范围

- `semantic_search` / `find_similar_functions`（向量搜索，Phase 6 范围）
- `get_symbol`（含多 Mixin 注入 issues/test_cases/evolution，迁移复杂，留待后续）
- `find_symbol_at_line` / `find_symbols_at_lines`（行号→符号映射，非图查询）
- FTS5 索引管理（`_disable_fts_triggers` / `_rebuild_and_enable_fts`，写入路径，已在 Phase 2-2 覆盖）

## 2. API 契约

### 2.1 已暴露的 PyO3 API（无需新增）

以下 Rust 方法已在 `GraphStore` pyclass 中暴露，无需新增 PyO3 注册：

```rust
// graph.rs - GraphStore #[pymethods]
pub fn get_callers(&self, callee_name: &str, qualified_name: Option<&str>) -> Option<CallersBatch>
pub fn get_callees(&self, caller_name: &str, qualified_name: Option<&str>) -> Vec<Bound<PyAny>>
pub fn search_symbols(&self, query: &str, kind: Option<&str>, limit: Option<usize>) -> SymbolSearchBatch
pub fn get_call_chain_down(&self, qualified_name: &str, max_depth: usize) -> Vec<Bound<PyAny>>
pub fn get_topological_order(&self) -> Vec<String>
pub fn detect_cycles(&self) -> Vec<Vec<String>>
```

### 2.2 Python 调用路径

Python 通过 `_get_graph_store()` 获取 `GraphStore` 实例（分级懒加载）：
1. 首次查询：加载 symbols（`load_symbols_from_sqlite`）
2. 后台线程：加载 calls（`load_calls_from_sqlite`）
3. `load_state() == "graph_ready"` 时短路生效

## 3. 行为契约（差分测试矩阵）

### 3.1 get_callers（Q1-Q6）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| Q1 | 短名匹配（无 QN） | SQL: `WHERE callee_name=?` | CSR reverse 遍历 | 两端 callers 列表一致（caller_name/callee_name/call_line） |
| Q2 | 显式 QN 精确匹配 | SQL: `WHERE callee_id=(SELECT id WHERE qualified_name=?)` | CSR: qname→id→reverse edges | 两端 callers 列表一致 |
| Q3 | 显式 QN 未找到 | 返回空 [] | 返回空 [] | 两端均返回空 |
| Q4 | 自动 QN 识别 + 降级 | 含 `.`/`::` → QN 查找 → 空时降级短名 | 同 | 两端 callers 列表一致 |
| Q5 | 无调用者 | 返回空 [] | 返回空 [] | 两端均返回空 |
| Q6 | 多调用者（3+ 个） | 返回所有 callers | 返回所有 callers | 两端 callers 列表一致 |

### 3.2 get_callees（C1-C6）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| C1 | 短名匹配（无 QN） | SQL: `WHERE s.name=?` | CSR forward 遍历 | 两端 callees 列表一致 |
| C2 | 显式 QN 精确匹配 | SQL: `WHERE s.qualified_name=?` | CSR: qname→id→forward edges | 两端 callees 列表一致 |
| C3 | 显式 QN 未找到 | 返回空 [] | 返回空 [] | 两端均返回空 |
| C4 | 自动 QN 识别 + 降级 | 含 `.`/`::` → QN 查找 → 空时降级短名 | 同 | 两端 callees 列表一致 |
| C5 | 无被调用者 | 返回空 [] | 返回空 [] | 两端均返回空 |
| C6 | 多被调用者（3+ 个） | 返回所有 callees | 返回所有 callees | 两端 callees 列表一致 |

### 3.3 search_symbols（S1-S5）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| S1 | 精确匹配 | FTS5 trigram | memchr 子串 | 两端结果一致（name/kind/qualified_name） |
| S2 | 部分匹配 | FTS5 trigram | memchr 子串 | 两端结果一致 |
| S3 | kind 过滤 | FTS5 + WHERE kind=? | memchr + kind 过滤 | 两端结果一致 |
| S4 | limit 限制 | LIMIT ? | take(limit) | 两端结果数一致 |
| S5 | 无匹配 | 返回空 [] | 返回空 [] | 两端均返回空 |

### 3.4 detect_cycles（D1-D4）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| D1 | 无环 | 返回空 [] | 返回空 [] | 两端均返回空 |
| D2 | 单环（A→B→A） | 返回 [[A,B]] | 返回 [[A,B]] | 两端环列表一致 |
| D3 | 多环 | 返回所有环 | 返回所有环 | 两端环列表一致 |
| D4 | 自环（A→A） | 返回 [[A]] | 返回 [[A]] | 两端环列表一致 |

### 3.5 get_topological_order（T1-T3）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| T1 | 空图 | 返回空 [] | 返回空 [] | 两端均返回空 |
| T2 | 线性链（A→B→C） | ORDER BY depth: [C, B, A] | Kahn: [C, B, A] | 两端顺序一致（底层在前） |
| T3 | 菱形（A→B, A→C, B→D, C→D） | ORDER BY depth | Kahn | 两端 depth 一致（顺序可能不同） |

**注意**：T3 的差分断言用 depth 集合而非顺序，因 Kahn 算法对同 depth 节点的顺序不保证一致。

## 4. 预期差异

| # | Python 行为 | Rust 行为 | 说明 |
|---|---|---|---|
| P1 | `get_topological_order` 用 `ORDER BY depth ASC, start_line ASC` | Kahn 算法（入度 0 入队 → BFS） | 语义不完全一致：Python 按 depth 字段排序，Rust 按拓扑序。差分测试用 depth 集合断言 |
| P2 | `get_call_chain_down` 走 `call_versions` 表（含 `is_current=1` 过滤） | CSR 内存图（假设已加载 current） | 数据源不同，差分测试需确保两端数据一致 |
| P3 | `search_symbols` 三级路由（FTS5→Rust→LIKE） | memchr 子串扫描 | FTS5 trigram 与 memchr 子串匹配语义可能不同（trigram 对 < 3 字符查询有特殊处理） |
| P4 | `detect_cycles` 三色 DFS on `call_versions` 表 | 三色 DFS on CSR 内存图 | 算法相同，数据源不同 |
| P5 | `get_callers`/`get_callees` 返回 dict 列表 | 返回 `CallersBatch`/`Vec<PyAny>` 懒批 | 懒批在服务边界必须物化（AGENTS.md 规则 17） |

## 5. 实现计划

### 5.1 差分测试（主要工作）

- 新建 `tests/test_phase2_5_behavioral_diff.py`：
  - `TestGetCallersDiff`：Q1-Q6
  - `TestGetCalleesDiff`：C1-C6
  - `TestSearchSymbolsDiff`：S1-S5
  - `TestDetectCyclesDiff`：D1-D4
  - `TestGetTopologicalOrderDiff`：T1-T3
- Python 路径走 `db_query.py`/`call_chain.py` 真实方法（通过 `_MinimalDb` 模拟）
- Rust 路径走 `GraphStore` pyclass（`load_from_sqlite` → 查询方法）
- 两端用同一 DB 文件初始化，确保数据一致

### 5.2 wire-production

- `get_topological_order`：在 `db_query.py:260` 添加 Rust 短路（`store.get_topological_order()`）
- `get_call_chain_down`（analyzers 版）：评估是否接入 Rust CSR 路径，或保留 SQL 路径
- `get_call_chain_up`：Rust 无直接对应，暂不接入（留待后续）
- 所有接入读取 `rollback_config.is_feature_rolled_back()` 判断

### 5.3 rollback config

- `cw rollback register`：feature=`rust_graph_query`, phase=2

### 5.4 migration-manifest 更新

- Phase 2-5 行状态更新为 `✅(behavioral)`

## 6. 风险与注意事项

### 6.1 GraphStore 加载时序

`_get_graph_store()` 是分级懒加载：
1. 首次查询：加载 symbols（同步，阻塞）
2. 后台线程：加载 calls（异步，`load_state() != "graph_ready"` 时需等待）

差分测试中需确保 `store.load_state() == "graph_ready"` 后再查询，否则 Rust 返回空。

### 6.2 懒批对象物化

`CallersBatch` / `SymbolSearchBatch` 是 PyO3 懒批对象，必须在服务边界执行 `list(result)`。
当前 `get_callers` 已正确处理（line 309 `materialized = list(rust_callers)`），
但 `get_callees` line 406 直接 `return rust_callees` 可能违反 AGENTS.md 规则 17。

### 6.3 FTS5 与 memchr 语义差异

FTS5 trigram tokenizer 对 < 3 字符查询有特殊处理（可能返回空或全表）。
memchr 子串扫描对所有查询长度行为一致。差分测试 S1-S5 需用 ≥ 3 字符查询避免此差异。

### 6.4 get_topological_order 语义决策

Python `ORDER BY depth` 依赖 `symbols.depth` 字段（由 `_build_depth` 预计算）。
Rust Kahn 算法是真正的拓扑排序。两者在 DAG 上结果一致，但在以下场景可能不同：
- 多入口图（多个入度 0 节点）：Kahn 顺序不确定，Python 按 start_line 排序
- 同 depth 节点：Python 按 start_line 排序，Kahn 按 BFS 顺序

差分测试 T3 用 depth 集合断言，不断言顺序。

## 7. 验收标准

- [ ] `tests/test_phase2_5_behavioral_diff.py` 差分测试全部通过
- [ ] 现有 Phase 1 + Phase 2-1~2-4 测试不受影响
- [ ] `cw rollback register` 登记新 feature（feature=rust_graph_query）
- [ ] migration-manifest.md 更新 Phase 2-5 行状态为 `✅(behavioral)`
