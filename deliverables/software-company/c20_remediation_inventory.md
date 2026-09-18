# C-20 step0 盘点：F2+F3 缺陷复核与同型扫描（强制前置，未改代码）

- **卡**：`T-1789397153198-ee7baf18`（C-20，父 `T-1788871227327-45c94bd8`，backlog §W20 F2+F3）
- **盘点 HEAD**：`e07fbec`（扫描时工作树干净，`rust_ext/src/daemon/admin_handlers.rs` 自 `e2a2853` 未再改动）
- **性质**：只读盘点。本 step 未修改任何代码，产出仅本文档。

---

## 1. 缺陷实证（行号为 HEAD 实测，非照抄 backlog/卡面）

### F2 · `admin.record_action_identity` 缺 `action_identities.action_id`

- handler：`rust_ext/src/daemon/admin_handlers.rs:660` `handle_record_action_identity`
- 缺陷语句：`admin_handlers.rs:674-690`

```rust
// admin_handlers.rs:675-676
"INSERT INTO action_identities (workspace_id, action_type, task_id, contract_id, contract_revision, agent_id, session_id, model_id, role, recorded_at)
 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)"
```

- 缺列：`action_id`（10 列全无）
- 返回：`{"ok": true, "recorded_at": now}`（`:691`）—— 不含 `action_id`

### F3 · `admin.register_attestation_revocation` 缺 `attestation_revocation_records.revocation_id`

- handler：`admin_handlers.rs:695` `handle_register_attestation_revocation`
- 缺陷语句：`admin_handlers.rs:713-714`

```rust
// admin_handlers.rs:713-714
"INSERT INTO attestation_revocation_records (workspace_id, issuer, signing_key_id, revocation_mode, revocation_reason, initiating_actor, revoked_at)
 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)"
```

- 缺列：`revocation_id`（7 列全无）

### schema 实证（`db/schema.py`，本卡零改动）

| 表 | CREATE 行 | 缺列 DDL | 语义注释 |
|---|---|---|---|
| `action_identities` | `db/schema.py:1546` | `db/schema.py:1549` `action_id TEXT NOT NULL UNIQUE` | `-- action 唯一标识（ACT-<uuid>）` |
| `attestation_revocation_records` | `db/schema.py:1589` | `db/schema.py:1592` `revocation_id TEXT NOT NULL UNIQUE` | `-- 撤销记录唯一标识（REV-<uuid>）` |

→ 两列均 `NOT NULL` 且无 `DEFAULT`，handler 不提供即恒 `NOT NULL constraint failed`。

---

## 2. id 生成范式锚（两条先例，均实测原文）

**先例 A（Rust 侧，卡 A/C-14..C-15 交付）** —— `admin_handlers.rs:33-39`：

```rust
/// `task_assignments.assignment_id` 为 `TEXT NOT NULL UNIQUE`（db/schema.py:1629），
/// 且文档/CLI/MCP 三面对外契约为 `ASG-xxx`。Rust 侧无 uuid crate，改用
/// OS CSPRNG 8 byte → 16 hex，熵特征与 Python `uuid4` 等价（先例见
/// `task_loop/role_worker.rs` 的 credential 签发）。
fn gen_assignment_id() -> Result<String, DaemonRpcError> {
    let mut entropy = [0_u8; 8];
    getrandom::fill(&mut entropy).map_err(...)?;
    Ok(format!("ASG-{}", hex::encode(entropy)))
}
```

**先例 B（Python 权威实现，`db/` 侧契约）**：

- `db/db_task_identity.py:609`：`revocation_id = f"REV-{uuid.uuid4().hex[:16]}"`（**内部生成**，无调用方入参）
- `db/db_task_identity.py:170-203`：`record_action_identity` 的 `action_id` **由调用方提供**入参，
  列序 `(workspace_id, action_id, action_type, task_id, contract_id, contract_revision, agent_id, session_id, model_id, role, recorded_at)`；
  UNIQUE 冲突 → `ERR_IDENTITY_ACTION_DUPLICATE`
- 调用方实际 id 形态：`db/db_tasks.py:1705/2561/2826/2986` → `ACT-report-<step_id>-<ms>` / `ACT-apply-<task_id>-<ms>` / `ACT-close-…` / `ACT-reopen-…`

---

## 3. 修法设计（本 step 只决策，step1 实施）

