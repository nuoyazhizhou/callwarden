# Rust Daemon 架构设计

> **⚠ 本文档已过时（Phase 0 步骤 3 标记）**
>
> Enterprise Daemon 架构已正式成为 v10.2 基线（`ad2e308`）。
> 当前权威主设计为 [enterprise-daemon-shared-snapshot-plan.md](enterprise-daemon-shared-snapshot-plan.md)。
>
> 以下描述已过时：
> - ~~"本文档不是 firmware refresh P0 的解法，而是 Call Warden 的长期演进方向"~~ → Enterprise Daemon 已进入正式实施路线图（Phase 0-8）
> - ~~"Rust Daemon 不是 firmware refresh 的 P0 解法，而是面向未来架构设计"~~ → Phase 1 已将 Rust parse 接入主 refresh 路径
> - ~~"建议满足以下任一条件时启动"~~ → 已启动，9 个 Phase 任务已建入数据库
>
> 本文档保留作为历史参考，不再更新。

## 0. 定位与背景

> **本文档不是 firmware refresh P0 的解法，而是 Call Warden 的长期演进方向。**

### 0.1 firmware refresh P0 已通过 Python 算法优化解决

firmware（125 仓库 / 20 万符号）的 "30min+ 卡死" 问题已通过 P0-P13 在
Python 层的算法优化彻底解决，**不需要 Rust Daemon 即可达成可用性能**：

| 优化 | 瓶颈 | 优化前 | 优化后 | 收益 |
|------|------|--------|--------|------|
| P0 | O(M×N) 调用解析 | — | 后缀反向索引 | 调用解析线性化 |
| P7 | `_write_calls_db` 逐条 SQL 热循环 | 42.23s | 0.35s | 120x |
| P8 | full build 触发 FTS5 触发器 | — | 延后 rebuild | 减少 trigger 开销 |
| P9 | C/C++ parser 递归爆栈 | 风险 | 显式栈遍历 | 消除递归 |
| P10 | 阶段耗时不可见 | — | register/parse 拆分 | 定位 parse 12.46s 为真正瓶颈 |
| P11 | GC archive 全量 `os.walk` | 1.85s | `parsed_new==0` 跳过 | 增量刷新 0 GC |
| P12 | clone detect 污染 perf 报告 | — | on-demand 拆出 | refresh 链路净化 |
| P13 | 性能回归无基线 | — | perf baseline + 1.5x 警告 | 防止热循环 SQL 回潮 |

**结论**：firmware refresh 22.1s（P7 后）/ 18.0s（P10 细拆后），其中
tree-sitter parse 占 12.46s（69%）。Python 算法优化空间已基本榨干，
**真正 tree-sitter parse 才是最后值得考虑 Rust 重写的部分**。

### 0.2 本文档的定位

Rust Daemon **不是** firmware refresh 的 P0 解法，而是面向以下三个长期演进场景的
**未来架构设计**：

1. **常驻服务**：当前 CLI 每次启动需加载 Python 解释器 + 打开 SQLite + 加载
   schema，冷启动 ~200ms。Rust Daemon 常驻后查询零冷启动开销，适合 IDE
   低延迟交互场景。

2. **低延迟查询**：当前 Python SQLite 查询 P95 ~2-5ms，受 GIL 和 SQLite 调用
   开销限制。Rust 内存索引（HashMap + CSR 邻接表）可压到 P95 < 1ms，
   适合实时补全 / 实时影响分析。

3. **watch 增量更新**：当前 watcher 触发后走全量 refresh 流程，无法做到
   "文件保存 → 1 秒内符号图更新"。Rust Daemon 的 Staging + Replicator
   架构可实现真正的增量子图重算。

### 0.3 何时启动 Rust Daemon

建议满足以下任一条件时启动：

- tree-sitter parse 成为唯一瓶颈且 Python 多线程已无法优化（当前 12.46s
  对 firmware 34M 行已可接受，但更大仓库可能不行）
- IDE 集成场景对查询延迟有 < 1ms 的硬要求
- watch 增量更新需要秒级响应（当前 watcher 触发后仍需走全量 refresh）

