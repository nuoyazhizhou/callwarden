# Call Warden 竞品分析报告

**日期**：2026-08-19
**类型**：竞品分析（产品战略团队 SOP · 工作流 2）
**参与成员**：竞析（Compa，竞品分析师）、数析（Metric，数据分析师）、瑞思（Reese，用户研究员）、方向明（Fang，主理人/汇编）

---

## 📌 TL;DR（执行摘要）

- **核心目标**：在已拥挤的"代码智能 / 代码知识图谱"赛道中，明确 Call Warden 的差异化定位与错位竞争策略。
- **关键决策**：**不与 Sourcegraph / CodeQL 在"代码理解"维度正面竞争**；改打 **"面向 AI Agent 的代码知识图谱 + 多 Agent 变更治理层（fail-closed）"** 蓝海——该能力（task/lease 租赁、evidence gate、verdict、audit chain、三角色治理）**目前没有任何竞品覆盖**，是矩阵中最该高亮的一列。
- **市场窗口**：AI 编码 Agent 爆发（Copilot 2000 万用户、Cursor $20 亿 ARR）+ MCP 已成事实标准（10,000+ server、28% 财富 500 已部署）+ 多 Agent 治理空白（327% 增长、>40% agentic 项目或因缺治理在 2027 前被砍、仅 21% 有成熟治理）+ 监管驱动（EU AI Act 2026-08 生效）。四股力量同时利好 Call Warden。
- **下一步**：刷新对外定位叙事 → 把 237 个 MCP 工具包装为"供给端飞轮"上架 registry → 借 EU AI Act 合规窗口做市场教育 → 补齐 IDE/PR 原生入口。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| 推荐方案 | 定位为 **"面向 AI Agent 的代码知识图谱 + 多 Agent 变更治理层（fail-closed）"**；以 MCP 供给端飞轮 + 合规叙事切入，避免与 Sourcegraph 在"代码理解"正面竞争 |
| 优先级 | **P0**：定位刷新 + 治理差异化叙事；**P1**：MCP server 上架 + IDE/PR 入口补齐 |
| 预期影响 | 卡位"Agent 治理"蓝海（无现存竞品），借 MCP 标准零成本分发，撬动金融/政企私有化刚需 |
| 资源需求 | 产出/文档（白皮书、MCP server 打包、参考实现）、社区与合规内容、工程补齐入口 |
| 风险等级 | **中**——大厂（Sourcegraph / Claude Code / Cursor）可能补治理层；市场需教育"为何 Agent 改动需要治理" |

---

## 1. 竞品全景图（来自竞析）

### 直接层：代码知识图谱 / 代码智能平台
- **Sourcegraph（Cody + Amp）**：用 SCIP 代码图让 AI 看懂整个组织的跨仓库代码库；企业版 $59/人/月，个人版已停售。
- **CodeSee**：自动生成并持续更新的代码地图，可视化依赖、数据流与变更影响（已被 GitKraken 收购，定价需询价）。
- **ArchGuard**：开源架构治理平台，按 C4 做依赖/API/数据库/变更影响分析（Thoughtworks 系，免费）。
- **GitHub CodeQL**：把代码当作可查询的关系数据库，做语义级污点追踪（OSS/公有库免费，私有库属 GitHub Advanced Security）。
- **Bloop**：基于 tree-sitter 嵌入 + 向量检索的自然语言代码语义搜索（个人免费）。
- **Greptile**：对代码库建语义索引做 AI 审查，并通过 Genius API 暴露代码理解能力（Pro $30/人/月）。
- **Lattix / NDepend**：依赖结构矩阵（DSM）做架构依赖、环检测与影响分析（偏 .NET/Java，商业询价）。

### 相邻层：AI 编码 Agent / 安全 / 架构可视化
- **Cursor**：AI 原生 IDE，Agent 模式多文件自主编辑（Hobby 免费；Ultra $200/月）。
- **Claude Code（Anthropic）**：终端 Agent，1M 上下文 + Agent Teams + MCP（Pro $20 / Max $100-200）。
- **Cognition Devin**：全自主 AI 工程师，云端沙箱 ticket→PR（Core $20 / Teams $500）。
- **OpenAI Codex CLI**：开源终端 Agent，沙箱执行（捆绑 ChatGPT Plus $20）。
- **GitHub Copilot**：IDE 内联补全 + 异步 coding agent（Free / Pro $10 / Enterprise $39）。
- **Cline / Aider**：开源 Agent（Plan/Act + MCP / repo-map + git 自动提交，免费 BYOK）。
- **Semgrep**：规则型 SAST，Pro 跨文件污点分析（Community 免费）。
- **SonarQube**：40+ 语言静态分析 + Quality Gate（Community 免费自托管）。
- **Tabnine**：隐私优先、可 air-gapped 部署的 Agentic 平台（合规驱动，$39 起）。

