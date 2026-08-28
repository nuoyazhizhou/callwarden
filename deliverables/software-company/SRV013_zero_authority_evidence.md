# SRV-013：query budget rollback authority 下沉证据

## 任务绑定

- task：`T-1787323461404-efba3d30`
- step：`S-1787323461406-efd72ef4`
- role：`executor` / runtime role `implementer`
- agent：`executor-workbuddy-v1-cur`
- session：`dcf88a76-0895-4f09-9245-1cc8cbaedb82`
- workspace：`workspace_id=376`，`workspace_instance_id=4baea3ff12c2ea5c`
- task contract：`TC-T-1787323461404-efba3d30`，revision `2`，hash `sha256:d047f55f25466ebc4dc6c7e57b893159e92304d47d2172ad5986ab7ce2c1c8b2`
- role contract：`rcl-T-1787323461404-efba3d30-executor`，revision `1`，hash `sha256:c034600e1c26623f5d1673d6ca60f9dd9cd9ac9ff92420cee1d8c5901c71d84c`
- canonicalization：`role-contract-c14n/v1`，hash `sha256:59ad755be8740794624c927294f95515d2b17790ffc21b58f6d9cf7155ff188d`

## 实现范围

1. 新增 `rust_ext/src/daemon/query_budget_handlers.rs`：Rust daemon 读取 `rollback_config` 中 `rust_daemon_acl_path_budget` 的最新行，库/表/查询异常均 fail-soft 为未回滚。
2. 在 `rust_ext/src/daemon/dispatch.rs` 以内联 `#[path]` 子模块方式注册 handler 和 RPC `mcp.query_budget.is_rust_budget_rolled_back`；未修改不在本 step 白名单内的 `daemon/mod.rs`。
3. 在 `rust_ext/src/daemon/http_server.rs` 增加 native/read-only/authority capability、CLI 映射、fixture 和 SRV-013 owner。
4. `server/query_budget.py::_is_rust_budget_rolled_back` 只保留 daemon RPC、缓存和 fail-soft 适配，不再连接本地 SQLite。
5. `tests/test_srv_013.py` 覆盖成功、畸形响应、authority、daemon unavailable、restart/cache、dispatch/capability、Rust 语义和 Python AST 门禁。

## 可复核结果

### AST authority 扫描

比较 `git show HEAD:server/query_budget.py` 与工作树源码，解析 `_is_rust_budget_rolled_back` 的可执行函数体（排除 docstring）：

| 阶段 | `sqlite3.connect` / `DB_PATH` / `SELECT` / `rollback_config` 可执行命中 | daemon RPC |
| --- | --- | --- |
| before | `true`（原始本地 rollback_config 查询） | `false` |
| after | `false` | `true`（`mcp.query_budget.is_rust_budget_rolled_back`） |

模块级扫描同时确认 `server/query_budget.py` 不含 `import sqlite3` 或 `from callwarden.config import DB_PATH`。

### 测试

- `python -m pytest tests/test_srv_013.py -q`：`12 passed`。
- `cargo test --manifest-path rust_ext/Cargo.toml query_budget_handlers --lib -- --nocapture`：`4 passed; 0 failed`（handler 在 `daemon::dispatch::query_budget_handlers` 下编译，cargo 仅报告既有 warning）。

### 运行时 provenance

- 当前 `HttpDaemonRpcClient` 对 daemon PID `5480` 的 `ping` 成功：HTTP transport、protocol `1`、authority/task DB fingerprint `7ce7ccb824889fbc03805083002c3c2a7a61512ca224a3a572d9b09007e9692a`。
- 对 `mcp.query_budget.is_rust_budget_rolled_back` 的真实 HTTP 探测返回 `method_not_found`。当前运行时尚未部署 SRV-013 handler/capability；本任务 scope 不含 `runtime/current`，因此没有修改运行时或伪造部署成功。

## 提交前限制

提交前尝试 `python cw.py --refresh-all`，daemon 返回 `method_not_found: build_full_graph`，代码图刷新未完成；未使用 SQLite fallback。工作树存在其他任务的 dirty/untracked 文件，本步骤只按任务白名单捕获。

## Executor handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立核验 SRV-013 的 Python zero-authority AST、Rust query-budget handler/dispatch、HTTP capability、12 个 Python 测试和 4 个 Rust 测试，并复核当前 daemon method_not_found 的部署 provenance
  reason: SRV-013 实现与定向测试通过；证据明确区分源码/测试通过和当前运行时尚未部署新增 RPC，未使用 SQLite fallback
  independence_requirement: required
```
