# 前置条件告知（prerequisite gate）— Contract 卡片

Feature: `preq` ｜ 快照：must = REQ-PREQ-04/01/02/03，指定 should = REQ-PREQ-06（文档），deferred = REQ-PREQ-05（work_next，批次 2）
任务树映射：卡 1→T1 ｜ 卡 2→T1（同文件同 ownership 合并）｜ 卡 3→T2 ｜ 卡 4→T3 ｜ 卡 5→T4 ｜ 卡 2 接线→T5 ｜ 集成测试→T6 ｜ REQ-PREQ-06→T7 ｜ 卡 6→T8（批次 2）

---

## 卡 1 ｜ evaluate_task_prerequisites（new）

- **文件/模块**：`rust_ext/src/daemon/task_loop/prerequisites.rs`（新文件，加入 `task_loop/mod.rs`）
- **REQ**：REQ-PREQ-04（must，批次 1）— 分层前置满足判定引擎
- **签名**：

```rust
pub(crate) struct PrerequisiteItem {
    pub provider_task_id: String,
    pub lifecycle_status: String,      // provider 当前 lifecycle（open/in_progress/review/applied/closed）
    pub dep_type: String,              // requires_existing / requires_artifact / requires_interface / 未知类型原样透传
    pub unmet_reason: String,          // provider_not_completed / artifact_stale / artifact_unresolved / interface_unselected / unknown_dep_type
    pub freshness_status: Option<String>, // 仅 requires_artifact 携带
}

pub(crate) struct PrerequisiteEvaluation {
    pub hard_unmet: Vec<PrerequisiteItem>,
    pub warnings: Vec<PrerequisiteItem>, // is_hard=0 且未满足的信息性边
    pub all_satisfied: bool,             // hard_unmet 为空即 true
}

pub(crate) fn evaluate_task_prerequisites(
    conn: &Connection, workspace_id: i64, task_id: &str,
) -> Result<PrerequisiteEvaluation, DaemonRpcError>
```

- **入参契约**：`workspace_id` 来自 claim 路径的 workspace binding（`write_domain` step 1 已解析）或投影路径的 task binding；`task_id` 为任务主键。task 不存在或无依赖边 → 空结果、`all_satisfied=true`（不报错）。
- **出参契约**：见结构体。判定规则（分层）：
  - `requires_existing`：provider lifecycle ∈ {applied, closed} 即满足；
  - `requires_artifact`：lifecycle 满足 **且** provider artifact `freshness_status='fresh'`；artifact 行缺失 → `artifact_unresolved`（fail-closed）；`stale/producing` → `artifact_stale`；
  - `requires_interface`：interface_provider_selections 存在该 interface 的 active selection（列语义以 `db/schema.py:1486` 为准）；
  - **未知 dep_type** → 一律计为 unmet（`unknown_dep_type`），不静默放行。
- **错误变体**：

| 错误 | 触发 | 处置 |
|---|---|---|
| `DaemonRpcError::internal_error` | dependency_edges / tasks / artifact_identities / interface_provider_selections 读取失败 | 向上抛；claim 路径映射为 Infrastructure（回滚）；投影路径整体失败 |

- **数据源**：`dependency_edges WHERE workspace_id=? AND consumer_task_id=? AND is_hard=1`（列见 `db/schema.py:1472`）；provider lifecycle 读 `tasks.status`；artifact 读 `artifact_identities.freshness_status`（经 `task_dependencies.artifact_id` 关联，声明表见 `db/schema.py:1433`）。
- **副作用**：无（纯读）。
- **依赖符号**：`db/schema.py` 表结构；`task_collab_governance.rs:1146` handle_get_dependency_edges 的查询模式（列清单）。
- **GWT 测试要点**（`prerequisites_test.rs` 或文件内 `#[cfg(test)]`）：
  1. Given requires_existing 边 provider=open → hard_unmet=1、`provider_not_completed`、all_satisfied=false；Then claim 前置判定拒绝；
  2. Given provider=closed → all_satisfied=true（**通过分支**）；
  3. Given requires_artifact + provider=closed + freshness=stale → `artifact_stale`；
  4. Given requires_artifact + artifact 行缺失 → `artifact_unresolved`（fail-closed 分支）；
  5. Given requires_interface + 无 active selection → `interface_unselected`；
  6. Given is_hard=0 且 provider 未完成 → 仅进 warnings，不进 hard_unmet；
  7. Given 未知 dep_type → `unknown_dep_type`（fail-closed 分支）；
  8. Given 无依赖边 → 空结果。
- **验收映射**：AC-01、AC-02、AC-03
- **Assumptions**：A1 dependency_edges 与 task_dependencies 按 (provider, consumer, edge_type/dep_type) 可关联；A2 interface selection 的 active 判定列以 schema.py:1486 实际列名为准（executor 实现时核对）。

