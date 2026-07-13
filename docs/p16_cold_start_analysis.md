# P16 架构冷启动分析与内存自适应方案

> **⚠️ 方向已废弃（review 反馈）**
>
> 本文档原设计的"内存主表 + SQLite 从表"架构（GraphStore 作为主表，SQLite 作为 dump 从表）**已废弃**。
>
> Call Warden 已走在更合理的混合架构上：
> - **SQLite/CAS 负责持久化真相**（不可降级，是 source of truth）
> - **Rust GraphStore/CSR 负责内存查询**（可重建，是缓存加速层）
> - **daemon 负责共享与发布**（多用户共享同一 GraphStore 实例）
>
> 原方向的问题：
> 1. 把 GraphStore 当作主表、SQLite 当作从表，颠倒了真相源
> 2. 内存数据丢失后需要从 dump 恢复，冷启动 140s 不可接受
> 3. 忽略了 CAS 去重（多工作区复用）才是真正的价值点
>
> 保留本文档作为历史参考，但**不要按此方向实施**。
> 正确方向见 Enterprise Daemon 架构演进（`T-1783830945165-1acc`）。

---

## 1. P16 架构定义（已废弃，仅作参考）

**P16 = 常驻内存 GraphStore（Rust CSR）+ SQLite 作为 dump 存储**

```
┌─────────────────────────────────────────────────────┐
│  写入路径（极快）                                     │
│  Python → GraphStore（内存 CSR）→ 直接修改内存        │
│         → 后台线程定期 dump 到 SQLite                 │
├─────────────────────────────────────────────────────┤
│  查询路径（极快）                                     │
│  MCP/CLI → GraphStore（内存 CSR）→ O(1) 查询         │
│         → 降级到 SQLite（有索引）当 GraphStore 失效   │
├─────────────────────────────────────────────────────┤
│  冷启动路径                                           │
│  启动 → SQLite WAL checkpoint → GraphStore.load()   │
│       → 从 SQLite 读取到内存 CSR（~30-120s）          │
└─────────────────────────────────────────────────────┘
```

