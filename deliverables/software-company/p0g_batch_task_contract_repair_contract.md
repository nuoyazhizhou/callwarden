# P0-G：批量任务合同修复、lease 恢复与原子治理建卡

**父任务：** `T-1787293451688-c14b1e44`（A′ 恢复父任务）  
**任务类型：** governance / control-plane repair  
**创建角色：** executor / planner  
**实施角色：** executor  
**独立复审：** reviewer  
**最终收尾：** adjudicator  
**目标工作区：** `workspace_id=1`、`workspace_instance_id=ws-1`

> **问题陈述。**A′ 批量预建任务在后续 `task.contract_bootstrap` 回填中写入了机械推导的 Task Contract revision 1。审计已证明这些合同包含 JSON list 再字符串化、通用风险/rollback、空依赖、缺失真实 port/Gate 语义，并遗留大量 active reviewer lease。既有 bootstrap 只能为“完全无投影”的任务创建 revision 1，不能覆盖或删除已落库历史。本任务必须以 append-only 方法恢复治理语义，并使未来 `task.create` 原子写入完整任务治理投影。

## 1. Scope

本任务仅涵盖下列四项能力：

1. 新增 daemon-native 受保护 mutation `task.contract_revise`：对已有 Task Contract 追加 revision `n+1`，保留 revision 1，不允许原地 UPDATE、DELETE 或重跑 bootstrap。
2. 令 revision-2 输入严格结构化：必须有完整的 objective、profile、allowed_edit_scope、interfaces、acceptance_clauses、risks、rollback、dependencies、handoff、source provenance、supersedes revision/hash；JSON array 以 JSON array 写入，不得再字符串化。
3. 强化治理 identity/lease：受保护治理写入要求 `agent_id`、`agent_instance_id`、`session_id`、`model_id`、`role` 均非空；Reviewer 与 Adjudicator 必须在三字段上全部不同；每任务同角色只允许一个可写 active lease，lease release 需按 token/fencing/idempotency 正式收口。
4. 扩展 `task.create`：在**同一 daemon transaction**写入 task、immutable workspace binding、steps、Task Contract revision 1、三角色 lineage/revision、Executor step binding 与 created event；任一步失败必须整体回滚，禁止再依赖事后 backfill。

## 2. 明确禁止范围

- 不删除、UPDATE、TRUNCATE 任何已存在的 `task_contract_revisions`、`role_contract_lineages`、`role_contract_revisions`、`task_step_role_contract_bindings`、task event 或 lease 历史。
- 不用 SQLite 直接修改合同、lease、任务 status 或 workspace binding。
- 不批量自动把 180 张 placeholder revision 1 伪造为可信 revision 2；每张修订必须具备任务来源、字段 provenance 和独立审查证据。
- 不 claim/report/handoff/apply/close 任一 A′ 迁移卡；不实现 MCP/CLI/SRV 业务迁移本身。
- 不扩大到 WorkBuddy Expert、Supervisor 或 agent UI 集成。

## 3. 精确实现定位

| 层 | 文件 | 目标函数/改动 |
|---|---|---|
| Task Contract revision domain | `rust_ext/src/daemon/task_loop/task_contract_revise.rs`（新增） | 严格解析、canonical JSON/hash、旧 revision continuity、append-only revision n+1、source provenance 验证 |
| Task loop wiring | `rust_ext/src/daemon/task_loop/mod.rs` | 注册新 domain module |
| Daemon handler/create atomicity | `rust_ext/src/daemon/task_collab.rs` | `handle_task_contract_revise`；完整 ActionIdentity 校验；lease 选择/释放收口；`handle_task_create` 在单 transaction 内写 full governance projection |
| Dispatch/protected mutation | `rust_ext/src/daemon/dispatch.rs` | `task.contract_revise` 与必要 lease repair/read action 的路由、capability registration 和 mutation protection |
| Python thin client | `server/daemon_client.py` | 仅新增 `task_contract_revise` 和受保护 lease-release 的 HTTP/RPC 转发；不得有 SQL fallback |
| CLI thin shell | `cli/main.py` / `cli/task_commands.py`（以实际现有 command module 为准） | 仅增加 JSON file 转发/结果格式化；不得解析或修订合同数据库 |
| Fixtures | `rust_ext/src/daemon/task_loop/*test*.rs`、`tests/` | revision/identity/lease/create atomicity 正负向 fixture |

## 4. 必须覆盖的负向测试矩阵

| 情形 | 必须结果 |
|---|---|
| revision input 缺 objective/provenance/old hash/任一必填 identity 字段 | 结构化 fail-closed；零行写入 |
| revision 不是 `old + 1`、old hash 不匹配、task 无 current contract | append-only continuity 拒绝；旧 revision 不变 |
| 试图 UPDATE/DELETE revision 1 或以 bootstrap 覆盖 | reject；保留历史审计 |
| Reviewer/Adjudicator agent、instance 或 session 任一相同/缺失 | `E_GOVERNANCE_*` 拒绝，零 mutation |
| token 不匹配、fencing stale、expired/released lease、同 role 多 active lease | 拒绝或正式 deterministic recovery；不得选择 `ORDER BY id ASC LIMIT 1` 误 lease |
| `task.create` 在 projection 第 k 步失败 | task/binding/steps/contracts/lineage/binding/events 全部回滚 |
| 未来新卡 role contracts 为 JSON 字符串嵌套或缺真实 task envelope | create fail-closed |
| daemon unavailable / HTTP manifest stale | Python client/CLI fail-closed；绝不 SQLite fallback |

## 5. 验收与证据

Executor 必须提交：

- 新 revision canonical payload/hash 与 supersedes 链的 Rust 单元测试输出；
- identity 三重分离、lease fencing/release 与 multi-active 拒绝矩阵；
- `task.create` 全投影成功与中途失败原子回滚矩阵；
- Python/CLI→HTTP round-trip 与 daemon unavailable/stale manifest 拒绝证据；
- migration audit：新建任务 `task_contract_revisions=1`、lineage=3、role revisions=3、executor binding=step_count，且无 nested JSON strings；
- 对现有 180 张 placeholder card 的**只读** repair manifest，逐卡列出 revision-2 所需 provenance、真实依赖与审阅状态，不得自动写入。

Reviewer 必须独立核验每一类 mutation 的 append-only 性、测试真实性和禁止直接 SQLite 的证据。Adjudicator 只有在 Reviewer PASS、真实独立 Reviewer lease、完整 identity、apply→close→`task.next_action=COMPLETE` 全部满足时，才能结束本任务。

## 6. 后续依赖

P0-G applied 后，才可创建以每张真实 manifest 为输入的“revision-2 修订批次”任务；先修 CLI-02/CLI-03 的未投影状态，再按 port Gate 和人工语义确认顺序处理 A′ 批量卡。SRV-019 与任何 A′ 业务迁移卡不得因 P0-G 的代码完成而自动放行。
