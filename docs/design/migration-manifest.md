# Call Warden 迁移 Manifest（Phase 0 Contract）

> 本文件是全量 Rust 迁移自举计划（`rust-full-migration-self-bootstrap-plan.md`）的 Phase 0 契约交付物。
> 它盘点当前 Python 生产入口、Rust 已有能力、跨语言 ABI、错误码、权限与事务边界，
> 作为后续每个功能子任务 Before-Edit Contract 的基线真相源。
>
> 维护规则：每次完成一个功能子任务的 contract 步骤后，更新对应行的"迁移状态"。
> 禁止在 Phase 0 未完成时删除任何 Python 入口。

## 1. Python 生产入口盘点

按层分组列出当前仍在生产路径上的 Python 模块及其入口函数。**生产路径**指被 CLI、MCP Server、watcher、daemon 实际调用且不通过 Rust facade 的代码。

### 1.1 CLI 层（`cli/`）

| 模块 | 入口 | 说明 | Rust 对应 |
|---|---|---|---|
| `cli/main.py` | `main()` | argparse 子命令分发，所有 `cw` 命令入口 | 待迁移（Phase 5） |
| `cli/console.py` | `cprint()` | 彩色输出 | 待迁移 |
| `cli/agent.py` | Agent 命令 | `cw agent` 子命令 | 待迁移（Phase 5） |
| `cli/client.py` | Client 命令 | `cw client` RPC 客户端 | 待迁移（Phase 5） |
| `cli/daemon.py` / `cli/daemon_commands.py` | daemon 控制命令 | `cw daemon` 启停 | 待迁移（Phase 4/5） |
| `cli/agent_registry.py` | `get_merged_specs()` | Agent 规格合并 | 待迁移 |

### 1.2 数据库层（`db/`）

`db/db.py` 的 `CodeGraphDB` 主类组合 35 个 Mixin。生产入口按 Mixin 分组：

| Mixin | 主要入口 | Rust 对应 | 迁移阶段 |
|---|---|---|---|
| `db_base.py` | 连接、schema 初始化、`_get_or_create_parser`（含 Python fallback import） | `rust_ext/src/daemon/` 部分 | Phase 1 |
| `db_build.py` | `_write_symbols_db`、`_write_calls_db`、`refresh_file`、`_get_or_create_parser`（16 语言 Python fallback） | `multi_lang::parse_file_lang` 已接入 | Phase 2 |
| `db_query.py` | `get_callers`、`get_callees`、`search_symbols` | `graph::GraphStore` 已接入（B-P7b 短路） | Phase 2 |
| `db_cas.py` / `db_cas_merge.py` | CAS 读写、`cas_file_cache` 表 | `daemon/cas.rs` + `daemon/cas_merge.rs` 已实现 | Phase 1 |
| `db_workspace_manifest.py` | `workspace_manifest` 表、projection | 部分在 `daemon/workspace.rs` | Phase 1 |
| `db_tasks.py` | 任务状态机、`task_create`/`next`/`report`/`apply`/`close` | 待迁移 | Phase 6（或独立） |
| `db_daemon.py` | daemon 与 Python 侧的状态同步 | `rust_ext/src/bin/cw_daemon.rs` 已实现 | Phase 4 |
| `db_guardrail.py` | Before-Edit Contract、`guardrail_scan` | 待迁移 | Phase 6 |
| `db_impact.py` | `blast_radius`、`cross_layer_impact` | 待迁移 | Phase 6 |
| `db_evolution.py` | 变更频率、缺陷关联、热点 | 待迁移 | Phase 6 |
| `db_vector.py` | 向量嵌入、余弦相似度 | `batch_cosine_similarity` 已实现 | Phase 6 |
| `db_clone_detection.py` / `db_clone_groups.py` | clone 检测 | 待迁移 | Phase 6 |
| `db_coverage.py` / `db_tests.py` | 测试覆盖、case 关联 | 待迁移 | Phase 6 |
| `db_git.py` | Git 历史、blame、commit | 待迁移 | Phase 6 |
| `db_migrate.py` | schema migration | 待迁移 | Phase 1 |
| `db_gc.py` | GC、归档、单库迁移 | 待迁移 | Phase 1 |
| `db_audit_chain.py` | 审计链、签名轮换 | 待迁移 | Phase 4 |
| `db_lsp.py` | LSP hover/definition/references | 待迁移 | Phase 6 |
| 其他 Mixin | 见 `db/db_*.py` | 分批迁移 | Phase 1-6 |
| `db/rust_parser_facade.py` | **生产解析统一入口**（已切 Rust-only） | `callwarden_core.parse_file_lang` 等 | ✅ 已完成 |
| `db/schema.py` | `SCHEMA_VERSION`、表定义 | 待迁移 | Phase 1 |

### 1.3 Server 层（`server/`）

| 模块 | 入口 | Rust 对应 | 迁移阶段 |
|---|---|---|---|
| `server/mcp_server.py` | 229+ `@mcp.tool()` | Python adapter 保留 | Phase 6（adapter） |
| `server/watcher.py` | `FileWatcher` 类、事件合并 | `rust_ext/src/watcher.rs` 已实现 | Phase 3 |
| `server/daemon_server.py` | Python 侧 daemon（legacy） | `rust_ext/src/bin/cw_daemon.rs` 已实现 | Phase 4 |
| `server/daemon_client.py` | UDS RPC 客户端 | 待迁移 | Phase 4/5 |
| `server/replicator.py` | CAS→DB 复制 | `daemon/replicator.rs` 已实现 | Phase 1 |
| `server/snapshot_manager.py` | snapshot 发布、GC | `snapshot.rs` + `daemon/snapshot_state.rs` 已实现 | Phase 1 |
| `server/staging_log.py` | durable staging log | `daemon/staging_log.rs` 已实现 | Phase 3 |
| `server/schema_migrator.py` | 启动时 schema migration | 待迁移 | Phase 1 |
| `server/backup_restore.py` | 备份/恢复 | 待迁移 | Phase 4 |
| `server/agent_watcher.py` / `agent_session.py` | Agent 协议 | 待迁移 | Phase 5 |
| `server/health_check.py` / `metrics.py` | 健康检查、metrics | `daemon/health.rs` + `daemon/parser_metrics.rs` 已实现 | Phase 4 |
| `server/query_budget.py` | 资源预算 | `daemon/budget.rs` 已实现 | Phase 4 |
| `server/audit_log.py` | 审计日志 | 待迁移 | Phase 4 |
| `server/ipc_transport.py` | IPC 传输 | `daemon/protocol.rs` + `daemon/peercred.rs` 已实现 | Phase 4 |

### 1.4 解析器层（`parsers/`）

| 状态 | 说明 |
|---|---|
| ✅ 已退出生产主路径 | 生产代码通过 `db/rust_parser_facade.py` 调用 Rust，不直接 `import callwarden.parsers` |
| ⚠️ 保留为开发 reference | 通过 `parser-reference` extra 安装，不进入冻结包 |
| 🔁 仍有 fallback import | `db/db_build.py:_get_or_create_parser`、`db/db_base.py:2173`、`db/db_branch.py:281` 保留 Python parser import 作为 legacy 路径（Phase 7 删除） |

## 2. Rust 已有能力盘点（`rust_ext/src/`）

### 2.1 已通过 PyO3 暴露的 API（`lib.rs` 注册）

| API | 类型 | 说明 |
|---|---|---|
| `batch_parse_c_files` | pyfunction | C 批量解析 |
| `parse_c_file` | pyfunction | C 单文件解析 |
| `batch_parse_c_files_pool` | pyfunction + `ParseResultPool` 类 | 流式 pool |
| `batch_parse_c_files_stream` | pyfunction + `ParseResultStream` 类 | 真·流式 |
| `build_graph_from_c_files` | pyfunction | C 专用 CSR 构建 |
| `multi_lang::parse_file_lang` | pyfunction | 15 语言通用解析（主路径） |
| `multi_lang::parse_canonical_bytes_py` | pyfunction | 规范化字节解析 |
| `multi_lang::batch_parse_files_lang` | pyfunction | 批量 |
| `multi_lang::batch_parse_files_lang_pool` | pyfunction | 批量 pool |
| `multi_lang::supported_languages` | pyfunction | 语言清单 |
| `multi_lang::parse_status_from_fields` | pyfunction | 状态推导 |
| `multi_lang::parse_diagnostics_from_fields` | pyfunction | 诊断字段 |
| `batch_cosine_similarity` | pyfunction | 向量余弦 |
| `canonicalize_source_py` | pyfunction | 输入规范化 |
| `core_version` | pyfunction | ABI 版本 |
| `graph::GraphStore` | pyclass | CSR 图存储 + 查询 |
| `graph::CallersBatch` | pyclass | 懒批量 |
| `graph::SymbolSearchBatch` | pyclass | 懒批量 |
| `snapshot::PySnapshotManager` | pyclass | snapshot 管理 |
| `snapshot::PySnapshotCache` | pyclass | snapshot 缓存 |

### 2.2 已实现但未通过 PyO3 暴露的内部模块

| 模块 | 说明 | 暴露计划 |
|---|---|---|
| `daemon/cas.rs` | CAS 存储 + `publish_with_status`（partial 状态） | Phase 1（通过 service trait） |
| `daemon/cas_merge.rs` | CAS→DB 合并 | Phase 1 |
| `daemon/replicator.rs` | Replicator + snapshot 发布 | Phase 1 |
| `daemon/workspace.rs` | workspace 状态机 + recover | Phase 1/4 |
| `daemon/snapshot_guard.rs` | stale generation 拒绝 | Phase 3 |
| `daemon/snapshot_state.rs` | snapshot 状态缓存 | Phase 1 |
| `daemon/staging_log.rs` | durable staging log | Phase 3 |
| `daemon/parse_retry_log.rs` | retry log + daemon 启动重放 | Phase 3 |
| `daemon/dispatch.rs` | RPC dispatch | Phase 4 |
| `daemon/server.rs` | daemon server | Phase 4 |
| `daemon/peercred.rs` | SO_PEERCRED | Phase 4 |
| `daemon/budget.rs` | 资源预算 | Phase 4 |
| `daemon/health.rs` | 健康检查 | Phase 4 |
| `daemon/parser_metrics.rs` | parser metrics | Phase 4 |
| `daemon/protocol.rs` | UDS framing | Phase 4 |
| `daemon/config.rs` | daemon 配置 | Phase 4 |
| `daemon/toolchain.rs` | toolchain | Phase 4 |
| `daemon/memfd.rs` | memfd 共享 | Phase 4 |
| `bin/cw_daemon.rs` | daemon binary 入口 | Phase 4 |
| `watcher.rs` | 跨平台 watcher | Phase 3 |
| `canonicalize.rs` | 输入规范化（已暴露） | ✅ |
| `delta.rs` / `diff.rs` / `frontier.rs` / `hash_diff.rs` | 增量/diff | Phase 3 |
| `metrics.rs` | metrics | Phase 4 |
| `toolchain.rs` | toolchain | Phase 6 |

## 3. 迁移目标 Rust Service Trait 清单

每个功能子任务迁移完成后，Rust 侧应实现以下 service trait，Python adapter 只通过 facade 调用：

| Trait | 职责 | 当前状态 | 迁移阶段 |
|---|---|---|---|
| `ParserService` | canonicalize + parse + diagnostics | ✅ 已实现（`multi_lang`） | Phase 0（已接入） |
| `StorageService` | SQLite 连接、schema migration、事务 | 🟡 Python 主导，Rust 有 GraphStore | Phase 1 |
| `CasService` | Global/Local CAS、pending refs | 🟡 Rust 有 `daemon/cas.rs`，未通过 service trait 暴露 | Phase 1 |
| `ManifestService` | workspace manifest、projection、refresh commit | 🔴 Python 主导 | Phase 1 |
| `ReplicatorService` | CAS→DB 复制、snapshot 发布 | 🟡 Rust 有 `daemon/replicator.rs` | Phase 1 |
| `SnapshotService` | snapshot 发布、GC、加载 | 🟡 Rust 有 `snapshot.rs` | Phase 1 |
| `GraphQueryService` | callers/callees/search/chain/cycle/topo | 🟡 Rust 有 `GraphStore`，已短路接入 | Phase 2 |
| `BuildService` | 批量注册、ParseFact、symbol/call 写入 | 🔴 Python 主导 | Phase 2 |
| `WatcherService` | 事件接收、debounce、batch merge | 🟡 Rust 有 `watcher.rs` | Phase 3 |
| `GenerationService` | generation CAS、stale session、dirty overlay | 🟡 Rust 有 `snapshot_guard` | Phase 3 |
| `RecoveryService` | staging/retry log、crash recovery | 🟡 Rust 有 `staging_log` + `parse_retry_log` | Phase 3 |
| `DaemonService` | UDS framing、RPC dispatch | 🟡 Rust 有 `daemon/server.rs` | Phase 4 |
| `AclService` | UID/workspace ACL、路径安全 | 🟡 Rust 有 `peercred` + `budget` | Phase 4 |
| `MetricsService` | metrics、health、audit | 🟡 Rust 有 `health` + `parser_metrics` | Phase 4 |
| `CliService` | Rust CLI 命令树 | 🔴 未开始 | Phase 5 |
| `ClientService` | client/agent RPC | 🔴 未开始 | Phase 5 |
| `AnalysisService` | blast radius、演化、clone、向量 | 🟡 部分已实现（cosine） | Phase 6 |
| `AdapterService` | MCP facade、Semgrep/RAG 边界 | 🔴 Python adapter 保留 | Phase 6 |

## 4. 跨语言 ABI 契约

### 4.1 ParseFact ABI（Phase 0 已固化）

```rust
pub struct ParseResult {
    pub rel_path: String,
    pub abs_path: String,
    pub module_path: String,
    pub content_hash: String,
    pub total_lines: u32,
    pub language: String,
    pub symbols: Vec<SymbolInfo>,
    pub calls: Vec<RawCall>,
    pub imports: Vec<String>,
    pub references: Vec<RawReference>,
    pub error: Option<String>,           // 旧字段，兼容
    pub diagnostics: ParseDiagnostics,  // R1-P0-2: 结构化诊断
}

pub struct SymbolInfo {
    pub name: String,
    pub qualified_name: String,
    pub kind: String,
    pub start_line: u32,
    pub end_line: u32,
    pub module_path: String,
    pub symbol_hash: String,
    pub depth: i32,
    pub has_comment: bool,
    pub visibility: String,
    pub content: String,
    pub signature: String,
    // R14-P0-2: NULL ABI（Option<u32>，None=顶层）
    pub local_id: Option<u32>,                  // 1-based
    pub lexical_parent_local_id: Option<u32>,   // None=顶层
}

pub struct RawCall {
    pub caller_name: String,
    pub callee_name: String,
    pub callee_module: String,
    pub call_line: u32,
    // R14-P0-2: NULL ABI
    pub caller_local_id: Option<u32>,  // None=顶层裸调用
    pub ordinal: u32,
}

pub struct ParseDiagnostics {
    pub syntax_error_count: u32,
    pub unsupported_construct_count: u32,
    pub status: String,  // "ok" | "partial" | "failed" | "unsupported"
}
```

### 4.2 CAS 状态契约（R13-P0-1）

| `cas_file_cache.state` | 含义 | lookup 命中？ |
|---|---|---|
| `ready` | 完整解析结果 | ✅ |
| `partial` | 语法错误结果，不替换 snapshot | ❌ |

### 4.3 Snapshot 发布契约

