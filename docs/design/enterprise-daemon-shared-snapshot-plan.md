# Enterprise Daemon Shared Snapshot 设计与实施计划

状态：**Baseline（Enterprise Daemon 主设计）**
日期：2026-07-10
基线版本：v10.2（`ad2e308`）

> **本文档是 Enterprise Daemon 架构的权威主设计**（Phase 0 步骤 1 确认）。
> 所有 enterprise daemon 相关实施以此文档为准，`rust_daemon_architecture.md` 降级为历史参考。

相关文档：
- [enterprise-architecture-evolution.md](enterprise-architecture-evolution.md) — 架构演进背景
- [rust_daemon_architecture.md](rust_daemon_architecture.md) — 历史参考（已标记过时）
- [parse-input-abi.md](parse-input-abi.md) — ParseInput ABI 规范
- [cas-gc-protocol.md](cas-gc-protocol.md) — CAS 发布与唯一 GC 协议
- [watcher-generation-state-machine.md](watcher-generation-state-machine.md) — Watcher Generation 状态机
- [daemon-ipc-security.md](daemon-ipc-security.md) — Daemon IPC 安全规范
- [roadmap_phase2_plan.md](../roadmap_phase2_plan.md) — Phase 2 路线图

## 1. 决策摘要

Call Warden 的企业部署目标不再是"每个用户、每个工作区各自运行一套 Python + SQLite"。目标架构应升级为：

> Rust daemon 作为共享代码图谱服务，统一负责解析、索引、查询、后台图计算和快照发布；Python 保留 CLI/MCP 表现层、兼容层、任务/规则等业务编排。

核心形态：

```text
Python CLI / MCP / hook
        |
        | UDS / optional mTLS TCP
        v
Rust Enterprise Daemon
        |
        +-- Coordinator: workspace registry, job scheduler, auth, merge, snapshot publish
        +-- Worker pools: parse, resolve, graph, clone, vector, semgrep wrapper
        +-- Shared immutable snapshots: ArcSwap<GraphSnapshot>
        +-- Storage: global CAS, toolchain CAS, workspace manifests, durable job log
```

关键原则：

- **单写多读**：daemon/coordinator 是唯一合并者和持久化写入者，MCP/CLI 客户端不直接写共享数据库。
- **只读快照共享**：所有在线查询读 immutable snapshot，更新时构建新 generation 后原子发布。
- **Worker 只产 delta**：worker 不直接修改全局图，只返回 parse/resolve/clone/vector delta。
- **内容寻址去重**：相同文件内容、相同 parser 版本、相同语言配置只解析一次。
- **路径和权限可信**：所有 workspace 先注册，查询只使用 `workspace_instance_id + relative_path`，daemon 做 UID、realpath、symlink 和 owner 校验。
- **在线请求不跑全量图计算**：MCP 查询只读预计算索引，clone/vector/semgrep 等重任务进入后台 job。

## 2. 背景与当前状态

### 2.1 目标部署场景

目标环境是一台共享 Linux 固件开发机：

- 十几个开发者共同使用。
- 每人有 3-10 个同一 repo 的不同本地分支工作区。
- Ubuntu 14.04 / 16.04 / 18.04 / 20.04 / 22.04 / 24.04 容器并存。
- 容器 `/opt` 和 `/home` 挂载在宿主机上。
- 用户通过 SMB、VSCode Remote、容器 shell 等方式访问同一批文件。
- `/opt` 下有多个厂家、多个版本、多个 sysroot 的工具链。

当前每个 workspace 一个 SQLite 的模式会导致：

- 相同 repo 的不同分支重复解析大量相同文件。
- 多用户同时刷新时 CPU、内存、磁盘 I/O 踩踏。
- 每个工作区生成一份图谱 DB，存储线性膨胀。
- 容器路径、宿主机路径、SMB 路径互相不一致。
- MCP 长连接和 CLI 写入之间仍有 SQLite 锁风险。

### 2.2 实际工作流痛点

这个场景的核心痛点不是“磁盘上有很多代码”这么简单，而是研发流程会迫使人保留很多重复工作区。

固件项目中一次完整 `repo sync` + `make` 可能需要几十分钟，部分项目一次编译约 70 分钟。架构师或维护人员经常需要临时帮不同开发者看不同分支、不同客户项目、同一分支的不同产品形态。若每次有人找过来都重新 checkout、sync、build，一上午很容易被等待时间吃掉。

因此实际行为会变成：

- 架构师自己保留多个稳定分支、客户分支、临时修复分支工作区。
- 同一开发者同时保留 3-10 个同源 repo 本地工作区。
- 相同产品线代码按日期、客户、版本、分支形成多个快照目录。
- 为避免临时构建等待，`out/`、临时产物、历史备份和整包 SDK 被长期保留。
- 架构分析、代码搜索、调用链确认这类工作被迫绑定到本地 checkout 和 build 状态。

Call Warden enterprise daemon 的一等目标应当是把“看代码、问图谱、做影响分析”从“我手上必须有一份已同步、已编译的工作区”中解耦出来：

```text
开发者/架构师提出问题
        |
        | 不要求重新 repo sync / make
        v
查询已注册 workspace/snapshot 的共享代码图谱
        |
        v
秒级得到符号、调用链、影响范围、跨分支差异
```

这意味着企业版必须提供：

