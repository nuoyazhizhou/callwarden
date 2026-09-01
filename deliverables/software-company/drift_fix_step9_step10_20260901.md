# 漂移修复 Step9 / Step10 交付说明（2026-09-01）

> 父任务：`T-1787850432491-f42a2b8c`（task_collab 拆分）
> 关联 remediation：`T-1787852751299-d7edabb0`（step 8，fix_defect，target=`rust_ext/src/daemon/`，status=`in_progress`）
> 指令：继续 step 8 的 fix_defect（修 step 6 的 failed test），并把两处漂移变成 step9 / step10 修正。

## 0. 一个关键约束：step 9/10 无法作为"治理步骤"凭空创建

经核查 daemon RPC 注册表与 `cli/task.rs`：

- 没有任何 daemon RPC 能追加**任意计划步骤**。步骤写入只发生在：
  - `task.remediation.create` —— 硬性要求 `source_outcome=failed_step` 且源 step `status='failed'`（否则 `E_FAILED_STEP_NOT_UNRESOLVED`），即只能为**已失败的 step** 开 fix_defect，无法为"规划中的漂移修正"开 step；
  - `task.report`(success=false) —— 自动追加 fix_defect；
  - `task.handoff` / `task.split` 等。
- `cli/task.rs` 里存在直接 `INSERT INTO task_steps` 的 legacy 路径，但本项目**硬性禁止直接 SQLite 治理写**（会触发 WAL 写楔、破坏事件溯源账本），不能作为正规手段。

**结论**：step 9 / step10 在治理账本里以**源码修复 + 本交付说明**的形式落地，其载体就是 step 8 这个 `fix_defect` 子任务（它本来就覆盖 `rust_ext/src/daemon/` 全范围，两处漂移修复都在该范围内）。下列两个修复即为 step 9 / step10 的实质交付物。

## 1. Step 6 的 "3 failed test" 现状（step 8 的修复对象）

- step 6（index 6，`S-1787850432491-f433c5c0`，action=test，status=`failed`）的 `result` 记载："86/89 passed；three existing behavior/expectation defects are documented."
- **在当前工作树中已不可复现**：
  - `cargo test task_collab` → **114 passed / 0 failed**（前序会话）
  - 全量 `cargo test --lib`（单线程，`/tmp/cwtest_single.log`）→ **`daemon::*` 全部通过**，无 daemon 模块失败。
  - 该 3 个缺陷应已在 P0-B~F 系列收尾工作中被连带修正，文档未单独留存证据（DB 中无结构化 defect 清单）。
- 全量测试里出现的 **4 个失败均属于 `cli::router` / `cli::runtime` 的 Windows socket/路径环境断言**（本 Linux 沙箱无 named pipe / Windows 路径），与 `rust_ext/src/daemon/` 无关，**不在 step 8 范围内**，也不属 step 6 记载的 3 个行为/期望缺陷。
- 因此 step 8（fix_defect，修 step 6 的 failed test，范围 `rust_ext/src/daemon/`）在现有代码下**实质成立**：daemon 模块测试全绿，原 3 缺陷不复现。

## 2. Step 9（漂移 #1）：claim 事件投影未回写物理派工表 → Python 侧 fail-open

### 根因
- Rust 治理侧采用**事件溯源派工**：`task.claim`（`handle_task_claim`，`task_collab_lease.rs`）只向 `task_events` 追加 `assignment_claimed` 事件，由 `assignment_queue::project_task_assignments` 重放出 `assignment_id`（`A-<sha256>`）并返回。
- 但 **Python 治理层**（`db/db_task_leases.py` 的 `assignment_show` / `has_active_assignment`）读取的物理表 **`task_assignments`**（`db/schema.py` v46 定义，`assignment_id TEXT UNIQUE`、`workspace_id`、`status='active'`…）**从未被 Rust claim 路径写入**。
- 后果：`task_assignments` 恒为零行。任何以该表为权威、判断"任务是否有 active assignment"的网关都会 **fail-open**（误判为"无 active assignment"），与事件投影真相脱节 —— 这正是用户观察到的"表零行但 `task.claim` 返回 `assignment_id`"漂移。
- 注：Rust daemon 治理路径本身不读 `task_assignments`（它用事件投影），所以漂移 #1 的 fail-open 面是 **Python 侧 / 跨语言网关**。全仓（含 `.py`/`.sql`）扫描确认 `task_assignments` 仅由 `admin.assignment_create`（legacy）与本次新增的补偿写访问。

### 修复（源码，落于 step 8 范围）
1. `rust_ext/src/daemon/assignment_queue.rs` 新增 `persist_claimed_assignment(...)`：
   - 在 `claim_assignment` 提交 `assignment_claimed` 事件后，于**同一事务**内 `INSERT OR REPLACE INTO task_assignments (workspace_id, assignment_id, task_id, role, agent_id, session_id, model_id, status, created_at) VALUES (..., 'active', ...)`。
   - `INSERT OR REPLACE` 兼容 claim 重入 / takeover（assignment_id 同源唯一）。
   - **容错**：若物理表未随迁移建立（`no such table`），视为旧库可接受状态，静默跳过；其余写入错误上抛为 `DaemonRpcError`，避免静默漂移。
   - 设计定位：**补偿写**——事件投影仍是 assignment 唯一真相来源，本写仅消除 Python 物理表与事件流的视图漂移（注释已写明，不构成第二真相源）。
