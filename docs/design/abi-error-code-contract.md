# Parse/Query/Storage ABI 与错误码契约（Phase 0 子任务 2 Contract）

> 本文件是 [rust-full-migration-self-bootstrap-plan.md](rust-full-migration-self-bootstrap-plan.md) Phase 0 第二个子任务的契约交付物。
> 它固定 Parse / Query / Storage 三个边界的 ABI、错误码、权限和事务边界，
> 作为后续 Phase 1-2 各功能子任务的 Before-Edit Contract 基线。
>
> 真相源：`migration-manifest.md` §4-6、`rust_ext/src/lib.rs`、`rust_ext/src/multi_lang.rs`、
> `rust_ext/src/graph.rs`、`db/schema.py`、`db/db_cas.py`、`db/rust_parser_facade.py`。
>
> 维护规则：每次 ABI 变更必须同步本文件 + `migration-manifest.md` 第 4-5 节。

## 1. Parse ABI 契约

### 1.1 入口函数（已通过 PyO3 暴露）

| 函数 | 签名 | 说明 |
|---|---|---|
| `parse_file_lang(path: &str, module_path: &str, lang: &str) -> PyResult<PyDict>` | 单文件解析（15 语言通用） | 主生产路径 |
| `parse_canonical_bytes_py(canonical_bytes: &[u8], file_size: usize, total_lines: u32, content_hash: &str, module_path: &str, lang: &str) -> PyResult<PyDict>` | 规范化字节解析 | 跳过 canonicalize，复用 CAS |
| `batch_parse_files_lang(files: Vec<(String, String, String)>, num_threads: usize) -> PyResult<Vec<PyDict>>` | 批量解析（一次性返回 list） | 小批量 |
| `batch_parse_files_lang_pool(files: Vec<(String, String, String)>, num_threads: usize) -> PyResult<ParseResultPool>` | 批量解析（pool 懒加载） | 大批量 |
| `parse_c_file(path: &str, module_path: &str) -> PyResult<PyDict>` | C 单文件解析 | C 专用快路径 |
| `batch_parse_c_files(files: Vec<(String, String)>, num_threads: usize) -> PyResult<Vec<PyDict>>` | C 批量 | C 专用 |
| `canonicalize_source_py(abs_path: &str) -> PyResult<PyDict>` | 输入规范化 | BOM 剥离 + CRLF→LF |