- **Workspace Catalog**：daemon 知道机器上有哪些已注册项目、分支、产品形态、owner、最后刷新时间。
- **Read-only Review View**：架构师可在授权范围内查询别人的 clean snapshot，而不是复制一份完整工作区。
- **Source-first Index**：源码级图谱不依赖完整编译成功；build context 用于提升 C/C++ 精度，但不能成为基础查询的前置条件。
- **Artifact Ignore Policy**：默认排除 `out/`、`temp/`、`bakdiff/`、`record/`、`adb/`、cache/build 产物等目录，防止索引系统重复吞掉编译产物。
- **Snapshot Reuse**：同一产品线相近分支共享大部分 clean snapshot，只为差异文件构建 overlay。

### 2.3 当前 Rust 能力

当前代码已经具备 Rust core 的基础能力：

- Rust PyO3 扩展暴露 `parse_file_lang`、`batch_parse_files_lang`、`batch_parse_files_lang_pool`，可用 rayon 并行解析多语言文件。
- Rust `GraphStore` 可通过 `rusqlite` 只读加载 SQLite 中的 `symbols` / `calls`，构建内存 CSR 图和索引。
- Python 查询层的 `get_callers` / `get_callees` / `search_symbols` 已优先走 Rust `GraphStore`，失败时降级 SQL。
- 当前写入路径仍主要由 Python 执行，例如 `_save_symbols_for_version`、`_write_calls_db`、`_save_file_version`。
- 当前主构建路径只把 C 文件批量解析接到 Rust pool，Rust 多语言批量解析能力还未完全接入 `db_build.py` 主路径。

因此本文档把现状定义为：

> Rust 已经具备 parse/query/storage-read 的核心能力，但 enterprise daemon 还需要完成主路径接线、共享快照、集中存储、权限模型和秒级 watcher。

## 3. 目标与非目标

### 3.1 目标

1. **多用户共享**：同一台开发机上多个用户、多个工作区共享 CAS、toolchain index 和 clean snapshot。
2. **秒级 watcher**：单文件保存后，相关符号和局部图谱在 1-3 秒内对查询可见。
3. **低延迟查询**：常用 MCP 查询读内存 snapshot，P95 目标小于 5ms，核心图查询目标小于 1ms。
4. **大仓库可用**：支持 Android/Linux kernel/firmware 级别项目，不让 clone detect、impact、topo 等功能在在线请求中退化为 O(n²) 或全图重算。
5. **安全隔离**：user1 不能通过 daemon 读取 user2 的 workspace，容器路径不能绕过宿主机权限边界。
6. **向后兼容**：无 daemon 时仍可回退到当前本地 Python + SQLite 模式。
7. **架构师只读共享视图**：授权用户可以查询已注册 workspace/snapshot 的图谱，不需要复制代码、重新 sync 或等待完整 make。

### 3.2 非目标

1. 第一阶段不重写所有 120+ MCP 工具。
2. 第一阶段不把 task/rule/guardrail/audit 等业务写逻辑全部搬到 Rust。
3. 第一阶段不承诺完整 C/C++ 编译器级语义解析。build context 会纳入 fingerprint 和解析输入，但精度逐步演进。
4. 第一阶段不做跨机器分布式集群。本文目标是单台共享 Linux 开发机。

## 4. 术语

| 术语 | 含义 |
| ---- | ---- |
| Coordinator | daemon 内唯一负责注册、调度、合并、持久化和发布 snapshot 的协调者 |
| Worker | 执行 parse/resolve/graph/clone/vector 等计算的线程池或子进程 |
| CAS | Content Addressable Storage，按内容 hash 存储解析结果 |
| GraphSnapshot | 某个 workspace/snapshot 的只读内存图谱，在线查询直接读取 |
| Generation | GraphSnapshot 的版本号，每次发布递增 |
| workspace_instance_id | 一个真实本地工作区实例，绑定 owner UID 和 host real root |
| snapshot_id | clean 工作区可共享的公共代码快照身份 |
| Dirty Overlay | 未提交修改、staged、untracked 文件形成的私有覆盖层 |
| Build Context | include path、宏、compile_commands、sysroot、toolchain、语言标准等解析上下文 |
| Staging | daemon 内存或轻量 WAL 中的待合并变更区 |
| Replicator | 将 Staging delta 合并进持久化存储并发布新 snapshot 的后台组件 |

## 5. 总体架构

