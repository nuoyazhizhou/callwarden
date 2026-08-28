# T-1787850432491-f42a2b8c / S-1787850432491-f432ca30

## Step

将 `TaskCollabStore` 与跨领域 `ActionIdentity` 共享类型从 `task_collab.rs` 抽取到
`rust_ext/src/daemon/task_collab_types.rs`，保留 `task_collab::TaskCollabStore` 公共导出和
现有字段语义；未改变 RPC、SQL、事务或错误处理逻辑。

## 变更

- 新增 `rust_ext/src/daemon/task_collab_types.rs`：共享 Store 状态、authoritative clock、dedup cache、sequence counter 与 action provenance 类型。
- `rust_ext/src/daemon/task_collab.rs`：通过带路径的 sibling module 声明并 re-export 类型；删除重复定义。

## 自测

```text
tokenslim run cargo check
result: PASS (exit 0)
```

仓库全量 `cargo fmt --check` 当前被既有无关文件格式差异阻断；本步骤未运行自动格式化，避免改变任务白名单之外的文件。

## 兼容性检查

- `dispatch.rs` 继续通过 `task_collab::TaskCollabStore` 持有 daemon store。
- `TaskCollabStore` 的 `conn`、`seq_counter`、`dedup_cache` 和 `clock` 仍是同一共享实例字段。
- `ActionIdentity` 的字段、可见性与 `parse_action_identity`/`record_action_identity` 调用契约未变。
