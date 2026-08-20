# HTTP Daemon MVP Task Plan

> 父任务：`T-1786590214634-9e740cdc`
> 目标：先统一 HTTP 通讯并恢复自举，再按 capability 串行迁移到 Rust
> 前置任务：`T-1786590722456-db00d074`（Legacy Baseline：237 MCP tools unified-entry availability）

## 1. 串行原则

本计划与 M2 分开。先完成 Legacy Baseline，使 237 个工具都有统一入口和明确 backend；不要求本阶段全部 Rust 化。Legacy Baseline 完成前不得启动 H0/H1。M2.1-M2.5 作为 legacy transport 证据保留，不重开、不改历史证据，也不作为 HTTP round-trip 的替代。

每个子任务必须：

1. 只修改白名单文件；
2. 有明确成功/失败/重启验收；
3. 有真实进程级 HTTP round-trip；
4. 只推进到 `review`；
5. 经 Independent Reviewer PASS 后由 Coordinator apply/close；
6. 严格按依赖图启动：H1/H2 是唯一可并行分支；H2I PASS 前禁止 H3；H4B 必须等 H4A Reviewer PASS 且 Coordinator close 后启动。

## 2. 子任务拆分

### H0：契约与 capability registry（依赖 Legacy Baseline）

启动条件：`T-1786590722456-db00d074` 已经通过独立 Reviewer，且 237 工具矩阵中没有未解释的 `unknown` 或静默失败。

交付：

- 冻结 `http-daemon-mvp-compatibility-contract.md`；
- 定义 method registry、error envelope、health/capabilities；
- 定义 MVP Transport Profile，显式处理与现有 Named Pipe/UDS-only requirements 的开发期例外；
- 定义 HTTP manifest、synthetic local identity、worker IPC、write-class serialization、request-id dedup 和 jobs 边界；
- 创建 H2I、H4A、H4B 的 Role Contract，并为既有 H1/H2/H3 补齐权威 executor/reviewer 合同；H1/H2 必须等 H0 closed，H3 必须等 H2I PASS/closed；
- 不修改生产代码。

验收：文档审查、方法分类完整、无 HTTP 与当前安全需求冲突的未声明假设。

### H1：Rust HTTP server transport（可与 H2 并行）

真实任务：`T-1786590214634-9e740cdc-sub-2`。当前权威合同：
`RC-T-1786590214634-9e740cdc-sub-2-implementer-r1` 与
`RC-T-1786590214634-9e740cdc-sub-2-independent_reviewer-r1`。Implementer
合同把 H0 closed 固定为领取前置条件；在此之前不得 claim。

建议白名单：

- `rust_ext/Cargo.toml`；
- `rust_ext/src/daemon/http_server.rs`；
- `rust_ext/src/daemon/server.rs`；
- `rust_ext/src/daemon/mod.rs`；
- `rust_ext/src/bin/cw_daemon.rs`；
- Rust focused tests。

交付：

- loopback HTTP listener；
- `/health`、`/capabilities`、`/v1/rpc`；
- JSON-RPC dispatch 适配现有 daemon handler；
- body/timeout/request-id 基础校验；
- 非 loopback endpoint fail-closed；
- 不改变 Named Pipe/UDS 默认行为。

验收：Rust 单测、真实 `cw-daemon` HTTP health/capabilities/raw JSON-RPC round-trip、非 loopback 拒绝、动态端口 manifest、重启恢复。H1 不得声称 Python client 或 MCP 已可用。

### H2：Python HTTP client 与自动发现（可与 H1 并行）

真实任务：`T-1786590214634-9e740cdc-sub-3`。当前权威合同：
`RC-T-1786590214634-9e740cdc-sub-3-implementer-r1` 与
`RC-T-1786590214634-9e740cdc-sub-3-independent_reviewer-r1`。Implementer
合同把 H0 closed 固定为领取前置条件；H2 fake-server 测试不得冒充 H2I 实际集成。

建议白名单：

- `server/daemon_client.py`；
- `config.py`；
- `server/daemon_autostart.py`；
- `cli/` 中仅涉及 daemon endpoint 的文件；
- Python client tests。

