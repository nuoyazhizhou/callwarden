# T7 独立评审报告 — v2 amendment 追加 prerequisite gate 设计章节

- **Role**: reviewer（柯明察，独立评审官）
- **Task**: T-1795663574847-eaea3d24（T7 文档单，REQ-PREQ-06 should）
- **独立性**: `independence=daemon_registered_new_session`
  - daemon 可达。经 `agent.register` 注册全新身份：`agent_id=reviewer-t1-t7` / `agent_instance_id=reviewer-t1-t7-inst`（非空）/ `session_id=sess-reviewer-t1-t7-t7-20261128` / `model_id=workbuddy-agent` / `role=reviewer`——与 T1 评审（sess-reviewer-t1-t7-t1-20261128）不同 session，与 executor（`sess-executor-alm-46762b-regprereq`）完全不同。未复用任何身份。
  - `lease.acquire`（reviewer）→ verdict blind_first_pass → `lease.release`。
- **评审对象**: commit `147742e`「docs(preq): v2 amendment 追加 prerequisite gate 设计章节」
  - 改动面：`docs/design/cw-role-handoff-task-loop-v2-amendment.md` +84 行（§8 全章，文件 175→259 行）。`git show 147742e --stat` 确认**仅此一文件**，无越界改动；`.design/` 零触碰。
  - executor 有 handoff 与 evidence manifest（06a4d5b），但本评审**未采信其结论**，全部结论基于自己对 diff 与设计基线的独立核对；仅将 manifest 的文件 SHA-256 声明作为可复现事实做了复算（b5ca087e…44329，与 `git show 147742e:<file> | sha256sum` 一致）。
- **只读声明**: 本次未改源码/文档/证据/任务状态，未建整改单，未 apply/close/supersede。本报告为评审产出文档。

---

## 结论：PASS

无 🔴 blocker。§8 章节四要素（三层结构/判定规则/capability 门禁/错误码）齐备，逐条与 .design/preq/ 冻结基线（8b1a1e3：contracts.md、ac-map.md、requirements-brief.md、task-map.md）及 T1 已实现代码（e133f34）一致；AC-12 判据满足。1 条 🟡（孤儿提交，归位由主理人处理，本报告记录在案）与 2 条 💭，均不阻断。

---

## 逐条核验（文档内容 vs 冻结基线 vs 实现）

| 文档位置 | 基线出处 | 核验 |
|---|---|---|
| §8.1 三层结构表（投影 prerequisites / 领取层 claim gate / 派工层 work_next 批次 2） | contracts.md 卡 3/卡 5/卡 6；task-map.md 批次 1（7 单）/批次 2（deferred） | ✅ 三层载体、行为、批次三列与 task-map T1-T7/T8 结构一致 |
| §8.1 claim gate 插入点「step 6 binding 校验之后、step 7 INSERT 之前」 | 卡 5（claim.rs:169-186） | ✅ 逐字一致 |
| §8.1 错误消息「直接指路 provider task_id」 | 卡 5 出参契约 | ✅ |
| §8.2 requires_existing ∈ {applied, closed}；provider 行缺失/空 target_task_id 未满足 | 卡 1 + prerequisites.rs:36/185-202/294-308 | ✅ 与实现逐字一致（fail-closed 方向） |
| §8.2 requires_artifact：lifecycle 完成 且 freshness='fresh'（按 workspace+provider+artifact_ref 取 produced_at 最新一行，对齐 get_artifact_freshness） | 卡 1 + db_task_dependencies.py:179-212 | ✅ 与 Python 侧查询语义逐字对齐（WHERE 三键 + ORDER BY produced_at DESC LIMIT 1） |
| §8.2 requires_interface：selection 行存在性（表无状态列） | 卡 1 Assumption A2 + schema.py:1492-1503 | ✅ 实测该表确无状态列，文档如实记录 |
| §8.2 provides_interface 跳过；未知 dep_type → unknown_dep_type fail-closed | 卡 1 + prerequisites.rs:116/118-124 | ✅ |
| §8.2 task 不存在或无声明 → 空结果 all_satisfied=true | 卡 1 入参契约 | ✅ |
| §8.3 capability `prerequisite_gate_v1` / `prerequisite_capability_v1` / 三键 `{name, schema_version, enabled}` / `capability_enabled()=true` | 卡 2 + prerequisites.rs:31-81 | ✅（契约「四键」系其内联笔误，文档取三键与实现一致）；「走 §3.1 daemon 公共 capability 通道」引用有效（§3.1 即 capability 通道定义章） |
| §8.3 `E_PREREQUISITE_NOT_MET`（Deterministic、dry_run 无副作用、外层事务回滚）+ message 样例 | 卡 5 错误变体表 + 出参契约 | ✅ message 样例与卡 5 逐字一致 |
| §8.3 enabled=false → 跳过 gate 与存量一致 | 卡 5 异常路径（AC-10） | ✅ |
| §8.4 prerequisites 五字段 shape（provider_task_id/lifecycle_status/dep_type/unmet_reason/freshness_status；空为 []；freshness_status 仅 requires_artifact 携带） | 卡 1 struct + AC-04/AC-05 | ✅ JSON 示例五字段齐全 |
| §8.4 依赖不可评估 ≠ 依赖未满足（prerequisites 置 []，不新增阻塞语义） | 卡 3 异常路径 | ✅ |
| §8.5 批次 1 = 7 单；批次 2 deferred；放行判据「全部 7 单到 review + 全程无 E_* 门禁错误 + ≥1 单完成 apply/close 全链路」 | task-map.md §批次/§放行判据 | ✅ 放行判据与 task-map 逐字一致 |
| §8.6 六场景（DB 失败向上抛/无声明空结果/reverted→未满足/环依赖不特殊处理/claim 并发 savepoint 复查/capacity 未启用跳过） | requirements-brief §4.3 异常矩阵 + 卡 1/卡 5 | ✅ 语义全部对齐（编号重排见 💭-1） |
| 来源行：父任务 T-1795661715269-f48、基线冻结 8b1a1e3 | task-map.md 头部 + brief 头部 | ✅ 两个标识符均核实 |
| 相对链接 `../../.design/preq/requirements-brief.md` | — | ✅ 目标文件存在于仓库（docs/design/ 上两级 = 仓库根） |

