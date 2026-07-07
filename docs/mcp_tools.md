# MCP 工具参考

Call Warden 通过 MCP（Model Context Protocol）Server 暴露 160+ 个工具，供 AI Agent 通过标准协议调用。本文档按功能分组列出全部工具、关键参数和返回值格式。

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
| 安全编辑 | 6 | propose_edit/range_patch/symbol_patch/revert/history/stats |
| 任务管理 | 20 | create/next/work_next_job/report/rollback/list/status/subtask/split/tree/create_from_plan/plan_template + task-symbol attribution + completion_review/quality_findings/resolve_quality_finding + capture_diff（自举闭环入口） |
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
| 外部依赖与 GC | 6 | 直接依赖读取/导入/外部符号瘦身/冷热 retention/policy |
| 摘要与简报 | 4 | 生成/获取摘要/项目简报/仓库图 |
| 影响分析 | 4 | blast_radius/review/跨层/diff映射 |
| Token 账本 | 2 | 记录节省/获取报告 |
| RAG 问答 | 1 | ask_codebase |
| 检查门禁 | 2 | 运行门禁/标记解决 |
| 工作区管理 | 6 | 列出/注册/切换/删除/活动/构建目录 |
| 文件操作 | 3 | 移除/构建目录/符号内容 |
| Agent Rule Memory | 10 | 候选规则 CRUD/审核、规则列表、上下文匹配、AGENTS.md 同步、标记块插入、自动提取、内置规则种子化 |
| 自举闭环 | 1 | bootstrap_status（自举健康摘要） |

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
  - `expected_hash: str = ""` — 编辑前期望文件 hash（并发保护）
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

### `propose_range_patch`
提交行号范围补丁，避免 Agent 读写整个大文件。
- **参数**：
  - `file_path: str`
  - `start_line: int` — 1-based 起始行
  - `end_line: int` — 1-based 结束行（闭区间）
  - `replacement: str` — 替换内容
  - `agent_task_id: str = ""`
  - `symbol_hash: str = ""`
  - `dry_run: bool = False`
  - `expected_hash: str = ""`
- **返回**：`dict` — 含审计信息、diff_summary、patch_scope、refreshed

### `propose_symbol_patch`
提交符号级补丁，由图谱定位函数/类范围后局部改写。
- **参数**：
  - `file_path: str`
  - `symbol_name: str`
  - `patch: str`
  - `mode: str = "replace"` — `replace` / `insert_before` / `insert_after`
  - `agent_task_id: str = ""`
  - `dry_run: bool = False`
  - `expected_hash: str = ""`
- **返回**：`dict` — 含审计信息、符号范围、刷新结果

### `propose_symbol_id_patch`
提交基于 `symbols.id` 的精确符号补丁。适合从 `work_next_job.allowed_edit_scope.symbol_id` 直接进入修改，避免重名符号歧义。
- **参数**：
  - `symbol_id: int` — 当前快照中的符号 ID
  - `patch: str`
  - `mode: str = "replace"` — `replace` / `insert_before` / `insert_after`
  - `agent_task_id: str = ""`
  - `dry_run: bool = False`
  - `expected_hash: str = ""` — 编辑前期望文件 hash
  - `expected_symbol_hash: str = ""` — 编辑前期望符号 hash
- **返回**：`dict` — 含审计信息、`patch_scope`、`guardrail` 决策和刷新结果；Before-Edit Contract 为 `block` 时拒绝写入

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

### `work_next_job`
领取下一项 Agent 工作，并返回完成它所需的最小上下文。推荐 Agent 优先使用它，而不是自行组合 `file_read` / `grep` / `task_next_step`。
- **参数**：`task_id: str`
- **返回**：`dict | None` — job 详情，包含：
  - `job_type` / `target_file` / `target_symbol`
  - `context.target_source` / `context.callers` / `context.callees` / `context.file_health`
  - `allowed_edit_scope`
  - `recommended_tools`
  - `report_with`

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

### `task_apply`
审核通过任务（review → applied）。由其他会话的 LLM 调用，写代码的 Agent 不能自己 apply。

**级联 close 机制**（T-1783309017863-a1b6）：apply 后查询所有兄弟子任务状态，若全部
`applied`/`closed` 则原子级联 close：
1. close 所有 applied 兄弟任务
2. 父任务 review → applied → closed 一次性推进
3. 递归向上检查祖父层级联

- **参数**：`task_id: str`, `reviewer: str = "reviewer"`
- **返回**：`dict` — `{task_id, status, applied_at, reviewer, cascaded_close?}`
  - `cascaded_close: List[str]` — 仅触发级联时存在，列出所有自动 close 的 task_id
- **拒绝场景**：
  - 父任务（有子任务）手动 apply：`{error, reason: "parent_task_must_cascade", subtask_count}`
  - 状态不是 review：`{error, reason: "invalid_status"}`

