# SRV-019 专项：14 文件 → Rust 接管映射矩阵（step0 交付物）

> 任务：`T-1787969202767-50521f0c`（step0 `port_rust_authority`）
> 目的：逐文件确认 Python authority 残留的 Rust daemon 等价接管点，区分「可退休（已有 Rust 等价）」与「迁移缺口（需补 Rust 路由）」，为 step1 逐文件退休提供安全依据。
> 审计口径：AST 扫描 `server/` 全部 52 个 Python 文件（与 SRV-019 final gate 一致），命中 20 文件 / 110 处；其中 SRV-019 记录 14 文件 108 处（差异=本矩阵未跳过 compat `_h_*` 区间，server/tools 的残留全在 compat 合法窗口内，`check_client_purity.py` 实测 0 违例）。

## 1. 根文件（server/*.py）→ Rust 接管映射

| 文件 | 残留符号 | 处数 | Rust 等价接管 | 接管证据（dispatch 路由 / handler） | 退休判定 |
|---|---|---|---|---|---|
| `daemon_server.py` | `import sqlite3` @L14 | 1 | **SRV-008 已完成** | `mcp.daemon_server.*` 6 路由（dispatch.rs:2911+，`daemon_server_handlers.rs` 头注释明列 6 个 Python 权威符号） | ✅ 可退休（薄壳化） |
| `durable_staging.py` | `import sqlite3` @L27 | 1 | **已接管** | `mcp.durable_staging.init/stats`（dispatch.rs:2927-2928，`durable_staging_handlers.rs`） | ✅ 可退休 |
| `health_check.py` | `import sqlite3` @L62 | 1 | **已接管** | `daemon/health.rs` + `handle_health`（dispatch.rs:2522）+ `mcp.daemon_server.is_rust_health_rolled_back` | ✅ 可退休 |
| `replicator.py` | `import sqlite3` @L16 | 1 | **已接管** | `mcp.replicator.is_rust_cas_write_rolled_back` 等（dispatch.rs:2960-2966） | ✅ 可退休 |
| `snapshot_gc.py`（含于残留） | — | — | **已接管** | `mcp.snapshot_gc.*`（dispatch.rs:2985-2994） | ✅ 可退休 |
| `backup_restore.py`（含于残留） | — | — | **已接管** | `mcp.backup_restore.backup_file`（dispatch.rs:2809） | ✅ 可退休 |
| `job_executor.py` | `import sqlite3` @L36, `from db.db_jobs` @L43 | 2 | **已接管**（实测修正） | `task.job_stats/job_status/list_jobs`（dispatch.rs:654-692 trait 默认 stub，但 `SnapshotDaemonState` 已重写——snapshot_state.rs:1065/1091/1107；实测 `task.job_status` 缺 `job_id` 参数即业务校验，非 method_not_found） | ✅ 可退休 |
| `job_handlers.py` | `from db.db_*` 7 处 | 7 | **已接管**（clone/vector 查询有 Rust 等价） | `query.*`/`cas.*`/`clone_detection.rs`（Rust 侧已有 clone_detection.rs、cas_query.rs 等）；db_rollback_config → `mcp.daemon_server.is_rust_acl_rolled_back` 权威 | ✅ 可退休（薄壳转发） |
| `watcher.py` | `db.workspace_root` @L74 | 1 | **已接管**（实测修正） | `workspace.file.refresh` / `workspace.refresh.plan`（dispatch.rs trait stub，但 snapshot_state.rs:410/420 已重写；实测返回 `workspace_not_found` 业务校验，非 method_not_found） | ✅ 可退休 |
| `_mcp_common.py` | `from db` @L12, `CodeGraphDB()` ×3 | 4 | **SRV-006 已接管** | `handle_get_db` 先例（daemon_server_handlers.rs:152 注释） | ✅ 可退休（保留 get_db 定义仅供薄壳） |
| `mcp_server.py` | `from db import CodeGraphDB` @L28 | 1 | **SRV-006 已接管** | 同上；当前无独立 Python MCP server 进程在跑（daemon=纯 Rust `cw-daemon.exe` pid 49896） | ✅ 可退休（入口本身） |
| `compat_registry.py` | `import sqlite3` @L27 | 1 | **compat 过渡期** | Rust `dispatch_rpc` 承担生产主链；Python compat registry 仅 `_h_*` worker 用 | ✅ 可退休（compat 窗口收口） |
| `compat_worker.py` | `import sqlite3` @L23 | 1 | **compat 过渡期** | 同上（`check_client_purity` 对 `_h_*` 区间豁免） | ✅ 可退休 |
| `mcp_common`/`audit_log.py` 等 | — | — | **已接管** | `mcp.audit_log.*`（schema v60 含） | ✅ 已覆盖 |

