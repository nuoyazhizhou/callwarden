# prerequisite-freeze — receipts 审计前置固化清单

- **feature**：receipts 审计：build_graph 成功/失败/转后台均落可验证审计回执
- **R 阶段**：R0 前置固化边界定义（scope 边界，规划职责）
- **生成时间**：2026-11-24（team-lead 追加任务时点）
- **依据基线**：主仓 HEAD=`1fbcd9bad21d58b7ed76e7e1710883213d5495cc`；origin/master=`359d56ee8c40b3548246cf3b599511e0344d09a6`；merge-base=`f5401787bd48bd7d7869b9a86f802d738a82fd91`
- **任务**：`T-1795466935294-3433ca6c`
- **性质**：纯只读核验结论；未写生产代码、未 `git add`/`commit`、未 claim 任务

## 0. 结论速览（给 team-lead 直接决策）

1. **分支分叉实证**：HEAD 领先 merge-base 3 提交、origin/master 领先 **35** 提交，二者已分叉；但**本任务前置的 4 个已存在 Rust 文件在 HEAD 与 origin/master 上 blob 完全一致**（`git diff --stat HEAD origin/master -- <4文件>` 为空，且 merge-base 之后两分支均未提交过它们）。
2. **可安全固化**：前置改动 = 工作树相对 HEAD 的那份增量（13/4、198/5、2/0、63/1 行 + build_flight.rs 新增 235 行），**不含对 origin/master 既有内容的覆盖/回退**。因为两基线上这 4 文件相同，这份增量无论落到 HEAD 还是 origin/master 新基线，结果一致，**不会覆盖或回退 origin/master 的 35 提交里的任何既有改动**（前提：那 35 提交不碰这 4 文件——已实证确认）。
3. **审计基础设施全部已固化**：`write_audit_events` DDL、`sign_audit_chain`、`handle_audit_verify_chain`、`audit_log_handlers::handle_query` 所在文件在 origin/master **均已存在且无在途改动**，无需固化。
4. **必须固化白名单**：6 个文件（见 §3）。若 executor 需端到端跑"工具触发 build_graph 收敛"，追加第 7 个 `server/tools/tools_workspace.py`；但它不是 Rust 回执写入的编译/运行前置。
5. **必须排除**：其余约 39 个在途文件（pytest 基线修复 / response_budget / db_risk / i18n / skills 文档 / MCP tool profile / 各类无关测试）与本任务前置无关，禁止纳入前置 commit。
6. **注意一处语义夹带**：`dispatch.rs` 与 `snapshot_state.rs` 的在途改动里含一处 instance→workspace_id 权威映射的覆写逻辑（`resolve_instance_workspace_id`），它不是 build_flight 本体，但**是 async 路由首轮调用不误判的运行时前置**，必须与 4 文件一起固化，不能单独剔除。

## 1. 符号 → 文件 → 新增/已有（按符号依赖的固化清单）

### 1.1 核心前置（必须固化）

