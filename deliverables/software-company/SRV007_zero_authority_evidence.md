# SRV-007 Zero-Authority Evidence Manifest

任务：`T-1787323461012-d8597160`（SRV-007 [control_plane]：server daemon protocol Python authority → Rust daemon）
Executor：`executor-workbuddy-v1-cur`（session `cw-exec-workbuddy-20260824`，model `workbuddy`）
基线：`f36bf24`（SRV-007 前）→ 提交链 `2bdf705`（step0）→ `3bdebd9`（step1）→ `1631c2e`（step2）→ 本文件（step3）

## 合同锚点

| 合同 | 版本 | sha256 |
|---|---|---|
| Task Contract `TC-T-1787323461012-d8597160` | rev2 | `6df87de488d549fcd19ad41e517ca18b077417639bc8ac13704002d4dec54d38` |
| Role Contract executor `rcr-...-executor-r1` | r1 | `3d872be2d45974ede3cc135d04f5a4d15f47d7ade26d13348dda1cb4ef57466c` |
| canonicalization rules | role-contract-c14n/v1 | `59ad755be8740794624c927294f95515d2b17790ffc21b58f6d9cf7155ff188d` |

白名单（Role Contract allowed_paths）：`server/daemon_protocol.py`、`rust_ext/src/daemon/dispatch.rs`、`rust_ext/src/daemon/http_server.rs`、`rust_ext/src/daemon/daemon_protocol_handlers.rs`、`tests/test_srv_007.py`、`deliverables/software-company/`。
实际改动 = 白名单 5 文件 + `rust_ext/src/daemon/mod.rs`（白名单外编译必需配套，见 finding-1）。

## check_items 核验（对应 acceptance_clauses）

### 1. Python module no longer opens SQLite or executes business query — PASS

before/after AST 扫描（`git show f36bf24:server/daemon_protocol.py` vs HEAD，函数体排除 docstring）：

| 状态 | `_is_rust_protocol_rolled_back` 函数体 SQLite 权威 token | 帧收发调用点 | RPC 依赖 |
|---|---|---|---|
| before（f36bf24） | `sqlite3`、`DB_PATH`、`SELECT`、`.connect(`、`rollback_config`（5 项违规） | 5 | 无 |
| after（HEAD） | **0 项违规** | 5（保持） | `_call_daemon_rpc` |

5 个调用点（send_message L137 / recv_message L168 / send_message_with_fds L211 / recv_message_with_fds L265 / parse_response L309）签名与条件分支零改动。模块级无 `sqlite3` 导入，无 `callwarden.config` 导入。

### 2. Rust target owns authority — PASS

- step0：`rust_ext/src/daemon/daemon_protocol_handlers.rs::handle_is_rust_protocol_rolled_back` 读权威库 `rollback_config`（feature=`rust_daemon_protocol`），SQL 语义逐字对齐 Python 旧实现（`WHERE feature_name=? ORDER BY updated_at DESC LIMIT 1`，COALESCE 行缺失/NULL→0），fail-soft（库不可打开/表缺失→`{rolled_back:false,reason}`）对齐 Python `except Exception→False` 与 SRV-003 先例。
- dispatch.rs 分支 `mcp.daemon_protocol.is_rust_protocol_rolled_back`（收敛 RPC catch-all 之前，只读）。
- http_server.rs capability 注册：`rust_native/available/read_only/authority`，owner `T-1787323461012-d8597160#SRV-007`，带 ok/err fixture。
- 直接 RPC 实测（真实 daemon PID 7924）：`{"rolled_back": true}`（权威库 rollback_config id=17 `rust_daemon_protocol` rollback_flag=1）。

### 3. HTTP/client semantics retained — PASS

- rollback=True 状态下真实 socket 帧收发往返：`send_message`/`recv_message` 双向 echo 逐字相等（PASS）。
- `parse_response` ok 路径返回 result、error 路径抛 `DaemonRemoteError(code="E_X")`（PASS）。
- 薄客户端实测：直接 RPC True；薄客户端 True（30.3ms）→ 缓存命中 True（0.00ms）。
- 语义一致性：`config.DB_PATH`=`~/.callwarden/callwarden.db` 与 daemon 权威库**同一文件**，老实现本地查询结果 True = 新薄客户端 True（无行为翻转）。

### 4. negative matrix passes — PASS

`tests/test_srv_007.py` 13 passed in 0.25s（success 3 / invalid 3 / authority 2 / unavailable 1 / restart 1 / reentry 2 / AST 门禁 1）。内存态 `FakeProtocolDaemon` 覆盖 `mcp.daemon_protocol.is_rust_protocol_rolled_back` 接缝；monkeypatch 模块级 `_call_daemon_rpc`（同 SRV-005 模式）；不依赖真实 daemon、不触碰本地 SQLite。

