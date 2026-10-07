# T9 叶子级 CLI↔MCP 对照 + 不一致清单

> 日期：2026-10-07
> 方法：三源交叉比对（真实工具名 tool_migration_matrix.json / CLI inventory / docs C8 Step#6 映射表），
> 脚本 t9_leaf_mapping.py 可复现。配套叶子级思维导图见 artifact。
> 目的：为"① 清理 alias/重复入口、消除二义性"提供精确清单。

## 1. 结论速览

| 类别 | 数量 | 性质 | 处理 |
|------|------|------|------|
| 文档名漂移 | 2 | **真 bug**（P1 改名后映射表未同步） | ✅ 本轮已修 |
| CLI `--flag` 重复入口 | 32 | 同一能力 kebab subcommand + 旧 --flag 并存（二义性/同义词） | ⬜ 待 ① 清理 |
| 同能力双 subcommand | 1 | `cw gc archive` 与 `cw gc retention` 同指 gc_retention | ⬜ 待 ① 评估 |
| 映射表漏登真实工具 | 22 | 映射表未覆盖（部分纯 MCP，部分可能漏标 CLI） | ⬜ 待 ① 补登/标注 |
| 合理单边（纯 MCP / 纯 CLI） | 多 | 架构分工（LSP/安装运维等） | 保留，明确标注 |

## 2. 文档名漂移（已修）

P1 把 MCP 工具改名后，docs C8 Step#6 映射表遗漏同步：

| CLI | 映射表旧写（错） | 真实注册名 | 状态 |
|-----|-----------------|-----------|------|
| `cw --detect-cycles` | `detect_cycles` | `detect_call_cycles` | ✅ 已修 |
| `cw dependency cycle` | `detect_cycle` | `detect_dependency_cycle` | ✅ 已修 |

（脚本另报 7 个"漂移"如 get_role_view→task_id，经核实为解析器对"纯 MCP 行"（CLI 列为 —）的列错位误判，非真漂移。）

## 3. CLI `--flag` 重复入口（32 项，待 ① 清理）

每项都是「新 kebab subcommand」+「旧 `--flag` 别名」并存，指向同一 MCP 工具。
项目未上线、不留同义词原则下，**建议保留 subcommand 形式、移除 `--flag` 别名**。

| # | 保留（subcommand） | 移除（--flag 别名） | 指向 MCP |
|---|------|------|---------|
| 1 | `cw refresh all` | `cw --refresh-all` | build_graph |
| 2 | `cw refresh <paths>` | `cw --refresh` | refresh_file |
| 3 | `cw symbol history <name>` | `cw --history` | get_symbol_history |
| 4 | `cw file read` | `cw --file` | file_read |
| 5 | `cw file grep` | `cw --search` | file_grep |
| 6 | `cw callers <name>` | `cw --callers` | get_callers |
| 7 | `cw callees <name>` | `cw --callees` | get_callees |
| 8 | `cw call-chain <name>` | `cw --call-chain` | get_call_chain_down |
| 9 | `cw topo` | `cw --topo` | get_topological_order |
| 10 | `cw metrics` | `cw --metrics` | get_code_metrics_summary |
| 11 | `cw complexity` | `cw --complexity` | get_complexity_hotspots |
| 12 | `cw coupling` | `cw --coupling` | get_coupling_analysis |
| 13 | `cw fn-metrics <name>` | `cw --fn-metrics` | get_function_metrics |
| 14 | `cw largest-fns` | `cw --largest-fns` | get_largest_functions |
| 15 | `cw coupled-fns` | `cw --coupled-fns` | get_most_coupled_functions |
| 16 | `cw git import` | `cw --git-import` | import_git_history |
| 17 | `cw git log` | `cw --git-log` | get_git_commits |
| 18 | `cw git show <hash>` | `cw --git-show` | get_commit_changes |
| 19 | `cw git stats` | `cw --git-stats` | get_git_stats |
| 20 | `cw semgrep scan` | `cw --semgrep` | run_semgrep_scan |
| 21 | `cw semgrep list` | `cw --semgrep-list` | get_semgrep_findings |
| 22 | `cw semgrep stats` | `cw --semgrep-stats` | get_semgrep_stats |
| 23 | `cw function-issues` | `cw --function-issues` | find_issues |
| 24 | `cw coverage comment` | `cw --comment-coverage` | get_comment_coverage |
| 25 | `cw coverage uncommented` | `cw --uncommented` | get_uncommented_symbols |
| 26 | `cw coverage test` | `cw --test-coverage` | get_test_coverage |
| 27 | `cw coverage import` | `cw --coverage-import` | import_coverage |
| 28 | `cw coverage fn <name>` | `cw --coverage-fn` | get_coverage_for_symbol |
| 29 | `cw coverage uncovered` | `cw --coverage-uncovered` | find_uncovered_functions |
| 30 | `cw who <path>` | `cw --who` | who_to_ask |
| 31 | `cw ownership-map` | `cw --ownership-map` | get_ownership_map |
| 32 | `cw symbol restore-comment` | `cw --restore-comment` | restore_comment |
| 33 | `cw symbol restore-all-comments` | `cw --restore-all-comments` | restore_all_comments |