交付：

- HTTP transport；
- `CW_DAEMON_TRANSPORT=http`；
- endpoint manifest；
- health/capabilities 探针；
- 有界 timeout；
- HTTP 业务错误原样保留；
- 默认禁止 HTTP 失败时静默直连 SQLite。

验收：Python 3.14 focused tests、contract fake-server 测试、manifest 解析、业务错误透传、连接失败 fail-closed。H2 在 H1 未关闭前不得声称真实 daemon round-trip 已通过。

### H2I：H1/H2 集成门禁（必须在 H3 前完成）

真实任务：`T-1786590214634-9e740cdc-h2i`（3 steps，tester + independent_reviewer Role Contracts）。

H2I 由独立 Integrator/Tester 执行，拥有独立 worktree、独立 HTTP manifest、动态端口和独立 evidence 目录。

交付：

- 当前 Git HEAD 的 Rust HTTP daemon；
- Python 3.14 production `DaemonClient`；
- health/capabilities/RPC/错误/timeout/restart 的真实端到端结果；
- HTTP 只绑定 loopback，MCP/CLI 尚未全面切换的明确边界。

门禁：H1 与 H2 均经 Reviewer PASS 和 Coordinator close 后，H2I PASS 才能启动 H3。

### H3：Python compatibility worker

真实任务：`T-1786590214634-9e740cdc-sub-4`。当前权威合同：
`RC-T-1786590214634-9e740cdc-sub-4-implementer-r1` 与
`RC-T-1786590214634-9e740cdc-sub-4-independent_reviewer-r1`。Implementer
合同把 H2I 的 Independent Reviewer PASS 与 Coordinator close 固定为领取前置条件；
H2I 未通过时不得 claim H3。

建议白名单：

- `server/compat_worker.py`；
- `server/compat_registry.py`；
- `rust_ext/src/daemon/compat_adapter.rs`；
- compatibility worker tests。

交付：

- daemon 管理 worker 生命周期和私有 child IPC；
- method/params/workspace context 传递；
- 建立可扩展 compatibility registry，并先恢复 `get_uncommented_symbols`、`stats` 或一个 build/publish 能力；
- worker 错误返回结构化 `E_COMPAT_WORKER_*`；
- worker 写操作经过 daemon 兼容写锁；
- 客户端不能直接启动或连接 worker。

验收：worker crash/restart、成功调用、异常调用、超时、无 worker fail-closed、SQLite 单写约束。

### H4A：MCP/CLI 核心薄壳与 self-bootstrap

真实任务：既有 `T-1786590214634-9e740cdc-sub-5`。H0 不重复创建 H4；该任务从本轮起正式解释为 **H4A**，并已冻结 `http-mvp-h4a-core-bootstrap/v1` Role Contract。原题名中的 “H4” 仅保留历史标识，不再代表 237 工具批量 cutover。

建议白名单：

- `server/tools/` 中路由适配文件；
- `server/mcp_server.py`；
- `cli/` 中路由适配文件；
- `tests/test_http_daemon_self_bootstrap.py`；
- `docs/mcp_tools.md`、`docs/cli_reference.md`。

交付：

- MCP/CLI 核心自举方法统一调用 HTTP client；
- 不直连 SQLite；
- 保持现有工具名和参数契约；
- native/compat/unsupported 状态可查询；
- health/workspace/task/query/至少一个 compatibility 方法可用。

验收：MCP/CLI 真实进程级测试、核心工具矩阵、无 daemon 时 fail-closed、重启恢复、跨工作区隔离。

### H4B：237 工具 compatibility cutover

真实父任务：`T-1786590214634-9e740cdc-h4b`（3 steps，planner + independent_reviewer Role Contracts）。

H4B 只在 H4A PASS 后启动。它按 H0 capability registry 将全部 237 个工具的客户端入口改为 HTTP native 或 HTTP compatibility route；不要求将工具业务逻辑重写为 Rust。

验收：所有 237 工具在矩阵中都有 HTTP route、backend、operation class、成功或明确结构化错误案例；没有 MCP/CLI 生产 SQLite 直连；不允许用静态注册数代替实际 routing evidence。

