# Workspace Authority Reconciliation v1

> Task: `T-1788346430756-8c900ec0` (Bootstrap repair carrier, step `S-1788346430759-8cc0b0c0`)
> Contract: `TC-T-1788346430756-8c900ec0` / `sha256:cc5ba142df94a7e7ce8293eef0eee715098382e5f881220a60e1dd211c126f5a`
> Author: executor `exec-bootstrap-repair-20260902` (independent session, dedicated identity)

## 0. 问题陈述

本卡是一个**受控 bootstrap 修复载体**：它临时绑定 task-DB 可见的遗留元组
`workspace_id=1 / workspace_instance_id=ws-1`，原因是使用 live registry 元组
`(workspace_id=1102, workspace_instance_id=4baea3ff12c2ea5c)` 创建普通任务时被 daemon 拒绝：

```
E_WORKSPACE_AUTHORITY_MISMATCH: task-DB 中不存在 workspace_id=1102
```

这不是要扩大 `ws-1` fallback，而是要**消灭** task transaction authority 与 runtime
workspace registry/snapshot authority 之间的迁移期分裂。

## 1. 权威源定位（实测）

| 源 | DB | 表 | CallWarden 记录 |
|---|---|---|---|
| registry（runtime） | `~/.callwarden/registry.db` | `daemon_workspaces` | `workspace_id=**144**`, `instance=4baea3ff12c2ea5c`, `client_view_root=C:\git_work\callwarden` |
| task-DB（transaction） | `~/.callwarden/callwarden.db` | `workspaces`（**无** `workspace_instance_id` 列） | `id=1`（与 `id=10` 并存） |
| task-DB | 同上 | `workspace_authority_captures` | `workspace_id=1` → `{4baea3ff12c2ea5c, ws-1, ws-1-bridgelive-*, ws-1-r5test-*}`；`workspace_id=10` → `{ws-10}` |

关键发现：**`instance=4baea3ff12c2ea5c` 同时存在于 registry.db（id=144）与 task-DB captures（id=1）**。
两套 DB 用**不同的数字主键**指向同一物理 workspace，而 `workspace_instance_id` 是跨 DB 的**稳定身份键**。

> 注：任务创建时记录的 `1102` 是当时的 live registry id；当前 live registry 已重注册为 `144`。
> 任何修复都必须对数字 id 漂移鲁棒——**以 instance id 为权威**，数字 id 仅作诊断 provenance。

## 2. 根因

`task_collab.rs::handle_task_create` 提取 `workspace_id`（来自请求参数，即 registry 数字 id）后，
直接以该数字 id 调用 `bind_task_to_workspace`，后者执行
`SELECT COUNT(*) FROM workspaces WHERE id = ?`（task-DB）。当请求携带 registry 数字 id `144`、
而 task-DB `workspaces` 只有 `id=1/10` 时 → `E_WORKSPACE_AUTHORITY_MISMATCH`。
数字 id 在两个 DB 间不共享，导致普通创建失败、权威分裂。

## 3. 统一权威模型（目标）

`(numeric_id, instance_id)` 中 **instance_id 是稳定身份**；numeric_id 在 registry 与 task-DB 间
允许不同，但必须经由 **append-only 别名** 显式关联，且**绝不改写历史 binding / capture / task event**。

### 3.1 新增 append-only 表 `workspace_reconciliation_aliases`（task-DB）

```sql
CREATE TABLE IF NOT EXISTS workspace_reconciliation_aliases (
  alias_id                 TEXT PRIMARY KEY,
  registry_workspace_id    INTEGER NOT NULL,
  registry_instance_id     TEXT NOT NULL,
  task_db_workspace_id     INTEGER NOT NULL,
  task_db_instance_id      TEXT NOT NULL,
  root_path_hash           TEXT NOT NULL,
  reconciliation_status    TEXT NOT NULL DEFAULT 'active',  -- active | superseded
  created_by               TEXT NOT NULL,
  authoritative_created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recon_alias_instance
  ON workspace_reconciliation_aliases(registry_instance_id, task_db_instance_id);
```

