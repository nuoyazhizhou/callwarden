# T1 独立评审报告 — 前置满足分层判定引擎与 capability 声明

- **Role**: reviewer（柯明察，独立评审官）
- **Task**: T-1795663519023-eb82b9a8（T1 判定引擎，REQ-PREQ-04 must + REQ-PREQ-03 声明部分）
- **独立性**: `independence=daemon_registered_new_session`
  - daemon 可达（本次与前几轮 stale manifest 不同）。经 `agent.register` 注册全新身份：`agent_id=reviewer-t1-t7` / `agent_instance_id=reviewer-t1-t7-inst`（非空）/ `session_id=sess-reviewer-t1-t7-t1-20261128` / `model_id=workbuddy-agent` / `role=reviewer`，与 executor 的 `sess-run-20261126-113111-7bc7`（T1 executor 会话）完全不同；T7 评审另用 `sess-reviewer-t1-t7-t7-20261128`（两单不同 session）。未复用任何 executor/reviewer/tester/adjudicator 身份。
  - `lease.acquire`（reviewer，fencing_counter=1）→ 本 verdict 为 blind_first_pass → `lease.release`。
- **评审对象**: commit `e133f34`「feat(task-loop): 前置满足分层判定引擎与 capability 声明」（已含于 origin/master 及多个 impl 分支）
  - 改动面：`rust_ext/src/daemon/task_loop/prerequisites.rs`（新增，651 行）+ `task_loop/mod.rs`（+2 行注册 `pub mod prerequisites;`）。`git show e133f34 --stat` 确认仅此两文件，无越界改动。
  - 本单无 executor handoff、无 executor 证据——评审取证全部由 reviewer 自行完成（见下）。
- **只读声明**: 本次未改源码/文档/证据/任务状态，未建整改单，未 apply/close/supersede。本报告为评审产出文档。

---

## 结论：PASS

无 🔴 blocker。冻结契约（contracts.md 卡 1 + 卡 2）的判定规则、结构体 shape、错误变体、副作用约束、capability 三常量逐条核验通过；18 个单测在 e133f34 干净 worktree 实测全绿（prerequisites 18 passed / task_loop 233 passed，与 commit message 一致）。1 条 🟡（契约数据源文字与实现的偏差，实现侧有据+已文档化，建议 planner 记录基线附录）与 3 条 💭，均不阻断。

---

## 证据等级与方法说明（如实声明）

- **运行时证据（真实跑过）**：从 e133f34 建只读 worktree（`run-20261126-122222-ce47`，HEAD=e133f34、工作树干净），按项目构建惯例构造隔离环境（剥离 git-bash `/usr/bin` 以解除对 MSVC link.exe 的遮蔽、`PYO3_CONFIG_FILE` 指向 Python 3.14、独立 `CARGO_TARGET_DIR`）：
  - `cargo test -p callwarden-core --lib prerequisites` → **18 passed; 0 failed**（17 个 prerequisites.rs 模块测试 + 1 个 stage_toggle 模块同名过滤命中）
  - `cargo test -p callwarden-core --lib task_loop` → **233 passed; 0 failed**
  - 与 commit message 声称的「18 passed / 233 passed」逐字一致。
- **静态证据**：prerequisites.rs 全文 651 行逐行对照 contracts.md 卡 1/卡 2；数据源语义交叉核对 `db/schema.py:1433-1521`、`db/db_task_dependencies.py:179-212/409-552`、`db/db_task_contracts.py:1054-1061`。
- **调用图证据**：daemon 图查询（get_callers/get_symbol）对新符号返回空（snapshot 未含新文件）；静态 `git grep` 确认 e133f34 上 `evaluate_task_prerequisites` **零生产调用点**（仅定义+测试）——T1 为根任务（task-map：blocked-by 无），影响半径封闭在模块内，无回归面。capability 接线在 T5 提交 e7164da（`dispatch.rs:456-459`，`"prerequisites" → prerequisites::capability_projection()`），三常量与 dispatch 键逐字一致（复核原文确认，T5 评审归属他人，此处仅交叉引用）。

---

## 契约逐条核验

### 卡 1 evaluate_task_prerequisites

