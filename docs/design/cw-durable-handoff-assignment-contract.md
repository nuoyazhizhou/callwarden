# cw Durable Handoff、Assignment 与角色工作队列契约 v1

> 状态：设计步骤草案，供任务 `T-1787823627134-d86d83b8` 的后续实现步骤使用。
> 本文不宣称相关 RPC、migration 或客户端已经完成；实现必须逐项以 daemon round-trip
> 和独立 Reviewer 证据为准。

## 1. 目标与边界

本契约解决三个角色之间“聊天里说已交接，但 daemon 不知道下一棒是谁”的问题：

```text
持久化 handoff
        ↓ 同一事务
产生明确的 queued assignment
        ↓
next_action 只读取队列投影
        ↓
同角色 claim / heartbeat / stale takeover
```

治理角色固定为 `executor`、`reviewer`、`adjudicator`。`planner`、`implementer`、
`tester`、`evidence` 只是 Executor 的 runtime mode；任何 recovery 都必须由同一治理角色
完成，禁止跨角色接管。

本任务只负责 durable handoff、assignment、heartbeat、超时接管和 client projection；不改变
Task Contract、Role Contract、identity policy、review verdict 的判定规则，也不引入 TLS。

## 2. 当前实现基线

实现前必须以当前代码为准，不能把冻结设计当作已实现事实：

1. `task.handoff` 已有 daemon handler，并在部分路径中追加 `handoff_structured` task event、
   自动追加 `fix_defect` 和 reopen；这不是独立的规范化 handoff ledger，也没有完整的
   assignment queue projection。
2. `task.remediation.create` 已有幂等和 provenance 校验，但它是显式 remediation 入口，不能
   由客户端承担 Reviewer BLOCKED 后的调度责任。
3. `task.claim` 已存在同角色 stale claim takeover，但当前 owner 判定主要从 task event 和
   `agent_registrations` 推导；必须与 assignment 当前行、holder kind、heartbeat 和 fencing
   统一。
4. `task_assignments` 是历史 P4 assignment 表，只有 `active/revoked` 生命周期，不能直接
   充当带 step、队列状态、超时和 request provenance 的新工作队列。
5. `task.next_action` 是只读 evaluator。它可以展示结果，但不能通过查询隐式创建 assignment、
   释放 claim 或修复 handoff。

## 3. 权威模型

### 3.1 Handoff Ledger

新增 append-only `task_handoff_events` 作为结构化 handoff 真相源；`task_events` 只追加
时间线通知并引用 `handoff_event_id`，不能反向成为 handoff authority。

最小字段：

```text
handoff_event_id       TEXT PRIMARY KEY
workspace_id           INTEGER NOT NULL
task_id                TEXT NOT NULL
step_id                TEXT NULL
source_role            TEXT NOT NULL
target_role            TEXT NOT NULL
outcome                TEXT NOT NULL
next_action            TEXT NOT NULL
reason_json            TEXT NOT NULL
source_verdict_id      TEXT NULL
source_report_id       TEXT NULL
evidence_path          TEXT NOT NULL
evidence_hash          TEXT NOT NULL
task_contract_revision TEXT NOT NULL
role_contract_revision TEXT NOT NULL
request_id             TEXT NOT NULL
authoritative_created_at REAL NOT NULL
```

约束：

- `task_id`、`source_role`、`target_role`、`outcome` 必须由 daemon 校验，不接受聊天文本推导。
- `step_id` 只有 task-level `reviewer_blocked` 可以为 NULL。
- `request_id` 在同一 workspace/task/method 范围内幂等；相同 hash 重放，冲突返回
  `E_REQUEST_ID_REUSE_MISMATCH`。
- source/target role 必须符合当前 Role Contract 的 `handoff_to` 和独立性要求。
- 历史 handoff 不 UPDATE、不 DELETE；不完整旧事件只读展示为 `UNVERIFIED`。

### 3.2 Work Queue Assignment

新增 `task_work_assignments` 作为当前派工投影，避免把历史 `task_assignments` 和 lease
混作同一语义：

