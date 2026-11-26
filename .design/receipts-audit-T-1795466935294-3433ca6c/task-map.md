# task-map — receipts 审计唯一追溯映射

- **feature**：receipts 审计：build_graph 成功/失败/转后台均落可验证审计回执
- **R 阶段**：R4 建单 / 追溯收口
- **生成时间**：2026-11-24 15:32:19
- **依据 HEAD**：`1fbcd9bad21d58b7ed76e7e1710883213d5495cc`
- **任务**：`T-1795466935294-3433ca6c`
- **说明**：本文件是 REQ ↔ 卡片 ↔ task_id ↔ AC ↔ 证据的**唯一**映射源；其他工件只引用，不重复维护映射表

## 1. 追溯矩阵（REQ ↔ 卡片 ↔ AC ↔ task_id）

| REQ | contract 卡片 | AC | task_id / step_id | 证据 |
|---|---|---|---|---|
| REQ-01 成功回执 | CARD-1 | AC-01 | `T-1795466935294-3433ca6c` / S-1795466935297-34636a9c（step 0） | evidence/AC-01.md |
| REQ-02 失败回执 | CARD-1 | AC-02 | 同上 / S-1795466935297-34636a9c | evidence/AC-02.md |
| REQ-03 超时转后台回执 | CARD-1 | AC-03 | 同上 / S-1795466935297-34636a9c + S-1795466935297-3463c53c | evidence/AC-03.md |
| REQ-04 链完整 | CARD-2 | AC-04 | 同上 / S-1795466935297-3463c53c（step 1） | evidence/AC-04.md |
| REQ-05 读面 + 幂等 | CARD-3 | AC-05 | 同上 / S-1795466935297-3463c53c | evidence/AC-05.md |
| REQ-06 既测不回归 | CARD-4 | AC-06 | 同上 / S-1795466935297-34640f10（step 2） | evidence/AC-06.md |

> 说明：daemon 卡原始 description 未使用 `REQ-xx` 编号。本表将 6 条验收目标归一为 REQ-01~06 作为 planner 侧追溯锚（G6.5 / description 首行约定）；这些 REQ 号是本设计文档的内部追溯标识，供后续若需建整改单时作为 `REQ-xx` 首行使用，不改变 daemon 既有任务卡。

## 2. 冻结 task 与 step 清单（daemon 权威，planner 不改动）

| task_id | step_id | step_index | action | target_file | target_symbol | status |
|---|---|---|---|---|---|---|
| T-1795466935294-3433ca6c | S-1795466935297-34636a9c | 0 | 在 build_flight.finish 与 run_build_graph 出口写入审计回执 | rust_ext/src/daemon/build_flight.rs | BuildFlights::finish | pending |
| T-1795466935294-3433ca6c | S-1795466935297-3463c53c | 1 | 回执写入复用 audit_log handler，并保证链完整性 | rust_ext/src/daemon/fs_handlers.rs | handle_build_graph_async | pending |
| T-1795466935294-3433ca6c | S-1795466935297-34640f10 | 2 | 补测试验证回执字段与幂等 | rust_ext/src/daemon/build_flight.rs | （空） | pending |

- 执行顺序：step 0 → step 1 → step 2（G6.4：`task_create` 无结构化依赖字段，按序认领；前序未闭环不领后续）
- 批次：单批次、单 executor ownership；无并行组（所有权文件 `build_flight.rs` / `fs_handlers.rs` 同属一个 executor 线程，不构成并行拆单条件）

## 3. 建单结论：沿用现有 binding，未新建子任务

- **决定**：`沿用现有 binding，不新建子任务`（理由详见 `contracts.md` §4）
- **新建 task_id 列表**：**无**（本轮未调用 `task.create_subtask` / `task.create`，未产生新 task_id）
- **整改单列表**：**无**（无需 BLOCKED 整改；本轮为 Phase 1 首次规划）
- **G6 契约核对（若后续建单必须遵守）**：title 用符号不用行号；description 首行 `REQ-xx`；check_items 传字符串；action ∈ {implement,fix,test,audit}；steps 非空；`blocked-by:` 仅文本约定
- **既有格式瑕疵登记**：冻结 3 步的 `check_items` 落库为转义 JSON 字符串（G6.3 反模式，U-04）——contract 已 frozen（hash 固定），本轮不改；交后续治理维护路径

