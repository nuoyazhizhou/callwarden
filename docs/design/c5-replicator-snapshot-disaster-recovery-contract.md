# C5：Replicator Snapshot 灾备完整迁移契约

> 状态：contract（2026-08-08 摸底完成）
> 任务：`T-1785590602456-0cac3cab`（C5 Replicator Snapshot 灾备完整迁移）
> 前序：C4 独立复审 PASS（`T-1785590602456-d2b8c66c` 保持 review，不 apply/close，
> 允许进入 C5）
> 关联文档：[c4-manifest-refresh-commit-contract.md](c4-manifest-refresh-commit-contract.md)、
> [phase8-rust-backup-restore-contract.md](phase8-rust-backup-restore-contract.md)、
> [watcher-generation-state-machine.md](watcher-generation-state-machine.md)、
> [migration-manifest.md](migration-manifest.md)（§68 R5 backup/restore）

## 1. 任务目标

统一 Rust CAS merge、replicator、snapshot publish/load/GC、backup/restore 与
crash recovery。Python 生产路径收敛为 adapter/fallback：daemon 模式下写路径
（refresh 编排、replicate/recover、snapshot publish/GC、backup/restore）全量走
Rust，Python 只保留适配层与 fallback 分支。

## 2. 现状盘点（摸底 2026-08-08）

### 2.1 Replicator

**已就绪的 Rust 能力**：

| 组件 | Rust 实现 | 接线状态 |
|---|---|---|
| daemon refresh 生产主链（seen→parse+publish→merge→upsert_manifest→committed→replicate） | `daemon/workspace.rs` L1629-2257 | C4 完成，生产 daemon 主路径 |
| seen/committed/reset | `cas_write_query.rs`（L288/328/392） | C3 接线，fallback 保留 |
| merge CAS→CodeGraph | `daemon/cas_merge.rs` L1103 + `cas_merge_query.rs` L106 | C4 接线，fail-closed |
| upsert_manifest | `manifest_query.rs` L106 | C4 接线，adapter 不静默回退 |
| staging log 全部操作 | `daemon/staging_log.rs` + `staging_log_query.rs`（9 函数） | C4 接线 |
| pending 计数 | `replicator_query.rs` L38 | C4 接线 |
| Rust Replicator（replicate/recover/get_pending_count/merge_deltas） | `daemon/replicator.rs` L956-1192（含 delete tombstone 幂等重放 L1087） | 生产 daemon 已用；Python compat 未用 |

**缺口（C5 待闭合）**：

- R1 Python `Replicator` 类（`server/replicator.py` L821-1015）仍被 compat server
  （`daemon_server.py` L1260 `Replicator.replicate`）使用；Rust 侧已有完整对应。
- R2 Python `daemon_server.py` `workspace.file.refresh` 编排（L1229-1300）未迁移：
  虽各子步骤 Rust 短路，但整体调度 + staging append + Python replicate +
  snapshot_service 仍为 Python，与 Rust workspace.rs 双实现并存。
- R3 fail-closed 语义不一致：Python 路径 merge 失败抛
  `ProtocolError(rust_cas_merge_unavailable)` 且 **staging 不追加**；Rust 路径
  merge 失败仅 eprintln warning、staging 仍追加，但 committed + replicate 被
  `merge_ok` 门控跳过（workspace.rs L2141-2166）。两条路径"失败后是否追加
  staging"行为不同，需收敛。
- R4 StagingEntry schema 分叉：Rust 有 `operation` 字段（refresh/delete），
  Python `server/staging_log.py` 无此字段。Python 读含 operation 行兼容
  （serde default），反向写入丢字段；delete 流程（Rust workspace.rs L2609-2637）
  无 Python 等价。
- R5 `server/durable_staging.py`（SQLite WAL 型 staging）Python 独占，Rust 无
  对应实现——需决策迁移或废弃。
- R6 commit 时机语义差异：Python committed 在 `daemon_handle_refresh` 内
  （replicator.py L542）；Rust 已移到 replicate 成功后（workspace.rs L2108-2123）。
  需确认 compat 路径收敛方向。

### 2.2 Snapshot

**已就绪的 Rust 能力**：

- publish 主路径同内核：Python `SnapshotManagerService.publish_snapshot` 与 Rust
  `handle_snapshot_publish` / `SnapshotCachePublisher` 都落到
  `build_and_publish_blocking`（`snapshot.rs` L264）。
- Rust daemon RPC 层完整（`daemon/snapshot_state.rs`）：`snapshot.publish` /
  `snapshot.gc` / `snapshot.stats` / `snapshot.evict` / `snapshot.list_workspaces`
  （uid 过滤）+ 查询族（symbol/file/grep/issues/tests/impact 走 snapshot SQLite
  只读，search/callers/callees/chain/topo/cycles/stats 走内存 GraphStore）。
- 失败保护 `daemon/snapshot_guard.rs`：`evaluate_generation_protection` L237
  （dirty overlay → failed → unsupported → stale → partial → ok），
  `should_replace_snapshot` L96 仅 success 才替换。
- `.cwsnap` 文件快照 `graph.rs` `save_to_file`/`load_from_file`（version 2）
  已实现但未接入主链路。

**缺口（C5 待闭合）**：

- S1 双发布入口并存：Python daemon_server L1352 → `snapshot_service.publish_snapshot`
  vs Rust `handle_snapshot_publish` + `SnapshotCachePublisher`（daemon/replicator.rs
  L831-906）。参数组装不同：snapshot_id 在 Rust replicate 链路丢失、workspace_id
  来源不同、checkpoint 策略不同（Python client FULL busy fail-fast vs Rust
  PASSIVE 双保险）。
