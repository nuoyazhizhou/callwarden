# requirements-cw 技能设计文档（v2.5）

> 状态：v2.4 实现已完成；**v2.5（2026-11-22）= 实现后的 ALM 缝合修订**（门禁抽到 `skills/_shared/`、审查侧接入 REQ 追溯、新增 `skills/README.md` 地图，§13.6）。
> 修订依据：v1 评审（🔴×2/🟡×4/💭×4，§13.1）+ 7 套专家实现参照 + ALM 视角复审（🔴 B1-B4 / 🟡 M1-M5 /
> 💭×5，§13.2）+ v2.1 复审（🔴 C1 / 🟡 C2-C4 / 💭 C5-C8，§13.3）+ v2.2 复审（🟡 D1 / 💭 D2-D4，§13.4）
> + 政策拍板与 daemon 实测（§13.5）+ ALM 覆盖度审计后的缝合（§13.6）。四条复审主线：**双向追溯**、
> **任务完成 ≠ 需求满足**、**交付节奏与证据链一致**、**证据可持久 = 验收可审计**。
> 关联：code-review-cw（下游审查，共用 `_shared/cw-gates.md`）、cw-planner-architect（design-only 规划协议）、
> cw-executor-senior-engineer（执行）、cw-task-loop（状态推进）。

## 1. 定位

**需求 intake + 生命周期门禁层**：把模糊需求（"我要做个 X"）逐步细化成可执行、可审查、可追溯的函数级设计，经强制评审门禁注入 callwarden 任务体系，再交给 executor 开发、code-review-cw 审查收口。

**追溯主线（ALM 底线）**：全程双向追溯——`REQ（需求）→ 卡片（设计）→ task_id（任务）→ AC（验收）→ 证据`。需求变更可反查影响面，任务可回溯需求来源（§5 R0/R3/R4、§3.4）；证据链末端按 §3.5 落盘并提交，**不留工作树脏文件**——证据不可持久 = 验收不可审计。

与 code-review-cw 合起来覆盖完整生命周期：

```
模糊需求 → requirements-cw（本技能）→ callwarden 任务 → cw-executor 开发
        → code-review-cw 审查 → feature 级验收 → 需求关闭/收口
```

**触发场景**：新功能/新模块的模糊需求、需求拆解、方案设计；**不触发**：已有代码的审查（那是 code-review-cw）。触发 description 必须带此负向边界（宿主选技能靠 description，见 §11）。

**两条链（条件分支，见 §3）**：存量仓功能迭代走精简链；新项目（从 0 到 1）走完整链（含技术栈选型门禁与测试门禁）。**feature 级验收两条链都有，深度不同（§3.4）**。

## 2. 评审门禁卡（Gate Card）

**每个评审门禁（需求评审 / 选型评审 / 设计评审）强制输出四件套，缺一不过门**。目的：制度化"找优点缺点 + 多轮交互"，从机制上防 happy path，不靠 prompt 自觉。

### 2.1 四件套

| # | 件 | 要求 | 来源参照 |
|---|---|---|---|
| 1 | **方案对比表** | ≥2 候选 × 优缺点 × 推荐项；大型功能可选并行多视角产出后合并 | feature-dev Phase 4、fullstack-architects Phase 1 |
| 2 | **被否决项及理由** | 明确写出"为什么不选它"；不写 = 门禁不通过 | fullstack-architects 输出规范（铁律） |
| 3 | **异常场景矩阵** | 8 类逐一过：空值 / 无权限 / 重复请求 / 冲突 / 依赖失败 / 重试 / 取消恢复 / 数据边界；每类标"不适用"也算过 | prd-acceptance-spec 异常/边界矩阵 |
| 4 | **未决项清单** | confirmed / assumption / open 三态分离；assumption 注明责任人与验证方式；不得把推测写成已批准事实；需求条目带稳定 REQ 编号（§5 R0） | prd-acceptance-spec 事实台账 |

### 2.2 多轮交互协议（防无限拉扯）

1. **复述确认**：门禁产出前先向用户复述一遍理解（"我理解你要做的是……"）。
2. **挑战 → 修订 → 复评**：用户对四件套任一项挑战后修订复评。
3. **每门禁 ≤2 轮挑战封顶**；超轮次分歧记入未决项（open），由用户裁决后继续，不得悬置阻塞。

### 2.3 反 happy-path 双防线

- **第一道（门禁层）**：异常场景矩阵强制（§2.1 第 3 件）。
- **第二道（卡片层）**：contract 卡片"异常路径"字段强制"每个失败点有去向"；"测试要点"必须含 ≥1 个失败分支用例；GWT 验收的 Then 允许是错误形态（§7）。

### 2.4 hotfix 豁免（#1 拍板）

**豁免条件（三条同时满足，才可跳过四件套第 1、2 件）**：**单文件改动** + **无新增符号** + **该文件已有测试覆盖**。

豁免仍须：① 保留异常矩阵与未决项清单；② 在 requirements-brief.md 写明豁免理由与**三条判据的逐条核对结果**（不是"我觉得小"）；③ 任一条件不满足即回落完整四件套。

理由：单文件 ≠ 低风险——改一行权限判断也是单文件，所以豁免锚在"无新增符号 + 有测试兜底"，而不是锚在文件数。

## 3. 生命周期条件分支

### 3.1 触发判定（开工判定一次，写进 requirements-brief.md）

| 判定 | 完整链（新项目） | 精简链（存量仓迭代） |
|---|---|---|
| 仓库是否存在 | 否 / 全新仓 | 是 |
| 技术栈是否已定 | 否 | 是（沿用） |
| 判定冲突时 | 用户裁决，记 confirmed | — |

### 3.2 链路

