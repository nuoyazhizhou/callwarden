# Checklist：close 终局 assignment 收口

对应 spec：`docs/specs/assignment-close-finalization/spec.md`
任务：`docs/specs/assignment-close-finalization/tasks.md`

## 不变量

- [x] 不变量 1：任务 closed 后，`task_events` 投影中该任务全部 active assignment
      均被追加 `assignment_completed`，投影状态为 `completed`
      —— Rust helper 单测断言 queued/claimed 两条投影全部 `completed` 且无 `is_active()`
- [x] 不变量 2：物理 `task_assignments` 中该任务 `status='active'` 行全部收敛为
      `completed` —— helper 单测与 daemon 集成测试均断言 active=0、completed=2
- [x] 不变量 3：close 后不再暴露可领取 assignment
      —— `current_assignment` 所依赖的 `status='active'` 查询结果为 0

## 实现

- [x] `rust_ext/src/daemon/assignment_queue.rs` 新增
      `finalize_task_assignments`，在同一事务内先收敛事件投影再更新物理表，
      并对旧库 `no such table` 安全兼容
- [x] `rust_ext/src/daemon/task_collab_lifecycle_apply.rs` 的
      `handle_task_close` 在 `closed` 事件后调用 helper，响应新增
      `completed_assignments`
- [x] `handle_task_cascade_close` 对每个收尾节点调用同一 helper，响应结构不变
- [x] `db/db_tasks.py` 新增 `_finalize_active_assignments`，`task_close` 在
      `commit` 前调用并回传 `completed_assignments`
- [x] 未新增 schema 迁移、未改变 `reopen` / `revoked` / assignment 查询面语义

## 测试与门禁

- [x] Rust helper 单测：`cargo test --manifest-path rust_ext/Cargo.toml
      finalize_task_assignments` → **1 passed / 0 failed**
- [x] Rust close 回归：`cargo test --manifest-path rust_ext/Cargo.toml task_close`
      → **11 passed / 0 failed**（含 `test_task_close_finalizes_all_active_assignments`）
- [x] Python close 回归：`pytest tests/test_task_close.py -o addopts='' -q`
      → **24 passed**
- [x] 验收矩阵：`python scripts/check_e2e_matrix_closure.py` → **PASS（exit 0）**
- [x] runtime release 构建与切换：证据
      `runtime/evidence/20261102-222152-d9e708619452-3f4c540d.json`
      （status=passed、git_head=d9e7086、cw-daemon sha256 `5959ef0d…` 与构建一致；
      新 daemon PID 35180，`cw daemon ping` 返回 ok）
- [x] `git diff --check` 无空白错误
- [x] Rust 格式：新增代码与文件既有缩进风格一致；仓库级 `cargo fmt --check`
      存在基线（非本卡）差异，本卡未引入新的格式漂移

## 文档与治理

- [x] `docs/design/cli-mcp-matrix-backlog.md` 追加 §34（现象/根因、处置表、
      验证表、边界与回滚）
- [x] A′ 四角色闭环完成：executor report + handoff → reviewer verdict pass +
      `reviewer_pass` handoff（event 13041）→ adjudicator reviewer-scope lease
      apply/close → 投影 `closed/completed/COMPLETE/finalize` → lease 全部释放
- [x] 本卡 8 个步骤全部 `done`，`task show` 显示 `workflow_status=completed`
- [x] 代码提交（message 内嵌 `T-1793627972400-4a1e4a0c`）——`c1adc9a`
- [x] `cw_task_commit_ledger.json` 台账条目单独提交——`d9e7086`
- [x] 工作树干净（仅允许运行期/本地临时产物未跟踪）

## 边界与残留

- [x] 明确非目标：历史已 closed 卡残留 assignment 的批量清理；reopen 后
      assignment 重建；`revoked` 语义变更；schema 迁移
- [x] 已知残留：本卡 close 由旧 daemon（PID 35836，started_at 1793626075）执行，
      真实库仍保留 `A-2ec419c65223e5f140e7eb92`（adjudicator/queued）；
      新代码部署后由后续 close 生效，历史残留按非目标不批量清理
- [x] 复核后变更仅限文档：backlog 尾部空白清理、tasks.md 勾选 T7、新增
      checklist.md；5 个生产/测试文件 sha256 与 reviewer view-manifest 一致
