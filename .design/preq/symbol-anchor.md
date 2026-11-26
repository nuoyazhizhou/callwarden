# R2 符号锚定 — 前置条件告知

> 降级声明：本会话 MCP daemon 不可用（端口配置不一致），符号定位走 IDE Read/Grep + CLI，
> 全部证据带 file:line。图数据可用性未走 cw 查询（daemon 查询 RPC 可用但本设计目标即扩展该层，
> 锚定以源码直读为准）。

## 锚定表

| # | 设计元素 | 判定 | 符号 / 位置 | 证据 |
|---|---|---|---|---|
| 1 | 依赖声明建模 | **reuse** | `task_dependencies` @ `db/schema.py:1433` | 四类 dep_type + is_informational + artifact_id 可空 |
| 2 | 依赖边物化 | **reuse** | `dependency_edges` @ `db/schema.py:1472` | provider/consumer/edge_type/source_type/contract_id/is_hard/created_at |
| 3 | artifact freshness | **reuse** | `artifact_identities` @ `db/schema.py:1455` | freshness_status: producing/fresh/stale |
| 4 | interface 选择 | **reuse** | `interface_provider_selections` @ `db/schema.py:1486` | selected/active 状态 |
| 5 | 依赖查询 RPC（只读） | **reuse** | `handle_get_dependency_edges` @ `rust_ext/src/daemon/task_collab_governance.rs:1146`；`handle_detect_cycle` @ 同文件 `:688` | 查询模式可直接复用（SELECT 列清单 1165-1167） |
| 6 | 判定引擎 | **new** | `rust_ext/src/daemon/task_loop/prerequisites.rs::task_loop::prerequisites::evaluate_task_prerequisites` | task_loop 模块现有单文件先例（claim.rs / next_action.rs） |
| 7 | next_action 投影 | **extend** | `evaluate_next_action` 投影组装 @ `rust_ext/src/daemon/task_loop/next_action.rs:1087-1111` | `blocking_conditions`/`blocking_reasons` 插入点旁追加 `prerequisites` |
| 8 | task.status 治理投影 | **extend** | `task_collab_query.rs`（治理投影 blocking_reasons 组装） | 与 next_action 同构追加 |
| 9 | claim 门禁 | **extend** | `claim_task` @ `rust_ext/src/daemon/task_loop/claim.rs:23-74` | 锁解析（36）后、tx 开始（40）与 insert（63）之间插 gate |
| 10 | E_PREREQUISITE_NOT_MET | **new** | 错误码常量（错误码清单参照 `task_prompt/context.rs` 的 `E_TASK_PROMPT_*` 模式） | DaemonRpcError::new(code, msg) 模式 |
| 11 | capability 声明 | **new** | `rust_ext/src/daemon/task_loop/prerequisites.rs::task_loop::prerequisites::{CAPABILITY_NAME, capability_enabled, capability_projection}` | 模式参照 `task_prompt/handler.rs:119-142`（const + enabled + projection + RPC 检查 267） |
| 12 | capability 投影接线 | **extend** | health/capability 聚合投影（`capability_projection()` 的调用点，参照 task_prompt 的接线处） | executor 实现时定位聚合点 |
| 13 | work_next 派工过滤（第二批） | **extend** | `handle_task_work_next` @ `rust_ext/src/daemon/task_collab_lease.rs:929` | RPC 入口 `task.work_next` @ `dispatch.rs:3596` |
| 14 | MCP 透传 | **reuse**（零改动） | `task.next_action` RPC @ `dispatch.rs:1588-1599` → MCP/CLI 透传 dict | Rust 加字段自动流经，Python 层无感知 |
| 15 | 事务/锁模型 | **reuse** | `claim.rs` tx + `conn.lock()`（claim.rs:30-40） | gate 放 tx 内 insert 前，锁顺序沿用既有单锁 |

## 冲突/回环检查

- 锚定结果与 R1 图无实质冲突：判定引擎 new、投影/claim/work_next extend、数据层全部 reuse
- 文件冲突矩阵：T1(prerequisites.rs 新文件) / T2(next_action.rs) / T3(task_collab_query.rs) / T4(claim.rs) /
  T5(prerequisites.rs 声明 + 聚合投影接线) —— T1 与 T5 同文件，T5 只追加 capability 常量与函数，
  串行执行（blocked-by T1）
