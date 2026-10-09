# MCP↔CLI 对齐审计（2026-10-08）

## 1. 背景与任务

全量映射真相源（`server/tools/_categories.py` 的 `TOOL_CATEGORIES` 17 分类 243 工具 +
`TOOL_CLI_MAPPING` 243 条映射）完成后，243 个 MCP 工具中有 100 个被判定为
「MCP 专属（None）」。本审计回应的质疑是：**这 100 条 None 里，会不会有 CLI
子命令实现了但没找到？**

本文档记录对全部 100 条 None 判定的逐条复核过程、结论与修正，并回答
「17 类理论上应该一一对应吗」。

## 2. 结论总览

| 指标 | 复核前 | 复核后 |
| --- | --- | --- |
| 有 CLI 对应 | 143 | **153** |
| MCP 专属（None） | 100 | **90** |
| 合计 | 243 | 243 |

**100 条 None 判定复核结果**：

| 判定 | 数量 | 含义 |
| --- | --- | --- |
| 层1·补映射 | 5 | 同 RPC 实锤（CLI handler 调用同一 RPC），原判定**漏映射**，已修正 |
| 层2·补映射 | 5 | 语义等价（不同实现路径，CLI 已有功能对应入口），原判定**漏映射**，已修正 |
| 语义分叉·保持 None | 4 | 层1 命中但两侧契约不同（认领 vs 派工、任务级 vs 步骤级），**维持 None** 并注明 |
| MCP-only·有意设计 | 36 | Agent 专属暴露面，架构上有意不做 CLI，**判定正确** |
| MCP-only·CLI 缺失 | 50 | 确无 CLI 入口，**判定正确**（其中含值得补 CLI 的候选，见 §6） |

即：100 条 None 中，**90 条维持 None**（36 条有意设计 + 50 条 CLI 缺失 +
4 条语义分叉谨慎保留），**10 条判定为漏映射已补**。用户的质疑部分成立——
CLI 确实「藏」了 10 个实现入口（多为聚合命令的内嵌段或 flag 变体，
如 `cw task show` 的 Related 段、`cw check-gate --resolve`）。

**10 条补映射清单**：

| MCP 工具 | CLI 入口 | 层级 | 证据要点 |
| --- | --- | --- | --- |
| `get_task_commits` | `cw task show` | 层1 | task show Related 段调 db.get_task_commits，同 RPC `task.get_commits` |
| `get_task_symbol_changes` | `cw task show` | 层1 | 同上，RPC `task.get_symbol_changes` |
| `get_symbol_change_tasks` | `cw symbol-history` | 层1 | symbol-history 的 Related Tasks 段，同 RPC 原名路由 |
| `get_token_savings_report` | `cw health-report` | 层1 | health-report 调 db.get_token_savings_report("30d")，同 RPC；CLI 固定 30d |
| `resolve_gate_findings` | `cw check-gate --resolve` | 层1 | 经 `_METHOD_MAP` 同 RPC `gate.resolve_findings`；CLI 传 task_id，MCP 契约 gate_id 必填 |
| `get_symbol_content_by_hash` | `cw diff` | 层2 | cw diff 内部调该查询 ×2 取两版本内容 |
| `get_commit_tasks` | `cw git show` | 层2 | git show 的 Related Tasks 段，原名路由 vs `query.commit_tasks` |
| `task_create_subtask` | `cw task create --parent-id` | 层2 | task.create 父子创建 vs task.create_subtask 专项治理 |
| `scan_semgrep_incremental` | `cw semgrep scan --incremental` | 层2 | CLI 直调 RPC；MCP 经 job 通道 sync 执行（同 run_semgrep_scan 先例） |
| `get_comment_from_version` | `cw restore-comment --preview` | 层2 | 只读预览等价 |

**4 条语义分叉（层1 命中但不补）**：`get_active_workspace`（仅 daemon status 显式
id 可达）、`work_next_job`（CLI task next 走 task.claim 认领，非派工）、
`task_resolve_block`（CLI reopen 为任务级，MCP 为步骤级）、
`get_test_coverage_summary`（仅 daemon query 入口，属 CLI-only daemon_ops 域）。
这 4 条的共同特征：RPC 键相同或相近，但**参数面/语义契约不同**，补映射会
误导用户，故保持 None 并在映射表与本文档中注明分叉原因。

## 3. 复核方法论（三层，硬证据优先）

> **术语说明（消除「HTTP 还是 RPC」的歧义）**：本文档的「RPC 键」指 daemon 的
> **JSON-RPC 2.0 方法键**（点分命名，如 `task.get_commits`），**经 HTTP 传输**：
> 请求体 `{"jsonrpc":"2.0","method":<键>,"params":...}` POST 到 `/v1/rpc`
> （`server/daemon_client.py` 的 `HttpDaemonRpcClient.call`，L2353-2400；
> 类 docstring L2132 原文「所有读/写请求都经 HTTP POST /v1/rpc 透传到
> cw-daemon（Rust）」）。即 **HTTP 是传输层、JSON-RPC 是调用风格**，不是两套
> 协议——类似 gRPC over HTTP/2。少数纯 HTTP 端点（如 `/health`）不走方法键路由。
> 下文简写「RPC」均指这些方法键。

真相源两侧的调用路径：

- **MCP 侧**：工具函数体经 `_route('rpc.name', params, 'TIER')`
  （`server/daemon_client.py` 的 `route_rpc`，纯薄壳无本地回落）直达 daemon RPC。
- **CLI 侧**：handler 经 `db.<method>()` → `RpcDBProxy.__getattr__` →
  `_rpc_call` → `_METHOD_MAP` 查表（无条目则按方法名原样 route_rpc），或直接
  `route_rpc("x")` / `client.call("x")` / `method = "xxx"` 字符串赋值动态路由。

三层复核：