| 符号 | 所在文件 | 新增 or 已有 | origin/master 状态 | 作用 |
|---|---|---|---|---|
| `build_flight::BuildFlights`（struct） | `rust_ext/src/daemon/build_flight.rs` | **新增文件**（235 行，未跟踪 `??`） | 不存在 | 单飞协调器，per-workspace 构建登记 + lastBuild |
| `BuildFlights::begin/finish/progress/last/is_running` | 同上 | 新增 | 不存在 | 本任务要在 `finish` 出口扩展审计回执 |
| `BuildProgress` + `snapshot()` | 同上 | 新增 | 不存在 | scanned/inserted/unchanged/deleted 等统计载荷 |
| `build_max_files()` / `build_wait_bound()` | 同上 | 新增 | 不存在 | bounded/超时预算（env 解析） |
| `pub mod build_flight;` | `rust_ext/src/daemon/fs_handlers.rs:36` | 新增（在途 +） | 文件存在、无此模块声明 | 把 build_flight 接入编译 |
| `fs_handlers::run_build_graph` | `fs_handlers.rs:1382` | 新增（从原 `handle_build_graph` 拆出） | 文件存在、无此函数 | **本任务回执写入点之一**（成功/失败 result 构造 :1585-1598） |
| `fs_handlers::handle_build_graph_async` | `fs_handlers.rs:1611` | 新增（在途） | 文件存在、无此函数 | **本任务回执写入点之一**（成功/失败/超时三出口 :1701-1736）；冻结 step 1 的 target_symbol |
| `handle_build_graph` 改造（dry_run/bounded/single-flight 参数） | `fs_handlers.rs:1331-` | 改造（在途，含 5 行删除替换） | 文件存在、旧版简单签名 | async 入口的同步包装 |
| `DaemonState::build_flights` 字段 + `Default` 初始化 | `rust_ext/src/daemon/dispatch.rs:206` | 新增（在途 13+/4-） | 文件存在、无此字段 | daemon 级共享 Arc，fs_handlers 与 snapshot_state 共用 |
| `SnapshotDaemonState::resolve_instance_workspace_id` 覆写 | `rust_ext/src/daemon/snapshot_state.rs:797` hunk | 新增（在途 62 行 hunk） | 文件存在、无此覆写 | **语义夹带但必需**：registry 权威 instance→workspace_id 映射，否则 async 首轮调用被误判伪造实例 |
| `SnapshotDaemonState` 路由 `handle_build_graph` → `handle_build_graph_async(&self.base.base.build_flights, ...)` | `snapshot_state.rs:3540` hunk | 改造（在途 63+/1-） | 文件存在、旧版调同步 handle_build_graph | 把 async handler 接入 daemon 实际路由 |
| `DaemonState` 测试构造体加 `build_flights: Arc::default()`（2 处） | `rust_ext/src/daemon/http_server.rs:6150/6865` | 新增（在途 2+/0-） | 文件存在、测试构造体无此字段 | 测试编译前置（不加则 Rust 测试编译失败） |

### 1.2 审计基础设施（已固化，无需纳入前置 commit）

| 符号 | 所在文件 | origin/master 状态 | 在途改动 | 判定 |
|---|---|---|---|---|
| `write_audit_events` DDL + append-only 触发器 + `idx_write_audit_events_*` | `rust_ext/src/daemon/fs_handlers.rs:2325-2416` | **已存在**（文件两基线一致） | 该段无改动（在途 diff hunk 在 1331/1382/797/3540，不含 2325 段） | **已固化在基线**，本任务直接复用 |
| `append_write_audit_inner`（含 `sign_audit_chain` 调用 + SAVEPOINT） | `fs_handlers.rs:2569-2632` | 已存在 | 无改动 | 已固化 |
| `WRITE_AUDIT_CHAIN_TABLE = "remote_write_events"` | `fs_handlers.rs:2419` | 已存在 | 无改动 | 已固化 |
| `sign_audit_chain` | `rust_ext/src/cli/task.rs:3768` | 已存在（文件 origin/master 存在、无在途改动） | 无 | 已固化 |
| `handle_audit_verify_chain` | `rust_ext/src/daemon/query_compat_handlers.rs:2104` | 已存在（无在途改动） | 无 | 已固化 |
| `verify_audit_chain` | `rust_ext/src/cli/security.rs` | 已存在（无在途改动） | 无 | 已固化 |
| `audit_log` DDL + `handle_append` + `handle_query(start_time/end_time/event_type)` | `rust_ext/src/daemon/audit_log_handlers.rs` | 已存在（无在途改动） | 无 | 已固化（方案 B 被否的参考面，本任务不改） |

### 1.3 客户端侧前置（分层标注，非 Rust 回执写入前置）