```
完整链（新项目）：
R0 澄清 → G-Tech 选型门禁 → R1 可视化 → R2 符号锚定 → R3 卡片+设计门禁+基线冻结
→ R4 注入 → R5 开发 → code-review-cw → G-Test 测试门禁 → feature 验收关闭 → 收口

精简链（存量仓）：
R0 澄清 → R1 → R2 → R3（卡片+设计门禁+基线冻结）→ R4 → R5
→ code-review-cw → FA 轻量 feature 验收 → 需求关闭/收口
```

**回环（C4）**：R2 锚定结果与 R1 图存在实质冲突（核心链路大面积 reuse 命中 / 锚定降级导致关键判定缺失）→ 局部返工回 R1（改图并重过 R1 确认）或回 R0（补问），返工原因与证据记录，之后才进 R3。

### 3.3 G-Tech 选型门禁（仅完整链）

- **输入**：R0 简报 + 团队已有栈约束 / 运维能力 / 许可边界（R0 必问项，见 §5 R0）。
- **产出**：`tech-choice.md`——门禁卡四件套（候选栈 ≥2、被否决栈及理由、失败/运维场景矩阵、未决项）。
- **参照**：fullstack-architects system-architect 的选型对比 + "至少一个被否决方案"铁律。

### 3.4 G-Test 测试门禁（完整链）/ FA 轻量 feature 验收（精简链）

**映射工件（C2）**：REQ→AC 映射统一由 **`ac-map.md`** 承载（列：REQ → AC id → 验收方式 → 证据位置），**两条链都产**。R0 出初版（每条 REQ ≥1 个 AC，AC-01… 编号唯一无悬空），R3 冻结时定稿并纳入基线。卡片的"验收映射"字段只引用 AC id。"证据位置"列指向 `.design/<feature>/evidence/<AC-id>.md`（§3.5）——**该列为空或指向不存在文件 = 该 AC 未通过**。

**两条链的共同要求（B2：feature 级验收不可缺位）**：

- **全部任务闭环后**按 ac-map.md 执行一遍，证据落盘 `evidence/`（命令输出/可观察行为记录，§3.5），**不留在对话里**；
- **需求关闭判据（C3）**：本次迭代 **scope 快照内**的 AC 全绿才算关闭——快照在 R3 冻结时定义（must + 指定 should）；快照外条目显式标 **deferred** 转入下批，不阻塞关闭、也不假装已完成；
- 验收发现走"建单 → 修复"闭环，不口头放过。

**完整链（G-Test）额外产出**：`test-plan.md`——测试金字塔分层用例清单（单测/集成/E2E，覆盖模块间集成缝隙，逐条引用 AC id）+ 缺陷分级标准。**范围（#2 拍板）：只产清单 + 映射，不生成测试代码、不执行测试**；执行由 R5 / CI 承担，执行结果回填 `evidence/`（§3.5）。

**边界声明**：结构通过 ≠ 业务批准 ≠ 测试实际通过；三句话显式声明，不得伪装。

### 3.5 工件与证据目录（D1）

运行时工件统一落盘 `<repo>/.design/<feature>/`，**全部保留 git 跟踪**（C1），与源码提交分离——不允许以工作树脏文件形式漂着，也不允许只存在于对话里。

| 工件 | 路径 | 产生时点 | 提交时点 |
|---|---|---|---|
| 简报 / 锚定表 / 卡片 / ac-map / task-map / 图 | `<feature>/*.md` | R0-R4 | **R3 基线提交**，此后变更走追加提交 |
| 验收证据 | `<feature>/evidence/<AC-id>[-r<n>].md` | R5 验收 | **R5 收口提交** |
| 扫描结果 | `<feature>/evidence/scan-<stage>.md`（注释 / orphan / coverage / guardrail 各一份） | R5 收口 | 同上 |
| 变更记录 | `<feature>/change-log.md`（**只追加**） | 每次变更单 | **每次变更单一次提交** |
| 停车场汇总 | `.design/parking-lot.md`（跨 feature，**只追加**） | R0 | 随所在 feature 的基线/收口提交 |

**提交纪律（三条，防与 hunk 级源码提交冲突）**：

1. 设计/证据提交是**独立提交**，不与源码任务提交混合——与 C1 基线提交同一原则，hunk 级纪律只约束源码。
2. 提交时点只有三个：**R3 基线 / 每次变更单 / R5 收口**；不逐步零散提交，避免提交噪音。
3. 基线提交范围限定 `.design/<feature>/`；`.design/parking-lot.md` 可随同一次提交（仍属设计提交）。

**证据内容规范**：

- 写**结论 + 关键输出**（执行命令、断言、可观察行为），不贴全量日志；
- **不得含密钥 / token / 个人数据**，输出须脱敏；
- 二进制证据（截图 / 日志包）允许，但须在同名 `.md` 中写明来源、取得方式与判读结论——
  否则后人无法判断这张图证明了什么；
- **缺失即失败**：ac-map"证据位置"为空或指向不存在文件 = 该 AC 未通过，不得口头声明通过
  （fail-closed，与"能在工具层验证的不靠 prompt 自觉"同向）。

## 4. 与现有能力的边界

| 现有件 | 状态 | 本技能的关系 |
|---|---|---|
| `cw-planner-architect` | design-only | 本技能填补现行空白：R4 直接用 `task_create` 注入任务。**切换开关 = 检测 daemon capability `planner_governance_v1`**（非人工判断版本）：声明后 R4 演进为向 planner 交接，本技能保留 R0-R3 |
| `cw-executor-senior-engineer` | 可用 | R5 的执行者；本技能产出的 steps 必须满足其 `allowed_edit_scope` 单文件白名单 |
| `cw-task-loop` | 可用 | 推进/查询任务状态走它，本技能不重造 |
| code-review-cw | 可用 | R5 收口审查复用；共享 preflight 门禁（§10） |
| callwarden MCP 工具 | 可用 | 符号读取、规范检查都通过它（§9），这是"可验证"而非"靠自觉"的关键 |

## 5. 六阶段流水线（修订版）

### R0 澄清（多轮询问，≤3 轮封顶）

