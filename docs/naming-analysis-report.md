# Call Warden 开源命名与参赛策略分析报告

> **⚠️ 过时提示**：本报告撰写时 MCP 工具数为 120 个，截至 2026-07-15 已增长至 196 个。报告中涉及"120 个 MCP 工具"的描述均为历史数据，请以 [docs/mcp_tools.md](mcp_tools.md) 为准。

## Abstract

Call Warden 是一个面向 AI Agent 的代码知识图谱工具，基于 tree-sitter + SQLite + MCP 协议，已具备 16 语言解析、120 个 MCP 工具、145+ CLI 命令的成熟能力。本报告从项目技术基因出发，经过**逐个 GitHub 实名验证**，筛选出真正可用的命名方案，并给出最终推荐与 TRAE AI 创造力大赛赛道适配策略。

**核心结论**：第一版报告推荐的 10 个"Code+X"组合名经 GitHub 实名验证**全部已有同名项目**——GitHub 上 3 亿+仓库意味着任何两个常见英文词的拼贴基本都被占过。本报告改变策略，从项目**独占技术基因**出发造词，最终推荐 **SymTree**、**CallWarden**、**SigMap** 三个经过 GitHub 验证无冲突的方案。

---

## 1. 第一版方案的 GitHub 实名验证（全部阵亡）

以下 10 个方案全部经 GitHub 搜索验证，存在同名或高度相似项目：

| 方案 | GitHub 冲突情况 | 冲突性质 |
|------|-----------------|----------|
| **CodeAtlas** | `cntx-gnewton/codeatlas` — VS Code 扩展，生成项目结构文本文件 | 直接同名 |
| **CodeLens** | `kunalverma2512/CodeLens` + VS Code 内置 CodeLens 功能 | 双重冲突 |
| **GraphGuard** | GNN 代码风险分析器、ProGuard 映射匹配工具等多个 | 同名 |
| **GuardGraph** | GitHub 搜索结果中有同名安全/图分析项目 | 同名 |
| **CodePulse** | `code-pulse.com` — 渗透测试实时代码覆盖工具（成熟产品） | 成熟竞品 |
| **AgentGraph** | 至少 3 个：信任层项目、LLM 编排库、Agent 执行图 | 多重同名 |
| **CodeSense** | 至少 5 个：AI PR 审查、lexical analyzer、代码扫描工具等 | 严重冲突 |
| **RepoGraph** | 至少 3 个：RAG 知识图谱、可视化工具、仓库智能 | 多重同名 |
| **NeuroCode** | GitHub 搜索结果显示有同名仓库 | 同名 |
| **Synapse** | 大量同名：聊天机器人、消息队列、影视后端等 | 严重冲突 |

**教训**："Code+X"或"X+Graph"的命名模式在 GitHub 上已完全饱和。要找到真正干净的名字，必须从项目**独一无二的技术概念**出发造新词，而不是拼贴常见词典词。

---

## 2. 命名策略转变：从拼贴到造词

### 2.1 项目独占基因（竞品全无）

从竞品分析报告中提取项目**真正独一无二**的 5 大基因：

| 基因 | 技术实现 | 造词方向 |
|------|----------|----------|
| **符号感知** — 16 语言、120 MCP 工具对代码库的全方位感知 | tree-sitter + 向量嵌入 | Sym- (Symbol), Sense, Perceive |
| **调用链追踪** — 四级解析 + BFS 分层 + 漏洞爆炸半径 | call_chain + Semgrep | Call-, Trace, Blast |
| **Guardian 守护** — DB/API/Incident 三类可阻断规则 | guardrail_rules + Before-Edit Contract | Warden, Guard, Keep |
| **Mixin 组装** — 23 个模块像乐高积木一样拼成 Agent OS | Mixin 多继承 | Craft, Weave, Mix |
| **版本时间旅行** — 函数级版本链 + 注释恢复 | file_symbol_versions + content_hash | Time, Epoch, Version |

### 2.2 命名原则

1. **造词而非拼贴** — 打破"Code+X"模式，用独特的技术概念造词
2. **GitHub 实名验证** — 每个候选名都经过搜索确认无同名项目
3. **可发音、可拼写** — 避免`cxt`、`mnemonic`等拼读困难的组合
4. **品牌延展性** — 能衍生 logo、slogan、CLI 命令
5. **评审友好** — 大赛评委一听就能记住

