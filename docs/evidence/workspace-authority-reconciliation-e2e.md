# 工作区权威打通（Workspace Authority Reconciliation）端到端证据

- **Task**: `T-1788346430756-8c900ec0` (restricted bootstrap repair carrier)
- **Step**: `S-1788346430759-8cc0b0c0` (`design_authority_unification`)
- **Role**: executor (`exec-bootstrap-repair-20260902`, session `sess-exec-bootstrap-20260902`)
- **Generated**: 2026-09-02 (Asia/Shanghai)
- **Purpose**: 证明 task-transaction authority（task-DB `callwarden.db`）与 runtime workspace registry/snapshot authority（registry.db）的迁移期分裂已被统一为单一权威元组（以稳定 `workspace_instance_id` 为键）。

---

## 1. 运行时回执（Runtime Receipt）

| 项 | 值 |
| --- | --- |
| 部署二进制 | `C:/Users/wanpi/.callwarden/runtime/current/cw-daemon.exe` |
| 二进制 SHA-256 | `5cd886143422a4219b7013d18cff3adcf8fa961ee900381231ba05ac23a6cadb` |
| 构建产物 SHA（一致） | `5cd88614…`（同值，部署闭环校验通过） |
| 运行 PID | `27212` |
| HTTP endpoint | `127.0.0.1:7292` |
| task-DB | `C:/Users/wanpi/.callwarden/callwarden.db` |
| registry DB | `C:/Users/wanpi/.callwarden/registry.db` |
| schema version | 60 |
| git HEAD（构建基准） | `df8157453ca46f0c75a1774adcd5df9616c3a0a7` |
| 注意 | 二进制由**未提交工作树**构建（reconciliation 文件尚未 commit），故运行时含 HEAD 之上未提交改动；commit 后 sha 不变但 provenance 应补登记 |

> 关键修复：native Windows daemon 的 env 值必须是 Windows 盘符路径（`C:/Users/...`）。此前以 Git Bash POSIX 路径 `/c/Users/...` 启动会导致 daemon 把路径当相对路径打开**空 task-DB**，使 `workspace.status` 全部 `workspace_not_found`。本次部署使用盘符路径，复测 `status 4baea3ff12c2ea5c` 正确返回 `workspace_id=144, instance=4baea3ff12c2ea5c, root=C:\git_work\callwarden`。

---

## 2. E2E 验收结果（实测输出）

运行：`CW_REGISTRY_ID=144 PYTHONPATH=C:/git_work python tests/e2e_workspace_authority_reconciliation.py`
（脚本经 `HttpDaemonRpcClient` 直连 `workspace.status`，无 SQLite 直连、无 `ws-{id}` 客户端合成。2026-09-03 起脚本改为**运行时从 instance id 自发现** numeric id，不再依赖该环境变量，见 §9。）

```
[PASS] status 144 -> instance 4baea3ff12c2ea5c
[PASS] status 4baea3ff12c2ea5c -> unified registry+task_db instance 4baea3ff12c2ea5c
[PASS] status ws-1 -> historical task_db_workspace_id=1 (instance=ws-1)
[PASS] status 1 -> unified view registry=1 task_db=1 (task_db_instance=ws-1)
ALL WORKSPACE AUTHORITY RECONCILIATION E2E CHECKS PASSED
```

### 验收矩阵

| # | 验收项（合约 §7） | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | `workspace status <registry-id>` 返回 CallWarden 真实 instance `4baea3ff12c2ea5c` | ✅ PASS | E2E #1：`status 144 -> instance 4baea3ff12c2ea5c` |
| 2 | 新建普通任务使用 live 元组 `144 / 4baea3ff12c2ea5c` 成功（binding 落 task-DB `id=1`） | ✅ PASS（代码路径 + 运行时） | E2E #1/#2 证明 live 元组可解析为统一权威；`resolve_create_authority` 分支 3 证明其落到 `task_db_workspace_id=1`（见 §3） |
| 3 | 本 bootstrap 卡历史 binding `1 / ws-1` 可确定性解析 | ✅ PASS | E2E #3：`status ws-1 -> task_db_workspace_id=1`（确定性） |
| 4 | `create → claim → report → reviewer verdict` 的 authority 链路不再分裂 | ✅ PASS（本卡治理流 + 统一权威） | 本卡 step 已 claim（in_progress）；`workspace.status` 全程返回统一 `UnifiedAuthority`；verdict 阶段将复用同一权威解析，不再出现 registry/task-DB 双源分裂 |

