# P0-L：Role Worker Task Contract Policy 与 Claim Enforcement

**状态**：待独立审查的任务草案；尚未创建 CW task，未修改 production source、runtime 或 task state。  
**建议父任务**：`T-P0J-ROLE-WORKER-IDENTITY`，作为已闭环 P0-K 的**独立 sibling remediation**；若该 parent 已不接受 child，只能由 planner 在 root migration parent 下新建，其 parent 选择必须在创建前通过 HTTP `task.status`/binding 只读核验。  
**port_type**：`governance_identity`。  
**risk profile**：`high_risk`。  
**identity policy**：目标为 `role_worker_v1`；只为 P0-L 的一次性受限自举定义记录型例外，绝不把 provider/account/model/agent/session 重新当作授权锚点。

## 1. 问题陈述

P0-K 已将 `verdict.submit`、`task.apply` 和 `task.close` 对显式 `role_worker_v1` 的治理写路径接入 stable CW-local Role Worker credential，保留 lease/fencing 的时序与并发语义，且把 provider/account/model/agent/session 降为无秘密 provenance。[1]

但后续 A″ 创建实测发现，`TaskCollabStore::handle_task_create` 会用内部 `task_create_contract_envelope(...)` 生成 generic revision-1 contract；该 helper 未接受创建请求提供的 policy，也未写入 `identity_policy`。`task.next_action` 随后把这一 generic current contract 视为可领取，返回具体 step 和空 `blocking_conditions`。当前 `task.contract_bootstrap` 与 `task.contract_revise` 仍强制 legacy `ActionIdentity` + adjudicator/reviewer lease path，未有 role-worker branch。[2]

结果是：新任务可在描述文字宣称 role-worker policy，却没有 daemon-side policy 字段，也没有 `next_action` / `task.claim` 的 fail-closed enforcement。这不是提示词可弥补的问题；若批量预建 A″-01…A″-37，就会扩大 legacy-claim bypass 的攻击与误操作面。

## 2. P0-L 唯一目标

在 Rust daemon 内建立 **Task Contract identity-policy 的完整闭环**：

```text
explicit policy on task.create / current contract revision
        → policy-aware contract bootstrap/revise
        → next_action returns requirements, never authorization-by-text
        → policy-aware task.claim validates stable worker credential pre-write
        → lease/fencing remains concurrency/review proof
        → runtime provenance remains append-only and secret-free
```

仅对 `identity_policy=role_worker_v1` 走 Role Worker path；`legacy_identity_v1` 走当前 legacy identity path。缺失、未知、多个或 policy/envelope 不一致时 fail-closed，绝不静默降级为 legacy。

## 3. 冻结边界

| 项目 | P0-L 内 | 明确不在 P0-L 内 |
|---|---|---|
| Task Contract policy | create 输入、canonical envelope、current revision read/validate、bootstrap/revise | 大规模补写历史卡；直接改表；删除旧 revision |
| dispatch | `task.next_action` 的 requirements/block、`task.claim` pre-write authorization | verdict/apply/close 主逻辑（P0-K 已处理；只可做回归测试） |
| Role Worker | 复用 `role_worker.rs` 的 parse/validation/provenance；必要时提取 read-only policy helper | 新 provider token、外部 account binding、raw credential/db persistence |
| lease/fencing | 维持并增强为 Role Worker reviewer/adjudicator review proof | 删除 lease 或以 runtime session/role string 代替 lease |
| compatibility | 显式 `legacy_identity_v1` 原路径不变；受控 legacy migration disposition | 对 missing/unknown policy 默认 legacy；对 185/A″ cards 批量 contract repair |
| deployment | 仅在独立 PASS 后受控 `refresh_shared_runtime.ps1`；验证 live/runtime SHA/schema/commit | Executor 直接 restart、replace runtime/current、部署 A″/MCP/CLI 功能 |

## 4. 建议步骤

### Step 1 — `map_task_contract_policy_and_preclaim_gap`

**目标文件**：只读 `rust_ext/src/daemon/task_collab.rs`、`rust_ext/src/daemon/task_loop/role_worker.rs`、`rust_ext/src/daemon/dispatch.rs`、`rust_ext/src/sqlite_query.rs`、canonical schema/migration、A″ creation observation。  
**交付**：`p0l_task_contract_policy_state_machine.md`。必须画出 `task.create → current contract → task.next_action → task.claim → bootstrap/revise` 的 source-to-RPC transaction map。