1. **层1·RPC 级交叉比对（最硬证据）**：用 AST 提取两侧函数体的 RPC 调用键并
   交叉比对。脚本 `.temp/rpc_cross_check.py`（结果存
   `.temp/rpc_cross_check.json`）：
   - MCP 侧：解析 `server/tools/*.py` 全部 `@mcp.tool()` 函数体的
     `_route` / `_call_daemon_rpc` / `.call` 调用键（含模块内辅助函数闭包展开）；
   - CLI 侧：解析 `cli/main.py` + `cli/daemon_commands.py` 全部 handler 的
     `route_rpc` / `.call` / `method="x"` 赋值 / `db.<method>()` 调用（经
     `_METHOD_MAP` 解析为真实 RPC；`_METHOD_MAP` 为 AnnAssign 带类型注解，
     提取需同时覆盖 `ast.Assign` 与 `ast.AnnAssign`），外加 dispatch 表与
     辅助函数传递闭包、If 分支级提取。
2. **层2·语义等价**：对层1 未直接命中但功能对应的条目，人工读码确认 CLI
   handler 内部确实消费同一查询/达成同一效果（如 `cw diff` 内部调
   `db.get_symbol_content_by_hash` ×2）。判定先例：`count_resolved_edges →
   build-context show`（聚合包含）、`run_semgrep_scan → semgrep scan`
   （MCP 走 job_submit sync=True）。
3. **层3·MCP-only 归因**：确认无 CLI 实现后，归因为「有意设计」（有架构
   依据，如 identity 域 CLI 仅暴露 revoke 以避免 T=M=D 三层暴露面重复）或
   「CLI 缺失」（值得补 CLI 候选）。

**已知局限**（保守处理的依据）：

- branchmap 对 argparse `opts.xxx` 风格 handler（task/rule/gc 等用
  `if opts.xxx:` 而非 `== "常量"` 分派）的子命令级 RPC 提取不全，改用
  handler 级闭包键 + add_parser 子命令清单 + 人工读码定位；
- 动态拼接的 RPC 名无法静态提取，靠 db 方法名 → `_METHOD_MAP` 映射兜底；
- 层2 判断本质是语义判断，**只补 CLI handler 内有直接消费实锤的条目**，
  拿不准的一律保持 None（4 条语义分叉即按此原则不补）。

映射修正的全部条目均通过 `tests/test_category_source.py::TestToolCliMapping`
的静态校验（顶层命令 ∈ 84 命令、多段路径的子命令在 add_parser 提取集合中
存在、值形状合法）。

## 4. 100 条逐条复核明细

RPC 列为 MCP 工具函数体实际调用的 daemon **JSON-RPC 2.0 方法键**（经 HTTP POST
`/v1/rpc` 传输，AST 提取，多个用 ` / ` 分隔）；判定列的 `→ cw ...` 为本次补映射
的 CLI 入口。

> **读表提示（N:1 聚合，不是歧义）**：同一 `cw <命令>` 出现在多行是刻意设计——
> 该 CLI 命令的 handler 依次调用多个 daemon RPC，把结果渲染成多个输出段。例如
> `cw task show` 聚合 `task_status`（任务详情）+ `get_task_commits`（Related
> commits 段）+ `get_task_symbol_changes`（Related 符号变化段）三个 MCP 工具。
> 「说明」列标注了每个工具对应哪个输出段，故无歧义；反向看，一个 CLI 命令自然
> 对应多个 MCP 原子工具（243 工具 ↔ 84 命令即 N:1 聚合关系）。