### `task_close`
关闭任务（applied → closed）。由其他会话的 LLM 调用。

**父任务禁止手动 close**：若任务有子任务，返回错误，提示由级联触发。

- **参数**：`task_id: str`, `reviewer: str = "reviewer"`
- **返回**：`dict` — `{task_id, status, closed_at, reviewer}`
- **拒绝场景**：
  - 父任务（有子任务）手动 close：`{error, reason: "parent_task_must_cascade", subtask_count}`
  - 状态不是 applied：`{error, reason: "invalid_status"}`

### `task_list`
列出任务。
- **参数**：`status_filter: str = None`, `limit: int = 20`
- **返回**：`list`

### `task_status`
获取任务详情和所有步骤。
- **参数**：`task_id: str`
- **返回**：`dict | None`

### `task_completion_review`
运行任务完成质量审查，触发任务质量门禁。

自动清理该 step 旧的 `check_gate` 发现（避免重复累积），调用 `run_check_gate`
（语法 + Semgrep）并叠加 5 个扩展检查器：
- `_check_scope_violations`：变更文件超出 `target_file` 范围 → error
- `_check_symbol_attribution`：`target_symbol` 非空但无 `task_symbol_changes` → warn
- `_check_file_health_findings`：文件过大（≥1000/2000 行）/复杂度热点（≥20）→ warn/error
- `_check_i18n_hardcoded`：硬编码 `print` / `cprint` / `logger.*` 输出 → warn
- `_check_signature_mismatch`：签名变更后调用方未解析 → block/info

根据 open 状态发现的严重度给出决策：
- `pass`：无发现（允许 step 进入 done）
- `warn`：仅有 info/warn（允许完成但记录）
- `block`：存在 error/block（阻塞完成，需修复后重审）

**Agent 应在 `task_report_step` 之前或之后调用此工具主动复查。**

- **参数**：`task_id: str`, `step_id: str = ""` — 步骤 ID（任务级审查留空）
- **返回**：`dict` — `{decision, findings, summary, counts, check_gate_result}`
  - `decision ∈ {"pass", "warn", "block"}`
  - `counts`: `{info, warn, error, block}` 各严重度的 open 发现数
  - `findings`: open 发现列表（含已转换的 check_gate 发现 + 扩展检查器发现）

### `task_quality_findings`
查询任务质量门禁发现。返回 `task_quality_findings` 表中匹配过滤条件的记录，
按 `created_at` 升序排列（旧的先处理）。

- **参数**：
  - `task_id: str` — 任务 ID（必填）
  - `status: str = "open"` — 状态过滤（open/resolved/wontfix/all）
  - `severity: str = ""` — 严重度过滤（info/warn/error/block），默认不过滤
- **返回**：`list[dict]` — finding 列表，每项含
  `id` / `task_id` / `step_id` / `finding_type` / `severity` / `status` /
  `message` / `evidence` / `source` / `created_at` / `resolved_at` / `resolved_by`

### `task_resolve_quality_finding`
解决或豁免单条任务质量门禁发现。将 finding 状态从 `open` 推进到 `resolved`
或 `wontfix`，记录解决者和解决时间。

`error` / `block` 级别的发现被解决后，该 step 的阻塞状态才会解除，
再次调用 `task_completion_review` 会重新评估决策。

- **参数**：
  - `finding_id: int` — finding ID
  - `resolution: str = "fixed"` — 解决方式
    - `fixed`：已修复（status → resolved）
    - `wontfix`：暂不修复，接受风险（status → wontfix）
    - `false_positive`：误报（status → wontfix）
  - `resolved_by: str = "agent"` — 解决者标识（agent/human/system）
- **返回**：`dict` —
  - 成功：`{success: True, finding_id, status, resolution, resolved_at}`
  - 失败：`{success: False, error: ...}`

> **阻塞语义**：`error` / `block` 级别的 open 发现会让 step 无法进入 done。
> 必须先 `task_resolve_quality_finding` 标记为 resolved/wontfix，
> 再 `task_completion_review` 复查决策。

### `task_capture_diff`
捕获外部 Agent 真实文件改动到 task / change / symbol / audit 闭环。
这是自举闭环（bootstrap closure）的核心入口。

当外部 Agent（非 Call Warden MCP）在文件系统中留下改动后，调用此工具
把这些变更归因到指定 task/step，并触发质量审查：
1. 检测自最近一次 `workspace_scan_runs.git_head` 以来的文件变更
2. dry-run 模式只返回计划，apply 模式写入事实表
3. apply 模式：写 `workspace_scan_runs`（status=running → completed）
   + 每文件 `change_audit`（hash_before/hash_after）+ `audit_chain` 签名
   + 关联 `task_symbol_changes` + 触发 `run_task_completion_review`
4. 根据 `quality_decision` 决定 `next_action`

