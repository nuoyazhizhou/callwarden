# 四角色入口重构（v4/Planner v1）审计报告

> 对象：`e70b0b7`（文档/模板/归档）+ `1f704ca`（ledger）。只出修改意见，未改动任何文件。
>
> **复审更新（2026-08-28 11:18，针对 `7c4edda` + `c2e2e0c`）：** 首轮全部 4 项 P1 已确认修复——
> ①§7"命令与已知坑"完整下沉 VCS 台账/instance-id/lease 生命周期/reviewer lease+fencing；②17 值
> `workflow_status` 枚举唯一化到 role-protocol §2，AGENTS/SKILL 改引用；③顶部"实现状态声明"明确
> daemon 未实现能力；④设计文档已标注四角色修订与能力缺口。P2 中 Planner outcome、模板内联 Handoff、
> AGENTS 双 Handoff 块、校验器升级（含 forbidden-marker 检查）均已修复，校验器独立复跑通过。
> 额外修复一处潜伏矛盾：AGENTS 默认工作规则由"提交前刷新"改为"提交后刷新"，与 VCS 纪律对齐。
> **残留（均 P3 轻微）：** ①finding schema 仍内联在 Reviewer/Executor 模板（未引用 protocol §4，
> `introduced_by_change/call_chain` 未进模板字段列表）；②cw-task-loop SKILL.md 仍内联完整 Handoff
> 块且字段顺序与 protocol §5 略异（校验器不检查 SKILL.md，漂移无门禁）；③role-protocol §2 编辑
> 残留格式问题（workflow_status 一句失去 bullet 标签、lifecycle 丢了"可 reverted"）；④证据文件
> finding 字段措辞（owner/blocking）与 protocol §4（建议归属）轻微不一致。
> 结论：文档层回补合格，可进入 daemon 实现阶段；`build_full_graph` method_not_found 属已部署
> daemon 陈旧，需按 `refresh_shared_runtime.ps1` 约定另行重建部署解决。

## 一、结论先行

重构方向完全正确：Planner 独立、抽共享 `role-protocol.md`、`g0-experiment` 不合并、旧 v1/v2/v3 归档并留 supersede 映射、加 `validate_template_compliance.py`、无 SQL 绕过（`build_full_graph` 缺失如实记录）——这些都做对了。

但有 **4 个 P1 级修订问题**（会导致 Agent 误操作或再漂移）和 5 个 P2 质量项，详见下。

## 二、P1（应尽快修）

### P1-1 v4 模板"泛化"掉了 v3 积攒的硬失败模式，且未迁入共享引用
这是最实质的回归。v3 里用血泪教训换来的具体命令/错误码，在 v4 里被压缩成一句话后**消失**，且没有下沉到 `role-protocol.md` 或 skill：

| 被丢失的细节 | 出处(v3) | 后果 |
|---|---|---|
| VCS 台账纪律：`git add` 白名单、**严禁 `git add .`**、commit message 内嵌 task_id、`git rev-parse HEAD` 取 commit_id、追加 `cw_task_commit_ledger.json`、`cw refresh --all` 必须排在 commit 之后 | Executor v3 §2.5 | worktree-prune 损失隔离纪律被掏空，只剩一句"白名单 add、含 task_id 提交、追加 ledger" |
| Reviewer 领取必须带 `--agent-instance-id inst-reviewer-wb-186loop`（否则 `E_IDENTITY_INSTANCE_MISMATCH`）+ lease acquire→release 全生命周期 | Reviewer v3 | 复审领取会再撞 instance mismatch |
| Adjudicator **apply/close 必须持 reviewer lease（非 adjudicator lease）**、`--lease-token/--fencing-counter` 完整命令、用后 `lease release`（36 条残留事故） | Adjudicator v3 | 收尾会再撞 `E_LEASE_REQUIRED` |

**改法**：在 `role-protocol.md` 下新增一节"命令与坑（concrete commands & pitfalls）"，把这些具体命令/错误码迁入；四份模板只引用，不删除。

### P1-2 状态枚举三处漂移（正是讨论要消灭的"逐份漂移"）
`workflow_status` 权威枚举现在写了三份、三份不一致：

| 文档 | 值数量 | 缺什么 |
|---|---|---|
| `AGENTS.md` §workflow_status | 16 | （最全：含 reverted / governance_blocked / waiting_for_decision / waiting_for_input） |
| `role-protocol.md` §2 | 13 | 缺 `reverted`、`waiting_for_decision`、`waiting_for_input`（后两个只在正文提，未入列表） |
| `cw-task-loop/SKILL.md` 状态表 | 14 | 缺 `reverted`、`governance_blocked` |