H0 已创建以下非重叠 children；六个任务均依赖 H4A PASS/closed 与 H4B planner gate：

| shard | 真实任务 ID | 唯一所有权摘要 |
| --- | --- | --- |
| native read/query | `T-1786590214634-9e740cdc-h4b-native-read` | `tools_query.py`、`tools_workspace.py`、native read test |
| compatibility read | `T-1786590214634-9e740cdc-h4b-compat-read` | `tools_summary.py`、`tools_semantic.py`、compat read test |
| index-write/job | `T-1786590214634-9e740cdc-h4b-index-job` | `tools_security.py`、`tools_rules.py`、index/job test |
| governance/unsupported/error | `T-1786590214634-9e740cdc-h4b-unsupported-error` | task/collab/P2/P3/P4 wrappers、error test |
| registry/docs | `T-1786590214634-9e740cdc-h4b-registry-docs` | compat registry、MCP/CLI docs、registry test |
| full matrix/evidence | `T-1786590214634-9e740cdc-h4b-full-matrix` | 237-row test 与 capability matrix evidence |

同一 shard 内的文件是其唯一 edit ownership；兄弟任务不得吸收对方文件。每个 child
均有 executor 与 Independent Reviewer Role Contract、至少 3 个实际步骤以及逐步
`target_file`，不存在 `Steps(0)`。

### H4C：python_compat read-only worker 可用性推进（第一批：基建+符号组+任务组）

真实父任务：`T-1786713075400-c8920cf4`（3 steps：freeze / verify / aggregate，planner gate）。

H4C 继承 H4B（已 closed）。H4B 完成 237 工具 cutover：193 个 python_compat 中除
2 个 compat_route 外全部 HTTP 模式 fail-closed `E_HTTP_COMPAT_UNSUPPORTED`。H4C
按父任务「未迁移能力通过 daemon 管理的 Python compatibility worker 保持可用」目标，
把 read_only python_compat 工具推进到经 H3 worker 实际执行。write 类
（governance_write / index_write）维持 fail-closed（MVP 契约，worker 收到即拒）。

公开方法名/参数契约不变；不重写业务为 Rust；worker 契约不变（read_only only、
daemon 注入显式 workspace context）。

**第一批（已完成并冻结，2026-08-15）**：worker 基建（H4C-1）+ 符号组（H4C-2）+
任务组（H4C-3）+ registry 2→35 适配全部闭环。第一批交付定义冻结，详见下方
§7.1「第一批冻结记录」、§7.2「第一批完成度核验」、§7.3「第一批交付聚合」。

**后续批次（保持开放定义）**：
- compat read 组：`tools_summary.py` / `tools_semantic.py` read_only；
- index 组：`tools_security.py` / `tools_rules.py` read_only；
- governance 其余组：collab / P2 / P3 / P4 read_only。

**第二批（已完成并冻结，2026-08-15，父任务 `T-1786747271865-5af00698` 收口）**：
上述三组全部闭环，compat registry 35 → 67 → 92 → 107，详见下方 §7.4「第二批冻结记录」、
§7.5「第二批完成度核验」、§7.6「第二批交付聚合」。

#### 7.1 第一批冻结记录（gate #0 freeze）

**第一批完成并冻结（2026-08-15，T-1786713075400-c8920cf4 收口）**：

| 交付件 | 真实任务 | 状态 |
| --- | --- | --- |
| worker 基建：registry 批量 read_only 注册 + Rust compat_route 批量白名单 + 两端对齐门 + daemon_client worker 路由便捷方法 | `T-1786713075422-d9a98426`（H4C-1） | closed |
| 符号组 read-only 工具接入 worker（tools_query.py，17 个） | `T-1786716190783-ba187c88`（H4C-2+3 合并） | closed |
| 任务组 read-only 工具接入 worker（tools_task.py，16 个） | `T-1786716190783-ba187c88`（H4C-2+3 合并） | closed |
| 原 H4C-2 符号组计划（并入合并任务，步骤 skipped） | `T-1786713075423-68e08544` | closed |
| 原 H4C-3 任务组计划（并入合并任务，步骤 skipped） | `T-1786713075423-a02e7630` | closed |
| H4B-R registry 测试断言同步（registry 2→35） | `T-1786721363018-63aa9993` | closed |