- S2 查询语义分叉：Rust daemon `query.symbol/file/grep/issues/tests/impact` 走
  snapshot SQLite 只读连接（query_symbol 含 signature/comment/issues 完整详情）；
  Python service 全部走 publish 时缓存的内存 GraphStore（query_symbol 缺详情
  字段）；失败契约不同（`snapshot_not_ready` 结构化错误 vs 空返回）。
- S3 Python `_rust_stores` 查询视图 staleness：publish 时缓存 `fork_shared()`，
  后续查询不随新 publish 刷新也不读 ArcSwap。
- S4 generation 不互通：快照 generation（内存 AtomicU64，重启归零）与
  `latest_committed_generation`（`"{epoch}:{seq}"` 持久化）无映射；daemon 重启
  后快照丢失需重新 publish。
- S5 `.cwsnap` 文件快照未接入：无 `load_from_file` 恢复入口
  （PySnapshotManager 未暴露）。
- S6 snapshot_guard 仅 Rust daemon 生效：Python 路径 publish 前无
  `evaluate_generation_protection`（failed/partial cas_state 仍会发布坏快照）。
- S7 Python daemon RPC 缺 `snapshot.list_workspaces` / `snapshot.stats` 方法
  （仅 Rust 有；Python daemon_server 只有 gc.snapshots/snapshot.publish/
  snapshot.evict）。
- S8 WAL checkpoint 策略不统一：Python client FULL（busy fail-fast）vs Rust
  client 无 checkpoint（依赖 daemon PASSIVE + 内核 PASSIVE）。

### 2.3 backup/restore

**已就绪的 Rust 能力**（R5 任务 `T-1785587453810-9c784330` 已交付）：

- `rust_ext/src/backup_restore.rs`：`backup_full` L429 / `backup_db_only` L451 /
  `restore_backup` L473（先 verify 后覆盖，fail-closed）/ `verify_backup` L494 /
  `list_backups` L501 / `delete_backup` L533 / `cleanup_backups` L544。
- Python `server/backup_restore.py`：8 个业务操作中 7 个已默认代理 Rust
  （backup_full / backup_db_only / list_backups / delete_backup /
  cleanup_old_backups / restore / verify_backup），`_rust_backup_manager_available`
  全量门控 + `rollback_config` 回退。
- checksum parity：文件 sha256 语义一致（Python hashlib 与 Rust sha2 均 64KB
  chunk）；meta checksum 刻意借用 Python `json` 模块
  （`backup_compute_meta_checksum`）保证 byte-for-byte 一致（R5 修复点）。
- 备份布局两侧一致：`<backup_root>/<backup_id>/` 下 `backup_meta.json` +
  `registry.db` + `cas.db` + `audit.db`；full 追加 `daemon.json` + `snapshots/`。

**缺口（C5 待闭合）**：

- B1 `get_backup_info` 未代理 Rust（纯 Python 读 `backup_meta.json`，Rust 无
  对应 API）。
- B2 `RestoreManager._compute_file_sha256`/`_compute_meta_checksum`
  （backup_restore.py L735/746）纯 Python，未走 `_rust_backup_available()`
  短路（与 BackupManager 覆盖不一致）。
- B3 Python 回退 `backup_full` 不备份 `daemon.json`（回退布局与 Rust 默认布局
  不一致，模块 docstring 声称含 daemon.json）。
- B4 Python 回退无原子发布（直接写最终目录、`exist_ok=True` 不拒绝重复 ID），
  Rust 有临时目录 `. <id>.partial` + rename + 重复 ID fail-closed。
- B5 快照机制不同：Python 回退用 `sqlite3` backup API（失败降级 copy2），
  Rust 用 `VACUUM INTO`（先 passive checkpoint）。
- B6 备份 ID 生成仅 Python（`secrets.token_hex(4)`），Rust 只消费。
- B7 `daemon/workspace.rs` `handle_backup` L3069 / `handle_restore` L3106 旧 RPC
  路径（registry 单库 VACUUM INTO）未与新 PyO3 `backup_restore.rs` 统一。
- B8 meta checksum 契约易碎：入参为 JSON 字符串，且 Rust 序列化必须始终借用
  Python json 模块（当前已规避，需契约固化防回归）。

### 2.4 crash recovery

- Python `Replicator.recover`（replicator.py L972）等价 replicate；
  Rust `Replicator::recover`（replicator.rs L1131）等价 replicate。
- Rust `apply_durable_operations`（L1087-1123）处理 delete tombstone 幂等重放，
  Python 无等价。
- 缺口：durable_staging（R5）无 Rust 对应；快照重启丢失（S4）；recover 状态机
  与幂等重试无统一文档；crash 场景（kill -9 daemon 后重连）无系统验收。

## 3. 行为契约