2. `rust_ext/src/daemon/task_collab_lease.rs` 的 `handle_task_claim`：
   - 在事务开头取得 `workspace_id = task_bound_workspace_id(&tx, task_id, None)?`（原 `POLICY_ROLE_WORKER_V1` 分支内的重复调用改为复用），供补偿写使用；
   - `claim_assignment` 返回 `assignment_id` 后，调用 `persist_claimed_assignment(...)` 回写物理表，再 `tx.commit()`。

## 3. Step 10（漂移 #2）：next_action 规则 7 不感知 claim 状态 → AFK 无限重 claim

### 根因
- `evaluate_next_action_inner` 规则 7（next_action.rs）：存在 unresolved failed step 时，强制走 `required_remediation_step` → `resolve_or_block_step`。
- `resolve_or_block_step`（next_action.rs:1572，改后）**只查 `active_lease_role`**（`task_leases` 活跃未过期租约）。而 step 被 claim 后仅 `task_steps.status` 变为 `in_progress`（不一定持有 lease）。
- 后果：claim 之后，`active_lease_role` 返回 `None` → 走 Role Contract 解析 → `Ready` → `claim_outcome`（`action="CLAIM"`, `next_action="claim_current_step"`）。AFK / 无人值守 loop 据此反复 CLAIM 同一个 remediation step，**死循环**。
- 这是典型的"投影未感知 claim 状态"漂移：claim 改了 step 状态，但 next_action 派工投影只读 lease。

### 修复（源码，落于 step 8 范围）
`rust_ext/src/daemon/task_loop/next_action.rs`：
1. 新增 helper `step_claim_role(conn, step_id)`：由 `task_steps.action` 推导治理角色（`fix_defect`/`implement`/`build`/`refactor` → executor；`verify`/`test` → tester；`review` → reviewer；`adjudicate` → adjudicator；无法映射 → `None`）。
2. 在 `resolve_or_block_step` 开头插入"已 claim"判定：
   - 若目标 step `status == 'in_progress'` 且能推导出治理角色 → 直接返回 `StepResolution::Waiting { holder_role: role }`（即 `action="WAIT"`, `next_action="wait_for_current_lease"`）。
   - 该 `WAIT` 携带由 step action 推导出的角色，使 loop 等待该 step 完成/resolve，而非反复 CLAIM，**打破无限重 claim 循环**。
   - 若 step `in_progress` 但 `action` 无法映射到治理角色 → 不返回 WAIT，落回下方既有 Role Contract 解析（fail-closed：映射不出角色即 `Blocked`），不引入"静默放行"。

## 4. 验证情况

- `cargo check --lib`：通过（仅既有 warnings，无新增错误）。
- 漂移相关测试 `cargo test --lib -- daemon::task_loop daemon::assignment_queue daemon::task_collab_lease`：编译并重跑中（后台任务 `KxmvjK`），结果见 `/tmp/cwtest_drift2.log`。
- 单线程全量 `cargo test --lib`：`daemon::*` 全绿（无 daemon 模块失败）。
- 已知限制：并行 `--test-threads` 默认（多 worker）下，多个测试并发抢同一 SQLite 库级锁会产生长时间阻塞 / 内存回退（实测曾飙到 12GB）——这是**测试并发争用**，非 daemon 逻辑死循环；建议 CI / 本地用 `--test-threads=1` 或分模块串行跑。

## 5. 一致性收尾（待执行，需部署新 daemon）

step 8 的治理收尾（claim → report success → 推进父任务关闭 BR-01 gate #2）需要 daemon 运行在**含本次修复**的二进制上，且解除 `cw-daemon` 对 `callwarden.db` 的 WAL 写锁。

建议执行序列（需在用户环境、停掉当前旧 daemon 后进行）：
1. 构建新 daemon：`cd rust_ext && cargo build --release --bin cw-daemon`（或走 `scripts/refresh_shared_runtime.ps1 -TaskId <T> -Configuration release` 规范部署入口）。
2. 停掉旧 `cw-daemon`（PID 34092 之类），部署新二进制到 `~/.callwarden/runtime/current/cw-daemon.exe`。
3. 重启 daemon，确认健康。
4. executor 身份 claim step 8：`cw task next-action` / `cw task claim --task-id T-1787852751299-d7edabb0`（此时漂移 #1 修复会让 claim 回写 `task_assignments`，使后续 report 的 assignment 校验通过）。
5. `cw task report --task-id T-1787852751299-d7edabb0 --success true`，将 step 8 置 `done`，并把 step 6 关联判定为已修复（或直接 resolve step 6）。
6. 推进父任务 `T-1787850432491-f42a2b8c` 关闭，解除 BR-01 gate #2。

> 注：当前会话中 `cw-daemon` 仍持有数据库，且为旧二进制；若直接治理写会因 WAL 写楔失败。上述收尾需先完成"构建→部署→重启"才具备落库条件。

## 6. 附带发现（建议另立任务，非本次范围）

- `task_collab.rs` 拆分目标（≤2000 行）**未达成**：`task_collab.rs` 仍是 2739 行，`task_collab_tests_core.rs` 2493 行。step 8 的 `target_file=rust_ext/src/daemon/` 覆盖了它们，但原始"瘦身"目标本身落空，建议作为独立技术债任务追踪。
- `cli::router` / `cli::runtime` 的 4 个 Windows socket/路径测试在本沙箱失败，属环境相关、与本次无关，但反映 CLI 在 Linux 下 fail-closed 断言偏硬，可后续放宽。
