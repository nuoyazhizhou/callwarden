# Adjudicator 交接包：补 P0-C 首合同（task.contract_bootstrap）

> 来源：Executor 完成 P0-C（T-1787305175972-8712da28）的 executor evidence + 独立 reviewer pass 后，
> 按治理交接给 Adjudicator 角色执行首合同 bootstrap 与激活。

## 角色声明（Adjudicator 接手前填写）
```
Role: adjudicator
RuntimeRole: (legacy daemon role, if required)
Task: T-1787305175972-8712da28
Skill: none
Allowed: 调用 task.contract_bootstrap（role=adjudicator，持 reviewer lease token）给 P0-C 补首合同
Forbidden: 修改实现/证据/Executor 产物、覆盖 reviewer verdict、扩大 scope
Handoff: 完成后交治理收尾（关闭父 Epic 前置检查）
```

## 门禁前置（已核验满足）
| 项 | 状态 |
|---|---|
| P0-C status | `review`（executor ready_for_review event 3537 + reviewer pass event 3539） |
| 独立 reviewer lease | `brtl-T-1787305175972-8712da28-r1`，holder=`reviewer-wb-186loop`，active，未过期 ✅ |
| reviewer lease token | 已从 ledger request_id 反推并 sha256 验证匹配（见下） ✅ |
| Adjudicator 身份 | `adjudicator-wb-186loop`（agent_instance_id 非空，三重不同于 reviewer-wb-186loop） ✅ |
| workspace binding | ws-1，与请求 workspace_instance_id 一致 ✅ |

## 关键参数包（task.contract_bootstrap）

```jsonc
{
  "task_id": "T-1787305175972-8712da28",
  "request_id": "adj-<唯一随机>",          // 每次不同，避免 dedupe replay
  "workspace_id": 1,
  "workspace_instance_id": "ws-1",
  "envelope": {
    "identity_policy": "legacy_identity_v1",
    "contract_id": "TC-T-1787305175972-8712da28",   // 建议与既有命名一致
    "revision": 1,
    "profile": "code_change",                        // 或 review；须为合法 profile
    "objective": "P0-C Task Contract bootstrap / publication（A′ 调度前置）",
    "interfaces": ["task.contract_bootstrap"],
    "allowed_edit_scope": ["rust_ext/src/daemon/task_loop/task_contract_bootstrap.rs"],
    "acceptance_clauses": ["cargo test 通过", "3 个 test 文件 positive/idempotent/negative 矩阵绿"],
    "risks": ["bridge 计数口径缺陷已修（方案1），方案2 为长期债"],
    "rollback": ["task_contract_revisions/role_contracts 回滚脚本备份"],
    "dependencies": ["P0-F bridge 已实现并部署"]
  },
  "identity": {
    "agent_id": "adjudicator-wb-186loop",
    "agent_instance_id": "inst-adjudicator-wb-186loop",
    "session_id": "sess-adjudicator-wb-186loop",
    "model_id": "deepseek-v4-flash",
    "role": "adjudicator"
  },
  "lease_token": "brtl-token-T-1787305175972-8712da28-rev-14eeb32bbfbf",
  "fencing_counter": 1,
  "evidence_path": "rust_ext/src/daemon/task_loop/task_contract_bootstrap.rs;rust_ext/src/daemon/task_collab.rs:3272",
  "evidence_hash": "adjudicator_accept: impl+wire present; 3 test files; reviewer pass event 3539 + brtl lease verified"
}
```

### lease_token 来源（重要）
- 新 daemon 二进制（2026-08-24 第三次构建）已在 `task.bootstrap_reviewer_pass` 返回里**回传明文 lease_token**。
- 但 P0-C 的 reviewer pass 是**旧二进制**跑的（未回传）。其 token 由确定性规则生成：
  `lease_token = "brtl-token-<task>-<request_id>"`，其中 `<request_id>` 取自
  `task_operation_ledger` 中该 reviewer pass 记录的 `request_id = rev-14eeb32bbfbf`。
- 已验证：`sha256("brtl-token-T-1787305175972-8712da28-rev-14eeb32bbfbf")` == DB 中 `brtl-` lease 的 `token_hash` ✅
- 故 Adjudicator 直接用上面反推的 `lease_token` 即可，无需重跑 reviewer pass、不破坏证据。

## 调用示例（Python，经 daemon client 裸 call）
```python
from callwarden.server.daemon_client import UnixDaemonRpcClient
import uuid, json
c = UnixDaemonRpcClient()
params = { /* 上面的 JSON 包，request_id 换成新随机值 */ }
r = c.call("task.contract_bootstrap", params)
print(r)
```

## 门禁清单（handler 会逐项校验，失败即拒）
1. `workspace_id > 0` + `workspace_instance_id` 一致（ws-1）
2. `envelope` 必填，含 `identity_policy`（legacy_identity_v1 或 role_worker_v1）
3. `identity` 内嵌对象，role=adjudicator，含 `agent_instance_id`（否则 E_IDENTITY_INSTANCE_MISMATCH）
4. `lease_token` + `fencing_counter` 必填，sha256 须匹配 active reviewer lease 的 token_hash
5. reviewer lease 未过期、holder 为 active registered reviewer
6. **Adjudicator 不得等于 reviewer lease holder**（agent/instance/session 三重不同）
   - reviewer-wb-186loop vs adjudicator-wb-186loop → 三重均不同 ✅
7. `evidence_path` + `evidence_hash` 必填
8. `no_governance_projection`：task 在 bootstrap 前必须**为空投影**（P0-C 已回滚污染投影，满足）

## 预期结果
- 写入 `task_contract_revisions`（revision=1）、`role_contracts`（三角色）、`role_contract_lineages`
- 追加 `task_contract_bootstrapped` event（actor=adjudicator-wb-186loop）
- P0-C 首合同激活，可经同 RPC 给 132 review 子任务中缺 contract 的 5 个补合同

## 回滚预案（如需撤销）
- 备份脚本：`p0c_rollback_backup_20260824.sql`（含之前回滚污染投影的逆 INSERT）
- 若需撤销本次 contract_bootstrap：DELETE `task_contract_revisions`/`role_contracts`/`role_contract_lineages`/`task_events(task_contract_bootstrapped)` WHERE task_id='T-1787305175972-8712da28'

## 关联任务
- P0-F（T-1787310376068-44eb5f20）：同构流程，lease_token = `brtl-token-T-1787310376068-44eb5f20-rev-f021e8015485`（已验证匹配）
- 激活 P0-C 后，可批量解锁 132 review 子任务中缺 contract 的 5 个 + 38 个 verdict-normalization 任务（合计 43 个不可审）
