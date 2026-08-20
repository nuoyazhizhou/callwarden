# C3：Global/Local CAS 完整迁移契约

> 状态：contract + implement + wire-production（2026-08-08 已接线）
> 父任务：`T-1785590602455-4f56ef24`（C3 Global Local CAS 完整迁移）
> 关联文档：[phase1-cas-contract.md](phase1-cas-contract.md)、[phase2-1-cas-merge-py暴露-contract.md](phase2-1-cas-merge-py暴露-contract.md)

## 1. 现状盘点（2026-08-08 核实）

### 1.1 Rust 侧已就绪（CasStore 单一实现）

`rust_ext/src/daemon/cas.rs::CasStore` 已实现全部写方法（原子 BEGIN IMMEDIATE 事务）：

| 方法 | 行号 | 语义 |
|---|---|---|
| `publish` / `publish_with_status` | — | Global CAS 发布（含 content 去重 + pending refs） |
| `pin` | — | pending refs 固定 |
| `gc` / `gc_unreferenced` | 663 / 808 | CAS GC（flock + BEGIN IMMEDIATE / grace_days 策略） |
| `file_generation_seen` | 905 | Local CAS 两阶段 seen（stale commit/seen 拦截） |
| `file_generation_committed` | 1005 | Local CAS 两阶段 committed（条件 UPDATE） |
| `file_generation_uncommit` | 1060 | Local CAS 回滚 |

PyO3 facade 已暴露：

| Facade | 文件 | 状态 |
|---|---|---|
| `compute_cas_key_v1` / `cas_global_lookup` / `cas_global_get_state` / `cas_global_count_files` / `cas_local_get_file_generation` | `cas_query.rs` | ✅ 已暴露（只读） |
| `cas_publish_with_retry` / `cas_pin` | `cas_write_query.rs` | ✅ 已暴露（写） |
| `cas_merge_to_codegraph` / `cas_merge_init_schema` | `cas_merge_query.rs` | ✅ 已暴露（写，M1-M8/S1-S2 契约） |

### 1.2 两条 daemon 生产路径

| 路径 | 平台 | refresh 处理 | file_generations 写 | merge | 是否合规 |
|---|---|---|---|---|---|
| Rust daemon（`cw-daemon.exe` / workspace.rs `handle_workspace_file_refresh` → `daemon_handle_refresh`） | Windows | Rust | **CasStore 方法**（非直接 SQL） | `cas_merge::merge_cas_to_codegraph` | ✅ 合规 |
| Python daemon（`EnterpriseDaemonServer`，`server/daemon_server.py` → `replicator.py::daemon_handle_refresh`） | Linux/macOS | Python | **直接 SQL**（replicator.py L273-309 seen、L478 committed） | **Python `db_cas_merge.py::merge_cas_to_codegraph`**（L382/407） | ❌ 缺口 A/B |

### 1.3 缺口清单

- **缺口 A**：`server/replicator.py::daemon_handle_refresh` 直接 SQL 写 `file_generations`（seen/committed），绕过 CasStore 原子语义。
- **缺口 B**：`server/replicator.py` 调用 Python `merge_cas_to_codegraph`（`db_cas_merge.py`），未接线 Rust `cas_merge_to_codegraph`（已实现）。
- **缺口 C**：Rust `CasStore::gc`/`gc_unreferenced` 已实现但 PyO3 facade 未暴露；Python `db_cas.py::cas_gc` 无生产调用者（GC 入口待接线，归 C9/C10）。

## 2. 目标与边界

### 2.1 目标

消除 Python 生产路径直接 SQL 写 CAS：Python daemon（Linux/macOS）的
file_generations 两阶段写与 CAS→CodeGraph merge 全部改走 Rust facade（CasStore
单一实现），Rust daemon 路径保持不变。

### 2.2 边界（本任务不做）

- 不消灭 Python `daemon_server.py`（Linux/macOS 仍可运行 Python daemon，但写路径走 Rust facade）。
- 不改 Rust daemon 路径（已合规）。
- GC 生产入口（`cw gc` 命令 / daemon gc）接线归 C9/C10；本任务仅暴露 Rust facade。

## 3. Rust facade 新增 API

按 `cas_write_query.rs` 既有模式（`CasStore::open(db_path)` + `#[pyfunction]`）新增：

