# P0-J：本地 Role Worker 授权与可变运行时 Provenance 分层设计

**任务：** `T-P0J-ROLE-WORKER-IDENTITY`  
**状态：** Executor 首步设计产物  
**范围：** daemon identity、lease、Task Contract bootstrap/verdict 的授权输入；Python/CLI 仅作 HTTP 参数转发。

## 1. 目标与非目标

本设计解决的不是“让任何字符串都能充当角色”，而是将**角色授权**从供应商账号、模型版本和临时聊天会话中剥离。每个治理操作先由 CW 本地签发的 Role Worker 凭证授权，再将本次实际调用的 Agent、模型、provider、账号别名和运行时会话作为不可变 provenance 追加记录。

> 外部账号切换、token 耗尽、模型升级或运行时重连不会改变同一 CW Role Worker 的授权归属；但它们必须留下新的 runtime provenance，供审计追溯。

本任务不存储供应商账号 token，不改变历史 Task Contract、Verdict、Evidence 或 Lease 行，不允许 Python 回退直写 SQLite，也不允许同一 Role Worker 借改写 `role` 字段跨角色行动。

## 2. 术语与不变量

| 概念 | 持久化锚点 | 授权作用 | 可变性 |
|---|---|---|---|
| Role Worker | `role_worker_id` | 角色唯一主体，如 `cw-reviewer-01` | 稳定；显式 revoke 才失效 |
| Role Instance | `role_instance_id` | 一个真实独立 worker/window 的生命周期 | 稳定到实例退出；恢复同一实例不变 |
| Role Session | `role_session_id` | 同一任务循环的本地恢复句柄 | 同一恢复循环不变；显式新循环才轮换 |
| Credential | raw secret 仅在本地 `credentials.bin`；authority 存 SHA-256 | 证明调用者持有已签发 worker 能力 | 可 rotate/revoke；不写 task evidence |
| Runtime provenance | provider、external agent/model/account alias/runtime session | 说明“这次调用实际由何运行时发起” | 每次调用可变；只追加 |

核心不变量如下。

1. **授权只依赖 Role Worker credential、角色和本地 owner peer。** 外部 `agent_id`、`model_id` 或账号切换不得单独拒绝授权。
2. **同一 Role Worker 的注册角色不可变。** 已登记 `executor` 的 worker 不能只改请求参数为 `reviewer`。
3. **同一任务中冲突治理角色必须有不同 Role Worker。** `executor` 不得兼任 `reviewer` 或 `adjudicator`；`reviewer` 与 `adjudicator` 也必须不同。
4. **Runtime provenance 仅追加。** 每次 protected mutation 记录本地 worker、runtime agent/model/provider/account alias/runtime session 和时间；禁止覆写历史。
5. **迁移期 legacy identity 只读兼容。** 已有 `agent_registrations`、`task_leases`、`action_identities` 继续可读；只有显式启用 Role Worker enforcement 的新任务使用新门禁。

## 3. 数据与凭证模型

P0-J 引入两张 authority-owned append-only 数据表与一张可撤销注册表。所有建表/索引由正式 schema migration 完成，daemon 启动期只做列/表存在性检查。

| 表 | 主键 | 关键列 | 语义 |
|---|---|---|---|
| `role_workers` | `role_worker_id` | owner_key、role、credential_hash、status、created_at、revoked_at | 本地签发的稳定授权主体；credential 永不明文保存 |
| `role_worker_instances` | `role_instance_id` | role_worker_id、owner_key、status、created_at、retired_at | 可恢复的独立窗口/worker instance |
| `role_runtime_provenance` | `event_id` | role_worker_id、role_instance_id、role_session_id、task_id、action_type、runtime JSON、recorded_at | 每个治理/lease/claim 运行时事实的 append-only 审计 |

`runtime JSON` 至少包含 `agent_id`、`model_id`、`provider`、`account_alias`、`runtime_session_id`、`client_id`、`runtime_hash`。它不得包含 raw token、cookie、OAuth refresh token 或可反向恢复凭证的秘密。

本地文件布局是：

