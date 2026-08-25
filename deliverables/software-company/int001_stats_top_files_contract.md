# INT-001：`stats_top_files` 内部 compat worker 路由 → Rust daemon native

**父任务：** `T-1787293451688-c14b1e44`  
**port_type：** `graph_snapshot`  
**port_key：** `server/compat_registry.py::_stats_top_files|RUST_COMPAT_ROUTE.stats_top_files|rust_ext/src/daemon/query_compat_handlers.rs::handle_stats_top_files|dispatch.rs|http_server.rs`  
**gate：** `false`  
**execution_dependency：** `CLI-01` 和 `MCP-016`（graph/query first native port）均须 `applied`；本卡预建不等于可领取。

## 发现来源

`stats_top_files` 位于 `server/compat_registry.py::RUST_COMPAT_ROUTE`，但不是 239 个公开 MCP 工具之一，因而未出现在 `tool_migration_matrix.json` 的 70 条 `python_compat` 清单中。它仍由 `server/compat_registry.py::_stats_top_files` 通过 Python/SQLite 执行业务查询，必须单独迁移，才能满足“Python 仅作为 API client/adapter”的目标。

## 唯一链路

```text
internal stats_top_files request
  → Python compat registry thin adapter
  → HTTP JSON-RPC stats_top_files
  → dispatch.rs::dispatch_rpc
  → query_compat_handlers.rs::handle_stats_top_files
  → Rust readonly authority connection
```

## 精确范围与验收

| 类别 | 强制目标 |
|---|---|
| Python entry | `server/compat_registry.py::_stats_top_files` 与 `RUST_COMPAT_ROUTE["stats_top_files"]` 的单项 retirement；保留 registry 框架，不删其他 70 个迁移中的方法。 |
| Rust handler | `rust_ext/src/daemon/query_compat_handlers.rs::handle_stats_top_files`；带 workspace filter 和 `limit` 1..500 clamp。 |
| transport | `rust_ext/src/daemon/dispatch.rs::dispatch_rpc` 的 `stats_top_files` branch；`rust_ext/src/daemon/http_server.rs` capability row 从 `python_compat` 更新至 `rust_native`。 |
| fixture | `tests/test_internal_stats_top_files_http_rpc.py`：success、limit 非法、missing/unknown workspace、daemon unavailable、restart parity。 |
| inventory | 更新 internal compat route inventory / generator input，并从 `RUST_COMPAT_ROUTE` 移除该单项；公开 239 MCP matrix 不得伪造添加不存在的 MCP 工具。 |

禁止修改 `db/schema.py`、其他 compat route、MCP public tool name、task/lease/verdict/gate mutation，禁止 hidden Python/SQLite fallback。

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立复现 stats_top_files 的 HTTP success 与全部负向矩阵；核验 Python compat registry 已不执行此业务 SQL，Rust handler 已成为唯一 authority。
  reason: INT-001 是覆盖审计识别出的唯一非 MCP 内部 compat route，完成后 compat worker 不再保留未入库业务方法。
  independence_requirement: required
```
