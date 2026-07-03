# MCP 工具参考

Call Warden 通过 MCP（Model Context Protocol）Server 暴露约 120 个工具，供 AI Agent 通过标准协议调用。本文档按功能分组列出全部工具、关键参数和返回值格式。

## MCP 协议简介

MCP（Model Context Protocol）是 Anthropic 推出的开放协议，用于 AI Agent 与外部工具/数据源通信。Call Warden 实现了 MCP Server，支持：

- **stdio 传输**：MCP client 启动并管理 Server 进程（默认模式）
- **SSE 传输**：远程访问，多客户端共享同一个 Server 实例

Server 通过 `FastMCP` 框架实现，所有工具以 Python 函数形式注册，自动生成 JSON Schema 供 client 调用。

启动 Server：

```bash
cw server                    # stdio 模式
cw server --transport sse    # SSE 模式
```

## 工具分类总览

| 分类 | 工具数 | 说明 |
|------|--------|------|
| 符号查询 | 12 | 搜索/定位/历史/拓扑 |
| 调用链分析 | 9 | 影响面/调用链/循环/热力图 |
| 安全护栏 | 4 | 规则扫描/编辑前检查/规则管理 |
| Semgrep 缺陷 | 4 | 扫描/统计/查询 |
| 安全编辑 | 4 | propose_edit/revert/history/stats |
| 任务管理 | 6 | create/next/report/rollback/list/status |
| 跨仓库分析 | 4 | 依赖检测/共享符号/影响/总览 |
| LSP 集成 | 6 | hover/定义/引用/诊断/补全/可用性 |
| 向量与语义搜索 | 4 | 语义搜索/嵌入/相似函数 |
| 注释恢复 | 3 | 单个/批量/版本查询 |
| Git 集成 | 4 | 历史/commit/变更/统计 |
| 代码度量 | 6 | 汇总/复杂度/耦合/最大函数 |
| 代码健康 | 2 | 健康检查/文件健康 |
| 演化智能 | 4 | 变更频率/缺陷关联/热点/流失 |
| 缺陷知识库 | 4 | 搜索/建议/学习/统计 |
| 分支感知 | 5 | 注册/列出/差异/切换/合并预览 |
| 覆盖率 | 4 | 导入/函数覆盖/未覆盖/测试影响 |
| 所有权 | 4 | 负责人/映射/CODEOWNERS/blame |
| 摘要与简报 | 4 | 生成/获取摘要/项目简报/仓库图 |
| 影响分析 | 4 | blast_radius/review/跨层/diff映射 |
| Token 账本 | 2 | 记录节省/获取报告 |
| RAG 问答 | 1 | ask_codebase |
| 检查门禁 | 2 | 运行门禁/标记解决 |
| 工作区管理 | 6 | 列出/注册/切换/删除/活动/构建目录 |
| 文件操作 | 3 | 移除/构建目录/符号内容 |

---

## 符号查询工具

### `get_stats`
获取代码图谱统计信息（文件数、函数数、调用关系数等）。
- **参数**：无
- **返回**：`dict` — 统计信息

### `search_symbols`
搜索符号（函数、类、结构体等）。
- **参数**：
  - `query: str` — 搜索关键词
  - `kind: str = ""` — 类型过滤
  - `limit: int = 20` — 返回数量
- **返回**：`list` — 符号列表

### `get_symbol`
获取符号详细信息。
- **参数**：`qualified_name: str`
- **返回**：`dict | None`

### `get_symbol_location`
获取符号位置（文件、行号、列号）。
- **参数**：`name: str`, `file_path: str = ""`
- **返回**：`dict | None`

### `get_file_symbols`
获取文件中的所有符号。
- **参数**：`file_path: str`
- **返回**：`list`

### `get_symbol_history`
获取符号的版本历史。
- **参数**：`qualified_name: str`
- **返回**：`list`

### `get_file_history`
获取文件的版本历史。
- **参数**：`file_path: str`
- **返回**：`list`

### `get_recent_changes`
获取近期变更的文件和符号。
- **参数**：`since: str = "1d"` — 时间范围（1h/1d/1w/2024-01-01）
- **返回**：`dict`