**改法**：把权威枚举只保留在 `role-protocol.md`，AGENTS.md 与 SKILL.md 改为"完整状态见 role-protocol.md §2"，各自只保留与角色相关的解释行。

### P1-3 决策/等待状态"文档先行于 daemon"未标注
`decision_request`、`waiting_for_decision`、`waiting_for_input` 被 `role-protocol.md`、`AGENTS.md`、`cw-task-loop` 写成现行语义，但 `rust_ext/src` 里**零实现**（grep `decision_request / decision.respond / waiting_for_*` 无匹配）。Agent 读文档会去 `next-action` JSON 里找 `decision_request` 字段却找不到，误判为缺失。

**改法**：在 `role-protocol.md` 顶部加一行显式声明——"以下 `decision_request` / `waiting_for_*` 为规划中状态，daemon 尚未 emit；当前遇到相关场景按 `blocking_reasons` + 现有状态处理"。避免文档与实现脱节。

### P1-4 陈旧设计文档与必读清单矛盾
`docs/design/cw-role-handoff-task-loop.md` 第 27 行仍把 `planner` 列为 legacy runtime role，未更新为独立治理角色；但 `cw-task-loop/SKILL.md` 的 Required Reading 明确指向它（§3/§5）。上游设计文档与新角色模型自相矛盾。

**改法**：二选一——更新该设计文档的 Planner 定位，或从 SKILL.md 必读清单摘除/降级为"历史参考"。

## 三、P2（质量/一致性）

1. **Planner v1 复制粘贴 bug**：第 62 行"字段无法验证时输出 `planner_replan_required` 或 `executor_blocked_to_user`"——后者是 Executor 的 outcome，Planner 枚举里没有。应改为 Planner 专属阻断表达。
2. **模板仍内联 Handoff/identity 全块**，与讨论"模板不应再复制 Handoff 规则"目标相悖。四份模板 + SKILL.md + AGENTS.md(两处) + role-protocol.md 各有一份 Handoff 块。模板应只写"见 role-protocol.md §5"，保留 role 专属 outcome/next_role 差异即可。
3. **`validate_template_compliance.py` 太弱**：只是 substring 存在性检查，不校验 identity 5 字段完整、lease acquire→release 生命周期、状态枚举一致性、字段顺序，且**根本没校验 `role-protocol.md`**。建议升级为结构化断言并纳入 role-protocol。
4. **AGENTS.md 出现两份 Handoff 块**（"最小路由 envelope" 6 字段 + "完整 provenance 硬门禁" 12 字段）。语义合理但易混，建议合并并注明"最小路由 ⊂ 完整 provenance"。
5. **`adjacent_defect` 字段 schema 三处漂移**：讨论强调"影响文件/调用链/回归范围/**是否本次引入**/严重级别/建议修复动作"，最终 `role-protocol §4` 只写"根因/复现/影响/建议归属"，Reviewer v4 用 finding 的 scope/severity/acceptance。"是否本次引入"与"调用链/影响半径"被弱化，建议在 role-protocol 统一 finding schema 一次定义。

## 四、还有优化空间吗（前瞻）

1. **daemon 层是最大缺口**：Planner 真实注册、planning/replanning 流转、`decision_request` 持久化 + `decision.respond`、`fix_defect` 自动追加路由、`adjacent_defect → related_to` 关联整改任务——文档已就位，代码未实现。这是下一阶段真正工作量，也是 P0-L 教训的根治点（"四层闭合"里只完成了模板/skill/文档，daemon+CLI/MCP 未动）。
2. **校验器未接入门禁**：目前是手动脚本，建议挂 commit hook / CI，或由 `cw-task-loop` 流程强制。
3. **决策卡缺结构化 schema**：`decision_request_id / choices / free_text / resume_action` 尚未落库，`cw decision list/show/respond` CLI 未实现——这是讨论里"人机分离"的核心产物，目前只存在于 prose。

## 五、优先级建议

1. 先修 P1-1（下沉 v3 硬失败细节）——否则实战立即踩坑。
2. 再修 P1-2/P1-3（状态枚举收敛 + 文档/daemon 脱节标注）——防再漂移、防 Agent 误判。
3. P1-4 与 P2 随后批量修。
4. 下一阶段把重心放到 daemon：Planner 注册 + 状态机 + decision_request 落库。