在以上条件未满足前，**优先继续在 Python 层做算法优化**，Rust Daemon 作为
演进方向储备。

---

## 1. 设计目标

### 1.1 性能目标（演进方向，非 firmware P0 硬约束）

> 注：firmware refresh 的"30min+ 卡死"已通过 P0-P13 Python 优化解决
> （当前 18.0s）。下表是 Rust Daemon 演进后的目标，不是 P0 解法。

| 指标 | Python 当前 | Rust Daemon 目标 | 说明 |
|------|-------------|-----------------|------|
| ios_muzoplayer refresh | 11.0s (P7 后) | < 5s | tree-sitter parse 仍占大头 |
| admin refresh | ~5s (P7 后) | < 3s | 小仓库收益有限 |
| firmware refresh | 18.0s (P10 后) | < 10s | parse 12.46s 是真正瓶颈 |
| search_symbols P95 | 5ms | < 1ms | 内存索引零 I/O |
| get_callers P95 | 2ms | < 0.1ms | CSR 邻接表 O(1) |
| get_call_chain_down（5 层）| 50ms | < 1ms | BFS 内存遍历 |

### 1.2 架构目标

- **内存常驻**: 启动时加载符号表/调用图/索引到 Rust 内存，查询零磁盘 I/O
- **主从表分离**: 写入走内存 Staging 表（无索引），后台异步合并到磁盘从表（带索引）
- **增量更新**: 文件变更只重算受影响子图，不全量重算
- **MCP Server 即 Daemon**: 单进程常驻，MCP 协议作为查询接口

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                    Rust Daemon 进程                         │
│                   （MCP Server 即 Daemon）                   │
│                                                             │
│  ┌─────────────────┐  ┌─────────────────────────────────┐  │
│  │  MCP 协议层     │  │  内存索引层（常驻）              │  │
│  │  (FastMCP 兼容) │  │  ┌──────────────────────────┐  │  │
│  │                 │  │  │ SymbolTable              │  │  │
│  │  173 个工具     │──│  │  (qname → SymbolInfo)    │  │  │
│  │  查询走内存     │  │  ├──────────────────────────┤  │  │
│  │  写入走 Staging │  │  │ CallGraph (CSR 邻接表)   │  │  │
│  │                 │  │  │  (caller → callees)      │  │  │
│  └─────────────────┘  │  ├──────────────────────────┤  │  │
│                        │  │ SuffixIndex (后缀反向)   │  │  │
│  ┌─────────────────┐  │  │  (suffix → [symbol_id])  │  │  │
│  │ Watcher (notify)│  │  ├──────────────────────────┤  │  │
│  │  文件变更监听    │  │  │ FTS5 (trigram 子串)     │  │  │
│  │  1s 防抖批处理   │  │  │  (SQLite 内存虚拟表)     │  │  │
│  └────────┬────────┘  │  ├──────────────────────────┤  │  │
│           │           │  │ VecIndex (sqlite-vec)    │  │  │
│           ▼           │  │  (向量语义搜索)          │  │  │
│  ┌─────────────────┐  │  └──────────────────────────┘  │  │
│  │ Staging 表      │  │  内存预算: ~512MB（20万符号）  │  │
│  │  (内存 :memory:)│  └─────────────────────────────────┘  │
│  │                 │                                        │
│  │  symbols_staging│  ┌─────────────────────────────────┐  │
│  │  calls_staging  │  │  异步 Replicator 线程            │  │
│  │  (无索引，纯写) │  │  (每 5 秒一次)                   │  │
│  └────────┬────────┘  │                                 │  │
│           │           │  1. 读取 Staging 表             │  │
│           │           │  2. 差分计算（受影响子图）       │  │
│           ▼           │  3. 局部 depth 重算（BFS）      │  │
│  ┌─────────────────┐  │  4. 写入磁盘从表（带索引）      │  │
│  │ 磁盘从表        │  │  5. 更新内存索引                │  │
│  │  (SQLite 文件)  │  │  6. 清空 Staging               │  │
│  │                 │  └─────────────────────────────────┘  │
│  │  symbols (索引) │                                        │
│  │  calls (索引)   │  ┌─────────────────────────────────┐  │
│  │  call_versions  │  │  rusqlite 连接                   │  │
│  │  FTS5           │  │  (WAL 模式，持久化)              │  │
│  │  vec0           │  └─────────────────────────────────┘  │
│  └─────────────────┘                                        │
└─────────────────────────────────────────────────────────────┘
         ▲
         │ stdio / SSE
         │