- **参数**：
  - `task_id: str` — 关联任务 ID（必填）
  - `step_id: str = ""` — 关联步骤 ID（可选，默认空）
  - `base: str = ""` — 基准 commit（空串自动取最近 scan baseline 的 git_head）
  - `dry_run: bool = True` — True 只返回计划不写库；False 写入事实表
- **返回**：`dict` —
  - `task_id: str` / `step_id: str` / `dry_run: bool`
  - `scan_id: int` — apply 模式才有，对应的 `workspace_scan_runs` ID
  - `changed_files: list[str]` — 变更文件路径列表
  - `linked_symbols: list[dict]` — 关联的符号变更列表
  - `quality_findings: list[dict]` — 触发的质量发现
  - `quality_decision: str` — `pass` / `warn` / `block` / `""`（dry-run 时为空）
  - `next_action: str` — `review` / `fix` / `commit` / `noop` / `""`

> **与 `task_report_step` 的关系**：`task_report_step` 是 Agent 声明完成 step；
> `task_capture_diff` 是从磁盘真实变更反向同步到任务上下文，二者配合构成
> "声明 + 验证"闭环。建议流程：`task_next_step` → 外部编辑 →
> `task_capture_diff` → `task_report_step`。

### `audit_chain_verify`
验证审计签名链的完整性与一致性。校验 `audit_chain` 表中每条记录的
`record_signature` 是否匹配重新计算的签名，以及 `prev_signature` 是否
正确链向上一条记录。可发现直接改库导致的审计记录篡改。

- **参数**：
  - `table_name: str = ""` — 指定表名时只验证该表的链；为空时验证全部
  - `limit: int = 1000` — 最多验证的记录数
- **返回**：`dict` —
  - `table_name: str` — 验证的表名（空串表示全部）
  - `total_count: int` — 验证的记录总数
  - `verified_count: int` — 通过验证的记录数
  - `broken_count: int` — 不通过的记录数
  - `broken_records: list` — 不通过的记录列表，每项含 `id / table_name / record_id / reasons`
  - `security_level: str` — 当前签名安全级别（`hash_only` 或 `hmac`）

**签名算法**：
- 无 HMAC key 时：`SHA-256(prev_signature + "|" + payload_hash)`，
  `signing_key_id='local'`
- 有 HMAC key 时：`HMAC-SHA256(key, prev_signature + "|" + payload_hash)`，
  `signing_key_id='hmac'`

**HMAC key 来源**（优先级从高到低）：
1. 环境变量 `CALLWARDEN_AUDIT_HMAC_KEY`
2. 文件 `~/.callwarden/audit.key`
3. 回落到 SHA-256 链

**reasons 含义**：
- `signature_mismatch`：`record_signature` 与重新计算的签名不匹配
- `chain_broken`：`prev_signature` 与上一条的 `record_signature` 不匹配
- `first_prev_not_empty`：首条记录 `prev_signature` 应为空串但非空

### `rotate_audit_signing_key`（C7）
轮换审计签名密钥。轮换后新记录用新密钥签名（`signing_key_id = key_id`），旧记录保持原签名不变，`audit_chain_verify` 按 `signing_key_id` 从 `audit_key_rotations` 表查找对应密钥验证。
- **参数**：
  - `key_id: str` — 新密钥标识（唯一，如 `key-2026-07`）
  - `key_secret: str = ""` — 新密钥内容；为空时自动生成 32 字节随机密钥（hex 编码，64 字符）
- **返回**：`dict` —
  - `success: bool`
  - `key_id: str` — 新密钥标识
  - `rotated_at: float` — 轮换时间戳
  - `previous_key_id: str` — 前一个 active 密钥的 `key_id`（无则为空串）
  - 失败时：`{"success": False, "error": str}`

**幂等性**：相同 `key_id` 再次轮换会更新 `key_secret` 并保持 `is_active=1`。

> **写操作**：会 INSERT/UPDATE `audit_key_rotations` 表。对应 CLI `cw audit rotate-key`。

### `list_audit_signing_keys`（C7）
列出所有签名密钥轮换记录，按 `rotated_at` 倒序。**不返回 `key_secret`** 以避免泄露密钥内容。
- **参数**：无
- **返回**：`list` —
  - 每项含 `key_id: str` / `rotated_at: float` / `is_active: int`
  - 失败时：`[{"error": str}]`

> **只读**：仅查询 `audit_key_rotations` 表。对应 CLI `cw audit keys`。

### `audit_chain` 签名密钥轮换机制（C7）

**Schema v29** 新增 `audit_key_rotations` 表，记录每次密钥轮换（`key_id` / `key_secret` / `rotated_at` / `is_active`）。

**密钥查找优先级**（`_get_active_signing_key`）：
1. `audit_key_rotations` 表中 `is_active=1` 的记录
2. 环境变量 `CALLWARDEN_AUDIT_HMAC_KEY` / 文件 `~/.callwarden/audit.key`
3. 回落到 SHA-256 链（`signing_key_id='local'`）