### `get_topological_order`
获取按依赖拓扑排序的符号列表。
- **参数**：`limit: int = 50`
- **返回**：`list`

### `get_symbol_content_by_hash`
根据内容哈希获取符号完整内容。
- **参数**：`content_hash: str`
- **返回**：`dict | None` — 含完整代码

### `get_status`
获取代码图谱完整状态概览。
- **参数**：无
- **返回**：`dict`

### `remove_file`
从图谱中移除文件（标记删除，保留历史）。
- **参数**：`file_path: str`
- **返回**：`bool`

---

## 调用链分析工具

### `get_callers`
查询指定函数的所有调用者。
- **参数**：`callee_name: str`
- **返回**：`list`

### `get_callees`
查询指定函数调用了哪些函数。
- **参数**：`caller_name: str`
- **返回**：`list`

### `get_impact`
影响面分析：向上追踪所有调用该函数的上游函数。
- **参数**：`qualified_name: str`, `max_depth: int = 10`
- **返回**：`dict` — `{start, total_upstream, max_depth_reached, levels: [...]}`

### `get_call_chain_down`
调用链向下：追踪该函数调用的所有下游函数。
- **参数**：`qualified_name: str`, `max_depth: int = 10`
- **返回**：`dict`

### `get_top_callers`
获取被调用次数最多的函数排行。
- **参数**：`limit: int = 20`, `kind: str = "fn"`, `module_filter: str = ""`
- **返回**：`list`

### `get_orphan_symbols`
获取未被调用的孤立符号。
- **参数**：`kind: str = "fn"`, `module_filter: str = ""`, `limit: int = 100`
- **返回**：`list`

### `get_deepest_functions`
获取调用深度最深的函数排行。
- **参数**：`limit: int = 20`, `module_filter: str = ""`, `kind: str = "fn"`
- **返回**：`list`

### `get_module_call_stats`
获取模块间调用统计。
- **参数**：`limit: int = 30`
- **返回**：`list`

### `detect_cycles`
检测循环调用。
- **参数**：`max_depth: int = 10`
- **返回**：`list` — 每个循环是一个函数名列表

### `get_call_heatmap`
获取函数调用频率热力图。
- **参数**：`group_by: str = "module"`, `top_n: int = 20`
- **返回**：`list`

### `get_test_coverage`
获取测试覆盖率统计（test 函数分布）。
- **参数**：无
- **返回**：`dict`

---

## 安全护栏工具（GuardrailMixin）

### `guardrail_scan`
对指定文件运行安全规则扫描。
- **参数**：`file_filter: str = ""` — 文件路径前缀过滤
- **返回**：`list` — findings，含 finding_id/category/severity/file_path

### `guardrail_check_edit`
编辑前阻断式检查（Before-Edit Contract 核心）。
- **参数**：`file_path: str`, `proposed_change: str = ""`
- **返回**：`dict` — `{"decision": "block"/"warn"/"pass", "findings": [...], "message": "..."}`

### `guardrail_list_rules`
列出已注册的安全规则。
- **参数**：`category_filter: str = ""` — db/api/incident
- **返回**：`list`

### `guardrail_add_rule`
添加自定义安全规则。
- **参数**：`category: str`, `pattern: str`, `severity: str = "warn"`, `action: str = "warn"`, `description: str = ""`
- **返回**：`dict`

---

## Semgrep 缺陷工具

### `run_semgrep_scan`
运行 Semgrep 扫描并将结果存入数据库。
- **参数**：`config: str = "p/default"`, `languages: list = None`, `timeout: int = 300`
- **返回**：`dict`

### `get_semgrep_stats`
获取 Semgrep 缺陷统计。
- **参数**：无
- **返回**：`dict` — 按严重程度/语言/规则分组

### `get_semgrep_findings`
查询 Semgrep 发现的缺陷。
- **参数**：`severity: str = ""`, `language: str = ""`, `rule_id: str = ""`, `limit: int = 50`
- **返回**：`list`

