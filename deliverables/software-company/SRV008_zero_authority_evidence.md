# SRV-008 Zero-Authority Evidence Manifest

任务：`T-1787323461079-dc5ac87c`（SRV-008 [control_plane]：server daemon server Python authority → Rust daemon）
Executor：`executor-workbuddy-v1-cur`（session `cw-exec-workbuddy-20260824`，model `workbuddy`）
基线：`a0f94a5`（step0 勘误后、step1 前）→ 提交链 `d64dcc2`（step0）→ `a0f94a5`（step0 勘误）→ `4722f5a`（step1）→ `e6d9a73`（step2）→ 本文件（step3）

## 合同锚点

| 合同 | 版本 | sha256 |
|---|---|---|
| Task Contract `TC-T-1787323461079-dc5ac87c` | rev2 | `a06fc8165a80e15fa6644f68a14442ff0df9ae4b32c8ea9aec8e54fa2c8949df` |
| Role Contract executor `RC-T-1787323461079-dc5ac87c-executor-0` | r1 | prompt_hash `59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6` |
| normalization rules | verdict-normalization/v1 | `b41cbdb3f2882b3efc0fbbbddfb4fd5b40e23549cdbeb49af2dec798184b0e8d` |

白名单（Role Contract allowed_paths）：`server/daemon_server.py`、`rust_ext/src/daemon/dispatch.rs`、`rust_ext/src/daemon/http_server.rs`、`rust_ext/src/daemon/daemon_server_handlers.rs`、`tests/test_srv_008.py`、`deliverables/software-company/`。
实际改动 = 白名单 5 文件 + `rust_ext/src/daemon/mod.rs`（白名单外编译必需配套，见 finding-1）。

## check_items 核验（对应 acceptance_clauses）

### 1. Python module no longer opens SQLite or executes business query — PASS