| 工具 | 分类 | RPC | 判定 | 证据/原因 |
| --- | --- | --- | --- | --- |
| `get_active_workspace` | [1] workspace_database | `workspace.status` | 语义分叉·保持 None | RPC workspace.status 仅 cw daemon status <id> 可达（daemon_ops 为 CLI-only 域，需显式 id）；无「当前活动工作区」独立查询入口 |
| `build_directory` | [1] workspace_database | `workspace.build_directory` | MCP-only·CLI 缺失 | CLI refresh 仅文件级（_handle_refresh L8947-8955），无目录级构建入口 |
| `remove_file` | [1] workspace_database | `workspace.file.remove` | MCP-only·CLI 缺失 | 图谱文件删除（索引维护）无 CLI 入口 |
| `register_branch` | [1] workspace_database | `admin.branch_register` | MCP-only·CLI 缺失 | 分支快照管理整域（register/list/diff/switch/merge_preview）CLI 缺失 |
| `list_branches` | [1] workspace_database | `list_branches` | MCP-only·CLI 缺失 | 同上 |
| `diff_branches` | [1] workspace_database | `query.diff_branches` | MCP-only·CLI 缺失 | 同上 |
| `switch_branch` | [1] workspace_database | `admin.branch_switch` | MCP-only·CLI 缺失 | 同上 |
| `merge_preview` | [1] workspace_database | `merge_preview` | MCP-only·CLI 缺失 | 同上 |
| `get_symbol_history` | [2] query_search | `get_symbol_history` | MCP-only·CLI 缺失 | CLI symbol-history 实调 get_symbol_commit_history（Git 维度），符号版本历史（versions 表维度）无入口 |
| `get_file_history` | [2] query_search | `query.file_history` | MCP-only·CLI 缺失 | 文件版本历史无 CLI 入口 |
| `get_symbol_content_by_hash` | [2] query_search | `query.symbol_content_by_hash` | 层2·补映射→ `cw diff` | cw diff <hash1> <hash2>（L10310）内部调 db.get_symbol_content_by_hash ×2 取两版本内容；单取内容无独立入口 |
| `file_read` | [2] query_search | `workspace.file.read` | MCP-only·有意设计 | Agent 上下文读取面（工作区白名单内文件），CLI 用户直接用编辑器查看 |
| `file_list` | [2] query_search | `workspace.file.list` | MCP-only·有意设计 | Agent 目录浏览面，同上 |
| `file_symbol_content` | [2] query_search | `workspace.file.symbol_content` | MCP-only·有意设计 | Agent 按符号读取源码面，同上 |
| `embed_symbols_async` | [2] query_search | `task.job_submit` | MCP-only·有意设计 | 异步作业变体（task.job_submit），CLI embed 为同步批量 |
| `embed_single_symbol` | [2] query_search | `task.job_submit` | MCP-only·CLI 缺失 | CLI embed 仅批量，单符号嵌入无入口 |
| `generate_summary` | [2] query_search | `summary.generate` | MCP-only·CLI 缺失 | 摘要写面无 CLI 入口 |
| `get_summary` | [2] query_search | `get_summary` | MCP-only·CLI 缺失 | 摘要查询无 CLI 入口 |
| `ask_codebase` | [2] query_search | `ask_codebase` | MCP-only·CLI 缺失 | RAG 问答管道无 CLI 入口 |
| `record_token_savings` | [2] query_search | `edit.record_token_savings` | MCP-only·有意设计 | Agent 自报 token 节省账本（编排度量面），无人工使用场景 |
| `get_token_savings_report` | [2] query_search | `get_token_savings_report` | 层1·补映射→ `cw health-report` | cw health-report（L9056）调 db.get_token_savings_report("30d")，同 RPC；CLI 固定 30d 窗口，MCP 可自定义 |
| `get_impact` | [3] call_chain | `get_impact` | MCP-only·CLI 缺失 | CLI impact 实调 blast_radius（symbol_hash 维度 BFS），QN 维度影响面分析无入口 |
| `diff_callers` | [3] call_chain | `query.diff_callers` | MCP-only·CLI 缺失 | 分支感知调用者对比无 CLI 入口 |
| `diff_callees` | [3] call_chain | `query.diff_callees` | MCP-only·CLI 缺失 | 分支感知被调用者对比无 CLI 入口 |
| `get_code_health_check` | [4] code_health | `query.code_health` | MCP-only·CLI 缺失 | 整体健康检查无 CLI 入口（metrics/complexity 等为分项命令） |
| `check_file_health` | [4] code_health | `workspace.file.health` | MCP-only·CLI 缺失 | 单文件健康检查无 CLI 入口 |
| `defect_correlation` | [4] code_health | `query.defect_correlation` | MCP-only·CLI 缺失 | 按 symbol_hash 的缺陷关联；CLI evolution --defects 实调 get_defect_correlation（QN 维度），维度不同 |
| `task_create_subtask` | [5] task | `task.create_subtask` | 层2·补映射→ `cw task create` | cw task create --parent-id（L3715）实现父子创建；MCP 走 task.create_subtask 专项治理 RPC |
| `task_create_from_plan` | [5] task | `task.create_from_plan` | MCP-only·有意设计 | Markdown 计划自动创建父子任务树（task.create_from_plan，Agent 编排自动化），CLI task create 为手动单建 |
| `task_plan_template` | [5] task | `task_plan_template` | MCP-only·CLI 缺失 | 计划模板查询无 CLI 入口（可低成本补 cw task plan-template） |
| `work_next_job` | [5] task | `task.work_next` | 语义分叉·保持 None | CLI task next 走 task.claim 认领语义（L4528），非 task.work_next 派工语义 |
| `task_resolve_block` | [5] task | `task.reopen` | 语义分叉·保持 None | CLI task reopen 为任务级（reviewer/reason，L5302）；MCP task.reopen 携带 step_id/resolution 为步骤级解封 |
| `record_task_symbol_change` | [5] task | `task.record_symbol_change` | MCP-only·CLI 缺失 | 步骤符号变化记账（Agent 编辑流内部面）无 CLI 入口 |
| `link_edit_audit_symbols` | [5] task | `task.link_edit_audit_symbols` | MCP-only·CLI 缺失 | edit_audit 符号映射（编辑流内部面）无 CLI 入口 |
| `get_task_symbol_changes` | [5] task | `task.get_symbol_changes` | 层1·补映射→ `cw task show` | cw task show 的 Related 段（main.py L6585-6619）调 db.get_task_symbol_changes，与 MCP 同 RPC |
| `get_symbol_change_tasks` | [5] task | `get_symbol_change_tasks` | 层1·补映射→ `cw symbol-history` | cw symbol-history 的 Related Tasks 段（L7413）调 db 同名方法，与 MCP 同为原名路由 |
| `cancel_job` | [5] task | `task.job_cancel` | MCP-only·有意设计 | 异步作业管理面（Agent 编排），CLI 为同步人机交互、无作业句柄 |
| `list_jobs` | [5] task | `task.list_jobs` | MCP-only·有意设计 | 同上 |
| `get_job_stats` | [5] task | `task.job_stats` | MCP-only·有意设计 | 同上 |
| `wait_for_job` | [5] task | `task.wait_for_job` | MCP-only·有意设计 | 同上（阻塞等待作业完成，Agent 轮询模式） |
| `get_job_status` | [5] task | `task.job_status` | MCP-only·有意设计 | 同上 |
| `get_task_commits` | [5] task | `task.get_commits` | 层1·补映射→ `cw task show` | cw task show 的 Related 段调 db.get_task_commits，与 MCP 同 RPC |
| `get_commit_tasks` | [5] task | `query.commit_tasks` | 层2·补映射→ `cw git show` | cw git show 的 Related Tasks 段（L11648）走 db.get_commit_tasks 原名路由，MCP 走 query.commit_tasks，功能等价 |
| `task_remediation_create` | [5] task | `task.remediation.create` | MCP-only·CLI 缺失 | 任务整改单创建无 CLI 入口 |
| `resolve_gate_findings` | [7] audit_bootstrap | `gate.resolve_findings` | 层1·补映射→ `cw check-gate` | cw check-gate <task_id> --resolve（L7463）经 _METHOD_MAP 映射到同 RPC gate.resolve_findings；参数面差异：CLI 传 task_id，MCP 契约 gate_id 必填（2026-09-30 契约修复） |
| `guardrail_check_edit` | [7] audit_bootstrap | `guardrail_check_edit` | MCP-only·CLI 缺失 | CLI guardrail 域仅 scan/rules 子命令，编辑前阻断检查无入口 |
| `guardrail_add_rule` | [7] audit_bootstrap | `guardrail.add_rule` | MCP-only·CLI 缺失 | CLI 无 guardrail 规则添加子命令 |
| `compare_snapshots` | [8] git | `admin.snapshot_compare` | MCP-only·CLI 缺失 | 快照对比无 CLI 入口 |
| `semgrep_scan_async` | [9] semgrep_defects | `task.job_submit` | MCP-only·有意设计 | 异步作业变体（task.job_submit），CLI semgrep scan 为同步 |
| `scan_semgrep_incremental` | [9] semgrep_defects | `task.job_submit` | 层2·补映射→ `cw semgrep scan` | cw semgrep scan --incremental（L11924）直调 db.scan_semgrep_incremental；MCP 经 task.job_submit sync=True 通道（同 run_semgrep_scan 先例） |
| `diff_to_symbol` | [9] semgrep_defects | `query.diff_to_symbol` | MCP-only·CLI 缺失 | diff→符号归因无 CLI 入口 |
| `cross_layer_impact` | [9] semgrep_defects | `cross_layer_impact` | MCP-only·CLI 缺失 | 跨层影响分析无 CLI 入口 |
| `get_test_coverage_summary` | [10] coverage_ownership | `query.tests` | 语义分叉·保持 None | RPC query.tests 聚合仅 cw daemon query（daemon_ops CLI-only 域）可达；cw tests 无 --coverage 聚合 flag |
| `get_comment_from_version` | [10] coverage_ownership | `get_comment_from_version` | 层2·补映射→ `cw restore-comment` | cw restore-comment --preview（L10398）只读预览等价；MCP 为独立只读查询 |
| `parse_codeowners` | [10] coverage_ownership | `parse_codeowners` | MCP-only·CLI 缺失 | CODEOWNERS 解析为导入管线内部步骤，无独立 CLI 入口 |
| `import_codeowners` | [10] coverage_ownership | `task.job_submit` | MCP-only·CLI 缺失 | CODEOWNERS 导入无 CLI 入口 |
| `import_git_blame` | [10] coverage_ownership | `task.job_submit` | MCP-only·CLI 缺失 | git blame 导入无 CLI 入口 |
| `get_project_dependencies` | [11] gc | `get_project_dependencies` | MCP-only·CLI 缺失 | 项目依赖查询（gc 外部符号域）无 CLI 入口 |
| `import_project_dependencies` | [11] gc | `task.job_submit` | MCP-only·CLI 缺失 | 项目依赖导入无 CLI 入口 |
| `prune_external_symbols` | [11] gc | `task.job_submit` | MCP-only·CLI 缺失 | 外部符号清理无 CLI 入口 |
| `detect_clones_async` | [12] diagnostics | `task.job_submit` | MCP-only·有意设计 | 异步作业变体（task.job_submit），CLI clone detect 为同步 |
| `list_clone_groups` | [12] diagnostics | `list_clone_groups` | MCP-only·CLI 缺失 | 克隆分组查询无 CLI 入口（clone 域仅 detect/list/stats/clear） |
| `get_clone_group_detail` | [12] diagnostics | `get_clone_group_detail` | MCP-only·CLI 缺失 | 同上 |
| `get_clone_group_stats` | [12] diagnostics | `task.clone_group_stats` | MCP-only·CLI 缺失 | 同上 |
| `get_clone_aware_impact` | [12] diagnostics | `get_clone_aware_impact` | MCP-only·CLI 缺失 | 克隆感知影响分析无 CLI 入口 |
| `propose_edit` | [12] diagnostics | `edit.propose` | MCP-only·有意设计 | MCP 安全编辑合约（propose→审计→revert 闭环），CLI 走常规编辑器 + git |
| `propose_range_patch` | [12] diagnostics | `edit.propose_range_patch` | MCP-only·有意设计 | 同上（range patch 变体） |
| `propose_symbol_patch` | [12] diagnostics | `edit.propose_symbol_patch` | MCP-only·有意设计 | 同上（符号级 patch 变体） |
| `propose_symbol_id_patch` | [12] diagnostics | `edit.propose_symbol_id_patch` | MCP-only·有意设计 | 同上（符号 ID 定位 patch 变体） |
| `revert_edit` | [12] diagnostics | `edit.revert` | MCP-only·有意设计 | 同上（编辑回滚走 edit 审计链，非 git checkout） |
| `get_edit_history` | [12] diagnostics | `get_edit_history` | MCP-only·有意设计 | 编辑审计查询（编辑合约配套） |
| `get_edit_stats` | [12] diagnostics | `edit.stats` | MCP-only·有意设计 | 编辑审计统计（编辑合约配套） |
| `detect_cross_repo_deps` | [12] diagnostics | `task.job_submit` | MCP-only·有意设计 | 跨仓分析面（Agent 多仓工作流），CLI 定位单仓 |
| `find_shared_symbols` | [12] diagnostics | `find_shared_symbols` | MCP-only·有意设计 | 同上 |
| `cross_repo_impact` | [12] diagnostics | `cross_repo_impact` | MCP-only·有意设计 | 同上 |
| `cross_repo_summary` | [12] diagnostics | `cross_repo_summary` | MCP-only·有意设计 | 同上 |
| `lsp_hover` | [12] diagnostics | `lsp_hover` | MCP-only·有意设计 | LSP hover 为 Agent 编辑会话面，CLI 无 LSP 客户端会话 |
| `lsp_definition` | [12] diagnostics | `lsp_definition` | MCP-only·有意设计 | LSP 跳转定义为 Agent 编辑会话面 |
| `lsp_references` | [12] diagnostics | `lsp_references` | MCP-only·有意设计 | LSP 引用查找为 Agent 编辑会话面 |
| `lsp_diagnostics` | [12] diagnostics | `lsp_diagnostics` | MCP-only·有意设计 | LSP 实时诊断为 Agent 编辑会话面 |
| `lsp_completion` | [12] diagnostics | `lsp_completion` | MCP-only·有意设计 | LSP 补全为 Agent 编辑会话面 |
| `lsp_check_available` | [12] diagnostics | `lsp_check_available` | MCP-only·有意设计 | LSP 可用性探测为 Agent 会话前置检查 |
| `get_active_build_context` | [13] build_context | `build_context.active` | MCP-only·CLI 缺失 | CLI activate 为写操作（set_active），活动 build context 查询无入口 |
| `get_metrics` | [12] diagnostics | `admin.metrics_get` | MCP-only·CLI 缺失 | cw daemon metrics 属 CLI-only daemon_ops 域，共享域内无查询入口 |
| `append_evidence` | [14] collab | `evidence.append` | MCP-only·CLI 缺失 | CLI collab publish 走 snapshot.publish（非 evidence.append），evidence 追加面缺失 |
| `find_evidence` | [14] collab | `find_evidence` | MCP-only·CLI 缺失 | CLI collab reveal 走 reveal.submit（写），evidence 查询无入口 |
| `get_freshness_status` | [14] collab | `get_freshness_status` | MCP-only·CLI 缺失 | freshness 状态查询无 CLI 入口 |
| `get_gate_decision` | [14] collab | `get_gate_decision` | MCP-only·CLI 缺失 | CLI collab gate-trigger 走 gate.decide（写），决策查询无入口 |
| `get_role_view` | [14] collab | `get_role_view` | MCP-only·CLI 缺失 | 角色视图查询无 CLI 入口 |
| `publish_interface` | [15] dependency | `admin.publish_interface` | MCP-only·CLI 缺失 | 接口发布（Req 9 写面）无 CLI 入口（dependency 域仅 inspect/list/cycle/explain/provider-select） |
| `get_interface_providers` | [15] dependency | `get_interface_providers` | MCP-only·CLI 缺失 | 接口 provider 查询无 CLI 入口 |
| `import_envelope_dependencies` | [15] dependency | `task.job_submit` | MCP-only·CLI 缺失 | envelope 依赖导入无 CLI 入口 |
| `record_artifact_identity` | [15] dependency | `admin.record_artifact_identity` | MCP-only·CLI 缺失 | artifact 身份记录无 CLI 入口 |
| `get_artifact_freshness` | [15] dependency | `get_artifact_freshness` | MCP-only·CLI 缺失 | artifact freshness 查询无 CLI 入口 |
| `record_action_identity` | [17] identity | `admin.record_action_identity` | MCP-only·有意设计 | CLI identity 域仅暴露 revoke，避免 T=M=D 三层暴露面重复（cli/categories.py 分类注释） |
| `get_action_identity` | [17] identity | `get_action_identity` | MCP-only·有意设计 | 同上 |
| `check_action_identity` | [17] identity | `check_action_identity` | MCP-only·有意设计 | 同上 |
| `check_session_separation` | [17] identity | `check_session_separation` | MCP-only·有意设计 | 同上 |
| `get_attestation_validity` | [17] identity | `get_attestation_validity` | MCP-only·有意设计 | 同上 |
| `list_attestation_revocations` | [17] identity | `list_attestation_revocations` | MCP-only·有意设计 | 同上 |