## 4. daemon 身份与独立性留痕

- Planner 本轮注册身份（自有 session）：`agent_id=planner-alm-1795466935`、`agent_instance_id=planner-alm-1795466935-inst`、`session_id=sess-planner-alm-1795466935-phase1`、`model_id=hunyuan-hy`、`role=planner`
- `agent.register` 返回 `status=registered`；workspace_id=1；未借用主理人或其他成员身份
- **独立性预告**：Phase 2 executor 与 Phase 3 reviewer 必须各自以**不同 `agent_instance_id` 和不同 `session_id`** 注册；`check_role_independence`（`task_collab.rs:565`）按 instance/session 匹配冲突角色，共用会被 daemon 拒绝认领（实测门禁：`(?2 != '' AND agent_instance_id = ?2) OR (session_id != '' AND session_id = ?3)`）

## 5. Handoff（planner → executor，经主理人中转）

```text
Handoff:
  task_id: T-1795466935294-3433ca6c
  step_id: S-1795466935297-34636a9c
  from_role: planner
  outcome: planner_ready_for_execution
  next_role: executor
  next_action: 以自有 agent_instance_id/session_id 注册 executor 身份并 claim step 0；开工前读 .design/receipts-audit-T-1795466935294-3433ca6c/ 全套工件，锁定方案 A 与 U-01/U-02
  reason: 现有 task_contract/role_contract/step/workspace/identity binding 齐全且 decision=READY；3 步与符号锚定逐条对齐，AC 均可在 frozen scope 内达成，无需新建子任务
  independence_requirement: required
  request_id: req-planner-phase1-1795466935
  report_request_id: unavailable
  evidence_path: .design/receipts-audit-T-1795466935294-3433ca6c/
  evidence_hash: unavailable（多文件目录，逐文件 hash 见各工件头）
  identity:
    agent_id: planner-alm-1795466935
    agent_instance_id: planner-alm-1795466935-inst
    session_id: sess-planner-alm-1795466935-phase1
    model_id: hunyuan-hy
    role: planner
```

> `planner_ready_for_execution` 为 design-only outcome（capability `planner_governance_v1` 未声明），此处以**交接文本**形式给出，不向 daemon 持久化、不伪造 `READY/PLAN` 投影；daemon 当前已直接给出 `READY/CLAIM`，executor 可据此领取。

---

# 整改回路追加记录（BLOCKED，loop_count=1）— 2026-11-24 planner

> 本节为 **追加** 记录，上方原计划历史行（§1-§5）保持原样，不覆盖、不重排。
> 背景：Phase 3 第 1 轮评审（reviewer 柯明察，reviewer-alm-3433ca6c-inst）结论 **BLOCKED**。
> 评审对象：origin/master diff `359d56e..9496098`（1cca6aa 前置 + 9496098 实现）。父任务 status=review_pending。

## 6. findings → 整改单映射（追加）

