# Call Warden 四角色治理 vs WorkBuddy 专家/角色体系 —— 对比与改造思路

> 目的：把你项目里已落地的 Planner / Executor / Reviewer / Adjudicator 四角色治理（AGENTS.md + 三份 v3 启动模板），与 WorkBuddy 现有的「专家团 / 角色 Skill」做一次对照，给出哪些可借鉴、以及你的模板该怎么改。本文只给思路，不改动你的文件。

## 一、两边分别在做什么

**你的四角色（Call Warden A′ 治理）**——一套 **daemon 原生、机器强制（fail-closed）** 的治理状态机：
- 角色：Planner（拆分/复杂度预检）→ Executor（实施+证据）→ Reviewer（独立复审）→ Adjudicator（最终裁决+apply/close）。
- 强制件：5 字段 identity、lease+fencing、Role Contract、workspace authority binding、provenance handoff 信封、lifecycle_status / workflow_status 双投影。
- 反自审：三角色 session/instance 互异、不得兼任；Reviewer PASS ≠ applied；残留 active lease 会污染 next_action（36 条事故）。
- 当前痛点：v1→v2→v3 反复累加（身份字段、lease 全生命周期、VCS 台账），模板间职责重叠、存在漂移风险。

**WorkBuddy 的专家/角色体系**——一套 **persona 软约束（prompt/SKILL.md 驱动）**：
- 专家包（Agent 型 / Team 型）：如 `aicoding 架构专家团`（主理人 Lead + business/system/platform/security 架构师 + product-story-designer），用 **Phase + Gate（G0–G6）** 编排，成员不直接互通、全部经主理人中转；阶段产物经 AskUserQuestion 人工闸门放行。
- 角色 Skill：如 `vibe-coding-architect`（架构选型方法论：6 问澄清 → 2-3 方案对比表 → 10 段架构 Prompt → 小步实施）、`expert-manager`、`find-skills`、`agent-browser`。
- 特点：没有 daemon 级 lease/fencing/identity 强制层，也没有 append-only verdict ledger；人工闸门靠 AskUserQuestion；可视/交付靠 Artifacts 面板。

## 二、关键维度对照表

| 维度 | 你的四角色治理 | WorkBuddy 专家/Team |
|---|---|---|
|  Enforcement | daemon 协议，fail-

-closed、机器强制 | prompt/SKILL.md，模型遵循，软约束 |
| 角色划分 | 治理向（Planner/Executor/Reviewer/Adjudicator） | 专业域向（业务/系统/平台/安全架构师等） |
| 交接 | 结构化信封 + lease + identity，下游必须按 task_id 重查 | Artifacts + 对话 + AskUserQuestion |
| 状态机 | lifecycle_status + workflow_status 双投影，显式 | 无原生；依赖 IDE 任务管理/人工 |
| 人工闸门 | user 作为状态机里的一个 role（executor_blocked_to_user） | Ask 模式 / AskUserQuestion 显式 |
| 溯源 | append-only verdict 事件、evidence hash、commitid↔taskid 台账 | Artifacts 卡片 + 文件 |
| 独立性/分离 | 三角色 session/instance 互异，禁自审 | 依赖不同专家实例；不强制 |
| 打包/分发 | markdown 模板散落在仓库根 | 专家市场 / `.workbuddy/skills/` 规范 |

结论：你的「机器强制治理」是 WorkBuddy 专家体系**没有**的能力；但 WorkBuddy 的「Phase/Gate 编排、中间确认协议、模板合规校验、专家包打包」是你可以**直接借鉴**的工程化外壳。

## 三、哪些对你有参考价值（按价值排序）

1. **aicoding 架构专家团 的 Phase/Gate + 主理人中转模型（最高价值）**
   - 它的「多角色团队、成员不直接互通、经 Lead 中转」「未过 Gate 不得进入下一阶段」「阶段产物 AskUserQuestion 人工审核」与你「信封必须带 task_id、下游按 task_id 重查」「单任务纪律」高度同构——**Lead 就是你 daemon 的运行时拓扑**。
   - 可借鉴：把「某 port_type 的全部卡片达到 applied 才解锁后继 port_type」显式落成 **G 级阶段门**（类似 G3/G4/G5），而不只是 per-task 的 review→applied。