## 5. 原因分类统计

### 5.1 判定类别统计

| 判定 | 数量 | 说明 |
| --- | --- | --- |
| 层1·补映射 | 5 | 同 RPC 实锤，已补 TOOL_CLI_MAPPING |
| 层2·补映射 | 5 | 语义等价（不同实现路径），已补 |
| 语义分叉·保持 None | 4 | 层1 命中但两侧契约不同 |
| MCP-only·有意设计 | 36 | Agent 专属暴露面（identity 6 / lsp 6 / 安全编辑合约 7 / 跨仓 4 / job 异步与编排 9 / Agent 读面 3 / token 账本 1） |
| MCP-only·CLI 缺失 | 50 | 无 CLI 入口（分支感知 5 / 索引维护 2 / 查询摘要 RAG 6 / 调用链 3 / 健康 3 / task 治理 4 / guardrail 2 / 快照对比 1 / semgrep 缺陷 2 / 所有权导入 3 / gc 外部符号 3 / clone 分组 4 + daemon 运行时指标 1 / build_context 1 / collab 5 / dependency 5） |
| **合计** | **100** | 原 None 判定全量复核 |

### 5.2 分域统计（复核前 → 复核后）

| 分类 | 工具数 | 原 None | 现 None | 有 CLI 对应 | 映射率 |
| --- | --- | --- | --- | --- | --- |
| [1] workspace_database | 16 | 8 | 8 | 8 | 50% |
| [2] query_search | 24 | 13 | 11 | 13 | 54% |
| [3] call_chain | 14 | 3 | 3 | 11 | 79% |
| [4] code_health | 12 | 3 | 3 | 9 | 75% |
| [5] task | 36 | 17 | 12 | 24 | 67% |
| [6] rule_memory | 11 | 0 | 0 | 11 | 100% |
| [7] audit_bootstrap | 10 | 3 | 2 | 8 | 80% |
| [8] git | 6 | 1 | 1 | 5 | 83% |
| [9] semgrep_defects | 18 | 4 | 3 | 15 | 83% |
| [10] coverage_ownership | 19 | 5 | 4 | 15 | 79% |
| [11] gc | 11 | 3 | 3 | 8 | 73% |
| [12] diagnostics | 27 | 22 | 23 | 4 | 15% |
| [13] build_context | 8 | 2 | 1 | 7 | 88% |
| [14] collab | 6 | 5 | 5 | 1 | 17% |
| [15] dependency | 10 | 5 | 5 | 5 | 50% |
| [16] assignment_lease | 8 | 0 | 0 | 8 | 100% |
| [17] identity | 7 | 6 | 6 | 1 | 14% |
| **合计** | **243** | **100** | **90** | **153** | **63%** |

