# TP-09 公共初始化与实体前置合同（冻结版 r1）

阶段 B1 · owner: harness-initialization · 日期 2026-10-12 · 前置：TP-08F、TP-05、TP-07（均已 closed）

本文冻结 TP-09 的隔离面、selector 与正负向义务。结果：**11 passed**（Windows + Py3.14 profile）。

## 1. 冻结 selector 与命令

```
selector: tests/acceptance/harness/initialization/test_initialization.py
harness : tests/acceptance/harness/initialization/init_harness.py
run 根  : C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-09/
         （home/ 隔离状态、ws/ 空 workspace、src/ TP-06 daemon 候选、reports/）
解释器  : C:/Python314/python.exe

C:/Python314/python.exe -m pytest tests/acceptance/harness/initialization/test_initialization.py \
    -p no:cacheprovider --noconftest -v --junitxml=<run根>/reports/junit.xml
  cwd=C:/git_work/callwarden；expected rc=0，11 passed
```

## 2. 已冻结的公开初始化链（实测）

| 步骤 | 命令 | 实测结果 |
|---|---|---|
| 空目录起点 | run 根 `ws/` 初始为空 | ✓ 无 `.callwarden`、无源码 |
| register | `cw workspace register <name> <root>` | ✓ rc=0，`ID=999002, name=tp09-ws, root=...\TP-09\ws` |
| list | `cw workspace list` | ✓ 只列 run 根内 workspace（隔离生效） |
| set（activate） | `cw workspace set <id>` | ✓ rc=0，`Switched to active workspace` |
| task 实体 | `cw task list` | **fail-closed**：`没有 active workspace（is_active=1），拒绝推导 workspace_id` |
| lease 实体 | `cw lease status <task_id>` | fail-closed 结构化拒绝，无未捕获异常 |

任务库 schema 就位：run 根 `callwarden.db` 含 `tasks` / `task_steps` / `task_events` /
`workspace_authority_captures` 等 99 表，公开初始化创建，**无 SQL 助手补表**。

## 3. 🔴 F-DAEMON-01：产品级隔离缺陷（P-FIX 登记）

**现象**：用 TP-07 的隔离 env（USERPROFILE/HOME/CALLWARDEN_HOME/CALLWARDEN_DB/
`CALLWARDEN_DAEMON_REGISTRY_DB` + daemon `--registry` 全部指向 run 根）启动隔离 daemon 后，
`cw workspace list` 仍返回 **153 条生产 workspace**（`C:\git_work\callwarden`、`Qoder`、
大量 `cw_http_*` 临时路径）。

**根因（源码实证）**：
- `rust_ext/src/daemon/task_collab.rs:1776-1780` `TaskCollabStore::list_unified_workspaces()`
  **独立解析** registry 路径：读 env `CW_DAEMON_REGISTRY_DB`，未设则兜底
  `default_registry_db_path()`；
- `rust_ext/src/daemon/config.rs:28-30` `default_registry_db_path()` 返回**硬编码**
  `/var/lib/callwarden/registry.db`（Windows 即 `C:\var\lib\callwarden\registry.db`
  = 生产 registry）；
- 该解析**与 daemon 启动时的 `--registry` / `CALLWARDEN_DAEMON_REGISTRY_DB` 无关**——
  启动用 `config.registry_db_path`（被 `--registry` 正确覆盖），RPC 却走另一套。

**证据链**：
1. 不注入 → 153 条生产数据；
2. 往 run 根 registry 插入标记行（`workspace_id=999001`）→ 不出现（证明确实没读 run 根）；
3. 在 **daemon 进程 env** 注入 `CW_DAEMON_REGISTRY_DB=<run根>` → 立即改为
   `Workspaces (1 total)` 只含标记行（先后撞见 `owner_uid`/`registered_at` 列类型错误，
  证明已切到 run 根库）；
