# C-16 契约裁决（卡 C step0 · adjudicate）

- **卡**：`T-1789365537146-bef4c2e4`（C-16 承接：daemon assignment_show workspace 权威解析修复）
- **step**：`S-1789365537156-bf8ea184`（step_index 0，action=adjudicate）
- **执行基线**：`C:/git_work/callwarden`，master，**HEAD = `b2056b48eb20f122303c628c31049fb6422097b1`**
- **执行身份**：`executor-wb-c16-01` / `inst-exec-wb-c16-01` / `sess-exec-wb-c16-20260914`
- **时间**：2026-09-14（+08:00）
- **性质**：**只读裁决，本 step 不改产品代码**。结论作为 step1/step2/step3 的依据。
- **裁决权**：本卡 executor（step0）。发现与既有证据冲突处**以本 HEAD 逐行复核为准**并显式登记。

---

## 1. 复核方法

三条独立证据链，全部在 **HEAD `b2056b4`** 上取得：

1. **源码逐行**：`handle_assignment_show` / `_is_task_scoped_authority_request` / `route_rpc` 注入块 / CLI 方法映射；
2. **活体复现**：对本卡真实数据（claim 时 daemon 自建的 active assignment）跑 CLI 与裸 RPC 双通道对照；
3. **契约对照**：`task_collab_shared.rs` 的权威 resolver 与同文件既有调用范式。

---

## 2. 复核结论 A · daemon 侧缺省路径（**属实，未变**）

`rust_ext/src/daemon/task_collab_lease.rs`（HEAD 下总 2102 行）：

| 位置 | 事实 |
|---|---|
| `:2003` | `pub fn handle_assignment_show(&self, _peer: PeerCredential, params: &Value)` |
| `:2005` | 形参名为 `_peer` —— **拿到 PeerCredential 却未使用**（无身份/权限判定） |
| `:2008-2011` | `let workspace_id: i64 = params.get("workspace_id").and_then(\|v\| v.as_i64()).unwrap_or(0);` ← **常量缺省 0** |
| `:2012-2021` | `task_id` / `role` 均以 `unwrap_or("")` 取，**空 task_id 不被拒绝** |
| `:2023` | `let conn = self.conn.lock().unwrap();` —— conn 在**参数校验之前**获取 |
| `:2065-2088` | SQL `WHERE workspace_id = ? AND task_id = ? [AND role = ?] AND status = 'active' ORDER BY id DESC LIMIT 1` |
| `:2090-2099` | 无命中 → `{"status":"none", "task_id":…, "role":…}` |

- `task_assignments.workspace_id` 的实际取值域为 `{1,2,3,5,6,7,8,9,10,11,17,19,20,21}`（本机库实测），**0 恒不命中** → 只要不显式传 `workspace_id`，SQL 恒空 → 恒走 `none` 分支。
- 该 handler **全程未调用** `task_bound_workspace_id`，与同文件其它 handler 的既有范式相反（见 §4）。

> 交接文档 §3.2 引用的 `:2003-2099` 行号在本 HEAD **依然准确**（本卡建卡提交只新增 deliverables 文件，未触碰 Rust 源码）。

---

## 3. 复核结论 B · 调用侧注入门禁（**关键更正：交接文档 §3.2 的更正本身不准确**）

### 3.1 事实：`route_rpc` 在 HTTP 模式下有**两个独立**注入门禁

`server/daemon_client.py`：

```python
:3919   if rpc_method not in _NO_WORKSPACE_METHODS:          # assignment_show 不在豁免集（:3865-3875 仅 5 项）
:3920       if http_enabled:
:3927           if client._project_root is None: client.configure_workspace(os.getcwd())
:3953           is_task_scoped = _is_task_scoped_authority_request(params)
:3954           if not is_task_scoped:                        # ← 门禁 A
:3956               params["workspace_instance_id"] = ws_id
:3962               params.setdefault("workspace_root", client._project_root)
:3973           if rpc_method.startswith(("task.", "lease.")) and not is_task_scoped:   # ← 门禁 B
:3977               params = _inject_workspace_id(params)
```

- **门禁 A**（`:3953-3957`）：`_is_task_scoped_authority_request()` 判定为 task-scoped 时，跳过
  **`workspace_instance_id` / `workspace_root`** 注入块；
- **门禁 B**（`:3973-3977`）：**仅当 `rpc_method` 以 `task.` / `lease.` 开头**且非 task-scoped 时，
  才调用 `_inject_workspace_id()` 注入**数值 `workspace_id`**。

`_is_task_scoped_authority_request()` 的判别实现在 `:3530-3551`：遍历 `("task_id", "superseded_id")`，
**非空白字符串即返回 True**；docstring（`:3531-3545`）明确写道该判别「**deliberately the discriminator,
rather than a hand-maintained RPC allowlist**」。

### 3.2 `assignment_show` 同时被两个门禁拦下

