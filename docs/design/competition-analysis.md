## Call Warden 竞争战略分析与行动计划

> 基于源码审计、差距分析报告、用户手册及 2026 年中 GitHub 竞品调研
>
> **更新（2026-07-19）**：原"未暴露"功能已全部接入 MCP（见 `docs/mcp_tools.md` 205 工具）。下文"只差接线"等表述为历史状态，保留作为审计轨迹。

---

### 一、你手里到底有什么（现状审计）

通读 `` 全部源码后，我发现一个关键事实：**你的差距分析报告里列的很多"P0/P1 缺失功能"，代码其实已经写好了，只是没有暴露到 MCP 和 CLI 中。**

| 功能 | 后端代码 | MCP 暴露 | CLI 暴露 | 真实差距 |
|------|----------|----------|----------|----------|
| 向量嵌入 / 语义搜索 | `db_vector.py` (496行) 完整实现 | ✅ 已暴露 | ✅ 已暴露 | **已完成**（2026-07 接入） |
| AI 摘要管理 | `db_summary.py` (245行) 完整实现 | ✅ 已暴露 | ✅ 已暴露 | **已完成**（2026-07 接入） |
| 所有权分析 | `db_ownership.py` (497行) 完整实现 | ✅ 已暴露 | ✅ 已暴露 | **已完成**（2026-07 接入） |
| 任务管理系统 | `db_tasks.py` (581行) 完整实现 | ✅ 已暴露 | ✅ 已暴露 | **已完成**（2026-07 接入） |
| 覆盖率导入 | `db_coverage.py` (550行) 完整实现 | ✅ 已暴露 | ✅ 已暴露 | **已完成**（2026-07 接入） |
| 代码度量 | `db_metrics.py` (795行) 完整实现 | 已暴露 | 已暴露 | **已完成** |
| Git 集成 | `db_git.py` (245行) 基本实现 | 已暴露 | 已暴露 | **符号级变更已导入**（`db_git.py` 已含 symbol-level history） |
| 跨仓库依赖 | `db_cross_repo.py` 已实现 | ✅ 已暴露 | ✅ 已暴露 | **已完成**（D7，原"不要做"建议已撤销） |

这意味着你的"补课成本"远比差距分析报告看起来要低得多。你不是从零开始追赶，而是**已经造好了引擎，只是还没装方向盘**。截至 2026-07，方向盘已全部装好。

---

### 二、竞争对手全景（2026 年中）

| 项目 | 星标 | 核心卖点 | 致命短板 |
|------|------|----------|----------|
| **Graphify** | ~55k | 多模态（代码+文档+图片），Leiden 社区检测 | 依赖 LLM 调用，JSON 存储不扩展 |
| **CodeGraph (pearai)** | ~42k | AI Agent 最佳搭档，省 47% token | 窄：只看代码，不看文档/历史 |
| **GitNexus** | ~37k | 最深图查询 + 影响分析 | **PolyForm 非商业许可**，企业不敢用 |
| **Understand-Anything** | ~20k | 可视化仪表盘，LLM 业务语义标注 | **没有 MCP**，AI Agent 用不了 |
| **Beads (Steve Yegge)** | ~22k | Agent 长期记忆，Git 式分支 | 不是代码图谱，是记忆系统 |
| **codebase-memory-mcp** | ~5k+ | 纯 C 性能怪兽，158 语言，Linux 内核 3 分钟 | 只读索引，无版本/历史/编辑 |
| **code-review-graph** | ~3k | PR 审查特化，爆炸半径分析 | 场景窄，非日常开发工具 |
| **Code-Graph-RAG** | 新兴 | 唯一把向量嵌入 + 图查询结合的 | 没有 MCP，没有编辑能力 |
| **Depwire** | 新兴 | 多 Agent 文件协调编辑 | 不做代码分析 |
| **Semgrep MCP** | 新兴 | 独立安全扫描，5000+ 规则 | 不与任何图谱集成 |

---

### 三、你的独占优势（竞品一个都没有的）

通读所有竞品的 README 和 Issues，以下功能**没有任何一个竞品同时具备**：

**1. 符号版本历史 + 注释恢复（护城河级）**

你通过 `file_symbol_versions` 表 + `content_hash` 去重 + `is_deleted` 标记，实现了函数级的完整版本链。配合 `restore_comment()` 可以从历史版本恢复被 `git checkout` 丢失的注释。这个功能在所有竞品中**完全空白**——codebase-memory-mcp 只做快照，Graphify 不追踪历史，CodeGraph 不关心注释。

**2. Semgrep 叠加在代码图谱上（安全核武器）**