| 契约条目 | 实现位置 | 核验 |
|---|---|---|
| 新文件 + mod.rs 注册 | prerequisites.rs 651 行；mod.rs `pub mod prerequisites;` | ✅ |
| `PrerequisiteItem` 五字段（provider_task_id/lifecycle_status/dep_type/unmet_reason/freshness_status: Option） | :40-55 | ✅ 字段名、类型、注释枚举值逐字一致 |
| `PrerequisiteEvaluation{hard_unmet, warnings, all_satisfied}`，all_satisfied = hard_unmet 为空 | :59-67，:137 `all_satisfied: hard_unmet.is_empty()` | ✅ warnings 不影响 all_satisfied |
| 签名 `(conn, workspace_id, task_id) -> Result<PrerequisiteEvaluation, DaemonRpcError>` | :91-95 | ✅ |
| task 不存在或无依赖 → 空结果、all_satisfied=true、不报错 | read_declarations 无行 → 空分区；测试 nonexistent_task/no_declarations | ✅ |
| requires_existing: lifecycle ∈ {applied, closed} | COMPLETED_LIFECYCLE（:36）+ :190 | ✅；reverted/行缺失/空 target_task_id → 未满足（fail-closed，异常矩阵 #7） |
| requires_artifact: lifecycle 满足 且 freshness='fresh'；行缺失 → artifact_unresolved（fail-closed）；stale/producing → artifact_stale | :210-250 | ✅（unmet_reason 优先级见 💭-1） |
| requires_interface: selection 存在性（列语义以 schema.py:1486 为准） | :254-292 | ✅ 实测 schema.py:1492-1503 该表**无状态列**（UNIQUE(workspace,consumer,contract,revision,interface_name)），「行存在即 active」为唯一合理读法，Assumption A2 已正确落地；ORDER BY selected_at DESC LIMIT 1 兼容跨契约 revision 多行 |
| 未知 dep_type → unmet（unknown_dep_type），不静默放行 | :118-124 | ✅ 测试 unknown_dep_type_is_fail_closed |
| DB 读失败 → DaemonRpcError::internal_error 向上抛 | :160/:172/:177/:227/:271/:306（六处全部映射）；dispatch.rs:266 确认构造器存在 | ✅ 无静默吞错 |
| 副作用：无（纯读） | 全文件无写路径 | ✅ |
| workspace 过滤不跨区 | 三张表查询均带 workspace_id；测试 cross_workspace_declarations_are_filtered | ✅ |

**数据源偏差（🟡-1，详见 Findings）**：契约文字写 `dependency_edges WHERE … is_hard=1`，实现读 `task_dependencies`（`is_informational==0` → hard）。核实结果：**实现正确，契约文字不可行**——
1. `db/schema.py:1505-1507`：dependency_edges「仅显式 requires_artifact 和已解析 requires_interface→provides_interface 建边」，requires_existing 与未知 dep_type 永不物化为边；按契约数据源则卡 1 自己的 requires_existing 规则永远无法评估。
2. `db/db_task_dependencies.py:545`：`_insert_dependency_edge` 硬编码 `is_hard=1`；`:438` 建边前过滤 `is_informational=0`；`:517`「requires_existing 和 provides_interface 不建边」。即 dependency_edges 无信息性边，warnings 分区同样无法从该表得出。
3. 实现的 `is_informational==0 → hard` 映射与 schema 注释「is_informational=true 的关系不参与阻断」（schema.py:1446）一致，且对 artifact/interface 子集与 dependency_edges 语义等价（信息性声明不建边 ⟺ 非边即硬）。
4. 偏差已在模块 docstring（:88-90）如实文档化。

### 卡 2 capability 声明（声明半；接线半属 T5）

| 契约条目 | 实现位置 | 核验 |
|---|---|---|
| `CAPABILITY_NAME="prerequisite_gate_v1"` | :31 | ✅ |
| `CAPABILITY_SCHEMA_VERSION="prerequisite_capability_v1"`（对齐 task_prompt 惯例） | :33 | ✅；brief §4.4 open 项「schema_version 起始值由 executor 按惯例取值」已闭环 |
| `capability_enabled() -> bool { true }` | :70-72 | ✅ v1 编译期常量 |
| `capability_projection()` 返回 `{name, schema_version, enabled}` | :75-81 | ✅ 三键（契约文字「四键」系笔误——其内联签名注释与 GWT 均为三键，实现取三键正确） |
| 模式参照 task_prompt/handler.rs:119-142 | git show e133f34 核对：同构（enabled() + projection() json! 三/五键） | ✅ |
| GWT：三键齐且 name 正确 | 测试 capability_projection_shape | ✅ |
| GWT：dispatch 聚合含该 projection | T5 e7164da dispatch.rs:456-459（交叉引用，非本单 scope） | ✅（接线存在性已复核） |

### GWT 测试要点 → 测试映射（契约 8 条全覆蓋）

| # | 契约 GWT | 测试 | 结果 |
|---|---|---|---|
| 1 | requires_existing + provider=open → hard_unmet=1/provider_not_completed/all_satisfied=false | requires_existing_provider_open_is_unmet | ✅ |
| 2 | provider=closed → all_satisfied=true | requires_existing_provider_applied_or_closed_is_satisfied | ✅ |
| 3 | requires_artifact + closed + stale → artifact_stale | requires_artifact_stale_or_producing_is_unmet（stale+producing 双例） | ✅ |
| 4 | requires_artifact + 行缺失 → artifact_unresolved（fail-closed） | requires_artifact_missing_row_is_unresolved_fail_closed | ✅ |
| 5 | requires_interface + 无 active selection → interface_unselected | requires_interface_without_selection_is_unmet | ✅ |
| 6 | is_hard=0 且未满足 → 仅 warnings | informational_unmet_goes_to_warnings_only | ✅ |
| 7 | 未知 dep_type → unknown_dep_type（fail-closed） | unknown_dep_type_is_fail_closed | ✅ |
| 8 | 无依赖边 → 空结果 | no_declarations_is_empty_and_satisfied | ✅ |