| 门禁 | 对 `assignment_show` 是否触发 | 后果 |
|---|---|---|
| A（`:3954`） | **触发** —— params 含非空 `task_id`（CLI 必传）→ task-scoped | 不注入 `workspace_instance_id` / `workspace_root` |
| B（`:3974`） | **不满足** —— `assignment_show` **不以 `task.` / `lease.` 开头** | 不注入数值 `workspace_id` |

CLI 侧确认只传两个键：`cli/main.py:1159`
`"get_assignment": ("assignment_show", "READ_ONLY", ("task_id","role"))`，
调用点 `cli/main.py:17696` `db.get_assignment(opts.task_id, opts.role)`。

→ 线上参数最终为 `{task_id, role}`，**两个 workspace 键都没有**。

### 3.3 更正 —— 交接文档 §3.2 把卡 A 证据 §4.1 判为「陈旧」，该判断**不成立**

- **卡 A 证据 §4.1** 写「只对 `task.` / `lease.` 前缀方法注入」：**在本 HEAD 上为真**，
  它描述的正是门禁 B（`:3973-3977`）——而且**对 `assignment_show` 而言这门禁才是数值
  `workspace_id` 缺失的直接原因**（方法名前缀不符）。
- **交接文档 §3.2** 写「通用 `task_id` 判别 → 整体跳过 workspace 注入块」：指向的是门禁 A，
  但它跳过的是 **instance/root** 注入块，**不是**数值 `workspace_id` 注入块。
- **结论**：两份文档各自描述了一个真实存在的门禁，**都不完整，也都不是「陈旧」**。
  正确表述是：`assignment_show` 的 workspace 参数缺失由**门禁 B**（前缀白名单）直接造成，
  门禁 A（通用 task_id 判别）是同时生效的第二重拦截。**不得**再用「A 陈旧 / B 正确」的框架叙述。

> 本更正须回写：`pyt_regression_step4_handoff_backlog.md` §W18 与
> `c16_c17_remediation_handoff_20260914.md` §3.2（后者为字节冻结工单，按 §5 只登记于回执）。

---

## 4. 裁决 · `server/daemon_client.py` **不改**

依据三条，任一独立成立即可：

1. **契约单源** — `:3531-3545` docstring 声明：「A task-scoped daemon method **must** resolve its
   numeric workspace through `task_workspace_bindings`. Injecting the legacy active workspace here is
   both unnecessary and unsafe in a multi-project daemon」。即**调用侧不传是设计**，缺的是 daemon 侧实现。
2. **改法反而有害** — 若把 `assignment_show` 加进门禁 B 的 `task.`/`lease.` 前缀白名单，注入的是
   **legacy active workspace**（本机实测 active workspace id 与 task binding 可能分属不同项目），
   正是上述注释所警告的 unsafe 行为；且 `_inject_workspace_id` 走 `mcp.daemon_client.inject_workspace_id`
   解析 active workspace，会与 `task_workspace_bindings` 形成两个权威源。
3. **blast radius** — 门禁 B 的前缀规则覆盖**全部** `task.*` / `lease.*` 方法；门禁 A 覆盖全部 task-scoped
   方法。改任一门禁都跨越本卡 `executor_allowed` 的语义边界与单次盲审面。

→ **本卡范围收敛为 daemon 单侧修复**（`rust_ext/src/daemon/task_collab_lease.rs`）。
`server/daemon_client.py` 已在 `allowed_paths` 内，但**裁决为不改**；若 step3 live 往返仍失败，
再回到本裁决复查（届时须以实测回执重新开裁决，不得直接改注入门禁）。

---

## 5. 修法定案（供 step1）

采用**本文件已有的范式**（`task_collab_lease.rs:1906-1912`，`handle_lease_list_events`），不引新 helper：

```rust
// 语义：task_id 必填（CLI 恒传）→ 空即 fail-closed，且必须在取 conn 之前；
//       workspace_id 未给数值 → 由不可变 task_workspace_bindings 解析；
//       显式给了 → 交 resolver 做一致性校验（不一致 → E_WORKSPACE_AUTHORITY_MISMATCH）。
let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("").to_string();
if task_id.is_empty() {
    return Err(DaemonRpcError::invalid_params(
        "assignment_show 需要非空 task_id（workspace 由不可变 binding 解析）",
    ));
}
...
let conn = self.conn.lock().unwrap();
let workspace_id = task_bound_workspace_id(&conn, &task_id, optional_workspace_id_param(params))?;
```

- `optional_workspace_id_param`（`task_collab_shared.rs:455-463`）：`None` = 未提供；筛掉 `<= 0`；兼容字符串数值。
- `task_bound_workspace_id`（`:471-507`）：无 binding → `E_TASK_WORKSPACE_UNBOUND`；显式不一致 →
  `E_WORKSPACE_AUTHORITY_MISMATCH`。**两处 fail-closed 语义由 resolver 统一提供**，handler 不再自己兜底。
- `role` 过滤、`ORDER BY id DESC LIMIT 1`、`status='active'` 过滤、`none` 返回体 **保持逐字不变**
  （append 语义下 active 过滤即已 revoke 的 assignment 自然回 `none`）。
