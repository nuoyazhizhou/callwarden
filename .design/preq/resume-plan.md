# 前置条件告知（preq）— 续开发计划（design-only 复核）

- Feature: `preq` ／ 阶段: R5 续开发设计复核（不写生产代码、不 apply/close/claim、不改 daemon 任务状态）
- 生成时间: 2026-11-26 12:59 UTC（epoch ≈ 1795697955）
- 依据 HEAD: `0d6fa326a75451c1f5aeadaf3c04a691af3a0a3c`（本地 master 实测，**非** 交接单所述 `1b2121d`；`1b2121d` 为 HEAD~1）
- 冻结设计基线: `8b1a1e3ee4a2a98f5cb7707d705bd7414a036abe`（本轮只读，未改）
- 身份声明:

```
Role: planner
Task: T-1795661715269-f3611f48（父任务，设计基线复核）
Skill: cw-planner-architect（项目无该 skill，用 none + requirements-cw / cw-planner 纪律）
Allowed: 只读 .design/preq/、git、cw task show/status-tree、daemon 只读查询；产出续开发计划文档
Forbidden: 写生产代码、apply/close/claim、修改在途未提交改动、改 daemon 任务状态
Handoff: team-lead（主理人钟守一）
```

> 证据来源：`cw task_status_tree(T-1795661715269-f3611f48)`、`lease_status` / `lease_list_events` / `task_assignment_status`(T3)、`get_edit_history`(task_collab_query.rs)、`get_active_workspace`、Git Bash 只读查询（HEAD / status / diff / log / merge-base / worktree list / branch --contains）。全部为只读。

---

## 0. Phase 0 复核结论（供主理人校准）

| 项 | 交接单值 | 本轮实测 | 结论 |
|---|---|---|---|
| daemon 健康 / 三元绑定 | 1193 / 1 / 4baea3ff… active | `get_active_workspace`: registry_workspace_id=1193、task_db_workspace_id=1、instance=4baea3ff12c2ea5c、status=active | ✅ 一致，复用 |
| 技能门禁 cw-gates ×3 | 齐全 | 未重复折腾，沿用 | ✅ |
| git HEAD | `1b2121d` | `0d6fa32`（`1b2121d` 之上多一提交 `0d6fa32 fix(daemon): D3 自愈守卫`，属另一功能线 T-1795507699058-41d9fcbc，已提交） | ⚠️ **漂移**：后续 worktree 基线必须重取 |
| 工作树在途改动 | fs_handlers.rs +955、build_journal.rs 新文件，原样保留 | `git status`: fs_handlers.rs 修改（diff +953/−76）、build_journal.rs 未跟踪；另有若干 `.trae_*.js` / `.cw_query.py` 未跟踪 | ✅ 确认存在，**本轮零触碰** |

---

## 1. 状态对照表（7 单 + 父任务）

冻结期望来自 `task-map.md` 批次 1；真实值来自 daemon `task_status_tree`（evaluated_at ≈ 1795697858 / 12:57:38 UTC）。

| # | task_id | REQ | 冻结期望（提交 8b1a1e3） | daemon 真实 | 差异定性 |
|---|---|---|---|---|---|
| 父 | `T-1795661715269-f3611f48` | — | open，等 7 单齐 | open / governance_blocked / progress **3/7 = 42.86%** / 无步骤 | ✅ 一致（父任务靠子单推进，本就 open） |
| T7 | `T-1795663574847-eaea3d24` | 06 should | review（文档先行，无依赖） | **review** / review_pending / step done / 有 `executor_ready_for_review` handoff（he-8e8d9fe） | ✅ 一致，待独立 reviewer |
| T1 | `T-1795663519023-eb82b9a8` | 04 must（根） | review | **review** / review_pending / step done / 交付 commit `e133f34`（在 origin/master）/ 无 handoff（诊断 no_handoff） | ✅ 一致，待独立 reviewer |
| T2 | `T-1795663530764-a75be4a4` | 01 must | review | **review** / review_pending / step done / 权威交付 `40a21d4`（origin/master）/ 有 handoff（he-6665de2b）/ ⚠️ **双实现披露** | ⚠️ 状态一致，但有未裁决分叉（见 §5 O-1） |
| T3 | `T-1795663535104-aa05bd64` | 01 must | 应与 T1 并行完成后进 review | **in_progress** / step `S-1795663535108-aa49c3d8` in_progress、result 空、progress 0% / 僵尸 lease | ❌ **卡死**：前会话占位未释放（见 §2） |
| T4 | `T-1795663549865-19deda64` | 02 must | open→认领→review | **open** / queued / step `S-1795663549870-1a2acc30` pending | ✅ 一致（尚未认领） |
| T5 | `T-1795663552556-ba42830c` | 03 must | open→认领→review | **open** / queued / step `S-1795663552561-ba8cbb0c` pending | ✅ 一致 |
| T6 | `T-1795663571791-34b69f20` | 01 must | open，等 T1–T5 齐 | **open** / queued / step `S-1795663571796-3508971c` pending | ✅ 一致（前置未满，正确未启动） |