```text
assignment_id          TEXT PRIMARY KEY
workspace_id           INTEGER NOT NULL
task_id                TEXT NOT NULL
step_id                TEXT NULL
role                   TEXT NOT NULL
source_handoff_id      TEXT NOT NULL
status                 TEXT NOT NULL
holder_kind            TEXT NULL
role_worker_id         TEXT NULL
role_instance_id       TEXT NULL
role_session_id        TEXT NULL
agent_id               TEXT NULL
agent_session_id       TEXT NULL
claimed_at             REAL NULL
heartbeat_at           REAL NULL
stale_at               REAL NULL
released_at            REAL NULL
completed_at           REAL NULL
fencing_counter        INTEGER NOT NULL DEFAULT 0
created_at              REAL NOT NULL
```

`status` 只允许：

```text
queued | claimed | stale | released | completed | revoked
```

关键索引和约束：

- 同一 `(workspace_id, task_id, step_id, role)` 最多一个非终态 assignment。
- `queued/claimed` 只能有一个当前 assignment；历史 assignment 保留。
- `holder_kind=role_worker` 时 worker/instance/session 三字段必须全有；
  `holder_kind=agent_identity` 时使用 legacy agent 字段，不能混用。
- assignment 的 role 必须来自当前 step binding，客户端传入的 role 只用于一致性校验。
- assignment 不替代 lease。assignment 表示“谁负责下一步”，lease 仍负责时效、fencing
  和受保护写入。

### 3.3 Assignment Event Ledger

所有状态变化追加 `task_assignment_events`：

```text
queued | claimed | heartbeat | stale_marked | takeover | released | completed | revoked
```

事件必须绑定 `assignment_id`、`task_id`、`step_id`、role、holder、fencing counter、
request_id 和 authoritative timestamp。raw lease token、worker credential 不得进入事件、
错误日志或 response cache。

## 4. 原子路由规则

`task.handoff` 在一个 daemon 事务中完成 handoff ledger、assignment projection、task status
和必要的 remediation step；不得先提交聊天 handoff，再异步补 assignment。

| outcome | target role | assignment | task lifecycle |
|---|---|---|---|
| `executor_ready_for_review` | reviewer | queued reviewer | `review` |
| `reviewer_pass` | adjudicator | queued adjudicator | 保持 `review` |
| `reviewer_blocked` | executor | queued `fix_defect` | 回到 `in_progress` |
| `adjudicator_returned` | executor | queued `fix_defect` | 回到 `in_progress` |
| `adjudicator_accepted` | complete | 不创建新 assignment | 进入 apply/close 门禁 |

`reviewer_blocked` 必须在同一事务中：

1. 写入结构化 handoff/verdict 关联；
2. 追加唯一 `fix_defect` step；
3. 写入 `remediation_of_step_id`、source verdict、finding IDs 和 source handoff；
4. 将 task 改为 `in_progress`；
5. 创建 Executor queued assignment。

重复 handoff request 只能重放第一次完整结果，不得产生第二个 remediation step 或 assignment。

## 5. Claim、Heartbeat 与同角色接管

### 5.1 Claim

`task.claim` 只能消费当前 task/step/role 的 queued assignment，或恢复同一 holder 的
`claimed` assignment。claim 成功必须在同一事务中：

```text
queued -> claimed
写入 holder + fencing_counter
追加 assignment claimed event
追加 task claimed event
```

不同角色不得 claim；不匹配的 step、Role Contract、workspace binding 或 identity policy
直接 fail closed。

### 5.2 Heartbeat

提供 daemon 权威的 `task.assignment.heartbeat`（CLI/MCP 仅转发）：

- 请求必须携带 task、assignment、holder/session 和当前 fencing counter。
- daemon 用 Authoritative_Clock 更新 heartbeat，不接受客户端时间。
- holder、role、task、step 或 fencing 不匹配时拒绝。
- heartbeat 只更新当前 assignment，不刷新已释放或已 stale 的 assignment。
- 心跳响应返回 `assignment_status`、`heartbeat_at`、`stale_after_seconds` 和下一动作，
  不返回任何 token/credential。

### 5.3 Stale takeover

同角色新 agent/worker 只有在以下条件同时满足时才能接管：

```text
当前 assignment = claimed
当前 authoritative time > heartbeat_at + stale_timeout
旧 holder 的 session/registration 已失效、撤销或无心跳
新 holder 的 role 与 assignment.role 完全一致
workspace binding、Task/Role Contract 仍有效
```