```text
                         +------------------------------+
                         | Python CLI / MCP / hook      |
                         | - command parsing            |
                         | - user-facing formatting     |
                         | - local fallback             |
                         +---------------+--------------+
                                         |
                                         | UDS, SO_PEERCRED
                                         v
+--------------------------------------------------------------------------------+
| Rust Enterprise Daemon                                                          |
|                                                                                |
|  +-------------------+     +---------------------+     +---------------------+ |
|  | Access Router     | --> | Workspace Registry  | --> | Path Mapper/Auth    | |
|  | UDS / mTLS TCP    |     | uid, roots, repo    |     | realpath checks     | |
|  +---------+---------+     +----------+----------+     +----------+----------+ |
|            |                          |                           |            |
|            v                          v                           v            |
|  +-------------------+     +---------------------+     +---------------------+ |
|  | Query Router      | --> | Snapshot Manager    | <-- | Replicator          | |
|  | MCP-compatible    |     | ArcSwap generation  |     | merge deltas        | |
|  +---------+---------+     +----------+----------+     +----------+----------+ |
|            |                          ^                           ^            |
|            v                          |                           |            |
|  +-------------------+                |                           |            |
|  | GraphSnapshot     |                |                           |            |
|  | Symbol/CSR/Index  |                |                           |            |
|  +-------------------+                |                           |            |
|                                       |                           |            |
|  +-------------------+     +----------+----------+     +----------+----------+ |
|  | Job Scheduler     | --> | Worker Pools        | --> | Delta Store/Staging | |
|  | dedup, priority   |     | parse/resolve/...   |     | durable job log     | |
|  +---------+---------+     +----------+----------+     +----------+----------+ |
|            |                          |                           |            |
|            v                          v                           v            |
|  +-------------------+     +---------------------+     +---------------------+ |
|  | Global CAS        |     | Toolchain CAS       |     | Workspace Manifests| |
|  | file parse cache  |     | /opt fingerprints   |     | snapshot/overlay   | |
|  +-------------------+     +---------------------+     +---------------------+ |
+--------------------------------------------------------------------------------+
```

### 5.1 Python 层职责

Python 保留：

- CLI 参数解析、输出格式化、兼容旧命令。
- MCP 包装层，过渡期可作为 daemon thin client。
- task/rule/guardrail/audit 等业务逻辑。
- local fallback 模式。
- 测试、调试和迁移工具。

Python 不再负责 enterprise 模式下的：

- 多进程解析调度。
- 共享数据库写入。
- 在线图遍历热路径。
- 多用户 workspace 可信路径判断。

### 5.2 Rust daemon 职责

Rust daemon 负责：

- workspace 注册、路径映射和权限校验。
- CAS 查询、miss 后解析、parse 结果落盘。
- build context 管理和 resolved call edge 计算。
- GraphSnapshot 构建、缓存、原子发布。
- 秒级 watcher。
- clone/vector/semgrep 等重任务调度和结果持久化。
- 查询 API 的内存索引服务。

### 5.3 职责边界禁止交叉

以下操作**禁止跨层**，确保信任边界清晰：

| 禁止 | Python 不能做 | Rust daemon 不能做 |
|------|-------------|-------------------|
| CAS DB 直写 | Python 不直接打开/写 CAS DB | — |
| 业务逻辑 | — | daemon 不实现 task/rule/guardrail/audit 业务逻辑 |
| 可信解析 | Python 不生成 ParseFact（enterprise 模式下） | — |
| 文件读取（enterprise） | agent 读文件后只回传 canonical bytes | daemon 不以用户身份读文件（通过 agent 回传） |
| Session 管理 | Python agent 不自行切换 session | — |
| hash 计算 | Python agent 的 content_hash 不被信任 | daemon 必须重新计算 sha256 |

## 6. 进程模型与 Master/Slave 边界

本文采用 **Coordinator + Worker Pool**，不采用多个进程共同修改同一块可变内存。

### 6.1 Coordinator 单写

Coordinator 是唯一可以执行以下动作的组件：

- 修改 workspace registry。
- 写 global CAS metadata。
- 合并 worker delta。
- 更新 workspace manifest。
- 发布新的 GraphSnapshot generation。
- 写审计日志。

这样可以避免多个 worker 同时写 SQLite/RocksDB/CAS 造成锁和一致性问题。

### 6.2 Worker 无状态或弱状态

Worker 只做计算：

- Parse worker：输入文件 hash/path/language/build hints，输出 symbols/raw_calls/imports。
- Resolve worker：输入 raw_calls + build context + symbol index，输出 resolved call_edges。
- Graph worker：输入 edge delta，输出 depth/cycle/topo/impact delta。
- Clone worker：输入 changed symbol token shingles，输出 clone candidate/group delta。
- Vector worker：输入 changed symbol content，输出 embedding delta。
- Semgrep worker：调用外部 semgrep，输出 findings delta。

Worker 不直接修改当前 active snapshot。Worker 输出 delta，由 Coordinator 验证、合并、持久化、发布。

### 6.3 共享只读快照

在线查询读 `Arc<GraphSnapshot>`：

```rust
pub struct SnapshotManager {
    active: ArcSwap<GraphSnapshot>,
    generations: LruCache<GenerationId, Arc<GraphSnapshot>>,
}
```

更新流程：

1. Coordinator 收到 delta。
2. 在后台构建 `GraphSnapshot generation + 1`。
3. 校验索引一致性。
4. 原子替换 active pointer。
5. 旧 generation 被正在进行的查询继续持有，查询结束后自然释放。

读路径没有写锁，写路径不阻塞正在运行的查询。

## 7. 存储模型

### 7.1 Global CAS

Global CAS 存放可跨用户、跨工作区共享的单文件解析结果。

CAS key：

```text
cas_key = sha256(
  file_content_hash,
  language,
  parser_version,
  callwarden_core_version,
  extraction_config_version,
  language_mode
)
```

CAS 存放：

- file metadata：size、mtime hint、language、parser version。
- symbol list：函数、类、结构体、枚举、宏等。
- raw calls：单文件内直接抽取的调用文本。
- imports/includes：单文件可见的 import/include 文本。
- token shingles：clone/vector 使用。

CAS 不存：