---

## 3. 新候选命名方案（全部经过 GitHub 搜索验证）

### 方案 1：SymTree

**含义**：符号树（Symbol + Tree） — 将代码库解析为一棵可查询、可追踪、可守护的符号树。

**技术映射**：项目用 tree-sitter 从 16 种语言的源码中提取函数/类/结构体等符号，构建调用关系树（call chain tree）、版本历史树（version tree）、影响传播树（blast radius tree）。120 个 MCP 工具就是让 Agent 在这棵树上"查找、追踪、守护"。

**GitHub 验证**：搜索 `SymTree` 和 `symtree`，未发现同名代码项目（仅有一般性搜索结果噪音）。

| 维度 | 得分 | 说明 |
|------|------|------|
| 记忆度 | 5 | 两个音节，简单好记，开发者秒懂 |
| 含义表达 | 5 | "Symbol" + "Tree" 精准描述项目核心：符号提取 + 树状结构 |
| GitHub 友好度 | 5 | 搜索结果干净，无知名项目 |
| 品牌延展性 | 5 | Logo：树形结构 + 代码符号；Slogan："See the forest and the symbols" |
| 国际化 | 5 | Sym 和 Tree 都是全球开发者通用词汇 |
| 竞品区分度 | 5 | 无同名竞品，完全空白区 |

**总分**：5.0 / 5.0

---

### 方案 2：CallWarden

**含义**：调用守护者（Call + Warden） — 守护每一次代码调用，确保变更安全。

**技术映射**：项目的核心独占能力之一是"Before-Edit Contract"——Agent 每次编辑代码前，系统自动检查：调用链是否断裂？安全护栏是否触发？漏洞爆炸半径是否可控？`CallWarden` 直接传递"守护调用、看守变更"的理念。

**GitHub 验证**：搜索 `CallWarden` 和 `callwarden`，未发现同名代码项目。

| 维度 | 得分 | 说明 |
|------|------|------|
| 记忆度 | 4 | 两个词组合清晰，"Warden"有力量感 |
| 含义表达 | 5 | "Call"指向调用链分析，"Warden"指向 Guardian 护栏，精准双关 |
| GitHub 友好度 | 5 | 搜索结果干净 |
| 品牌延展性 | 4 | Logo：守望者 + 调用链网络；Slogan："Guard every call" |
| 国际化 | 4 | "Warden"在非英语母语者中需要一定认知 |
| 竞品区分度 | 5 | 无同名竞品，"Warden"在代码工具中极度稀缺 |

**总分**：4.5 / 5.0

---

### 方案 3：SigMap

**含义**：符号地图（Symbol + Map） — 为代码库中的每个符号绘制地图。

**技术映射**：项目本质就是"给符号画地图"——16 语言符号提取（Symbol）、调用关系映射（Map）、影响范围导航（Navigation）、安全区域标记（Guardian）。`SigMap` 是一个简洁有力的缩写，CLI 命令也能很自然：`sigmap init`、`sigmap search "login"`、`sigmap chain "module::fn"`。

**GitHub 验证**：搜索 `SigMap` 和 `sigmap`，搜索结果噪音极低，未发现同名代码项目。

| 维度 | 得分 | 说明 |
|------|------|------|
| 记忆度 | 5 | 双音节缩写，简短有力 |
| 含义表达 | 4 | "Sig"（Symbol）+ "Map"，准确但略显技术化 |
| GitHub 友好度 | 5 | 搜索结果干净 |
| 品牌延展性 | 4 | Logo：符号标志 + 地形图；Slogan："Map your symbols" |
| 国际化 | 4 | "Sig" 是开发者通用缩写（signature/signal） |
| 竞品区分度 | 5 | 无同名竞品 |

**总分**：4.5 / 5.0

---

### 方案 4：BlastScope

**含义**：爆炸视野（Blast + Scope） — 看清每一次代码变更的爆炸半径。