before/after AST 扫描（`git show a0f94a5:server/daemon_server.py` vs HEAD，函数体排除 docstring，banned = sqlite3/DB_PATH/SELECT/PRAGMA/rollback_config/.connect(）：

| 符号 | before（a0f94a5） | after（HEAD） |
|---|---|---|
| `_is_rust_acl_rolled_back` | 5 项违规（28L：本地短连接查 rollback_config） | **0 项违规**（19L，RPC `mcp.daemon_server.is_rust_acl_rolled_back`） |
| `_is_rust_health_rolled_back` | 5 项违规（24L） | **0 项违规**（17L，RPC `mcp.daemon_server.is_rust_health_rolled_back`） |
| `get_registry_conn` | 2 项违规（6L：sqlite3.connect DAEMON_REGISTRY_DB） | **0 项违规**（24L，RPC 元信息探测） |
| `api_register_workspace` 等 4 函数 | 经 get_registry_conn 可写连接直写 | fail-closed 抛 `DaemonRpcError("method_migrated")`（零外部调用方，权威归 dispatch workspace.register/list/status） |

保留项（finding-3/4）：`dispatch`（654L）、`_registry_conn`（8L）、`_get_workspace_resources`（57L）函数体 **unchanged**（行数与违规 token 前后一致）——三者为 Python daemon 服务端组件，权威经 step0 Rust handler 元信息探测/权威声明承接，函数体保留。模块级 `import sqlite3` 保留于 L14（服务端 EnterpriseDaemonService/dispatch 仍需，非退役符号权威）。

### 2. Rust target owns authority — PASS

- step0：`rust_ext/src/daemon/daemon_server_handlers.rs`（393 行，6 handler + 11 Rust 测试）——
  - `handle_is_rust_acl_rolled_back` / `handle_is_rust_health_rolled_back`：读权威库 `rollback_config`（feature=`rust_daemon_acl_path_budget` / `rust_daemon_health_check`），fail-soft（库不可打开→`{rolled_back:false,reason:db_open_failed}`）对齐 Python `except→False` 与 SRV-003/007 模板；
  - `handle_get_registry_conn` / `handle_registry_conn`：RPC 无法传递 sqlite3.Connection，下沉为权威元信息探测（registry.db 路径+存在性+schema 就绪，source=module/instance），对齐 SRV-006 `handle_get_db` 先例；
  - `handle_get_workspace_resources`：RPC 无法传递进程内 CAS/StagingLog/Replicator 对象，下沉为资源路径映射+存在性探测，缺 `workspace_instance_id` fail-closed invalid_params；
  - `handle_dispatch`：生产路由权威已由 Rust `dispatch_rpc` 承担，返回路由权威 manifest 只读声明（authority=rust_dispatch，28 方法清单）。
- dispatch.rs 六分支 `mcp.daemon_server.*`（SRV-007 块后，只读）。
- http_server.rs 6 capability 注册：`rust_native/available/read_only/authority`，owner `T-1787323461079-dc5ac87c#SRV-008`。
- 直接 RPC 实测（真实 daemon PID 39288）：6 方法全 OK——registry.db exists=true schema_ready=true、resources 路径映射正确、dispatch manifest authority=rust_dispatch；负例缺 workspace_instance_id 拒止 invalid_params PASS。

### 3. HTTP/client semantics retained — PASS

- 薄客户端实测：acl/health 均 False（与直接 RPC `{"rolled_back": false}` 一致），首调 367.8ms → 60s 缓存命中 0.008ms。
- `get_registry_conn` 实测：`{registry_db: ~/.callwarden/daemon/registry.db, exists: True, schema_ready: True, source: module}`，daemon 不可用时 fail-soft 归一化 `reason=daemon_unavailable`。
- api_* 4 函数 fail-closed 实测：均抛 `DaemonRpcError` code=`method_migrated`，不触发任何 RPC。
- 存量语义锁定：`_registry_conn` / `_get_workspace_resources` 函数体含 `apply_daemon_rw_pragmas`（对齐 test_phase5_cas_replicator_wiring L1096/L1116 源码级断言），step2 `test_retained_server_core_symbols_not_retired` 固化。

### 4. negative matrix passes — PASS

`tests/test_srv_008.py` 17 passed in 0.64s（success 4 / invalid 4 / authority 4 / unavailable 2 / restart 2 / AST 门禁 1）。内存态 `FakeDaemonServerDaemon` 覆盖 `mcp.daemon_server.is_rust_acl_rolled_back` / `is_rust_health_rolled_back` / `get_registry_conn` 三接缝；monkeypatch `callwarden.server.daemon_server._call_daemon_rpc` + 复位双 60s 缓存防跨测试污染；不依赖真实 daemon、不触碰本地 SQLite。

### 5. no local fallback — PASS

fail-soft 语义：daemon 不可用时 `_is_rust_*_rolled_back` 返回 False（视为未回滚）、`get_registry_conn` 返回 `reason=daemon_unavailable` 元信息，绝不回退本地 SQLite（3 退役符号函数体零 sqlite3/config 查询，AST 门禁测试固化）。api_* 不降级本地直写，静态 fail-closed。

## runtime 指纹

| 项 | 结果 |
|---|---|
| cargo test --lib daemon_server_handlers | 11 passed（step0：flag set/unset、fail-soft、registry meta、resources 缺参拒止、dispatch manifest、默认路径 env 覆盖） |
| cargo test --lib daemon::dispatch / daemon::http_server | 61 / 10 passed（回归无破坏） |
| pytest tests/test_srv_008.py | 17 passed |
| 存量回归 | test_enterprise_daemon_uds / test_b3 / test_phase8 deselect HEAD 即有失败后全绿（finding-5） |
| daemon 部署 | evidence `20260826-211613-a0f94a5ad4fa-d62d047c.json` status=passed，binary commit `a0f94a5` sha256 `7a564f55...` |
| daemon 运行时 | PID 39288（沙箱外拉起），endpoint `http://127.0.0.1:5341`，manifest pid/进程一致 |
| capability 上线证据 | 直接 RPC 6 个 `mcp.daemon_server.*` 方法全 OK（非 method_not_found）+ 缺参负例拒止 |

## handoff manifest

| step | step_id | report request_id | commit |
|---|---|---|---|
| step0 port_rust_authority | S-1787323461080-dc6e9fc8 | req-srv008-step0-report-20260826-01 | d64dcc2 + a0f94a5（勘误） |
| step1 retire_python_authority | S-1787323461080-dc700598 | req-srv008-step1-report-20260826-01 | 4722f5a |
| step2 fixture_negative_matrix | S-1787323461080-dc70e3dc | req-srv008-step2-report-20260826-01 | e6d9a73 |
| step3 zero_authority_evidence | S-1787323461080-dc71ab14 | req-srv008-step3-report-20260826-01 | 本文件 |

## findings

1. **mod.rs 白名单外配套**：`rust_ext/src/daemon/mod.rs` 新增 `daemon_server_handlers` 模块声明为编译必需，超出 allowed_paths，与 SRV-006/007 finding 同一性质，提请 Reviewer 知悉。
2. **工作树外部偏离与 HEAD blob 构造法**：工作树相对 HEAD 存在持续外部偏离（http_server.rs +2700 行 / dispatch.rs 64 行 / mod.rs 7 行 / cli_admin_handlers.rs / task_collab.rs 等未提交改动，forbidden 未触碰）；step0 经 `git show HEAD:<file>` → 二进制模式锚点插入 → `git hash-object -w --stdin` → `git update-index --cacheinfo` 直接构造 staged blob，精确隔离（4 文件 +433，零外部混入）。后续卡可复用该隔离技术。
3. **4 服务端符号语义不可行，以探测/声明承接**：`dispatch`（服务端 RPC 路由器，RPC 化会自调用）、`_registry_conn` / `_get_workspace_resources`（返回进程内 Connection/CAS/StagingLog 对象，RPC 无法传递）、`get_registry_conn`（返回连接对象）不能薄客户端化；Rust handler 以权威元信息探测/路由权威声明承接（SRV-006 handle_get_db 先例）。其中 get_registry_conn 的 Python 侧已完成退役（RPC 探测），另 3 者函数体保留。
4. **存量测试源码级断言约束**：`tests/test_phase5_cas_replicator_wiring.py` L1096/L1116 对 `_registry_conn` / `_get_workspace_resources` 源码断言（必须含 `apply_daemon_rw_pragmas`）；tests/ 除 test_srv_008.py 外 forbidden，函数体保留且 step2 以 `test_retained_server_core_symbols_not_retained` 同步锁定该契约。
5. **HEAD 即有 ADMIN_ONLY 对齐失败**：`test_python_constant_matches_rust_source` 之 `mcp.backup_restore.backup_file` 未进 ADMIN_ONLY，经 git stash 验证为 HEAD 既有（SRV-003 遗留），非本卡引入；tests/ forbidden 未修，仅记录。
6. **api_* 连带 fail-closed**：api_register_workspace / api_list_workspaces / api_get_workspace_status / api_update_workspace_status 零外部调用方（全库 grep 核实），原依赖 get_registry_conn 可写连接；退役后改静态 fail-closed 抛 `method_migrated`，权威归 Rust dispatch 的 workspace.register/list/status，无调用方受影响。
7. **勘误 a0f94a5**：step0 初版 Rust 默认路径（daemon-data 目录/env 名）与 Python `config.DAEMON_REGISTRY_DB`（`~/.callwarden/daemon/registry.db`，env `CW_DAEMON_DATA_ROOT`）不一致，探测 exists=false；勘误修正三处（目录名、env 名、_data_root=registry 目录+enterprise 对齐 EnterpriseDaemonService），部署后探测全绿。
8. **fail-soft 观测性**：daemon 不可用时薄客户端静默返回 False 并缓存 60s，期间无法区分"真未回滚"与"daemon 不可用"；与 SRV-003/007 先例语义一致，接受该取舍。