**验证时密钥查找**（`_lookup_signing_key`）：
1. `audit_key_rotations` 表中 `key_id` 对应的 `key_secret`
2. `key_id == "hmac"`：回落到当前环境变量/文件密钥（向后兼容）
3. `key_id == "local"`：返回 `None`（SHA-256 链）
4. 未知 `key_id`：返回 `None`（无法验证，标记为 `signature_mismatch`）

**向后兼容**：
- legacy `signing_key_id="hmac"` 记录（无轮换表时签发）仍能用当前环境变量/文件密钥验证
- legacy `signing_key_id="local"` 记录（SHA-256 链）无需密钥即可验证

### `task_create_subtask`
在父任务下创建子任务。任务过大时拆分子任务，子任务完成后系统自动推进父任务状态，避免 Agent 遗漏任务或遗忘上下文。
- **参数**：`parent_task_id: str`, `title: str`, `description: str = ""`, `steps: list = None`, `creator: str = "agent"`
- **返回**：`str` — 新建子任务的 task_id

### `task_split`
将大任务拆分为多个子任务。原任务的自身步骤保留为汇总/验证步骤，具体工作由子任务完成。`task_next_step` 会自动深度优先下钻到最底层子任务执行。
- **参数**：`task_id: str`, `subtasks: list` — subtasks 元素含 title/description/steps
- **返回**：`list` — 新建子任务的 ID 列表

### `task_status_tree`
获取任务树详情（含子任务树和进度）。返回完整的任务树结构，包括每层的进度百分比、子任务列表、自身步骤状态。
- **参数**：`task_id: str` — 根任务 ID
- **返回**：`dict | None` — 含 progress、steps、subtasks 递归结构

### `task_create_from_plan`
从 Markdown 任务计划自动创建父子任务树。Agent 只需传入任务标题和 Markdown 格式计划，系统自动解析标题层级和列表项，生成完整任务树并入库。
- **参数**：
  - `title: str` — 根任务标题
  - `plan_md: str` — Markdown 格式的任务计划（见格式说明）
  - `description: str = ""` — 根任务补充描述（可选）
- **返回**：`str` — 根任务 ID
- **Markdown 格式**：
  - `# 一级标题` = 根任务描述
  - `## 二级标题` = 子任务标题
  - `### 三级标题` = 步骤分组
  - `- [ ] 列表项` / `- 列表项` = 任务步骤
- **示例**：
  ```
  # 性能优化专项
  ## 1. 数据库查询优化
  - 慢 SQL 分析
  - 添加索引
  ## 2. 解析器优化
  - 大文件符号提取
  - 增量解析支持
  ```

### `task_plan_template`
获取 `task_create_from_plan` 的标准格式模板。Agent 在创建任务前先获取模板，按模板格式填写确保解析正确。
- **参数**：无
- **返回**：`str` — Markdown 格式的模板字符串（含格式说明）
- **使用流程**：
  1. `task_plan_template()` → 获取模板
  2. 按模板填写任务计划
  3. `task_create_from_plan(title, plan_md)` → 自动创建任务树

### `record_task_symbol_change`
记录一次任务/步骤到文件或符号版本变化的归因。用于把“为什么变”连接到“哪个 symbol 变了”。

- **参数**：
  - `task_id: str`
  - `file_path: str`
  - `step_id: str = ""`
  - `edit_audit_id: int = 0`
  - `change_audit_id: str = ""`
  - `qualified_name: str = ""`
  - `symbol_name: str = ""`
  - `symbol_hash_before: str = ""`
  - `symbol_hash_after: str = ""`
  - `change_type: str = "modified"` — added/modified/deleted/edit 等
  - `source: str = "manual"`
  - `metadata: dict = None`
- **返回**：`dict` — `{success, id}`

### `link_edit_audit_symbols`
在图谱刷新后，把某次 `propose_edit` / `propose_range_patch` / `propose_symbol_patch` 产生的 `edit_audit_id` 映射到具体符号 before/after hash。

- **参数**：`audit_id: int`, `step_id: str = ""`
- **前置条件**：编辑前后的文件版本都已进入 `file_versions`；通常需要先运行 `cw --refresh-all` 或完整构建。
- **返回**：`dict` — `{success, audit_id, linked, changes}`

### `get_task_symbol_changes`
查询某个任务/步骤实际归因到的文件/符号变化。

- **参数**：`task_id: str`, `step_id: str = ""`, `file_path: str = ""`, `limit: int = 100`
- **返回**：`list[dict]` — `task_symbol_changes` 记录列表

### `get_symbol_change_tasks`
反查某个符号版本或限定名由哪些任务改变过。

- **参数**：`symbol_hash: str = ""`, `qualified_name: str = ""`, `limit: int = 50`
- **返回**：`list[dict]` — 相关归因记录

---

## 文件操作工具

Agent 通过 MCP 读取代码，完全替代 IDE 内置 Read/Grep/Glob 工具。所有工具都有工作区安全边界检查。

