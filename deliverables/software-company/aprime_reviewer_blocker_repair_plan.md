# A′ 首批 Reviewer 阻断修复方案

**触发证据：** `review_outcome_T-1787293451688.md`  
**影响任务：** CLI-02 `T-1787321708568-d292ab3c`、CLI-03 `T-1787321708639-d6d362f4`、MCP-001 `T-1787321708699-da5d8224`  
**无影响任务：** CLI-01 `T-1787321020926-b7ed7500` 已有 Task Contract 且 `reviewer_pass` 已落库。

> 这不是实现质量 BLOCKED。三张任务都已通过执行质量检查和审计链核验；唯一阻断是它们没有 `task_contract_revisions` 的 revision-1 精确绑定，故 `verdict.submit` 依法拒绝 `E_TASK_CONTRACT_BINDING_INVALID`。

## 一、根因与正确修复边界

| 问题 | 根因 | 正确修复 | 明确禁止 |
|---|---|---|---|
| CLI-02/CLI-03/MCP-001 无法提交 reviewer verdict | 旧式预建卡写入了 legacy `role_contracts`，但未发布 Task Contract Envelope / governance projection | 用现有 daemon 的 **`task.contract_bootstrap`** 对每张“完全零治理投影”的任务一次性追加 revision-1、3 个 Role Contract lineage/revision 与所有 pending step 的 executor binding | 直接 `INSERT/UPDATE` SQLite；伪造 contract hash；覆盖 CLI-01 revision；批量自动补 185 张卡 |
| MCP `submit_verdict` 失败 | Python MCP adapter 把 legacy JSON string 原样透传；daemon native wire 要 JSON arrays、native phase/overall 和嵌套完整 identity | 单独新建代码整改任务，修复 adapter 的**语法适配**后纯转发至 daemon | 在 Python 复刻 daemon 的 lease、contract、verdict 业务判断；SQLite fallback |
| MCP `task_quality_findings` 报 `no such column: details` | Rust handler 查询了旧 `details` 列；真实 v21 authority schema 为 `evidence`、`source`，且 `step_id` 是 TEXT、`resolved_at` 可为空 | 单独新建代码整改任务，令 Rust handler 映射实际 authority schema，并保留 `details=evidence` 兼容返回 | 新建不必要 `details` 列或篡改历史 finding 数据 |

## 二、先恢复治理：三张任务的 bootstrap 顺序

这一步是**数据补投影**，不是源码修复。必须由独立 Reviewer 与 Adjudicator 以真实身份完成，不能由本报告作者或同一窗口兼任。

| 顺序 | 角色 | 写入目标 | 必须输入 | 成功后只读断言 |
|---:|---|---|---|---|
| 0 | Reviewer | 每张目标任务的 reviewer lease | 完整真实 identity、task_id、role=reviewer | 获得新 lease token + fencing；不记录 token 到文件 |
| 1 | Adjudicator | `task.contract_bootstrap` | 与 Reviewer 三重独立的完整 identity、有效 reviewer lease token/fencing、任务特异 envelope、证据路径/hash | `task_contract_revisions=1`、`role_contract_lineages=3`、`role_contract_revisions=3`、executor step binding 数=待执行 steps |
| 2 | Reviewer | `verdict.submit` | 新 lease、精确 contract/revision/hash、reviewer Role Contract revision/hash、原已核验的审查证据 | `reviewer_pass` 写入；不得再将原结论伪装为新执行证据 |
| 3 | Adjudicator | 后续 apply/close（仅门禁齐全时） | 独立复核、真实 reviewer lease、所有 evidence gate | `apply → close → task.next_action=COMPLETE` |

建议顺序是 **CLI-02 → CLI-03 → MCP-001**。每张 bootstrap/重新 verdict 完成后再处理下一张；不要把三张卡及 185 张 placeholder 合同放入一次批量写入。

### bootstrap envelope 的最低语义要求

每张 envelope 都必须带 `revision=1`、非空 `contract_id`、合法 `profile`，以及 JSON array 形式的 `interfaces`、`allowed_edit_scope`、`acceptance_clauses`、`risks`、`rollback`、`dependencies`。为防止再出现机械模板，`objective` 与 `source_provenance` 必须引用该卡自身的任务描述、实现证据和 reviewer 报告。