**必答问题**：

1. current contract 如何精确选择（`is_current=1` / revision / task id），如何拒绝 zero/multiple/malformed policy；
2. `task.create` 目前忽略哪个 caller envelope 字段，怎样做到 caller policy 与 persisted policy 完全一致；
3. reviewer lease 与 adjudicator Role Worker proof 如何分离，避免 adjudicator 冒充 reviewer 或接收 raw reviewer token；
4. older explicit `legacy_identity_v1` contract 怎样保持行为；pre-P0-L missing policy 如何处置而不 silent downgrade；
5. A″ parent/G0 如何在 P0-L 后**逐卡**追加 policy revision，为什么这不是批量/placeholder bootstrap。

### Step 2 — `implement_canonical_identity_policy_on_task_create`

**主要文件/函数**：

| 文件 | 精确 target | 必须实现 |
|---|---|---|
| `rust_ext/src/daemon/task_collab.rs` | `handle_task_create`; `task_create_contract_envelope`; current-contract helpers | 解析并验证 caller-provided canonical Task Contract envelope，持久化 `identity_policy`；输入 policy 和 persisted canonical policy 必须 exact match；不能再忽略 envelope |
| `rust_ext/src/daemon/task_loop/role_worker.rs` 或一个窄 policy module | typed `IdentityPolicy` parser/validator | 枚举仅允许 `legacy_identity_v1` / `role_worker_v1`；zero/multiple/unknown/malformed 一律 error；不含 secret |
| `rust_ext/src/sqlite_query.rs` 与 canonical migration | 若现有 envelope JSON 查询无法可靠表达 current policy，增加最小 v61 compatibility/migration support | migration idempotent；不直接从 Python/SQLite 写；不 bulk rewrite historical tasks |
| `db/schema.py`, `db/db_base.py` | canonical schema 与 Python migration parity（只有 Rust schema 变化时） | Python 只做 schema compatibility migration，不实现 authorization/business decision |

**决策**：P0-L 后新建有 role contracts 的任务必须显式带 `identity_policy`；申请 `role_worker_v1` 的任务缺 policy/envelope/match 任一项则 `E_TASK_IDENTITY_POLICY_REQUIRED`/`E_TASK_IDENTITY_POLICY_MISMATCH` 并在同一 transaction 回滚 task/steps/contracts/bindings。明确请求 `legacy_identity_v1` 的新卡保留 legacy path。P0-L 前的 current contract 无 policy 不是 legacy default，而是 `policy_missing_legacy_migration_required`，只能由一个明确、逐 task、append-only revision/disposition 流程处置。

### Step 3 — `implement_role_worker_contract_bootstrap_and_revision`

**主要文件/函数**：

| 文件 | 精确 target | 必须实现 |
|---|---|---|
| `rust_ext/src/daemon/task_collab.rs` | `handle_task_contract_bootstrap`; `handle_task_contract_revise`; `bootstrap_task_governance_contracts`; `validate_reviewer_lease_for_adjudication` and narrow policy-aware successor | 为 explicit role-worker policy 建立独立 branch；legacy branch 原样保留 |
| `rust_ext/src/daemon/task_loop/role_worker.rs` | `parse_role_worker_auth`; `validate_and_record` 或 typed authorize helper | 期望 role=adjudicator；在同一 tx 认证 stable worker、记录无秘密 runtime provenance；wrong/revoked mismatch pre-write reject |
| lease helper (same module or narrow module) | reviewer proof resolver | require a separate reviewer role-worker lease/proof already recorded through daemon; adjudicator 不提供/持有 reviewer raw token；fencing/currentness validated |
| `rust_ext/src/daemon/dispatch.rs` | route coverage / protected mutation set | handler/route signature consistency；不加 Python business shim |

**需要的 transition 规则**：

- target envelope `legacy_identity_v1`：完全调用当前 legacy identity + reviewer lease/fencing path；
- target envelope `role_worker_v1`：expected Role Worker=adjudicator；读取 task workspace binding；role worker 与 reviewer proof 不能为相同 `worker_id`；lease/fencing 保留为 transaction serialization/review evidence；
- current policy missing：不允许通过 bootstrap 偷偷默认 legacy；只能在有专门 migration evidence 的受限 role-worker branch 写**追加 revision**，并记录 `policy_migration_reason` 和 old hash；
- existing generic revision 不得 delete/rewrite；只允许 hash-linked append revision；
- any policy branch must reject provider/account/model/session token fields and never log a raw credential.

