# Tasks：跨项目任务单归属 fail-closed

来源：`docs/specs/cross-project-workspace-binding/spec.md`
执行顺序即依赖顺序；每步完成即验证，失败不进入下一步。

## [x] T1 — 解析器去隐式 PROJECT_ROOT 回退
文件：`server/daemon_client.py`（`resolve_workspace_pair_from_daemon`）
动作：
- 未传 `project_root` 时抛 `DaemonUnavailableError`，消息含 `E_WORKSPACE_ROOT_REQUIRED`。
- 全仓核对调用点：`cli/main.py:4431`（T2 修）、`cli/main.py:16298`（已传 root）、tests。
验证：`python -c "from callwarden.server.daemon_client import resolve_workspace_pair_from_daemon as f; f(None)"` 期望抛错且消息含错误码。

## [x] T2 — CLI create 按调用项目根解析 + 显式冲突拒绝
文件：`cli/main.py`（`_handle_task` create 分支 4420-4461；新增 `_resolve_task_create_project_root`）
动作：
- caller_root = `db.client_workspace_root` → cwd 检测 → fail-closed。
- caller_pair = 解析器(caller_root)。
- 显式 `--workspace-id` / `--workspace-instance-id` 与 caller_pair 不一致 → `E_WORKSPACE_PROJECT_MISMATCH`，打印三方对比，不发起 RPC。
- 保持一致或未给出 → 下发 caller_pair。
验证：见 T7 用例 2/3/4；并在真实 TokenSlim 目录跑一次 create 观察归属。

## [x] T3 — MCP 项目上下文解析 + 启动绑定
文件：`server/_mcp_common.py`（新增 `resolve_mcp_project_root`）、`server/mcp_server.py:50-52`
动作：
- 优先级 `CALLWARDEN_WORKSPACE` → `detect_project_root(os.getcwd())` → `PROJECT_ROOT`。
- `configure_workspace(resolve_mcp_project_root())`。
验证：单测覆盖三级优先级；启动日志可打印解析结果（stderr）。

## [x] T4 — MCP task_create 服务端归属校验/补齐
文件：`server/tools/tools_task.py`（`task_create`）
动作：
- 解析 `ctx_pair`；agent 传入值与 ctx_pair 不一致 → 结构化 `E_WORKSPACE_PROJECT_MISMATCH`（无 RPC）。
- 传入 0/空 → 服务端补 `ctx_pair`。
- 解析失败 → `E_WORKSPACE_ROOT_UNRESOLVED`（fail-closed，绝不回落 callwarden）。
验证：T7 用例 5/6。

## [x] T5 — 安装：项目级 pin 项目根
文件：`cli/main.py`（`_mcp_callwarden_entry` 及项目级/全局级调用点）
动作：
- 新增 `pin_workspace` 形参，项目级写入加 `"env": {"CALLWARDEN_WORKSPACE": root}`；全局合并写入不加。
- 逐个调用点确认 root 语义为项目根；不确定则不加。
验证：T7 用例 7。

## [x] T6 — TokenSlim 权威绑定收敛
动作：
- `cw workspace register --root C:\git_work\TokenSlim`（RPC，禁 SQL）。
- 若 175/1288 重复导致无法唯一收敛 → 在 Rust registry 侧修 `client_view_root` 归一化判重 + 回归测试。
- 证据：`workspace.list` / `workspace.status` 输出写入 `.trae-cn/evidence/`。
验证：`resolve_workspace_pair_from_daemon(r"C:\git_work\TokenSlim")` 返回唯一配对（AC-6）。