### 5. no local fallback — PASS

fail-soft 语义：daemon 不可用时 `_is_rust_protocol_rolled_back` 返回 False（视为未回滚），绝不回退本地 SQLite（函数体零 sqlite3/config 导入，AST 门禁测试固化）。

## runtime 指纹

| 项 | 结果 |
|---|---|
| cargo test --lib daemon_protocol_handlers | 6 passed（step0：flag set/unset、latest-row-wins、row/table missing fail-soft、other feature ignored） |
| cargo test --lib daemon::dispatch / daemon::http_server | 61 / 10 passed（回归无破坏） |
| pytest tests/test_srv_007.py | 13 passed |
| 相邻回归 pytest test_srv_003+005+006 | 41 passed |
| daemon 部署 | `scripts/refresh_shared_runtime.ps1 -TaskId T-1787323461012-d8597160` evidence `20260826-190256-2bdf7058b1e4-0923c6b7.json` status=passed，binary sha256 `2d532c9f...`，smoke `cw --version` + `cw daemon ping` 全过 |
| daemon 运行时 | PID 7924（沙箱外拉起，见 finding-5），endpoint `http://127.0.0.1:4205`，manifest pid/进程一致 |
| capability 上线证据 | 直接 RPC `mcp.daemon_protocol.is_rust_protocol_rolled_back` → `{"rolled_back": true}`（非 method_not_found） |
| inject_workspace_id 连锁探针 | `{"injected": true}`（SRV-006 finding-11 旧二进制连锁问题随部署消除） |

## handoff manifest

| step | step_id | report request_id | commit |
|---|---|---|---|
| step0 port_rust_authority | S-1787323461013-d86d6fbc | req-srv007-step0-report-20260826-01 | 2bdf705 |
| step1 retire_python_authority | S-1787323461013-d86e9770 | req-srv007-step1-report-20260826-01 | 3bdebd9 |
| step2 fixture_negative_matrix | S-1787323461013-d86f6b28 | req-srv007-step2-report-20260826-01 | 1631c2e |
| step3 zero_authority_evidence | S-1787323461013-d8705f4c | req-srv007-step3-report-20260826-01 | 本文件 |

## findings

1. **mod.rs 白名单外配套**：`rust_ext/src/daemon/mod.rs` 新增 `daemon_protocol_handlers` 模块声明为编译必需，超出 allowed_paths，与 SRV-006 finding 同一性质，提请 Reviewer 知悉。
2. **http_server.rs 工作树外部偏离**：工作树相对 HEAD 有 +2200 行非本任务改动，step0 capability 注册经 HEAD 基线补丁重建后逐 hunk 隔离提交（4 文件 +196，零外部混入）。
3. **协议传输层自依赖环（SRV-007 特有）**：daemon_protocol 是 RPC 帧传输层本身（UnixDaemonRpcClient 经 send_message/recv_message/parse_response 传输），缓存过期瞬间存在 `_is_rust_protocol_rolled_back→RPC→send_message→_is_rust_protocol_rolled_back` 递归；以 `_ROLLBACK_QUERY_STATE` in-flight 重入守卫解决（in-flight 视为未回滚且不写缓存），2 个专用测试固化。
4. **daemon 部署时差**：运行中旧二进制 daemon 无新 RPC 方法且 manifest 过期（`E_HTTP_MANIFEST_STALE`），step0 report 曾被阻塞；经 `refresh_shared_runtime.ps1` 重新构建部署后消除。后续 SRV 卡在 step0 提交后应先部署再 report。
5. **沙箱 Job 连带杀进程**：沙箱内 Start-Process/后台终端拉起的 daemon（PID 9256/22836）均在父命令树结束后被杀；沙箱外拉起（PID 7924）存活。无人值守循环中 daemon 重启需沙箱外执行。
6. **compat_worker 存量崩溃（HEAD 即有）**：`server/tools/tools_p2_graph.py` L421 `_P2_READ_ONLY_METHODS` 为空 dict → `register_compat_routes` 抛 `ValueError: methods 不能为空`；git status 证实文件无工作树改动，属 HEAD 存量 bug，且该文件在 SRV-007 forbidden_paths 内，未修复仅记录。
7. **权威库 rollback 现状**：`rust_daemon_protocol` rollback_flag=1（Phase 4 历史条目 id=17），帧收发按权威库语义走 Python 路径；新旧实现结果一致（均 True），非迁移引入的行为变化。若未来需恢复 Rust 短路，应经 rollback_config 权威写点置 0。
8. **fail-soft 观测性**：daemon 不可用时薄客户端静默返回 False 并缓存 60s，期间无法区分"真未回滚"与"daemon 不可用"；与 SRV-003 先例语义一致，接受该取舍。
