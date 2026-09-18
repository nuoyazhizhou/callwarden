# C-24 缺陷复核与归一化边界厘清（step0·adjudicate，不改代码）

> 卡：`T-1789572547537-1bc143b4`（C-24，父 `T-1788871227327-45c94bd8`）
> 来源：C-23 卡 `T-1789564402123-9b47d988` A′ 收尾实证，证据见
> `c23_remediation_inventory.md` §5.3。
> 复核基线：HEAD `e023048`（docs-only；代码基线等同 `f59890c` 部署态）。
> 所有行号均为本 HEAD 实测，未照抄交接文档。

## 1. 缺陷形态：canonical 归一化的「定义存在、边界缺失」

`canonical_claim_role`（`task_collab_shared.rs:328`，`pub(crate)`）已声明归一化意图：

```rust
// task_collab_shared.rs:328-335
pub(crate) fn canonical_claim_role(role: &str) -> &str {
    match role.trim() {
        "executor" | "planner" | "implementer" | "tester" | "evidence" => "executor",
        "reviewer" | "independent_reviewer" => "reviewer",
        "adjudicator" => "adjudicator",
        other => other,   // 未知角色透传（不编造）
    }
}
```

但 lease 的**存储**与**查找**两条路径都绕过了它，只有「比较」路径用它。

### 1.1 存储侧（两处，字面落库）

| 位置 | 代码 | 效果 |
|---|---|---|
| `task_loop/lifecycle_lease.rs:187-188` | `AcquireInput::from_params` 取 `params["role"]` 原文入结构 | runtime role（如 `implementer`）未经归一直接进入领域层 |
| `task_collab_lease.rs:1360-1368` | `INSERT INTO task_leases (…, role, …) VALUES (…, ?4, …)`，`params![…, role, …]` | **持久化的是字面 role** |

fencing 聚合键同源（`task_collab_lease.rs:1349-1351`）：
`SELECT COALESCE(MAX(fencing_counter),0) FROM task_leases WHERE workspace_id=?1 AND task_id=?2 AND role=?3`
—— 同样按字面 role 聚合，`implementer` 与 `executor` 各算一条 fencing 序列。

### 1.2 查找侧（三处，字面匹配）

三处 `check_lease` / 恢复查询都是 `WHERE … AND role = ?` 字面匹配：

| 位置 | SQL 片段（实测） |
|---|---|
| `task_loop/report_handoff.rs:690-695` | `SELECT lease_id, token_hash, … FROM task_leases WHERE task_id=?1 AND role=?2 AND status='active' ORDER BY id ASC LIMIT 1` |
| `task_loop/verdict_evidence_gate.rs:686-690` | 同上（verdict.submit 的 reviewer lease 重检） |
| `task_loop/lifecycle_lease.rs:496-502` | `SELECT id, lease_id, … FROM task_leases WHERE workspace_id=?1 AND task_id=?2 AND role=?3 AND status='active' ORDER BY id ASC LIMIT 1`（`recover_orphaned_lease_for_task`） |

调用点传入的是 identity 原始 role，未归一：
- `report_handoff.rs:461-470` `check_lease(tx, &input.task_id, &input.acting_role, …)`
- `verdict_evidence_gate.rs:456-465` 同款，`&input.acting_role`

注意：`report_handoff.rs:453` 与 `verdict_evidence_gate.rs:447` 的 identity 比较用了
`runtime_role(&input.acting_role)`，但**比较用完就丢**，查询仍用原值。

### 1.3 校验侧（白名单只收治理角色）

```rust
// task_collab.rs:2143 / 2176
const ALLOWED_TARGET_ROLES: &[&str] = &["executor", "reviewer", "adjudicator"];
…
.filter(|value| ALLOWED_TARGET_ROLES.contains(value))
```

→ 历史存成 `implementer` 的 lease，`lease.recover` 连参数校验都过不了（`invalid_params`）；
改传 `executor` 则通过校验但查库无行（`E_LEASE_NOT_FOUND`，§1.2）。

### 1.4 已正确归一化的两处（语义保持，不动）

| 位置 | 用途 |
|---|---|
| `task_collab_lease.rs:416-417` | claim 接管的 old/new role 比较（`canonical_claim_role` 两边都过） |
| `task_collab_lifecycle.rs:207` | 合同角色归一（`canonical_claim_role(&id.role)`） |

## 2. 实证后果（A′ 链上实际命中）

C-23 A′ 收尾期间，executor lease 以 `implementer` 存储，随后：

| 操作 | 结果 |
|---|---|
| `task handoff --role executor` | `E_LEASE_NOT_FOUND`（查 `role='executor'` 无行；实际存 `implementer`） |
| `lease.recover --role executor` | `E_LEASE_NOT_FOUND`（过校验、查无行） |
| `lease.recover --role implementer` | `invalid_params`（不在 ALLOWED_TARGET_ROLES） |
| 绕过 | 直连 daemon `lease.acquire` 触发孤儿回收（holder 无 `agent_registrations` 行） |

