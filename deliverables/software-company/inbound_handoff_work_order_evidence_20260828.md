# 任务交接自描述化（inbound_handoff + work_order 只读投影）证据文件

- 任务：T-1787912195064-2c66e0a8
- 步骤：S-1787912195071-2ccff444（freeze_projection_contract / implement_inbound_handoff_projection /
  implement_work_order_projection / wire_into_next_action / prove_negative_matrix /
  prepare_independent_review 六步合一执行）
- 执行者：implementer-workbuddy-v1（RuntimeRole=implementer；Role=executor）
- 日期：2026-08-28
- 冻结计划：`deliverables/software-company/plan_inbound_handoff_work_order_v1.md`（唯一 scope 依据）

## 1. 当前 HEAD

```
869a92dfb419eb068f3385e54ccc39e8e20da54b
```

计划基线 HEAD 与本轮 HEAD 一致（本任务在计划冻结后未发生基线移动）。

## 2. 改动文件（白名单，全部按冻结计划 §4）

| 文件 | 状态 | 说明 |
|---|---|---|
| `rust_ext/src/daemon/task_loop/inbound_handoff.rs` | 新建 | 投影逻辑（纯只读） |
| `rust_ext/src/daemon/task_loop/inbound_handoff_test.rs` | 新建 | 8 条负向测试 |
| `rust_ext/src/daemon/task_loop/mod.rs` | 修改（+2 行） | 仅模块注册 |
| `rust_ext/src/daemon/task_loop/next_action.rs` | 修改（最小接线） | `evaluate_next_action_inner` + public wrapper |
| `deliverables/software-company/inbound_handoff_work_order_evidence_20260828.md` | 新建 | 本文件 |