---

## 卡 2 ｜ prerequisite_gate_v1 capability 声明（new，含接线）

- **文件/模块**：`rust_ext/src/daemon/task_loop/prerequisites.rs`（声明）+ `rust_ext/src/daemon/dispatch.rs`（聚合接线，:450 附近）
- **REQ**：REQ-PREQ-03（must，批次 1）
- **签名**：

```rust
pub const CAPABILITY_NAME: &str = "prerequisite_gate_v1";
pub const CAPABILITY_SCHEMA_VERSION: &str = "prerequisite_capability_v1"; // 起始值，对齐 task_prompt 惯例
pub fn capability_enabled() -> bool { true } // v1 无构建资产，随 daemon 交付即启用；后续接运行时开关只改此函数
pub fn capability_projection() -> Value // {"name","schema_version","enabled"}
```

- **入参/出参契约**：`capability_projection()` 返回固定四键 JSON；`capability_enabled()` 无入参。模式参照 `task_prompt/handler.rs:119-142`。
- **错误变体**：无（声明函数不失败）。
- **异常路径**：无。
- **副作用**：无。
- **依赖符号**：`dispatch.rs:450`（`task_prompt::handler::capability_projection()` 聚合点，紧邻追加一行）。
- **GWT 测试要点**：
  1. Given projection → name/schema_version/enabled 三键齐且 name="prerequisite_gate_v1"；
  2. Given dispatch 聚合投影 → capabilities 列表含该 projection（接线冒烟）。
- **验收映射**：AC-11
- **Assumptions**：v1 enabled 为编译期常量；disabled 分支（AC-10）经 claim 门禁的 `enabled` 参数直测（见卡 5）。

---

## 卡 3 ｜ next_action 投影 prerequisites（extend）

- **文件/模块**：`rust_ext/src/daemon/task_loop/next_action.rs`
- **REQ**：REQ-PREQ-01（must，批次 1）
- **签名（改动点）**：
  - `struct Rendered` 增字段 `prerequisites: Value`（默认 `json!([])`）；
  - `evaluate_next_action_inner`（:1461）在组装 `Rendered` 时调用 `evaluate_task_prerequisites` 填充 `hard_unmet` 数组；
  - `render`（:982）在 `"blocking_reasons"`（:1092-1095）之后、`revision_hint`（:1096-1111）之前插入 `"prerequisites"` 键。
- **出参契约**：`prerequisites: [{provider_task_id, lifecycle_status, dep_type, unmet_reason, freshness_status?}]`；无未满足硬依赖时为 `[]`（**字段必须存在**，AC-05）。
- **错误变体**：引擎 DB 失败 → `DaemonRpcError` 向上抛，`evaluate_next_action` 整体失败——**不静默省略 prerequisites 字段**（fail-closed，异常矩阵 #5）。
- **异常路径**：task 无 binding/workspace 无法解析 → 沿用既有 blocking_reasons 路径，prerequisites 置 `[]`（依赖不可评估 ≠ 依赖未满足，不新增阻塞语义）。
- **副作用**：无（只读投影）。
- **依赖符号**：卡 1 引擎；`next_action.rs:982/1461/1854`。
- **GWT 测试要点**：
  1. Given 硬依赖未满足的卡 → 投影含 prerequisites 且 provider_task_id 正确；
  2. Given 无依赖 → prerequisites==[] 且字段存在（**空数组语义分支**）；
  3. Given 引擎查询失败（构造 DB 异常）→ RPC 整体报错而非省略字段（失败分支）。
- **验收映射**：AC-04、AC-05
- **Assumptions**：无。

---

## 卡 4 ｜ task.status 治理投影 prerequisites（extend）

- **文件/模块**：`rust_ext/src/daemon/task_collab_query.rs`
- **REQ**：REQ-PREQ-01（must，批次 1）
- **签名（改动点）**：`tree_governance_projection`（:1017）在 `blocking_reasons`（:1030-1153 组装区）之后追加 `prerequisites` 键；若 :106/:694 处的字段清单构成投影输出 allowlist，同步追加 `"prerequisites"`（executor 实现时核实用途）。
- **出参契约**：与卡 3 同构（同引擎、同 shape），保证 task.status / task.status_tree / task.next_action 三处投影一致（AC-06）。
- **错误变体**：引擎失败 → 沿用该文件既有 fail-closed 模式（:1178 的 "无法生成治理投影" 错误路径），不静默。
- **异常路径**：closed/reverted 任务 → prerequisites==[]。
- **副作用**：无。
- **依赖符号**：卡 1 引擎；`task_collab_query.rs:1017/1030/1153/1178`。
- **GWT 测试要点**：
  1. Given 硬依赖未满足 → task.status 治理投影含 prerequisites，shape 与 next_action 一致（同构分支）；
  2. Given closed 任务 → prerequisites==[]；
  3. Given status_tree → 节点投影同样携带（接线分支）。
