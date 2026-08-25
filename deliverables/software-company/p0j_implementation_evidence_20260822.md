# P0-J 本地 Role Worker 授权与可变 Runtime Provenance：实现证据

**任务**：`T-P0J-ROLE-WORKER-IDENTITY`  
**记录时间**：2026-08-22（本地 Windows 工作区）  
**执行边界**：本记录仅说明工作树实现与隔离测试。它不宣称生产 daemon 已部署，也不包含任何 raw credential、lease token 或外部供应商 token。

## 实现范围

P0-J 在 Rust daemon 中建立了稳定的 CW-local Role Worker 授权锚点。`role_worker_id`、冻结的角色、worker instance、local peer owner key 与 daemon 签发 credential hash 共同决定授权；provider、account alias、agent、model 与 runtime session 被限制为 append-only runtime provenance，不能作为角色锁定条件。

| 组件 | 已实现内容 | 安全边界 |
|---|---|---|
| `role_worker.rs` | enrollment、credential hash 校验、角色/instance 绑定、runtime provenance、revoke、owner-scoped status | raw credential 仅在 enrollment response 单次返回；数据库不保存 raw credential 或 provider token |
| `task_collab.rs` | `role_worker.enroll` / `revoke` / `status` handlers；P0-G `task.contract_revise` recovery；bootstrap staged identity policy | `role_worker_v1` 必须显式写入 Task Contract envelope；legacy policy 携带 worker auth 一律拒绝 |
| `dispatch.rs` | role worker enroll/revoke/status routes；enroll/revoke 为 protected mutation | status 为 owner-scoped read，省略 credential hash 与 runtime payload |
| `sqlite_query.rs` | Rust schema version 60、canonical schema-derived Role Worker self-heal、schema audit description | 即使 schema version 已为 60，缺表/缺索引也会从 embedded canonical schema 补齐并验证 |
| `server/daemon_client.py` | Unix/HTTP pure forwarding wrappers | Python 不进行 authorization、SQLite 写入或 credential 存储 |

## 验证结果

下列测试均在 Windows 本地源码和内存/临时 test database 上运行；没有对 `%USERPROFILE%\.callwarden\callwarden.db` 作任何直接 SQLite 操作。

| 验证 | 结果 | 证明的行为 |
|---|---:|---|
| `daemon::task_loop::role_worker::tests` | 5 passed | OS CSPRNG 32-byte credential、hash-only persistence、credential/role impersonation rejection、可变 runtime 接受并追加 provenance、secret key rejection、revoked worker rejection |
| `sqlite_query::tests::role_worker_v60_*` | 2 passed | fresh v60 table/index creation、二次迁移幂等、schema-version 60 short-circuit self-heal |
| `p0g_contract_revise_rejects_adjudicator_without_agent_instance_id_before_lease` | 1 passed | P0-G recovered revise handler 对空 `agent_instance_id` 在 authority/lease 前 fail-closed |
| `p0j_*` bootstrap policy tests | 2 passed | `role_worker_v1` 缺 auth 拒绝；legacy envelope 隐式携带 worker auth 拒绝 |
| `cargo check` cw-daemon | passed | Rust domain、dispatch、task-collab recovery、v60 migration 可编译 |
| Python 3.14 `py_compile` | passed | `db/schema.py`、`db/db_base.py`、`server/daemon_client.py` 语法有效 |

编译存在仓库既有 warnings，但上述命令均以零错误完成。

## 生产 authority 只读探测

当前生产 HTTP authority 的 `/health` 可读，且 P0-J executor lease 在本记录时仍有效。该 daemon binary 尚未包含本工作树的 P0-J deployment：对 `role_worker.status` 的只读 RPC probe 返回 `method_not_found`。因此本任务的工作树实现已具备测试证据，但**生产 deployment、真实 local reviewer/adjudicator worker enrollment，以及 CLI-02/CLI-03/MCP-001 的逐卡 bootstrap 都尚未执行**。

> 本证据不是任务完成声明。部署必须采用受控运行时刷新并由独立 Reviewer/Adjudicator 复核；不得使用历史 `reviewer-wb-186loop` token 或对生产 SQLite 直接写入。

## 后续受控步骤

1. 在不影响现有 authority 的受控运行时刷新窗口中部署当前 Rust binary，并复核 manifest binary hash、schema version 60 与 `role_worker.status` route。
2. 通过 daemon HTTP authority 分别 enrollment 独立的 reviewer 与 adjudicator local Role Worker；凭据只写入各自受限的本地 role-session credential file。
3. 以显式 `identity_policy=role_worker_v1` 逐卡处理 CLI-02、CLI-03、MCP-001；每次仅一张卡，先 bootstrap，再由独立 Reviewer 和 Adjudicator 完成治理循环。
4. 在独立代码审查后再决定 P0-J task 的 reviewer verdict 与 closure。

## 关联日志

- `deliverables/software-company/p0j_role_worker_tests_v3.log`
- `deliverables/software-company/p0j_sqlite_v60_migration_tests.log`
- `deliverables/software-company/p0j_bootstrap_policy_tests.log`
- `deliverables/software-company/p0g_recovered_handler_identity_test_v3.log`
- `deliverables/software-company/p0j_final_cargo_check.log`
- `deliverables/software-company/p0j_production_readonly_probe_after_implementation.json`
