# cw-task-loop 用户文档

`cw-task-loop` 是 cw 任务循环的聊天入口：它查询 daemon 的只读派工器
`task.next_action`，把结果渲染成一张可复制的角色卡，并告诉你下一步该由 Planner、Executor、Reviewer 或 Adjudicator、
在哪个窗口、以什么身份去做。

- 只读：本入口从不写任务状态。
- 权威：一切以 `cw task next-action <task-id> --json` 的响应为准，不猜测、不补造。

## 1. 调用方式

```
$cw-task-loop <task-id>
```

例如 `$cw-task-loop T-1783350489327`。

前置条件：daemon 已启动（`cw daemon start`）。daemon 未启动时返回
`E_DAEMON_UNAVAILABLE`，先启动 daemon 再重试。

## 2. 角色卡示例

对 `READY/CLAIM`（executor 派工），渲染结果大致如下（字段逐字来自 daemon 响应；`READY/PLAN`
为协议保留派工，capability `planner_governance_v1` 声明前 daemon 不产生，示例不演示）：

```text
=== 系统派工（next-action）===
  决策: READY
  动作: CLAIM
  Role:      executor
  Task:      T-1783350489327  (contract T-…/rev 3/hash 8f3a…)
  Step:      S-…step-1
  Skill:     —（本步骤未绑定专用 skill；以 Role Contract 为准）
  Allowed:   ["src/cli/", "tests/…"]
  Forbidden: ["docs/design/…", "server/db/…"]
  Handoff:   executor → claim_current_step
             原因: 当前步骤可领取（唯一 verified Role Contract binding）
  领取指引: 请在【新会话】以真实 agent_id/session_id/model_id 调用 claim 路径；
            不得从聊天内容伪造 identity。
```

## 3. 窗口 / 会话独立性

| `next_session.role` | `must_be_new_session` | 要求 |
|---|---|---|
| `executor`（CLAIM） | `false` | 新会话或既有 Executor 会话均可领取 |
| `executor`（REVISE） | `false` | 同一或新 Executor 会话；逐字呈现 revision card 后交回（仅实现缺陷的 `fix_defect`） |
| `planner`（PLAN） | `false` | 协议保留：`planner_governance_v1` 声明前 daemon 不派工；届时 Planner 分析复杂度、拆分并冻结 Contract；不写生产代码 |
| `reviewer` | `true` | **必须新建独立 Reviewer 窗口/会话** |
| `adjudicator` | `true` | **必须新建独立 Adjudicator 窗口/会话**（独立于 Executor 与 Reviewer） |
| `null`（WAITING/BLOCKED） | — | 不派工，只解释原因 |
| `complete`（COMPLETE） | — | 已终态，只读 |

规则：每个窗口只保留一个任务角色；不得在同一窗口从 Executor 切换为
Reviewer/Adjudicator。建议聊天窗口标题包含 `task_id + role + session_id`。
角色提示词只是 fallback 文本，Task Envelope / Role Contract 始终优先。

## 4. 各决策行为指引

- **READY/CLAIM**：只有目标角色（executor）的**新会话**才能执行领取；需提供真实
  identity（agent_id / session_id / model_id / role）。skill 只给指引，不代领。
- **READY/PLAN**（协议保留：`planner_governance_v1` 声明前 daemon 不产生）：只有目标角色（planner）执行复杂度预检和计划；若存在多条安全路线，创建带 A/B/C 和自由文本入口的
  `decision_request`。
- **READY/REVIEW**：打开新的 Reviewer 窗口；只读核验，输出 `PASS` 或 `BLOCKED`。
- **READY/ADJUDICATE**：打开新的 Adjudicator 窗口；apply/close 前仍须逐项取得真实
  reviewer lease。
- **READY/REVISE**：skill 逐字呈现 revision card（来源 verdict、finding、proposed
  action、allowed/excluded paths、acceptance、capture_isolation），交回 Executor
  （仅实现缺陷的 `fix_defect` 整改；`owner_route=planner` 的 scope/Contract/架构缺陷按
  role-protocol §3 双轨：pre-cutover 由 Executor 复查后登记内部 capability/governance gap
  并交 Planner/治理维护路径，不硬修、不升级客户，post-cutover 交 Planner）；Executor 自行修订计划，skill 与
  Reviewer/Adjudicator 不得代劳。
- **WAITING/WAIT**：有 active 未过期 lease；等待持有角色释放，不写操作。
- **BLOCKED/NONE**：缺 Role Contract、hash 不匹配或 binding 不可验证等；只读解释
  `blocking_conditions`，禁止 claim。
- **COMPLETE/NONE**：任务已 closed；只读。

`waiting_for_decision` 与 `waiting_for_input` 是可解释的控制台等待状态，不是失败（协议保留值：capability
`decision_request_v1` 声明前 daemon 不 emit，客户端不得自行合成）。前者必须展示候选方案、默认建议和未选后果；
后者必须展示缺少的事实/授权。若只有唯一安全路径，agent 应继续推进，不把普通 bug 或可由同角色恢复的 stale claim 推给用户。

## 5. 每次 report / verdict 后重新查询

不要相信 Agent 的最后一句自然语言；每次 report、verdict 或 handoff 后重新调用
`$cw-task-loop <task-id>` 核对 daemon 的真实决策。

## 6. 常见问题

- **返回 `E_TASK_NOT_FOUND_OR_UNAUTHORIZED`**：任务 id 不存在或对该身份不可见；核对
  task_id 与 workspace。
- **返回 `E_WORKSPACE_AUTHORITY_UNAVAILABLE`**：任务存在但缺少可复核的 workspace
  binding/capture 链；属 fail-closed，不是本 skill 可修复。
- **返回 `E_DAEMON_UNAVAILABLE`**：daemon 未启动；`cw daemon start` 后重试。
- **决策与预期不符**：以 daemon 响应为准；如怀疑响应错误，请向 daemon 侧反馈，
  不要在聊天里自行改写决策。
- **发现相邻缺陷**：Reviewer/Adjudicator 必须记录 `adjacent_defect` 的根因、复现、影响和归属，不能因为不在当前 diff 而忽略。

完整状态、四角色边界和结构化交接见 `references/role-protocol.md`。
