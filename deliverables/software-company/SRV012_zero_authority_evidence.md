# SRV-012：metrics rollback authority 下沉证据

## 任务绑定

- task：`T-1787323461346-ec4e03e8`
- step：`S-1787323461348-ec621d9c`
- role：`executor` / runtime role `implementer`
- agent：`executor-workbuddy-v1-cur`
- session：`dcf88a76-0895-4f09-9245-1cc8cbaedb82`
- workspace：`workspace_id=376`，`workspace_instance_id=4baea3ff12c2ea5c`
- task contract：`TC-T-1787323461346-ec4e03e8`，revision `2`，hash `sha256:71d18311c3dc8dfa953f4851e90515a108e4aa660eebfe2b6d37b87459da03f3`
- role contract：`rcl-T-1787323461346-ec4e03e8-executor`，revision `1`，hash `sha256:b6f4aff472553e90fd7386bbf3fdc2ee669f8493981887847374a3b77f41dc95`
- canonicalization：`role-contract-c14n/v1`，hash `sha256:59ad755be8740794624c927294f95515d2b17790ffc21b58f6d9cf7155ff188d`

## 实现范围

1. `server/metrics.py::_is_rust_metrics_rolled_back` 移除 Python `sqlite3.connect`、`DB_PATH` 和本地 `SELECT`，改为调用 `mcp.metrics.is_rust_metrics_rolled_back`，保留 60 秒缓存和 daemon 不可用时的 fail-soft 语义。
2. `rust_ext/src/daemon/metrics_handlers.rs` 增加 Rust authority handler：读取 `rollback_config` 中 `rust_daemon_metrics_compute` 的最新配置；库、表、行或查询异常均返回未回滚，不把错误降级为 Python SQLite。
3. `rust_ext/src/daemon/dispatch.rs` 增加 native dispatch；`rust_ext/src/daemon/http_server.rs` 增加 HTTP capability registry、CLI/MCP 映射、fixture 和任务 owner。
4. `tests/test_srv_012.py` 覆盖客户端路由、缓存、fail-soft、dispatch/capability 和 Python AST zero-authority 断言；Rust 单测覆盖 flag、latest row、缺失表/行和打开失败。

## 可复核结果

### Python AST authority 扫描

命令：比较 `git show HEAD:server/metrics.py` 与工作树文件，解析 `_is_rust_metrics_rolled_back` 函数体。

| 阶段 | 函数体含 `sqlite3` / `DB_PATH` / `SELECT` | 模块级 `sqlite3` import |
| --- | --- | --- |
| before | `true` | `false` |
| after | `false` | `false` |

### 测试

- `python -m pytest tests/test_srv_012.py -q`：`12 passed`。
- `cargo test --manifest-path rust_ext/Cargo.toml metrics_handlers --lib -- --nocapture`：`6 passed; 0 failed`（cargo 生成既有 warning，不影响结果）。

### 运行时 provenance

- `HttpDaemonRpcClient` 对当前 daemon `ping` 成功：PID `5480`，HTTP transport，protocol `1`，authority/task DB fingerprint `7ce7ccb824889fbc03805083002c3c2a7a61512ca224a3a572d9b09007e9692a`。
- 对 `mcp.metrics.is_rust_metrics_rolled_back` 的真实 HTTP 探测返回 `method_not_found`。这说明当前运行中的 daemon 尚未部署本步骤的新 handler/capability；该任务允许路径不含 `runtime/current`，因此未修改运行时或伪造部署成功。Reviewer 应将此项作为部署 provenance 待办复核，而不是将本地测试结果误判为已部署运行时通过。

## 变更与排除

本步骤仅捕获任务合同允许的 `server/metrics.py`、Rust metrics dispatch/handler/HTTP registry、`tests/test_srv_012.py` 和本证据目录。工作树中其他 dirty/untracked 文件属于既有或其他任务变更，未纳入本步骤。

## Executor handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立核验 SRV-012 的 Python zero-authority AST、Rust handler 语义、HTTP capability/dispatch、12 个 Python 测试和 6 个 Rust 测试，并复核当前 daemon method_not_found 的部署 provenance
  reason: 本步骤实现和定向测试通过；当前 daemon ping 正常但尚未暴露新增 RPC，证据已明确区分代码测试与运行时部署状态
  independence_requirement: required
```