未触碰：dispatch.rs、task_loop/operation_store.rs、scripts/refresh_shared_runtime.ps1、
task_collab*.rs、report_handoff.rs、db/schema.py、db/db_base.py、cli/main.py、server/**。
`next_action_test.rs` 未修改（既有测试已覆盖回归，全部通过）。

## 3. 行数门禁（规则 47 硬阈值 1500）

```
next_action.rs       1492 行  < 1500 ✓
inbound_handoff.rs    425 行
inbound_handoff_test.rs  610 行
```

`next_action.rs` 改动仅：`evaluate_next_action` 改名 `evaluate_next_action_inner` +
文件末尾新增 15 行 public wrapper（导入+调用+字段插入），净增 15 行（1477 → 1492），
未越过硬阈值。

## 4. 验收命令执行结果

### 4.1 rustfmt

```
rustfmt --edition 2021 rust_ext/src/daemon/task_loop/inbound_handoff.rs
rustfmt --edition 2021 rust_ext/src/daemon/task_loop/inbound_handoff_test.rs
```

通过（无错误）。

### 4.2 cargo check

```
tokenslim run "cargo check --manifest-path rust_ext/Cargo.toml --lib"
```

真实输出摘要：

```
warning: function `register` is never used   (staging_log_query.rs:183)   [既有 warning]
warning: function `parse_diagnostics_from_result` is never used  (multi_lang.rs:1729) [既有]
warning: field `source_db_file` is never read (snapshot.rs:57)      [既有]
warning: function `minhash_jaccard_estimate` is never used (clone_detection.rs:511) [既有]
warning: `callwarden-core` (lib) generated 159 warnings (run `cargo fix ...`)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 16.34s
```

结论：**通过**。159 条 warning 全部为既有（未新增；本次 diff 未引入新 warning 类别）。

### 4.3 cargo test（完整 daemon 回归）

```
tokenslim run "cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib"
```

真实输出摘要：

```
test result: FAILED. 1133 passed; 13 failed; 0 ignored; 0 measured; 416 filtered out
```

**13 个失败全部位于并行任务在途 dirty 模块，与本次白名单改动无交集**：

| 失败测试 | 失败根因 | 归属 |
|---|---|---|
| `route_matrix::tests::test_coverage_is_239` | 并行任务在 route_matrix.rs 新增 `task_step_bind_role_contract` route（工作树 dirty，+1 行），TOOL_ROUTES.len() 239→240 | 并行 P0-L 在途 |
| `route_matrix::tests::test_meta_tools_length` | 同上，meta_tools 长度 239→240 | 并行 P0-L 在途 |
| `task_collab::tests::core::test_task_collab_full_lifecycle` | `E_GOVERNANCE_REVIEWER_INVALID`（reviewer lease holder 必须为 active registered reviewer）——并行任务在 task_collab_lease.rs / task_collab_contract.rs 改动 identity 门禁，测试基建未同步 | 并行在途 |
| `task_collab::tests::core::test_task_collab_migrates_v46_db_to_v50` | 同上 | 并行在途 |
| `task_collab::tests::governance::test_orphan_claim_recovery_requires_stale_owner_and_preserves_step_state` | 同上 | 并行在途 |
| `task_supersede::tests::*`（8 个） | `E_GOVERNANCE_REVIEWER_INVALID` / `assertion failed: store.handle_task_supersede(...).is_ok()` | 并行在途（task_collab_lifecycle_ops.rs 门禁） |

证据：`git status --short` 显示 `route_matrix.rs`、`task_collab_lease.rs`、`task_collab_contract.rs`、
`claim.rs` 等均为 M（并行任务在途 dirty）；`git diff HEAD -- route_matrix.rs` 确认新增 route 行。
上述 13 个失败模块全部不在本任务白名单内，本任务未触碰这些文件。

**本任务白名单模块测试结果（全绿）**：

```
cargo test inbound_handoff --lib:
  test result: ok. 8 passed; 0 failed; 0 ignored; 0 measured; 1557 filtered out

cargo test next_action --lib:
  test result: ok. 22 passed; 0 failed; 0 ignored; 0 measured; 1543 filtered out
  （含 next_action_test 20 条 + dispatch::tests 的 2 条 task.next_action 集成用例）
```

## 5. 负向矩阵逐项结果（冻结计划 §8，全部真实 Rust 测试）

| 用例 | 期望 | 结果 |
|---|---|---|
| `no_handoff_yields_diagnosis_no_handoff_and_routing_unchanged` | diagnosis=no_handoff；routing 与实施前一致（READY/CLAIM/executor/queued 逐字不变）；work_order.objective=当前 step action | ✅ PASS |
| `single_handoff_projects_each_field_equal_to_envelope` | 逐字段等于落库 envelope（handoff_event_id/from_role/target_role/outcome/reason/request_id/step_id/monotonic_seq/timestamp 不重算）；matches_current_routing=false 且 routing 未改写 | ✅ PASS |
| `multiple_handoffs_take_max_seq_and_list_ascending` | inbound_handoff 取 monotonic_seq 最大者；prior_handoffs 升序完整（1,2,3） | ✅ PASS |
| `unparsable_envelope_fails_soft_without_panic` | diagnosis=unparsable_handoff；不 panic；routing 不受影响；prior_handoffs 过滤损坏事件 | ✅ PASS |
| `mismatched_target_role_reports_false_and_keeps_routing` | matches_current_routing=false 且 routing 未被改写（next_role 仍 executor，origin_kind 仍 system_evaluator） | ✅ PASS |
| `failed_step_appears_in_prior_attempts` | prior_attempts 含该 failed step 的 step_id/step_index/action/status/result | ✅ PASS |
| `over_20_handoffs_and_failed_steps_truncate_with_flag` | prior_handoffs 截断 20 条 + truncated=true；prior_attempts 截断 20 条；inbound_handoff 仍取最大 seq | ✅ PASS |
| `projection_is_strictly_read_only` | 调用前后 task_events/tasks/task_steps 行数与内容逐字节完全不变（dump 全表比较） | ✅ PASS |

**8/8 通过。**

## 6. 真实响应片段（库级 round-trip）

`task next-action` 走 daemon RPC 前先经 `evaluate_next_action`（dispatch.rs 调用），以下为该函数
在内存 DB（claim-ready + 1 条 handoff_structured，seq=7）上的真实完整响应（`--nocapture` 捕获，
与 daemon 投影同构）：

```json
{
  "task_id": "t-1",
  "lifecycle_status": "open",
  "workflow_status": "queued",
  "current_role": "executor",
  "next_role": "executor",
  "next_action": "claim_current_step",
  "review": { "state": "not_in_review" },
  "decision": "READY",
  "action": "CLAIM",
  "required_role": "executor",
  "step_id": "1",
  "task_contract": { "id": "tc-t-1", "revision": 1, "hash": "sha256:task-1" },
  "role_contract": {
    "id": "rcl-t-1-implementer",
    "revision_id": "rcr-t-1-implementer-r1",
    "revision": 1,
    "hash": "sha256:3dd2e8ad...",
    "canonicalization_version": "role-contract-c14n/v1",
    "skill_id": "skill-1",
    "prompt_template_id": "pt-1",
    "handoff_to": ""
  },
  "routing": {
    "origin_kind": "system_evaluator",
    "next_role": "executor",
    "next_action": "claim_current_step",
    "reason": ["当前步骤 1 可领取（唯一 verified Role Contract binding）"]
  },
  "next_session": {
    "role": "executor", "task_id": "t-1", "step_id": "1", "must_be_new_session": false
  },
  "source": {
    "task_status": "open",
    "task_contract_hash": "sha256:task-1",
    "role_contract_hash": "sha256:3dd2e8ad...",
    "evaluated_at": "1787916951.293928"
  },
  "inbound_handoff": {
    "handoff_event_id": "he-t-1-r1",
    "from_role": "executor",
    "target_role": "reviewer",
    "outcome": "executor_ready_for_review",
    "reason": "已完成，请求评审",
    "request_id": "req-he-t-1-r1",
    "step_id": "1",
    "monotonic_seq": 7,
    "authoritative_timestamp": 1787000000.25,
    "matches_current_routing": false
  },
  "work_order": {
    "objective": "implement",
    "task_title": "task-t-1",
    "allowed_paths": ["src/"],
    "excluded_paths": ["target/"],
    "acceptance_checks": ["pass"],
    "required_evidence": ["log"],
    "commands": ["echo"],
    "prior_attempts": [],
    "prior_handoffs": [
      {
        "handoff_event_id": "he-t-1-r1",
        "outcome": "executor_ready_for_review",
        "reason": "已完成，请求评审",
        "monotonic_seq": 7
      }
    ]
  }
}
```

要点：
- `decision`/`action`/`routing`/`next_session`/`workflow_status`/`lifecycle_status` 与实施前
  逐字一致（对比既有 next_action_test 断言）；
- `inbound_handoff` 逐字段来自落库 envelope（未重算），`matches_current_routing=false`
  仅暴露"上一棒指向 reviewer 与当前 executor 派工不一致"事实，未改写 routing；
- `work_order` 的 objective/合同路径/验收/证据全部来自 daemon 既有数据，prior_* 为空数组
  时不编造。

## 7. 部署门禁（二选一，明确选择）

**选择 B：本轮未部署 runtime，live daemon 仍为旧 binary，新字段仅在库测试中验证。**

依据：
1. 本任务只改 daemon 库代码（`--lib`），冻结计划 §10 明确"不要求 live runtime 行为变更"；
2. `scripts/refresh_shared_runtime.ps1` 当前在工作树中为 dirty（并行任务在途改动），
   冻结计划明确"Executor 不得修改它"；执行它会触发全量重建（~26min）并重启 daemon，
   会与并行任务（route_matrix/task_collab_lease 等在途）的部署状态互相污染；
3. `git status` 确认 `scripts/refresh_shared_runtime.ps1` 为 M（并行在途），强行执行将
   吸收/覆盖并行改动，违反任务白名单纪律；
4. live `cw.py task next-action` 命中旧 binary 时新字段不可见属预期；新字段已通过
   8 条库级负向测试 + next_action 22 条回归全绿验证（§4.3/§5），实现正确性由库测试
   保障，无需 runtime 部署即可交付评审。

## 8. 证据文件 SHA-256

> 约定：以下哈希为**删除本段两行（本说明行 + `sha256:...` 行）后**的文件内容 SHA-256，
> 保证写入哈希行本身不影响取值，Reviewer 可复现（`sed '/^sha256:/d'` 后再 sha256sum）。

```
sha256:ff4265bd7b4d13a4dd627141154dac449d2edab0115cf915f62796ce58df2925
```

## 9. 备注与后续

- `cw refresh --all` 计划在 report 后执行；若返回 `method_not_found: build_full_graph`
  将如实记录"刷新未完成"（不旁路）。
- 13 个并行在途失败测试与本任务无关，已记录归属；Reviewer 复核时若需排除可参照
  §4.3 表逐项核对模块归属。
