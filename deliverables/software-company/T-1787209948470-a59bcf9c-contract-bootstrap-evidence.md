# S2 T-1787209948470-a59bcf9c Task Contract bootstrap 证据

- 任务：S2 compat 79 工具 M2 迁 Rust handler（重建·authority 绑定）
- 治理动作：`task.contract_set`（executor/reviewer/adjudicator 三角色 legacy Role Contract）
  + `task.contract_bootstrap` 追加 v1 Task Contract（`identity_policy=legacy_identity_v1`）
- 依据：
  - S2 重建任务"治理投影完全缺失但已绑定 authority"（无 contract revisions / role contract
    lineages / step bindings），命中 P0-C `task.contract_bootstrap` 目标场景；
  - 任务既有 binding：workspace_id=1，capture `wc-ws-1-4230255108`（workspace_instance_id=ws-1）；
  - S2 为 P0-L 之前遗留的普通实现卡（非 role_worker 授权任务），采用 `legacy_identity_v1`，
    不使用 A″/P0-L 的 role_worker_v1 强制路径。
- 授权：
  - Adjudicator identity：`adjudicator-wb-adjrp10-01`（active registered）；
  - Reviewer proof：reviewer `reviewer-wb-adjrp10-02` 持有 S2 active reviewer lease
    （raw token + fencing_counter，legacy 治理写路径）。
- 不变式：
  - `task.contract_set` 为 append-only role_contract 新 revision（is_current=1）；
  - `task.contract_bootstrap` 在同一事务写入 contract revision 1 + 三角色 lineage/revision
    + 审计事件，operation ledger 持久幂等；
  - 不修改 S2 的 binding / task 行，不删除历史数据。
- 影响面：仅 S2 自身的 Task Contract 治理投影；不修改 production Rust/Python、db/schema、
  task governance 代码。