- **验收映射**：AC-06
- **Assumptions**：:106/:694 清单的用途由 executor 核对（allowlist 则加，白名单注释则不改）。

---

## 卡 5 ｜ claim 硬依赖门禁 E_PREREQUISITE_NOT_MET（extend）

- **文件/模块**：`rust_ext/src/daemon/task_loop/claim.rs`
- **REQ**：REQ-PREQ-02（must，批次 1）
- **签名（改动点）**：
  - 新增错误码常量 `pub const ERR_PREREQUISITE_NOT_MET: &str = "E_PREREQUISITE_NOT_MET";`
  - `write_domain`（:105）在 step 6（binding 链校验，:169-173）之后、step 7（INSERT，:175-186）之前插入 gate：

```rust
fn check_prerequisites_gate(
    tx: &Connection, workspace_id: i64, task_id: &str, step_id: &str, enabled: bool,
) -> Result<Option<Vec<PrerequisiteItem>>, ClaimDomainError>
```

  返回 `Some(warnings)` 时把信息性警告并入响应 JSON（`prerequisite_warnings`）；`hard_unmet` 非空 → `ClaimDomainError::Deterministic { code: ERR_PREREQUISITE_NOT_MET, message 含每个 provider_task_id 与 unmet_reason }`。
- **入参契约**：`enabled` 由调用点传 `prerequisites::capability_enabled()`（参数化以便单测覆盖 disabled 路径）；`workspace_id` 复用 step 1 解析结果；引擎在 savepoint 事务连接上只读执行。
- **出参契约**：阻断时 message 形如 `"task X 存在未满足硬依赖: provider Y (requires_existing, provider_not_completed)；请先完成 provider 任务"`——**错误消息直接指路 provider**。
- **错误变体**：

| 错误 | 触发 | 处置 |
|---|---|---|
| `E_PREREQUISITE_NOT_MET` | enabled 且 hard_unmet 非空 | Deterministic，dry_run=true 无副作用；实际路径外层事务回滚，无任何 INSERT/状态变化 |
| Infrastructure | 引擎 DB 读取失败 | 外层事务回滚（异常矩阵 #5：依赖失败 → 向上抛、不静默放行） |

- **异常路径**：enabled=false → 跳过 gate，claim 行为与存量完全一致（AC-10，兼容零破坏）；信息性边未满足 → 不阻断，仅 `prerequisite_warnings`。
- **副作用**：gate 本身无写；仅其通过后的既有 INSERT 流程照常。
- **依赖符号**：卡 1 引擎；卡 2 `capability_enabled()`；`claim.rs:105/169-186/205`。
- **GWT 测试要点**：
  1. Given enabled + 硬依赖未满足 → E_PREREQUISITE_NOT_MET、message 含 provider_task_id、任务状态与步骤零变化（**失败分支 + 无副作用断言**）；
  2. Given provider=closed → claim 成功（通过分支）；
  3. Given 仅信息性边未满足 → claim 成功且响应含 prerequisite_warnings（警告不阻断分支）；
  4. Given enabled=false → 与存量行为逐字节一致（兼容分支）；
  5. Given 引擎 DB 错误 → Infrastructure、事务回滚、无部分写入。
- **验收映射**：AC-07、AC-08、AC-09、AC-10
- **Assumptions**：无。

---

## 卡 6 ｜ work_next 派工过滤（extend，批次 2 deferred）

- **文件/模块**：`rust_ext/src/daemon/task_collab_lease.rs`
- **REQ**：REQ-PREQ-05（should，**批次 2，本批不建单**）
- **签名（改动点）**：`handle_task_work_next`（:929）候选卡筛选处调用引擎过滤 `hard_unmet` 非空的卡；把未满足卡的 provider 卡加入推荐（排序规则批次 2 设计时定，当前 open）。
- **入参/出参契约**：复用现有响应 shape；被过滤卡不出现在派工结果；provider 卡作为 `recommended_next` 之类字段出现（字段名批次 2 定）。
- **错误变体**：引擎失败 → 整体错误，不降级为"无卡可派"（fail-closed）。
- **异常路径**：无硬依赖卡 → 行为与存量一致。
- **副作用**：无（只读筛选择）。
- **依赖符号**：卡 1 引擎；`task_collab_lease.rs:929`。
- **GWT 测试要点**：
  1. Given 池中有硬依赖未满足的卡 → 不出现在结果；
  2. Given provider 卡可领取 → 出现在推荐中；
  3. Given 全部满足 → 正常派工。
- **验收映射**：AC-13（deferred）
- **Assumptions**：排序规则 open（见 brief 未决项 O2）。