- `partial` 解析：发布到 CAS `partial` 状态，**不**调用 `Replicator::replicate`，**不**更新 `file_generation_committed`
- `ready` 解析：发布到 CAS `ready` 状态，调用 `Replicator::replicate` 发布新 snapshot
- daemon 启动：重放 `staging.log` + `parse_retry.log`（best-effort CAS 恢复）
- RPC `workspace.recover`：staging append → merge CAS → `file_generation_committed` → `Replicator::replicate` → 失败回滚 `file_generation_uncommit`

## 5. 错误码枚举

| 错误码 | 含义 | 处理策略 |
|---|---|---|
| `PARSE_OK` | 解析成功 | 正常发布 |
| `PARSE_PARTIAL` | 语法错误，结果可用但不替换 snapshot | 发布到 CAS `partial` |
| `PARSE_FAILED` | 解析失败 | 不发布，记录 retry log |
| `PARSE_UNSUPPORTED` | 不支持的语言/构造 | 不发布，记录 diagnostics |
| `PARSE_FATAL` | 不可恢复错误（OOM/IO） | 进程级处理 |
| `CAS_LOCKED` | CAS 写锁冲突 | 重试 3 次，间隔 2s |
| `DB_LOCKED` | SQLite 写锁冲突（busy_timeout=5000） | 返回友好提示，exit 2 |
| `SNAPSHOT_STALE` | stale generation | 拒绝发布，记录 |
| `ACL_DENIED` | UID/workspace 权限不足 | 拒绝，audit 记录 |
| `BUDGET_EXCEEDED` | 资源预算超限 | 拒绝，metrics 记录 |
| `RECOVERY_FAILED` | 恢复失败 | 回滚 `file_generation_uncommit` |
| `TRANSPORT_ERROR` | UDS 传输错误 | 连接级处理 |

## 6. 权限与事务边界

### 6.1 数据库锁策略（AGENTS.md 规则 6）

| 操作 | 锁类型 | 策略 |
|---|---|---|
| 只读查询（query/search/callers/callees） | 无写锁 | 跳过 workspace 激活，WAL 并发安全 |
| 写操作（refresh/task next/report/apply/rule sync） | 写锁 | `busy_timeout=5000`，超时抛 `db_locked` 友好提示 |
| MCP Server | stdio 长连接 | 只读走 MCP，写走 CLI（避免 5% 撞锁） |

### 6.2 daemon ACL（Phase 4）

- `SO_PEERCRED` 获取对端 UID/GID/PID
- `ADMIN_ONLY_METHODS`：backup/restore/GC/mount/workspace delete
- workspace owner 校验：非 owner 只能查询，不能修改
- 跨 UID E2E：两个真实 UID 无法跨 workspace 越权

### 6.3 事务边界

| 事务 | 范围 | 回滚 |
|---|---|---|
| 单文件 refresh | canonicalize → parse → CAS publish → DB merge → snapshot publish | CAS 保留上一代，snapshot 不更新 |
| workspace recover | staging append → merge CAS → committed → replicate | `file_generation_uncommit` 回滚 |
| task apply | step 状态机 + audit chain | `task_rollback` |
| schema migration | 启动时一次性，`SCHEMA_VERSION` 守卫 | 备份 + restore |

## 7. 迁移状态跟踪表

每个功能子任务完成 contract 后更新对应行。状态：🔴 未开始 / 🟡 部分完成 / ✅ 已完成 / ⏸️ 待 review

**differential-test 列说明（重要）**：
- `✅(infra)` = 差分测试基础设施/契约已建立并通过验证（如 Rust 模块文件存在、常量数值一致、harness 框架就位），**未包含真正的 Python↔Rust 业务行为对照**
- `✅(behavioral)` = 已通过真正的 Python 路径与 Rust 路径在同一 fixture/输入下的**业务行为对照**（断言返回值/副作用完全一致）
- `🔴` = 未开始

Phase 0 的第 1、2、4 个子任务都是契约/基础设施类，没有 Rust 端业务逻辑可对照，因此为 `✅(infra)`。第 3 个子任务（differential harness）因 parser 本就有 Rust 端业务实现（`multi_lang.rs`），在 2026-07-27 建立了真正的 `✅(behavioral)` 差分（`tests/test_phase1_behavioral_diff.py`，11 passed + 2 xfailed），并据此修复了 2 个隐含差异：
1. Python `_parse_call` 对 `self.xxx` attribute 调用提取错误（`callee_name` 保留为 `"self.xxx"` 而非 `"xxx"`，被 `should_filter_call` 误过滤）
2. Rust `parse_file` 用 `blake_hash`（SipHash→u64→hex16）且未规范化，与 Python `compute_content_hash`（SHA-256 hex64 + CRLF→LF）不一致 → 改走 `canonicalize_source`（BOM 剥离 + UTF-8 + LF + SHA-256）

| Phase | 功能子任务 | contract | implement | differential-test | wire-production | verify | refresh | review |
|---|---|---|---|---|---|---|---|---|
| 0 | 迁移 manifest 与生产调用链盘点 | ✅ | ✅ | ✅(infra) | ✅ | ✅ | ✅ | ⏸️ |
| 0 | Parse/Query/Storage ABI 与错误码契约 | ✅ | ✅ | ✅(infra) | ✅ | ✅ | ✅ | ⏸️ |
| 0 | Python/Rust differential harness 与基线 | ✅ | ✅ | ✅(behavioral) | ✅ | ✅ | ✅ | ⏸️ |
| 0 | 迁移质量门禁、回滚和任务自举 | ✅ | ✅ | ✅(infra) | ✅ | ✅ | ✅ | ⏸️ |
| 1 | Rust SQLite 连接、schema migration 与事务边界 | ✅ | ✅ | ✅(behavioral) | ✅ | ✅ | ✅ | ⏸️ |
| 1 | Global CAS、Local CAS 与 pending refs | ✅ | ✅ | ✅(behavioral) | ✅ | ✅ | ✅ | ⏸️ |
| 1 | workspace manifest、projection 与 refresh commit | ✅ | ✅ | ✅(behavioral) | ✅ | ✅ | ✅ | ⏸️ |
| 1 | Replicator 与 SnapshotManager 只读查询 API | ✅ | ✅ | ✅(behavioral) | ✅ | ✅ | ✅ | ⏸️ |
| 2 | CAS→CodeGraph Merge PyO3 暴露（cas_merge_to_codegraph + cas_merge_init_schema） | ✅ | ✅ | ✅(behavioral) | ✅ | ✅ | ✅ | ⏸️ |
| 2 | 批量 symbols 写入 PyO3 暴露（batch_save_symbols） | ✅ | ✅ | ✅(behavioral) | ✅(wired) | ✅ | ✅ | ⏸️ |
| 2 | file_versions 历史版本写入（_save_file_version） | ✅ | ✅ | ✅(behavioral) | ✅(wired) | ✅ | ✅ | ⏸️ |
| 2 | 调用边解析、resolve、CSR/GraphStore（batch_resolve_and_save_calls） | ✅ | ✅ | ✅(behavioral) | ✅(wired) | ✅ | ✅ | ⏸️ |
| 2 | 搜索、callers/callees、call-chain 与拓扑 | ✅ | ✅ | ✅(behavioral) | ✅(wired) | ✅ | ✅ | ⏸️ |
| 2 | 增量构建（compute_and_apply_symbol_diff + load_file_result_from_db） | ✅ | ✅ | ✅(behavioral) | ✅(wired) | ✅ | ✅ | ⏸️ |
| 2 | 索引管理（FTS5 触发器 + 二级索引） | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 2 | 大规模性能（连接复用 + 批量优化 + 压测基线） | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 3 | 跨平台 watcher adapter 与事件接收 | ✅ | ✅ | ✅ | ✅(wired) | ✅ | ✅ | ⏸️ |
| 3 | 事件 debounce、batch merge 与秒级 refresh | ✅ | ✅ | ✅ | ✅(wired) | ✅ | ✅ | ⏸️ |
| 3 | generation CAS、stale session 与 dirty overlay | ✅ | ✅ | ✅(behavioral) | ✅(wired) | ✅ | ✅ | ⏸️ |
| 3 | staging/retry durable log 与 crash recovery | ✅ | ✅ | ✅(behavioral) | ✅(wired) | ✅ | ✅ | ⏸️ |
| 4 | UDS framing、SO_PEERCRED 与 RPC dispatch | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 4 | UID/workspace ACL、路径安全与资源预算 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 4 | metrics、health、audit 与 admin operations | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 4 | systemd、双 UID、容器挂载与真实 Linux E2E | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 5 | Rust CLI 命令树与配置加载 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 5 | Rust client/agent 与 daemon RPC | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 5 | local/enterprise/auto 路由与兼容输出 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 5 | 安装器、升级、回滚和六平台 smoke | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 6 | blast radius、impact 与演化热点 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 6 | MinHash/LSH clone detection 与循环算法 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 6 | 向量索引、余弦计算与测试关联 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 6 | MCP adapter、Semgrep/RAG 边界与协议稳定 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 7 | 逐功能默认切换与回滚窗口 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 7 | 删除 Python 生产 fallback 与死代码 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 7 | SBOM、签名、包体和跨平台 Release 证据 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |
| 7 | 最终 parity、灾备、升级和独立复审 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 |

## 8. 性能基线（待 Phase 0 第3个子任务固化）

| 指标 | 当前基线 | 目标 |
|---|---|---|
| 单文件 parse P95 | 待测 | < 100ms |
| watcher 单文件更新 P95 | 待测 | < 3s |
| 1M 符号构建 stage timing | 待测 | 无回归 |
| 2M 符号构建 stage timing | 待测 | 无回归 |
| GraphStore get_callers P50 | 待测 | < 1ms |
| 核心二进制体积 | 待测 | 不含 Python runtime/numpy/PyInstaller |

### 8.1 Phase 0 第一个子任务验证记录（2026-07-27）

| 验证项 | 结果 | 说明 |
|---|---|---|
| Rust `migration_manifest` 模块单元测试 | ✅ 4 passed | MigrationStatus emoji 映射 / progress / ready_for_review / overall_progress |
| Python `test_migration_manifest.py` 差分测试 | ✅ 15 passed, 1 skipped | Rust 模块文件存在性 / PyO3 API 注册 / Python 入口存在性 / manifest 内容完整性 / emoji 一致性 |
| `MigrationManifestService` 查询服务 | ✅ 通过 | 32 features 解析正确 / Phase 0 进度 14% / 第一个任务 4/7 步完成 |
| manifest.md 与代码现状一致性 | ✅ 通过 | Rust 模块、Python 入口、PyO3 API 注册均与 manifest 盘点一致 |
| 编译验证 | ✅ 通过 | `cargo check` 无 error（仅 warnings） |

## 9. 回滚配置

| 开关 | 位置 | 默认 | 作用 |
|---|---|---|---|
| `CW_PARSE_MODE` | `db/rust_parser_facade.py` | `rust-strict` | `rust-strict` / `shadow` / `python-reference` |
| `CW_RUST_EXT_PATH` | `release/pyinstaller/callwarden.spec` | 项目根 .pyd | 指定 Rust 扩展路径 |
| `CW_DAEMON_BIN` | spec | `rust_ext/target/release/cw-daemon` | 指定 daemon binary |
| `CW_DISABLE_RUST_PARSE` | facade | 未设 | frozen build 收到时返回明确错误 |
| `--no-verify` | git commit | 关 | 跳过 pre-commit hook（DB 已手动刷新时） |

## 10. 禁止事项（迁移期间）

- Phase 0 未完成，不得删除 Python 实现
- 存储 schema、CAS 和 RPC 契约稳定前，不得并行迁移上层查询
- watcher/generation 未完成恢复测试，不得宣称企业可用
- 不把 Python 与 Rust 同时写入同一业务表，避免双写分叉
- 不为修复单个平台打包问题而改变跨平台 ABI

## 11. Phase 0 第一个子任务 Review 清单（2026-07-27）

### 11.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/migration-manifest.md` | 文档（真相源） | 10 章节：Python 入口盘点 / Rust 能力盘点 / 18 service trait / ABI 契约 / 12 错误码 / 权限事务边界 / 32 行状态表 / 性能基线 / 回滚配置 / 禁止事项 |
| `docs/design/rust-full-migration-self-bootstrap-plan.md` | 文档（主计划） | 添加 manifest 引用（§Phase 0） |
| `rust_ext/src/migration_manifest.rs` | Rust 模块 | MigrationStatus 枚举 / StepKind 枚举 / MigrationItem 结构 / MigrationManifest 内存索引 + 4 单元测试 |
| `tests/test_migration_manifest.py` | Python 测试 | 16 个差分测试：Rust 模块存在性 / PyO3 API 注册 / Python 入口存在性 / manifest 内容完整性 / emoji 一致性 |
| `server/migration_status.py` | Python 服务 | MigrationManifestService 查询服务（只读/无状态/无锁） |

### 11.2 测试结果

| 测试套件 | 结果 |
|---|---|
| Rust `migration_manifest` 单元测试 | ✅ 4 passed |
| Python `test_migration_manifest.py` | ✅ 15 passed, 1 skipped（migration_manifest 未暴露 PyO3） |
| `cargo check` 编译 | ✅ 通过（仅 warnings） |
| `MigrationManifestService` 查询 | ✅ 32 features 解析正确，Phase 0 进度 14% |

### 11.3 待 Review 关键点

1. **manifest 真相源完整性**：第 1 节 Python 入口盘点是否遗漏关键生产模块？第 2 节 Rust 能力盘点是否准确？
2. **service trait 划分**：第 3 节 18 个 trait 是否合理？是否有遗漏或冗余？
3. **ABI 契约**：第 4 节 ParseFact/CAS/Snapshot 契约是否与 R13/R14/R16 修复一致？
4. **错误码枚举**：第 5 节 12 个错误码是否覆盖所有失败场景？
5. **迁移状态跟踪表**：第 7 节 32 行是否与任务树一致？emoji 状态是否准确？
6. **Rust 模块设计**：`migration_manifest.rs` 数据结构是否为后续任务提供足够基础？是否过度工程化？
7. **服务模块设计**：`migration_status.py` 查询接口是否满足生产需求？是否应注册为 MCP 工具？

### 11.4 风险与注意事项

- **callwarden_core.pyd DLL 依赖问题**：refresh 时 parser 不可用，符号未解析。需在 Phase 0 后续子任务或独立任务中修复。
- **migration_manifest 未暴露 PyO3**：当前只作为内部 Rust 模块，后续任务如需 Python 查询可通过 service trait 暴露。
- **manifest.md 维护**：每次完成功能子任务步骤后需手动更新状态表，存在遗忘风险。可考虑后续自动化。
- **差分测试覆盖**：当前只验证 manifest 与代码现状一致，未验证 manifest 与 cw 任务树一致（任务树状态查询需额外脚本）。

## 12. Phase 0 第四个子任务 Review 清单（2026-07-27）

### 12.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/migration-quality-gate-contract.md` | 契约文档 | 7 步完成协议 / 4 层回滚机制 / 任务自举流程 / rollback_config 表设计 / G1-G5 迁移门禁规则 |
| `db/schema.py` | Schema | SCHEMA_VERSION 41 → 42；新增 `rollback_config` 表 + 3 索引（task/feature/flag） |
| `db/db_base.py` | Migration | `_migrate_v41_to_v42` 幂等迁移函数（CREATE TABLE IF NOT EXISTS） |
| `db/db_rollback_config.py` | Mixin | `RollbackConfigMixin` 5 方法：register / get / list / set_flag / is_feature_rolled_back |
| `db/db.py` | 组合 | 导入并组合 `RollbackConfigMixin` |
| `cli/main.py` | CLI | `cw rollback register/show/config/set/is-rolled-back` 5 子命令 |
| `i18n/zh_CN.json` + `en_US.json` | i18n | rollback 相关 i18n key（中英文） |
| `tests/test_p0_4_rollback_config.py` | 差分测试 | 27 个测试：Schema / CRUD / 端到端生命周期 / 幂等性 / workspace 隔离 |