| 符号 | 所在文件 | 新增/已有 | 判定 |
|---|---|---|---|
| `_poll_build_result` / `_build_poll_timeout_secs` / `_BUILD_POLL_*` | `server/tools/tools_workspace.py`（在途 +96/-4，全部 build 相关，无夹带） | 新增（函数）；文件已有于 origin/master | **间接前置**：不影响 Rust 编译与回执写入，但 executor 若用 `workspace.build_graph` 工具端到端验证三条路径收敛，需要它。建议与 6 文件一并固化（低风险、语义完整），或由 executor 在 worktree 内确认 |
| `build_graph(dry_run=...)` 工具签名扩展 | 同上 | 改造（在途） | 同上 |
| `tests/test_build_graph_async_client.py`（4 个轮询单测） | 未跟踪 `??`（89 行） | 新增文件 | 客户端测试；建议一并固化，供 AC-03 证据 |

## 2. 分叉与覆盖风险实证（git diff 证据）

```
$ git rev-parse HEAD origin/master ; git merge-base HEAD origin/master
HEAD         = 1fbcd9bad21d58b7ed76e7e1710883213d5495cc
origin/master= 359d56ee8c40b3548246cf3b599511e0344d09a6
merge-base   = f5401787bd48bd7d7869b9a86f802d738a82fd91
$ git rev-list --left-right --count HEAD...origin/master
3   35        # HEAD 领先 3，落后 35

# 4 个已存在前置文件：merge-base 后两分支均无提交
$ git log --oneline f5401787..origin/master -- <4文件>   → 空
$ git log --oneline f5401787..HEAD           -- <4文件>   → 空

# HEAD 与 origin/master 的 4 文件 blob 一致
$ git diff --stat HEAD origin/master -- <4文件>          → 空

# 工作树相对两基线的增量数字相同（说明是同一份增量，与基线无关）
$ git diff --numstat origin/master -- <4文件>
13  4   dispatch.rs
198 5   fs_handlers.rs
2   0   http_server.rs
63  1   snapshot_state.rs
$ git diff --numstat HEAD -- <4文件>                     → 完全相同

# 删除行性质（均为被新代码替换，非回退 origin 内容）
dispatch.rs    删除 4 行 = resolve_instance_workspace_id 的旧注释段落（改写为权威映射说明）
fs_handlers.rs 删除 5 行 = 旧 handle_build_graph 的 3 行参数解析 + 2 行 scan 初始化（被新结构替换）
snapshot_state 删除 1 行 = 旧 `fs::handle_build_graph(...)` 调用（替换为 async 调用）
http_server.rs 删除 0 行
```

**判断**：满足"可安全作为前置 commit 落地"条件。因为：
- 4 文件在两基线内容相同 → 固化结果不依赖选哪条基线；
- merge-base 后 origin/master 的 35 个提交**未触碰**这 4 文件 → 不存在"覆盖 origin 既有改动"的风险；
- 删除行全部是 build_flight 重构内部的替换，不是把 origin/master 的内容往回退；
- build_flight.rs 是纯新增文件，无覆盖风险；
- 唯一需要意识到的语义夹带是 `resolve_instance_workspace_id` 覆写（§0 第 6 点），它必须与 4 文件同批固化，不可拆分。

**残留风险（需 team-lead 知悉）**：本次核验以"当前工作树快照"为准。若 origin/master 在固化前继续前进并触碰这 4 文件，需重跑 §2 的 `git diff --stat HEAD origin/master -- <4文件>` 确认为空后再固化。另 `dispatch.rs` 的 CRLF→LF 警告是换行规范化提示，非内容风险。

## 3. 必须固化文件白名单（精确）

### 3.1 核心白名单（6 个，Rust 编译 + daemon 运行前置）
1. `rust_ext/src/daemon/build_flight.rs` —— 新增文件（235 行）
2. `rust_ext/src/daemon/fs_handlers.rs` —— 在途 +198/-5（pub mod + run_build_graph + handle_build_graph_async + 改造）
3. `rust_ext/src/daemon/dispatch.rs` —— 在途 +13/-4（build_flights 字段 + resolve_instance 注释）
4. `rust_ext/src/daemon/snapshot_state.rs` —— 在途 +63/-1（resolve_instance 覆写 + async 路由）
5. `rust_ext/src/daemon/http_server.rs` —— 在途 +2/-0（测试构造体字段）

