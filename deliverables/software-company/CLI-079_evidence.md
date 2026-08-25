# CLI-079 修复证据（fix_defect 整改）

**任务：** `T-1787322799418-ce4698f0` — CLI-079 [cli_command_projection]：cw local-close → Rust daemon HTTP thin client
**整改步骤：** `T-1787700302263-09407b70`（fix_defect，remediation_of `S-1787322799419-ce53fa54`）
**整改提交：** `5e53440`（master）
**日期：** 2026-08-26

## 失败原因（步骤 S-1787322799419-ce53fa54 原裁决）

> source scan 证实 `cli/main.py::_local_close` 仍直接调用 `db.task_close`，且
> `tests/test_cli_079_http_rpc.py` 仍固化 legacy local fallback；定向测试 3 passed
> 但不满足冻结 thin-client 合同（RC-T-1787322799418-ce4698f0-executor-0
> acceptance #1：*Python handler has no direct db/Unix RPC/local analysis business path*）。

## 整改内容

### 1. before/after source scan

| | `cli/main.py::_local_close` |
|---|---|
| **before** | `def _local_close(): return db.task_close(opts.task_id, **close_kwargs)`（L4805-4806，直接 db 业务路径） |
| **after** | `raise SharedTaskWriterRequiredError("cw local-close 必须经 daemon 权威写点（thin-client 冻结合同）；local 模式需连接本地 daemon，禁止直接 db.task_close")`（fail-closed，无 db 直连） |

- `route_task_write("task.close", {...}, _local_close)` 结构保留：enterprise/auto 走 daemon RPC（`task.close` → Rust `handle_task_close`），local 模式无 daemon 时 fail-closed，不再静默回退 SQLite。

### 2. 测试更新（tests/test_cli_079_http_rpc.py）

- `test_cli079_close_local_fallback_uses_db`（legacy fallback 断言）→ 替换为
  `test_cli079_close_local_fallback_fails_closed`（断言 `SharedTaskWriterRequiredError` 被抛出、无 db 路径执行）。
- 运行结果：**3 passed**（daemon 路径带 lease 透传 / lease 缺失 fail-closed / local 无 daemon fail-closed）。

### 3. runtime fingerprint

- 安装运行时 `C:/Users/wanpi/.callwarden/runtime/current/cw.exe`（2026-08-26 构建）；
  `task.close` 在 enterprise/auto 模式经 daemon RPC（`route_task_write`），P4 lease 凭证原样透传，
  daemon 端 `handle_task_close` fail-closed（require_lease_params + fencing）。

### 4. MCP dependency

- 依赖的权威写点为 `task.close` daemon 路由（`rust_ext/src/daemon/task_collab.rs::handle_task_close`），
  Python 侧无业务 SQL；MCP 依赖声明已应用。

## 待办（状态机闭环，须由任务 owner 会话执行）

- fix_defect 步骤 `T-1787700302263-09407b70` 的 claim 必须由持有 CLI-079 claim 的会话
  （`cw-exec-workbuddy-20260824`）执行：
  1. `task.claim`（`remediation_step_id=T-1787700302263-09407b70` + `contract_claim` skill_id=none/
     prompt_hash=59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6）
     → daemon 自动创建该步骤的 Role Contract binding（claim.rs INSERT）。
  2. `task.report`（step 成功）→ 步骤 done → `task.next_action` 解除 BLOCKED，任务可进入 review。
- 说明：步骤绑定在 claim 时创建；此前 next_action 报"无唯一可验证 Role Contract binding"即因该步骤
  未被 owner 会话 claim 过。

## 台账

- 恢复台账 `cw_task_commit_ledger.json` 应追加：`5e53440 ↔ T-1787322799418-ce4698f0`（CLI-079 fix_defect）。