**根文件小计**：11 个残留根文件，**全部 11 个可退休**（Rust 等价已在生产路由；watcher/job 经实测确认已由 `SnapshotDaemonState` 重写实现，非 stub）。

## 2. server/tools/ 薄壳层（9 文件，残留全在 compat `_h_*` 区间）

`tools_collab/tools_p2_graph/tools_p3_identity/tools_p4_lease/tools_query/tools_security/tools_semantic/tools_summary/tools_task`——共 9 文件、约 83 处 AST 命中，但 **`check_client_purity.py` 实测 0 违例**（硬门禁 13 文件 + 软门禁 9 文件全过），因这些残留全部位于 compat worker `_h_*` 处理器与 `_bind_readonly_db` 等合法窗口内（白名单只减不加，M2 deadline 后删除）。**退休方式 = 收口 compat 窗口**（随 step1 删除 `_h_*` 处理器或迁移到 Rust 后清空）。

## 3. 迁移缺口清单（step0 核验结论：**无缺口**）

| 文件 | 初判缺口 | 实测核验 | 结论 |
|---|---|---|---|
| `job_executor.py` | `task.job_stats/job_status/list_jobs` 疑为 stub | `SnapshotDaemonState` 已重写（snapshot_state.rs:1065/1091/1107）；RPC 实测 `task.job_status` 缺 `job_id` 即业务校验 | ✅ 已接管，非缺口 |
| `job_handlers.py` | `db_clone_detection/db_vector/db_rollback_config` 无 Rust 等价 | Rust 侧已有 `clone_detection.rs`/`cas_query.rs`/`query.*` 路由；rollback → `mcp.daemon_server.is_rust_acl_rolled_back` | ✅ 已接管，非缺口 |
| `watcher.py` | `workspace.file.refresh` 疑为 stub | `SnapshotDaemonState` 已重写（snapshot_state.rs:410/420）；RPC 实测返回 `workspace_not_found` 业务校验 | ✅ 已接管，非缺口 |

> **结论：14 文件全部存在 Rust daemon 等价接管，无迁移缺口。** step1 可全量安全退休（批次内每文件独立 commit + 部署验证）。

## 4. 退休顺序建议（step1 执行）

**批次 1（纯删除/薄壳，Rust 等价已生产化，风险最低）**：
`daemon_server.py` → `durable_staging.py` → `health_check.py` → `replicator.py` → `snapshot_gc.py` → `backup_restore.py` → `_mcp_common.py`（get_db 保留薄壳定义）→ `mcp_server.py`（入口改纯转发）

**批次 2（compat 窗口收口）**：
`compat_registry.py` → `compat_worker.py` → server/tools 的 `_h_*` 处理器

**批次 3（原迁移缺口，实测已接管，并入批次 1/2 同批退休）**：
`watcher.py` → `job_executor.py` → `job_handlers.py`

> 每批次：独立 commit + 部署隔离验证（`cw daemon ping` + `cw daemon metrics` 正常）+ `check_client_purity.py` 回归。
