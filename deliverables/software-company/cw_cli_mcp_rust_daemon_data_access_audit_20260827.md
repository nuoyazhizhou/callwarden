# CallWarden CLI / MCP / 数据库访问盘点（只读）

**生成时间（UTC）**：2026-08-27T01:56:48.247848+00:00  
**审计性质**：静态源码与仓库内迁移矩阵盘点；未导入 CW runtime、未打开 SQLite、未运行 CLI/MCP、未修改项目源码或任务状态。

## 结论摘要

公开 MCP 和 CLI 的**入口路由**已经大幅收敛为 HTTP daemon 调用；但这不等同于每个工具的业务/SQL实现都在 Rust。应严格区分入口路径与最终执行后端。

| 审计口径 | 数量 | 结论 |
|---|---|---|
| 公开 MCP 工具 | 239 | 源代码中均为 FastMCP 注册工具；静态扫描均使用 daemon 路由薄壳，而非在 MCP 函数体中直接打开 SQLite。 |
| Rust-native MCP 后端 | 141 | 最终目标后端为 Rust daemon handler。 |
| task_rpc MCP 后端 | 40 | 全部先进入 daemon；其中 17 个是 Rust job_runner 工作项、23 个为 Rust task/job 生命周期 RPC。 |
| python_compat MCP 后端 | 58 | 入口经 HTTP daemon，但 daemon 会委派 Python compat worker；worker 以只读 SQLite connection 执行 handler，因此尚未完成 Rust business migration。 |
| CLI 顶级子命令关键字 | 65 | `cw <subcommand>` 控制面；另含 legacy flag 命令和 `cw daemon` 管理面。 |
| CLI RpcDBProxy 方法映射 | 157 | legacy `db.<method>` 语法由 proxy 转为 `route_rpc`，不暴露 conn/db_path。 |
| CLI main.py 语法 `db.<method>` 调用点 | 234 | 均以 `RpcDBProxy` 为变量来源；不代表 234 次 Python SQLite 调用。 |
| CLI main.py 直接 SQLite/CodeGraphDB 导入 | 0 | 未发现；当前主入口构造 `RpcDBProxy`。 |
| Python 生产层直接数据库信号（cli/server/db 扫描） | 541 | 包括 db 层的实现函数、compat worker 和遗留/运维辅助；不能直接等同为对外工具数量。 |

> **直接回答**：若“直连数据库”指 MCP/CLI 客户端函数自己 `sqlite3.connect` 或创建 `CodeGraphDB`，公开 MCP 函数为 **0/239**，CLI 主入口为 **0**。若“通过 HTTP API 的 Rust daemon 实现”要求业务 SQL 也必须为 Rust，则仍有 **58/239** MCP 工具（`python_compat`）在 HTTP ingress 后由 Python worker 以只读 SQLite 执行；这 58 个不能算作 Rust-native。

## 一、CLI 参数与实际数据访问路径

`cw` 主入口先拦截 `daemon` 和顶级子命令；普通 legacy flags 仍会创建变量名为 `db` 的对象，但该对象是 `RpcDBProxy`，其 `__getattr__` 按 `_METHOD_MAP` 调用 `route_rpc`，并拒绝暴露 `conn`/`db_path`。因此源文件中 234 个 `db.<method>` 语法位置是兼容调用形态，而不是 234 个本地 SQLite 调用。[1]

| CLI 面 | 参数/入口量 | 数据库实现结论 |
|---|---|---|
| `_SUBCOMMANDS` | 65 | 顶级命令进入 `_run_subcommand_mode`，使用 `RpcDBProxy`。 |
| `create_parser()` 子 parser entries | 103 | 逐项参数索引见伴随 JSON/CSV；该计数是 parser declaration，不等同于唯一业务能力。 |
| Legacy flag 分支 | 234 | 234 个 `db.<method>` 语法调用 → proxy → route_rpc；读取/写入 op_class 由 `_METHOD_MAP` 规定。 |
| Direct DB object | 0 | main.py 未导入/构造 `CodeGraphDB` 或 `sqlite3`；`RpcDBProxy.conn/db_path` 显式抛 AttributeError。 |
| Watcher/refresh | proxy route | `--refresh`/`--refresh-all` 调用 `db.refresh_file/build_full_graph`，但 `db` 仍是 RpcDBProxy；FileWatcher 接收该 proxy。 |