### 新兴 / 潜在层：Code-Graph + LLM 创业项目
- **Codegen**：语义代码图驱动的 ticket→PR 自动迁移（Individual ~$10 / Teams ~$199）。
- **Sourcery**：面向"AI 提速但漏洞也提速"的 PR/IDE 安全审查（SOC2，可 BYOLLM）。
- **Sweep（Sweep AI）**：把 GitHub issue 变成初级工程师式 PR（开源免费）。
- **Grit.io**：基于 GritQL（AST 查询）的大规模代码迁移（2025-04 被 Honeycomb 收购，独立产品正 sunset，仅 GritQL 开源留存，矩阵中建议降权）。

> 注：同类开源 **CodeGraph（suatkocar/codegraph）** 与 Call Warden 同技术栈（tree-sitter + SQLite + MCP），2026.5 约 2.9 万 star，实测 token −57%、工具调用 −71%——证明赛道需求真实，但**它不含治理层**，是 Call Warden 的天然"同栈友商 / 对标样板"。

---

## 2. 功能对比矩阵

**图例**：✅ 完整支持 ｜ ⚠️ 部分/有限 ｜ ❌ 无 ｜ — 不适用

| 维度（来自竞析 12 维建议） | Call Warden | Sourcegraph | CodeSee | CodeQL | Greptile | Cursor | Claude Code | Devin | Cline | Semgrep | Tabnine | Codegen |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1. 代码图构建（符号/调用边/依赖边） | ✅ | ✅(SCIP) | ✅ | ⚠️(关系库) | ⚠️(语义索引) | ❌(嵌入) | ❌(1M ctx) | ⚠️(项目图) | ❌ | ⚠️(污点) | ❌ | ✅ |
| 2. 跨仓影响分析（Blast Radius/克隆/环检测） | ✅ | ⚠️ | ✅(可视化) | ⚠️(污点) | ⚠️(多跳) | ❌ | ❌ | ⚠️ | ❌ | ⚠️(Pro) | ❌ | ✅ |
| 3. 语义检索（向量化/自然语言） | ✅ | ✅ | ⚠️ | ⚠️ | ✅ | ✅(@codebase) | ✅(MCP) | ✅ | ⚠️ | ❌ | ⚠️ | ✅ |
| 4. 本地/私有部署（SQLite/air-gapped） | ✅ | ⚠️(企业自托管) | ❌(云) | ✅(OSS) | ⚠️(BYOLLM) | ❌ | ⚠️ | ❌(云) | ✅ | ✅(CLI) | ✅(air-gapped) | ❌(云) |
| 5. **MCP 供给端暴露（把图/治理当工具输出）** | ✅(237工具+145CLI) | ⚠️(MCP+CLI输出) | ❌ | ❌ | ⚠️(Genius API只读) | ⚠️(消费端) | ⚠️(消费端) | ❌ | ⚠️(消费端) | ❌ | ⚠️(消费端) | ❌ |
| 6. **多 Agent 治理：task/lease 租赁 fencing** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ⚠️(Teams编排) | ⚠️(Teams并行) | ❌ | ❌ | ❌ | ❌(并行不协作) |
| 7. **证据门禁 evidence gate + 裁决 verdict** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 8. **审计链 audit chain（不可篡改）** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 9. **fail-closed 治理正确性** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 10. 合并前风险门禁（阻断高风险变更） | ✅ | ❌ | ⚠️(影响视图) | ❌ | ❌ | ❌(Bugbot) | ❌ | ❌ | ❌ | ⚠️(CI门禁) | ❌ | ❌ |
| 11. 开源/免费档 | ✅(开源) | ❌(个人版停) | ❌(询价) | ✅(OSS) | ❌ | ✅(Hobby) | ⚠️($20) | ⚠️($20) | ✅(BYOK) | ✅(Community) | ❌($39起) | ⚠️ |
| 12. 合规（SOC2/ISO/air-gapped/BYOLLM） | ✅(本地+BYOLLM) | ✅(SOC2/ISO) | ⚠️ | ⚠️ | ⚠️(SOC2) | ⚠️ | ⚠️ | ⚠️ | ✅(自托管) | ✅ | ✅(air-gapped) | ⚠️ |