你把 Semgrep 扫描结果入库并与符号关联。这意味着 AI Agent 修改代码前可以直接查"这个函数有没有安全漏洞"，改完后可以跑增量扫描。竞品中 Semgrep MCP 是独立的，不做图谱关联；code-review-graph 只做 PR 审查。没人做过"漏洞爆炸半径分析"——改了这个函数，哪些调用链上的函数可能受影响安全问题。

**3. 任务驱动 MCP（Agent 操作系统）**

`db_tasks.py` 实现了完整的 `task_create → task_next_step → task_report_step → task_rollback` 状态机。这正是你在配置自检码中反复讨论的"MCP 编排层"——让 AI Agent 必须领取任务、执行步骤、回报结果，不能跳步、不能乱改文件。这个理念与 Superpowers 6.0 的 SDD 工作流高度一致，但你做的是**通用版**（不绑定 Claude Code，任何支持 MCP 的 Agent 都能用）。

**4. AI Agent 健康检查**

`db_metrics.py` 中的 `get_code_health_check()` 和 `check_file_health()` 专门为 AI Agent 设计——它会警告"这个文件 3500 行，Token 溢出风险，建议先拆分再修改"。这种"Agent 安全"的设计理念在其他工具中完全没有。

---

### 四、接下来该做什么（三阶段行动计划）

#### 第一阶段：接线（1-2 周，投入最小收益最大）

你已经造好了引擎，现在只需要装方向盘。这一步的目标是**把所有已实现但未暴露的功能接入 MCP 和 CLI**。

**必做项（P0）：**

1. **向量搜索接入 MCP + CLI**
   - 在 `mcp_server.py` 中注册 `semantic_search` 和 `find_similar_functions` 工具
   - 在 `cli/main.py` 中添加 `--semantic-search` 和 `--similar` 参数
   - 预估工作量：2-3 小时

2. **任务系统接入 MCP**
   - 注册 `task_create`、`task_next_step`、`task_report_step`、`task_rollback`、`task_list`、`task_status` 共 6 个工具
   - 这是你的"Agent 操作系统"理念的核心载体
   - 预估工作量：3-4 小时

3. **所有权查询接入 MCP**
   - 注册 `who_to_ask`、`get_ownership_map`、`import_codeowners` 工具
   - 预估工作量：1-2 小时

4. **AI 摘要接入 MCP**
   - 注册 `generate_summary`、`get_summary`、`project_brief`、`repo_map` 工具
   - 预估工作量：2-3 小时

5. **覆盖率导入接入 MCP + CLI**
   - 注册 `import_lcov`、`import_cobertura`、`get_coverage_for_symbol`、`find_uncovered_functions`、`test_impact_selection` 工具
   - 预估工作量：2-3 小时

6. **修复已知 Bug**
   - `db_comment.py` 补充 `import os`
   - `issues.py` 修复 `self.ISSUE_RULES` 未定义
   - `db_git.py` 补充符号级变更导入逻辑

**第一阶段完成后你将拥有的 MCP 工具数：** 从 50 个增长到约 **65-70 个**，覆盖语义搜索、任务管理、所有权、摘要、覆盖率等全部维度。

---

#### 第二阶段：差异化（2-4 周，构建竞品无法快速复制的能力）

这一阶段的目标不是追赶竞品的"红海功能"，而是**深化你已有的蓝海优势**。

**1. 漏洞爆炸半径分析（Vulnerability Blast Radius）**

把 Semgrep 发现与调用链打通：当 Semgrep 在函数 A 中发现 SQL 注入漏洞时，自动计算"所有调用 A 的函数 → 所有调用这些函数的函数 → ..." 的传播链。

```
实现路径：
- 新增 MCP 工具 `get_vulnerability_blast_radius(symbol_name)`
- 内部逻辑：get_semgrep_findings → get_call_chain_up → 逐层标注风险等级
- 输出：受影响函数列表 + 风险等级 + 建议修复方案
```

这个功能在所有竞品中**完全不存在**。Semgrep MCP 只做独立扫描，不做传播分析；code-review-graph 做 PR 爆炸半径但不关联安全漏洞。

**2. Repo Map / 仓库地图增强**

你的 `db_summary.py` 已经有 `project_brief()` 和 `repo_map()` 方法，但输出比较简陋。增强方向：

- 加入模块热度图（按调用频率着色）
- 加入复杂度热点分布
- 加入所有权归属标注
- 输出为可嵌入的 Mermaid 或 HTML

**3. Token 节省账本**

参考 tokensave 和 codebase-memory-mcp 的做法，统计每次 MCP 查询实际返回的 token 数 vs 传统"读整个文件"的 token 数，生成节省报告。这既是宣传利器（"本项目为你节省了 X 万 token"），也是优化依据。

