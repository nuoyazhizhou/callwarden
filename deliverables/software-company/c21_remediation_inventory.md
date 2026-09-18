# C-21 step0 盘点：`edit.*/gate.*/rule.*` 第二路由块代理 workspace id 缺陷

- **卡**：`T-1789397153231-f07a8d84`（C-21，backlog §W20 F4 承接）
- **父卡**：`T-1788871227327-45c94bd8`（PYT 回归出界缺陷）
- **盘点 HEAD**：`15c18a8c58ec78ce16c009ccd99866473223fc02`（2026-09-15）
- **盘点对象**：`rust_ext/src/daemon/snapshot_state.rs`（9252 行 / 392558 bytes）、
  `rust_ext/src/daemon/edit_handlers.rs`、`db/schema.py`
- **盘点性质**：**只登记，不改任何代码**（step0 强制前置）

> ⚠️ 纪律：本盘点全部行号、方法清单、表结构与 `workspace_id` 用法均为 **HEAD 实测**，
> 未照抄 backlog §W20 或卡描述中的行号（`3262 邻域`）与方法数（`19 方法`）。
> 实测结果：19 方法（数量与卡描述一致），实际行号 3266-3305。

---

## 1. 第二路由块：HEAD 实测清单

匹配臂位于 `handle_convergence_rpc` 内，**行 3266-3272**（臂头），块体 **行 3273-3305**。

| # | 方法名 | handler（`edit_handlers.rs` 行） | 路由行 | `workspace_id` 用法（实测） | 判别力 |
|---|---|---|---|---|---|
| 1 | `edit.propose` | `handle_propose_edit`（48） | 3285 | `INSERT INTO file_edit_audit(workspace_id,…)`（`FK → workspaces(id)`） | **类 1**（FK 失败） |
| 2 | `edit.propose_range_patch` | `handle_propose_range_patch`（65） | 3286 | 同上（经 `record_edit_audit`） | **类 1** |
| 3 | `edit.propose_symbol_id_patch` | `handle_propose_symbol_id_patch`（82） | 3287 | `SELECT … WHERE fi.workspace_id = ?2` 先查后写 | **类 2**（`symbol_not_found`） |
| 4 | `edit.propose_symbol_patch` | `handle_propose_symbol_patch`（107） | 3288 | 同族（按符号名查 `file_instances.workspace_id`） | **类 2** |
| 5 | `edit.revert` | `handle_revert_edit`（123） | 3289 | `UPDATE file_edit_audit … WHERE id=? AND workspace_id=?` | **类 2**（`edit_not_found`） |
| 6 | `edit.restore_all_comments` | `handle_restore_all_comments`（143） | 3290 | `SELECT … WHERE fi.workspace_id = ?1` | **类 2**（恒 0 行） |
| 7 | `edit.restore_comment` | `handle_restore_comment`（~185） | 3291 | 同族 | **类 2** |
| 8 | `edit.record_token_savings` | `handle_record_token_savings`（200） | 3292 | `INSERT INTO token_savings_ledger(…, workspace_id, …)`（`FK → workspaces(id)`，schema 实测） | **类 1**（FK 失败） |
| 9 | `gate.resolve_findings` | `handle_resolve_gate_findings`（227） | 3293 | `task_id IN (SELECT id FROM tasks WHERE workspace_id = ?4)` —— **`tasks` 表无 `workspace_id` 列** | ⚠️ **独立缺陷 NF1**（prepare 失败，与本卡修复**无关**） |
| 10 | `gate.run_check` | `handle_run_check_gate`（249） | 3294 | 写 `task_gate_decisions`（**无** workspace_id 列） | 不可判别 |
| 11 | `rule.seed_bootstrap` | `handle_rule_seed_bootstrap`（280） | 3295 | `let _ = (workspace_id, source.clone());` | 不可判别 |
| 12 | `rule.extract_candidates` | `handle_extract_rule_candidates`（308） | 3296 | `WHERE file_instance_id IN (SELECT id FROM file_instances WHERE workspace_id = ?1)` | **类 2**（恒 0 候选） |
| 13 | `rule.candidate_accept` | `handle_rule_candidate_accept`（~385） | 3297 | 未使用（更新 `agent_rule_candidates`） | 不可判别 |
| 14 | `rule.candidate_create` | `handle_rule_candidate_create`（356） | 3298 | `let _ = workspace_id;` | 不可判别 |
| 15 | `rule.candidate_reject` | `handle_rule_candidate_reject`（417） | 3299 | `workspace_root(conn, workspace_id)` → `SELECT root_path FROM workspaces WHERE id=?` | **类 2**（恒「查询 workspace root 失败」） |
| 16 | `rule.insert_agents_md_block` | `handle_rule_insert_agents_md_block`（439） | 3300 | 同上 `workspace_root` | **类 2** |
| 17 | `rule.sync_agents_md` | `handle_rule_sync_agents_md`（481） | 3301 | 同上 `workspace_root` | **类 2** |
| 18 | `guardrail.add_rule` | `handle_guardrail_add_rule`（553） | 3302 | `let _ = workspace_id;`（写 `guardrail_rules`，无 workspace_id 列） | 不可判别 |
| 19 | `summary.generate` | `handle_summary_generate`（~585；臂内 `_ =>` 兜底） | 3303 | `SELECT … WHERE fi.workspace_id = ?1`，后写 `symbol_summaries` | **类 2**（恒 generated=0） |

