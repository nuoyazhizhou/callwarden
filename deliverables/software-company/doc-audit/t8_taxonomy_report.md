# T8 CLI 命令 / MCP 工具分类梳理报告

> 日期：2026-10-07
> 方法：从权威 inventory（audit_cli_inventory.json 70 top/234 叶子命令 + audit_mcp_inventory.json
> 243 工具/12 模块）自动分析分类覆盖、CLI↔MCP 覆盖对比、命名/参数易混淆（脚本
> t8_tool_taxonomy_analysis.py，可复现），再人工研判。
> 配套思维导图见 artifact。

## 1. 规模与分类全景

- **CLI**：70 个顶层命令，234 个叶子命令（含子命令）
- **MCP**：243 个工具，12 个模块

MCP 模块分布：tools_task 55 / tools_security 36 / tools_query 32 / tools_summary 31 /
tools_workspace 27 / tools_semantic 19 / tools_p2_graph 10 / tools_collab 8 /
tools_p4_lease 8 / tools_rules 9 / tools_p3_identity 7 / tools_task_prompt 1。

CLI 按功能域聚为 13 组（详见思维导图）。分类覆盖整体合理，**无结构性空缺**。

## 2. 分类覆盖问题

### 2.1 唯一未归类命令：`bootstrap`（轻微）
`bootstrap`（仅 `bootstrap status`）未落入任何功能域。它是 self-bootstrap 运行时门禁的状态查询，
语义上属"GC 与运维"。**建议**：文档/help 分组时归入运维域；非功能缺陷。

### 2.2 CLI↔MCP 覆盖对比（134 两侧都有 / 109 仅 MCP / 138 仅 CLI）
绝大多数差异是**合理的架构分工**，不是遗漏或重复：

**仅 CLI（138）—— 合理，不应强行加 MCP 工具**：
- 安装/配置类：`install` / `install-agent`（24 个 IDE 子命令）/ `install-hook` / `setup` /
  `doctor` / `server` —— 这些是人机安装运维，不该暴露给 Agent 调用。
- daemon 运维类：`daemon ping/health/backup/restore/mount/bridge/...`（22 子命令）—— 运维面，
  Agent 不需要。
- 实验/治理人工入口：`experiment *`（13）/ `collab *` / `task` 的治理子命令
  （contract-bootstrap/attest-legacy-workspace-binding 等）—— 人工治理操作。

**仅 MCP（109）—— 合理，CLI 用参数聚合替代独立命令**：
- 细粒度查询：`get_deepest_functions` / `get_largest_functions` / `get_most_coupled_functions`
  等在 CLI 侧由 `largest-fns` / `coupled-fns` 等聚合命令覆盖（名字不同但能力对应）。
- LSP 面：`lsp_hover/definition/references/diagnostics/completion/check_available` —— Agent 专用，
  CLI 无对应（合理，CLI 用户用 IDE 自带 LSP）。
- 编辑提案：`propose_edit` / `propose_range_patch` / `revert_edit` / `restore_comment` —— Agent
  安全编辑面，CLI 侧由 `edit`/`review` 相关命令或直接编辑覆盖。
- 导入面：`import_git_history` / `import_coverage` / `import_codeowners` 等在 CLI 由
  `git import` / `coverage import` 覆盖（kebab 子命令形式）。

**结论**：覆盖对比未发现"本应双向暴露却缺失"的真遗漏，也未发现"同一能力重复实现两个工具"
的真重复。CLI 偏运维/人工治理，MCP 偏 Agent 细粒度查询/编辑，分工清晰。

## 3. 命名易混淆（真实风险，建议治理）

### 3.1 高风险：detect_cycle vs detect_cycles（MCP，仅差 1 字母 's'）
- `detect_cycles`（tools_query，参数 `max_depth`）：检测**函数调用环**
- `detect_cycle`（tools_p2_graph，参数 `workspace_id`）：检测**硬依赖图环**（Req 9.7）