### `find_issues`
查找代码缺陷（缺注释、硬编码、unwrap 等）。
- **参数**：`issue_type: str = ""`, `limit: int = 30`
- **返回**：`list`

---

## 安全编辑工具（EditSafetyMixin — Agent OS 核心）

### `propose_edit`
提交安全编辑请求。执行流程：SHA-256 校验 → 生成 diff 摘要 → 写审计表 → 原子写入文件 → 更新审计状态。

- **参数**：
  - `file_path: str` — 文件路径（相对 workspace_root 或绝对）
  - `new_content: str` — 编辑后的完整内容
  - `operation: str = "edit"` — edit/create/delete
  - `agent_task_id: str = ""` — 关联任务 ID
  - `symbol_hash: str = ""` — 关联符号 hash
  - `dry_run: bool = False` — 仅预览不写入
- **返回**：
  ```json
  {
    "audit_id": 42,
    "file_path": "src/main.rs",
    "file_hash_before": "a1b2...",
    "file_hash_after": "d4e5...",
    "diff_summary": "+5 行 / -2 行",
    "status": "applied",
    "success": true
  }
  ```

### `revert_edit`
回滚编辑（标记审计状态为 reverted）。
- **参数**：`audit_id: int`
- **返回**：`dict` — `{audit_id, status, message, file_path, file_hash_before}`
- **注意**：审计表不存储完整文件内容，实际内容回滚需依赖 git checkout

### `get_edit_history`
查询文件编辑历史。
- **参数**：`file_path: str = ""`, `limit: int = 20`
- **返回**：`list` — 审计记录列表

### `get_edit_stats`
获取文件编辑统计。
- **参数**：`time_window: str = "30d"`
- **返回**：`dict` — `{total, by_status, by_operation, revert_rate}`

---

## 任务管理工具（TaskMixin）

### `task_create`
创建任务并返回 task_id。
- **参数**：`title: str`, `description: str = ""`, `steps: list = None`, `creator: str = "agent"`
- **返回**：`str` — task_id
- **steps 元素结构**：`{action, target_file, target_symbol, check_items}`

### `task_next_step`
领取任务的下一个待执行步骤。Agent 必须通过此工具领取步骤，不能自由决定下一步。
- **参数**：`task_id: str`
- **返回**：`dict | None` — 步骤详情，含 guardrail_alert/guardrail_warning/structured_instruction

**Before-Edit Contract**：当步骤为编辑类操作时，系统自动调用护栏检查：
- 返回 `guardrail_alert`（block）：步骤状态为 blocked，需先调用 `task_resolve_block`
- 返回 `guardrail_warning`（warn）：可执行，但需关注告警

### `task_resolve_block`
处理 blocked 步骤的护栏告警，恢复为 pending。
- **参数**：`task_id: str`, `step_id: str`, `resolution: str = "ack"` — ack/override/fix_applied
- **返回**：`dict | None`

### `task_report_step`
回报步骤执行结果。失败时自动插入"修复缺陷"步骤。
- **参数**：`task_id: str`, `step_id: str`, `result: str = ""`, `success: bool = True`, `changes: list = None`
- **返回**：`dict | None` — 下一步骤信息

### `task_rollback`
回滚任务中的变更。
- **参数**：`task_id: str`, `change_id: str = None`, `reason: str = ""`
- **返回**：`dict`

### `task_list`
列出任务。
- **参数**：`status_filter: str = None`, `limit: int = 20`
- **返回**：`list`

### `task_status`
获取任务详情和所有步骤。
- **参数**：`task_id: str`
- **返回**：`dict | None`

---

## 跨仓库分析工具（CrossRepoMixin）

### `detect_cross_repo_deps`
检测跨仓库依赖关系（扫描 import 语句匹配目标仓库符号）。
- **参数**：`source_workspace: str`, `target_workspace: str = ""`
- **返回**：`dict` — `{source_workspace, detected_deps: [...], total_deps}`

### `find_shared_symbols`
查找跨仓库共享符号（相同 content_hash 的函数）。
- **参数**：`workspace_a: str = ""`, `workspace_b: str = ""`
- **返回**：`dict` — `{total_shared, shared_symbols: [...]}`