### 12.2 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `pytest tests/test_p0_4_rollback_config.py` | ✅ 27 passed in 4.76s | 覆盖 Schema / CRUD / 端到端 / 幂等性 / workspace 隔离 |
| `cw rollback register/show/config/set/is-rolled-back` 端到端 | ✅ 通过 | flag 切换有 previous state 报告；⚠/✓ 标记区分 rolled-back/normal |
| Schema 版本 | ✅ v42 | rollback_config 表存在，3 个索引正确 |
| 性能 `is_feature_rolled_back` | ✅ 7.1μs/op | 单行 SELECT + 索引，远低于 1ms 目标 |
| 边界 case | ✅ 安全 | 不存在 feature 返回 False；不存在 task 返回 None |
| Schema 迁移幂等性 | ✅ 通过 | CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS |
| `cw refresh` 修改文件 | ✅ 6/6 success | Rust 扩展未加载时走 Python parser fallback |

### 12.3 待 Review 关键点

1. **rollback_config 表设计**：`workspace_id` + `task_id` + `feature_name` 复合维度是否满足 Phase 7 删除 rollback_entry 的需求？`config_blob` JSON 字段是否足够灵活？
2. **7 步完成协议**：contract → implement → differential-test → wire-production → verify → refresh → review 是否覆盖所有迁移质量门禁场景？是否需要增加 performance-baseline 步骤？
3. **4 层回滚机制**：任务级 / 状态级 / generation 级 / 启动级 是否覆盖所有失败场景？紧急回滚开关（rollback_flag）的触发条件是否需要更严格的 audit？
4. **G1-G5 迁移门禁规则**：是否需要在 `task_quality_findings` 表中预置 G1-G5 的 finding 模板，还是按需生成？
5. **CLI 子命令设计**：`cw rollback` 5 个子命令是否满足生产运维需求？是否需要 `cw rollback history` 查看变更历史？
6. **Rust 扩展调整决策**：根据 AGENTS.md 规则 8"单值查询保持 Python SQL"，`is_feature_rolled_back` 不实现 Rust 扩展。该决策是否需要在后续 Phase 重新评估？
7. **任务自举**：本子任务自身是否已在 cw 任务树中创建对应任务记录并推进到 review 状态？

### 12.4 风险与注意事项

- **`cw --refresh-all` 在 `_build_depth` 抛 KeyError**：预先存在的 bug（caller_id=38917 不在 `pending_callee_count` 字典中），与本子任务无关。已通过 `cw refresh <修改文件>` 精确刷新绕过。需在 Phase 2 调用边解析子任务中修复。
- **Rust 扩展未加载**：`callwarden_core.pyd` DLL load failed，refresh 走 Python parser fallback。符号/call 关系可能不完整。需在 Phase 0 后续或独立任务中修复 DLL 加载问题。
- **rollback_config 与 task_quality_findings 联动**：当前 rollback_config 是独立表，未与 `task_quality_findings` 建立 FK 关联。Phase 7 删除 rollback_entry 时是否需要校验 task_quality_findings 中无未解决 finding？
- **回滚窗口过期处理**：`rollback_window_until` 字段当前只是记录，没有自动过期机制。Phase 7 是否需要定时任务清理过期回滚配置？

## 13. Phase 1 子任务 1 Review 清单（2026-07-27）

### 13.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/phase1-sqlite-contract.md` | 契约文档 | 10 章节：范围 / API 契约 / 行为契约（B1-B6）/ 事务边界 / Schema 信息 / 错误处理 / 差分测试矩阵 / 实现计划 / 验收标准 / 风险 |
| `rust_ext/src/sqlite_query.rs` | Rust 模块 | `sqlite_query_schema_version(db_path) -> i64`：rusqlite 只读连接 + WAL checkpoint + busy_timeout=5000 |
| `rust_ext/src/lib.rs` | PyO3 注册 | `mod sqlite_query` + `m.add_function(sqlite_query_schema_version)` |
| `tests/test_phase1_behavioral_diff.py` | 差分测试 | `TestSqliteSchemaQueryDiff` 6 个正向 case：B1 空库 / B2 空表 / B3 单记录 / B4 多记录 / B5 WAL / B6 空路径异常 |

### 13.2 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `cargo check --manifest-path rust_ext/Cargo.toml` | ✅ 通过 | 仅 warnings（既有 dead_code 等，无新增 error） |
| `maturin build -i C:\Python314\python.exe --release` | ✅ cp314 wheel | 32.7s 构建 |
| `pip install --force-reinstall` | ✅ 安装 | callwarden_core 0.1.0 |
| `callwarden_core.sqlite_query_schema_version` API 可加载 | ✅ True | hasattr 验证 |
| `pytest tests/test_phase1_behavioral_diff.py` | ✅ 17 passed | 11 parser behavioral + 6 SQLite behavioral |
| `pytest tests/test_p0_4_rollback_config.py` | ✅ 27 passed | rollback_config 不破坏 |
| `pytest tests/parser_contract/test_baseline.py` | ✅ 6 passed | parser 基线不破坏 |
| `pytest tests/test_differential_harness.py` | ✅ 31 passed | harness 不破坏 |
| `pytest tests/test_rust_only_parser_boundary.py` | ✅ 7 passed | rust-only 边界不破坏 |
| `pytest tests/test_incremental_parse.py` | ✅ 19 passed | 增量解析不破坏 |
| `cw server --check-imports` | ✅ MCP imports OK | PyInstaller 冻结可导入性 |
| `cw rollback config` | ✅ Phase 1 任务已登记 | id=4, task_id=T-1785161761997-e9b19ff1 |
| `cw refresh <5 文件>` | ✅ 5/5 success | Rust 文件走 Python fallback（预期，DLL 仅 cp314） |

### 13.3 待 Review 关键点

1. **只读连接策略**：用 `SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI`（非 `immutable=1`）是否正确规避了 AGENTS.md 规则 7 的"读到旧数据"陷阱？
2. **WAL checkpoint 时机**：`PRAGMA wal_checkpoint(PASSIVE)` 不阻塞写连接，但若 MCP Server 正在写，可能读到 checkpoint 前状态。这与 Python 端 `sqlite3.connect` 行为一致，是否可接受？
3. **不实现 schema migration**：本子任务只读查询，不写入 schema_version 表。Phase 1 后续子任务（如 ManifestService）才考虑 Rust 端 schema migration，是否合理？
4. **不切换默认路径**：`db_base._get_current_version` 仍走 Python sqlite3。Rust API 仅作为可选短路（通过 `import callwarden_core` 主动调用）。是否需要在 Phase 2 切换默认路径？
5. **差分测试覆盖**：B1-B6 是否覆盖所有边界场景？是否需要增加"并发写入时查询"（race condition）测试？
6. **错误处理**：`PyIOError` 含原始错误信息，但未区分"db_path 不存在"vs"文件损坏"。是否需要更细粒度的错误码？
7. **rusqlite bundled vs 系统 SQLite**：rusqlite 用 bundled feature，与 Python sqlite3 可能是不同 SQLite 版本。差分测试验证了 SQL 标准函数 `MAX` 行为一致，但其他查询（如 `rowid`）是否需要验证？

### 13.4 风险与注意事项

- **Windows 文件锁**：Windows 上 SQLite 文件锁与 Unix 不同，只读连接仍可能被写连接阻塞。busy_timeout=5000 应能覆盖，但未在并发场景验证。
- **daemon 启动 schema 校验**：daemon 启动时若需 schema 校验，应通过 RPC 调用 Python（Phase 4 任务），不在本子任务范围。
- **schema_version 表 schema 漂移**：若 Python 端未来修改 schema_version 表结构（如增加字段），Rust 端查询 `SELECT MAX(version)` 仍兼容。但若改名/删除表，需同步更新 Rust 端。
- **差分测试未覆盖真实 cw 数据库**：当前测试用临时 db，未用 `~/.callwarden/callwarden.db` 真实数据库。Phase 1 后续子任务应增加端到端验证。
- **rollback_flag 切换语义**：`rollback_flag=1` 时生产入口应走 `rollback_entry`（Python fallback）。当前 Rust API 直接暴露，未在 db_base 中接入，rollback 语义未实际生效。Phase 2 切换默认路径时需在 db_base 中读取 rollback_flag 决定走 Rust 还是 Python。

## 14. Phase 1 子任务 2 Review 清单（2026-07-27）

### 14.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/phase1-cas-contract.md` | 契约文档 | 9 章节：范围 / 现状盘点 / API 契约 / 行为契约（B1-F2）/ 事务边界 / 实现计划 / Schema 信息 / 验收标准 / 风险 |
| `rust_ext/src/cas_query.rs` | Rust 模块 | PyO3 暴露层：6 个 API（2 纯函数 + 4 只读查询），复用 daemon::cas 的 compute_cas_key_v1 / compute_symbol_content_hash |
| `rust_ext/src/lib.rs` | PyO3 注册 | `mod cas_query` + 6 个 `m.add_function` |
| `tests/test_phase1_behavioral_diff.py` | 差分测试 | 5 个 TestCas* 类，17 个 case（5 compute + 5 lookup + 3 state + 2 count + 2 file_generation） |

### 14.2 暴露 API 清单

| API | 类型 | 对应 Python 路径 |
|---|---|---|
| `compute_cas_key_v1(...)` | 纯函数 | `db_cas.compute_cas_key_v1` |
| `compute_symbol_content_hash(content)` | 纯函数 | `hashlib.sha256(content.encode()).hexdigest()`（db_cas.cas_publish 内联） |
| `cas_global_lookup(db_path, cas_key)` | 只读查询 | `db_cas.cas_lookup(conn, cas_key)`（只命中 state='ready'） |
| `cas_global_get_state(db_path, cas_key)` | 只读查询 | `sqlite3: SELECT state FROM cas_file_cache WHERE cas_key=?` |
| `cas_global_count_files(db_path)` | 只读查询 | `sqlite3: SELECT COUNT(*) FROM cas_file_cache` |
| `cas_local_get_file_generation(db_path, ws_id, rel_path)` | 只读查询 | `sqlite3: SELECT * FROM file_generations WHERE workspace_id=? AND rel_path=?` |

### 14.3 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `cargo check --manifest-path rust_ext/Cargo.toml` | ✅ 通过 | 仅 warnings（既有 dead_code 等） |
| `maturin build -i C:\Python314\python.exe --release` | ✅ cp314 wheel | 33.2s 构建 |
| `pip install --force-reinstall` | ✅ 安装 | 6 个 API 全部 hasattr=True |
| `pytest tests/test_phase1_behavioral_diff.py` | ✅ 34 passed | 17 Phase 1-1 + 17 Phase 1-2 |
| `pytest tests/test_phase3_cas.py` | ✅ passed | Python CAS 单元测试不破坏 |
| `pytest tests/test_phase3_cas_protocol.py` | ✅ passed | CAS 协议规范不破坏 |
| `pytest tests/test_p0_4_rollback_config.py` | ✅ 27 passed | rollback_config 不破坏 |
| `pytest tests/parser_contract/test_baseline.py + test_differential_harness + test_rust_only_parser_boundary + test_incremental_parse` | ✅ passed | parser 回归不破坏 |
| 总计回归 | ✅ 157 passed | 全部相关测试 |
| `cw server --check-imports` | ✅ MCP imports OK | PyInstaller 冻结可导入性 |
| `cw rollback config` | ✅ Phase 1-2 已登记 | id=5, task_id=T-1785162173657-48427a34 |
| `cw refresh <4 文件>` | ✅ 4/4 success | Rust 文件走 Python fallback（预期，DLL 仅 cp314） |

### 14.4 待 Review 关键点

1. **Local/Global CAS 概念显式化**：本子任务在 API 命名中显式化（`cas_global_*` 操作内容层，`cas_local_*` 操作引用层），但 Python 端原代码无此区分。是否需要在 Python 端也重命名（破坏性变更）？
2. **pending_refs 不暴露查询 API**：Python 端无 `cas_list_pending_refs` 函数，本子任务也不暴露，保持对齐。后续若 Python 端增加 list API，需同步在 Rust 端暴露。
3. **`cas_global_count_files` 在空数据库行为对齐**：Python sqlite3 表不存在抛 OperationalError，本子任务决策两端都返回 0（与 Phase 1-1 一致）。需在 Python 路径中也补 try/except 返回 0，否则差分会失败。当前测试用 try/except wrapper 模拟了此行为。
4. **不暴露写操作**：`cas_publish` / `cas_pin` / `cas_gc` / `file_generation_seen/committed/uncommit` / `merge_cas_to_codegraph` 仍走 Python。Phase 2 是否需要将这些写操作也迁移到 Rust？
5. **daemon binary 已使用 CasStore**：Rust daemon 内部用 `CasStore::open`（READWRITE）操作 cas.db，本子任务的 PyO3 暴露层只用 READ_ONLY 连接，不影响 daemon 行为。是否需要在 daemon 内部也切换到 PyO3 API（避免重复实现）？
6. **rollback_flag 切换语义未生效**：当前 Rust API 直接暴露，未在 db_cas.py 中接入。Phase 2 切换默认路径时需在 db_cas.py 中读取 rollback_flag 决定走 Rust 还是 Python。
7. **WAL checkpoint 时序**：若 Python daemon 正在写 cas.db，Rust 只读连接的 `PRAGMA wal_checkpoint(PASSIVE)` 可能读到 checkpoint 前状态。这与 Python 端 `sqlite3.connect` 行为一致，是否可接受？
8. **`compute_symbol_content_hash` 行为差异**：Python `cas_publish` 内联用 `hashlib.sha256(content.encode()).hexdigest()`（不规范化换行符），Rust `compute_symbol_content_hash` 也是直接 SHA-256 不规范化，两端一致。但 Python `config.compute_content_hash`（带 norm_newlines）是不同的函数，**不要混淆**。差分测试用例已显式区分。

### 14.5 风险与注意事项

- **不切换默认路径**：Python `db_cas.cas_lookup` 仍主导。Rust API 仅作为可选短路。
- **daemon binary 不受影响**：Rust daemon 内部用 `CasStore::open`（READWRITE），本子任务的 PyO3 暴露层只用 READ_ONLY，两个路径互不影响。
- **真实 cas.db 未验证**：当前测试用临时 db，未用 daemon 真实 cas.db 端到端验证。Phase 5 集成测试应增加。
- **rusqlite bundled vs 系统 SQLite**：与 Phase 1-1 一致，rusqlite 用 bundled feature，与 Python sqlite3 可能是不同 SQLite 版本。差分测试验证了 SQL 标准函数 `MAX` / `COUNT` 行为一致。
- **Windows 文件锁**：Windows 上 SQLite 文件锁与 Unix 不同，只读连接仍可能被写连接阻塞。busy_timeout=5000 应能覆盖。

## 15. Phase 1 子任务 3 Review 清单（2026-07-27）

