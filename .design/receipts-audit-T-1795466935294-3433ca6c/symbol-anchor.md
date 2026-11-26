# symbol-anchor — receipts 审计符号锚定（R2）

- **feature**：receipts 审计：build_graph 成功/失败/转后台均落可验证审计回执
- **R 阶段**：R2 符号锚定
- **生成时间**：2026-11-24 15:32:19
- **依据 HEAD**：`1fbcd9bad21d58b7ed76e7e1710883213d5495cc`
- **任务**：`T-1795466935294-3433ca6c`

## 0. 锚定方法双重交叉说明（强制）

按项目 AGENTS.md 规则：MCP 激活后只读走 MCP（吃狗粮），同时用 cw 命令 + 内置工具双重交叉。实测记录：

| 查询 | MCP / cw 结果 | 内置 Grep/Read 结果 | 交叉结论 |
|---|---|---|---|
| `search_symbols("build_flight")` | MCP 无输出 | 命中 `build_flight.rs`、`fs_handlers.rs`、`dispatch.rs` 等 | 符号**未入库** |
| `file_grep("BuildFlights\|handle_build_graph_async\|run_build_graph\|audit_verify_chain", rust_ext/src/daemon)` | MCP `count=0` | 命中 6+ 处（见下） | 符号**未入库** |
| `cw symbol BuildFlights::finish` | `Symbol not found` | Read 命中 `build_flight.rs:105` | 符号**未入库** |
| `cw symbol handle_build_graph_async` | `Symbol not found` | Read 命中 `fs_handlers.rs:1611` | 符号**未入库** |

**根因**：`rust_ext/src/daemon/build_flight.rs` 为 git 未跟踪文件（`??`），且工作树大量在途 dirty，符号图谱未刷新（`build_full_graph` RPC 缺失，规则 45a）。`fs_handlers.rs` 虽被修改但新符号同样未入索引。

**处理**：本轮符号锚定以**内置 Grep/Read 源码实证为准**，MCP/cw 侧留痕证明"图谱滞后"这一事实；不把"cw 查不到"误判为"符号不存在"。所有判定给出**符号名 + 文件 + 作用**，不以行号为唯一锚点（行号仅为阅读方便，会漂）。

## 1. 符号判定表（reuse / extend / new）

