# CLI-081 执行闭环证据（2026-08-24）

## 任务
- **task_id**: `T-1787322799532-d51d9d18`
- **title**: CLI-081 [cli_command_projection]：cw local-completion-review → Rust daemon HTTP thin client
- **父任务**: `T-1787293451688-c14b1e44`
- **身份**: implementer-workbuddy-v1（session 8dadb119-a307-43bc-bb2f-b81b3b563196，role implementer）

## 阶段一：角色独立性门禁（用户已修复，claim 成功）
- 用户修复 `task_collab.rs` 第 1663 行 `(?2 != '' AND agent_instance_id = ?2)` 后，
  `cw task next T-1787322799532-d51d9d18`（agent-id implementer-workbuddy-v1）一次 claim 成功，
  未再报 `E_ROLE_INDEPENDENCE_VIOLATION`。任务进入 in_progress（step0）。

## 阶段二：开发与自测（4 步 report 全 success）
- **step0 `port_or_verify_rust`**：核验 `task.completion_review` 链路——
  - `dispatch.rs` 2684 行路由 `"task.completion_review" => state.handle_task_completion_review(peer, params)`；2060 行 capability 已注册。
  - `task_collab.rs::handle_task_completion_review`（15116 行）为真实 Rust 实现：零步骤返回
    `{task_id, decision:"blocked", reason:"E_NO_STEPS", findings:[block finding]}`；否则聚合未解决
    `task_quality_findings`，有 error/block → decision="block" 否则 "pass"，返回 `{task_id, decision, findings}`。
  - 注意：任务卡 Rust target 写的是 `cli_local_completion_review_handlers.rs::handle_run_task_completion_review`，
    该文件不存在，实际实现在 `task_collab.rs`（历史命名漂移，功能等价）。无 Rust 改动需求。
  - **字段兼容**：Rust 返回无 Python 预期的 `summary`/`counts`；Python 输出层 fail-soft
    （`counts` 缺省空 dict、`summary` 缺省空串均不打印），天然兼容。
- **step1 `thin_cli_client`**：`cli/main.py` 5269 行 completion-review 分支——
  - 移除 `db.run_task_completion_review` 本地业务路径；`_local_completion_review` 改为 forbidden 回调
    （raise DaemonUnavailableError）。
  - 仅经 `route_task_write("task.completion_review", {task_id, step_id}, _local_completion_review)` 走 daemon。
  - 解包 HTTP client `call_with_autostart` 的 `{result, degraded}` 包装（与 `route_task_read` 一致）；
    `degraded=True` fail-closed 提示；`DaemonUnavailableError` fail-closed 提示；`DaemonRemoteError`
    （method_not_found 等）原样格式化。补 `blocked` 决策红色映射。
- **step2 `fixture_negative_matrix`**：`tests/test_cli_081_http_rpc.py` 新增 10 项全部通过（success /
  no-step-id / invalid input / error dict / wrong authority / daemon unavailable / HTTP wrapper 解包 /
  HTTP degraded fail-closed / restart 一致 / local fallback forbidden）。
- **step3 `evidence_and_dependency_verify`**：
  - source scan：completion-review 分支已无 direct DB、UnixDaemonRpcClient、本地分析器调用。
  - MCP 依赖：`mcp_dependencies=[]`（任务卡确认），无 MCP 卡需要 applied。
  - manifest：`aprime_cli_residue_manifest.json` CLI-081 条目已存在（card_key/command/handler/direct_calls）。
    按契约不写 "migrated"（由 reviewer 独立 review evidence 后确认）。
  - 端到端：`cw task completion-review T-1787322799532-d51d9d18` → `Review decision: pass`；
    `--step-id` 透传；不存在的任务 → `blocked` + E_NO_STEPS finding 格式化输出。
- 4 步 `cw task report` success → 任务自动 `in_progress → review`。

## 阶段三：handoff（实际尝试，被 lease 门禁拒绝，记录并跳过）
- 尝试 `cw task handoff T-1787322799532-d51d9d18 --from-role executor --outcome
  executor_ready_for_review --next-role reviewer ... --evidence-hash
  230c22d53f448018b4a7be736dcecb641f7bca604939ff7784f126fd382d6ef9`
  → `E_LEASE_REQUIRED: task.apply/task.close 必须携带完整 reviewer lease 凭证（lease_token + fencing_counter）`。
- 根因：Rust `handle_task_handoff`（task_collab.rs 4122）强制 P4 lease 凭证；executor claim
  走 Rust CLI 直写 task_steps（无 P4 lease），名下无 active executor lease，handoff 无法携带合法凭证。
- 先例：CLI-077（dc14936）/ CLI-078（3863d72）/ CLI-080（da1e7cb）均未执行 handoff，
  仅 report → review 即完成闭环。本任务按同一先例处理：任务已在 review，跳过 handoff，进入 git 提交。

## 阶段四：图刷新（cw --refresh-all）——环境既有缺口，已记录跳过
- 同 CLI-080：`cw --refresh-all` → daemon `method_not_found: build_full_graph`；
  原生 `workspace.build_graph` 的 `codegraph_db_path_template` 为 Linux 路径，Windows 不可用。
  两路刷新失败均属环境/契约既有缺口，非 CLI-081 引入，记录跳过，不影响提交。

## 门禁纪律
- 状态变更一律走 `cw` 命令（claim/report 均通过 daemon）；未用 SQL 改任何状态。
- daemon 报 `E_LEASE_REQUIRED`、`method_not_found: build_full_graph` 已记录，按纪律跳过，不死循环。