后续批次（compat read 组 summary/semantic、index 组 security/rules 等）保持开放
定义，不在本批冻结范围内；开工前需按 §1 串行原则与父任务依赖图重新领取。

#### 7.2 第一批完成度核验（gate #1 verify）

独立核验证据（2026-08-15，脚本 `.trae-cn/evidence/h4c_gate_verify.py`，只读）：

1. **5 子任务全部 closed**：H4C-1（3/3）、H4C-2+3 合并（4/4）、原 H4C-2（3/3 skipped）、
   原 H4C-3（3/3 skipped）、registry 适配（4/4）。满足 AGENTS.md 规则 7 父任务门禁。
2. **compat registry 35 项三端对齐**：
   - Rust `COMPAT_ROUTE_WHITELIST`（`rust_ext/src/daemon/http_server.rs`）= **35 项**
     （H4C-1 默认 2 + H4C-2 符号组 17 + H4C-3 任务组 16）；
   - Python registry 单例（`server/compat_registry.py` `RUST_COMPAT_ROUTE` + 默认 registry
     + `tools_query`/`tools_task` 模块级 `register_compat_routes` 注册）= **35 项**；
   - `validate_against_rust_route()` → `aligned=True`，missing/extra/mismatch 均空；
   - matrix metadata `compat_registry.registered_methods = 35`。
3. **capability matrix 标注**（`.trae-cn/evidence/http-daemon-capability-matrix.json`，
   237 tools）：35 个 worker 方法中 34 个以 MCP 工具名存在于矩阵，
   `backend=python_compat` 且 `direct_sqlite_access: false` 全部通过；
   `stats_top_files` 为 worker 内部方法名（非公开 MCP 工具，对应 `get_stats` 走
   rust_native daemon client），属预期例外。矩阵与 registry 三端一致。
4. **docs/mcp_tools.md**：头部「HTTP MVP 路由状态」已写明 35 个 read_only
   python_compat 方法路由到 H3 compat worker（H4C-1 默认 2 + H4C-2 17 + H4C-3 16，
   `COMPAT_ROUTE_WHITELIST`），并注明两端一致性对齐门。

结论：第一批完成度核验 **PASS**。

#### 7.3 第一批交付聚合（gate #2 aggregate）

第一批关键产出汇总（2026-08-15）：

- **H4C-1 三 API**：`register_read_only_batch`（CompatRegistry 批量注册）、
  `register_compat_routes`（模块级批量注册 + 同步 RUST_COMPAT_ROUTE）、
  `validate_against_rust_route`（两端对齐门）+ Rust `COMPAT_ROUTE_WHITELIST`
  批量白名单 + daemon_client worker 路由便捷方法；
- **H4C-2+3 工具层路由接入 + 整改闭环**：`tools_query.py` 符号组 17 handler +
  `tools_task.py` 任务组 16 handler（模块级 `register_compat_routes` 装配），
  Rust `http_server.rs` 3 处同步（COMPAT_ROUTE_WHITELIST + build_capability_registry
  python_compat 行）；governance_write task 工具保持 fail-closed；
- **registry 适配 35 全量**：H4B-R 遗留测试 `test_http_capability_registry.py`
  断言由 2→35 同步，7 个过时用例修复；
- **测试门数量**：combined cutover `test_http_combined_worker_cutover.py` 15 +
  capability matrix `test_http_daemon_capability_matrix.py` 17 + registry
  `test_http_capability_registry.py` 25 = 57 用例门。

验收记录：三个 gate 步骤均 done，result 记录真实核验证据；生产代码零改动
（仅 `docs/design/http-daemon-mvp-task-plan.md` 与证据脚本更新）。

#### 7.4 第二批冻结记录（gate #0 freeze）

**第二批完成并冻结（2026-08-15，父任务 `T-1786747271865-5af00698` 收口）**：

第一批完成并冻结后，后续批次按 §1 串行原则领取，见 §2 后续批次。第二批把 §2 后续批次
列出的三组 read_only 工具全部接入 worker，compat registry 35 → 67 → 92 → 107：

