# NF1 修复盘点（step0 / adjudicate）— T-1789436398881-877c169c

> 卡：NF1 承接：gate.resolve_findings SQL 引用不存在列 tasks.workspace_id 修复（§W20 F1 同族，C-21 期新发现）
> 纪律：行号本 HEAD 实测（HEAD = `4f0f765`）；本 step 不改任何代码。

## 1. 缺陷点复核（本 HEAD 实测）

`rust_ext/src/daemon/edit_handlers.rs` `handle_resolve_gate_findings`（fn 声明 **L227**）：

```rust
// L235-241（实测）
let changed = conn
    .execute(
        "UPDATE task_gate_decisions SET reason = ?1, decision_time = ?2
         WHERE decision_id = ?3 AND task_id IN (SELECT id FROM tasks WHERE workspace_id = ?4)",  // L237-238 ← 坏列
        rusqlite::params![resolution, now, gate_id, workspace_id],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("resolve_gate_findings: {e}")))?;  // L241
if changed == 0 {
    return Err(DaemonRpcError::new("gate_not_found", format!("gate {gate_id} 不存在")));   // L242-244
}
Ok(json!({ "ok": true, "gate_id": gate_id, "resolution": resolution }))                    // L245
```

坏列引用在 **L237-238**：`SELECT id FROM tasks WHERE workspace_id = ?4` —— tasks 表没有
workspace_id 列。**缺陷实际形态（step2 A/B 实测修正 C-21 登记推断）**：C-21 登记为
「prepare 恒失败」，实测**并非 prepare 炸**——SQLite correlated name resolution 把子查询
中的未知列 `workspace_id` **静默解析为外层 UPDATE 目标表 task_gate_decisions 的同名列**
（task_gate_decisions 有 schema 预留的 workspace_id 列，权威库 261 行全 NULL，见 §2）→
子查询恒空集 → `task_id IN (空集)` → `changed == 0` → **gate_not_found 静默失败**
（L242-244）。单跑子查询 `SELECT id FROM tasks WHERE workspace_id = 1` 才报 no such column
（无外层可回退）；最小复现（task_gate_decisions 无 workspace_id 列的两表内存库）亦炸。
即：生产上该方法自上线起恒返回 gate_not_found（与其余 gate_not_found 语义混淆，可诊断性
更差）。修复后 `changed == 0 → gate_not_found` 分支恢复**真实语义**（gate 不存在或
task 未绑定该 workspace）。

## 2. PRAGMA 实证（权威库 `~/.callwarden/callwarden.db`，只读）

- **tasks**（12 列）：`id, title, description, creator, status, created_at, updated_at,
  applied_at, closed_at, parent_id, depth, sort_order` —— **无 workspace_id**。
- **task_workspace_bindings**：`task_id, workspace_id, workspace_binding_id,
  workspace_capture_id, created_by, authoritative_created_at` —— (task_id, workspace_id)
  权威映射在列（`task_collab_shared.rs::task_bound_workspace_id` 同源语义；task binding
  不可变，`task_collab_lease.rs` L2032 注释「由不可变 task_workspace_bindings 解析」）。
- **task_gate_decisions**：有 `workspace_id` 列（schema 预留）与 `task_id`、`decision_id`、
  `reason`、`decision_time` 等。**权威库实况：261 行中 workspace_id NOT NULL = 0 行
  （全部 NULL），task_id NOT NULL = 261 行（全填充）**。

## 3. 修复方向裁决：同表 workspace_id 过滤不可行，C-19 先例 join 是唯一正确修法

step0 评估过更小的修复（`WHERE decision_id = ?3 AND workspace_id = ?4`，直接用
task_gate_decisions 自带的 workspace_id 列），**排除**，依据：

该列在全部三个写入路径中从未被填充真实值：
1. `edit_handlers.rs` L270-274（`handle_run_check_gate` 的 INSERT）——列清单不含 workspace_id；
2. `task_collab_evidence.rs` L556-572（runtime_task_gate 决策 INSERT）——显式写 `NULL`
   （VALUES 尾项 `?7, NULL`）；
3. `sqlite_query.rs` L1258（迁移测试夹具）——不含该列。

加上 §2 的 261 行 0 非空实证 → 同表过滤永远命中 0 行 → `gate_not_found` 误判（换一种方式
复现同一缺陷语义）。**裁决：修法 = C-19 先例套用**：

```sql
UPDATE task_gate_decisions SET reason = ?1, decision_time = ?2
WHERE decision_id = ?3
  AND task_id IN (SELECT task_id FROM task_workspace_bindings WHERE workspace_id = ?4)
```

对照先例 `admin_handlers.rs` `handle_gc_audit_get`（C-19 修复后形态，L160-193）：
`FROM change_audit WHERE id = ?1 AND task_id IN (SELECT task_id FROM task_workspace_bindings
WHERE workspace_id = ?2)` —— 同款子查询、同参数位置语义（?2/?4 = daemon workspace id i64，
与 handler 签名 `workspace_id: i64` 同域）。`handle_gc_audit_list`（L196-208）的 JOIN 形态亦为
同族参照。修复后 `changed == 0 → gate_not_found` 语义**保持**（gate_id 不存在或 task 未绑定
该 workspace 都走该分支——fail-closed，不区分两种 0 行原因，与 C-19 形态一致）。