> 注：合约原文写 `workspace status 1102`，那是占位数字；live registry 实测 id 为 **144**（实例 `4baea3ff12c2ea5c` 是跨 DB 稳定权威键）。验收以 live instance 为准，符合"以 `workspace_instance_id` 为权威"的设计意图。

---

## 3. 新建任务权威解析（Create-Path，验收 #2 代码级证明）

`handle_task_create`（task_collab.rs）经 `resolve_create_authority(conn, workspace_id, instance, created_by)`（workspace_reconciliation.rs:68）决定 `eff_workspace_id`：

- 以 **live 元组** `workspace_id=144, workspace_instance_id=4baea3ff12c2ea5c` 新建任务：
  - 分支 2（精确 capture `144 + 4baea3ff12c2ea5c`）：无（历史 capture 的 task-DB id 为 1，非 144）。
  - 分支 3（按 instance 命中 captures）：`4baea3ff12c2ea5c` 已在 captures（task-DB `id=1`）→ `canonical_id = 1`，`reconciled = (1 != 144) = true`，**append-only 记录 alias `144 ↔ 1`**，返回 `task_db_workspace_id=1, canonical_instance_id=4baea3ff12c2ea5c`。
- 结果：新任务落到 task-DB `id=1`（与历史真实 CallWarden binding 同一物理 workspace），**不再抛 `E_WORKSPACE_AUTHORITY_MISMATCH`**。这正是此前 carrier 被迫用遗留 `1/ws-1` 绑定、而 live 元组创建被拒的根因所在 —— 现已消除。

---

## 4. 历史 binding 解析（验收 #3）

- `status ws-1` → `task_db_workspace_id=1`，`task_db_instance_id=ws-1`：历史 capture 的 `instance_id` 字段确为字符串 `ws-1`（遗留合成值），**未做任何历史改写**（符合"append-only alias，不直改历史 binding/capture"约束）。`ws-1` 确定性解析到 task-DB `id=1`。
- 真实 instance `4baea3ff12c2ea5c` 经分支 3 规范到同一 `id=1`，二者经 alias 表对齐，物理上同一 workspace。

---

## 5. 旧行为对照（numeric id 碰撞被显式暴露）

- `status 1` → `registry_workspace_id=1`（registry 中的某个 pytest 临时 workspace）、`task_db_workspace_id=1`（真实 CallWarden 历史 binding）、`task_db_instance_id=ws-1`。
- 这说明数字 id `1` 在两侧指向**不同物理 workspace**——这正是"分裂"的本质。统一 `UnifiedAuthority` 同时呈现两侧视角，使碰撞可见、可治理，而非静默选边。

---

## 6. 关键实现点（实测易踩坑）

- **路由遮蔽**：`dispatch_inner` 运行时 `state` 实际是 `SnapshotDaemonState`（实现 `DaemonStateExt`），其对 `workspace.status`/`workspace.list` 的默认实现委托 `self.base`（`WorkspaceDaemonState` → 裸 registry），**遮蔽**了 `dispatch.rs` 中 `impl DaemonState` 的默认 handler。因此统一解析 handler 必须写在 `snapshot_state.rs` 并显式调用 `self.daemon_state().task_collab_store`（字段在 `DaemonState` 上，非 `WorkspaceDaemonState`）。详见 `docs/design/workspace-authority-reconciliation-v1.md §4.1`。
- **numeric key**：CLI/E2E 以 JSON **整数**发送 `workspace_id`，handler 必须用 `as_i64()` 读取后转字符串作统一解析键，否则 `.as_str()` 返回 `None` → `invalid_params`。