> **注（2026-10-08 分类修正）**：`get_metrics` 原误归 [13] build_context（根因：与
> toolchain/build_context 同源 `server/tools/tools_rules.py` 模块分组），语义实为
> **daemon 运行时指标**，已迁至 [12] diagnostics。故 [12] 工具数 26→27、现 None
> 22→23；[13] 工具数 9→8、现 None 2→1。合计不变（243 工具 / 90 MCP-only / 153 有 CLI）。

## 6. 「值得补 CLI」建议清单

按价值与成本分优先级。下列 P1-P3 逐条覆盖 §4 中「MCP-only·CLI 缺失」的
**全部 50 条**（另含 1 条例外：`task_create_from_plan` 虽归「有意设计」，但对
CLI 用户价值明确，列入 P2）；「语义分叉」4 条若后续 CLI 契约对齐也可纳入：

**P1 · 补齐查询盲区（用户可直接感知）**

1. `cw impact --qn <QN>`（`get_impact`）——现 `cw impact` 实调 blast_radius
   （hash 维度），QN 维度影响面分析缺失且两者易混淆；
2. `cw health [file]`（`get_code_health_check` / `check_file_health`）——整体/单文件
   健康检查，现仅有 metrics/complexity 等分项命令；
3. `cw summary <QN>` + 写入口（`get_summary` / `generate_summary`）；
4. 分支快照 5 件套 `cw branch register/list/diff/switch/merge-preview`
   （`register_branch` / `list_branches` / `diff_branches` / `switch_branch` /
   `merge_preview`）——`diff_callers` / `diff_callees` / `compare_snapshots` 等
   分支感知查询依赖该域数据，是最大的整域缺口。