| finding | 处置 | 整改 task_id | REQ 首行 | action | steps | 并入说明 |
|---|---|---|---|---|---|---|
| 🔴1 open 失败出口单飞泄漏 + 0 回执 | 建单 | `T-1795513182788-09a23f38` | REQ-02（单飞泄漏部分为 REQ-GAP 性质，正文已注明） | fix | 4 | 并入 💭死代码（step2）与 💭E5 哨兵（step1 内） |
| 🟡4 Python 审计回归证据不可追溯 | 建单 | `T-1795513182883-0f484fcc` | REQ-06 | fix | 2 | 日志补交或书面声明二选一 |
| 🟡2 AC-02 契约字面 vs E4 物理不可能 | **文档修订，不建单** | — | — | — | — | ac-map.md AC-02 措辞已修订（偏差口径：审计库物理不可用 → 0 回执 + 注释，不伪造审计成功） |
| 🟡3 fail-closed 边界取舍 | **文档修订，不建单** | — | — | — | — | ac-map.md AC-03 + contracts.md CARD-1 已补两条例句：(a) 转后台路径 fail-closed 不可达、build_seq 对账兜底；(b) 同步路径审计失败报 Err 为 CARD-1 约定行为、保留 |
| 💭 同步版 handle_build_graph 死代码 | **并入 🔴1 整改单**（step 2：注明保留理由或删除，executor 决策） | T-1795513182788-09a23f38 | — | fix | — | 同文件同 ownership，整改 commit 顺带处理，避免二次占坑 |
| 💭 E5 分支 ensure_workspace_row_by_root unwrap_or(0) 哨兵 | **并入 🔴1 整改单**（step 1：映射失败仍调 finish 释放单飞，但跳过回执 + 注释，消除哨兵回执污染） | T-1795513182788-09a23f38 | — | fix | — | 与 🔴1 同在后台闭包区域，方案②重构自然覆盖 |
| 💭 resolve_workspace 双重调用 | **backlog，不建单** | — | — | — | — | 纯优化，无功能风险；留待后续技术债清理 |
| adjacent 图谱对本次改动符号零覆盖 | **不建单** | — | — | — | — | 主理人 Phase 6 汇编登记为工具缺口（daemon workspace-1 图谱 revision 落后 origin/master） |

## 7. 整改单建单回执（daemon 返回原文）

```
CARD-R1（🔴1）: task_id=T-1795513182788-09a23f38, parent=T-1795466935294-3433ca6c, status=open, step_count=4
CARD-R2（🟡4）: task_id=T-1795513182883-0f484fcc, parent=T-1795466935294-3433ca6c, status=open, step_count=2
```
- 两卡 workspace 自动绑定 `4baea3ff12c2ea5c`（G1 权威）；description 首行分别为 `REQ-02 | 🔴 blocker` / `REQ-06 | 🟡`；check_items 全部为**字符串**（G6.3 合规，已读回验证）；action 仅用 implement/fix/test（G6.2）；steps 非空。

## 8. 🔴1 架构裁决（planner 定夺，写入整改单 description）

**推荐方案②（fail-fast）**：把 `open_codegraph_write`（含 codegraph_db 未配置检查）提前到 `flights.begin` 之前。理由：
1. 结构上消除泄漏类：失败发生在 begin 前，不占单飞、不消耗 build_seq、无需回执（无可审计的构建发生）——不依赖"每条失败路径记得调 finish"的纪律；
2. begin 后所有失败出口均持有 conn，回执物理可写，AC-02 偏差口径（🟡2）的适用面缩小到极小；
3. 改动收敛在单函数 `handle_build_graph_async` 内，单文件单 ownership。

**备选方案①**（reviewer 提出，保留为回退）：后台闭包对 open 失败加外层 match，Err 分支无条件 `flights_bg.finish(root_key, ok=false stats, no-op 回调)` 后返回错误（恢复 1cca6aa 语义；回执物理不可得按 E4 降级口径注释）。若实施中证实方案②不可行（如 conn 打开会长期阻塞 dispatch 全局状态锁），按方案①实施并在 result 说明——**不得**在两者都未实施的情况下关闭本单。

## 9. 整改执行约定

- 执行顺序：先 CARD-R1（🔴1，blocker）后 CARD-R2（🟡4）；两卡同文件 ownership（fs_handlers.rs / evidence），**不得并行**（规则 40 同文件冲突）。
- executor（郑兑现）以自有 agent_instance_id/session_id 认领（与 Phase 2 同一 executor 身份可复用；与 reviewer 保持 instance/session 双不同）。
- 修完回 Phase 3 由**同一 reviewer**（柯明察）复评；复评通过后方可进入验收取证。
- planner 本轮仅建单与修订设计工件：未写生产代码、未 claim/lease 整改步骤、未 apply/close。

