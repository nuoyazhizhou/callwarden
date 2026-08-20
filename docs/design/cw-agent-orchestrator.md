# CallWarden 外部 Orchestrator 与角色执行循环协议 v1

> 状态：设计草案，未表示已实现
> 适用范围：CallWarden 自举项目中的外部机械调度器，以及通过 cw daemon
> 驱动 Executor、Reviewer、Adjudicator 会话的循环控制。
> 本协议不新增治理角色，不替代任务、合同、Verdict、Evidence 或 Lease 真相源。

## 1. 权威依赖与冲突优先级

本协议的冻结依赖按以下顺序解释：

1. `docs/design/requirements.md`；
2. `docs/design/multi-llm-contract-driven-collaboration-design.md`；
3. `docs/design/tasks.md`；
4. `docs/design/cw-role-handoff-task-loop.md`；
5. `docs/design/agent-task-contract-design.md` 与角色提示词文档。

上层文件冲突时，以前面的文件为准。本协议只规定外部循环如何消费 cw 的权威结果；它
不能放宽上述文件规定的 identity、workspace、lease、Evidence Gate、独立审核或
fail-closed 条件。

## 2. 术语与边界

### 2.1 三个治理角色

系统只有三个治理角色：

- `executor`：把用户语言写成需求/设计，或把既有需求/设计落实为代码、测试和证据。
  规划、实现、测试、证据是 Executor 的工作模式，不是额外治理角色。
- `reviewer`：独立只读核验执行者产物，只能产生 `PASS` 或 `BLOCKED` 的结构化结论。
  `BLOCKED` 直接返回 Executor；Reviewer 不修改实现、不改计划、不创建 remediation 步骤。
- `adjudicator`：在 Reviewer `PASS` 后作独立最终复审。接受时才可取得合法 reviewer
  lease 并执行 `apply/close`；不接受时只把具体缺口返回 Executor，不代替 Executor 制定整改计划。

现有 `planner`、`implementer`、`tester`、`evidence`、`independent_reviewer` 仅是
兼容期 `RuntimeRole`。外部 Orchestrator 不得把它们暴露成第四种治理角色，也不得据此
切换当前聊天窗口的角色。

### 2.2 外部 Orchestrator

External Orchestrator 是无治理权限的机械控制面。它可以：

- 读取 daemon 的 authority、任务状态、`task.next_action`、事件、lease 状态和已提交
  的结构化结果；
- 按 cw 返回的路由唤起或通知目标角色的新会话；
- 维护自己的短期轮询游标、退避计时器和 transport 重试计数；
- 在 stop condition 满足时停止循环并把原因报告给用户。

它不得：

- 直读或直写 SQLite，使用 `active_task_id` 或 active workspace 作为授权依据；
- claim、report、handoff、verdict、evidence、gate、lease、apply 或 close；
- 代 Reviewer 产生 finding，代 Adjudicator 裁决，或根据 finding 自行创建整改任务；
- 修改 allowed/excluded paths、验收、capture 隔离方案或合同；
- 伪造 identity、lease、fencing、handoff 或“已完成”文本；
- 建立第二个 Protected Mutation 串行化点。

角色会话是唯一的执行者：它必须自行注册 identity、取得适当 lease、执行受保护 mutation，
并通过 daemon 报告。Orchestrator 只负责把已冻结的路由传给目标会话。

### 2.3 cw 中间层

cw CLI、MCP client 和 daemon 组成唯一的任务控制面：

- daemon 是 task/step、contract binding、workspace authority、lease、事件、Evidence
  Gate 和 Verdict Ledger 的权威读写入口；
- CLI 是任务编排写操作的稳定入口；MCP/其他客户端不得为方便而绕过 daemon；
- `task.next_action` 是只读 evaluator，不创建 lease、不推进步骤、不写事件；
- 外部循环不可把自然语言回复当作状态确认，必须等待权威查询反映已提交事件。

## 3. Authority、identity 与 Lease

### 3.1 Workspace authority

每次循环实例固定一个 `workspace_instance_id` 与 authority endpoint。任何查询先由
daemon 验证请求 workspace authority 可用，再查询 task，最后验证已存在 task 的不可变
task→workspace binding、capture 链和 snapshot/hash。不可验证时返回稳定的
`E_WORKSPACE_AUTHORITY_UNAVAILABLE`、`E_WORKSPACE_AUTHORITY_MISMATCH` 或统一非泄露的
`E_TASK_NOT_FOUND_OR_UNAUTHORIZED`；不得用当前 IDE workspace、`active_task_id` 或
调用者本地数据库补齐。

