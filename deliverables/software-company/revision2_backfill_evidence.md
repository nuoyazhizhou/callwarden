# Revision-2 机械合同回填证据（P0-G 解锁批次）

- **执行角色（治理分离 F2）**：独立 Reviewer `reviewer-wb-186loop` 逐卡 acquire reviewer-role lease；
  Adjudicator `adjudicator-wb-186loop` 执行 `task.contract_revise`。`validate_reviewer_lease_for_adjudication`
  强制 reviewer holder ≠ adjudicator（agent/instance/session 全异）。
- **范围**：Epic `T-1787293451688-c14b1e44` 下 180 张机械推导 rev1 合同
  （`created_by=adjudicator-workbuddy-v1`），全部 `workspace_id=1`、均有 workspace binding。
- **缺陷（rev1，100%）**：`acceptance_clauses`/`allowed_edit_scope`/`interfaces`/`risks`/`rollback`
  被存为「list 包裹字符串化 JSON 数组」（`['["a","b"]']`）；`dependencies=[]`。
- **修复（rev2，append-only）**：`task.contract_revise` 追加 revision=2，要求 envelope 为**结构化真数组**且
  `dependencies` 非空。本批次将 5 个数组字段反序列化为真数组，dependencies 填为 Epic 父任务
  `T-1787293451688-c14b1e44`（真实、统一的最小依赖；如需真实卡间依赖可后续 refine）。
- **逐卡 provenance**：每张 rev2 envelope 含 `source_provenance` 字段，声明反序列化与依赖填充方法与本证据文档。
- **审计约束 #3 遵守**：append-only（不原地 UPDATE/DELETE），逐卡 source provenance，独立 reviewer 门禁，不自动伪造。
- **清单**：`revision2_mechanical_inventory.json`（180 候选）+ `revision2_prepared_envelopes.json`（180 修正 envelope）。
- **回滚**：append-only，rev2 不可删；如需回退可经后续 revise 追加 rev3 纠正，不破坏 rev1/rev2 历史。
- **批次执行时间**：2026-08-24。
