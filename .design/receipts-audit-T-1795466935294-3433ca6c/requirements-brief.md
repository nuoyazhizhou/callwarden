# requirements-brief — receipts 审计

- **feature**：receipts 审计：build_graph 成功/失败/转后台均落可验证审计回执
- **R 阶段**：R0 需求澄清 + 复杂度预检 + 方案对比
- **生成时间**：2026-11-24 15:32:19
- **依据 HEAD**：`1fbcd9bad21d58b7ed76e7e1710883213d5495cc`（master；工作树另有在途 dirty/未跟踪文件，均非本 feature 产物，不纳入本设计基线）
- **任务**：`T-1795466935294-3433ca6c`；workspace_id=1 / instance=`4baea3ff12c2ea5c`
- **追溯映射**：见 `task-map.md`（本文不重复维护映射表）

## 1. 需求原文（daemon open 卡，未改写）

> 对标 GitNexus 的 receipts（构建回执可审计）。现状：刚落地的 build_flight 已有 build_seq/completed_at/lastBuild（仅内存），daemon 重启即丢，无法事后审计"谁在什么时间对哪个 workspace 建了什么、耗时多少"。目标：1) build_graph 完成（成功/失败/超时转后台）时写审计回执到 audit_log（字段：build_seq、root、scanned/inserted/unchanged/deleted、耗时、ok/error）；2) 复用现有 audit_verify_chain 保证链完整性；3) 提供按 workspace+时间范围查回执的读面（复用既有审计查询，不新增 MCP 工具）。验收：成功/失败两条路径各一条审计记录；重复读幂等；既有审计测试不回归。

## 2. 需求拆解：card 已明确 vs 未决

### 2.1 card 已明确（confirmed）

| 编号 | 明确项 | 依据 |
|---|---|---|
| C-1 | 三条完成路径（成功 / 失败 / 超时转后台）均需落回执 | 目标 1 原文 + `work_order.objective` |
| C-2 | 回执字段集：build_seq、root、scanned/inserted/unchanged/deleted、耗时、ok/error | 目标 1 原文 |
| C-3 | 复用 `audit_verify_chain` 保证链完整性，不自建链 | 目标 2 原文 |
| C-4 | 读面复用既有审计查询，不新增 MCP 工具 | 目标 3 原文 |
| C-5 | 验收下限：成功/失败各一条记录、重复读幂等、既有审计测试不回归 | 验收原文 |
| C-6 | implementation scope 锚点：`BuildFlights::finish` 与 `run_build_graph` 出口 | `work_order.objective` |
| C-7 | 冻结 step 共 3 步，target 文件 `build_flight.rs` / `fs_handlers.rs` | daemon task.status 返回的 steps |
| C-8 | 身份策略 `legacy_identity_v1`；required_role=executor；lease_role=implementer | governance 投影 |

### 2.2 未决 / 需假设（见 §6 三态表）

- audit_log 语义分叉（独立 `audit_log` 表 vs 主库 `write_audit_events` 审计面）——**open，阻塞 step 1 落库选型**。
- "超时转后台"路径的回执时机（转后台Accepted 时落一条 vs 后台真正完成时落一条）——**open，阻塞 AC-03 落点**。
- 读面"按 workspace 过滤"的具体字段承载方式——**assumption，需 executor 在计划中确认**。

## 3. 复杂度预检（cw-planner-architect）

| 判据 | 结论 |
|---|---|
| 涉及独立模块数 | 2 个 Rust 模块（`fs_handlers::build_flight` + `fs_handlers` 审计面）+ 审计子系统读面（复用，不改）→ 未超阈值但跨审计域 |
| 是否同时改 schema/daemon/CLI/MCP | 否：`forbidden_paths` 明确排除 out-of-scope schema change；design 约定 **不新增表/列/索引/工具** |
| 独立验收目标数 | 3（写回执 / 链完整 / 读面幂等）→ 对应 3 个冻结 step，一一映射 |
| 实现步骤数 | 3（= 冻结 step 数，未超 5） |
| ownership | 单一 executor（`rust_ext/src/daemon/fs_handlers.rs` + 其子模块 `build_flight.rs` 同 ownership） |
| 巨型文件门禁（规则 47） | `fs_handlers.rs` 为超线文件（见下注）；本任务必须在既有文件内增改，须提出拆分建议但不阻断 |
| 失败后原 scope 可修复 | 是，write 路径闭环在同一对文件内 |

**预检结论**：**非 atomic_hotfix**（跨 2 文件 + 3 验收目标 + 触及审计链），但**单一 ownership、冻结 3 步与原 plan 一致、未新增 schema/MCP**，可由一个 executor 按现有 binding 直接开工，无需拆子任务（详见 `contracts.md` §4）。

