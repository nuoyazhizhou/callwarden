# P0-L step3 证据：next_action / claim policy enforcement

- 任务：T-1787801315246-e3e3a08c（P0-L：Role Worker Task Contract policy / preclaim enforcement remediation）
- 步骤：S-1787801315285-f68b2378（enforce_policy_in_next_action_and_task_claim）
- 执行者：executor-workbuddy-v1-cur（session cw-exec-wb-20260827-p0l，model workbuddy，role executor）
- 提交：**5b3e6f51d28a6f69201c03202697cbde1e5c95ad**（分支 `p0l-s3-tmp`，基于 b0d98d6 = master HEAD）
- 日期：2026-08-27

## 1. 实现内容（白名单内：task_collab.rs + dispatch.rs）

### 1.1 task_collab.rs（+387）
1. **三态 policy 状态枚举** `TaskContractPolicyState`（NoContractRevision / Unresolved / Declared(String)）
   + `get_current_task_contract_policy_state(conn, task_id)`：读最新 `task_contract_revisions.envelope_payload`；
   无行 → NoContractRevision（历史任务原路径）；有行但无可解析 `identity_policy` → Unresolved（fail-closed）；
   有声明 → Declared。
2. **`handle_task_claim` 同事务门禁**（`unchecked_transaction` 开启后、任何 step/contract binding 与
   status UPDATE 之前）：
   - NoContractRevision / Declared(legacy_identity_v1) → 原路径，零行为变化；
   - Declared(role_worker_v1) → 必须携带 identity + `role_worker_auth`，调用
     `validate_role_worker(&tx, auth, owner_key, bound_workspace, task_id, "task.claim", expected_role=identity.role)`：
     owner/active/credential-hash 校验、frozen role 映射、实例校验、同任务角色分离，
     并追加 append-only `role_runtime_provenance`（action_type=task.claim，不存 raw credential）；
     失败随事务回滚，无半状态；
   - Declared(未知策略) / Unresolved → `E_TASK_IDENTITY_POLICY_MISMATCH` 拒绝（禁止隐式降级为 legacy）。

### 1.2 dispatch.rs（+179/-4）
`handle_task_next_action`：单次 `with_conn` 内同时取 (projection, policy_state)，**只读、零 mutation**，
向派工投影注入结构化字段：
- `identity_policy`（null 或声明值）+ `identity_policy_status`（no_contract_revision / unresolved / declared）；
- role_worker_v1 → `claim_requirements{role_worker_auth:{required, expected_role=required_role, credential 说明}, identity:{required}, workspace_binding:{required}, separation:{required}}`；
- Unresolved → `claim_requirements{blocked:true, reason:...}`。

## 2. 测试（4 新增，全绿）

| 测试 | 场景 |
|---|---|
| `test_task_claim_role_worker_v1_requires_expected_worker_and_records_provenance` | 无 auth → E_TASK_IDENTITY_POLICY_REQUIRED；adjudicator worker → E_ROLE_WORKER_ROLE_MISMATCH；错 credential → E_ROLE_WORKER_CREDENTIAL_INVALID；正确 executor worker → claim 成功 + provenance 恰 1 行（不含 raw credential）+ status=in_progress |
| `test_task_claim_unknown_or_unresolved_policy_is_fail_closed` | 未知 policy（mystery_policy_v9）与空 envelope `{}` 均拒 E_TASK_IDENTITY_POLICY_MISMATCH；status 仍 open、provenance 0 行 |
| `test_task_claim_legacy_and_policyless_tasks_keep_original_path` | 显式 legacy（含 role_contracts）identity claim 无需 auth；无合同裸任务无 identity claim——原路径不变 |
| `test_task_next_action_projection_carries_policy_and_claim_requirements` | role_worker_v1 任务 → identity_policy/declared/claim_requirements 齐备且 expected_role=required_role；裸任务 → identity_policy=null/no_contract_revision/无 claim_requirements |

```
running 4 tests ... test result: ok. 4 passed; 0 failed（主树与隔离 worktree 双环境复验）
```

回归：`cargo test --lib -- role_worker identity_policy contract_bootstrap contract_revise claim next_action`
→ 85 passed / 1 failed。唯一失败 `next_action_test::review_with_reviewer_lease_yields_review_not_waiting`
经隔离 worktree `git stash` 归因实证：**纯 HEAD（无本补丁）同样失败**，为预先存在失败
（setup 阶段 set_task_contract 报 E_TASK_BINDING_REQUIRED，cd004d2 引入），与本步骤无关。

## 3. 并行会话脏树隔离提交说明（共享工作树纪律）

- 主工作树存在并行会话（任务 T-1787816580852-413f3158）的大量未提交改动（77 个脏文件，
  含本白名单文件），无法按文件整体 `git add`。
- 处置：编辑前快照 `.tmp_p0l_s3_base\` → 生成快照→当前 no-index diff → 以我的特征标记
  （`.tmp_p0l_s3_filter_diff.py`）逐 hunk 归属筛选（11 hunk 中 3 个属我，8 个属并行会话，无交叠混合）→
  在 `git worktree add --detach .tmp_p0l_s3_wt HEAD` 干净基线上 `git apply --check` 通过后应用 →
  worktree 内编译 + 4 个新测试全绿 + 回归子集复验 → 提交 5b3e6f5 → 分支 `p0l-s3-tmp` 锚定 →
  移除临时 worktree。
- `git merge --ff-only p0l-s3-tmp` 因主树脏文件（并行会话未提交改动）被拒，属预期；
  提交已在 `p0l-s3-tmp` 锚定（master 为其祖先），待并行会话提交后由受控流程合并。
- 主工作树文件中我的改动保持完好（未丢失、未被覆盖）。

## 4. 验收对照（合同 check items）

- ✅ next_action 返回 structured requirements（identity_policy / identity_policy_status / claim_requirements）
- ✅ claim 在同一事务、任何 step/contract binding 前验证 expected executor stable worker、policy、
  separation、contract claim 与 workspace binding（validate_role_worker 全链 + provenance）
- ✅ 无 implicit legacy downgrade（未知/未解析 policy 一律 fail-closed）
- ✅ 无白名单偏离（仅改 task_collab.rs / dispatch.rs；role_worker.rs 快照→当前零改动）