小结：3 单在 review（T1/T2/T7）、1 单僵尸 in_progress（T3）、3 单 open（T4/T5/T6）、父 open。**放行判据（7 单齐 review + 无 E_* + ≥1 单 apply/close）尚未满足。**

---

## 2. T3 占用判定：僵尸 lease（非活跃占用）

### 2.1 证据（全部只读）

| 证据源 | 值 |
|---|---|
| `lease_status(T3)` | status=**active**（行状态，非按过期求值）／ lease_id=`L-0c812edaf8ac5dcc` ／ role=executor ／ agent=`callwarden-exec-worker-46762b` ／ session=`sess-executor-alm-46762b-regprereq` ／ model=claude-sonnet-4.6 ／ fencing_counter=**1** ／ acquired_at=1795688694（10:24:54）／ **expires_at=1795692294（11:24:54）** |
| `lease_list_events(T3)` | **仅 1 条** `acquire`（10:24:54，fencing 1）；**无 renew、无 release** |
| `task_assignment_status(T3)` | status=**claimed** ／ holder_session=`sess-executor-alm-46762b-regprereq` ／ claimed_at=last_heartbeat_at=**1795688682（10:24:42）** ／ 此后无心跳 |
| `task_status_tree(T3)` | status=in_progress ／ step result=**空串** ／ completed_at=null ／ progress=**0%** ／ claimed_by=`sess-executor-alm-46762b-regprereq` |
| `get_edit_history(task_collab_query.rs)` | **空**（该会话对 T3 目标文件无任何落盘编辑记录） |
| 当前时刻 | 1795697870（12:57:50）→ lease 已过期 **≈1h33m**，心跳停滞 **≈2h33m** |

### 2.2 定性

**僵尸 lease（zombie）。** 判定依据：lease 到期 93 分钟、无续租事件、心跳停 2.5 小时、step 结果为空且进度 0%、目标文件零编辑记录；占用会话 `sess-executor-alm-46762b-regprereq` 正是前会话 executor（它已把 T1/T2/T7 送完并结束），T3 是它认领后未做完就退出留下的残留。daemon 的 `lease_status` 显示 active 是**行状态**而非按过期求值，`assignment` 仍 claimed，故 T3 卡在 in_progress 而不会自动回退——不是有人在干活。

### 2.3 建议恢复路径（**建议，不执行**）

1. 由**新的独立 executor 会话**（新 agent instance / 新 session，禁止复用 `sess-executor-alm-46762b-regprereq`）对 step `S-1795663535108-aa49c3d8` 走正常 `task.claim`。T3 治理投影显示 `decision=READY / action=CLAIM / required_role=executor / lease_required=true / fencing_required=true / different_session_from=[]`（无身份排斥），新会话在契约上可认领。
2. 因旧 lease 已过期且 fencing_counter=1，新 claim 应以 **fencing_counter=2** 取得新 token 并自动使旧 token 失效。**不得手工改 DB 或强release**，除非 daemon 暴露正式的过期回收（reap）路径。
3. ⚠️ **待验证风险**：`lease_status` 仍报 active，若 claim 以「lease 仍被持有」为由拒绝（E_LEASE_HELD / E_LEASE_CONFLICT），需走 daemon 的 lease-expiry reclaim 路径而非强清。此路径的可用性由 executor/主理人在认领时实测确认，本设计不预设。
4. 因 step 结果为空、目标文件无编辑记录，重认领是**干净重做**，无半成品需要 reconcile。

