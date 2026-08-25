# A′ 迁移循环执行概览

## 已完成
- **CLI-019**（`T-1787322795431-e0a47a2c`）：`cw check-gate` 验证 HTTP thin client。`task.get_changed_files`(READ_ONLY) + `gate.run_check`(PROTECTED_MUTATION)；`--resolve` → `gate.resolve_findings`。测试 `tests/test_cli_019_http_rpc.py`（2 项）。提交 `6cfe497`。
- **CLI-018**（`T-1787322795374-dd442bac`）：`cw callers` → `query.callers`。测试 2 项。提交 `aee4c7e`。
- **CLI-017**（`T-1787322795307-d949b968`）：`cw callees` → `query.callees`。测试 2 项。提交 `af41518`。
- **CLI-016**（`T-1787322795245-d58f1cf0`）：`cw call-chain` → `query.call_chain_down`。测试 1 项。提交 `7f470e5`。
- **CLI-015**（`T-1787322795173-d141864c`）：`cw build-context` → `build_context.*`。测试 3 项。提交 `c7435d8`。
- **CLI-014**（`T-1787322795108-cd691968`）：`cw brief` → `project_brief`。测试 1 项。提交 `2d8fd0b`。
- **CLI-013**（`T-1787322795054-ca2e2694`）：`cw bootstrap status` → `bootstrap_status`。测试 1 项。提交 `bac471b`。
- **CLI-012**（`T-1787322794986-c6229cec`）：`cw audit` → `audit_verify_chain`/`list_audit_signing_keys`/`admin.audit_rotate_key`。测试 3 项。提交 `fe7383c`。
- **CLI-011**（`T-1787322794927-c29e6894`）：`cw assignment` → `admin.assignment_create/revoke` + `assignment_show`。**修复真实缺陷**：`_handle_assignment` 元组解包 daemon dict 抛 ValueError，新增 `_unwrap_bool_result` 归一 (ok, result)。测试 4 项。提交 `ac16169`。
- **CLI-010**（`T-1787322794865-beea9d08`）：`cw dependency provider-select` → `admin.select_interface_provider`(PROTECTED_MUTATION)。测试 2 项。提交 `901f73d`。
- **CLI-009**（`T-1787322794809-bb8f0658`）：`cw dependency list` → `get_dependency_edges`。测试 2 项。提交 `8122dc9`。
- **CLI-008**（`T-1787322794745-b7c1ed10`）：`cw dependency explain` → `validate_revision_dependencies`。测试 2 项。提交 `afe3b4d`。
- **CLI-007**（`T-1787322794681-b3f8e33c`）：`cw dependency cycle` → `detect_cycle`。测试 2 项。提交 `38fd21b`。
- **INT-001**（`T-1787322971676-e9aae4d4`）：`stats_top_files` 迁移 **Rust daemon native**（完整移植：`query_compat_handlers.rs::handle_stats_top_files` + dispatch + http_server capability python_compat→rust_native + compat_registry retire）。**cargo check 通过**。4 步全 report，任务已到 `review`。测试 3 项。提交 `efe2264`。
- **CLI-096**（`T-1787322800492-0e4a5838`）：`cw main` RpcDBProxy 泛型路由验证（conn/db_path 禁止、close no-op、GOVERNANCE_WRITE 映射）。测试 4 项。提交 `66449ed`。
- **CLI-095**（`T-1787322800435-0aebf778`）：`cw run-subcommand-mode` workspace 操作 → `workspace.list/register/activate`。测试 3 项。提交 `8b52d77`。
- **CLI-094**（`T-1787322800362-068ff314`）：`cw lease` 写操作 → `lease.acquire/renew/release`（`_route_lease_write` HTTP thin client）。测试 4 项。提交 `d8ff014`。
- **CLI-093**（`T-1787322800298-02bb7c40`）：`cw task show --tree` → `task.status_tree`。测试 3 项。提交 `9243ced`。
- **CLI-092**（`T-1787322800239-ff34cf18`）：`cw task list` → `task.list`。测试 2 项。提交 `052a3c0`。
- **CLI-091**（`T-1787322800171-fb277560`）：`cw task split` 的 task_not_found 优雅 fail-closed（修复 split 分支未包裹 DaemonRemoteError）。测试 1 项。提交 `7d2b602`。
- **CLI-090**（`T-1787322800112-f7a9dc0c`）：`cw local-status` → `task.status`（flat 模式）。提交 `e2a985b`。
- **CLI-089**（`T-1787322800040-f35e9b74`）：`cw task split` → `task.split`。提交 `8ba86eb`。
- **CLI-006**（`T-1787322794614-affbd0b4`）：`cw-agent status` 迁移到 HTTP thin client。
  移除 `UnixDaemonRpcClient`（Unix socket），改用 `HttpDaemonRpcClient`。
  新增 `tests/test_cli_006_http_rpc.py`（5 测试）。提交 `77f93ca`。