**关键**：Call Warden 已有 Rust GraphStore（[rust_ext/src/graph.rs](file:///c:/git_work/callwarden/rust_ext/src/graph.rs)），P16 是将其从"懒加载查询加速层"升级为"常驻写入主路径"。

---

## 2. 冷启动查询性能分析

### 2.1 冷启动流程

1. 打开 SQLite 连接（<1s）
2. `PRAGMA wal_checkpoint(PASSIVE)` 确保 WAL 数据刷入主文件（~1s）
3. `GraphStore.load_from_sqlite(db_path)` 从 SQLite 加载到内存 CSR

### 2.2 加载阶段耗时估算（10M 符号，70M 调用边）

基于现有 [graph.rs:300-488](file:///c:/git_work/callwarden/rust_ext/src/graph.rs#L300-L488) 实现分析：

| 阶段 | 操作 | 数据量 | 估算耗时 | 瓶颈 |
|------|------|--------|----------|------|
| 1a | 加载 file_instances | 2M 行 | ~5s | 磁盘 I/O + String 分配 |
| 1b | 加载 symbols | 10M 行 | ~30-50s | 磁盘 I/O + string pool 构建 |
| 1c | 构建 by_qname 排序数组 | 10M 排序 | ~3s | CPU 排序 |
| 1d | 构建 by_simple_name HashMap | 10M 插入 | ~5s | HashMap 哈希 |
| 1e | 构建 by_file HashMap | 10M 插入 | ~3s | HashMap 哈希 |
| 2a | 加载 calls | 70M 行 | ~60-90s | 磁盘 I/O + Vec 扩容 |
| 2b | 构建 CSR forward（按 caller_id 排序） | 70M 排序 | ~10s | CPU 排序 |
| 2c | 构建 CSR backward（按 callee_id 排序） | 70M 排序 | ~10s | CPU 排序 |
| 2d | 构建 by_callee_name HashMap | 70M 插入 | ~15s | HashMap 哈希 |
| 2e | 构建 roots | 遍历 | ~1s | CPU |
| **总计** | | | **~140-190s** | **磁盘 I/O 为主** |

**对比 P12 冷启动**：P12 无 GraphStore 加载，直接 SQL 查询，冷启动 <1s，但每次查询 1-10ms（走索引）。

### 2.3 冷启动后的查询性能

| 查询类型 | P12（SQL+索引） | P16（GraphStore CSR） | 加速比 |
|----------|----------------|----------------------|--------|
| `get_callers(qname)` | 1-5ms | **<1μs** | 5000x |
| `get_callees(qname)` | 1-5ms | **<1μs** | 5000x |
| `search_symbols(name)` | 2-10ms | **~0.5ms** | 10x |
| `get_symbol(qname)` | 1-3ms | **<1μs** | 3000x |
| `get_call_chain(qname, depth=5)` | 50-200ms | **<1ms** | 200x |
| `detect_cycles()` | 500-2000ms | **~50ms** | 20x |

**结论**：冷启动 140-190s 后，查询性能提升 10-5000 倍。对于长时间运行的 MCP Server，冷启动成本可摊薄。

### 2.4 冷启动优化方案

| 优化 | 原理 | 预期收益 | 复杂度 |
|------|------|---------|--------|
| **A. 二进制快照 dump** | GraphStore 序列化到 `.bin` 文件，冷启动直接 mmap 读取 | 140s → **~5s** | 中 |
| **B. 分级加载** | 先加载 symbols，后台构建完整 calls CSR 并按 generation 发布 | 1M: 3.15s 可查符号，10.05s 完整图 | 已实施 |
| **C. mmap 模式** | SQLite 以 mmap 打开，避免 page cache 复制 | -20% | 低 |
| **D. 增量加载** | 只加载最近 N 天变更的符号，旧数据延迟加载 | 140s → **~10s** | 高 |

**推荐 A**：二进制快照 dump。GraphStore 内部是 Vec + String pool，可直接序列化：
```rust
// dump：写 4 个文件（symbols.bin, calls.bin, strings.bin, indices.bin）
// load：mmap 4 个文件，零拷贝
```
冷启动从 140s 降到 ~5s（mmap 零拷贝，仅建立虚拟内存映射）。

**分级加载实测（2026-07-13）**：

- 1M：symbols-ready 3.15s，full-ready 10.05s，首次可查询时间提前 69%。
- 2M：symbols-ready 9.12s，full-ready 26.97s，首次可查询时间提前 66%。
- PyO3 加载与 snapshot dump/load 期间释放 GIL；symbols-only store 发布后，后台 full load 不阻塞符号查询。
- 加载窗口会短暂同时持有 stage/full 两份 SymbolTable；2M 实测峰值约 1.10GB，待 daemon 用 `Arc<SymbolTable>` 共享消除重复。

---

## 3. 内存占用分析（10M 符号，70M 调用边）

### 3.1 GraphStore 内存占用（基于 [graph.rs](file:///c:/git_work/callwarden/rust_ext/src/graph.rs) 结构）

#### SymbolTable（[graph.rs:110-128](file:///c:/git_work/callwarden/rust_ext/src/graph.rs#L110-L128)）

| 组件 | 计算公式 | 10M 规模占用 |
|------|---------|-------------|
| `by_id: Vec<GraphSymbol>` | 10M × 48 字节 | **480 MB** |
| `name_pool: String` | 10M × ~15 字节 | **150 MB** |
| `qname_pool: String` | 10M × ~30 字节 | **300 MB** |
| `module_pool: String` | 10M × ~15 字节 | **150 MB** |
| `by_qname_keys: Vec<String>` | 10M × ~30 字节 + 24 字节/String | **540 MB** |
| `by_qname_values: Vec<u32>` | 10M × 4 字节 | **40 MB** |
| `by_simple_name: FxHashMap` | ~1M key × (24+8) + 10M × 4 | **~80 MB** |
| `by_file: FxHashMap` | 2M key × (24+8) + 10M × 4 | **~140 MB** |
| `file_paths: Vec<String>` | 2M × ~40 字节 + 24 字节/String | **~130 MB** |
| **SymbolTable 小计** | | **~2.0 GB** |

#### CallGraph（[graph.rs:219-247](file:///c:/git_work/callwarden/rust_ext/src/graph.rs#L219-L247)）

| 组件 | 计算公式 | 70M 边占用 |
|------|---------|-----------|
| `forward_edges: Vec<CallEdge>` | 70M × 16 字节 | **1,120 MB** |
| `forward_offsets: Vec<usize>` | 10M × 8 字节 | **80 MB** |
| `backward_edges: Vec<BackwardEdge>` | 70M × 8 字节 | **560 MB** |
| `backward_offsets: Vec<usize>` | 10M × 8 字节 | **80 MB** |
| `by_callee_name: FxHashMap` | ~100K key × 32 + 70M × 4 | **~320 MB** |
| `callee_names_pool: String` | 70M × ~15 字节 | **1,050 MB** |
| `callee_names_offsets: Vec<u32>` | 70M × 4 字节 | **280 MB** |
| `callee_name_to_idx: FxHashMap` | ~100K × 56 字节 | **~6 MB** |
| `roots: Vec<u32>` | ~10K × 4 字节 | **<1 MB** |
| **CallGraph 小计** | | **~3.5 GB** |

### 3.2 总内存占用

| 组件 | 10M 规模 | 5M 规模 | 1M 规模 |
|------|---------|---------|---------|
| GraphStore SymbolTable | 2.0 GB | 1.0 GB | 200 MB |
| GraphStore CallGraph | 3.5 GB | 1.75 GB | 350 MB |
| SQLite cache（PRAGMA） | 64-256 MB | 64-256 MB | 64 MB |
| SQLite mmap | 256-1024 MB | 256 MB | 256 MB |
| Python 解释器 + 其他 | ~200 MB | ~200 MB | ~200 MB |
| **总计** | **~6.5 GB** | **~3.3 GB** | **~1.0 GB** |

### 3.3 内存占用 vs 规模

| 规模 | GraphStore 内存 | SQLite 内存 | **总内存** | 适用机器 |
|------|----------------|-------------|-----------|---------|
| 100K | 20 MB | 320 MB | **~500 MB** | 任何机器 |
| 1M | 550 MB | 320 MB | **~1.0 GB** | 4GB+ 机器 |
| 5M | 2.75 GB | 320 MB | **~3.3 GB** | 8GB+ 机器 |
| 10M | 5.5 GB | 320 MB | **~6.5 GB** | 16GB+ 机器 |
| 20M（预估） | 11 GB | 512 MB | **~12 GB** | 32GB+ 机器 |

---

## 4. 冷启动查询性能总结

| 场景 | 冷启动时间 | 冷启动后查询 | 适用场景 |
|------|-----------|-------------|---------|
| **P12（当前）** | <1s | 1-10ms/查询 | 短时 CLI 命令 |
| **P16 无优化** | 140-190s（10M） | <1μs/查询 | 长时 MCP Server |
| **P16 + 快照 dump（推荐）** | ~5s（10M） | <1μs/查询 | 所有场景 |
| **P16 + 分级加载** | 30s 可查询，后台继续加载 | <1μs/查询 | 渐进式启动 |

**结论**：
- P16 冷启动 140s 是主要问题，但通过**快照 dump 优化可降到 5s**
- 优化后的 P16 适用于所有长时间运行场景（MCP Server、watcher）
- 对于一次性 CLI 命令（`cw --search`），P12 更优（冷启动 <1s）

---

## 5. 内存自适应方案设计

### 5.1 策略分级（已修正：P13/P14/P15 是一套 SQLite 构建参数优化，不是独立架构）

> **⚠️ 修正（review 反馈）**：P13/P14/P15 不应变成三种独立运行架构。它们是**一套 SQLite 构建参数优化**的候选子集，需通过参数矩阵实验验证后合并实施。真正需要按机器内存动态选择的只有 cache_size、mmap_size 和 GraphStore 是否常驻；索引集合和 page_size 应尽量保持统一。

| 维度 | 候选值 | 决策依据 | 状态 |
|------|--------|---------|------|
| **cache_size** | 64MB / 256MB / 512MB | 按机器内存动态选择（这是唯一需要自适应的参数） | 待参数矩阵实验验证 |
| **mmap_size** | 256MB / 1GB | 按机器内存动态选择 | 待参数矩阵实验验证 |
| **temp_store** | MEMORY / FILE | 统一选择（不按机器分） | 待参数矩阵实验验证 |
| **page_size** | 4KB / 8KB / 16KB | 统一选择（建库后难改，不按机器分） | 待参数矩阵实验验证 |
| **索引集合** | 完整 / 精简 | 统一选择（不按机器分） | 待索引耗时分析后决策 |
| **GraphStore 常驻** | 是 / 否 | 按使用场景（CLI/MCP Server/daemon）和内存决定 | 合并到混合架构 |

**关键区别**：
- **SQLite 构建参数**（cache/mmap/temp/page_size/索引集合）：统一选择，不按机器分
- **GraphStore 是否常驻**：按使用场景决定（CLI 一次性查询不常驻，daemon 常驻）

这样设计避免了"三种独立架构"的复杂度，同时保留了按内存动态调整的能力。

### 5.2 自适应选择逻辑（已修正：只动态选 cache_size/mmap_size 和 GraphStore 是否常驻）

```python
def select_sqlite_params(available_mem_mb: int) -> dict:
    """根据可用内存动态选择 SQLite cache_size 和 mmap_size

    Args:
        available_mem_mb: 可用内存（MB）

    Returns:
        dict: cache_size, mmap_size（其他参数统一，不按机器分）

    Note:
        page_size、temp_store、索引集合是统一选择，不在此动态调整。
        需先完成参数矩阵实验（T-1783907815346-75de）确定最优值。
    """
    # 预留 2GB 给系统和其他进程
    usable_mem = available_mem_mb - 2048

    if usable_mem <= 0:
        return {"cache_size": -65536, "mmap_size": 268435456}  # 64MB cache, 256MB mmap
    elif usable_mem >= 8192:  # 8GB+ usable
        return {"cache_size": -262144, "mmap_size": 1073741824}  # 256MB cache, 1GB mmap
    elif usable_mem >= 4096:  # 4GB+ usable
        return {"cache_size": -131072, "mmap_size": 536870912}   # 128MB cache, 512MB mmap
    else:
        return {"cache_size": -65536, "mmap_size": 268435456}   # 64MB cache, 256MB mmap


def should_load_graph_store(available_mem_mb: int, symbol_count: int, is_long_running: bool) -> bool:
    """决定是否加载 GraphStore 到内存

    Args:
        available_mem_mb: 可用内存（MB）
        symbol_count: 预估符号数
        is_long_running: 是否长期运行（MCP Server/watcher/daemon vs 一次性 CLI）

    Returns:
        bool: 是否加载 GraphStore

    Note:
        - 一次性 CLI 命令（cw --search 等）不加载，避免冷启动开销
        - 长期运行的服务在内存足够时加载，查询性能 5000x 加速
    """
    if not is_long_running:
        return False  # 一次性 CLI 不加载，冷启动成本无法摊薄

    graph_store_mem = (symbol_count / 1_000_000) * 550 if symbol_count > 0 else 0
    usable_mem = available_mem_mb - 2048

    # GraphStore 内存 + 512MB SQLite 缓冲
    return usable_mem >= graph_store_mem + 512
```

### 5.3 统一的 PRAGMA 配置（已修正：不再按策略分级，参数矩阵实验后统一选择）

```python
# 待参数矩阵实验（T-1783907815346-75de）确定后填入
# 以下为占位值，实验完成前不要直接使用

UNIFIED_SQLITE_CONFIG = {
    # 统一参数（不按机器分）
    "page_size": 4096,           # TODO: 参数矩阵实验后确定（4KB/8KB/16KB）
    "temp_store": "MEMORY",     # TODO: 参数矩阵实验后确定（MEMORY/FILE）
    "journal_mode": "WAL",
    "synchronous": "NORMAL",

    # 索引集合：完整版或精简版（实验后统一选择，不按机器分）
    "slim_indexes": False,      # TODO: 索引耗时分析后确定

    # 动态参数（按机器内存自适应）
    # cache_size 和 mmap_size 通过 select_sqlite_params() 动态选择
}

# GraphStore 加载策略
GRAPH_STORE_CONFIG = {
    # 通过 should_load_graph_store() 决定是否加载
    # - 一次性 CLI：不加载（冷启动成本无法摊薄）
    # - MCP Server/daemon：内存足够时加载（查询 5000x 加速）
    # 不再区分 "lazy" 和 "resident" 两种模式
}
```

### 5.4 场景适配矩阵（已修正：SQLite 参数统一，只有 cache_size 和 GraphStore 按场景分）

| 场景 | 笔记本（8GB） | 台式机（32GB） | 服务器（128GB）+ daemon |
|------|-------------|--------------|----------------------|
| **SQLite cache_size** | 64MB | 128-256MB | 256MB |
| **SQLite mmap_size** | 256MB | 512MB-1GB | 1GB |
| **page_size** | 统一 | 统一 | 统一 |
| **索引集合** | 统一 | 统一 | 统一 |
| **GraphStore** | 不加载（CLI） | 不加载（CLI） | 加载（daemon） |
| **1M 符号** | cache 64MB | cache 256MB | GraphStore 550MB |
| **5M 符号** | cache 64MB | cache 256MB | GraphStore 2.75GB |
| **10M 符号** | cache 64MB | cache 256MB | GraphStore 5.5GB |
| **20M 符号** | 可能 OOM | cache 256MB | GraphStore 11GB |

**关键差异**：笔记本/台式机用 CLI 一次性查询（不加载 GraphStore），服务器用 daemon 常驻 GraphStore。
**SQLite 构建参数（page_size/temp_store/索引集合）在所有场景下统一**，不按机器分。

### 5.5 运行时自动检测（已修正：简化为 cache_size 动态选 + GraphStore 按场景选）

```python
import psutil

def detect_and_configure(db: CodeGraphDB, is_long_running: bool = False):
    """运行时自动检测内存并配置 SQLite 参数和 GraphStore

    Args:
        db: CodeGraphDB 实例
        is_long_running: 是否长期运行（MCP Server/watcher/daemon vs 一次性 CLI）
    """
    mem = psutil.virtual_memory()
    available_mb = mem.available // (1024 * 1024)

    # 1. 动态选择 SQLite cache_size 和 mmap_size
    params = select_sqlite_params(available_mb)
    db.conn.execute(f"PRAGMA cache_size={params['cache_size']}")
    db.conn.execute(f"PRAGMA mmap_size={params['mmap_size']}")

    # 2. 获取当前数据库符号数（快速 COUNT）
    try:
        symbol_count = db.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    except Exception:
        symbol_count = 0

    # 3. 决定是否加载 GraphStore
    if should_load_graph_store(available_mb, symbol_count, is_long_running):
        store = db._get_graph_store()
        if store is not None:
            print(f"[GraphStore] 已加载到内存（{symbol_count} 符号，{available_mb}MB 可用）")
    else:
        if is_long_running:
            print(f"[GraphStore] 内存不足（{available_mb}MB），降级到 SQLite 查询")
        # 一次性 CLI 不打印，避免噪音
```

---

## 6. P16 冷启动的优缺点

### 优点
1. **查询极快**：CSR 邻接表 O(1) 查找，比 SQL 快 5000 倍
2. **写入极快**：内存写入，无 SQLite 索引维护开销
3. **崩溃恢复**：SQLite 作为 dump，崩溃后从 dump 恢复（类似 Redis AOF）

### 缺点
1. **冷启动慢**：140-190s（10M），需快照 dump 优化到 5s
2. **内存占用大**：10M 需 6.5GB，不适合小内存机器
3. **实现复杂**：需要实现写入主路径改造 + 快照 dump + 增量更新

### 适用场景
- ✅ MCP Server（长时运行，冷启动成本可摊薄）
- ✅ watcher 守护进程（持续运行）
- ✅ 大内存服务器（>32GB）
- ❌ 一次性 CLI 命令（`cw --search`，冷启动成本无法摊薄）
- ❌ 小内存开发机（<8GB）

---

## 7. 推荐实施路径（已修正：P13/P14/P15 合并为参数矩阵实验，不分级实施）

> **⚠️ 修正（review 反馈）**：P13/P14/P15 不应分级实施，应作为**一套 SQLite 构建参数优化**，通过参数矩阵实验验证后合并实施。

| 阶段 | 内容 | 前置条件 | 状态 |
|------|------|---------|------|
| **参数矩阵实验** | 1M/2M 规模跑 cache/mmap/temp/page_size/index_mode 全组合 | 修正压测基准体系 | 待实施（T-1783907815346-75de）|
| **SQLite 参数优化** | 根据实验结果，统一选择 page_size/temp_store/索引集合；动态选 cache_size/mmap_size | 参数矩阵实验完成 | 待决策 |
| **GraphStore P1-P6** | 字符串池/kind 枚举/紧凑 backward edge/FxHash，每项分别测 | 无（可并行） | 已实施并完成 1M/2M 复测 |
| **GraphStore 分级加载** | symbols-only 快速发布 + 后台 full graph generation 替换 | GraphStore P1-P6 | 已实施（T-1783937504339-3839） |
| **混合架构** | SQLite/CAS 真相 + GraphStore 内存查询 + daemon 共享 | GraphStore P1-P6 完成 | 合并到 Enterprise Daemon |

**推荐顺序**：
1. 修正压测基准体系（T-1783907127361-2d86）
2. 参数矩阵实验（T-1783907815346-75de）— 与 GraphStore P1-P6 并行
3. 根据 1+2 的结果决策 SQLite 参数优化方案
4. GraphStore P1-P6 完成 + 真实企业 E2E 测试（T-1783907127393-2d1f）
5. Enterprise Daemon 架构演进（T-1783830945165-1acc）

**关键原则**：不要依据当前报告的推测收益直接写进正式架构。先实验，再决策。