---

## 3. 续开发执行顺序（三条链）

```
链A 评审闭环（T1/T2/T7，已在 review）
    → 3 个「新会话 + reviewer 角色」并行独立评审
    → PASS → adjudicator 裁决 → apply/close 全链路（≥1 单即可解锁放行判据）
链B 认领实现（T3 僵尸恢复 + T4/T5 open）
    → 新独立 worktree（基线见 §5 O-2）→ T3 重认领、T4/T5 并行认领
    → 各自 implement → 送 review → 进链A 的评审闭环
链C 集成测试（T6）
    → 等 T1–T5 全部到 review 后再启动（blocked-by 五单，当前正确保持 open）
放行判据达成 → 注入 T8（批次2 work_next，REQ-PREQ-05 / AC-13，排序规则 open 项须先定）
```

**具体顺序建议：**

1. **并行立即启动链A 三单评审**（T1/T2/T7）。三单互不依赖，各派一个**新会话 reviewer**；身份约束见 §5 O-3。T2 reviewer 必须先处理双实现裁决（O-1）。
2. **并行启动链B 实现**。先解决 worktree 基线分裂（O-2）——这是 T4/T5/T6 能否编译的前提（它们依赖 T1 的 `prerequisites.rs`）。基线一旦定，T4（claim.rs）、T5（dispatch.rs）并行认领；T3 按 §2.3 恢复后认领。
3. **T3/T4/T5 送 review 后启动 T6**（端到端集成测试），它消费 T1–T5 全部产出。
4. **放行判据**：7 单全部 review + 全程无 E_* 门禁错误 + ≥1 单完成 apply/close 全链路 → 注入 T8；T8 前先定 work_next 推荐排序规则（brief 未决项 O2 / 本计划 O-4）。

> 注：`blocked-by:` 仅为 description 文本约定，daemon 不强制拦截（见 task-map）。T6 虽显示 open+queued，但必须由主理人**显式**卡住「T1–T5 全到 review 后才认领」，防抢跑。

---

## 4. T4 / T5 contract 摘要（供 executor 冻结 scope）

来源：`contracts.md` 卡 5 / 卡 2；锚定见 `symbol-anchor.md` #9/#11/#12。

### T4 — claim 硬依赖门禁（卡 5，REQ-PREQ-02，AC-07/08/09/10）

- **符号锚**：新增 `check_prerequisites_gate(tx, workspace_id, task_id, step_id, enabled) -> Result<Option<Vec<PrerequisiteItem>>, ClaimDomainError>` 与常量 `ERR_PREREQUISITE_NOT_MET = "E_PREREQUISITE_NOT_MET"`；插入点 `rust_ext/src/daemon/task_loop/claim.rs::write_domain`（:105）step6 binding 链校验（:169-173）之后、step7 INSERT（:175-186）之前。
- **依赖符号**：卡 1 引擎 `evaluate_task_prerequisites` + 卡 2 `capability_enabled()`；`claim.rs:105/169-186/205`。**T1 引擎已在 origin/master `e133f34` 实现**，但**不在本地 master 工作树**——worktree 基线必须含它（O-2）。
- **验收点**：
  - AC-07 enabled + 硬依赖未满足 → `E_PREREQUISITE_NOT_MET`，message 含每个 provider_task_id 与 unmet_reason 指路 provider；无 lease 插入、任务状态与步骤零变化（无副作用断言）。
  - AC-08 provider=closed（+artifact fresh）→ 正常放行。
  - AC-09 仅信息性边（is_hard=0）未满足 → 放行，响应附 `prerequisite_warnings`。
  - AC-10 enabled=false → 与存量 claim 行为逐字节一致。
- **异常路径**：引擎 DB 读失败 → Infrastructure + 外层事务回滚 + 无部分写入（异常矩阵 #5，不静默放行）；E_PREREQUISITE_NOT_MET 路径 dry_run=true 无副作用；gate 本身无写。
- **GWT 要点**（daemon check_items 已固化 5 条）：失败分支+无副作用、通过分支、警告不阻断、兼容分支、DB 错误回滚；测试 `cd rust_ext; cargo test claim`。