> 注：上列为 5 个，team-lead 已知清单里也是"五个 Rust 文件"，一致。第 6 个是下面客户端侧。

### 3.2 建议一并固化（客户端闭环，2 个，非 Rust 编译前置）
6. `server/tools/tools_workspace.py` —— 在途 +96/-4（build_graph dry_run + _poll_build_result）
7. `tests/test_build_graph_async_client.py` —— 新增文件（89 行，轮询单测）

### 3.3 必须排除（与本任务前置无关，禁止纳入）
- **pytest 基线修复类**（22 个）：`tests/convergence/conftest.py`、`test_b_p7b_integration.py`、`test_c17_admin_route_workspace_authority.py`、`test_cli02_search_daemon_only.py`、`test_cli_004/02/054/056/061/064_http_rpc.py`、`test_cli_refresh_multi_path.py`、`test_cli_task_c9_subcommands.py`、`test_mcp_compat_tools_security_http_rpc.py`、`test_mcp_compat_tools_summary_http_rpc.py`、`test_mcp_find_similar_functions_http_rpc.py`、`test_mcp_get_project_dependencies_http_rpc.py`、`test_mcp_parse_codeowners_http_rpc.py`、`test_phase4_daemon_client.py`、`test_query_file/grep/issues/symbol/tests_rpc.py`、`test_rust_cli_diff.py`、`test_task_next_action_e2e.py`、`test_tools_task_create_workspace_params.py`、`test_workspace_rpc_http.py`、`test_workspace_snapshot_rpc_http.py`、`test_workspace_write_rpc_http.py`（其中 `test_c21_*` 的 "accepted" 是治理语义命中，非 build）
- **独立特性在途**：`server/response_budget.py` + `tests/test_mcp_response_budget.py`、`db/db_risk.py` + `tests/test_db_risk.py`、`server/tools/__init__.py`（MCP tool profile）、`db/db_impact.py`、`server/daemon_client.py`（无 build 改动）、`server/tools/tools_task.py`
- **i18n/文档/清单**：`i18n/en_US.json`、`i18n/zh_CN.json`、`docs/testing/manifests/cli/cli_tree.json`、`AGENTS.md`、`.agents/skills/cw-executor-senior-engineer/SKILL.md`、`scripts/refresh_shared_runtime.ps1`、`docs/reports/testcode-11-repo-analysis-2026-11-23.md`、`docs/reports/web-observer-mvp-executor-evidence-20261124.md`、`deliverables/software-company/server_authority_residue_audit.json`、`register_callwarden_skills.ps1`
- **本设计工件**：`.design/receipts-audit-T-1795466935294-3433ca6c/`（规划产物，非生产前置）

## 4. 给 team-lead 的操作建议（不代执行）

1. 用 §3.1 的 5 文件（+ §3.2 两个客户端文件）作为前置 commit 的唯一 add 集合，**按路径精确 `git add <path>`**，禁止 `git add .` / `git add -A`（工作树还有 40+ 无关在途文件，规则 40/48）。
2. 前置 commit 落在新基线后，executor 从该新基线建 worktree（独立 `agent_instance_id`/`session_id`），receipts 审计只改 `build_flight.rs` + `fs_handlers.rs` 的审计写入点。
3. 固化前按 §2 末尾"残留风险"重跑一次 `git diff --stat HEAD origin/master -- <4文件>` 确认为空。
4. 若 team-lead 决定只固化 5 个 Rust 文件（排除客户端两文件），需在 executor handoff 里说明：`workspace.build_graph` 工具端到端验证可能受 Python 层在途状态影响，AC-03 的证据采集应优先走 Rust 单测 + `dry_run` RPC 直连路径。
