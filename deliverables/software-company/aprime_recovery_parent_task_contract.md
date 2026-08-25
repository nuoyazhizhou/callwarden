# A′ 逐链路 Rust daemon 迁移恢复（MCP/CLI 渐进切换）

**任务类型：** Epic 子任务 / A′ 恢复父任务  
**父任务：** `T-1787203926824-9f873bfc`  
**工作区 authority：** `workspace_id=1`，`workspace_instance_id=ws-1`  
**创建角色：** executor / planner  
**交接起点：** reviewer  
**版本：** A′ v1，2026-08-21

> 本任务把 Callwarden 从“Python 兼容路径与 Rust HTTP daemon 混合、旧 S2 范围陈旧”的状态，转为**一次只迁移一个 MCP 工具或一条 CLI 链路**、三角色独立治理、可滚动派发且可由状态机闭环的 A′ 流水线。它是两个旧 S2 的合法 successor；旧任务历史不删除、不改写。

## 1. 已核验基线与 supersede 关系

当前权威工具迁移矩阵的基线为 **129 `rust_native`、70 `python_compat`、40 `task_rpc`**。A′ 范围中的 70 个 `python_compat` 工具将被重新分解为 `MCP-001` 至 `MCP-070` 的逐工具卡；不根据两个旧 S2 中已陈旧的“79 工具、9 已完成”文字伪造进度。已经落在 `rust_native` 的工作仅作为矩阵历史事实，不重新认领。

| 旧任务 | 现状 | A′ 处理 |
|---|---|---|
| `T-1787203937201-0a156564` | S2-original，`open`，无 workspace binding | 在本任务创建并绑定后，以正式 append-only `task.supersede` 收口。 |
| `T-1787209948470-a59bcf9c` | S2-rebuilt，`open`，绑定 `workspace_id=1 / ws-1` | 在本任务创建并绑定后，以正式 append-only `task.supersede` 收口。 |
| `T-1787203937193-0993d120` | S1-original，`review` | 不属于本任务 supersede 范围，保持不变。 |
| `T-1787203937203-0a795c68` | S3 retirement | 保留为 retirement 工作，不作为恢复前置，不删除、不 supersede。 |

任何 supersede 必须由具备 adjudicator identity、有效 reviewer lease/fencing counter、同一 workspace authority 和 evidence manifest/hash 的正式 daemon 路径执行。**本任务的创建不自行执行 supersede。**

## 2. A′ 流水线与滚动窗口

A′ 将工作按 `port_type` 分为 `control_plane`、`read_only_query`、`index_projection`、`protected_mutation`、`governance` 与 `retirement`。每张任务卡必须只覆盖一个 MCP 工具或一条 CLI 链路，并明确 Python 入口函数、目标 Rust `.rs` 文件和函数、dispatch/capability 变更、fixture、负向测试、矩阵更新条件、evidence manifest 和 successor rule。

| 阶段 | 可建任务 | 强制门禁 |
|---|---|---|
| Phase 0 | 本恢复父任务完成绑定后，收口两个陈旧 S2 | successor 必须先存在且同 workspace authority。 |
| Phase 1 | 仅 `CLI-01 [control_plane Gate]` | 不建 MCP 卡；不 reopen S1；不处理 `cli/main.py` 296 处引用清理。 |
| Phase 2+ | 单条 CLI 或单个 MCP 逐链路卡 | 同 `port_type` 的前置 Gate 必须真实为 `applied`，不能以文本 PASS 或 ACCEPT 替代。 |
| 所有阶段 | reviewer/adjudicator 治理动作 | 必须保持 executor、reviewer、adjudicator 独立；Adjudicator 完成 `ACCEPT → lease → apply → close → COMPLETE`。 |

滚动窗口规则：一个 port_type 的 Gate 仍处于 `open`、`in_progress`、`review` 或仅有文本 ACCEPT 时，Planner 不得创建或派发该模块的后继工作。后继卡仅在 gate 的 `task.apply` 已成功且只读状态验证为 `applied` 后创建；每次只释放满足该约束的一张首卡或一条可独立工作的链路。

## 3. 三角色循环合同

三份可供独立窗口启动时固定加载的版本化模板已与本任务冻结。每个角色周期性从 Epic `T-1787203926824-9f873bfc` 的 A′ 树查询属于其身份的 `task.next_action`，只领取状态机明确允许的下一件工作；无候选时返回 idle 并在下周期重新查询。

| 角色 | 启动模板 | SHA-256 | 正确完成定义 |
|---|---|---|---|
| Executor / Planner | `deliverables/software-company/aprime_role_contracts/executor_planner_startup_v1.md` | `59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6` | 步骤、测试、证据完成，并提交 `executor_ready_for_review`；不 apply/close。 |
| Reviewer | `deliverables/software-company/aprime_role_contracts/reviewer_startup_v1.md` | `6415033D8F134392DE16FCA130BFB762CB6C70D9F466C770EC18A20FC4CE139E` | 独立复审后以 `reviewer_pass` 转交 adjudicator，或 `reviewer_blocked` 退回 executor；PASS 不是终态。 |
| Adjudicator | `deliverables/software-company/aprime_role_contracts/adjudicator_startup_v1.md` | `42A5F1DEFA81008B009058C1BAF5D1A14B3EF4521E291B7B55C19BB473A77C3E` | ACCEPT 后必须取得有效 lease/fencing，执行并核验 `apply → close → task.next_action=COMPLETE`。 |

`ACCEPT`、`PASS`、聊天中的“完成”或未执行的交接提示均不改变状态机。任何 authority、lease、fencing、evidence、independence、未完成步骤或未解决 finding 缺失必须 fail-closed，不能用 local SQLite 或直接改状态绕过。

## 4. 首卡：CLI-01 control-plane Gate

本恢复父任务创建之后，只允许创建并挂载以下首卡：

- **标题：** `CLI-01 [control_plane Gate]：manifest/health/capability 可观测性恢复`
- **Python 入口：** `server/daemon_client.py` 中 manifest 验证与 daemon health/capability 调用路径。
- **Rust 目标：** `rust_ext/src/daemon/http_server.rs` 与 `rust_ext/src/daemon/dispatch.rs` 中对应的 manifest、health、capability 请求处理和路由。
- **禁止范围：** 不处理 `cli/main.py` 的 296 处引用清理；这属于 S1-review 边界，不能搭车纳入 CLI-01。
- **验收：** authority-aware manifest 与健康检查实际 daemon round-trip；能力清单与 dispatch 路由一致；可复现 fixture；至少一个协议/身份/不可用 daemon 的负向路径；通过后才满足 `control_plane` successor rule。

## 5. 本父任务步骤与验收

本父任务自身不实施 70 个工具的生产迁移。它负责维持可审计的 A′ 治理合同、绑定、首卡范围和后续滚动派发约束。

1. 以 daemon `task.create` 在同一事务创建本任务、至少一个治理步骤、不可变 `task_workspace_bindings`、authority capture 与三份 role contracts。
2. 只读核验父子链、`workspace_id=1 / ws-1` binding、三份 role contract 的 prompt hash、两个旧 S2 的可 supersede 前置条件，以及 CLI-01 的独立 `control_plane` scope。
3. 由具备权限的 Adjudicator 在后续独立动作中收口旧 S2；本任务不得自行标称 supersede 已完成。

本父任务在所有派生卡片完成后才能进入最终 review/apply/close；它不可因为创建成功或出现文本 ACCEPT 而直接闭环。
