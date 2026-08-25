# S1 独立验收测试报告 — cw 自举 CLI 纯 client 化

- **任务**: T-1787203937193-0993d120（S1：cli/main.py 296 处 db. 引用迁移为 daemon RPC，移除 check_client_purity 白名单）
- **QA**: Edward（software-qa-engineer）
- **被测对象**: `cli/main.py`（RpcDBProxy）、`scripts/check_client_purity.py`、`server/cli_admin.py`
- **日期**: 2026（Round 1 + Round 2 回归）
- **方法**: 独立重跑门禁 + AST/源码静态抽查 + 独立环境变量 E2E fail-closed + 回归导入 + cli 相关测试套件

---

## Round 2 回归结论（工程师修复后）✅ **PASS — Routing: NoOne**

| 回归项 | 结果 |
|---|---|
| `tests/convergence/test_m4_cli_fail_closed.py`（QA 修复隔离后） | 6 passed |
| `tests/test_cw_client_rpc_proxy.py`（QA 更新断言后） | 12 passed |
| `tests/test_cli_main_help.py` | 27 passed |
| 三套件合跑 | **45 passed** |
| 门禁 `check_client_purity.py` | exit 0，0 违例 |
| 静态残留复查（AST） | import sqlite3/CodeGraphDB( 无残留；db.conn/db_path/workspace_root 0 业务访问；_METHOD_MAP 仍 157 条；LazyDBProxy 0 引用 |
| **P1 `cw task list` fail-closed E2E** | ✅ 死端点不再静默空列表：`✗ Subcommand 'task' failed: E_HTTP_MANIFEST_STALE`（本机 stale manifest 场景）；隔离 HOME 无 manifest 场景：`E_HTTP_DAEMON_UNAVAILABLE: 无法连接 daemon (url=http://127.0.0.1:1/v1/rpc)`——两种 fail-closed 路径均确认，未降级本地 |

**工程师修复确认**（cli/main.py 工作区 diff）：
1. task list handler 移除 `except Exception: tasks = []`，改为直接上抛（`cli/main.py:5025-5037`，注释引用 "QA Round 1 P1"）；
2. 同类吞异常排查：`_print_task_link_section` 两处（Related commits/changes）新增 `except DaemonUnavailableError: raise`；
3. `_handle_clone` 移除 `db.conn.execute(...)` 直接 SQL 兜底（db.conn 残留清零）。

**QA 自修测试问题（P2，非源码）**：
1. `tests/test_cw_client_rpc_proxy.py`：3 个"非 Linux 一律 return 2"断言更新为"不受支持平台（freebsd）return 2"，
   并新增 `test_run_client_mode_win32_delegates_to_daemon_command`（验证 S1 放宽后 win32 client 委托）；
2. `tests/convergence/test_m4_cli_fail_closed.py`：加 autouse fixture 重置 `HttpDaemonRpcClient` 单例
   （消除跨测试缓存污染）+ local 用例显式 `CW_DAEMON_TRANSPORT=named-pipe`；fail-closed 断言放宽为
   接受 `E_HTTP_*` 系列结构化错误（连接失败 E_HTTP_DAEMON_UNAVAILABLE 与 manifest 校验 E_HTTP_MANIFEST_STALE
   均为拒绝执行、不降级本地）。

---

## Round 1 记录（原始验收，保留审计）

## Summary

| 指标 | 值 |
|---|---|
| 验证项总数 | 12 |
| 通过 | 10 |
| 失败（源码缺陷） | 1（`cw task list` fail-open，存量代码，S1 验收点 3 未达标） |
| 失败（测试自身问题，非源码） | 2 组（测试断言过时 / 测试隔离缺陷） |
| Routing Decision | **Engineer**（1 个需工程师修复的源码缺陷 + 2 组测试问题建议同步处理） |

**核心结论**：S1 主交付（LazyDBProxy→RpcDBProxy 迁移、白名单清空、CLI 不再本地 SQLite）**验证通过**：
门禁 0 违例、`import sqlite3`/`CodeGraphDB(`/`db.conn` 等无业务残留、_METHOD_MAP 157 条映射抽查 8 条全部一致、
`cw --status`/`cw status` 死端点 fail-closed 抛 `E_HTTP_DAEMON_UNAVAILABLE`、`import callwarden.cli.main` 通过、
`server/cli_admin.py` SQL 直连限定 daemon 宿主侧只读维护命令。

**但发现 1 个真实缺陷**：`cw task list` 在 daemon 不可达时**静默返回空列表、exit 0**（`route_task_read` 已抛
`DaemonUnavailableError`，被 `cli/main.py:5035-5036` 的 `except Exception: tasks = []` 吞掉）——违反 PRD §3.2 M4
fail-closed 契约，S1 验收点 3（`cw task list` 须抛 E_HTTP_DAEMON_UNAVAILABLE）**未达标**。

---

## 验证明细

### 1. 门禁独立重跑 ✅

```
$ python scripts/check_client_purity.py
硬门禁: 扫描 13 个文件（server/tools + cw.py）
软门禁: 扫描 9 个文件（cli/，含白名单存量）
通过: server/tools + cw.py + cli/ 纯净（0 违例）
EXIT_CODE=0
```

- 白名单确已移除（非豁免）：`scripts/check_client_purity.py` 中 `LEGACY_CLI_ALLOWLIST: Set[str] = set()`
  （空集），且软门禁路径 `cli` 不在白名单 → 0 违例 = 真实通过。

### 2. 静态抽查 ✅

| 检查项 | 结果 |
|---|---|
| `import sqlite3` | 无残留（cli/main.py 0 命中） |
| `from ..db import CodeGraphDB` | 无残留 |
| `CodeGraphDB(` 构造 | 无残留 |
| `LazyDBProxy` | 无残留（已删除） |
| `db = RpcDBProxy(...)` | 2 处实例化（line 1440 / 11456），无 LazyDBProxy 实例化 |
| `db.conn` / `db.db_path` / `db.workspace_root` 业务访问 | 仅 1 处注释（line 5200 说明文字），无实际访问 |
| `RpcDBProxy.__getattr__` 对 `conn`/`db_path` | 显式 `raise AttributeError`（fail-closed，禁止本地 SQLite） |

**_METHOD_MAP 映射抽查（8 条，对照 CodeGraphDB 签名 + Rust/Python daemon 分发）**：

| CodeGraphDB 方法 | 签名（db/*.py） | _METHOD_MAP → RPC | daemon 分发 | 判定 |
|---|---|---|---|---|
| `get_stats()` | db_query.py:26 | `query.stats` | daemon_server.py:1416 | ✅ |
| `get_status()` | db_query.py:155 | `query.status` | rust_ext snapshot_state.rs:2361 / metrics_handlers.rs | ✅ |
| `search_symbols(query, kind, limit)` | db_query.py:651 | `query.search` | daemon_server.py:1422 | ✅ |
| `get_symbol(qualified_name)` | db_query.py:809 | `query.symbol` | daemon_server.py:1418 | ✅ |
| `get_callers(callee_name, qualified_name)` | db_query.py:302 | `query.callers` | daemon_server.py:1429 | ✅ |
| `get_callees(caller_name, qualified_name)` | db_query.py:392 | `query.callees` | daemon_server.py:1435 | ✅ |
| `get_file_symbols(file_path)` | db_query.py:637 | `query.file` | rust_ext route_matrix.rs:132 / dispatch.rs:2147 | ✅ |
| `task_create(title, desc, steps, creator)` | db_tasks.py:202 | `task.create` (GOVERNANCE_WRITE) | daemon_client.py:157 / tools_task.py:65 | ✅ |
| `task_list(status_filter, limit)` | db_tasks.py:3091 | `task.list` + `_PARAM_RENAME status_filter→status` | rust_ext route_matrix.rs:287 / dispatch.rs:2262 | ✅ |

- _METHOD_MAP 条目数：**157**（与工程师声明一致）。

### 3. fail-closed 独立验证（E2E，独立环境变量覆盖，不影响本机运行 daemon）✅ / ⚠️

方法：`CW_DAEMON_MODE=http CW_DAEMON_HTTP_ENDPOINT=http://127.0.0.1:1`（死端口，进程内环境变量隔离）。

| 命令 | 结果 | 判定 |
|---|---|---|
| `cw --status` | 抛 `DaemonUnavailableError: E_HTTP_DAEMON_UNAVAILABLE: 无法连接 daemon (url=http://127.0.0.1:1/health)`，未降级本地 | ✅ fail-closed |
| `cw status`（子命令） | 同上，`✗ Subcommand 'status' failed: E_HTTP_DAEMON_UNAVAILABLE: ...` | ✅ fail-closed |
| `cw task list` | **静默输出 `Total tasks: 0`、exit 0，无任何错误** | ❌ **fail-open 缺陷** |

**缺陷根因**：
- `server/daemon_client.py` `route_task_read` 本身 fail-closed 正确（独立探针确认：HTTP 死端点下对
  `task.list` 抛 `DaemonUnavailableError`）；
- 但 `cli/main.py:5025-5036` 的 task list handler 用 `try/except Exception: tasks = []` 把异常吞掉，
  用户看到空列表而非错误 → 违反 fail-closed 契约。
- **存量代码**：`git diff HEAD` 显示该 handler 不在 S1 变更集内（S1 未引入回归），但 S1 验收点 3
  明确要求 `cw task list` 抛 E_HTTP_DAEMON_UNAVAILABLE → 需工程师修复。

**附加观察（非 S1 回归）**：daemon 可达（auto 模式）时 `cw status` 返回 `method_not_found: 未知方法:
query.status`——Rust daemon 对 `query.status` 的兼容路径与当前运行 daemon 版本有关，属 daemon 侧兼容
演进问题，不在 S1 CLI 迁移范围；CLI 转发行为本身正确（结构化错误透传，fail-closed 语义）。

### 4. 回归抽查 ✅

- `cd C:/git_work && python -c "import callwarden.cli.main; ..."` → `IMPORT_OK`（正确包路径导入通过）。
- 注意：`import cli.main`（直接在仓库根）因 `from ..config` 相对导入报 `ImportError`——这是 Python
  包结构预期（`callwarden` 是顶层包，仓库根不是包路径），真实入口 `cw.py` 会先注入父目录到 sys.path，
  实际 `cw --status` 等命令可正常运行，非缺陷。

### 5. 边界：server/cli_admin.py ✅（符合约束）

- `server/` 位于 daemon 宿主侧，**不在** `check_client_purity` 扫描范围（硬门禁=server/tools + cw.py，
  软门禁=cli）——设计如此。
- 引入 `import sqlite3` 但**全部为只读连接**（`file:...?mode=ro`, uri=True, timeout=3），无写路径：
  - `open_readonly_conn` / `read_pragmas` / `connection_test` / `scan_hash_databases` /
    `read_task_dependencies` → 全部 mode=ro；
  - `migrate_single_db` 委托 `db_migrate.migrate_to_single_db`（权威实现），本模块不直接写。
- CLI 侧仅通过纯函数接口调用（`cli/main.py` line 6810/6879/7231/7376/11085/15016），CLI 不直接操作
  SQLite；模块 docstring 明确 fail-closed 与"只读不写、不持有写锁"约束。
- 判定：**边界符合**——SQL 直连仅限 doctor/gc-db-cleanup/db-migrate/dependency-inspect 等本机维护
  命令，且只读、fail-closed 语义明确。

### 6. cli 相关测试套件 ⚠️（2 组失败，均非 S1 源码回归）

```
pytest tests/convergence/test_m4_cli_fail_closed.py tests/test_cw_client_rpc_proxy.py tests/test_cli_main_help.py
```

| 失败测试 | 原因 | 判定 |
|---|---|---|
| `test_m4_cli_fail_closed.py::test_route_rpc_fail_closed_never_falls_back_local`（合跑时） | **测试隔离缺陷**：`HttpDaemonRpcClient` 单例跨测试缓存，前序 local 模式测试（`test_route_rpc_local_mode_allowed_with_test_mode`）在 HTTP 默认开启下创建了 HTTP 单例并连到真实 daemon，导致本测试收到 `snapshot_not_ready` 业务错误而非连接错误；**单独运行该测试通过** | 测试自身问题，非源码 bug |
| `test_cw_client_rpc_proxy.py::test_run_client_mode_non_linux_returns_2 / ignores_argv / does_not_call_daemon_command_on_non_linux` | **测试断言过时**：`run_client_mode` 平台门禁已放宽为 `("linux","win32","darwin")`（HEAD 与工作区一致），Windows 上不再 return 2，而是正常执行 client 模式；测试仍断言非 Linux 一律 return 2 | 测试自身问题，建议更新断言（预期：win32/darwin 允许，其它平台 return 2） |

- M4 其它 5 个用例单独/合跑均通过；`test_cli_main_help.py` 通过。
- 上述失败**均不在 S1 变更集内**（测试文件未在 git status 修改列表），不影响 S1 主交付判定。

---

## 已知问题 / 建议修复（路由 Engineer）

1. **[P1] `cli/main.py:5035-5036`（task list handler）fail-open**：daemon 不可达时 `cw task list`
   静默返回空列表、exit 0。应移除/收窄 `except Exception: tasks = []`，对 `DaemonUnavailableError`
   上抛（或至少输出 `E_HTTP_DAEMON_UNAVAILABLE` 明确错误），保证 PRD M4 fail-closed 契约。
   涉及函数：`_handle_task` 的 `opts.action == "list"` 分支（工作区 line ~5018-5036）。
   建议同时检查其它 `route_task_read`/`route_rpc` 调用点是否存在同类吞异常模式。

2. **[P2] `tests/test_cw_client_rpc_proxy.py` 3 个用例断言过时**：更新为 win32/darwin 允许、其余
   平台 return 2 的预期（或按实现放宽平台门禁语义）。

3. **[P2] `tests/convergence/test_m4_cli_fail_closed.py` 测试隔离**：`test_route_rpc_local_mode_allowed_with_test_mode`
   在 HTTP 默认开启下创建 HTTP 单例并连接真实 daemon，污染后续 fail-closed 用例。建议在测试间
   `HttpDaemonRpcClient.reset_instance()` 或显式关闭 HTTP（CW_DAEMON_TRANSPORT=named-pipe）。

---

## Routing Decision

- **Round 1**: **Send To: Engineer**（缺陷 1 需源码修复；2/3 为测试侧问题，QA 自修）
- **Round 2**: **NoOne** — 工程师已修复 P1（task list 上抛 + 同类排查），QA 已自修 2 组测试问题；
  45 测试全过、门禁 0 违例、`cw task list` 两种 fail-closed 场景 E2E 确认。S1 验收通过，闭环。

## 复现命令（供回归）

```bash
# Round 2 fail-closed 已达标（不再静默空列表）
cd C:/git_work/callwarden
CW_DAEMON_MODE=http CW_DAEMON_HTTP_ENDPOINT=http://127.0.0.1:1 python cw.py task list   # E_HTTP_MANIFEST_STALE（本机 stale manifest）或 E_HTTP_DAEMON_UNAVAILABLE（无 manifest）
CW_DAEMON_MODE=http CW_DAEMON_HTTP_ENDPOINT=http://127.0.0.1:1 python cw.py status      # E_HTTP_DAEMON_UNAVAILABLE ✅
CW_DAEMON_MODE=http CW_DAEMON_HTTP_ENDPOINT=http://127.0.0.1:1 python cw.py --status    # E_HTTP_DAEMON_UNAVAILABLE ✅

# 测试套件（注意 basetemp 避开沙箱 bulk-delete 保护）
C:/Python314/python.exe -m pytest tests/convergence/test_m4_cli_fail_closed.py tests/test_cw_client_rpc_proxy.py tests/test_cli_main_help.py -q --basetemp=_qa_s1_tmp/r2_$(date +%s)
```