注意：`server/watcher.py` 和旧进程内 `JobExecutor` 仍包含 Python DB 接口/SQLite 代码，用于 Rust watcher 的事件处理兼容、测试或旧生命周期。它们不应被当作普通 `cw`/MCP 主入口直接打开 DB 的证据；但是它们是后续清理与可达性审计的风险点。[2] [3]

### CLI proxy 映射覆盖

| 指标 | 值 |
|---|---|
| `RpcDBProxy._METHOD_MAP` | 157 |
| `route_rpc` 直接调用点 | 62 |
| `db.<method>` 调用的不同方法数 | 163 |
| 最常见 db 兼容调用（method → RPC） | set_active_workspace → unmapped; get_active_workspace → unmapped; register_workspace → unmapped; delete_workspace → unmapped; rule_sync_agents_md → rule.sync_agents_md; get_stats → query.stats; get_symbol → query.symbol; get_symbol_location → query.symbol_location; list_workspaces → unmapped; get_semgrep_stats → query.semgrep_stats; task_status → task.status; get_active_task → get_active_task |

## 二、MCP 工具：入口与最终后端必须分层统计

FastMCP 在 `server/tools` 的 11 个功能域模块注册全部 239 个工具。各工具函数的共同入口是 Python 参数适配与 `route_rpc`，其 production 失败语义是 daemon 不可用即失败关闭，不回退本地 SQLite。[4]

| Matrix target_backend | 工具数 | 执行含义 | 是否完成 Rust business migration |
|---|---|---|---|
| `rust_native` | 141 | Rust HTTP daemon handler 在 authority 进程内访问 SQLite。 | 是（按当前矩阵目标/状态） |
| `task_rpc` | 40 | 经 Rust daemon task/job RPC；17 个带 job_type，23 个直接 task lifecycle RPC。 | 是（Rust job_runner / task handlers；不是 Python JobExecutor 主链） |
| `python_compat` | 58 | 经 Rust HTTP ingress/compat route 后，Python worker 打开只读 SQLite 执行注册 handler。 | 否，仍是 Python SQL business implementation |
| 总计 | 239 | 公开 MCP 工具集合。 | — |

`python_compat` 的 58 个矩阵项与 `RUST_COMPAT_ROUTE` 的名称交集完整；静态比对未发现矩阵 58 项缺少 compat route 的名称。Compat worker 对每个 frame 从 authority 配置取 DB 路径，并以 `sqlite3.connect(file:...?mode=ro)` 打开只读连接后执行 Python handler。[5]

### MCP 功能域分布

| 工具模块 | 总数 | rust_native | task_rpc | python_compat |
|---|---|---|---|---|
| tools_collab | 8 | 8 | 0 | 0 |
| tools_p2_graph | 10 | 8 | 2 | 0 |
| tools_p3_identity | 7 | 5 | 0 | 2 |
| tools_p4_lease | 8 | 7 | 0 | 1 |
| tools_query | 32 | 22 | 2 | 8 |
| tools_rules | 9 | 9 | 0 | 0 |
| tools_security | 36 | 20 | 1 | 15 |
| tools_semantic | 19 | 8 | 6 | 5 |
| tools_summary | 31 | 11 | 1 | 19 |
| tools_task | 52 | 17 | 27 | 8 |
| tools_workspace | 27 | 26 | 1 | 0 |

58 个 Python compatibility 工具的逐项 `name/module/rpc_method/status/batch` 已输出至 `cw_mcp_python_compat_candidates_20260827.json`；40 个 task RPC 的逐项 `job_type` 已输出至 `cw_mcp_task_rpc_candidates_20260827.json`。这些文件是后续“一个工具一个任务”迁移卡的不可变候选清单，而不是建议批量修改。

## 三、Python 直接数据库代码：与公开入口分开盘点

