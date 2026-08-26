# SRV-005 零权威证据 manifest（server daemon autostart Python authority → Rust daemon）

- **任务**: `T-1787323460652-c2eaada8`（SRV-005，port_type=control_plane，route B thin-client）
- **Task Contract**: rev2 `sha256:62295d28db5850d22bcfe7bc1f4e67fb0a04297545e6bbda6b056688a053c351`
- **Role Contract**: r1 `sha256:2f1c874d23bd34cd07a8c451728bf264ede21df37d61540e791b92265401ae66`
- **Executor**: `executor-workbuddy-v1-cur` / session `cw-exec-workbuddy-20260824` / model `workbuddy`
- **退役目标**: `server/daemon_autostart.py` 三个本地 socket connect 探测权威函数
  `_try_connect_tcp` / `_try_connect_unix` / `try_http_connect`
  → Rust handler `rust_ext/src/daemon/daemon_autostart_handlers.rs`
  （`handle_try_connect_tcp` / `handle_try_connect_unix` / `handle_try_http_connect`）

## 1. AST / 静态扫描 before / after

**before**（`1886ef7~1` 版本，三函数体内直接权威调用）：

| 函数 | 静态命中 |
|---|---|
| `_try_connect_tcp` | L194 `socket.socket(AF_INET, SOCK_STREAM)`；L196 `sock.connect((host, int(port)))` |
| `_try_connect_unix` | L216 `socket.socket(AF_UNIX, SOCK_STREAM)`；L218 `sock.connect(endpoint)` |
| `try_http_connect` | L900 `from urllib.parse import urlparse`；L906 `socket.socket`；L908 `sock.connect((host, port))` |

**after**（当前 worktree，`1886ef7`）：

| 函数 | 现状 |
|---|---|
| `_try_connect_tcp` | L223 `_call_daemon_rpc("mcp.daemon_autostart.try_connect_tcp", ...)`，纯薄客户端 |
| `_try_connect_unix` | L269 `_call_daemon_rpc("mcp.daemon_autostart.try_connect_unix", ...)`，纯薄客户端 |
| `try_http_connect` | L1031 `_call_daemon_rpc("mcp.daemon_autostart.try_http_connect", ...)`，返回 bool 签名兼容 |

三函数体 AST 扫描（剥 docstring）零命中：`socket.socket` / `.connect(` / `urllib` /
`urlparse` / `sqlite3` / `get_db(` / `PRAGMA ` / `SELECT `（`tests/test_srv_005.py::
test_no_probe_authority_in_source` 门禁持续看守）。

模块级 DB authority 扫描（剥注释/docstring）：无 `sqlite3` 导入、无 `get_db(`、
无业务 SQL——满足 check items `no SQLite / no get_db / no business fallback`。

**残留 socket 站点说明（非退役目标）**：
- `_transport_connect_tcp`（L245/247）/ `_transport_connect_unix`（L287/289）：
  transport bootstrap——客户端连接自身 daemon 的活 socket 为物理必需
  （`daemon_client.py:428 conn = try_connect(...)` 依赖活连接做帧通信；
  RPC 本身依赖该连接，无法经 RPC 传递 socket）。transport 适配职责，
  非 DB/业务 authority，不 open SQLite、不执行业务 SQL。
- `_try_connect_windows`（L299 起）：Windows 命名管道 CreateFileW，非本卡 target symbol。

## 2. Runtime 指纹

| 验证 | 结果 |
|---|---|
| `cargo test --lib daemon_autostart_handlers` | **9 passed**（解析变体、成功/不可达回环、fail-soft、invalid_params） |
| `cargo test --lib daemon::dispatch` | **61 passed**（回归无破坏） |
| `cargo test --lib daemon::http_server` | **10 passed**（capability registry 回归） |
| `pytest tests/test_srv_005.py` | **12 passed**（success 4 / invalid 3 / authority 2 / unavailable 1 / restart 1 / AST 门禁 1） |
| import 冒烟 | OK（`_call_daemon_rpc` 函数体延迟导入，无循环依赖） |
| `try_connect` transport 回环验证 | 成功路径返回活 socket；拒绝路径返回 None |
| `try_http_connect` fail-closed | daemon 不可用抛 `DaemonUnavailableError`（不回退本地探测） |
| 存量测试评估 | `tests/test_wsl_authority_routing.py` 除 1 个 monkeypatch 旧符号用例外全过；`tests/test_daemon_autostart.py` 除 1 个存量失败（`_find_daemon_binary` 环境变量优先级，与本卡无关）外全过 |