- resolved cross-file call_edges。
- workspace 绝对路径。
- owner UID。
- dirty 工作区私有内容的可共享 snapshot。

### 7.2 Toolchain CAS

固件环境中 `/opt` 不是一个单一工具链。不同厂家、版本、target、sysroot、宏和 include path 会改变符号解析结果。

`toolchain_fingerprint`：

```text
sha256(
  toolchain_root_realpath,
  compiler_version,
  target_triple,
  sysroot_path,
  include_dirs_ordered,
  predefined_macros,
  language_standard,
  parser_version,
  callwarden_core_version
)
```

Toolchain CAS 存放：

- 系统头文件和 vendor SDK 的 parse cache。
- 外部符号表。
- include/import 解析索引。
- toolchain audit metadata。

Workspace 必须绑定一个或多个 `toolchain_fingerprint`。未绑定时只能做 best-effort 解析，并在状态里标记精度降级。

### 7.3 Workspace Manifest

Workspace 不再独立持有完整图谱 DB，而是持有 manifest 和 overlay。

`workspace_instance_id`：

```text
sha256(
  owner_uid,
  host_real_root,
  git_remote_url,
  git_head_commit_sha,
  submodule_state_hash,
  sparse_checkout_hash,
  working_tree_dirty_hash
)
```

`snapshot_id`：

```text
sha256(
  git_remote_url,
  git_head_commit_sha,
  submodule_state_hash,
  sparse_checkout_hash,
  toolchain_fingerprint,
  build_context_hash
)
```

规则：

- Clean workspace 可以共享 `snapshot_id` 对应的 GraphSnapshot 和 resolved edge store。
- Dirty workspace 使用 clean snapshot + private dirty overlay。
- staged/untracked 文件进入 dirty overlay，不污染共享 snapshot。
- 无 git 项目使用 directory manifest hash 兜底，但不跨用户共享。

### 7.4 Resolved Edges 归属

必须区分 raw calls 和 resolved edges：

| 数据 | 归属 | 原因 |
| ---- | ---- | ---- |
| raw calls | Global CAS | 只依赖单文件内容和 parser |
| imports/includes | Global CAS | 单文件可抽取 |
| resolved call_edges | snapshot/workspace | 依赖 build context、toolchain、同名符号、sysroot |
| impact/depth/cycles | snapshot/workspace | 依赖 resolved graph |
| clone candidates | snapshot 或 CAS 辅助索引 | token shingles 可共享，候选/分组需按 workspace 过滤 |

## 8. 内存快照结构

### 8.1 GraphSnapshot

```rust
pub struct GraphSnapshot {
    pub workspace_instance_id: WorkspaceInstanceId,
    pub snapshot_id: Option<SnapshotId>,
    pub generation: u64,
    pub build_context_hash: Hash,
    pub manifest: FileManifest,
    pub symbols: SymbolTable,
    pub calls: CallGraph,
    pub suffix_index: SuffixIndex,
    pub search_index: SearchIndex,
    pub clone_index: Option<CloneIndex>,
    pub vector_index: Option<VectorIndex>,
    pub health: SnapshotHealth,
}
```

### 8.2 Symbol ID

推荐使用两层 ID：

- `stable_symbol_id = hash(cas_key, symbol_range, symbol_kind, qualified_name_hint)`
- `snapshot_symbol_id = compact u32 index in GraphSnapshot`

外部 API 返回 stable ID，内存图使用 compact u32，提高 CSR 和 HashMap 性能。

### 8.3 CallGraph

```rust
pub struct CallGraph {
    pub edges: Vec<CallEdge>,
    pub forward: Vec<Range<usize>>,
    pub backward: Vec<Vec<u32>>,
    pub unresolved_by_name: HashMap<InternedStr, Vec<EdgeId>>,
}
```

要求：

- `get_callees(symbol_id)` 为 O(out_degree)。
- `get_callers(symbol_id)` 为 O(in_degree)。
- short name 查询必须先提示歧义或限制返回数量，不允许无界扫描。
- chain/impact 默认有 depth、node limit、timeout。

### 8.4 Version Diff Index

企业版必须把跨版本对比作为一等索引，而不是临时对两个目录跑文本 diff。架构师排查问题时最常问的是：

- 这个函数两个版本之间改了什么。
- 函数签名、参数、返回值、可见性有没有变。
- 这个函数新增或丢失了哪些 caller/callee。
- 调用链从版本 A 到版本 B 是否绕到了另一条路径。
- 同名函数在不同产品分支里是否其实不是同一个语义点。

因此 snapshot 需要保存可复用的 diff 基础数据：

```rust
pub struct SymbolVersionKey {
    pub repo_id: RepoId,
    pub logical_path: InternedStr,
    pub qualified_name_hint: InternedStr,
    pub symbol_kind: SymbolKind,
}

pub struct SymbolDiffRecord {
    pub left_symbol_id: Option<StableSymbolId>,
    pub right_symbol_id: Option<StableSymbolId>,
    pub change_kind: SymbolChangeKind,
    pub signature_change: Option<SignatureDiff>,
    pub body_hash_changed: bool,
    pub caller_delta: EdgeDeltaSummary,
    pub callee_delta: EdgeDeltaSummary,
}
```

`SymbolChangeKind` 至少包括：