### 1.2 ParseResult 输出契约（ABI v1）

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
    pub error: Option<String>,           // 兼容旧调用方
    pub diagnostics: ParseDiagnostics,  // 结构化诊断（替代 error）
}
```

Python dict 字段一一对应（snake_case）。`error` 字段保留以兼容旧调用方，新代码必须读 `diagnostics`。

### 1.3 SymbolInfo 字段契约

| 字段 | 类型 | 说明 | NULL 语义 |
|---|---|---|---|
| `name` | String | 符号简单名 | 永非空 |
| `qualified_name` | String | 限定名（`module_path.SymbolName`） | 永非空 |
| `kind` | String | 符号类型（`fn`/`method`/`struct`/`class`/`enum` 等） | 永非空 |
| `start_line` | u32 | 起始行（1-based） | 永非 0 |
| `end_line` | u32 | 结束行（1-based，含） | >= start_line |
| `module_path` | String | 模块路径 | 永非空 |
| `symbol_hash` | String | 内容哈希（blake2b） | 永非空 |
| `depth` | i32 | 调用深度 | -1 = 未计算 |
| `has_comment` | bool | 是否有注释 | false = 无 |
| `visibility` | String | 可见性（`public`/`private`/`protected`/`internal`/`package`/`package-private`） | 永非空 |
| `content` | String | 符号源码正文 | 永非空 |
| `signature` | String | 声明头（不含 body） | 永非空 |
| `local_id` | Option<u32> | 文件内 1-based ID | None = 未分配 / 0 = 保留（synthetic module symbol） |
| `lexical_parent_local_id` | Option<u32> | 词法父符号 ID | None = 顶层符号 |

### 1.4 RawCall 字段契约

| 字段 | 类型 | 说明 | NULL 语义 |
|---|---|---|---|
| `caller_name` | String | 调用者名 | 顶层裸调用时为空字符串 |
| `callee_name` | String | 被调用者名 | 永非空 |
| `callee_module` | String | 被调用者模块 | 空字符串 = 未解析 |
| `call_line` | u32 | 调用行（1-based） | 永非 0 |
| `caller_local_id` | Option<u32> | 调用者 local_id | None = 顶层裸调用 |
| `ordinal` | u32 | 调用序号（同一 caller 内） | 0-based |

### 1.5 ParseDiagnostics 契约

```rust
pub struct ParseDiagnostics {
    pub syntax_error_count: u32,
    pub unsupported_construct_count: u32,
    pub status: String,  // "ok" | "partial" | "failed" | "unsupported"
}
```

**状态推导规则**（`parse_status_from_fields`）：

| `syntax_error_count` | `unsupported_construct_count` | `status` | CAS 发布策略 |
|---|---|---|---|
| 0 | 0 | `ok` | 发布到 `ready`，替换 snapshot |
| > 0 | 0 | `partial` | 发布到 `partial`，不替换 snapshot |
| 0 | > 0 | `unsupported` | 不发布，记录 diagnostics |
| > 0 | > 0 | `partial` | 发布到 `partial` |
| 不可恢复错误 | - | `failed` | 不发布，记录 retry log |

## 2. Query ABI 契约

### 2.1 GraphStore 入口（已通过 PyO3 暴露）

| 方法 | 签名 | 说明 |
|---|---|---|
| `GraphStore.new()` | 构造空 store | 初始状态 `empty` |
| `load_from_sqlite(db_path, workspace_id=0)` | 加载 symbols + calls | 返回 (symbols, calls) 数量 |
| `load_symbols_from_sqlite(db_path, workspace_id=0)` | 仅加载符号 | 分级冷启动 |
| `load_calls_from_sqlite(db_path, workspace_id=0)` | 仅加载 calls | 复用已加载符号层 |
| `fork_symbols()` | 共享符号层创建新 store | 后台构建调用图 |
| `load_state()` | 返回 `empty`/`symbols_ready`/`graph_ready` | 查询路径选择 |
| `get_callers(callee_name, qualified_name=None)` | 反向调用 | 返回 `CallersBatch`（懒批量） |
| `get_callees(caller_name, qualified_name=None)` | 正向调用 | 返回 list[dict] |
| `get_symbol(qualified_name)` | 查询单符号 | 返回 Option[dict] |
| `search_symbols(query, kind=None, limit=None)` | 模糊搜索 | 返回 `SymbolSearchBatch`（懒批量） |
| `get_call_chain_down(qualified_name, max_depth)` | 下行调用链 | 返回 list[dict] |
| `get_topological_order()` | 拓扑排序 | 返回 list[str] |
| `detect_cycles()` | 环检测 | 返回 list[list[str]] |
| `stats()` | 统计 | 返回 dict |
| `memory_breakdown()` | 内存分布 | 返回 dict |
| `compute_depth_all()` | 计算深度 | 返回 list[(u32, i32)] |
| `dump_to_file(path)` | 序列化到文件 | 用于 snapshot |
| `load_from_file(path)` | 从文件加载 | 用于 snapshot |

### 2.2 GraphStore 内部 Rust API（lib.rs 之外）

| 方法 | 说明 | 暴露计划 |
|---|---|---|
| `new_with_data(symbols, calls)` | 直接构造 | 内部使用 |
| `file_count()` | 文件数 | 内部 |
| `get_symbol_ref(qname)` | 查符号引用 | 内部 |
| `get_caller_ids(callee_id)` | 反向调用 ID 列表 | 内部 |
| `get_callee_ids(caller_id)` | 正向调用 ID 列表 | 内部 |
| `call_chain_down_rust(qname, max_depth)` | 下行调用链 | 内部 |
| `topological_order_rust()` | 拓扑排序 | 内部 |
| `detect_cycles_rust()` | 环检测 | 内部 |
| `get_symbol_by_id(id)` | ID 查符号 | 内部 |
| `get_file_rel_path(file_instance_id)` | 文件路径 | 内部 |
| `get_symbols_by_file(rel_path)` | 文件符号列表 | 内部 |
| `get_name_to_qnames()` | 名字→qname 映射 | 内部 |
| `get_all_qualified_names()` | 全部 qname | 内部 |

### 2.3 GraphStore 加载状态机

```text
empty → load_symbols_from_sqlite → symbols_ready
empty → load_from_sqlite → graph_ready
symbols_ready → load_calls_from_sqlite → graph_ready
symbols_ready → fork_symbols → empty (with shared symbols)
```

### 2.4 GraphStore workspace_id 过滤契约

- `workspace_id=0`：不过滤（兼容旧测试和单 workspace DB）
- `workspace_id>0`：SQL 层 `WHERE workspace_id = ?` 过滤 file_instances 和 symbols
- 生产路径（daemon / db_base）必须传 >0 的 workspace_id，避免 snapshot 混入其他 workspace 符号

### 2.5 懒批量对象契约

| 类 | 暴露方法 | 说明 |
|---|---|---|
| `ParseResultPool` | `__len__`, `__getitem__`, `to_list`, `count` | parse 结果池 |
| `ParseResultStream` | `__len__`, `__getitem__`, `to_list`, `count` | 流式 parse 结果 |
| `CallersBatch` | `__len__`, `__getitem__`, `to_list`, `count` | get_callers 懒批量 |
| `SymbolSearchBatch` | `__len__`, `__getitem__`, `to_list`, `count` | search_symbols 懒批量 |

**边界物化规则**（AGENTS.md 规则 17）：MCP / daemon service / 公开 Python API 若声明返回 `List[...]`，必须在边界执行 `list(result)`，不直接交给 JSON 序列化或依赖 list 契约的调用方。

## 3. Storage ABI 契约

### 3.1 数据库路径

- **用户级单库**：`$HOME/.callwarden/callwarden.db`
- **WAL 模式**：`PRAGMA journal_mode=WAL`
- **busy_timeout**：5000ms（写操作）
- **immutable=1 只读连接**：用于 GraphStore 加载，加载前必须 `PRAGMA wal_checkpoint(PASSIVE)`（AGENTS.md 规则 7）

### 3.2 Schema 版本契约

- **当前版本**：`SCHEMA_VERSION = 41`（`db/schema.py`）
- **迁移机制**：`db_migrate.py` 启动时根据 `schema_version` 表升级
- **CAS schema**：独立 DDL（`db/db_cas.py:CAS_SCHEMA_DDL`），由 `init_cas_schema()` 初始化
- **不变量**：schema 变更必须同步更新 `SCHEMA_VERSION` + `migration-manifest.md` 第 4 节 + `docs/architecture.md`

### 3.3 核心表契约（生产路径使用）

| 表 | 用途 | 关键字段 | 写入方 |
|---|---|---|---|
| `workspaces` | workspace 注册 | `id`/`name`/`root_path`/`is_active` | CLI `workspace register` |
| `file_contents` | 内容去重 | `content_hash` (PK) | refresh |
| `file_instances` | 文件实例 | `workspace_id`/`rel_path`/`current_content_hash` | refresh |
| `symbols` | 符号快照 | `file_instance_id`/`symbol_hash`/`qualified_name` | refresh |
| `calls` | 调用快照 | `caller_id`/`callee_name`/`call_line` | refresh |
| `comments` | 注释 | `symbol_hash` | refresh |
| `file_versions` | 版本历史 | `file_instance_id`/`commit_sha` | git import |
| `symbol_contents` | 符号正文去重 | `content_hash` (PK) | refresh |
| `file_symbol_versions` | 符号版本 | `symbol_hash`/`commit_sha` | refresh |
| `call_versions` | 调用版本 | `caller_id`/`commit_sha` | refresh |
| `cas_file_cache` | CAS 文件缓存 | `cas_key` (PK)/`state` | Rust daemon |
| `cas_symbols` | CAS 符号 | `cas_key`/`local_symbol_id` | Rust daemon |
| `cas_raw_calls` | CAS 调用 | `cas_key`/`caller_local_id` | Rust daemon |
| `cas_imports` | CAS imports | `cas_key`/`import_path` | Rust daemon |
| `cas_pending_refs` | GC 引用保护 | `cas_key`/`workspace_id`/`expires_at` | Rust daemon |
| `cas_symbol_contents` | CAS 符号正文 | `content_hash` (PK) | Rust daemon |
| `file_generations` | 两阶段 CAS | `workspace_id`/`rel_path`/`latest_committed_generation` | Rust daemon |

### 3.4 CAS 状态契约

| `cas_file_cache.state` | 含义 | lookup 命中？ | 发布者 |
|---|---|---|---|
| `building` | 正在写入 payload | ❌ | `cas_publish` 阶段 1 |
| `ready` | 完整解析结果 | ✅ | `cas_publish` 阶段 4 |
| `partial` | 语法错误结果，不替换 snapshot | ❌ | `publish_with_status(partial)` |

**R13-P0-1 不变量**：`partial` 状态不会被 `cas_lookup` 命中，防止第二次 refresh 绕过 snapshot_guard 保护。

### 3.5 CAS Key 计算契约

```python
cas_key = sha256(
    f"{content_hash}|{language}|{parser_version}|{callwarden_version}|"
    f"{extraction_config_version}|{abi_version}|{input_abi_version}"
)
```

- `parser_version`：Rust parser 版本（`core_version()`）
- `callwarden_version`：项目版本
- `extraction_config_version`：提取配置版本（`v1`）
- `abi_version`：ParseFact ABI 版本（`v1`）
- `input_abi_version`：输入规范化 ABI 版本（`v1`）

### 3.6 file_generations 两阶段 CAS 契约

```text
file_generations:
  workspace_id, rel_path (PK)
  latest_session_id, latest_session_epoch, latest_seq
  latest_seen_generation      # parse 完成但未 commit
  latest_committed_generation # 已 commit，可被查询