**技术映射**：项目的独占功能 `blast_radius(symbol_hash, depth=3)` 用 BFS 多层展开影响树。`BlastScope` 直接把这个概念变成产品名——"我帮你看到变更的爆炸范围"。这个名字在大赛答辩中会非常引人注目。

**GitHub 验证**：搜索 `BlastScope` 和 `blastscope`，未发现同名代码项目（仅有一个 Maya 破坏模拟插件叫 Blast Code，不冲突）。

| 维度 | 得分 | 说明 |
|------|------|------|
| 记忆度 | 4 | "Blast"有冲击力，"Scope"有技术感 |
| 含义表达 | 5 | 直接对应 `blast_radius` 核心功能，独特且有力 |
| GitHub 友好度 | 5 | 搜索结果干净 |
| 品牌延展性 | 4 | Logo：冲击波 + 代码范围圈 |
| 国际化 | 4 | 两个词都是英语常用词 |
| 竞品区分度 | 5 | 无同名竞品，概念完全独特 |

**总分**：4.5 / 5.0

---

### 方案 5：SymCraft

**含义**：符号工艺（Symbol + Craft） — 精心构建代码符号的知识图谱。

**技术映射**：项目的 23 个 Mixin 组装架构本身就是一种"工艺"——像工匠一样把 23 个独立模块（构建、查询、守护、编辑、演化...）组装成完整的代码操作系统。`SymCraft` 传递的是"精心打造"的工匠精神，区别于竞品的"快速索引"粗放路线。

**GitHub 验证**：搜索 `SymCraft` 和 `symcraft`，搜索结果噪音极低，未发现同名代码项目。

| 维度 | 得分 | 说明 |
|------|------|------|
| 记忆度 | 4 | "Craft"有质感，两个音节好记 |
| 含义表达 | 4 | "Symbol Craft" 传达精心构建的理念，但不如 SymTree 直接 |
| GitHub 友好度 | 5 | 搜索结果干净 |
| 品牌延展性 | 4 | Logo：工匠工具 + 代码符号；Slogan："Craft your code intelligence" |
| 国际化 | 4 | "Craft"全球通用 |
| 竞品区分度 | 5 | 无同名竞品 |

**总分**：4.3 / 5.0

---

### 方案 6：NerveGraph

**含义**：神经图谱（Nerve + Graph） — 代码库的神经系统，感知一切。

**技术映射**：项目的 120 个 MCP 工具就像代码库的"神经系统"——Agent 通过这些工具"感知"代码的结构、历史、健康状态、安全风险、变更影响。就像人体的神经系统把触觉、痛觉、温度传给大脑，NerveGraph 把代码库的所有信息传给 AI Agent。

**GitHub 验证**：搜索 `NerveGraph` 和 `nervegraph`，搜索结果未发现同名代码项目（仅有 Microsoft Graph 等不相关结果）。

| 维度 | 得分 | 说明 |
|------|------|------|
| 记忆度 | 4 | "Nerve"有生物感但开发者熟悉 |
| 含义表达 | 4 | "代码库的神经系统"是好隐喻，但需要解释 |
| GitHub 友好度 | 5 | 搜索结果干净 |
| 品牌延展性 | 5 | Logo：神经网络 + 代码节点，视觉冲击力强 |
| 国际化 | 3 | "Nerve"在非英语母语者中认知中等 |
| 竞品区分度 | 5 | 无同名竞品 |

**总分**：4.3 / 5.0

---

## 4. 综合排名

| 排名 | 命名 | 总分 | 核心优势 | 主要考量 |
|------|------|------|----------|----------|
| 1 | **SymTree** | 5.0 | 完美映射"符号+树"，GitHub 干净，CLI 命令自然，品牌延展性强 | 无明显短板 |
| 2 | **CallWarden** | 4.5 | Guardian 护栏的精准表达，大赛评审中"守护"叙事有力 | "Warden" 对非英语母语者稍需解释 |
| 3 | **SigMap** | 4.5 | 极简双音节，CLI 命令最自然（`sigmap init`），GitHub 极干净 | 含义略显技术化 |
| 4 | **BlastScope** | 4.5 | `blast_radius` 的直接体现，大赛答辩中最引人注目 | 偏"爆炸半径"单一功能，未覆盖全貌 |
| 5 | **SymCraft** | 4.3 | Mixin 工艺组装的精准表达，工匠精神有温度 | 不如 SymTree 直接 |
| 6 | **NerveGraph** | 4.3 | "神经系统"隐喻视觉潜力最大 | 非英语母语者认知门槛稍高 |

