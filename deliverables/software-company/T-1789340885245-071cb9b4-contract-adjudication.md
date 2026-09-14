# T-1789340885245-071cb9b4 step0 契约单源裁决：`assignment_revoke`

- **task_id**: `T-1789340885245-071cb9b4`
- **step_id**: `S-1789340885248-074dfe98`（step_index 0，action=adjudicate）
- **executor identity**: `executor-pytreg-01` / `sess-exec-pytreg-01-6c9613cd` / `trae-agent-executor` / `inst-exec-pytreg-01-6c9613cd`
- **lease**: `L-fdb1065d0d53dec9`（implementer，fencing_counter=2）
- **内容**: C-14 / C-15 的契约单源裁决；本 step 不改产品代码
- **日期**: 2026-09-14

---

## §0 裁决结论摘要

| 编号 | 议题 | 裁决 |
|------|------|------|
| D1 | `admin.assignment_revoke` 入参单源 | **以 `assignment_id` 为准**（必填）。Rust `require_str_param(params,"task_id")` 为唯一异类，删除 |
| D2 | 是否保留 `task_id` 兼容回退 | **不保留**。文档面从无 `task_id` 契约；保留会产生二义性与误撤销风险 |
| D3 | 撤销语义 | `UPDATE ... SET status='revoked', revoked_at=? WHERE workspace_id=? AND assignment_id=? AND status='active'`（逐字对齐 Python 权威） |
| D4 | 返回体 | `{ok:true, assignment_id, revoked_at}`；不再返回 `task_id`/`role`/`reason` |
| D5 | 失败形态 | `changed==0` → `assignment_not_found`，detail 含 `assignment_id` |
| D6 | 是否新增 `assignment_id → task_id` 反查 RPC | **不需要**（四项理由见 §2.6） |
| D7 | C-14 修复口径 | `admin.assignment_create` INSERT 补 `assignment_id` 列，写 `ASG-<16hex>`；返回值用该字符串，不用 `last_insert_rowid()` |
| D8 | id 命名空间 | 本卡只修 admin/manual 路径；**不统一**列级命名空间（governance claim 路径的 `A-<24hex>` 保持不动，越界） |
| D9 | 新发现 C-16 | `assignment_show` positive 分支对**全部生产调用方**不可达 → 出本卡边界，另建承接卡 |
| D10 | 卡内往返验收口径 | 按 **daemon RPC 层**（显式 `workspace_id`）在 `tests/` 验真；MCP/CLI wrapper 面因 C-16 仍为 `none`，如实记录 |

---

## §1 事实基线（五面契约 + 实现偏离 + 实跑回执）

### 1.1 契约面：五处一致指向 `assignment_id`