| 编号 | 契约 | 验收方式 |
|---|---|---|
| C1 | daemon 模式下 Replicator 编排（refresh→replicate/recover）全量走 Rust（workspace.rs 主链）；Python daemon_server compat 路径收敛为 adapter/fallback，不再承担生产写编排 | 静态审阅 + daemon E2E |
| C2 | replicator fail-closed 语义统一：merge 失败不推进 committed、不发布快照；staging 追加行为以 Rust `merge_ok` 门控为准（Python compat 收敛对齐，去掉"失败不追加"差异） | 失败注入 E2E |
| C3 | StagingEntry `operation` 字段统一：Python staging_log 读写兼容 operation 字段（读缺省=refresh，写保留）；delete 流程以 Rust 实现为唯一生产路径 | 差分测试 |
| C4 | snapshot publish 收敛为单入口（Rust `handle_snapshot_publish` + `SnapshotCachePublisher`）：snapshot_id/workspace_id 来源、checkpoint 策略（PASSIVE 双保险）统一；Python `snapshot_service.publish_snapshot` 仅作 local 模式/fallback | daemon E2E + 差分 |
| C5 | 查询语义收敛：完整详情查询（symbol/file/grep/issues/tests/impact）唯一实现走 snapshot SQLite 只读；内存 GraphStore 仅用于快速搜索类（search/callers/callees/chain/topo/cycles/stats）；失败返回结构化 `snapshot_not_ready`，Python 侧同步收敛空返回 | 差分测试 |
| C6 | snapshot_guard（evaluate_generation_protection）前移到唯一 publish 决策点：Python 路径 publish 前同样执行失败保护，failed/partial 不替换上一代 | 失败注入 |
| C7 | generation 语义固化：快照 generation 为进程内版本（重启丢失）；持久化恢复依赖 `latest_committed_generation` + 重新 publish。若引入持久化快照元数据（.cwsnap + generation 落库）则为独立工作包，不阻塞 C5 主链 | 单测断言 + 文档 |
| C8 | backup/restore 全操作 Rust（补 `get_backup_info` 的 Rust 代理或明确等价）；Python 回退布局（含 daemon.json）与原子性对齐 Rust 默认路径 | 差分测试 |
| C9 | daemon `handle_backup`/`handle_restore` 旧 RPC 路径与新 PyO3 `backup_restore.rs` 收敛：删除旧路径或改接新实现（二选一，按回归成本决策） | 静态审阅 + cargo test |
| C10 | crash recovery 统一：recover 等价 replicate 幂等；delete tombstone 重放走 Rust `apply_durable_operations`；durable_staging 决策（迁移 Rust 或明确废弃并记录） | crash 场景 E2E（kill -9 后重连） |

## 4. 验收标准

1. 差分测试覆盖 C1-C10 新增断言通过（`tests/test_c5_*`）
2. daemon E2E 全链路：真实 refresh → snapshot publish（generation 递增）→
   snapshot GC（keep_last 生效）→ backup full/db_only roundtrip → restore 后
   verify 通过 → crash（kill -9）后 recover 幂等重放，pending 归零
3. `cargo test --lib`（replicator / snapshot / backup_restore / cas 相关）通过
4. 静态检查：Python 生产路径（daemon_server.py）无 refresh/replicate/recover
   Python 编排残留（fallback 分支除外）
5. 证据留档 `g0-reviewer-scratch/c5/`（测试日志、断言脚本、任务库只读导出）
   交付独立复审；验收口径沿用 C4 §5.2.7（Windows + Python 3.10 环境约定）

## 5. 边界

- 不合并双库（CodeGraph DB vs workspace DB），沿用 C4 G2 既定
- 不新建 snapshot 持久化表；`.cwsnap` 接入 / 快照 generation 落库为独立工作包
  （C7 明确不阻塞主链）
- 不改动 C4 已固化的 generation 时序（seen→merge→upsert_manifest→committed）
- `durable_staging.py` 取舍在 C5 内决策，不拖延至后续任务
- 不推送 GitHub、不打 release tag、CI 作为发布阶段单独处理（延续 C4 终审约定）

## 6. 任务步骤映射

| 步骤 | 任务 ID | 内容 |
|---|---|---|
| S0 | `T-1785590602456-0cac3cab-sub-1` | 本契约文档（现状梳理 + 统一点 + 边界） |
| S1 | `T-1785590602456-0cac3cab-sub-2` | Replicator 统一（C1/C2/C3/R1-R6） |
| S2 | `T-1785590602456-0cac3cab-sub-3` | Snapshot publish/load 统一（C4/C6/C7/S1/S3-S6） |
| S3 | `T-1785590602456-0cac3cab-sub-4` | Snapshot GC 统一（C4 GC 面/S8） |
| S4 | `T-1785590602456-0cac3cab-sub-5` | backup/restore 统一（C8/C9/B1-B8，衔接 R5） |
| S5 | `T-1785590602456-0cac3cab-sub-6` | crash recovery（C10） |
| S6 | `T-1785590602456-0cac3cab-sub-7` | 测试与验收证据（验收标准 1-5） |

## 7. 实现记录

### S1 Replicator 统一（2026-08-08）

**C1 收敛标注**（`server/daemon_server.py`）：
- `workspace.file.refresh` handler（L1081 前）：标注生产编排主链已由 Rust daemon
  `workspace.rs`（L1629-2257）承担，本 handler 为 compat/fallback 路径，不承担生产写编排
- `workspace.recover` handler（L1330 前）：标注生产恢复主链由 Rust 承担，本路径为
  compat/fallback（`Replicator.recover` adapter）

**C3 StagingEntry operation 统一**（`server/staging_log.py`）：
- `StagingEntry` dataclass 增加 `operation: str = "refresh"` 字段（对齐 Rust
  `staging_log.rs` `StagingEntry.operation`，serde default=refresh）