### 3.2 解析函数（新模块 `workspace_reconciliation.rs`）

`resolve_create_authority(conn, requested_workspace_id: i64, requested_instance_id: &str)
  -> Result<ReconciledAuthority>`：

1. `requested_instance_id` 空 → `E_TASK_WORKSPACE_INSTANCE_REQUIRED`（BR-01，禁止合成 `ws-{id}`）。
2. 精确 capture `(requested_workspace_id, requested_instance_id)` 命中 → 原样返回（无 reconciliation）。
3. 仅 `requested_instance_id` 在 captures 命中（跨任意 task-DB id）→ canonical = 该 task-DB id；
   若 `requested_workspace_id != canonical` → **记录 append-only alias**（idempotent）并标记 `reconciled=true`。
   这就是 `144/4baea3ff12c2ea5c` → `1/4baea3ff12c2ea5c` 的修复路径。
4. instance 不在 captures 但 task-DB `workspaces` 含 `requested_workspace_id` → 允许（将建首个 capture）。
5. 其它 → `E_WORKSPACE_AUTHORITY_MISMATCH`（不得 invent）。

`resolve_status_authority(conn, registry, key)`：接受 instance id **或** 任一数字 id（registry/task-DB），
经 alias 表回交叉解析，返回统一权威元组 `{registry_id, task_db_id, instance_id, root_path}`。

## 4. 接入点

| 位置 | 改动 |
|---|---|
| `task_collab.rs::handle_task_create` | 提取 `(workspace_id, instance)` 后先 `resolve_create_authority`，以 **canonical task-DB id** 调 `bind_task_to_workspace` + 记录 alias |
| `task_loop/create.rs::write_domain` | 同解析（1A 路径 / 领域测试复用） |
| `workspace.rs::get_workspace_status` / `list_workspaces` 包装 | 经 `resolve_status_authority` 统一；status 接受 instance 或数字 id |

### 4.1 运行时路由关键点（实测，易踩坑）

`dispatch_inner` 的 `state` 实际类型是 **`SnapshotDaemonState`**（实现 `DaemonStateExt`），
**不是** `DaemonState`。`SnapshotDaemonState` 对 `workspace.status` / `workspace.list` 的
`DaemonStateExt` 实现**默认委托给 `self.base`（`WorkspaceDaemonState`）**，而 `WorkspaceDaemonState`
的 `handle_workspace_status` 只查 registry、返回 registry 记录——会**遮蔽** `DaemonState` 默认 impl
（`dispatch.rs::handle_workspace_status`，调用 `unified_workspace_authority`）使其成为死代码。

因此必须在 **`snapshot_state.rs::handle_workspace_status` / `handle_workspace_list`** 中显式改为：
`self.daemon_state().task_collab_store → unified_workspace_authority(key)` / `list_unified_workspaces()`
（`task_collab_store` 挂在 `DaemonState` 上，经 `WorkspaceDaemonState.base` 可达，统一经
`self.daemon_state()` 访问）。仅改 `dispatch.rs` 的默认 impl 不会生效。
| `task_supersede.rs:1186` `ws-inst-sup-{task_id}` 合成 | 改为使用解析后的真实 instance，禁止合成 `ws-{id}` |
| `cli/main.py` / `server/daemon_client.py` | 删除 `ws-{id}` 兜底；next-action/创建只透传 daemon 返回的 `workspace_id` + `workspace_instance_id` |

## 5. 客户端透传纪律

- 客户端**不得**本地合成 `ws-{numeric_id}` 或自行推导 authority。
- `cw task next-action` 优先使用 daemon 在 `next-action` 投影中返回的 `workspace_id` + `workspace_instance_id`；
  仅在完全离线 local-mode 下才允许 `derive_workspace_instance_id(root)`（根哈希派生，**非** `ws-{id}` 数字合成）。
- `task.create` 必须显式携带 daemon 解析出的 `workspace_id` + `workspace_instance_id`。

## 6. 测试矩阵（CallWarden + TokenSlim 共存）

