# C-16 / C-17 承接卡转交文档（交接给下一个 agent 接力）

- **交接时间**：2026-09-14（+08:00）
- **交接基线**：`C:/git_work/callwarden`，分支 `master`，**HEAD = `78bb2af46e59da2ea33644fc28b2d4c6696b50d1`**
- **交接方**：卡 A `T-1789340885245-071cb9b4`（C-14 + C-15 承接卡）执行/复核/收尾会话
- **本文档性质**：**只读工单**。写给**下一个接手 agent**，使其无需人工补提示词即可建卡并开工。
- **权威 finding 单源**：[pyt_regression_step4_handoff_backlog.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step4_handoff_backlog.md) §W13（C-16/C-17 两行）与 §W18（出界登记整节）。本文档与之一致，冲突时以 backlog 为准并回报差异。

> **读本文档前请先执行**（勿臆测 task_id）：
> ```powershell
> & C:\Python314\python.exe C:/git_work/callwarden/cw.py task list --flat
> ```

---

## 0. 一句话交接

卡 A（C-14/C-15，`assignment_create`/`assignment_revoke` 契约与 workspace 命名空间）**已 closed / workflow=completed**。
卡 A 执行期**额外实测发现两条越出其 `executor_allowed` 的真缺陷 C-16 与 C-17**，按 C 桶纪律登记未修。
**接手 agent 的任务 = 把 C-16、C-17 建为 2 张独立承接卡并各自走完 `executor → reviewer → adjudicator` 闭环。**

---

## 1. 已完成，勿重做（接手前必读）

### 1.1 卡 A 终态与提交

| 项 | 值 |
|---|---|
| task_id | `T-1789340885245-071cb9b4` |
| findings | C-14、C-15（+ 卡内收敛 C-17 的 assignment 两处） |
| 终态 | `lifecycle_status=closed` / `workflow_status=completed` / `next_action=finalize` |
| 盲审 verdict | `V-da5ad4a015f77515dc890d76`（blind_first_pass / overall=pass / findings=0） |
| 主提交 | `158432be194ca24693471373c73325650962de15`（代码 + 证据 + 契约裁决 + reviewer 回执） |
| 台账提交 | `5509a4e639061c564d5af73315e5b50f97d80b13`（`cw_task_commit_ledger.json` 单独提交） |
| 治理文档提交 | `78bb2af46e59da2ea33644fc28b2d4c6696b50d1`（adjudicator-close + backlog 回写） |

### 1.2 卡 A 已修内容（**勿重复修**）