**Commits**：

| step | commit | 内容 |
|---|---|---|
| 0 port_rust_authority | `d1c6e92` | daemon_autostart_handlers.rs 新建 + dispatch 三分支 + capability 三行 + mod.rs 声明 |
| 1 retire_python_authority | `1886ef7` | 三函数 RPC 薄客户端化 + transport bootstrap 分离 |
| 2 fixture_negative_matrix | `9fadd7c` | tests/test_srv_005.py（12 passed） |
| 3 zero_authority_evidence | 本文件 | 证据 manifest |

## 3. Capability 证据（`http_server.rs`）

| method | backend | status | operation_class | scope | route | owner |
|---|---|---|---|---|---|---|
| `mcp.daemon_autostart.try_connect_tcp` | rust_native | available | read_only | authority | /v1/rpc | `T-1787323460652-c2eaada8#SRV-005` |
| `mcp.daemon_autostart.try_connect_unix` | rust_native | available | read_only | authority | /v1/rpc | `T-1787323460652-c2eaada8#SRV-005` |
| `mcp.daemon_autostart.try_http_connect` | rust_native | available | read_only | authority | /v1/rpc | `T-1787323460652-c2eaada8#SRV-005` |

三方法全只读（无 DB、无写锁），不进 ADMIN_ONLY / PROTECTED_MUTATION 清单；
dispatch 接线位于收敛 RPC catch-all 之前。

## 4. Handoff manifest

| 项 | 值 |
|---|---|
| step0 request_id | `req-srv005-step0-report-20260826-01` |
| step1 request_id | `req-srv005-step1-report-20260826-01`（change_audit `CA-e265d26a8221603bf4526dc4`） |
| step2 request_id | `req-srv005-step2-report-20260826-01`（change_audit `CA-abb53fa4bbff9be9b44c4719`） |
| step3 request_id | `req-srv005-step3-report-20260826-01`（目录型 target，changes 省略） |
| step_id | S-1787323460653-c300656c（0）/ S-1787323460654-c301bad4（1）/ S-1787323460654-c302e224（2）/ S-1787323460654-c303c068（3） |

## 5. Findings

1. **finding-1（白名单外配套）**：`rust_ext/src/daemon/mod.rs` 3.24 节模块声明
   不在 allowed_paths 内，为新 handler 编译必需配套（与 SRV-004 同模式）。
2. **finding-2（transport bootstrap 保留）**：`try_connect` 的活 socket 语义为
   客户端 transport 物理必需（鸡生蛋：RPC 依赖该连接），新增
   `_transport_connect_tcp/_transport_connect_unix` 保留最小本地连接——
   transport 适配职责，非 DB/业务 authority。
3. **finding-3（白名单外测试不兼容）**：
   `tests/test_wsl_authority_routing.py::test_try_connect_prefers_tcp_on_windows`
   monkeypatch 旧符号 `_try_connect_tcp`，退役后失效（该文件不在 allowed_paths，
   未修改）。其余 wsl 路由测试通过。
4. **finding-4（存量失败）**：
   `tests/test_daemon_autostart.py::TestFindDaemonBinary::test_env_var_takes_priority`
   在本卡改动前即失败（`_find_daemon_binary` 环境变量优先级，未触碰该函数）。
5. **finding-5（破坏性签名变更）**：`_try_connect_tcp`/`_try_connect_unix` 返回
   由 socket 变为探测 dict（RPC 无法传 socket，同 SRV-004 finding-1 模式）；
   模块内无残留调用方，`try_connect` 已改走 `_transport_connect_*`。
6. **finding-6（环境）**：`cw --refresh` 刷新本批文件失败——daemon 报
   `unable to open database file /var/lib/callwarden/workspaces/.../codegraph.db`
   （Linux 路径出现在 Windows 环境，daemon codegraph 配置异常，属环境问题）。
7. **finding-7（编译隔离）**：自测期间工作树存在 INT-001（T-1787322971676-e9aae4d4）
   未提交半成品导致编译失败；经临时 stash/注释隔离完成自测后已完整恢复
   （git stash pop + 备份覆盖），未随 SRV-005 commit 提交。