**矩阵判读**：维度 1–4 是红海（Sourcegraph/CodeQL/CodeSee 已设标）；维度 5–9（尤其 6–9 的"多 Agent 治理 + fail-closed"）**整列仅 Call Warden 为 ✅**，是绝对差异化护城河。维度 10（合并前门禁）Semgrep/CodeSee 有弱版，但无治理正确性。

---

## 3. 市场数据 & 趋势（来自数析）

### A. 市场概览（规模 / 增速 / 预测）

| 口径 | 数值 | 来源 |
|---|---|---|
| 企业级 AI 编码 Agent 市场（年化，2026.4） | $9.8–11.0B | Gartner |
| AI 代码生成（全球，2030 预测） | $25.7B（CAGR +24.5%） | Grand View / 综合 |
| AI 编程工具大盘（2030 预测） | $26B | AgentMarketCap |
| 开发者工具（窄口径，2030） | $15.91B（CAGR +17.1%） | TBRC |
| 开发工具与平台（宽口径，2030） | $275B（CAGR +16.5%） | IIM |
| 国内 AI 编程市场 | 2025 ¥3.99 亿 → 2026 末 ¥11.73 亿（年增速近 200%） | IDC 中国 |

全球开发者约 1.08 亿，AI 工具渗透率约 85%，付费 AI 工具 MAU ~4,000 万。AI 编码是生成式 AI 中 ROI 最清晰、增长最快的落地场景之一。

### B. 关键趋势（4 条，附数据）
1. **AI 编码 Agent 爆发**：Copilot 累计 2,000 万用户、付费 470 万（+75%）、生成 46% 活跃代码量；Cursor ARR $1亿→$20亿、估值 ~$60B（SpaceX 拟收购）；Claude Code ARR $10亿→$25亿；Devin ARR $37M→$492M（约 13×）。来源：axis-intelligence / gradually.ai / agentmarketcap / valueaddvc。
2. **MCP 生态爆发（"USB-C for AI"）**：2026.3 已有 10,000+ 活跃公共 server、9,700 万次/月 SDK 下载；2025.12 捐 Linux 基金会 AAIF（146 家成员）；2026 Q1 28% 财富 500 已部署 MCP server（金融 45%、医疗 32%）。来源：agentmarketcap。
3. **代码图/代码理解需求上升**：Agent 在大型仓库"检索文件而非关系"产生幻觉与错误改动；Sourcegraph 转身做"Agent 代码理解"、Devin 建项目图、开源 CodeGraph 已达 2.9 万 star 且实测省 57% token。来源：dev.to / atoms.dev / github.com/suatkocar/codegraph。
4. **多 Agent 协作 / Agent 治理成为焦点**：多 Agent 系统 4 个月增长 327%；有治理的公司生产项目数是 12×；Gartner 警告 >40% agentic AI 项目或因成本/价值不清/缺风险控制在 2027 前被砍；仅 21% 公司有成熟 Agent 治理；新加坡 2026.1 发首个 agentic AI 治理框架。来源：latentview / trantorinc / superkind。

### C. 竞争格局动态（节选）
- Cursor Series C/D：$900M / $2.3B → $29.3B（2025.6–11）
- Cognition 收购 Windsurf、ARR $492M、估值 $25–26B（2026.5）
- Sourcegraph：关 Cody 个人版、推 Amp 并分拆，~$50M ARR
- MCP 捐 Linux 基金会 AAIF（2025.12）；OpenAI/Anthropic/Google 亲自下场做完整编码 Agent

### D. 对 Call Warden 的定位启示（数析）
1. 同时踩中 **MCP-native + 代码知识图谱**两条最快增长曲线，建 MCP server 暴露图即可零成本接入 Claude Code/Cursor/Codex/Gemini 全矩阵；本地优先契合金融/政企私有化刚需（近 47% 企业把代码本地留存列为采购首要条件）。
2. 代码知识图谱是真需求且有量化验证，但 Call Warden 的差异化不在"搜索"，而在**本地、结构化影响分析**。
3. **多 Agent 治理是尚被低估的空白楔点**——纯代码图或纯 Agent 玩家均未覆盖，可作错位竞争点。