超额覆盖（契约未要求、实现补强）：reverted/悬空 provider、fresh+completed 通过分支、open provider 的 provider_not_completed、interface 有 selection 通过分支、provides_interface 跳过、mixed hard/info 分区、跨 workspace 过滤、不存在的 task。freshness 关联语义与 Python `get_artifact_freshness`（db_task_dependencies.py:179-212）逐字对齐（同 WHERE + `ORDER BY produced_at DESC LIMIT 1`）。

---

## 覆盖矩阵

| 改动点 | 调用方（e133f34 时点） | 覆盖 | 缺口 |
|---|---|---|---|
| `evaluate_task_prerequisites` 引擎本体 | 生产：**无**（根任务，零调用点，静态 grep + daemon 图查询空双确认）；测试：17 个模块单测直调 | 全部 8 条契约 GWT + 8 条超额分支；DB 失败路径静态核验（六处映射） | 无生产调用方 → 无集成面可测（T2/T3/T4 接线后才有）；DB 失败注入未做（仅静态核验 error 映射） |
| `read_declarations` | 引擎内部 | 正常/空/跨 workspace 三态 | 契约 revision 混入场景未测（见 💭-2） |
| `evaluate_requires_existing` | 引擎内部 | open/applied/closed/reverted/缺失 五态 | — |
| `evaluate_requires_artifact` | 引擎内部 | stale/producing/fresh/行缺失/open provider 五态 | 多 artifact 同 ref 时 produced_at 并列未测（LIMIT 1 确定性依赖排序稳定性，与 Python 侧同语义） |
| `evaluate_requires_interface` | 引擎内部 | 无 selection/有 selection | selection 行的 consumer/契约 revision 隔离未逐一测（UNIQUE 约束兜底） |
| capability 三常量 + projection | T5 e7164da dispatch.rs:458（交叉引用） | projection shape 单测 + 接线点原文复核 | dispatch 聚合冒烟测试属 T5 scope |
| `read_task_status` | 三个 evaluate_* 共用 | 空串/缺失/正常 | — |

---

## Findings

- 🟡 **[契约基线偏差-记录项]** `prerequisites.rs:88-90,147-182` — 冻结契约 contracts.md 卡 1「数据源」条目写 `dependency_edges WHERE workspace_id=? AND consumer_task_id=? AND is_hard=1`，实现读 `task_dependencies`（`is_informational==0` 为 hard）。**实现是对的**（dependency_edges 只物化 artifact/interface 硬边且 is_hard 恒 1，requires_existing/未知类型/信息性边在该表中不可表示，见 schema.py:1505-1507 与 db_task_dependencies.py:438/517/545 的核验），偏差也已在 docstring 文档化；但冻结基线文字自此与权威实现不一致。**建议动作**：由 planner 在 .design/preq/ 追加一页契约附录（记录数据源决策与依据，不改写历史行），供后续 T2/T3/T6 与批次 2 引用；不阻断本单。

- 💭 **[nit]** `prerequisites.rs:230-242` — requires_artifact 同时满足「lifecycle 未完成」与「freshness≠fresh」时 unmet_reason 取 `provider_not_completed`（lifecycle 优先）；「行缺失 + lifecycle 未完成」时取 `artifact_unresolved`（行缺失优先）。契约未规定双故障优先级，实现取舍合理（provider 未完成是更早的失败），仅记录供批次 2 排序设计参考。

- 💭 **[nit]** `prerequisites.rs:147-158` — read_declarations 不按 contract_revision 过滤，同一 task 跨 envelope revision 的旧声明行会持续参与判定（导入侧仅按 (contract_id, revision) 幂等重灌，旧行不删）。契约指定的 dependency_edges 数据源有完全相同的性质（INSERT OR IGNORE、无清理），故本实现与契约基线语义等价，不构成回归；若批次 2 派工过滤对「旧 revision 声明残留」敏感，再统一设计。

- 💭 **[nit]** 模块 docstring（:11）称分层规则出处「requirements-brief §4.4」——已核对 brief §4.4 confirmed 行确为该分层规则，引用准确（此条为正向确认，非缺陷）。

---

## 未覆盖路径

1. **生产调用链**：e133f34 上引擎零生产调用点，claim 门禁（T4）/next_action 投影（T2）/status 投影（T3）/dispatch 接线（T5）路径的集成行为不在本单可验证范围（各自单评）。
2. **DB 读失败注入**：六处 `DaemonRpcError::internal_error` 映射为静态核验，未做 DROP TABLE 类注入测试（T4 评审已对 claim 路径做过 task_dependencies 注入并全绿）。
3. **daemon 调用图交叉验证**：get_callers/get_symbol 对新符号返回空（snapshot 未刷新），影响半径以静态 grep 双确认替代；此为方法学降级，如实记录。
4. **RPC/MCP 端到端**：capability projection 经 health RPC 的透传未做端到端验证（T5 scope）。

---

## Handoff

`PASS` → 交 adjudicator。🟡-1（契约数据源基线附录）请 planner 决定是否落附录，不阻断 T1 关单。
