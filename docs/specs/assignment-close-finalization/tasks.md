# close 终局 assignment 收口（Tasks）

> 对应 `spec.md`。每个任务均需在 executor 阶段以 `cw task report` 留痕；
> 目标文件必须提前写入建卡 steps 的 `target_file`（防止 scope 外改动）。

## 进度

- [x] T1. assignment_queue 收口 helper + 单测（Rust）——`cargo test finalize_task_assignments` 1 passed
- [x] T2. daemon close / cascade_close 接入（Rust）——`cargo test task_close` 11 passed
- [x] T3. close 集成测试（Rust）——新增 `test_task_close_finalizes_all_active_assignments` passed
- [x] T4. Python task_close 同语义（db_tasks.py）——24 passed
- [x] T5. Python 回归用例——`tests/test_task_close.py` 24 passed
- [x] T6. backlog §34 + 验证矩阵——`check_e2e_matrix_closure.py` PASS
- [x] T7. A′ 四角色闭环（建卡/lease/report/verdict/apply/close/commit/台账）

## T1. assignment_queue 收口 helper + 单测（Rust）

- 文件：`rust_ext/src/daemon/assignment_queue.rs`
- 改动：
  - 新增 `pub(crate) fn finalize_task_assignments(tx, task_id, actor_identity, actor_session_id, seq, ts) -> Result<Vec<String>, DaemonRpcError>`：
    - 调 `complete_assignments(None, None, ...)` 收敛事件投影；
    - `UPDATE task_assignments SET status='completed' WHERE task_id=? AND status='active'`；
    - `no such table` 兼容（返回已收敛 id），其它错误上抛。
  - 新增单测 `finalize_task_assignments_completes_projection_and_physical_rows`：
    - 内存库建 `task_events` + `task_assignments`；
    - `queue_assignment`（executor, queued）+ `claim_assignment`（reviewer, claimed）；
    - 调 helper 后：投影无 active、物理行无 active 且为 `completed`。
- 验收：`cargo test --manifest-path rust_ext/Cargo.toml finalize_task_assignments` PASS。

## T2. daemon close / cascade_close 接入（Rust）

- 文件：`rust_ext/src/daemon/task_collab_lifecycle_apply.rs`
- 改动：
  - `handle_task_close`：`closed` 事件后、`record_action_identity` 前调用 helper；
    响应新增 `completed_assignments`（string[]）。
  - `handle_task_cascade_close`：每个节点 `cascade_closed` 事件后调用 helper；响应不变。
- 验收：
  - `cargo test --manifest-path rust_ext/Cargo.toml test_task_close_finalizes_all_active_assignments` PASS；
  - 既有 close/cascade 单测全 PASS（`cargo test --manifest-path rust_ext/Cargo.toml task_close`）。

## T3. close 集成测试（Rust）

- 文件：`rust_ext/src/daemon/task_collab_tests_lease.rs`
- 改动：新增 `test_task_close_finalizes_all_active_assignments`（步骤见 spec §3.3）。
- 验收：断言 close 响应含两条 assignment id；投影与物理表均无 active；物理行 `completed`。

## T4. Python task_close 同语义（db_tasks.py）

- 文件：`db/db_tasks.py`
- 改动：
  - 新增 `_finalize_active_assignments(task_id) -> List[str]`；
  - `task_close` 在 commit 前调用，响应新增 `completed_assignments`。
- 验收：`$env:PYTHONPATH='c:\git_work'; C:\Python314\python.exe -m pytest tests/test_task_close.py -o addopts='' -q` 全 PASS。

## T5. Python 回归用例

- 文件：`tests/test_task_close.py`
- 改动：新增 `test_task_close_finalizes_active_assignments`（步骤见 spec §3.5）。
- 验收：新增用例 PASS，既有 close 用例不回归。

## T6. backlog §34 + 验证矩阵

- 文件：`docs/design/cli-mcp-matrix-backlog.md`
- 改动：追加 §34（现象 / 处置 / 验证表 / 边界）。
- 验收：
  - `python scripts/check_e2e_matrix_closure.py` 输出 PASS（exit 0）；
  - backlog §34 验证表与实际命令输出一致。

## T7. A′ 四角色闭环

- 建卡：steps 覆盖 T1–T6 的 6 个文件。
- executor：report + `collab publish` + handoff `executor_ready_for_review`。
- reviewer：独立 session/lease + `collab verdict`（pass）+ handoff `reviewer_pass`。
- adjudicator：reviewer-scope lease（同身份、fencing=2）+ `task apply` / `task close`
  + `governance-projection`（closed/completed/COMPLETE/finalize）+ release lease。
- 收尾：代码 commit（message 含 task_id）+ 台账条目单独 commit + 复跑矩阵门禁。

## 依赖与顺序

T1 → T2 → T3；T4 → T5；T6 依赖 T1–T5 结果；T7 最后执行。

## 完成定义（DoD）

- 6 个文件均在卡内 `target_file` 留痕；
- Rust/Python 新增用例与既有回归全 PASS；
- `check_e2e_matrix_closure.py` PASS；
- 投影 close 后无 active assignment（事件与物理表双向一致）；
- 工作树干净、lease 全部释放。