> 另有一批纯 `--flag`（无 subcommand 新形式）：`cw --impact` / `cw --top-callers` /
> `cw --orphan-symbols` / `cw --deepest` / `cw --module-calls` / `cw --call-heatmap` /
> `cw --export-module-graph` / `cw --changes` / `cw --git-blame` / `cw --issue-summary` /
> `cw --symbol-content-by-hash`。这些**没有对应 subcommand**，清理前需先补 subcommand 形式
> 再移除 flag，否则会丢失 CLI 入口。属 ① 的二期。

## 4. 同能力双 subcommand（1 项）

| 子命令 A | 子命令 B | 同指 MCP | 说明 |
|---------|---------|---------|------|
| `cw gc archive` | `cw gc retention` | gc_retention | 两个子命令指向同一能力，需确认是否合并或语义区分 |

## 5. 映射表漏登的真实工具（22 项，待补登/标注）

这些工具在真实注册表中存在，但 docs C8 Step#6 映射表未覆盖。需逐个标注"纯 MCP"或补 CLI 对应：

- **异步 job 面（纯 MCP 合理）**：`cancel_job`、`wait_for_job`、`list_jobs`、`embed_symbols_async`、`scan_semgrep_incremental`
- **任务治理查询（纯 MCP 合理）**：`get_commit_tasks`、`get_task_commits`、`get_symbol_change_tasks`、`task_governance_projection`、`task_remediation_create`、`task_step_resolve`、`task_assignment_status`、`task_assignment_heartbeat`、`task_get_role_prompt`
- **diff 对比（可能漏标 CLI）**：`diff_callers`、`diff_callees` — 有 `cw callers`/`cw callees` 但 diff 变体是否有 CLI？需核
- **其他**：`build_directory`（对 `cw workspace build-dir`?）、`remove_file`、`compare_snapshots`、`get_clone_aware_impact`、`get_job_stats`/`get_job_status`

## 6. 合理单边（保留，不强求一对一）

- **纯 MCP（合理）**：LSP 实时能力（lsp_hover/definition/references/diagnostics/completion/check_available，编辑器协议专属）；细粒度查询（get_role_view/get_freshness_status/get_action_identity 等）；异步 job。
- **纯 CLI（合理）**：install/install-agent（24 IDE）/install-hook/setup/doctor/server（人机安装运维）；daemon 运维 22 子命令；experiment 治理 13 子命令；rollback 迁移。

## 7. 给 ① 的执行建议（按风险/价值排序）

1. **P1（已修）** 文档漂移 2 处 → 已对齐 detect_call_cycles/detect_dependency_cycle。
2. **① 核心** 清理 33 个 `--flag` 别名（§3 表），统一为 subcommand。改动面：cli/main.py 的 argparse flag 定义 + 文档。需你确认后执行。
3. **① 二期** 11 个纯 `--flag`（无 subcommand）先补 subcommand 再移 flag。
4. **① 收尾** 映射表补登 22 个漏登工具 + 标注纯 MCP；评估 `cw gc archive` vs `cw gc retention` 是否合并。
5. 合理单边保留，映射表显式标注"纯 MCP/纯 CLI + 原因"。

本报告为分析/清单，§2 的文档漂移已修，其余待 ① 逐项执行。