**4. 分支感知图谱**

当前是单分支快照。增强方向：
- 支持 `cw build --branch feature-x` 为不同分支构建独立图谱
- 支持 `cw diff-branch main feature-x` 对比两个分支的结构差异（新增/删除/修改的函数、调用链变化）

---

#### 第三阶段：护城河（1-3 个月，建立长期竞争壁垒）

**1. MCP 编排层（Agent OS）**

这是你在配置自检码中反复论证的核心理念。任务系统（`db_tasks.py`）只是第一步，完整的 MCP 编排层还需要：

- **安全文件编辑 MCP**：`propose_edit(task_id, file_path, operation, content, expected_hash)` → hash 校验 → dry-run → 临时分支验证 → apply → 审计日志
- **检查门禁**：Agent 回报后自动触发 Semgrep/ast-grep 检查，失败则下一步变成"修复问题"
- **结构化指令压缩**：`task_next_step` 返回的不是提示词，而是结构化 JSON 指令，Agent 无法自由发挥

这个编排层的价值在于：它不绑定任何特定 Agent（Claude Code / Cursor / Kimi Code / Gemini CLI 都能用），是一个**通用的 Agent 操作系统**。Superpowers 6.0 只服务于 Claude Code，你做的服务于所有 MCP Agent。

**2. RAG 管道（检索增强生成）**

你的向量嵌入已经实现（`db_vector.py`），但缺少 RAG 的"最后一公里"：

```
用户提问 → semantic_search 找到相关函数 → 自动拉取调用链上下文
→ 拼接为结构化 prompt → 返回给 AI Agent
```

新增 MCP 工具 `ask_codebase(question)` 实现这个管道。这让你的工具从"查询工具"升级为"问答工具"，直接对标 Sourcegraph Cody 和 GitHub Copilot 的检索增强能力。

**3. 性能优化（PyO3 热路径重写）**

Python 是当前的性能瓶颈。根据差距分析报告的结论，建议：

- 先用 profiling 找出 20% 热点函数（大概率是 tree-sitter 解析、调用图构建、向量计算）
- 用 PyO3 将热路径用 Rust 重写为 Python 扩展
- 保留 Python 的上层调度和 MCP 服务

预期收益：索引速度提升 10-50 倍，接近 codebase-memory-mcp 的性能水平。

---

### 五、什么不该做（避坑指南）

**不要去卷 RAG 红海。** 差距分析报告建议"不要跟 tokensave 卷 RAG"，这个判断基本正确，但需要修正：RAG 本身不是红海，"通用 RAG 框架"才是红海。你应该做的是**代码图谱增强的 RAG**——利用你的调用链、版本历史、Semgrep 结果作为 RAG 的上下文增强，这是 Sourcegraph Cody 和 GitHub Copilot 都做不到的。

**不要做可视化仪表盘。** Understand-Anything 在这个方向上已经很强（20k 星标），你追不上也没必要追。你的定位是 **AI Agent 的基础设施**，不是人类开发者的可视化工具。Mermaid/DOT 导出已经够用。

**不要做跨仓库（暂时）。** 这个功能需求真实存在（微服务架构），但实现复杂度极高（跨仓库符号解析、网络拓扑、部署依赖）。等单仓库做到极致后再考虑。

> **更新（2026-07-19）**：此建议已撤销。`db_cross_repo.py`（D7）已实现跨仓库依赖检测 + 共享符号 + 影响传播，MCP 已暴露。微服务架构场景的真实需求推动了实现。

**不要集成 ast-grep。** 与 Semgrep 高度重叠，增加依赖和维护成本，收益不成比例。

> **更新（2026-07-19）**：此建议仍有效。`analyzers/issues.py` 未集成 ast-grep，仅用 Semgrep + Guardrail findings 按符号聚合（避免重复造轮子）。

---

### 六、一句话总结

**你的 Call Warden 不是"落后于竞品"，而是"已经领先但还没亮剑"。** 向量搜索、任务管理、所有权分析、AI 摘要、覆盖率导入——这些竞品花大力气才做出来的功能，你的代码已经写好了，只是没接到 MCP 里。第一阶段花 1-2 周接线，MCP 工具数从 50 跳到 70，直接覆盖竞品的核心功能。第二阶段花 2-4 周深化漏洞爆炸半径和 Agent OS，建立竞品无法快速复制的蓝海壁垒。第三阶段用 PyO3 重写热路径，补齐性能短板。

你的终局定位不是"又一个代码图谱工具"，而是 **"AI Agent 的代码操作系统"**——它知道代码在哪、知道改了什么、知道改了会炸什么、还强制 Agent 按规矩办事。这个定位在整个竞品版图中**无人占据**。
