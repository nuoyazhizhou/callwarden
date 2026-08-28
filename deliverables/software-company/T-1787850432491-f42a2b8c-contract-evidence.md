# T-1787850432491-f42a2b8c / S-1787850432491-f432fa3c

## Step

将合同、身份策略与 governance projection handler 从 `task_collab.rs` 拆到
`rust_ext/src/daemon/task_collab_contract.rs`。

## 变更

- 保留 `TaskCollabStore::handle_p0l_reviewer_block_repair`、`handle_task_contract_set`、
  `handle_task_contract_bootstrap`、`handle_task_contract_revise`、
  `handle_task_contract_get`、`handle_task_governance_projection_get` 的公开方法名。
- 保留原有父模块 helper、SQL、事务顺序、错误码和 JSON 字段；新文件仅通过
  `use super::*` 访问共享 domain，不创建第二连接。
- `task_collab_contract.rs` 964 行，低于 2000 行；主文件减少到 17,489 行，后续步骤继续拆分。

## 自测

```text
tokenslim run cargo check
result: PASS (exit 0)

tokenslim run cargo test task_collab::tests::test_contract
result: PASS (4 passed, 0 failed)
```

仓库既有非本任务格式差异仍未自动修复。