4. 停掉隔离 daemon → CLI 报 `E_HTTP_MANIFEST_STALE: manifest PID <隔离 pid> 已不存活`，
   证明 CLI 无生产回退（回退链本身是干净的）。

**影响**：任何以 `--registry <隔离路径>` 启动的测试/多库 daemon，其 `workspace.list`
（及同族 RPC）都会读**生产 registry**——验收隔离被静默穿透，且可能把生产 workspace
数据当作测试基线。

**处置（TASK_BREAKDOWN §6 P-FIX）**：
- source finding：F-DAEMON-01（本文件 + memory 台账）
- 受阻任务：TP-09（本卡以 harness 注入 workaround 绕过，功能验证不阻塞）
- source revision：`c053763`（TP-06 候选构建源）；HEAD 见 build_results.json
- 具体能力缺口：`list_unified_workspaces` 未复用 daemon 启动期已解析的
  `config.registry_db_path`，而是按不同 env 名 + 硬编码默认值二次解析
- 建议修复方向（须独立合同，不由本卡实施）：让 RPC 侧 registry 解析复用启动期
  `config.registry_db_path`（或在 `DaemonConfig` 上缓存并在 handler 注入），
  删除/统一 `CW_DAEMON_REGISTRY_DB` 与 `CALLWARDEN_DAEMON_REGISTRY_DB` 的双轨命名
- 回归 oracle：本卡 `test_fdaemon01_isolation_guard` 已把两个源码常量钉为断言
  （`config.rs` 的 `DEFAULT_DATA_ROOT` 与 `task_collab.rs` 的解析命名），
  修复后须同步更新该断言并复跑本套件

**harness workaround（本卡采用，非产品修改）**：`init_harness.InitializationEnv.start_daemon()`
在 daemon 启动前向进程 env 注入 `CW_DAEMON_REGISTRY_DB=<run根>/registry.db`。

## 4. 正向义务

| 义务 | 测试 |
|---|---|
| 空目录起点 | `test_empty_workspace_dir_starts_clean` |
| 公开 register/list，无 SQL | `test_public_workspace_register_and_list`、`test_no_sql_helper_used` |
| 任务库 schema/迁移就位 | `test_task_db_schema_and_migration_ready` |
| 隔离 daemon 是 TP-06 冻结候选 | `test_daemon_candidate_is_frozen_binary` |
| register→set 公开链 | `test_task_entities_fail_closed_without_snapshot`（含 set 成功断言） |

## 5. 负向义务

| 场景 | 测试 / 期望 |
|---|---|
| F-DAEMON-01 隔离穿透 | `test_fdaemon01_isolation_guard`：源码常量双探针 + harness 注入断言 |
| registry 越界到生产 | `test_registry_path_must_stay_inside_run_root`：覆盖值必须在 run 根内且≠生产路径 |
| 外来 UID workspace 激活 | `test_foreign_owner_workspace_activation_forbidden`：必须 `workspace_forbidden` |
| 重复 register 静默分裂 | `test_repeated_register_is_reproducible`：计数不降、单次增量 ≤1 |
| task 实体无 snapshot | `test_task_entities_fail_closed_without_snapshot`：结构化 fail-closed，无 Traceback |
| lease 未知 task | `test_lease_status_fail_closed_for_unknown_task`：不崩溃，rc∈{0,2} |

## 6. 边界与未决

- **task/lease 实体全链未闭合**：空 workspace 无已发布 snapshot，任务库 `workspaces` 表
  为空 → `task` 实体 fail-closed。完整实体前置需「发布 snapshot」阶段（非空 workspace +
  build_graph），超出本卡「空目录公开初始化」范围，**显式记为未决**，不用 fixture 补绿。
- 未创建正式 daemon 任务（§7 入库门禁未过），不伪造 task_id。
- 与旧 `tests/convergence` 隔离：`--noconftest`，未改任何旧 conftest/fixture。
- F-DAEMON-01 的**产品修复**须独立合同（§6 P-FIX 流程），本卡不实施产品改动。