两者语义不同（调用图 vs 依赖图）、参数不同，但名字几乎一致。**LLM 和用户极易选错**
（T5 真实 LLM 测试已标记过此歧义）。**建议改名**消歧，例如：
- `detect_cycles` → `detect_call_cycles`（函数调用环）
- `detect_cycle` → `detect_dependency_cycle`（依赖图环）

### 3.2 中风险：stats vs status 系列（语义不同，名字近）
| 工具/命令 | 语义 | 易混对象 |
|-----------|------|---------|
| `get_stats` | 图谱统计（文件/函数/调用数） | `get_status`（图谱完整状态概览） |
| `get_job_stats` | 任务统计总览（无参） | `get_job_status`（单任务状态，需 job_id） |
| CLI `stats` | 图谱统计 | CLI `status`（图谱状态） |

stats（统计数据）与 status（状态）英文相近、中文都常说"状态"，是经典易混。**建议**：
在 help/描述中强对比措辞（"统计数值" vs "状态概览"），或长期考虑把 stats 类统一为
`*_statistics` / status 类统一为 `*_state`。短期优先改文档描述。

### 3.3 低风险：对称命名（可接受，语义自解释）
以下成对出现、语义对称，混淆风险低，**无需改**：
- `get_callers` / `get_callees`（调用者 / 被调用者）
- `diff_callers` / `diff_callees`
- `gc_policy_get` / `gc_policy_set`
- `get_active_workspace` / `set_active_workspace`
- CLI `callers` / `callees`

### 3.4 低风险：CLI 短命令形近（可接受）
`test`/`tests`、`graph`/`grep`、`file`/`rule`、`task`/`test`、`rule`/`rules` 距离近但语义清晰，
且 `test`（内部自测入口，help_rc=1）与 `tests`（符号测试用例查询）确实不同。**建议**：仅在
help 顶部对 `test` vs `tests` 加一行区分说明；其余可接受。

## 4. 参数易混淆（低风险，建议文档消歧）

跨命令的 flag 近似对（编辑距离 ≤2），均属不同命令、不会在同一命令内冲突：

| flag 对 | 所属命令 | 风险 |
|---------|---------|------|
| `--secret` / `--socket` | audit / daemon | 低（不同命令） |
| `--table` / `--title` | audit / rule,task | 低 |
| `--all` / `--fail` / `--full` | install,refresh / task / dashboard | 低 |
| `--top` / `--type` | dashboard / function-issues | 低 |
| `--file` / `--fixed` | guardrail / grep | 低 |

这些不在同一命令内，argparse 不会歧义。**建议**：无需改动；若未来同一命令同时需要类似 flag，
注意避免（如 task 的 `--fail` 与潜在 `--file`）。

## 5. 改进建议优先级

| 优先级 | 项 | 建议 |
|--------|----|----|
| **P1** | detect_cycle / detect_cycles 歧义 | 改名消歧（detect_call_cycles / detect_dependency_cycle），或至少在描述首句强对比 |
| P2 | stats / status 系列 | help/工具描述强对比措辞；长期统一命名后缀 |
| P3 | bootstrap 未归类 | 文档分组归入运维域 |
| P4 | test vs tests | help 顶部加一行区分 |
| — | 参数 flag 近似 / 对称命名 | 可接受，无需改动 |

## 6. 结论

- **分类覆盖**：13 个 CLI 功能域 + 12 个 MCP 模块，结构完整，仅 `bootstrap` 一个轻微未归类。
- **遗漏**：无真遗漏——CLI↔MCP 的 109/138 差异是合理的人机/Agent 分工。
- **重复**：无真重复——所有形近工具经核实均语义不同（含 detect_cycle vs detect_cycles）。
- **易混淆**：1 个高风险（detect_cycle/detect_cycles）、1 组中风险（stats/status 系列）值得治理，
  其余为语义自解释的对称命名或跨命令 flag，可接受。

本报告为分析/建议，未改动任何代码。改名类建议（P1/P2）涉及对外工具契约，需单独评估兼容性
（可能需保留旧名 alias 一段时间）后再实施。
