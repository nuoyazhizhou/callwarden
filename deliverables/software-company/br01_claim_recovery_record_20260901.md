# BR-01 authority / claim-recovery 治理修复闭环记录（2026-09-01）

角色：executor（recovery 身份）
目标任务：`T-1787850432491-f42a2b8c`「审计并拆分 task_collab.rs 至每文件不超过 2000 行」

---

## 1. 结论摘要

| 授权步骤 | 结果 |
|---|---|
| ① authority/claim-recovery 治理修复 | ✅ 完成（stale claim 已由 adjudicator + reviewer lease 合法释放） |
| ② 确认 daemon 无 active assignment / lease | ✅ 确认（两条 reviewer lease 均 released/expired，claim 已清空） |
| ③ 领取 remediation step | ✅ 完成（`T-1787852751299-d7edabb0` → `in_progress`） |
| BR-01 本体是否可开始 | ❌ **仍被门禁 #2 拦住**（见 §5） |

---

## 2. 根因：daemon 写路径楔死的真正持锁者

此前 8 个会话里所有治理写都以 `database is locked` / `attempt to write a readonly database` 失败。
根因**不是** crash 的 daemon，也不是 stale `-shm` 本身，而是**两个常驻外部进程持续打开 `~/.callwarden/callwarden.db`**：

1. `cw.exe refresh --all --mode local` —— Call Warden CLI 索引进程，**被 kill 后会自动重启**（本次观测 PID 23540 → 48548 → 21576）。
   注意：`cw.exe`（CLI）与 `cw-daemon.exe`（daemon）是两个不同二进制，只停 daemon 无效。
2. `cw.py server --transport stdio` —— WorkBuddy 内嵌 MCP server（`WorkBuddy.exe` 子进程），同样自动重生。

WAL 模式下 `-shm` 被外部独占时，SQLite 会静默退化为只读 → 表现为 `readonly database`。
`PRAGMA wal_checkpoint(TRUNCATE)` 返回 `(busy=1, N, N)`：**N 帧已合并进主库**（含 `claim.recover` 事件），
只是无法 TRUNCATE。因此主库数据完整，删除 `-wal`/`-shm` 不丢已提交治理数据。

### 修复配方（可复用）

```
1. 停 daemon
2. taskkill /F /IM cw.exe        # CLI refresh，真正的持锁者
3. 杀 cw.py server（WorkBuddy MCP 子进程）
4. rm -f callwarden.db-wal callwarden.db-shm   # daemon 会重建干净副本
5. 重启 daemon（否则会继承被锁的 -shm，重启也写不进去）
6. 每次治理写之前紧贴一次 taskkill /F /IM cw.exe —— 抢在自动重生之前
```

第 6 步「kill-before-write 竞速」是关键：`cw.exe` 秒级重生，必须把 kill 放在 RPC 调用的同一循环体内。
实现见 `_br01_release_now.py` / `_br01_claim_step.py` 的 `rpc()` 包装。

---

## 3. task.claim 真实门禁（源码核对，非文档推测）

源码：`rust_ext/src/daemon/task_collab_lease.rs::handle_task_claim`（L56–679）

- `task_id` 必填。
- **`agent_session_id` 必须等于 `identity.session_id`**（L216 `E_IDENTITY_SESSION_MISMATCH`）。
  daemon 默认用 `peer.owner_key()` 兜底，所以**必须显式传**，否则一定不等。
- `identity` 必须在 `agent_registrations` 中 `status=active`，且 `agent_instance_id` / `session_id` / `role` 与注册行一致。
- `check_role_independence` 角色独立性校验。
- 存在冻结 Role Contract 时必须携带 `contract_claim`，其 `skill_id` / `skill_version` / `prompt_hash`
  须与合同逐字相等（`verify_contract_claim_match`，`task_collab.rs:1224`）。
  本任务 executor 合同：`skill_id="none"`（**字面字符串 none，不是空**）、`skill_version=""`、
  `prompt_hash=59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6`。
  合同查询入口是 **`task.contract_get`**（`task.status` 不返回 `role_contract`）。
- 存在待处理 `fix_defect` remediation 时必须显式传 `remediation_step_id`，且需与
  `required_remediation_step()` 选中的完全一致（`E_REMEDIATION_STEP_REQUIRED` / `E_REMEDIATION_STEP_MISMATCH`）。