**P2 · 编排能力下放（低成本高价值）**

5. `cw task plan-template` + `cw task create --from-plan <file>`
   （`task_plan_template`；`task_create_from_plan` 虽归有意设计，但对 CLI 用户
   价值明确）；
6. `cw guardrail add-rule` / `cw guardrail check-edit`（`guardrail_add_rule` /
   `guardrail_check_edit`）——现有 `cw guardrail scan` / `cw guardrail rules`
   之外，规则管理闭环只剩这两个口子；
7. `cw history symbol <QN>` / `cw history file <path>`（`get_symbol_history` /
   `get_file_history`，DB 维度版本历史，与 Git 维度 symbol-history 互补）；
8. `cw ask <question>`（`ask_codebase`，RAG 问答）。

**P3 · 长尾按需**

9. clone 分组查询 3 件 + `get_clone_aware_impact`；
10. collab 只读面 5 件（evidence 追加/查询、freshness、gate 决策、role view）；
11. dependency 接口/工件面 5 件（publish_interface、providers、envelope 导入、
    artifact 身份/freshness）；
12. gc 外部符号 3 件（依赖查询/导入/清理）；
13. 所有权导入 3 件（CODEOWNERS 解析/导入、git blame 导入）；
14. 零散长尾（补齐其余全部）：`build_directory` / `remove_file`（目录级刷新）、
    task 治理 3 件（`link_edit_audit_symbols` / `record_task_symbol_change` /
    `task_remediation_create`；`task_plan_template` 已列 P2）、
    `diff_to_symbol` / `cross_layer_impact`、`get_active_build_context` /
    `get_metrics`（daemon 运行时指标）、`defect_correlation`（缺陷相关性）、
    `embed_single_symbol`、`diff_callers` / `diff_callees` / `compare_snapshots`
    （分支感知查询，依赖 P1.4 域数据）。

> 注：`get_metrics` 与 `defect_correlation` 已有**部分覆盖**——前者经
> `cw daemon metrics`（属 [19] daemon CLI-only 域，`--format`/`--name` 能力与
> MCP 工具重叠），后者经 `cw evolution --defects`（QN 维度）。二者仍列本清单，
> 是指**共享域 [1]-[17] 内缺对称独立入口**。

## 7. 「理论上 17 类应该一一对应吗」——数据支撑结论

**不应该，也不需要。17 类是「功能域同构」（domain-level parity），不是
「工具一一对应」（tool-level parity）。** 数据支撑：

1. **映射率天然分层且差异巨大**：从 100%（[6] rule_memory、[16]
   assignment_lease——规则与派工两类两侧使用模式一致）到 14-17%（[17]
   identity、[12] diagnostics、[14] collab——Agent 编排/会话/协作面），
   全域 63%。若「一一对应」是设计目标，这个分布不可能出现。
2. **使用模式决定域内粒度**：MCP 面向 Agent，需要细粒度原子工具（36 条
   有意设计：异步作业句柄、LSP 会话、安全编辑合约、跨仓分析、身份证明）；
   CLI 面向人，需要聚合命令——`cw health-report` 一个命令聚合多个 MCP 查询，
   `cw task show` 聚合 3 个 MCP 工具的输出段。243 MCP 工具 ↔ 84 CLI 命令
   本身就是 N:1 的聚合关系。
3. **不对称是双向的**（直接回答「MCP 能用 CLI 不能用，或反过来？」）：

   | 方向 | 数量 | 性质 |
   | --- | --- | --- |
   | MCP 能、CLI 无 | 90 | 36 有意设计（Agent 专属面）+ 50 CLI 缺失（可补，见 §6）+ 4 语义分叉 |
   | CLI 能、MCP 无 | 9 命令 | [18]-[21] CLI-only 域（daemon 运维 / 安装 / 回滚 / 盲评实验，9 个顶层命令；其中 daemon 含 23 个子命令），人类运维动作，有意不暴露给 Agent |

   两侧共用**同一个 Rust daemon 后端**（HTTP/JSON-RPC 同一路由），差别只在门面
   各自裁剪：MCP 面向 Agent 要原子 + 安全边界，CLI 面向人要聚合 + 运维可达。
   因此「域级对齐、条目级互补」，不存在「后端能力一侧通、另一侧不通」的情况——
   90 条 MCP-only 里 36 条是**有意不给 CLI**，50 条是**后端通但门面未包**（真缺口）。