接管必须是单事务 CAS：旧 assignment 标记 `stale`，追加 takeover event，生成新的 fencing
counter，再由新 holder claim。旧 holder 的迟到 report/heartbeat 必须因 fencing 失败。

不得出现以下行为：

- Reviewer 接管 Executor；
- Adjudicator 清除 Reviewer 或 Executor claim；
- 只因查不到 registration 就清除仍可能工作的 holder；
- 只看 `role_workers.status=active` 而忽略 instance/session；
- `next_action` 查询过程中隐式回收或接管。

## 6. next_action 与客户端投影

`task.next_action` 只读当前 assignment/handoff/verdict 投影，至少返回：

```json
{
  "task_id": "T-…",
  "lifecycle_status": "review",
  "workflow_status": "review_pending",
  "current_role": "reviewer",
  "next_role": "reviewer",
  "next_action": "review_current_step",
  "assignment": {
    "assignment_id": "ASG-…",
    "status": "queued",
    "source_handoff_id": "H-…",
    "required_role": "reviewer",
    "stale_after_seconds": 900
  },
  "blocking_reasons": []
}
```

投影规则：

- queued assignment → `READY`，显示可领取的精确 task/step/role。
- fresh claimed assignment → `WAITING`，显示 holder kind、非秘密 session 摘要和 heartbeat。
- stale assignment → `READY`，显示“同角色可接管”，不显示旧 token。
- 缺 handoff、assignment、binding、contract 或 provenance → `BLOCKED/NONE`，明确缺口。
- `reviewer_pass` 只产生 `adjudication_pending`，不宣称 applied/closed。
- 聊天消息只展示已持久化 handoff event；没有 event 的自然语言 Handoff 不参与路由。

CLI 和 MCP 必须共享此投影。写操作仍经 daemon HTTP/Named Pipe；不得恢复 Python SQLite
fallback。MCP/CLI 的 opaque session handle 只在本机 session store 解析，绝不进入 argv、
日志、ledger 或错误回显。

## 7. 历史数据迁移与 fail-closed

迁移必须是 append-only、可重放和可审计：

1. 从结构完整且 provenance 可验证的既有 handoff/task event 生成 handoff/assignment 投影。
2. 能明确确定 source task、step、role、request 和 evidence 的记录才允许生成 current queue。
3. 缺 step、缺 binding、角色冲突、重复或无法验证的历史记录保持 `UNVERIFIED`，任务进入
   `governance_blocked`，不猜测当前 holder。
4. 旧 `task_assignments` 不删除；仅在可验证映射时追加 migration relation。
5. migration 使用独立 request_id 和 operation ledger；失败回滚，重试同 hash 重放。

## 8. 实施与发布门禁

本任务后续实现必须按以下边界拆分，不能把 schema、RPC、CLI/MCP 和全量测试塞进一个步骤：

1. schema/ledger 与 migration；
2. shared assignment/handoff domain，先于任何 handler 修改；
3. handoff→assignment 原子路由；
4. claim/heartbeat/stale takeover；
5. next_action 与 CLI/MCP 薄适配；
6. migration、并发、幂等、角色隔离和部署 round-trip gate。

每一阶段要求前一阶段已 `closed`，并提交 task-owned evidence。最终 Gate 必须覆盖：

- 同 request 重放与 request 参数冲突；
- 并发 claim 只有一个成功；
- fresh claim 不可接管；stale claim 可被同角色原子接管；
- 跨角色接管全部拒绝；
- heartbeat/revoke/expiry/fencing 负例；
- reviewer PASS、BLOCKED、adjudicator return 的完整状态投影；
- task-level BLOCKED 自动生成唯一 fix_defect；
- daemon 重启后 queue/handoff/lease 状态可恢复；
- 无 raw token/credential 泄露；
- CLI/MCP 只调用 HTTP daemon；
- `tokenslim run` 形式的全量测试、实际部署实例和真实 round-trip 指纹一致。

该契约不授权 apply/close，也不改变历史 verdict/evidence。只有独立 Reviewer PASS 且
Adjudicator 通过最终门禁后，才允许进入既有 apply/close 生命周期。
