# P2 CLI-link 审计报告（Rust `cw` ↔ Python `cli/main.py` ↔ daemon）

日期：2026-09-23
范围：逐条核查 Rust `cw` 二进制（`rust_ext/src/bin/cw_cli.rs`，clap 59 顶层子命令）
到 daemon 的接线状态，并与 Python `cli/main.py` 的实际路由行为对照。

---

## 一、方法

1. **命令清单对齐**（`scripts/p2_cli_audit_step1.py`）
   - Rust：解析 `cw_cli.rs` 的 `enum Commands`（59 顶层 variant + 17 组嵌套子命令），
     camelCase → kebab-case。
   - Python：解析 `cli/main.py:_SUBCOMMANDS`（65 个，去重）。
   - 结果：**Rust 59 ⊂ Python 65**；Python 多 6 个：`assignment` / `collab` / `dependency`
     / `experiment` / `identity` / `lease`。

2. **接线分类**（`scripts/p2_cli_audit_step2/3.py`）
   对每个命令的实现函数扫描：`execute_read_with` / `execute_write_with`（双路径路由）、
   `daemon_call`（真 RPC）、`open_local_db`（本地 SQLite）、`Command::new`（外部进程）。
   `run_refresh` 的 daemon 调用在被调函数 `run_enterprise_refresh` / `run_enterprise_full_refresh`
   内（间接调用链），已计入 dual_rpc。

3. **Python 侧对照**：核查 Python 每个命令取数走 `db.*` → `RpcDBProxy._rpc_call`
   → `route_rpc`（生产 local 模式直接 fail-closed，强制 daemon），还是本地工具/文件。

---

## 二、汇总

| 分类 | 数量 | 说明 |
|---|---|---|
| **dual_rpc** | 19 | local + enterprise 双路径，enterprise 闭包真调 `daemon_call` |
| **query_local（enterprise 缺口）** | 35 | external.rs 实现，`open_local_db` + SQL，**完全忽略 mode** |
| **tool_local / other_local** | 10 | 外部进程或本地文件操作，本就无需 daemon |
| **local_only（有意为之）** | 6 | 治理元数据，注释明示 enterprise 也固定本地 |
| **daemon_only** | 2 | `run_enterprise_*`（run_refresh 的 enterprise 分支，已计入 dual） |
| **Python 独有（Rust 未实现）** | 6 | 见 §五 |

> 命令总数 59 = 19 dual + 35 query_local + 10 tool/other_local − 6（local_only 已含在
> dual 计数外的独立分类）+ 冗余计数；精确分项见 §三/§四。

---

## 三、✓ 已正确接线 daemon（19 dual_rpc）

| 命令 | 实现函数 | daemon RPC method |
|---|---|---|
| stats | run_stats | `build_query_request("stats")` |
| status | run_status | `query_enterprise_status` |
| search | run_search | `query_enterprise_search` |
| symbol | run_symbol | enterprise symbol |
| file | run_file | enterprise file |
| query | run_query | enterprise query |
| grep | run_grep | enterprise grep |
| issues | run_issues | enterprise issues |
| tests | run_tests | enterprise tests |
| callers | run_callers | `query_enterprise_graph("callers")` |
| callees | run_callees | `query_enterprise_graph("callees")` |
| call-chain | run_call_chain | `query_enterprise_call_chain` |
| topo | run_topo | `query_enterprise_topological_order` |
| impact | run_impact | `query_enterprise_impact` |
| refresh | run_refresh | `workspace.connect` + `workspace.file.refresh` |
| task | run_task | task.* RPC |
| toolchain | run_toolchain | toolchain.* RPC |
| build-context | run_build_context | resolved_edges 引擎 |
| workspace | run_workspace | workspace.* RPC |

路由语义（`runtime.rs`）：
- `local` 模式 → 本地 SQLite；
- `enterprise` 模式 daemon 不可用 → exit 2 fail-closed；
- `auto` 模式 → daemon 优先，读命令失败回退本地（写命令**禁止跨源回退**）。