**实现期修订（2026-10-28，用户批准「扩展 daemon 做有界再认证」）**：
- 实测 registry 归一化已生效，阻塞在 task-DB 侧 workspace 10 的 legacy `ws-10` capture 链 + 0c 门禁。
- 新增 `verify_canonical_adoption`（legacy-only 链 + registry active + 同 owner + 同归一化 root）与 `bind_task_to_workspace_adopting`（0c 放行、新 instance 的 capture 链从 revision=1 起、supersede legacy 最新 capture），`task.create` 接线。
- 回归：`task_collab_tests_ws_authority.rs` 新增 3 例（正向用例断言首个 canonical capture = revision 1、第二次 create 后链连续）；`cargo test --lib ws_authority` 7 passed、`cargo test --lib daemon::task_collab::tests` 164 passed。
- 部署：`scripts/refresh_shared_runtime.ps1 -TaskId T-1793180472391-931120c4 -RestartMcp -RunSmokeTests`（release → runtime/current → 重启 daemon，pid 41456，hash `1776027521…`）。
- 数据修复：首版「全局最大 + 1」在 workspace 10 留下 canonical capture revision 3/4，已就地重编号为 1/2（capture id / binding 不变），链连续性门禁恢复；`cw task show` 不再报「capture 链断链」。

## [x] T7 — 回归测试
文件：`tests/test_task_create_workspace_binding.py`（扩展）+ 必要时 `tests/test_cross_project_workspace_binding.py`
用例：spec §3.6 的 7 条。
结果：27 passed。

## [x] T8 — 端到端验收与治理闭环
动作：
- AC-1/AC-2：分别在 TokenSlim 与 callwarden 目录实跑 create → show 验证归属。
- AC-3/AC-4：显式跨项目/ MCP 跨项目均被拒。
- 按治理环：report steps → handoff（独立性）→ reviewer verdict（`--view-manifest-hash` 取自 `task prompt --format json`）→ adjudicator lease → apply → close → release；证据落 `.trae-cn/evidence/`。

结果（2026-10-28）：AC-1 PASS（`T-1793182549588-35a1881c`，ws10/`d079f60cac8d6186`）、AC-2 PASS（`T-1793182558214-37c72fdc`，ws1/`4baea3ff12c2ea5c`）、AC-4 PASS（case A 拒绝零落库、case B 补配 ws10）、AC-5 PASS（29 passed）；治理环 `T-1793182868491-75bb7e7c` 全链闭环（verdict `V-5891a9dfd72e3ed6c60f0539`，终态 closed/completed）。证据：`.trae-cn/evidence/T8-cross-project-e2e-20261028.md`。

AC-3 缺陷修复（2026-10-28）：首轮复验发现拒绝成立但 legacy create 退出码为 0（`DaemonUnavailableError` 落入通用 `except` → `return True`）。已在 `cli/main.py` `_dispatch_subcommand` 通用 except 中对 `E_WORKSPACE_PROJECT_MISMATCH` 单独 `SystemExit(2)`（其它异常语义不变），并新增 2 条回归用例（`test_dispatch_project_mismatch_exits_nonzero` / `test_dispatch_other_daemon_unavailable_keeps_rc_zero`）。真实 CLI 双向复验：EXIT=2、错误码明确、任务总数 1473→1473（零落库）。AC-3 PASS。

遗留测试债修复（2026-10-28）：广义回归暴露 2 个既有测试与 T2 的
`resolve_workspace_pair_from_daemon(root)` 签名/调用点不兼容（0 参 lambda、
假 RPC client 未预期 `workspace.list`），已在测试侧修正并复跑：
`test_cli_082_http_rpc.py` + `test_task_create_parent_governance.py` = 29 passed，
`-k "dispatch or daemon_unavailable or workspace_binding"` = 277 passed，
绑定两套件 = 29 passed。

registry 重复行清理（2026-10-28，用户批准）：先只读核验 `[1288] 6108ef48c489cb1b`
在任务库无任何 capture/binding 引用，再执行 `cw workspace delete 1288`（软删除 →
status=archived）。清理后 TokenSlim active 行仅剩 `[175] d079f60cac8d6186`，
`resolve_workspace_pair_from_daemon` 对 TokenSlim / callwarden 仍分别为
`10 / d079f60cac8d6186`、`1 / 4baea3ff12c2ea5c`。

## 约束
- 不触碰 `T-1789563951668-ba0c338c [live-e2e]`。
- 后台命令 `b3fe9f7b1baddd1f304a0e562439bb0e` 仍在运行，不重跑。
- Python：`C:\Python314\python.exe`；daemon endpoint `http://127.0.0.1:6867`。