```

**daemon recover 状态机**：

1. `staging_log.append(parse_result)` → best-effort CAS 恢复
2. `merge_cas_to_db(cas_key)` → symbols/calls 写入 DB
3. `file_generation_committed(generation)` → UPDATE file_generations
4. `Replicator::replicate` → 发布 snapshot
5. 失败回滚：`file_generation_uncommit` → 不更新 committed_generation

## 4. 错误码契约

### 4.1 错误码枚举（与 manifest.md §5 一致）

| 错误码 | 含义 | 处理策略 | exit code |
|---|---|---|---|
| `PARSE_OK` | 解析成功 | 正常发布 | 0 |
| `PARSE_PARTIAL` | 语法错误，结果可用但不替换 snapshot | 发布到 CAS `partial` | 0 |
| `PARSE_FAILED` | 解析失败 | 不发布，记录 retry log | 1 |
| `PARSE_UNSUPPORTED` | 不支持的语言/构造 | 不发布，记录 diagnostics | 1 |
| `PARSE_FATAL` | 不可恢复错误（OOM/IO） | 进程级处理 | 2 |
| `CAS_LOCKED` | CAS 写锁冲突 | 重试 3 次，间隔 2s | 2 |
| `DB_LOCKED` | SQLite 写锁冲突（busy_timeout=5000） | 返回友好提示 | 2 |
| `SNAPSHOT_STALE` | stale generation | 拒绝发布，记录 | 1 |
| `ACL_DENIED` | UID/workspace 权限不足 | 拒绝，audit 记录 | 3 |
| `BUDGET_EXCEEDED` | 资源预算超限 | 拒绝，metrics 记录 | 3 |
| `RECOVERY_FAILED` | 恢复失败 | 回滚 `file_generation_uncommit` | 2 |
| `TRANSPORT_ERROR` | UDS 传输错误 | 连接级处理 | 2 |

### 4.2 PyO3 错误映射契约

| Rust 错误类型 | PyO3 异常 | Python 端处理 |
|---|---|---|
| `std::io::Error` | `pyo3::exceptions::PyIOError` | 记录日志，返回错误 |
| `rusqlite::Error` | `pyo3::exceptions::PyRuntimeError` | 区分 BUSY / LOCKED |
| `serde_json::Error` | `pyo3::exceptions::PyValueError` | 解析失败 |
| `tree_sitter::LanguageError` | `pyo3::exceptions::PyRuntimeError` | grammar 加载失败 |
| 自定义错误 | `pyo3::exceptions::PyRuntimeError` | 携带错误码字符串 |

### 4.3 SQLite 锁错误友好提示

```python
# db_base.py / cli/main.py
try:
    conn.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError as e:
    if "database is locked" in str(e):
        print("数据库正忙，请几秒后重试")
        sys.exit(2)
    raise