| 元素 | 判定 | 符号名 + 文件 + 作用 | 证据（内置 Grep/Read） | cw/MCP 交叉 |
|---|---|---|---|---|
| 单飞协调器 `BuildFlights` | **reuse** | `fs_handlers::build_flight::BuildFlights`（`build_flight.rs:67`），per-workspace 构建登记 + lastBuild（内存）；`fs_handlers.rs:36 pub mod build_flight`，`dispatch.rs:206` 持 Arc | Read build_flight.rs 全文；Grep 命中 pub mod/use/Arc 字段 | MCP/cw 未入库（见 §0） |
| `BuildFlights::finish` | **extend** | `BuildFlights::finish`（`build_flight.rs:105`）：移除单飞登记、注入 build_seq/completed_at、写 `self.last`。需扩展为"同时 append 审计回执" | Read 105-120；既有签名 `finish(&self, root_key, stats)`，stats 已含 ok/error（失败时由调用方注入） | 未入库 |
| 成功路径出口 | **reuse（改造点）** | `run_build_graph`（`fs_handlers.rs:1382`）：Ok 分支在 `:1585-1598` 构造 stats map（ok/scanned/inserted/unchanged/deleted/...）；调用方后台线程 `:1701-1705` 据 result 生成 stats 并 `flights_bg.finish` | Read 1382-1599、1694-1706 | 未入库 |
| 失败路径出口 | **reuse（改造点）** | 同 `fs_handlers.rs:1701-1705`：`Err(e)` → `stats={ok:false,error:e.code,message:e.message}`；另 `:1677-1681`（db 未配置）、`:1708-1713`（spawn 失败）两处提前 `finish` | Read 1674-1714 | 未入库 |
| 超时转后台路径 | **reuse** | `handle_build_graph_async`（`fs_handlers.rs:1611`）的 `rx.recv_timeout`（`:1716-1734`）：`RecvTimeoutError::Timeout` → 返回 `accepted=true`；后台 worker 跑完后 `flights_bg.finish` 落结果 | Read 1611-1737 | 未入库 |
| `root` 字段来源 | **reuse** | `root_key`（`fs_handlers.rs:1620-1623`：canonical(root) posix 化）即回执 root；`prepare_build_graph(registry,peer,params)` 提供 root/scan_root | Read 1618-1623 | 未入库 |
| 审计写入面（主库） | **reuse** | `append_write_audit_inner`（`fs_handlers.rs:2569`）→ `conn.execute INSERT write_audit_events` → `sign_audit_chain(conn,"remote_write_events",...)` → 回填 `audit_record_id`；SAVEPOINT 原子（`:2560-2566`） | Read 2560-2632 | 审计相关工具已在 route_matrix（见下），但源码符号未入库 |
| 签名链单一入口 | **reuse** | `sign_audit_chain`（`rust_ext/src/cli/task.rs:3768`）：canonical payload → sha256 → 取前一条 `record_signature` → HMAC → INSERT `audit_chain(table_name,...)`；`WRITE_AUDIT_CHAIN_TABLE="remote_write_events"`（`fs_handlers.rs:2419`） | Read task.rs 3768-3805；Grep 常量命中 | route_matrix.rs:250 有 `audit_verify_chain` 路由（已迁移） |
| 审计链验证读面 | **reuse** | `handle_audit_verify_chain`（`query_compat_handlers.rs:2104`）：按 `table_name`/`limit` 调 `cli::security::verify_audit_chain`；`limit=0` 空结果、`<0` fail-closed | Read 2096-2133 | Grep 命中 dispatch.rs:2689 / http_server.rs:4391 / route_matrix.rs:250 |
| 审计查询读面（独立库） | **reuse as reference** | `audit_log_handlers.rs::handle_query`（`:129`）：`audit_log` 表（独立 audit.db）支持 start_time/end_time/event_type/actor_uid/result，倒序；**无 workspace 列、无签名链** → 方案 B 被否的决定性证据 | Read audit_log_handlers.rs 1-203（DDL :23-41） | Grep 命中处理函数清单 |
| `write_audit_events` 表结构 | **reuse** | DDL `fs_handlers.rs:2325-2350`：列 workspace_id/db_workspace_id/event_type(CHECK grant/commit/reject)/outcome(CHECK allow/deny)/detail_json/audit_record_id/recorded_at；索引 `idx_write_audit_events_{grant,path,type}`；append-only 触发器 `:2350-2353,2402-2409` | Read 2325-2416 | 未入库 |
| 冻结 step 绑定的符号 | **reuse** | step0 `BuildFlights::finish`@`build_flight.rs`；step1 `handle_build_graph_async`@`fs_handlers.rs`；step2 测试@`build_flight.rs` | daemon task.status steps | — |
| 既有审计测试 | **reuse（回归基线）** | `tests/test_audit_chain.py`、`test_audit_chain_mixin.py`、`test_audit_chain_full_flow.py`、`test_audit_key_rotation.py`、`test_mcp_audit_verify_chain_http_rpc.py`、`test_phase8_audit_log.py`、`test_remote_write_governance.py`；客户端轮询测试 `tests/test_build_graph_async_client.py`（未跟踪） | Grep files_with_matches 命中 | — |

## 2. 符号落点结论（供 executor）

- **不改的**：`run_build_graph` 统计逻辑、`sign_audit_chain`、`handle_audit_verify_chain`、`write_audit_events` DDL、既有审计测试。
- **要扩展的**：`BuildFlights::finish`（或在其调用点前）增加"append 回执"副作用；失败提前返回分支（E4/E5）补 append。
- **不新增的**：新表、新列、新索引、新 MCP 工具（受 `forbidden_paths` + C-4 约束）。
- **关键设计张力**：`audit_log`（独立库，无链）与 `write_audit_events`+`audit_chain`（主库，有链）是两套审计面；需求同时点名二者，只能通过方案 A（写主库审计面 + detail_json 承载字段 + 复用 verify_chain）统一，已登记 U-01/U-02/U-03。