| 交付件 | 真实任务 | 状态 |
| --- | --- | --- |
| compat read 组：`tools_summary.py` 27 + `tools_semantic.py` 5（35→67） | `T-1786747295213-64204cce` | closed |
| index 组：`tools_security.py` 17 + `tools_rules.py` 8（67→92） | `T-1786747295227-49c90d68` | closed |
| governance 组：`tools_collab.py` 4 + `tools_p2_graph.py` 5 + `tools_p3_identity.py` 5 + `tools_p4_lease.py` 1（92→107） | `T-1786747295227-b876fddf` | closed |

第二批 3 子任务全部 closed，满足 AGENTS.md 规则 7 父任务门禁；第二批批次定义冻结。

#### 7.5 第二批完成度核验（gate #1 verify）

独立核验证据（2026-08-15）：

1. **3 子任务全部 closed**：① compat read 组 `T-1786747295213-64204cce`
   （tools_summary 27 + tools_semantic 5，35→67）；② index 组
   `T-1786747295227-49c90d68`（tools_security 17 + tools_rules 8，67→92）；
   ③ governance 组 `T-1786747295227-b876fddf`（tools_collab 4 +
   tools_p2_graph 5 + tools_p3_identity 5 + tools_p4_lease 1，92→107）。
2. **compat registry 107 项三端对齐**：
   - Rust `COMPAT_ROUTE_WHITELIST`（`rust_ext/src/daemon/http_server.rs`）= **107 项**；
   - Python registry 单例（`server/compat_registry.py` `RUST_COMPAT_ROUTE` + 模块级
     `register_compat_routes` 批量注册同步）= **107 项**；
   - `validate_against_rust_route()` → `aligned=True`，missing/extra/mismatch 均空。
3. **语义分类**（governance 语义面完整分类，15 + 11 + 5 = 31 个方法）：
   - 只读 **15 个**接入 worker（route_worker_call 经 compat worker 执行）：
     collab 4 + p2_graph 5 + p3_identity 5 + p4_lease 1；
   - 写语义 **11 个**维持 fail-closed `E_HTTP_COMPAT_UNSUPPORTED`：
     import_envelope_dependencies / record_artifact_identity / publish_interface /
     select_interface_provider / build_hard_dependency_edges / record_action_identity /
     register_attestation_revocation / assignment_create / assignment_revoke /
     submit_verdict / append_evidence；
   - `lease.*` **5 个** rust_native 保留真名透传（lease_acquire→lease.acquire 等）。
4. **测试门**：`tests/test_http_combined_worker_cutover.py`
   （EXPECTED_TOTAL=107）、`tests/test_http_capability_registry.py`
   （_EXPECTED_COMPAT_METHODS_107）、`tests/test_http_governance_error_cutover.py`
   （含真实 daemon 门）组合运行 **150 passed**。
5. **文档同步**：`docs/mcp_tools.md` 与 `docs/cli_reference.md` 已更新 compat
   路由数字 107、其余 python_compat 86。

结论：第二批完成度核验 **PASS**。

#### 7.6 第二批交付聚合（gate #2 aggregate）

第二批关键产出汇总（2026-08-15）：

- **三组 72 个只读方法接入 worker**：compat read 组 32（summary 27 + semantic 5）、
  index 组 25（security 17 + rules 8）、governance 组 15（collab 4 + p2_graph 5 +
  p3_identity 5 + p4_lease 1），均经模块级 `register_compat_routes` 装配 + Rust
  `http_server.rs` COMPAT_ROUTE_WHITELIST 同步，HTTP 模式经 route_worker_call
  由 compat worker 执行；
- **写语义 fail-closed 不变**：governance 面 11 个写语义方法维持
  `E_HTTP_COMPAT_UNSUPPORTED` 结构化失败，绝不触碰 get_db()/本地 SQLite；
  `lease.*` 5 个 rust_native 保留真名透传（dispatch.rs 真实 lease.* RPC 分支，
  禁止改动）；