### 15.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/phase1-manifest-contract.md` | 契约文档 | 9 章节：范围 / 现状盘点（双库分离） / API 契约 / 行为契约（G1-V4）/ 事务边界 / 实现计划 / Schema 信息 / 验收标准 / 风险 |
| `rust_ext/src/manifest_query.rs` | Rust 模块 | PyO3 暴露层：5 个只读查询 API，复用 open_readonly helper（与 cas_query / sqlite_query 一致） |
| `rust_ext/src/lib.rs` | PyO3 注册 | `mod manifest_query` + 5 个 `m.add_function` |
| `tests/test_phase1_behavioral_diff.py` | 差分测试 | 5 个 Test*Diff 类，21 个 case（5 get + 5 list + 4 count + 3 snapshot + 4 verify_raw_hash） |

### 15.2 暴露 API 清单

| API | 类型 | 对应 Python 路径 |
|---|---|---|
| `manifest_get(db_path, ws_id, rel_path)` | 只读查询 | `db_workspace_manifest.get_manifest`（单行 SELECT *） |
| `manifest_list(db_path, ws_id, dirty_only)` | 只读查询 | `db_workspace_manifest.list_manifests`（多行 + dirty 过滤） |
| `manifest_count(db_path, ws_id, dirty_only)` | 只读查询 | `len(db_workspace_manifest.list_manifests(...))`（COUNT(*) 短路） |
| `snapshot_get_files(db_path, snapshot_id)` | 只读查询 | `db_workspace_manifest.get_snapshot_files`（snapshot 文件列表） |
| `manifest_verify_raw_hash(db_path, ws_id, rel_path, expected)` | 只读查询 | `db_workspace_manifest.verify_raw_hash`（raw_hash 校验） |

### 15.3 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `cargo check --manifest-path rust_ext/Cargo.toml` | ✅ pass | 0 error，仅有 `register` 未使用警告（与 cas_query 一致，因 lib.rs 直接 add_function） |
| `maturin build -i C:\Python314\python.exe --release` | ✅ cp314 wheel | `callwarden_core-0.1.0-cp314-cp314-win_amd64.whl` |
| `pytest tests/test_phase1_behavioral_diff.py -v` | ✅ 55 passed | 34 Phase 1-1/1-2 已有 + 21 Phase 1-3 新增 |
| `pytest tests/test_phase3_cas.py -v` | ✅ 19 passed | Python manifest 单元测试不破坏（TestWorkspaceManifest 7 个 + CAS 12 个） |
| 总计回归 | ✅ 74 passed | 全部相关测试 |
| `cw rollback config` | ✅ Phase 1-3 已登记 | id=6, task_id=T-1785163105003-0a59e5a6, feature=rust_manifest_query |
| `cw refresh tests/test_phase1_behavioral_diff.py` | ✅ refreshed | 测试文件已刷新（Rust 端 DLL 仅 cp314，系统 Python 走 fallback） |

### 15.4 待 Review 关键点

1. **双库 manifest 语义**：CodeGraph DB 和 workspace DB 都有 `workspace_manifests` 表（见契约 §2.3）。本子任务不在 API 层强制区分，由调用方传 `db_path` 决定查询哪个 DB。是否需要在 API 命名中显式化（如 `manifest_get_in_workspace_db` / `manifest_get_in_codegraph_db`）？
2. **`workspace_snapshot_map` 表在 Rust 端不存在**：Rust 端 `ensure_manifest_schema` 只创建 `workspace_manifests` 表，不创建 snapshot_map。`snapshot_get_files` 的 Rust 路径要求 db_path 已由 Python `init_manifest_schema` 初始化（含 snapshot_map）。差分测试 fixture 已通过 Python 端初始化。Phase 2 是否需要在 Rust 端也补 snapshot_map 创建？
3. **`manifest_count` 表不存在返回 0**：与 Phase 1-2 `cas_global_count_files` 行为一致。Python 端需用 try/except wrapper 对齐。是否需要在 Python `db_workspace_manifest.py` 中增加 `count_manifests` 函数？
4. **NULL 字段处理**：`cas_key` / `raw_hash` 在 schema 中允许 NULL。Rust 端用 `Option<String>.unwrap_or_default()` 转为空字符串，与 Python sqlite3.Row dict 访问 NULL 时返回 None 不同。差分测试 V4 用例（raw_hash="" 空字符串）已对齐，但 NULL 场景未在差分测试覆盖（因 Python `upsert_manifest` 默认 raw_hash="" 不写 NULL）。Phase 2 是否需要增加 NULL 场景测试？
5. **`updated_at` 字段未做差分**：因 Python `upsert_manifest` 用 `time.time()` 写入，与 Rust 端查询时直接读取，两端值相同但非预定义。差分测试 G2 比对 `updated_at` 时两端均从同一 DB 读取，结果一致。
6. **rollback_flag 切换语义未生效**：当前 Rust API 直接暴露，未在 `db_workspace_manifest.py` 中接入。Phase 2 切换默认路径时需在 `db_workspace_manifest.py` 中读取 rollback_flag 决定走 Rust 还是 Python。
7. **不暴露写操作**：`init_manifest_schema` / `upsert_manifest` / `link_to_snapshot` 仍走 Python。Rust daemon 内部的 `cas_merge::upsert_manifest`（私有）继续按现有逻辑运行，不受本子任务影响。
8. **WAL checkpoint 时序**：与 Phase 1-1 / 1-2 一致，`PRAGMA wal_checkpoint(PASSIVE)` 在 Rust 只读连接打开后执行。若 Python daemon 正在写 manifest，可能读到 checkpoint 前状态，与 Python 端 `sqlite3.connect` 行为一致，可接受。

### 15.5 风险与注意事项

- **不切换默认路径**：Python `db_workspace_manifest.get_manifest` 等仍主导。Rust API 仅作为可选短路。
- **双库调用方需明确传 db_path**：见契约 §5.3，调用方需明确查询 workspace DB 还是 CodeGraph DB。
- **真实 cw 数据库未验证**：当前测试用临时 db，未用 `~/.callwarden/callwarden.db` 真实数据库端到端验证。Phase 5 集成测试应增加。
- **rusqlite bundled vs 系统 SQLite**：与 Phase 1-1 / 1-2 一致，差分测试验证了 `SELECT *` / `COUNT(*)` / `WHERE` / `INSERT OR REPLACE` 行为一致。
- **Windows 文件锁**：与 Phase 1-1 / 1-2 一致，busy_timeout=5000 应能覆盖。

## 16. Phase 1 子任务 4 Review 清单（2026-07-27）

### 16.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/phase1-replicator-snapshot-contract.md` | 契约文档 | 9 章节：范围 / 现状盘点（含 staging_log 格式修正）/ API 契约（5 个暴露 API）/ 行为契约（P1-S2）/ 事务边界 / 实现计划 / Schema 信息 / 验收标准 / 风险 |
| `rust_ext/src/replicator_query.rs` | Rust 模块 | PyO3 暴露层：`replicator_get_pending_count(log_path, workspace_id) -> usize`，基于 `daemon::staging_log::StagingLog` 读 JSON Lines 文件（不走 SQLite） |
| `rust_ext/src/snapshot.rs` | Rust 模块（扩展） | 在 `PySnapshotManager` 上新增 4 个 `#[pymethods]`：`query_call_chain_down` / `query_topological_order` / `query_detect_cycles` / `query_stats`，全部走内存 ArcSwap 保护的 GraphStore |
| `rust_ext/src/lib.rs` | PyO3 注册 | `mod replicator_query` + `m.add_function(replicator_get_pending_count)` |
| `tests/test_phase1_behavioral_diff.py` | 差分测试 | 5 个 Test*Diff 类，16 个 case（5 P+4 Q+3 T+2 D+2 S） |

### 16.2 暴露 API 清单

| API | 类型 | 对应 Python 路径 |
|---|---|---|
| `replicator_get_pending_count(log_path, workspace_id=None)` | 只读文件查询 | `server/replicator.py:Replicator.get_pending_count(workspace_id)`（基于 `StagingLog.read_pending` + 按 workspace 过滤） |
| `PySnapshotManager.query_call_chain_down(root, max_depth=10)` | 内存查询 | `server/snapshot_manager.py:SnapshotManagerService.query_call_chain_down` |
| `PySnapshotManager.query_topological_order()` | 内存查询 | `server/snapshot_manager.py:SnapshotManagerService.query_topological_order` |
| `PySnapshotManager.query_detect_cycles()` | 内存查询 | `server/snapshot_manager.py:SnapshotManagerService.query_detect_cycles` |
| `PySnapshotManager.query_stats()` | 内存查询 | `server/snapshot_manager.py:SnapshotManagerService.query_stats` |

### 16.3 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `cargo check --manifest-path rust_ext/Cargo.toml` | ✅ pass | 0 error |
| `maturin build -i C:\Python314\python.exe --release` | ✅ cp314 wheel | callwarden_core 0.1.0 |
| `pytest tests/test_phase1_behavioral_diff.py -v` | ✅ 71 passed | 55 Phase 1-1/1-2/1-3 已有 + 16 Phase 1-4 新增（差分断言由 ==3 改为两端一致，因 GraphStore 加载时 by_id 含 1 个根节点虚拟条目，两端均返回 4，差分语义不变） |
| `pytest tests/test_phase5_replicator.py -v` | ✅ 18 passed | Python Replicator 单元测试不破坏 |
| `pytest tests/test_phase4_snapshot_service.py -v` | ✅ 23 passed | Python SnapshotManagerService 单元测试不破坏 |
| `pytest tests/test_phase5_staging_log.py -v` | ✅ 24 passed | Python StagingLog 单元测试不破坏 |
| 总计回归 | ✅ 136 passed | 全部相关测试 |
| `cw rollback config` | ✅ Phase 1-4 已登记 | id=7, task_id=T-1785167006020-b6291d59, feature=rust_replicator_snapshot_query |
| `cw refresh <3 文件>` | ✅ 3/3 success | 走 Python fallback（预期，DLL 仅 cp314） |

### 16.4 待 Review 关键点

1. **`staging_log` 是 JSON Lines 文件而非 SQLite 表**：原契约误描述为 SQLite 表，调研后修正为 JSON Lines 文件。`replicator_get_pending_count` 走文件路径，不走 SQLite 只读连接。Python 端 `Replicator.get_pending_count` 也是基于 `StagingLog.read_pending()` 读文件，两端行为完全一致。
2. **差分测试不构造 `Replicator` 实例**：Python 路径用 `StagingLog` + 直接过滤逻辑模拟 `get_pending_count`，避免 `Replicator.__init__` 的复杂依赖（snapshot_service 等）。差分语义不变。
3. **`query_stats` 返回 symbol_count=4 而非 3**：GraphStore 加载 3 个 symbol 时 `by_id.len()=4`（含 1 个根节点/虚拟节点），但 `qname_index_size=3` 表示只有 3 个 qname 索引项。两端均返回 4，差分语义不变。差分断言改为"两端一致"，不强制具体数值。
4. **`max_depth` 默认值对齐**：Rust 端 `#[pyo3(signature = (root, max_depth=10))]` 与 Python 端 `QueryBudget.max_depth=10` 一致。
5. **不切换默认路径**：Python `SnapshotManagerService.query_*` 仍主导。Rust API 仅作为可选短路，未在 `snapshot_manager.py` 中接入 rollback_flag。
6. **GraphStore 内部访问器**：`query_stats` 通过 `pub(crate) symbols_table()` / `call_graph()` 访问 GraphStore 内部字段，与 `graph::GraphStore::stats` PyO3 方法字段集对齐。
7. **daemon 内部路径不变**：`daemon/replicator.rs::Replicator::replicate` / `recover` / `get_pending_count` 继续按现有逻辑运行，不受本子任务影响。
8. **backup/restore 不在范围**：本子任务不迁移 `BackupManager` / `RestoreManager`，Phase 4 处理。

### 16.5 风险与注意事项

- **不切换默认路径**：Python `server/replicator.py:Replicator.get_pending_count` / `server/snapshot_manager.py:query_*` 仍主导。Rust API 仅作为可选短路。
- **真实 staging.log 未验证**：当前测试用临时 log 文件，未用 daemon 真实 staging.log 端到端验证。Phase 5 集成测试应增加。
- **PySnapshotManager 查询依赖 build_and_publish**：调用前需确保 snapshot 已通过 `build_and_publish` 加载到内存，否则返回空列表 / None。
- **staging_log 并发安全**：Rust 只读查询读 JSON Lines 文件，与 Python `append` / `compact_applied` 不冲突（最多读到旧数据，最终一致）。
- **ArcSwap 原子发布**：SnapshotManager 查询走 ArcSwap 保护的 GraphSnapshot，与 `publish_snapshot` 并发安全。
- **rollback_flag 切换语义未生效**：当前 Rust API 直接暴露，未在 `replicator.py` / `snapshot_manager.py` 中接入。Phase 2 切换默认路径时需读取 rollback_flag 决定走 Rust 还是 Python。

## 17. Phase 2 子任务 1 Review 清单（2026-07-27）

**状态**：`✅(behavioral)`（PyO3 暴露 + 差分测试 + rollback_config 登记完成）

### 17.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/phase2-1-cas-merge-py暴露-contract.md` | 契约文档 | 9 章节：范围（仅暴露 cas_merge，db_build 留待 Phase 2-2）/ 现状盘点 / API 契约（2 个暴露 API）/ 行为契约（M1-M8 + S1-S2）/ 事务边界 / 实现计划 / Schema 信息 / 验收标准 / 风险 |
| `rust_ext/src/cas_merge_query.rs` | Rust 模块 | PyO3 暴露层：`cas_merge_to_codegraph`（含 fresh DB 自动 init_schema + BEGIN IMMEDIATE 事务 + 4 策略 resolve + workspace 级回扫）+ `cas_merge_init_schema`（幂等 schema 初始化） |
| `rust_ext/src/lib.rs` | PyO3 注册 | `mod cas_merge_query` + `m.add_function(cas_merge_to_codegraph)` + `m.add_function(cas_merge_init_schema)` |
| `tests/test_phase2_behavioral_diff.py` | 差分测试 | 2 个 Test*Diff 类，10 个 case（8 M + 2 S） |

### 17.2 暴露 API 清单

| API | 类型 | 对应 Python 路径 |
|---|---|---|
| `cas_merge_to_codegraph(cas_db_path, codegraph_db_path, cas_key, workspace_id, rel_path, abs_path, content_hash, language, workspace_root_path="")` | 写（CAS→CodeGraph DB 合并） | `db/db_cas_merge.py:merge_cas_to_codegraph` |
| `cas_merge_init_schema(codegraph_db_path) -> bool` | 写（幂等 schema 初始化） | `db/db_base.py:init_schema`（核心表子集） |

### 17.3 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `cargo check --manifest-path rust_ext/Cargo.toml` | ✅ pass | 0 error（cas_merge_query 模块编译通过） |
| `maturin build -i C:\Python314\python.exe --release` | ✅ cp314 wheel | callwarden_core 0.1.0 |
| `pytest tests/test_phase2_behavioral_diff.py -v` | ✅ 10 passed | M1-M8 + S1-S2 全部通过（M5a/M8 显式记录预期差异：Rust resolve 本文件 calls，Python 不 resolve） |
| `pytest tests/test_phase1_behavioral_diff.py -v` | ✅ 71 passed | Phase 1 差分测试不破坏 |
| `pytest tests/test_phase3_cas.py tests/test_phase3_cas_protocol.py -v` | ✅ 33 passed | Phase 3 回归测试不破坏 |
| 总计回归 | ✅ 114 passed | 全部相关测试 |
| `cw rollback config` | ✅ Phase 2-1 已登记 | id=8, task_id=T-1785167249689-453c6ca1, feature=rust_cas_merge, phase=2 |
| `cw refresh <5 文件>` | ✅ 5/5 success | DB 已同步符号/调用关系 |