### T5 — capability 聚合接线（卡 2，REQ-PREQ-03，AC-11）

- **符号锚**：`rust_ext/src/daemon/dispatch.rs::capability_projection`（daemon target_symbol `daemon::dispatch::capability_projection`），聚合点 :450（`task_prompt::handler::capability_projection()` 紧邻）追加一行 `prerequisites::capability_projection()`。声明本体 `CAPABILITY_NAME="prerequisite_gate_v1"` / `CAPABILITY_SCHEMA_VERSION="prerequisite_capability_v1"` / `capability_enabled()` / `capability_projection()` 已由 **T1 在 prerequisites.rs 实现**（卡 2 声明部分随 T1 交付，T5 只做接线）。
- **依赖符号**：T1 的 capability 声明；`dispatch.rs:450`。同样受 O-2 基线约束。
- **验收点**：AC-11 `capability` 投影暴露 `{name: prerequisite_gate_v1, schema_version: prerequisite_capability_v1, enabled: true}`（三键齐）；接线冒烟 → capabilities 列表含该 projection；回归 → 既有 `role_prompt_compiler_v1` 投影不受影响。
- **异常路径**：无（声明函数不失败、无新错误路径、无副作用）。
- **GWT 要点**：聚合投影含三键 + 回归分支；测试 `cd rust_ext; cargo test capability`。

> scope 边界提醒：T4 只动 claim.rs、T5 只动 dispatch.rs:450（追加一行）；两者**不得**重复实现 prerequisites.rs 声明（T1 已交付）。与 symbol-anchor #12「T1 与 T5 同文件、串行」的描述相比，真实 daemon 建单把 capability 声明并入 T1、T5 仅接线——以 daemon 现有 step 卡为准，不再改单。

---

## 5. 风险与未决项（三态）

状态定义：`已定`=无需再决策；`待定`=有合理默认但需主理人/相关角色确认；`阻塞`=未解则卡住对应步骤。

| ID | 状态 | 内容 | 阻塞哪一步 / 处置 |
|---|---|---|---|
| O-1 | **待定** | **T2 双实现分叉**：权威交付 `40a21d4`（origin/master，6 内联测试、带证据报告 req-f766513b6727）vs 平行实现 `b950e36`（分支 `run/t2-parallel-b950e36`，+380/−3、manifest f1b5fbb），executor 原文「二选一交 reviewer/Planner 处置」 | 阻塞 T2 评审结论。**本设计不裁决**；建议由独立 reviewer 以冻结卡 3（AC-04/05：字段恒存在/空数组/五字段 shape/引擎失败 fail-closed）为尺，择一为交付物并让另一实现退出工作树（该分支挂在独立 worktree `run-20261126-164401-a75be4a`，可物理隔离不碰） |
| O-2 | **阻塞** | **worktree 基线三分叉**（实测 `merge-base(HEAD, origin/master)=21a8e10`）：①本地 master `0d6fa32` 有 .design/ 基线提交（8b1a1e3…）但**无** T1 引擎 `prerequisites.rs`、无 T2 代码、无 T7 §8 文档；②origin/master `40a21d4` 有 T1/T2 代码但**无** .design/ 基线、无 T7 文档、无 `747ab9a`(workspace 修复)/`0d6fa32`(D3 修复)；③T7 交付 `147742e` 是**孤儿提交**（parent=b463184，`git branch -a --contains` 为空，不在任何分支）。主检出树 `prerequisites.rs` 不存在 | **阻塞 T3/T4/T5/T6 实现**（T4/T5/T6 依赖 T1 引擎，从 master HEAD 起 worktree 会缺文件、无法编译）。须主理人决策新独立 worktree 基线：建议基于含 `e133f34`（T1）+`40a21d4`（T2）的线，再合入所需的 master 修复/基线，并把 T7 文档 `147742e` 显式纳入；本设计只报风险，**不指定合并策略、不执行 merge** |
| O-3 | **已定（硬约束，转达）** | **评审独立性身份要求**：T1/T2/T7 治理投影 `next_session.must_be_new_session=true`、`required_role=reviewer`、`lease_required=true`、`fencing_required=true`、`handoff_to=adjudicator`；forbidden=production edits / task.apply / task.close / task.supersede。三单实现会话为 `sess-run-20261126-113111-7bc7`(T1) 与 `sess-executor-alm-46762b-regprereq`(T2/T7) | 链A 三单**必须各派新会话 + reviewer 角色**，不得复用实现会话身份，不得写生产代码；裁决由 adjudicator 角色承担。T1 无 handoff（no_handoff），reviewer 需自行从契约+diff 取证 |
| O-4 | **待定** | work_next 推荐排序规则（provider 卡位置/数量）——brief 未决项 O2 / 卡 6 assumption | 阻塞 T8（批次2）设计，不阻塞批次1；T8 注入前定，否则 T8 仅缺推荐、可先交付过滤 |
| O-5 | **待定** | capability `schema_version` 起始值（brief 未决项，卡 2 取 `prerequisite_capability_v1` 对齐 task_prompt 惯例） | 不阻塞；executor 按 task_prompt 惯例取值，reviewer 核 AC-11 三键即可 |
| O-6 | **待定** | 两条 assumption 复验：`requires_interface` 的 active selection 判定列（schema.py:1486 实际列名）；两投影 `prerequisites` 同构（AC-06） | 由对应 executor 实现时核对；若 selection 语义不同，回卡 1 修订（revision+1），不静默改判 |
| O-7 | **已定（约束）** | 在途未提交改动（fs_handlers.rs +953/−76、build_journal.rs 新文件、`.trae_*.js`）为另一功能线，用户要求原样保留 | 全部实现走**另起独立 worktree**（见 O-2 基线）；主检出树只读，零触碰 |
| O-8 | **待定** | 父任务 `workflow_status=governance_blocked`、原因是「无 pending 步骤且无待处理 remediation」——父任务本身无步骤，靠子单推进 | 不影响子单；父任务是否需补一步骤做汇总验收，由主理人决定（本设计不建议改冻结基线） |