**合计 19 方法**。判别力分布：**类 1（FK 写失败）3 个**、**类 2（WHERE/查根不命中）10 个**、
**不可判别 5 个**、**独立缺陷 NF1 1 个**。

### 1.1 缺陷机制（HEAD 实测代码，行 3273-3283）

```rust
let ws = require_str_param(params, "workspace_instance_id")?;
let workspace = super::workspace::owned_workspace(
    &self.base.registry,
    peer.uid,
    ws,
)?;
let workspace_id = workspace
    .get("workspace_id")
    .and_then(Value::as_i64)
    .ok_or_else(|| DaemonRpcError::internal_error("workspace_id 缺失".to_string()))?;
let conn = open_write(self, ws)?;
```

`owned_workspace(...).workspace_id` 是 **daemon registry 代理 ROWID**（本机实例 = 207），
与物理库 `workspaces.id`（本机 = 1；值域 {1,2,3,5,6,7,8,9,10,11,17,19,20,21}）
**不是同一命名空间** —— 与 C-17 admin 路由块**同根因、同形态**。

### 1.2 连接差异复核

| | `open_write`（行 2767-2777，局部闭包） | `open_codegraph_db_write`（行 285-330） |
|---|---|---|
| ACL | 无（调用方已做 `owned_workspace`） | 内部 `owned_workspace` 做 ACL |
| 打开方式 | `Connection::open(db)` | `open_with_flags(READ_WRITE \| NO_MUTEX \| URI)` + `PRAGMA busy_timeout=5000` |
| 路径解析 | `codegraph_db(state, ws)` 模板替换 | 同模板（空模板回落 `default_codegraph_db_path()`） |
| 返回 | `Connection` | `(真 workspace_id, Connection)` |

**结论**：两者解析到**同一物理库**，唯一差异是 `workspace_id` 取值。修复只换取值来源即可，
不改变落库目标。

---

## 2. 正确形态对照（同文件既有锚）

| 锚 | 行 | 形态 |
|---|---|---|
| **admin 路由块**（C-17 已收口） | 3215 | `let (workspace_id, conn) = self.open_codegraph_db_write(peer, ws)?;` |
| **semgrep 写面块**（C-13 接线；紧邻正确形态锚） | 3250 | 同上 |
| **semgrep 读面**（`get_semgrep_summary`） | 3261 | `let (workspace_id, conn) = self.open_query_connection(peer, ws)?;` |

第二路由块（3273-3283）是**唯一**仍用「`owned_workspace` 代理 ROWID + `open_write`」组合的路由块。

---

## 3. 同型扫描：是否存在第三路由块

全文件 `snapshot_state.rs` 扫描（实测）：

- `owned_workspace(` 出现 45 处（`owned_workspace_by_id` 另 13 处）；其中**取 `.workspace_id`
  作 handler 作用域**的仅 3 处：行 3168（`task.job_submit`）、行 3274（**本卡目标**）、
  行 3210 起（admin 块，C-17 已修，仅存于注释）。
- `open_write(` 调用点全文件**仅 1 处**：行 3283（即本卡目标）。
- `open_codegraph_db_write(` 使用点：2243 / 2412 / 2461 / 2568 / 2685 / 3215 / 3250（均已收口形态）。

### 3.1 第三处同型（**只登记，本卡不修**）

```rust
// 行 3166-3179
"task.job_submit" => {
    let ws = require_str_param(params, "workspace_instance_id")?;
    let workspace = super::workspace::owned_workspace(&self.base.registry, peer.uid, ws)?;
    let workspace_id = workspace.get("workspace_id").and_then(Value::as_i64)...;
    let db = codegraph_db(self, ws)?;
    job::rpc_job_submit(workspace_id, ws, db, params)
}
```

`task.job_submit` 与 admin 块 / 第二路由块**同源同形态**（代理 ROWID 传入 `rpc_job_submit`）。
本卡验收条款明确「第二路由块单点收口」+「admin 路由块与其它路由块零触碰」，
故**此处只登记、不顺手修改**，作为后续卡候选。

---

## 4. handler 对 `conn` / `workspace_id` 用法复核（零 handler 改动可行性）

全部 19 个 handler 签名统一：

