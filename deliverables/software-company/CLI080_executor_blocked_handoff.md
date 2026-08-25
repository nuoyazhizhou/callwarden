# CLI-080 执行闭环证据（2026-08-24）

## 任务
- **task_id**: `T-1787322799482-d215a638`
- **title**: CLI-080 [cli_command_projection]：cw local-commits → Rust daemon HTTP thin client
- **父任务**: `T-1787293451688-c14b1e44`
- **身份**: implementer-workbuddy-v1（session 8dadb119-a307-43bc-bb2f-b81b3b563196，role implementer）

## 阶段一：角色独立性门禁（已由用户修复，claim 成功）
- 此前（2026-08-23）：`check_role_independence` 以 `agent_instance_id = ?2` 判定共享，
  `implementer-workbuddy-v1` 与 `reviewer-w2-3` 的 instance 均为空串 `''` → `''=''` 误判共享，
  claim 被 `E_ROLE_INDEPENDENCE_VIOLATION` 拒绝。
- 用户修复：`rust_ext/src/daemon/task_collab.rs` 第 1663 行查询条件改为
  `(?2 != '' AND agent_instance_id = ?2)` —— 空 instance 不再参与共享判定。
- 修复后 claim CLI-080 成功（task_events event 3499/3500/3502，in_progress）。

## 阶段二：开发与自测（4 步 report 全 success）
- step0 `port_or_verify_rust`：核验 `task.get_commits` 链路
  （dispatch.rs 2693 → task_collab.rs::handle_task_get_commits，行 15713，Rust native；HTTP capability 已注册；返回字段与 Python 消费端对齐）。无 Rust 改动需求。
- step1 `thin_cli_client`：`cli/main.py::_print_task_link_section` commits 段 thin client 化——
  移除 `db.get_task_commits` 本地回退，`_local_commits` 改为 forbidden 回调（raise DaemonUnavailableError）；
  仅经 `route_task_read("task.get_commits", {task_id}, _local_commits)` 走 daemon；
  auto/HTTP/enterprise 不可达 fail-closed 不回退；DaemonRemoteError（method_not_found）fail-soft 跳过。
- step2 `fixture_negative_matrix`：`tests/test_cli_080_http_rpc.py` 新增 5 项通过 +
  CLI-078 回归 4/4 通过（pytest 全绿）。
- step3 `evidence_and_dependency_verify`：矩阵核验 stable；aprime_cli_residue_manifest 条目存在；
  mcp_dependencies=[]；端到端 `cw task show T-1784979928079-e7033874` 经 daemon HTTP 渲染 Commits(3)。
- 4 步均 `cw task report` success → 任务自动 `in_progress → review`（task_events event 3506）。

## 阶段三：handoff 被 lease 门禁拒绝（记录并跳过，不绕过）
- 尝试 `cw task handoff ... --outcome executor_ready_for_review --next-role reviewer ...`
  （2026-08-24 重试，r2）→ `E_LEASE_REQUIRED: task.apply/task.close 必须携带完整 reviewer lease
  凭证（lease_token + fencing_counter）`。
- 根因：Rust `handle_task_handoff`（task_collab.rs 4122）强制 `require_lease_params` +
  `validate_lease_for_mutation`；executor claim 走 Rust CLI 直写 task_steps（无 P4 lease），
  名下无 active executor lease，handoff 无法携带合法 lease 凭证。
- 先例：CLI-077（dc14936）/ CLI-078（3863d72）均未执行 handoff（task_events 无 handoff 记录），
  仅 report → review 即完成闭环。本任务按同一先例处理：报告已在 review，跳过 handoff，进入 git 提交。

## 阶段四：图刷新（cw --refresh-all）——环境既有缺口，已记录跳过
- `cw --refresh-all`（及 `cw refresh --all`）→ `db.build_full_graph` → route_rpc 走
  daemon → `method_not_found: 未知方法: build_full_graph`（daemon 无此 RPC 分支；
  CLI-049 契约"Rust 侧核验"未实际落地 handler）。
- daemon 原生 `workspace.build_graph`（fs_handlers.rs:149，真实实现）在本机不可用：
  `codegraph_db_path_template` 为 `/var/lib/callwarden/workspaces/{ws}/codegraph.db`
  （Linux 路径模板），Windows 上 `unable to open database file`。
- 两路刷新均失败属环境/契约既有缺口，非 CLI-080 引入；按纪律记录跳过，不影响提交。

## 门禁纪律
- 状态变更一律走 `cw` 命令（claim/report 均通过 daemon）；未用 SQL 改任何状态。
- daemon 报 `E_LEASE_REQUIRED`、`method_not_found: build_full_graph` 已记录，按纪律跳过/handoff，不死循环。
- 本文件更新为最终闭环证据，替代此前 2026-08-23 的预检阻塞记录（该阻塞已被用户修复）。