- `_peer` 仍未使用，**本卡不动**（是否加身份校验属另一 finding，不在 C-16 scope）。

---

## 6. 验收映射与负向矩阵（step1/step2 依据）

| 合同验收 | 本裁决对应的验证方式 |
|---|---|
| ① show 返回 active 行 | live：`cw assignment show T-1789365537146-bef4c2e4 --role implementer`（CLI 通道，不传 workspace） |
| ② show→create→revoke→show 往返 | live CLI 四步（probe role，避免污染真实 implementer assignment） |
| ③ 无 binding → fail-closed | 单测：伪造无 binding 的 task_id → `E_TASK_WORKSPACE_UNBOUND`（**不得**回 `none`） |
| ④ workspace 不一致 | 单测：显式传错 `workspace_id` → `E_WORKSPACE_AUTHORITY_MISMATCH` |
| ⑤ build/test 无退化 | step3：cargo build 零 error + cargo test 与 HEAD 基线同集对照 |
| ⑥ `git diff --check` clean | step3 |
| ⑦ 提交前缀 | step3：`[T-1789365537146-bef4c2e4]`（禁复用 PYT / 卡 A id） |

负向矩阵（step2 必须落成**可执行用例**，禁止只做源码字符串断言）：

| # | 用例 | 期望 |
|---|---|---|
| 1 | 无 `workspace_id`、task **有** binding | 命中 active 行（assignment_id 逐字一致） |
| 2 | 无 `workspace_id`、task **无** binding | `E_TASK_WORKSPACE_UNBOUND` |
| 3 | `workspace_id` = 错误值 | `E_WORKSPACE_AUTHORITY_MISMATCH` |
| 4 | `task_id` 为空 | `invalid_params`（不 panic、不查 binding） |
| 5 | 已 revoke 的 assignment | `{"status":"none"}` |
| 6 | 只读性 | 调用前后 `task_assignments` 行集合与内容不变 |

---

## 7. live「修复前」回执（HEAD `b2056b4`，未部署新 binary）

| 项 | 值 |
|---|---|
| daemon 端点 | `http://127.0.0.1:11338`（`cw daemon health` 回执） |
| daemon pid | `34852` |
| daemon `git_commit` | `29e0f99715d73ffcb1a817cfc3153041adf20f67`（**早于** HEAD） |
| live binary | `C:/Users/wanpi/.callwarden/runtime/current/cw-daemon.exe`，len `45735424`，SHA256 `BE67915CBC5D4641AE3FBC255AC160D21ADA8D791B163CB98B0A0ED19DD3DF92`（与交接文档 §8 基线一致） |
| 活体夹具 | `task_assignments.id=450`，`assignment_id=A-8efb4d6a242692d9031d2349`，task_id=`T-1789365537146-bef4c2e4`，role=`implementer`，status=`active`（**本卡 claim 时 daemon 自建**，非人工造） |

对照实测：

```
# 通道 1：CLI（等价 RPC 不带 workspace_id）
$ cw assignment show T-1789365537146-bef4c2e4 --role implementer
  Active Assignment / assignment_id: None / task_id: T-1789365537146-bef4c2e4 / role: implementer

# 通道 2：裸 RPC，不带 workspace_id（复刻 CLI 线上参数 {task_id, role}）
assignment_show {"task_id": "...", "role": "implementer"}
  → {"status": "none", "task_id": "T-1789365537146-bef4c2e4", "role": "implementer"}

# 通道 3：裸 RPC，显式 workspace_id=1
assignment_show {"task_id": "...", "role": "implementer", "workspace_id": 1}
  → {"id": 450, "workspace_id": 1, "assignment_id": "A-8efb4d6a242692d9031d2349", "task_id": "...",
     "role": "implementer", "agent_id": "S-1-5-21-...-1001", "session_id": "sess-exec-wb-c16-20260914",
     "model_id": "deepseek-v4.1-flash", "status": "active", "created_at": 1789365863.7313807, "revoked_at": null}
```

→ **同一份数据、三条通道、结论分歧**：通道 1/2 报无 assignment，通道 3 命中同一 active 行。
C-16 现象在 HEAD 上**完整复现**，且夹具为 daemon 自建、可信。

---

## 8. 未做与限制（诚实披露）

1. **本 step 未改任何产品代码**（裁决性质）。
2. **未部署 runtime**：live binary 仍为 `29e0f99` 时代（早于 HEAD，**且早于** C-14/C-15 修复 `158432b`）。
   因此上文 live 回执**同时混合了 C-14 与 C-16 两条缺陷的效应**；C-16 的「修复后」验收必须在 step3
   部署新 binary 后重取，**不得**用本次回执冒充修复后结果。
3. **未跑 cargo build / cargo test**（属 step1/step3）。
4. **未改 `server/daemon_client.py`**，且**未**在未实测前调整任何注入门禁。
5. **本次 live 复现未创建/撤销任何 assignment**，未写入业务数据（仅只读查询）。
6. 与既有证据的差异已登记：§3.3（交接文档 §3.2 的更正更正）、§2（交接文档行号仍准确）。