2. **intermediate_confirmation 协议（4 字段：论题/候选方案/阻塞范围/默认建议）**
   - 你的 `executor_blocked_to_user` / `adjudicator_returned` 现在只是自由文本 `reason`。建议改成该 4 字段结构化阻塞，便于机器解析、减少来回。

3. **模板合规校验（`bin/validate_template_compliance.py`）**
   - 你的四份模板 + AGENTS.md 已出现 v1/v2/v3 重复与字段漂移。做一个轻量结构校验（必含 identity 5 字段、handoff 信封字段、lease 生命周期步骤）即可在坍塌前抓住漂移。

4. **vibe-coding-architect 的 Planner 输出范式**
   - 把「6 问澄清 + 2-3 方案对比表 + 10 段架构 Prompt」作为 **Planner 交付物标准格式**，让 Planner→Executor 交接可被机器校验，而不是自由文本。

5. **expert-manager / 专家包打包**
   - 可把四角色治理封装成一个 WorkBuddy **Team 型专家包**（Lead=治理编排，成员=Planner/Executor/Reviewer/Adjudicator）用于跨项目复用；但务必保留 Rust daemon 的 lease/fencing/identity 作为唯一强制真相源，专家包只做「人设+流程」文档层。

## 四、你的模板改造思路（仅思路，待你确认再动）

- **A. 加一层阶段门（Phase Gate）**：在现有 per-task 状态机之上，引入 Epic 级 G 门——同一 port_type 全部 applied 才解锁后继；直接对标 aic,oding 团队的 G3/G4/G5。
- **B. 阻塞结构化**：把 `executor_blocked_to_user` / `adjudicator_returned` 的 `reason` 升级为 intermediate_confirmation 的 4 字段，减少语义漂移。
- **C. 模板结构校验**：写一个小校验脚本/引用，扫描四模板与 AGENTS.md，断言身份块、信封字段、lease acquire→release 步骤完整；对标 `validate_template_compliance.py`。
- **D. Planner 标准化**：采用 vibe-coding-architect 的「澄清→对比表→架构 Prompt」三段式作为 Planner 交付模板。
- **E. 做成专家包**：用 expert-manager 把四角色沉淀为 Team 型专家包复用，但 daemon 仍是强制后端。
- **F. 整理现有 skill**：你已有的 `callwarden-reviewer-loop`、`callwarden-mcp-card-migration` 与 Reviewer/Executor v3 模板 + AGENTS.md **明显重叠**（reviewer-loop 覆盖了 Reviewer v3 的 lease/contract/verdict 细节）。建议归并责任边界：模板讲「角色纪律」，skill 讲「直连 daemon 的命令/坑」，互不抄写。

## 五、风险提示与下一步

- 你的「机器强制」是护城河，任何向 WorkBuddy 专家体系的迁移都**不能**用 persona 软约束替代 lease/fencing/identity——只能当作上层编排与文档壳。
- 模板当前 v1/v2/v3 混杂，若不组织，后续极易产生冲突版本；建议先做 C（结构校验）再动 A/B。
- 下一步若你愿意，我可以：① 把四角色打包成 Team 型专家包（保留 daemon 强制）；② 写 C 的模板校验脚本；③ 按 B/E 重写阻塞信封与 Planner 交付格式。需要我动手时请切到 Craft 模式。

---

### 备注（Agent 反射）
本轮发现你现有 skill 明显混乱：`callwarden-reviewer-loop` 与 Reviewer v3 模板、`callwarden-mcp-card-migration` 与 Executor v3 模板职责重叠且内容交叉。按规则**提醒你**：这些 skill 应当组织，避免重复与漂移。若你希望我合并/梳理，请明确说一声（我不会擅自删改）。