### `file_read`
读取文件内容，支持行号偏移和行数限制。
- **参数**：`file_path: str`, `offset: int = 0`, `limit: int = 200`
- **返回**：`dict | None` — `{path, total_lines, offset, limit, content}`

### `file_grep`
在工作区内搜索文件内容（ripgrep 风格），支持正则表达式和 glob 过滤。
- **参数**：`pattern: str`, `path: str = ""`, `glob: str = ""`, `output_mode: str = "files_with_matches"`, `head_limit: int = 50`
- **返回**：`dict` — `{results, count, truncated}`
- **output_mode**：`files_with_matches`（文件名列表）/ `content`（含行号内容）/ `count`（每文件匹配数）

### `file_list`
列出目录下的文件和子目录。
- **参数**：`path: str = ""`, `glob: str = ""`
- **返回**：`list` — 元素 `{name, path, type}`（type 为 dir 或 file）

### `file_symbol_content`
读取文件中指定符号的源码内容。结合数据库中的符号位置信息，精确读取函数/类/方法的源码，比 file_read 更高效。
- **参数**：`file_path: str`, `symbol_name: str`
- **返回**：`dict | None` — `{symbol_name, symbol_type, qualified_name, file, start_line, end_line, content}`

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

## 外部依赖与 GC 工具

### `get_project_dependencies`
读取当前项目 manifest 中的直接依赖，不展开传递依赖。
- **参数**：`languages: list | None = None`
- **返回**：`dict` — `{language: {package_name: version}}`

### `import_project_dependencies`
导入项目直接依赖的第一层外部符号，并更新包的 `last_seen_at`。
- **参数**：无
- **返回**：`dict` — `{created}`

### `prune_external_symbols`
手动瘦身外部符号索引。适合确认只保留当前项目直接依赖时使用，不属于默认 GC。
- **参数**：`keep_project_deps: bool = True`, `package_names: list | None = None`, `vacuum: bool = False`
- **返回**：`dict` — `{before, after, deleted, vacuum}`

### `gc_retention`
按冷热策略清理旧文件版本和可选外部包；默认只预演，执行前默认压缩备份完整 SQLite 数据库。
- **参数**：`older_than_days: int | None = None`, `keep_versions: int | None = None`, `include_external: bool | None = None`, `external_stale_days: int | None = None`, `dry_run: bool = True`, `backup: bool | None = None`, `vacuum: bool | None = None`, `save_policy: bool = False`
- **返回**：`dict` — `{audit_id, dry_run, policy, saved_policy, backup_path, backup_size, candidate_file_versions, candidate_external_packages, deleted_*, vacuum, estimate}`
- **说明**：未传策略参数时读取数据库中的 GC policy；传入参数只覆盖本次运行，除非 `save_policy=True`。
- **`estimate`（v20 新增）**：Top N 收益预估，dry-run 与 apply 都返回。结构：
  ```json
  {
    "approximate_deleted_rows": {
      "file_versions": 12,
      "file_symbol_versions": 47,
      "call_versions": 31,
      "symbol_contents": 0,
      "external_symbols": 0,
      "external_packages": 0
    },
    "affected_files_top_n": [
      {"rel_path": "src/a.py", "candidate_versions": 5, "oldest_parsed": 1700000000, "newest_parsed": 1705000000}
    ],
    "external_packages_top_n": [
      {"package_name": "ext-python-oldpkg", "package_version": "1.0", "symbol_count": 12, "last_touch": 1700000000}
    ],
    "is_estimate": true
  }
  ```
  > 所有数量均为估算（基于候选 ID 集合预统计），不承诺精确磁盘节省。仅当 `--vacuum` 真正执行后 SQLite 文件空间才会释放到磁盘，单纯 DELETE 只会把空闲页留给后续写入。

### `gc_archive_list`
列出 `gc_archives/*.db.gz` 备份文件，按修改时间倒序。
- **参数**：`limit: int = 20`（钳制到 1–100）
- **返回**：`list[dict]` — 每条含 `{name, path, size, reason, mtime}`
- **说明**：`reason` 从文件名 `{YYYYMMDD-HHMMSS}-{reason}.db.gz` 解析。

### `gc_archive_inspect`
以只读模式打开 `.db.gz` 备份，返回 schema 版本、各表行数、关键摘要。
- **参数**：`path: str`（完整路径或文件名简写）
- **返回**：`dict` — `{name, path, size, schema_version, tables: {name: rows}, summary}`
- **说明**：使用 `file:path?mode=ro` URI 只读打开备份，绝不修改。

### `gc_archive_import`
从备份恢复指定文件或外部包到当前数据库，INSERT OR IGNORE 幂等。
- **参数**：`path: str`, `file_path: str = ""`, `package_name: str = ""`, `dry_run: bool = True`
- **返回**：`dict` — `{audit_id, dry_run, mode, target, restored_*, skipped_*, already_exists_*}`
- **说明**：`file_path` 与 `package_name` 二选一，必须有一个非空；已存在的行不会被覆盖（当前库优先）。