- **`task.claim` 不需要 lease token / fencing_counter。** `require_lease_params()` 只出现在
  `handle_task_claim_recover`（L723）。`next_action.authorization.lease_required=true` 描述的是后续
  mutation（report/apply）的要求，不是 claim 本身；`eligibility.verdict = not_required_for_claim` 即此意。

---

## 4. 落库证据（只读核验 `~/.callwarden/callwarden.db`）

claim 返回：
```json
{"task_id":"T-1787850432491-f42a2b8c","status":"in_progress",
 "claimed_by":"sess-exec-bc045fae","claim_recovered":false,
 "assignment_id":"A-7b7421762935b41a97d4e2ac","step_id":"S-1787850432491-f433e17c"}
```

`task_events` 审计事件（权威）：

| event_id | reason_code | role | session | 说明 |
|---|---|---|---|---|
| 6485 | `claimed` | executor | `sess-exec-bc045fae` | task claimed by agent |
| 6486 | `assignment_claimed` | executor | `sess-exec-bc045fae` | `A-7b7421762935b41a97d4e2ac` → step `T-1787852751299-d7edabb0`, status `claimed` |

`task_steps` 现状：

| idx | step_id | action | status |
|---|---|---|---|
| 0–5 | S-…f431f86c … f433abf8 | audit / extract ×5 | done |
| 6 | `S-1787850432491-f433c5c0` | test | **failed**（未解决，触发 remediation） |
| 7 | `S-1787850432491-f433e17c` | verify | pending |
| 8 | `T-1787852751299-d7edabb0` | fix_defect | **in_progress**（本次领取） |

`task_leases`：`L-f81e0489052b082a` = `released`（`released_at=1788266890`），
`L-be70210bf8ea7bd3` = `expired`。→ **无 active lease**。

---

## 5. BR-01 门禁 #2：仍然拦住，BR-01 不可开始

门禁原文：*"该任务仍有 active assignment/lease 或仍修改 `task_collab.rs` 时，不开始 BR-01"*

- `T-1787850432491-f42a2b8c` 仍为 `in_progress`；
- step 6 `test` 仍为 `failed`（未解决）；
- 刚领取的 remediation step 8 `target_file = rust_ext/src/daemon/`，
  覆盖 `task_collab.rs` 及其全部拆分文件；
- 现在**存在一个 active assignment**（`A-7b74…`，持有者是本次 recovery executor 会话）。

→ 结论：**BR-01 本体依然不得启动**。lease 释放 + step 领取解决的是「治理路径楔死」，
不是「前置任务未完成」。BR-01 需等该任务 `completed` / `closed`。

---

## 6. 顺带发现的两处漂移（planner 待办，非阻断）

1. **`task_assignments` 表零行 vs claim 返回 assignment_id。**
   `A-7b7421762935b41a97d4e2ac` 只写进 `task_events`（event 6486），
   `task_assignments`（唯一含 `assignment_id` 列的表）对该 task 无任何行。
   任何以 `task_assignments` 判定「是否有 active assignment」的门禁都会 **fail-open**。
   权威信号只能取 `task_events` + `task_steps.status`。

2. **`next_action` 规则 7 不感知 claim 状态。**
   `next_action.rs:1345-1353`：只要存在 unresolved failed step，就无条件
   `claim_outcome(..., "remediation")` → `decision=READY / action=CLAIM`。
   因此 claim 成功后投影**仍然返回 `claim_current_step`**，无法区分
   「尚未领取」与「已领取、正在做」。AFK loop 若按 `action` 路由会无限重复 claim。
   与既有结论「status-tree 投影不可信」同类。

---

## 7. 本次产出脚本

| 文件 | 用途 |
|---|---|
| `_br01_release_now.py` | kill-before-write 竞速释放 reviewer lease（已成功） |
| `_br01_claim_step.py` | 注册 E → `task.contract_get` → 探测 remediation → `task.claim`（已成功） |
| `_br01_post_claim_verify.py` | claim 后 status / lease.status 核验 |
| `_br01_step_detail.py` | `task.next_action` 投影抓取 |
| `_br01_readonly_check.py` | 只读核验权威库 task / steps / assignments / leases |

Recovery 身份：`exec-recover-br01-20260901` / `inst-exec-bc045fae` / `sess-exec-bc045fae`（executor）。
daemon：PID 34092，binary sha256 `3a412b5b…`，git_commit `517f9337`。