**AC-12 判据**：三层语义（§8.1）✅、判定规则（§8.2）✅、capability 门禁（§8.3）✅、错误码（§8.3）✅ —— 四要素齐备，文档 diff + 链接可达均满足。

**与实现的一致性**：文档章节与 T1 交付 prerequisites.rs（e133f34）逐项对齐（capability 常量、判定规则、五字段 shape、错误码），未发现文档承诺了实现没有的行为、或实现有而文档遗漏的关键语义。

---

## 覆盖矩阵

| 改动点（§8 各节） | 消费方 | 覆盖 | 缺口 |
|---|---|---|---|
| §8.1 三层结构 | 批次 1 各单（T2-T5）+ 批次 2（T8） | 与 task-map 批次结构逐项对照 | work_next 层为 deferred，无实现可对照（设计声明与卡 6 一致即可） |
| §8.2 判定规则 | T1 prerequisites.rs（e133f34） | 五类 dep_type 逐条对照源码 | — |
| §8.3 capability + 错误码 | T1 声明 + T4 claim.rs + T5 dispatch | 常量/错误码/message 样例对照（T4/T5 为交叉引用，其单详评） | — |
| §8.4 五字段 shape | T2/T3 投影（未评，交叉引用卡 3/卡 4） | shape 与卡 1 struct 一致 | 投影侧字段存在性由 T2/T3 单评验证 |
| §8.5 批次与放行判据 | task-map.md | 逐字一致 | — |
| §8.6 异常要点 | brief §4.3 | 六场景语义一致 | 编号未沿用 brief 矩阵原号（💭-1） |

---

## Findings

- 🟡 **[仓库治理-记录项]** commit `147742e` 为**孤儿提交**（`git branch --contains 147742e` 无输出，不在任何分支）；其证据链 `b463184 → 147742e（文档）→ c5c459e（ledger 簿记）→ 06a4d5b（evidence manifest）` 整段悬空。内容本身已核验通过（本报告全部结论基于该提交树内内容）。**处置**：归位（cherry-pick/merge 到分支）由主理人处理，本评审不移动任何提交；此 finding 仅留痕，提醒归位前 `docs/evidence/T-1795663574847-eaea3d24-prereq-gate-design.md` 在任何分支上均不可达（checkout 147742e 或归位后才可见）。

- 💭 **[nit]** §8.6 异常要点表对 requirements-brief §4.3 八场景矩阵做了压缩重排（文档 #1=DB 失败对应 brief #5；文档 #3=reverted 对应 brief #7）。语义无冲突，但跨文档引用场景编号时可能错位；建议后续引用 brief 矩阵时带「brief §4.3 #N」原文编号。可在下次文档维护时顺手加注，不阻断。

- 💭 **[nit]** §8.2 使用 `is_hard=0` 词汇描述信息性依赖，而实际声明表列为 `is_informational`（is_informational=1 ⟺ is_hard=0）。该词汇沿自冻结契约卡 1 原文（warnings 定义即用 is_hard=0），且文档头部已声明数据源含 task_dependencies，语义无歧义；记录供批次 2 文档统一词汇。

---

## 未覆盖路径

1. **T2/T3/T4/T5 交付内容**：文档对投影/门禁/接线的描述与 T1 实现逐项核对通过，但 T2（next_action 投影）、T3（status 投影）、T4（claim 门禁）、T5（dispatch 接线）的实现是否兑现文档承诺，属各单评审范围，本单未验证。
2. **批次 2（work_next 派工过滤）**：文档仅作设计声明（deferred），无实现可对照，AC-13 不在本批验收。
3. **孤儿提交归位后的分支可见性**：归位动作本身及其对 ledger 链的影响由主理人处理，本评审未验证归位结果。
4. **MCP/CLI 渲染**：文档章节在 MCP 资源/CLI 帮助中的呈现未验证（非本单交付物）。

---

## Handoff

`PASS` → 交 adjudicator。🟡-1（孤儿提交）由主理人归位处理；两条 💭 不阻断 T7 关单。
