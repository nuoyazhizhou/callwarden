# P0-K 独立 Reviewer 启动提示词与证据清单

> **适用任务**：`T-1787407700109-f5562c60`  
> **当前阶段**：P0-K Executor 已完成 source implementation 与定向验证；当前 live daemon 仍是 schema v58 debug authority，尚未载入 P0-K source。请进行**独立 source-level review**，不要把“未部署”误判为源码失败，也不要以未审代码修正 live drift。

## 可直接粘贴给独立 Reviewer 的提示词

```text
Role: reviewer
RuntimeRole: independent_reviewer
Task: T-1787407700109-f5562c60
Skill: none

Allowed:
  - 只读读取 P0-K task/evidence、AGENTS.md、git diff、以下限定 Rust source与测试日志；
  - 在本地以 --lib target 执行或复核定向 Rust test/cargo check；
  - 独立判定 reviewer_pass 或 reviewer_blocked，并写入一个新的无秘密审查报告；
  - 若你拥有已合法配置的独立 Reviewer Role Worker context，按当前 authority 能力和 Task Contract 执行合法的 verdict 流程；若 live daemon schema/route 未包含 P0-K code，则明确记录不能在旧 live binary 中提交 role_worker_v1 verdict，不伪造/借用 legacy identity 或 token。

Forbidden:
  - 修改源码、schema、Python business logic、task contract、task step、任务状态或数据库；
  - 直接 SQLite、伪造/借用 credential 或 lease token、输出 raw credential/hash；
  - 运行 refresh_shared_runtime.ps1、重启/停止 daemon、替换 runtime/current 或 debug binary；
  - 对 P0-J-D 的 pre-review deployment remediation 作删除、覆盖、pass/close 或“历史重写”；
  - 对 CLI-02、CLI-03、MCP-001 或批量任务做 bootstrap/修复。

Review objective:
  审查 P0-K 是否只对冻结 identity_policy=role_worker_v1 的任务接入稳定 CW-local Role Worker authorization，且 provider/account/model/agent/runtime session 只作为 append-only 无秘密 provenance；legacy_identity_v1 是否仍严格保持原 ActionIdentity + lease-holder matching 语义。

Mandatory checks:
  1. task_collab.rs 的 current_task_identity_policy 是否只从 task_contract_revisions 最新 envelope_payload 读取 policy，客户端不可选择；malformed/unknown policy fail-closed；legacy task 携带 role_worker_auth 返回 explicit mismatch。
  2. verdict.submit 是否在同一 transaction 内对 role_worker_v1 强制 reviewer worker auth，调用 role_worker::validate_and_record；lease token/fence仍保留，但不把 mutable provider/account/model/session 作为 authorization anchor。
  3. task.apply/task.close 是否在 role_worker_v1 强制 adjudicator worker auth，同时要求 reviewer lease token/fence和已有可验证 reviewer worker pass verdict；同一 worker 承担 reviewer/adjudicator 必须拒绝。
  4. raw credential 是否从不进入 task_verdict_events、task_events、role_runtime_provenance、status response、logs或evidence；只允许安全 reference（worker/instance/role-session）与无秘密 runtime provenance。
  5. 验证 Executor worker 无法作为 reviewer/adjudicator；wrong/revoked credential fail pre-write；provider/account/model/session rotation不使同一 stable worker失效；legacy verdict/apply/close regression不变。
  6. 审查是否出现任何 direct SQLite、外部 runtime identity binding、Python authorization business logic、unreviewed deployment或 P0-J-D historical fact alteration。
  7. 单独记录 live authority drift：latest manifest health schema=58，而 source schema=60 / P0-K code仅在 source build中通过。该事实意味着 live no-secret endpoint probe尚不可证明P0-K capability；它是受控 refresh 前的 deployment condition，不得隐藏或被 Executor 修复。

Evidence set:
  - deliverables/software-company/p0k_governance_mutation_auth_matrix.md
  - deliverables/software-company/p0k_role_worker_governance_mutation_implementation_evidence.md
  - deliverables/software-company/p0k_mapping_report_and_implementation_claim_receipt.json
  - deliverables/software-company/p0k_implementation_report_and_test_claim_receipt.json
  - deliverables/software-company/p0k_test_report_receipt.json
  - deliverables/software-company/p0k_test_step_authority_status.json
  - rust_ext/src/daemon/task_loop/role_worker.rs
  - rust_ext/src/daemon/task_collab.rs
  - .workbuddy/p0k_test_p0k_.log
  - .workbuddy/p0k_role_worker_domain_tests.log
  - .workbuddy/p0k_cargo_check_lib.log

Verdict rules:
  - reviewer_pass only if all mandatory checks pass, role_worker_v1 and legacy branches are explicit/non-overlapping, tests are reproducible, and the report states live drift/deployment remains unresolved.
  - reviewer_blocked if any authorization fallback, provider/session binding, credential leak, role bypass, same-worker conflict, missing reviewer lease/verdict proof, legacy weakening, direct SQLite/Python authority logic, or unreviewed deployment is found.
  - A source-level reviewer_pass does NOT authorize an Executor deployment. Any later controlled refresh requires separate Adjudicator authorization and post-refresh manifest/PID/health/SHA/schema/commit plus no-secret role_worker probes.

Required handoff format:
Handoff:
  from_role: reviewer
  outcome: reviewer_pass | reviewer_blocked
  next_role: adjudicator | executor
  next_action: <specific evidence-based action>
  reason: <precise source/test/drift result>
  independence_requirement: required
```

## Reviewer evidence map

| Evidence | What it proves | What it does not prove |
|---|---|---|
| `p0k_governance_mutation_auth_matrix.md` | Frozen P0-K design: explicit identity-policy split, roles, lease/fence semantics, non-goals. | Actual source correctness. |
| `p0k_role_worker_governance_mutation_implementation_evidence.md` | Implementation claims, scoped test commands, no-deployment boundary, known debug-lock limitation. | Independent review or live deployment. |
| `p0k_test_p0k_.log` | Five focused P0-K tests passed on the library target. | Live daemon behavior. |
| `p0k_role_worker_domain_tests.log` | Existing five security tests for CSPRNG/hash-only, runtime filtering, revocation and separation passed. | Governance handler integration by itself. |
| `p0k_cargo_check_lib.log` | Library build passes. | Replacing or validating the locked live debug executable. |
| `p0k_*_receipt.json` | Append-only executor report/claim provenance without raw token. | A reviewer/adjudicator verdict. |
| `p0k_test_step_authority_status.json` | At read time, live manifest health was healthy but schema version was **58**, and task projection lacked v60 P0-K details. | Source deployment or runtime convergence. |

## Required reviewer report sections

The independent report must state the source revision/diff examined, the tests actually re-run or read, the exact policy branch behavior, negative authorization cases, runtime provenance/credential-leak result, legacy regression result, and the live drift conclusion. It must explicitly say whether a source-level pass is conditional on later separate controlled-refresh verification.

A reviewer must not report that P0-J or P0-J-D is closed merely because P0-K source is accepted. The pre-review P0-J-D refresh remains an append-only remediation fact requiring its own independent disposition.

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_independent_review
  next_role: reviewer
  next_action: Conduct the bounded P0-K source-level review using the evidence set; document verdict and live-authority drift without deployment.
  reason: All four P0-K executor steps have source/evidence deliverables; implementation/tests passed on library target; deployment remains prohibited pending independent review.
  independence_requirement: required
```