### `gc_audit_list`
列出 `gc_runs` 审计记录，按时间倒序。
- **参数**：`limit: int = 20`（钳制到 1–100）
- **返回**：`list[dict]` — 每条含 `{audit_id, operation, status, dry_run, started_at, completed_at}`

### `gc_audit_get`
查看指定审计记录的完整详情。
- **参数**：`audit_id: int`
- **返回**：`dict` — `{audit_id, operation, status, dry_run, policy_json, candidate_counts, deleted_counts, backup_path, backup_size, operator, started_at, completed_at, error}`

### `gc_policy_get`
读取当前 workspace 的 GC retention 策略。
- **参数**：无
- **返回**：`dict` — `{older_than_days, keep_versions, include_external, external_stale_days, backup_enabled, vacuum_enabled}`

### `gc_policy_set`
更新当前 workspace 的 GC retention 策略。
- **参数**：`older_than_days: int | None = None`, `keep_versions: int | None = None`, `include_external: bool | None = None`, `external_stale_days: int | None = None`, `backup_enabled: bool | None = None`, `vacuum_enabled: bool | None = None`
- **返回**：`dict` — 更新后的策略

CLI 心智模型：
- `cw gc retention --dry-run`：按数据库策略预演，不保存参数。
- `cw gc retention --apply --older-than 730`：本次执行临时覆盖，不保存参数。
- `cw gc policy set --older-than 730 --keep-versions 200`：只保存策略，不执行清理。
- `cw gc retention --apply --older-than 730 --save-policy`：保存传入策略并执行。
- 外部包清理必须显式启用 `--include-external` 或写入 policy；普通 `gc archive` 不会按当前分支删除外部符号。

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

## Agent Rule Memory 工具（AgentRulesMixin）

Agent Rule Memory 是 Call Warden 的项目规则记忆系统，支持"候选规则 → 审核 → 生效 → 注入 → 同步 AGENTS.md"全链路。Agent 在任务执行过程中观察到的规律可沉淀为候选规则，经审核后写入 `agent_rules`，再按上下文匹配注入到 `task_next_step` / `work_next_job` / `get_symbol` / `file_symbol_content` 的返回值中。

**scope 匹配规则**：
- 空 scope = 全局匹配
- 同字段内多值 OR 匹配（如 `languages: [python, rust]`）
- 不同字段间 AND 匹配
- `file_patterns` 支持 glob；`module_prefixes` 前缀匹配

**排序**：severity 优先级 → 命中字段数 → `updated_at` 倒序。

### `rule_candidate_create`
创建候选规则（pending 状态）。Agent 观察到的规则候选需走审核流程：创建 → 审核（accept/reject）→ 写入 `agent_rules` 生效。
- **参数**：
  - `title: str` — 规则标题（简短描述）
  - `rule_text: str` — 规则正文（Agent 注入时会原文返回）
  - `scope: dict = {}` — 作用域，支持 `languages` / `file_patterns` / `symbol_kinds` / `actions` / `finding_types` / `module_prefixes`
  - `severity: str = "info"` — `critical` / `error` / `warning` / `info`
  - `source: str = "manual"` — `manual` / `auto_quality_findings` / `auto_semgrep` / `task_review` / `other`
  - `evidence: dict = {}` — 证据（如 `task_id`、`occurrences` 等）
  - `confidence: float = 0.0` — 置信度 0.0-1.0
- **返回**：`{"candidate_id": "ARC-xxx"}`，失败返回 `{"error": str}`

### `rule_candidate_list`
列出候选规则。
- **参数**：
  - `status: str = "pending"` — `pending` / `accepted` / `rejected`，空串返回所有
  - `limit: int = 50` — 返回数量上限
- **返回**：`{"candidates": [...], "count": int}`

### `rule_candidate_accept`
接受候选规则，写入 `agent_rules`（active）。幂等：重复 accept 已 accepted 的 candidate 会返回原 `linked_rule_id`。
- **参数**：
  - `candidate_id: str` — 候选规则 ID（`ARC-xxx`）
  - `reviewer: str = "agent"` — 审核人标识
- **返回**：`{"rule_id": "AR-xxx"}`，失败返回 `{"error": str}`

### `rule_candidate_reject`
拒绝候选规则。
- **参数**：
  - `candidate_id: str` — 候选规则 ID（`ARC-xxx`）
  - `reviewer: str = "agent"` — 审核人标识
  - `reason: str = ""` — 拒绝原因（可选）
- **返回**：`{"rejected": bool}`，失败返回 `{"error": str, "rejected": False}`

### `rule_list`
列出已生效规则。
- **参数**：
  - `status: str = "active"` — `active` / `deprecated` / `removed`，空串返回所有
  - `limit: int = 100` — 返回数量上限