---

## 4. 用户感知（来自瑞思）

### A. 用户画像与诉求
- **开发者用户**（Cursor/Copilot/Cline/Aider 日常使用者）：要"在 IDE 里更快更稳"，已容忍 AI 缺陷。诉求=速度可控、大型仓库可理解（"看懂"跨文件依赖而非反复 grep）、隐私/本地优先、准确性优先于花哨。
- **AI Agent 构建者**（多 Agent 编码系统工程师/Tech Lead）：诉求从"写代码"转向"验证意图与治理自主"。真正购买的是可审计、可回滚、受约束的自主：影响分析与爆炸半径、治理原语（按任务限权/短生命周期凭证/持续日志/谁负责证据链）、跨 Agent 可核查（一写一审）、跨仓/跨语言 Graph 而非 Wiki。

### B. 痛点地图（按严重度）
| 严重度 | 痛点 | 主要用户 |
|---|---|---|
| 🔴 高 | 大型/多仓代码库超上下文窗口，扁平文本丢失调用/依赖边 → 幻觉与"almost-right"代码 | 开发者+构建者 |
| 🔴 高 | 多 Agent 责任归属空白，错误在链路静默传播（Berkeley 研究：41.8% 规范/设计、36.9% Agent 间错位） | 构建者 |
| 🔴 高 | 安全/隐私失守（未授权文件操作 43.1%、越权 chmod；本地≠私有，token 残留可被窃） | 开发者+构建者 |
| 🟠 中 | 改动不安全/影响分析盲区（多文件重构成功率 <30%，无依赖图则不知改签名动哪些调用方） | 开发者+构建者 |
| 🟠 中 | 审查过载/监督扩展危机（Agent 并行产出远超人审带宽） | 构建者 |
| 🟡 低 | 成本与权限蔓延（整库 dump、无 deny-list、merge 冲突） | 开发者 |

### C. 对 Call Warden 类能力的感知与期待
社区对"代码图 + 治理"呈 **高接受、强期待、信任待建立**：开源 CodeGraph 把探索调用从 32 次降到 3 次（降 94%）、结构化上下文较摘要带来 ~44% 任务解决提升；用户已自发用 AGENTS.md / .cursorrules / worktree 隔离 / 审计 swarm 补位——Call Warden 的 impact/blast radius、evidence gate、verdict、audit chain、三角色治理正对应这些补位动作。**信任门槛明确**：开发者把 AI 当"不可信软件"自建护栏；构建者认为"能否看见 Agent 做了什么、能否回滚"比原始准确率更影响采用。

### D. 产品启示（瑞思，4 条）
1. **"改动前先算清爆炸半径"**——用代码图把影响分析从猜测变枚举，回应"almost-right 代码"与重构盲区。
2. **"让 AI 编码留痕、可审计、可归责"**——task/lease + evidence gate + verdict + audit chain，对应 EU AI Act（2026-08 生效）合规刚需。
3. **"治理结构即护栏，而非事后补丁"**——按任务限权、三角色治理、短生命周期凭证。
4. **"本地/私有的代码智能"**——代码不出域，契合开发者隐私诉求与受监管行业（HIPAA/GDPR/SOX）。

---

## 5. SWOT 分析

**Strengths（优势）**
- 唯一把"代码知识图谱" + "多 Agent 协作治理（fail-closed）"合体的产品；治理栈（task/lease / evidence gate / verdict / audit chain / 三角色）完整且当前无竞品覆盖。
- MCP **供给端**独特定位（237 工具 + 145 CLI），可零成本接入主流 Agent 矩阵，形成分发飞轮。
- 本地优先（tree-sitter + SQLite），契合金融/政企私有化刚需，支持 BYOLLM。
- 开源，可审计、可自托管，天然信任友好。

**Weaknesses（劣势）**
- 品牌/生态远弱于 Sourcegraph、GitHub；单点能力（语义检索/影响分析）与成熟图工具功能重叠。
- 缺 IDE/PR **原生入口**，需借道 MCP 进入 Agent 工作流，获客路径更长。
- 市场需教育"为何 Agent 改动需要治理"；商业定价/上手路径未明确。

