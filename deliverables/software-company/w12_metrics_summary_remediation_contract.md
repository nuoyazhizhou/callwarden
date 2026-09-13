# W12 承接卡：`query.metrics_summary` 越 scope 契约缺陷修复

- **承接卡 task_id**：`T-1789274621921-e5464ad8`（daemon 权威 `task.create` 创建；status=`open`）
- **承接来源**：`T-1788871227327-45c94bd8`（PYT 回归卡 step#4 `fix_defect`）
- **裁决**：方案 A —— 剥离为独立 remediation 卡承接（用户 2026-09-13 裁决）
- **裁决依据**：[pyt_regression_step4_handoff_backlog.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step4_handoff_backlog.md#L190-L250) 的 W12
  与本合同同目录；W12 记录触发、缺陷、修复落地与消费者影响结论。
- **建卡脚本**：[create_w12_metrics_summary_remediation_task.py](file:///c:/git_work/callwarden/deliverables/software-company/create_w12_metrics_summary_remediation_task.py)
- **状态**：改动已在工作树（**未提交**），由本卡承接并负责提交与回归闭环。

---

## 1. 为什么需要独立卡

PYT 回归卡 step#4 的合同 `allowed_paths = deliverables/software-company/**、tests/**`，
`forbidden_paths` 明确含 `rust_ext/src/**`、`rust_ext/src/daemon/**`、`server/**`。

`tests/test_cli_046_http_rpc.py` 按「覆盖真实 HTTP transport」改写后（移除 `route_rpc` 打桩、
改打隔离 daemon），实跑暴露两个**生产真缺陷**，修复必须落在上述 forbidden 路径。按方案 A，
这批越 scope 改动**不留在 PYT 卡**，改由本 remediation 卡承接（保留现有改动、不改写历史）。

## 2. 缺陷

- **(A) 空 workspace 崩溃**：`rust_ext/src/daemon/metrics_handlers.rs` 的
  `SELECT AVG(s.depth)` 在无匹配行时返回 NULL，`scalar_f64` 转换失败 → `internal_error`。
  即任何空 workspace 上 `cw metrics` 必崩。
- **(B) 返回契约不一致**：`query.metrics_summary` 原返回私有 7 字段
  (`symbols` / `calls` / `files` / `commented_symbols` / `functions` / `avg_depth` / `comment_coverage`)，
  而客户端渲染契约与 legacy 权威 `db/db_metrics.py::get_code_metrics_summary` 均为 8 字段
  （`file_count` / `function_count` / `total_lines` / `total_calls` / `avg_complexity` /
  `max_complexity` / `complexity_distribution` / `comment_coverage`）
  → `cw metrics`（RpcDBProxy → `query.metrics_summary`）渲染时报 `KeyError: 'file_count'`。

## 3. 改动范围

### 3.1 allowed（本卡自有）

| 文件 | 改动 |
|---|---|
| `rust_ext/src/daemon/metrics_handlers.rs` | `handle_metrics_summary` 改为复用既有 `summary_metrics_summary`；删除失效的 `scalar_f64` |
| `rust_ext/src/daemon/query_compat_handlers.rs` | `summary_metrics_summary` 由 `fn` 提升为 `pub(crate) fn` + 补 doc |
| `server/tools/tools_workspace.py` | 新增 `CodeMetricsSummary(TypedDict)` 显式 output 契约；`get_code_metrics_summary` 返回注解 `-> dict` 改为 `-> CodeMetricsSummary` |
| `tests/test_cli_046_http_rpc.py` | 真实 HTTP transport 回归（`w3_live` 隔离 daemon + HTTP 单例装载 + 出向信封断言） |
| `tests/test_cli_084_http_rpc.py` … `tests/test_cli_088_http_rpc.py` | 同源迁移回归 |
| `deliverables/software-company/**` | 卡片/合同/证据 |

### 3.2 forbidden

`cli/**`、`db/**`、`scripts/refresh_shared_runtime.ps1`；
以及受保护生命周期写 `task.apply` / `task.close` / `task.supersede` / 状态伪造。

## 4. 契约要点（单一真相源）

- 8 字段 legacy 契约的 Rust 权威实现 = `query_compat_handlers::summary_metrics_summary`，
  由 `query.metrics_summary` 与 `project_brief` **共用**，避免出现第二套字段集。
- Python 侧 `CodeMetricsSummary(TypedDict)` 逐字段对齐该实现；FastMCP 依据返回注解生成
  `outputSchema` 并在工具返回时校验、fail-closed（pydantic 默认 `extra=ignore`，
  多余键容忍，未来加字段不炸）。

## 5. 消费者影响（已穷尽检索）

**无任何消费者依赖旧 7 字段**。

- 仓库内：`avg_depth` 全仓库 0 引用；`commented_symbols` 命中均为**不同来源**
  （`dashboard["code_scale"]` / `stats.get("commented")`），非 `query.metrics_summary` 回包。
- 仓库外：7 个外部 MCP 客户端配置仅含 server 启动声明（零字段引用）；
  TRAE 缓存的 tool 描述符只含 input schema、无 `outputSchema`（过期缓存、客户端重启即刷新）；
  `.trae-cn\mcps` 全量缓存 `avg_depth` 0 命中；外部项目仅 TokenSlim 自带的 code_graph 拷贝命中，
  且其消费 callwarden 的方式是 CLI（`cw.py --workspace <ROOT> <子命令>`），不经 metrics 字段。

详见 `pyt_regression_step4_handoff_backlog.md` W12「消费者影响（仓库内/仓库外）」。

## 6. 验收

1. `cargo test` 覆盖 `daemon::metrics_handlers` 单测（修复前 6 passed）。
2. `pytest tests/test_cli_046_http_rpc.py tests/test_cli_084_http_rpc.py … tests/test_cli_088_http_rpc.py`
   全绿；且 `test_cli_046` 的出向信封断言命中 `query.metrics_summary`（证明真实走 HTTP transport）。
3. `git diff --check` 干净。
4. 提交前缀使用**本卡 task_id**，不得复用 `T-1788871227327-45c94bd8`。
5. 不触发 shared runtime 发布；不修改 `cli/**`、`db/**`。

## 7. 与 PYT 卡的关系

PYT 卡保持 tests-only 边界（其 step#4 仍只承接 `tests/**` 与 `deliverables/**` 的迁移工作）；
本卡的 3 处生产改动与 PYT 卡 step#4 的验收解耦：PYT 卡无需、也不得提交这 3 个文件。
