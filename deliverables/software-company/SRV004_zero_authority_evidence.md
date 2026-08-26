# SRV-004 零权威证据 manifest（zero_authority_evidence）

- **任务**：`T-1787323460580-bea19180`（SRV-004：server cli admin Python authority → Rust daemon）
- **步骤**：step 3 `zero_authority_evidence`（`S-1787323460582-beb76e60`）
- **Role Contract hash**：`sha256:16d9079ab453587ea20126dd237e4fbb15530ffd0f31a42e6632d44693d693bb`
- **Executor 身份**：agent_id=`executor-workbuddy-v1-cur`，session=`cw-exec-workbuddy-20260824`，model=`workbuddy`，role=`executor`
- **日期**：2026-08-26

## 1. AST scan（before / after）

### Before（任务描述静态命中行）

`server/cli_admin.py` 静态命中行 37 / 65 / 99 / 147 / 211：

| 行号 | 原权威代码 |
|---|---|
| 37 | `sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=3)`（`open_readonly_conn`） |
| 65 | `sqlite3.connect(...mode=ro...)`（`read_pragmas` + `conn.execute(PRAGMA ...)`） |
| 99 | `sqlite3.connect(...mode=ro...)`（`connection_test` + `SELECT 1`） |
| 147 | `sqlite3.connect(...mode=ro...)`（`scan_hash_databases` 读 workspaces 表） |
| 211 | `sqlite3.connect(...mode=ro...)`（`read_task_dependencies` 三表只读查询） |

另有顶层 `import sqlite3`（原第 18 行）。

### After（提交 f0991b2 后实测）

扫描方法：`ast.parse` 全文件遍历 + docstring 剥离后文本扫描（`.tmp_srv004_ast.py`，运行于 2026-08-26）：

- `sqlite imports: NONE`（无 `import sqlite3` / `from sqlite3 import`）
- `sqlite3.* 属性访问: NONE`；`get_db()/CodeGraphDB() 调用: NONE`
- 文本残留（mode=ro / sqlite3 / conn.execute，剥离 docstring 后）: NONE
- 五个 `mcp.cli_admin.*` RPC 接线：全部 True（薄客户端函数体仅 `_call_daemon_rpc` + 结果整形）
- 测试内建 AST 门禁 `tests/test_srv_004.py::test_no_sqlite_authority_in_source` PASSED
  （禁 `import sqlite3`；五个目标函数体禁 `mode=ro`/`sqlite3.`/`conn.execute`/`PRAGMA `/`SELECT `）

结论：该 Python 模块不再 open SQLite、不再调用 `get_db()`、不执行业务 SQL、
不保留本地 business fallback（fail-closed）；仅保留 RPC 调用、JSON 结果整形与
默认路径计算等非业务适配职责。`get_default_db_path`（纯路径计算）与
`migrate_single_db`（委托 `db_migrate` 权威模块）不属于 SQLite authority，保留。

## 2. Runtime fingerprint（自测证据）

| 命令 | 结果 |
|---|---|
| `cargo test --lib cli_admin_handlers`（rust_ext） | 8 passed / 0 failed |
| `cargo test --lib daemon::dispatch` | 61 passed / 0 failed |
| `cargo test --lib daemon::http_server` | 10 passed / 0 failed |
| `python -m pytest tests/test_srv_004.py` | 14 passed / 0 failed |
| `python .tmp_srv004_ast.py`（AST + import 冒烟） | AST SCAN CLEAN；8 个公共函数齐备 |

提交指纹（git）：

| commit | 内容 |
|---|---|
| `03d8c08` | step0：cli_admin_handlers.rs 新建（5 handler + cell/dep_row_mapper）+ dispatch 五分支 + capability 五行 + mod.rs 声明（4 files, +557） |
| `f0991b2` | step1：cli_admin.py 五函数薄客户端化（1 file, +98/-166） |
| `6fc9bf0` | step2：新建 tests/test_srv_004.py 负矩阵（1 file, +309） |

## 3. Capability evidence

`rust_ext/src/daemon/http_server.rs` `build_capability_registry()` 新增 5 行（fail-closed registry）：