- `added`
- `removed`
- `moved`
- `renamed`
- `signature_changed`
- `body_changed`
- `callers_changed`
- `callees_changed`
- `unchanged`

匹配规则按置信度分层：

1. `stable_symbol_id` 完全一致。
2. `logical_path + qualified_name + kind` 一致。
3. 函数签名相似、body shingles 相似、上下文调用关系相似。
4. 低置信度时返回 ambiguous，不自动断言同一函数。

调用图差异不能只看文本 diff，需要比较 resolved edges：

| 差异 | 含义 |
| ---- | ---- |
| added_callee | 新增下游依赖，可能扩大影响范围 |
| removed_callee | 移除调用，可能改变初始化、释放、校验流程 |
| added_caller | 新入口开始调用该函数，风险常被忽略 |
| removed_caller | 旧入口不再调用该函数，可能是 dead path 或产品差异 |
| chain_changed | A 到 B 的路径发生变化，适合架构审查 |
| cycle_changed | 新增/消除循环依赖 |

这些 diff 结果应当可以缓存为 `DiffSnapshot(left_snapshot_id, right_snapshot_id, scope)`，并设置 TTL/LRU。在线 MCP 请求只读取已缓存结果；未命中时提交后台 job，或在小 scope 内同步计算。

## 9. Watcher 增量更新

### 9.1 文件保存流程

```text
file event
  |
  v
debounce 300-1000ms
  |
  v
compute content hash
  |
  +-- unchanged -> drop
  |
  +-- changed -> CAS lookup
          |
          +-- hit -> load parse result
          |
          +-- miss -> parse worker
  |
  v
resolve affected file raw calls
  |
  v
compute affected graph frontier
  |
  v
build snapshot generation + 1
  |
  v
atomic publish
  |
  v
async persist
```

### 9.2 影响范围

单文件变更不应触发全仓重算。默认 affected set：

- 当前文件的 symbols。
- 当前文件新增/删除/修改 raw calls 的 caller/callee。
- 直接调用这些 symbols 的 callers。
- 受 include/import 变化影响的文件。
- build context 变化时扩大到同一 compile unit 或 module。

当 affected set 超过阈值，例如超过全图 20%，daemon 自动退化为 batch rebuild job，但在线查询继续读旧 generation，直到新 generation 发布。

### 9.3 查询一致性

默认一致性：eventual consistency，目标 1-3 秒可见。

API 可选：

- `refresh_file(wait=true)`：等待新 generation 发布再返回。
- `query(min_generation=N)`：若当前 snapshot 低于 N，返回 `indexing` 状态或等待。
- `status`：显示 active generation、pending jobs、last error。

## 10. Job Scheduler

### 10.1 Job Dedup

Job key：

```text
job_key = sha256(
  operation,
  cas_key or workspace_instance_id,
  build_context_hash,
  algorithm_version
)
```

相同 key 的 job 只执行一次，后续请求挂到同一个 future。

### 10.2 优先级

| 优先级 | Job |
| ------ | --- |
| P0 | 用户显式查询所需的 missing index |
| P1 | watcher 单文件 refresh |
| P2 | git pull / checkout batch refresh |
| P3 | clone detect / vector embedding |
| P4 | semgrep full scan / health report |
| P5 | CAS compaction / old snapshot GC |

### 10.3 资源预算

资源按 host、workspace、uid 三层限制：

- host max parse threads。
- per-user concurrent jobs。
- per-workspace queue length。
- memory soft/hard limit。
- background heavy jobs 的 CPU quota。

## 11. 权限与路径模型

### 11.1 UDS 身份

默认通信使用 Unix Domain Socket：

- `/var/run/callwarden/daemon.sock`
- socket 权限 `0660`
- 属主 `callwarden:callwarden`

daemon 接受连接后读取 `SO_PEERCRED`，得到 `pid/uid/gid`。客户端传入的 uid 一律不可信。

### 11.2 Workspace 注册

注册内容：

```json
{
  "client_view_root": "/home/user/work/firmware",
  "repo_remote": "ssh://git/firmware.git",
  "container_hint": "ubuntu_2204",
  "toolchain_hint": "arm-none-eabi-gcc-9.3"
}
```

daemon 自动补充：

```json
{
  "owner_uid": 1001,
  "host_real_root": "/data/docker_volumes/user/work/firmware",
  "workspace_instance_id": "ws_...",
  "snapshot_id": "snap_..."
}
```

后续所有请求必须使用 `workspace_instance_id`，不能继续传任意 `workspace_root` 让 daemon 读文件。

架构师或维护人员访问他人 workspace 时不改变 owner。daemon 通过显式授权表判断是否允许只读查询：

```text
workspace_acl(
  workspace_instance_id,
  grantee_uid_or_group,
  permission = read_snapshot | refresh | admin,
  granted_by,
  expires_at
)
```

默认策略：

- owner 拥有 refresh/read 权限。
- admin 组可注册和维护全局策略。
- 架构师组默认只读，不允许读取 dirty overlay，除非 owner 显式授权。
- 只读授权只能访问已经发布的 clean snapshot，不能绕过文件系统权限直接读取任意源文件内容。

### 11.3 路径校验链

每次解析文件路径时执行：