| 用例 | 断言 |
|---|---|
| 正向-CW | `task.create` 用 `144/4baea3ff12c2ea5c` → 成功，binding 落 task-DB `id=1`，alias `144↔1` 已记录 |
| 正向-TS | TokenSlim 注册独立 workspace，创建任务成功，authority 不与 CW 串扰 |
| 隔离 | 两项目 instance 不同 → 各自 capture/verdict/snapshot 互不污染 |
| 冲突 | 同 instance 不同数字 id → 解析到 canonical，记录 alias；不报 mismatch |
| 历史 binding | `ws-1`（task-DB id=1）与 `ws-10`（id=10）可确定性解析到对应 task-DB id + live instance |
| snapshot | `workspace.status 144` / `status 4baea3ff12c2ea5c` 返回同一权威元组 |
| verdict | reviewer verdict 链路读取的 binding instance 与 create 一致，无分裂 |
| task-create | `create → claim → report → reviewer verdict` 全链路 authority 一致 |
| legacy | `ws-{id}` 合成被禁；空 instance → `E_TASK_WORKSPACE_INSTANCE_REQUIRED` |

## 7. 验收口径（运行时，非单测）

1. `cw daemon status 144` 返回 CallWarden 真实 instance `4baea3ff12c2ea5c`。
2. 新建普通任务使用 `144/4baea3ff12c2ea5c` 成功（binding 落 task-DB `id=1`）。
3. 本 bootstrap 卡历史 binding `1/ws-1` 可确定性解析。
4. `task create → claim → report → reviewer verdict` authority 链路不再分裂。
5. `task_supersede_relations` / `task_supersede_events` 中涉及本卡或 v1 的行为符合预期（不伪造）。

## 8. 禁止项（合约）

- 直接 SQLite 写历史 binding/capture/task event（只 append-only 写 alias 表）。
- 修改 Role Prompt Compiler / 冻结 work-order。
- `task.apply` / `task.close` / `task.supersede`。
- 把 `ws-1` 作为常规 fallback。

## 9. 持久性修复（T-1788382908707-bbdd0cfc，remediation of §7 不完整验收）

### 9.1 问题
T-1788346430756 关闭时只验证了 **instance 键** `workspace.status`（经 `workspace_authority_captures` 持久捕获解析），但 **数字键** 路径依赖 `workspace_reconciliation_aliases.registry_workspace_id`（不稳定 autoincrement）。fresh daemon restart 后 registry id 漂移（144→156），alias 行过期 → `status(<numeric id>)` 返回 `task_db_workspace_id=null`。即「registry 可见 ≠ task authority 一致」。

### 9.2 修复（`workspace_reconciliation.rs::resolve_status_authority`）
数字键/instance 键解析出 registry instance 后，**一律经稳定 instance 重新解析 task-DB 侧**：
1. `task_db_capture_by_instance(instance)` —— 读持久 `workspace_authority_captures`；
2. `alias_by_instance(instance)` —— 按 instance（非数字 id）查审计 alias 行补全。
因此 registry 数字 id 漂移或重启都不影响统一视图；不再依赖可能过期的 alias 数字列。

### 9.3 验收（运行时，fresh-restart 后）
- `status(<registry numeric id>)` 返回非空且一致的 `registry_workspace_id / registry_instance_id / task_db_workspace_id / task_db_instance_id`；两 instance 相等。
- `status(<instance id>)` 同样一致。
- 普通 `task.create` 用 daemon 返回的元组成功，恰好一个 binding/capture，三份 Role Contract 与 identity policy 完整。
- 新任务 `claim → report → persisted reviewer verdict` 在同一 authority 下成立。
- CallWarden / TokenSlim 同时注册、可区分、不能跨项目 bind。
- 负向：registry-only / 跨项目 / 未知 instance 的 `task.create` fail-closed 且不产生 partial 行；stale 数字 id + 有效 instance 确定性 reconcile；`ws-1`/`ws-10` 走 audited reconciliation。
- 验证脚本 `tests/e2e_workspace_authority_reconciliation_v2.py`（fresh client process；`CW_E2E_LIFECYCLE=1` 触发完整生命周期证明）。