- **返回**：`{"rules": [...], "count": int}`

### `get_applicable_rules`
按上下文返回匹配的 active 规则。注入点（`task_next_step` / `work_next_job` / `get_symbol` / `file_symbol_content`）内部均调用此工具的同名 db 方法。
- **参数**：
  - `context: dict` — 上下文，支持字段 `languages` / `file_patterns` / `symbol_kinds` / `actions` / `finding_types` / `module_prefixes`
  - `limit: int = 10` — 返回数量上限
- **返回**：`{"rules": [...], "count": int}`

### `rule_sync_agents_md`
把 active 规则同步到 AGENTS.md 标记区。

**安全策略**：
- `dry_run=True`（默认）只返回 preview，不写文件
- apply 模式只替换 `CALLWARDEN_RULES_START` / `CALLWARDEN_RULES_END` 之间内容，不触碰人工维护区域
- 标记区不存在时返回 `error` + `suggested_block`（需先调用 `rule_insert_agents_md_block`）
- 写入后记录 `agent_rule_sync_log` 并标记规则 `synced_to_agents_md=1`

标记区格式示例：

```
<!-- CALLWARDEN_RULES_START -->
<!-- 自动同步区域，请通过 cw rule sync 更新，不要手改 -->
1. [critical] 不要在 utils 中直接调用 db 层
2. [warning] 新增 MCP 工具必须更新 docs/mcp_tools.md
<!-- CALLWARDEN_RULES_END -->
```

- **参数**：
  - `target_path: str = "AGENTS.md"` — AGENTS.md 文件路径（相对 workspace 或绝对）
  - `dry_run: bool = True` — `True`=只返回 preview，`False`=实际写入
  - `actor: str = "agent"` — 操作者标识
- **返回**：`{"success": bool, "rule_count": int, "preview": str, ...}`，失败返回 `{"error": str, "success": False}`

### `rule_insert_agents_md_block`
在 AGENTS.md 末尾插入 Call Warden 规则标记块。当标记区不存在时调用此方法插入空标记块，之后 `rule_sync_agents_md` 才能正常工作。重复插入会返回失败。
- **参数**：
  - `target_path: str = "AGENTS.md"` — AGENTS.md 文件路径
  - `actor: str = "agent"` — 操作者标识
- **返回**：`{"success": bool, "target_path": str, "message": str}`，失败返回 `{"error": str, "success": False}`

### `extract_rule_candidates_from_quality_findings`
从 `task_quality_findings` 聚合重复问题生成候选规则。

**聚合维度**：`(finding_type, severity, source)`
- 同一聚合键 `count >= min_occurrences` 时生成 1 个 pending 候选
- 去重：同一聚合键已有 pending 候选时跳过
- `evidence` 保存 `finding_ids`（最多 10 条）和 `occurrences`
- `confidence = min(1.0, occurrences/10)`

- **参数**：
  - `task_id: str = ""` — 任务 ID，空串则全库扫描
  - `min_occurrences: int = 2` — 阈值
- **返回**：`{"candidate_ids": [...], "count": int}`

### `rule_seed_bootstrap`
种子化内置自举 active rules。把 Call Warden 自身的 5 条核心规约以
**固定 ID** `AR-bootstrap-*` 写入 `agent_rules` 表，让 `task_next_step` /
`work_next_job` / `file_symbol_content` / `get_symbol` 等注入点能向
Agent 提供稳定的行为约束。

**幂等性**通过固定 ID 实现：
- 不存在 → `create`
- 存在但 `rule_text` 变化 → `update`
- 存在且无变化 → `skip`

**内置 5 条规则**：

| ID | severity | scope | 说明 |
|----|----------|-------|------|
| `AR-bootstrap-i18n` | warning | `{}` (global) | 用户可见输出必须通过 i18n.t() |
| `AR-bootstrap-refresh-before-commit` | warning | `{actions:[commit]}` | git commit 前必须 `cw --refresh-all` |
| `AR-bootstrap-task-split` | info | `{actions:[task_create]}` | 3+ 文件或 5+ 步骤必须 task_split |
| `AR-bootstrap-completion-review` | warning | `{actions:[task_report]}` | task_report 前必须 run_task_completion_review |
| `AR-bootstrap-capture-diff` | info | `{actions:[task_report]}` | task_report 前建议 task_capture_diff 验证磁盘 |

- **参数**：
  - `dry_run: bool = True` — True 只返回计划不写库；False 写入 `agent_rules` 表
- **返回**：`dict` —
  - `dry_run: bool`
  - `total: int` — 内置规则总数（5）
  - `created: int` — 新建数量
  - `updated: int` — 更新数量
  - `skipped: int` — 跳过数量
  - `rules: list[dict]` — 每条规则的执行结果
    - `{"id": str, "title": str, "action": "create"|"update"|"skip"}`