┌────────┴────────┐
│  MCP 客户端     │
│  (Trae/Cursor)  │
└─────────────────┘
```

## 3. 核心数据结构（Rust 内存层）

### 3.1 SymbolTable（符号表）

```rust
/// 符号信息（紧凑布局，SoA 设计提升缓存命中）
#[derive(Clone, Debug)]
pub struct SymbolInfo {
    pub id: u32,                    // 符号 ID（对应 symbols.id）
    pub file_instance_id: u32,      // 文件实例 ID
    pub kind: SymbolKind,           // 类型（fn/method/class/...）
    pub qualified_name: Rc<str>,    // 完全限定名（Rc 共享，避免重复分配）
    pub simple_name: Rc<str>,       // 简名
    pub name: Rc<str>,              // 原始 name
    pub start_line: u32,
    pub end_line: u32,
    pub depth: u16,                 // 调用深度
    pub content_hash: [u8; 16],     // 内容哈希（前 16 字节）
}

pub struct SymbolTable {
    /// id → SymbolInfo（Vec 紧凑存储，O(1) 索引访问）
    pub by_id: Vec<SymbolInfo>,
    /// qualified_name → symbol_id（HashMap，查询用）
    pub by_qname: HashMap<Rc<str>, u32>,
    /// simple_name → [symbol_id]（同名可能有多个）
    pub by_simple_name: HashMap<Rc<str>, Vec<u32>>,
    /// file_instance_id → [symbol_id]（文件维度索引）
    pub by_file: HashMap<u32, Vec<u32>>,
}
```

**内存预算**（20 万符号）:
- `by_id`: 20万 × ~80B = 16MB
- `by_qname`: 20万 × ~50B（key + value） = 10MB
- `by_simple_name`: ~15万 unique name × ~40B = 6MB
- `by_file`: ~5000 文件 × ~32B = 160KB
- **合计: ~32MB**

### 3.2 CallGraph（调用图，CSR 压缩稀疏行）

```rust
/// 调用边（紧凑存储）
#[derive(Clone, Copy, Debug)]
pub struct CallEdge {
    pub caller_id: u32,
    pub callee_id: u32,         // 0 表示未解析
    pub callee_qname: Option<Rc<str>>,  // 未解析时存名字
    pub call_line: u32,
    pub is_cross_file: bool,
}