- **前置（v2 新增）**：轻量上下文获取——`get_stats` + 相关模块 `semantic_search`，让第一轮问题带上代码库上下文（不问现有代码已回答的问题）。不做全仓扫描。
- **输入卫语句（v2 新增）**：需求 >500 字先摘要请用户确认；太短/太模糊先要细节再进提问轮。
- **动作**：每轮 ≤4 个高价值问题（带候选选项或示例），按 范围 → 数据模型 → 异常与边界 → 验收 顺序补齐；完整链追加必问项：技术栈约束 / 运维能力 / 许可与预算边界。
- **需求级优先级（M4）**：每条需求标 **MoSCoW**（must/should/could）——模糊需求场景下防"全都要"；卡片与任务继承需求优先级，R4 首批只建 scope 快照内 must 链路。
- **NFR 显式位（v2.1）**：性能 / 安全 / 兼容性必问一格；**有来源才填数值，无来源记 open，不发明毫秒/SLA/并发量**（prd-acceptance-spec 原则）。
- **范围管理（v2 + D3 出口）**：in/out 边界 + **停车场**（好想法但当前范围外，记 parking-lot 类型 assumption，带推翻条件）；scope 新增必须伴随 scope 移除或里程碑调整。
  **停车场有进有出（D3）**：R0 开工第一步回看 `.design/parking-lot.md`（跨 feature 汇总索引，§3.5）——仍有效的条目纳入本轮 MoSCoW 评估，已失效/已被别处实现的标 closed；R0 收口时把本轮新增 parking-lot 条目**追加**到该文件（只追加）。一行成本，避免只进不出、无限堆积。
- **退出准则（F1 修正）**：端到端数据流可画 + 验收方式可执行 + **复述确认通过**。（"每个核心函数卡片填满"移至 R3——函数清单在 R2 锚定后才存在，R0 不可能达成。）
- **产出**：`requirements-brief.md`（每条需求带 **REQ 编号 REQ-01…、MoSCoW、NFR 项**；含需求评审门禁卡四件套 + assumptions + 停车场）+ **`ac-map.md` 初版**（§3.4，C2）。
- **硬规则**：澄清不了的项目写 assumptions，随任务落库（R4 带进 description）；不无限追问、不假设后不声明。
- **探路分支 R0-X（v2.1 转正）**：3 轮后仍模糊 → **不产 contract 卡片**，改为建 spike 任务单（时间盒，产出可执行结论），spike 结论回填 R0 后重走流程。**时间盒为 advisory（C6）**：cw 无任务超时机制，靠人工盯，不构成自动终止。

### R1 可视化（Mermaid 工件）

- **动作**：`mindmap.md`（领域概念）+ `flowchart.md`（端到端数据流/状态机，**含异常分支**）；跨模块交互多时加 `sequence.md`。
- **产出**：落盘 `<repo>/.design/<feature>/`（纯 Markdown 内嵌 Mermaid，三家宿主通用），**保留 git 跟踪**（C1）。
- **工件提交时机（C1 定稿）**：不 ignore、不入 hunk 级源码提交；由 **R3 冻结时作为独立"设计基线提交"** 一次性入库（见 §5 R3）——git 历史即基线证据，证据链不断。install.ps1 不再写入 .gitignore。
- **退出准则**：用户确认图与理解一致（一次确认，不逐图打断）。

### R2 符号锚定（读取符号都通过 cw）

- **动作**：对设计中每个"疑似要新写"的函数/模块先查现有符号：`semantic_search` / `get_file_symbols` / `get_symbol` / `get_code_metrics_summary`。
- **判定**：reuse（不建开发任务，**建接线任务**，§8）/ extend / new，记录 `qualified_name` 证据。
- **v2 新增（F2）**：`symbol-anchor.md` 的 new/extend 清单兼作 **R5 收口扫描基线**。
- **退出准则（B4 修正）**：**锚定表完成** = 每个设计元素有 reuse/extend/new 判定 + cw 证据。不含卡片要求（"卡片填满"属 R3——消除循环依赖）。
- **graph 不可用兜底（v2.1）**：未建图 / stale 且无法刷新 → 退化为 Grep + 源码阅读定位，**显式声明"锚定降级"**，不得假装已锚定。
- **回环（C4 新增）**：锚定结果与 R1 图实质冲突（核心链路大面积 reuse 命中 / 降级致关键判定缺失）→ 局部返工回 R1（改图重确认）或回 R0（补问），返工原因与证据写入 symbol-anchor.md，完成后才进 R3。

### R3 contract 卡片设计与设计评审门禁 + 基线冻结

- **动作**：为每个 new/extend 函数填卡片（模板 §7，**带 REQ 字段**），**逐卡自审**后进入**设计评审门禁卡**（四件套；方案对比 = 设计路线 ≥2 选 1，被否决路线写明理由）。
- **卡片审查 checklist**（吸收官方 requirements-generate 的 MUST/MUST NOT）：
  - 每项直接映射代码动作；精确文件路径 / 函数签名 / 表结构；禁模糊描述；KISS/YAGNI，不过度设计
  - 入参：类型 + 约束 + **校验归属**（调用方/被调方，不重复不漏）
  - 出参：成功形态 + 错误枚举（每个变体 ↔ 失败点）
  - 异常路径：每个失败点有明确去向；**禁止**裸 unwrap / 静默吞异常 / 只写快乐路径
  - 副作用：IO/状态/锁明确列出；命名与注释：doc comment 写 what + why + 错误语义
  - 测试性：边界值、异常分支可测，**必须含 ≥1 失败分支用例**