不同 workspace 的 task、lease、contract、evidence 和事件不得交叉消费。Orchestrator
发现 authority fingerprint、daemon generation、workspace snapshot 或 task binding 变化时，
立即暂停当前循环，重新从 authority preflight 开始；不得继续使用旧查询结果驱动 mutation。

### 3.2 Identity 与会话

每个 role session 必须拥有可验证的 `agent_id`、`agent_instance_id`、`session_id`、
`model_id`、`provider` 和 runtime provenance。Orchestrator 不能把自己的身份冒充为角色
session，也不能替角色补齐缺失 identity。

`Executor → Reviewer` 与 `Reviewer PASS → Adjudicator` 必须是不同的 instance/session；
需要 `high_risk` 时遵循主设计的三会话约束。`READY/CLAIM` 若目标 Executor session
尚不存在，Orchestrator 必须创建/唤起一个新的 Executor session 后由该 session claim，
不能声称“复用尚不存在的 session”。

### 3.3 Lease

Orchestrator 只观察 lease，不取得、续租或释放 lease。角色会话负责用真实 identity
取得 lease，并在每个 protected mutation 前提供当前 token 与 fencing counter。

- Executor 使用 runtime `implementer` 等兼容值，但治理 role 是 `executor`；
- Reviewer 的治理 role 与 lease role 均为 `reviewer`；
- Adjudicator 的 acting role 是 `adjudicator`，最终 `apply/close` 取得的 lease role
  仍按现有兼容契约为 `reviewer`，二者必须在审计记录中分开；
- lease 过期、token 不匹配或 fencing 旧时，daemon fail closed 且不改变 task/step 数据。

Lease 只保证 daemon 在线期间的并发正确性；防篡改归属于 daemon Attestation 与追加式
Evidence Ledger。Orchestrator 不得把 lease 描述为防止离线直接改库。

## 4. 统一路由与 Handoff

### 4.1 权威状态机

Orchestrator 只能消费 `task.next_action` 的确定结果：

| decision/action | Orchestrator 动作 | 目标 | 是否创建 mutation |
|---|---|---|---|
| `READY/CLAIM` | 唤起目标 Executor session，传递 claim 指引 | Executor | 否 |
| `READY/REVIEW` | 唤起独立 Reviewer session | Reviewer | 否 |
| `READY/ADJUDICATE` | 唤起独立 Adjudicator session | Adjudicator | 否 |
| `READY/REVISE` | 把现有 finding/合同事实交给 Executor，由其决定修订 | Executor | 否 |
| `WAITING/WAIT` | 按退避规则等待后重新查询 | 无 | 否 |
| `BLOCKED/NONE` | 停止并报告 authority/事实缺口 | 无 | 否 |
| `COMPLETE/NONE` | 结束循环 | `complete` | 否 |

`task.next_action` 的系统 routing 不伪造 `from_role`、角色 outcome 或已发生 handoff。
无目标角色的状态使用 `next_role: null`、`next_session: null`；完成使用
`next_role: complete`、`next_session: null`。Orchestrator 不得把 `null` 猜成 Executor。

### 4.2 角色输出 envelope

角色面向用户、下游角色或已提交 `task.handoff` 的输出必须包含唯一的六字段 envelope：

```yaml
Handoff:
  from_role: executor|reviewer|adjudicator
  outcome: executor_ready_for_review|executor_blocked_to_user|reviewer_pass|reviewer_blocked|adjudicator_accepted|adjudicator_returned
  next_role: executor|reviewer|adjudicator|complete|user
  next_action: <single concrete action>
  reason: <finding, evidence, or immutable contract reference>
  independence_requirement: required|not_required|not_applicable
```

固定路由为：Executor 可审交付→Reviewer；Reviewer `PASS`→Adjudicator；Reviewer
`BLOCKED`→Executor；Adjudicator 接受→complete；Adjudicator 退回→Executor；仅缺用户
授权或必要事实时使用 Executor→user。`executor_blocked_to_user` 的独立性为
`not_applicable`。Orchestrator 只转发 envelope，不据 envelope 直接改变状态。

`task.report` 只报告当前步骤结果、证据和 identity，其响应 `handoff` 固定为 `null`；
完整交接只能来自已提交的 `task.handoff` event。成功的 `task.handoff` 必须包含并持久化
`handoff_event_id`、`request_id`、source/target role、step、outcome、Task Contract
三元组、Role Contract 三元组、identity 和 authoritative timestamp。Orchestrator 只有
在 event 已由 daemon 提交并可查询时，才可把角色输出视为已交接。