```rust
pub fn handle_xxx(conn: &Connection, workspace_id: i64, params: &Value) -> Result<Value, DaemonRpcError>
```

- `conn` 全部为 **`&Connection`** 只读借用；`open_codegraph_db_write` 返回**拥有所有权**的
  `Connection`，路由层以 `&conn` 传入即可 —— **签名完全兼容，handler 层零改动可行**。
- `workspace_id` 用法四类（对照 C-17 分类并扩展）：
  - **类 1（带 FK 的写）**：`edit.propose` / `edit.propose_range_patch`（`file_edit_audit`）、
    `edit.record_token_savings`（`token_savings_ledger`）—— 两表 `workspace_id` 均有
    `FOREIGN KEY … REFERENCES workspaces(id)`（schema 实测）→ 代理 id 恒 FK 失败。
  - **类 2（WHERE 过滤 / 查根）**：`edit.revert`、`edit.propose_symbol_*_patch`、
    `edit.restore_*_comments`、`rule.extract_candidates`、`rule.candidate_reject`、
    `rule.insert_agents_md_block`、`rule.sync_agents_md`、`summary.generate`
    → 代理 id 恒不命中（`edit_not_found` / `symbol_not_found` / 0 行 / `workspace_root` 查询失败）。
  - **不可判别**：`gate.run_check`、`rule.seed_bootstrap`、`rule.candidate_accept`、
    `rule.candidate_create`、`guardrail.add_rule` —— 或 `let _ = workspace_id;`，
    或写入表无 `workspace_id` 列；**修复前后行为一致**，不能作判别锚。
  - **独立缺陷 NF1**：`gate.resolve_findings`。

### 4.1 ⚠️ 新发现 NF1：`gate.resolve_findings` 引用不存在的列

`edit_handlers.rs:227 handle_resolve_gate_findings` 的 SQL：

```sql
UPDATE task_gate_decisions SET resolution = ?1, resolved_at = ?2
 WHERE decision_id = ?3 AND task_id IN (SELECT id FROM tasks WHERE workspace_id = ?4)
```

`db/schema.py` 的 `tasks` 表**没有 `workspace_id` 列**（实测：列集 `id/title/description/
creator/status/created_at/updated_at/applied_at/closed_at/parent_id/depth/sort_order`）
→ prepare **恒失败**，与 §W20 **F1 同源**（C-19 已修 `admin.gc_audit_get/list` 的同一错误模式，
但 `gate.resolve_findings` 不在 C-19 范围）。

- **与本卡关系**：NF1 **不是**代理 id 缺陷，本卡的路由层收口**修不好它**（换成真 id 后
  SQL 仍 prepare 失败）。
- **处置**：本卡**只登记**（NF1），不改 handler（本卡验收硬约束为「handler 层零改动」）；
  step2 隔离矩阵对该方法**如实标注为 known-defect 锚（保持红）**，不以任何方式伪装为绿。
- **建议**：由后续承接卡按 C-19 同款修法（改经 `task_workspace_bindings` join）处理。

---

## 5. 修复设计（step1 待执行）

**单点收口**：将行 3273-3283 的「`owned_workspace` + 取值 + `open_write`」替换为与
admin/semgrep 块逐字同形的一行：

```rust
let ws = require_str_param(params, "workspace_instance_id")?;
let (workspace_id, conn) = self.open_codegraph_db_write(peer, ws)?;
```

**不变项**（验收硬约束）：

- 方法名列表 / match 臂（行 3266-3272）**零改动**；
- 19 个 handler 调用（行 3284-3304）**零改动**（仅 `workspace_id` 取值来源变化）；
- admin 路由块（3186-3239）、semgrep 块（3246-3263）、`task.job_submit`（3166-3179）**零触碰**；
- `db/**` **零触碰**。

**副作用与处置（预先登记）**：`open_write` 闭包（行 2767-2777）在移除其**唯一**调用点
（行 3283）后变为未使用 → 触发 `unused` 警告。处置：在该 `let` 上加 `#[allow(unused)]`
并附 C-21 说明注释（**不删除闭包**，保持 diff 最小；且 `codegraph_db` 闭包仍被行 3177
`task.job_submit` 使用，不受影响）。

---

## 6. step2 隔离矩阵选靶（基于本盘点）

判别力锚（隔离环境预置 registry dummy 行占 ROWID=1，使真实行 = 2 = 代理 id；真 id = 1）：