```

## 5. 权限与事务边界

### 5.1 数据库锁策略（AGENTS.md 规则 6）

| 操作类型 | 锁 | 策略 |
|---|---|---|
| 只读查询 | 无写锁 | 跳过 workspace 激活，WAL 并发安全 |
| 写操作 | 写锁 | `busy_timeout=5000`，超时抛 `db_locked` |
| MCP Server stdio 长连接 | 只读走 MCP，写走 CLI | 避免 5% 撞锁 |

### 5.2 只读命令分类（AGENTS.md 规则 6）

| 命令 | 类型 |
|---|---|
| `cw symbol`/`cw callers`/`cw callees`/`cw call-chain`/`cw search`/`cw grep`/`cw file` | 只读 |
| `cw task list`/`show`/`findings` | 只读 |
| `cw rule list`/`candidate`/`applicable`/`extract` | 只读 |
| `cw audit verify`/`keys` | 只读 |
| `cw bootstrap status` | 只读 |
| `cw clone list`/`stats` | 只读 |
| `cw workspace list` | 只读 |
| `cw git log`/`show`/`stats`/`check-task`/`destructive-log` | 只读 |
| `cw semgrep list`/`stats` | 只读 |
| `cw coverage fn`/`uncovered` | 只读 |
| `cw fts status` | 只读 |
| `cw graph build-from-c` | 只读（仅内存 CSR + 报告） |
| `cw config explain`/`paths` | 只读 |

### 5.3 daemon ACL（Phase 4）

- `SO_PEERCRED` 获取对端 UID/GID/PID
- `ADMIN_ONLY_METHODS`：`backup`/`restore`/`gc`/`mount`/`workspace delete`
- workspace owner 校验：非 owner 只能查询，不能修改
- 跨 UID E2E：两个真实 UID 无法跨 workspace 越权

### 5.4 事务边界

| 事务 | 范围 | 回滚 |
|---|---|---|
| 单文件 refresh | canonicalize → parse → CAS publish → DB merge → snapshot publish | CAS 保留上一代，snapshot 不更新 |
| `cas_publish` 四阶段 | building → payload → raw calls → ready | `BEGIN IMMEDIATE` 包裹，GC 清理 building 残留 |
| workspace recover | staging append → merge CAS → committed → replicate | `file_generation_uncommit` 回滚 |
| task apply | step 状态机 + audit chain | `task_rollback` |
| schema migration | 启动时一次性，`SCHEMA_VERSION` 守卫 | 备份 + restore |

## 6. 生产接入点契约

### 6.1 已接入的 Rust 生产入口

| 入口 | 模块 | 切换方式 |
|---|---|---|
| `db/rust_parser_facade.py` | 生产解析统一入口 | `CW_PARSE_MODE` 环境变量 |
| `db/db_build.py:_write_symbols_db` | symbol 写入 | 通过 facade 调用 Rust parse |
| `db/db_build.py:_write_calls_db` | call 写入 | 通过 facade 调用 Rust parse |
| `db/db_query.py:get_callers` | 反向调用 | Rust `GraphStore` 短路（B-P7b） |
| `db/db_query.py:get_callees` | 正向调用 | Rust `GraphStore` 短路（B-P7b） |
| `db/db_query.py:search_symbols` | 模糊搜索 | Rust `GraphStore` 短路（B-P7b） |
| `db/db_daemon.py` | daemon 状态同步 | `rust_ext/src/bin/cw_daemon.rs` |

### 6.2 待接入的 Rust 入口（Phase 1-2）

| 入口 | 模块 | 迁移阶段 |
|---|---|---|
| `db/db_base.py` 连接管理 | `StorageService` trait | Phase 1 |
| `db/db_migrate.py` schema migration | `StorageService` trait | Phase 1 |
| `db/db_cas.py` CAS 操作 | `CasService` trait | Phase 1 |
| `db/db_workspace_manifest.py` manifest | `ManifestService` trait | Phase 1 |
| `server/replicator.py` CAS→DB 复制 | `ReplicatorService` trait | Phase 1 |
| `server/snapshot_manager.py` snapshot | `SnapshotService` trait | Phase 1 |
| `db/db_build.py` 批量写入 | `BuildService` trait | Phase 2 |

## 7. 性能基线（待 Phase 0 第3个子任务固化）

| 指标 | 目标 | 验证方法 |
|---|---|---|
| 单文件 parse P95 | < 100ms | `tests/test_phase1_parse_benchmark.py` |
| GraphStore 加载 1M 符号 | < 5s | `tests/test_graphstore_staged_loading.py` |
| GraphStore get_callers P50 | < 1ms | `tests/test_b_graph_store.py` |
| watcher 单文件更新 P95 | < 3s | `tests/test_phase5_watcher.py` |
| 核心二进制体积 | 不含 Python runtime | `release/inspect_pyinstaller_bundle.py` |

## 8. 不变量（迁移期间不得违反）

1. **CAS 状态隔离**：`partial` 状态不被 `cas_lookup` 命中
2. **workspace_id 过滤**：生产路径必须传 >0 的 workspace_id
3. **懒批对象物化**：MCP/daemon/公开 API 必须在边界 `list(result)`
4. **schema 版本同步**：DDL 变更必须更新 `SCHEMA_VERSION`
5. **ABI 向后兼容**：新增字段必须有默认值，不破坏旧调用方
6. **错误码统一**：所有失败路径必须返回枚举错误码，不静默吞掉异常
7. **事务原子性**：`cas_publish` / `file_generation_committed` / `Replicator::replicate` 必须原子
8. **回滚可恢复**：任何事务失败后必须能恢复到上一致状态

## 9. Review 清单

### 9.1 待 Review 关键点

1. **ABI 完整性**：第 1-3 节是否遗漏关键字段？是否与现有代码一致？
2. **错误码覆盖**：第 4 节 12 个错误码是否覆盖所有失败场景？
3. **权限边界**：第 5 节只读/写命令分类是否完整？daemon ACL 是否合理？
4. **事务原子性**：第 5.4 节事务回滚是否覆盖所有场景？
5. **生产接入点**：第 6 节是否遗漏关键入口？
6. **不变量**：第 8 节 8 个不变量是否充分？
7. **性能基线**：第 7 节指标是否合理？是否需要补充？

### 9.2 风险与注意事项

- **`error` 字段双重含义**：旧字段保留兼容，新代码必须读 `diagnostics`，存在误用风险
- **`workspace_id=0` 兼容**：旧测试和单 workspace DB 使用 0，生产必须 >0，存在混用风险
- **懒批对象**：未物化时 JSON 序列化会失败，需在所有边界确保 `list(result)`
- **CAS schema 独立 DDL**：与主 schema 分离，初始化顺序敏感
- **immutable=1 只读连接**：必须先 `wal_checkpoint(PASSIVE)`，否则读到旧数据

## 10. Phase 0 子任务 2 Review 清单（2026-07-27）

### 10.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/abi-error-code-contract.md` | 文档（真相源） | 9 章节：Parse ABI / Query ABI / Storage ABI / 错误码 / 权限事务 / 生产接入点 / 性能基线 / 不变量 / Review |
| `rust_ext/src/abi_contract.rs` | Rust 模块 | ABI 版本常量 / CAS 状态常量 / GraphStore 加载状态 / ParseStatus 枚举 / ErrorCode 枚举 / 契约不变量函数 + 12 单元测试 |
| `tests/test_abi_contract.py` | Python 测试 | 26 个差分测试：Parse ABI 一致性 / Query ABI 一致性 / Storage ABI 一致性 / 错误码一致性 / 不变量 |
| `server/abi_contract_service.py` | Python 服务 | AbiContractService 查询服务（只读/无状态/无锁），镜像 Rust 模块常量和枚举 |