- **退出准则（承接 F1）**：全部卡片填满 + 门禁四件套齐 + **ac-map.md 定稿** + 用户**一次性确认**（附确认摘要：卡片数 / 风险 top3 / assumptions 数 / 快照范围，不甩整份文档）。
- **基线冻结（B3/C3/C1 合并）**：确认后
  1. git commit `.design/<feature>/` 全部工件（独立设计基线提交；`.design/parking-lot.md` 可随同，范围见 §3.5）；
  2. **记录 HEAD commit 为设计基线**（写入 brief）；
  3. **定义本次迭代 scope 快照**（must + 指定 should；其余 deferred，§3.4）。
  此后改动一律走变更单，不走静默漂移。
- **变更单程序（B3 + C5 合并）**：
  - **卡片变更**：revision+1 + 关联任务留言 + 重跑该卡 check_items；已建任务未闭环 → 留言 + 重跑；已闭环 → 按 REQ 新建整改单。
  - **REQ 变更（C5）**：同样走变更单——REQ-xx revision+1，**须用户确认（业务变更）**；沿 task-map 反查受影响 AC / 卡片 / 任务，同步修订 ac-map。
  - **升级条件**：受影响卡 >3 张（可调）→ 升级完整重规划（交 planner / 记 capability gap）。
  - 变更记录**只追加不改写历史**，落盘 `.design/<feature>/change-log.md`（每条：类型／对象／修订号／原因／影响面／批准人）；**每次变更单做一次追加提交**（独立提交，§3.5）。

### R4 注入 callwarden（复用四门禁 preflight）

- **前置**：完整复用共享门禁（§10）。
- **title 格式（v2）**：`<相对路径>:<符号> <动作> <一句话>`（沿用 code-review-cw 约定）；**description 首行带 REQ 编号**（B1）。
- **task-map.md 三列双向追溯（B1）**：`REQ ↔ 卡片 ↔ task_id`；AC 映射由 **`ac-map.md`** 承载（两条链同一工件，C2）。
- **依赖关系（F4 实测结论，已关闭）**：`task_create` / `task_create_subtask` **无结构化依赖字段**（参数仅 title/description/steps/creator/workspace_*；子任务树只表达层级，不是 blocking）。→ 定稿走三件套：**优先级排序**（must 链路先）+ description 写 `blocked-by: <task_id>` + R5 按序认领；跨文件 helper 依赖同理。
- **steps 字段实测结论（M5 关闭）**：`action` 是**非空字符串、无枚举校验**（`task_collab_contract.rs` 仅校验非空，落库原样）；惯例值 `implement`（开发）/ `test`（测试）/ `audit`（审查）。**不要手工写 `fix_defect`**——那是 daemon 生成整改步骤的保留值，有专门判定逻辑（`task_collab_lease.rs`）。`check_items` 是**字符串**（传数组会被 `step_field` 序列化成 JSON 文本落库，读回是转义串）→ 模板用换行分隔字符串。**`steps` 必须非空**，否则建出无步骤任务，executor 卡在 `task_next_step`。
- **reuse 接线任务（M1）**：粒度表见 §8；R4 退出准则 = 所有卡片有 task_id **且所有 reuse 条目有接线任务或显式豁免记录**（该需求内无对接面时豁免须写明理由）。
- **交接包（M2）**：R4 产出物含移交清单 = `task-map.md` + `symbol-anchor.md` + `contracts.md`（含基线 HEAD）+ `requirements-brief.md`（REQ/assumptions）+ `ac-map.md` + 当前 `snapshot_id`。**移交身份约定：executor 以自身注册身份（agent.register 五字段）领取，agent_session_id 用 executor 自己的 session——身份不跨会话借用**（E_IDENTITY_SESSION_MISMATCH 为实测结论）；交接包路径写进首个认领任务的 description。
- **分批（v2 修订 + C3/C7 + D2）**：**首批 ≤8 单，只建 scope 快照内 must 链路**。快照内**指定 should** 不标 deferred（它是本次关闭条件的一部分），排入后续批次；**只有快照外**的 should/could 标 deferred 转下批（不阻塞本 feature 关闭）。
  **放行判据（C7 可操作化 + D4 职责边界）**：首批全部任务到达 review 状态、全程无 `E_*` 门禁错误（E_IDENTITY_* / E_CONTRACT_* / E_TASK_REPORT_SNAPSHOT_REQUIRED / E_CHANGE_PATH_NOT_ALLOWED）、且至少 1 单完成 apply/close 全链路 → 放行后续批次；未满足则先修门禁问题，不扩批。**本技能只读判定**（`task_status` / `task_governance_projection`），apply/close 由 executor/reviewer 按其角色协议执行（cw-task-loop），本技能不自行 apply/close。
  **批次推进（D2）**：放行判据满足后立即注入快照内剩余条目（同样 ≤8/批，累计不超 20 硬上限）；**快照内条目全部注入并闭环**后，才允许进入 R5 收口判定——否则关闭判据永远绿不了。
- **产出**：`task-map.md`（三列）+ **过程度量记录（留位）**：reuse 率 / 冻结后变更单数 / 扫描发现密度（首版只记录不设门槛）。
- **退出准则**：所有卡片有 task_id + 所有 reuse 有接线任务或豁免记录，无遗漏；汇总向用户报告后进入 R5。

### R5 开发与收口

