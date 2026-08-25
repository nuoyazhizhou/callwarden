# SRV-003 完成证据（zero-authority evidence）

**任务：** `T-1787323460500-b9e232bc` — SRV-003 [governance_projection]：server backup restore Python authority → Rust daemon
**父任务：** `T-1787293451688-c14b1e44`（Call Warden A′ migration）
**port_type：** `governance_projection`（narrow 端口，仅收敛仍直接 open SQLite 的两个助手，非全套 7 方法备份套件）
**执行角色：** Executor（worktree `pilot-srv-003`，路径 `C:/git_work/callwarden-wt/srv-003`）
**执行路线：** Route B —— thin-client 重构 + Rust native authority 下沉（与 SRV-002 同构）

---

## 1. 不变量（invariants）

- Python `server/backup_restore.py` 中仍直接 open SQLite 的两个助手退化为纯 daemon RPC 薄客户端：
  * `BackupManager._backup_file` → `mcp.backup_restore.backup_file`（不再 `import sqlite3`、不再 `open` 本地 DB、不再执行业务 SQL）。
  * `_is_rust_backup_rolled_back` → `mcp.backup_restore.is_rust_backup_rolled_back`（读 daemon 权威 `rollback_config`，fail-soft 视为未回滚）。
- daemon（Rust `backup_restore_handlers`）成为单文件快照（`.db` 走 VACUUM INTO 一致性快照，其它走文件复制）+ sha256 + `rollback_config` 读写的**唯一权威**。
- fail-closed / fail-soft 边界：
  * `backup_file` 是 admin-only `Protected_Mutation`（写盘）：daemon 不可用时薄客户端上抛
    `DaemonUnavailableError`，**绝不**回退 Python SQLite 业务路径；源文件不存在时 daemon 返回
    `null` → 薄客户端透传 `None`（对齐旧语义）。
  * `is_rust_backup_rolled_back` 是只读 authority 读：daemon 不可用时 fail-soft 视为未回滚
    （返回 `False`，捕获异常不抛），与 metrics/health/audit 的 rollback 探测保持一致模式。
- 与既有 `workspace.rs::handle_backup`/`handle_restore`（registry DB `VACUUM INTO`、属
  `ADMIN_ONLY`+`PROTECTED_MUTATION`）**解耦**：SRV-003 收敛的是 `backup_restore.rs` 全量备份的
  两个 Python 助手，不触碰那两个既有 RPC。

## 2. Before / After AST scan（server/backup_restore.py）

### Before（迁移前）
`_backup_file` 用 `sqlite3.connect` / `conn.backup()` 做单文件 SQLite 备份；`_is_rust_backup_rolled_back`
用 `sqlite3.connect(registry_db)` 读 `rollback_config` 判断 feature 是否回滚。两者均在 Python 侧
持有 SQLite 权威与业务 SQL。

### After（迁移后）
模块**删除两处 SQLite 业务**：`_backup_file` 仅 `return _call_daemon_rpc("mcp.backup_restore.backup_file", {...})`；
`_is_rust_backup_rolled_back` 经 `_call_daemon_rpc("mcp.backup_restore.is_rust_backup_rolled_back", {})`
读取 daemon 权威（保留 60s 缓存，异常 → 视为未回滚）。模块级 `import sqlite3` 已不存在（仅 docstring
合法描述 daemon 行为）。

> 注意：本文件其余部分（`BackupManager` 全量备份/恢复）仍通过 PyO3 `callwarden_core` 走 Rust
> `backup_restore.rs`，属既有实现，不在 SRV-003 收敛范围；AST 扫描仅对两个已迁移助手做零权威核对。

### 静态命中核对（AST scan，`tests/test_srv_003.py::test_no_sqlite_authority_in_source`）
| 检查项 | 结果 |
|---|---|
| `import sqlite3` / `from sqlite3 import` | **0**（已移除） |
| 函数体含 `VACUUM INTO` / `wal_checkpoint` / `PRAGMA`（排除 docstring） | **0** |
| 函数属性 `sqlite3` | **0** |

> 运行时指纹：同上测试用 monkeypatch 将 `callwarden.server.backup_restore._call_daemon_rpc`
> 替换为内存态 `FakeBackupDaemon`，断言 `backup_file` 缺参时 daemon 端 `invalid_params` 被透传、
> 源缺失时返回 `None`、rollback 探测 reads daemon 权威。

## 3. Rust native authority 接线

新增模块 `rust_ext/src/daemon/backup_restore_handlers.rs`（2 个 handler + 6 单测），并在收敛面 4 处接线：

| # | 文件:行 | 改动 |
|---|---|---|
| 1 | `rust_ext/src/daemon/mod.rs` (`pub mod backup_restore_handlers;`) | 新模块声明 |
| 2 | `rust_ext/src/daemon/dispatch.rs` `CONVERGENCE_RPC_METHODS` | 新增 `mcp.backup_restore.backup_file` / `mcp.backup_restore.is_rust_backup_rolled_back` |
| 3 | `rust_ext/src/daemon/dispatch.rs` `ADMIN_ONLY_METHODS` | 新增 `mcp.backup_restore.backup_file`（admin 才可调写盘快照） |
| 4 | `rust_ext/src/daemon/dispatch.rs` `PROTECTED_MUTATION_METHODS` | 新增 `mcp.backup_restore.backup_file`（串行化写） |
| 5 | `rust_ext/src/daemon/snapshot_state.rs` `handle_convergence_rpc` | `use super::backup_restore_handlers as backup;` + 2 个 match 分支扇出 |
| 6 | `rust_ext/src/daemon/http_server.rs` `build_capability_registry` | 新增 2 个 `add(...)`，owner=`T-1787323460500-b9e232bc#SRV-003`，backup_file=write/authority，is_rust_backup_rolled_back=read_only/authority |

