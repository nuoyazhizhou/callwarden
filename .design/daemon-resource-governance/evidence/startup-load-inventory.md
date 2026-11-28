# 启动加载路径盘点证据（T6 / REQ-RG-03 / AC-RG-03-3）

- 任务：T-1795832952572-3e03e5f4（step S-1795832952577-3e4c44e8，action=audit）
- 日期：2026-11-28
- 取证对象：`rust_ext/src/bin/cw_daemon.rs`（启动恢复入口）、`rust_ext/src/daemon/workspace.rs`（workspace.recover handler）、`rust_ext/src/daemon/parse_retry_log.rs`（重放读取）
- 行号基准：工作树当前版本（commit 64dc1f2 之后）

## 结论（先行）

**daemon 启动路径不存在"把已有索引全量载入内存"的加载点。** 启动恢复逐 workspace 走 RPC `workspace.recover`，仅当该 workspace 存在 pending staging entries 时才可能触发 snapshot 构建；0-pending 的 workspace 在 handler 内直接返回成功，不构建、不加载任何 snapshot。内存驻留语义为：snapshot 在首次查询（cache miss 冷加载）或发布（replicate）后常驻 `SnapshotCache`；generation history 不自动 GC（`gc_generations` 需显式调用）；空闲驱逐机制已由 T3 实现（`SnapshotCache::evict_idle`）但尚未接入主循环 resource tick，启动路径本身无任何驱逐/加载行为。

## ① 启动恢复入口：遍历注册 ws，逐个 dispatch workspace.recover

生产入口 `recover_all_workspaces_with_snapshot`：

- Unix 分支：`rust_ext/src/bin/cw_daemon.rs` L1170-1237
- Windows 分支（同一生产路径的副本，逐字一致）：`rust_ext/src/bin/cw_daemon.rs` L2847-2914
- 唯一生产调用点：`serve()` 内 L442-448（注释明确"durable recovery 必须在 publisher/cache/state 都构造完成后执行，统一走 workspace.recover"）

加载条件链（L1177-1195）：

1. `WorkspaceRegistry::open(registry_db_path)` 打开注册表（L1177）
2. `registry.list_workspaces(None)` 取**全部**已注册 workspace（L1184）——注意：只是读注册表元数据行，不含索引内容
3. 构造 `SnapshotDaemonState::with_registry_and_data_root(...).with_snapshot_publisher(...).with_codegraph_db_path_template(...)`（L1191-1194）
4. 对每个 workspace 取 `workspace_instance_id` + `owner_uid`，以 owner 身份构造内部 peer（`PeerCredential::new_unix(owner_uid, 0, 0)`），dispatch `"workspace.recover"`（L2889-2894 unix 副本同 L1177 起）
5. 成功则累加 `applied_count + retry_recovered_count`；失败打 WARN `durable recovery deferred`（L2907-2911）

关键点：**启动恢复不直接打开任何 CodeGraph SQLite、不 parse 任何源文件、不构建任何 snapshot**——它只逐 ws 转发 RPC，真正的加载决策全部在 handler（见②）。

## ② 0-pending 跳过路径：不构建 snapshot（实读证据）

`handle_workspace_recover`：`rust_ext/src/daemon/workspace.rs` L4060-4455。

pending 计数（L4105-4109）：

```rust
let pending_for_workspace = resources
    .staging_log
    .read_pending()
    .map(|entries| entries.iter().filter(|e| e.workspace_id == ws_id).count())
    .unwrap_or(0);
```

跳过路径（L4110-4115，逐字）：

```rust
let result = if pending_for_workspace == 0 {
    crate::daemon::replicator::ReplicationResult {
        success: true,
        workspace_id: ws_id.clone(),
        ..Default::default()
    }
} else if workspace_id_num != 0 && !db_path.is_empty() && self.snapshot_publisher.is_some()
```

即：**该 workspace 无 pending staging entries 时，handler 构造一个 `success: true` 的空 `ReplicationResult` 直接返回**（generation=0、applied_count=0），不进入 Replicator、不触碰 snapshot publisher、不构建 snapshot。这就是懒加载语义：启动不为"已有索引但无新数据"的 workspace 付任何构建代价。

有 pending 时（L4116-4125）才走 `Replicator::replicate`（staging → snapshot publish）；publisher/db 未就绪则走 deferred 分支（L4126-4137，`success: false`，pending 保留等下次恢复）。