**Opportunities（机会）**
- AI 编码 Agent 爆发 + MCP 成标准（10k+ server、28% 财富 500 采用）。
- 多 Agent 治理空白（327% 增长、>40% 项目 2027 前或因缺治理被砍、仅 21% 有治理）。
- 监管驱动（EU AI Act 2026-08 生效、新加坡框架）→ Agent 改动可审计/可回滚成刚需。
- 同类开源 CodeGraph 已验证省 57% token，证明赛道需求；企业本地代码留存刚需（47%）。

**Threats（威胁）**
- Sourcegraph 已转身做"Agent 代码理解"，可能补治理层；模型厂商（Cursor/Devin/Claude Code）亲自下场可能内置图/治理。
- Gartner 预警 >40% agentic 项目因成本/价值不清/缺风险被砍 → 预算收紧。
- 大厂标准化 Agent 编排（A2A + MCP）可能把治理层标准化并吸收，挤压独立治理层空间。

---

## 6. 差异化机会

1. **切换对比锚点**：从"代码理解工具"重新定位为 **"面向 AI Agent 的代码知识图谱 + 多 Agent 变更治理层（fail-closed）"**，避开与 Sourcegraph 正面竞争。
2. **把治理列做成唯一高亮项**：在一切对比材料中，将"fail-closed 治理（task/lease / evidence gate / verdict / audit chain）"做成竞品矩阵中仅 Call Warden 为 ✅ 的一列，建立"治理正确性"品类心智。
3. **MCP 供给端飞轮**：把 237 个 MCP 工具包装为官方 server 上架 registry，让任何 Claude Code/Cursor/Codex/Gemini Agent 一键接入"代码大脑 + 治理总线"，零自建集成成本。
4. **合规叙事卡位**：借 EU AI Act 生效窗口，把"Agent 改动可审计、可回滚、可归责"包装为受监管行业（金融/医疗/政企）的采购刚需。
5. **同栈升级定位**：对标开源 CodeGraph（同 tree-sitter+SQLite+MCP）但叠加治理层，主张"CodeGraph + 治理"，承接其 2.9 万 star 流量与社区。

---

## 7. 行动建议

- **定位刷新（P0）**：对外叙事统一为"Agent 协作的可信底座 / 治理总线"，核心一句话对标 fail-closed；所有材料先用新锚点。
- **MCP 供给端（P1）**：打包官方 MCP server 上架公共 registry，降低 Agent 接入 friction；同步补齐 IDE/PR 原生入口（VS Code / Cursor 扩展或 GitHub Action）。
- **市场教育（P1，持续）**：出"为什么 AI Agent 改动需要治理"白皮书/博客/演示，借 EU AI Act 合规窗口放大。
- **参考实现（P1）**：挑 1–2 个金融/政企私有化场景做端到端参考实现，证明"本地 + 治理"的可量化价值（爆炸半径 + 证据链 + 裁决 demo）。
- **护城河加固（P0）**：把 fail-closed 治理、evidence gate、audit chain 做成不可绕过且可演示的核心闭环，防止大厂"补一层治理"即抹平差异。
- **竞品监测（持续）**：紧盯 Sourcegraph / Claude Code / Cursor 是否补治理层，提前卡位叙事与标杆案例。

---

## ✅ 行动清单

| # | 行动 | 负责方 | 时间窗 |
|---|------|--------|--------|
| 1 | 定位叙事刷新：从"代码理解"切到"Agent 可信底座/治理总线"，确立 fail-closed 为核心卖点 | 产品 / 市场 | 2 周内 |
| 2 | 打包官方 MCP server 上架公共 registry，降低 Agent 接入 friction | 工程 | 3–4 周 |
| 3 | 补齐 IDE/PR 原生入口（VS Code / Cursor 扩展或 GitHub Action） | 工程 | 1 季度 |
| 4 | 出"AI Agent 改动为何需要治理"白皮书/博客，借 EU AI Act 合规窗口 | 市场 / 内容 | 持续 |
| 5 | 金融/政企私有化参考实现 1–2 个，证明本地+治理价值 | 方案 / 工程 | 1 季度 |
| 6 | 监测 Sourcegraph / Claude Code 是否补治理层，提前卡位 | 产品 | 持续 |

---

## ⚠️ 待确认 / 假设 / Non-goals