### `cross_repo_impact`
跨仓库影响分析（联合 cross_repo_deps + blast_radius）。
- **参数**：`symbol_hash: str`, `depth: int = 2`
- **返回**：`dict` — `{source_symbol, source_workspace, local_impacted_count, impacted_repos: [...], risk_level}`

### `cross_repo_summary`
跨仓库分析总览。
- **参数**：无
- **返回**：`dict` — `{total_repos, repos: [...], total_cross_deps, total_shared_symbols, deps_by_type}`

---

## LSP 集成工具（LspMixin）

### `lsp_check_available`
检查 LSP 服务器是否可用（调用其他 lsp_* 工具前应先调用）。
- **参数**：`language: str = ""` — python/typescript/go/rust，为空检查所有
- **返回**：`dict` — `{available_servers: {...}, total_available: N}`

### `lsp_hover`
获取符号的 hover 信息（类型签名、文档注释）。
- **参数**：`file_path: str`, `line: int`（0-based）, `character: int`（0-based）
- **返回**：`dict` — `{file_path, line, character, contents, available}`

### `lsp_definition`
跳转到定义（跨文件跳转）。
- **参数**：`file_path: str`, `line: int`, `character: int`
- **返回**：`dict` — `{definitions: [{uri, file_path, line, character}], available}`

### `lsp_references`
查找符号的所有引用（比 calls 表更准确）。
- **参数**：`file_path: str`, `line: int`, `character: int`, `include_declaration: bool = True`
- **返回**：`dict` — `{references: [...], total, available}`

### `lsp_diagnostics`
获取文件诊断信息（编译错误、类型错误、lint 警告）。
- **参数**：`file_path: str`
- **返回**：`dict` — `{file_path, diagnostics: [...], total, available}`

### `lsp_completion`
获取代码补全建议。
- **参数**：`file_path: str`, `line: int`, `character: int`
- **返回**：`dict` — `{completions: [{label, kind, detail}], total, available}`

---

## 向量与语义搜索工具（VectorMixin）

### `semantic_search`
语义搜索：用自然语言查找相关函数。
- **参数**：`query: str`, `top_k: int = 5`
- **返回**：`list` — `{qualified_name, file_path, similarity, summary}`
- **降级**：向量索引不可用时自动回退到关键词匹配

### `embed_symbols`
为所有函数生成向量嵌入（首次使用前需执行）。
- **参数**：`force: bool = False`
- **返回**：`dict` — `{total, success, skipped, failed}`

### `embed_single_symbol`
为单个函数生成向量嵌入。
- **参数**：`symbol_hash: str`
- **返回**：`dict` — `{success, symbol_hash, message}`

### `find_similar_functions`
查找与指定函数语义相似的其他函数。
- **参数**：`qualified_name: str`, `threshold: float = 0.8`, `top_k: int = 20`
- **返回**：`list`

---

## 注释恢复工具（CommentMixin）

### `get_comment_from_version`
从历史版本中获取注释。
- **参数**：`spec: str` — 格式：`文件路径:符号名@版本号` 或 `文件路径:行号`
- **返回**：`dict | None`

### `restore_comment`
恢复函数注释（从历史版本）。
- **参数**：`spec: str`, `preview: bool = True`
- **返回**：`dict` — `{success, qualified_name, file_path, old_comment, new_comment, ...}`

### `restore_all_comments`
批量恢复所有有注释历史的函数注释。
- **参数**：`preview: bool = True`, `file_filter: str = ""`
- **返回**：`dict` — `{total_found, restored, skipped, failed, files, errors}`

---

## Git 集成工具（GitMixin）

### `import_git_history`
导入 Git 历史记录到数据库。
- **参数**：`max_commits: int = 100`
- **返回**：`dict` — `{success, commits_imported, total_commits}`

### `get_git_commits`
获取 Git commit 列表。
- **参数**：`limit: int = 20`, `offset: int = 0`
- **返回**：`list`

### `get_commit_changes`
获取指定 commit 的变更详情。
- **参数**：`commit_hash: str`
- **返回**：`dict` — `{commit, file_changes}`