4. **补齐的正确方向不是扩映射表，而是 §6 的补 CLI 清单**：映射表如实
   记录现状（含语义分叉的 4 条），缺口由新 CLI 命令消解后自然转为映射。

## 8. 变更清单与验证

> **2026-10-08 复审修订**：① 修正 §7.3 CLI-only 命令数（原写 14 → 实测 9 个顶层
> 命令，[18]-[21] 经 `cli/categories.py` 统计）；② 补 §3 术语说明（「RPC」=
> JSON-RPC 2.0 方法键经 HTTP POST `/v1/rpc` 传输，非独立协议）；③ 补 §4 读表
> 提示（同一 `cw <命令>` 多行是 N:1 聚合、非歧义）；④ 重写 §2 结论文案，消除
> 「90 条判定正确」与「4 条语义分叉」并列表述的重复计数歧义；
> ⑤ 补 §6 建议清单：补漏列工具（`defect_correlation` / `get_metrics`，均注明
> 已有部分覆盖来源）、`cw guardrail check` → `check-edit`（对齐工具名
> `guardrail_check_edit`）、消除 §6.14「task 治理 4 件」与 P2.5
> `task_plan_template` 的重复计数，使 P1-P3 逐条覆盖全部 50 条 CLI 缺失
> （经 `server/tools/_categories.py` 的 None 集合与 §5.1 归类逐条核对）；
> ⑥ 修正 `get_metrics` 分类错位：原归 [13] build_context（根因同源
> `tools_rules.py` 模块分组），语义为 daemon 运行时指标，迁至 [12]
> diagnostics；同步 §4 分类列、§5.1 归类明细、§5.2 分域计数（[12] 26→27、
> [13] 9→8）、§6.14 标签。
> ⑦ 修正文档过时 flag（静态 flag 审计法定位）：扫描全部 shipped `.py` 中
> `--flag` 字面量得权威集合，与 `cli_reference.md` / `mcp_tools.md` 出现的
> flag 求差，修 6 处「文档写了但代码里已不存在」——`get_metrics` 配套 CLI
> 由 `--reset`/`--local` 改为 `--format`/`--name`/`--from-file`；
> `cw daemon metrics` 移除 `--local` 本地降级与隐式 snapshot fallback
> （CLI-004 fail-closed，改 `--from-file` 显式离线快照）；`cw daemon serve
> --http-bind` → `cw-daemon.exe --http-bind`（G1 影子 server 下线、H6 默认
> 动态端口）；evolution 删除 `--window-commits`（parser 无此项，窗口固定
> 默认 5）；tests 删除 `--coverage`（MCP 专属 `get_test_coverage_summary`）；
> task report `--success` → `--result`。
> ⑧ 全量回溯复审（CLI 命令 × MCP 工具 × 文档，逐分类比对）：基线校验
> `--check` OK + `test_category_source` 17 项通过 + `@mcp.tool()` 计数 243
> 吻合，确认**分类体系本身无遗漏/无漂移**；缺陷集中在不受 marker/test 保护
> 的**手写交叉引用**——(a) `mcp_tools.md`「场景→工具索引」11 处 CLI 列与
> `TOOL_CLI_MAPPING` 矛盾（含 3 处指向不存在的子命令）；(b) `cli/categories.py`
> 2 处文案过时（[13] scope 残留「指标」、`tests` desc 列出假子命令
> `case/coverage`）；(c) 新发现 `TOOLS.md` 同类过时 12 处（`cw impact <QN>`
> →`<hash>`、`cw git log --author/--since`→`[limit]`、`defect import/add`
> →`learn/build`、`refresh --watch`→`watch`、`cw daemon serve`→`start`、
> `guardrail list`→`rules` 及 3 处不存在的 `symbol comment-from-version` /
> `defect cross-layer` / `guardrail check-edit`，以及 MCP 工具计数 237→243）。
> ⑨ 续审收尾（同日第二轮，逐文档计数核对）：补齐首批未覆盖的活文档残留——
> `docs/agent-usage-guide.md`「83 个顶层 CLI 命令」→84；`docs/architecture.md`
> 命令风格节 2 处「12 主分类 / 12 大类」→「21 主分类 / 17 分类」；
> `docs/mcp_tools.md` L10 讨论「12 主分类」→17 主分类；本档 §7.3 daemon
> 「约 21 个子命令」→23。同步修正 `docs/cli_reference.md` cw-client 表述
> （原「**禁止 serve**，其他 15 个子命令与 `cw daemon` 完全一致」→ Python 路径
> 与 `cw daemon` 共用 23 个子命令 / `CW_USE_RUST_CLIENT=1` 时 Rust 加速版 18 个
> 子命令含通用 `rpc`）及「与 cw daemon 的差异」表 `rpc` 行（原误标 `cw daemon`
> 支持 `rpc`）；更新测试 `tests/test_cli_docs_structure.py` 主分类断言
> 12→21；`cli/daemon_commands.py` 的 `run_daemon_command` docstring 中
> 「cw daemon 允许 serve」过时表述一并更正。
> ⑩ 第三轮审计（docs/design/ 活文档交叉引用与计数）：计数漂移仅余
> `docs/design/implementation-status.md` L16「83 命令」→84（79 subcommand +
> install/server/test 3 standalone + setup + daemon）。失效交叉引用按链接风格
> 修 3 组——(A) `docs/architecture.md`、`docs/mcp_tools.md` 指向
> `_feature_matrix.md` 的 `../design/…`→`design/…`，`docs/cli_reference.md`
> 的 `[TOOLS.md](TOOLS.md)`→`../TOOLS.md`；(B) `docs/design/_feature_matrix.md`
> 的 `docs/architecture.md#…`→`../architecture.md#…`、5 处
> `docs/design/implementation-status.md`→`implementation-status.md`；(C)
> `docs/role-loop-templates/` 4 个模板 11 处 `role-protocol.md` 由仓库根相对
> 改 doc-relative `../../.agents/…`、`Planner v1.md` 的
> `docs/design/cw-role-handoff-task-loop-v2-amendment.md`→`../design/…`，
> `docs/performance_report_million_symbols.md` 的
> `[docs/roadmap_phase2_plan.md](../roadmap_phase2_plan.md)`→
> `[roadmap_phase2_plan.md](roadmap_phase2_plan.md)`。
> ⑪ 第四轮审计（根 `*.md` + `docs/*.md` + `.agents/**`，共 246 文档的交叉引用与计数）：
> 计数面无漂移（243/84/21/17 一致；237/239/206/120 全落在 `docs/design`、
> `docs/reports`、`deliverables` 历史快照）。修 2 处活文档断链——`docs/architecture.md`
> 的 transport 目录链接 `../rust_ext/src/daemon/transport/`→`transport.rs`
> （transport 为扁平文件非目录）；`TOOLS.md` 的 `docs/task_create_subtask.py`
> →`archive/docs-legacy/task_create_subtask.py`（2026-08-28 归档）。另将
> `docs/performance_report_million_symbols.md` 2 处失效数据源引用
> （`tests/_check_parse.py` 临时脚本 / `tests/_bench_report.json` 生成产物）
> 去链接并标注。

