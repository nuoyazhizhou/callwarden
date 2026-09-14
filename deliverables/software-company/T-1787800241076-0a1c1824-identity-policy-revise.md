# A″ T-1787800241076-0a1c1824 identity_policy revise 证据

- 任务：A″ PyO3 数据库 / daemon transport 调用面收敛（Python HTTP thin-client 化）
- 治理动作：`task.contract_revise` 追加 revision 2，声明 `identity_policy=role_worker_v1`
- 依据：
  - 任务描述明确要求"由独立 Reviewer/Adjudicator 通过 append-only `task.contract_bootstrap` 追加并验证 `identity_policy=role_worker_v1` revision"；
  - revision 1 为 `task.create` generic projection，缺少可解析 identity policy，claim fail-closed（禁止隐式降级）；
  - bootstrap 通道不可用（revision 1 已存在，`no_governance_projection` fail-closed），故走 `task.contract_revise` hash-linked 升级。
- 授权：
  - Adjudicator Role Worker：`cw-adjudicator-p0j-v1`（role_worker.rotate 一次性 credential，instance `inst-cw-adjudicator-p0j-v1-aprime-20260908`）；
  - Reviewer proof（server-side）：reviewer `reviewer-wb-adjrp10-02` 持有 A″ active reviewer lease（reviewer_lease_id + fencing_counter，无 raw token 外传）。
- 不变式：
  - `contract_id` 不变；`supersedes_contract_hash` = revision 1 hash（`sha256:245996d05c02fb71caf9c23fccd66adf638f6d1f1b8543949f27ea0babfa6e77`）；
  - revision 2 仅追加，不修改历史 revision；operation ledger 持久幂等。
- 影响面：仅 A″ 自身的 Task Contract 治理投影；不修改 production Rust/Python、db/schema、task governance 代码。
