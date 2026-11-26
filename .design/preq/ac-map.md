# AC Map — 前置条件告知

> REQ → AC → 验收方式 → 证据位置。证据位置为空 = 该 AC 未通过（硬规则 5）。

| AC id | REQ | 验收方式（可执行） | 证据位置 |
|---|---|---|---|
| AC-01 | REQ-PREQ-04 | 分层判定单测：requires_existing × {provider open, applied, closed} × satisfied/unmet 三态正确 | `cargo test -p callwarden-core prerequisites` 输出 + 测试源码 |
| AC-02 | REQ-PREQ-04 | requires_artifact：provider applied 但 freshness=stale → unmet；fresh → satisfied | 同上 |
| AC-03 | REQ-PREQ-04 | requires_interface：无 active selection → unmet；有 → satisfied | 同上 |
| AC-04 | REQ-PREQ-01 | next_action 投影：有未满足硬依赖的卡返回 `prerequisites` 非空，元素含 provider_task_id/lifecycle_status/dep_type/unmet_reason | `cargo test` 投影断言测试 |
| AC-05 | REQ-PREQ-01 | next_action 投影：无依赖卡 `prerequisites=[]`（不缺字段） | 同上 |
| AC-06 | REQ-PREQ-01 | task.status 治理投影与 next_action 投影的 `prerequisites` 同构（同一引擎输出、字段一致） | 两投影对比测试 |
| AC-07 | REQ-PREQ-02 | claim 硬依赖未满足 → `E_PREREQUISITE_NOT_MET`，消息含每个 provider task_id 与缺口；无 lease 插入、任务状态不变 | `cargo test` claim 门禁测试 |
| AC-08 | REQ-PREQ-02 | claim 硬依赖满足（provider applied/closed + fresh）→ 正常放行 | 同上 |
| AC-09 | REQ-PREQ-02 | claim 仅信息性依赖（is_hard=0）→ 放行，响应附 warning | 同上 |
| AC-10 | REQ-PREQ-03 | capability 未启用 → claim 行为与存量一致（回归零破坏） | 回归测试（存量 claim 测试套全绿） |
| AC-11 | REQ-PREQ-03 | capability projection 暴露 `prerequisite_gate_v1`（name/enabled/schema_version） | capability 投影测试 |
| AC-12 | REQ-PREQ-06 | v2 amendment 含 prerequisite gate 章节：三层语义、判定规则、capability 门禁、错误码 | 文档 diff + 链接可达 |
| AC-13 | REQ-PREQ-05（第二批） | work_next 不派 hard_unmet 非空的卡；被过滤时推荐 provider 卡 | `cargo test` work_next 过滤测试（第二批放行后执行） |

## 快照范围（R3 冻结）

- **must**：REQ-PREQ-04、REQ-PREQ-01、REQ-PREQ-02、REQ-PREQ-03（AC-01 ~ AC-12）
- **指定 should**：REQ-PREQ-06（AC-12，随第一批交付，作为 capability 的设计依据）
- **deferred**：REQ-PREQ-05（AC-13，第二批，首批放行判据满足后注入）