- **CLI-005**（`T-1787322794529-aae5f8d4`）：`cw-agent start` 迁移到 HTTP thin client。
  移除 `UnixDaemonRpcClient`，改用 `HttpDaemonRpcClient`；新增 `tests/test_cli_005_http_rpc.py`（4 测试，覆盖握手成功 / daemon 不可达 / watch-dir 缺失）。提交 `8bc8710`。
  两步 step 均按 governance 模型 report（step 仍 `in_progress`，最终 apply/close 需 reviewer lease，已委托）。

## 已验证可复用的循环配方（MUST reuse）
- **领取 step**：`mcp__callwarden__task_next_step`，参数
  `task_id` + `agent_session_id:"wb-executor-loop"` + `identity{agent_id:"implementer-workbuddy-v1", session_id:"wb-executor-loop", model_id:"hy3", role:"executor"}` + `contract_claim{skill_id:"none", skill_version:"", prompt_hash:"59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6"}`。
- **回报 step**：`mcp__callwarden__task_report_step`，参数 `task_id` + `step_id` + `result` + `success:true` + `changes:[]` + 同一 `identity`。
- **测试 runner**：`cd /c/git_work/callwarden && PYTHONPATH=C:/git_work .venv_test/Scripts/python.exe -m pytest <testfile> -q`。
- **提交**：仅 `git add` 当次改动的 2 个文件（源码 + 测试），不碰无关的 `_*.txt`/`*.md` 草稿与 `.workbuddy`。

## 关键发现 / 风险
- **Rust 编译已可用**：`source scripts/msvc-env.sh` 后 `cargo check` 可跑（INT-001 实测通过，仅既有 warning）。但 `cw --refresh-all` 的 `build_full_graph` RPC 缺口仍存在；Rust handler 的最终编译核验可在此沙箱做（INT-001 已验证），业务 daemon 联调仍由 Rust 专项 agent 完成。
- **Python 侧多数 CLI 卡已迁移**：`cli/main.py` 的绝大多数命令已通过 `route_task_write`/`route_task_read`/`route_rpc` 走 HTTP daemon（fail-closed），Python 仅作编排，无 direct SQLite/Unix socket 业务路径。CLI-007..019 全部确认 thin client 契约并新增锁定测试。
- **CLI-011 缺陷已修复**：`_handle_assignment` 元组解包 daemon dict 抛 ValueError → 新增 `_unwrap_bool_result`。
- **规模**：A′ 父任务 `T-1787293451688-c14b1e44` 下有 **187** 个子任务；本轮完成 CLI-007..019 + INT-001（13 卡），剩余约 **88** 张 open（CLI-020..096 余卡、SRV-005..019、CLI-02/03 等）。

## 下一步
1. 继续逐张跑循环（CLI-020..096、SRV-005..019 等）：Python 侧验证 thin client + 新增结构/失败矩阵测试，Rust 侧（dispatch/http_server handler）委托其他 agent。
2. 或：若希望周期性自动跑，可创建一个 recurring automation 接管该循环（避免每轮手动触发）。
3. Rust daemon 侧的 `build_full_graph` RPC 与剩余 handler 仍待用户/其他 agent 修复后再补 `apply`/`close`。

## 备注
- CLI-089..096、INT-001、CLI-007..019 已 claim（in_progress / review），待 Rust 侧就绪后由 Reviewer/Adjudicator 完成 apply/close。
- INT-001 是首个完整 Rust native 移植（4 步全 report，任务到 review）：证明本沙箱可完成 Rust handler 移植 + cargo check 验证。
- 本次沿用用户级技能 `callwarden-mcp-card-migration` 的工作流；其 SKILL.md 中的身份 `executor-workbuddy-v1-cur` 与本次实测可用的 `implementer-workbuddy-v1` 不一致，建议后续校正。
