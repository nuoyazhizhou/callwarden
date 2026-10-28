# Checklist：跨项目任务单归属 fail-closed

对应 spec：`docs/specs/cross-project-workspace-binding/spec.md`
任务：`docs/specs/cross-project-workspace-binding/tasks.md`

- [x] `resolve_workspace_pair_from_daemon(None)` 抛 `E_WORKSPACE_ROOT_REQUIRED`，不再隐式回退 PROJECT_ROOT（T1）
- [x] CLI `task create` 以调用项目根解析配对；显式配对与调用方不一致时抛 `E_WORKSPACE_PROJECT_MISMATCH` 且不发起 RPC（T2）
- [x] MCP 启动时按 `CALLWARDEN_WORKSPACE` → cwd 检测 → `PROJECT_ROOT` 解析并 `configure_workspace`（T3）
- [x] MCP `task_create` 校验/补齐服务端项目配对；解析失败 `E_WORKSPACE_ROOT_UNRESOLVED`（T4）
- [x] 项目级 MCP 配置写入 `CALLWARDEN_WORKSPACE` pin，全局级不 pin（T5）
- [x] TokenSlim 权威绑定收敛：`resolve_workspace_pair_from_daemon(r"C:\git_work\TokenSlim")` 唯一配对 = workspace 10 / `d079f60cac8d6186`（T6 / AC-6）
- [x] legacy→canonical 有界再认证：仅 legacy-only 链 + 同 owner + 同归一化根时放行，新 instance capture 链从 revision=1 起（T6）
- [x] `task_collab` 回归全绿：`ws_authority` 7 passed、`daemon::task_collab::tests` 164 passed（T6）
- [x] Rust 回归用例 7 条（spec §3.6）全绿：`tests/test_task_create_workspace_binding.py` 27 passed（T7）
- [x] capture 链连续性门禁不再误报：`cw task show` 对 TokenSlim 任务无「capture 链断链」（T6 数据修复）
- [x] AC-1：`C:\git_work\TokenSlim` 下 `cw task create` → `cw task show` 归属 TokenSlim（`T-1793182549588-35a1881c` = ws10/`d079f60cac8d6186`）
- [x] AC-2：`c:\git_work\callwarden` 下同样流程归属 callwarden（`T-1793182558214-37c72fdc` = ws1/`4baea3ff12c2ea5c`）
- [x] AC-3：任一项目下显式传另一项目配对 → 非零退出（EXIT=2）+ `E_WORKSPACE_PROJECT_MISMATCH`，且无任务落库（1473→1473）
- [x] AC-4：MCP `task_create` 在调用方项目实例中拒绝他项目配对（结构化拒绝 + 零落库）；0/空自动补为当前项目（`T-1793182749929-daecff6c` = ws10）
- [x] AC-5：`pytest tests/test_task_create_workspace_binding.py tests/test_cross_project_workspace_binding.py -q` 全绿（29 passed）
- [x] 广义回归无遗留失配：`pytest tests -k "dispatch or daemon_unavailable or workspace_binding"` 277 passed（排除依赖 cargo 重建的两套件）；`test_cli_082_http_rpc.py` 与 `test_task_create_parent_governance.py` 已适配 T2 resolver 签名
- [x] 治理环闭环：report → handoff → reviewer verdict → adjudicator lease → apply → close → release（`T-1793182868491-75bb7e7c`，lifecycle closed / workflow completed），证据落 `.trae-cn/evidence/T8-cross-project-e2e-20261028.md`
- [x] registry 重复 TokenSlim 行清理：核验 `[1288] 6108ef48c489cb1b` 无 capture/binding 引用后 `cw workspace delete 1288`（archived）；TokenSlim active 仅剩 `[175] d079f60cac8d6186`，resolver 配对不变