pub struct CallGraph {
    /// 所有调用边（紧凑 Vec，提升缓存命中）
    pub edges: Vec<CallEdge>,
    /// 正向邻接表: caller_id → [edge_index]（CSR 格式）
    /// caller_id 作为下标，值是该 caller 的边在 edges 中的起始位置和长度
    pub forward: Vec<(usize, usize)>,  // (start, len)
    /// 反向邻接表: callee_id → [caller_id]（用于 get_callers）
    pub backward: HashMap<u32, Vec<u32>>,
    /// 顶层节点（无 caller 的函数，用于 topo 排序）
    pub roots: Vec<u32>,
}
```

**内存预算**（200 万调用边）:
- `edges`: 200万 × 24B = 48MB
- `forward`: 20万 × 8B = 1.6MB
- `backward`: 200万 × (4+4)B = 16MB
- **合计: ~66MB**

### 3.3 SuffixIndex（后缀反向索引）

```rust
pub struct SuffixIndex {
    /// 后缀 → [symbol_id]（用于策略 2/4 的后缀匹配）
    /// 复用 P0 优化的设计，但用 Rust HashMap
    pub index: HashMap<Rc<str>, Vec<u32>>,
}
```

**内存预算**（20 万符号，平均 4 段 → 80 万后缀）:
- 80万 × ~40B = **32MB**

### 3.4 内存总预算

| 数据结构 | 内存 | 说明 |
|---------|------|------|
| SymbolTable | 32MB | 符号表 + 3 个索引 |
| CallGraph | 66MB | CSR 邻接表 |
| SuffixIndex | 32MB | 后缀反向索引 |
| FTS5（SQLite 内存表）| ~100MB | trigram 全文索引 |
| VecIndex（sqlite-vec）| ~200MB | 向量嵌入 |
| 其他（path/imports/...）| ~50MB | 辅助结构 |
| **合计** | **~480MB** | 远低于 1GB 预算 |

## 4. 主从表设计

### 4.1 Staging 表（内存主表，无索引极速写）

```sql
-- ATTACH DATABASE ':memory:' AS staging
CREATE TABLE staging.symbols_staging (
    -- 与 symbols 表相同的 schema，但没有任何索引
    id INTEGER PRIMARY KEY,
    file_instance_id INTEGER,
    symbol_hash TEXT,
    name TEXT,
    kind TEXT,
    qualified_name TEXT,
    -- ... 其他字段
    change_type TEXT  -- 'insert' / 'update' / 'delete'
);

CREATE TABLE staging.calls_staging (
    caller_id INTEGER,
    callee_id INTEGER,
    callee_name TEXT,
    callee_qualified TEXT,
    call_line INTEGER,
    is_cross_file INTEGER,
    change_type TEXT
);
```

**写入策略**: watcher 检测到文件变更 → tree-sitter 解析 → 直接 INSERT 到 staging 表（无 B-Tree 分裂开销，纯顺序写）。

### 4.2 磁盘从表（带索引，查询用）

保持现有 `symbols` / `calls` / `call_versions` 表结构不变，所有索引保留。FTS5 + vec0 也在磁盘从表。

### 4.3 异步 Replicator 线程

```rust
/// 后台 Replicator 线程（每 5 秒执行一次）
fn replicator_loop(
    staging_conn: &Connection,      // 内存 DB 连接
    disk_conn: &Connection,          // 磁盘 DB 连接
    mem_symbols: &RwLock<SymbolTable>,
    mem_callgraph: &RwLock<CallGraph>,
) {
    loop {
        thread::sleep(Duration::from_secs(5));

        // 1. 读取 Staging 表
        let changes = staging_conn.prepare(
            "SELECT * FROM symbols_staging WHERE change_type != 'synced'"
        )?;
        let call_changes = staging_conn.prepare(
            "SELECT * FROM calls_staging WHERE change_type != 'synced'"
        )?;

        if changes.is_empty() && call_changes.is_empty() {
            continue;
        }

        // 2. 差分计算（仅受影响符号）
        let affected_symbols = compute_affected_symbols(&changes);
        let affected_callers = compute_affected_callers(&call_changes);

        // 3. 事务性写入磁盘从表
        disk_conn.execute("BEGIN TRANSACTION")?;
        apply_symbol_changes(disk_conn, &changes)?;
        apply_call_changes(disk_conn, &call_changes)?;

        // 4. 局部 depth 重算（仅受影响子图 BFS）
        recompute_depth_local(disk_conn, &affected_symbols, &mem_callgraph)?;

        // 5. 提交磁盘事务
        disk_conn.execute("COMMIT")?;

        // 6. 更新内存索引（写锁）
        {
            let mut symbols = mem_symbols.write();
            for change in &changes {
                symbols.apply_change(change);
            }
        }
        {
            let mut callgraph = mem_callgraph.write();
            for change in &call_changes {
                callgraph.apply_change(change);
            }
        }

        // 7. 清空 Staging
        staging_conn.execute("DELETE FROM symbols_staging")?;
        staging_conn.execute("DELETE FROM calls_staging")?;
    }
}
```

## 5. 增量更新策略

### 5.1 单文件变更（最常见场景）

```
开发者修改 src/foo.py（新增一个函数调用）
│
├── Step 1: Watcher 检测（1s 防抖）
│   └── notify 回调 → 加入 pending_changes dict
│
├── Step 2: 解析（< 100ms）
│   ├── tree-sitter 解析 src/foo.py
│   ├── content_hash 比对 → 确认变更
│   ├── 得到 new_symbols（~10 个）+ new_calls（~50 条）
│   └── 写入 Staging 表（< 1ms，内存无索引）
│
├── Step 3: Replicator 异步合并（下次 tick，< 100ms）
│   ├── 差分: new_symbols vs old_symbols（src/foo.py 的旧符号）
│   ├── 受影响符号: src/foo.py 的符号（~10 个）
│   ├── 受影响调用者: 调用 src/foo.py 符号的其他文件（~50 个符号）
│   ├── 局部 depth 重算: BFS 从受影响节点出发，仅重算子图
│   ├── 写入磁盘从表（带索引，~10ms）
│   └── 更新内存索引（SymbolTable + CallGraph）
│
└── Step 4: 查询立即可见
    └── MCP 工具查询走内存索引（纳秒级）