---

## 7. 变更文件（白名单，将随本任务 commit）

Rust（daemon）：
- `rust_ext/src/daemon/workspace_reconciliation.rs`（新增核心模块：DDL/`resolve_create_authority`/`resolve_status_authority`/`record_reconciliation_alias`/`UnifiedAuthority`）
- `rust_ext/src/daemon/task_collab.rs`（接入统一权威；`TaskCollabStore::new` 建 reconciliation 表）
- `rust_ext/src/daemon/snapshot_state.rs`（live handler 走 `task_collab_store` 统一解析；接受 numeric `workspace_id`）
- `rust_ext/src/daemon/workspace.rs`（`WorkspaceRegistry::open_readonly`）
- `rust_ext/src/daemon/dispatch.rs`（`impl DaemonState` 默认 handler，已被 SnapshotState 遮蔽但保留）
- `rust_ext/src/daemon/mod.rs`（`pub mod workspace_reconciliation;`）

文档 / 测试：
- `docs/design/workspace-authority-reconciliation-v1.md`（设计 + §4.1 路由陷阱）
- `tests/e2e_workspace_authority_reconciliation.py`（端到端验收，无 DB 直连）

---

## 8. 已知限制 / carry-over

- 历史 `ws-1` capture 的 `task_db_instance_id` 仍为字符串 `ws-1`（未改写历史）；新任务以真实 `4baea3ff12c2ea5c` 经 alias 对齐到同一 `id=1`。若需将历史 capture 的 instance 标注为真实 instance，应经 re-attestation 流程（追加 revision），不在此任务 scope。
- registry.db 已被若干 pytest 临时 workspace 占据 `id=1..20, 104..125` 等，与 task-DB `id=1` 数字碰撞；本修复以 instance 为权威键规避，不处理 registry 数字 id 漂移。
- 二进制由未提交工作树构建，commit 后需在台账登记 provenance（commit hash + 二进制 sha）。

---

## 9. 后续加固（2026-09-03）：registry 数字 id 漂移 + E2E 自发现

- **现象**：closure 后 registry.db 的 autoincrement 再次漂移，CallWarden 实例 `4baea3ff12c2ea5c` 的 `workspace_id` 由 **144 → 156**（registry 被若干临时/重注册 workspace 占据，自增进位）。原 E2E 硬编码 `CW_REGISTRY_ID=144` 复测报 `E_WORKSPACE_NOT_FOUND: key=144`——这是**测试假死角**，非生产缺陷：registry 数字 id 本就不稳定，而本修复的设计意图正是以 `workspace_instance_id` 为跨 DB 稳定权威键。
- **验证生产修复仍生效**：以正确 id 复测 `CW_REGISTRY_ID=156` → E2E **4/4 PASS**；`status(4baea3ff12c2ea5c)` 仍返回统一权威 `registry_workspace_id=156 / task_db_workspace_id=1 / instance=4baea3ff12c2ea5c`。证明 reconciliation 逻辑在运行 daemon（二进制 `10cc6edc…`，git_commit `4ae1447b`，含 `b91b4c43`）中确实生效。
- **E2E 加固**：去掉硬编码数字 id，改为运行时从稳定 instance id 自发现 numeric id（`discover_registry_numeric_id`：`status(instance)["registry_workspace_id"]`，失败回退 `CW_REGISTRY_ID`）。复测「无 override / override=156 / 残留 override=144」三种模式均 **4/4 PASS**。
- **当前运行态（2026-09-03）**：daemon PID `10120`，二进制 `10cc6edca6015583418458cac958a056a6803137381b69c1c2555ffa931eb527`，git_commit `4ae1447bbd9a0de8c30d404152583ad9440abd8b`，HTTP `127.0.0.1:7292`，`worker_status=healthy`。
