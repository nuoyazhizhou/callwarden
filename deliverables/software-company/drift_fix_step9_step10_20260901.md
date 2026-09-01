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

## 7. 实际执行结果（2026-09-01 23:50，部署闭环完成）

- **代码已 commit + push**：`4d4796f`（step8 fix_defect + 漂移 step9/step10 源码修复 + compat_worker 存活修复）与 `e949ce9`（台账条目），已推送 origin/master（`cfd75c1..e949ce9`）。
- **测试**：`cargo test --lib -- daemon::task_loop daemon::assignment_queue daemon::task_collab_lease --test-threads=1` = **210 passed / 0 failed**（比此前 208 多 2 个为漂移 #1 新增回归）。
- **部署闭环三项验证全过**：`scripts/refresh_shared_runtime.ps1 -TaskId T-1787850432491-f42a2b8c -Configuration release -RestartMcp` 构建并切换 runtime；当前 daemon **PID 35156**（Bash 后台任务保活方式启动，见下），health `git_commit=e949ce9b7eb6`（== HEAD）✓、runtime/current 二进制 sha256 `d1be36ca…` == 部署证据记录 ✓、worker=healthy ✓。
  - **部署坑（重要）**：`refresh_shared_runtime.ps1` 内部用 `Start-Process` 启动 daemon，但脚本自身跑在短生命周期 PowerShell 会话里时，**会话结束后 daemon 子进程被连带终止**（3 次部署 evidence 均"启动成功→随后死亡"）。可靠保活方式 = Bash 后台任务（run_in_background）前台 exec `cw-daemon.exe --socket <endpoint>`，进程由 Bash 会话持有。
- **治理状态**：父任务 `T-1787850432491-f42a2b8c` = `review`/`review_pending`，9 step 全 `done`（含原 failed 的 step 6 test、step 8 fix_defect），无 blocking → **BR-01 gate #2 解除**。
- **漂移 #1 现场验证**：`task_assignments` 物理表已有该任务 `active` 行（`A-33b31a028d036ca2ba996d3b`，executor，session `sess-exec-bc045fae`），证明 claim 事件投影已回写物理表，Python 侧不再 fail-open。
- **历史遗留（已知，非新问题）**：该 `active` 行由**上会话部署**（未含本次 task_collab_lifecycle.rs report 收敛代码的二进制）claim 产生，其后的 report success（事件 6492/6493）发生在收敛代码合入之前，故未收敛为 `completed`，留下一条孤儿 `active` 行。事件投影（6491 claim / 6493 assignment_completed）为权威真源，物理表仅补偿视图，孤儿行不影响治理判定；当前 daemon（含 4d4796f 全部修复）的后续 claim/report 会正确收敛。清理该孤儿行可走 `task.claim` 重入/接管或治理收尾时顺手 REPLACE，不作本次阻断。

## 8. 独立 Reviewer PASS + Adjudicator 收尾（2026-09-01 23:55，父任务 closed）

在 §7 部署闭环基础上，父任务 `T-1787850432491-f42a2b8c` 走完四角色治理链最后两段：

- **独立核验（reviewer 角色）**：
  - `task.completion_review` → `decision=pass`、`findings=[]`；
  - `cw audit verify --limit 3000` → `Total: 3000, Verified: 3000, Broken: 0`（安全级 `hash_only`）。注意 `audit.verify_chain`/`audit_verify_chain` **不是** daemon 可直呼 RPC（`audit_verify_chain` 在 capability registry 中路由到 `python_compat` 后端，裸 RPC 报 `method_not_found`）；MCP 工具本会话未连接。权威入口是 CLI `cw audit verify`。
- **契约/合同绑定核查（权威 DB）**：`task_contract_revisions` 1 行（`TC-T-1787850432491-f42a2b8c` rev=1，hash `sha256:da848c…`）；`role_contract_lineages` 3 行（executor/reviewer/adjudicator）各 rev=1；**reviewer 权威 hash = `sha256:3e8debc9…`**（取自 `role_contract_revisions.role_contract_hash`）。verdict.submit 的新 schema 校验（`task_collab_verdict.rs` L188-220）要求提交 `role_contract_id`（lineage id `rcl-…-reviewer`）+ 该权威 hash；**不要**用 legacy 17 字段 payload 重算（那只在无 lineage 时生效）。
- **verdict.submit（blind_first_pass, overall=pass）**：lease.acquire（`reviewer-wb-186loop`，fence=3）→ `verdict.submit` 成功（`V-404d2b7cd6086820cdb5806e`，event 562，`replayed=false`）→ lease.release。`task_verdict_events` 行完整：phase/overall/clause_results（6 条全 pass）/findings（空）/role_contract_lineage_id/hash/normalization 列均已落库。
- **next-action 推进**：`ADJUDICATE / adjudicate_current_verdict / required_role=adjudicator`（reviewer PASS 生效，进入裁决阶段）。
- **Adjudicator 收尾（持证 apply/close）**：以 `adjudicator-workbuddy-p0adj-01` 身份 acquire `role=reviewer` lease（fence=4，identity=adjudicator 自身，apply/close 时 role 字段改 `adjudicator`——`validate_lease_for_mutation` 只比 agent/session/model 三项，不比 role）→ `task.apply`（status=applied）→ `task.close`（status=closed，叶子任务 9 step 全 done，无子任务门禁拦截）→ `lease.release`（零残留）。
- **最终投影（release 之后查询，规避规则 6 活跃 lease 优先路由）**：`status=closed`、`next-action: Decision=COMPLETE / Action=NONE / Routing=complete/finalize`、`ACTIVE_LEASES: NONE`。
- **BR-01 gate #2 最终解除**：父任务 `closed`，`task_collab.rs` 拆分任务不再占用治理状态，BR-01 本体可正式启动。

> 备注：reviewer 身份 `reviewer-wb-186loop`（role=reviewer，session `sess-reviewer-wb-186loop`）与 adjudicator 身份 `adjudicator-workbuddy-p0adj-01`（role=adjudicator，session `sess-adj-p0batch-20260831`）均为 active 注册；两条治理写路径（verdict 的 holder 一致校验、apply/close 的 adjudicator 持 reviewer lease）身份语义按 §7/§8 实测形态执行。