> **注入点**：seed 后的 active rules 会通过 `get_applicable_rules`
> 被以下注入点读取：`task_next_step`（applicable_rules）、
> `work_next_job`（project_rules + context.applicable_rules）、
> `build_structured_instruction`（project_rules）、
> `get_symbol`（applicable_rules）、`file_symbol_content`（applicable_rules）。

### `cleanup_agent_rule_sync_log`
清理 `agent_rule_sync_log` 表中的旧记录，防止无限增长（C6 GC）。

每次 `rule_sync_agents_md` 都会写入一条 sync_log 记录，长期累积膨胀。
本工具按**双重过滤策略**清理旧记录。

**清理策略**（同时满足才删除）：
1. `created_at` 早于 `older_than_days` 天前
2. 不在最近 `keep_latest` 条记录内（按 `created_at` 倒序）

**默认 dry-run**：`dry_run=True` 时只预估删除数量（`SELECT COUNT`），不执行 `DELETE`；
传 `dry_run=False` 才真正删除并 `commit`。

**fail-soft**：任何异常都封装为 `{"success": False, "error": ...}`，不抛出。

- **参数**：
  - `older_than_days: int = 90` — 超过多少天的记录进入候选
  - `keep_latest: int = 100` — 保留最近多少条记录不删除
  - `dry_run: bool = True` — True 只预估不删除；False 真正执行 DELETE
- **返回**：`dict` —
  - `success: bool`
  - `dry_run: bool`
  - `deleted_count: int` — dry_run 时为预估值，apply 时为实删数
  - `remaining_count: int` — 清理后剩余记录数
  - `total_before: int` — 清理前总记录数
  - `older_than_days: int`
  - `keep_latest: int`
  - `error: str` — 仅 `success=False` 时存在

**对应 CLI**：`cw rule cleanup-sync-log [--older-than 90] [--keep-latest 100] [--apply]`

### `bootstrap_status`
返回自举健康状态摘要。一行调用汇总以下信息，帮助判断当前自举闭环
（bootstrap closure）是否健康：

1. `db_stale` — DB 是否滞后（最近 scan_run 的 git_head 与当前 HEAD 不一致）
2. `active_rules_count` / `pending_candidates_count` — 已生效规则数 / 待审候选数
3. `open_findings_count` / `blocking_findings_count` — open 质量发现数 / 阻塞发现数
4. `audit_verify` — `audit_chain` 验证摘要（total / verified / broken / security_level）
5. `latest_scan_run` — 最近一次 `workspace_scan_runs` 记录
6. `tasks` — 任务按状态分组计数（open / in_progress / review / applied）
7. `recommended_next_action` — 推荐下一条命令

- **参数**：无
- **返回**：`dict` —
  - `db_stale: bool`
  - `current_head: str`
  - `active_rules_count: int`
  - `pending_candidates_count: int`
  - `open_findings_count: int`
  - `blocking_findings_count: int`
  - `audit_verify: {total_count, verified_count, broken_count, security_level}`
  - `latest_scan_run: dict | None`
  - `tasks: {open, in_progress, review, applied}`
  - `recommended_next_action: str` — 例如 `"cw --refresh-all"` /
    `"cw rule seed-bootstrap --apply"` / `"cw audit verify"` /
    `"cw task next <id>"`

> **只读工具**：不写数据库，不触发 workspace 激活，可安全与 CLI 写操作并发。

---

## MCP Server 配置方法

### stdio 模式（推荐，默认）

适用于 MCP client 直接启动并管理 Server 进程。

**Claude Desktop 配置**（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "callwarden": {
      "command": "python",
      "args": ["-m", "callwarden.server"],
      "env": {
        "CALLWARDEN_WORKSPACE": "/path/to/your/project"
      }
    }
  }
}
```

**Trae IDE 配置**：

```json
{
  "mcpServers": {
    "callwarden": {
      "command": "python",
      "args": ["-m", "callwarden.server"],
      "cwd": "/path/to/callwarden/parent",
      "env": {
        "CALLWARDEN_WORKSPACE": "/path/to/your/project"
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
| `CALLWARDEN_WORKSPACE` | 默认工作区根路径 |
| `CALLWARDEN_DB_PATH` | 自定义数据库路径（覆盖默认 hash 路径） |

### 多容器共享部署

```bash
# 1. 宿主机安装
pip install tree-sitter tree-sitter-languages fastmcp

# 2. 数据库放在 $HOME/.callwarden/<16位hash>/（所有容器共享 $HOME）
# 3. 每个容器配置 MCP client 指向同一数据库路径
```

> SQLite 支持多读者单写者，写入会自动排队，多进程安全。

详细部署见 [部署指南](deployment.md)。

## 下一步

- [CLI 命令参考](cli_reference.md)：CLI 等价命令
- [架构设计](architecture.md)：Mixin 架构与扩展指南
- [快速开始](quickstart.md)：完整示例会话