> 注：`fs_handlers.rs` 为已知超线文件（AGENTS.md 规则 47 硬/灾难线清单语境）。按规则 47，Executor 须在 report 记录其当前行数并提出可执行拆分建议（按 build_flight / 审计面 / build_graph 职责切分），由 Planner 决定本任务内是否拆分——本设计明确**不在本任务内拆分**（会放大 scope、破坏冻结 step 的 target 锚点），立为相邻技术债提示。

## 4. 方案对比（≥2）+ 被否决理由

### 方案 A（推荐）：写入 `write_audit_events` + `audit_chain`，回执 payload 放 `detail_json`
- **做法**：在 `BuildFlights::finish` 调用点（后台线程 `flights_bg.finish` 前，见 `fs_handlers.rs:1701-1705`）新增一次审计 append：事件类型取合法枚举内的 `commit`，把 `build_seq/root/scanned/inserted/unchanged/deleted/耗时/ok/error` 序列化进 `detail_json`；`sign_audit_chain(conn,"remote_write_events",...)` 自动挂链。读面复用 `audit_verify_chain(table_name="remote_write_events")` + 既有 `write_audit_events` workspace 过滤查询。
- **优点**：①唯一同时满足"链完整（audit_verify_chain 原生支持）"+"按 workspace 过滤（表已有 workspace_id 与 idx_write_audit_events_* 索引）"+"不新增表/MCP 工具"的现成面；②append-only 触发器已就位；③`detail_json` 无需 schema 变更即可承载全部回执字段。
- **失效模式**：`event_type` CHECK 仅允许 `grant/commit/reject`、`outcome` 仅 `allow/deny`，build 回执需映射到 `commit`/`allow`（成功）与 `commit`/`deny`（失败），语义上是"借壳"，须在 detail_json 明确 `kind=build_receipt` 区分；读面"时间范围"需确认既有查询是否暴露 start/end（见 OPEN-03）。

### 方案 B：写入独立 `audit_log` 表（`audit_log_handlers.rs::handle_append`）
- **做法**：把回执写到 `audit.db` 的 `audit_log`，字段塞进 `details` JSON，`event_type` 取 `build_graph`，读面复用 `handle_query(start_time/end_time/event_type)`。
- **优点**：`handle_query` 已原生支持 start_time/end_time/event_type 过滤，读面最省事；`details` 为 JSON 可承载字段。
- **被否决理由（决定性）**：①`audit_log` 表**无任何签名链列**，也不写入 `audit_chain`，`audit_verify_chain(table_name=...)` 无法验证它——直接违反 C-3/目标 2；②`audit_log` 在独立 `audit.db`（`audit_db_path`），与 `write_audit_events`/`audit_chain` 不在同一库，无法用同一链保证完整性；③`audit_log` 无 workspace 结构字段（actor_uid/action/target），"按 workspace 过滤"只能靠 `details` 字符串，不如方案 A 的原生列+索引。为让方案 B 满足 C-3 需改 audit_log handler 增加签名链写库——属于 out-of-scope schema/生产改动，被 `forbidden_paths` 排除。

### 方案 C：新建专用 `build_receipts` 表 + 专用签名链分区
- **做法**：新建 build_receipts 表，字段一一对应回执字段，`sign_audit_chain(conn,"build_receipts",...)` 挂链，新增只读查询。
- **被否决理由**：①新增表 = schema 变更 = `forbidden_paths` 明确排除的 "out-of-scope production/schema changes"；②新增读面违反 C-4 "不新增 MCP 工具"；③与冻结 3 步 scope 不符。若未来 receipts 成为一等治理对象，可立独立技术债任务，不在本任务。

**结论**：采用方案 A。方案 B 因签名链硬缺口被否；方案 C 因 schema/工具禁令被否。

## 5. 异常场景矩阵（8 类）

| # | 场景 | 触发 | 期望行为 | 回执/链影响 | 证据采集 |
|---|---|---|---|---|---|
| E1 | 成功完成 | `run_build_graph` 返回 Ok | 后台线程 `flights_bg.finish(ok stats)` 前 append 一条回执，ok=true | 1 条回执 + 1 条链记录 | 单测断言回执 row + chain row 各 +1 |
| E2 | 构建内部失败 | `run_build_graph` 返回 Err（workspace 映射失败/DB 错误） | `stats={ok:false,error,message}`，`finish` 前 append 回执，ok=false | 1 条失败回执（不得静默丢失） | 负向单测：注入 failure 断言回执落库且 error 字段存在 |
| E3 | 超时转后台 | `rx.recv_timeout` 到点（默认 15s） | 立即返回 `accepted=true`；回执在后台**真正跑完**时由 worker 落（时机见 OPEN-02） | Accepted 当下不落回执；完成时落 1 条 | 集成测试：设 `CALLWARDEN_BUILD_WAIT_MS=0`，轮询 dry_run 收敛后查回执 |
| E4 | codegraph db 未配置 | `codegraph_db=None` | `flights.finish({ok:false,...})` 后返回错误 | 该提前返回分支**当前不落回执**，须补 append（与 C-1 一致的失败路径） | 分支单测：确认该退出点也 append |
| E5 | 后台线程 spawn 失败 | `thread::Builder::spawn` Err | `flights.finish({ok:false,...})` 后返回错误 | 同 E4，须补 append | 分支单测/spawn 失败注入 |
| E6 | 重复 begin（单飞冲突） | 同 workspace 已有构建 | 返回 `build_in_progress`，**不**新增回执（无新构建发生） | 0 条回执（正确：不该有重复构建记录） | 单测：重复调用后回执计数不变 |
| E7 | 审计写入失败 | audit DB 锁/不可写 | 沿用 `E_WRITE_AUDIT_UNAVAILABLE` fail-closed 语义，**不得**吞异常伪造成功 | 回执与链均不落（SAVEPOINT 原子），错误上抛 | 负向单测：注入 append 失败断言无半成品 |
| E8 | 既有审计测试回归 | 跑既有 `test_audit_*` / `test_mcp_audit_verify_chain_http_rpc` 等 | 全绿、枚举/链 hash 契约不变 | 不改变 `remote_write_events` 既有行与签名序列 | 回归命令见 `ac-map.md` AC-06 |