```

**关键**: 20 万符号里改 1 个文件，只动 ~100 个节点的子图，不是 20 万全扫。

### 5.2 多文件批量变更（git pull / checkout）

```
开发者 git pull，100 个文件变更
│
├── Step 1: Watcher 检测（1s 防抖）
│   └── 100 个文件加入 pending_changes
│
├── Step 2: 批量解析（并行，< 5s）
│   ├── Rayon 并行解析 100 个文件（par_iter）
│   ├── 得到 new_symbols（~1000 个）+ new_calls（~5000 条）
│   └── 批量写入 Staging 表
│
├── Step 3: Replicator 合并（< 1s）
│   ├── 差分计算（1000 个符号）
│   ├── 受影响子图（~5000 节点）
│   ├── 局部 depth 重算
│   ├── 批量写入磁盘从表
│   └── 批量更新内存索引
│
└── Step 4: 查询可见
```

### 5.3 深度增量重算算法

```rust
/// 局部 depth 重算（仅受影响子图）
fn recompute_depth_local(
    affected_symbols: &HashSet<u32>,
    callgraph: &CallGraph,
    depth_cache: &mut HashMap<u32, u16>,
) {
    // 1. 收集受影响符号的反向可达闭包（所有可能受影响的 caller）
    let mut to_recompute: HashSet<u32> = affected_symbols.clone();
    let mut queue: VecDeque<u32> = affected_symbols.iter().copied().collect();
    while let Some(sym_id) = queue.pop_front() {
        // 沿反向边（caller → callee）向上找所有调用者
        if let Some(callers) = callgraph.backward.get(&sym_id) {
            for &caller_id in callers {
                if to_recompute.insert(caller_id) {
                    queue.push_back(caller_id);
                }
            }
        }
    }

    // 2. 对受影响集合做拓扑排序
    let topo_order = topological_sort(&to_recompute, callgraph);

    // 3. 按拓扑序重算 depth
    for sym_id in topo_order {
        let max_callee_depth = callgraph
            .forward_callees(sym_id)
            .map(|c| depth_cache.get(&c).copied().unwrap_or(0))
            .max()
            .unwrap_or(0);
        depth_cache.insert(sym_id, max_callee_depth + 1);
    }
}
```

## 6. MCP Server 即 Daemon

### 6.1 进程架构

```
Rust Daemon 进程
├── 主线程: MCP 协议处理（stdio/sse）
│   ├── 接收 MCP 请求（search_symbols / get_callers / ...）
│   ├── 查询内存索引（RwLock 读锁）
│   └── 返回结果
├── Watcher 线程: 文件监听（notify crate）
│   ├── 事件回调 → pending_changes dict
│   └── 1s 防抖 → tree-sitter 解析 → Staging 表
├── Replicator 线程: 异步合并
│   └── 每 5 秒: Staging → 磁盘从表 + 内存索引更新
└── rusqlite 连接池
    ├── 内存 DB 连接（:memory:，Staging 表）
    └── 磁盘 DB 连接（文件，带索引）