## 5. 单次循环协议

一次 loop 绑定一个 task、workspace authority、orchestrator run id 和取消信号，步骤如下：

1. `authority preflight`：ping daemon，核对 endpoint、workspace authority、daemon
   generation 和 schema/runtime fingerprint；失败则进入 recovery/stop，不访问 SQLite。
2. `read next action`：调用只读 `task.next_action`，保存 response hash、evaluated_at、
   task/role contract hash 和 routing；不缓存跨 authority 的结果。
3. `route`：仅按上表选择角色会话。需要新 instance/session 时，等待目标会话注册成功；
   未注册、角色不匹配或独立性无法证明时停止并报告。
4. `await authoritative result`：角色会话自行 claim/report/handoff/verdict/apply/close；
   Orchestrator 只轮询 task status、events、lease status、evidence/gate/verdict 查询。
5. `re-evaluate`：看到新 authoritative event 后丢弃旧 routing，重新执行步骤 1–2；不得
   根据上一轮文本直接跳过 evaluator。
6. `stop`：命中 §8 任一条件时结束本轮，输出结构化 stop reason、最后 authority fingerprint
   和最后一次 evaluator response hash。

### 5.1 并发规则

同一 `(authority, workspace_instance_id, task_id)` 同时只允许一个 Orchestrator loop
拥有调度标记；这只是外部重复调度抑制，不是 lease 或 ownership。即使外部发生重复
poll，daemon 仍是唯一 Protected Mutation 串行化点。角色 mutation 必须使用 request_id、
lease/fencing 和 daemon 事务；Orchestrator 不通过本地锁代替 daemon 门禁。

不同 task 可以并行轮询，但共享 authority 的请求必须有界并发、带 jitter，避免把 daemon
变成第二个调度器。发现 task 处于已有 lease 的 `WAITING/WAIT` 时，不能抢占或强制释放。

### 5.2 Polling 与退避

默认参数是可配置的：首次等待 1 秒，指数退避到 30 秒上限，加入 10%–25% jitter；每次
成功的权威事件或 routing 变化都把退避重置为 1 秒。daemon 自动唤起/连接重试遵循
Requirements 14.22 的有界默认 10 秒窗口；窗口耗尽后禁止治理写入。

轮询请求必须是只读且可取消。超过单次 RPC deadline 不代表 mutation 失败：先用同一
authority 查询事件和 task 状态，再决定是否重试。Orchestrator 不得用重复 claim/report
来“确认”一个超时请求。

### 5.3 Request-id 与重试

角色会话负责为每个 mutation 生成稳定 request id。传输断开、响应丢失或 daemon 重启后：

- 原请求参数未改变时，使用同一 request id 重试，依赖 daemon 幂等 replay；
- 参数、task、step、workspace、role、contract 或 payload 任一改变时，必须生成新 request
  id，并重新执行 authority/evidence/gate preflight；
- 同一 request id 不同参数必须返回稳定 `E_REQUEST_ID_REUSE_MISMATCH`，不得执行第二次；
- 首次请求结果未知时，先查询 request/event ledger；不以客户端“未收到响应”推断未提交；
- Orchestrator 不保存 raw lease token，不在日志中输出 token、隐藏推理或未提交 verdict。

## 6. 故障恢复

### 6.1 daemon/authority 故障

只允许按现有 daemon auto-start + bounded retry 机制恢复。恢复前保留本轮 run id，但丢弃
未重新验证的 routing、lease、snapshot 和 contract projection。恢复后必须重新 ping、重新
读取 `task.next_action`，不能直接续跑旧 action。

Governance_Write（claim/report/handoff/verdict/evidence/gate/lease/apply/close）在 daemon
不可用且有界唤起失败时 fail closed；不使用本地 SQLite fallback。只读查询也必须回到同一
authority 才能继续 loop，避免任务树在不同 worktree/thread 显示分裂。

### 6.2 角色会话故障

角色进程退出、session 断连或 lease 失效时，Orchestrator 先查询 daemon 是否已有提交的
report/handoff/verdict：

- 已提交：按新的 `task.next_action` 继续，不能重复执行旧步骤；
- 未提交且 lease 仍有效：不得替角色继续写，等待该 session 恢复或由 Executor/用户决定；
- lease 已过期/被 fencing：停止该 session 的 mutation，等待合法新 lease；
- Reviewer/Adjudicator 消失：不得由 Executor 自评，也不得由 Orchestrator 代审。