- **Non-goals**：本次为**整体产品竞品分析**，不针对单一模块撰写 PRD；不做一手用户访谈（用户感知来自公开社区二手数据）。
- **假设**：Call Warden 自身定价未公开，按"开源/免费 + 潜在企业版"呈现；竞品定价以公开来源为准，部分来源/地区有出入，以官网为准。
- **待确认**：
  1. Call Warden 是否已发布官方 MCP server 到公开 registry？若未，行动 #2 优先级应再提前。
  2. 目标客群优先级：是优先"AI Agent 构建者（治理刚需）"还是"普通开发者（理解刚需）"？两者叙事侧重不同。
  3. 是否计划做 IDE/PR 原生扩展？这决定获客路径长短。

---

## 附录：竞品定价锚点基准（来自数析，对齐竞析三层框架）

用于支撑定位启示 #3（CW 走"治理层"、按席位或按 Agent 调用量定价）。锚点来自竞析提供的明细，初步版，后续以竞析完整定价 CSV 扩展至 15–20 个竞品并交叉校验。

| 工具 | 所属层 | 定价锚点（USD/月） | 计费模式 |
|---|---|---|---|
| Sourcegraph（Code Search/Enterprise） | 直接层·代码智能 | ~$59/人 | 按席位 |
| Greptile | 直接层·代码评审/智能 | ~$30 | 按席位 |
| Tabnine | 相邻层·AI 补全 | $39–59 | 按席位 |
| GitHub Copilot | 相邻层·AI Agent | $10(Pro)/$19(Business)/$39(Enterprise) | 按席位→用量（2026.6 起转 AI credit） |
| Cursor | 相邻层·AI Agent IDE | ~$20(Pro) | 按席位+用量 |
| Claude Code | 相邻层·AI Agent CLI | ~$20（捆绑 Claude 订阅） | 捆绑订阅 |
| Devin（Cognition） | 相邻层·自主 Agent | $20 起 | 按 ACU 用量 |
| Semgrep / SonarQube | 相邻层·安全/质量 | 开源 / 企业定制 | 席位或源码行 |
| **Call Warden（建议）** | **新兴层·Agent 协作治理** | **按席位 或 按 Agent 调用/任务量** | **用量制，避开与代码理解工具直接比价** |

**定价启示（数析）**
- 直接层（代码理解）已被 Sourcegraph 以 $59/人/月锚定，CW 若按"代码理解"定价会被直接比价、难超存量主导者 → 不建议。
- 相邻层 Agent 工具普遍 $10–39/席，Devin 已验证"按用量（ACU）"模式成立 → CW 治理层可借鉴用量制，与代码理解工具不在同一比价维度。
- CW 差异化定价逻辑：以"多 Agent 协作治理（task/lease、evidence gate、verdict、audit chain）"为价值锚，按席位或按 Agent 调用/任务量计费，既避开 Sourcegraph 存量比价，又贴合 Agent 爆发带来的新付费意愿。

---

## 📚 数据来源 & 成员产出索引

- **竞析（竞品分析师）**：竞品全景三层分组、12 家逐家画像（8 字段）、12 维对比矩阵建议、5 条关键洞察。来源：Sourcegraph / CodeSee / ArchGuard / CodeQL / Bloop / Greptile / Lattix / Cursor / Claude Code / Devin / Codex CLI / Copilot / Cline / Aider / Semgrep / SonarQube / Tabnine / Codegen / Sourcery / Sweep / Grit 官网与评测（详见逐家画像来源 URL）。
- **数析（数据分析师）**：市场概览（Gartner / Grand View / TBRC / IIM / IDC 中国）、4 条关键趋势、融资收购动态、3 条定位启示。来源：agentmarketcap.ai、axis-intelligence.com、gradually.ai、valueaddvc.com、sacra.com、sourcegraph.com/blog、github.com/suatkocar/codegraph、latentview.com、trantorinc.com、superkind.ai 等。
- **瑞思（用户研究员）**：两类用户画像、痛点地图（6 项，含 Berkeley 1600+ 轨迹研究与约克大学 110 万 Reddit 帖子研究）、感知与期待、4 条产品启示。来源：Hacker News、Reddit、DEV Community、The Register、行业博客与开源项目评测（详见报告内 URL）。
- **方向明（主理人）**：按 SOP 工作流 2 汇编为竞品全景图 / 功能对比矩阵 / 市场数据 / 用户感知 / SWOT / 差异化机会 / 行动建议。

---

> 本报告由产品战略团队 AI 协作生成，重要决策请由产品负责人审定。