### 10.2 测试结果

| 测试套件 | 结果 |
|---|---|
| Rust `abi_contract` 单元测试 | ✅ 12 passed |
| Python `test_abi_contract.py` | ✅ 26 passed |
| Python `test_migration_manifest.py`（回归） | ✅ 15 passed, 1 skipped |
| `cargo check` 编译 | ✅ 通过（仅 warnings） |
| `AbiContractService` 跨语言一致性 | ✅ abi_version=v1/schema_version=41/CAS partial state 正确 |

### 10.3 待 Review 关键点

1. **ABI 完整性**：第 1-3 节是否遗漏关键字段？是否与现有代码一致？
2. **错误码覆盖**：第 4 节 12 个错误码是否覆盖所有失败场景？exit code 分配是否合理？
3. **权限边界**：第 5 节只读/写命令分类是否完整？daemon ACL 是否合理？
4. **事务原子性**：第 5.4 节事务回滚是否覆盖所有场景？`cas_publish` 四阶段是否真的原子？
5. **生产接入点**：第 6 节是否遗漏关键入口？已接入 vs 待迁移划分是否准确？
6. **不变量**：第 8 节 8 个不变量是否充分？是否需要补充？
7. **跨语言一致性**：Rust `abi_contract.rs` 和 Python `abi_contract_service.py` 镜像是否完整？后续是否应通过 PyO3 直接共享？
8. **SCHEMA_VERSION 同步**：Rust 镜像常量 41 与 `db/schema.py` 一致，但 schema 变更时如何保证同步更新？

### 10.4 风险与注意事项

- **`error` 字段双重含义**：旧字段保留兼容，新代码必须读 `diagnostics`，存在误用风险
- **`workspace_id=0` 兼容**：旧测试和单 workspace DB 使用 0，生产必须 >0，存在混用风险
- **懒批对象**：未物化时 JSON 序列化会失败，需在所有边界确保 `list(result)`
- **CAS schema 独立 DDL**：与主 schema 分离，初始化顺序敏感
- **immutable=1 只读连接**：必须先 `wal_checkpoint(PASSIVE)`，否则读到旧数据
- **跨语言常量镜像**：Rust `abi_contract.rs` 和 Python `abi_contract_service.py` 是镜像关系，变更时需双向同步。Phase 1+ 可考虑通过 PyO3 直接暴露 Rust 模块，消除镜像
- **SCHEMA_VERSION 真相源**：Python `db/schema.py` 是真相源，Rust 侧只是镜像常量。schema 变更时必须同步更新两处
- **错误码扩展性**：新增错误码时需同步更新 Rust 枚举 + Python 注册表 + 契约文档，存在遗漏风险