```text
%USERPROFILE%\.callwarden\role-sessions\<opaque-handle>\
  state.json          # role_worker / instance / role-session，非秘密
  credentials.bin     # raw credential 或 lease token，仅当前用户 ACL
```

host 或 Expert/Plugin 在请求进入模型前将 `state.json` 中的非秘密字段注入环境/常驻上下文；模型账号切换不会创建新 Role Worker。

## 4. RPC 与授权流程

### 4.1 enrollment

新增 daemon-native `role_worker.enroll`。它只接受本地 owner peer 发起的 bootstrap/enrollment，创建 Role Worker、Role Instance、随机 credential 并返回 raw credential 一次。后续 `role_worker.rotate`、`role_worker.revoke` 也必须携带既有 credential 或由独立 Adjudicator 执行。

### 4.2 protected mutation

新增 `role_worker_auth` 参数：

```json
{
  "role_worker_id": "cw-reviewer-01",
  "role_instance_id": "inst-reviewer-01-a",
  "role_session_id": "sess-reviewer-cli02-01",
  "credential": "raw-secret-never-persisted-in-task-evidence",
  "runtime": {
    "agent_id": "workbuddy-agent-abc",
    "model_id": "deepseek-v4-flash",
    "provider": "workbuddy",
    "account_alias": "user-switch-2",
    "runtime_session_id": "vendor-session-xyz",
    "client_id": "workbuddy-desktop",
    "runtime_hash": "..."
  }
}
```

daemon 验证 credential hash、owner peer、worker role、instance active 状态以及 task 内冲突角色 worker 后，才执行 lease/claim/bootstrap/verdict/apply/close。验证成功时向 `role_runtime_provenance` 追加事件；数据库中的 legacy `agent_id/session_id/model_id` 保留为兼容投影，但不再充当授权决策来源。

### 4.3 账号切换

同一 `role_worker_id` + `role_instance_id` + `role_session_id` 可发送新的 runtime agent/model/provider/account alias。daemon 接受，只追加新 provenance 行。若新请求换了 worker/instance/session 或 credential 不匹配，daemon fail-closed。

## 5. 迁移与启用策略

1. 正式 schema 升级创建三张表和索引；历史行不修改。
2. Rust handler 先支持新 `role_worker_auth`，并把 legacy identity 放入 provenance 兼容字段。
3. 对已有任务，默认保持 legacy identity 读取。只在 Task Contract `identity_policy=role_worker_v1` 或新 P0-J 后创建的 contract 上强制新 auth。
4. CLI-02/CLI-03/MCP-001 bootstrap 在 P0-J 审核通过后，先 enrollment 独立 `cw-reviewer-*` 与 `cw-adjudicator-*` worker，再写 revision-1；外部账号/模型数据只写 provenance。

## 6. 负向矩阵

| 场景 | 预期 |
|---|---|
| Executor 把请求 `role` 改为 reviewer | `E_ROLE_WORKER_ROLE_MISMATCH`，无写入 |
| 未携带 credential | `E_ROLE_WORKER_CREDENTIAL_REQUIRED`，无写入 |
| credential hash 不匹配/worker revoked | 结构化 fail-closed，追加拒绝审计而不改变任务 |
| 同 worker 执行 executor 后申请 reviewer lease | `E_ROLE_WORKER_SEPARATION_VIOLATION` |
| 同 reviewer worker 从账号 A 切至账号 B / model X 切至 Y | 允许；新增一行 runtime provenance |
| 仅 external session 改变 | 允许；不改变 worker/instance/session 授权锚点 |
| 旧任务无 `identity_policy=role_worker_v1` | legacy 读兼容；不自动改写历史 |

## 7. 下一步

Executor 将根据本设计实现 schema migration、Rust domain/handlers/dispatch、HTTP thin-client 转发与定向回归测试。完成后将 evidence、runtime receipt 与 hash 追加到 P0-J，交由独立 Reviewer 复审；Adjudicator ACCEPT 后才可用新 worker 为 CLI-02、CLI-03、MCP-001 bootstrap。