1. 校验 peer uid 是否等于 workspace owner uid，或属于 admin 组。
2. `relative_path` 必须是相对路径，不允许 `..` 跳层。
3. 拼接 `host_real_root + relative_path`。
4. `realpath` 后必须仍在 `host_real_root` 下。
5. symlink 策略默认拒绝逃逸，可配置为 allowlist。
6. `stat.st_uid` 必须与 owner uid 一致，或在受信 group allowlist 内。
7. 记录审计日志。

### 11.4 TCP 通路

TCP 默认关闭。必须打开时要求：

- mTLS。
- per-container token。
- token 绑定宿主机 uid。
- 只监听 `127.0.0.1` 或受限网段。
- 所有请求进入同一权限校验链。

## 12. API 设计

### 12.1 管理 API

```text
register_workspace(client_view_root, repo_hint, toolchain_hint) -> workspace_instance_id
list_workspaces()
workspace_status(workspace_instance_id)
catalog_search(repo_hint, branch, product, owner, updated_after)
grant_workspace_access(workspace_instance_id, grantee, permission, ttl)
revoke_workspace_access(workspace_instance_id, grantee)
register_toolchain(path, target, sysroot, include_dirs, macro_probe_cmd)
daemon_status()
job_status(job_id)
```

### 12.2 Refresh API

```text
refresh_workspace(workspace_instance_id, mode=incremental|full, wait=false)
refresh_file(workspace_instance_id, relative_path, wait=true)
remove_file(workspace_instance_id, relative_path)
rescan_manifest(workspace_instance_id)
```

### 12.3 Query API

```text
search_symbols(workspace_instance_id, query, kind, limit)
get_symbol(workspace_instance_id, stable_symbol_id | qualified_name)
get_callers(workspace_instance_id, symbol_id | qualified_name, depth=1, limit=100)
get_callees(workspace_instance_id, symbol_id | qualified_name, depth=1, limit=100)
get_impact(workspace_instance_id, symbol_id, depth=3, node_limit=1000)
get_topological_order(workspace_instance_id, limit=5000)
detect_cycles(workspace_instance_id, node_limit=1000)
compare_snapshots(left_workspace_instance_id, right_workspace_instance_id, scope)
diff_symbol(left_workspace_instance_id, right_workspace_instance_id, qualified_name | symbol_id)
diff_signature(left_workspace_instance_id, right_workspace_instance_id, qualified_name | symbol_id)
diff_callers(left_workspace_instance_id, right_workspace_instance_id, qualified_name | symbol_id)
diff_callees(left_workspace_instance_id, right_workspace_instance_id, qualified_name | symbol_id)
diff_call_chain(left_workspace_instance_id, right_workspace_instance_id, from_symbol, to_symbol, depth)
list_symbol_changes(left_workspace_instance_id, right_workspace_instance_id, filters)
```

跨版本 diff API 默认以函数/文件/module 为 scope。仓库级全量对比必须走后台 job，避免在线请求退化成全图扫描。

### 12.4 Heavy Job API

```text
start_clone_detect(workspace_instance_id, scope, threshold) -> job_id
list_clones(workspace_instance_id, filters)
start_vector_index(workspace_instance_id, scope) -> job_id
semantic_search(workspace_instance_id, query, limit)
start_semgrep_scan(workspace_instance_id, scope, config) -> job_id
list_findings(workspace_instance_id, filters)
start_snapshot_diff(left_workspace_instance_id, right_workspace_instance_id, scope) -> job_id
list_snapshot_diffs(left_workspace_instance_id, right_workspace_instance_id, filters)
```

Heavy job API 默认异步。MCP 工具不能在请求内全量跑 clone detect 或 semgrep。

## 13. CLI/MCP 模式

### 13.1 模式

```text
enterprise: 必须连接 daemon，失败则报错
auto: 优先 daemon，失败回退 local，并打印 warning
local: 当前单机模式
```

配置：

```ini
[client]
mode = enterprise
uds_path = /var/run/callwarden/daemon.sock
fallback_warning = true
```

企业部署建议使用 `enterprise`，避免静默回退导致每个用户又各自建本地 DB。

### 13.2 MCP 过渡策略

阶段性做法：

1. Python MCP server 保留，内部调用 daemon client。
2. 高频查询工具先迁到 daemon API。
3. task/rule/guardrail 等写业务继续 Python 执行，但需要通过 daemon 查询图谱。
4. Rust daemon 稳定后再评估是否直接实现 MCP stdio/SSE。

## 14. 实施路线图

### Phase 0: 文档与边界收敛

目标：统一 enterprise、rust daemon、phase2 roadmap 的表述。

任务：

- [ ] 明确 Python/Rust 职责边界。
- [ ] 将本文档设为 enterprise daemon 主设计。
- [ ] 在 roadmap 中增加 enterprise daemon epic。
- [ ] 标记旧设计中已过时的"Rust daemon 只是未来储备"描述。

验收：

- 读者能从文档判断当前做什么、不做什么、为什么做。

### Phase 1: Rust 多语言 parse 接入主 refresh 路径

目标：让 `batch_parse_files_lang_pool` 不只存在于 PyO3 API 和测试中，而是成为 `db_build.py` 默认解析路径。

任务：

