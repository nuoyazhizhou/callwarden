# A3 决议：`cli/main.py` `_local_contract_get` SQLite 直调的越权归属

> 任务：`T-1786671619396-246d708c`（独立任务，非 H 系列子任务）
> 状态：**方案 A（合法化保留）已决议并实施**
> 本文件只记录决议与边界声明，**不修改** `http-daemon-mvp-compatibility-contract.md`
> 等既有协议定义文件的内容。

## 1. 背景

H2 独立复审（`http-daemon-mvp-h1-h2-independent-review-handoff.md` §L83）发现：
`cli/main.py` 新增了 `_local_contract_get()` 直接打开 SQLite 读取
`role_contracts`，该改动超出 H2 所有权范围，且与"HTTP no-fallback"规则存在
表面冲突，被标记为 **P0/P1 越权改动**。

由于该改动已随 H1/H2 整改提交进入生产代码，且承担合同任务 claim/report 的
`contract_claim` 构造职责，需要正式决议其归属：A) 合法化保留 或 B) 移除改造。

## 2. 决议结论：方案 A（合法化保留）

保留 `_local_contract_get` 与 `_fetch_contract_claim`，并补充边界声明注释
（见 `cli/main.py` 两处函数 docstring）。依据如下。

## 3. 决议依据（证据链）

### 3.1 `route_task_read` 三分支语义（`server/daemon_client.py` L1980-2000）

| 模式 | daemon 不可达时的行为 |
|---|---|
| `local` | 直接执行 fallback_func（本场景即 `_local_contract_get`） |
| `enterprise` | 硬 fail-closed：抛 `DaemonUnavailableError`，fallback 不可达 |
| `auto` | daemon 优先，不可达时降级 fallback_func |

- `DaemonRemoteError`（业务错误）在任何模式**永不降级**，原样透传。
- 因此 `_local_contract_get` 只在 `local`/`auto` 模式下可达，`enterprise`
  模式不会触达本地 SQLite 降级。

### 3.2 daemon 权威侧存在同源 RPC

daemon 侧已实现 `task.contract_get` RPC（`rust_ext/src/daemon/task_collab.rs`
`handle_task_contract_get`），读取同一 `role_contracts` 表（按 role 过滤 +
`ORDER BY revision DESC LIMIT 1`），与 `_local_contract_get` 查询语义一致。
即：本地降级读的是与 daemon 权威读完全相同的冻结合同数据，无数据分叉。

### 3.3 只读无写，不绕过单写点

`_local_contract_get` 使用 `sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)`
只读连接，不做任何写操作；写（claim/report）仍全部经 daemon RPC
`task.claim`/`task.report` 执行。不违反 daemon 唯一写协调点原则。

### 3.4 与 HTTP no-fallback 契约的边界

`http-daemon-mvp-compatibility-contract.md` §2.1 的 fallback 禁止行约束的是
**HTTP profile client**（`HttpDaemonRpcClient`，`CW_DAEMON_TRANSPORT=http`）。
`_fetch_contract_claim` 始终走 Named Pipe/UDS `route_task_read`，不在 HTTP
profile 路径上；HTTP 模式下的 client 不存在该降级闭包。二者边界不冲突。

## 4. 实施内容

| 文件 | 变更 |
|---|---|
| `cli/main.py` `_local_contract_get` | docstring 补充 4 条 A3 边界声明（可达模式 / 只读 / HTTP 边界 / 业务错误不降级） |
| `cli/main.py` `_fetch_contract_claim` | docstring 补充 A3 边界声明（Named Pipe/UDS 路径、降级仅限连接层失败） |
| 本文件 | 决议记录（不修改协议定义） |

## 5. 验收（实施后）

- `git diff cli/main.py` 仅包含两处 docstring 注释增强，无逻辑改动；
- `python -m py_compile cli/main.py` 通过；
- `tests/test_http_daemon_client.py` + `tests/test_http_manifest_discovery.py`
  全量通过；
- `route_task_read` 三分支语义核验记录已输出。

## 6. 后续职责

本决议不改变 HTTP no-fallback 契约的既有内容；若未来 HTTP profile 需要
`contract_get` 能力，须在契约层另行批准（profile 变更流程），不得复用本
降级路径。