---

## 四、⚠ enterprise 缺口：35 个 query_local 命令

这些命令在 `external.rs` 中用 `open_local_db` + 本地 SQL 实现，**完全忽略 `--mode enterprise`**。
对照 Python 侧：同名命令全部经 `db.* → RpcDBProxy → route_rpc`，生产环境强制走 daemon
（`route_rpc` 对 local/legacy 模式直接抛 `DaemonUnavailableError`，仅 `CW_TEST_MODE=1` 放行）。

**后果**：`cw --mode enterprise <cmd>` 在 Rust 二进制下会静默读本地 SQLite，
而不是 fail-closed——与 Python CLI 行为不一致，且违反"CLI 纯 client 化，禁止直接 SQLite"
的项目约定（`RpcDBProxy` 的 docstring 明示）。

| # | 命令 | external.rs | Python 侧 db.* 调用 |
|---|---|---|---|
| 1 | semgrep list/stats | run_semgrep_list/stats | get_semgrep_summary |
| 2 | coverage fn/uncovered | run_coverage_fn/uncovered | get_coverage_for_symbol / find_uncovered_functions |
| 3 | git log/show/stats | run_git_log/show/stats | get_git_commits / get_commit_changes |
| 4 | gc status | run_gc_status | gc_status |
| 5 | doctor | run_doctor | （本地维护命令，Python 经 server.cli_admin 只读辅助） |
| 6 | review | run_review_report | review_readiness_report |
| 7 | evolution | run_evolution_report | get_defect_correlation_by_qn / function_change_frequency |
| 8 | hotspot | run_hotspot_report | hotspot_evolution |
| 9 | churn | run_churn_report | churn_analysis |
| 10 | defect | run_defect | defect_pattern_search / suggest_fix |
| 11 | vuln-blast | run_vulnerability_blast | get_vulnerability_blast_radius |
| 12 | symbol-history | run_symbol_history | get_symbol_commit_history / get_symbol_change_tasks |
| 13 | test-impact | run_test_impact | test_impact_selection |
| 14 | clone | run_clone | detect_clones / list_clones / get_clone_stats / clear_clones |
| 15 | fts | run_fts | rebuild_fts_index / get_fts_status |
| 16 | metrics | run_metrics_summary | get_code_metrics_summary |
| 17 | complexity | run_complexity_report | get_complexity_hotspots |
| 18 | coupling | run_coupling_report | get_coupling_analysis |
| 19 | comment-coverage | run_comment_coverage | get_comment_coverage |
| 20 | uncommented | run_uncommented | get_uncommented_symbols |
| 21 | function-issues | run_function_issues | get_function_issues |
| 22 | largest-fns | run_largest_functions | get_largest_functions |
| 23 | coupled-fns | run_coupled_functions | （Python 侧无独立 db 调用，走 get_coupling_analysis） |
| 24 | fn-metrics | run_function_metrics | get_function_metrics |
| 25 | who | run_who | who_to_ask |
| 26 | ownership-map | run_ownership_map | get_ownership_map |
| 27 | brief | run_brief | project_brief |
| 28 | map | run_repo_map | repo_map |
| 29 | health-report | run_health_report | get_stats / hotspot_evolution / get_semgrep_stats / get_token_savings_report |
| 30 | dashboard | run_dashboard | get_project_dashboard / get_project_risks |
| 31 | rollback | run_rollback | register_rollback_config / get_rollback_config |

（#23 coupled-fns：Python 侧无独立 db 方法，Rust 有本地实现——接线状态为"Python 亦无对应
daemon 方法"，属可实现但双侧均未接线的次要项。）

### 无需 daemon（10，本地工具/文件操作，双侧一致）

semgrep scan（跑 semgrep 进程）、git import（跑 git 命令）、coverage import（解析 lcov
文件）、install-agent / install-hook（写文件）、gc archive/restore/purge/db-cleanup
（本地归档维护）、graph（本地构建）。