- [ ] 按 language 对 `to_parse` 分组。
- [ ] 对 Rust 支持语言调用 `batch_parse_files_lang_pool(files, language, num_threads)`。
- [ ] 不支持语言或 Rust 扩展不可用时回退 Python parser。
- [ ] 保留 `CW_DISABLE_RUST_PARSE`。
- [ ] 增加 parse alignment smoke tests。
- [ ] 增加 benchmark，验证 Python ProcessPool 退出主路径。

验收：

- 支持语言默认不走 Python `ProcessPoolExecutor`。
- 解析结果与 Python parser 核心字段一致。
- 大批量解析时父进程 RSS 不再持有全部 Python dict 峰值。

### Phase 2: Daemon Skeleton + UDS + Workspace Registry

目标：建立企业 daemon 最小可运行骨架。

任务：

- [ ] 新增 Rust daemon crate 或扩展 `rust_ext` 为 daemon binary。
- [ ] 实现 UDS server。
- [ ] 实现 `SO_PEERCRED`。
- [ ] 实现 workspace registry schema。
- [ ] 实现 container mount mapping 配置。
- [ ] 实现 register/list/status API。
- [ ] Python CLI 增加 daemon client 和 `enterprise/auto/local` 模式。

验收：

- 普通用户只能注册自己的 workspace。
- 未注册 workspace 不能查询。
- 客户端不能通过传任意 root 让 daemon 读文件。

### Phase 3: Global CAS + Workspace Manifest

目标：相同文件跨用户、跨工作区只解析一次。

任务：

- [ ] 设计 CAS schema：file_cache、symbol_cache、raw_calls、imports、token_shingles。
- [ ] CAS key 包含 parser/version/config。
- [ ] daemon refresh 时先 hash，再 CAS lookup。
- [ ] miss 才调用 parse worker。
- [ ] 实现 clean snapshot manifest。
- [ ] 实现 dirty overlay manifest。
- [ ] 实现 CAS GC 和引用计数或 mark-sweep。

验收：

- 50 个同 repo clean workspace 中，相同文件 parse 只发生一次。
- 第二个 clean workspace 注册后主要耗时为 manifest 构建和 snapshot 绑定。
- dirty 文件不会污染共享 snapshot。

### Phase 4: Snapshot Query Service

目标：在线查询完全读 Rust GraphSnapshot。

任务：

- [ ] 实现 GraphSnapshot generation。
- [ ] 实现 ArcSwap 原子发布。
- [ ] 将当前 `GraphStore` 演进为 snapshot manager。
- [ ] 支持多个 workspace 的 snapshot cache。
- [ ] query API 全部带 `workspace_instance_id`。
- [ ] 加入 query budget：depth、limit、timeout、frontier。
- [ ] 实现函数级 `diff_symbol` / `diff_signature`。
- [ ] 实现 `diff_callers` / `diff_callees`，基于 resolved edge delta。
- [ ] 实现小 scope `compare_snapshots` 同步查询，仓库级 diff 转后台 job。
- [ ] Python MCP 查询工具改为 daemon client。

验收：

- `get_callers/get_callees/search_symbols/get_impact` 不直接查询 SQLite。
- 更新发布时不阻塞正在运行的查询。
- 查询可报告自身使用的 generation。
- 架构师可在两个 workspace/snapshot 间查看函数正文、签名、caller/callee 变化。
- 同名但低置信度匹配的函数返回 ambiguous，不误判为同一函数。

### Phase 5: 秒级 Watcher + Delta Replicator

目标：文件保存后 1-3 秒内图谱可见。

任务：

- [ ] 使用 Rust `notify` crate 监听 workspace roots。
- [ ] 实现 debounce 和 batch event coalescing。
- [ ] 实现 changed file hash diff。
- [ ] 实现 parse delta、resolve delta。
- [ ] 实现 affected frontier 计算。
- [ ] 实现局部 depth/cycle/impact 更新。
- [ ] 实现 Staging durable log。
- [ ] Replicator 合并 delta 并发布新 generation。

验收：

- 单文件修改后，`get_symbol` 和 `get_callers` 在 1-3 秒内看到新 generation。
- 100 文件 git pull 不触发全仓同步重算，除非 affected set 超阈值。
- daemon crash 后可从 durable log 或 manifest 恢复。

### Phase 6: Toolchain CAS 和 Build Context

目标：固件工具链和 sysroot 进入一等公民模型。

任务：

- [ ] 实现 `register_toolchain`。
- [ ] 实现 compiler version、target triple、sysroot、include_dirs、predefined_macros 探测。
- [ ] 实现 toolchain_fingerprint。
- [ ] workspace 绑定 build context。
- [ ] resolved edges 按 build_context_hash 隔离。
- [ ] compile_commands.json / Makefile / Kconfig 的接入策略。

验收：

- 同一源码在不同 sysroot 下不会共享错误 resolved edges。
- `/opt` 同一工具链被多个 workspace 复用。
- 未识别 build context 时明确降级并提示。

### Phase 7: Heavy Jobs 后台化

目标：clone/vector/semgrep 不阻塞 MCP 在线请求。

任务：

- [ ] Clone detect 改为 job，存 clone groups，而不是无界展开 pairs。
- [ ] MinHash/LSH 使用稳定 hash 和 shingle，增加大桶保护。
- [ ] Vector indexing 改为 changed symbol 增量 job。
- [ ] Semgrep scan 改为 bounded external process job。
- [ ] MCP 工具返回 job_id/status/result summary。

验收：