```

### 6.2 MCP 工具下沉策略

173 个 MCP 工具分三类处理:

| 类别 | 数量 | 处理方式 |
|------|------|---------|
| 查询类 | 75 | **下沉到 Rust 内存层**（search/callers/callees/chain/topo）|
| 分析类 | 41 | **部分下沉**（impact/blast_radius 走内存图，clone 走 SQLite）|
| 写操作类 | 57 | **保留 Python**（task/rule/guardrail 写操作走 Python，避免 Rust 重复实现）|

### 6.3 查询路径（读操作）

```rust
// MCP 工具: search_symbols
fn search_symbols(query: &str, kind: Option<&str>, limit: usize) -> Vec<SearchResult> {
    let symbols = MEM_SYMBOLS.read();  // RwLock 读锁（纳秒级）

    // 1. FTS5 trigram 查询（SQLite 内存虚拟表）
    let fts_results = mem_fts_conn.query(
        "SELECT rowid FROM symbols_fts WHERE symbols_fts MATCH ?",
        &[build_trigram_query(query)],
    )?;

    // 2. 从内存 SymbolTable 补全字段
    let mut results = Vec::with_capacity(limit);
    for rowid in fts_results.iter().take(limit) {
        let sym = symbols.by_id.get(*rowid as usize)?;
        if kind.is_some_and(|k| sym.kind != k) { continue; }
        results.push(SearchResult {
            qualified_name: sym.qualified_name.clone(),
            file_path: get_file_path(sym.file_instance_id),
            // ...
        });
    }
    Ok(results)
}

// MCP 工具: get_callers
fn get_callers(callee_name: &str) -> Vec<CallerInfo> {
    let callgraph = MEM_CALLGRAPH.read();

    // 纯内存查表，零 SQL
    let caller_ids = callgraph
        .backward
        .get(&callee_name)  // 或 by callee_id
        .unwrap_or(&vec![]);

    caller_ids.iter().map(|&caller_id| {
        let sym = MEM_SYMBOLS.read().by_id[caller_id as usize];
        CallerInfo { name: sym.name.clone(), file: sym.file_path.clone() }
    }).collect()
}
```

### 6.4 写入路径（写操作）

```
MCP 工具 refresh_file(path)
├── 1. tree-sitter 解析文件（Rust，< 50ms）
├── 2. content_hash 比对
│   ├── 未变 → 直接返回（< 1ms）
│   └── 变更 → 继续
├── 3. 写入 Staging 表（内存 :memory:，< 1ms）
├── 4. 立即返回（不等 Replicator）
└── 5. Replicator 后台异步合并（5 秒内完成）

注意: 查询在 Replicator 合并前可能看到旧数据
     → 可接受（最终一致性，5 秒内收敛）
     → 或用合并视图: SELECT * FROM staging UNION ALL SELECT * FROM main
```

## 7. 启动流程

```
cw server（启动 Rust Daemon）
│
├── Step 1: 打开磁盘 DB 连接（< 100ms）
│   └── 检测 schema 版本，必要时迁移
│
├── Step 2: 从磁盘 DB 加载到内存（< 10s）
│   ├── SELECT * FROM symbols → SymbolTable（20 万行，~3s）
│   ├── SELECT * FROM calls → CallGraph（200 万行，~5s）
│   ├── 构建 SuffixIndex（内存计算，~1s）
│   └── ATTACH ':memory:' AS staging → 创建 Staging 表
│
├── Step 3: 启动 Watcher 线程
│   └── notify 监听 workspace_root
│
├── Step 4: 启动 Replicator 线程
│   └── 每 5 秒检查 Staging
│
└── Step 5: 启动 MCP 协议层（stdio/sse）
    └── 等待 MCP 客户端连接
