# CW Rust Client Convergence —— 迁移指南（T05）

> 项目：`cw-rust-client-convergence`
> 版本：v0.1
> 关联：`tool_migration_matrix.json`（239 工具单一真相源）、`rust-client-convergence-protocol.md`

本文档记录每批次迁移的 handler / 薄壳 / 测试清单，供后续迭代与 QA 协同使用。

---

## 1. 迁移批次总览

| 批次 | 范围 | 数量 | 目标 backend | 落地位置 |
|---|---|---|---|---|
| T01 基础设施 | 路由矩阵 + 脚手架 + 协议 | — | — | `tool_migration_matrix.json` / `scripts/*` / `route_matrix.rs` |
| T02-fs | 文件/构建面（18 本地 SQL 的文件面） | 9 | rust_native | `fs_handlers.rs` |
| T02-metrics | 度量/状态面（18 本地 SQL 的度量面） | 9 | rust_native | `metrics_handlers.rs` |
| T02-job | 异步长任务（61 拒止的异步组） | 18 | task_rpc | `job_runner.rs` |
| T02-admin | GC/审计/运维/协同写面（61 拒止的运维组） | 22 | rust_native | `admin_handlers.rs` |
| T02-edit | 编辑/提案/规则写面（61 拒止的编辑组） | 21 | rust_native | `edit_handlers.rs` |
| T03 | Python MCP 薄壳化 | 239 | — | `server/tools/*.py`（thinify_tools.py） |
| T04 | CLI 纯 client 化 | — | — | `cli/dispatcher.py` + config fail-closed |
| T05 | 集成验收 | — | — | 本文档 + 验证脚本 |

## 2. 单工具迁移检查单

1. **矩阵**：确认 `tool_migration_matrix.json` 中该工具 `target_backend/rpc_method/op_class/batch/status` 正确；
2. **Rust handler**：在对应 handler 模块实现业务逻辑（fs/metrics/job/admin/edit）；
3. **dispatch**：rpc_method 加入 `CONVERGENCE_RPC_METHODS`（写操作同时加入 `PROTECTED_MUTATION_METHODS`）；
4. **snapshot_state**：`handle_convergence_rpc` 增加分发分支（ACL + 连接解析）；
5. **Python 薄壳**：运行 `scripts/thinify_tools.py`（幂等，重新生成薄壳函数）；
6. **验证**：`scripts/verify_route_matrix.py`（239/239）+ `scripts/check_client_purity.py`（0 违例）+ `cargo check --lib`。

## 3. 迁移规则

- **写路径权威**：所有 INSERT/UPDATE/DELETE 只发生在 daemon（Rust handler 经
  SerializationPoint 串行化 + CAS/lease）；Python 薄壳零本地写。
- **白名单纪律**：`COMPAT_ROUTE_WHITELIST` / `RUST_COMPAT_ROUTE` 只减不加；
  每个 python_compat 条目必须关联迁移 ticket，M2 deadline（2 个发布里程碑后）清空。
- **workspace_instance_id**：daemon handler 经 `owned_workspace` 做 owner ACL；
  Python 只透传 `workspace.register` 权威值。
- **错误码**：业务错误一律 `E_*` 结构化（新增见 `error_codes.rs` + `config.py`）。

## 4. compat worker 过渡清单（P0 剩余 79 项，M2 前逐批迁移）

当前 79 个 python_compat 工具全部在 `COMPAT_ROUTE_WHITELIST`（80 项含 1 个内部
worker 方法 stats_top_files）。按「实现成本/返回结构复杂」优先级迁移到
rust_native（优先批次）：
- 第一批候选（成本低）：`get_recent_changes` / `get_symbol_history` /
  `get_impact` / `get_top_callers` / `get_orphan_symbols` / `get_deepest_functions` /
  `get_comment_coverage` / `find_issues`；
- 第二批候选（中等）：`get_summary` / `project_brief` / `repo_map` /
  `find_uncovered_functions` / `test_impact_selection` / `who_to_ask`；
- 第三批候选（高成本，M2 前最后处理）：`ask_codebase` / `lsp_*` / `semantic_search`。

每个迁移完成即从白名单移除（只减不加）。

## 5. 验收命令

```bash
# Rust 编译（编译期门禁）
cd rust_ext && cargo check --lib

# Python 导入
python -c "import callwarden.server.mcp_server"

# 路由矩阵一致性（239/239）
python scripts/verify_route_matrix.py

# 薄壳纯净度（server/tools + cw.py 0 违例；cli/main.py 存量遗留 T04-followup）
python scripts/check_client_purity.py
```

## 6. 已知遗留（T04-followup）

- `cli/main.py`（15K 行）承载 ~318 处本地 DB 业务实现（query/search/task/gc 等子
  命令），已列入 `check_client_purity.py` 的 `LEGACY_CLI_ALLOWLIST`（迁移 ticket
  `T04-followup`）。迁移方式：逐子命令改为 `cli/dispatcher.call_daemon()` +
  格式化输出，完成后从白名单移除。
- daemon 二进制 link（Windows link.exe .def BOM 问题）为环境遗留，与本次代码无关；
  `cargo check --lib` 为编译门禁。