- **测试门数量**：combined_worker_cutover（EXPECTED_TOTAL=107）+
  capability_registry（_EXPECTED_COMPAT_METHODS_107）+
  governance_error_cutover（含真实 daemon 门）组合运行 **150 passed**；
- **文档同步**：`docs/mcp_tools.md` / `docs/cli_reference.md` compat 路由
  数字同步 107、其余 python_compat 86。

验收记录：父任务 `T-1786747271865-5af00698` 已收口，3 子任务 closed；生产代码
零改动（仅 `docs/design/http-daemon-mvp-task-plan.md` 补录本批记录）。

### H5：HTTP MVP 独立复审与统一部署

交付：

- 当前 Git commit 构建的 fresh daemon；
- Python 3.14 / Rust toolchain / binary provenance；
- 完整 HTTP 自举 evidence bundle；
- capability registry 快照；
- 后续 Rust migration 恢复顺序的决策记录。

门禁：Reviewer PASS 后 Coordinator 才能 apply/close；M2.5 只有在 H5 PASS 后重新启动。

## 3. 后续 Rust 迁移顺序

Legacy Baseline PASS 后，先完成 H0 → (H1 || H2) → H2I → H3 → H4A → H4B → H5。HTTP MVP PASS 后，按以下顺序串行迁移：

1. workspace/snapshot/manifest/refresh；
2. stats/uncommented/metrics；
3. build/build_directory/semgrep/job；
4. Git、coverage、defect、review 等工具组；
5. 删除已替换的 compatibility worker handlers。

每组迁移都只切换 capability registry 的 backend，不修改客户端公开方法名。

## 4. 失败与回滚

- HTTP listener 失败：保留 Named Pipe/UDS，MVP 任务 BLOCKED，不改客户端默认安全路径；
- compatibility worker 失败：返回结构化错误，不直连 SQLite；
- native handler 失败：保留 `python_compat`，不得宣称 Rust 迁移完成；
- fresh binary 不可复现：UNVERIFIED，不推进任务状态；
- 任一测试超时：记录原始 timeout，不以旧 binary 或静态检查替代成功。

## 5. 角色交接

```text
planner/H0
  -> implementer/H1 || implementer/H2
  -> independent_reviewer/H1 || independent_reviewer/H2
  -> integrator/H2I
  -> implementer/H3
  -> independent_reviewer/H3
  -> implementer/H4A
  -> implementer/H4B
  -> tester/evidence
  -> independent_reviewer/H5
  -> coordinator apply/close
```

Implementer、Tester、Evidence、Independent Reviewer 均不得越权 apply/close；Coordinator 必须使用真实 identity 和有效 lease。

## 6. Legacy Baseline 交接记录（B6，2026-08-13）

前置任务 `T-1786590722456-db00d074`（Legacy Baseline：237 MCP tools unified-entry availability）B 系列已完成至 B6（`T-1786590722456-db00d074-sub-6`，剩余工具组与全量验收）。B6 交接证据：

- **237 工具最终矩阵已固化**：`current_status` 收口为 `runtime_verified=54`（B 系列测试运行时覆盖）+ `entry_verified=183`（B6 入口核验通过），`unknown=0`，满足 H0 启动条件（§2 L24"无未解释 unknown"）；矩阵与 SHA 记录位于 `.trae-cn/evidence/mcp-tool-matrix-baseline.json`（本地 evidence，gitignore，不随 commit）；
- **全量入口核验 237/237 通过**：source_file 存在 + `def {tool_name}` 定义 + `@mcp.tool(` 注册 + 函数体统一入口引用（get_db / daemon client），无客户端直连 SQLite；
- **全量守护测试** `tests/test_legacy_237_tools_baseline.py`：8 passed（0.79s），含 current_status 收口断言与全量入口冒烟断言；
- **详细记录**：账本 §9.8（`docs/design/daemon-rust-migration-ledger.md`）。

**交接结论**：B6 closed 后 B 父任务即可由 Coordinator 收口，随后按 §2 启动 H0。H4B 所需"237 工具在矩阵中都有 HTTP route/backend/operation class/成功或结构化错误案例"以本矩阵为基础，H0 阶段需将 `entry_verified` 工具逐项补运行时 route evidence。