```

## 8. 与现有架构的兼容

### 8.1 CLI 兼容

| 场景 | MCP 激活时 | MCP 未激活 |
|------|-----------|-----------|
| 查询 | 走 Rust Daemon 内存索引 | 走 SQLite（降级，慢但可用）|
| 写入 | 走 Daemon 的 Staging 表 | 走 CLI SQLite 写（现有行为）|
| 刷新 | 走 Daemon 的 Watcher | 走 CLI `cw --refresh`（现有行为）|

### 8.2 渐进迁移

```
阶段 1: Rust Daemon 作为 MCP Server 旁路运行
├── 现有 Python MCP Server 保留
├── 新增 Rust Daemon 进程，通过共享内存/Socket 提供查询加速
└── CLI 不变

阶段 2: Rust Daemon 接管 MCP Server
├── Rust Daemon 直接实现 MCP 协议
├── Python MCP Server 降级为 fallback
└── 查询走 Rust，写操作仍走 Python（过渡期）

阶段 3: Rust Daemon 完全接管
├── 所有读写都在 Rust Daemon
├── Python 仅保留 CLI 命令解析和 task/rule 等业务逻辑
└── SQLite 纯做 dump
```

## 9. 技术选型

| 组件 | 选型 | 理由 |
|------|------|------|
| 语言 | Rust | 性能 + 内存安全 + 与现有 rust_ext 复用 |
| SQLite 绑定 | rusqlite | 成熟、广泛使用 |
| MCP SDK | 自实现轻量协议层 | FastMCP 是 Python，Rust 需自实现 stdio/sse |
| tree-sitter | tree-sitter Rust 绑定 | 复用现有 grammar |
| 文件监听 | notify crate | 替代 Python watchdog |
| 并行 | rayon | 数据并行，无 GIL 限制 |
| 序列化 | serde + msgpack | MCP 协议用 JSON-RPC，内部用 msgpack |
| 内存索引 | std::HashMap + Vec | 标准库足够，不引入第三方 |

## 10. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Rust MCP 协议实现不完整 | 阶段 1 先旁路，Python MCP 保持可用 |
| 增量更新逻辑复杂 | 先实现全量 + Staging，再加增量 |
| 内存数据与 SQLite 不一致 | Replicator 事务性 + 启动时全量重建 |
| 崩溃丢数据 | Staging 表用 SQLite :memory:，但磁盘从表是真相源；崩溃恢复走 SQLite WAL |
| 173 个工具迁移工作量大 | 优先迁移高频查询（search/callers/callees/chain），其余保留 Python |

## 11. 实施路线图

### Phase 1: Rust Daemon 骨架（验证可行性）
- [ ] rusqlite 连接管理 + ATTACH ':memory:'
- [ ] SymbolTable / CallGraph 内存结构
- [ ] 从磁盘 DB 加载到内存
- [ ] 基准查询: search_symbols / get_callers（对比 Python 性能）

### Phase 2: Staging + Replicator
- [ ] Staging 表设计 + 写入
- [ ] Replicator 后台线程
- [ ] 增量 depth 重算

### Phase 3: Watcher + 增量更新
- [ ] notify crate 替代 watchdog
- [ ] 增量解析 + Staging 写入
- [ ] 多文件批量变更处理

### Phase 4: MCP 协议层
- [ ] MCP stdio 协议实现
- [ ] 高频查询工具下沉（search/callers/callees/chain/topo/impact）
- [ ] 与 Python MCP Server 共存验证

### Phase 5: 完整迁移
- [ ] 所有查询工具下沉
- [ ] 写操作迁移
- [ ] CLI 适配

## 12. 待决策项（已验证）

1. **Rust MCP 协议实现**: ✅ 使用官方 `rmcp` crate (wenhaozhao/mcp-rust-sdk)，基于 tokio 异步运行时
2. **tree-sitter grammar 复用**: 待 Phase 1 验证。Rust tree-sitter 绑定需要重新编译 grammar，但语法定义文件可复用
3. **FTS5 / vec0 在内存 DB 的可用性**: ✅ rusqlite 通过 `features = ["bundled"]` 编译自带 FTS5 的 SQLite；sqlite-vec 是 loadable extension，通过 `load_extension` 加载
4. **Python ↔ Rust 通信**: 阶段 1 用 PyO3（复用现有 rust_ext 基础设施），阶段 2+ 用 MCP 协议直连