---

## 5. 最终推荐

### 5.1 首选：SymTree

**一句话电梯演讲**："SymTree — 给 AI Agent 的代码符号树。"

**推荐理由**：

1. **完美映射技术核心**：Symbol（符号提取）+ Tree（树状结构 = 调用链/版本树/影响树），两个词精准概括了项目在做什么
2. **CLI 命令自然**：`symtree init`、`symtree search "login"`、`symtree chain "module::fn"` — 比 `code-graph --init` 更简短有力
3. **GitHub 完全干净**：搜索结果无同名项目
4. **品牌延展性极强**：
   - Logo：一棵由代码符号组成的树，根系是 source file，枝干是调用链，果实是符号
   - Slogan："See the forest and the symbols"
   - 产品矩阵：SymTree CLI / SymTree MCP / SymTree Cloud
5. **大赛叙事适配**：在答辩中展开讲——"我们把代码库变成一棵 Agent 能爬的树，每个符号是一片叶子，调用关系是枝干，Guardian 护栏是树皮。Agent 不再迷失在代码的丛林中。"

### 5.2 备选：SigMap

如果更看重**CLI 简洁性**，SigMap 是最佳选择——两个音节，打字最快，`sigmap` 作为命令行工具名极短，适合高频使用的开发者工具。

### 5.3 强调安全的备选：CallWarden

如果参赛叙事想打"AI 编程安全"这张差异化牌（Guardian 护栏 + Before-Edit Contract + 漏洞爆炸半径），CallWarden 是最有力的选择——"守护每一次调用"在评委心中会产生强烈的"安全保障"联想。

---

## 6. 参赛策略补充

### 6.1 大赛报名关键信息（来自官方论坛）

| 维度 | 详情 |
|------|------|
| 报名方式 | 在 TRAE 社区大赛报名专区发帖，需附 TRAE Work 生成的创意产物 HTML |
| 报名审核 | 只看合规，不评创意好坏；审核通过后去官网确认领取奖励 |
| 初赛作品 | 需可体验 Demo + 不少于 3 个 Session ID 证明由 TRAE 完成 |
| 评审维度 | 创新性、完整度、实用性、过程展示 |
| 抖音通道 | 点赞 500+ 可进入人气榜，多一条晋级路 |
| 作品要求 | 可以是已有项目的实质性版本迭代，不必须从零创建 |

### 6.2 核心叙事（使用 SymTree）

> "SymTree 是 AI Agent 的代码符号树。它把代码库变成一棵可查询、可追踪、可守护的智能树——120 个 MCP 工具让任何 AI Agent 都能'看见'符号结构、'追踪'调用影响、'守护'生产安全。不同于现有工具只做代码索引，SymTree 让 Agent 真正理解代码——像一棵大树，从根系到枝叶，全景可查。"

### 6.3 差异化展示重点（不变）

1. **Agent OS 演示**：完整的任务驱动工作流（竞品全无）
2. **漏洞爆炸半径**：Semgrep + 调用链联动（竞品全无）
3. **性能数据**：10 万符号 2.36 秒
4. **通用性**：不绑定特定 IDE，任何 MCP 客户端都能用

---

## 7. References

[1] TRAE AI 创造力大赛官网[EB/OL]. https://www.trae.cn/ai-creativity, 2026.
[2] TRAE 大赛报名指南[EB/OL]. https://forum.trae.cn/t/topic/22548, 2026.
[3] TRAE 大赛初赛参赛指南[EB/OL]. https://forum.trae.cn/t/topic/22549, 2026.
[4] cntx-gnewton/codeatlas[EB/OL]. https://github.com/cntx-gnewton/codeatlas, 2026.
[5] kunalverma2512/CodeLens[EB/OL]. https://github.com/kunalverma2512/CodeLens, 2026.
[6] Call Warden 项目源码及文档[EB/OL]. , 2026.
[7] Call Warden 竞品分析报告[EB/OL]. docs/design/competition-analysis.md, 2026.