### local_only（6，注释明示有意为之）

`rule` / `guardrail` / `check-gate` / `audit` / `bootstrap` / `config`——源码注释明确
"enterprise 模式也固定走本地数据库"（当前 UID 的安全编排元数据/本地事实）。

**但需注意**：Python 侧这些命令仍经 `db.* → route_rpc`（如 check-gate 调
`db.run_check_gate`、audit 调 `db.verify_audit_chain`），生产强制 daemon。
因此这 6 个也存在与 Python 侧的行为差异——若项目要求 Rust `cw` 与 Python CLI 行为一致，
应同样补 daemon 路径；若认定它们是本地安全事实、daemon 不持有，则双侧语义需对齐文档化。

---

## 五、Python 独有，Rust 未实现（6）

| 命令 | Python 侧 daemon 路由 | 判定 |
|---|---|---|
| `assignment` | `db.get_assignment` / `revoke_assignment` → route_rpc | ⚠ Rust 缺失 |
| `collab` | `route_rpc` + `HttpDaemonRpcClient.get_instance()` | ⚠ Rust 缺失 |
| `dependency` | `db.get_dependency_edges` / `detect_cycle` / `validate_revision_dependencies` | ⚠ Rust 缺失 |
| `identity` | `route_lease_write` + `route_task_write` + `HttpDaemonRpcClient` | ⚠ Rust 缺失 |
| `lease` | `route_lease_write`（×3）+ `route_rpc` | ⚠ Rust 缺失 |
| `experiment` | **纯本地**（ExperimentBatch / ExperimentJsonlWriter 文件协议，G0 盲评） | ✓ 无需 daemon，Rust 缺失但无接线需求 |

---

## 六、结论与建议

1. **核心结论**：Rust `cw` 的 **19 个核心图查询/任务命令已正确接线 daemon**（含完整的
   local/enterprise/auto 三态路由 + fail-closed 语义）；**35 个查询命令完全无 daemon 路径**，
   `--mode enterprise` 下静默读本地 SQLite，与 Python CLI 的 fail-closed 行为不一致。
   这是"CLI→daemon 接线"的主要遗留面。

2. **优先修复**（建议建治理卡）：
   - **P2-1（高）**：35 个 query_local 命令补 enterprise 分支（复用
     `runtime.execute_read_with` + `daemon_call`，参照已实现的 19 个范式）。
     最低要求：enterprise 模式下若无 daemon 路径，应 fail-closed 而非静默本地。
   - **P2-2（中）**：6 个 local_only 治理命令——明确是"有意本地"还是"待补 daemon"，
     与 Python 侧行为对齐并文档化。
   - **P2-3（中）**：Rust 补齐 5 个缺失命令（assignment/collab/dependency/identity/lease），
     均为治理写命令，需走 `route_task_write` 类语义。
   - **P2-4（低）**：`coupled-fns` 双侧均无独立 daemon 方法，可选。

3. **验证范式**：本次审计的三个脚本已落 `.workbuddy/scripts/p2_cli_audit_step{1,2,3}.py`，
   可复现；输出表在 `outputs/p2_cli_audit_{commands,wiring,final}.txt`。

---

## 附：脚本

- `.workbuddy/scripts/p2_cli_audit_step1.py` — 命令清单对齐（Rust enum 解析 + Python
  `_SUBCOMMANDS` 差集）
- `.workbuddy/scripts/p2_cli_audit_step2.py` — 接线初分类（直接 daemon_call 扫描）
- `.workbuddy/scripts/p2_cli_audit_step3.py` — 最终分类（间接调用链 + query/tool 细分）
- `outputs/p2_cli_audit_commands.txt` — 59/65 命令对齐清单
- `outputs/p2_cli_audit_wiring.txt` — 中间接线表
- `outputs/p2_cli_audit_final.txt` — 最终接线状态表