- **开发**：本会话继续，或移交 `cw-executor-senior-engineer` 按任务单认领（P1-P4 前置与共享门禁同一套；移交按 R4 交接包与身份约定）。
- **卡片缺陷路径（F3）**：开发中发现卡片缺陷 → 按 cw-executor-senior-engineer 既有协议记录 finding + 走 §5 R3 变更单程序，不得擅自改设计。
- **收口**：
  1. `code-review-cw` 常规审查（图谱选靶 → 审查 → 发现建单 → 修复）
  2. **规范扫描（F2）**：以 R2 `symbol-anchor.md` 的 new/extend 清单为基线——`get_uncommented_symbols` / `get_orphan_symbols` 只扫本次新增/修改符号；**orphan 判死必须回读源码复核**；全仓扫描降级为可选周期项
  3. **coverage 扫描（M3）**：`find_uncovered_functions` / `get_test_coverage_summary` 逐个 new/extend 符号查覆盖；**数据源未导入时记 N/A 并显式声明，不得记 0 冒充"已测"**（S0 卫生规则）
  4. guardrail/semgrep 例外处理检查（开发期已拦 + 收口复查）
  5. **基线漂移检查（B3）**：对比当前 HEAD 与 R3 冻结基线，漂移则核对每处偏差是否有对应变更单（含 REQ 变更）
  6. **feature 级验收（B2/C3）**：全部任务闭环后按 §3.4 执行——**scope 快照内** REQ→AC 全绿 + 留证据，才算需求关闭；快照外条目按 deferred 处理并记录下批归属
  7. **证据落盘与收口提交（D1）**：验收证据写 `evidence/<AC-id>.md`、扫描结果写 `evidence/scan-*.md`，ac-map"证据位置"列逐条回填；完成后做一次 **R5 收口追加提交**（独立提交，不混源码）——**证据不落盘 = 验收不可审计**
  - 扫描与验收发现同样走"建单 → 修复"闭环，不口头放过；**这些整改单同样遵守 B1（C8）：description 首行带 REQ 编号，无对应 REQ 的记 REQ-GAP 并归类，不得无编号建单**
- **退出准则**：全部任务单闭环 + 各扫描零未处置发现 + **scope 快照内 REQ→AC 验收全绿且证据齐备已提交**（需求关闭）。

## 6. 多轮澄清协议细则

- 轮次上限 **3**；每轮问题数 ≤4；问题必须带候选选项或示例（降低用户回答成本）。
- 已能回答的问题不重复问；用户说"你定"时，agent 给出默认方案并记入 assumptions，不反问。
- assumptions 模板：`{假设内容, 影响范围, 推翻条件}`；**parking-lot 类型**额外标注"当前范围外原因"。
- **探路分支 R0-X（v2.1 转正）**：3 轮后仍模糊 → 不产 contract 卡片，建 **spike 任务单（时间盒，advisory——无自动终止机制，C6）**，结论回填 R0 后重走流程；spike 任务同样入 task-map（REQ 标 REQ-SP）。

## 7. 函数 contract 卡片模板（v2.2 定稿，后续未改）

```markdown
### <函数名>
- **文件/模块**：<相对路径>::<qualified_name>
- **REQ**：<REQ-xx>（<must/should/could>，revision：<n>）
- **签名**：<语言相关签名，Rust 例：fn process(req: Request) -> Result<Response, ProcError>>
- **入参契约**：
  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
- **出参契约**：
  - 成功：<形态>
  - 失败：错误枚举 <E>，每个变体 ↔ 失败点：
    | 错误变体 | 触发条件 | 上层如何处理 |
  - **异常场景矩阵勾选**：<8 类中本函数涉及哪些；不涉及标"不适用">
- **异常路径**：<每个可能失败的点 → 处理方式；禁止裸 unwrap/吞异常>
- **副作用**：<IO / 状态 / 锁 / 网络>（<关联 NFR 项，无则省略>）
- **依赖符号**：reuse <qname> / extend <qname> / new <本卡片>
- **测试要点（GWT 双格式）**：
  - Given/When/Then ≥2 条，**其中 ≥1 条为失败分支**（Then 为错误形态）
  - 清单：[ ] 边界值 [ ] 异常分支 [ ] 并发场景
- **验收映射**：<ac-map.md 的 AC id>
- **注释要求**：<doc comment：what + why + 错误语义>
- **assumptions**：<R0 遗留假设，含 parking-lot，无则"无">
```

## 8. 任务粒度与注入策略

粒度判断引用 cw-planner-architect 的复杂度预检（>3 独立模块 / >5 实现步骤 → 拆分），**引用不复制阈值**（唯一单源纪律）。

| 函数类型 | 粒度 | 理由 |
|---|---|---|
| 核心函数（被依赖/含业务规则/有外部 IO） | **一单一 function** | 可独立验收、独立审查 |
| 叶子/工具函数（同文件） | 按文件**分组**一单 | 避免任务单爆炸 |
| 纯 CRUD/包装 | 并入调用方任务 | 无独立设计价值 |
| **reuse 接线**：调用既有符号的对接代码 | 一单或按文件分组 | 复用不落地 = 假复用（M1）；同样入 task-map 追溯 |

- **跨文件函数**：一单的 steps 全部 `target_file` 指向同一文件（匹配 executor 单文件白名单）；跨文件 helper 单独建单，依赖声明按 §5 R4 的 F4 三件套。
- **优先级**：需求级 MoSCoW（R0）→ 卡片与任务继承；函数级 🔴 主链路 / 🟡 支撑叠加；**首批只建 scope 快照内 must 链路**（C3 对齐）。
- **分批**：首批 ≤8 试点 + 20 硬上限，放行判据见 §5 R4（C7）。

## 9. 规范可执行化（规则不是 prompt，是检查）

| 规范 | 检查方式（cw 工具） | 时点 |
|---|---|---|
| 每个函数都有注释 | `get_uncommented_symbols` / `get_comment_coverage`（**限新增符号基线**） | R5 收口 + 可选周期扫描 |
| 避免死函数 | `get_orphan_symbols`（**限新增符号基线 + 回读源码复核**） | R5 收口 |
| 异常分支有测试（M3） | `find_uncovered_functions` / `get_test_coverage_summary`（**限新增符号基线；数据源未导入记 N/A 并声明，不记 0**） | R5 收口 |
| 例外处理避免快乐路径 | `guardrail_add_rule`（编辑期拦截）+ semgrep + R3 门禁卡异常矩阵 | 开发期 + 收口 |
| 读取符号都通过 cw | `semantic_search` / `get_file_symbols` / `get_symbol` | R2 锚定 + 开发期定位 |
| 验收覆盖率 100% | ac-map.md REQ→AC 映射（§3.4，两条链；按 scope 快照判关闭） | feature 验收 |
| 禁止 TODO 进提交 / 禁 print 进库等 | 已有 rule sync + `guardrail_scan` | 开发期 |