### Handler 语义（`backup_restore_handlers.rs`）
- `handle_backup_file(&Value)`：参数缺失 → `invalid_params`；源非文件 → 返回 `null`（薄客户端透传 `None`）；
  `.db` → 先 `wal_checkpoint(PASSIVE)` 再 `VACUUM INTO`（失败降级 `fs::copy`）；其它 → `fs::copy`；
  返回 `{"name","type":"file","size","sha256","source_path"}`（sha256 流式 64KB 计算，无 py/GIL 依赖）。
- `handle_is_rust_backup_rolled_back(&Path)`：registry 路径为空 / 表缺失 / 查询失败 →
  `{"rolled_back": false, "reason": ...}`（fail-closed 永不抛）；否则读 `rollback_config`
  `WHERE feature_name='rust_daemon_backup_compute'` 返回 `{"rolled_back": value==1}`。

## 4. Fixture 负向矩阵（tests/test_srv_003.py，11/11 通过）

覆盖 task step[2] `fixture_negative_matrix`：`["success", "invalid", "authority", "unavailable", "restart"]` + 零权威 AST 扫描。

| 场景 | 期望 |
|---|---|
| success（backup_file） | 返回 `{"name","type":"file","size","sha256","source_path"}` |
| success（rolled_back） | 默认 daemon 报告未回滚 → `False` |
| success（missing source） | 源缺失 → daemon 返回 `null` → 薄客户端返回 `None` |
| invalid | 缺必填参数 → daemon 端 `invalid_params`（经 `FakeBackupDaemon` 透传） |
| authority（rollback 归属） | `mcp.backup_restore.is_rust_backup_rolled_back` 返回 daemon 权威 `rolled_back` |
| authority（委托） | `_backup_file` 实发 `mcp.backup_restore.backup_file` 且参数完整 |
| unavailable（backup_file fail-closed） | daemon 不可用 → 抛 `DaemonUnavailableError`，不回退本地 SQLite |
| unavailable（rolled_back fail-soft） | daemon 不可用 → `False`（捕获异常不抛） |
| restart（backup_file） | 首次不可用 → 恢复后成功 |
| restart（rolled_back） | 失效 60s 缓存后重新探测 → 读到正确回滚位 |
| 零权威 AST | 两助手不含 `sqlite3` / `VACUUM INTO` / `wal_checkpoint` / `PRAGMA` |

运行命令（worktree 根包名为 `srv-003` 非 `callwarden`，pytest 经 `pytest_link` 符号链接 + package-free
临时目录强制 worktree 优先于 `.venv_test` 指向主仓库的可编辑安装）：
```bash
# 见 C:/Users/wanpi/.callwarden/run_srv003.py（强制 sys.path[0]=pytest_link 并清洗主仓库路径）
# 结果：11 passed
```

## 5. Rust 编译验证

- `cargo check -p callwarden-core`（`source scripts/msvc-env.sh`，MSVC 环境）通过（lib + bin，仅既有警告）。
- `cargo test -p callwarden-core backup_restore_handlers`：**6 passed**
  （plain_copy_and_sha256 / missing_source_returns_null / invalid_params / db_vacuum_into /
  is_rust_backup_rolled_back_reads_flag / is_rust_backup_rolled_back_unconfigured，均基于临时文件/
  `:memory:` 连接，不触碰本地权威 SQLite）。

## 6. Handoff manifest

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立复现 backup_file / is_rust_backup_rolled_back 的 HTTP/daemon success 与负向矩阵，
              并以静态扫描（AST 禁 sqlite3/VACUUM INTO/wal_checkpoint/PRAGMA）与运行时探针
              （monkeypatch _call_daemon_rpc 为 FakeBackupDaemon）核验 server/backup_restore.py
              已无 SQLite 权威或业务 fallback；并确认 Rust 2 handler 与 capability 注册一致。
  reason: SRV-003 将 server backup/restore 子系统中仍直接 open SQLite 的两个助手完整下沉至
          Rust daemon；完成态要求 Python 仅为 thin client/adapter（governance_projection 收敛）。
  independence_requirement: required
  note: 既有 workspace.rs::handle_backup/handle_restore（registry DB VACUUM INTO）与 SRV-003 解耦，
        不在此次收敛范围；meta_checksum 因 daemon 无 GIL 未移植（改为纯 Rust 流式 sha256）。
        worktree 根包名为 srv-003，pytest 需经符号链接以 callwarden 名义导入（见 §4）。
```

## 7. step 完成映射

| step | PK | action | 文件 | status |
|---|---|---|---|---|
| 0 | `S-1787323460502-b9fca05c` | port_rust_authority | `backup_restore_handlers.rs` + dispatch/http_server/mod/snapshot_state 接线 | 完成（cargo check + 6 单测通过） |
| 1 | `S-1787323460502-b9fe0758` | retire_python_authority | `server/backup_restore.py`（删两处 SQLite 业务，改 daemon RPC 薄客户端） | 完成（AST 扫描 0 命中） |
| 2 | `S-1787323460503-ba01104c` | fixture_negative_matrix | `tests/test_srv_003.py` | 完成（11/11 通过） |
| 3 | `S-1787323460503-ba024930` | zero_authority_evidence | `deliverables/software-company/SRV-003_evidence.md` | 完成（本文件） |