### 6.3 重启与 workspace 变化

daemon 重启、runtime hash/schema fingerprint 改变、workspace snapshot 改变、authority
generation 变化或 contract revision 变化后，旧的未提交 role view 和 routing 全部作废。
已提交的 append-only event 不回写；有效性由新 authority 按绑定 revision/hash 重新评估。

## 7. 循环安全不变量

实现必须能证明以下不变量；无法证明即停止而不是猜测：

1. 每一轮最多产生一个目标角色调度决定，且该决定来自同一 authority 的一次完整
   `task.next_action` evaluation。
2. Orchestrator 从不拥有治理 role，不持有 lease，不写 SQLite，不产生 Verdict 或 Evidence。
3. 所有 protected mutation 只有 daemon 单一串行化点能提交，所有写入带真实 identity、
   request id、lease/fencing（适用时）和 authoritative clock。
4. Reviewer `BLOCKED` 不会修改历史 verdict/evidence/failed step；整改由 Executor 自行规划，
   并在原计划允许时建立 parent-linked remediation。
5. Reviewer `PASS` 不会自动变成完成；必须交给独立 Adjudicator，且只有 Adjudicator 接受后
   才能 apply/close。
6. 任意 authority、contract、identity、lease、independence、evidence 或 freshness 不可用
   时，结果只能是明确 `WAITING`/`BLOCKED`/`user` stop，不得默认路由到 Executor。

## 8. Stop conditions

Orchestrator 必须停止并保留可审计 reason 的情况：

- `COMPLETE/NONE`：任务已完成，正常终止；
- `BLOCKED/NONE`：workspace/authority、身份、合同、schema 或其他必要事实不可验证；
- `next_role: user`：缺用户授权、外部事实或明确的人工决策；
- daemon auto-start/重试超过有界窗口；
- workspace、daemon generation、runtime/schema fingerprint、contract revision 或 snapshot
  在一次角色执行期间变化；
- lease 过期、fencing 冲突、request-id 参数冲突或 independent-session 证明失败；
- 达到用户配置的最大轮数、最大 wall-clock、最大连续 transport/role failure，或收到取消信号；
- 发现一个 task 同时存在互斥 current step、多个有效 contract binding、多个 authority 或
  unresolved failed step 无唯一 remediation 路径；
- 角色输出缺少六字段 Handoff、`task.report` 携带被禁止的 handoff 字段，或文本声称完成但
  daemon 没有对应已提交 event。

停止输出至少包含：`run_id`、task/workspace/authority 标识、最后一次 `task.next_action`
的 decision/action、稳定错误码或 stop reason、authority generation、evaluated_at、
最后 event/request id（如有）、是否可安全重试，以及下一合法角色或 `user/none`。禁止把
停止原因改写成成功或自动创建未授权的整改 scope。

## 9. 验收边界

后续实现任务至少需要证明：

- 外部 Orchestrator 只能读取 authority/task.next_action，并且不能通过任何入口 claim、
  report、handoff、verdict、lease、apply/close 或直连 SQLite；
- unclaimed step 会先唤起真实 Executor session，再由该 session claim；不会出现“尚未 claim
  却已 ready for review”的响应；
- `CLAIM`、`REVIEW`、`ADJUDICATE`、`REVISE`、`WAIT`、`BLOCKED`、`COMPLETE` 的 routing
  与 `next_role/next_session` 组合稳定，系统状态不伪造 `from_role`；
- Executor→Reviewer、Reviewer PASS→Adjudicator 使用不同 instance/session，Reviewer
  BLOCKED 回到 Executor，Adjudicator return 不创建新治理角色；
- `task.report` 响应始终 `handoff: null`，成功 handoff 必须能以 event id、outcome 和
  request id 在 daemon 中查询，缺 event 时不能声称交接完成；
- 相同 request id 的 replay 不重复写入，不同参数稳定冲突；响应丢失、daemon 重启、lease
  过期、fencing 变化和 authority 变化均 fail closed 或按新 evaluator 结果恢复；
- polling 有界、可取消、带 jitter；单 task 不会被两个 loop 同时调度，daemon 仍保持唯一
  Protected Mutation 串行化点；
- `BLOCKED` 历史保持不可变，只有 Executor 能提交修订计划/代码，Reviewer 和 Adjudicator
  不会创建 remediation 或修改 scope；
- runtime/schema/authority 不一致、task_not_found 跨 workspace、缺 identity 或 lease 时，
  任务状态、Evidence、Gate、Verdict 和历史事件均无额外写入。