| 任务 | 建议 contract_id | profile | 必须反映的任务特异语义 |
|---|---|---|---|
| CLI-02 | `TC-T-1787321708568-d292ab3c` | `code_change` | `search` daemon-only 读取；Python 仅 HTTP thin client；禁止本地 SQLite fallback；CLI-01 control-plane Gate 已审查 |
| CLI-03 | `TC-T-1787321708639-d6d362f4` | `code_change` | `task show/list/status-tree` 只读诊断；Rust daemon authority；不得触碰 S1 的 `cli/main.py` 引用清理 |
| MCP-001 | `TC-T-1787321708699-da5d8224` | `code_change` | `get_role_view` Rust daemon 迁移；任务投影 Gate；公开 MCP 入参与返回稳定；Python 仅 HTTP 转发 |

## 三、需要独立创建的代码整改任务：P0-I

在 P0-G 完成正式 bootstrap/review/close，且 daemon 的 atomic `task.create` 可用后，以完整 Task Envelope + 3 份 Role Contract + steps **一次性创建**如下独立任务。不要把它塞进 CLI-02/03/MCP-001 的历史 scope，也不要让 Reviewer/Adjudicator 修改代码。

| 字段 | 固定内容 |
|---|---|
| 标题 | `P0-I：MCP verdict native-wire adapter 与 task quality authority schema compatibility` |
| 父任务 | `T-1787293451688-c14b1e44` |
| Executor allowed paths | `server/tools/tools_collab.py`、`rust_ext/src/daemon/task_collab.rs`、`tests/test_http_governance_error_cutover.py`、新增的受控 Rust/HTTP tests |
| Forbidden paths | `db/schema.py`、任意 SQLite 数据文件、`cli/main.py` 的 S1 清理范围、A′ 业务迁移源码、历史 Task Contract/lease 表 |
| 目标 1 | `submit_verdict` 将 legacy `PRE_VERDICT/POST_VERDICT` → `blind_first_pass/post_reveal_amendment`，legacy overall → `pass/block`；将 `clause_results`/`findings` JSON text 解析为 JSON arrays；透传嵌套 `identity` object，新增 `identity_agent_instance_id` 参数；解析失败 fail-closed，不写本地数据库。 |
| 目标 2 | `handle_task_quality_findings` 从实际列 `evidence`/`source` 读取；`step_id` 按 TEXT 返回；nullable `resolved_at` 返回 JSON null；为了兼容旧消费者可返回 `details` 并令其等于 `evidence`，但 SQL 不得引用不存在的 details 列。 |
| 必测正向 | MCP submit_verdict 传 JSON text arrays 后 daemon 收到 array；native enum 映射与完整 identity 透传；quality query 在 `evidence/source` schema 上返回 finding。 |
| 必测负向 | `clause_results` 或 `findings` 非法 JSON/非 array 必须得到稳定 fail-closed error；缺 agent_instance_id 必须 fail-closed；quality query 的 `resolved_at=NULL`、文字 step_id 不 panic。 |
| 验收 | Python 侧无 SQL fallback；Rust `cargo test` 定向通过；HTTP daemon round-trip；migration matrix 仅在独立 Reviewer PASS 后更新。 |

## 四、对当前 Reviewer 的下一棒提示

当前 Reviewer 已正确释放 CLI-02/03/MCP-001 的 lease，因而不应复用旧 token。下一个治理周期应由同一真实 Reviewer（或另一个满足独立性要求的 Reviewer）重新获取每张任务的 reviewer lease；独立 Adjudicator 先 bootstrap，随后 Reviewer 再提交原质量结论对应的 verdict。

```text
Handoff:
  from_role: reviewer
  outcome: reviewer_blocked
  next_role: adjudicator
  next_action: 以真实独立 identity 和新获取的 reviewer lease，逐张执行 CLI-02、CLI-03、MCP-001 的 task.contract_bootstrap；每张仅 revision-1，随后交回独立 Reviewer 重提 verdict。
  reason: 三张任务的实现/审计检查均通过，但 task_contract_revisions 零行导致 verdict.submit 的 E_TASK_CONTRACT_BINDING_INVALID；该写入只能走 daemon append-only bootstrap。
  independence_requirement: required
```

> **遗留 lease 边界：** 不释放、不篡改 `reviewer-wb-186loop` 的 187 个历史 lease。缺 raw token 的记录只能等待 TTL 或走 daemon 已有正式回收机制；本恢复流程只使用新签发、目标任务限定的 lease。