## ③ parse_retry 重放：仅在完整链路成功（有序 publish）后才标记 applied

重放入口：`handle_workspace_recover` L4154-4395（R16-P1-1 完整状态机）。

重放条目读取：`replay_pending`（`rust_ext/src/daemon/parse_retry_log.rs` L404-409）→ `read_retryable`（同文件 L241-247）——按 staging/parse_retry 日志的既有顺序（append 序）返回 `retry_count < max_retry` 的 pending entries，不重排、不并发。

每条 entry 的重放状态机（workspace.rs L4177-4387，注释 L4146-4153 列明步骤）：

1. `_daemon_parse_and_publish` → CAS publish（L4178-4184）
2. `staging_log.append`（L4203-4220）
3. `cas_merge::merge_cas_to_codegraph`（L4258-4268）
4. `file_generation_committed`（generation CAS，L4296-4301）
5. `Replicator::replicate` → **snapshot publish**（L4320-4326）
6. 失败回滚 `file_generation_uncommit`（L4327-4337）

**只有完整链路成功（`snapshot_published == true`）才 `mark_applied`**（L4354-4362，逐字注释："只有完整链路成功才清除 durable retry。CAS 命中或 merge 成功都不足以证明 snapshot 已可查询"）。即：重放不允许跳步发布——merge 未做、committed 未持久化时 publish 失败会回滚并保留 durable retry，下次启动重新走全链路。解析仍失败/耗尽的 entry 分别计数（L4365-4386），不阻塞其他 entry。

## ④ 内存语义结论

- **启动无全量索引进内存点**：①②③证明启动只读注册表元数据 + 逐 ws 恢复 durable 日志，0-pending 即跳过；有 pending 也只发布增量 generation，不从零加载全量索引。
- **内存驱动在查询/发布后常驻**：snapshot 一经构建（查询 miss 的 `build_and_publish_blocking` 冷加载，或 replicate 发布），`GraphStore` 以 `Arc` 常驻 `SnapshotCache`（per-workspace `SnapshotManager` + ArcSwap），无自动卸载。
- **history 不自动 GC**：`SnapshotManager` 的 generation history 仅在显式调用 `gc_generations(keep_last)` 时清理（`rust_ext/src/snapshot.rs` L320）；启动路径不调用。
- **无空闲驱逐（启动路径）**：T3 已实现 `SnapshotCache::evict_idle`（snapshot.rs L492，AC-RG-02-1），但当前仅在测试中调用；C6 的 resource tick 接线属后续任务，启动路径无任何驱逐行为。
- build 侧（cw-build-graph）jsonl 输出已有覆盖，本轮不涉及（requirements-brief L51 口径）。

## ⑤ 启动日志现状对照 contracts.md C6

C6 要求（contracts.md L135）：「启动日志：逐 ws built/reused/skipped（C12，T6）」。

现状（cw_daemon.rs）：

| 位置 | 内容 | 缺口 |
|---|---|---|
| L449-452（main） | 汇总一行 `recovered {} durable entries through snapshot pipeline` | 无 built/reused/skipped 分类 |
| L2907-2911（unix 副本同） | 仅失败时 WARN `durable recovery deferred for ws={}` | 成功/跳过的 ws 无逐 ws 日志 |

**gap**：启动路径无逐 ws 日志，无三类计数。由本任务 step#1（test）补齐：逐 ws 报告 status ∈ {built, reused, skipped} + 汇总三类计数（判定口径见 step#1 证据）。

## 逐项代码位置索引（AC-RG-03-3）

| check_item | 代码位置 | 证据类型 |
|---|---|---|
| ① 遍历注册 ws 逐个 dispatch | cw_daemon.rs L1170-1237（unix）/ L2847-2914（windows 副本）；调用点 L442-448 | 实读 |
| ② pending==0 不构建 snapshot | workspace.rs L4105-4109（计数）、L4110-4115（跳过分支） | 实读（逐字引用） |
| ③ parse_retry 有序才 publish | parse_retry_log.rs L241-247、L404-409；workspace.rs L4177-4387（状态机）、L4354-362（仅成功 mark_applied） | 实读 |
| ④ 启动无全量加载点结论 | 综合①②③ + snapshot.rs L320/L492 | 推理（基于实读） |
| ⑤ C6 启动日志现状 | cw_daemon.rs L449-452、L2907-2911；contracts.md L135 | 实读 + 对照 |