落点唯一文件 [admin_handlers.rs](file:///c:/git_work/callwarden/rust_ext/src/daemon/admin_handlers.rs)：

- `handle_assignment_create`：INSERT 补 `assignment_id`（`ASG-<16hex>`，`getrandom::fill` + `hex::encode`，不引新 crate），返回值由 `last_insert_rowid()` 改为字符串；并改走权威 workspace resolver（C-17 的 assignment 侧）。
- `handle_assignment_revoke`：入参由 `task_id`/`role`/`reason` 改为 **`assignment_id`**；WHERE 改 `workspace_id=?2 AND assignment_id=?3 AND status='active'`；返回 `{ok, assignment_id, revoked_at}`；新增 `assignment_id → task_id` 反查。

### 1.3 权威 resolver（C-17 的既有解药，接手时直接复用）

[task_collab_shared.rs:471-507](file:///c:/git_work/callwarden/rust_ext/src/daemon/task_collab_shared.rs#L471-L507)：

```rust
pub(crate) fn task_bound_workspace_id(
    conn: &Connection,
    task_id: &str,
    requested_workspace_id: Option<i64>,
) -> Result<i64, DaemonRpcError>
```

- 读不可变 `task_workspace_bindings`；无 binding → `E_TASK_WORKSPACE_UNBOUND` **fail-closed**；
- 显式 `requested` 与 binding 不一致 → `E_WORKSPACE_AUTHORITY_MISMATCH`；
- 经 `task_collab.rs` 的 `pub(crate) use` 再导出，调用路径为 `crate::daemon::task_collab::task_bound_workspace_id`。

---

## 2. 待承接工作总览

| 卡 | findings | 落点（**均在卡 A `executor_allowed` 之外**） | 前置 | 闭环后解锁 |
|---|---|---|---|---|
| **卡 C** | C-16 | `rust_ext/src/daemon/task_collab_lease.rs`、`server/daemon_client.py`（按 §3 复核，后者**可能无需改**） | 无 | `cw assignment show` 的 CLI 端 `show → create → revoke` 往返 |
| **卡 D** | C-17 | `rust_ext/src/daemon/snapshot_state.rs`（admin 路由块，**其余 19 个 handler**） | **先做盘点 step** | 其余 admin handler 的 workspace 语义可达性 |

- **不合并一卡**：两者落点不同、根因不同（C-16 = 缺省值 `0` 未走权威 resolver；C-17 = 路由层传入的 workspace 命名空间错配），且 C-17 需要「先盘点再定修法」的前置 step，与 C-16 的「直接修」节奏不同。
- **不继续 parked**：两者均为**已复现**真缺陷（非推断），继续挂起会让刚闭环的 C-14/C-15 长期停在「仅 daemon RPC 层可用、CLI 端不可用」。

---

## 3. 卡 C 规格（C-16）

### 3.1 现象

`cw assignment show <task_id>` 在 assignment **确实存在**时仍恒返回 `{"status":"none", "task_id": ..., "role": ...}`。
若绕过 CLI、直接以 daemon RPC 调用 `assignment_show` 并**显式传 `workspace_id=1`**，则能命中同一个 active 行（卡 A §2.6 步 3 实测命中 `id` 与步 2 的 `assignment_id` 逐字一致）。
→ **同一份数据，只因调用通道不同而结论相反。**

### 3.2 根因（本交接文档已在 HEAD `78bb2af` 上逐行复核，**不同于卡 A 证据 §4.1 的表述**）

两个半句共同致因：

1. **daemon 侧缺省值错误** — [task_collab_lease.rs:2003-2099](file:///c:/git_work/callwarden/rust_ext/src/daemon/task_collab_lease.rs#L2003-L2099)：

```rust
let workspace_id: i64 = params
    .get("workspace_id")
    .and_then(|v| v.as_i64())
    .unwrap_or(0);      // ← 缺省 0，而 0 永远匹配不到 task_assignments.workspace_id
```

   随后 SQL 为 `WHERE workspace_id = ? AND task_id = ? AND status='active'` → 恒无命中 → 走 `none` 分支（`:2090-2098`）。
   该 handler **拿到 `_peer` 却未用**（`_peer: PeerCredential`），也未调用 §1.3 的权威 resolver。

2. **调用侧刻意不注入** — [daemon_client.py:3529-3550](file:///c:/git_work/callwarden/server/daemon_client.py#L3529-L3550) 的 `_is_task_scoped_authority_request()`：参数含**非空 `task_id`**（或 `superseded_id`）即判定为 task-scoped 请求；[daemon_client.py:3953-3957](file:///c:/git_work/callwarden/server/daemon_client.py#L3953-L3957) 据此**整体跳过 workspace 注入块**。设计意图（见该函数 docstring）是「task-scoped 方法的数字 workspace 应由 daemon 从不可变 `task_workspace_bindings` 解析」。

3. CLI 侧确实只传 `task_id`/`role`：[cli/main.py:1159](file:///c:/git_work/callwarden/cli/main.py#L1159) `"get_assignment": ("assignment_show", "READ_ONLY", ("task_id","role"))`；调用点 [cli/main.py:17696](file:///c:/git_work/callwarden/cli/main.py#L17696) `db.get_assignment(opts.task_id, opts.role)`。

**合成结论**：调用侧按「daemon 会自己解析 binding」的契约跳过注入，但 daemon 侧 `handle_assignment_show` **并未实现该契约**（回落到 `0`）。契约两侧各自自洽、合起来断裂。

> ⚠️ **交接提示**：卡 A 证据 §4.1 把调用侧描述为「只对 `task.` / `lease.` 前缀方法注入」，那是**陈旧表述**；本 HEAD 上的实际门禁是上述通用的 `task_id` 判别。接手 agent **必须在自己 HEAD 上重新逐行确认**（行号会漂移），并在自己的证据中写明真实机制，**不要照抄卡 A 证据的表述**。

### 3.3 建议修法（裁决权在承接卡 executor / step0）

- **首选（daemon 单侧修复）**：`handle_assignment_show` 把 `unwrap_or(0)` 改为走权威 resolver，与卡 A 的 `handle_assignment_create`/`revoke` 同源：

```rust
// 语义：参数未给数值 workspace_id → 由不可变 binding 解析；
//       显式给了 → 交给 resolver 做一致性校验（不一致 → E_WORKSPACE_AUTHORITY_MISMATCH）
let requested = params.get("workspace_id").and_then(|v| v.as_i64());
let workspace_id = crate::daemon::task_collab::task_bound_workspace_id(conn, &task_id, requested)?;
```

  注意：`task_bound_workspace_id` 需要 `task_id` 非空；`task_id` 为空时应在**取 conn 之前**就 fail-closed（`invalid_params`），不要带着空 task_id 去查 binding。
- **`server/daemon_client.py` 是否需要改**：按 §3.2 的通用判别规则，**大概率不需要**（跳过的语义本就正确，缺的是 daemon 侧实现）。**但必须用实测证伪或证实**：修完 daemon、部署后跑真实 `cw assignment show` 往返；若仍恒 `none`，再回到调用侧定位。
  **不得**在未实测前就改 `server/daemon_client.py` 的注入门禁——那会把 task-scoped 通用规则改成按方法白名单，blast radius 覆盖全部 task/lease 方法。

### 3.4 边界（合同）

```text
allowed（建议）:
  rust_ext/src/daemon/task_collab_lease.rs
  tests/
  deliverables/software-company/
forbidden（沿用 C 桶一律）:
  db/**, scripts/refresh_shared_runtime.ps1, direct SQLite writes,
  task.apply, task.close, task.supersede, status forgery
```

若 step0 裁决认为必须动 `server/daemon_client.py`，应**在卡创建时就把该文件写进 `allowed_paths`**，而不是执行期扩边。

### 3.5 验收（卡 C）

| # | 判据 |
|---|---|
| ① | `cw assignment show <task_id> --role <role> --json` 在 assignment 存在时返回 **active 行**（非 `{"status":"none"}`），且 `assignment_id` 与 create 回执逐字一致 |
| ② | `show → create → revoke → show` 四步在 **CLI 通道**端到端可用（撤销后 show 回到 `none`） |
| ③ | 无 binding 的 legacy task 调 show → **fail-closed**（`E_TASK_WORKSPACE_UNBOUND`），不得静默返回 `none` 掩盖 |
| ④ | 显式传不一致 `workspace_id` → `E_WORKSPACE_AUTHORITY_MISMATCH` |
| ⑤ | `cargo build` 零 error；`cargo test` **零新增失败**（须与 HEAD 基线同集对照） |
| ⑥ | `git diff --check` clean |
| ⑦ | commit 前缀用**本卡自身 task_id**（严禁复用 PYT 卡 `[T-1788871227327-45c94bd8]` 或卡 A `[T-1789340885245-071cb9b4]`） |

### 3.6 负向测试矩阵（禁止只做源码字符串断言）

| 用例 | 期望 |
|---|---|
| 参数无 `workspace_id`、task 有 binding | 命中 active 行 |
| 参数无 `workspace_id`、task **无** binding | `E_TASK_WORKSPACE_UNBOUND` |
| 参数 `workspace_id` = 错误值 | `E_WORKSPACE_AUTHORITY_MISMATCH` |
| `task_id` 为空 | `invalid_params`（不得 panic / 不得查 binding） |
| 已 revoke 的 assignment | `{"status":"none"}`（append 语义下 active 过滤生效） |
| 只读性 | 调用前后 `task_assignments` 内容不变 |

> 卡 A 的 `tests/test_c14_c15_assignment_contract.py`（源码契约范式）**不覆盖**本卡；本卡需**新增**自己用例文件。若新增「隔离 daemon live」用例，见 §7.4 的二进制陈旧陷阱。

---

## 4. 卡 D 规格（C-17）

### 4.1 现象与根因

admin 路由块 [snapshot_state.rs:3182-3229](file:///c:/git_work/callwarden/rust_ext/src/daemon/snapshot_state.rs#L3182-L3229) 统一经 `owned_workspace` 取 workspace：

```rust
let workspace = super::workspace::owned_workspace(&self.base.registry, peer.uid, ws)?;
let workspace_id = workspace.get("workspace_id").and_then(Value::as_i64)   // ← daemon registry 代理 id
    .ok_or_else(|| DaemonRpcError::internal_error("workspace_id 缺失".to_string()))?;
let conn = open_write(self, ws)?;                                          // ← 落到 ~/.callwarden/callwarden.db
match method { ... }
```

- 传入 handler 的 `workspace_id` 是 **daemon registry 的代理 id**（卡 A 实测：`daemon/registry.db`=**36**、`.callwarden/registry.db`=**207**）；
- `open_write` 在模板为空时回落到**主机级单库** `~/.callwarden/callwarden.db`（见 [snapshot_state.rs:2742-2747](file:///c:/git_work/callwarden/rust_ext/src/daemon/snapshot_state.rs#L2742-L2747) 注释）；
- 该库内 `workspaces.id` ∈ {1,2,3,5,6,7,8,9,10,11,17,19,20,21}，且 `task_assignments` 带 `FOREIGN KEY (workspace_id) REFERENCES workspaces(id)`（`db/schema.py:1638`）。
- → **两个命名空间混用**：`create` 恒 FK 失败、`revoke` 恒误报 `assignment_not_found`。C-14 的 NOT NULL 错误恰好掩盖了此缺陷。

### 4.2 存量：本路由块共 21 个方法名（**本次逐行复核**）

```
gc_archive_import, gc_archive_inspect, gc_archive_list,
gc_audit_get, gc_audit_list, gc_policy_get,
gc_policy_set, gc_retention, audit_rotate_key,
cleanup_rule_sync_log, clear_clones,
snapshot_compare, branch_register, branch_switch,
assignment_create, assignment_revoke,          ← 卡 A 已收口（2）
record_action_identity, register_attestation_revocation,
record_artifact_identity, publish_interface, select_interface_provider
```

- 合计 **21**；卡 A 已收口 **2**；**其余 19 个待盘点**。
- ⚠️ backlog §W18 与卡 A 证据 §4.2 记作「**18 个**」。**本次逐行复核为 19**（差值须在承接卡证据中澄清并回报 backlog），**不要**在未复核的情况下沿用 18。

### 4.3 卡 D 的**强制前置 step：workspace 命名空间盘点**（先盘点，后决定修法）

不得跳过盘点直接照抄卡 A 的 assignment 修法——**其余 19 个 handler 未必同因**：

- 部分 handler（`gc_*` / `clear_clones` / `snapshot_compare` / `branch_*`）写的是 **codegraph 面**表，其 workspace 语义与 task DB 的 `workspaces(id)` **未必同源**；
- 部分（`record_action_identity` / `register_attestation_revocation` / `record_artifact_identity` / `publish_interface` / `select_interface_provider`）可能同时落 task DB 与 codegraph DB。

**盘点 step 的交付物**（逐 handler 一行，落 `deliverables/software-company/`）：

| 字段 | 含义 |
|---|---|
| `method` | RPC 方法名 |
| `handler` | `admin_handlers.rs` 中的函数 |
| `writes_to` | 实际被写的 DB 文件 + 表名（**须读源码确认，禁止推断**） |
| `fk_target` | 该表是否有 `FOREIGN KEY ... REFERENCES workspaces(id)` |
| `workspace_id_用法` | 直接写入 / 仅 WHERE 过滤 / 未使用 |
| `风险等级` | 恒失败 / 静默误报 / 无风险（附依据） |
| `建议修法` | 走 `task_bound_workspace_id` / 改路由签名 / 无需改 |

**盘点完成后才写实现 step。** 若盘点结论为「多数 handler 无风险」，应把范围**收窄**并把结论写进卡 D 的合同（而不是硬把 19 个都改）。

### 4.4 边界与验收（卡 D）

- `allowed（建议）`：`rust_ext/src/daemon/snapshot_state.rs`、`rust_ext/src/daemon/admin_handlers.rs`（若盘点结论需要）、`tests/`、`deliverables/software-company/`。
- `forbidden`：同 §3.4。
- 验收：盘点表完整且逐项有源码依据；被判定有风险的 handler 有**实测前后回执**；判无风险的须给**源码/实测依据**（不得空白）；`cargo build` 零 error；`cargo test` 零新增失败；`git diff --check` clean；commit 前缀用本卡 task_id。

---

## 5. 建卡方式（daemon 权威，幂等）

**唯一合法建卡通道 = daemon `task.create`**（`governance_projection ok=true`），沿用 [create_c_bucket_remediation_tasks.py](file:///c:/git_work/callwarden/deliverables/software-company/create_c_bucket_remediation_tasks.py) 的既有范式：

1. **扩 `build_cards()`**：新增两条 dict（`title` / `findings` / `executor_allowed` / `acceptance` / `evidence` / `steps`），**不要新建脚本**；
2. 脚本已实现**幂等**（先 `task.list` 按 title 判重）；
3. 建卡参数（与卡 A/B 完全一致）：

```text
PARENT_ID             = "T-1788871227327-45c94bd8"   # PYT 回归卡
WORKSPACE_ID          = 1
WORKSPACE_INSTANCE_ID = "4baea3ff12c2ea5c"
identity_policy       = "legacy_identity_v1"
ENDPOINT              = "http://127.0.0.1:1615"
```

4. **卡 D 建议把 step0 设为 `action=adjudicate`（盘点）**，`target_file=deliverables/software-company/`，实现 step 待盘点后由 executor 依合同落地（卡 A 的 step0 契约裁决即此范式）。
5. 运行：`& C:\Python314\python.exe deliverables/software-company/create_c_bucket_remediation_tasks.py`
6. 建卡后**立即核对回执**：`task_id`、`governance_projection`、`status=open`，并回写 backlog §W14 的建卡回执表与 §W18 的「待建承接卡」标记。

---

## 6. 角色闭环流程（每卡都要走完）

```text
executor: claim → 实现 → report(evidence) → handoff executor_ready_for_review
reviewer: 独立 acquire lease → 盲审 → cw collab verdict --overall pass --phase blind_first_pass
          → handoff reviewer_pass（三元组逐字一致）
adjudicator: 独立 acquire reviewer lease → cw task apply → 验 applied_pending_close
          → cw task close → 验 completed → cw lease release
```

**硬约束（卡 A 踩过的坑，直接抄结论）**：

- `cw task apply` / `cw task close` **都要求 `--lease-token` + `--fencing-counter`**，且门禁是 `validate_lease_for_mutation(..., "reviewer", ...)` → **adjudicator 收尾必须持 `--role reviewer` 的 lease**；
- `cw task apply` / `cw task close` **不接受 `--json`**（与 `handoff`/`verdict`/`lease` 不同！）；
- `reviewer_pass` handoff 有 **verdict provenance 门禁**：须存在同 task 的 `overall='pass'` verdict，且 `verdict_step_id == source_step_id`、`snapshot_id`/`view_manifest_hash` 非空、`verdict_workspace == task_workspace_bindings.workspace_id`；
- 角色详情见 [role-protocol.md](file:///c:/git_work/callwarden/.agents/skills/cw-task-loop/references/role-protocol.md) §5/§7。

---

## 7. 环境前置与已知坑（**照做，否则会得到假失败**）

### 7.1 前置

- `CW_TEST_MODE=1`（否则 local/legacy 报 `E_MODE_DEPRECATED`）；
- 隔离 `USERPROFILE`/`HOME` 指向临时目录（否则真实 HOME 的 stale manifest → `E_HTTP_MANIFEST_STALE`）；
- 去代理：`NO_PROXY=127.0.0.1,localhost`；
- `PYTHONPATH=C:/git_work`；
- daemon 端点：`http://127.0.0.1:1615`（卡 A 期间实际监听端口见 `cw daemon health`，**以 health 回执为准**）。

### 7.2 已知坑

| # | 坑 | 处理 |
|---|---|---|
| K1 | `cw task show --json` / `cw task apply --json` / `cw task close --json` **均不支持**（RC=2） | 去掉 `--json` |
| K2 | lease **raw token 仅在 acquire 响应返回一次、不落盘**；跨 `RunCommand` 进程的 `$env:` 不保留 | 把 `hash 计算 → lease acquire → verdict/apply/close → release` **放进同一条 PowerShell 命令** |
| K3 | 未走 `agent.register` 的 holder 会被 daemon **orphan 回收**（`holder_registration_missing`），同期新 lease 的 `fencing_counter` **递增** | **禁止硬编码 counter**，一律用 acquire 回执里的值 |
| K4 | MCP `get_role_view` 端点陈旧（`WinError 10061`，指向 `http://127.0.0.1:12671/v1/rpc`） | 用只读探针直调 `HttpDaemonRpcClient.get_instance()` |
| K5 | PowerShell 内联 `python -c` 引号转义易失败 | 写独立 `.py` 脚本文件 |
| K6 | `cargo test` 全量有 **6 项环境既有失败**（全在 `src/cli/`：router/runtime/refresh），与 daemon 无关 | 用 `git stash` 回落 HEAD 做基线对照证明「同集」，**不要**当成自己引入的 |
| K7 | 隔离 daemon 夹具 `tests/convergence/conftest.py::_pick_bin()` **优先取 `rust_ext/target/release/cw-daemon.exe`**，该文件可能**早于你的修复**（陈旧）→ 直接复用会得到失真失败 | 用部署门禁产出的 `rust_ext/target/stage-refresh/release/cw-daemon.exe`（见 §8）；**不要**为此改共享夹具（blast radius 覆盖全套收敛测试） |
| K8 | `cw refresh --all` 当前返回 `method_not_found: 未知方法: build_full_graph`（`REFRESH_EXIT=2`） | **如实登记「刷新未完成」**，严禁旁路伪造成功 |

---

## 8. 部署门禁（AGENTS.md 规则 43）

改 `rust_ext/**` 后若需 live 验证，必须：

```powershell
pwsh -File .\scripts\refresh_shared_runtime.ps1 -TaskId <本卡 task_id>
```

核对**三方哈希一致**：构建产物 == `%USERPROFILE%\.callwarden\runtime\current\cw-daemon.exe` == 运行中进程可执行文件；并记录 `cw daemon ping` / `cw daemon health` 回执与 `rollback=false`。

> 卡 A 现状（可作为基线参照）：`runtime/current/cw-daemon.exe` 与 `stage-refresh/release/cw-daemon.exe` 的 `SHA256 = BE67915CBC5D4641AE3FBC255AC160D21ADA8D791B163CB98B0A0ED19DD3DF92`、`Length 45735424`、`LastWriteTime 14/9/2026 11:40:32`。**你的修复会改变该哈希**，须以你自己的部署回执为准。

若**不部署**：必须在 report 里写明「本轮未部署 runtime，live daemon 仍为旧 binary，结论仅在库测试中成立」，不得含糊、不得冒充端到端。

---

## 9. VCS provenance 纪律（role-protocol §7 五步）

```powershell
# 1) 按白名单 add（严禁 git add . / git add -A；工作树有大量并行 dirty/untracked 与探针）
git add <本卡改动的具体文件，逐个列出>
git diff --check            # 期望 exit 0
git commit -m "[<本卡 task_id>] fix(rust_ext): ..."
git rev-parse HEAD           # 记录完整 commit id
# 2) 追加 cw_task_commit_ledger.json 的 task_commit_entries 条目（含 evidence_sha256 / note）
git add cw_task_commit_ledger.json
git commit -m "[<本卡 task_id>] chore(ledger): ..."     # 台账单独提交
# 3) 治理文档（adjudicator-close + backlog 回写）单独一次 chore(governance) 提交
# 4) 最后才尝试 cw refresh --all（见 K8）
```

参考卡 A/C-13 先例：`158432b`（主）→ `5509a4e`（台账）→ `78bb2af`（治理文档）。

---

## 10. 严禁提交的临时物（**目前仍为未跟踪，勿误 add**）

```text
deliverables/_insp_agents_a.py            deliverables/_insp_ledger_ids.py
deliverables/_insp_d10_rpc_show.py        deliverables/_insp_ledger_ids2.py
deliverables/_insp_fk_ws.py               deliverables/_insp_ledger_ids3.py
deliverables/_insp_fk_ws2.py              deliverables/_insp_registry.py
deliverables/_insp_handoff_a.py           deliverables/_insp_role_view.py
deliverables/_insp_hash_before.py         deliverables/_insp_show_probe.py
deliverables/_insp_leases_a.py            deliverables/_insp_state.py
deliverables/_insp_steps_a.py             deliverables/_insp_ws_policy.py
deliverables/software-company/_c13_impl_mod_dispatch.diff
deliverables/software-company/_c13_impl_snapshot_state.diff
```

（自建只读探针请沿用 `_insp_*.py` 前缀，便于识别与排除。）

---

## 11. 诚实披露要求（本仓库硬性文化，卡 A 已按此执行）

报告/证据中**必须**如实列出，不得省略：

1. 本轮**未做**的事（未部署 / 未全量重跑 / 未新增 live 用例 / 出界未修）；
2. 一切**无效尝试**（如 `--json` 被拒、lease 未用即释放）；
3. 与既有证据/backlog 的**数字或机制差异**（如 §3.2 的机制表述更正、§4.2 的 18 vs 19）；
4. **fail-closed 未被绕过**的声明（未伪造 token、未直接写库、未伪造身份）。

---

## 12. 接手第一步（最短路径）

1. 读 [backlog](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step4_handoff_backlog.md) §W13 的 C-16/C-17 两行 + §W18 整节；
2. 在本 HEAD 上按 §3.2 / §4.1 逐行复核两个根因（**行号会漂移，不要照抄本文档行号**）；
3. 在 [create_c_bucket_remediation_tasks.py](file:///c:/git_work/callwarden/deliverables/software-company/create_c_bucket_remediation_tasks.py) 的 `build_cards()` 追加卡 C / 卡 D 两条；
4. 确认 daemon `http://127.0.0.1:1615` 健康后运行脚本建卡；
5. 按 §6 走 A′ 三角色闭环；卡 D **务必先做 §4.3 的盘点 step**。