### 门禁四件套复核（设计基线，非本轮新增）

- 方案对比 ≥2（A/B/C 含优缺点+失效模式）✅ `requirements-brief.md` §4.1
- 被否决方案理由 ✅ §4.2（B/C 及两个子决策）
- 异常场景矩阵 8 类 ✅ §4.3
- 未决项三态 ✅ §4.4 + 本计划 §5
→ 四件套齐全，本轮无需补门禁。

---

## 6. AC 列表与证据采集方式（回传主理人）

| AC | REQ | 归属单 | 证据采集方式 |
|---|---|---|---|
| AC-01/02/03 | 04 | T1 | `cargo test -p callwarden-core prerequisites` 输出 + 测试源码（T1 已报 18 passed，待 reviewer 独立复跑） |
| AC-04/05 | 01 | T2 | `cargo test next_action` 投影断言（权威 `40a21d4` 已报 37 passed；双实现待裁决 O-1） |
| AC-06 | 01 | T3 + T6 | 两投影同构对比测试 + 端到端集成（T3/T6 未实现） |
| AC-07/08/09/10 | 02 | T4 | `cargo test claim` 门禁测试（含无副作用断言、enabled=false 逐字节回归）（未实现） |
| AC-11 | 03 | T5 | capability 投影测试 + dispatch 聚合冒烟（未实现） |
| AC-12 | 06 | T7 | v2 amendment §8 文档 diff + 链接可达（文档在孤儿提交 `147742e`，待确认归位 O-2） |
| AC-13 | 05 | T8（批次2） | `cargo test` work_next 过滤测试（deferred，放行后执行） |

---

*本文件为设计工件，未改任何生产代码、未改 daemon 任务状态、未触碰在途改动。所有决策项已三态标注并指明阻塞步骤，待主理人裁定后由 executor 在独立 worktree 执行。*

---

## 批次落定预备评估（2026-11-28 只读追加，供主理人裁定；不建单、不碰状态）

> 背景：T6 评审 PASS（verdict V-8fb9755d221a02328c571e6f，event 933），adjudication_pending。关单回执到后 7 单全部闭环，放行判据正式满足。以下为本轮只读预备评估，最终裁定权在主理人。

### 1. T8 注入形态预判

**结论：建议建单注入（单张 T8），scope 以卡 6 已冻结的过滤逻辑为主。**