## 6. 未决项三态

| ID | 状态 | 内容 | 阻塞 | 消解方式 |
|---|---|---|---|---|
| U-01 | **open** | `write_audit_events.event_type` CHECK 仅 `grant/commit/reject`，build 回执须借 `commit` 壳；确认此映射是否被接受，或需扩大枚举 | 阻塞 step 1 落库选型与 AC-04 | 推荐方案 A 借壳（detail_json 标 `kind=build_receipt`）；若团队拒绝借壳 → 需 schema 变更 → 触发重规划（out-of-scope，交 Planner） |
| U-02 | **open** | 超时转后台回执落点：Accepted 当下 vs 后台完成时刻 | 阻塞 AC-03 精确落点与测试断言 | 推荐"后台完成时落"（那时才有 scanned/inserted/耗时真实值）；Accepted 当下无真实统计字段可写。若验收要求 Accepted 也落，需定义占位语义 → 交主理人/用户决策 |
| U-03 | **assumption** | "按 workspace+时间范围读面"：`write_audit_events` 有 workspace_id 与 type 索引但**未实证存在 start/end 时间范围的现成 RPC**；假设复用既有 workspace 过滤查询 + 时间条件，或经 `audit_verify_chain` 的 table 视图 | 阻塞 AC-05 读面实现方式 | Executor 在计划中确认既有读面是否完整支持；若不支持且不允许新增工具 → 记为 capability gap 交 Planner，**不得**擅自新增 MCP 工具 |
| U-04 | **confirmed** | 冻结 3 步的 `check_items` 落库为转义 JSON 字符串（G6.3 反模式），但 contract 已 frozen（hash 固定） | 不阻塞开工 | 本设计不改历史 contract；在 `task-map.md` 登记为既有格式瑕疵，交后续维护路径 |
| U-05 | **confirmed** | 三份共享门禁 `cw-gates.md` 位于用户级 skill 目录、三份 md5 一致（`dbd5e43e...`） | 无 | 已核验，作为本轮门禁依据 |

## 7. 风险与依赖

| 风险 | 等级 | 缓解 |
|---|---|---|
| 审计落库子系统选错（audit_log vs write_audit_events） | 高 | §4 方案对比 + U-01；executor 计划阶段必须锁定方案 A 或上报 |
| 签名链序号被破坏导致既有审计测试回归 | 高 | 复用 `sign_audit_chain` 单一写入口；AC-06 回归门禁 |
| 共享工作树在途 dirty 文件被误吸收 | 中 | planner 只写 `.design/`；executor 须 whitelist capture（规则 48/40） |
| Windows daemon 编译≠部署（规则 43） | 中 | 本 feature 触及 Rust daemon 行为，executor 须按 `refresh_shared_runtime.ps1` 部署门禁；workspace runtime_policy 非 self_bootstrap 时按任务合同，若无 deployment 声明则构建+测试即可（待 executor 核实） |
| 超时转后台回执字段无真实值 | 中 | U-02 推荐后台完成时落 |
| 读面能力缺口 | 中 | U-03；不新增 MCP 工具是硬约束 |

## 8. Handoff 预告（planner → executor，经主理人中转）

- from_role=planner；next_action=`claim_current_step`（现有冻结 step 0，S-1795466935297-34636a9c）
- 计划修订：**沿用现有 binding，不新建子任务**（见 `contracts.md` §4 / `task-map.md`）
- Executor 开工前须消化 U-01/U-02，并在 implementation plan 中锁定方案 A 的借壳映射与回执落点
- 独立性预告：Phase 2 executor 与 Phase 3 reviewer 必须使用**不同 `agent_instance_id` 且不同 `session_id`**，并各自以自有 session 调 `agent.register`；`check_role_independence`（task_collab.rs:565）按 instance/session 匹配冲突角色，共用会被 daemon 拒绝认领