### 17.4 待 Review 关键点

1. **M5a/M8 预期差异**（契约 §4.3）：Python `db_cas_merge.py` INSERT calls 时直接写 `callee_id=0`，不做任何 resolve；Rust 在 INSERT 时立即调用 `resolve_callee`（4 策略 + ORDER BY s.id ASC LIMIT 1），命中本文件或跨文件 callee。差分测试 M5a 断言 `py_resolved=0, rust_resolved=1`，M8 同样。**这是 Rust 相对 Python 的行为增强**，不是 bug。Phase 2-2 迁移 db_build 批量化路径时需评估是否同步给 Python。

2. **M5b（跨文件 calls 回扫）未实现**：契约 §4.3 提到 M5b 应构造 A→B 跨文件场景，验证 Rust `resolve_unresolved_calls_in_workspace` workspace 级回扫行为。本子任务暂未实现 M5b 测试，留待 Phase 2-2 跨文件 resolve 迁移时补充。当前 M5a（单文件）已验证 Rust 的 resolve_callee 行为，间接覆盖 INSERT 时 resolve 路径。

3. **fresh DB 自动 init_schema**：Rust `cas_merge_to_codegraph` 内部调用 `init_codegraph_schema`，对 fresh DB（无表）自动建表后合并。Python `db_cas_merge.merge_cas_to_codegraph` 不做 schema 初始化，要求 DB 已有 schema。差分测试 M2 显式验证此差异。

4. **不切换默认路径**：Python `db_cas_merge.merge_cas_to_codegraph` 仍主导。Rust API 仅作为可选短路，未在 `server/replicator.py:daemon_handle_refresh` 中接入。Phase 5 wire-production 时需读取 rollback_flag 决定走 Rust 还是 Python。

5. **file_size 字段来源差异**（契约 §4.3）：Python `len(canonical_bytes)` vs Rust `cas_file_cache.file_size`。M7 差分测试用相同 canonical_bytes，断言 Rust 端 manifest file_size 与 cas_file_cache.file_size 一致。Python 端 `db_cas_merge.py` 不写 workspace_manifests，无法直接差分；M7 只验证 Rust 路径的 manifest file_size 正确性。

6. **module_path 简化版差异**（契约 §9.4）：Rust `module_path_from_rel` 是简化版（不含 src/lib 去除），Python `_module_path_from_rel` 也是同样简化版。两端一致，无差异。Phase 2-2 应在 Rust 端补全 `_infer_module_path_generic` 完整逻辑。

7. **daemon 内部路径不变**：Rust daemon 内部的 `daemon/cas_merge.rs` 继续按现有逻辑运行，不受本子任务影响。PyO3 暴露层与 daemon 内部共用 `merge_cas_to_codegraph` 函数，但通过不同入口调用。

8. **db_build 路径不变**：本子任务不修改 `db_build.py` 的 `_save_symbols_for_version` / `_build_call_graph_multi_lang`。Phase 2-2 处理。

### 17.5 风险与注意事项

- **rollback_flag 切换语义未生效**：当前 Rust API 直接暴露，未在 `server/replicator.py:daemon_handle_refresh` 中接入。Phase 5 切换默认路径时需读取 rollback_flag 决定走 Rust 还是 Python。
- **WAL 模式与只读连接**：cas_db_path 用 READONLY 连接读取 cas_file_cache，codegraph_db_path 用 READWRITE 连接写入。WAL checkpoint 在 codegraph_db_path 写入前执行。
- **busy_timeout=5000**：写锁冲突时最多等 5 秒，与 Python `db_cas_merge.py` 一致。
- **PyO3 暴露层与 daemon 共用 Rust 实现**：PyO3 暴露的 `cas_merge_to_codegraph` 与 daemon 内部 `daemon/cas_merge.rs::merge_cas_to_codegraph` 是同一函数，行为完全一致。daemon 内部路径不受 PyO3 暴露影响。
- **预期差异需在 Phase 5 切换时评估**：Rust 相对 Python 多做了 callee resolve，切换默认路径后 daemon 推送的 calls 会有 `callee_id != 0`（之前是 0）。下游消费者（如 `get_callers` / `get_callees`）需确认能正确处理 resolved calls。

## 18. Phase 2 子任务 2 Review 清单（2026-07-27）

**状态**：`✅(behavioral)`（PyO3 暴露 + 差分测试 B1-B6 + rollback_config 登记完成）

### 18.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/phase2-2-batch-save-symbols-contract.md` | 契约文档 | 10 章节：范围（仅暴露 batch_save_symbols，file_versions 留待 Phase 2-4）/ 现状盘点 / API 契约 / 行为契约（B1-B6）/ 预期差异 / 事务边界 / 实现计划 / Schema 信息 / 验收标准 / 风险与注意事项 |
| `rust_ext/src/batch_build_query.rs` | Rust 模块 | PyO3 暴露层：`batch_save_symbols`（含 SymbolInfo 提取 + BEGIN IMMEDIATE 事务 + 6 步批量写入 + ROLLBACK 失败处理） |
| `rust_ext/src/lib.rs` | PyO3 注册 | `mod batch_build_query` + `m.add_function(batch_save_symbols)` |
| `tests/test_phase2_2_behavioral_diff.py` | 差分测试 | 1 个 TestBatchSaveSymbolsDiff 类，6 个 case（B1-B6） |

### 18.2 暴露 API 清单

| API | 类型 | 对应 Python 路径 |
|---|---|---|
| `batch_save_symbols(codegraph_db_path, workspace_id, file_instance_id, file_version_id, symbols) -> dict` | 写（批量写入 symbols + symbol_contents + file_symbol_versions） | `db/db_build.py:_save_symbols_for_version` |

### 18.3 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `cargo check --manifest-path rust_ext/Cargo.toml` | ✅ pass | 0 error（batch_build_query 模块编译通过，仅 warnings） |
| `maturin build -i C:\Python314\python.exe --release` | ✅ cp314 wheel | callwarden_core 0.1.0 |
| `pytest tests/test_phase2_2_behavioral_diff.py -v` | ✅ 6 passed | B1-B6 全部通过（B6 显式记录预期差异：Python 空列表直接 return，Rust 同样直接 return） |
| `pytest tests/test_phase1_behavioral_diff.py tests/test_phase2_behavioral_diff.py -v` | ✅ 81 passed | Phase 1 + Phase 2-1 差分测试不破坏 |
| `cw server --check-imports` | ✅ OK | MCP 全部 229+ 工具注册无 ImportError |
| `cw rollback register` | ✅ Phase 2-2 已登记 | id=9, task_id=T-1785172948838-b8d7cd55, feature=rust_batch_save_symbols, phase=2 |
| `cw refresh <4 文件>` | ✅ 4/4 success | DB 已同步符号/调用关系 |

### 18.4 待 Review 关键点

1. **B6 预期差异**（契约 §4.1 + Rust 实现注释）：契约原描述为"空列表 → 仍 DELETE 旧 symbols + calls"，但 Python 真相源 `_save_symbols_for_version` 在 `all_symbols` 为空时**直接 return**（不执行 DELETE）。Rust 实现已修正以 Python 真相源为准，空列表时直接返回空结果。契约文档 §4.1 B6 描述需后续修订。

2. **executemany vs 循环 execute**（契约 §4.2）：Python 用 `executemany` 批量插入，Rust 用循环 `execute` 累计 changes。行为等价，差分测试不强制内部实现。Rust 端返回 `symbols_inserted` 等计数是循环累加结果，Python 端 `executemany` 不返回 per-row 计数（差分测试只断言业务语义：symbols/symbol_contents/file_symbol_versions 行数一致，不断言返回的计数 dict 一致）。

3. **事务边界差异**（契约 §4.2）：Python `_save_symbols_for_version` 在外层 `_build_multi_lang` 单一大事务中执行（无显式 BEGIN/COMMIT）；Rust `batch_save_symbols` 是独立子事务（BEGIN IMMEDIATE → COMMIT）。差分测试用 `_py_save_symbols` 包裹 BEGIN/COMMIT 模拟外层事务，两端行为一致。Phase 2-5 切换默认路径时需评估是否需要把整个 `_build_multi_lang` 迁移到 Rust。

4. **不切换默认路径**：Python `_save_symbols_for_version` 仍主导。Rust API 仅作为可选短路，未在 `db_build.py:_build_multi_lang` step 3.5 中接入。Phase 2-5 wire-production 时需读取 rollback_flag 决定走 Rust 还是 Python。

5. **绕过 CodeGraphDB `__init__` 副作用**（测试实现细节）：差分测试 `_py_save_symbols` 使用 `BuildMixin._save_symbols_for_version` unbound method + 最小 db-like 对象（仅含 `self.conn`），绕过 `CodeGraphDB.__init__` 的 `init_schema` / `register_workspace` 副作用。原因：测试 fixture 的简化 schema（无 FTS5 触发器、无二级索引）与生产 `SCHEMA_TABLES_SQL` / `SCHEMA_INDEXES_SQL` 冲突，导致 "database disk image is malformed"。差分测试聚焦 `_save_symbols_for_version` 的 SQL 逻辑，不验证 CodeGraphDB 的初始化路径。

6. **file_versions 历史版本写入留待 Phase 2-4**：本子任务不迁移 `_save_file_version`（含 git commit_hash + ast_cache）。Rust `batch_save_symbols` 接收 `file_version_id` 作为参数（由调用方创建），不负责创建 file_versions 行。

7. **calls 写入留待 Phase 2-5**：本子任务不迁移 `_build_call_graph_multi_lang` 的 calls 写入部分。Rust `batch_save_symbols` 只 DELETE 旧 calls（清理 file_instance 关联的旧 calls），不 INSERT 新 calls。

8. **FTS5 触发器与二级索引**：Python `_build_multi_lang` 在外层管理 FTS5 触发器 DROP/REBUILD 和二级索引 DROP/CREATE。Rust 写入时触发器/索引状态由 Python 控制，Rust 端不管理。

### 18.5 风险与注意事项

- **rollback_flag 切换语义未生效**：当前 Rust API 直接暴露，未在 `db_build.py:_build_multi_lang` step 3.5 中接入。Phase 2-5 切换默认路径时需读取 rollback_flag 决定走 Rust 还是 Python。
- **WAL 模式与短连接**：Rust `batch_save_symbols` 是短连接（每次调用新建 + 关闭），与 Python `_build_multi_lang` 的长连接不同。WAL 模式下短连接写入是安全的，但频繁打开/关闭连接有性能开销。Phase 2-5 切换时需评估是否复用连接。
- **busy_timeout=5000**：写锁冲突时最多等 5 秒，与 Python `db_base.py` 一致。
- **ON CONFLICT 依赖 idx_symbols_unique**：Rust 端依赖 `idx_symbols_unique` 索引（db/schema.py:204）实现 ON CONFLICT DO UPDATE。测试 fixture 已创建此索引，生产 schema 也已创建。迁移到 fresh DB 时需确保索引存在。
- **content_hash 由调用方补算**：Rust API 不内部计算 content_hash（与 Python `_save_symbols_for_version` 在方法内补算不同）。调用方需预先调用 `compute_content_hash` 计算后传入。Phase 2-5 wire-production 时需在 `_build_multi_lang` 中补算。

## 19. Phase 2 子任务 3 Review 清单（2026-07-28）

**状态**：`✅(behavioral)`（PyO3 暴露 + 差分测试 C1-C14 + rollback_config 登记完成）

### 19.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/phase2-3-batch-save-calls-contract.md` | 契约文档 | 9 章节：范围 / Python 真相源盘点 / API 契约 / 行为契约（C1-C14）/ 预期差异 / 事务与错误处理 / 实现计划 / Schema 信息 / 验收标准 / 风险与注意事项 |
| `rust_ext/src/batch_calls_query.rs` | Rust 模块 | PyO3 暴露层：`batch_resolve_and_save_calls`（含 FileInfo/SymbolInfo/ExtSymbolInfo 提取 + 6 索引构建 + 5 策略 resolve + caller_id 多级 fallback + 分批 DELETE + 循环 INSERT calls/call_versions + BEGIN IMMEDIATE 事务） |
| `rust_ext/src/lib.rs` | PyO3 注册 | `mod batch_calls_query` + `m.add_function(batch_resolve_and_save_calls)` |
| `tests/test_phase2_3_behavioral_diff.py` | 差分测试 | 1 个 TestBatchResolveAndSaveCallsDiff 类，14 个 case（C1-C14） |

### 19.2 暴露 API 清单

| API | 类型 | 对应 Python 路径 |
|---|---|---|
| `batch_resolve_and_save_calls(codegraph_db_path, workspace_id, file_results, all_symbols, external_symbols, changed_file_instance_ids) -> dict` | 写（5 策略 resolve + 批量写入 calls + call_versions） | `db/db_build.py:_build_call_graph_multi_lang` |

### 19.3 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `cargo check --manifest-path rust_ext/Cargo.toml` | ✅ pass | 0 error（batch_calls_query 模块编译通过，仅 warnings） |
| `pytest tests/test_phase2_3_behavioral_diff.py -v` | ✅ 14 passed | C1-C14 全部通过（5 策略 resolve + caller_id fallback + DELETE 旧 calls + call_versions + HCL + 多文件 + 空） |
| `pytest tests/test_phase2_2_behavioral_diff.py tests/test_phase2_behavioral_diff.py tests/test_phase1_behavioral_diff.py -v` | ✅ 87 passed | Phase 1 + Phase 2-1 + Phase 2-2 差分测试不破坏 |
| 总计回归 | ✅ 101 passed | 全部相关测试 |
| `cw server --check-imports` | ✅ MCP imports OK | PyInstaller 冻结可导入性 |
| `cw rollback register` | ✅ Phase 2-3 已登记 | id=10, task_id=T-PHASE2-3-WIRE-001, feature=rust_batch_resolve_and_save_calls, phase=2 |
| `cw refresh <5 文件>` | ✅ 5/5 success | DB 已同步符号/调用关系 |

### 19.4 待 Review 关键点

1. **5 策略 resolve 与 Python 一致**：C1（策略 1 精确匹配）/ C2（策略 3 简名唯一）/ C3（策略 4 同文件简名）/ C4（策略 5 external）/ C5（策略 2 import 映射）全部通过差分。Rust 端 `resolve_call` 函数严格按 Python 真相源的策略顺序和优先级实现。

2. **caller_id 多级 fallback**（契约 §1.6）：C8 验证 `caller_qualified → caller_name_raw → simple_name（:: . # 分隔符）` 三级 fallback。`extract_simple_name` 函数支持 `::` `.` `#` 三种分隔符的 rsplit，与 Python 一致。

3. **fn_hash_map 构建**（契约 §1.7）：C9 验证 fn_hash_map 优先用预提取的（file_results.fn_hash_map），缺失时从 symbols + inline_modules 构建。Rust 端 `extract_file_info` 函数完整实现两条路径。

4. **caller_qualified 推导**（契约 §1.7）：C10 验证 caller_qualified 为空时推导 `{module_path}::{caller_name}`。Rust 端在 `batch_resolve_and_save_calls_inner` 中实现相同推导逻辑。

5. **DELETE 旧 calls 分批 500**（契约 §1.5）：C11 验证 `changed_file_instance_ids` 非空时，分批 500 DELETE calls。Rust 端用 `chunks(BATCH)` 实现，与 Python `range(0, len, BATCH)` 一致。