对 `cli/`、`server/`、`db/` 的 AST 只读扫描发现 541 个函数级“直接数据库信号”（constructor、connect/cursor/execute/query/transaction 等）。其中绝大多数属于本来就承载 SQLite 实现的 `db/` 模块；它们应被视为 Rust migration 的业务实现库存，而不是 541 条对外 MCP/CLI 命令。

| 非 `db/` 路径（按静态 DB signal） | 函数数 | 解释 |
|---|---|---|
| server/durable_staging.py | 8 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/daemon_server.py | 4 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/health_check.py | 4 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/job_executor.py | 4 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/replicator.py | 3 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/daemon_autostart.py | 2 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/tools/tools_collab.py | 2 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/tools/tools_p2_graph.py | 2 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/_mcp_common.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/compat_worker.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/daemon_client.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/daemon_config.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/job_handlers.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/tools/tools_p3_identity.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/tools/tools_p4_lease.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/tools/tools_query.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/tools/tools_security.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/tools/tools_semantic.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/tools/tools_summary.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |
| server/tools/tools_task.py | 1 | 需进一步按可达性判定；见 JSON 中 function/line/calls。 |

其中真正影响当前 HTTP MCP 后端的核心是 `server/compat_worker.py` + 各 `server/tools/*` compat handler：前者负责受 daemon 控制的只读连接，后者保留 58 个工具的 Python 查询语义。`server/durable_staging.py`、`server/daemon_config.py` 和保留的 `server/job_executor.py` 是运维、阶段兼容或测试相关 DB 代码，需单独做运行时可达性审计，不能因存在调用就直接判断为生产主路径。

## 四、审计边界与下一步

本盘点回答的是**当前工作树源码和矩阵**，没有宣称正在运行的 daemon 二进制已与源码一致。此前已记录 live debug authority 与受控 runtime/current 制品存在 schema/可执行文件漂移；在独立审查与授权的受控刷新完成前，不能用 live daemon 的结果反向覆盖本源码审计，也不得将 P0-K 未审代码部署。

下一轮应优先对 58 项 `python_compat` 做逐工具可达性与 Rust handler parity 审查：每张卡只迁移一个 `rpc_method`，先验证 Rust dispatch/handler、负向权限、workspace isolation、返回形状和 no-fallback，再移除对应 compat registration。40 项 `task_rpc` 当前不应重新拆成 Python-to-Rust 迁移项；其正式生产 authority 已是 Rust job_runner/task handler，审计重点应是 job type 的功能与持久化语义，而不是重写 Python JobExecutor。

## 附件与复核路径

| 文件 | 内容 |
|---|---|
| `cw_cli_mcp_data_access_static_inventory_20260827.json` | 完整 AST/迁移矩阵交叉清单；含每个 MCP 工具、CLI parser entry、Python DB signals、Rust daemon file metrics。 |
| `cw_cli_mcp_data_access_static_inventory_20260827.csv` | 便于过滤的 MCP/CLI 逐项表。 |
| `cw_cli_direct_db_ast_20260827.json` | CLI main.py 的 234 个 db syntax call sites 与 daemon route call sites。 |
| `cw_mcp_python_compat_candidates_20260827.json` | 58 个仍由 Python compatibility worker 执行的 MCP 候选。 |
| `cw_mcp_task_rpc_candidates_20260827.json` | 40 个 Rust task/job RPC 工具及 18 个 job_type 标注。 |
| `cw_compat_route_source_evidence_20260827.txt` | MCP tools 模块对 compat route/readonly DB binding 的 source matches。 |

## References

[1] [`cli/main.py`](../../cli/main.py)：`RpcDBProxy` `_METHOD_MAP`、`_rpc_call`、`__getattr__` 及 legacy CLI 分支。
[2] [`server/watcher.py`](../../server/watcher.py)：FileWatcher 接收 db interface、Rust watcher 优先与兼容路径。
[3] [`server/job_executor.py`](../../server/job_executor.py)：Python JobExecutor 的保留兼容/测试定位及 Rust job_runner 生产权威说明。
[4] [`server/daemon_client.py`](../../server/daemon_client.py)：`route_rpc` 的 HTTP fail-closed 路由语义。
[5] [`server/compat_worker.py`](../../server/compat_worker.py)：daemon 控制的 compat frame 与只读 SQLite connection。