---

# 建单通道修正与废弃映射（追加）— 2026-11-24 planner（第二轮）

> 本节为追加记录，上方 §1-§9 历史行保持原样。

## 10. 整改卡建单通道修正（governance_blocked → READY）

**现象**：首轮经 `task.create_subtask` 建的两张整改卡被 daemon 判 `governance_blocked`（blocking_reasons=["Task Contract 缺失、多版本冲突或 revision 链不连续"]），executor 无法认领。

**根因链（三轮实证）**：
1. 第 1 轮 `task.create_subtask`：裸建通道，不带 role_contracts → 空 Task Contract 投影 → governance_blocked。
2. 第 2 轮 `task.create`（RPC 直连，role_contracts 缺省）：MCP 工具层的"缺省注入三角色模板"是 Python 包装行为，**RPC 直连绕过该层**，daemon 收到空 role_contracts → 仍 governance_blocked（返回 contract_count=0）。
3. 第 3 轮 `task.create`（RPC 直连，显式传 role_contracts 三角色模板 + identity_policy=legacy_identity_v1 + workspace 显式配对）：**成功**，governance 投影完整（contract_count=3）。

**最终有效整改卡（读回验证 10/10 全绿）**：

| 卡 | task_id | steps | decision/action | required_role | step_id | task_contract | role_contract | blocking_reasons | workflow |
|---|---|---|---|---|---|---|---|---|---|
| CARD-R1（🔴1） | `T-1795513761767-d77a6c10` | 4 | READY/CLAIM | executor | S-1795513761791-d8e47fdc | TC-...-d77a6c10 r1 sha256:2b1fb3c7... | rcl-...-executor r1 sha256:4dfdf3e6... | [] | queued |
| CARD-R2（🟡4） | `T-1795513761922-e0b4ca78` | 2 | READY/CLAIM | executor | S-1795513761976-e3efe45c | TC-...-e0b4ca78 r1 sha256:bdad3d92... | rcl-...-executor r1 sha256:4dfdf3e6... | [] | queued |

两卡 workspace 均绑定 `4baea3ff12c2ea5c`；description 首行 REQ-02 / REQ-06；check_items 字符串（G6.3）；action ∈ implement/fix/test；title 符号锚定。

## 11. 废弃映射（死卡 → 有效卡；supersede 留给 Phase 5 adjudicator）

| 废弃死卡（governance_blocked，action=NONE 永不可认领） | 废弃原因 | 替代有效卡 |
|---|---|---|
| `T-1795513182788-09a23f38` | task.create_subtask 裸建缺 Task Contract 投影 | T-1795513761767-d77a6c10 |
| `T-1795513182883-0f484fcc` | 同上 | T-1795513761922-e0b4ca78 |
| `T-1795513688065-ae849a44` | task.create RPC 直连绕过 MCP 层缺省注入、role_contracts 空 | T-1795513761767-d77a6c10 |
| `T-1795513688143-b324cb64` | 同上 | T-1795513761922-e0b4ca78 |

内容等价性：有效卡与死卡的 title/description/steps/check_items 语义一致（重建时原样沿用，仅在 description 追加重建溯源说明）；§6 findings→整改单映射表中的 task_id 以本节有效卡为准。

## 12. RPC 直连建单通道要点（供后续维护，属 team-lead 登记的治理工具缺口同族）

`task.create` 经 RPC 直连（HttpDaemonRpcClient）时必须显式提供：
1. `role_contracts`：非空列表，每项 `{role∈{executor,reviewer,adjudicator}, prompt, description, instructions}`（模板见 tools_task.py::_default_task_role_contracts）；
2. `identity_policy`：如 `legacy_identity_v1`（缺失报 E_TASK_IDENTITY_POLICY_REQUIRED）；
3. `workspace_id` + `workspace_instance_id`：G1 权威配对（缺失报 E_TASK_WORKSPACE_UNBOUND，RPC 生产路径禁止隐式补齐——与 MCP 工具层"服务端补齐"行为不一致）。
缺任一项 → governance_blocked 空投影或建单被拒。`task.create_subtask` 裸建通道当前不可用于治理任务。