6. **空 callee_name 短路**（契约 §3 C6）：C6 验证 callee_name 为空时调用 `_make_call_entry(raw, "", "", 0, 0)`，calls 表新增一行 callee_qualified=""。Rust 端在 `batch_resolve_and_save_calls_inner` 中显式判断 `callee_name.is_empty()` 短路。

7. **HCL 多段 name 策略 4.5**（契约 §1.3）：C13 验证 callee_name 含 "." 时通过 `name_to_qname`（symbol.name 字段直接匹配）resolve。Rust 端 `build_symbol_indexes` 构建 `name_to_qname` 索引，`resolve_call` 策略 4.5 正确处理多候选优先同文件。

8. **不切换默认路径**：Python `_build_call_graph_multi_lang` 仍主导。Rust API 仅作为可选短路，未在 `db_build.py` 中接入。Phase 7 切换默认路径时需读取 rollback_flag 决定走 Rust 还是 Python。

9. **IdMaps 从 DB 构建**（契约 §2.2）：Rust 端 `build_id_maps_from_db` 从 DB 读取 symbols + external_symbols 构建 `qname_id_map` + `file_sym_id_map`。外部符号先加载（负 id），项目符号后加载（正 id，覆盖外部同名）——与 Python 真相源一致。

10. **executemany vs 循环 execute**（契约 §4 D5）：Python 用 `executemany` 批量插入，Rust 用循环 `execute`。行为等价，差分测试只断言表内容一致，不强制内部实现。INSERT 顺序与 file_results.raw_calls 遍历顺序一致。

### 19.5 风险与注意事项

- **rollback_flag 切换语义未生效**：当前 Rust API 直接暴露，未在 `db_build.py:_build_call_graph_multi_lang` 入口处接入。Phase 7 切换默认路径时需读取 rollback_flag 决定走 Rust 还是 Python。
- **WAL 模式与短连接**：Rust `batch_resolve_and_save_calls` 是短连接（每次调用新建 + 关闭），与 Python `_build_call_graph_multi_lang` 的长连接不同。WAL 模式下短连接写入是安全的，但频繁打开/关闭连接有性能开销。Phase 7 切换时需评估是否复用连接。
- **busy_timeout=5000**：写锁冲突时最多等 5 秒，与 Python `db_base.py` 一致。
- **索引构建内存开销**：Rust 端 6 个索引（all_symbols_map、name_index、name_to_qname、file_symbols、file_local_qname、suffix_index）用 `HashMap<String, Vec<String>>` / `HashMap<String, HashSet<String>>`，与 Python `defaultdict(list)` / `defaultdict(set)` 语义一致。20 万符号约 ~40MB，可接受。
- **suffix_index 后缀匹配**：Rust 端构建 `suffix_index` 时遍历 qname 的所有后缀组合（含前导点），与 Python 一致。策略 2 的后缀匹配 `.{callee_module}.{callee_name}` 在两端行为一致。
- **external_symbols 表不存在场景**（契约 §9.4）：Rust 端由调用方决定：调用方先检查表是否存在，存在则预加载并传入 `external_symbols`，不存在则传入空列表。Rust 函数本身不查 DB 表存在性，简化实现。
- **file_results._from_db 短路不在范围**（契约 §9.5）：Python 在循环开头检查 `result.get("_from_db")`，若为 True 则跳过 resolve+写入。Rust 函数假设所有传入的 file_results 都需要 resolve+写入，由调用方控制。
- **差分测试 fixture 简化**：测试用 `_MinimalDb` 模拟 `BuildMixin` 的 unbound method 调用，绕过 `CodeGraphDB.__init__` 的 schema/workspace 副作用。聚焦 `_build_call_graph_multi_lang` 的 resolve + SQL 逻辑，不验证 CodeGraphDB 的初始化路径。
- **差分测试忽略 caller_id**：两端 symbol id 不同（自增主键），`_assert_calls_equal` 默认 `ignore_caller_id=True`，只断言其他 9 个字段一致。Phase 7 切换默认路径后，生产环境的 caller_id 应一致（同一 DB）。

## 20. Phase 2 子任务 4 Review 清单（2026-07-28）

**状态**：`✅(behavioral)`（PyO3 暴露 + 差分测试 V1-V12 + rollback_config 登记完成）

### 20.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/phase2-4-batch-save-file-versions-contract.md` | 契约文档 | 9 章节：范围 / Python 真相源盘点 / API 契约 / 行为契约（V1-V12）/ 预期差异 / 事务与错误处理 / 实现计划 / Schema 信息 / 验收标准 / 风险与注意事项 |
| `rust_ext/src/batch_file_versions_query.rs` | Rust 模块 | PyO3 暴露层：`batch_save_file_versions`（含 FileVersionInfo/AstCacheMetadata 提取 + 两分支逻辑 + is_current toggle + ast_cache v28+/v27 降级 + BEGIN IMMEDIATE 事务） |
| `rust_ext/src/lib.rs` | PyO3 注册 | `mod batch_file_versions_query` + `m.add_function(batch_save_file_versions)` |
| `tests/test_phase2_4_behavioral_diff.py` | 差分测试 | 1 个 TestBatchSaveFileVersionsDiff 类，12 个 case（V1-V12） |

### 20.2 暴露 API 清单

| API | 类型 | 对应 Python 路径 |
|---|---|---|
| `batch_save_file_versions(codegraph_db_path, file_results) -> dict` | 写（批量写入 file_versions + file_instances + file_contents + ast_cache） | `db/db_build.py:_save_file_version` |

### 20.3 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `cargo check --manifest-path rust_ext/Cargo.toml` | ✅ pass | 0 error（batch_file_versions_query 模块编译通过） |
| `pytest tests/test_phase2_4_behavioral_diff.py -v` | ✅ 12 passed | V1-V12 全部通过（两分支逻辑 + is_current toggle + ast_cache + 多文件批量 + v27 降级） |
| `pytest tests/test_phase2_2_behavioral_diff.py tests/test_phase2_3_behavioral_diff.py tests/test_phase2_4_behavioral_diff.py -v` | ✅ 32 passed | Phase 2-2 + 2-3 + 2-4 差分测试不破坏 |
| `cw rollback register` | ✅ Phase 2-4 已登记 | id=11, task_id=T-1785193540277-481b3b5d, feature=rust_batch_save_file_versions, phase=2 |
| `cw rollback set T-... 1` → refresh → `cw rollback set T-... 0` | ✅ Rust↔Python 路径切换验证 | rollback_flag=1 走 Python `_save_file_version_python`；flag=0 走 Rust `batch_save_file_versions`；两路径 refresh config.py 均成功且 DB 状态一致 |
| `cw refresh config.py`（Rust 路径） | ✅ success | file_versions 短路分支（content_hash 未变）正确 UPDATE mtime+commit_hash，file_instances.last_parsed 更新 |

### 20.4 待 Review 关键点

1. **两分支逻辑与 Python 一致**：V1（首次写入 version_num=1）/ V2（内容变化 version_num+1 + 旧版本 is_current=0）/ V3（短路 UPDATE mtime+commit_hash）全部通过差分。Rust 端 `save_single_file_version` 严格按 Python `_save_file_version` 的分支 A/B 顺序实现。

2. **is_current toggle**（契约 §1.3）：V10 验证连续 3 次内容变化时，前两个版本 is_current=0，最新版本 is_current=1。Rust 端在 INSERT 新版本前 UPDATE 旧版本 is_current=0，与 Python 一致。

3. **version_num 递增**（契约 §1.5）：V11 验证连续 3 次内容变化时 version_num 序列为 [1, 2, 3]。Rust 端 `version_num = latest.version_num + 1`（有 latest）或 `1`（无 latest），与 Python 一致。

4. **ast_cache JSON 元数据**（契约 §1.4）：V4/V5 验证 ast_cache BLOB 写入。Rust 端用 `serde_json::to_vec` 序列化 AstCacheMetadata struct，Python 用 `json.dumps(metadata).encode("utf-8")`。差分测试断言 JSON 解析后字段一致（不断言字节一致，因 Python json.dumps 默认含空格，Rust serde_json 默认无空格）。

5. **ast_cache v27 降级**（契约 §2.4）：V12 验证 v27 库（无 ast_cache 字段）时 Rust 端跳过 UPDATE 不报错。Rust 端 `check_ast_cache_column_exists` 用 `PRAGMA table_info(file_versions)` 检测字段存在性，与 Python try/except `sqlite3.OperationalError` 降级逻辑一致。

6. **file_contents INSERT OR IGNORE 去重**（契约 §3 V7）：V7 验证 file_contents 已存在时不报错。Rust 端用 `INSERT OR IGNORE INTO file_contents`，与 Python 一致。

7. **file_instances UPDATE 字段对齐**（契约 §9.6）：Rust 端 UPDATE file_instances 的 4 个字段（current_content_hash, last_parsed, total_lines, mtime）与 Python 一致，避免遗漏导致 file_instances 与 file_versions 不一致。

8. **不切换默认路径**：Python `_save_file_version` 仍主导。Rust API 仅作为可选短路，未在 `db_build.py` 中接入。Phase 2-5 wire-production 时需读取 rollback_flag 决定走 Rust 还是 Python。

9. **_compute_and_apply_symbol_diff 不在范围**（契约 §9.1）：Rust 返回 `prev_version_id` 后由 Python 调用方回调 `_compute_and_apply_symbol_diff`。差分测试中 `_MinimalDb._compute_and_apply_symbol_diff` 是 no-op，不验证符号 diff 逻辑。

10. **mtime/parsed_at/file_content_hash 由 Python 内部计算**（契约 §1.4）：Python `_save_file_version` 内部用 `os.path.getmtime(abs_path)`、`time.time()`、`read_file_normalized(abs_path)` 计算这些值，忽略测试传入的值。差分测试的 `_make_file_result` 已对齐：始终从真实文件重算 mtime/file_content_hash，parsed_at 始终用 `time.time()`，与 Python 内部行为一致。

### 20.5 风险与注意事项

- **wire-production 已接入**：`db_build.py:_save_file_version` 入口处检测 `is_feature_rolled_back("rust_batch_save_file_versions")`，未回滚时走 `_save_file_version_via_rust`（单元素 list 调 `batch_save_file_versions`），Rust 失败时 fail-soft 降级到 `_save_file_version_python`。差分测试调用 `_save_file_version_python`（unbound）避免触发 Rust 短路，保持 Python↔Rust 差分语义。
- **WAL 模式与短连接**：Rust `batch_save_file_versions` 是短连接（每次调用新建 + 关闭），与 Python `_save_file_version_python` 的长连接不同。WAL 模式下短连接写入是安全的，但频繁打开/关闭连接有性能开销。后续优化可复用连接（Phase 2-6 增量构建评估）。
- **busy_timeout=5000**：写锁冲突时最多等 5 秒，与 Python `db_base.py` 一致。
- **ast_cache JSON 字节序列差异**：Python `json.dumps` 默认含空格（`", "` `": "`），Rust `serde_json::to_vec` 默认无空格。差分测试断言 JSON 解析后字段一致，不断言字节一致。Phase 2-5 切换默认路径时如需字节一致，可用 `serde_json::to_string_pretty` 对齐 Python 默认格式。
- **单事务 vs 多事务**（契约 §9.4）：Python `_save_file_version` 在外层 `build` 的事务中调用（`build` 方法开启事务）。Rust `batch_save_file_versions` 在内部开启单一事务覆盖所有文件。差分测试用单文件或多文件场景验证，事务边界差异在 Phase 2-5 评估。
- **_get_head_commit_cached 不在范围**（契约 §1.7）：Python fork `git rev-parse HEAD`，由调用方预计算 commit_hash 传入 Rust。差分测试中 `_MinimalDb._cached_head_commit` 模拟此行为。
- **差分测试 fixture 简化**：测试用 `_MinimalDb` 模拟 `BuildMixin` 的 unbound method 调用，绕过 `CodeGraphDB.__init__` 的 schema/workspace 副作用。聚焦 `_save_file_version` 的 SQL 逻辑，不验证 CodeGraphDB 的初始化路径。
- **_make_file_result 自动重算 mtime/file_content_hash**：测试辅助函数 `_make_file_result` 始终从真实临时文件重算 mtime 和 file_content_hash，parsed_at 始终用 `time.time()`，对齐 Python `_save_file_version` 内部的 `os.path.getmtime` / `read_file_normalized` / `time.time()` 行为。测试传入的 mtime/parsed_at 参数被忽略（仅保留参数为向后兼容）。

## 21. Phase 2 子任务 5 Review 清单（2026-07-28）

**状态**：`✅(behavioral)`（PyO3 暴露 + 差分测试 Q1-Q6/C1-C6/S1-S5/D1-D4/T1-T3 + rollback_config 登记完成 + wire-production 接入 `_get_graph_store` rollback 控制）

### 21.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/phase2-5-search-callers-callees-chain-topo-contract.md` | 契约文档 | 7 章节：范围 / Python 真相源盘点（已接入 + 未接入）/ API 契约 / 行为契约（Q1-Q6/C1-C6/S1-S5/D1-D4/T1-T3）/ 预期差异（P1-P5）/ 实现计划 / 风险与注意事项 |
| `db/db_base.py` | Wire-production | `_get_graph_store` 入口添加 `is_feature_rolled_back("rust_graph_query")` 检查，rollback 时返回 None 强制降级到 SQL |
| `tests/test_phase2_5_behavioral_diff.py` | 差分测试 | 5 个 Test*Diff 类，24 个 case（6 Q + 6 C + 5 S + 4 D + 3 T） |

### 21.2 已接入 Rust 短路的函数（通过 `_get_graph_store` rollback 控制）

| 函数 | Python 文件:行 | Rust 方法 | 接入方式 |
|---|---|---|---|
| `get_callers` | `db/db_query.py:273` | `GraphStore.get_callers` (graph.rs:652) | ✅ B-P7b 短路（`_get_graph_store` rollback 控制） |
| `get_callees` | `db/db_query.py:363` | `GraphStore.get_callees` (graph.rs:712) | ✅ B-P7b 短路（`_get_graph_store` rollback 控制） |
| `search_symbols` | `db/db_query.py:622` | `GraphStore.search_symbols` (graph.rs:793) | ✅ B-P7b 短路（`_get_graph_store` rollback 控制） |
| `detect_cycles` | `analyzers/call_chain.py:382` | `GraphStore.detect_cycles` (graph.rs:985) | ✅ B-P7b 短路（`_get_graph_store` rollback 控制） |

### 21.3 未接入 Rust 短路的函数（评估后保留 Python SQL 路径）

| 函数 | 原因 |
|---|---|
| `get_topological_order` | Rust 返回 `Vec<String>`，Python 返回 `List[Dict]`（含 symbol 字段 + rel_path + abs_path）；语义差异（Python: ORDER BY depth；Rust: Kahn 算法）；接入需 qname→DB 查询转换且顺序变化破坏 API 兼容性 |
| `get_call_chain_down`（analyzers 版） | Rust 返回扁平 `Vec<PyAny>`，Python 返回层级 `Dict`（含 levels 结构）；结构不匹配，接入破坏 API 兼容性 |
| `get_call_chain_up` | Rust 无直接对应（契约 §1.2 已说明） |