- 依据：冻结 task-map 明文「批次 2（deferred，放行判据满足后注入）」——现在判据满足，按冻结计划应注入；AC-13（deferred）由 T8 兑现。
- scope 切分：卡 6 已冻结部分（hard_unmet 非空卡不派工、引擎失败 fail-closed 整体报错、无硬依赖卡行为与存量一致）为**必交付**；provider 卡推荐与排序规则（brief O4 / 本计划 O-4 仍 open）按 brief §4.4 明文「不定则第二批仅缺推荐不阻塞」处理——推荐子项在 T8 卡内标注 deferred 或最小实现，排序规则另定后追加。
- 冲突面事实：`task_collab_lease.rs` 在 master 独有提交与 preq 线（T6 集成线改动文件清单）中**均无改动**，T8 是全新改动面、零冲突风险。
- 前置：O-2 合并收口（T8 需基于含 T1–T7 的完整 preq 线，见 §3）。

### 2. T9 / T10 / T11 立单及时机建议

| 候选 | 建议 | 时机 | 理由 |
|---|---|---|---|
| T9（5 条契约措辞/基线附录，doc-only） | **立** | **T8 注入前**（不等合并，可立即） | 批次 2 开工前固化契约口径（卡 1/2/3 措辞 + 数据源决策附录），防 T8 在漂移口径上实现；doc-only 零生产风险 |
| T11（3 项 test-only 补强） | **立** | **O-2 合并收口后、T8 注入前** | T8 将改 work_next 路径，先锁定批次 1 的 [] 语义/多依赖/无 binding 行为测试，降低批次 2 回归风险；落点为 preq 线的 `next_action_test.rs`（本地 master 无此文件，须基于合并线） |
| T10（3 项 tester 独立复跑） | **立** | **与 T8 并行或收尾** | 非阻塞证据等级提升，不占 T8 关键路径；证据闭环宜早，人力允许可与 T8 同时启动 |

**建议顺序**：T6 关单 → [O-2 合并收口 ∥ T9] → T11 ∥ T8 → T10（并行/收尾）。

### 3. O-2 合并策略（只读事实 + 选项，不裁定、不执行）

**事实（本轮只读核验）**：
- 两线共同基 `21a8e10`；本地 master `62cff9b`（45 文件改动：daemon-resource-governance 设计文档 + F2/F3/F6/F7 整改，与 preq 线**文件级零交集**）；origin/master `40a21d4`（T1+T2）。
- preq 交付分支**全部基于 40a21d4**：T3=`68e57a4`（impl/t3-status-projection）、T4=`193b928`（impl/t4-claim）、T5=`e7164da`（impl/t5-capability）、T6 集成线=`521514b`（run/preq-t6-integration，**已 merge 含 T3/T4/T5 全部**）、T7 文档=`bcf4cfa`（preq/t7-doc-placement，**尚未并入 T6 集成线**）。
- 冲突面：master 独有 vs T6 集成线 = **空**；master 独有 vs T7 文档分支 = **空** → **合并预期零冲突**。
- 硬约束：master 主工作树有未提交改动（fs_handlers.rs +953/−76、build_journal.rs、.trae_*，用户要求原样保留）→ **合并必须在独立 worktree 做**，禁止在主 worktree checkout/merge/stash。

**选项**：
- **A（推荐）**：独立 worktree 中把 `preq/t7-doc-placement` 并入 `run/preq-t6-integration` 得完整 preq 线（T1–T7），再将完整线 merge 进本地 master。优点：保留双方历史、零冲突预期、符合本地 master 作集成分支惯例（有 `50a778e merge origin/master：分叉收口` 先例）；缺点：master 历史多一个 merge 节点。
- **B**：master 独有提交并入 preq 线后推 origin/master。缺点：把本地设计文档/整改提交带向远端，方向与「master 为集成分支」惯例相反。
- **C**：新建集成分支双向合并、验证后各自收口。最稳但多一步。

本评估只报事实与选项，**不执行合并、不指定最终策略**，待主理人裁定。

### 4. 其余既定事项

- 主工作树未提交改动继续原样保留；O-2 合并操作避开主 worktree。
- 完整 preq 线合并收口前，T8/T11 不具备正确基线；T9（doc-only）不受此限。