| 项 | 决策 | 依据 |
|---|---|---|
| `action_id` | **优先取调用方入参 `action_id`**（对齐 Python 契约，UNIQUE 幂等语义可保留）；入参为空时 Rust 生成 `ACT-<hex16>` | `db_task_identity.py:170-203` + 先例 A 的 `hex16` 熵形 |
| `revocation_id` | **Rust 内部生成 `REV-<hex16>`**，不接受调用方入参 | 严格镜像 `db_task_identity.py:609` |
| 生成实现 | 复用先例 A 形态：`getrandom` 8 byte → `hex::encode` → `format!("ACT-{}"/"REV-{}", …)`；抽为 `gen_action_id()` / `gen_revocation_id()`（与 `gen_assignment_id` 同文件同范式） | Rust 侧无 uuid crate |
| 列序 | 与 Python 权威实现一致（`action_id` 紧跟 `workspace_id`） | 跨语言契约一致 |
| 返回体 | `record_action_identity` 增返 `action_id`；`register_attestation_revocation` 增返 `revocation_id` | 调用方可回执审计 |
| 不改 | `db/**`、schema、其它 admin handler、路由层 `snapshot_state.rs` | 卡合同边界 |

---

## 4. 迁移范围（F2/F3 锚；F1 禁碰）

`tests/test_c17_admin_route_workspace_authority.py`（HEAD 实测行号）：

- `:627` `test_record_action_identity_f2_known_defect_still_fails` —— 断言「调用恒失败」
  （`_admin(client, "admin.record_action_identity", {...})` 后 `assert err is not None`）
- `:638` `test_register_attestation_revocation_f3_known_defect_still_fails` —— 同形态
- **禁碰**：C-19 已迁移的 F1 正例/隔离负例（同文件 `test_gc_audit_get_*` / `test_gc_audit_list_*`）

---

## 5. 同型扫描（rust_ext/src 全部 INSERT 缺 NOT NULL UNIQUE 列）

- 解析 `INSERT INTO <t> (<cols>) VALUES` 语句 **364 条**（跨 `rust_ext/src/**/*.rs`，含测试模块）
- 按「列在 `db/schema.py` 中为 `NOT NULL` + `UNIQUE` 且未出现在 INSERT 列清单」筛选 → **9 条原始命中**
- 逐条判定：

| 命中 | 判定 | 依据 |
|---|---|---|
| `admin_handlers.rs:675` `action_identities.action_id` | ✅ **生产缺陷（F2，本卡修）** | handler 生产写路径 |
| `admin_handlers.rs:713` `attestation_revocation_records.revocation_id` | ✅ **生产缺陷（F3，本卡修）** | handler 生产写路径 |
| `workspace_reconciliation.rs:596` `workspaces.name/root_path` | ❌ 误报 | 测试夹具 `INSERT INTO workspaces (id) VALUES (1)`（见 `:594-598` 注释「task-DB 侧…绑定 instance」） |
| `job_executor_handlers.rs:212` `workspaces.name/root_path` | ❌ 误报 | 同文件 `:208` 测试内 `CREATE TABLE IF NOT EXISTS workspaces (id INTEGER PRIMARY KEY)`（DDL 非生产 schema） |
| `query_compat_handlers.rs:3353` `workspaces.name` | ❌ 误报 | `:3351` `let conn = temp_db();` |
| `cli/runtime.rs:500`（×2）`workspaces.root_path` | ❌ 误报 | `:495-497` 测试内建表（含 `is_active`，非生产 schema） |

- **结论**：`rust_ext/src` 生产写路径中「缺 NOT NULL UNIQUE 列」的缺陷**仅 F2/F3 两处**，无第三处同型缺陷。
- 另注（不在本卡 scope，只登记）：`db_tasks.py` 等 Python 侧写入均显式提供 `action_id`，无同型缺陷；
  `db/db_base.py:2596` 的 `action_id TEXT NOT NULL UNIQUE` 与 `db/schema.py:1549` 同义，未漂移。

---

## 6. 验证计划（step2-step4）

1. **A/B 判别力**：以修复前二进制（当前部署 `e2a2853` 产物）跑迁移后的测试 → F2/F3 正例**如实失败**
   （`NOT NULL constraint failed`）；修复后二进制 → 全绿。
2. **正例**：隔离 daemon 调两方法成功；只读核查落库行 `action_id` 以 `ACT-` 前缀、`revocation_id` 以 `REV-` 前缀，
   全字段读回；重复调用生成不同 id（UNIQUE 不撞）。
3. **负例保留**：F1 正例/隔离负例不受影响；ACL 门禁不受影响。
4. **部署门禁**（step4）：`refresh_shared_runtime.ps1 -TaskId 本卡 task_id`、`health.git_commit==HEAD`、
   三方 sha256 一致、PID、`rollback=false`；生产**只读**核查两表行数与最新行 id 形态，
   并披露「写路径缺陷不做生产合成写 probe」。