**变更文件**：

- `server/tools/_categories.py`：`TOOL_CLI_MAPPING` 10 条 None →
  `(path, note)`（note 注明证据与参数面差异）；头部新增复核记录注释；
  `cli_only_in_shared_categories` docstring 的 CLI 独有示例更新
  （`diff` 因新增映射不再是 CLI 独有）；另 `get_metrics` 从 `build_context`
  分类迁至 `diagnostics`（工具列表 + 两处 scope 文案）。
- `docs/mcp_tools.md`：经 `--emit-mapping` 重新生成「CLI↔MCP 命名映射
  对照表」与「[1]-[17] 域内 CLI 独有命令」两块（`diff`、`health-report`
  离开 CLI 独有命令清单）；经 `--emit-mcp` 重新生成概览表；正文新增
  「daemon 运行时指标工具（[12] Diagnostics）」章节承载原属「构建上下文
  感知」小节的 `get_metrics` 说明块，并同步手写「各分类工具清单」计数
  （[12] 26→27、[13] 9→8）；另修正「场景→工具索引」手写表 11 处 CLI 列与
  `TOOL_CLI_MAPPING` 对齐（`file_symbol_content`→—、`get_symbol_content_by_hash`
  →`cw diff`、`blast_radius`→`cw impact`、`get_recent_changes`→`cw changes`、
  `review_readiness`→`cw review`、`guardrail_list_rules`→`cw guardrail rules`、
  `get_comment_from_version`→`cw restore-comment`；`get_impact`/
  `cross_layer_impact`/`get_symbol_history`/`guardrail_check_edit` 标 MCP 专属）。
- `cli/categories.py`：修正 2 处文案——[13] build_context scope 删残留
  「指标」；`tests` default_desc 的假子命令 `case/coverage` 改为真实 flag 面
  （`--reverse/--build/--history/--import`）。
- `docs/cli_reference.md`：修正过时 flag——`daemon metrics` 章节（`--local`
  → `--from-file`）、HTTP transport 段（`cw daemon serve --http-bind` →
  `cw-daemon.exe --http-bind`）、evolution（删 `--window-commits`）、
  tests（删 `--coverage`）、task report（`--success` → `--result`）；经
  `--emit-cli` 重生成命令概览（[13] 行去掉「指标」）。
- `TOOLS.md`：修正 12 处过时 CLI 路径/计数——`cw impact <QN>`→`<hash>`、
  `cw git log [--author/--since]`→`[limit]`、`cw symbol-history <NAME>`→
  `<hash>`、`cw symbol comment-from-version`→`cw restore-comment --preview`、
  `cw guardrail list`→`rules`、`cw daemon serve`→`start`、`defect import/add`
  →`defect learn/build`、`refresh --watch`→`watch`，以及 `defect cross-layer`/
  `guardrail check-edit`/`get_test_coverage_summary` 3 处标 MCP 专属、
  MCP 工具分组计数 237→243（改「节选」并指向 `docs/mcp_tools.md`）。
- `docs/design/mcp-cli-parity-audit.md`：本报告（新建）。
- `.temp/rpc_cross_check.py`（RPC 交叉比对脚本，含结果
  `rpc_cross_check.json`）、`.temp/gen_audit_table.py`（本报告数据底料
  生成，含完整性断言）保留备查。

**验证命令与结果**：

```
$ python scripts/gen_category_overview.py --emit-mapping
已写回 mapping: docs\mcp_tools.md
已写回 cli-only: docs\mcp_tools.md

$ python scripts/gen_category_overview.py --check
CHECK OK: cli_reference.md（84 命令 / 21 分类）与 mcp_tools.md（243 工具 / 17 分类）概览表、映射表（153 有 CLI 对应 / 90 MCP 专属）、CLI 独有命令表均与真相源一致

$ python -m pytest tests/test_category_source.py tests/test_cli_docs_structure.py
36 passed in 1.78s
```

**相关真相源与校验**：`server/tools/_categories.py`（TOOL_CATEGORIES /
TOOL_CLI_MAPPING）、`cli/categories.py`（COMMAND_CATEGORIES）、
`tests/test_category_source.py::TestToolCliMapping`（键覆盖、顶层命令存在、
值形状、子命令静态存在性）、`scripts/gen_category_overview.py`
（--emit-mapping / --check 字节级漂移校验）。