---

# 用户新裁决与 CARD-R3/R4 建单（追加）— 2026-11-24 planner（第三轮）

> 本节为追加记录，上方 §1-§12 历史行保持原样。

## 13. 用户裁决（2026-11-24，卡 owner 决策）与两张新卡

### 13.1 裁决原文要点
1. **AC-05 读面 → 开新卡补读面**：放开任务卡「不新增 MCP 工具」约束（**仅限本读面**），补「按 workspace+时间范围查 write_audit_events」读面；本轮父任务先不关单，等新卡闭环后一并裁决。
2. **daemon round-trip → 方案 B 登记**：本轮以单测+复跑证据为基础；「round-trip 补证」建独立验证单，待 daemon 空闲维护窗口（提前广播）部署新二进制后补验三点；不阻塞本轮关单流程（关单时点在 CARD-R3 闭环之后）。
- 设计工件联动：U-03（读面能力缺口，assumption）由 CARD-R3 闭合；§4 方案对比中「不新增 MCP 工具」约束对本读面失效（用户授权），其余场景约束不变。

### 13.2 新卡回执（读回验证 10/10 全绿）

| 卡 | task_id | REQ 首行 | steps | decision/action/role | step_id | task_contract hash |
|---|---|---|---|---|---|---|
| CARD-R3（读面实现） | `T-1795584884815-7777ebbc` | REQ-05（AC-05 ↔ REQ-05，task-map §1 映射） | 3 | READY/CLAIM/executor | S-1795584884866-7a8d8fc8 | sha256:2c20e972... |
| CARD-R4（round-trip 补证验证单） | `T-1795584884984-81944550` | REQ-GAP（round-trip 补证无既有 REQ 卡；work_order.required_evidence 有 daemon round-trip evidence 但无 REQ 编号） | 2 | READY/CLAIM/executor | S-1795584885020-83b9a078 | sha256:8ab05425... |

- 两卡 contract_count=3（三角色模板）、identity_policy=legacy_identity_v1、workspace=1/4baea3ff12c2ea5c、blocking_reasons=[]、workflow=queued；check_items 全字符串、action ∈ implement/audit/test、title 符号锚定。
- CARD-R3 要点：workspace_id 用 registry 权威列（reviewer loop1 立场）；start/end 过滤 recorded_at；limit<=0 语义对齐 handle_audit_verify_chain；不改 write_audit_events schema 与写入路径；op_class 只读。
- CARD-R4 要点：**建单登记不执行**（执行时机由用户控制，保持 open）；执行前提=daemon 空闲维护窗口+提前广播（部署脚本全量 kill cw-daemon.exe）；补验三点（回执 12 字段落库 / verify_chain broken_count=0 / build_seq 连续对账）+ 部署纪律与二进制字符串/sha256 验证铁律，已写入 description 与 check_items。

### 13.3 全部整改/新增卡执行顺序（覆盖 §9，以本节为准）

| 顺序 | 卡 | 执行者 | 说明 |
|---|---|---|---|
| 1 | CARD-R1 `T-1795513761767-d77a6c10`（🔴1） | executor 郑兑现 | blocker 先行 |
| 2 | CARD-R2 `T-1795513761922-e0b4ca78`（🟡4） | executor | 不同文件（evidence），可与 R1 串行 |
| 3 | CARD-R3 `T-1795584884815-7777ebbc`（读面） | executor | 用户裁决新增；实现 → 同一 reviewer 复评 → tester 对 AC-05 重新取证 |
| 4 | 全部闭环后统一进 Phase 5 关单 | adjudicator | 届时一并处理 4 张死卡（§11）supersede |
| 挂起 | CARD-R4 `T-1795584884984-81944550` | executor（待用户指令） | 建单登记不执行；不阻塞关单流程，维护窗口执行 |
