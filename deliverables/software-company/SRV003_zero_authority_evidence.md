# SRV-003 零权威证据 manifest（zero_authority_evidence）

- **任务**：`T-1787323460500-b9e232bc`（SRV-003：server backup restore Python authority → Rust daemon）
- **步骤**：step 3 `zero_authority_evidence`（`S-1787323460503-ba024930`）
- **Role Contract hash**：`sha256:f6761f61b1b67c374b445ee7360650b537e0f5874c49c4af57fd535c9a7d70c3`
- **Executor 身份**：agent_id=`executor-workbuddy-v1-cur`，session=`cw-exec-workbuddy-20260824`，model=`workbuddy`，role=`executor`
- **日期**：2026-08-26

## 1. AST scan（before / after）

### Before（任务描述静态命中行）

`server/backup_restore.py` 静态命中行 104 / 485 / 486：

| 行号 | 原权威代码 |
|---|---|
| 104 | `conn = _sqlite3.connect(_DB_PATH)`（`_is_rust_backup_rolled_back` 直读 `rollback_config` 表） |
| 485 | `src_conn = sqlite3.connect(src_path, timeout=5)`（`BackupManager._backup_file` 打开源 DB） |
| 486 | `dest_conn = sqlite3.connect(dest_path, timeout=5)`（`_backup_file` 打开目标 DB + `src_conn.backup`） |

另有顶层 `import sqlite3`（原第 48 行）。

### After（提交 6ea58e3 后实测）

扫描方法：`ast.parse` 全文件遍历（`.tmp_srv003_ast.py`，运行于 2026-08-26）：

- `sqlite imports: NONE`（无 `import sqlite3` / `from sqlite3 import`）
- `sqlite/get_db calls: NONE`（无 `*.connect` sqlite 调用、无 `get_db()` 调用）
- `mcp.backup_restore.backup_file` RPC 接线：True
- `mcp.backup_restore.is_rust_backup_rolled_back` RPC 接线：True
- 测试内建 AST 门禁 `tests/test_srv_003.py::test_no_sqlite_authority_in_source` PASSED
  （禁 `import sqlite3`；`_backup_file`/`_is_rust_backup_rolled_back` 函数体禁
  `VACUUM INTO`/`wal_checkpoint`/`PRAGMA`/sqlite3 attr）

结论：该 Python 模块不再 open SQLite、不再调用 `get_db()`、不执行业务 SQL、
不保留本地 business fallback；仅保留 HTTP 薄客户端适配职责。

## 2. Runtime fingerprint（自测证据）

| 命令 | 结果 |
|---|---|
| `cargo check --lib`（rust_ext） | Finished，0 error（156 既有 warning） |
| `cargo test --lib daemon::dispatch` | 61 passed / 0 failed |
| `cargo test --lib backup_restore_handlers` | 6 passed / 0 failed |
| `cargo test --lib daemon::http_server` | 10 passed / 0 failed |
| `python -m pytest tests/test_srv_003.py` | 11 passed / 0 failed |
| `python -m pytest tests/test_phase8_backup_restore.py tests/test_phase8_rust_backup_restore.py` | 47 passed / 0 failed |
| `python -c "from callwarden.server import backup_restore"` | import OK，`_call_daemon_rpc` callable |

提交指纹（git）：

| commit | 内容 |
|---|---|
| `a6ff51e` | step0：dispatch 路由 + admin 门禁 + 串行化点 + capability registry（2 files, +28） |
| `6ea58e3` | step1：backup_restore.py 薄客户端化（1 file, +35/-53） |

## 3. Capability evidence

`rust_ext/src/daemon/http_server.rs` `build_capability_registry()` 新增 2 行（fail-closed registry）：

| method | backend | status | operation_class | workspace_scope | route | owner |
|---|---|---|---|---|---|---|
| `mcp.backup_restore.backup_file` | rust_native | available | write | authority | /v1/rpc | `T-1787323460500-b9e232bc#SRV-003` |
| `mcp.backup_restore.is_rust_backup_rolled_back` | rust_native | available | read_only | authority | /v1/rpc | `T-1787323460500-b9e232bc#SRV-003` |

dispatch 权威接线（`rust_ext/src/daemon/dispatch.rs`，提交 a6ff51e）：

- `ADMIN_ONLY_METHODS` 新增 `mcp.backup_restore.backup_file`（与 backup/restore 同级，
  非 root/daemon-uid 调用被 `permission_denied` fail-closed 拒绝；
  测试 `test_admin_only_method_denied_for_non_admin` 覆盖）
- `PROTECTED_MUTATION_METHODS` 新增 `mcp.backup_restore.backup_file`（经唯一串行化点；
  `test_is_protected_mutation_classification` 覆盖正/反分类）
- `dispatch_inner` match 直调 `backup_restore_handlers::handle_backup_file` /
  `handle_is_rust_backup_rolled_back`（registry 路径取 `CW_DAEMON_REGISTRY_DB`，
  fallback `default_registry_db_path()`；fail-closed 语义由 handler 保证）

## 4. Handoff manifest

| 字段 | 值 |
|---|---|
| from_role | executor |
| outcome | executor_ready_for_review |
| next_role | reviewer |
| independence_requirement | required |
| report_request_id（step0） | `req-srv003-step0-report-20260826-04` |
| report_request_id（step1） | `req-srv003-step1-report-20260826-01` |
| report_request_id（step2） | `req-srv003-step2-report-20260826-01` |
| report_request_id（step3） | `req-srv003-step3-report-20260826-01` |

## 5. 已知 finding（移交 reviewer 判断，非本任务 scope 内整改）

1. `tests/test_c5_s4_backup_restore_unify.py` 中 5 个 `TestFallback*` 用例测试的是
   已退役的 Python fallback 文件复制权威（B3/B4/P1 布局与原子发布），且依赖**旧二进制
   daemon**（不识别新 method，返回 `method_not_found`）。该文件不在 SRV-003 白名单，
   建议由后续 legacy 测试整改任务处理（模式参照 SRV-002 重构提交 2e6b269）。
2. report changes 归属：step 0 的 `target_file` 以 `'; '` 分隔多文件，与 daemon
   `task_collab.rs` 的 `','` split 校验不一致，精确 change_audit 归属不可达；
   本轮 report 省略 changes，归属以 commit hash（a6ff51e / 6ea58e3）与本 manifest 承载。
3. `cw --refresh` 因 daemon workspace 映射到容器路径（`/var/lib/callwarden/...`）
   在本机不可用（环境限制），提交前刷新未执行；Rust/Python 变更的测试证据见 §2。
