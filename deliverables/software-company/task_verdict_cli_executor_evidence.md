# T-1787721165244-93dff614 — CLI task-bound reviewer verdict create entry（executor 证据）

> 任务：修复 `cw` CLI 缺少任务级 reviewer verdict 创建入口的问题。该入口必须经 daemon
> 权威协议创建并绑定 task_id、step_id、role contract 三元组、结构化 findings、完整
> reviewer identity、reviewer lease token 与 fencing counter；不得使用直接 SQL、通用
> 无关联 verdict 写入或状态绕过。

## 1. 实现范围（step2 implement_task_verdict_create）

受支持 CLI 入口为 `cw collab verdict`（`cli/main.py` 中 `_handle_collab` 的
`verdict` 子命令，对应 daemon RPC `verdict.submit`）。

提交契约全量透传（与 daemon `handle_verdict_submit` 要求逐字段对齐）：

| CLI 参数 | daemon 字段 | 必填 |
|---|---|---|
| `--task-id` / `--step-id` | `task_id` / `step_id` | 是 |
| `--contract-id` / `--contract-hash` / `--contract-revision` | 同左 | 是 |
| `--role-contract-id` / `--role-contract-hash` / `--role-contract-revision` | 同左 | 是 |
| `--snapshot-id` | `snapshot_id` | 是 |
| `--request-id` | `request_id`（同任务内幂等） | 是 |
| `--phase`（blind_first_pass / post_reveal_amendment） | `phase` | 是 |
| `--overall`（pass / block） | `overall` | 是 |
| `--attestation` | `attestation` | 是 |
| `--amendment-ref` | `amendment_ref` | 条件 |
| `--clause-results` / `--findings`（JSON） | 同左（结构化） | 可选 |
| `--view-manifest-hash` | `view_manifest_hash` | 可选 |
| `--verdict-id` | `verdict_id`（幂等覆盖） | 可选 |
| `--agent-id` / `--session-id` / `--model-id` / `--role` | `identity`（reviewer/independent_reviewer） | 是 |
| `--agent-instance-id` | `identity.agent_instance_id` | **是**（argparse required） |
| `--lease-token` / `--fencing-counter` | `lease_token` / `fencing_counter` | 是 |

关键约束（step1 trace 结论落地）：
- reviewer `agent_instance_id` 设为必填（`required=True`），与 daemon
  `E_IDENTITY_INSTANCE_MISMATCH` 校验语义一致；
- `role` 仅允许 `reviewer` / `independent_reviewer`（daemon 侧
  `E_IDENTITY_ROLE` 拒绝其他角色）；
- 结构化 `clause_results` / `findings` 必须为合法 JSON，解析失败 fail-closed
  （`E_INVALID_JSON`），不发起 RPC；
- 全程经 `DaemonClient` governance-write 序列化点，daemon 不可达时 fail-closed
  输出 Structured_Reason（`_collab_governance_rejection`），无本地降级、无直写 SQL。

实现提交：`cli/main.py` verdict 子命令（2026-08-26，commit `87c79d4`）。

## 2. 测试范围（step3 fixture_negative_matrix）

测试文件：`tests/test_task_verdict_cli.py`（5 项，提交 `152ecba`）。

覆盖矩阵（对齐 step3 check_items）：

| 用例 | 覆盖点 |
|---|---|
| `test_task_bound_verdict_submits_complete_provenance` | 成功路径：task/step/双 contract/snapshot/request/phase/overall/findings/identity（含 agent_instance_id）/lease_token/fencing_counter 全量透传 |
| `test_task_bound_verdict_rejects_malformed_structured_inputs` | 必填 + 结构化输入：`--clause-results` / `--findings` 非法 JSON → `E_INVALID_JSON`，不发 RPC |
| `test_task_bound_verdict_requires_reviewer_instance_identity` | 实例身份：缺 `--agent-instance-id` → argparse SystemExit，不发 RPC |
| `test_task_bound_verdict_daemon_unavailable_fails_closed` | daemon authority 不可达 → fail-closed（无本地 verdict 落库） |

运行结果：

```
$ python -m pytest tests/test_task_verdict_cli.py -q
..... [100%]
5 passed
```

## 3. Reviewer 正式使用方式（step4 zero_authority_evidence）

Reviewer 使用以下命令提交任务级 verdict（全部经 daemon `verdict.submit` 权威协议）：

```powershell
cw collab verdict --json `
  --task-id <TASK_ID> --step-id <STEP_ID> `
  --contract-id <TC_ID> --contract-hash <TC_HASH> --contract-revision <N> `
  --role-contract-id <RCL_ID> --role-contract-hash <RCL_HASH> --role-contract-revision <N> `
  --snapshot-id <SNAPSHOT_ID> `
  --request-id "review-<TASK_ID>-<STEP_ID>-<seq>" `
  --phase blind_first_pass --overall block --attestation "<attestation>" `
  --findings '[{"code":"<CODE>","message":"<MSG>","file_path":"<PATH>","line":<N>}]' `
  --agent-id <AGENT_ID> --agent-instance-id <INST> --session-id <SESSION> --model-id <MODEL> --role reviewer `
  --lease-token <TOKEN> --fencing-counter <N>
```

前置条件：
- 已通过 `cw task claim` 领取 review 步骤并 `lease.acquire`（reviewer lease）；
- `snapshot_id` 为 daemon authority 发布（`snapshot.publish` / workspace register）返回的权威值；
- 双 contract id/hash/revision 从 `cw task show` 的 task_contract / role_contract 字段原样拷贝。

错误语义：
- daemon 不可达 / 降级 → `E_RPC_FAILED` / fail-closed Structured_Reason，绝无本地落库；
- 业务拒绝（lease、identity、contract 不匹配）→ daemon 原样 error 透传。

## 4. 验收对照

- [x] 受支持 CLI 创建入口：`cw collab verdict`
- [x] 绑定 task_id / step_id / 双 contract 三元组 / snapshot / request_id
- [x] 结构化 findings + clause_results
- [x] 完整 reviewer identity（5 字段，agent_instance_id 必填）
- [x] reviewer lease token + fencing counter
- [x] 无直接 SQL、无通用无关联 verdict、无状态绕过（纯 client，经 daemon 序列化点）
- [x] 负向矩阵（必填字段、实例身份、daemon authority error）与成功路径均有测试