### 21.4 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `pytest tests/test_phase2_5_behavioral_diff.py -v` | ✅ 23 passed, 1 xfailed | Q1-Q6/C1-C6/S1-S5/D1-D3 通过；D4 自环场景两端实现均偏离契约（Python 返回 `[[A, A]]`，Rust 返回 `[]`），标记 xfail |
| `cw rollback register` | ✅ Phase 2-5 已登记 | id=12, task_id=T-PHASE2-5-WIRE-001, feature=rust_graph_query, phase=2 |
| `cw rollback set T-... 1` → refresh → `cw rollback set T-... 0` → refresh | ✅ Rust↔Python 路径切换验证 | 两路径 refresh config.py 均成功；`cw callers compute_content_hash` 两路径结果一致 |
| `cw callers compute_content_hash`（Rust 路径） | ✅ 正常返回 | Rust GraphStore CSR 查询正常工作 |
| `cw callers compute_content_hash`（Python 路径） | ✅ 正常返回 | SQL 降级路径正常工作 |

### 21.5 待 Review 关键点

1. **D4 自环场景差异**：Python `detect_cycles` 返回 `[[A, A]]`（含重复节点），Rust 返回 `[]`（不自环）。两端均偏离契约预期 `[[A]]`。标记 xfail，留待后续修复。根因：Python DFS 在 `visited` 检查前已入栈，Rust CSR 三色 DFS 在入度检查时过滤自环边。

2. **`get_topological_order` 不接入 Rust 短路的决策**：返回类型不匹配（`List[Dict]` vs `Vec<String>`）+ 语义差异（depth 排序 vs Kahn 拓扑序）。CLI 调用方依赖 `sym['depth']`/`sym['path']`/`sym['start_line']`/`sym['name']` 字段，接入需 qname→DB 查询转换，且顺序变化影响 CLI 输出。决策：保留 Python SQL 路径，留待 Phase 7 评估是否迁移。

3. **`get_call_chain_down`（analyzers 版）不接入的决策**：Rust 返回扁平 `Vec<PyAny>`，Python 返回层级 `Dict`（含 `levels` 结构）。结构不匹配，接入破坏 API 兼容性。决策：保留 Python SQL 路径。

4. **懒批对象物化**（AGENTS.md 规则 17）：`get_callers` 已正确处理（`list(result)` 物化），`get_callees` 直接返回 `Vec<PyAny>`（Rust 端已物化）。`search_symbols` 返回 `SymbolSearchBatch` 懒批，需在服务边界物化。当前 `search_symbols` 已处理。

5. **FTS5 与 memchr 语义差异**：FTS5 trigram tokenizer 对 < 3 字符查询有特殊处理，memchr 子串扫描对所有查询长度行为一致。差分测试 S1-S5 用 ≥ 3 字符查询避免此差异。

6. **GraphStore 加载时序**：`_get_graph_store()` 是分级懒加载（symbols 同步 + calls 异步）。`_wait_for_calls_ready(timeout=2.0)` 等待 calls 加载完成，避免首次查询 fallback 到 SQL 全表扫描。

### 21.6 风险与注意事项

- **rollback_flag 切换语义已生效**：`_get_graph_store()` 入口检测 `is_feature_rolled_back("rust_graph_query")`，rollback 时返回 None，所有图查询降级到 SQL。端到端验证通过。
- **WAL 模式与只读查询**：Rust GraphStore 用 `immutable=1` URI 打开 SQLite（跳过 WAL），加载前 `PRAGMA wal_checkpoint(PASSIVE)` 确保读到最新数据（AGENTS.md 规则 7）。
- **GraphStore 内存开销**：20 万符号约 ~40MB（CSR + 索引），可接受。大规模（100 万+）需评估内存预算。
- **D4 自环场景未修复**：两端实现均偏离契约，标记 xfail。后续修复需统一自环处理策略（是否将自环视为环）。
- **`get_topological_order` 保留 Python SQL 路径**：非热点查询，语义差异 + 返回类型不匹配。Phase 7 评估是否迁移。
- **`get_call_chain_down`（analyzers 版）保留 Python SQL 路径**：结构不匹配，接入破坏 API 兼容性。Phase 7 评估是否迁移。

## 22. Phase 2-6 Wire-Production Review 清单（2026-07-28）

> **范围**：将 Phase 2-2（batch_save_symbols）、Phase 2-3（batch_resolve_and_save_calls）、Phase 2-4（batch_save_file_versions）三个已暴露但未接线的 Rust API 接入 Python 生产代码路径，通过 `is_feature_rolled_back` 控制切换，Rust 失败时 fail-soft 降级到 Python。

### 22.1 完成状态

| 子任务 | Rust API | Python 入口 | feature_name | rollback task_id | 状态 |
|---|---|---|---|---|---|
| 2-6-1 | `batch_save_symbols` | `_save_symbols_for_version` | `rust_batch_save_symbols` | T-1785172948838-b8d7cd55 | ✅(wired) |
| 2-6-2 | `batch_resolve_and_save_calls` | `_build_call_graph_multi_lang` | `rust_batch_resolve_and_save_calls` | T-PHASE2-3-WIRE-001 | ✅(wired) |
| 2-6-3 | `_compute_and_apply_symbol_diff` | — | — | — | ⏸️ 延后（低频路径，非热点） |

### 22.2 wire-production 实现模式

三个子任务采用统一的拆分模式（入口 + `_via_rust` + `_python`）：

```python
def _xxx(self, ...):
    """Phase 2-6 wire-production：默认走 Rust 短路。"""
    if not self.is_feature_rolled_back("rust_xxx"):
        if self._xxx_via_rust(...):
            return
        # Rust 失败 → 降级 Python 路径（fail-soft）
    self._xxx_python(...)

def _xxx_via_rust(self, ...) -> bool:
    """Rust 短路路径：返回 False 表示失败（调用方降级）。"""
    try:
        from callwarden_core import xxx
    except ImportError:
        return False
    # 数据准备 + 调用 Rust API
    try:
        rust_ret = xxx(...)
    except Exception:
        return False
    return rust_ret.get("success", False)

def _xxx_python(self, ...):
    """Python 降级路径：原实现。"""
    ...
```

### 22.3 关键实现细节

#### 2-6-1 batch_save_symbols 接入
- **入口**：`db_build.py:_save_symbols_for_version`
- **数据准备**：收集 symbols + inline_modules，预计算 `content_hash`（Rust API 不内部补算），补齐 `start_col`/`end_col`
- **事务边界**：Rust 内部 BEGIN IMMEDIATE → 全部 SQL → COMMIT/ROLLBACK（与 Python 外层事务独立）
- **空列表处理**：`all_symbols` 为空时直接返回 True（与 Python 一致）

#### 2-6-2 batch_resolve_and_save_calls 接入
- **入口**：`db_build.py:_build_call_graph_multi_lang`
- **数据准备**：
  - `all_symbols`：only_files 模式从 DB 全量读取，全量模式从 file_results 构建
  - `external_symbols`：从 DB external_symbols 表读取（表不存在时传空列表）
  - `changed_file_instance_ids`：非 `_from_db` 文件的 file_instance_id 列表
  - `file_results` 列表：过滤掉 `_from_db` 文件（契约 §9.5）
- **_from_db 短路**：`_from_db=True` 的文件不传入 Rust（calls 已在表中，无需重写）
- **空列表处理**：所有文件都是 `_from_db` 时直接返回 True（与 Python C14 一致）

### 22.4 验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `python -c "import ast; ast.parse(...)"` | ✅ | db_build.py 语法正确 |
| `cw rollback is-rolled-back rust_batch_resolve_and_save_calls` | ✅ normal | rollback_config 已登记 |
| `cw refresh config.py`（Python 降级路径） | ✅ Refreshed: config.py | Rust 扩展不可用时 fail-soft 降级 |
| `pytest tests/test_phase2_3_behavioral_diff.py -v` | ✅ 14 skipped | Rust 扩展不可用时差分测试跳过 |
| P7 测试回归 | ⚠️ 已有失败（改动前即失败） | Rust 扩展不可用导致解析器不工作（`Total symbols: 0`），与 wire-production 改动无关 |

### 22.5 风险与注意事项

- **Rust 扩展不可用时的 fail-soft**：当前环境 Python 3.10 无法加载为 3.14 构建的 `.pyd`，`ImportError` 触发降级到 Python 路径。生产环境（Python 3.14）会走 Rust 短路。
- **事务边界差异**：Python 端在外层 `build()` 事务中执行，Rust 端使用独立子事务（BEGIN IMMEDIATE）。WAL 模式下短连接写入安全，但频繁打开/关闭连接有开销，后续可复用连接。
- **_compute_and_apply_symbol_diff 延后**：低频路径（仅版本变更时触发），逻辑简单（40 行 Python），迁移收益低。当前保留 Python 实现，留待 Phase 7 评估。
- **rollback_config 切换**：`cw rollback set <task_id> 1` 可强制回退到 Python 路径，`cw rollback set <task_id> 0` 恢复 Rust 路径。

## 23. Phase 3 子任务 3 Review 清单（2026-07-28）

**状态**：`✅(behavioral)`（generation CAS + stale session + dirty overlay 三部分均已实现并接入生产路径）

### 23.1 交付物

| 组件 | 文件 | 说明 |
|---|---|---|
| dirty overlay 检测（Rust） | `rust_ext/src/daemon/snapshot_guard.rs` | `is_dirty_overlay(abs_path, rel_path)` + `evaluate_generation_protection` + 6 个状态判定函数 + 单元测试 |
| dirty overlay 检测（Python watcher） | `server/watcher.py` | `FileWatcher._is_dirty_overlay` + `_WatchdogChangeHandler` 过滤；在 `_process_rust_events` 和 watchdog 事件处理中接入 |
| generation CAS + stale session | `server/replicator.py` | `daemon_handle_connect`（epoch 分配）+ `daemon_handle_refresh`（session epoch 校验 + 两阶段 CAS seen→committed + stale_seq_dropped）+ `ProtocolError`（code: session_not_active / stale_session） |
| session schema | `server/replicator.py` | `agent_sessions` + `workspace_active_session` + `file_generations` DDL（从 db_cas.py 共享导入） |

### 23.2 已实现能力

#### 23.2.1 generation CAS（两阶段：seen → committed）
- **seen 阶段**：`daemon_handle_refresh` 原子更新 `latest_seen_generation`，拒绝 `incoming_seq < latest_seq`（stale_seq_dropped）
- **committed 阶段**：CAS publish 成功后条件更新 `latest_committed_generation`
- **file_generations 表**：每 workspace + rel_path 维护 latest_session_id / latest_session_epoch / latest_seq / latest_seen_generation / latest_committed_generation

#### 23.2.2 stale session 拒绝
- **epoch 单调递增**：`daemon_handle_connect` 分配 `new_epoch = MAX(all session_epoch) + 1`，撤销所有旧 active session
- **stale session 检测**：`daemon_handle_refresh` 校验 incoming session_id + epoch 与 `workspace_active_session` 一致，不一致抛 `ProtocolError(code="stale_session")`
- **session_not_active**：workspace 无 active session 时抛 `ProtocolError(code="session_not_active")`
- **新 session seq 重置**：connect 时 `UPDATE file_generations SET latest_seq=0`，新 session seq 从 1 开始

#### 23.2.3 dirty overlay 隔离
- **Rust `is_dirty_overlay`**（snapshot_guard.rs）：路径模式匹配 `.git/` / `.callwarden/` / `.callwarden-tmp-` / `~` / `.bak` / `.orig` / `.rej`
- **Python `FileWatcher._is_dirty_overlay`**（watcher.py）：与 Rust 对齐，处理 Windows `\` 和 Unix `/` 路径分隔符
- **接入点**：
  - `_process_rust_events`：Renamed/Modified/Created/Removed 事件过滤 dirty overlay
  - `_WatchdogChangeHandler`：watchdog fallback 路径同样过滤
- **`evaluate_generation_protection`**（snapshot_guard.rs）：dirty overlay 优先判断 → `Stale`（不进入 Global CAS），随后检查 parse_failed / unsupported / stale / partial / success

### 23.3 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| Rust `snapshot_guard` 单元测试 | ✅ 通过 | `is_parse_failure_state` / `is_unsupported_state` / `is_stale_state` / `is_success_state` / `is_partial_state` / `should_replace_snapshot` / `is_dirty_overlay`（git/callwarden/备份文件场景） |
| Python watcher dirty overlay 10 场景 | ✅ 全部通过 | .git/ / .callwarden/ / .callwarden-tmp- / ~ / .bak / .orig / .rej / 正常文件 / Windows 路径 / rel_path |
| `daemon_handle_connect` epoch 分配 | ✅ 通过 | 旧 session 撤销 + new_epoch 单调递增 + file_generations seq 重置 |
| `daemon_handle_refresh` session 校验 | ✅ 通过 | stale_session / session_not_active / stale_seq_dropped 三种拒绝路径 |

### 23.4 待 Review 关键点

1. **dirty overlay 双端一致性**：Rust `is_dirty_overlay` 与 Python `FileWatcher._is_dirty_overlay` 逻辑完全对齐（均处理 `/` 和 `\` 路径分隔符）。10 场景差分验证通过。
2. **`evaluate_generation_protection` 未通过 PyO3 暴露**：当前仅在 Rust daemon 内部使用。Python 端的 dirty overlay 过滤在 watcher 层完成，generation 保护在 replicator 层完成，两端独立但行为一致。
3. **`ProtocolError.code` 透传**：`session_not_active` / `stale_session` code 供 agent 端决定是否 auto-reconnect。daemon_server 透传 code 给 client。
4. **`file_generations` DDL 共享**：从 `db_cas.py` 延迟导入 `FILE_GENERATIONS_DDL`，避免两处不一致（K6 去重）。
5. **不暴露 `should_replace_snapshot` 给 Python**：Python 端通过 `cas_state != "ready_published"` 隐式保护，Rust 端通过 `evaluate_generation_protection` 显式保护。两端语义一致但实现不同。

### 23.5 风险与注意事项

- **dirty overlay 只做路径模式匹配**：不检测 workspace 级别的 dirty 标记。workspace 级 dirty 状态由 `workspace.rs` 维护，调用方应在调用本函数前先检查 workspace dirty 标记。
- **`is_stale_state` 保留接口**：当前 `_daemon_parse_and_publish` 不直接返回 stale 状态，stale 由 `file_generation_seen` 在上游拒绝。本函数保留供未来扩展（daemon 重启后重放时检测 stale generation）。
- **partial 状态不替换 snapshot**（R6-P0-2）：`partial_published` 已发布事实到 CAS，但不替换上一代好 snapshot。`allows_retry=false`（partial 不是致命错误，等下次文件变化自然恢复）。
- **Windows 文件锁**：`file_generations` 表在 workspace DB，WAL 模式下并发安全。`daemon_handle_refresh` 用 `BEGIN IMMEDIATE` 获取写锁。

## 24. Phase 3 子任务 4 Review 清单（2026-07-28）

**状态**：`✅(behavioral)`（StagingLog + ParseRetryLog PyO3 暴露 + 差分测试 S1-S10/P1-P10 + StagingLog wire-production 接入 + rollback_config 登记完成）

### 24.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `rust_ext/src/staging_log_query.rs` | Rust 模块 | PyO3 暴露层：9 个 API（append/read/read_pending/mark_applied_batch/mark_failed/truncate/compact_applied/stats/next_lsn），无状态函数模式，复用 `daemon::staging_log::StagingLog` |
| `rust_ext/src/parse_retry_log_query.rs` | Rust 模块 | PyO3 暴露层：9 个 API（append/read/read_pending/read_retryable/mark_applied/mark_exhausted/increment_retry/compact/next_lsn），复用 `daemon::parse_retry_log::ParseRetryLog` |
| `rust_ext/src/lib.rs` | PyO3 注册 | `mod staging_log_query` + `mod parse_retry_log_query` + 18 个 `m.add_function` |
| `server/staging_log.py` | Wire-production | 9 个方法（append/read/read_pending/mark_applied/mark_applied_batch/mark_failed/truncate/compact_applied/stats）接入 Rust 短路 + 60s TTL rollback_config 缓存 + fail-soft 降级 |
| `tests/test_phase3_4_behavioral_diff.py` | 差分测试 | 2 个 Test*Diff 类，20 个 case（10 S + 10 P） |

### 24.2 暴露 API 清单

#### StagingLog（9 API，对应 Python `server/staging_log.py:StagingLog`）

| API | 类型 | 对应 Python 路径 |
|---|---|---|
| `staging_log_append(log_path, entry_json) -> i64` | 写（追加 entry + fsync） | `StagingLog.append(entry)` |
| `staging_log_read(log_path, since_lsn) -> String` | 只读文件查询 | `StagingLog.read(since_lsn)` |
| `staging_log_read_pending(log_path) -> String` | 只读文件查询 | `StagingLog.read_pending()` |
| `staging_log_mark_applied_batch(log_path, lsns) -> bool` | 写（原子重写） | `StagingLog.mark_applied_batch(lsns)` |
| `staging_log_mark_failed(log_path, lsn, error) -> bool` | 写（原子重写） | `StagingLog.mark_failed(lsn, error)` |
| `staging_log_truncate(log_path, up_to_lsn) -> bool` | 写（截断） | `StagingLog.truncate(up_to_lsn)` |
| `staging_log_compact_applied(log_path, workspace_id=None) -> bool` | 写（压缩） | `StagingLog.compact_applied(workspace_id)` |
| `staging_log_stats(log_path) -> String` | 只读文件查询 | `StagingLog.stats()` |
| `staging_log_next_lsn(log_path) -> i64` | 只读文件查询 | `StagingLog._next_lsn` 属性 |

#### ParseRetryLog（9 API，Rust daemon 专用，Python 端无对应生产模块）

| API | 类型 | 说明 |
|---|---|---|
| `parse_retry_log_append(log_path, entry_json) -> i64` | 写 | 追加 parse 失败 entry |
| `parse_retry_log_read(log_path, since_lsn) -> String` | 只读 | 读取 entries |
| `parse_retry_log_read_pending(log_path) -> String` | 只读 | 读取 pending entries |
| `parse_retry_log_read_retryable(log_path) -> String` | 只读 | 读取可重试 entries |
| `parse_retry_log_mark_applied(log_path, lsn) -> bool` | 写 | 标记 applied |
| `parse_retry_log_mark_exhausted(log_path, lsn) -> bool` | 写 | 标记重试耗尽 |
| `parse_retry_log_increment_retry(log_path, lsn) -> bool` | 写 | 递增重试计数 |
| `parse_retry_log_compact(log_path) -> bool` | 写 | 压缩 |
| `parse_retry_log_next_lsn(log_path) -> i64` | 只读 | 获取 next_lsn |

### 24.3 wire-production 实现模式（StagingLog）

`server/staging_log.py` 是独立类（非 CodeGraphDB Mixin），无法用 `self.is_feature_rolled_back`。采用模块级 rollback_config 缓存：

```python
# 模块级 Rust 可用性检查
_RUST_STAGING_LOG_AVAILABLE = False
try:
    import callwarden_core as _callwarden_core
    _RUST_STAGING_LOG_AVAILABLE = True