### `get_git_stats`
获取 Git 集成统计信息。
- **参数**：无
- **返回**：`dict` — `{commit_count, file_change_count, change_types}`

### `get_symbol_commit_history`
获取符号的 Git 变更历史（查 git_symbol_changes 表）。
- **参数**：`symbol_hash: str`, `limit: int = 20`
- **返回**：`list` — `{commit_hash, timestamp, author, message, change_type}`

---

## 代码度量工具（MetricsMixin）

### `get_code_metrics_summary`
获取代码度量汇总统计。
- **参数**：无
- **返回**：`dict` — `{file_count, function_count, total_lines, total_calls, avg_complexity, ...}`

### `get_complexity_hotspots`
获取圈复杂度最高的函数。
- **参数**：`limit: int = 20`, `module_filter: str = ""`
- **返回**：`list`

### `get_coupling_analysis`
获取模块耦合度分析。
- **参数**：`limit: int = 30`
- **返回**：`list` — `{module, afferent, efferent, total_coupling, instability}`

### `get_function_metrics`
获取单个函数的度量详情。
- **参数**：`qualified_name: str`
- **返回**：`dict | None`

### `get_largest_functions`
获取代码行数最多的函数。
- **参数**：`limit: int = 20`, `module_filter: str = ""`
- **返回**：`list`

### `get_most_coupled_functions`
获取耦合度最高的函数（扇入+扇出最大）。
- **参数**：`limit: int = 20`
- **返回**：`list`

---

## 代码健康检查工具

### `get_code_health_check`
代码健康检查：识别大文件、复杂函数、高耦合模块。

> ⚠️ AI Agent 修改代码前强烈建议先调用此工具！

- **参数**：`severity: str = "all"` — all/high/medium/low
- **返回**：`dict` — 含评分、问题分类列表和 Agent 指导

### `check_file_health`
检查单个文件的健康状态（Agent 修改文件前必调用）。

> ⚠️ 在读取或修改任何文件之前，先调用此工具！

- **参数**：`file_path: str`
- **返回**：`dict | None` — 含 should_split_first 标志

---

## 演化智能工具（EvolutionMixin）

### `evolution_frequency`
函数变更频率分析。
- **参数**：`qualified_name: str`, `time_window: str = ""` — 30d/90d/1y
- **返回**：`dict`

### `defect_correlation`
缺陷关联分析。
- **参数**：`symbol_hash: str`, `window_commits: int = 5`
- **返回**：`dict`

### `hotspot_evolution`
热点函数演化。
- **参数**：`module_filter: str = ""`
- **返回**：`list`

### `churn_analysis`
代码流失分析。
- **参数**：`module_filter: str = ""`, `time_window: str = "90d"`
- **返回**：`dict`

---

## 缺陷知识库工具（DefectKbMixin）

### `defect_search`
缺陷模式搜索。
- **参数**：`category: str = ""`, `severity_filter: str = ""`
- **返回**：`list`

### `defect_suggest_fix`
修复建议。
- **参数**：`symbol_hash: str`, `finding_id: int = 0`
- **返回**：`dict`

### `defect_learn`
从修复中学习。
- **参数**：`fix_commit_hash: str`
- **返回**：`dict`

### `defect_stats`
缺陷知识库统计。
- **参数**：无
- **返回**：`dict`

---

## 分支感知工具（BranchMixin）

### `register_branch`
注册分支工作区。
- **参数**：`branch_name: str`, `repo_root: str = ""`
- **返回**：`dict` — `{workspace_id, branch_name, is_new}`

### `list_branches`
列出所有分支工作区。
- **参数**：无
- **返回**：`list`

### `diff_branches`
比较两个分支的符号差异。
- **参数**：`source_branch: str`, `target_branch: str`
- **返回**：`dict` — `{added, removed, modified, unchanged_count}`

### `switch_branch`
切换活动工作区到指定分支。
- **参数**：`branch_name: str`
- **返回**：`dict` — `{branch_name, workspace_id, symbol_count}`

