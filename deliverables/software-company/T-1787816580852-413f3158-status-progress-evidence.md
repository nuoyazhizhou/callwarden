# T-1787816580852-413f3158 实现与验证证据

## 范围

- 修复 daemon 的 `task.status`、`task.list`、`task.status_tree` 输出，使 raw lifecycle 与 daemon 派生 workflow 状态同时可见。
- 历史任务缺少不可变 workspace binding/capture、Task Contract 或可验证治理事实时，统一返回 `workflow_status=governance_blocked` 与 `blocking_reasons`；不静默回填、不伪造历史 verdict/lease/status。
- 修复 flat status 丢失 `task_steps` 的问题，统一返回步骤明细和进度对象。
- 统一进度单位：legacy `progress` 保持 0..1 ratio；新增明确的 `ratio`（0..1）和 `percent`（0..100，四舍五入两位）。CLI 展示固定两位小数。

## 改动范围

- `rust_ext/src/daemon/task_collab.rs`
- `rust_ext/src/daemon/task_loop/next_action.rs`
- `rust_ext/src/cli/task.rs`
- `cli/main.py`
- `db/db_tasks.py`
- `server/tools/tools_task.py`
- `tests/test_cli_093_http_rpc.py`
- `docs/architecture.md`
- `docs/cli_reference.md`
- `docs/mcp_tools.md`
- `README.md`
- `AGENTS.md`

## 验证结果

### 静态/单元测试

- `tokenslim run python -m py_compile cli/main.py db/db_tasks.py server/tools/tools_task.py`：通过。
- `tokenslim run cargo check --manifest-path rust_ext/Cargo.toml`：通过；仅有既存 warning。
- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --lib test_task_list_includes_daemon_governance_projection`：1 passed。
- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --lib test_task_status_includes_steps_and_normalized_progress`：1 passed。
- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml task_tree_progress_and_findings_match_contract`：1 passed。
- `tokenslim run python -m pytest tests/test_cli_093_http_rpc.py -q -k ratio`：1 passed。

### 真实 runtime round-trip

部署命令：

```text
.\scripts\refresh_shared_runtime.ps1 -TaskId T-1787816580852-413f3158 -Configuration release
```

部署证据：

- 文件：`C:\Users\wanpi\.callwarden\runtime\evidence\20260827-163417-af52d462e1a0-7c6fe457.json`
- 文件 SHA-256：`sha256:6CF082C02C31362237EB06B3DABC3F26E4568F2735018C9F286A25BFA1FF01C5`
- runtime：`20260827-163417-af52d462e1a0-7c6fe457`
- daemon PID：`54076`
- daemon binary SHA-256：`sha256:b9dd28840a6324f49821b77a308aea886f033030db192c96cc3f000ec05b336e`
- transport：Windows HTTP named pipe
- ping：exit 0，`python_dependency_mode=python_free`
- task DB fingerprint：`1354d303edf7e8485ef75ea887f8969e7a8f5f797e128bb9261e44f13d4edca6`

实际查询结果：

- `task show T-1787816580852-413f3158 --flat`：正确显示 `Steps (2)`，不再是 `Steps (0)`；当前步骤为 `in_progress`，下一步骤为 `pending`。
- `task status-tree T-1787293451688-c14b1e44`：正确显示 `Progress: 758/764 (99.21%)`。
- 同一 status tree 中，缺少治理事实的历史任务显示 `governance_blocked`，例如 SRV-019；具备治理事实的 review 任务显示 `review_pending`。
- `task list --status review`：列表项同时显示 `[review / review_pending]`，证明列表也使用 daemon 治理投影。

## 说明

本任务没有对生产 SQLite 直接执行修复 SQL。历史数据的“修复”采用只读兼容投影：保留原始 lifecycle，按当前可验证事实给出 workflow 或明确的 governance_blocked 诊断。