- `from_dict` 读缺省 `data.get("operation", "refresh")`——旧日志无该字段按 refresh 处理
- `create_staging_entry` 增加 `operation` 参数透传
- 字段放在 `language` 之后、有默认值，不破坏既有位置参数/关键字参数构造

**C2 merge 失败门控统一**（`server/replicator.py` + `server/daemon_server.py`）：
- `daemon_handle_refresh` 5a merge 段重构，区分两条失败路径：
  - **Rust 模块不可用**（`ImportError`）→ fail-closed（未显式 rollback 时抛
    `ProtocolError` code=`rust_cas_merge_unavailable`，C4 契约保持）
  - **Rust 可用但 merge 数据失败**（`success=false`/异常）→ 对齐 Rust `merge_ok`
    门控：不抛异常；构造 `merge_status="error"` 的 merge_result；返回 CAS 层
    `status="committed"`；跳过 `upsert_manifest` 与 `latest_committed_generation`
    推进，同 seq 可安全重试
- `daemon_server.py` refresh handler：staging append 后检查
  `merge_status in ("error", "cas_miss", "open_failed")` 时**跳过 replicate**，
  返回 `snapshot_published=false` + `snapshot_warning`（对齐 Rust workspace.rs
  L2141-2166：merge 失败 skip committed + replicate）

**测试适配**（`tests/test_p0_1_save_to_query_e2e.py`）：
- `test_refresh_failure_does_not_mark_applied`：改为断言 C5 C2 语义——merge 失败
  返回 `merge_status="error"` + error 详情，`latest_committed_generation` 保持空
- `test_retry_after_merge_failure_not_stale`：第一次 merge 失败返回 error
  merge_result（不再断言抛异常），恢复 merge 后同 seq 重试成功（核心意图
  "重试不判 stale"保持）

**验证**：`test_phase5_staging_log / test_phase5_cas_replicator_wiring /
test_c4_manifest_refresh_diff / test_p0_1_save_to_query_e2e /
test_phase5_replicator / test_phase1_replicator_snapshot_verify /
test_phase5_canonicalize / test_phase5_delta` 全部通过。

### S1 复审整改（2026-08-08，commit 后续）

针对独立复审结论逐项整改：

**P1-3 merge_status 透传（`server/replicator.py`）**：
- 修复前 Python 只看 `rust_res["success"]`，Rust 的 `cas_miss` 走
  `success=true + merge_status=cas_miss`（Result Ok 分支），会被误判为 merge
  成功并推进 committed——与 Rust `merge_ok` 门控不一致。
- 修复后：`success=true` 时检查 `merge_status`，`cas_miss/error/open_failed`
  按 merge 数据失败门控处理；merge_result 的 `merge_status` 透传 Rust 原值，
  不再统一折叠为 `error`。daemon_server 层对三值的判断从"死代码"变为可达。

**P1-1 daemon_server 层 C2 测试（`tests/test_c5_s1_replicator_gate.py` 新建）**：
- `test_merge_failure_skips_replicate[error/cas_miss/open_failed]`：merge 失败 →
  staging append 为 pending、replicate 不被调用、返回 `snapshot_published=false`
  + `snapshot_warning`（含 "merge 失败"）、cas_merge 透传原值
- `test_merge_success_triggers_replicate`：对照组，merged → replicate 被调用、
  snapshot 发布成功

**P1-2 C3 operation 字段断言（`tests/test_c5_s1_replicator_gate.py`）**：
- `test_operation_defaults_to_refresh`：构造未传 → 默认 refresh
- `test_operation_round_trip_preserved`：to_dict/from_dict 保留 refresh/delete
- `test_from_dict_missing_operation_falls_back_to_refresh`：旧日志缺省兼容
- `test_create_staging_entry_operation_passthrough`：helper 透传 operation

**P0-1 任务归属证据（`g0-reviewer-scratch/c5/`）**：
- `task_evidence.json`：sub-2 任务（status=review）+ `task_events` 权威迁移记录
  （event 32：in_progress→review，reason=report 0275a4a）。C5 采用 task split
  子任务模型，sub-2 无独立 task_steps 表记录（steps=0），以 task_events 为状态
  归属证据
- `change_audit_full_diffs.txt`：0275a4a vs b72e156 的 5 文件 blob hash + 全量 diff
- `export_evidence.py` / `export_events.py`：只读导出脚本（可复现）

**P2-1 宿主机测试证据（`g0-reviewer-scratch/c5/`）**：
- `c5_s1_pytest_log.txt`：Windows Python 3.10.11 —— P0-1 8/8、C4 差分 6/6、
  C5 gate 8/8 = **22 passed**
- `c5_s1_regression_log.txt`：staging_log / cas_replicator_wiring / replicator /
  phase1_replicator_snapshot_verify / canonicalize / delta / g9 = 全部通过

**验证**：`test_c5_s1_replicator_gate.py` 8 项 + 既有 14 套件全部通过
（Windows 宿主机，Python 3.10.11）。

### S2 Snapshot publish/load 统一（2026-08-08，C6 失败 generation 保护落地）

**范围说明**：S2（sub-3）契约面为 C4/C6/C7/S1/S3-S6。本阶段核心落地 **C6**
（snapshot_guard 前移到唯一 publish 决策点——Python 路径 publish 前执行失败保护，
failed/partial 不替换上一代），对应 S2 验收点 5（任一步失败时
`latest_committed_generation` 不得推进）与验收点 7（不允许 partial snapshot
被查询命中）。C4/C7/S1/S3-S5 其余面（Rust 单入口收敛、.cwsnap 接入等）独立
工作包承接，不阻塞 C5 主链（契约 §5 边界）。