- 20 万符号 clone detect 不在 MCP 请求内同步执行。
- clone 查询读缓存结果。
- 后台 job 有 cancel、progress、resource budget。

### Phase 8: 生产化

目标：可在共享 Linux 开发机长期运行。

任务：

- [ ] systemd unit。
- [ ] config 文件和权限模板。
- [ ] metrics endpoint。
- [ ] health check。
- [ ] audit log。
- [ ] backup/restore。
- [ ] schema migration。
- [ ] snapshot GC。
- [ ] chaos tests。

验收：

- daemon restart 后自动恢复 workspace registry 和 snapshots。
- 内存、CPU、队列、错误率可观测。
- 权限测试覆盖越权路径、symlink 逃逸、TCP token 错误、跨 UID 查询。

## 15. 测试计划

### 15.1 单元测试

- CAS key 稳定性。
- toolchain fingerprint 稳定性。
- path normalization 和 symlink escape。
- GraphSnapshot generation publish。
- affected frontier。
- MinHash/LSH recall/precision。
- SymbolVersionKey 匹配稳定性。
- SignatureDiff 对参数、返回值、可见性变化的识别。
- EdgeDeltaSummary 对 caller/callee 增删的识别。

### 15.2 集成测试

- 两个用户、同 repo、不同分支。
- clean snapshot 共享。
- dirty overlay 隔离。
- git checkout batch update。
- watcher save-to-query。
- 两个产品分支之间函数正文 diff、签名 diff、调用链 diff。
- 同名不同语义函数应返回 ambiguous。
- daemon restart recovery。
- Python local fallback。

### 15.3 安全测试

- user1 查询 user2 workspace 应拒绝。
- relative path 包含 `..` 应拒绝。
- workspace root symlink 逃逸应拒绝。
- TCP 无 token 或错误 token 应拒绝。
- admin 操作应写审计日志。

### 15.4 性能测试

最低测试矩阵：

| 场景 | 目标 |
| ---- | ---- |
| 10 用户 x 5 workspace x 同 repo clean | 重复 parse 率小于 5% |
| 20 万符号 snapshot load | 小于 5s |
| 100 万符号 snapshot load | 小于 30s |
| 单文件 watcher update | P95 小于 3s |
| get_callers/get_callees | P95 小于 1ms |
| impact depth=3 node_limit=1000 | P95 小于 10ms |
| 函数级 diff_symbol/diff_signature | P95 小于 10ms |
| 小 scope diff_callers/diff_callees | P95 小于 20ms |
| daemon memory | 20 万符号小于 1GB，100 万符号小于 4GB |

## 16. 迁移策略

### 16.1 从本地 SQLite 迁移

1. daemon 注册 workspace。
2. 读取现有 `$HOME/.callwarden/<hash>/callwarden.db`。
3. 导入 file manifests、symbols、calls 到 CAS/snapshot。
4. 校验 stats 一致。
5. 将本地 DB 标记为 legacy backup。

### 16.2 回退

回退方式：

- `CW_MODE=local` 或 `cw --local ...`。
- daemon 不删除 legacy DB。
- enterprise 模式中 daemon 不可用时默认报错，不静默创建新本地 DB。

## 17. 风险与缓解

| 风险 | 缓解 |
| ---- | ---- |
| Rust parser 与 Python parser 不完全对齐 | Phase 1 做 alignment tests，按语言灰度启用 |
| build context 识别不完整 | 将 build_context_hash 显式暴露，未知时降级并提示 |
| daemon 成为单点 | systemd restart，durable job log，snapshot 可从存储重建 |
| snapshot 内存过大 | LRU generation，按 workspace unload，冷 workspace 只留 manifest |
| 权限实现出错 | SO_PEERCRED、路径校验链、安全集成测试必须第一批完成 |
| clone detect 大桶退化 | clone groups、大桶上限、scope 限制、后台 job |
| SQLite 写入仍成瓶颈 | Coordinator 单写 + batch write，必要时再评估 RocksDB/sharded SQLite |
| Python/Rust 双实现复杂 | Python 保留表现层和业务层，核心索引与查询尽快单源化到 Rust |

## 18. 待决策项

1. daemon 是独立 Rust binary，还是先作为 PyO3 扩展由 Python server 拉起。
2. 企业存储第一版继续 SQLite，还是引入 RocksDB 做 CAS/manifest。
3. Rust daemon 是否直接实现 MCP，还是长期保留 Python MCP thin client。
4. Dirty overlay 使用 copy-on-write table，还是 per-workspace overlay manifest。
5. Semgrep 作为 daemon job 管理，还是继续由 Python 调度外部进程。
6. CAS 内容压缩使用 zstd、lz4，还是先保持 SQLite blob。

## 19. 推荐下一步

建议立即执行：

1. Phase 1：把 Rust 多语言 parse pool 接入 `db_build.py` 主路径。
2. Phase 2：实现 daemon skeleton、UDS、SO_PEERCRED、workspace registry。
3. Phase 3：落 Global CAS 和 clean snapshot manifest。

理由：

- Phase 1 能立刻关闭 Python ProcessPool 的历史问题。
- Phase 2 是 enterprise 权限和多用户共享的前提。
- Phase 3 是消灭重复解析和重复 DB 的核心收益。

Phase 4 之后再做 snapshot query service 和秒级 watcher，风险更可控。