| method | backend | status | operation_class | workspace_scope | route | owner |
|---|---|---|---|---|---|---|
| `mcp.cli_admin.connection_test` | rust_native | available | read_only | authority | /v1/rpc | `T-1787323460580-bea19180#SRV-004` |
| `mcp.cli_admin.open_readonly_conn` | rust_native | available | read_only | authority | /v1/rpc | `T-1787323460580-bea19180#SRV-004` |
| `mcp.cli_admin.read_pragmas` | rust_native | available | read_only | authority | /v1/rpc | `T-1787323460580-bea19180#SRV-004` |
| `mcp.cli_admin.read_task_dependencies` | rust_native | available | read_only | authority | /v1/rpc | `T-1787323460580-bea19180#SRV-004` |
| `mcp.cli_admin.scan_hash_databases` | rust_native | available | read_only | authority | /v1/rpc | `T-1787323460580-bea19180#SRV-004` |

dispatch 权威接线（`rust_ext/src/daemon/dispatch.rs`，提交 03d8c08）：

- 五方法全部只读（daemon 内 `mode=ro` + busy 3s），**不进** `ADMIN_ONLY_METHODS` /
  `PROTECTED_MUTATION_METHODS` 清单（无写盘、无状态变更）；
- `dispatch_inner` match 在收敛 RPC catch-all 之前直调
  `cli_admin_handlers::handle_{connection_test,open_readonly_conn,read_pragmas,read_task_dependencies,scan_hash_databases}`；
- 错误语义（stable errors）：参数缺失 → `invalid_params`；库不可打开/查询失败 →
  稳定空值（空串/空列表/error 字段），与 Python 下沉前一致；
- Rust 侧单测 8 个覆盖：连通计数、不可达库、探测开/不可开、PRAGMA 白名单与
  invalid_params、task/contract 二选一校验、缺库空列表、hash 目录布局（大写 hex/
  非法名跳过）、目录不存在空列表。

## 4. Handoff manifest

| 字段 | 值 |
|---|---|
| from_role | executor |
| outcome | executor_ready_for_review |
| next_role | reviewer |
| independence_requirement | required |
| report_request_id（step0） | `req-srv004-step0-report-20260826-01` |
| report_request_id（step1） | `req-srv004-step1-report-20260826-01` |
| report_request_id（step2） | `req-srv004-step2-report-20260826-01`（change_audit `CA-f4f0b1d96ceaf27a6c8c38a0`） |
| report_request_id（step3） | `req-srv004-step3-report-20260826-02`（-01 因目录型 change_audit 归属被拒作废） |

## 5. 已知 finding（移交 reviewer 判断，非本任务 scope 内整改）

1. **open_readonly_conn 调用方不兼容**：下沉为探测语义（返回 dict）后，
   `cli/main.py:11589` 的 `context resolve` 流程（`compute_resolved_edges(conn, ...)`
   需要真实 `sqlite3.Connection` 对象做本地多 SQL 计算）运行时不兼容。
   `cli/` 与 `analyzers/` 不在本卡白名单，未修改；建议后续卡新增
   `resolved_edges.compute` daemon 下沉后再切换该调用点。
2. **mod.rs 声明超白名单一行**：新建 `cli_admin_handlers.rs` 必须在
   `rust_ext/src/daemon/mod.rs` 增加 `pub mod cli_admin_handlers;` 声明
   （否则不参与编译）；该一行是新建文件的必要配套，超出白名单六路径，特此申报。
3. **contract 分支 revision>0 微差异**：Rust mapper 对 revision>0 的依赖行输出
   `contract_revision: null` 键（Python 原 `dict(row)` 不含该键）。下游
   `cw dependency inspect` 仅做展示，影响可忽略，记录备查。
4. **多文件 step 的 change_audit 归属**：step0 `target_file` 以 `'; '` 分隔多文件，
   daemon `task_collab.rs` 按 `','` split，精确归属不可达，step0 report 省略 changes
   （commit 03d8c08 承载）；单文件步骤（step2）归属成功，证明机制本身可用。
5. **claim 角色门禁**：任务上存在既有 `independent_reviewer`（reviewer-w2-3）身份
   记录，`--role implementer` claim 触发 `E_ROLE_INDEPENDENCE_VIOLATION`；
   改用 `--role executor`（与 next-action `required_role` 一致）后成功。
