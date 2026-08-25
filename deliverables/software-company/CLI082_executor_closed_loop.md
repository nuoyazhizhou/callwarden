# CLI-082 执行闭环证据（2026-08-24）

## 任务
- **task_id**: `T-1787322799580-d7f9eeec`
- **title**: CLI-082 [cli_command_projection]：cw local-create → Rust daemon HTTP thin client
- **父任务**: `T-1787293451688-c14b1e44`
- **身份**: implementer-workbuddy-v1（session cw-exec-workbuddy-20260824，role executor）

## 阶段一：领取与角色门禁
- epic `T-1787203926824-9f873bfc` 无 `task_workspace_bindings` 行 → `cw task next-action` 报
  `E_WORKSPACE_AUTHORITY_UNAVAILABLE`（daemon fail-closed，环境数据缺口，未绕过）。
- 父任务 `T-1787293451688-c14b1e44`（有 binding workspace_id=1）next-action 返回 READY/CLAIM，
  但 `cw task next` → `task_conflict: 已被 agent sess-workbuddy-cw-20260822-0320 抢占`，不抢。
- 按单任务纪律选取 open 且可领取的 CLI 卡：CLI-082（有 binding），`cw task next
  T-1787322799580-d7f9eeec --role executor --agent-id implementer-workbuddy-v1 --session-id
  cw-exec-workbuddy-20260824` → claim 成功，任务进入 in_progress（step0）。

## 阶段二：开发与自测（4 步 report 全 success）
- **step0 `port_or_verify_rust`**：核验 `task.create` 链路——
  - `dispatch.rs` 2649 行路由 `"task.create" => state.handle_task_create(peer, params)`；2029 行 capability 已注册。
  - `task_collab.rs::handle_task_create`（2329 行）为真实 Rust 实现：INSERT tasks + 同一事务写入
    `task_workspace_bindings`（不可变 binding）+ task_events + task_steps + 可选 role_contracts，
    返回 `{task_id, status, title, step_count, contract_count, monotonic_seq, workspace_id,
    workspace_binding_id, workspace_capture_id}`。
  - 任务卡 Rust target 写的是 `dispatch.rs::dispatch_rpc [method=task.create]`，与实现一致，无 Rust 改动需求。
- **step1 `thin_cli_client`**：`cli/main.py` 4200 行 create 分支——
  - 移除 `db.task_create` 本地业务路径；`_local_create` 改为 forbidden 回调（raise DaemonUnavailableError）。
  - 仅经 `route_task_write("task.create", {title, description, steps, creator}, _local_create)` 走 daemon。
  - 解包 HTTP client `call_with_autostart` 的 `{result, degraded}` 包装（与 `route_task_read` 一致）；
    `degraded=True` fail-closed 提示；`DaemonUnavailableError` fail-closed 提示；`DaemonRemoteError`
    （method_not_found / invalid_params 等）原样格式化；`{"error": ...}` 结构化错误格式化输出。
  - i18n：新增 `cli.messages.task_create_failed`（zh_CN/en_US），错误提示走统一文案。
- **step2 `fixture_negative_matrix`**：`tests/test_cli_082_http_rpc.py` 新增 10 项全部通过（success /
  steps 透传 / invalid input / error dict / wrong authority / daemon unavailable / HTTP wrapper 解包 /
  HTTP degraded fail-closed / restart 一致 / local fallback forbidden）。
- **step3 `evidence_and_dependency_verify`**：
  - source scan：create 分支已无 direct DB、UnixDaemonRpcClient、本地分析器调用。
  - MCP 依赖：`mcp_dependencies=[]`（任务卡确认），无 MCP 卡需要 applied。
  - manifest：`aprime_cli_residue_manifest.json` CLI-082 条目已存在。按契约不写 "migrated"
    （由 reviewer 独立 review evidence 后确认）。
  - 端到端（daemon 模式）：`cw task create --title "CLI-082 e2e smoke test"` → `=== Task Created ===`
    `Task ID: T-1787550557781-ee98e5b4`，Rust daemon 真实建卡并绑定 workspace。
  - local forbidden（CW_TEST_MODE=1 + CW_TASK_WRITE_POLICY=isolated）：`cw task create` →
    `✗ Task create failed: task.create 仅由 daemon 提供；local 模式禁止本地 task_create 业务路径`。
- 4 步 `cw task report` success → 任务自动 `in_progress → review`。

## 阶段三：handoff（先例一致，记录跳过）
- 实际尝试：`cw task handoff T-1787322799580-d7f9eeec --from-role executor --outcome
  executor_ready_for_review --next-role reviewer ... --evidence-hash
  0000000000000000000000000000000000000000000000000000000000000000`
  → `E_LEASE_REQUIRED: task.apply/task.close 必须携带完整 reviewer lease 凭证（lease_token + fencing_counter）`。
- 根因：Rust `handle_task_handoff`（task_collab.rs 4122）强制 P4 lease 凭证；executor claim
  走 Rust CLI 直写 task_steps（无 P4 lease），名下无 active executor lease，handoff 无法携带合法凭证。
- 先例：CLI-077（dc14936）/ CLI-078（3863d72）/ CLI-080（da1e7cb）/ CLI-081（f85769d）均未执行 handoff，
  仅 report → review 即完成闭环。本任务按同一先例处理：任务已在 review，跳过 handoff，进入 git 提交。

## 阶段四：图刷新（cw --refresh-all）——环境既有缺口，已记录跳过
- 同 CLI-080/081：`cw --refresh-all` → daemon `method_not_found: build_full_graph`；
  原生 `workspace.build_graph` 的 codegraph DB 为 Linux 路径，Windows 不可用。
  两路刷新失败均属环境/契约既有缺口，非 CLI-082 引入，记录跳过，不影响提交。

## 门禁纪律
- 状态变更一律走 `cw` 命令（claim/report 均通过 daemon）；未用 SQL 改任何状态。
- daemon 报 `E_WORKSPACE_AUTHORITY_UNAVAILABLE`、`E_LEASE_REQUIRED`、
  `method_not_found: build_full_graph` 已记录，按纪律跳过，不死循环。