### `merge_preview`
合并预览：分析 source 分支变更对 target 分支的影响。
- **参数**：`source_branch: str`, `target_branch: str`
- **返回**：`dict` — `{affected_symbols, impact_layers, risk_level}`

---

## 影响分析工具（ImpactMixin）

### `blast_radius`
计算变更影响半径。
- **参数**：`symbol_hash: str`, `depth: int = 3`
- **返回**：`dict` — 影响半径报告

### `review_readiness`
审查就绪报告。
- **参数**：`symbol_hash: str`
- **返回**：`dict`

### `cross_layer_impact`
跨层影响分析（代码/API/DB/UI）。
- **参数**：`symbol_hash: str`
- **返回**：`dict`

### `diff_to_symbol`
将 git diff 映射到受影响符号。
- **参数**：`diff_text: str`
- **返回**：`list`

### `get_vulnerability_blast_radius`
计算漏洞的爆炸半径（Semgrep findings × 调用链反向传播）。
- **参数**：`finding_id: int = 0`, `severity_filter: str = ""`, `depth: int = 3`
- **返回**：`dict` — `{risk_level, total_findings, total_impacted_symbols, findings: [...]}`

---

## 覆盖率工具（CoverageMixin）

### `import_coverage`
导入覆盖率报告。
- **参数**：`file_path: str`, `format: str = "lcov"` — lcov/cobertura
- **返回**：`dict`

### `get_coverage_for_symbol`
获取函数的代码覆盖率。
- **参数**：`qualified_name: str`
- **返回**：`dict | None`

### `find_uncovered_functions`
查找覆盖率低于阈值的函数。
- **参数**：`module_filter: str = ""`, `threshold: int = 50`
- **返回**：`list`

### `test_impact_selection`
测试影响选择：改了某函数后需要运行哪些测试。
- **参数**：`qualified_name: str`
- **返回**：`list`

---

## 所有权工具（OwnershipMixin）

### `who_to_ask`
查询文件负责人（综合 CODEOWNERS + git blame）。
- **参数**：`file_path: str`
- **返回**：`dict | None` — `{owner, source, confidence, last_commit_author, ...}`

### `get_ownership_map`
获取模块所有权映射。
- **参数**：`module_filter: str = ""`
- **返回**：`list`

### `parse_codeowners`
解析 CODEOWNERS 文件（不写入数据库）。
- **参数**：`file_path: str = ""`
- **返回**：`list`

### `import_codeowners`
从 CODEOWNERS 文件导入所有权到数据库。
- **参数**：无
- **返回**：`dict`

### `import_git_blame`
从 git log 导入每个文件最近一次提交者信息。
- **参数**：无
- **返回**：`dict`

---

## 摘要与简报工具（SummaryMixin）

### `generate_summary`
为函数生成/保存摘要。
- **参数**：`qualified_name: str`, `summary: str`, `model: str = "manual"`
- **返回**：`dict`

### `get_summary`
获取函数的当前摘要。
- **参数**：`qualified_name: str`
- **返回**：`dict | None`

### `project_brief`
获取项目简报（适合 Agent 首次接触项目时调用）。
- **参数**：无
- **返回**：`dict` — `{project_type, file_count, function_count, health_score, modules, hot_functions}`

### `repo_map`
生成仓库模块依赖图。
- **参数**：`format: str = "text"` — text/mermaid
- **返回**：`str`

---

## Token 账本工具（TokenSavingsMixin）

### `record_token_savings`
记录一次操作的 token 节省。
- **参数**：`operation: str`, `original_tokens: int`, `actual_tokens: int`, `agent_task_id: str = ""`, `detail: str = ""`
- **返回**：`dict` — `{id, tokens_saved, savings_pct}`

### `get_token_savings_report`
获取 Token 节省报告。
- **参数**：`time_window: str = "30d"` — 7d/30d/90d/""（全部）
- **返回**：`dict` — `{total_saved, total_operations, avg_savings_pct, by_operation, daily_trend, headline}`

---

## RAG 问答工具