原则不变：**能在工具层验证的规范不写进 prompt 靠自觉**；工具覆盖不了的由门禁卡 + R3 卡片审查 + code-review-cw 收口兜底。

## 10. 共享门禁 reference（已落地为仓库级单一源）

门禁**不再放在任一技能包内**，抽取到 `skills/_shared/cw-gates.md`：

| 小节 | 内容 | 来源 |
|---|---|---|
| G0 | 工具可用性（按名后缀匹配，查不到 ≠ 技能坏） | code-review-cw 阶段零 |
| G1 | canonical 工作区绑定（registry ≠ task_db） | 同上，实测 registry=175 / task_db=10 |
| G2 | 身份注册（不自动注入；会话对齐；身份不跨会话借用） | 同上 + `E_IDENTITY_SESSION_MISMATCH` 实测 |
| G3 | 合同声明（hash 当场动态读取） | 同上 |
| G4 | snapshot 刷新 | 同上 |
| G5 | 变更路径白名单（一单一文件） | 同上 |
| **G6** | **建单格式契约**：title 用符号不用行号 / description 首行 `REQ-xx` / `action` 取值 / `check_items` 字符串 / 依赖三件套 / 追溯闭环 | 本轮新增（ALM 缝合，§13.6） |

分发方式：**源码单一、分发独立**——两个 `install.ps1` 各自把 `../_shared/cw-gates.md` 注入为安装包的 `references/cw-gates.md`，装到宿主的每个包自包含、无跨包路径依赖。缺 `_shared` 时脚本 fail-closed 报错，不会装出一个缺门禁的包（已实测）。

code-review-cw 的三处内嵌段（阶段零门禁、阶段三 P1-P4 表、错误码速查）已替换为指针，只在指针旁保留最易踩的实测值，不再有第二份全文。

## 11. 分发包结构

```
skills/_shared/
└── cw-gates.md                    # G0-G6 唯一事实源（两包 install.ps1 各自注入，不在包内）

skills/README.md                   # ALM 技能地图：阶段归属 / 交接契约 / 已知缺口

skills/requirements-cw/
├── DESIGN.md                      # 本文档（v2.5，随包分发）
├── SKILL.md                       # 技能主体（做薄：六阶段流程 + 门禁插入点 + 工具映射）
├── references/
│   ├── contract-card.md           # contract 卡片模板（§7）
│   ├── gate-card.md               # 评审门禁卡模板（§2）+ 异常矩阵 8 类清单
│   └── change-ticket.md           # 冻结后变更单程序（§5 R3，含 REQ 变更）
├── install.ps1                    # 三家宿主安装 + AGENTS.md 注入 + 共享门禁注入
└── README.md                      # 同事侧说明
```

**上架元数据（F6 落点调整）**：`plugin.json` 与 `agents/*.md` 属**专家市场上架包**产物（参照 code-review-cw 在 `my-experts` 的包结构），不放入本仓 `skills/requirements-cw/`——该目录只放跨宿主安装件（SKILL.md + references + install.ps1 + README）。上架时按 code-review-cw schema 生成 displayName / displayDescription / quickPrompts（如"只做 R0-R3 出方案，暂不建单"）/ tags / defaultInitPrompt，正文由 SKILL.md 派生。

**触发 description（含负向边界）**：
`需求分析与拆解专家：新项目从需求澄清、技术栈选型到函数级 contract 设计全程门禁化（方案对比+被否决理由+异常矩阵+未决项），存量仓功能迭代走精简链；需求 REQ 编号双向追溯到任务与验收，多轮澄清≤3 轮，注入 callwarden 任务单后交 executor 开发，feature 级验收后才关闭需求，收口扫描死函数/注释缺口/异常违规。已有代码的审查不触发（用 code-review-cw）。触发词：新功能、新项目、需求拆解、设计一下、模糊需求、做一份方案。`

## 12. 已拍板结论（v1 五问）

1. **澄清 3 轮封顶 ✓**；3 轮后转 R0-X 探路分支（已转正，§6）。
2. **卡片一次性确认 ✓** + 确认摘要（§5 R3）。
3. **跨文件独立建单 ✓**（单文件白名单是实测门禁）；依赖弱约束按 F4（§5 R4）。
4. **planner 落地后交接 ✓**；切换开关 = capability `planner_governance_v1` 检测（§4）。
5. **警戒线改首批 ≤8 试点 + 20 硬上限**，首批限 scope 快照内 must 链路（§5 R4、§8）。

## 13. 修订日志

### 13.1 v1 → v2

| 级别 | 条目 | 落点 |
|---|---|---|
| 🔴 F1 | R0 退出准则时序倒置 → 数据流可画 + 验收可执行 + 复述确认；"卡片填满"归 R3 | §5 R0/R3 |
| 🔴 F2 | 收口扫描缺新增符号基线（ruff 9927 同型事故）→ 以 symbol-anchor 为基线 + orphan 回读复核 | §5 R2/R5、§9 |
| 🟡 F3 | R5 卡片缺陷路径缺失 → 引用 executor 既有协议，不重新发明 | §5 R5 |
| 🟡 F4 | task_create 依赖字段待实测 → 三件套兜底 | §5 R4 |
| 🟡 F5 | `.design/` 工件处置 → **后被 C1 修订为"保留跟踪 + R3 独立基线提交"，gitignore 方案撤销** | §5 R1、§11 |
| 🟡 F6 | plugin.json 元数据缺失 | §11 |
| 💭 | 卡片模板笔误 / description 负向边界 / title 格式 / 引用不复制、SKILL.md 做薄 | §7、§11、§5 R4、§8 |
| v2 新增 | 评审门禁卡（四件套 + 多轮协议 + 反 happy-path 双防线） | §2 |
| v2 新增 | 生命周期条件分支（完整链 / 精简链 + G-Tech + G-Test） | §3 |
| v2 新增 | R0 前置轻量上下文 + 输入卫语句 + 停车场 | §5 R0 |
| v2 新增 | GWT 验收双格式 + 失败分支强制 + 验收映射 | §7 |