## 4. C-21 NF1 锚复核（迁移范围确认）

`tests/test_c21_edit_rule_route_workspace_authority.py` **L739-757**：

- `test_gate_resolve_findings_nf1_known_defect(c21_daemon)`（L749），xfail(strict=True,
  reason L742-746）正例体：`_edit(client, "gate.resolve_findings", {"gate_id":
  "GATE-c21-nf1", "resolution": "resolved"})` → `assert err is None`（L756）→
  `assert res.get("ok") is True`（L757）。
- `_edit` helper（L370-375）：任何 `DaemonRemoteError` 都计为 err（含 gate_not_found）。

**迁移要求（step2 执行，本 step 仅登记）**：删除 L739-747 的 xfail 标记（保留正例体断言）。
但仅删标记**不足以转绿**——实测夹具无预置 gate decision 行，也无 task/binding 种子
（全文无 `INSERT INTO task_workspace_bindings`/`INSERT INTO tasks`；`GATE-c21-nf1` 全文
仅 L754 一处出现）：修复后 UPDATE 经 bindings 子查询命中 0 行 → `gate_not_found` →
err 非 None → 测试仍失败。**迁移时须补种子三件套**：tasks 行（task X）+
task_workspace_bindings 行（X → 夹具 workspace）+ task_gate_decisions 行
（decision_id='GATE-c21-nf1', task_id=X），并补库内效果断言（reason='resolved' 落行）。
种子方式对齐同文件既有 seed 形态（L173 起 symbols seed / L680-694 run_check 用
task_id='T-C21-GATE' 直写夹具库的先例）。NF2 锚（L717-736）**禁碰**（属 NF2 卡）。

## 5. 同型扫描

`rust_ext/src` 全量（os.walk 剪枝 target* 目录）扫描
`FROM tasks WHERE workspace_id | JOIN tasks…workspace_id | tasks.workspace_id`：

- **唯一残留**：`edit_handlers.rs:238`（本卡缺陷点）。
- C-19 已修复的 `admin_handlers.rs` 两处（gc_audit_get L169 子查询 / gc_audit_list L206
  JOIN）现均为 task_workspace_bindings 形态，不再命中。
- 结论：NF1 = tasks.workspace_id 同族的最后残留；本卡修复后该族归零。

Python 侧（db/、server/）扫描：`db_gc.py` 的 gc_audit_* 走 gc_runs 表（C-19 inventory 已留
排除依据，与本缺陷无关）；gate 决策无 Python 侧写面。

## 6. 路由层零触碰确认

`gate.resolve_findings` 的路由臂：`dispatch.rs`（route 注册）+ `snapshot_state.rs` 第二路由块
匹配臂——两者已在 **C-21**（T-1789397153231-f07a8d84）整体收口至 `open_codegraph_db_write`
路由层单点，回执见 backlog §W20 F4。本卡只改 handler 内 SQL 字符串，**零触碰**
dispatch.rs / snapshot_state.rs / db/** / scripts/**。

## 7. 修复后行为预期（step1 实测目标）

| 场景 | 修复前（实测） | 修复后 |
|---|---|---|
| gate 存在 + task 绑定该 workspace | gate_not_found（子查询经 correlated 解析恒空） | ok=true + 库内 reason/decision_time 更新 |
| gate 不存在 | gate_not_found（同形态，无法区分） | gate_not_found（真实语义） |
| gate 存在但 task 未绑定该 workspace | gate_not_found（同形态） | gate_not_found（fail-closed，跨 workspace 不可见） |
| 未绑定 task 的隔离负例 | 不可判别（同形态） | 不可见（负例断言可行） |

before 腿实测（修复前二进制 4EA587D3… + 已迁移正例 + 种子四行）：1F/20P/1xf ——
`test_gate_resolve_findings_nf1_fixed` 失败且错误文本恰为 `gate_not_found: gate
GATE-c21-nf1 不存在`，与 §1 修正后的缺陷形态判断吻合；其余 20 正例 + NF2 xfail 零扰动。

## 8. 验证计划（step2/step3 落地）

- C-21 矩阵 22 测试（`.venv_test` 解释器 + `PYTHONPATH=C:/git_work`）：NF1 锚转绿、NF2 锚
  保持 xfail、其余 20 例零回归；
- cargo build 零 error；cargo test 同集对照零新增失败；
- 隔离 daemon 前后实测：before（internal_error: no such column）/ after（ok=true + 种子行
  更新 + gate_not_found 负例）；
- 部署门禁（step3）：`scripts/refresh_shared_runtime.ps1 -TaskId T-1789436398881-877c169c`
  → health.git_commit==HEAD + 三方 sha256 + PID + rollback=false；生产只读 probe
  （gate.resolve_findings 修复前恒 internal_error → 部署后结构化响应；无自然写触发时
  披露「生产无合成写 probe，功能证明=隔离矩阵」）。