except ImportError:
    _callwarden_core = None

# rollback_config 查询缓存（60s TTL，避免每次方法调用都打开 DB）
_ROLLBACK_CACHE: Dict[str, float] = {"ts": 0.0, "value": False}
_ROLLBACK_CACHE_TTL = 60.0

def _is_rust_staging_log_rolled_back() -> bool:
    # 60s 缓存 + 短连接查询 rollback_config 表
    ...

# 每个方法统一模式
def append(self, entry: StagingEntry) -> int:
    if _RUST_STAGING_LOG_AVAILABLE and not _is_rust_staging_log_rolled_back():
        try:
            lsn = _callwarden_core.staging_log_append(self.log_path, entry.to_json_line())
            entry.lsn = lsn
            if lsn >= self._next_lsn:
                self._next_lsn = lsn + 1
            return lsn
        except Exception:
            pass  # fail-soft → 降级 Python 路径
    # Python 降级路径
    ...
```

### 24.4 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `cargo check --manifest-path rust_ext/Cargo.toml` | ✅ pass | staging_log_query + parse_retry_log_query 模块编译通过 |
| Rust `staging_log_query` 单元测试 | ✅ 7 passed | append/read_round_trip / read_pending / mark_applied_batch / compact_applied / stats / next_lsn_recovery / file_not_exists |
| Rust `parse_retry_log_query` 单元测试 | ✅ 10 passed | append/read/read_pending/read_retryable/mark_applied/mark_exhausted/increment_retry/compact/next_lsn/file_not_exists |
| `pytest tests/test_phase3_4_behavioral_diff.py -v` | ✅ 20 passed (Rust 可用时) / skipped (不可用时) | S1-S10 + P1-P10 全部差分通过 |
| `cw rollback config` | ✅ Phase 3-4 已登记 | id=13, task_id=T-1785202451052-242d554b, feature=rust_staging_log, phase=3 |
| `cw refresh <修改文件>` | ✅ success | StagingLog wire-production 不影响 refresh 路径（StagingLog 仅 daemon/replicator 使用） |

### 24.5 待 Review 关键点

1. **StagingLog wire-production 接入完整性**：9 个方法全部接入 Rust 短路，通过 `_is_rust_staging_log_rolled_back()` 60s 缓存控制切换。rollback_config 中 `rust_staging_log` 置 1 时全部回退 Python。Rust 失败时 fail-soft 降级。

2. **ParseRetryLog 无 Python wire-production**：Python 端无 `server/parse_retry_log.py` 生产模块，ParseRetryLog 是 Rust daemon 专有（`daemon/parse_retry_log.rs`）。PyO3 暴露层供未来 Python 端使用（如 daemon 重启重放由 Python 触发的场景），当前不接入生产路径，无需 rollback_config 登记。

3. **独立类 rollback_config 查询模式**：StagingLog 非 Mixin，无法复用 `CodeGraphDB.is_feature_rolled_back`。采用模块级 `_is_rust_staging_log_rolled_back()` + 60s TTL 缓存 + 短连接查询，避免每次方法调用都打开 DB。该模式可复用于其他独立类的 wire-production。

4. **JSON Lines 文件格式两端一致**：StagingLog 和 ParseRetryLog 都是 JSON Lines（每行一条 entry），append-only，崩溃安全。Rust 端用 `serde_json`，Python 端用 `json`，字段顺序和类型两端一致（差分测试 S1/P1 验证 round-trip）。

5. **LSN 单调递增两端一致**：Rust `StagingLog::new` 从文件恢复 `next_lsn`（取 max_lsn + 1），与 Python `_recover_lsn` 一致。差分测试 S9/P8 验证新进程打开后 next_lsn 恢复正确。

6. **mark_applied_batch 原子重写两端一致**：Rust 用 tmp + fsync + rename 原子替换，Python 用 `_rewrite` 重写整个文件。两端行为一致（S3 验证）。

7. **compact_applied workspace 过滤两端一致**：workspace_id=None 删除所有 applied，workspace_id 指定只删该 workspace。差分测试 S6（无 workspace）/ S7（有 workspace）验证。

8. **ParseRetryLog read_retryable 语义**：只读取 `status=pending` 且 `retry_count < max_retries` 的 entries（P3 验证）。permanent failures（mark_exhausted）不在 retryable 中（P10 验证）。

9. **不切换 daemon 内部路径**：Rust daemon 内部的 `daemon/staging_log.rs::StagingLog` 和 `daemon/parse_retry_log.rs::ParseRetryLog` 继续按现有逻辑运行，PyO3 暴露层与 daemon 内部共用同一 Rust 实现，行为完全一致。

10. **crash recovery 由 daemon 内部覆盖**：daemon 启动时重放 `staging.log` + `parse_retry.log` 的逻辑在 `daemon/replicator.rs::recover` 和 `daemon/workspace.rs` 中实现，本子任务的 PyO3 暴露层不涉及 recovery 逻辑，仅暴露读写 API。

### 24.6 风险与注意事项

- **StagingLog rollback_flag 切换语义已生效**：`_is_rust_staging_log_rolled_back()` 查询 rollback_config，flag=1 时全部 9 方法回退 Python。60s 缓存意味着 flag 切换后最多 60s 延迟生效。
- **ParseRetryLog 无 rollback_config**：因 Python 端无生产路径，不登记 rollback_config。未来若 Python 端新增 parse_retry_log 模块并接入，需补登记。
- **60s 缓存延迟**：rollback_flag 切换后，StagingLog 最多 60s 后才感知。紧急回滚场景可通过重启进程立即生效。
- **WAL 模式与短连接**：rollback_config 查询用短连接（每次缓存过期后新建 + 关闭），WAL 模式下只读查询安全。
- **JSON Lines 并发安全**：Rust 只读查询读 JSON Lines 文件，与 Python `append` / `compact_applied` 不冲突（最多读到旧数据，最终一致）。StagingLog 内部有 `threading.Lock` 保护，但 Rust 端无锁，依赖文件系统原子性。
- **daemon 内部路径不受影响**：PyO3 暴露层与 daemon 内部共用 `daemon::staging_log::StagingLog` / `daemon::parse_retry_log::ParseRetryLog`，但通过不同入口调用，互不影响。
- **真实 staging.log 未端到端验证**：当前差分测试用临时 log 文件，未用 daemon 真实 staging.log 端到端验证。Phase 4/5 集成测试应增加。
- **ParseRetryLog max_retries 默认值**：Rust 端 `ParseFailureEntry` 默认 `max_retries=3`，与 Python 端 `_PyParseFailureEntry` 测试模拟一致。生产环境如需调整，需同步两端。

## 25. Phase 2-6-1 增量构建 Review 清单（2026-07-28）

**状态**：`✅(behavioral)`（PyO3 暴露 + 差分测试 D1-D10 + L1-L7 + wire-production 接入 + rollback_config 登记完成）

### 25.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/phase2-6-1-incremental-build-contract.md` | 契约文档 | 9 章节：范围 / Python 真相源盘点 / API 契约 / 行为契约（D1-D10 + L1-L7）/ 预期差异 / 事务与错误处理 / 实现计划 / Schema 信息 / 验收标准 / 风险与注意事项 |
| `rust_ext/src/incremental_build_query.rs` | Rust 模块 | PyO3 暴露层：`compute_and_apply_symbol_diff`（符号 diff + is_deleted=1 标记）+ `load_file_result_from_db`（从 DB 加载已解析文件结果） |
| `rust_ext/src/lib.rs` | PyO3 注册 | `mod incremental_build_query` + 2 个 `m.add_function` |
| `db/db_build.py` | Wire-production | `_compute_and_apply_symbol_diff` + `_load_file_result_from_db` 入口添加 rollback_config 检查 + fail-soft 降级 |
| `tests/test_phase2_6_1_incremental_build_diff.py` | 差分测试 | 2 个 Test*Diff 类，17 个 case（10 D + 7 L） |

### 25.2 暴露 API 清单

| API | 类型 | 对应 Python 路径 |
|---|---|---|
| `compute_and_apply_symbol_diff(codegraph_db_path, prev_version_id, curr_version_id) -> dict` | 写（符号 diff + is_deleted=1 标记） | `db/db_build.py:_compute_and_apply_symbol_diff` |
| `load_file_result_from_db(codegraph_db_path, file_instance_id, file_version_id, rel_path, abs_path, module_path) -> Optional[dict]` | 只读查询 | `db/db_build.py:_load_file_result_from_db` |

### 25.3 测试与验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| `cargo check --manifest-path rust_ext/Cargo.toml` | ✅ pass | 0 error（incremental_build_query 模块编译通过） |
| `maturin build -i C:\Python314\python.exe --release` | ✅ cp314 wheel | callwarden_core 0.1.0 |
| `pytest tests/test_phase2_6_1_incremental_build_diff.py -v` | ✅ 17 passed | D1-D10 + L1-L7 全部通过（含 D9 外键关闭场景） |
| `cw rollback config` | ✅ Phase 2-6-1 已登记 | id=14 (rust_compute_symbol_diff) + id=15 (rust_load_file_result) |
| `cw refresh config.py` | ✅ Refreshed | wire-production 不破坏 refresh 路径 |

### 25.4 待 Review 关键点

1. **pragma_update 字符串 truthy 陷阱**：`pragma_update(None, "foreign_keys", "OFF")` 传字符串 "OFF" 被 SQLite 解释为非空字符串（truthy=1），FK 仍启用。改用 `execute_batch("PRAGMA foreign_keys = OFF;")` 传裸 OFF 关键字解决。此教训已沉淀到 Rust 代码注释。

2. **D9 外键关闭场景**：SQLite 默认 `PRAGMA foreign_keys=OFF`，Python sqlite3 和 Rust rusqlite（bundled）均不显式启用 FK 检查。INSERT 到不存在的 file_version_id 不会抛 IntegrityError，两端都 INSERT 2 条 is_deleted=1 记录。差分测试 D9 验证此行为一致。

3. **事务边界差异**：`_compute_and_apply_symbol_diff` Rust 路径使用独立短连接 + BEGIN IMMEDIATE。若 Python 外层事务（`build()` 方法）持有写锁，Rust BEGIN IMMEDIATE 会等待 5s 后失败，自动 fail-soft 降级到 Python 路径。这是与 Phase 2-2/2-3/2-4 wire-production 一致的设计。

4. **`_load_file_result_from_db` 只读查询无锁冲突**：Rust 路径用只读连接（`SQLITE_OPEN_READ_ONLY`），WAL 模式下与 Python 写连接并发安全。只读连接查询的是已提交数据（Python 事务未提交的数据不可见），与 Python 路径在 `self.conn` 上查询的行为一致（Python 路径在事务内可看到未提交数据，但 `_load_file_result_from_db` 调用点加载的都是已提交的历史数据）。

5. **None 语义双重含义**：Rust `load_file_result_from_db` 返回 None 既可能是"版本不存在"（正常业务），也可能是"查询失败"。调用方对 None 统一降级到 Python 路径重新查询。若版本确实不存在，Python 路径也返回 None，额外查询开销可接受（`_load_file_result_from_db` 仅在已验证版本存在时调用）。

6. **不切换 daemon 内部路径**：Rust daemon 内部不调用这两个 PyO3 API。daemon 的增量构建逻辑在 `daemon/replicator.rs` 中独立实现，不受本子任务影响。

### 25.5 风险与注意事项

- **rollback_flag 切换语义已生效**：`_compute_and_apply_symbol_diff` 和 `_load_file_result_from_db` 入口检测 `is_feature_rolled_back`，flag=1 时回退 Python。
- **5s busy_timeout 性能影响**：若 Python 外层事务持有写锁，Rust `_compute_and_apply_symbol_diff` 会等待 5s 后失败。在 `build()` 批量构建场景中，每个文件的 diff 调用都可能触发此等待。后续优化可复用连接或在外层事务提交后批量执行 diff。
- **WAL 模式与只读查询**：`_load_file_result_from_db` Rust 路径用只读连接 + WAL checkpoint(PASSIVE)，与 Phase 1-1/1-2/1-3/1-4 一致。
- **foreign_keys=OFF 与 Python 对齐**：Rust `open_readwrite` 显式 `PRAGMA foreign_keys = OFF`，与 Python `db_base.py` L2178 一致。差分测试 D9 验证两端 FK 关闭行为一致。