### 13.2 v2 → v2.1（ALM 复审）

| 级别 | 条目 | 落点 |
|---|---|---|
| 🔴 B1 | 追溯链单向 → REQ 编号 + 卡片 req 字段 + description 首行 REQ + task-map 三列（REQ↔卡片↔task） | §1、§5 R0/R3/R4、§7 |
| 🔴 B2 | 无 feature 级验收 → REQ→AC 映射 + 全任务闭环后执行 + 留证据才算需求关闭；与 G-Test 合并为同一机制两条链深度不同 | §3.4、§5 R5 |
| 🔴 B3 | 冻结后变更程序缺失 → 变更单（revision+1 + 任务留言 + 重跑 check_items + >3 张升级重规划）+ R3 冻结记 HEAD 基线 + R5 漂移检查 | §5 R3/R5 |
| 🔴 B4 | R2/R3 退出准则循环依赖 → R2 退出 = 锚定表完成，卡片要求归 R3 | §5 R2 |
| 🟡 M1 | reuse 无落地路径 → 接线任务入粒度表 + R4 退出准则覆盖 + 豁免须写明理由 | §8、§5 R4 |
| 🟡 M2 | 跨会话移交身份/衔接未提 → R4 交接包清单 + executor 自身身份领取（身份不跨会话借用） | §5 R4 |
| 🟡 M3 | coverage 扫描缺失 → §9 加行 + N/A 卫生（不记 0 冒充） | §9、§5 R5 |
| 🟡 M4 | 需求级优先级缺失 → R0 MoSCoW + 卡片继承 + 首批限 must | §5 R0、§8 |
| 🟡 M5 | steps action 值未验证 → 实现期实测项 ②（fix 实测 / implement 待验证） | §5 R4 |
| 💭 | NFR 显式位 / R0-X 探路转正 / graph 降级声明 / 过程度量留位 / `.design/` 提交时机 | §5 R0/R2/R4、§6 |

### 13.3 v2.1 → v2.2（C1-C8 复审）

| 级别 | 条目 | 落点 |
|---|---|---|
| 🔴 C1 | `.design/` 处置自相矛盾（gitignore vs 基线提交）→ **定稿：保留 git 跟踪，R3 冻结作独立设计基线提交；撤销 gitignore 条款与 install.ps1 写 .gitignore** | §5 R1/R3、§11 |
| 🟡 C2 | 精简链 AC 无承载工件 → `ac-map.md` 统一承载（两条链都产；R0 初版、R3 定稿）；卡片验收映射只引用 AC id | §3.4、§5 R0/R3/R4、§7 |
| 🟡 C3 | MoSCoW 分批与"全绿才关闭"冲突 → R3 定义 **scope 快照**；关闭判据 = 快照内 AC 全绿；快照外 deferred 不阻塞 | §3.4、§5 R3/R4/R5、§8 |
| 🟡 C4 | R2 锚定可推翻 R1 设计但无回环 → R2 实质冲突时局部返工回 R1/R0，返工原因入 symbol-anchor.md | §3.2、§5 R2 |
| 💭 C5 | REQ 自身变更程序未定义 → 并入变更单：REQ revision+1 + 用户确认 + 沿 task-map 反查同步 ac-map | §5 R3 |
| 💭 C6 | spike 时间盒无强制机制 → 标注 advisory（无自动终止，不造成错觉） | §5 R0、§6 |
| 💭 C7 | 首批放行无判定标准 → 可操作判据：全部到 review + 无 E_* 门禁错误 + ≥1 单 apply/close 全链路 | §5 R4、§8 |
| 💭 C8 | 整改单未带 REQ 断链 → 验收/扫描整改单同样首行带 REQ；无对应 REQ 记 REQ-GAP 归类 | §5 R5 |

**参照来源**：官方 requirements-driven-workflow、feature-dev、product-strategy-team、mvp-dev-expert-team、prd-acceptance-spec（REQ/AC 无重复无悬空 / 三态台账 / 异常矩阵 / 覆盖率 100%）、fullstack-architects（复述确认 / 被否决铁律 / 决策与权衡）、code-review-cw（四门禁 / title 约定 / E_IDENTITY_SESSION_MISMATCH 实测）。

### 13.4 v2.2 → v2.3（D1-D4 复审）

| 级别 | 条目 | 落点 |
|---|---|---|
| 🟡 D1 | 基线后证据无落盘与提交策略（证据链后段悬空）→ 新增 §3.5 工件与证据目录：`evidence/` + `change-log.md` + `.design/parking-lot.md`；三个提交时点（R3 基线 / 变更单 / R5 收口）；证据缺失 = AC 未通过 | §3.4、§3.5、§5 R3/R5 |
| 💭 D2 | snapshot 内指定 should 无注入时点（关闭判据永远绿不了）→ 首批只 must，放行后注入快照剩余条目，全部闭环才进收口判定；deferred 只指快照外 | §5 R4 |
| 💭 D3 | 停车场无出口 → R0 开工回看 `.design/parking-lot.md`、收口追加；改为跨 feature 汇总索引（否则"回顾上一 feature"不可操作） | §5 R0、§3.5 |
| 💭 D4 | 放行判据依赖 reviewer 会话 → 本技能只读判定，apply/close 归 executor/reviewer 角色协议 | §5 R4 |

**本轮另补两处防复发**（自评审追加，非复审提出）：

1. **D1 的"独立提交"条款**——与 C1 同类冲突（两处各写各的，未对账）预先对账：证据/设计提交独立于源码任务提交，hunk 级纪律只约束源码。
2. **D2 同时修正 R4 原文口径**——原文"should/could 标 deferred"与 §3.4 快照含"指定 should"冲突；修正为 deferred 仅指快照外条目，否则 D2 修完仍是矛盾。