即：**同一 lease 在「查得到」与「允许恢复」两个判据上互相矛盾**，recover 通道对
runtime-role lease 完全不可达。只能靠 acquire 的孤儿回收兜底。

## 3. 边界厘清：`runtime_role` vs `canonical_claim_role`

| | `canonical_claim_role` | `runtime_role`（4 处私有重复） |
|---|---|---|
| 定义点 | `task_collab_shared.rs:328`（`pub(crate)`，单份） | `inbound_handoff.rs:127` / `next_action.rs:85` / `report_handoff.rs:258` / `verdict_evidence_gate.rs:240`（四份同体拷贝） |
| 用途 | runtime role → **治理角色**（存储/查找/比较的权威归一） | acting_role → **治理角色**（identity 比较专用） |
| 未知角色 | 透传 `other` | 映 `""`（fail-closed） |
| 本次修法 | **用这个** | 不动（identity 比较语义保持） |

`runtime_role` 的四份重复属同型重复缺陷（与 `canonical_claim_role` 体近同），**本卡只登记不修**（超出边界）。

## 4. 可见性核验（跨模块引用可行性）

三处待修文件现有 import 形如 `use crate::daemon::dispatch::DaemonRpcError;`
（`report_handoff.rs:35` / `verdict_evidence_gate.rs:31` / `lifecycle_lease.rs:31`），
同 crate 根可达 → 引用路径
`crate::daemon::task_collab_shared::canonical_claim_role` 成立，无需改可见性。

## 5. task_leases 全量 role 过滤盘点（应改 / 不改 分类）

逐条实证（`SELECT … FROM task_leases` 全部站点）：

**应归一化（字面 role 参数，跨名称会 miss）**
- `task_collab_lease.rs:1263 / 1349 / 1486 / 1657 / 1753 / 1817`（acquire/renew/release/extend/status 族，参数化 role）
- `task_loop/lifecycle_lease.rs:338 / 500 / 972 / 1044 / 1162 / 1310 / 1378`（含本卡 §1.2 三处）
- `task_loop/report_handoff.rs:692`、`task_loop/verdict_evidence_gate.rs:688`
- `dispatch` 层 `assignment_queue.rs:463`（`SELECT role FROM task_leases`，读投影）
- `task_collab.rs:2406`（`SELECT session_id FROM task_leases`）

**业务语义确为 reviewer（不改，需在卡内论证）**
- `task_collab_lifecycle_ops.rs:307 / 425`：`role='reviewer'` 硬编码 —— reviewer 是治理角色，
  `canonical_claim_role("reviewer")=="reviewer"` 且 `runtime_role("independent_reviewer")=="reviewer"`；
  但 `independent_reviewer` 存库时会落字面值 → **这两处反而需要归一化**（否则 independent_reviewer
  lease 查不到）。归为「应改」。
- `bootstrap_review_bridge.rs:370 / 379`：`role='reviewer'` + `DELETE` —— 同上，归「应改」。
- `task_supersede.rs:473 / 815`：`SELECT lease_id FROM task_leases …`（supersede 的 reviewer
  lease 查找）—— 同上，归「应改」。
- `bootstrap_review_bridge_test.rs` / `lifecycle_lease_test.rs` / `task_collab_tests_lease.rs`：测试内
  断言，随实现同步调整。

**结论**：硬编码 `role='reviewer'` 的站点在「存储归一」后自然正确（存的就是 `reviewer`）；
在「查找归一」前对历史 `independent_reviewer` 行不兼容。故修法采取**双端归一**：
存储侧归一（新行全按治理角色）+ 查找侧归一（覆盖历史行），硬编码 reviewer 站点在双端归一下
等价正确，无需改动。

## 6. 同型扫描（task_leases 之外）

- `task_assignments` / `task_events` 的 role 字段：assignment 的 role 由 claim/lease 链写入，
  与 lease 同源；本卡只修 lease 侧，assignment 侧是否需要同款归一化**只登记不修**。
- `runtime_role` 四份重复：登记（§3）。

## 7. 修法定案（step2 实施依据）

**双端归一**，最小切面：

1. **存储侧**：在 `AcquireInput::from_params`（`lifecycle_lease.rs:187`）解析时即归一
   `role`（最早切面，覆盖全部下游 INSERT + fencing 聚合，单点改动）。
2. **查找侧**：三处 `check_lease` / `recover_orphaned_lease_for_task` 在构造查询前归一 role
   （兼容历史 `implementer`/`planner`/`tester`/`evidence`/`independent_reviewer` 行）。
3. **recover 白名单**（`task_collab.rs:2176`）：对归一化后的 role 判定
   （`canonical_claim_role(value)` 后再 `contains`），使历史 runtime-role lease 可恢复。
4. claim 接管比较（`task_collab_lease.rs:416-417`）与合同角色（`task_collab_lifecycle.rs:207`）
   既有归一语义不变。

**不做**：不迁移历史数据（查找侧归一已兼容）；不改 db schema；不改 server/**；
不动 `runtime_role` 重复；不改 `scripts/refresh_shared_runtime.ps1`。