| 类别 | 方法 | 修复前（代理 id=2） | 修复后（真 id=1） |
|---|---|---|---|
| 类 1 | `edit.propose` | `FOREIGN KEY constraint failed` | 写入成功，`file_edit_audit.workspace_id == 1` |
| 类 1 | `edit.record_token_savings` | `FOREIGN KEY constraint failed` | 写入成功，`token_savings_ledger.workspace_id == 1` |
| 类 2 | `edit.revert` | `edit_not_found` | `status == 'reverted'`，受影响行 = 1 |
| 类 2 | `rule.candidate_reject` / `rule.sync_agents_md` | 「查询 workspace root 失败」 | 成功（root 解析到真实 workspace） |
| 类 2 | `summary.generate` | `generated == 0` | 命中真实 workspace 的 symbols |
| 不可判别（按卡验收仍纳入正例） | `rule.candidate_create` / `guardrail.add_rule` | 成功 | 成功（**明确标注不具判别力**） |
| NF1（保持红） | `gate.resolve_findings` | prepare 失败 | **仍 prepare 失败**（如实标注） |

外加：跨 workspace 隔离负例（他主 workspace_instance_id → ACL 拒绝），
以及 `n_ws == 1` 判别力断言（与 C-17/C-20 同夹具范式）。

---

## 7. step0 结论

| 项 | 结论 |
|---|---|
| 第二路由块方法数 | **19**（实测，行 3266-3305） |
| 缺陷确认 | 是（与 C-17 admin 块同根因：代理 ROWID + `open_write`） |
| 具判别力方法 | 13（类 1 × 3、类 2 × 10） |
| 不具判别力 | 5（`gate.run_check`、`rule.seed_bootstrap`、`rule.candidate_accept`、`rule.candidate_create`、`guardrail.add_rule`） |
| 独立新发现 | **NF1**：`gate.resolve_findings` 引用不存在的 `tasks.workspace_id`（F1 同源）→ 只登记 |
| 第三路由块 | 存在（`task.job_submit`，行 3166-3179）→ 只登记 |
| handler 层改动 | **不需要**（签名 `(&Connection, i64, &Value)` 完全兼容） |
| 本 step 代码改动 | **无** |

## 8. step2 A/B 实测判别力修订与 NF2 登记（2026-09-15）

step2 隔离矩阵 A/B 实测（before = 部署二进制 `BD3AD671…` 无本卡修复；after =
隔离 `target-c21` 构建 `44117CBD…` 含本卡修复）对 step0 静态分类做了两处机械修订：

1. **`edit.restore_comment` / `rule.candidate_reject` 判别力修订**：step0 按 handler
   家族静态归入类 2；机械复验确认二者 SQL **不消费 workspace_id**（`let _ =
   workspace_id;` / `let _ = (workspace_id, reason);`），新旧实现均通过 →
   实际归入「不可判别」（不可判别集由 5 扩至 **7**，严格判别集 13 → **10**）。
2. **NF2 新发现（step2 A/B 实测）**：`summary.generate` 的 upsert
   `ON CONFLICT(symbol_hash) DO UPDATE`（edit_handlers.rs:612）在权威 schema
   （db/schema.py `symbol_summaries`）上**无匹配 PRIMARY KEY/UNIQUE 约束**
   （仅有非唯一索引 `idx_summaries_hash`）→ workspace 存在函数符号时恒
   `internal_error: symbol_summaries upsert: ON CONFLICT clause does not match
   any PRIMARY KEY or UNIQUE constraint`。修复前该缺陷被路由缺陷**双重掩盖**
   （代理 id WHERE 恒空 → 从不触达 upsert，静默返回 `generated=0`）；路由修复后
   真实暴露。结论：**summary.generate 自上线起从未端到端可用**。
   处置：与 NF1 同款纪律——本卡只登记不修（edit_handlers.rs / db/** 均不在本卡
   allowed_paths），测试以 `xfail(strict)` 锚记录，转绿须由独立缺陷卡承接
   （修法二选一：DDL 加 UNIQUE 索引，或 handler 改为显式 DELETE+INSERT / 先查后写）。

### A/B 实测汇总（最终口径：两腿测试代码逐字同一份，22 测试；计数取 junitxml 权威值）

| 腿 | 二进制 | 结果 | 失败集 |
|---|---|---|---|
| before | `BD3AD671…`（C-19+C-20，无本卡修复） | **10 failed / 10 passed / 2 xfailed**（exit=1） | 恰为 10 个严格判别锚（3×FK constraint failed、edit_not_found、2×symbol_not_found、restored=0、extracted=0、2×查根 Query returned no rows、NF2 旧形态静默 0）；NF1/NF2 锚按设计 xfail |
| after | `44117CBD…`（隔离 target-c21，含本卡修复） | **0 failed / 20 passed / 2 xfailed**（exit=0） | 无失败；NF1/NF2 两个已知缺陷锚按设计 xfail |

环境披露：pytest 控制台输出在本环境管道下尾部计数行被截断（短摘要 FAILED 行可见、
最终计数行缺失），两腿均以 `--junitxml` 落盘计数为准（tests/failures/errors/skipped
= 22/10/0/2 与 22/0/0/2）。

