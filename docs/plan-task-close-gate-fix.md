# 实施计划：修复 task close 父子状态门禁、零步骤误关闭与 lease fail-closed

**任务 ID**: T-1786412969125-6edaa100
**缺陷根因**: `rust_ext/src/daemon/task_collab.rs:1291-1346` handle_task_close 完全不检查子任务状态、步骤状态、lease clock。

## S1: 父任务关闭门禁 - 子任务状态检查
- **target_file**: rust_ext/src/daemon/task_collab.rs
- **target_symbol**: handle_task_close (行 1291-1346)
- **验收标准**:
  - close 前 SELECT COUNT(*) FROM tasks WHERE parent_id = ? AND status != 'closed'
  - 存在 open/in_progress/review/applied/blocked/failed 子任务时返回 E_CHILD_TASKS_NOT_CLOSED
  - 检查与 close 写入处于同一事务
- **测试命令**: `cargo test --manifest-path rust_ext/Cargo.toml task_collab::tests --lib`

## S2: 零步骤普通任务禁止关闭
- **target_file**: rust_ext/src/daemon/task_collab.rs
- **target_symbol**: handle_task_close
- **验收标准**:
  - 普通任务（无 evidence-only 标识）steps=[] 时返回 E_NO_STEPS
  - 必须至少一个步骤且全部 done/skipped
  - 存在 failed/pending/blocked 步骤时返回 E_STEPS_NOT_DONE
  - evidence-only 任务必须用正式字段标识，不得靠描述文字绕过
- **测试命令**: `cargo test --manifest-path rust_ext/Cargo.toml task_collab::tests --lib`

## S3: lease clock fail-closed
- **target_file**: rust_ext/src/daemon/task_collab.rs, rust_ext/src/daemon/clock.rs
- **target_symbol**: handle_task_apply, handle_task_close
- **验收标准**:
  - lease clock 不可用时 apply/close 返回 E_LEASE_CLOCK_UNAVAILABLE
  - 不得自动降级为无 lease token 的兼容关闭
  - apply 也必须同样 fail-closed（当前完全跳过 lease 校验）
- **测试命令**: `cargo test --manifest-path rust_ext/Cargo.toml task_collab::tests --lib`

## S4: completion-review 零步骤任务 blocked
- **target_file**: rust_ext/src/daemon/task_collab.rs
- **target_symbol**: handle_task_completion_review (行 1523-1570)
- **验收标准**:
  - 零步骤普通任务 completion-review 返回 blocked/needs_changes
  - 不能返回 vacuous pass
- **测试命令**: `cargo test --manifest-path rust_ext/Cargo.toml task_collab::tests --lib`

## S5: closed_at 真实时间戳 + 级联规则保留
- **target_file**: rust_ext/src/daemon/task_collab.rs
- **target_symbol**: handle_task_close
- **验收标准**:
  - closed_at 写入真实非零时间戳
  - 保持现有父任务自动级联规则
  - 任何 open 子任务必须阻止父任务关闭
- **测试命令**: `cargo test --manifest-path rust_ext/Cargo.toml task_collab::tests --lib`

## S6: 回归测试
- **target_file**: tests/test_task_close_gate.py (新建)
- **验收标准**:
  - 父任务含 open 子任务时 close 被拒绝
  - 子任务 review/applied/in_progress 时 close 被拒绝
  - 所有子任务 closed 后父任务才允许 close
  - 空步骤普通任务不能 close
  - steps 含 failed/pending/blocked 不能 close
  - lease clock 不可用时 apply/close 被拒绝
  - close 成功后 closed_at 非零
  - task_events 记录正确状态变迁
- **测试命令**: `python -m pytest tests/test_task_close_gate.py -v`

## 执行记录
| 步骤 | 状态 | 验证 |
|---|---|---|
| S1 | ✅ | `handle_task_close` 增加子任务门禁：`parent_id = ?1 AND status != 'closed'` 存在即返回 E_CHILD_TASKS_NOT_CLOSED；与 close 写入同一事务，拒绝后状态不变 |
| S2 | ✅ | 叶子任务零步骤返回 E_NO_STEPS；pending/failed/blocked 步骤返回 E_STEPS_NOT_DONE |
| S3 | ✅ | `parse_lease_params`（lease_token+fencing_counter 成对才启用）+ `validate_lease_for_mutation`（clock→lease 存在→token hash→未过期→fencing→holder，6 步校验）；store 未注入时钟时 apply/close 均 E_LEASE_CLOCK_UNAVAILABLE fail-closed；`with_clock(Arc<AuthoritativeClock>)` 注入点就绪 |
| S4 | ✅ | `handle_task_completion_review` 零步骤返回 decision=blocked + reason=E_NO_STEPS（不得 vacuous pass） |
| S5 | ✅ | close 的 UPDATE 写入 `closed_at = ?1`（真实非零时间戳）；级联规则保留（Rust 从未实现级联，无删除对象，不破坏现有行为） |
| S6 | ✅ | Rust `task_collab::` 19 passed（新增 9 个门禁/lease/review 测试）；Python `tests/test_task_close_gate.py` 9 passed（5 源码静态断言 + 4 真实 CLI E2E） |

## 测试期间修复的两个测试自身问题
1. **task_leases FK 约束**：`task_leases.workspace_id` 有 `FOREIGN KEY -> workspaces(id)`，测试直接插 `workspace_id=0` 触发 FK 失败（`PRAGMA foreign_keys=ON` 生效）。修复：测试先补 `INSERT INTO workspaces (id=0, ...)` 一行。
2. **持有 conn guard 再调 handler 死锁**：`test_task_apply_close_lease_clock_unavailable_fail_closed` 在调用 `handle_task_close` 前仍持有 `store.conn.lock()` guard（非重入 Mutex）→ 挂起 >60s。修复：状态查询改为 block 作用域内完成并释放 guard。

## E2E 断言说明
- 真实 CLI `cw task close` 对 daemon 业务错误打印到 stdout 并返回 0 退出码（`cli/main.py` 既有契约，非本任务可改范围）。回归测试因此断言「错误码出现在 stdout+stderr + 任务状态未被写入 closed」，不断言非零退出码。
- 4 个 E2E 全部通过：父任务含 open 子任务被拒（E_CHILD_TASKS_NOT_CLOSED）、pending 步骤被拒（E_STEPS_NOT_DONE）、零步骤被拒（E_NO_STEPS）、全部步骤 done 后 close 成功且 closed_at>0。