| Facade | 签名 | 包装 | 返回 |
|---|---|---|---|
| `cas_file_generation_seen` | `(db_path, workspace_id, rel_path, session_id, epoch, seq)` | `CasStore::file_generation_seen_inner`（短连接） | `bool`（stale 返回 false）✅ |
| `cas_file_generation_committed` | `(db_path, workspace_id, rel_path, epoch, seq)` | `CasStore::file_generation_committed_inner`（短连接） | `bool` ✅ |
| `cas_file_generation_uncommit` | `(db_path, workspace_id, rel_path)` | `CasStore::file_generation_uncommit_inner`（短连接） | `bool` ✅ |
| `cas_file_generation_reset` | `(db_path, workspace_id, session_id, epoch)` | `CasStore::file_generation_reset_inner`（短连接） | `()` ✅ |
| `cas_gc` | `(db_path, grace_days)` | `CasStore::gc_unreferenced` | `u64`（回收条目数）✅ |

失败统一返回 `{"error": ...}` 或 PyErr，与既有 facade 一致；禁止吞异常。

实现说明：
- 短连接 `open_file_generations_conn`（`cas_write_query.rs`）：`Connection::open` +
  busy_timeout(5s) + WAL + `CasStore::ensure_file_generations_table`（只 ensure
  file_generations 表，不注入 CAS_SCHEMA_DDL）。
- `daemon/cas.rs`：seen/committed/uncommit/reset 的 inner 函数改 `pub(crate)`，
  新增 `ensure_file_generations_table` 与 `file_generation_reset`（公开事务包装）。
- `lib.rs` 已注册 5 个新 facade 函数。

## 4. Python 生产接线改造

### 4.1 `server/replicator.py::daemon_handle_refresh`（✅ 已接线）

- seen 阶段：优先 `callwarden_core.cas_file_generation_seen(...)`，
  fallback 现有 Python SQL（`# fallback` 标注，与 cas_publish_with_retry 模式一致）。
- committed 阶段：优先 `callwarden_core.cas_file_generation_committed(...)`，
  fallback Python SQL。
- Rust facade 返回 false 的语义映射：seen → `{"status": "stale_seq_dropped"}`；
  committed → 抛 `ProtocolError(stale_manifest_commit)`（与 Python 条件 UPDATE
  rowcount != 1 行为一致）。

### 4.2 `server/replicator.py` merge 调用点（✅ 已接线）

- 优先 `callwarden_core.cas_merge_to_codegraph(...)`，fallback `db_cas_merge.merge_cas_to_codegraph`。

### 4.3 `daemon_handle_connect` 会话重置（✅ 已接线，契约外补充）

- 缺口 A 之外的 file_generations 写：connect 的会话级 reset 同样优先
  `callwarden_core.cas_file_generation_reset(...)`。
- **事务结构调整**：reset 从原单一事务中移出到 session 激活提交之后（Rust
  facade 为独立短连接事务，不能在 ws_conn 事务内调用）。会话级 epoch 单调递增，
  新 session epoch 恒大于旧 epoch，reset 单独提交不破坏 stale 拦截语义。
- 调用方（`server/daemon_server.py`）：`_get_workspace_resources` 新增
  `ws_db_path` 资源；connect/refresh 两个调用点传 `res["ws_db_path"]`。

### 4.4 `db/db_cas.py::cas_gc`

- 保留 Python 实现作为 fallback；Rust `cas_gc` facade 已暴露（GC 生产入口归 C9/C10）。

## 5. 行为契约（差分验证）

Python 改造后与 Rust CasStore 语义必须一致：

- **C1 原子性**：seen/committed 均为单事务（BEGIN IMMEDIATE），并发 handler 互不撕裂。
- **C2 stale 拦截**：`epoch < committed.epoch || (== 且 seq <= committed.seq)` → seen 返回 false；
  seen 严格更旧代际 → 拒绝覆盖已有 uncommitted generation。
- **C3 幂等**：重复 publish / 重复 committed 同一代际不产生重复行。
- **C4 空结果**：file_generations 无该 rel_path → 自动 INSERT OR IGNORE 初始化。
- **C5 workspace 隔离**：所有操作带 `workspace_id` 过滤，跨 workspace 不串扰。
- **C6 merge 行为**：复用 phase2-1 契约 M1-M8/S1-S2（cas_miss、幂等替换、workspace 自动创建等）。

## 6. 验收标准

1. `cargo test --lib cas_write_query::` + `cas_query::` + `cas_merge_query::` 全绿。
2. 新增差分测试：Python `cas_publish_with_retry` + Rust seen/committed 混合写入同一 DB，
   行为与纯 Python 一致（stale 场景逐项断言结构化状态，见规则 35）。
3. `server/replicator.py` 静态检查无 `file_generations` 直接 INSERT/UPDATE 残留
   （fallback 分支除外，且标注 `# fallback`）。
4. Python daemon UDS 测试（`test_enterprise_daemon_uds.py`）回归全绿。