| # | 面 | 位置 | 契约原文 |
|---|----|------|----------|
| ① | MCP tool schema | `mcp_callwarden/tools/assignment_revoke.json` | `properties: {assignment_id: string}`；`required: ["assignment_id"]`；doc 首行 `assignment_id: Assignment ID（ASG-xxx）`；`Returns: {ok: True, assignment_id, revoked_at}` |
| ② | 文档（MCP） | [mcp_tools.md](file:///c:/git_work/callwarden/docs/mcp_tools.md#L1972) | `assignment_revoke \| assignment_id \| {ok: True, assignment_id, revoked_at} \| 撤销 assignment（append 语义，不删除记录）` |
| ③ | 文档（CLI） | [cli_reference.md](file:///c:/git_work/callwarden/docs/cli_reference.md#L3251) | `cw assignment revoke <assignment-id>` |
| ④ | CLI 契约表 | [main.py](file:///c:/git_work/callwarden/cli/main.py#L1268) | `"revoke_assignment": ("admin.assignment_revoke", "GOVERNANCE_WRITE", ("assignment_id",))` |
| ⑤ | Python 权威语义 | [db_task_leases.py](file:///c:/git_work/callwarden/db/db_task_leases.py#L297-L322) | `revoke_assignment(assignment_id, workspace_id=None)`；`WHERE workspace_id=? AND assignment_id=? AND status='active'`；返回 `_ok(assignment_id=..., revoked_at=now)` |

CLI 实参侧亦一致：[main.py](file:///c:/git_work/callwarden/cli/main.py#L17681-L17719) `revoke_p.add_argument("assignment_id", ...)` + `db.revoke_assignment(opts.assignment_id)`；
MCP 注册侧亦一致：[tools_p4_lease.py](file:///c:/git_work/callwarden/server/tools/tools_p4_lease.py#L216-L228) `def assignment_revoke(assignment_id: str)` → `_route('admin.assignment_revoke', {"assignment_id": assignment_id}, 'PROTECTED_MUTATION')`。

### 1.2 实现面：唯一异类

[admin_handlers.rs](file:///c:/git_work/callwarden/rust_ext/src/daemon/admin_handlers.rs#L577-L600) `handle_assignment_revoke`：

```rust
let task_id = require_str_param(params, "task_id")?;          // ← 与 ①-⑤ 全部冲突
let role = get_str_param_or(params, "role", "implementer");   // ← 文档无此参数
let reason = get_str_param_or(params, "reason", "");          // ← 文档无此参数
... WHERE workspace_id = ?2 AND task_id = ?3 AND role = ?4 AND status = 'active'
Ok(json!({ "ok": true, "task_id": task_id, "role": role, "reason": reason }))  // ← 与 ①-⑤ 返回体冲突
```

C-15 由此定案：**五面 vs 一面，单源取五面**。

### 1.3 C-14 事实（create 侧）

[admin_handlers.rs](file:///c:/git_work/callwarden/rust_ext/src/daemon/admin_handlers.rs#L557-L573) `handle_assignment_create`：

```rust
INSERT INTO task_assignments (workspace_id, task_id, role, agent_id, session_id, model_id, status, created_at)
VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active', ?7)      // ← 缺 assignment_id 列
let assignment_id = conn.last_insert_rowid();      // ← 整数，非 ASG-<hex16> 字符串
```

而 [schema.py](file:///c:/git_work/callwarden/db/schema.py#L1626-L1639)：

```sql
CREATE TABLE IF NOT EXISTS task_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    assignment_id TEXT NOT NULL UNIQUE,        -- assignment 唯一标识（ASG-<uuid>）
    task_id TEXT NOT NULL, role TEXT NOT NULL, agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL, model_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL, revoked_at REAL DEFAULT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);
```

→ `NOT NULL constraint failed: task_assignments.assignment_id` 必然命中。

Python 权威生成式（[db_task_leases.py:236](file:///c:/git_work/callwarden/db/db_task_leases.py#L236)）：`assignment_id = f"ASG-{uuid.uuid4().hex[:16]}"`；INSERT 显式写该列（:240-247）；返回含 `assignment_id/task_id/role/agent_id/session_id/model_id/created_at`（:256-264）。

### 1.4 「前」实跑回执（本 step 采集，逐字）

```
=== BEFORE #1: cw assignment create T-1789340885245-071cb9b4 --role tester --agent-id probe-c14 --session-id probe-c14 --model-id probe ===
✗ Subcommand 'assignment' failed: internal_error: assignment_create: NOT NULL constraint failed: task_assignments.assignment_id
RC=2

=== BEFORE #2: cw assignment revoke ASG-no-such ===
✗ Subcommand 'assignment' failed: invalid_params: 缺少字段: task_id
RC=2

=== BEFORE #3: cw assignment show T-1789340885245-071cb9b4 --role executor --json ===
{ "status": "none", "task_id": "T-1789340885245-071cb9b4", "role": "executor" }
RC=0
```

`#1` 即 C-14 根因；`#2` 即 C-15 根因（按文档契约传 `assignment_id` 必失败）；`#3` 见 §1.6。

### 1.5 列级双命名空间事实（重要、影响 D8）

`task_assignments.assignment_id` 列当前由**两个不同生产者**写入，格式不同：

| 生产者 | 生成器 | 格式 | 写入点 |
|--------|--------|------|--------|
| admin/manual（`cw assignment create` → `admin.assignment_create`；Python `db.create_assignment`） | Python `uuid4().hex[:16]`；Rust 侧当前**无生成器**（C-14 缺陷） | `ASG-<16hex>` | `db_task_leases.py:236`；Rust handler 待补 |
| governance claim（`task.claim` / `report` 补偿写） | `assignment_queue::assignment_id_for` | `A-<sha256[..24]>`（24 hex） | [assignment_queue.rs:75-92](file:///c:/git_work/callwarden/rust_ext/src/daemon/assignment_queue.rs#L75-L92) 定义；[:481-499](file:///c:/git_work/callwarden/rust_ext/src/daemon/assignment_queue.rs#L481-L499) `persist_claimed_assignment` 落库 |

**实证**：本任务 `task_assignments` 现存 active 行 `id=440`，其 `assignment_id='A-52848dbb809cc676f6deebe6'`——该行由 governance claim 路径写入（C-14 决定了 admin 路径当前根本无法写行，故不可能是 admin 路径产物）。

结论：**「列级单源格式」这一表述不成立**；schema 注释 `ASG-<uuid>`（[schema.py:1629](file:///c:/git_work/callwarden/db/schema.py#L1629)）描述的是 admin/manual 契约面。本卡只对 admin/manual 面负责。

### 1.6 新发现 C-16：`assignment_show` positive 分支对生产调用方不可达

`handle_assignment_show`（[task_collab_lease.rs:2003-2011](file:///c:/git_work/callwarden/rust_ext/src/daemon/task_collab_lease.rs#L2003-L2011)）：

```rust
let workspace_id: i64 = params.get("workspace_id").and_then(|v| v.as_i64()).unwrap_or(0);  // ← 缺省 0，非绑定解析
```

而生产调用方**从不传数值 `workspace_id`**：

- MCP tool schema `assignment_show.json` 只声明 `task_id` / `role`（FastMCP 按 [`tools_p4_lease.py:202`](file:///c:/git_work/callwarden/server/tools/tools_p4_lease.py#L202) 类型签名生成 schema，多传的键被丢弃）；
- [daemon_client.py:3918-3977](file:///c:/git_work/callwarden/server/daemon_client.py#L3918-L3977) `route_rpc` 仅在 `rpc_method.startswith(("task.", "lease."))` 时调用 `_inject_workspace_id`；`assignment_show` 不满足该前缀 → 数值 `workspace_id` 永不注入；
- CLI 契约表亦只声明 `("task_id", "role")`（[main.py:1159](file:///c:/git_work/callwarden/cli/main.py#L1159)）。

**探针回执（只读，直连 daemon RPC）**：

```
--- with workspace_id=1 ---
{'id': 440, 'workspace_id': 1, 'assignment_id': 'A-52848dbb809cc676f6deebe6',
 'task_id': 'T-1789340885245-071cb9b4', 'role': 'executor', ... , 'status': 'active', 'revoked_at': None}
--- without workspace_id (production caller shape) ---
{'status': 'none', 'task_id': 'T-1789340885245-071cb9b4', 'role': 'executor'}
```

即：**daemon RPC 层 positive 分支本身可用**（显式传 `workspace_id` 即命中），但**生产包装层（MCP schema / CLI 契约表 / `route_rpc` 注入白名单）永不给这个参数** → `assignment_show` 恒返回 `none`。

**测试盲区**：[test_mcp_assignment_show_http_rpc.py:36-80](file:///c:/git_work/callwarden/tests/test_mcp_assignment_show_http_rpc.py#L36-L80) 5 个用例**全部**是 negative 分支（`NO-SUCH-TASK` / `workspace_id=999999`），且显式传 `workspace_id=1`——既掩盖了调用方不传参数的事实，也从未覆盖命中分支。

**为何不在本卡修**：修复需动 `rust_ext/src/daemon/task_collab_lease.rs`（handler 数值 workspace 解析）与/或 `server/daemon_client.py`（注入白名单）；两者**均不在**本卡 `executor_allowed` 内。故记为独立 finding **C-16**。

---

## §2 逐条裁决与理由

### D1 / D2：`assignment_revoke` 入参 = `assignment_id`（必填，无 `task_id` 回退）

理由：
1. 契约面 5/5（§1.1 ①②③④⑤）一致为 `assignment_id`；Rust 实现 1/6 为异类 → 单源判定无争议。
2. 保留 `task_id` 回退会引入二义性：同一 task 可能存在多条 active 行（§2.8），按 `task_id+role` 撤销会命中**非调用方所指**的那一行，属静默误撤销，违反 append-only 审计语义。
3. CLI/MCP 两侧调用方已按 `assignment_id` 实现完毕，回退分支无任何调用方。

### D3：撤销语义（逐字对齐 Python）

```sql
UPDATE task_assignments SET status = 'revoked', revoked_at = ?1
WHERE workspace_id = ?2 AND assignment_id = ?3 AND status = 'active'
```

`workspace_id` 由 daemon 权威解析（[snapshot_state.rs:3195-3222](file:///c:/git_work/callwarden/rust_ext/src/daemon/snapshot_state.rs#L3195-L3222)：`admin.assignment_revoke` 属 `require_str_param(params,"workspace_instance_id")` → `owned_workspace`），与 Python 的 `_get_active_workspace_id()` 语义等价，无需 task 反查。

### D4 / D5：返回体与失败形态

- 成功：`{ok:true, assignment_id, revoked_at}`（对 ① 的 `Returns`、② 的返回列、⑤ 的 `_ok(assignment_id=..., revoked_at=now)` 逐字一致）。移除 `task_id` / `role` / `reason`。
- 失败：`changed == 0` → `DaemonRpcError::new("assignment_not_found", ...)`，detail 须含 `assignment_id`（对齐 ⑤ 的 `detail=f"assignment_id={assignment_id} 不存在或已撤销"` 与 `assignment_id=` 回带）。

### D6：不新增 `assignment_id → task_id` 反查 RPC

四项理由：
1. **契约无需求**：①-⑤ 的全部面都只需 `assignment_id`，无任何面提出反查。
2. **机制上无必要**：daemon 已用 `workspace_instance_id` 独立解析数值 `workspace_id`，撤销条件 `(workspace_id, assignment_id)` 完备，无需经 task 中转。
3. **权威面没有**：Python `db.revoke_assignment` 无任何反查步骤。
4. **越界成本**：新增 RPC 需改路由（`dispatch.rs` / `snapshot_state.rs`）与 `task_collab_lease.rs`，均不在 `executor_allowed`。
→ **step2 的「assignment_show 反查」判定为不需要**，step2 实现范围收敛为 `handle_assignment_revoke` 单函数。

### D7：C-14 修复口径（`ASG-<16hex>`）

- INSERT 补 `assignment_id` 列，与 Python `db_task_leases.py:240-247` 的列清单逐字对齐；
- 生成器：Rust 侧现无 `ASG-` 生成器（全仓 grep `ASG-` 仅命中 schema/CLI 帮助/docs/Python，`rust_ext/src` 无实现）。可用既有依赖实现，**不引入新 crate**（`rust_ext/Cargo.toml` 已有 `sha2 = "0.10"`、`getrandom = "0.3"`、`hex = "0.4"`；无 `uuid`）：
  - **推荐**：`getrandom::fill(&mut [u8;8])` + `hex::encode` → 16 hex（熵特征对齐 Python 的 `uuid4`）；API 先例见 [role_worker.rs:203-209](file:///c:/git_work/callwarden/rust_ext/src/daemon/task_loop/role_worker.rs#L203-L209)。
  - **可接受备选**：复用 `gen_lease_id` 同款模式 `format!("ASG-{}", &sha256_hex(format!("{}:{}", now_ts(), rand_val()).as_bytes())[..16])`（先例 [task_collab_shared.rs:376-382](file:///c:/git_work/callwarden/rust_ext/src/daemon/task_collab_shared.rs#L376-L382)；`sha256_hex` 为 `crate::canonicalize::sha256_hex`，`rand_val()` 经 `daemon::task_collab` 重导出）。
- 返回值：该字符串（**不得**为 `last_insert_rowid()` 整数）；其余字段与 Python `_ok(...)` 的 7 字段一致。

### D8：不统一列级命名空间

仅修 admin/manual 面；`assignment_queue.rs` 的 `A-<24hex>` 生产者保持不动。理由：本卡 `executor_allowed` 不含 `assignment_queue.rs`；且两命名空间均为 `TEXT NOT NULL UNIQUE` 的「稳定标识符」，两生产者并存是既有事实（§1.5），统一它属于独立的架构收口议题。**本卡与文档均不得宣称「该列单源格式为 ASG-」。**

### D9：C-16 出界披露

见 §1.6。记录为独立 finding，另建承接卡；本卡不改 `task_collab_lease.rs` / `server/daemon_client.py` / `cli/main.py`。建议承接卡范围（供后续建卡参考，本卡不落库）：`handle_assignment_show` 改为按 `task_bound_workspace_id(&tx, task_id, optional_workspace_id_param(params))` 解析（先例见同文件 :1257 / :1479 / :1649 / :1810），并补 positive 分支测试。

### D10：卡内往返验收口径

acceptance 的「show → create → revoke 往返可用」按 **daemon RPC 层（显式 `workspace_id`）** 验真，落在 `tests/`（在 `executor_allowed` 内）：

| 序 | 动作 | 期望 |
|----|------|------|
| 1 | `assignment_show{workspace_id, task_id, role}` | `status: none` |
| 2 | `admin.assignment_create{...}` | `assignment_id` 匹配 `^ASG-[0-9a-f]{16}$`，非整数 |
| 3 | `assignment_show{workspace_id, task_id, role}` | 命中，且 `assignment_id` == 步骤 2 返回值 |
| 4 | `admin.assignment_revoke{assignment_id}` | `{ok:true, assignment_id, revoked_at}` |
| 5 | `assignment_show{...}` | `status: none` |
| 6 | `admin.assignment_revoke{assignment_id}`（重放） | `assignment_not_found` |

同时**如实记录**：MCP/CLI wrapper 面在步骤 3/5 仍为 `none`（C-16），该差异属 C-16 而非本卡失败。

### D11（附带事实，不改行为）：`create` 不去重

Python `db.create_assignment` 是**裸 INSERT**，不撤销既有 active 行；同一 `(task, role)` 重复 create 会产生多条 active 行，`assignment_show` 取 `ORDER BY id DESC LIMIT 1`。本卡**对齐 Python 不改**（一致性优先于「看起来更干净」）。step3 用例须避免依赖「唯一 active」假设。

---

## §3 对后续 step 的硬约束

| step | 约束 |
|------|------|
| step1 `handle_assignment_create` | 按 D7：INSERT 补列 + `ASG-<16hex>` + 返回字符串；不得改动列清单外的字段顺序/语义 |
| step2 `handle_assignment_revoke` | 按 D1-D5；**单函数**范围（D6）；append 语义不删行 |
| step3 `tests/` + docs | 按 D10 六步往返；同步核对 [mcp_tools.md:1968-1972](file:///c:/git_work/callwarden/docs/mcp_tools.md#L1968-L1972) / [cli_reference.md:3245-3257](file:///c:/git_work/callwarden/docs/cli_reference.md#L3245-L3257) 与实现逐字一致。**风险点**：[test_cli_011_http_rpc.py:25,42](file:///c:/git_work/callwarden/tests/test_cli_011_http_rpc.py#L25) 现断言 `assignment_id: 5`（整数形态），与 D7 的 `ASG-<hex16>` 字符串契约存在潜在冲突，step3 必须评估并处置 |
| step4 `release_verify` | `cargo build` 零 error；`cargo test` 未退化；`git diff --check` clean；commit 前缀 = `T-1789340885245-071cb9b4` |

---

## §4 诚实披露（出界/未覆盖）

1. **C-16 未修**（§1.6）：`assignment_show` positive 分支对生产调用方不可达，根因字面落在 `task_collab_lease.rs` 与 `server/daemon_client.py`，**均不在本卡 `executor_allowed`**。本卡 acceptance 的 `show` 腿仅能在 daemon RPC 层（显式 `workspace_id`）验真。
2. **列级命名空间未统一**（§1.5）：本卡落地后，该列仍同时存在 `ASG-<16hex>`（admin 面）与 `A-<24hex>`（governance claim 面）两种格式。**任何文档/验收不得声称该列格式已单源化。**
3. **本 step 未改产品代码**（符合 step0 check_items 第 2 条）；§1.4/§1.6 回执为修复前基线。
4. **D6 未做反查**：`assignment_id → task_id` 反查能力在裁决后**不存在**；若后续出现「仅持 assignment_id 需定位 task」的需求，须另立卡。
5. 本裁决的 `docs/` 引用行号为**当前工作区**行号，step3 同步文档后需以实际内容为准。

---

## §5 证据索引

| 证据 | 位置 |
|------|------|
| MCP revoke schema | `mcp_callwarden/tools/assignment_revoke.json:1-17` |
| MCP create/show schema | `mcp_callwarden/tools/assignment_create.json`、`assignment_show.json` |
| Rust create（C-14 落点） | [admin_handlers.rs:546-574](file:///c:/git_work/callwarden/rust_ext/src/daemon/admin_handlers.rs#L546-L574) |
| Rust revoke（C-15 落点） | [admin_handlers.rs:577-600](file:///c:/git_work/callwarden/rust_ext/src/daemon/admin_handlers.rs#L577-L600) |
| schema 定义 | [schema.py:1626-1639](file:///c:/git_work/callwarden/db/schema.py#L1626-L1639) |
| Python 权威 create/revoke | [db_task_leases.py:236-322](file:///c:/git_work/callwarden/db/db_task_leases.py#L236-L322) |
| governance id 生成器 | [assignment_queue.rs:75-92](file:///c:/git_work/callwarden/rust_ext/src/daemon/assignment_queue.rs#L75-L92)、[:481-499](file:///c:/git_work/callwarden/rust_ext/src/daemon/assignment_queue.rs#L481-L499) |
| show handler（C-16 根因） | [task_collab_lease.rs:1999-2087](file:///c:/git_work/callwarden/rust_ext/src/daemon/task_collab_lease.rs#L1999-L2087) |
| workspace 注入白名单 | [daemon_client.py:3918-3980](file:///c:/git_work/callwarden/server/daemon_client.py#L3918-L3980) |
| admin 路由/whitelist | [snapshot_state.rs:3191-3222](file:///c:/git_work/callwarden/rust_ext/src/daemon/snapshot_state.rs#L3191-L3222) |
| CLI 契约与实参 | [main.py:1159](file:///c:/git_work/callwarden/cli/main.py#L1159)、[:1266-1268](file:///c:/git_work/callwarden/cli/main.py#L1266-L1268)、[:17681-17719](file:///c:/git_work/callwarden/cli/main.py#L17681-L17719) |
| 测试盲区 | [test_mcp_assignment_show_http_rpc.py:36-80](file:///c:/git_work/callwarden/tests/test_mcp_assignment_show_http_rpc.py#L36-L80) |
| id 生成先例 | [task_collab_shared.rs:376-390](file:///c:/git_work/callwarden/rust_ext/src/daemon/task_collab_shared.rs#L376-L390) |
| CSPRNG 先例 | [role_worker.rs:203-209](file:///c:/git_work/callwarden/rust_ext/src/daemon/task_loop/role_worker.rs#L203-L209) |
| 依赖可用性 | `rust_ext/Cargo.toml`：`sha2:97`、`getrandom:129`、`hex:121` |