### 13.5 实现前置：政策拍板与实测关闭（2026-11-22）

| 项 | 结论 | 证据 / 落点 |
|---|---|---|
| #1 hotfix 豁免 | **按影响面判定**：单文件 + 无新增符号 + 已有测试覆盖，三条同时满足才跳过四件套第 1/2 件；仍保留异常矩阵 + 未决项，brief 写明判据核对结果 | §2.4 |
| #2 G-Test 范围 | **只产清单 + AC 映射**，不生成测试代码、不执行；执行交 R5/CI，结果回填 evidence/ | §3.4、§3.5 |
| 实测 ① task_create 依赖 | **不支持**结构化依赖；子任务树只表达层级，不是 blocking | MCP 工具 schema：参数仅 `title/description/steps/creator/workspace_id/workspace_instance_id`；`task_create_subtask` 只有 `parent_task_id` |
| 实测 ② steps.action | **无枚举校验**，非空字符串即可；惯例 `implement`/`test`/`audit`；`fix_defect` 为 daemon 保留值（整改步骤专用）不可手工用 | `task_collab_contract.rs:1746-1765`（仅校验非空）、`task_collab_lease.rs:313`（`fix_defect` 判定） |
| 实测 ③ check_items 类型 | **字符串**；传数组会被序列化为 JSON 文本落库，读回是转义串 | `task_collab.rs:524-535` `step_field` |
| 实测 ④ steps 非空 | 空 `steps` 不报错但建出无步骤任务 → executor 卡在 `task_next_step` | `insert_task_steps` 遍历空数组直接通过 |

### 13.6 ALM 缝合（2026-11-22，路线 A 的 ①②③）

**背景**：对 `skills/` 做 ALM 覆盖度审计，确认三个包不是"ALM 被切成三份"——`requirements-cw` 与 `code-review-cw` 是首尾相接的两个 ALM 技能，`g0-experiment` 是盲审实验协议、与交付链无关。审计发现 7 处断点，本轮修其中 3 处结构性断点。

| 项 | 断点 | 修法 | 落点 |
|---|---|---|---|
| ① | 门禁有两份事实源（code-review-cw 内嵌全文 + requirements-cw 抽取版），改一处另一处必漂 | 抽到仓库级 `skills/_shared/cw-gates.md`；两包 install.ps1 各自注入；内嵌段改指针 | §10、两包 SKILL.md / install.ps1 / README |
| ② | 追溯链在审查处断掉：code-review-cw 全文 0 个 REQ 概念，整改单不带 REQ，C8 在真实链路上失效 | description 首行强制 `REQ-xx`（无则 `REQ-GAP`）；阶段一结构化条目加 `req` 字段；整改单追加进 task-map 三列 | 两包 SKILL.md、`_shared/cw-gates.md` §G6 |
| ② | title 格式冲突（审查侧 `<路径>:<行号>` vs 需求侧 `<路径>:<符号>`），且行号随修复漂移 | 统一为**用符号不用行号**；新增文件写 `<路径>:新增 …` | `_shared/cw-gates.md` §G6.1、两包 SKILL.md |
| ② | `check_items` 传数组被 daemon 序列化成 JSON 文本，executor 侧读回是转义串 | 契约层固定为字符串（换行分隔），两包模板同步 | §G6.3、code-review-cw 阶段二 |
| ③ | 没有入口说明"哪段归谁、哪里还缺" | 新增 `skills/README.md`：覆盖矩阵 11 行 + 两条链 + 交接契约 + 提交节奏 + 缺口表 + 新增技能准入 | `skills/README.md` |

**验证证据（本轮实测）**：

- 两个 `install.ps1` 在临时 `USERPROFILE` 下实跑：`requirements-cw` 装出 4 个 references、`code-review-cw` 装出 1 个，两份 `cw-gates.md` 哈希相同。
- 缺 `_shared` 的退化场景：脚本 `throw`（"分发时须连同 skills/_shared/ 一起拷贝"），且不产出缺门禁的包 —— fail-closed 成立，非口头承诺。
- Parser 校验两脚本 `PARSE_OK`（442 / 437 tokens）。

**本轮未做（保持分层，不混装）**：

| 缺口 | 为什么不塞进 requirements-cw |
|---|---|
| ④ 测试执行者缺位（无技能拥有"跑测试并回填证据"） | 需先定"谁执行 / 在哪跑 / 证据格式"三问；直接塞进来会让 R5 变巨无霸，且与 CI 职责重叠 |
| ⑤ `g0-experiment` 双份拷贝 | 与 ALM 交付无关，去重方向需用户定（保留 `.agents/` 一份 or 反之） |
| 开发/审核技能在 `.agents/skills/`、角色模板在 `docs/role-loop-templates/` | 分层正确：`skills/` 只放"给宿主客户端用"的技能，已在 §1 矩阵与 README §6 显式标注 |

## 14. 开放问题（待拍板）

1. ~~**门禁卡对小型 hotfix 是否过重？**~~ → **已拍板：按影响面判定**（单文件 + 无新增符号 + 已有测试覆盖，三条同时满足才跳过第 1/2 件，见 §2.4）。
2. ~~**G-Test 自动化程度**~~ → **已拍板：只产清单 + 映射**，不生成测试代码、不执行；执行交 R5/CI，结果回填 evidence/（§3.4）。
3. **G-Tech 候选池来源**：固定清单 vs 按场景检索社区/市场数据？
4. **数值校准**：首批 ≤8 试点、门禁 ≤2 轮挑战、变更单升级阈值 3 张——均标注可调，首跑后校准。

> **实现前置**：#1 / #2 已拍板；两个实现期实测项已关闭（§13.5，含证据行号）→ **可直接进 SKILL.md**；#3 / #4 首跑后校准。