### `ask_codebase`
RAG 管道：基于调用链增强的代码库问答上下文组装。组装完整 RAG 上下文（种子函数 + 调用方 + 被调用方 + 摘要）。
- **参数**：`question: str`, `top_k: int = 5`, `include_callers: int = 2`, `include_callees: int = 1`, `max_tokens: int = 4000`
- **返回**：`dict` — `{rag_context, context_blocks, estimated_tokens}`

---

## 检查门禁工具（CheckGateMixin）

### `run_check_gate`
手动触发检查门禁（F6）。对变更文件运行语法检查 + Semgrep 扫描。
- **参数**：`task_id: str`, `step_id: str`, `changed_files: list`
- **返回**：`dict` — `{passed, checks_run, findings, fix_required, summary}`

### `resolve_gate_findings`
标记任务的门禁发现为已解决。
- **参数**：`task_id: str`
- **返回**：`dict` — `{resolved_count, task_id}`

---

## 工作区管理工具

### `list_workspaces`
列出所有工作区。
- **返回**：`list`

### `register_workspace`
注册新工作区。
- **参数**：`name: str`, `root_path: str`, `description: str = ""`
- **返回**：`int` — workspace_id

### `set_active_workspace`
设置活动工作区。
- **参数**：`workspace_id_or_name: str`
- **返回**：`bool`

### `delete_workspace`
删除工作区（级联删除所有实例和版本）。
- **参数**：`workspace_id_or_name: str`
- **返回**：`bool`

### `get_active_workspace`
获取当前活动工作区信息。
- **返回**：`dict | None`

### `build_directory`
构建指定目录的代码图谱。
- **参数**：`dir_path: str`
- **返回**：`dict`

### `build_graph`
完整构建代码知识图谱（全量扫描）。
- **返回**：`bool`

### `refresh_file`
刷新单个文件（增量更新）。
- **参数**：`file_path: str`
- **返回**：`bool`

### `get_comment_coverage`
获取注释覆盖率统计。
- **参数**：`group_by: str = "module"` — module/file/kind
- **返回**：`dict`

### `get_uncommented_symbols`
获取未注释的符号列表。
- **参数**：`kind: str = "fn"`, `module_filter: str = ""`, `limit: int = 100`
- **返回**：`list`

### `export_module_graph`
导出模块依赖图。
- **参数**：`format: str = "mermaid"` — mermaid/dot
- **返回**：`str`

---

## MCP Server 配置方法

### stdio 模式（推荐，默认）

适用于 MCP client 直接启动并管理 Server 进程。

**Claude Desktop 配置**（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "code-graph": {
      "command": "python",
      "args": ["-m", "code_graph.server"],
      "env": {
        "CODE_GRAPH_WORKSPACE": "/path/to/your/project"
      }
    }
  }
}
```

**Trae IDE 配置**：

```json
{
  "mcpServers": {
    "code-graph": {
      "command": "python",
      "args": ["-m", "code_graph.server"],
      "cwd": "/path/to/code_graph/parent",
      "env": {
        "CODE_GRAPH_WORKSPACE": "/path/to/your/project"
      }
    }
  }
}
```

### SSE 模式

适用于远程访问或多客户端共享。

```bash
# 启动 SSE Server
cw server --transport sse
```

Client 配置指向 `http://localhost:<port>/sse`（默认端口由 FastMCP 决定）。

### 环境变量

| 变量 | 说明 |
|------|------|
| `CODE_GRAPH_WORKSPACE` | 默认工作区根路径 |
| `CODE_GRAPH_DB_PATH` | 自定义数据库路径（覆盖默认 hash 路径） |

### 多容器共享部署

```bash
# 1. 宿主机安装
pip install tree-sitter tree-sitter-languages fastmcp

# 2. 数据库放在 $HOME/.code_graph/（所有容器共享 $HOME）
# 3. 每个容器配置 MCP client 指向同一数据库路径
```

> SQLite 支持多读者单写者，写入会自动排队，多进程安全。

详细部署见 [部署指南](deployment.md)。

## 下一步

- [CLI 命令参考](cli_reference.md)：CLI 等价命令
- [架构设计](architecture.md)：Mixin 架构与扩展指南
- [快速开始](quickstart.md)：完整示例会话
