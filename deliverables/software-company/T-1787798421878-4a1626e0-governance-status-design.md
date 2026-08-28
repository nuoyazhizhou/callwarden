# T-1787798421878-4a1626e0 任务状态可见性设计

## 目标

让任务调用方同时看到不可变的生命周期门禁和当前治理进度，不再把 `review` 误解为“等待 Reviewer”。

## 两层模型

`tasks.status` 保持现有生命周期语义，继续作为 `task.apply`/`task.close` 的权限门禁：

```text
open -> in_progress -> review -> applied -> closed
                              \-> reverted
```

在 daemon 只读响应中增加派生的治理投影，不新增会与旧状态机冲突的主表状态：

| workflow_status | 判定 | 当前责任角色 | 下一动作 |
|---|---|---|---|
| `queued` | `status=open` | executor | claim |
| `execution_in_progress` | `status=in_progress` 且无待整改 step | executor | continue |
| `remediation_in_progress` | `status=in_progress` 且存在 pending/in_progress `fix_defect` | executor | revise |
| `review_pending` | `status=review` 且无有效 verdict | reviewer | review |
| `adjudication_pending` | `status=review` 且最新有效 verdict 为 pass | adjudicator | adjudicate |
| `remediation_pending` | `status=review` 且最新有效 verdict 为 block | executor | revise |
| `apply_pending` | 已记录 `adjudicator_accepted` 但仍未 applied | adjudicator | apply |
| `applied_pending_close` | `status=applied` | adjudicator | close |
| `completed` | `status=closed` | complete | none |
| `reverted` | `status=reverted` | executor | reopen |

## 统一响应字段

`task.next_action`、`task.governance_projection.get`、`task.status` 和 `task.status_tree` 使用一致字段：

```json
{
  "status": "review",
  "lifecycle_status": "review",
  "workflow_status": "adjudication_pending",
  "current_role": "adjudicator",
  "next_role": "adjudicator",
  "next_action": "adjudicate_current_verdict",
  "review": {
    "state": "passed",
    "verdict_id": "...",
    "findings_count": 0
  },
  "blocking_reasons": []
}
```

`status` 不改含义；`workflow_status` 是用户可读的当前阶段；`review` 只回显权威 verdict 摘要；
`blocking_reasons` 只来自 daemon 可复核事实。Reviewer BLOCKED 后若已追加 `fix_defect`，主状态回到
`in_progress`，治理状态显示 `remediation_in_progress`，而不是丢失 BLOCKED 历史。

## 一致性约束

所有投影必须由 daemon 在同一只读连接基于 tasks、task_steps、task_verdict_events 和 task_events 派生；
CLI/MCP 只展示，不自行推断。Verdict Ledger 与历史事件保持 append-only；apply/close 仍只接受原有
`review`/`applied` 门禁。无效或无法验证的 verdict 进入 `workflow_status=review_pending` 并在
`blocking_reasons` 标出 `UNVERIFIED`，不得伪装成 pass。