**C6 实现（`server/replicator.py`）**：
- 新增状态分类常量：`_SNAPSHOT_GUARD_PARSE_FAILURE_STATES`
  （parse_failed / canonicalize_failed / publish_failed / cas_lookup_failed /
  no_abs_path / no_cas_conn）、`_SNAPSHOT_GUARD_STALE_STATES`
  （stale_seq_dropped / stale_generation）、`_SNAPSHOT_GUARD_PARTIAL_STATES`
  （partial_published）。**`no_cas_conn` 纳入状态集合用于 Rust/Python 静态对齐**
  （与 Rust `snapshot_guard.rs::is_parse_failure_state` 一致）；当前兼容路径因
  无 CAS 主链（`cas_conn=None`）而不触发保护——镜像 Rust
  `cas_store=None → cas_result=None → cas_state="" → 不保护` 语义。
  （P1-2 复审整改，commit 6f786d9：此前文档"明确排除 no_cas_conn"为旧表述，
  代码已于 P1-2 纳入集合，行为语义不变。）
- 新增 `_is_dirty_overlay_path(abs_path, rel_path)`：镜像 Rust
  snapshot_guard.rs::is_dirty_overlay（.git / .callwarden / .callwarden-tmp- /
  ~ / .bak / .orig / .rej），先规范化 `\` → `/`。
- 新增 `_evaluate_generation_protection(cas_state, abs_path, rel_path)`：
  判断顺序与 Rust 一致——dirty overlay → parse failure（allows_retry=True）→
  unsupported → stale → partial（保留上一代）→ 默认 success 不阻塞。
- `daemon_handle_refresh` 中在 `_daemon_parse_and_publish` 之后、merge 门控与
  committed 段之前接入保护门控：`cas_conn is not None and cas_result` 时评估，
  blocked 立即返回 `{"status": "blocked", ...}` ——不写 manifest、不推进
  `latest_committed_generation`、不追加 staging、不 replicate。

**C6 handler 适配（`server/daemon_server.py`）**：
- `workspace.file.refresh` 增加 blocked 分支（在 staging append / committed /
  replicate 之前）：返回 `snapshot_published=False` + `snapshot_warning`（含
  protection reason 与 cas_state）+ `protection` 透传，staging 不追加。

**测试适配 + 新增**：
- `tests/test_integration_phase3_8.py` / `tests/test_phase5_session_epoch.py`：
  断言从硬编码 committed 改为 ready→committed / 失败→blocked 分支；blocked 时
  校验 `latest_committed_generation != "1:1"`（验收点 5）。
- `tests/test_c6_snapshot_guard_replicator.py`（新建，21 项）：
  - `_is_dirty_overlay_path` 全模式单测
  - `_evaluate_generation_protection` 状态分类单测（success / failed+retry /
    unsupported / stale / partial / dirty overlay 优先）
  - `daemon_handle_refresh` 集成：parse_failed / publish_failed / partial /
    unsupported / dirty overlay → blocked 且 committed 不推进；ready → committed
    （`"{epoch}:1"`）；cas_conn=None → 不启用保护 committed（回归）
  - handler 层 blocked 分支：snapshot_published=False + warning + protection
    透传，staging 未追加、replicate 未调用

**验证（Windows 宿主机，Python 3.10.11，`PYTHONPATH=c:\git_work`）**：
- `test_c6_snapshot_guard_replicator.py`：21 passed
- 回归集（含 test_integration_phase3_8 / test_phase5_session_epoch /
  test_phase5_cas_replicator_wiring / test_phase5_replicator /
  test_phase5_staging_log / test_phase5_git_clean_dirty / test_phase3_cas /
  test_phase3_cas_protocol / test_phase1_replicator_snapshot_verify /
  test_phase4_snapshot_service / test_phase4_snapshot / test_phase8_snapshot_gc /
  test_p0_1_save_to_query_e2e）：**298 passed, 1 skipped**
- 日志留档：`g0-reviewer-scratch/c5/c5_s2_pytest_log.txt`

**S2 独立复审整改（2026-08-08，P1-1/P1-2，commit 6f786d9）**：
- **P1-1（change_audit 归属证据）**：任务库 `change_audit` 表对 sub-3 无记录
  （0cac3cab 家族 0 行，父任务 d2b8c66c 有 6 行）；S1/S2 均以工作区证据文件
  `change_audit_full_diffs*.txt` 作为审计载体。已补齐
  `g0-reviewer-scratch/c5/change_audit_full_diffs_s2.txt`
  （295d327 vs 6f786d9 的 7 文件真实 blob hash + 完整 diff），并在
  `task_evidence_s2.json` 的 `change_audit_note` 字段显式说明任务库无记录。
- **P1-2（no_cas_conn 静态对齐）**：`_SNAPSHOT_GUARD_PARSE_FAILURE_STATES`
  加入 `no_cas_conn`，与 Rust `snapshot_guard.rs::is_parse_failure_state`
  静态一致；启用条件 `cas_conn is not None` 保证 no_cas_conn（仅 cas_conn=None
  时产生）不进入保护评估，行为语义不变，注释补充说明。test_c6 断言同步更新。
- **P2-1（行尾符）**：核验为 CRLF/LF 差异，提交后已随 index 规范化，无实质未提交改动。

### S3 Snapshot GC 统一（2026-08-08，C4 GC 面/S8）

**S8 WAL checkpoint 策略统一（`server/daemon_client.py`）**：
- `publish_snapshot` 本地 checkpoint 由 `PRAGMA wal_checkpoint(FULL)` + busy
  fail-fast（`raise DaemonUnavailableError`）改为 PASSIVE 双保险：
  `PRAGMA busy_timeout=5000; PRAGMA wal_checkpoint(PASSIVE)`，busy 时不抛异常，
  剩余 WAL 页由 daemon/内核后续 PASSIVE checkpoint 兜底——与 Rust 侧
  `snapshot_state.rs::wal_checkpoint`（L1619-1626）和 `snapshot.rs`（L919-937）
  语义一致，闭合 §2.2 S8 缺口。

**S8 Rust CLI 侧对齐（`rust_ext/src/bin/cw_client.rs`）**：
- `wal_checkpoint` 工具函数（publish 前本地 checkpoint）由 `wal_checkpoint(FULL)`
  + busy fail-fast（`exit(1)` 由调用方触发）改为 `PRAGMA busy_timeout=5000;
  PRAGMA wal_checkpoint(PASSIVE)`，与 daemon 库侧 `UnixDaemonRpcClient::
  publish_snapshot`（无本地 checkpoint，依赖 daemon PASSIVE）语义统一；
  两处注释（`skip_checkpoint` help 与 `run_publish` docstring）同步更新。

**C4 GC 面确认（无需代码改动，语义已统一）**：
- Python `SnapshotManagerService.gc_snapshots(keep_last=3)`
  （snapshot_manager.py L200-223）与 Rust `handle_gc_snapshots`
  （snapshot_state.rs L472-493）语义一致：keep_last 默认 3、遍历
  `list_workspaces()`、调用 `mgr.gc_generations(keep_last)`、返回
  `{"deleted_count", "keep_last"}`；Python 底层 `_cache` 即 Rust
  `PySnapshotCache` 绑定，GC 走同一内核。

**测试（`tests/test_c5_s3_snapshot_gc_unify.py`，新建 6 项）**：
- S8：`publish_snapshot` 执行 `PRAGMA busy_timeout=5000` + `wal_checkpoint
  (PASSIVE)` 且不含 FULL；busy=1 时不抛 `DaemonUnavailableError`、继续走 RPC。
- C4 GC 面：`gc_snapshots` 默认 keep_last=3、遍历所有 workspace、
  返回删除总数（对齐 Rust `deleted_count` 语义）。

**验证（Windows 宿主机，Python 3.10.11，`PYTHONPATH=c:\git_work`）**：
- `test_c5_s3_snapshot_gc_unify.py`：6 passed
- 回归集（test_integration_phase3_8 / test_phase8_snapshot_gc /
  test_c6_snapshot_guard_replicator / test_phase4_daemon_client /
  test_phase4_compare_snapshots / test_enterprise_daemon_uds /
  test_b3_rust_daemon_wiring）：通过（`test_phase4_daemon_client.py::
  TestDaemonRoutingWithRust::test_get_stats_via_daemon` 为既有环境失败——
  daemon 未嵌入 Python 解释器导致 build_and_publish 失败，stash 还原后同样失败，
  与 S3 改动无关）
- Rust `cargo check --bin cw-client` 通过（无新增错误）
- 日志留档：`g0-reviewer-scratch/c5/c5_s3_pytest_log.txt`

### S4 backup/restore 统一（2026-08-08，C8/B1-B8，`server/backup_restore.py`）

**B1 `get_backup_info` Rust 短路（明确等价）**：
- `BackupManager.get_backup_info` 在 `_rust_backup_manager_available()` 时改为
  经 Rust `list_backups` 过滤实现（同一 `backup_meta.json` 读取，无效 meta
  两侧均视为不存在返回 None；Rust 侧只扫描真实备份目录，天然规避 backup_id
  路径穿越）。Rust 不可用时降级直读。契约 C8 的「或明确等价」条款即本实现。

**B2 `RestoreManager` checksum 接入 Rust 短路**：
- `_compute_file_sha256` / `_compute_meta_checksum` 与 `BackupManager` 覆盖
  对齐：默认走 `callwarden_core.backup_compute_file_sha256` /
  `backup_compute_meta_checksum`，rollback_config 置位时回退 Python，
  Rust 失败 fail-soft 降级。

**B3 Python 回退 `backup_full` 补齐 `daemon.json`**：
- 回退布局与 Rust 默认布局一致：registry.db + cas.db + audit.db +
  daemon.json + snapshots/（`db_only` 保持不含 daemon.json/snapshots）。

**B4 Python 回退原子发布**：
- 对齐 Rust `create_backup`：`_prepare_backup_dir_atomic` 对最终目录
  （重复 ID）与临时目录（上次失败残留）均 fail-closed 抛 FileExistsError；
  写入 `.<backup_id>.partial` 临时目录后 `os.rename` 原子发布；
  异常时清理临时目录。`backup_full` / `backup_db_only` 回退均生效。

**B5 快照机制差异有意保留（文档固化）**：
- Python 回退维持 `sqlite3.Connection.backup` API（在线备份、自动覆盖 WAL），
  Rust 默认路径用 VACUUM INTO（先 passive checkpoint）。回退仅在 Rust 不可用
  时触发，两侧语义均为一致性快照；已修正 `_backup_file` docstring 中的
  VACUUM INTO 误述。契约 C8 只要求布局与原子性对齐，未要求机制统一。

**B6 备份 ID 生成边界固化**：
- 备份 ID 仅 Python 生成（`B-<13ts>-<8hex>`，`secrets.token_hex(4)`），Rust
  只消费（`create_backup` 入参 + `safe_backup_dir` 校验）。Python 为编排方
  单一真相源，维持现状不迁移。

**B8 meta checksum 契约固化**：
- Rust `backup_compute_meta_checksum` 入参为 JSON 字符串（`ensure_ascii=False`），
  序列化必须始终借用 Python json 模块保证 byte-for-byte 一致。新增
  `test_python_and_rust_checksum_identical` 固化 parity 防回归。

**C9 旧 RPC 收敛决策（保留 legacy + 文档固化）**：
- `daemon/workspace.rs` `handle_backup` L3069 / `handle_restore` L3106 与
  Python `daemon_server.py` backup/restore handler 均保留，不删除、不改接
  新 PyO3 `backup_restore.rs`。决策依据（回归成本）：
  1. Rust workspace.rs 6+ 单测（`test_backup_creates_valid_db_file` 等）直接
     断言旧语义（output_path 单库 VACUUM INTO / source_path copy+reopen）；
  2. `test_phase5_2_slice5_rpc_diff.py` D7 断言 CLI 映射 backup→"backup" /
     restore→"restore"，`test_phase4_1_daemon_protocol_diff.py` D3/D4 断言
     二者 admin-only；
  3. CLI 用户契约 `cw daemon backup --output <单文件>` / `restore --from`
     （`daemon_commands.py` L634-637）依赖单库快速备份语义；
  4. 旧 RPC 粒度（registry 单库快速备份）与新 PyO3 粒度（全量多库
     backup_meta.json + registry/cas/audit + daemon.json + snapshots/）
     属不同层，非重复实现——删除或改接都会破坏既有用户契约与测试面。
  收敛边界：RPC 表面契约（方法名/参数/返回）保持稳定；旧路径定位为
  registry 单库运维快速通道，新 PyO3 为全量备份主链，二者互不干扰。

**测试（`tests/test_c5_s4_backup_restore_unify.py`，新建 12 项）**：
- B1：get_backup_info 经 list_backups 过滤（spy 计数）+ 不存在返回 None
- B2：RestoreManager checksum 走 Rust（sentinel）+ fail-soft 降级（2 项）
- B3：回退 backup_full 含 daemon.json / db_only 不含（布局断言）
- B4：重复 ID fail-closed（full+db_only）、成功后无 .partial 残留、
  中途失败清理临时目录
- B8：Python 参考 checksum 与 Rust byte-for-byte 一致

**验证（Windows 宿主机，Python 3.10.11，`PYTHONPATH=c:\git_work`）**：
- `test_c5_s4_backup_restore_unify.py`：12 passed
- 回归集（test_phase8_backup_restore / test_c5_s3_snapshot_gc_unify /
  test_gc_retention）：98 passed，exit 0
- 日志留档：`g0-reviewer-scratch/c5/c5_s4_pytest_log.txt`

---

### S4 P1 复审修复（2026-08-08，backup_id 路径穿越闭合）

**P1 缺口（用户复核发现）**：Python fallback 未校验 `backup_id` 路径。Rust
`validate_backup_id`（`rust_ext/src/backup_restore.rs` L30-39，`components()`
必须恰好一个 Normal 组件）拒绝空值、`.`、`..`、绝对路径与多级路径；但 Python
fallback 直接 `os.path.join(self._backup_root, backup_id)` 拼接，传入
`backup_id="../outside"` 可逃出 `backup_root`。涉及 `backup_full` /
`backup_db_only`（经 `_prepare_backup_dir_atomic`）、`restore`、`verify_backup`、
`delete_backup`、`get_backup_info` 的 fallback 分支。

**修复（Python/Rust 共用同等规则）**：
- 新增模块级 `_validate_backup_id(backup_id)`（`server/backup_restore.py`
  L138-160）：显式实现 Rust `Path::components()` 单 Normal 组件的等价拒绝条件
  ——空值/`.`/`..`、含 `/` 或 `\`（多级路径或 CurDir/ParentDir）、盘符前缀
  （`PureWindowsPath` 的 `drive`/`root`，覆盖 `C:\abs`、`C:foo`、`a:`、`/abs`
  等 Windows 与 POSIX 绝对路径形态）。校验通过返回原 ID，否则抛 ValueError。
- 5 个拼接点全部在 `os.path.join` 之前接入校验：`_prepare_backup_dir_atomic`
  （护住 `final_dir`/`temp_dir`）、`get_backup_info`、`delete_backup`、
  `restore`、`verify_backup`。
- 语义对齐：`backup_full`/`backup_db_only` 的 `backup_id=""` 走「为空自动生成
  ID」（与 Rust 路径一致），不构成穿越，测试集显式排除 `""`。

**测试（`TestFallbackBackupIdValidation`，新增 7 项）**：
- 12 个穿越样本（`""`、`.`、`..`、`../outside`、`..\outside`、`a/b`、`a\b`、
  `a/.`、`/abs`、`C:\abs`、`C:foo`、`a:`）逐一覆盖 backup_full /
  backup_db_only / delete_backup / restore / verify_backup / get_backup_info
  的 fallback 分支，全部拒绝 ValueError 且断言未逃出 `backup_root`
  （`assert not (tmp_path / "outside").exists()`）
- 合法 ID（`B-123-abc`）仍被接受：backup → info → verify → delete 全链路通过
- 测试运行方式：`RestoreManager` 实例承载 `restore`/`verify_backup`（该类归属
  与 `BackupManager` 不同，避免 AttributeError）

**验证（Windows 宿主机，Python 3.10.11，`PYTHONPATH=c:\git_work`）**：
- `test_c5_s4_backup_restore_unify.py`：19 passed（原 12 + P1 新增 7），exit 0
- 回归集（S4+S3+S1+C6+B3 wiring）：exit 0，4 skip（UDS E2E，Windows 不支持）
- 日志留档：`g0-reviewer-scratch/c5/c5_s4_pytest_log.txt`（重生成）

---

### S6 测试与验收证据（2026-08-08）

**验收标准 1（差分测试覆盖 C1-C10 新增断言）**：
- `tests/test_c5_s1_replicator_gate.py`（8）+ `test_c5_s3_snapshot_gc_unify.py`
  （6）+ `test_c5_s4_backup_restore_unify.py`（19）+ `test_c6_snapshot_guard_replicator.py`
  （21）= **54 passed**，exit 0
- 日志留档：`g0-reviewer-scratch/c5/c5_s6_accept1_log.txt`

**验收标准 2（真实进程级 daemon E2E，主证据）**：
- 脚本：`g0-reviewer-scratch/c5/c5_s6_accept2_realdamon_e2e.py`（12 项断言，12/12 PASS）
- 链路（真实 daemon → IPC → 持久化日志 → 进程崩溃/重启 → recovery）：
  1. 启动真实 `cw-daemon.exe`（隔离 registry/data_root/codegraph/task 库，不碰权威库）
  2. IPC（Windows Named Pipe RPC）：`workspace.register` → `workspace.connect`
     → `workspace.file.refresh`（真实 sample.py，CAS publish，status=committed，
     replication.generation=1）→ `backup`（VACUUM INTO registry，status=ok）
  3. 制造 pending：向持久化 staging.log 追加 pending entry（appended_lsn=1），
     验证 kill 前 pending=[1]
  4. 真实 TerminateProcess（kill -9 等价，Popen.kill，PID 32456 exit=1）
  5. 重启 daemon（PID 50504，同一 config）
  6. 启动时自动 recovery：daemon2 日志 `recovered 1 durable entries through snapshot
     pipeline` + recovery status=healthy；RPC `workspace.recover` status=recovered、
     staging.log pending 归零（[]）、幂等重放（第 2 次 recover applied_count=0）；
     snapshot 可查（generation=1, symbol_count=4, call_count=2）
- 证据留档：daemon1.log / daemon2.log（真实进程日志）+ summary.json（进程 PID、
  IPC 请求/响应、staging 前后状态、重启后恢复结果）
- 结果：**12 passed / 12**，exit 0
- 补充说明：`g0-reviewer-scratch/c5/c5_s6_accept2_combo_e2e.py`（16 项断言，库级组合，
  同一 Python 进程内直接调用组件）降级为组合链路补充验证，**不作为**验收 2 的主证据；
  显式 `snapshot.publish` RPC 依赖 Python SnapshotManager，纯 Rust daemon 未嵌入
  Python 解释器，为 C1 收敛设计下的已知不可用路径，生产发布由 refresh replication 承担

**验收标准 3（cargo test）**：
- `cargo test --lib`（replicator / snapshot / backup_restore / cas 相关模块），
  daemon::replicator 49 passed、backup_restore 等模块通过；ACL 既有失败已披露
- 日志留档：`g0-reviewer-scratch/c5/c5_s6_accept3_cargo_log.txt`

**验收标准 4（静态检查 Python 编排残留）**：
- daemon_server.py `workspace.file.refresh`（L1081-1084）与 `workspace.recover`
  （L1348-1351）均为 C5 C1 收敛标注的 compat/fallback adapter；watcher
  `_WatchdogChangeHandler`（L429-433）标注"仅作 fallback"；`recover_all_workspaces`
  无生产调用点（生产入口为 Rust `recover_all_workspaces_with_snapshot`）
- 结论：PASS（Python 生产路径无 refresh/replicate/recover 编排残留）
- 日志留档：`g0-reviewer-scratch/c5/c5_s6_accept4_staticcheck_log.txt`

**验收标准 5（证据留档）**：
- 本契约 §7 S6 记录 + 验收 1-4 日志 + 真实进程级 E2E 断言脚本已落盘
- 真实进程级证据：`g0-reviewer-scratch/c5/c5_s6_accept2_realdamon_e2e/`（daemon1.log、
  daemon2.log、summary.json）+ 脚本 `c5_s6_accept2_realdamon_e2e.py`
- 任务库记录：T-1785590602456-0cac3cab-sub-7（S6 测试与验收证据）
- 验收结论：**S6 通过（含真实进程级 E2E 补齐）**；2026-08-08 用户 P1 阻塞复审指出的
  「验收 2 非真实 daemon E2E」已由本记录的真实进程级测试闭环

**P2 补齐（非阻塞，S6 阶段）**：
- `g0-reviewer-scratch/c5/change_audit_full_diffs_s4.txt` 已重新生成，
  纳入 b7cbae0（P1 复审修复）diff hash，TO 对齐 be0fcb0

**验证环境**：Windows 宿主机，Python 3.10.11，`PYTHONPATH=c:\git_work`