### Step 4 — `enforce_policy_in_next_action_and_task_claim`

**主要文件/函数**：

| 文件 | 精确 target | 必须实现 |
|---|---|---|
| `rust_ext/src/daemon/task_collab.rs` | `handle_task_next_action` and its eligibility helper | next action reads current policy and returns structured `claim_requirements` only; no textual/open state may equal authorization |
| `rust_ext/src/daemon/task_collab.rs` | `handle_task_claim` before step/contract binding writes | explicit role-worker policy requires `role_worker_auth`, expected executor worker role, contract claim and workspace binding; call validation/prewrite in same tx |
| `rust_ext/src/daemon/task_loop/role_worker.rs` | authorization helper(s) | credential validation uses local stable worker mapping; runtime changes accepted as provenance; validation writes no raw credential |
| `rust_ext/src/daemon/dispatch.rs` | `task.next_action`/`task.claim` schema/route test if necessary | no route allows a Python/CLI local fallback bypass |

**强制语义**：

- `task.next_action` 是 read-only **requirements/eligibility** response；在 role-worker task 无 auth 时返回 `requires_role_worker_auth` 或 policy blocker，而不是 unqualified `claim_current_step`；
- `task.claim` 才做 authoritative authorization；role string、agent_id、session_id、provider/model alone never satisfy it；
- valid executor worker may claim only executor step; reviewer/adjudicator or same task conflicting worker fail before binding/write; revoked/wrong/malformed credential fails pre-write; 
- `legacy_identity_v1` remains its existing registered ActionIdentity and contract_claim behavior; no Role Worker param is accepted as downgrade there; 
- missing/unknown/multiple policy blocks both next action and claim; neither record fake provenance nor mutate lease/step binding.

### Step 5 — `prove_policy_and_claim_negative_matrix`

Add focused in-memory/domain + handler tests. Every listed assertion must show zero unexpected task/step/contract/role-provenance mutation on rejection.

| Test family | Required positive / negative proofs |
|---|---|
| create atomicity | explicit role-worker envelope persists exact policy; missing/unknown/multiple/mismatched policy rolls back task, workspace binding, roles, steps and ledger result correctly |
| legacy compatibility | explicit `legacy_identity_v1` create/next_action/claim preserves prior identity/contract behavior; role_worker_auth on legacy route rejected, never implicitly used |
| bootstrap/revise | correct adjudicator worker plus separate reviewer worker proof succeeds; wrong/revoked credential, wrong role, stale fencing, same reviewer/adjudicator worker, malformed/missing policy all fail pre-write; generic current revision yields append-only policy revision only under explicit transition evidence |
| next action | missing/unknown/multiple policy returns blocker, no unqualified claim signal; role-worker policy exposes requirements but does not validate/record credentials on read |
| claim | valid executor worker with changed provider/model/session runtime can claim; executor cannot reviewer/adjudicator claim; wrong/revoked/malformed credential and cross-role same worker fail prebinding; duplicate request/retry retains idempotency/fencing |
| no secret | role-worker status/events/provenance/errors/evidence never include raw credential/hash/token/password/cookie/provider token |
| P0-K regression | existing `verdict.submit`/`task.apply`/`task.close` role-worker-v1 and legacy tests remain green |
| authority convergence | source/build tests separate from live proof; after independent PASS only, manifest PID/health/schema/commit/live SHA/runtime/current SHA agree |

### Step 6 — `prepare_independent_review_and_controlled_release`

Produce source mapping, test log/manifest, raw-secret scan assertion, migration/revision plan for exactly A″ parent and G0 (not A″-01…37), and live authority drift evidence. Executor does not deploy. Independent Reviewer verifies. Only independent Adjudicator after PASS may authorize `refresh_shared_runtime.ps1`; only then may authorized role workers append P0-L policy revisions for A″ parent/G0 and requery next action.

## 5. Acceptance criteria

P0-L is not complete because code compiles. It is complete only if all conditions below are independently reproducible:

1. Every new Task Contract has a canonical, exact, persisted identity policy; task.create no longer ignores the caller-provided policy envelope.
2. `role_worker_v1` path covers **create → revision/bootstrap → next_action → claim**; all four fail closed when policy/auth/separation invalid.
3. Stable CW-local role worker credential + frozen worker-role mapping authorize; agent/provider/account/model/session remain append-only, secret-free provenance.
4. Reviewer and adjudicator use different workers; reviewer proof keeps fencing/temporal semantics but does not expose or transfer raw reviewer token.
5. Legacy explicit policy behavior stays unchanged and missing legacy-era contracts need explicit migration/disposition, not a silent default.
6. All listed tests pass and no Python authorization or direct SQLite business logic is added.
7. Independent Reviewer PASS, independent Adjudicator verification, controlled deployment, and live binary/runtime/source convergence proof all exist.
8. Only then is it legal to append role-worker policy revisions for **A″ parent and A″-G0**, and only after their gates to create the next one small A″ card. P0-L never bulk bootstraps A″-01…A″-37.

## 6. One-time constrained self-bootstrap protocol

The current gap means a task that fixes policy enforcement cannot be perfectly protected by the policy it has not implemented. This is a narrow bootstrap paradox, not a reason to use fake identity.

If the user decides to create P0-L before its code exists, record the following **bounded exception in the P0-L task description and creation receipt**:

1. User directly authorizes a single P0-L `task.create` under the chosen parent through the healthy local HTTP daemon; use actual generated task ID and immutable workspace binding.
2. It must contain only the P0-L six steps and the three fixed prompt-hash role contracts; no A″/MCP/CLI card and no unrelated task creation.
3. The executor must use the already-enrolled executor local Role Worker credential in its own ACL-protected session file, even though pre-P0-L `task.claim` cannot yet prove it. The credential is never printed, copied, logged or inserted into DB/evidence.
4. The executor’s first report must bind the bootstrap exception, current generic-policy gap, and all source changes. Executor cannot apply/close/deploy.
5. An independent reviewer and independent adjudicator must be separately identified and may not borrow the executor worker. They may reject P0-L on any incomplete enforcement or provenance evidence.
6. After P0-L code is independently reviewed and controlled deployment is live, P0-L itself must be brought under an append-only `role_worker_v1` revision through the new policy-aware revision flow before final apply/close.
7. This exception expires after this single task. It cannot justify creating the 37 A″ cards or repairing any unrelated historical task.

## 7. Required three-role contracts

All role contracts use `skill_id=none` because this is normal Rust/Python daemon work, subject to AGENTS.md and TokenSlim rules. Production code scope is Rust daemon authority; Python may only be touched for schema compatibility, thin HTTP client fixtures, and test harnesses—never authorization or direct SQLite business behavior.

| Role | Allowed | Forbidden | Handoff |
|---|---|---|---|
| Executor | implement only P0-L handlers/helpers/migrations/tests/evidence under frozen allowlist; use stable executor worker locally | apply/close, deployment, direct SQLite, credentials in output, P0-K/A″/S3 rewrite, scope expansion | Reviewer |
| Reviewer | independent read-only source/test/evidence/authority verification; submit daemon verdict with own worker/lease once P0-L policy supports it | code/evidence/task modification, bootstrap repair, apply/close/deploy | Adjudicator on PASS; Executor on BLOCKED |
| Adjudicator | independent policy/role/lease/fencing/review evidence validation; after PASS apply/close and authorize controlled refresh | implementation, task planning, reviewer impersonation, historical rewrite | User or Executor for a specific finding |

## References

[1] [`p0k_role_worker_governance_mutation_implementation_evidence.md`](p0k_role_worker_governance_mutation_implementation_evidence.md)：P0-K governance mutation coverage and limits.  
[2] [`aprime2_role_worker_contract_bootstrap_blocked_20260827.md`](aprime2_role_worker_contract_bootstrap_blocked_20260827.md)：A″ generic contract/next-action empirical finding, `task.create` and bootstrap/revise policy gap.  
[3] [`rust_ext/src/daemon/task_collab.rs`](../../rust_ext/src/daemon/task_collab.rs)：`task_create_contract_envelope`、`handle_task_create`、`handle_task_contract_bootstrap`、`handle_task_contract_revise` 和 `handle_task_claim` source anchors.  
[4] [`rust_ext/src/daemon/task_loop/role_worker.rs`](../../rust_ext/src/daemon/task_loop/role_worker.rs)：stable role-worker credential, hash-only persistence and provenance validation domain.
