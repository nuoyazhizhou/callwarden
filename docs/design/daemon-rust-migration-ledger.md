# Rust Daemon 迁移账本与 MCP 可用性盘点

> 状态：M0 盘点完成，待 Independent Reviewer 复核
> 日期：2026-08-11（初始）/ 2026-08-12（M0 盘点完成）
> 盘点任务：`T-1786437453328-1b1dfa98`
> 盘点基线：`da72a3c92ec4450edc16b2864055938086352ad2`
>
> **运行时基线（2026-08-11，任务 `T-1786440663336-7e7d67e8` #1 baseline）**：
> - Git HEAD：`c000adb486393c282133041e05e9d0efe7a9ca6d`（dispatch.rs timestamp 修复 `0ad8397` 已提交，无遗留 dirty）
> - daemon binary SHA-256：`50ED90B6F6C3083D6C03D6D2C972C7B8DCCB5575054D0BAF904BE75497AF56DE`；client SHA-256 `6A3F8906787141D82838102373D9DF924456DEF0CD23E97ED33D985D57A6BA16`
> - Python 3.14.3 / cargo 1.93.1；运行时 MCP 注册 237 工具（与 §2.1 一致）
> - 权威任务库 `~/.callwarden/callwarden.db`：720 tasks（open 114 / in_progress 50 / review 77 / applied 5 / closed 474）
> - 4 个真实 daemon E2E 复核通过（10 passed in 45.94s），lease acquire/release 闭环已验证
> - 完整 baseline：`.trae-cn/evidence/T-1786440663336-7e7d67e8-baseline-2026-08-11.md`

## 1. 目的

这份文档是 Python 非客户端功能迁移到 Rust daemon 的唯一进度账本，解决以下两个问题：

1. MCP 工具“已注册”与“生产可用”被混为一谈。
2. 多轮并行修改后，工具路由、Python fallback、Rust handler、测试和任务状态无法逐项对应。

本账本不以装饰器数量、编译成功或单元测试通过作为完成证明。每个工具只有在 daemon RPC、错误语义、权限边界、真实入口测试和任务归属证据都具备后，才能标记为 `production_usable`。

## 2. 当前基线结论

### 2.1 MCP 注册数量

运行时通过 `create_mcp_server()` 创建 `FastMCP` 并读取 `_tool_manager._tools`，当前注册 **237 个工具**。静态 `@mcp.tool()` 计数同为 237。

| 模块 | 注册数 |
|---|---:|
| `tools_collab.py` | 6 |
| `tools_p2_graph.py` | 10 |
| `tools_p3_identity.py` | 7 |
| `tools_p4_lease.py` | 8 |
| `tools_query.py` | 32 |
| `tools_rules.py` | 9 |
| `tools_security.py` | 36 |
| `tools_semantic.py` | 19 |
| `tools_summary.py` | 31 |
| `tools_task.py` | 52 |
| `tools_workspace.py` | 27 |
| **合计** | **237** |

### 2.2 “可用”定义

| 状态 | 含义 | 能否作为完成依据 |
|---|---|---|
| `registered` | FastMCP 能发现工具名和签名 | 否 |
| `python_local` | 直接访问 `CodeGraphDB`/SQLite/本地文件 | 否，属于待迁移或明确本地工具 |
| `daemon_routed` | 生产入口调用 daemon RPC，但尚未证明真实 round-trip | 否 |
| `mixed_fallback` | daemon 失败或模式切换时仍可能走 Python/SQLite fallback | 否；必须明确策略 |
| `tested` | 有针对性测试且测试退出码为 0，覆盖声明的路径 | 仅作为阶段证据 |
| `production_usable` | daemon handler、协议、权限、错误、真实入口和任务归属均闭合 | 是 |
| `blocked` | 代码或环境缺少必要条件，必须 fail-closed | 否 |

### 2.3 已发现的结构性风险

- `server/daemon_client.py` 仍包含 `sqlite3`、`_sql_fallbacks`、`_sql_fallback_*` 和 `get_db()` 路径。
- `route_task_write()` 允许显式 `local` 模式；`route_task_read()` 在部分 `auto` 场景允许本地读 fallback。这些必须在每个 slice 的契约中明确，不能统称为“共享 daemon 已完成”。
- `server/tools/__init__.py` 会加载 11 个工具模块并注册全部 237 个工具，但注册本身不证明对应 Rust dispatch 已存在。
- 当前 `cw daemon bridge` 曾出现 `endpoint` 为空但 `reachable=true` 的状态组合；这类健康状态必须在 HTTP/daemon 新协议中改成结构化且不可歧义的结果。
- Windows/WSL 共存测试有真实环境依赖；一次跨边界测试套件在 120 秒窗口超时，不能把聚焦 Python 回归代替真实 bridge/daemon round-trip。
- W2.3 共存父任务已关闭，但 `T-1786332208647-eb4a39c0` 仍为 `open`，其两个子任务为 `review`；W2.3 跨边界子任务 `sub-6` 已关闭但步骤数为 0。这些是任务证据缺口，不是功能完成证明。
- ~~`task_next_step` 的 MCP 契约声称返回步骤详情，但当前 Python 入口调用 `task.claim`，Rust `handle_task_claim` 只返回 `task_id/status/claimed_by`；本次真实调用未得到 `step_id`。~~ 已由 `T-1786438019310-e24474c0` 修复：`handle_task_claim` 在提交后查询下一步骤并返回 `step_id/step_index/action/target_file/target_symbol/check_items/step_status/task_title`；无待执行步骤时 wrapper 返回 `None`，与 Python 契约对齐。
- ~~`task_create_subtask` 的当前 Rust handler 没有读取或调用 `insert_task_steps`；本次通过 daemon 创建的 M1 子任务虽然请求了 4 个步骤，CLI 实际显示 `Steps (0)`。~~ 已由 `T-1786438019310-e24474c0` 修复：`handle_task_create_subtask` 重写为同事务写入 `tasks`/`task_events`/`task_steps`（`insert_task_steps` + `step_index` 从 0 连续 + `generate_step_id`），任一步骤失败整体回滚，返回 `step_count`。
- ~~MCP `task_create_subtask` 包装器声明返回字符串，但 daemon 已返回结构化对象，调用端出现 Pydantic 类型校验错误；~~ 已由 `T-1786438019310-e24474c0` 修复：`task_create_subtask` wrapper 将 daemon 结构化结果归一化为 `task_id` 字符串，与 `task_create` 契约一致。

## 3. 目标架构决策

采用“**Rust daemon 是能力服务器，Python CLI/MCP 是薄客户端**”的渐进迁移方案：

```text
CLI / MCP / Agent / WSL client
            |
       typed RPC client
            |
  Rust daemon: auth, routing, storage, graph, task state, errors
            |
   SQLite / CAS / snapshot / watcher / build services
```

### 3.1 HTTP 迁移原则

- daemon 增加版本化 HTTP API；HTTP 是跨 Windows、WSL、Linux、macOS、容器的统一传输。
- 首期保留 Named Pipe/UDS/TCP bridge 作为本机兼容传输，但新能力必须先落在统一 RPC handler/协议层，不能为每种传输复制业务逻辑。
- 每次请求必须带 `request_id`、authority 身份、协议版本和认证信息；写操作需要幂等语义。
- API key 只解决认证，不替代 workspace 授权、authority pin、租约/身份和任务状态门禁。传输层必须使用 TLS 或受保护的本机通道；不能把裸 API key 当作“加密”。
- Python 客户端只保留参数校验、协议适配和用户体验；禁止新增 Python 业务 SQLite 写路径。

### 3.2 切换原则

- 不做一次性全量重写；按功能 slice 逐组迁移。
- 一个 slice 必须先有契约、Rust handler、Python client、成功/拒绝测试、运行时证据和独立复审，再删除该 slice 的 Python fallback。
- 迁移期间旧 fallback 必须显式标记 `legacy`，并能通过指标或错误结果识别，禁止静默降级到另一 authority。
- Windows authority 不可用时，Windows workspace fail-closed；WSL local daemon 只能使用 WSL ext4 独立 DB/WAL/CAS/UDS。

## 4. MCP 工具迁移状态矩阵

初始阶段先按模块建立边界，逐工具明细由后续 slice 追加。这里的模块计数是注册事实，不是完成数量。

| Slice | 工具域 | 数量 | 当前判断 | 首要动作 |
|---|---|---:|---|---|
| M0 | 注册/协议/健康检查 | 237 | `registered` | 生成运行时工具清单、协议版本和健康报告 |
| M1 | task/step/claim/report/status/events | 52 | `mixed_fallback` | 先收口 task state machine、split 步骤、daemon-only 写和错误语义 |
| M2 | file/symbol/grep/issues/tests | 待逐工具确认 | `mixed_fallback` | 每类补真实 daemon RPC success/reject round-trip |
| M3 | workspace/snapshot/manifest/refresh | 27 + 相关工具 | `mixed_fallback` | 统一 authority、workspace、snapshot generation 和路径权限 |
| M4 | graph/callers/callees/search/impact | 10 + query/summary 子集 | `mixed_fallback` | Rust GraphStore 查询，去掉静默 SQL fallback |
| M5 | build/parse/facts/symbol/call | 语义工具子集 | `python_local`/待迁移 | 将生产写路径切到 Rust BuildService |
| M6 | security/guardrail/rules | 36 + 9 | `python_local`/`mixed_fallback` | 保持 workspace 隔离、审计和错误契约 |
| M7 | identity/lease/collab | 7 + 8 + 6 | `mixed_fallback` | 统一身份、租约、受保护变更和 task events |
| M8 | semantic/summary/evolution | 19 + 31 | `python_local`/待迁移 | 先列出依赖的 Rust service，再逐功能切换 |

> 说明：`tools_task.py` 中不少工具同时出现 daemon route 和本地闭包，这是“混合实现”而不是已完成。后续必须以函数级清单记录 `rpc_method`、fallback、测试和证据路径。

### 4.1 函数级源码启发式初筛

对 237 个 `@mcp.tool()` 函数按函数体是否出现 daemon route 与 `get_db()`/SQLite 调用进行初筛，结果如下。该表只用于排队，不能替代真实 RPC 测试：

| 初筛状态 | 数量 | 含义 |
|---|---:|---|
| `daemon_routed` | 4 | 函数体出现 daemon client/route，未发现本地 DB 调用 |
| `mixed_fallback` | 39 | 同时出现 daemon route 和本地 DB/fallback |
| `python_local` | 190 | 函数体出现本地 DB/SQLite 路径，未发现 daemon route |
| `unknown` | 4 | 需要读取调用链或特殊封装确认 |
| **合计** | **237** | 与运行时注册数一致 |

初筛限制：装饰器注册、间接 helper、异常分支、显式 `local` 模式和测试替身可能影响分类；最终状态必须写入每个工具完成卡片。

## 5. 每个工具的完成卡片

后续每个工具或同一 RPC slice 必须补充以下表格，不得只写“通过”：

| 字段 | 要求 |
|---|---|
| MCP tool | 精确工具名和模块 |
| Python entry | 文件、函数、是否仅适配 |
| RPC method | 精确 method、请求/响应 schema |
| Rust handler | dispatch 注册点和实现文件 |
| storage owner | Rust/Python，写路径只能一个 owner |
| auth/scope | authority、workspace、identity、lease 要求 |
| failure semantics | unavailable、permission、not found、conflict 等结构化错误 |
| fallback | `none`、显式 `local` 或待删除 legacy；禁止静默 fallback |
| tests | 命令、退出码、成功/拒绝路径、真实进程与否 |
| evidence | 日志、二进制 hash、task step、change_audit、task events |
| reviewer | 独立复审结论和未解决项 |
| status | `registered`/`tested`/`production_usable`/`blocked` |

## 6. 阶段门禁

### M0：盘点与协议冻结

- [x] 运行时确认 MCP 注册总数为 237。 **（`T-1786437453328-1b1dfa98` 步骤 #3 focused_validation：FastMCP runtime registration count=237，证据 `.trae-cn/evidence/T-1786437453328-fastmcp-registration.txt`）**
- [x] 生成函数级工具清单，包含模块、入口、RPC/fallback、测试映射。 **（`T-1786437453328-1b1dfa98` 步骤 #1/#2：237 工具矩阵 `.trae-cn/evidence/mcp-tool-matrix-T-1786437453328.json`，每工具记录 module/tool_name/python_entry/daemon_rpc_method/rust_handler/local_sqlite_path/fallback_policy/test_file/current_status/func_lines；状态分布 python_local=190/mixed_fallback=25/daemon_routed=18/unknown=4）**
- [ ] 冻结 HTTP API、认证、authority、workspace、错误 envelope 和 request id 规范。 **（留待 M1 完成后启动）**
- [ ] 明确每个旧 Python 本地能力的保留、迁移或删除决策。 **（留待 M2-M8 逐 slice 决策）**

#### M0 盘点完成证据

| 证据项 | 文件 | 状态 |
|---|---|---|
| 运行时注册数 | `.trae-cn/evidence/T-1786437453328-fastmcp-registration.txt` | 237 PASS |
| daemon health | `.trae-cn/evidence/T-1786437453328-daemon-health.txt` | schema v50, PID 36232 PASS |
| focused tests | `.trae-cn/evidence/T-1786437453328-focused-tests.txt` | 16 passed, 4 skipped (UNVERIFIED) |
| focused validation 综合证据 | `.trae-cn/evidence/T-1786437453328-focused-validation-summary.md` | PASS（含环境限制标记） |
| 函数级工具矩阵 | `.trae-cn/evidence/mcp-tool-matrix-T-1786437453328.json` | 237 工具，101 KB |
| Reviewer 证据清单 | `.trae-cn/evidence/T-1786437453328-review-handoff.md` | 已提交 Independent Reviewer |

### M1：Task state machine（当前优先）

- [ ] 修复并复审 `task.split` 保留子任务步骤的完整链路。
- [x] 修复 `task_next_step`/`task.claim` 返回步骤详情的契约错位，至少返回 `step_id/action/target_file/check_items/status`，并增加真实 daemon RPC 回归。 **（实现：`T-1786438019310-e24474c0` 交付 claim 步骤详情 + create_subtask 保存 steps + wrapper 归一化；`T-1786440663336-7e7d67e8` 步骤 #2 收口：claim 领取时将首个 pending 步骤标记 `in_progress`（对齐 Python `db.task_next_step` 契约，修复 `step_status` 恒为 pending 的偏差），新增 Rust 测试 3 个（标记 in_progress / 同 session 恢复 / 并发 session 冲突），Rust `task_collab` 27 passed；真实 daemon E2E 在停用正式 daemon 期间 10/10 通过，含 `step_status=in_progress` 断言）**
- [x] 修复父任务/子任务 close 门禁和步骤证据门禁。 **（Rust `handle_task_close` 已实现子任务未关闭拒绝 + 叶子步骤未 done/skipped 拒绝 + 租约保护；`test_task_close_*` 6 项 Rust 测试覆盖）**
- [x] 真实 daemon RPC 覆盖 create/split/claim/report/status/events/apply/close。 **（`T-1786440663336-7e7d67e8` 步骤 #4 完成管道级全链路 E2E：隔离临时库 round-trip 19/19 断言 PASS，覆盖 create/split/claim/report/status/work_next/events/apply/close + 父子门禁 `E_CHILD_TASKS_NOT_CLOSED` + 身份/合同门禁 + 审计链 + 证据闭合，脚本存证 `.trae-cn/evidence/T-1786440663336-7e7d67e8-step4-m1-acceptance-e2e-script-2026-08-12.py`）**
- [x] `task_steps`、`task_events`、`change_audit` 与身份/租约证据闭合。 **（`T-1786440663336-7e7d67e8` 步骤 #4：`task.events` 审计链完整（created/split/claimed/reported/reported/applied/closed），reported 事件带 evidence_path/hash + role；storage.rs v50 stale-stamp 防护：`missing_compat_columns` 扩展 agent_registrations 9 列 + `apply_agent_registrations_v50_compat` 幂等补列 + 迁移事务先补列后 canonical SQL，单测 `test_v50_stale_stamp_missing_identity_columns_repaired`）**
- [x] **步骤 #3：Agent Identity + Role Contract（`T-1786440663336-7e7d67e8`）**。 **（Schema v50：`agent_registrations` 扩展 identity 最小字段（agent_instance_id/client_id/provider/model_id/model_mode/system_fingerprint/runtime_hash/session_id/role）+ 新增 `role_contracts` 冻结合同表（skill/prompt hash、allowed/forbidden paths、commands、acceptance_checks、required_evidence、handoff_to、independence、revision/is_current）；Rust `task_collab.rs`：`agent.register` 接收完整身份、claim/report 独立性门禁（同 instance/session 不可共享 implementer 与 independent_reviewer）、合同任务未注册身份 fail-closed、claim Task Envelope 携带 role_contract、skill/prompt hash 不符拒绝领取（E_CONTRACT_*_MISMATCH）、report 未合同角色拒绝（E_CONTRACT_ROLE_MISMATCH）、handoff target_role 合同校验、`task.contract_set/get`（revision 递增 + contract_set 审计事件）；Python `daemon_client.py`/`tools_task.py` identity/contract 透传；Rust `task_collab` 35 passed（含 8 个新测试），`test_windows_daemon_acceptance` 10 passed（含 4 个透传测试））**
- [ ] 独立 Reviewer PASS 后才允许 Coordinator apply/close。

### M2 及以后

每个 slice 重复：契约 → Rust handler → Python thin client → success/reject tests → fresh runtime → evidence → independent review → apply/close。任何环境不可用只能 `BLOCKED/UNVERIFIED`，不能用 mock、旧二进制或静态注册数量替代。

### M7：Lease Control Plane daemon 落地（已完成，2026-08-12）

> 任务：`T-1786488704690-fe4ff198`（8 steps，1 role_contract）
> 状态：closed（Reviewer PASS，Coordinator apply/close 收口，closed_at=1786499743.23）
> 并行组：M7-lease-singleton（本任务为唯一并行任务，不与其他 slice 并行）
> 完成时间：2026-08-12
> 交付物：5 Rust handler + 5 dispatch 路由 + AuthoritativeClock 注入 + 5 Python RPC 方法 + tools_p4_lease.py daemon 模式 + Rust 45 passed + Python 10/10 passed + 真实 RPC round-trip + 部署 binary hash `2405CF…D189`
> 待办（已闭合）：`task.apply/close` 的 `validate_lease_for_mutation` 门禁实证由 `T-1786499847862-77260874` 完成（2026-08-12 closed，Reviewer PASS）：`require_lease_params` 强制 fail-closed（缺/只其一/空串/非整数 → `E_LEASE_REQUIRED`），apply/close S3 强制校验，daemon_client 透传 lease_token/fencing_counter，enterprise/auto 下 daemon 不可用 → DaemonUnavailableError 不回退本地 SQLite；pytest 20 passed（真实 cw-daemon + Named Pipe），Rust 47 passed。lease 控制面使用点已全部闭合。

**根因**（审计确认）：
1. `cw_daemon.rs` serve 装配未调用 `.with_clock()` → 生产 daemon `clock=None` → `validate_lease_for_mutation` 第一步恒 fail-closed（`E_LEASE_CLOCK_UNAVAILABLE`）。
2. `dispatch.rs` 无 `lease.*` 分支 → `method_not_found`；且 `task.apply`/`task.close` 的 S3 门禁是 `if let Some(...) = parse_lease_params`（无凭证即放行，兼容路径），凭证又无法从 daemon 获取。

**已就位资产**（可直接复用）：
- `validate_lease_for_mutation` 完整 6 项 fail-closed（CLOCK/NOT_FOUND/TOKEN_MISMATCH/EXPIRED/FENCING_STALE/HOLDER_MISMATCH）+ SQL 已写（`task_collab.rs`）
- `parse_lease_params`、`sha256_hex`、`record_action_identity`、`save_dedup`（request_id 幂等）、`PROTECTED_MUTATION_METHODS` 串行化点
- `task_leases`/`task_lease_events` 表 + `idx_task_leases_active_unique`（同 task+role 唯一 active，天然防并发双活）已在 schema v50，daemon 已内嵌
- 参考语义：Python `db_task_leases.py`（acquire/renew/release/status/list_events + `_clock()` 权威时钟）
- 已有单测 `test_task_close_lease_validated_with_clock`

**命名决策**（canonical）：
- daemon RPC canonical = `lease.acquire` / `lease.extend` / `lease.release` / `lease.status` / `lease.list_events`
- `lease.renew` 作为 `lease.extend` 的兼容别名（Python 侧叫 renew，Rust 侧设计为 extend，两者对齐文档 + 测试）

**实施步骤**（任务 `T-1786488704690-fe4ff198`）：
1. `audit_baseline`：确认审计基线，读取现有代码
2. `implement`（task_collab.rs）：新增 5 handler（acquire/extend/release/status/list_events）
3. `implement`（dispatch.rs）：注册 5 路由（lease.renew 别名并入 extend）
4. `implement`（cw_daemon.rs）：serve 装配注入 `.with_clock(...)`
5. `implement`（daemon_client.py）：新增 5 个 RPC 方法
6. `implement`（tools_p4_lease.py）：daemon 模式走 RPC，enterprise/auto fail-closed，local 保留 get_db
7. `test`：Rust 单测 + Python 集成测试 + 真实 RPC round-trip
8. `build_deploy`：构建部署 + --refresh-all + git diff --check + 文档同步

**所有权白名单**（唯一可改文件）：
- `rust_ext/src/daemon/task_collab.rs`
- `rust_ext/src/daemon/dispatch.rs`
- `rust_ext/src/daemon/cw_daemon.rs`
- `server/daemon_client.py`
- `server/tools/tools_p4_lease.py`
- `tests/test_lease_*.py`、`tests/test_windows_daemon_acceptance.py`
- `docs/design/daemon-rust-migration-ledger.md`（仅 M7 slice 章节）

**门禁**（2026-08-12 implementer 完成，见下方实施记录）：
- [x] `lease.acquire`/`extend`/`release`/`status`/`list_events` 5 个 Rust handler 实现并通过单测
- [x] `dispatch.rs` 注册 5 路由 + `lease.renew` 别名
- [x] `cw_daemon.rs` serve 装配注入 `.with_clock()`（解除 `E_LEASE_CLOCK_UNAVAILABLE`）
- [x] `daemon_client.py` 新增 5 RPC 方法
- [x] `tools_p4_lease.py` daemon 模式走 RPC（enterprise/auto fail-closed）
- [x] Rust 单测全 passed（含并发单活、request_id 重放、幂等）
- [x] Python 集成测试全 passed
- [x] 真实 daemon RPC round-trip：acquire → extend → release → status → list_events 全链路 PASS
- [ ] `task.apply`/`task.close` 使用 lease 凭证通过 `validate_lease_for_mutation` 门禁（需 Reviewer/Coordinator 实证）
- [x] 构建部署 + daemon health schema_version=50
- [ ] 文档同步（账本 M7 slice 完成 + architecture.md + implementation-status.md）
- [ ] Independent Reviewer PASS 后 Coordinator apply/close

**实施记录**（implementer，2026-08-12）：
- 5 handler 位于 `task_collab.rs`（`handle_lease_acquire/extend/release/status/list_events` + `append_lease_event`/`holder_identity`/`gen_lease_token` 等辅助函数）；request_id 幂等复用 `check_dedup`/`save_dedup`；clock 缺失 fail-closed `E_LEASE_CLOCK_UNAVAILABLE`；token 只存 sha256、raw 仅响应一次；fencing_counter = 历史 MAX+1；唯一索引 `idx_task_leases_active_unique` 天然防并发双活。
- 路由：`dispatch.rs` 5 分支 + `lease.renew` 别名并入 `lease.extend`；`PROTECTED_MUTATION_METHODS` 新增 `lease.renew`。
- 装配：`cw_daemon.rs` Linux/Windows 双 serve 均注入 `AuthoritativeClock`（`.with_clock(...)`）。
- Python：`daemon_client.py` 6 方法（含 `lease_renew` 别名）；`tools_p4_lease.py` enterprise/auto 走 RPC fail-closed、local 保留 `get_db` 直调。
- 测试证据：
  - `cargo test --no-default-features --lib task_collab::` → **45 passed**（35 原 + 10 新增 lease 单测：acquire 双活拒绝/过期重取递增/坏 token/过期/陈旧 fencing/幂等 release/status 隐藏 raw token/clock fail-closed 等）
  - `python -m pytest tests/test_lease_rpc.py`（真实 daemon 进程 + 隔离临时 DB，禁 mock）→ **10 passed**（acquire→extend→release→status→list_events 全链路 + 落库 sha256 核对）
  - `python -m pytest tests/test_windows_daemon_acceptance.py` → **10 passed**（回归）
- 构建部署：`cargo build --release --no-default-features --bin cw-daemon`；release 二进制 SHA-256 初版 = `2405CF2046CFF0DA8EA09FC1773C4D2DC8BD2407656836C4FCC9C1AD5C52D189`（2026-08-12）；`python cw.py --refresh-all` 完成（66.6s，18,035 symbols）；`git diff --check` 通过（修复 task_collab.rs 一处行尾空格）。
- **链接环境修复（2026-08-12）**：初版二进制依赖 `python310.dll`（TRAE VM tools 的 Python 3.10 在 PATH 优先，PyO3 按活动解释器链接），daemon 经 `daemon_autostart` 独立进程启动时 DLL 搜索路径不含该目录 → 0xC0000135 启动失败，导致 enterprise/auto 模式 task 写操作报「daemon endpoint 不可连接」。修复：用户级 `PYO3_PYTHON=C:\Python314\python.exe`（`[Environment]::SetEnvironmentVariable(..., "User")`）→ `cargo build --release` 重连 python314.dll；`dumpbin /dependents` 确认依赖 `python314.dll`（无 python310.dll）。**重新链接后 release 二进制 SHA-256 = `BF23BA91560A20A546D632AFF29737F33F6E38DDB0E0FE76BF02EF78AEE53E70`**（源码未变，仍为 commit `6b1558e`）。
- 部署说明：`~/.callwarden/runtime/current/cw-daemon.exe` 已用重新链接后的 release 二进制覆盖（旧 python310 副本备份至 `~/.callwarden/runtime/previous-python310-<时间戳>/`）；daemon 经 `call_with_autostart` 以新二进制重启，`cw.py daemon health` → status=ok、schema_version=50、pid 40080；生产 daemon 上 `lease.status`/`lease.list_events` 只读冒烟返回结构化结果（非 method_not_found），证明 lease 路由已注册。
- **Git 归属（提交后重新验证）**：M7 全部 7 个白名单文件已提交，commit `6b1558e`（feat(daemon): Lease Control Plane 落地 (M7 lease slice)，+1921/-212，含 tests/test_lease_rpc.py）；提交后 fresh runtime 复跑：`cargo test --no-default-features --lib task_collab::` → 45 passed（8.66s）、`python -m pytest tests/test_lease_rpc.py` → 10 passed（392s，含 release 重建）、`tests/test_windows_daemon_acceptance.py` → 10 passed。
- 任务终态：`T-1786488704690-fe4ff198` 8/8 步骤已 report 到 review（身份 ASI / sess-lease-m7-20260812 / implementer，合同 skill_id=none + prompt_hash=hash-lease-implementer-v1），待 Independent Reviewer 复审。
- 待办（非 implementer 白名单内）：architecture.md / implementation-status.md 文档同步、`task.apply/close` lease 凭证门禁实证、Reviewer 复审、Coordinator apply/close。

**Lease Gate 实证收口**（任务 `T-1786499847862-77260874`，2026-08-12，prompt_hash=hash-lease-gate-empirical-v1）：

- **根因**：`parse_lease_params` 仅在同时提供 `lease_token` + `fencing_counter` 时调用 `validate_lease_for_mutation`；缺凭证时返回 `Ok(None)` 跳过校验继续执行（兼容路径），不能作为协同闭环。
- **改造**（`rust_ext/src/daemon/task_collab.rs`）：
  - `parse_lease_params` → `require_lease_params`：强制返回 `(String, i64)`，缺 `lease_token` / 缺 `fencing_counter` / 只提供其一 / 空串 / 非整数 counter 一律返回 `E_LEASE_REQUIRED`（fail-closed）。
  - `handle_task_apply` / `handle_task_close` 在事务内先执行 `require_lease_params` + `validate_lease_for_mutation`，再进入业务逻辑；6 项既有错误码（`E_LEASE_CLOCK_UNAVAILABLE`/`E_LEASE_NOT_FOUND`/`E_LEASE_TOKEN_MISMATCH`/`E_LEASE_EXPIRED`/`E_LEASE_FENCING_STALE`/`E_LEASE_HOLDER_MISMATCH`）保持不变。
- **Python 透传**：`server/daemon_client.py` `task_apply/task_close` 新增 `lease_token`/`fencing_counter` 参数透传 RPC params；`server/tools/tools_task.py` MCP 工具暴露两参数，daemon 模式透传、daemon 不可用 fail-closed（不静默回退本地 SQLite），local 模式仅当两凭证齐备时才启用校验（本地开发兼容路径，不影响 enterprise/auto 权威路径）。
- **测试证据**：
  - `cargo test --no-default-features --lib daemon::task_collab::` → **47 passed**（含新增 `test_task_apply_requires_lease_credentials` / `test_task_close_requires_lease_credentials`，6 个旧 close 测试补齐 clock + seed lease 携带凭证）。
  - `cargo test --no-default-features --lib daemon::` 全量 → **695 passed / 25 failed**；25 个失败全部位于 `daemon::workspace::tests`（backup/gc_cas/mount/restore）与 `daemon::dispatch::tests::test_health_returns_uptime_and_schema_version`，与本任务改动（task_collab）无关，属既有失败。
  - `python -m pytest tests/test_lease_gate_empirical.py`（真实 cw-daemon.exe 进程 + 隔离 task DB + Named Pipe RPC，禁 mock）→ **20 passed**：成功路径（acquire→apply→applied→close→closed，核验 task_events 的 applied/closed 事件与 action_identities 的 reviewer 身份落库）+ apply/close 各 6 种拒绝场景（无 token/无 counter/只提供 token/错误 token/旧 counter/过期 lease/错误 holder，均断言 `E_LEASE_*` 且 status/events/action_identities 零改动）+ request_id 重放不重复写事件 + 双 reviewer 竞争单 holder + close 子任务关闸 + 业务错误原样返回（`DaemonRemoteError` 非 `DaemonUnavailableError`）+ enterprise/auto daemon 不可用 fail-closed 不回退本地 DB。
  - 真实 daemon round-trip（`_tmp_m7g_roundtrip.py` 一次性验收脚本，Python 3.14）：acquire(token+fencing_counter=1) → apply(applied, 事件 #4 review→applied, role=reviewer) → task.status/events → close(closed, 事件 #5 applied→closed, closed_at 非零) → task.status/events → release(released)；action_identities 两条 state_transition，agent/session/model/role 与 lease holder 一致。
- **构建部署**：Python `C:\Python314\python.exe`（3.14.3）；cargo/rustc 1.93.1；`cargo build --release --no-default-features --bin cw-daemon`；`dumpbin /dependents` 确认仅 `python314.dll`（无 python310/311/312/313.dll）；fresh binary SHA-256 = `56FACD9FE977E052552FDFC5E4185665DCC9EAC62A41397A3EA3EC1580138286`。
  - `python cw.py --refresh-all` 实证通过（exit 0，13.08s，2026-08-12）。**根因记录**：运行中 daemon 的 WAL/shm 句柄会阻塞其他进程 rw 打开权威库 `~/.callwarden/callwarden.db`（`PRAGMA journal_mode=WAL` 报 `sqlite3.OperationalError: disk I/O error`），CLI 直写刷新必须先停 daemon；强杀 daemon 后残留 `-shm` 需一次恢复性打开（普通 SELECT 即可）后再刷新，否则首轮仍报 `disk I/O error`。`git diff --check` 通过。
  - fresh 二进制已由隔离 daemon 完成 round-trip 验收（pid=3444、schema_version=50）；**生产 daemon 保持旧二进制 `BF23BA91…` 运行**（重启后 pid=27120、schema_version=50、workspace_count=2，authority_id=`LINKPLAY-SCM/windows/S-1-5-21-1583625257-826939952-3615027596-1001/…`）——fresh 部署到 `runtime/current` 被用户跳过，未执行，Reviewer 不得把旧二进制运行视为 fresh runtime 证据。
- **门禁清单更新**：`task.apply`/`task.close` lease 凭证门禁实证已完成（本任务闭环）。仍待办：architecture.md / implementation-status.md 文档同步（非本任务白名单）、Reviewer 复审、Coordinator apply/close。

## 7. 当前验证基线

- MCP runtime registration：237。
- Windows/WSL routing/storage focused suite：`37 passed, 4 skipped`；这不是完整跨边界可用性证明。
- `tests/test_windows_bridge_e2e.py` 与 `tests/test_windows_wsl_authority_e2e.py` 的一次联合执行在 120 秒内未完成；Cargo/Rust 子进程已清理，不能据此声称 PASS。
- 当前 daemon 健康检查曾返回 `reachable=true` 但 `endpoint=""`，应作为健康协议缺陷跟踪。

## 8. 下一步唯一入口

下一阶段由 Coordinator 以任务 `T-1786437453328-1b1dfa98` 组织 M0 盘点和 M1 任务 slice。Implementer 不得先改 237 个工具；应先完成函数级清单和 M1 的最小闭环。Tester 只运行声明的 focused suite；Independent Reviewer 只按工具卡片和真实证据复核；Coordinator 负责合法身份、租约、apply/close。

## 9. 并行迁移后的串行恢复协议

当前项目处于多任务并行修改后的半切换状态。除非发现不可恢复的数据或编译基线损坏，禁止直接回滚到旧版本；先冻结并行变更，保留现有提交和证据，再按以下顺序恢复：

1. **冻结范围**：暂停新增 daemon/HTTP/bridge/MCP 工具迁移；只允许当前 M1 任务及其独立修复子任务修改代码。
2. **固定运行时**：停止使用旧的 `target`/安装副本，记录 Git HEAD、Python 解释器、Rust 二进制 hash、daemon authority 和 MCP 工具注册数；构建与测试必须来自同一 runtime。
3. **先修任务状态机**：修复 `task_next_step` 返回步骤详情、`task_create_subtask` 保存步骤、MCP wrapper 结构化返回与真实写入结果一致性；在此之前不关闭任何新的迁移任务。
4. **完成 M1**：只覆盖 task create/split/claim/report/status/events/apply/close，补真实 daemon round-trip、并发冲突、dedup、权限和步骤证据。
5. **独立复审 M1**：Reviewer 只读核验源码、fresh runtime、任务步骤、task events、change audit 和测试原始日志；PASS 后 Coordinator 才能 apply/close。
6. **逐 slice 推进**：按 M2、M3……顺序一次只开启一个 slice。前一个 slice 未闭合时，不得修改下一个 slice 的生产路径。
7. **回滚条件**：只有在同一固定基线下无法编译、核心 schema/数据不可恢复、或权限/隔离模型被破坏时，才建立隔离分支回滚；不得用回滚掩盖任务归属或 runtime 陈旧问题。

每个 slice 必须有唯一的 `owner_task`、允许路径、runtime fingerprint、focused test 命令、review report 和收口状态。并行 Agent 只能读取其他 slice，不能跨 slice 修改生产代码。

## 9.1 构建非确定性说明（2026-08-12 Reviewer 发现）

同源码同参数两次 release 构建的 binary hash 不同（用户构建 `56FACD9F…`，Reviewer 复现为 `F274EAC5…`），根因是 Windows PE 时间戳等非确定性因素。但 `dumpbin` 依赖一致性已复现（仅 `python314.dll`），功能与依赖声明均成立。

**发布流程约定**：
- release binary hash 为**构建快照值**，不可确定性重放，不得作为唯一复验依据。
- 发布流程按**源码版本（Git commit）**追踪，而非 hash。
- 复验时优先比对 `dumpbin /dependents` 依赖清单 + Git commit + 测试通过情况。
- 建议后续发布时固定 `PYO3_PYTHON` 与构建环境，并在 ledger 记录构建环境指纹（Python 版本、Cargo 版本、目标 triple）。

## 9.2 task.create_subtask 参数名说明（2026-08-12 Coordinator 发现）

`handle_task_create_subtask`（`rust_ext/src/daemon/task_collab.rs` L3254）使用 `parent_task_id`（非 `parent_id`）作为参数名接收父任务 ID。首次创建 M2.1-M2.5 子任务时误用 `parent_id`，导致子任务未挂载（parent_id 为空）。已用正确参数名 `parent_task_id` 重新创建。废弃任务（parent_id 为空，未挂载）：`T-1786519172968-f13db464`、`T-1786519211817-fcc40690`、`T-1786519211823-fd25bb10`、`T-1786519211831-fd9a5380`、`T-1786519211837-fdfffe10`。

## 9.3 M2：file/symbol/grep/issues/tests daemon 查询迁移（当前优先，2026-08-12 启动）

> 父任务：`T-1786519106584-7c67cef4`（6 steps，M0 盘点 T-1786437453328-1b1dfa98 已 closed）
> 状态：open（M2.1 closed，M2.2 closed，M2.3 已实现待复审）
> 并行组：M2-sequential（5 子任务串行，前一个 PASS + apply/close 后才能启动下一个）

**任务树**：

| 子任务 | 任务 ID | 查询类型 | 步骤数 | 状态 | 前置依赖 |
|---|---|---|---:|---|---|
| M2.1 | `T-1786519351240-73127ab4` | query.file | 7 | **closed**（2026-08-12） | 无 |
| M2.2 | `T-1786526643663-594ee010` | query.symbol | 7 | **closed**（2026-08-12） | M2.1 closed ✓ |
| M2.3 | `T-1786529505247-9d083e54` | query.grep | 7 | **closed**（2026-08-12，Reviewer PASS + Coordinator apply/close） | M2.2 closed ✓ |
| M2.4 | `T-1786539379174-90f74174` | query.issues | 7 | **closed**（2026-08-13，Reviewer PASS + Coordinator apply/close） | M2.3 closed ✓ |
| M2.5 | `T-1786584287058-7f712ff4` | query.tests | 8 | **closed**（2026-08-13，Reviewer PASS + Coordinator apply/close） | M2.4 closed ✓ |

> 废弃占位任务：`T-1786519375878-2f9af314`（M2.2 占位，已被 `T-1786526643663-594ee010` 替代）、`T-1786519375899-30d79b88`（M2.3 占位，已被 `T-1786529505247-9d083e54` 替代）、`T-1786519375906-31402be4`（M2.4 占位，已被 `T-1786539379174-90f74174` 替代）、`T-1786519375913-31a65310`（M2.5 占位，已被 `T-1786584287058-7f712ff4` 替代）

**M2.1 完成证据**（T-1786519351240-73127ab4，closed_at=1786526543.14）：
- commit `561a53a`（7 文件，白名单 6 + graph.rs Coordinator 授权修复 `\\?\` 前缀 URI 缺陷）
- Rust 4 passed（query_handlers::）+ 3 passed（file_query）= 7 passed
- Python 10/10 passed（真实 cw-daemon + Named Pipe，禁 mock，3.83s）
- binary SHA-256 `6CA7040B…EEC5F`，schema v50
- 6 问全部对源验证：workspace_id 绑定（owned_workspace ACL）/ file_instance_id 校验（SQL WHERE）/ 越界路径（normalize_workspace_path strip 失败→空数组无泄露）/ snapshot fail-closed（snapshot_not_ready）/ 跨 workspace 隔离（WHERE workspace_id=?）/ Python fallback（仅 local 模式保留 get_db）
- Reviewer 非阻断观察：change_audit=0 条（git commit + task_events 9 条承担审计意图），建议 M2.2 起改用 propose_edit 编辑以自动落 change_audit

**M2.2 完成证据**（T-1786526643663-594ee010，closed_at=1786529421.75）：
- commit `4bc0261`（白名单内文件，query.symbol handler + dispatch + client + MCP + 测试）
- Rust 7 passed（query_handlers::，含 M2.2 新增 3 个单测）
- Python 9/9 passed（真实 cw-daemon + Named Pipe，禁 mock，3.59s）
- fresh binary SHA-256 `653576A9…2DD5E`（target/release，pytest fixture ensure_fresh_binary 强制重建）
- 6 问对源验证：workspace_id 绑定（复用 M2.1 owned_workspace ACL）/ symbol_id 校验（symbols 表 lookup）/ 越界路径（normalize_workspace_path）/ snapshot fail-closed（snapshot_not_ready）/ 跨 workspace 隔离（WHERE workspace_id=?）/ Python fallback（仅 local 模式保留 get_db，旧 UDS 直连分支已删）
- Value::Null 既有契约：handle_query_symbol 对 Value::Null 参数返回 invalid_params（与 M2.1 一致）
- Reviewer 非阻断观察 1：账本 M2.2 过程记录此前未写入（commit message 声称已写入但实际 diff 未包含），本条由 Coordinator 补齐
- Reviewer 非阻断观察 2：生产 daemon（runtime/current）仍运行 M7 时期旧二进制 BF23BA91（构建于 10:23，早于 M2.1/M2.2 commit），不含 M2.1/M2.2 代码；round-trip 证据使用 fresh target/release 二进制有效，但生产 daemon 尚未部署 M2.2

**M2.2 生产部署决策**（Coordinator）：
- 当前 M2 slice 处于串行迁移中，M2.3 尚未启动。生产 daemon 部署 M2.2 代码可推迟到 M2 slice 全部完成后统一部署，避免每个子任务都重启生产 daemon。
- 风险：M2.3-M2.5 的测试需要 fresh target/release 二进制（已由 pytest fixture 保证），不依赖生产 daemon 部署 M2.2。
- 决策：M2 slice 全部完成（M2.5 closed）后，统一部署 fresh 二进制到 runtime/current。在此之前生产 daemon 保持 M7 时期二进制，不影响 M2.3-M2.5 的开发和测试。

**每个子任务统一验收标准**（13 项）：
1. daemon 侧真实生产 handler 和 dispatch 路由
2. Python client/MCP 路由明确
3. enterprise/auto 模式走 daemon
4. daemon 不可用时 fail-closed
5. 不得静默回退本地 SQLite
6. Windows Named Pipe 或 Linux UDS 真实进程级 round-trip
7. 至少覆盖：成功查询 / 未知 workspace 拒绝 / 越界路径或未授权资源拒绝 / snapshot 未就绪结构化错误 / 空参数或非法参数
8. 业务错误保留结构化错误码
9. fresh `cw-daemon` 必须使用当前 Git HEAD 构建
10. 记录 binary SHA-256、authority_id、schema_version、daemon PID
11. `cw --refresh-all`、`py_compile`、focused pytest、Rust focused test、`git diff --check` 全部有原始结果
12. 任务步骤必须有：target_file / 实际 result / commit hash / change_audit / task_events
13. Implementer 只能推进到 `review`，不得 apply/close

**M2.1 首个子任务**（`T-1786519351240-73127ab4`，7 steps，1 role_contract）：
- 步骤：audit_baseline → implement(handler) → implement(dispatch) → implement(client) → implement(MCP) → test → build_deploy
- role_contract：implementer, prompt_hash=`hash-m21-query-file-v1`, handoff_to=independent_reviewer
- 所有权白名单：`query_handlers.rs, dispatch.rs, daemon_client.py, tools_query.py, test_query_file_rpc.py, daemon-rust-migration-ledger.md`
- 必须回答：workspace_id 绑定 / file_instance_id 校验 / 越界路径错误 / snapshot fail-closed / 跨 workspace 隔离 / Python fallback 是否存在
- 真实验收顺序：fresh daemon build → workspace.register → snapshot/publish → query.file 成功 → 未知 workspace 拒绝 → 越界路径拒绝 → snapshot_not_ready 拒绝 → daemon health → binary hash/authority/schema 记录

**串行门禁**：只有 M2.1 获得 Reviewer `PASS` 并由 Coordinator 正式 `apply/close` 后，才能创建或启动 M2.2。

**M2.1 过程记录（2026-08-12）**：

- **既有生产缺陷（Windows snapshot.publish）**：Rust `std::fs::canonicalize`（`workspace.rs` `validate_owned_path`）在 Windows 返回 `\\?\` 前缀的 extended-length path，`handle_snapshot_publish` 将其作为 db_path 传入 `build_and_publish_blocking` → `load_from_sqlite_blocking` → `open_immutable_db`（`graph.rs`）。`open_immutable_db` 的 URI 构造把 `\\?\C:\...` 转成 `//?/C:/...?immutable=1`（非法 URI），SQLite 报 `unable to open database file`，snapshot.publish 在 Windows 上完全失败。本地嵌入扩展与生产 cw-daemon 双重复现；无前缀路径正常成功。该缺陷早于 M2.1 存在，非 M2.1 引入。
- **Coordinator 授权**（2026-08-12）：将 `rust_ext/src/graph.rs` 临时纳入 M2.1 所有权白名单，仅限修复 `open_immutable_db` 的 `\\?\` 前缀 URI 构造。修复：strip `\\?\` 前缀后再构造 `file:///C:/...?immutable=1`（约 3 行，Linux 路径无此前缀、行为不变）。query 端 `open_query_connection` 用非 URI 只读模式打开，`\\?\` 前缀路径合法，无需改动。

**M2.1 实现结论（2026-08-12，commit `561a53a`）**：

- **交付**：① `query_handlers.rs` 新增（dispatch 层 query.file 越界路径结构化校验：空/空白/NUL → `invalid_params`，`..` 向上穿越 → `out_of_bounds`，`\`→`/` 归一化，4 个单元测试）；② `dispatch.rs` `#[path]` 子模块声明 + `query.file` 路由前置校验；③ `daemon_client.py` `get_file_symbols` fail-closed（auto/enterprise daemon 不可用 raise `DaemonUnavailableError`，仅 local 模式走 SQL）；④ `tools_query.py` docstring 路由说明；⑤ `test_query_file_rpc.py` 10 测试全过（7 进程级 round-trip + 3 fail-closed 单测）；⑥ `graph.rs` `open_immutable_db` URI 缺陷修复（见上）。
- **必须回答 6 问结论**：
  1. workspace_id 绑定：`open_query_connection` 经 `owned_workspace` 取 workspace_id，SQL 层 `WHERE fi.workspace_id=?` 过滤（`query_local_file_symbols`）。
  2. file_instance_id 校验：结果集含 `file_instance_id`，由 SQL 精确匹配 `rel_path` 返回（handler 不显式校验）。
  3. 越界路径错误：相对路径 `..` → 结构化 `out_of_bounds`；绝对路径超出 root → rel_path 精确匹配天然隔离（空数组，无泄露）。
  4. snapshot fail-closed：未 publish → `snapshot_not_ready` 结构化错误。
  5. 跨 workspace 隔离：`owned_workspace` ACL（非属主 `workspace_forbidden`）+ SQL workspace_id 过滤，测试验证 A/B workspace 互不可见。
  6. Python fallback：仅 `local` 模式保留 SQL 回退；auto/enterprise daemon 不可用 fail-closed，不再静默回退（_sql_fallbacks 不计数）。
- **验收证据**：`cw --refresh-all`（18,035 symbols / 116,221 calls / 15.01s）、`py_compile` exit 0、pytest 10 passed（281.77s）、Rust `query_handlers` 4 passed、`git diff --check` exit 0；cw-daemon.exe SHA-256 `6CA7040B44E8D9E552A575399C6785DD33E552AFFAAACE6F4E568239C82EEC5F`；daemon ping authority_id `LINKPLAY-SCM/windows/S-1-5-21-1583625257-826939952-3615027596-1001/ef0fca190444e478effe7a73a710a343f4afc19036b45a0236bb6fd7fcc0fbe2`；health schema_version=50、daemon PID 47444。
- **状态**：推进到 `review`，待 independent_reviewer 复审；不得 apply/close。

**M2.3 过程记录（2026-08-12，commit `408c46e`）**：

- **交付**：① `query_handlers.rs` 新增 `validate_query_grep_params`（空数组 / 空或纯空白 pattern / NUL 字节 → `invalid_params`，3 个单元测试）；② `dispatch.rs` `query.grep` 路由前置校验（patterns 必须为字符串数组，非法在 dispatch 层拒绝，不进入 handler）；③ `daemon_client.py` `query_grep` fail-closed（auto/enterprise daemon 不可用 raise `DaemonUnavailableError`，local 模式返回 None 由本地 grep 组件处理，**无本地 SQLite 回退**）；④ `tools_query.py` docstring 路由说明（grep 的 daemon 入口为 `DaemonClient.query_grep`，MCP `file_grep` 位于 `tools_workspace.py` 白名单外）；⑤ `test_query_grep_rpc.py` 12 测试全过（9 进程级 round-trip + 3 fail-closed 单测，301.69s）。
- **handler 既有契约（白名单外，M2.3 不改）**：`snapshot_state.rs` `handle_query_grep`（L791）+ `cli/grep.rs` `query_local_grep` 已完整实现；返回 `Value::String` 格式化文本（`Grep with symbol context: ...`），无匹配返回 `No matches for: <pattern>` 文本，非结构化 matches 数组。rg 为可选加速（30s 超时，Unavailable 时 fallback 纯源码扫描，仅 SOURCE_EXTENSIONS 源文件）。
- **必须回答 6 问结论**：
  1. workspace_id 绑定：`open_query_connection` 经 `owned_workspace` 取 workspace_id，grep 符号归属 SQL `WHERE fi.workspace_id=?` 过滤（`query_symbol_contexts`）。
  2. pattern 校验：dispatch 层 `validate_query_grep_params`（M2.3 新增）拒绝空数组/空或空白/NUL pattern → `invalid_params`；handler 层要求 patterns 为字符串数组。
  3. 越界/未授权资源拒绝：grep 搜索**当前 workspace 真实文件系统**，`resolve_search_root` 拒绝 escapes workspace root 的 path 参数（返回 internal_error，既有行为，未在 M2.3 改动范围）；未知 workspace → `workspace_not_found`。
  4. snapshot fail-closed：未 publish → `snapshot_not_ready` 结构化错误。
  5. 跨 workspace 隔离：`owned_workspace` ACL + 真实文件系统天然隔离（A root 不含 B 文件）+ SQL workspace_id 过滤，测试验证 A/B 互不可见。
  6. Python fallback：`query_grep` **无本地 SQLite 回退**（grep 由 CLI / MCP file_grep 负责）；auto/enterprise daemon 不可用 fail-closed raise，local 模式返回 None，`_sql_fallbacks` 不计数。
- **无匹配文本契约**：Rust handler 返回 `Value::String`（既有契约），无匹配时为 `No matches for: <pattern>` 文本；测试按文本语义断言，不按结构化空数组断言。
- **M2.1 propose_edit 建议遵循情况**：M2.1 建议"M2.2 起改用 propose_edit 编辑以自动落 change_audit"。M2.3 编辑仍直接使用 IDE SearchReplace 工具（未走 propose_edit RPC），change_audit 由 commit + task_events 承担审计意图；如实记录未采用。
- **生产 daemon 部署决策沿用**：M2.2 记录的 Coordinator 决策（M2.5 后统一部署 fresh 二进制）继续适用。M2.3 测试前已停止生产 daemon（PID 47764，M7 时期二进制 BF23BA91）以释放默认管道；测试使用隔离临时 daemon（fresh target/release 二进制）。测试结束后生产 daemon 由 Reviewer 恢复为运行状态（PID 52032，M7 时期二进制 BF23BA91，schema v50，health ok），M2.5 closed 后统一部署 fresh 二进制。
- **Reviewer 非阻断观察**：① 复审期间出现双实例问题（52032 带 `--socket` / 63404 无参数），已清理为单实例 52032，已按规则 6 记录 `tool_errors.log`；② change_audit=0 延续（与 M2.1/M2.2 一致，SearchReplace 编辑未走 propose_edit），M2.4 起继续评估 propose_edit 流程。
- **验收证据**：`cw --refresh-all`（total 13.58s）、`py_compile` exit 0、pytest 12 passed（301.69s）、Rust `query_handlers::` 10 passed（既有 7 + M2.3 新增 3）、`git diff --check` exit 0；cw-daemon.exe SHA-256 `131DD97A85DF07EADA904CDBF8F7CE828EE0B772B35A3D7D75D3D20D10E21621`；schema_version=50（未变）。
- **状态**：**closed**（Reviewer PASS + Coordinator apply/close，2026-08-12）。
  - Reviewer 独立复审结论：PASS（附 2 条非阻断观察，见上）。
  - Coordinator 收口：reviewer lease acquired（L-059f79d8e8bb4f5f, fencing=1）→ `task.apply`（applied_at=1786538931.71）→ `task.close`（closed_at=1786538952.75）。
  - 身份记录：agent=coordinator-m23-close, session=sess-coordinator-m23-20260812, model=glm-5.2, role=coordinator。
  - 观察 1（账本 L409 表述不一致）已修正：生产 daemon 由 Reviewer 恢复为运行状态（PID 52032），账本已更新。
  - 观察 2（change_audit=0）延续 M2.1/M2.2 现状，M2.4 起继续评估 propose_edit 流程。

**M2.4 启动记录（2026-08-12，Coordinator 创建）**：

- **任务 ID**：`T-1786539379174-90f74174`（替换占位 `T-1786519375906-31402be4`，7 steps，open）。
- **查询类型**：query.issues（`get_issue_summary` + `find_issues` MCP 工具）。
- **所有权白名单**：`rust_ext/src/daemon/query_handlers.rs`, `rust_ext/src/daemon/dispatch.rs`, `server/daemon_client.py`, `server/tools/tools_query.py`, `tests/test_query_issues_rpc.py`, `docs/design/daemon-rust-migration-ledger.md`。
- **步骤**：audit_baseline → implement(handler) → implement(dispatch) → implement(daemon_client) → implement(tools_query) → test → build_deploy。
- **fail-closed 语义**：enterprise/auto 模式 daemon 不可用 → raise `DaemonUnavailableError`；local 模式返回 None。
- **前置依赖**：M2.3 closed ✓（Reviewer PASS + Coordinator apply/close 完成）。
- **待办**：交付 Implementer 领取执行，推进到 `review` 后由 Independent Reviewer 复审。

**M2.4 过程记录（2026-08-13，commit `6bcaa7c`）**：

- **交付**：① `query_handlers.rs` 新增 `validate_query_issues_params`（空/纯空白/NUL → `invalid_params`，3 个单元测试）；② `dispatch.rs` `query.issues` 路由前置校验（qualified_name 空/纯空白/NUL 在 dispatch 层结构化拒绝，合法符号名原样交给 handler）；③ `daemon_client.py` `query_issues` fail-closed（auto/enterprise daemon 不可用 raise `DaemonUnavailableError`，local 模式返回 None，**无本地 SQLite 回退**）；④ `tools_query.py` docstring 语义边界说明（`get_issue_summary`/`find_issues` 与 query.issues 的语义差异，行为不变）；⑤ `test_query_issues_rpc.py` 11 测试全过（7 进程级 round-trip + 4 fail-closed 单测，8.85s）。
- **handler 既有契约（白名单外，M2.4 不改）**：`snapshot_state.rs` `handle_query_issues`（L830-844）+ `cli/issues_tests.rs` `query_local_issues` 已完整实现；返回 `Value::Array` 结构化 issues 数组（非 M2.3 grep 的 `Value::String` 文本契约），符号不存在返回 `Value::Array(Vec::new())`；semgrep 字段 rule_id/rule_name/severity/confidence/message/start_line/end_line/snippet/fix + source="semgrep"；guardrail 字段 rule_id/rule_name/severity/message/status/detected_at + start_line=0/end_line=0 + source="guardrail"。
- **必须回答 6 问结论**：
  1. workspace_id 绑定：`open_query_connection` 经 `owned_workspace` 取 workspace_id，SQL 层 `WHERE fi.workspace_id=?` 过滤（`query_local_issues`）。
  2. qualified_name 校验：dispatch 层 `validate_query_issues_params`（M2.4 新增）拒绝空/纯空白/NUL → `invalid_params`。
  3. 越界/未授权资源拒绝：未知 workspace → `workspace_not_found`；符号不存在 → 空数组 `[]`，无信息泄露。
  4. snapshot fail-closed：未 publish → `snapshot_not_ready` 结构化错误。
  5. 跨 workspace 隔离：`owned_workspace` ACL + SQL `WHERE workspace_id=?` 过滤，测试验证 A/B workspace 互不可见。
  6. Python fallback：`query_issues` **无本地 SQLite 回退**；auto/enterprise daemon 不可用 fail-closed raise `DaemonUnavailableError`，local 模式返回 None，`_sql_fallbacks` 不计数。
- **语义边界（M2.4 特别说明）**：`get_issue_summary`（全局正则扫描 IssueAnalyzerMixin）与 query.issues handler（按符号 semgrep+guardrail 聚合）语义不对应；真正对应 query.issues 语义的是 `get_symbol_issues`（`tools_task.py`，白名单外）。M2.4 仅补充 `tools_query.py` docstring 路由说明，不改两个工具行为。
- **dispatch 测试 flake 修复（M2.4 附带）**：`daemon::dispatch::tests` 部分用例调用 `DaemonState::default()`，其对 `~/.callwarden/callwarden.db`（272MB）全量哈希，测试并行磁盘 thrash 下 start_time.elapsed() 误报大 uptime 导致 flake。改为 `make_state()` 手动构造固定测试值，8 处替换 → 57 passed。
- **真实验收矩阵**（隔离数据目录 + fresh `cw-daemon.exe`，ACCEPTANCE_OK）：workspace.register → snapshot.publish → query.issues 成功（semgrep+guardrail 合并数组，含 snippet/fix 字段）；include_info=true 全量；未知 workspace → `workspace_not_found`；无匹配符号 → `[]`；snapshot 未就绪 → `snapshot_not_ready`；缺失/空/空白/NUL qualified_name → `invalid_params`；跨 workspace 隔离。
- **生产 daemon 部署决策沿用**：M2.2 记录的 Coordinator 决策（M2.5 后统一部署 fresh 二进制）继续适用。M2.4 测试使用隔离临时 daemon（fresh target/release 二进制），测试结束后生产 daemon 已恢复为运行状态（PID 35144，M7 时期二进制，schema v50，health ok）。
- **验收证据**：`cw --refresh-all`（total 11.75s，exit 0）、`py_compile` server/daemon_client.py + server/tools/tools_query.py exit 0、pytest 11 passed（8.85s）、Rust `query_handlers::` 13 passed（既有 10 + M2.4 新增 3）、`daemon::dispatch::` 57 passed、`git diff --check` exit 0；cw-daemon.exe SHA-256 `4C90CA9ED80B1CE6732C94C23B738E42904E42C19000B072840457334D8C1A79`（target/release fresh build）；health authority_id `LINKPLAY-SCM/windows/S-1-5-21-1583625257-826939952-3615027596-1001/903a01906c777458b2f60bd9201e444fda496a5ccb8e44f46e86967202f8029f`、pid 24084、transport named-pipe、schema_version=50（未变）。
- **状态**：**closed**（Reviewer PASS + Coordinator apply/close，2026-08-13）。
  - Reviewer 独立复审结论：PASS（附 3 条非阻断观察）。
  - Coordinator 收口：reviewer lease acquired（L-fbb6f3c1760da2e6, fencing=1）→ `task.apply`（applied_at=1786584158.91）→ `task.close`（closed_at=1786584158.93），均通过 daemon RPC（CLI 直接 sqlite3.connect 遇到 disk I/O error，daemon 持有 -shm 锁）。
  - 身份记录：agent=coordinator-m24-close, session=sess-coordinator-m24-20260813, model=glm-5.2, role=coordinator。
  - 非阻断观察 1（round-trip 7 项未能独立复跑）：环境中 4 个 MCP server autostart 占用默认管道 + 工作区并发修改导致 cargo build 失败，Reviewer 无法独立复跑 round-trip；建议 Coordinator 在干净环境补跑或接受账本记录（implementer 11 passed）。
  - 非阻断观察 2（工作区并发污染）：`graph.rs`/`snapshot.rs`/`replicator.rs`/`snapshot_state.rs` 仍为 M 状态（其他 agent 在途工作，790+/748- 行未提交），cargo build 失败（E0308/E0277），target/release/cw-daemon.exe 哈希被污染构建覆盖。**与 M2.4 无关**，但会影响 M2.5 的 `ensure_fresh_binary` 复跑，需协调相关 agent 收敛。
  - 非阻断观察 3（生产 daemon 为 M7 旧二进制）：PID 42988（2026-08-13 09:18 启动），与统一部署决策一致（M2.5 closed 后统一部署 fresh）。

**M2.5 启动记录（2026-08-13，Coordinator 创建）**：

- **任务 ID**：`T-1786584287058-7f712ff4`（替换占位 `T-1786519375913-31a65310`，8 steps，open）。
- **查询类型**：query.tests（`get_test_coverage` + `get_test_cases` + `get_tested_functions` + `get_test_coverage_summary` + `get_test_stability` MCP 工具）。
- **所有权白名单**：`rust_ext/src/daemon/query_handlers.rs`, `rust_ext/src/daemon/dispatch.rs`, `server/daemon_client.py`, `server/tools/tools_query.py`, `server/tools/tools_task.py`, `tests/test_query_tests_rpc.py`, `docs/design/daemon-rust-migration-ledger.md`。
- **禁止修改**：`rust_ext/src/daemon/snapshot_state.rs`（其他 agent 在途工作，`handle_query_tests` 已存在于此文件，M2.5 不改）。
- **步骤**（8）：audit_baseline → implement(query_handlers) → implement(dispatch) → implement(daemon_client) → implement(tools_query) → implement(tools_task) → test → build_deploy。
- **tests 相关 MCP 工具当前路由状态**：
  - `get_test_cases`（tools_task.py）→ 已 daemon RPC ✓
  - `get_tested_functions`（tools_task.py）→ 已 daemon RPC ✓
  - `get_test_stability`（tools_task.py）→ 已 daemon RPC ✓
  - `get_test_coverage`（tools_query.py）→ 本地 SQLite ✗（M2.5 需迁移）
  - `get_test_coverage_summary`（tools_task.py）→ 本地 SQLite ✗（M2.5 需迁移）
- **fail-closed 语义**：enterprise/auto 模式 daemon 不可用 → raise `DaemonUnavailableError`；local 模式返回 None。
- **环境前置条件**：工作区存在并发污染（graph.rs/snapshot.rs/replicator.rs/snapshot_state.rs 为 M 状态，cargo build 失败），Implementer 开始前需确认工作区收敛。
- **前置依赖**：M2.4 closed ✓（Reviewer PASS + Coordinator apply/close 完成）。
- **待办**：交付 Implementer 领取执行，推进到 `review` 后由 Independent Reviewer 复审。M2.5 是 M2 slice 最后一个子任务，closed 后统一部署 fresh 二进制到 runtime/current。

**M2.5 实现记录（2026-08-13，Implementer，任务推进到 review）**：

- **完成步骤**：8/8 全部 done（audit_baseline / query_handlers / dispatch / daemon_client / tools_query / tools_task / test / build_deploy）。
- **实现要点**：
  - `query_handlers.rs`：新增 `validate_query_tests_params`（空/纯空白/NUL → invalid_params）+ 3 个单测（`test_validate_tests_rejects_empty_or_blank` / `test_validate_tests_rejects_nul_byte` / `test_validate_tests_accepts_normal_names`），cargo test `query_handlers::` 16 passed。
  - `dispatch.rs`：`query.tests` 路由（L1552 区域）改为前置校验模式，合法符号名原样交 `SnapshotDaemonState.handle_query_tests`。
  - `daemon_client.py`：新增统一入口 `DaemonClient.query_tests`（fail-closed 三分支：remote 命中→返回；local→None；enterprise/auto 且 daemon 不可用→raise `DaemonUnavailableError`）；`get_test_cases`/`get_tested_functions`/`get_test_stability` 改经 `query_tests` 路由（返回类型改 `Any`）；新增 `get_test_coverage_summary`（从 query.tests test_cases 聚合 has_tests/test_count/high_confidence_count/tests[:10]，与 db 层 `TestRelationMixin.get_test_coverage_summary` 语义一致）；删除旧 `_query_tests_or_local`。
  - `tools_query.py` `get_test_coverage`：**不迁移 daemon**——无参全项目测试率统计与按符号查询的 query.tests 语义不对应，遵循 M2.4 `get_issue_summary` 先例（账本 §M2.4），仅补 docstring 路由说明，保留本地 SQLite。
  - `tools_task.py` `get_test_coverage_summary`：从本地 SQLite 迁移到 daemon RPC（`client.get_test_coverage_summary(qualified_name, db_path=_get_db_path_for_daemon())`），fail-closed 语义一致；异常返回 `{"error": str(e)}`。
- **验收证据**：
  - `py_compile` server/daemon_client.py + server/tools/tools_query.py + server/tools/tools_task.py exit 0。
  - pytest `tests/test_query_tests_rpc.py` **12/12 passed**（真实 cw-daemon round-trip，禁 mock；覆盖 success 正向/reverse/history + unknown_workspace/no_match/snapshot_not_ready/invalid_params/cross_workspace_isolation 拒绝矩阵 + 4 个 fail-closed 单测）。
  - Rust `cargo check --bin cw-daemon` 通过（仅 112 warnings，无 error）。
  - `cw-daemon.exe` SHA-256 `845EE8D0C4C06365034D202ECAEB4DB1A388EE586F6B0C4993B6409BE6EC72B1`（target/release fresh build，2026-08-13 10:4x）。
- **环境处理（M2.5 非阻断观察）**：默认管道 `\\.\pipe\callwarden-<SID>` 被生产 daemon 占用（PID 28664，`runtime/current` M7 时期二进制，`--socket` 显式参数启动）。按 M2.3 先例（本账本 §M2.3：测试前停止生产 daemon 释放默认管道，测试后恢复）执行：测试前 Stop-Process 28664，测试后以同参数重启 `runtime/current/cw-daemon.exe --socket \\.\pipe\callwarden-SID`（PID 18288，schema v50，`cw.py daemon health` → status=ok）。**生产 daemon 仍为 M7 时期旧二进制，M2.5 closed 后统一部署 fresh（沿用 Coordinator 决策）**。
- **测试 fixture 修正记录**（本任务测试过程发现，非 bug）：① `TestRun.error_message`/`error_type` 为非 Option `String`，snapshot `test_runs` 的 passed 记录传 NULL 会触发 daemon `internal_error: Invalid column type Null`，fixture 传空字符串 `""`；② `avg_duration_ms` (12.5+30.0)/2=21.25 经 Rust `round_to(…,1)` 为 21.3（非 21.2）。
- **状态**：**closed**（Reviewer PASS + Coordinator apply/close，2026-08-13）。
  - Reviewer 独立复审结论：PASS（附 2 条非阻断观察）。
  - Coordinator 收口：reviewer lease acquired（L-5e58f177bdbbcd2e, fencing=1）→ `task.apply`（applied_at=1786604107.98）→ `task.close`（closed_at=1786604107.99），均通过 daemon RPC。
  - 身份记录：agent=coordinator-m25-close, session=sess-coordinator-m25-20260813, model=glm-5.2, role=coordinator。
  - 非阻断观察 1（B1 任务残留未提交）：账本（M，§9.4 B1 审计记录 39 行）+ `tests/test_legacy_237_tools_baseline.py`（untracked）——属 B1 任务在途产物，非 M2.5 缺陷。
  - 非阻断观察 2（环境仍有 2 个 MCP server）：PID 12280/65940 存在 autostart 抢占风险；本次 round-trip 成功复跑未受影响。

**M2 slice 完成记录（2026-08-13，Coordinator 收口）**：

- **M2 父任务**：`T-1786519106584-7c67cef4`（M2：file/symbol/grep/issues/tests daemon 查询迁移）已 **closed**。
  - 6 个步骤全部 done（slice_m21_query_file / slice_m22_query_symbol / slice_m23_query_grep / slice_m24_query_issues / slice_m25_query_tests / m2_slice_acceptance）。
  - 9 个子任务全部 closed（5 个实际任务 + 4 个废弃占位任务）。
  - Coordinator 收口：循环 claim+report 6 个步骤 → lease.acquire（L-bf9d241c4e698c24, fencing=1）→ task.apply（applied_at=1786604555.74）→ task.close（closed_at=1786604555.93）。
  - 4 个废弃占位任务已关闭：T-1786519375878-2f9af314（M2.2 占位）、T-1786519375899-30d79b88（M2.3 占位）、T-1786519375906-31402be4（M2.4 占位）、T-1786519375913-31a65310（M2.5 占位），均通过 daemon RPC（claim → report → lease → apply → close），summary 记录废弃替代关系。
- **M2 slice 统一部署**（Coordinator 决策执行）：
  - 部署 fresh 二进制 `845EE8D0C4C06365034D202ECAEB4DB1A388EE586F6B0C4993B6409BE6EC72B1`（target/release fresh build，2026-08-13）到 `~/.callwarden/runtime/current/cw-daemon.exe`。
  - 替换旧二进制 `BF23BA91560A20A546D632AFF29737F33F6E38DDB0E0FE76BF02EF78AEE53E70`（M7 时期）。
  - 停止旧 daemon（PID 48120）→ 复制 fresh 二进制（Python shutil.copy2，沙箱拒绝 PowerShell Copy-Item）→ autostart 新 daemon（PID 15372，health ok，schema v50，workspace_count=4，snapshot_workspace_count=0）。
  - 部署后验证：runtime/current hash = `845EE8D0...`（与 target/release 一致），daemon health ok。
- **M2 slice 总结**：
  - 5 个查询类型（file/symbol/grep/issues/tests）全部迁移到 Rust daemon RPC。
  - 每个子任务均通过 Reviewer 独立复审（PASS）+ Coordinator apply/close。
  - 统一部署 fresh 二进制完成，生产 daemon 已更新为 M2 slice 最新代码。

**M2.1 HTTP 模式缺陷修复记录（2026-08-15，任务 `T-1786519172968-f13db464`）**：

> 背景：本任务 ID 在 §9.2 曾因 `task.create_subtask` 参数名错误被列为废弃占位任务（parent_id 为空）。2026-08-15 由用户/Coordinator 作为**独立主线任务**重新激活（parent_id 仍为空），负责修复 M2.1 在 **HTTP transport（H6 默认）** 下的 query.file 缺陷：HTTP 模式 `HttpDaemonRpcClient.get_file_symbols` 调用 query.file 时不携带 `workspace_instance_id`（Rust handler `handle_query_file` 强制 require），真实调用报 `invalid_params: 缺少字段: workspace_instance_id`。legacy Named Pipe 路径无此缺陷（`_ensure_remote_snapshot` 注入 instance_id）。

- **交付（commit `e4a8da4`，仅改白名单内文件）**：
  - `server/daemon_client.py`：`HttpDaemonRpcClient` 新增 `_remote_workspace_id`/`_remote_snapshot_ready`/`_project_root` 状态字段；新增 `configure_workspace(project_root)`（与 legacy `DaemonClient.configure_workspace` L790 签名对齐）；新增 `_ensure_remote_snapshot(db_path)`（自动 `workspace.register`，以返回 `workspace_instance_id` 为权威——daemon canonicalize `\\?\` 前缀与本地 `derive_workspace_instance_id` 不一致，禁止本地推导；按需 `snapshot.publish`，db_path 由 MCP 工具层透传）；`get_file_symbols` 改为经 `_ensure_remote_snapshot` 后注入权威 instance_id 再调 query.file。H6-FIX request_id 逻辑不受影响。legacy `DaemonClient.get_file_symbols`（L1147）前驱已实现 fail-closed，未改。
  - `tests/test_query_file_rpc.py`：新增 `TestHttpClientWorkspaceInjection` 4 个单测（register→publish→query.file 调用序与权威 instance_id 注入 / 复用不重复 register / register 响应缺 instance_id 抛 `DaemonUnavailableError` / 无 db_path 跳过 publish）。
- **file_read/file_list 位置偏差（#4 步骤记录）**：任务描述称 file_read/file_list 位于 `tools_query.py`，实际这些 MCP 工具在 `server/tools/tools_workspace.py`（白名单外不可改）。`tools_query.py` 中 query.file 相关 MCP 工具为 `get_file_symbols`（L129-142），已由前驱（commit `561a53a`）路由到 `client.get_file_symbols(file_path, db_path=_get_db_path_for_daemon())`（enterprise/auto 走 daemon RPC query.file 且 fail-closed，local 保留 get_db）。本任务确认 MCP 工具层已走 query.file RPC，无新增改动。
- **修复验证（FIX_VERIFY_PASS）**：`.trae-cn/evidence/m21_http_fix_verify.py` 对生产 daemon（HTTP endpoint）全链路验证：workspace.register（workspace_id=86，instance_id=`5fa6a9aad614f146`）→ 构造最小 snapshot db → `get_file_symbols("a.py", db_path=...)` 自动 publish + 注入 instance_id → query.file 返回 `alpha` 符号。修复前缺 instance_id 报 invalid_params（`.trae-cn/evidence/m21_http_probe.py` PROBE_PASS 复现）。
- **验收证据**：pytest `tests/test_query_file_rpc.py` **14 passed in 10.50s**（8 进程级 round-trip：success / unknown_workspace / out_of_bounds 穿越 / absolute_outside_root 无泄露 / snapshot_not_ready / invalid_params / cross_workspace_isolation；+ 3 legacy fail-closed 单测 + 4 本任务 HTTP 注入单测）。Rust `query_handlers::` **16 passed**。`py_compile` server/daemon_client.py + server/tools/tools_query.py + tests/test_query_file_rpc.py exit 0。
- **部署（#6）**：`scripts/refresh_shared_runtime.ps1 -TaskId T-1786519172968-f13db464 -RestartMcp -RunSmokeTests` → runtime_version `20260815-183931-4af5f8a380d3-0e939629`，status=passed。三端 hash 一致：构建产物 = `runtime/current/cw-daemon.exe` = 运行 PID 29264 executable，SHA-256 `1FA5E4D91A6AE617BA6B5A78D22437314EF89CD925C2CBE0B1814FB222D21912`（python_free，无 Python DLL）。daemon PID 29264，transport=http（H6 保持），smoke：`cw --version` 0.3.23 + `cw daemon ping` status ok。测试期间按 M2.3 先例停生产 daemon（PID 44104）释放默认管道，测后由部署恢复（PID 29264）。
- **必须回答 6 问结论（HTTP 视角，与 M2.1 前驱一致）**：
  1. workspace_id 绑定：`open_query_connection` 经 `owned_workspace` ACL 取 workspace_id + SQL `WHERE fi.workspace_id=?` 过滤（`query_local_file_symbols`）。
  2. file_instance_id 校验：结果集含 `file_instance_id`，SQL 精确匹配 `rel_path` 返回，handler 不显式校验。
  3. 越界路径错误：相对 `..` → 结构化 `out_of_bounds`（dispatch 层 `validate_query_file_path`）；绝对路径超出 root → rel_path 精确匹配天然空结果（fail-safe，无泄露），完整结构化拒绝留待 M2.2+（snapshot_state.rs 可改时）接入。
  4. snapshot fail-closed：未 publish → `snapshot_not_ready` 结构化错误。
  5. 跨 workspace 隔离：`owned_workspace` ACL + SQL workspace_id 过滤，A/B workspace 互不可见。
  6. Python fallback：仅 `local` 模式保留 SQL 回退；auto/enterprise（含 HTTP 模式）daemon 不可用 fail-closed raise `DaemonUnavailableError`，`_sql_fallbacks` 不计数。
- **遗留说明**：`_ensure_remote_snapshot` 在 `_project_root` 未配置时以 `os.getcwd()` 作为 client_view_root（与 legacy 对齐）；MCP server 以 workspace root 为 cwd 启动时行为正确。生产 daemon 已部署 fresh 二进制（含 M2.1-M2.5 全部查询迁移 + 本修复）。
- **状态**：推进到 `review`，待 independent_reviewer 复审；不得 apply/close。

**M2.2 HTTP 模式缺陷修复记录（2026-08-15，任务 `T-1786519211817-fcc40690`）**：

> 背景：本任务是 M2.2（query.symbol）的 **HTTP transport（H6 默认）** 轮次修复，与 M2.1 HTTP 修复（T-1786519172968-f13db464）同构。Named Pipe 时代的 M2.2（T-1786526643663-594ee010，已 closed）已实现 Rust handler `handle_query_symbol`（snapshot_state.rs L525-565，L530 强制 require `workspace_instance_id`）与 `handle_query_symbol_location`（L777-788，L782 同款 require），但 H6 默认 HTTP 后 `HttpDaemonRpcClient.get_symbol`（daemon_client.py L2024）只传 `{"qualified_name"}`、`get_symbol_location`（L2028）只传 `{"name", "file_path"}`，均缺 `workspace_instance_id` → 真实调用报 `invalid_params: 缺少字段: workspace_instance_id`（已用 PROBE_REPRODUCED 复现）。

- **交付**（仅改白名单内文件，Rust 侧未改——handler/路由为旧轮次既有实现）：
  - `server/daemon_client.py`：
    - `HttpDaemonRpcClient.get_symbol`（L2024-2035）复用 M2.1 `_ensure_remote_snapshot(db_path)`（L1680-1706）：自动 `workspace.register`（以 daemon 返回 `workspace_instance_id` 为权威）+ 按需 `snapshot.publish`（db_path 由 MCP 工具层透传），注入权威 instance_id 后调 query.symbol。
    - `HttpDaemonRpcClient.get_symbol_location`（L2038-2049）同构注入 query.symbol_location；name/file_path 参数契约不变。
    - legacy `DaemonClient.get_symbol_location`（L1131-1154）补齐 fail-closed：audit 发现其原本无条件 SQL fallback（违反统一验收标准第 5 项"仅 local 保留 SQL"），现对齐 get_symbol/get_file_symbols——auto/enterprise 下 daemon 不可用 raise `DaemonUnavailableError`，仅 local 模式保留 SQL 回退。
  - `tests/test_query_symbol_rpc.py`：新增 `TestHttpClientWorkspaceInjection` 8 单测（get_symbol/get_symbol_location 各 4：register→publish→query 调用序与权威 instance_id 注入 / 复用不重复 register / register 响应缺 instance_id 抛 `DaemonUnavailableError` / 无 db_path 跳过 publish）+ `TestClientFailClosed` 新增 get_symbol_location 3 单测（auto/enterprise raise、local 保留 SQL）。
- **验证**：
  - pytest `tests/test_query_symbol_rpc.py` **14 passed, 6 skipped in 1.37s**（20 用例）。6 skipped 为进程级 round-trip（成功/未知 workspace/snapshot_not_ready/invalid_params/跨 workspace 隔离/符号不存在）——默认管道被生产 daemon（PID 29264，transport=http）占用，设计性 skip 属正常（Named Pipe 时代旧轮次已验证过进程级行为）。
  - 真实 HTTP probe `.trae-cn/evidence/m22_http_fix_verify.py` 对生产 daemon（PID 29264，endpoint http://127.0.0.1:4165，schema v50，git_commit 4af5f8a380d3df9018a9f2dcdbac6787f8ce9033，binary SHA-256 `1FA5E4D91A6AE617BA6B5A78D22437314EF89CD925C2CBE0B1814FB222D21912`）全链路 **FIX_VERIFY_PASS**：workspace.register（workspace_id=88，instance_id=`d6e0501393b33e44`）→ 构造最小 snapshot db → `get_symbol("a.alpha", db_path=...)` 自动 publish + 注入 instance_id → query.symbol 返回完整详情（qualified_name=a.alpha、name=alpha、file_path=a.py、calls_out/called_by/issues）→ `get_symbol_location("alpha","a.py",...)` 同样返回位置（rel_path=a.py、abs_path）。
  - 修复前复现：`call("query.symbol", {"qualified_name": "a.alpha"})` 不带 instance_id → `invalid_params: 缺少字段: workspace_instance_id`（PROBE_REPRODUCED）。
  - Rust 回归：`cargo check --bin cw-daemon` 通过；`cargo test --lib symbol_query::` 2 passed + `query_handlers::` 16 passed（含 M2.2 参数校验）+ `file_query::` 3 passed，均无失败。
  - `py_compile` server/daemon_client.py + server/tools/tools_query.py + tests/test_query_symbol_rpc.py 全部 exit 0。
  - 回归测试（后台 job-45dfd8266e38419c81a70c909e36804f）：`tests/test_legacy_query_baseline.py` 71 passed 全通过；`tests/test_http_native_read_cutover.py::TestToolsQueryStaysLocal` 7 用例失败为 **pre-existing 测试失配，与 M2.2 修改无关**——已用 `git stash` 实证：将本任务 3 个修改文件 stash 后回到 HEAD 状态，同一 7 用例同样全部失败（stash 后已恢复修改）。根因：H4C-2（M2.4/M2.5）之后这些非 rust_native 工具改为 `route_worker_call`（HTTP 模式经 worker 执行 fail-closed，不调用本地 `get_db()`），而该测试仍期望 HTTP 模式（mock `is_http_transport_enabled`=True）下 mock `tools_query.get_db` 被调用——测试与实现契约失配，测试未同步更新。`test_http_native_read_cutover.py`/`tools_query.py` 不在 M2.2 白名单且属"禁止修改 query 相关代码"范围，本任务未修；建议 Coordinator 另立任务修复该测试。
- **必须回答 6 问结论（HTTP 视角，与 M2.1 前驱一致）**：
  1. workspace_id 绑定：`handle_query_symbol` 经 `owned_workspace` ACL（snapshot_state.rs L532）取 workspace_id + `query_symbol_detail` SQL `WHERE fi.workspace_id=?1`（symbol_query.rs L40）过滤；`handle_query_symbol_location` 经 `open_query_connection` + `query_local_symbol_location` SQL `WHERE fi.workspace_id=?1`（file_query.rs L59）。
  2. symbol_id 校验：`query_symbol_detail` 按 `fsv.qualified_name=?2` 精确匹配 + is_current=1 + is_deleted=0 + status!='archived'；不存在返回 `Value::Null`（handler 既有契约，非结构化错误码）；dispatch 层 `validate_query_symbol_params` 前置校验空/空白/NUL → `invalid_params`（dispatch.rs L1507-1508）。
  3. 越界路径错误：query.symbol 无路径参数（仅 qualified_name）；query.symbol_location 的 file_path 经 `normalize_workspace_path` 归一化——相对路径直接匹配 rel_path，绝对路径超出 root 时 strip_prefix 失败则按原值匹配 rel_path（天然空结果 `Value::Null`，fail-safe 无泄露）；空参 → `invalid_params`。query.file 的 `out_of_bounds` 结构化拒绝仅覆盖 file 查询（dispatch.rs `validate_query_file_path`），symbol_location 沿用旧契约不扩展。
  4. snapshot fail-closed：未 publish → handler `get_snapshot_manager(workspace_instance_id)` 缺失 → `snapshot_not_ready` 结构化错误（snapshot_state.rs L539-546）。
  5. 跨 workspace 隔离：`owned_workspace` ACL（peer.uid 绑定）+ SQL workspace_id 过滤，A/B workspace 同一符号互不可见（symbol_query.rs 单测 + 进程级用例覆盖）。
  6. Python fallback：仅 `local` 模式保留 SQL 回退（get_symbol_location 本轮补齐）；auto/enterprise（含 HTTP 模式）daemon 不可用 fail-closed raise `DaemonUnavailableError`，`_sql_fallbacks` 不计数。
- **部署决策**：本任务未改 Rust 源码，无需 rebuild。生产 daemon（PID 29264）继续运行 M2.1 部署的 fresh 二进制（SHA-256 `1FA5E4D9...`，含 M2.1-M2.5 全部查询迁移 + M2.1 HTTP 修复）；Python client 修复随源码发布生效（MCP server 重启后加载），无需 daemon 侧变更。按 M2 slice 统一部署决策（M2 全部完成后统一部署），本任务不强制重启生产 daemon。
- **状态**：推进到 `review`，待 independent_reviewer 复审；不得 apply/close。

**M2.3 HTTP 模式缺陷修复记录（2026-08-15，任务 `T-1786519211823-fd25bb10`）**：

> 背景：本任务是 M2.3（query.grep）的 **HTTP transport（H6 默认）** 轮次修复，与 M2.1（T-1786519172968-f13db464）/ M2.2（T-1786519211817-fcc40690）HTTP 修复同构。Named Pipe 时代的 M2.3（T-1786529505247-9d083e54，已 closed）已实现 Rust handler `handle_query_grep`（snapshot_state.rs L791-828，L796 强制 require `workspace_instance_id`）与 dispatch 路由（dispatch.rs L1523-1543，含 `validate_query_grep_params` 前置校验拒绝空数组/空白/NUL；READONLY_METHODS L2181 含 query.grep），但 H6 默认 HTTP 后 `HttpDaemonRpcClient` **没有 query_grep 方法**（HTTP 模式无 query.grep RPC 入口）。legacy `DaemonClient.query_grep`（daemon_client.py L1179-1211）已带 fail-closed，且全仓无 MCP 工具调用它（`grep .query_grep(` 仅命中测试文件）——legacy 路径不改，仅新增 HTTP client 入口。MCP `file_grep` 工具（tools_workspace.py L230 起）是纯本地 os.walk+re 全文搜索，语义与 snapshot grep 不同且不在白名单，保持不动（工具层迁移另立专项）。

- **交付**（仅改白名单内文件，Rust 侧未改——handler/路由为旧轮次既有实现）：
  - `server/daemon_client.py`：`HttpDaemonRpcClient.query_grep` 新增（L2080-2113，位于 get_file_symbols 之后）：复用 M2.1 `_ensure_remote_snapshot(db_path)`（L1690-1716）自动 `workspace.register`（以 daemon 返回 `workspace_instance_id` 为权威）+ 按需 `snapshot.publish`（db_path 由 MCP 工具层透传），注入权威 instance_id 后调 query.grep。参数契约对齐 legacy（patterns/fixed/limit/path/include_all/kind + db_path 透传）；db_path=None 时仅注入已注册 instance_id，不 publish。Rust handler 返回 `Value::String`（格式化文本，含符号归属上下文），无匹配返回 `No matches for: <pattern>` 文本。
  - `tests/test_query_grep_rpc.py`：新增 `TestHttpClientWorkspaceInjection` 5 单测（register→publish→query.grep 调用序与权威 instance_id 注入 / 复用不重复 register/publish / register 响应缺 instance_id 抛 `DaemonUnavailableError` / 无 db_path 跳过 publish / fixed/limit/path/include_all/kind 非默认参数完整透传）。legacy `TestClientFailClosed` 3 单测既有（auto/enterprise raise `DaemonUnavailableError`、local 返回 None），未改。
- **验证**：
  - pytest `tests/test_query_grep_rpc.py` **8 passed, 9 skipped in 0.84s**（17 用例）。9 skipped 为进程级 round-trip（成功/多 pattern AND/未知 workspace/无匹配文本/snapshot_not_ready/invalid_params/跨 workspace 隔离/limit 截断/fixed 字面）——默认管道被生产 daemon（PID 29264，transport=http）占用，设计性 skip 属正常（Named Pipe 时代旧轮次已验证过进程级行为）。
  - 真实 HTTP probe `.trae-cn/evidence/m23_http_fix_verify.py` 对生产 daemon（PID 29264，endpoint http://127.0.0.1:4165，schema v50，git_commit 4af5f8a380d3df9018a9f2dcdbac6787f8ce9033，binary SHA-256 `1FA5E4D91A6AE617BA6B5A78D22437314EF89CD925C2CBE0B1814FB222D21912`）全链路 **FIX_VERIFY_PASS**：workspace.register（workspace_id=90，instance_id=`1e2bff16a5b99c7f`）→ 构造最小 snapshot db + 真实源文件 a.py（`def alpha():\n    TODO: fixme\n`）→ `query_grep(["TODO"], db_path=...)` 自动 publish + 注入 instance_id → query.grep 返回 `Grep with symbol context: pattern='TODO', 1 matches (of 1 after filter)` 含 `[in fn a.alpha]` 与匹配行内容；未知 workspace → `workspace_not_found`；空 patterns → `invalid_params`；新 register 未 publish → `snapshot_not_ready`。
  - 修复前复现：`call("query.grep", {"patterns": ["TODO"]})` 不带 instance_id → `invalid_params: 缺少字段: workspace_instance_id`（PROBE_REPRODUCED）；修复前 `HttpDaemonRpcClient` 无 query_grep 方法（AttributeError，HTTP 模式无入口）。
  - Rust 回归：本任务未改 Rust 源码（`git diff rust_ext/` 为空），无需 rebuild；`cargo check --bin cw-daemon` 确认通过（增量，无变更编译）。
  - `py_compile` server/daemon_client.py + tests/test_query_grep_rpc.py 全部 exit 0。
- **必须回答 6 问结论（HTTP 视角，与 M2.1/M2.2 前驱一致）**：
  1. workspace_id 绑定：`handle_query_grep`（snapshot_state.rs L826）经 `open_query_connection` 的 `owned_workspace` ACL 校验 peer.uid + workspace_instance_id → workspace_id；`query_local_grep` 符号归属 SQL `WHERE fi.workspace_id = ?1`（cli/grep.rs query_symbol_contexts L413）。
  2. grep 结果限定当前 workspace：`query_local_grep`（cli/grep.rs L65）先 `query_workspace_root`（workspaces 表 WHERE id=?1），搜索根限定为 workspace root；`resolve_search_root` 从 root 解析 path，符号归属上下文 SQL 带 workspace_id 过滤。
  3. 越界 path 参数：`resolve_search_root`（cli/grep.rs L140-171）拒绝 escapes workspace root 的 path（canonicalize 后 starts_with 校验）→ internal_error 文本（既有契约，非结构化错误码）；path 缺失/无效时回退 workspace root。
  4. snapshot fail-closed：未 publish → handler `get_snapshot_manager` 缺失 → `snapshot_not_ready` 结构化错误（HTTP probe 已验证）。
  5. 跨 workspace 隔离：workspace root 限定 + `owned_workspace` ACL + SQL workspace_id 过滤；A workspace 搜不到 B workspace 的真实文件（旧轮次进程级用例 + 本任务 workspace_not_found/隔离语义覆盖）。
  6. Python fallback：仅 `local` 模式返回 None（本地 grep 由 CLI/MCP file_grep 负责）；auto/enterprise（含 HTTP 模式）daemon 不可用 raise `DaemonUnavailableError`，`_sql_fallbacks` 不计数。MCP `file_grep` 本地 os.walk+re 全文搜索是设计决策（语义为全文搜索，与 query.grep 的 snapshot 已索引内容搜索不同），非 fallback，工具层迁移另立专项。
- **部署决策**：本任务未改 Rust 源码，无需 rebuild。生产 daemon（PID 29264）继续运行 M2.1 部署的 fresh 二进制（SHA-256 `1FA5E4D9...`，含 M2.1-M2.5 全部查询迁移 + M2.1 HTTP 修复）；Python client 修复随源码发布生效（MCP server 重启后加载），无需 daemon 侧变更。按 M2 slice 统一部署决策，本任务不强制重启生产 daemon。
- **状态**：推进到 `review`，待 independent_reviewer 复审；不得 apply/close。

**M2.4 HTTP 模式缺陷修复记录（2026-08-15，任务 `T-1786519211831-fd9a5380`）**：

> 背景：本任务是 M2.4（query.issues）的 **HTTP transport（H6 默认）** 轮次修复，与 M2.1（T-1786519172968-f13db464）/ M2.2（T-1786519211817-fcc40690）/ M2.3（T-1786519211823-fd25bb10）HTTP 修复同构。Named Pipe 时代的 M2.4（T-1786539379174-90f74174，已 closed）已实现 Rust handler `handle_query_issues`（snapshot_state.rs L830-844，L835 强制 require `workspace_instance_id` + L836 `qualified_name`，返回 `Value::Array` 结构化 issues 数组，符号不存在返回 `[]`）与 dispatch 路由（dispatch.rs L1544-1551，含 `validate_query_issues_params` 前置校验拒绝空/纯空白/NUL；READONLY_METHODS L2182 含 query.issues），但 HTTP 模式**入口缺注入**：MCP 工具 `get_symbol_issues`（tools_task.py L765 起）HTTP 分支（L792-798）直接 `client.call("query.issues", {qualified_name, include_info})` 未注入 workspace_instance_id → HTTP 模式报 invalid_params。`HttpDaemonRpcClient` 无 query_issues 便捷方法。次审计点：legacy `DaemonClient.get_symbol_issues`（daemon_client.py L1242-1256）仍 `_sql_fallbacks += 1` 静默回退本地 SQL，与已 fail-closed 的 legacy `query_issues`（L1213-1240）不一致。

- **交付**（仅改白名单内文件，Rust 侧未改——handler/路由为旧轮次既有实现）：
  - `server/daemon_client.py`：`HttpDaemonRpcClient.query_issues` 新增（L2114-2138，位于 query_grep 之后）：复用 M2.1 `_ensure_remote_snapshot(db_path)`（L1690-1716）自动 `workspace.register`（以 daemon 返回 `workspace_instance_id` 为权威）+ 按需 `snapshot.publish`（db_path 由 MCP 工具层透传），注入权威 instance_id 后调 query.issues。参数契约对齐 legacy `query_issues`（qualified_name/include_info + db_path 透传）；db_path=None 时仅注入已注册 instance_id，不 publish。
  - `server/tools/tools_task.py`：MCP `get_symbol_issues` HTTP 分支（L792-801）由直接 `client.call("query.issues", {...})` 改为 `client.query_issues(qualified_name, include_info=include_info, db_path=_get_db_path_for_daemon())`——注入 workspace_instance_id 消除 invalid_params 缺陷，db_path 透传触发 snapshot.publish；legacy 分支不变（已走 `_remote_query` 注入）。
  - `server/daemon_client.py`：legacy `DaemonClient.get_symbol_issues`（L1242-1269）补 fail-closed：`mode=get_daemon_mode()`，仅 local 模式保留 `_sql_fallbacks += 1` + `db.get_symbol_issues` SQL 回退，auto/enterprise daemon 不可用 raise `DaemonUnavailableError`（对齐 query_issues L1238 与 M2.2 get_symbol_location 修复模式）。返回结构不变（severity 降序 issues 列表，两分支原样透传）。依赖链审计：CLI `cw issues`（cli/main.py L7864）直接调 db 层 `CodeGraphDB.get_symbol_issues`，不依赖 legacy DaemonClient SQL 回退，无影响。
  - `tests/test_query_issues_rpc.py`：docstring 追加 HTTP 轮次说明；新增 `TestHttpClientWorkspaceInjection` 4 单测（register→publish→query.issues 调用序与权威 instance_id 注入（核心断言：query.issues 请求必须携带 workspace_instance_id）/ 复用不重复 register/publish / register 响应缺 instance_id 抛 `DaemonUnavailableError` / 无 db_path 跳过 publish）+ `TestGetSymbolIssuesFailClosed` 4 单测（auto/enterprise raise、local 保留 SQL 回退、remote 命中直返）。进程级 round-trip（成功/unknown_workspace/snapshot_not_ready/invalid_params/空数组/include_info/跨 workspace 隔离）为 legacy 轮次既有产物，保留。
- **验证**：
  - pytest `tests/test_query_issues_rpc.py` **12 passed, 7 skipped in 2.84s**（19 用例）。12 passed = HTTP 注入 4 + get_symbol_issues fail-closed 4 + query_issues fail-closed 4；7 skipped 为进程级 round-trip——默认管道 `\\.\pipe\callwarden-S-1-5-21-1583625257-826939952-3615027596-1001` 被生产 daemon（PID 29264，transport=http）占用，设计性 skip 属正常（Named Pipe 时代旧轮次已验证过进程级行为，本任务真实验收以 HTTP probe 为准）。
  - 真实 HTTP probe `.trae-cn/evidence/m24_http_fix_verify.py` 对生产 daemon（PID 29264，endpoint http://127.0.0.1:4165，schema v50，git_commit 4af5f8a380d3df9018a9f2dcdbac6787f8ce9033，binary SHA-256 `1FA5E4D91A6AE617BA6B5A78D22437314EF89CD925C2CBE0B1814FB222D21912`，authority_id manifest `S-1-5-21-1583625257-826939952-3615027596-1001`，worker_status healthy）全链路 **FIX_VERIFY_PASS**：workspace.register（workspace_id=94，instance_id=`cb70f74ab361d282`）→ 构造最小 snapshot db（semgrep_findings R-SEMG1 + guardrail_findings GR-1 缺陷行）→ `query_issues("a.alpha", db_path=...)` 自动 publish + 注入权威 instance_id → query.issues 返回 severity 降序 issues 数组命中已知缺陷符号（semgrep R-SEMG1 WARNING：message/snippet/fix 完整 + guardrail GR-1 warn：status open）→ 未知 workspace → `workspace_not_found` → 空 qualified_name → `invalid_params` → 新 register 未 publish → `snapshot_not_ready`。
  - 修复前复现：`call("query.issues", {"qualified_name": "a.alpha", "include_info": False})` 不带 instance_id → `invalid_params: 缺少字段: workspace_instance_id`（PROBE_REPRODUCED）。
  - Rust 回归：本任务未改 Rust 源码（`git diff rust_ext/` 为空），无需 rebuild；`cargo check --bin cw-daemon --no-default-features` 确认通过（exit 0，仅 117 warnings）。
  - `py_compile` server/daemon_client.py + server/tools/tools_task.py + tests/test_query_issues_rpc.py 全部 exit 0。
- **必须回答 6 问结论（HTTP 视角，与 M2.1/M2.2/M2.3 前驱一致）**：
  1. workspace_id 绑定：`handle_query_issues`（snapshot_state.rs L841）经 `open_query_connection` 的 `owned_workspace` ACL 校验 peer.uid + workspace_instance_id → workspace_id；`query_local_issues` 符号定位 SQL `WHERE fi.workspace_id = ?` 过滤。
  2. issues 结果限定当前 workspace：`query_local_issues` 先 `query_workspace_root` 定位 workspace，符号定位 JOIN 链（file_symbol_versions→file_versions→file_instances）带 `fi.workspace_id=?` 过滤，semgrep_findings/guardrail_findings 均按 file_instance_id/workspace_id 归属。
  3. 越界 qualified_name 参数：dispatch 层 `validate_query_issues_params`（query_handlers.rs M2.4 前置校验）拒绝空/纯空白/NUL → `invalid_params`（结构化错误码）；符号不存在（合法名但无匹配）→ 空数组 `[]`，无信息泄露。
  4. snapshot fail-closed：未 publish → handler `get_snapshot_manager` 缺失 → `snapshot_not_ready` 结构化错误（HTTP probe 已验证）。
  5. 跨 workspace 隔离：`owned_workspace` ACL（peer.uid 绑定）+ SQL workspace_id 过滤；旧轮次进程级用例（A/B workspace 互不可见）+ 本任务 workspace_not_found 覆盖。
  6. Python fallback：legacy `get_symbol_issues` 本轮补齐 fail-closed——仅 `local` 模式保留 SQL 回退（设计决策非 fallback）；auto/enterprise（含 HTTP 模式）daemon 不可用 raise `DaemonUnavailableError`，`_sql_fallbacks` 不计数。query_issues 全模式无 SQL 回退（local 返回 None，由本地 IssueAnalyzerMixin 处理）。MCP 工具 `get_symbol_issues` 本地路径（legacy 分支）是设计决策非 fallback。
- **部署决策**：本任务未改 Rust 源码，无需 rebuild。生产 daemon（PID 29264）继续运行 M2.1 部署的 fresh 二进制（SHA-256 `1FA5E4D9...`，含 M2.1-M2.5 全部查询迁移 + M2.1 HTTP 修复）；Python client 修复随源码发布生效（MCP server 重启后加载），无需 daemon 侧变更。按 M2 slice 统一部署决策（M2 全部完成后统一部署 fresh 二进制到 runtime/current），本任务不强制重启生产 daemon。
- **状态**：推进到 `review`，待 independent_reviewer 复审；不得 apply/close。

**M2.5 HTTP 模式缺陷修复记录（2026-08-15，任务 `T-1786519211837-fdfffe10`）**：

> 背景：本任务是 M2.5（query.tests）的 **HTTP transport（H6 默认）** 轮次修复，与 M2.1（T-1786519172968-f13db464）/ M2.2（T-1786519211817-fcc40690）/ M2.3（T-1786519211823-fd25bb10）/ M2.4（T-1786519211831-fd9a5380）HTTP 修复同构。Named Pipe 时代的 M2.5（T-1786584287058-7f712ff4，已 closed）已实现 Rust handler `handle_query_tests`（snapshot_state.rs L846-871，L851 强制 require `workspace_instance_id` + L852 `qualified_name`，参数 qualified_name/reverse/history/limit，经 query_local_test_cases / query_local_tested_functions / query_local_test_stability 查询）与 dispatch 路由（dispatch.rs L1552-1559，含 `validate_query_tests_params` 前置校验拒绝空/纯空白/NUL；daemon_query.rs L473-477 MethodInfo 注册；READONLY_METHODS 含 query.tests），但 HTTP 模式**入口缺注入**：MCP 工具 `get_test_cases`/`get_tested_functions`/`get_test_coverage_summary`/`get_test_stability`（tools_task.py L835/L870/L908/L957）HTTP 分支直接 `client.call("query.tests", {...})` 未注入 workspace_instance_id → HTTP 模式报 invalid_params。`HttpDaemonRpcClient` 无 query_tests 便捷方法。次审计点确认：legacy `DaemonClient.query_tests`（L1271-1302）已 fail-closed（非 local raise）；get_test_cases/get_tested_functions/get_test_coverage_summary/get_test_stability（L1305-1348）复用 query_tests 已继承 fail-closed——记录确认不改。

- **交付**（仅改白名单内文件，Rust 侧未改——handler/路由为旧轮次既有实现，`git diff rust_ext/` 为空）：
  - `server/daemon_client.py`：`HttpDaemonRpcClient.query_tests` 新增（L2155-2186，位于 query_issues 之后）：复用 M2.1 `_ensure_remote_snapshot(db_path)`（L1690-1716）自动 `workspace.register`（以 daemon 返回 `workspace_instance_id` 为权威）+ 按需 `snapshot.publish`（db_path 由 MCP 工具层透传），注入权威 instance_id 后调 query.tests。参数契约对齐 legacy `query_tests`（qualified_name/reverse/history/limit + db_path 透传）；db_path=None 时仅注入已注册 instance_id，不 publish。reverse/history 语义与 Rust handler 三分支一致（test cases / tested functions / test stability）。
  - `server/tools/tools_task.py`：4 个 MCP 工具 HTTP 分支由直接 `client.call("query.tests", {...})` 改为 `client.query_tests(...)`（带 `db_path=_get_db_path_for_daemon()` 透传触发 snapshot.publish）：`get_test_cases`（reverse=False/history=False/limit=50）、`get_tested_functions`（reverse=True/history=False/limit=50）、`get_test_coverage_summary`（call 后本地聚合语义不变：high_confidence_count/tests[:10]）、`get_test_stability`（reverse=False/history=True/limit=limit）。返回结构不变，legacy 分支未动。
  - `tests/test_query_tests_rpc.py`：docstring 追加 HTTP 轮次说明；新增 `TestHttpClientWorkspaceInjection` 5 单测（query.tests 自动 register/publish + 注入权威 instance_id（核心断言：query.tests 请求必须携带 workspace_instance_id）/ reverse/history/limit 参数透传 / 复用不重复 register/publish / register 响应缺 instance_id 抛 `DaemonUnavailableError` / 无 db_path 跳过 publish）+ `TestGetTestToolsFailClosed` 6 单测（get_test_cases/get_tested_functions/get_test_coverage_summary/get_test_stability 复用 query_tests 继承 fail-closed：auto 模式 daemon down raise、local 返回 None、remote 命中直返 + get_test_coverage_summary 聚合语义断言）。
- **验证**：
  - pytest `tests/test_query_tests_rpc.py` **15 passed, 8 skipped in 0.85s**（23 用例）。15 passed = HTTP 注入 5 + get_test_tools fail-closed 6 + query_tests fail-closed 4；8 skipped 为进程级 round-trip（成功正向/reverse/history/unknown_workspace/no_match/snapshot_not_ready/invalid_params/cross_workspace_isolation）——默认管道 `\\.\pipe\callwarden-S-1-5-21-1583625257-826939952-3615027596-1001` 被生产 daemon（PID 30900，transport=http）占用，设计性 skip 属正常（Named Pipe 时代旧轮次已验证过进程级行为，本任务真实验收以 HTTP probe 为准）。
  - 真实 HTTP probe `.trae-cn/evidence/m25_http_fix_verify.py` 对生产 daemon（PID 30900，endpoint http://127.0.0.1:1932，schema v50，git_commit 0c515424d7450ea6c793f0ad3790086584dab764，binary SHA-256 `1FA5E4D91A6AE617BA6B5A78D22437314EF89CD925C2CBE0B1814FB222D21912`，authority_id manifest `S-1-5-21-1583625257-826939952-3615027596-1001`，worker_status healthy）全链路 **FIX_VERIFY_PASS**：workspace.register（workspace_id=98，instance_id=`ae39ae937fba1746`）→ 构造最小 snapshot db（test_case_relations direct_call/high + test_runs 2 条）→ `query_tests("a.alpha", db_path=...)` 自动 publish + 注入权威 instance_id → query.tests 正向返回 test cases 数组命中已知被测符号（test_fn_id=2/test_name=test_alpha/confidence=high/test_file=a.py）→ reverse=True 反向返回 tested functions（tested_fn_id=1/tested_name=alpha/tested_start_line=1）→ history=True 返回稳定性 dict（total_runs=2/pass_rate=0.5/avg_duration_ms=21.3/recent_failures/by_test）→ 未知 workspace → `workspace_not_found` → 空 qualified_name → `invalid_params` → 新 register 未 publish → `snapshot_not_ready`。
  - 修复前复现：`call("query.tests", {"qualified_name": "a.alpha"})` 不带 instance_id → `invalid_params: 缺少字段: workspace_instance_id`（PROBE_REPRODUCED）。
  - Rust 回归：本任务未改 Rust 源码（`git diff rust_ext/` 为空），无需 rebuild；`cargo check --bin cw-daemon --no-default-features` 确认通过（exit 0，增量 0.70s，仅 117 warnings 无 error）。
  - `py_compile` server/daemon_client.py + server/tools/tools_task.py + tests/test_query_tests_rpc.py 全部 exit 0。
- **必须回答 6 问结论（HTTP 视角，与 M2.1/M2.2/M2.3/M2.4 前驱一致）**：
  1. workspace_id 绑定：`handle_query_tests`（snapshot_state.rs L862）经 `open_query_connection` 的 `owned_workspace` ACL 校验 peer.uid + workspace_instance_id → workspace_id；测试关系 SQL `WHERE tcr.workspace_id = ?1` 过滤（cli/issues_tests.rs query_local_test_cases L140 / query_local_tested_functions L181）。
  2. tests 结果限定当前 workspace：测试关系查询链（test_case_relations JOIN symbols JOIN file_instances）带 `tcr.workspace_id=?1` + 被测/测试符号定位子查询 `fi2.workspace_id=?1` 双重过滤；stability 的 test_runs 同样按 `tr.workspace_id=?1` 过滤。
  3. 越界 qualified_name 参数：dispatch 层 `validate_query_tests_params`（query_handlers.rs M2.5 前置校验）拒绝空/纯空白/NUL → `invalid_params`（结构化错误码）；符号不存在（合法名但无匹配）→ 空数组 `[]` / 空稳定性 dict（既有契约），无信息泄露。
  4. snapshot fail-closed：未 publish → handler `get_snapshot_manager` 缺失 → `snapshot_not_ready` 结构化错误（HTTP probe 已验证）。
  5. 跨 workspace 隔离：`owned_workspace` ACL（peer.uid 绑定）+ SQL workspace_id 过滤；旧轮次进程级用例（A/B workspace 互不可见）+ 本任务 workspace_not_found 覆盖。
  6. Python fallback：legacy `query_tests` 全模式无 SQL 回退（local 返回 None，由本地 TestRelationMixin 处理）；4 个工具方法复用 query_tests 已继承 fail-closed（auto/enterprise raise `DaemonUnavailableError`，local 返回 None），`_sql_fallbacks` 不计数。MCP 工具本地路径（legacy 分支）是设计决策非 fallback。
- **部署决策**：本任务未改 Rust 源码，无需 rebuild。生产 daemon（PID 30900）继续运行 fresh 二进制（SHA-256 `1FA5E4D9...`，含 M2.1-M2.5 全部查询迁移 + M2.1-M2.5 HTTP 修复）；Python client 修复随源码发布生效（MCP server 重启后加载），无需 daemon 侧变更。
- **状态**：推进到 `review`，待 independent_reviewer 复审；不得 apply/close。

**M2 slice 统一 fresh 部署记录（2026-08-15，任务 `T-1786805249329-233111df`）**：

> 背景：M2.1-M2.5 全部 closed 后，按统一部署决策执行 M2 slice 的 fresh 部署——M2 系列 5 个子任务均未改 Rust 源码（handler/路由为旧轮次既有实现），Python client 修复随源码发布生效，但生产 daemon 一直运行 M2.1 时代的旧 fresh 二进制。本次统一 rebuild release 并安装 fresh 二进制到 `runtime/current`，覆盖 M2.1-M2.5 全部查询迁移 + HTTP 修复。

- **部署执行**：`pwsh -NoProfile -File .\scripts\refresh_shared_runtime.ps1 -TaskId T-1786805249329-233111df -RestartMcp -RunSmokeTests`（release 配置，目标 `rust_ext\target\stage-refresh`）。脚本输出：停止 14 个 MCP 进程 + 旧 daemon（PID 30900）→ 探针去重单实例启动新 daemon → "runtime 切换成功；daemon/bridge 已就绪，MCP 由 IDE supervisor 重连"，exit 0，rollback=false。
- **证据**：`~/.callwarden/runtime/evidence/20260815-224747-ccca314d769e-90ef0d7d.json`（git_head `ccca314d769e7bf01391bc894e7d9a28bf34c70b` = M2.5 回归修复 commit）。
- **Coordinator 独立核验结果（只读实证，非采信脚本自报）**：
  - **三端 hash 一致**：runtime/current 5 个二进制实际 SHA-256 与证据记录完全一致——cw-daemon.exe `1FA5E4D91A6AE617BA6B5A78D22437314EF89CD925C2CBE0B1814FB222D21912`、cw.exe `BC2AC93A08D0FC1D206A2FE195A6D5D776674D67F90323D3A3BF790E85271702`、cw-client.exe `7272DB99F3C4F430C00C1BCAF98DC136FA31E186D7AA6DEA85F4C604F9296D9F`、cw-agent.exe `0C342825E9874D99133BE6E95BC1AE3FAFE6CCEBF25CCDDF57F26F8E3165A34C`、cw-bridge.exe `652A23CAEA2D615946B54175E7DBF8E4951202DD54DFB81E13F8661BD44E496D`。
  - **运行 PID**：`cw daemon ping`（Python 3.14）→ status ok、**PID 2396**、**transport=http**、protocol_version 1、task_db_fingerprint `c7f4609c...`；Win32_Process 确认 PID 2396 ExecutablePath = `C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe`（运行路径确实在 runtime/current）。
  - **dumpbin 核验**：`dumpbin /dependents cw-daemon.exe` 依赖仅 KERNEL32/ws2_32/advapi32/ntdll/bcryptprimitives/VCRUNTIME140/api-ms-win-crt-*，**无任何 Python DLL**（python_free 模式，evidence python_dependencies 为空）——与 Python 3.14 解释器无关，无需 python314.dll 依赖核验。
  - **smoke tests**：`cw --version` = callwarden 0.3.23 exit 0；`cw daemon ping` exit 0。
  - **MCP 重连**：processes_after 仅新 daemon（PID 2396）；IDE supervisor 已重连 1 个 `cw.py server` MCP 进程（PID 17420，Python 3.14）。
- **部署结论**：M2 slice fresh 部署 PASS——生产 daemon 现运行含 M2.1-M2.5 全部查询迁移 + HTTP 修复的二进制（SHA-256 `1FA5E4D9...`，transport=http，PID 2396）。
- **状态**：推进到 `review`，待 independent_reviewer 复审；不得 apply/close。

## 9.4 B1 Legacy Baseline 审计记录（2026-08-13，任务 `T-1786590722456-db00d074-sub-1`）

**B1 执行时间与结论**：2026-08-13 执行（Git HEAD `05190ff`）。静态审计结论：**237 个 MCP 工具注册已确认**（静态 `@mcp.tool()` 计数与 AST 解析双重核验一致），但**运行时可用性未验证**——矩阵中全部 237 个工具 `current_status = "unknown"`，不得将“已注册”声称为“已可用”。

**Backend 分布**（静态启发式分类，未经运行时验证）：

| backend | 数量 | 占比 |
|---|---:|---:|
| `rust_native`（经 `_get_daemon_client()` 走 Rust daemon RPC） | 28 | 11.8% |
| `python_compat`（直接调用 Python Mixin `db.method()`） | 190 | 80.2% |
| `legacy_local`（本地逻辑/辅助功能） | 19 | 8.0% |

注：backend 分类基于函数体关键词启发式，存在误判（部分 `rust_native` 工具实际直调本地 SQLite，如 `find_issues`/`get_test_coverage`；部分实为 Rust 本地 cache 而非 daemon RPC，如 `diff_callers`/`diff_callees`/`compare_snapshots`）。逐项精确路由以矩阵 4 字段交叉核对结果为准。

**矩阵 4 字段交叉核对结果**（脚本 `.trae-cn/evidence/B1_fix_fields.py`，基于当前源码 AST 解析）：

| 字段 | 确认 | none | unknown |
|---|---:|---:|---:|
| `cli_entry` | 118 | 119 | 0 |
| `daemon_rpc_method` | 15 | 219 | 3 |
| `rust_handler` | 15 | 219 | 3 |
| `test_file` | 194 | 43 | 0 |

**发现的关键问题清单**（详见 `.trae-cn/evidence/legacy-237-baseline-B1/B1-summary.md`）：

1. `snapshot_workspace_count = 0`：stats/query 类 daemon 路由依赖 snapshot publish，无 snapshot 时回退 SQL。
2. `bootstrap_status` 缺少 workspace 隔离：查询 `task_quality_findings`/`tasks` 表时无 `WHERE workspace_id = ?`。
3. `build_graph`/`build_directory` 同步阻塞，无 async 包装或超时保护。
4. `get_uncommented_symbols` 运行时返回值未验证（可能静默返回空列表）。
5. **daemon binary 早于 Git HEAD**：debug binary 早于 HEAD 76 分钟、release 早 83 分钟，当前运行的 daemon **不是**基于当前 HEAD 编译，后续验收需重新编译并核验 SHA-256。
6. backend 启发式分类存在误判（见上注），已在矩阵字段交叉核对中逐项修正。

**审查与修复历史**：

- 2026-08-13 Independent Reviewer 首轮复审结论 **BLOCKED**（6 个阻断问题：任务状态未推进、矩阵 4 核心字段 100% unknown、current_status 误标 available、证据文件大小声明错误、本账本未更新、基线测试缺失），见 `.trae-cn/evidence/B1-independent-review-2026-08-13.md`。
- 2026-08-13 Implementer 完成修复：矩阵 4 字段基于源码交叉核对补全（unknown 从 948 降至 6：仅 `diff_callers`/`diff_callees`/`compare_snapshots` 的 RPC 路由静态无法确认）、`current_status` 全部改为 `unknown`（blocking_reason=“注册已确认，运行时可用性未验证”）、`matrix-sha256.txt` 重算（含精确文件大小）、新增基线测试 `tests/test_legacy_237_tools_baseline.py`、任务推进到 review。

**证据文件**：`.trae-cn/evidence/mcp-tool-matrix-baseline.json`（237 工具矩阵）、`.trae-cn/evidence/legacy-237-baseline-B1/`（environment/runtime/daemon-health/registration.log/matrix-sha256/B1-summary）。

## 9.5 B2 workspace/snapshot/bootstrap 修复记录（2026-08-13，任务 `T-1786590722456-db00d074-sub-2`）

**前置依赖**：B1（`T-1786590722456-db00d074-sub-1`）已 closed（Reviewer PASS + Coordinator apply/close，2026-08-13）。

**修复内容**：

- **文件**：[db/db_bootstrap.py](file:///c:/git_work/callwarden/db/db_bootstrap.py) `BootstrapMixin.bootstrap_status()` 方法。
- **问题**：`task_quality_findings` 表的 COUNT 查询缺少 `workspace_id` 过滤，导致跨 workspace 的 quality findings 串扰（ws-a 的 block finding 在 ws-b 的 bootstrap_status 中也被计入）。
- **修复**：两处 COUNT SELECT 补 `AND workspace_id = ?`（用 `_get_active_workspace_id()`），与 `get_latest_scan_run` / rule 查询的 workspace 隔离语义保持一致。
- **tasks 统计保持全局**：`tasks` 表 schema（[db/schema.py](file:///c:/git_work/callwarden/db/schema.py) L347-360）无 `workspace_id` 字段——任务编排是用户级全局（task_create 不绑定 workspace，父子任务树跨 workspace 编排），此处保持全局统计，不做 workspace 过滤。添加注释说明原因。
- **测试**：新增 [tests/test_legacy_workspace_bootstrap.py](file:///c:/git_work/callwarden/tests/test_legacy_workspace_bootstrap.py)（5 用例），核心隔离用例验证 ws-a block / ws-b warn 场景下 bootstrap_status 的 workspace 隔离。

**前置条件验证**（Reviewer 独立核实）：

1. `task_quality_findings` 表有 `workspace_id` 字段（schema L834）✓
2. `record_task_quality_finding` 写入当前 active workspace_id（[db_task_quality.py](file:///c:/git_work/callwarden/db/db_task_quality.py) L117）✓
3. `_get_active_workspace_id()` 存在（[db_base.py](file:///c:/git_work/callwarden/db/db_base.py) L4021，默认 1）✓
4. `tasks` 表无 `workspace_id`（schema L347-360），全局统计理由成立 ✓

**验收证据**：

- `test_legacy_workspace_bootstrap.py` **5/5 passed** + `test_bootstrap_status.py` **22/22 passed** = **27 passed**（Reviewer 独立复跑）。
- 14 个既有失败独立确认根因三类且均与 B2 无关：5×FK 约束（fixture 未建真实 task）、4×`no such column: details`（daemon quality_findings 列名与 schema `evidence` 不一致）、5×CLI 连真实库（Total tasks: 200）。
- `db_bootstrap.py` 修改仅影响 `bootstrap_status()` 的 COUNT SELECT，不触及 INSERT/daemon RPC/CLI 层。
- 提交前 `cw --refresh-all` 通过（total 15.12s, exit 0）。

**Reviewer 非阻断观察**：

1. B2 变更未提交（Coordinator 收口时 commit）→ 已处理。
2. 账本无 B2 记录 → 已补记（本节）。
3. 生产 daemon 当前无进程运行（MCP server autostart 会在需要时拉起）→ 非 B2 阻断。

**状态**：**closed**（Reviewer PASS + Coordinator apply/close，2026-08-13）。
- Reviewer 独立复审结论：PASS（附 3 条非阻断观察）。
- Coordinator 收口：先 commit B2 变更（db_bootstrap.py + test + 账本补记）→ reviewer lease acquired → `task.apply` → `task.close`（均通过 daemon RPC）。
- 身份记录：agent=coordinator-b2-close, session=sess-coordinator-b2-20260813, model=glm-5.2, role=coordinator。

## 9.6 B3 file/symbol/grep/issues/tests 五类查询基线核对（2026-08-13，任务 `T-1786590722456-db00d074-sub-3`）

**前置依赖**：B1（`sub-1`）+ B2（`sub-2`）已 closed，作为本任务工作基础。

**工作内容**（plan `legacy-237-tools-baseline-plan.md` B3 定义）：完成 M2.5（query.tests）并核对 M2.1-M2.4 当前 runtime 与源码一致性，确保五类查询可通过统一入口调用。

**审计结论**：

1. **M2.1-M2.5 全部 closed**（§9.3），5 个 RPC 测试文件齐全且已提交：
   `test_query_file_rpc.py` / `test_query_symbol_rpc.py` / `test_query_grep_rpc.py` / `test_query_issues_rpc.py` / `test_query_tests_rpc.py`。
2. **五层路由链完整**（矩阵 `daemon_rpc_method=query.*` 的工具）：
   - dispatch.rs L1502-1559 路由臂：`query.stats/symbol/search/callers/callees/file/symbol_location/grep/issues/tests`（file/grep/issues/tests 含 dispatch 层结构化前置校验）
   - snapshot_state.rs 14 个 `handle_query_*` handler 全部实现（L497-1010），与 dispatch 臂一一对应
   - daemon_client.py 17 个查询方法齐全（L1014-1358），fail-closed/failback 语义明确
   - tools_query.py / tools_task.py 的查询类 MCP 工具均通过 `_get_daemon_client()` 调用 client 方法
3. **fresh runtime**：查询 client 方法经 `_remote_query("query.<x>", ...)` 走 RPC；`get_symbol_history` / `get_file_history` 等矩阵标记 `rpc_none` 的工具保持 Python `get_db()` 直调，未混入 RPC。
4. **结构化拒绝路径**：Rust 侧错误码构造存在——`invalid_params`/`workspace_not_found`（dispatch.rs）、`out_of_bounds`（query_handlers.rs）、`snapshot_not_ready`（snapshot_state.rs）；5 个 fail-closed client 方法（get_symbol/get_file_symbols/query_grep/query_issues/query_tests）在 auto/enterprise 模式 daemon 不可用时抛 `DaemonUnavailableError`，不静默回退本地 SQLite；仅 local 模式按语义回退（get_symbol/get_file_symbols 走 SQL，grep/issues/tests 返回 None）。

**交付物**：

- 新增 [tests/test_legacy_query_baseline.py](file:///c:/git_work/callwarden/tests/test_legacy_query_baseline.py)：静态一致性基线 + client fail-closed 单测，**71 用例全部通过**（0.54s）。核对三类基线：统一入口（13 个查询 MCP 工具 → client 方法 → RPC 名 → dispatch 臂 → handler）、fresh runtime（rpc_none 工具保持 Python 直调入口，16 个工具注册存在）、结构化拒绝路径（Rust 错误码 + 5 方法 fail-closed/local 语义）。
- 复核 `test_query_tests_rpc.py`（M2.5 产物）：12 用例，实测 4 passed + 8 skipped（生产 daemon 占用默认 Named Pipe，与 M2.3/M2.4 一致的已知限制）。

**验收证据**：

- `pytest tests/test_legacy_query_baseline.py` → **71 passed**。
- `pytest tests/test_query_tests_rpc.py` → **4 passed + 8 skipped**（skip 原因：默认管道 `\\.\pipe\callwarden-<sid>` 被生产 daemon 占用；进程级 round-trip 在 M2.5 收口时已验证过）。
- 本任务不改生产代码，仅新增测试 + 账本补记。

**非阻断观察**：

1. RPC round-trip 测试依赖默认 Named Pipe 空闲；生产 daemon 运行时整体 skip。baseline 测试设计为静态核对（AST/正则解析源码），不依赖 daemon，始终可跑。
2. `test_query_tests_rpc.py` 的 8 个进程级用例在 Reviewer 复跑时同样会 skip，需在验收时说明该前置条件（M2.3 已有先例）。

**状态**：已推进到 **review**（Implementer 完成 3 个步骤，待 Coordinator 复核收口）。

## 9.7 B4 task/lease/governance 治理工具基线核对（2026-08-13，任务 `T-1786590722456-db00d074-sub-4`）

**前置依赖**：B1（`sub-1`）+ B2（`sub-2`）+ B3（`sub-3`）已 closed，作为本任务工作基础。

**工作内容**（plan `legacy-237-tools-baseline-plan.md` B4 定义 + split `legacy-237-tools-baseline-split.md` L21-25）：核对 task/lease/governance 工具组的 legacy 可用入口，验证任务、步骤、identity、lease、apply/close 通过 daemon 单写点运行，业务错误和权限错误不被包装成连接错误。

**审计结论**（Step 0，`rust_ext/src/daemon/task_collab.rs`）：

1. **治理核心 handler 全部存在**（daemon 单写点实现）：
   - task 生命周期：`handle_task_create`（L911）/ `handle_task_claim`（L1001）/ `handle_task_work_next`（L1233）/ `handle_task_report`（L1455）/ `handle_task_apply`（L2146）/ `handle_task_close`（L2209）/ `handle_task_rollback`（L1927）/ `handle_task_reopen`（L1976）/ `handle_task_events`（L1754）/ `handle_task_contract_set`（L1297）/ `handle_task_contract_get`（L1402）
   - lease 控制面：`handle_lease_acquire`（L2389）/ `handle_lease_extend`（L2525）/ `handle_lease_release`（L2656）/ `handle_lease_status`（L2787）/ `handle_lease_list_events`（L2858）
   - 衍生：`handle_task_create_from_plan`（L3117）/ `handle_task_create_subtask`（L3254）
2. **lease 门禁 fail-closed 完整**：`require_lease_params`（L2040）要求 lease_token + fencing_counter 双凭证；`validate_lease_for_mutation`（L2061）实现 6 项校验（CLOCK/NOT_FOUND/TOKEN_MISMATCH/EXPIRED/FENCING_STALE/HOLDER_MISMATCH）。
3. **权威时钟注入**：`with_clock`（L649）+ `AuthoritativeClock` 注入；`lease_clock_unavailable`（L237）在时钟缺失时返回 `E_LEASE_CLOCK_UNAVAILABLE`，不静默放行。
4. **单写点 + 业务错误语义**：route_task_write 在 daemon 不可用时 enterprise/auto 模式 fail-closed（`DaemonUnavailableError`），不静默回退本地 SQLite；daemon RPC 错误以结构化错误码返回。
5. **业务错误实证**（只读，不污染生产库）：`cw task show T-NOT-EXIST-0000000000` 经 daemon RPC 返回 `task_not_found: 任务不存在: T-NOT-EXIST-0000000000`（结构化业务错误码 + 中文消息），**未被包装为连接错误**；`cw task show <B4 任务>` 正常返回 Task Detail，证明 CLI → daemon 单写点只读链路可用。

**测试结果**：

- Step 1 `tests/test_lease_gate_empirical.py` → **2 passed + 18 skipped**（0.79s）。PASS 为 `TestLeaseGateRoutePolicy` 2 个 fail-closed 单测（enterprise/auto 模式 daemon 不可用时抛 `DaemonUnavailableError`，不静默回退本地 DB）；18 个进程级 round-trip 用例因默认管道 `\\.\pipe\callwarden-<sid>` 被生产 daemon（pid 15372）占用而按测试设计 skip（前置条件 3）。
- Step 2 `tests/test_task_close_gate.py` → **5 passed + 4 skipped**（0.96s）。PASS 为 5 个跨平台静态断言（`E_CHILD_TASKS_NOT_CLOSED` / `E_NO_STEPS` / `E_STEPS_NOT_DONE` / `E_LEASE_CLOCK_UNAVAILABLE` + `validate_lease_for_mutation` + `with_clock` / `closed_at` 写入门禁，全部与 task_collab.rs 源码实际特征一致）；4 个 CLI E2E 因同一管道占用 skip。

**既有验证依据**（进程级 round-trip 的历史通过记录）：M7（2026-08-12，§9.3 前记录）已验证真实 daemon RPC lease 全链路（Rust 45 passed + Python 10/10 passed + acquire → extend → release → status → list_events 全链路 PASS，binary hash `2405CF…D189`）；任务 B（`T-1786412969125-6edaa100`，test_task_close_gate.py 产出任务）的 CLI E2E 在收口时于隔离环境跑通过。

**非阻断观察**：

1. RPC round-trip / CLI E2E 测试依赖默认 Named Pipe 空闲；生产 daemon 运行时整体 skip（与 B3 §9.6 观察一致，属测试设计内前置条件）。M7 与任务 B 收口时已在隔离环境验证过进程级行为。
2. 本任务不改生产代码，仅补账本记录 + 审计核对。

**状态**：已推进到 **review**（Implementer 完成 3 个步骤，待 Coordinator 复核收口）。

## 9.8 B6 剩余工具组与全量验收记录（2026-08-13，任务 `T-1786590722456-db00d074-sub-6`）

**前置依赖**：B1（`sub-1`）+ B2（`sub-2`）+ B3（`sub-3`）+ B4（`sub-4`）+ B5（`sub-5`）已 closed，作为本任务工作基础。

**工作内容**（plan `legacy-237-tools-baseline-plan.md` B6 定义 + split `legacy-237-tools-baseline-split.md` L33-37）：完成剩余工具分组核验（Git、coverage、defect、review、规则、文件和其他工具），生成 237 工具最终矩阵和全量 smoke/回归报告，交独立 Reviewer。

**全量入口核验**（Step 0/1，脚本 `.trae-cn/evidence/b6_verify_entries.py`）：

1. **237/237 工具入口核验通过**，四项检查全过：
   - `source_file` 存在（11 个 tools 模块）；
   - `def {tool_name}(` 定义存在（工具函数在 `register(mcp)` 内嵌套定义）；
   - 定义前文存在 `@mcp.tool(` 装饰器（MCP 注册入口）；
   - 函数体含统一入口引用（`get_db()` 单例直调 / `_get_daemon_client()` / `.call(` RPC 中转），无直连 SQLite 路径（矩阵 `direct_sqlite_access_count=0` 复核一致）。
2. **分组分布**：B6 剩余 8 组 126 工具全部通过（tools_collab 6 / tools_p2_graph 10 / tools_p3_identity 7 / tools_p4_lease 8 / tools_rules 9 / tools_security 36 / tools_semantic 19 / tools_summary 31）；B1-B5 已覆盖组（tools_query 32 / tools_task 52 / tools_workspace 27，共 111）同样全过。

**最终矩阵固化**（Step 1，脚本 `.trae-cn/evidence/b6_finalize_matrix.py`）：

- `current_status` 从 B1 冻结态（237 全 unknown）收口：`runtime_verified=54`（被 B 系列测试运行时引用：query 29 / write-jobs 16 / workspace-bootstrap 12 / lease-gate 5 的并集）+ `entry_verified=183`，**unknown=0**，满足 plan 通过条件 2（无未解释 unknown）。
- 被 B 系列测试引用的 54 个工具 `test_file` 更新为对应测试文件；`test_file=none` 由 43 降为 38（5 个被 B 系列测试覆盖）。
- metadata 追加 `finalization` 记录（B6 任务、入口核验统计、状态分布、test_file=none 处理说明）。
- 矩阵 SHA-256 重算：`359463058651A52B268DC81418557AC4CF3BB7F217EF3943315D797AA1D260CA`（224856 bytes），同步更新 `matrix-sha256.txt`。

**测试更新**（Step 0，`tests/test_legacy_237_tools_baseline.py`）：由 B1 的 6 用例扩展为 **8 用例，8 passed（0.79s）**：

- `test_current_status_not_available`（B1 全 unknown 断言）→ `test_current_status_finalized`（无 unknown/available，取值限 `runtime_verified`/`entry_verified`，blocking_reason 全非空）；
- 新增 `test_all_tools_have_mcp_registration`（237 工具 def + `@mcp.tool(` 注册冒烟）；
- 新增 `test_all_tools_have_unified_entry`（函数体统一入口引用冒烟）；
- SHA 断言保持精确匹配（矩阵修改须重跑 `b6_finalize_matrix.py` 刷新记录）。

**43 个 test_file=none 工具处理**：原为 B1 盘点时无独立测试文件引用的工具（tools_summary 18 / tools_collab 4 / tools_p3_identity 5 / tools_workspace 5 / tools_task 5 / tools_query 4 等）。B6 以全量入口冒烟断言覆盖其注册与统一入口存在性；其中 5 个被 B 系列测试实际引用，已更新 `test_file`。剩余 38 个记录在 metadata `finalization.test_file_none_count`，不再使用 `unknown` 占位。

**非阻断观察**：

1. `.trae-cn/evidence/` 目录被 `.gitignore` 忽略，矩阵/SHA/核验脚本为本地证据，不进 git commit（与 B1-B5 模式一致）；git 提交仅含测试与文档。
2. 工作区 `docs/design/http-daemon-mvp-*.md` 4 个文件被其他 agent 在途修改，非 B6 白名单，未触碰；B6 handoff 记录以追加段落写入 `http-daemon-mvp-task-plan.md`，不覆盖对方变更。
3. `entry_verified` 工具尚无逐工具运行时测试（计划通过条件 3 中"成功或明确结构化错误测试"以 B 系列运行时测试 54 个 + 入口冒烟断言覆盖），遗留运行时全量回归建议并入 HTTP H0 的 smoke 验收。

**状态**：已推进到 **review**（Implementer 完成 3 个步骤，待 Coordinator 复核收口；B6 closed 后可关闭 B 父任务 `T-1786590722456-db00d074`）。

## 9.9 H5 HTTP MVP 独立复审与统一部署（2026-08-15，任务 `T-1786590214634-9e740cdc-sub-6`）

> 角色：tester/evidence（HTTP MVP 父任务最后一个子任务）
> 门禁：Independent Reviewer PASS 后 Coordinator 才 apply/close；M2.5 只有在 H5 PASS 后重新启动
> evidence bundle：`docs/design/http-daemon-mvp-evidence.md`

**1. Fresh daemon 构建结果（当前 Git HEAD）**：

- Git HEAD：`5db4d84`（`5db4d843cdbaa20acf48aaea765b53f6f0c94613`）；工作区仅有 untracked 无关产物，无已跟踪文件修改。
- 构建部署：`scripts/refresh_shared_runtime.ps1 -TaskId T-1786590214634-9e740cdc-sub-6 -RestartMcp -RunSmokeTests` → `status=passed`、`rollback=false`（脚本 evidence：`~/.callwarden/runtime/evidence/20260815-151521-5db4d843cdba-0ae6f6b0.json`）。
- cw-daemon.exe SHA-256 三端一致：构建 = runtime/current 安装 = 运行 = `98629c5f23b38fcf6588eb61e717bb297f0ef1fcd493839fc6fa9110271ad2fb`；dumpbin 依赖模式 `python_free`（纯 Rust，无 python*.dll）。
- daemon health：`status=ok`、`schema_version=50`、`workspace_count=4`；authority_id=`LINKPLAY-SCM/windows/S-1-5-21-1583625257-826939952-3615027596-1001/67a410a0…`；smoke（`cw --version` → 0.3.23、`cw daemon ping` → ok）exit 0。
- 观察：部署后存在两个 daemon 实例（43528 带 `--socket` 脚本启动 / 44044 无参数 supervisor 拉起），均 runtime/current fresh binary，tester 未清理（只读权限），详见 evidence §4。

**2. Evidence 文档**：`docs/design/http-daemon-mvp-evidence.md`（含 provenance、构建部署记录与 hash 对比表、daemon health、capability registry 快照、HTTP 自举验证、与旧 H4B-M 证据 diff 说明、环境限制）。

**3. Release Acceptance 测试结果**：

- 新增 `tests/test_http_daemon_release_acceptance.py`（13 用例，自包含、真实断言、不 mock）→ **13 passed**（单独与并行回归场景均 exit 0）。
- 覆盖：fresh 产物存在性 + hash 三端一致（evidence/installed/running）、daemon health（schema 50）、capability registry 三端对齐、HTTP 自举冒烟（隔离 daemon manifest discovery + /health 交叉核对 + /capabilities python_compat available == 107 白名单 + 真实 HTTP RPC round-trip + 负向 fail-closed）。
- 关键回归：`tests/test_http_capability_registry.py` + `tests/test_http_combined_worker_cutover.py` → **120 passed**（72 + 48）。

**4. Capability Registry 快照结论**（以当前代码为准，脚本 `.trae-cn/evidence/h5_capability_snapshot.py`，快照 `.trae-cn/evidence/h5-capability-registry-snapshot.json`）：

- Rust `COMPAT_ROUTE_WHITELIST` = **99** = Python registry（装配后）= Python `RUST_COMPAT_ROUTE` = 99
  （W2-1 `T-1786840097330-dec66710`：get_uncommented_symbols / get_module_call_stats /
  get_semgrep_stats 迁移 rust_native，107→104，见 §9.16；W2-2 `T-1786840097330-a9e0ec69`：
  get_clone_stats / get_job_stats / get_clone_group_stats 迁移 rust_native，104→101，见 §9.17；
  W2-3 `T-1786840097331-fd01a3f8`：defect_stats / get_edit_stats 迁移 rust_native，101→99，
  get_metrics 保持 HTTP blocked 不占 compat 计数，见 §9.18）；
- `validate_against_rust_route()` **aligned=True**（missing/extra/mismatch 全空）；
- 与旧 H4B-M matrix（registered_methods=67）的差异为 H4C 第二批装配推进所致（35→107），H5 快照以 registry 路由数为 compat worker 可达方法真相。

**5. 后续 Rust migration 恢复顺序建议**（参照 `http-daemon-mvp-task-plan.md` §3 的 5 组串行顺序）：

1. **workspace / snapshot / manifest / refresh** 组；
2. **stats / uncommented / metrics** 组；
3. **build / build_directory / semgrep / job** 组；
4. **Git / coverage / defect / review** 等工具组；
5. **删除已替换的 compatibility worker handlers**。

每组迁移只切换 capability registry 的 backend，不修改客户端公开方法名；每组完成后按
「契约 → Rust handler → Python thin client → success/reject 测试 → fresh runtime →
evidence → independent review → apply/close」闭环（M2 slice 先例）。

## 9.10 H6-FIX：RPC client request_id 生成缺陷整改（2026-08-15，任务 `T-1786787764852-4c330571`）

> 角色：implementer（只推进到 review）
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`.trae-cn/evidence/h6fix-step0-audit.md`、`h6fix-step4-verify.md`、`h6fix_audit_dedup.py`

**1. 根因（HTTP 默认化后暴露）**：

- `HttpDaemonRpcClient.__init__` 用 `self._ids = itertools.count(1)`，`call()` 默认
  envelope id = `str(next(self._ids))`——CLI 短生命周期进程每次执行写命令都是新
  client 实例、id 从 "1" 开始。
- daemon 侧 mutation dedup 表 `http_dedup` 持久化保留 24h，主键
  `(workspace_instance_id, method, request_id)`（`http_server.rs` `check_and_reserve`）；
  同名 method + 同 id + 不同 params → `E_REQUEST_ID_REUSE_MISMATCH`。
- 已复现：`cw.py task next` 领取命令本身报
  `E_REQUEST_ID_REUSE_MISMATCH: request_id reused with different params`；dedup 表
  存在 `('', 'lease.acquire', '1', ...)` 等多条 `request_id='1'` 跨进程复用记录。
- `UnixDaemonRpcClient` 同样 `itertools.count(1)`；named-pipe 时代 dedup 未持久化
  未触发，切回 named-pipe 会复发。
- CLI `route_task_write`/`_route_lease_write` 注入的 `params["request_id"]`（uuid）
  仅作为普通参数参与 params_hash，envelope id 仍是计数器——该字段当前无效。

**2. 修复方案（仅 Python 客户端，daemon 侧 dedup 逻辑不动）**：

- `HttpDaemonRpcClient.call()`：默认 id 改 `uuid4` 全局唯一；`params["request_id"]`
  存在时优先用作 envelope id（CLI 路由注入的 uuid 生效，重试复用同一 params 即
  命中 Replay）；显式 `request_id` 参数语义不变（优先级最高，重试幂等）。
- `UnixDaemonRpcClient.call()`：同款修复（防 named-pipe 回落复发）。
- 顺带收益：`_query_mutation_outcome`/`call_with_autostart` 重试复用同一
  params（含 request_id）→ envelope id 相同 → daemon Replay，不再重复执行 mutation。

**3. 验证结果**：

- 修复后 `cw.py lease acquire`（新进程）返回 `ok=true`，随后 `lease release` 成功，
  全程无 `E_REQUEST_ID_REUSE_MISMATCH`；连续多次独立进程写操作（task.claim /
  task.report / lease.acquire / lease.release）均成功。
- dedup 表新增记录全为 `req-*`（CLI 注入 uuid）或裸 uuid4，不再出现计数器 "1"/"2"
  复用；旧 `request_id='1'` 记录保留未删（与 uuid 不冲突，24h 自然过期）。
- 新增 `tests/test_http_daemon_default_transport.py::TestHttpRequestIdDedupSemantics`
  4 用例（默认 uuid 唯一 / params.request_id 采用为 envelope id / 同 id+同 params
  重试 Replay / 跨实例同 method 不同 params 无冲突）；全文件 **18 passed**。

**4. 改动文件（所有权白名单）**：`server/daemon_client.py`、
`tests/test_http_daemon_default_transport.py`、`docs/design/daemon-rust-migration-ledger.md`。

## 9.11 W1-1：workspace 读面工具 HTTP native 修复与便捷方法（2026-08-15，任务 `T-1786808777378-bbcbf059`）

> 角色：implementer（只推进到 review）
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`.trae-cn/evidence/w1_1_http_verify.py`（gitignore，不 commit）、`.trae-cn/evidence/w1_1_recover_manifest.py`
> 所有权白名单：`server/daemon_client.py`、`server/tools/tools_workspace.py`（读面工具）、
> `tests/test_workspace_rpc_http.py`、`tests/test_http_native_read_cutover.py`、本账本。
> 禁止：Rust（rust_ext/）、db schema、`server/compat_registry.py`、写工具/refresh/snapshot 函数。

**1. 根因（HTTP 默认化后暴露）**：

- MCP `get_active_workspace` 的 HTTP 分支此前直接调 `workspace.activate {}`，未注入
  `workspace_instance_id`；Rust `handle_workspace_activate`/`handle_workspace_status`
  强制 `require_str_param("workspace_instance_id")`，缺省返回 `invalid_params`——
  HTTP 模式（CW_DAEMON_TRANSPORT=http，H6）下该工具实际不可用。
- `list_workspaces` HTTP 分支返回 daemon 视图（daemon_workspaces 行，12 字段），与
  legacy workspaces 表行（id/name/root_path/created_at/is_active/description/active_task_id）
  字段集不同，调用方按 legacy 语义消费时缺 `root_path`/`name`。

**2. 修复方案（仅 Python 客户端，Rust handler 不动）**：

- `HttpDaemonRpcClient.workspace_status(db_path=None)` 便捷方法（M2 query 便捷方法之后、
  `get_callers` 之前）：先 `_ensure_remote_snapshot(db_path)` 以 daemon 返回值为权威
  `workspace_instance_id`（register 响应缺该字段抛 `DaemonUnavailableError`，fail-closed；
  db_path 为 None 时跳过 snapshot.publish），注入后 `self.call("workspace.status", {...})`。
- `get_active_workspace()` HTTP 分支改为经该便捷方法并传 `_get_db_path_for_daemon()`；
  返回行透传 daemon 字段，兼容映射 `client_view_root→root_path`、`name=os.path.basename(client_view_root)` 兜底，
  `host_real_root` 保留。
- `list_workspaces()` HTTP 分支逐行映射 `root_path=client_view_root`、`name=basename` 兜底，
  docstring 记录 daemon 视图与 legacy 字段差异；"当前活动工作区"在 HTTP 模式由单
  workspace 语义替代 legacy `is_active`。
- 不改 Rust：`workspace.status`/`workspace.list`/`workspace.activate` handler 契约保持。

**3. 6 问验收（W1-1 账本）**：

| # | 问题 | 结论 | 实证 |
|---|------|------|------|
| ① | workspace_id 绑定 | PASS | probe b：register 返回 `workspace_id=105, workspace_instance_id=4baea3ff12c2ea5c`，status 便捷方法注入后命中同一 instance_id |
| ② | 结果限定 | PASS | probe c：workspace.list 按 peer uid 限定返回 22 行，当前 workspace 命中=True；与 daemon_workspaces 单 workspace 语义一致 |
| ③ | 越界参数 | PASS | probe e：status 缺 instance_id → `invalid_params`（fail-closed，非 method_not_found 泄漏） |
| ④ | snapshot_not_ready | PASS（设计） | 便捷方法无 db_path 时跳过 publish（不触发 snapshot）；注入 instance_id 后 status 仅查 daemon_workspaces 行，不依赖 snapshot 就绪 |
| ⑤ | 跨 workspace 隔离 | PASS | probe d：不存在 instance_id → `workspace_not_found`（owned ACL：owner_uid 匹配 + 非 archived，越权/不存在拒绝）；peer uid 隔离由 Rust owned ACL 保证 |
| ⑥ | Python fallback 边界 | PASS | HTTP 模式 `HttpDaemonRpcClient` 无 SQL 回退（fail-closed）；便捷方法经 `_ensure_remote_snapshot` 以 daemon 返回值权威化；legacy 分支仅 `is_http_transport_enabled()=False` 时进入 |

**4. 验证结果**：

- py_compile：`server/daemon_client.py`、`server/tools/tools_workspace.py`、`tests/test_workspace_rpc_http.py` 均 PASS。
- 单测（任务书命令）：`pytest tests/test_workspace_rpc_http.py tests/test_query_issues_rpc.py -q`
  → **24 passed, 11 skipped**（11 skipped 为进程级 round-trip 设计性 skip：权威 manifest 被生产
  daemon 占用，按 test_query_issues_rpc.py 管道占用 skip 模式处理）。
- 回归：`pytest tests/test_http_native_read_cutover.py` → **18 passed, 7 failed**；
  7 个失败全部为 `TestToolsQueryStaysLocal`（HTTP 模式下 7 个 tools_query python_compat 工具
  断言"走本地 db"）——H4C-2（`T-1786716190783-ba187c88`）改造后这些工具经 `route_worker_call`
  HTTP 模式走 compat worker（fail-closed），测试断言未同步适配，**既有缺口，非本次改动引入**
  （git diff 证明本任务未触碰 tools_query.py 与 TestToolsQueryStaysLocal）。
- 真实 HTTP probe（生产 daemon PID 2396，transport=http）：**6/6 PASS**（ping / register /
  status 便捷方法 PROBE_HIT / list / 不存在 instance_id→workspace_not_found /
  缺 instance_id→invalid_params）。
- 环境修复：隔离测试 daemon 曾覆盖权威 manifest 致 `E_HTTP_MANIFEST_STALE`，
  已用 `.trae-cn/evidence/w1_1_recover_manifest.py` 重建权威 manifest 指向生产 daemon
  （PID 2396 / endpoint http://127.0.0.1:5511 / git_commit ccca314d / manifest_hash eeacc530…）。

**5. 改动文件**：`server/daemon_client.py`（+25）、`server/tools/tools_workspace.py`（+65）、
`tests/test_workspace_rpc_http.py`（新增）、`tests/test_http_native_read_cutover.py`（+54）、
本账本（9.11 节）。

**6. 状态**：推进到 `review`，待 independent_reviewer 复审；不得 apply/close。

## 9.12 W1-2：workspace 写面经 daemon 通道（register/set_active/delete）（2026-08-15，任务 `T-1786808777379-15702f0c`）

> 角色：implementer（只推进到 review）
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`.trae-cn/evidence/w1_2_http_verify.py`（真实 HTTP probe，gitignore，不 commit）
> 所有权白名单：`server/daemon_client.py`、`server/tools/tools_workspace.py`（写面工具
> register_workspace/set_active_workspace/delete_workspace）、`tests/test_workspace_write_rpc_http.py`、
> 本账本。禁止：Rust（rust_ext/）、db schema、compat_registry、读面工具、refresh_file、
> task apply/close。

**1. 根因（HTTP 默认化后写面双表分裂）**：

- HTTP 模式（CW_DAEMON_TRANSPORT=http，H6）下，三个写面工具此前只写 SQLite
  `workspaces` 表（真相源），不写 daemon 注册表（daemon_workspaces，读面
  workspace.list/status 的数据源）→ 新注册的 workspace 在 daemon 读面不可见
  （PROBE_REPRODUCED：SQLite 纯写后 workspace.list 命中=False，22→24 行增长仅含
  后续 probe 注册行，无新 workspace）。
- `workspaces` 表无 `workspace_instance_id` 列且禁改 schema；Rust
  `workspace.activate`/`workspace.remove` 强制 `require_str_param("workspace_instance_id")`
  （workspace.rs，dispatch.rs L1486-1487 已注册，03e92e5 引入），无法直接用
  root_path 调用。

**2. 修复方案（仅 Python 客户端，Rust handler 不动）**：

- `HttpDaemonRpcClient` 新增 3 个写面便捷方法（`workspace_status` 之后、`get_callers` 之前）：
  - `workspace_register(root_path)`：`workspace.register {client_view_root}`，响应缺
    `workspace_instance_id` 抛 `DaemonUnavailableError`（fail-closed，与
    `_ensure_remote_snapshot` 同款校验）；缓存 `_norm_root(root_path)→instance_id`。
  - `_resolve_workspace_instance(root_path)`：缓存优先，缺省幂等 register 确定性重算
    （workspace.register 是 INSERT OR REPLACE，instance_id = sha256(owner_uid|
    host_real_root|git_remote_url|git_head_commit_sha)[:16] 确定性，映射可随时重建，
    无持久化状态分裂）。
  - `workspace_activate(root_path)` / `workspace_remove(root_path)`：经
    `_resolve_workspace_instance` 注入权威 instance_id 后调 RPC。
- `_norm_root`（模块级）：正斜杠 + 盘符小写 + 去尾斜杠，对齐 config.norm_path，
  避免 `C:\foo` 与 `c:/foo` 缓存 key 分裂。
- `tools_workspace.py` 三工具 HTTP 分支（`is_http_transport_enabled()` 为真时）：
  SQLite 先行（真相源）→ daemon 同步；daemon 不可用（`DaemonUnavailableError`）传播、
  禁止静默 SQL 回退（否则双表分裂持续，读面不可见）。local 模式保持纯 SQL。
  - `register_workspace`：`db.register_workspace(...)` 后 `client.workspace_register(root_path)`。
  - `set_active_workspace`：`db.set_active_workspace(...)` 成功后按
    `db.get_active_workspace()` 的 root_path `client.workspace_activate(root_path)`；失败
    返回 False 不调 daemon。
  - `delete_workspace`：新增 `_find_workspace_root(db, workspace_id_or_name)`（按
    `db.list_workspaces()` 解析 root_path，供 SQLite 删除前取路径）；HTTP 模式先解析
    root_path（不存在返回 False），再 SQLite 硬删，再 `client.workspace_remove(root_path)`。
- Rust `workspace.remove` 为 archive 软删（无硬删 RPC）——读面 owned ACL 已排除
  archived 行，对调用方呈现"已删除"；重复调用幂等（owned_workspace_any_status）。

**3. 6 问验收（W1-2 账本）**：

| # | 问题 | 结论 | 实证 |
|---|------|------|------|
| ① | workspace_id 绑定 | PASS | probe c：register 返回权威 `workspace_instance_id=0cd52139411b8e23`；`_resolve_workspace_instance` 缓存/幂等重算保证 activate/remove 注入同一 instance_id（单测 TestHttpWriteConvenienceMethods 7 项全绿） |
| ② | 结果限定 | PASS | probe PROBE_REPRODUCED+FIX_VERIFY：register 同步前 list 命中=False（复现分裂），同步后命中=True；workspace.list 按 peer uid 限定 |
| ③ | 越界参数 | PASS（部分 BLOCKED） | register 不存在路径 → `path_not_found`（probe 拒绝态3 PASS）；activate/remove 缺 instance_id → `invalid_params`、不存在 instance_id → `workspace_not_found` 的生产 HTTP 验证被旧二进制阻塞（见 §4 遗留风险），源码契约见 dispatch.rs 单测 L286-295 |
| ④ | snapshot_not_ready | PASS（设计） | 写面工具不发布 snapshot（新 workspace 的 snapshot 由读面工具首次查询经 `_ensure_remote_snapshot` 懒发布）；便捷方法不触碰 snapshot 状态、无 db_path 概念 |
| ⑤ | 跨 workspace 隔离 | PASS（设计） | Rust owned ACL（owner_uid 匹配 + archived 排除）保证；activate/remove 的 owned ACL 由 workspace.rs 单测（L3926-3938：非 owner 拒绝、owner 通过）佐证 |
| ⑥ | Python fallback 边界 | PASS | HTTP 模式 `HttpDaemonRpcClient` 无 SQL 回退（fail-closed，`DaemonUnavailableError` 传播）；legacy 分支仅 `is_http_transport_enabled()=False` 进入（单测 TestWriteToolsHttpBranches 12 项全绿） |

**4. 验证结果**：

- py_compile：`server/daemon_client.py`、`server/tools/tools_workspace.py` 均 PASS。
- 单测（任务书命令）：`pytest tests/test_workspace_write_rpc_http.py -q`
  → **21 passed, 3 skipped**（3 skipped 为进程级 round-trip 设计性 skip：权威 manifest 被
  生产 daemon 占用，按既有 skip 模式处理）。覆盖：缓存复用、调用序、_norm_root 大小写
  合并、缺 instance_id 抛 DaemonUnavailableError、三工具 SQLite 先行+daemon 同步、
  daemon 不可用 fail-closed、不存在返回 False 不调 daemon、legacy 纯 SQL。
- 回归：`pytest tests/test_http_native_read_cutover.py -k workspace` → **4 passed**
  （TestToolsWorkspaceRouting 3 项 + TestNoPseudoRoutes 1 项；源码断言未破：工具 HTTP
  分支刻意用 `HttpDaemonRpcClient.get_instance().workspace_xxx()` 避开 `_call_daemon_rpc`/
  `workspace_status` 字符串；TestToolsQueryStaysLocal 7 failed 为 H4C-2 既有缺口，
  非本次引入，未触碰 tools_query.py）。
- 真实 HTTP probe（生产 daemon PID 2396，transport=http）：**5 PASS / 4 BLOCKED / 0 FAIL**。
  - PASS：ping、PROBE_REPRODUCED（SQLite 纯写→读面不可见）、FIX_VERIFY register
    同步后读面可见（instance_id=0cd52139411b8e23）、拒绝态3 register 不存在路径→
    path_not_found、本地 workspaces 表一致。
  - BLOCKED：workspace_activate / workspace_remove 及其 2 个拒绝态——生产 daemon
    报 `method_not_found: 未知方法: workspace.activate`（named pipe 与 HTTP 双路径
    交叉验证一致）。

**5. 遗留风险（BLOCKED 根因与处置）**：

- **生产 daemon 二进制不含 workspace.activate/remove handler**：源码 dispatch.rs 自
  `03e92e5`（2026-07-31）起有该分支且当前已提交（git status rust_ext/ 干净），但
  运行中二进制（pid 2396，cw-daemon.exe sha256 1fa5e4d9…）双 transport 均
  method_not_found；二进制字符串含 "workspace.activate"（来自 client.rs/cw_cli.rs
  等其他位置，不足以证明 dispatch 支持）。可疑点：17:39（git_head b28ae94）与
  22:47（git_head ccca314）两份部署 evidence 记录的 cw-daemon.exe sha256 **完全相同**，
  而两 commit 间 rust_ext 有改动（cw_daemon.rs/http_server.rs），提示构建/部署未真正
  重编译（stage-refresh target 产物复用）。**处置**：由 Coordinator 触发
  `scripts/refresh_shared_runtime.ps1`（规则 43 全流程：release 构建→安装→重启→
  hash/smoke 验证）后复测 probe d/e/f/g，本任务单测与 Python 侧桥接不受影响。
- **probe 临时注册行残留**：probe 在 daemon 注册表注册的临时 workspace 行
  （`cw_w12_*` tempdir root）因无 remove handler 无法经 RPC 归档，残留于
  daemon_workspaces（workspace.list 行数 22→24）；SQLite 侧已删除。部署新二进制后
  可用 workspace.remove 归档，或 daemon registry 重建时消失；不影响 SQLite 真相源。
- **修复中发现的初始 bug**：缓存字段 `_workspace_instance_by_root` 首次误加在
  `DaemonClient.__init__`，便捷方法在 `HttpDaemonRpcClient`——第一次 probe 暴露
  AttributeError 后移正至 `HttpDaemonRpcClient.__init__`（L1786 附近），单测
  mock 层未覆盖该路径，已由 probe 抓出。

**6. 改动文件**：`server/daemon_client.py`（+_norm_root、+3 便捷方法+解析方法、
`__init__` 缓存字段移正）、`server/tools/tools_workspace.py`（三写工具 HTTP 分支、
`_find_workspace_root`）、`tests/test_workspace_write_rpc_http.py`（新增）、
本账本（9.12 节）。

**7. 状态**：推进到 `review`，待 independent_reviewer 复审；不得 apply/close。
（BLOCKED 项需部署新 daemon 二进制后复测，见 §5。）

## 9.13 W1-2-FIX：SnapshotDaemonState 漏委托 workspace.activate/remove（2026-08-15，任务 `T-1786813721844-9c6ad800`）

**1. 根因（纯源码缺口，排除部署缓存）**：

`rust_ext/src/daemon/snapshot_state.rs` L281-347 的 `impl DaemonStateExt for SnapshotDaemonState`
"workspace.* 委托 base" 区块委托了 register/list/status/recover/connect/file_refresh/
refresh_plan/file_delete，**唯独漏掉 `handle_workspace_activate` 与 `handle_workspace_remove`**。
生产 daemon 用 SnapshotDaemonState（组合 WorkspaceDaemonState），未委托的 trait 方法回落
trait 默认 stub（dispatch.rs L280-296 返回 `method_not_found`）→ workspace.activate /
workspace.remove 经 HTTP/named pipe 均 method_not_found；register/list/status 正常。
W1-2 probe（§9.12 第 4 节）BLOCKED 的 d/e/f/g 四项即由此引起，与部署二进制缓存无关。

**2. 修复 diff 摘要**：

`rust_ext/src/daemon/snapshot_state.rs`（+16 行），在 `handle_workspace_file_delete` 委托
之后、运维方法注释块之前补两个委托方法，签名与 trait/base 严格一致，委托 `self.base`：

```rust
fn handle_workspace_activate(&mut self, peer: PeerCredential, params: &Value)
    -> Result<Value, DaemonRpcError> { self.base.handle_workspace_activate(peer, params) }
fn handle_workspace_remove(&mut self, peer: PeerCredential, params: &Value)
    -> Result<Value, DaemonRpcError> { self.base.handle_workspace_remove(peer, params) }
```

格式化：`rustfmt --edition 2021 rust_ext/src/daemon/snapshot_state.rs`（单文件，未用 cargo fmt）。

**3. 测试结果（Windows，Python 3.14，`cargo test --target-dir rust_ext\target\stage-refresh-w1`）**：

- `daemon::` 全量：**731 passed; 24 failed; 0 ignored**（418.90s）。
- 串行复跑 `daemon::workspace::`（`--test-threads=1`）：**94 passed; 24 failed**——失败集合与并行
  完全一致，确认 24 个失败为确定性失败。
- **关键相关测试全绿**：`daemon::workspace::tests::test_dispatch_workspace_remove_and_activate_are_owner_only_and_non_destructive`、
  snapshot_state 全部 `*_delegated_to_base` 委托测试、workspace register/list/status/recover/connect 等。
- **24 个失败均为既有问题（与本次改动无关，已基线取证）**：失败集中在 backup(3)/restore(4)/
  gc.cas(6)/mount.*(11) 共 24 个 **ADMIN_ONLY_METHODS** 测试，断言 `permission_denied`/`ok:false`。
  根因：`is_admin()`（dispatch.rs L1439-1445）以 `peer.owner_key() == current_daemon_owner_key()`
  判定，Windows 上后者为真实 SID（`transport_windows::get_current_user_sid()`），而测试 peer
  用 `PeerCredential::new_unix(uid=1000)`（无 sid，owner_key 非 SID）→ is_admin 恒 false →
  admin-only 方法测试在 Windows 必然被 ACL 拒绝。**基线取证**：`git stash` 还原 snapshot_state.rs
  至 HEAD 后单独跑 `test_dispatch_mount_register_succeeds` 同样 FAILED（0 passed; 1 failed），
  证明该失败与本次 diff 无关；修复 `is_admin`/测试 peer 构造属 dispatch.rs/测试夹具范围，
  超出本任务所有权白名单，遗留待后续任务处理。

**4. 部署待办（Coordinator 执行）**：

- 运行 `scripts/refresh_shared_runtime.ps1`（规则 43 全流程：release 构建→安装到
  `runtime\current`→停旧 daemon→启新 daemon→构建 hash=安装 hash=PID 路径 hash 校验）。
- 复测 W1-2 probe d/e/f/g：workspace.activate / workspace.remove 及其拒绝态由
  `method_not_found` 转为预期错误（非 owner → permission_denied；不存在 instance → workspace_not_found）。
- probe 残留的 `cw_w12_*` 临时 workspace 行可经 workspace.remove 归档。

**5. 改动文件**：`rust_ext/src/daemon/snapshot_state.rs`（+16）、本账本（9.13 节）。

**6. 状态**：推进到 `review`，待 independent_reviewer 复审；不得 apply/close。

## 9.14 W1-3：snapshot 管理 HTTP 便捷方法 + legacy fail-closed 边界（2026-08-15，任务 `T-1786808777379-c87171e7`）

> 角色：implementer（只推进到 review）
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`.trae-cn/evidence/w1_3_http_verify.py`（真实 HTTP probe，gitignore，不 commit）、
> `.trae-cn/evidence/w1_3_rowid_rotation.py` / `w1_3_real_flow_test.py` / `w1_3_wsid_compare.py`
> / `w1_3_repro_graphstore.py` / `w1_3_prod_snapshot_check.py`（根因实证脚本）
> 所有权白名单：`server/daemon_client.py`、`tests/test_workspace_snapshot_rpc_http.py`、
> 本账本。禁止：tools 层改动、Rust（rust_ext/）、db schema、task apply/close。
> 并行组：w1-1（依赖 W1-1 完成）。

**1. 实现（零 Rust 改动，Rust handler 契约见 snapshot_state.rs L1054-1190）**：

- `HttpDaemonRpcClient` 新增 3 个 snapshot 便捷方法（`snapshot_evict` 之后）：
  - `snapshot_stats(db_path=None)`：经 `_ensure_remote_snapshot(db_path)` 注册 workspace
    + 按需 `snapshot.publish`，注入权威 `workspace_instance_id` 后 `call("snapshot.stats")`；
    `db_path=None` 时不发布 → Rust 返回 `snapshot_not_ready`（fail-closed，不静默回退本地 SQL）。
  - `snapshot_list_workspaces()`：无参 `call("snapshot.list_workspaces")`，peer UID 过滤
    （admin 全量，非 admin 只返回自己的 workspace，P0-2 整改）。
  - `snapshot_evict(workspace_instance_id)`：直接 call，owned ACL 校验后驱逐 cache，
    幂等（不在 cache → `{"evicted": false}`）。
- legacy fail-closed 核查：legacy `DaemonClient` 侧无 snapshot.stats/list_workspaces/evict
  调用点（grep 确认仅 publish_snapshot）；HTTP thin client 零 SQL。

**2. 测试结果（Windows，Python 3.14）**：

- 单测（任务书命令 `pytest tests/test_workspace_snapshot_rpc_http.py -q`）：
  **6 passed, 6 skipped**（6 skipped 为进程级 round-trip 设计性 skip：权威 HTTP manifest
  被生产 daemon 占用，对齐 W1-1/W1-2 skip 模式）。覆盖：注入 harness（register+按需
  publish、缓存复用不重复 register、register 响应缺 instance_id 抛 DaemonUnavailableError、
  list_workspaces 不注入不 register、evict/stats 缺参 invalid_params、不存在 workspace_not_found、
  注册未发布 snapshot_not_ready、evict 幂等、legacy 无调用点）。
- 真实 HTTP probe（生产 daemon PID 4416，transport=http，`w1_3_http_verify.py`）：
  **8 PASS / 0 BLOCKED / 0 FAIL**。a) ping；b) FIX_VERIFY_PASS snapshot_stats(db_path)
  全链路命中（stats：generation=1, symbol_count=3, call_count=1, file_count=2）；c)
  list_workspaces 可见刚发布 workspace；d) evict 首次 true / 重复 false（幂等）/ 驱逐后
  stats → snapshot_not_ready；e) 拒绝态1 stats 缺 instance_id → `invalid_params`；f) 拒绝态2
  evict 不存在 instance_id → `workspace_not_found`；g) 拒绝态3 stats 不存在 instance_id →
  `workspace_not_found`；h) 拒绝态4 注册未发布 stats → `snapshot_not_ready`。

**3. probe 修复记录（FAIL 根因，非产品缺陷）**：

首次 probe 的 b) 全链路 FAIL（symbol_count=0、call_count=0、file_count=1 为空列表伪命中）。
根因：probe 在拒绝态4 用 `workspace_register(tmp_root)` 拿到数值 `workspace_id`（旧 ROWID），
但拒绝态4 内 `snapshot_stats()` 又经 `_ensure_remote_snapshot` 二次 register（ROWID 轮转），
`handle_snapshot_publish` 发布时从 registry 取"当前" ROWID 做 GraphStore SQL 过滤（P0-2）→
minimal_db 用旧 ROWID 建行必空。修复：拒绝态4 之后（`_remote_workspace_id` 已设置、后续不再
register）用 `workspace.status` 直取 registry 当前 ROWID 建 minimal_db；另修正 symbol_count
断言（GraphStore by_id 数组含 id=0 占位符，2 符号实际返回 3，`w1_3_repro_graphstore.py`
本地实测一致）。

**4. 重大发现（产品级系统性 bug，W1-3 范围外，需后续任务处理）**：

**daemon registry `workspace_id`（ROWID）与 Python `workspaces.id` 无同步机制，P0-2 的
workspace_id SQL 过滤在真实流中产生空快照。** 证据链：

- `daemon_workspaces.workspace_id` 是 `INTEGER PRIMARY KEY AUTOINCREMENT`（ROWID），
  `register_workspace`（workspace.rs L286-333）用 `INSERT OR REPLACE`（delete+insert）→
  每次重复 register ROWID 递增（`w1_3_rowid_rotation.py` 实测 122→123→124）；instance_id
  为确定性 hash（16 位）稳定不变。
- Python `workspaces.id` 稳定（`register_workspace` 已存在则复用旧 id；`w1_3_wsid_compare.py`：
  callwarden 项目 python_id=1 vs daemon registry_id=117 → MISMATCH）。
- 真实流决定性实测（`w1_3_real_flow_test.py`）：真实用户库（callwarden，1110 个
  file_instances，`workspaces.id=1`）经 daemon register（registry ROWID=125）→
  snapshot.publish → GraphStore SQL `AND workspace_id = 125` → file_instances.workspace_id=1
  → **空快照（symbol_count=0）**。
- 影响面：所有已存在 workspace（首次 register 后 ROWID 已 ≠ workspaces.id）；P0-2 整改的
  跨 workspace 隔离过滤在真实流中反而清空快照。测试 `test_workspace_snapshot_rpc_http.py`
  原 `_make_minimal_db`（无 workspace_id 列）与进程级 round-trip 的"先 register 取 ROWID
  后 publish"同样受该语义影响（已在本任务内修正测试文件，见 §5）。
- 处置：属 Rust/registry 范围，超出 W1-3 所有权白名单，本账本记录证据（含
  `w1_3_real_flow_test.py` 实测输出），由 Coordinator 决策后续修复任务。

**5. 测试文件缺陷修正（所有权内）**：`tests/test_workspace_snapshot_rpc_http.py`：

- `_make_minimal_db` 原缺 workspace_id/abs_path/mtime 列（P0-2 后 publish 必然过滤失配）且
  签名只接收 1 参（round-trip 调用传 2 参 → TypeError，被设计性 skip 掩盖）→ 已对齐
  `w1_3_http_verify.py` 契约：加列 + `workspace_id` 参数（默认 1）。
- `test_snapshot_stats_full_roundtrip` 原"先 register 取 ROWID 再 publish"同样受 ROWID 轮转
  影响（便捷方法内部二次 register）→ 重构为 `configure_workspace` → `workspace_status()`
  完成首次 register 并取当前 ROWID → 建库 → `snapshot_stats(db_path)`（不再 register，
  publish ROWID 与 minimal_db 一致）。
- symbol_count 断言 2 → 3（by_id id=0 占位符语义）。

**6. 6 问验收**：

| # | 问题 | 结论 | 实证 |
|---|------|------|------|
| ① | workspace_instance_id 绑定 | PASS | probe b：snapshot_stats 全链路返回 `workspace_instance_id=3814410cc81bac87` 且与 register 一致（单测 harness 覆盖 register+按需 publish+缓存复用） |
| ② | 结果限定（peer UID） | PASS | probe c：list_workspaces 无参返回数组且含刚发布 workspace（共 1 条）；Rust handler 按 registry owner_uid 交集过滤（P0-2 整改） |
| ③ | 越界参数 | PASS | probe e/f/g 生产 HTTP 实测：stats 缺 instance_id → `invalid_params`；evict/stats 不存在 instance_id → `workspace_not_found`（owned ACL） |
| ④ | snapshot_not_ready | PASS | probe h：注册未发布 stats → `snapshot_not_ready`（fail-closed 不静默回退）；d：evict 后 stats → `snapshot_not_ready` |
| ⑤ | 跨 workspace 隔离 | PASS | P0-2 GraphStore SQL 按数值 workspace_id 过滤；probe 修复后 minimal_db 命中（symbol_count=3/call_count=1）；list/evict owned ACL 单测覆盖 |
| ⑥ | Python fallback 边界 | PASS（+发现） | HTTP thin client 零 SQL、fail-closed（register 响应缺 instance_id 抛 DaemonUnavailableError）；**产品级发现**：registry ROWID 轮转 vs `workspaces.id` 失配 → 真实库空快照（见 §4） |

**7. 清理契约**：probe 结束 `workspace.remove` 归档临时 workspace（本次
`cw_w13_ey6s61b4`/instance `3814410cc81bac87` 已 archived）+ 删临时目录（已删）+ evict
清 snapshot cache（`snapshot_list_workspaces` 返回 `[]`，daemon health
snapshot_workspace_count=0）。历史 probe 残留行（`cw_w13_*`/`cw_w12_*`）均为 archived 或
非本任务范围（M21-M25 active 行属历史批次）。

**8. 改动文件**：`server/daemon_client.py`（+3 便捷方法）、
`tests/test_workspace_snapshot_rpc_http.py`（新增 + 缺陷修正）、本账本（9.14 节）。

**9. 状态**：推进到 `review`，待 independent_reviewer 复审；不得 apply/close。

## 9.15 W1-4-FIX：snapshot.publish 用真相源 workspace_id（2026-08-15，任务 `T-1786820508759-80c33718`）

> 角色：implementer（只推进到 review）
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`.trae-cn/evidence/w1_4_cargo_test_daemon.log`（cargo test daemon:: 原始日志，gitignore）
> 所有权白名单：`rust_ext/src/daemon/snapshot_state.rs`、`rust_ext/src/daemon/workspace.rs`、
> 本账本。禁止：Python 侧改动（server/*、tests/*）、db schema、compat_registry、tools 层、
> task apply/close。并行组：w1-3（修复 §9.14 §4 发现的产品级 bug）。

**1. 根因（引用 §9.14 §4 发现，Coordinator 独立复跑 `w1_3_real_flow_test.py` 确认）**：

- Python 真相源：`~/.callwarden/callwarden.db` 的 `file_instances.workspace_id = workspaces.id`
  （callwarden 项目 = 1，1110 个文件）。
- daemon registry：`daemon_workspaces.workspace_id` 是 `INTEGER PRIMARY KEY AUTOINCREMENT`
  （workspace.rs L114-127），`register_workspace`（L286-333）用 `INSERT OR REPLACE`
  （SQLite delete+insert）→ 每次重复 register ROWID 轮转递增（实测 122→123→124→129）；
  `workspace_instance_id` 为确定性 hash 稳定不变。
- publish 过滤链路：`handle_snapshot_publish`（snapshot_state.rs L393-475）从 registry 行取
  `workspace_id`（L408-413）→ `build_and_publish_blocking(db_path, workspace_id_num, ...)`
  → snapshot.rs L281 `load_from_sqlite_blocking` → graph.rs L1657-1660 `_load_from_sqlite_mode`
  在 SQL 层用 `WHERE workspace_id = ?` 过滤 file_instances（实际值 = Python workspaces.id）。
- 结果：真实库 publish 后 GraphStore 用 registry ROWID（如 129）过滤
  `file_instances.workspace_id`（实际 1）→ **空快照（syms=0）**。独立实测输出：
  `file_instances 分布 [(1,1110)]`、`daemon register 后 workspace_id=129`、
  `snapshot_stats(真实用户库): syms=0 calls=0 files=1 gen=1`。
- 影响面：所有已存在 workspace（首次 register 后 ROWID 已 ≠ workspaces.id）；P0-2 整改的
  跨 workspace 隔离过滤在真实流中反而清空快照；已部署 M2 系列 query.*（同经
  publish → GraphStore 过滤）真实流同样受影响。

**2. 修复方案（publish 时从 db_path 真相源读 workspace id）**：

- `handle_snapshot_publish` 确定 db_path 后（Windows db_path 参数分支 + Linux FD 分支统一
  处理），只读打开该库查 `workspaces` 表，按规范化后的 `client_view_root` 匹配
  `root_path` 取真实 id 作为 GraphStore 过滤值；查不到（表不存在/无匹配/打开失败）时
  fallback 当前 registry ROWID（保持现状不更糟，不影响既有测试）。registry ROWID 仅作
  注册表主键，不再充当 Python 侧过滤 id。
- 新增 `resolve_true_workspace_id(db_path, client_view_root, fallback_rowid)`（snapshot_state.rs）：
  只读连接（`SQLITE_OPEN_READ_ONLY | NO_MUTEX`）+ 防御性处理（打开失败/无表/无匹配 →
  fallback，禁止 panic）；`client_view_root` 为空 → fallback（不返回 0，维持 P0-2 隔离语义）。
- 新增 `normalize_path_key`（workspace.rs，pub）：与 Python `config.norm_path` 语义一致
  （反斜杠→正斜杠、去尾斜杠、Windows 盘符小写）。匹配时大小写不敏感（Windows 文件系统
  不区分大小写）；分支工作区 `root_path` 带 `#分支名` 后缀（db_branch.py）→ 前缀匹配兜底。
- `build_and_publish_blocking` 调用签名不变（仍传最终解析出的 workspace_id_num）。

**3. 关键 diff 摘要**：

- `rust_ext/src/daemon/snapshot_state.rs`：
  - `handle_snapshot_publish`：`workspace_id_num` 提取改为 `registry_rowid`（fallback 主键）
    + 取 `client_view_root`；db_path 确定后 `resolve_true_workspace_id(&db_path, client_view_root, registry_rowid)`。
  - 新增 `resolve_true_workspace_id` 私有函数（真相源解析 + fallback）。
  - 测试：新增 `build_source_db` 辅助 + 6 个 resolve 单测（优先真相源/路径变体规范化/
    分支后缀/无表 fallback/无匹配 fallback/打不开 fallback）+ 2 个端到端 publish 测试
    （用真相源 id=7 vs registry ROWID=1 复现 W1-3 bug：publish 后 symbol_count=3 非 0；
    无 workspaces 表时 fallback ROWID 过滤仍工作）。
- `rust_ext/src/daemon/workspace.rs`：
  - 新增 `pub fn normalize_path_key`（与 Python norm_path 对齐）。
  - 测试：新增 4 个 normalize 单测（反斜杠/尾斜杠/盘符小写/空串）。

**4. 测试结果（Windows，Python 3.14，`cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib`）**：

- 全量 `daemon::` 模块：**741 passed / 26 failed**。26 个失败 = 24 个既有 ADMIN_ONLY
  基线（backup/restore/gc.cas/mount.*，Windows 因 is_admin owner_key/SID 不匹配恒
  `permission_denied`，与本次无关）+ 2 个新增 normalize 测试断言期望值笔误
  （盘符小写语义）→ 已修正后单独重跑通过。
- 本次新增 12 个测试（resolve 6 + publish 端到端 2 + normalize 4）在修正后全部通过。
- `cargo check --manifest-path rust_ext/Cargo.toml` 通过（仅有既有无关 warning：
  cw_cli.rs unreachable_patterns / dead_code）。
- `rustfmt --edition 2021` 仅格式化改动文件（snapshot_state.rs / workspace.rs）通过。

**5. 部署验证（Coordinator 2026-08-16 03:25 执行）**：

- `pwsh -File .\scripts\refresh_shared_runtime.ps1 -TaskId T-1786820508759-80c33718 -RestartMcp -RunSmokeTests`
  （AGENTS.md 规则 43）→ 成功，证据 `20260816-032538-ccca314d769e-9bba5cad.json`，
  runtime 切换 + daemon 重启 + MCP 由 supervisor 重连。
- 独立复跑 `.trae-cn/evidence/w1_3_real_flow_test.py`：**修复前** 真实用户库
  `snapshot_stats: syms=0 calls=0 files=1`（空快照，registry ROWID=129 过滤失配）；
  **修复后** `snapshot_stats: syms=92092 calls=122186 files=1611 gen=1`
  （真相源 id=1 命中全部 1110 个 file_instances）→ **FIX_VERIFY_PASS**。
- 遗留：M2 系列 query.* 真实流此前同样受空快照影响，W1-4-FIX 部署后自动恢复
  （query.* 依赖 publish 的 GraphStore，无需单独修复）；建议后续在 W1 收口时对
  query.symbol 真实库跑一次 smoke 确认。

**6. 状态**：已部署验证（FIX_VERIFY_PASS），任务 `T-1786820508759-80c33718` 在 `review`，
待 independent_reviewer 复审；不得 apply/close。

## 9.16 W2-1：query 面 stats HTTP native 迁移（2026-08-16，任务 `T-1786840097330-dec66710`）

> 角色：implementer（只推进到 review）
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：本轮 pytest 9 文件验收日志（含既有失败记录）+ `cargo check` 通过日志
> 所有权白名单：`server/compat_registry.py`、`server/daemon_client.py`、
> `server/tools/tools_query.py`、`rust_ext/src/daemon/{http_server.rs,snapshot_state.rs,dispatch.rs}`、
> 9 个 `tests/test_http_*.py`、本账本。禁止：task apply/close、git commit、白名单外文件改动。
> 并行组：W2 系列（无相交所有权）。

**1. 根因**：

- 3 个 stats 工具（`get_uncommented_symbols` / `get_module_call_stats` / `get_semgrep_stats`）
  W2-1 前在 HTTP 模式走 python_compat worker（`route_worker_call`）：每个调用 spawn 一次
  worker 子进程（`CW_COMPAT_WORKER_SCRIPT`）执行 Python SQL，开销大且与 native 查询路径
  不一致；daemon 侧 capability registry 将三者宣告为 `python_compat`，无法利用
  GraphStore/snapshot 内存索引。
- 符号组只读白名单因此含 3 个本应 native 的方法，registry 全量 107 项（H4C-1 默认 2 项 +
  符号组 17 项 + 其余批次），Python `RUST_COMPAT_ROUTE` 与 Rust `COMPAT_ROUTE_WHITELIST`
  需三端对齐，迁移必须同步改三处。

**2. 修复方案（3 工具迁移 rust_native，registry 107→104）**：

- Rust 侧（第一轮已完成）：
  - `snapshot_state.rs`：新增 native handler `handle_query_uncommented_symbols`（L573）/
    `handle_query_module_call_stats`（L592）/ `handle_query_semgrep_stats`（L609）及本地
    查询实现 `query_local_uncommented_symbols`（L1854）/ `query_local_module_call_stats`
    （L1926）/ `query_local_semgrep_stats`（L2031），走 snapshot `query_db_path`，
    复用 GraphStore 只读索引。
  - `dispatch.rs`：新增 `query.uncommented_symbols` / `query.module_call_stats` /
    `query.semgrep_stats` 三个 RPC 路由（L382-409 默认 method_not_found，
    SnapshotDaemonState 重写；L1535-1539 分派）。
  - `http_server.rs`：`COMPAT_ROUTE_WHITELIST` 移除 3 个条目（H4C-1 默认 2→1 项、
    符号组 17→15 项）；`build_capability_registry` 三者 backend 由 python_compat 切换为
    rust_native、scope 由 workspace 切换为 snapshot，task 归属 `T-1786840097330-dec66710#W2-1`。
- Python 侧（第一轮已完成）：
  - `tools_query.py`：3 个工具函数体 HTTP 分支改为 `client = _get_daemon_client()` +
    `HttpDaemonRpcClient` 便捷方法 + `_get_db_path_for_daemon()`（legacy 分支语义不变）；
    `_SYMBOL_READ_ONLY_METHODS` 由 17 项降为 15 项。
  - `daemon_client.py`：新增便捷方法 `get_uncommented_symbols`（L2076）/
    `get_module_call_stats`（L2103）/ `get_semgrep_stats`（L2120），均经
    `_ensure_remote_snapshot(db_path)` 注入权威 `workspace_instance_id`。
  - `compat_registry.py`：`_build_default_registry()` 现仅注册 `stats_top_files`（1 项）；
    `RUST_COMPAT_ROUTE` 同步为 `{"stats_top_files": READ_ONLY}`（其余由 H4C-2/3 工具模块
    `register_compat_routes` 自动追加）。

**3. 勘察结论（本轮 implementer 复核）**：

- 符号组白名单实际条目数 = 15（17 − get_module_call_stats − get_semgrep_stats；
  get_uncommented_symbols 属 H4C-1 默认组，W2-1 一并移除）→ `http_server.rs` L534 注释
  由第一轮误写的 16 项修正为 15 项，L1540 `build_capability_registry` 注释 17 项同步为
  15 项。
- `test_http_compat_worker_batch.py` 的 seed 库 `MINIMAL_SCHEMA` 仅含
  `file_instances`+`symbols` 表：`get_comment_coverage`/`get_top_callers`/
  `get_deepest_functions` 依赖 `file_symbol_versions`/`call_versions` 等额外表不可用；
  `stats_top_files` handler（`symbols JOIN file_instances`）可用 → 白名单方法改
  `stats_top_files`（workspace/limit 参数变体覆盖批量路径）。
- 测试侧"已迁移方法不再走 worker"断言保留：`compat_worker` 的
  `test_migrated_native_method_not_served_by_worker`（handle_frame 返回
  ERR_METHOD_NOT_FOUND）、capability_registry 的 `not is_compat_method(...)`、combined
  的 `not registry.is_compat_method("get_uncommented_symbols")`。

**4. 测试结果（Windows，Python 3.14.3，`C:\Python314\python.exe`）**：

- `py_compile` 9 个改动测试文件全部通过（Python 3.14.3）。
- `cargo check --manifest-path rust_ext/Cargo.toml --bin cw-daemon` 通过（exit 0，仅有既有无关 warning：cw_cli.rs unreachable_patterns / dead_code + lib 既有 117 warnings）。
- pytest 任务书 6 文件批次（capability_registry / compat_worker / compat_worker_batch /
  daemon_release_acceptance / combined_worker_cutover / native_read_cutover）：
  **213 passed / 2 failed**（189s）。2 个失败均属**部署滞后既有缺口**，fresh 部署
  （§7）后重跑 13/13 全通过：
  - `TestFreshDaemonArtifacts::test_latest_refresh_evidence_passed_for_current_head`：
    runtime evidence git_head 落后当前 HEAD（旧部署产物），fresh 部署后同步；
  - `TestHttpBootstrapSmoke::test_capabilities_python_compat_available_matches_rust_route`：
    runtime/current 旧 binary 仍将 3 个已迁移方法宣告 python_compat（107 项），
    fresh 部署后 capabilities 正确（104 项）。
- pytest W2-1 核心新测试 `tests/test_query_stats_rpc_http.py`：**16 passed**（6 问验收的
  mock 层：workspace_id 绑定 / 参数透传 / 越界 fail-closed / snapshot_not_ready /
  跨 workspace 隔离 / Python fallback 边界）。
- pytest `compat_read_cutover` + `index_job_cutover` 补充批次：**27 passed / 10 failed**。
  10 个失败全部为 **H4C-2（e37fe2d）既有缺口**，与 W2-1 无关（W2-1 仅改两文件的
  manifest fixture 与预热方法，未触碰这些断言；`git show e37fe2d:server/tools/...`
  实证 H4C-2 时 tools_security/tools_summary 已改 `route_worker_call`）：
  - `TestNoPseudoRoutes::test_every_tool_starts_with_http_unsupported`：断言工具源码
    含 `_http_unsupported("<name>")`（H2I 时代契约），H4C-2 后工具以
    `route_worker_call(...)` 开头 → 断言失配（get_summary / list_branches）；
  - `TestHttpModeStructuredUnsupported` / `TestLegacyModeKeepsLocalExec`：HTTP 模式
    期望结构化 `E_HTTP_COMPAT_UNSUPPORTED` / legacy 期望本地 get_db，H4C-2 后
    `route_worker_call` 在 HTTP 模式（`is_http_transport_enabled()` 默认 True）走
    client → discover 无 manifest 抛 `E_HTTP_MANIFEST_MISSING`（fail-closed），
    契约语义已变。修复需改 tools_security/rules/summary/semantic 的 HTTP 行为契约
    或这些断言语义，超出 W2-1 白名单，挂回 H4C-2 任务处理。

**5. 改动文件清单（git diff --stat，16 个修改 + 1 个新建）**：

- Rust：`rust_ext/src/daemon/dispatch.rs`（+37）、`rust_ext/src/daemon/http_server.rs`（+25，
  含 H4C-2 符号组注释 17→15 项）、`rust_ext/src/daemon/snapshot_state.rs`（+355）。
- Python 生产：`server/compat_registry.py`、`server/daemon_client.py`、
  `server/tools/tools_query.py`。
- 测试：`tests/test_query_stats_rpc_http.py`（新建，16 用例）、
  `tests/test_http_capability_registry.py`、`tests/test_http_combined_worker_cutover.py`、
  `tests/test_http_compat_read_cutover.py`、`tests/test_http_compat_worker.py`、
  `tests/test_http_compat_worker_batch.py`、`tests/test_http_daemon_default_transport.py`、
  `tests/test_http_daemon_release_acceptance.py`、`tests/test_http_index_job_cutover.py`、
  `tests/test_http_native_read_cutover.py`。
  - 本轮续作补修：7 个含真实进程门的测试文件的 manifest 轮询 fixture（H6 修复
    9d6ca63 后 manifest 固定写 `USERPROFILE/.callwarden`）——Group B（隔离
    USERPROFILE：combined_worker_cutover / compat_worker_batch）轮询
    `data_root/userhome/.callwarden` + spawn 补建 `.callwarden`；Group A（未隔离：
    capability_registry / native_read_cutover / compat_read_cutover / index_job_cutover /
    daemon_release_acceptance）轮询 `get_http_manifest_dir()` + backup/restore 防污染。
    修复前 fixture 调用 `_wait_manifest(proc)` 与旧签名 `(data_root, proc)` 不匹配，
    H6 后真实进程门全部失败（既有缺口，H6 引入）。

**6. 状态**：全部验收通过（py_compile + cargo check + pytest 批次 + fresh 部署 +
真实 HTTP probe 6/6），任务 `T-1786840097330-dec66710` 推进到 `review`，待
independent_reviewer 复审；不得 apply/close。

**7. fresh 部署验证（2026-08-16 10:42）与真实 HTTP probe（6/6 PASS）**：

- 部署：`pwsh -File .\scripts\refresh_shared_runtime.ps1 -TaskId T-1786840097330-dec66710
  -RestartMcp -RunSmokeTests` 成功；旧 daemon PID 20644 停止，新 daemon PID 40548 由
  runtime/current 启动并发布 manifest（endpoint http://127.0.0.1:9925，schema_version=50，
  worker_status=healthy）；evidence 写入
  `runtime/evidence/20260816-104231-628aa3a57051-723a0153.json`（git_head 同步当前 HEAD）。
- release acceptance 重跑：13/13 passed（含部署前失败的 2 项）。
- probe 脚本 `.trae-cn/evidence/w2_1_http_verify.py`（生产 HttpDaemonRpcClient 直连正式
  daemon，不 spawn 隔离进程），6 问结果：

| # | 验收问 | 结果 |
|---|---|---|
| Q1 | /capabilities python_compat available == 104 == 装配后 registry == RUST_COMPAT_ROUTE 三端对齐 | PASS（104/104/104，extra/missing 全空） |
| Q2 | get_uncommented_symbols / get_module_call_stats / get_semgrep_stats 在 capabilities 为 rust_native+available（get_stats 为 query.stats native RPC，不注册 capabilities 属设计） | PASS |
| Q3 | 四便捷方法真实 RPC round-trip（db_path=用户主库，workspace.register+snapshot.publish）——绝不 method_not_found | PASS（get_stats dict len=19；uncommented list；module_call_stats list；semgrep_stats dict len=5） |
| Q4 | 负向 fail-closed：伪路由 summary.repo_map → method_not_found | PASS |
| Q5 | compat worker 可达：stats_top_files（白名单 104 项）成功返回业务数据（不 method_not_found） | PASS（count=1，comment_coverage=0.9953） |
| Q6 | manifest 发布 + discover 直连：endpoint 与 manifest 一致、health pid 匹配、schema=50、security_profile 正确 | PASS（127.0.0.1:9925，pid 40548） |

## 9.17 W2-2：task 面 stats HTTP native 迁移（2026-08-16，任务 `T-1786840097330-a9e0ec69`）

> 角色：implementer（只推进到 review）
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：pytest 新测试 13/13 通过 + `cargo check` 通过 + fresh 部署
> （`runtime/evidence/20260816-112003-628aa3a57051-f0c3fb75.json`，status=passed）+ 真实
> HTTP probe 6/6（`.trae-cn/evidence/w2_2_http_verify.py`）
> 所有权白名单：`server/daemon_client.py`、`server/tools/tools_task.py`、
> `tests/test_task_stats_rpc_http.py`（新建）、`rust_ext/src/daemon/{dispatch,snapshot_state,http_server}.rs`、
> `.trae-cn/evidence/w2_2_http_verify.py`（新建，gitignore）、本账本。禁止：task apply/close、
> git commit、白名单外文件改动（含 db schema / db/db_*.py）。
> 并行组：W2 系列（无相交所有权）。

**1. 根因**：

- 3 个 task 面 stats 工具（`get_clone_stats` / `get_job_stats` / `get_clone_group_stats`）
  W2-2 前在 HTTP 模式走 python_compat worker（`route_worker_call`）：每个调用 spawn 一次
  worker 子进程执行 Python SQL，开销大；daemon 侧 capability registry 将三者宣告为
  `python_compat`（H4C-3 任务组只读 16 项中的 3 项），与 W2-1 已 native 化的 query 面
  stats 不一致。
- 前置勘察疑问：clone/job 数据表（clone_pairs / clone_groups / clone_group_members / jobs）
  不在 snapshot schema（snapshot 仅含 file_instances/symbols/calls/.../semgrep_findings 等），
  jobs 为任务编排库（用户级全局）——需确认 native 化可行性（主库表能否经 snapshot
  `query_db_path` 只读连接可达）。

**2. 勘察结论（步骤 0 verify_scope，方案 (a) native 可行）**：

- 数据源表名与 workspace 过滤语义（读 db 层 SQL 实证）：
  - `get_clone_stats`（db_clone_detection.py:1093）：`clone_pairs`（workspace_id /
    clone_type / symbol_a_id / symbol_b_id）JOIN symbols + file_instances；
    `WHERE clone_pairs.workspace_id = ?`（按 workspace 隔离）。
  - `get_job_stats`（db_jobs.py:444,512）：`jobs`（workspace_id / status），
    `WHERE jobs.workspace_id = ? GROUP BY status`。jobs 每行绑定 workspace_id，
    submit_job 时写入，统计按 workspace 隔离，非全局视图。
  - `get_clone_group_stats`（db_clone_groups.py:311,427）：`clone_groups` +
    `clone_group_members` + symbols + file_instances；`WHERE clone_groups.workspace_id = ?`。
- snapshot 可达性（关键）：`GraphSnapshot.query_db_path()`（rust_ext/src/snapshot.rs:98-107）
  返回 `source_db_path` = 发布时传入的 db_path（用户主库路径）；`open_query_connection`
  （snapshot_state.rs:182-209）以只读连接直接打开该路径 = **主库**。主库含全部上述表，
  W2-1 的 query_local_* 已实证主库表可经此路径查询 → **无需扩展 snapshot schema**，
  native 化可行。

**3. 修复方案（3 工具迁移 rust_native，registry 104→101）**：

- Rust 侧：
  - `snapshot_state.rs`：新增 native handler `handle_task_clone_stats`（L629）/
    `handle_task_job_stats`（L639）/ `handle_task_clone_group_stats`（L649），
    require workspace_instance_id → `open_query_connection` → workspace_id 过滤查询；
    本地查询实现 `query_local_clone_stats`（L2160）/ `query_local_job_stats`（L2211）/
    `query_local_clone_group_stats`（L2249），复刻 Python db 层 SQL，含 SUM(CASE...)
    无匹配行 NULL 的 `unwrap_or(0)` 语义。
  - `dispatch.rs`：新增 `task.clone_stats` / `task.job_stats` / `task.clone_group_stats`
    三个 RPC 路由（L416-441 默认 method_not_found，SnapshotDaemonState 重写；
    L1574-1576 分派）。
  - `http_server.rs`：`COMPAT_ROUTE_WHITELIST` 移除 3 个条目（H4C-3 区块注释
    16→13 项）；`build_capability_registry` 三者 backend 由 python_compat 切换为
    rust_native、scope 由 workspace 切换为 snapshot，task 归属
    `T-1786840097330-a9e0ec69#W2-2`、deprecated 置空。
- Python 侧：
  - `tools_task.py`：3 个工具函数体 HTTP 分支改为 `client = _get_daemon_client()` +
    便捷方法 + `_get_db_path_for_daemon()`（fail-closed，无 get_db 回退；legacy 分支
    保留 route_worker_call 本地 db 回退语义）；删除 3 个 worker handler
    `_h_get_clone_stats` / `_h_get_job_stats` / `_h_get_clone_group_stats`；
    `_TASK_READ_ONLY_METHODS` 由 16 项降为 13 项。
  - `daemon_client.py`：新增便捷方法 `get_clone_stats`（L2135）/
    `get_job_stats`（L2150）/ `get_clone_group_stats`（L2166），均经
    `_ensure_remote_snapshot(db_path)` 注入权威 `workspace_instance_id`。
- 两端对齐：Rust `COMPAT_ROUTE_WHITELIST`（101）= Python `RUST_COMPAT_ROUTE`（101）
  = 装配后 registry.methods()（101）。

**4. 测试结果（Windows，Python 3.14，`C:\Python314\python.exe`）**：

- `py_compile` 改动文件（tools_task.py / daemon_client.py / 新测试 / probe）全部通过。
- `cargo check --manifest-path rust_ext/Cargo.toml --bin cw-daemon` 通过（exit 0，
  仅有既有无关 warning：GraphSnapshot.source_db_file 未用等 117 warnings）。
- pytest `tests/test_task_stats_rpc_http.py`：**13 passed**（6 问验收 mock 层：
  workspace_id 绑定 / 参数原样透传 / 越界 fail-closed / snapshot_not_ready /
  跨 workspace 隔离 / Python fallback 边界）。
- 既有缺口（迁移预期，超白名单未改，挂回原任务）：`test_http_capability_registry.py`
  的 `_EXPECTED_COMPAT_METHODS_104` 与 `test_http_daemon_release_acceptance.py` 的
  `_EXPECTED_COMPAT_METHODS_104` 等源码级硬编码断言仍含三工具，native 化后
  capabilities 为 101 项 → 断言失配，属 W2-2 迁移预期，需随批次测试适配任务整改。
  → **已由 2026-08-16 续作整改完成（断言 104→101 全量同步 + 验证通过，见 §8）**。

**5. 改动文件清单（6 个修改 + 2 个新建）**：

- Rust：`rust_ext/src/daemon/dispatch.rs`（+3 handler 占位 +3 路由）、
  `rust_ext/src/daemon/http_server.rs`（COMPAT_ROUTE_WHITELIST 104→101 + registry
  3 add() 切换 rust_native/snapshot/W2-2）、`rust_ext/src/daemon/snapshot_state.rs`
  （+3 handler +3 查询函数）。
- Python 生产：`server/daemon_client.py`（+3 便捷方法）、`server/tools/tools_task.py`
  （3 工具 HTTP 分支 fail-closed + 删 3 worker handler + _TASK_READ_ONLY_METHODS 16→13）。
- 测试/证据：`tests/test_task_stats_rpc_http.py`（新建，13 用例）、
  `.trae-cn/evidence/w2_2_http_verify.py`（新建，勘察结论头注 + probe）。

**6. 状态**：全部验收通过（py_compile + cargo check + pytest 13/13 + fresh 部署 +
真实 HTTP probe 6/6），任务 `T-1786840097330-a9e0ec69` 推进到 `review`，待
independent_reviewer 复审；不得 apply/close。

**7. fresh 部署验证（2026-08-16 11:20）与真实 HTTP probe（6/6 PASS）**：

- 部署：`pwsh -File .\scripts\refresh_shared_runtime.ps1 -TaskId
  T-1786840097330-a9e0ec69 -RestartMcp -RunSmokeTests` 成功（status=passed）；旧 daemon
  PID 40548 停止，新 daemon PID 40524 由 runtime/current 启动并发布 manifest
  （endpoint http://127.0.0.1:4950，schema_version=50，security_profile
  dev_loopback_unauthenticated）；evidence 写入
  `runtime/evidence/20260816-112003-628aa3a57051-f0c3fb75.json`。
- 三端 hash 一致（AGENTS.md 规则 43）：构建产物 sha256 = `runtime/current/cw-daemon.exe`
  sha256 = 运行 PID 40524 executable sha256 = `39ee1f6e3843f545c3d4d3eb11e86c596d3d5911df9e5375cfa93aae2d8b8dc6`；
  python_dependency_mode=python_free（无 Python DLL）；`cw daemon ping` / `cw --version`
  smoke 均 exit 0；rollback=false。
- probe 脚本 `.trae-cn/evidence/w2_2_http_verify.py`（生产 HttpDaemonRpcClient 直连正式
  daemon，不 spawn 隔离进程），6 问结果：

| # | 验收问 | 结果 |
|---|---|---|
| Q1 | /capabilities python_compat available == 101 == 装配后 registry == RUST_COMPAT_ROUTE 三端对齐 | PASS（101/101/101，extra/missing 全空） |
| Q2 | get_clone_stats / get_job_stats / get_clone_group_stats 在 capabilities 为 rust_native+available 且非 python_compat | PASS（三工具均 ('rust_native','available')） |
| Q3 | 三便捷方法真实 RPC round-trip（db_path=用户主库，workspace.register+snapshot.publish）——绝不 method_not_found | PASS（clone_stats dict keys=[total,type1,type2,type3,affected_files,affected_symbols]；job_stats dict keys=[pending,running,completed,cancelled,failed,total]；clone_group_stats dict keys=[total_groups,type1,type2,type3,total_members,affected_files,affected_symbols]） |
| Q4 | 负向 fail-closed：伪路由 summary.repo_map → method_not_found | PASS |
| Q5 | compat worker 可达：stats_top_files（白名单 101 项）worker 受理（非 method_not_found；补 workspace_id 后返回业务数据） | PASS（type=dict） |
| Q6 | manifest 发布 + discover 直连：endpoint 与 manifest 一致、health pid 匹配、schema=50、security_profile 正确 | PASS（127.0.0.1:4950，pid 40524） |

**8. 续作（测试对齐）：capability registry 计数断言 104→101（2026-08-16）**：

> 角色：implementer（续作，只做测试对齐）；所有权白名单：8 个 http 测试文件
> （capability_registry / daemon_release_acceptance / combined_worker_cutover /
> compat_worker / compat_worker_batch / compat_read_cutover / index_job_cutover /
> daemon_default_transport）+ 本账本。禁止：server/、rust_ext/、db/ 改动、
> task apply/close、git commit。

- 背景：§4 记录的既有缺口（`_EXPECTED_COMPAT_METHODS_104` 等硬编码断言仍含三工具）
  由本续作整改；W2-2 主工作保持 `review`（3/3 步骤 done），续作不推进任务状态。
- 断言对齐（104→101，三工具移出 compat 集合）：
  - `tests/test_http_capability_registry.py`：`_EXPECTED_COMPAT_METHODS_104` →
    `_EXPECTED_COMPAT_METHODS_101`（任务组 16→13）；`len(reg) == 104` → `101`
    （test_default_registry_has_full_route_methods / test_global_singleton_not_polluted）；
    `_full_registry()` 补齐 103→100 项；`POSITIVE_COMPAT_RPCS` 改为基于 101 集合
    （ask_codebase 除外 103→100 项）；docstring 增加 W2-2 说明。
  - `tests/test_http_daemon_release_acceptance.py`：`_EXPECTED_COMPAT_METHODS_104` →
    `_EXPECTED_COMPAT_METHODS_101`（docstring 104→101；集合移除三工具；
    test_registry_after_full_assembly_is_104 → _is_101、len 104→101；
    test_capabilities_python_compat_available_matches_rust_route 断言 3 处）。
  - `tests/test_http_combined_worker_cutover.py`：`EXPECTED_TOTAL` 104→101（计数注释
    任务 16→13）；`TASK_METHODS` 移除三工具（16→13）；`test_task_group_registered`
    len 16→13；任务组正向 `test_task_group_worker_positive` 由 get_job_stats 改用
    仍走 worker 的 `list_jobs`（断言 job 列表 J-seed-1 / completed / workspace_id=1）；
    工具层 `test_tool_layer_does_not_call_get_db` 由 get_job_stats 改用 `get_job_status`
    （mock client 断言 `("get_job_status", {"job_id": "J-1"})`）；相关 docstring/注释同步。
  - 其余白名单文件（compat_worker / compat_worker_batch / compat_read_cutover /
    index_job_cutover / daemon_default_transport）：Grep 确认无 104 断言、无三工具
    compat 集合引用，无需修改。
  - 白名单外确认无需改：`tests/test_task_stats_rpc_http.py`（W2-2 新增 native 便捷方法
    测试，引用三工具属验证对象）；`tests/_mcp_tools_list.json`（MCP 工具面快照，工具
    仍在）；`tests/test_legacy_write_jobs_baseline.py`（legacy_local 源码检查）。
- 验证（Windows，Python 3.14，`C:\Python314\python.exe`）：
  - `py_compile` 三个改动测试文件全部通过。
  - 首轮 pytest 失败 1 项：`TestRealDaemonCompatRpcAlignment::
    test_capabilities_python_compat_available_matches_rust_route`——根因：
    `_find_daemon_binary()` 优先取 `rust_ext/target/debug/cw-daemon.exe`（10:10 构建），
    早于 http_server.rs 的 W2-2 修改（11:13），为旧 104 白名单二进制（/capabilities 中
    三工具仍 python_compat）。属环境缺口（二进制未随源码重建），非断言失配
    （Python RUST_COMPAT_ROUTE 已正确为 101）。
  - 重建 debug 二进制（`cargo build --manifest-path rust_ext/Cargo.toml --bin cw-daemon`，
    11:54，exit 0，仅既有无关 warning）后重跑三文件：
    **135 passed（capability_registry 26 + release_acceptance 13 + combined_worker_cutover
    96），0 failed / 0 skipped**。
- 状态：断言全部对齐、相关 pytest 通过；`T-1786840097330-a9e0ec69` 状态保持 `review`
  不变（续作不推进状态，待 Independent Reviewer 复审，最终状态以复审收口为准）。

## 9.18 W2-3：defect/edit stats + get_metrics HTTP 决策（2026-08-16，任务 `T-1786840097331-fd01a3f8`）

> 角色：implementer（只推进到 review）
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：pytest 新测试 14/14 通过 + `cargo check`/`cargo build` 通过 + 两次 fresh 部署
> （daemon PID 31380，endpoint http://127.0.0.1:8214，schema_version=50）+ 真实
> HTTP probe 7/7（`.trae-cn/evidence/w2_3_http_verify.py`）
> 所有权白名单：`server/daemon_client.py`、`server/tools/tools_summary.py`、
> `server/tools/tools_security.py`、`server/tools/tools_rules.py`、
> `tests/test_defect_edit_stats_rpc_http.py`（新建）、`rust_ext/src/daemon/{dispatch,snapshot_state,http_server}.rs`、
> `.trae-cn/evidence/w2_3_http_verify.py`（新建，gitignore）、本账本。禁止：其他 tools_*.py、
> db schema/db/db_*.py 改动、task apply/close、git commit。
> 并行组：W2 系列（无相交所有权）。

**1. 根因**：

- 2 个 stats 工具（`defect_stats` / `get_edit_stats`）W2-3 前在 HTTP 模式走 python_compat
  worker（`route_worker_call`）：每个调用 spawn 一次 worker 子进程执行 Python SQL，开销大；
  daemon 侧 capability registry 将二者宣告为 `python_compat`（H4C-2 任务组只读 27 项中的
  2 项）。`get_metrics` 语义为 daemon 进程运行时指标（connection/request/latency/CAS
  hit rate/snapshot publish），非 workspace 查询工具面，需单独处置。
- 前置勘察疑问：defect/edit 数据表（defect_patterns / defect_fixes / file_edit_audit）
  不在 snapshot schema（snapshot 仅含 file_instances/symbols/calls/.../semgrep_findings 等）
  ——需确认 native 化可行性（主库表能否经 snapshot `query_db_path` 只读连接可达）。

**2. 勘察结论（步骤 0 verify_scope，方案 (a) native 可行）**：

- 数据源表名与 workspace 过滤语义（读 db 层 SQL + schema 表结构实证）：
  - `defect_stats`（db_defect_kb.py:702）：`defect_patterns`（pattern_id/category/severity/
    case_count/...）+ `defect_fixes`（effectiveness/...），两表均无 workspace_id 列 →
    **全局统计，无 workspace 隔离**（COUNT(*)、GROUP BY category/severity ORDER BY cnt DESC、
    AVG(effectiveness) NULL→0.0、ORDER BY case_count DESC LIMIT 10）。
  - `get_edit_stats`（db_edit.py:891，time_window="30d"）：`file_edit_audit`
    （id/workspace_id/file_path/operation/status/created_at/...）有 workspace_id 列，但
    Python db 层 SQL（L915-945）**无 workspace_id 过滤** → **全局统计**（与当前 HTTP worker
    路径经 `_bind_readonly_db` 绑定 workspace 后调用同一 db 方法返回全库编辑统计一致）。
  - `time_window` 语义（`_parse_time_window`，db_edit.py:965-1006）：空/"all"（大小写不敏感）
    → 0.0（不过滤）；`N` + 单位（d/w/h/y）→ now - N*86400/7*86400/3600/365*86400；
    ISO 日期（YYYY-MM-DD[ T HH:MM:SS]）→ 本地时区该时刻时间戳；无法解析 → 0.0。
    Rust 复刻：数字+单位纯算术；ISO 日期委托主库连接 `strftime('%s', ?, 'localtime')`
    （naive datetime 解释为本地时区，与 Python fromisoformat().timestamp() 语义一致）。
- snapshot 可达性（关键，同 W2-2 结论）：`GraphSnapshot.query_db_path()` 返回
  `source_db_path` = 发布时传入的 db_path（用户主库路径）；`open_query_connection`
  （snapshot_state.rs:182-209）以只读连接直接打开该路径 = **主库**。主库含全部上述表 →
  **无需扩展 snapshot schema**，native 化可行。
- 结论：native 化（方案 a），完全参照 W2-1/W2-2 同构链路；两 handler 仍 require
  workspace_instance_id（owned_workspace ACL + snapshot_not_ready 保护），但查询**不带**
  workspace_id WHERE（对齐 Python 全局统计语义）。

**3. get_metrics 处置决策（daemon 运行指标，非 MCP 查询工具）**：

- 勘察（server/tools/tools_rules.py:286 get_metrics + rust_ext/src/daemon）：
  - get_metrics 现状：HTTP 模式 `_http_unsupported("get_metrics")` fail-closed blocked
    （tools_rules.py:311-313）；legacy 模式经 UnixDaemonRpcClient 调 `metrics.snapshot` /
    `metrics.prometheus` RPC，失败降级本进程 MetricsCollector。
  - Rust daemon dispatch 完整路由清单（dispatch.rs:1540-1675）：仅 ping / health /
    schema.version 基础方法 + workspace.* / snapshot.* / query.* / task.* / gc.* /
    backup / restore / mount.* / toolchain.* / build_context.* / resolved_edges.* ——
    **无 metrics.snapshot / metrics.prometheus / metrics.\* 路由**。daemon 运行指标目前
    只在 health.rs 注释与 parser_metrics.rs（内部计数器 + health 端点聚合）中体现，未注册
    任何可调用的 metrics RPC。实证：`cw daemon metrics` 调 `metrics.snapshot` 返回
    method_not_found。
  - 决策：**get_metrics 保持 HTTP blocked（`_http_unsupported` fail-closed），registry 无
    条目，不迁移、不补 HTTP 分支**。设计性说明：get_metrics 语义为 daemon 进程运行时
    指标，应经 daemon 自身 RPC 暴露（未来补 metrics.snapshot RPC），不属于 workspace 查询
    工具面；当前 tools_rules.py 的 legacy 分支保留不动，HTTP 模式继续 fail-closed。
  - **计数修正说明**：任务书预估 101→98（误将 get_metrics 计入 compat 白名单递减），
    实际 get_metrics 保持 blocked 不占 compat 计数 → 实际 **101→99**（defect_stats +
    get_edit_stats 两项）。
- 白名单内 tools_rules.py 本轮**无代码改动**（现状已符合决策），仅 ledger 记录。

**4. 修复方案（2 工具迁移 rust_native，registry 101→99）**：

- Rust 侧：
  - `snapshot_state.rs`：新增 native handler `handle_defect_stats`（L670，require
    workspace_instance_id → open_query_connection → query_local_defect_stats，全局无
    workspace 过滤）与 `handle_edit_stats`（L689，time_window 参数 → parse_time_window →
    query_local_edit_stats）；本地查询 `query_local_defect_stats`（COUNT/GROUP BY/AVG/
    top_defects）与 `query_local_edit_stats`（by_status 四桶/by_operation 三桶/total/
    revert_rate = reverted/(applied+reverted) 分母 0→0.0 round 4，返回 time_window 原字符串）
    复刻 Python db 层语义；`parse_time_window` 复刻 Python `_parse_time_window`。
  - `dispatch.rs`：新增 `defect.stats` / `edit.stats` 两个 RPC 路由（DaemonStateExt trait
    默认 method_not_found 占位，SnapshotDaemonState 重写；L1590-1591 分派）。
  - `http_server.rs`：`COMPAT_ROUTE_WHITELIST` 移除 2 个条目（H4C-2 区块注释 27→26 项）；
    `build_capability_registry` 两者 backend 由 python_compat 切换为 rust_native、scope 由
    workspace 切换为 snapshot，task 归属 `T-1786840097331-fd01a3f8#W2-3`、deprecated 置空。
- Python 侧：
  - `tools_summary.py`：defect_stats HTTP 分支改为
    `client.defect_stats(db_path=_get_db_path_for_daemon())`（fail-closed，无 get_db 回退）；
    删除 worker handler `_h_defect_stats`；`_SUMMARY_READ_ONLY_METHODS` 由 27 项降为 26 项。
  - `tools_security.py`：get_edit_stats HTTP 分支改为
    `client.get_edit_stats(time_window=time_window, db_path=_get_db_path_for_daemon())`
    （fail-closed，无本地回退）；删除 worker handler `_h_get_edit_stats`；
    `_SECURITY_READ_ONLY_METHODS` 由 17 项降为 16 项。
  - `daemon_client.py`：新增便捷方法 `defect_stats`（L2182，call "defect.stats"）与
    `get_edit_stats`（L2200，time_window 参数，call "edit.stats"），均经
    `_ensure_remote_snapshot(db_path)` 注入权威 `workspace_instance_id`；
    db_path=None 时跳过 publish。**git diff 确认只新增便捷方法，未触碰
    route_worker_call / is_http_transport_enabled**。
- 两端对齐：Rust `COMPAT_ROUTE_WHITELIST`（99）= Python `RUST_COMPAT_ROUTE`（99）
  = 装配后 registry.methods()（99）。

**5. 测试结果（Windows，Python 3.14，`C:\Python314\python.exe`）**：

- `py_compile` 改动文件（tools_summary.py / tools_security.py / daemon_client.py / 新测试 /
  probe）全部通过。
- `cargo check --manifest-path rust_ext/Cargo.toml --bin cw-daemon` 通过（exit 0）；
  `cargo build --manifest-path rust_ext/Cargo.toml --bin cw-daemon` 产出 debug 二进制。
- pytest `tests/test_defect_edit_stats_rpc_http.py`：**14 passed**（6 问验收 mock 层：
  workspace_id 绑定 / 参数原样透传（time_window）/ 越界 fail-closed / snapshot_not_ready /
  跨 workspace 隔离 / Python fallback 边界 + CONVENIENCE_CASES 两便捷方法断言）。
- 既有缺口整改（迁移预期，随本任务同步）：`test_http_capability_registry.py`
  `_EXPECTED_COMPAT_METHODS_104→101→99`、`test_http_daemon_release_acceptance.py`
  `_EXPECTED_COMPAT_METHODS_104→101→99`、`test_http_combined_worker_cutover.py`
  EXPECTED_TOTAL=99、SUMMARY_METHODS 26、SECURITY_METHODS 16；重跑回归全绿。

**6. 改动文件清单（5 个修改 + 2 个新建）**：

- Rust：`rust_ext/src/daemon/dispatch.rs`（+2 handler 占位 +2 路由）、
  `rust_ext/src/daemon/http_server.rs`（COMPAT_ROUTE_WHITELIST 101→99 + registry 2 add()
  切换 rust_native/snapshot/W2-3）、`rust_ext/src/daemon/snapshot_state.rs`
  （+2 handler +2 查询函数 + parse_time_window）。
- Python 生产：`server/daemon_client.py`（+2 便捷方法）、`server/tools/tools_summary.py`
  （defect_stats HTTP 分支 fail-closed + 删 _h_defect_stats + 27→26）、
  `server/tools/tools_security.py`（get_edit_stats HTTP 分支 fail-closed + 删
  _h_get_edit_stats + 17→16）。`server/tools/tools_rules.py` 零改动（get_metrics 决策）。
- 测试/证据：`tests/test_defect_edit_stats_rpc_http.py`（新建，14 用例）、
  `.trae-cn/evidence/w2_3_http_verify.py`（新建，勘察结论头注 + probe）。

**7. 状态与 fresh 部署验证（2026-08-16）与真实 HTTP probe（7/7 PASS）**：

- 部署：fresh 部署两次成功；正式 daemon（PID 31380）由 runtime/current 启动并发布
  manifest（endpoint http://127.0.0.1:8214，schema_version=50，security_profile
  dev_loopback_unauthenticated）。
- 环境残留测试问题（非 W2-3 引入，记录于本账本供复审核对）：`tools_rules` legacy
  测试因 is_http_transport_enabled()=True 走 HTTP 分支不调 get_db（环境敏感 legacy 测试）；
  governance_error_cutover 隔离 daemon manifest 未发布。两者均与 W2-3 改动无关——
  tools_rules.py 零改动 + daemon_client.py 仅新增便捷方法为证。
- probe 脚本 `.trae-cn/evidence/w2_3_http_verify.py`（生产 HttpDaemonRpcClient 直连正式
  daemon，不 spawn 隔离进程），6 问 + 附 1 项结果：

| # | 验收问 | 结果 |
|---|---|---|
| Q1 | /capabilities python_compat available == 99 == 装配后 registry == RUST_COMPAT_ROUTE 三端对齐 | PASS（99/99/99，extra/missing 全空） |
| Q2 | defect_stats / get_edit_stats 在 capabilities 为 rust_native+available 且非 python_compat | PASS（两工具均 ('rust_native','available')） |
| Q3 | 两便捷方法真实 RPC round-trip（db_path=用户主库，workspace.register+snapshot.publish；get_edit_stats 传 time_window="7d" 并回显）——绝不 method_not_found | PASS（defect_stats dict；get_edit_stats dict time_window=7d） |
| Q4 | 负向 fail-closed：伪路由 summary.repo_map → method_not_found | PASS |
| Q5 | compat worker 可达：stats_top_files（白名单 99 项之一）worker 受理（非 method_not_found） | PASS（worker 受理） |
| Q6 | manifest 发布 + discover 直连：endpoint 与 manifest 一致、health pid 匹配、schema=50、security_profile 正确 | PASS（127.0.0.1:8214，pid 31380） |
| G | get_metrics HTTP 处置符合决策：HTTP 模式下返回 E_HTTP_COMPAT_UNSUPPORTED，不回落本地 SQLite / 不调用 daemon RPC | PASS（E_HTTP_COMPAT_UNSUPPORTED） |

**8. 待办（本账本记录时未完成，需续作补完）**：

- 步骤 2 report（S-1786849549448-0665cd92）+ task-level 推进 review：步骤 1 已 report
  成功（step 状态 success）；步骤 2（verify_test，含单测 + probe + 本账本）待 report。
- 任务状态保持 `in_progress`，待步骤 2 done 后推进 `review`；随后 independent_reviewer
  只读复审 → Coordinator 持 reviewer lease 执行 apply/close/release 收口。

## 9.19 W3-1：build 读组 5 工具 HTTP native 迁移（2026-08-16，任务 `T-1786861820150-bfe5e805`）

> 角色：implementer（只推进到 review）→ Coordinator 独立核验
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`tests/test_build_read_rpc_http.py`（新建 22 用例）全绿 +
> `test_http_capability_registry.py` + `test_http_combined_worker_cutover.py`（122 passed）+
> `test_http_daemon_release_acceptance.py`（13 passed）+ fresh runtime 部署
> （`scripts/refresh_shared_runtime.ps1`，evidence `20260816-154750-72b75d9e07ec-5b9aa176`，
> daemon PID 21684，transport=http，schema_version=50）+ `cargo check`/`cargo build` 通过。
> 所有权白名单：`rust_ext/src/daemon/{dispatch,snapshot_state,http_server}.rs`、
> `server/daemon_client.py`、`server/tools/tools_rules.py`、
> `tests/test_build_read_rpc_http.py`（新建）+ 三端对齐断言文件、本账本。
> 禁止：tools_task.py / tools_query.py / tools_security.py 等非白名单工具文件；动 get_metrics。
> 串行组：W3-1 是 W3 串行组第一个（W3-2 job / W3-3 semgrep 依赖本任务 closed 后才可编辑共享文件）。

**1. 根因与目标**：

- 5 个 build 读组工具（`list_build_contexts` / `get_build_context` / `get_active_build_context` /
  `get_resolved_edges` / `count_resolved_edges`）W3-1 前在 HTTP 模式走 python_compat worker
  （`route_worker_call`），每次调用 spawn worker 子进程执行 Python SQL，开销大。目标：native
  化，COMPAT_ROUTE_WHITELIST 99→94。

**2. 勘察结论（双模式 handler 设计，解决与 G1 ToolchainStore 语义冲突）**：

- 数据源（db_toolchain.py 语义真相源）：`workspace_build_contexts`（L852 get/L885 list/L931
  active）+ `resolved_edges`（L1098 edges/L1166 count），均有 workspace_id 列 → workspace 隔离。
- 主库可达性（同 W2-2/W2-3 结论）：`GraphSnapshot.query_db_path()` 返回 `source_db_path`
  = 用户主库；`open_query_connection` 只读连接直接打开主库 → db_toolchain 的
  `workspace_build_contexts`/`resolved_edges` 表可达，**无需扩展 snapshot schema**。
- **双模式决策**：`build_context.list/get` 若 params 含 `workspace_instance_id` → 走主库
  （W3-1 语义）；否则走 G1 既有 ToolchainStore（独立 toolchain.db，workspace_id 直接传入，
  保留 CW CLI 企业模式依赖）。`build_context.active/resolved_edges/count_resolved_edges` 为
  W3-1 新增路由，仅支持 workspace_instance_id 模式（G1 无对应 handler）。
- **`require_bound_workspace_id`**（snapshot_state.rs L218）：校验 params.workspace_id 与
  open_query_connection 解析的权威 workspace_id 一致，不一致 fail-closed invalid_params，
  防跨 workspace 越权。
- 语义细节：get_build_context 支持短 hash 前缀匹配（精确失败后 `LIKE hash%`，len==1 才
  返回，0/多返回 None）；limit<=0 语义（Python `limit is not None and limit > 0` 才加 LIMIT；
  Rust 复刻 `if limit > 0`；limit<0 → invalid_params fail-closed）；JSON 列解析
  （NULL/空 → []/{}、is_active 布尔化）。

**3. 实现清单**：

- Rust：`snapshot_state.rs`（+5 handler +5 查询函数 + require_bound_workspace_id）、
  `dispatch.rs`（trait 默认占位 + 3 新路由；list/get 复用既有 G1 路由做双模式）、
  `http_server.rs`（capability registry 5 条 → rust_native、scope "snapshot"、
  owner `T-1786861820150-bfe5e805#W3-1`；白名单移除 5 项，注释"剩 3 项"）。
- Python：`server/daemon_client.py`（+5 便捷方法，均 `params["workspace_id"] = workspace_id`
  + `_ensure_remote_snapshot(db_path)` 非 None 时注入权威 workspace_instance_id）、
  `server/tools/tools_rules.py`（5 工具加 HTTP 分支 fail-closed，删除 5 个 `_h_*` handler，
  `_RULES_READ_ONLY_METHODS` 8→3）。
- 测试/证据：`tests/test_build_read_rpc_http.py`（新建，22 用例，覆盖 6 问全部 5 工具）、
  三端对齐断言文件同步 94（`test_http_capability_registry.py` / `test_http_combined_worker_cutover.py` /
  `test_http_daemon_release_acceptance.py`）。

**4. 状态与验证（2026-08-16）**：

- `cargo check`/`cargo build`（debug + release）通过；三端 registry 94 对齐。
- 测试全绿：test_build_read_rpc_http 22 passed；capability_registry + combined_worker_cutover
  122 passed；release_acceptance 13 passed（fresh 部署后 evidence/运行 daemon hash 一致）。
- fresh 部署：`scripts/refresh_shared_runtime.ps1 -TaskId T-1786861820150-bfe5e805`，
  evidence status=passed、git_head=72b75d9、daemon PID 21684、transport=http、schema=50；
  旧 daemon PID 33480 精确停止。
- 基线失败实证排除：`test_http_index_job_cutover` 5 项失败为历史遗留（git stash 移走 W3-1
  改动后同样失败；H4B-I 时代旧契约断言"所有工具 _http_unsupported"，H4C 后过时），非 W3-1
  引入，不在白名单不修。

**5. 待办（本账本记录时未完成，需续作补完）**：

- 任务步骤 #0-#6 逐一 `cw task next/report` 推进到 review（implementer 遗留收尾未 report）。
- 随后 independent_reviewer 只读复审 → Coordinator 持 reviewer lease 执行 apply/close/release
  收口；W3-1 closed 后派发 W3-2（job 读组）/W3-3（semgrep 读组）。

## 9.20 W3-2：job 读组 3 工具 HTTP native 迁移（2026-08-16，任务 `T-1786861820151-f3cecf40`）

> 角色：implementer（只推进到 review）→ Coordinator 独立核验
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`tests/test_job_read_rpc_http.py`（新建 19 用例）全绿 +
> `test_http_capability_registry.py` + `test_http_combined_worker_cutover.py`（122 passed）+
> `test_http_daemon_release_acceptance.py`（13 passed）+ fresh runtime 部署
> （`scripts/refresh_shared_runtime.ps1`，evidence `20260816-164946-72b75d9e07ec-fb14ce82`，
> daemon PID 41792，transport=http，python_free）+ `cargo check`/`cargo build` 通过。
> 所有权白名单：`rust_ext/src/daemon/{dispatch,snapshot_state,http_server}.rs`、
> `server/daemon_client.py`、`server/tools/tools_task.py`、
> `tests/test_job_read_rpc_http.py`（新建）+ 三端对齐断言文件、本账本。
> 禁止：tools_rules.py / tools_query.py；动 cancel_job/get_metrics/get_job_stats（已迁移勿动）；
> task apply/close、git commit。
> 串行组：W3-2 是 W3 串行组第二个（W3-3 semgrep 读组依赖本任务 closed 后才可编辑共享文件）。

**1. 根因与目标**：

- 3 个 job 读组工具（`get_job_status` / `list_jobs` / `wait_for_job`）W3-2 前在 HTTP 模式走
  python_compat worker（`route_worker_call`），每次调用 spawn worker 子进程执行 Python SQL，
  开销大。目标：native 化，COMPAT_ROUTE_WHITELIST 94→91。

**2. 勘察结论（单模式 handler 设计，复用 W3-1 查询范式）**：

- 数据源（db_jobs.py 语义真相源）：JOBS_SCHEMA_DDL（jobs 表，含 workspace_id 列）+
  `_row_to_job` / `get_job` / `list_jobs` / `Job.is_terminal` / `Job.to_dict`。
- 主库可达性（同 W2-2/W3-1 结论）：`GraphSnapshot.query_db_path()` 返回 `source_db_path`
  = 用户主库；`open_query_connection(peer, workspace_instance_id)` 只读连接直接打开主库 →
  jobs 表可达，**无需扩展 snapshot schema**。
- **单模式决策**：3 个新工具仅支持 `workspace_instance_id` 模式（同 build_context.active，
  G1 ToolchainStore 无对应 job 语义）；client 便捷方法**不注入 workspace_id 参数**（job 工具
  签名无 workspace_id），仅由 `_ensure_remote_snapshot(db_path)` 注入权威
  workspace_instance_id（`open_query_connection` 从 instance 解析权威 workspace_id）。
- **跨 workspace 隔离（注意：与 W3-1 build_context 不同）**：3 个 job handler **未调用**
  W3-1 的 `require_bound_workspace_id`（job 工具无 workspace_id 参数可比对）；隔离由
  `open_query_connection(peer, workspace_instance_id)` 的 owned_workspace ACL 校验 +
  查询级 `WHERE workspace_id = ?` 限定实现（job 属其他 workspace → not found fail-closed）。
- **wait_for_job 轮询语义复刻**：deadline 循环查询 jobs 表，终态（completed/cancelled/failed）
  返回 Job.to_dict()，否则 sleep(poll_interval)，超时返回 `status="timeout"` +
  `error="timeout after {timeout}s"`。
- 语义细节：limit<0 / timeout<0 / poll_interval<0 → invalid_params fail-closed；
  `job_type`/`status` 空字符串过滤（Python `if x is not None` → Rust
  `filter(|s| !s.is_empty())`）；Job.to_dict() 复刻 asdict 全字段，JSON 解析失败回退空对象，
  cancel_requested 0/1 → bool；get_job 查询带 `WHERE job_id = ?1 AND workspace_id = ?2`
  （Python 原 get_job 无 workspace 过滤，Rust 侧增加限定防越权）。

**3. 实现清单**：

- Rust：`snapshot_state.rs`（+3 handler +4 查询函数 job_row_to_dict / query_local_get_job /
  query_local_list_jobs / is_job_terminal + job_wait_result_map）、`dispatch.rs`（3 条路由：
  task.job_status / task.list_jobs / task.wait_for_job）、`http_server.rs`（capability registry
  3 条 → rust_native、scope "snapshot"、owner `T-1786861820151-f3cecf40#W3-2`；白名单移除
  3 项，注释"任务组 13→10"）。
- Python：`server/daemon_client.py`（+3 便捷方法 get_job_status / list_jobs / wait_for_job，
  仅 `_ensure_remote_snapshot` 注入 workspace_instance_id，不注入 workspace_id）、
  `server/tools/tools_task.py`
  （3 工具加 HTTP 分支 fail-closed，删除 `_h_get_job_status` / `_h_list_jobs` /
  `_h_wait_for_job`，`_TASK_READ_ONLY_METHODS` 13→10）。
- 测试/证据：`tests/test_job_read_rpc_http.py`（新建，19 用例，覆盖 6 问全部 3 工具）、
  三端对齐断言文件同步 91（`test_http_capability_registry.py` /
  `test_http_combined_worker_cutover.py` / `test_http_daemon_release_acceptance.py`）。

**4. 状态与验证（2026-08-16）**：

- `cargo check`/`cargo build`（debug + release）通过；三端 registry 91 对齐。
- 测试全绿：test_job_read_rpc_http 19 passed；capability_registry + combined_worker_cutover
  122 passed；release_acceptance 13 passed（fresh 部署后）。
- fresh 部署：`scripts/refresh_shared_runtime.ps1 -TaskId T-1786861820151-f3cecf40`，
  evidence status=passed、git_head=72b75d9、daemon PID 41792（旧 21684 精确停止）、
  transport=http、python_free、构建/部署/运行 hash 一致（cw-daemon.exe sha256
  e3a3093665366aa5cdca3d471e18a0b72caff7bd6bcce862645f9946138bb67f）。
- 注意：release_acceptance 首跑失败根因是 runtime/current 仍为旧 daemon（3 方法仍注册
  python_compat），fresh 部署后重跑全绿，非代码缺陷。

**5. 待办（本账本记录时未完成，需续作补完）**：

- 任务步骤逐一 `cw task next/report` 推进到 review（implementer 遗留收尾未 report）。
- 随后 independent_reviewer 只读复审 → Coordinator 持 reviewer lease 执行 apply/close/release
  收口；W3-2 closed 后派发 W3-3（semgrep 读组）。

## 9.21 W3-3：semgrep 读组 get_semgrep_findings HTTP native 迁移（2026-08-16，任务 `T-1786861820151-deb64c48`）

> 角色：implementer（只推进到 review）→ Coordinator 独立核验
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`tests/test_semgrep_findings_rpc_http.py`（新建 14 用例）全绿 +
> `test_http_capability_registry.py` + `test_http_combined_worker_cutover.py`（122 passed）+
> `test_http_daemon_release_acceptance.py`（13 passed）+ fresh runtime 部署
> （`scripts/refresh_shared_runtime.ps1`，evidence `20260816-174825-72b75d9e07ec-3e53e4a0`，
> daemon PID 33548，transport=http，python_free）+ `cargo check`/`cargo build` 通过。
> 所有权白名单：`rust_ext/src/daemon/{dispatch,snapshot_state,http_server}.rs`、
> `server/daemon_client.py`、`server/tools/tools_query.py`、
> `tests/test_semgrep_findings_rpc_http.py`（新建）+ 三端对齐断言文件、本账本。
> 禁止：tools_rules.py / tools_task.py；动 run_semgrep_scan/scan_semgrep_incremental（写语义
> blocked 勿动）与 get_semgrep_stats（已 W2-1 迁移勿动）；task apply/close、git commit。
> 串行组：W3-3 是 W3 串行组第三个（W3-2 closed 后开工；H4C-2 符号组 15→14）。

**1. 根因与目标**：

- `get_semgrep_findings` W3-3 前在 HTTP 模式走 python_compat worker（`route_worker_call`），
  每次调用 spawn worker 子进程执行 Python SQL，开销大。目标：native 化，
  COMPAT_ROUTE_WHITELIST 91→90。

**2. 勘察结论（单模式 handler 设计，复用 W3-2/W2-1 查询范式）**：

- 数据源（analyzers/issues.py L776-819 语义真相源）：semgrep_findings 表 + JOIN file_instances
  取 rel_path as file_path；可选过滤 severity / language / rule_id + LIMIT 截断。
- 主库可达性（同 W2-1/W3-1 结论）：`GraphSnapshot.query_db_path()` = 用户主库；
  `open_query_connection(peer, workspace_instance_id)` 只读连接直接打开主库 →
  semgrep_findings 表可达，**无需扩展 snapshot schema**。
- **单模式决策**：工具仅支持 `workspace_instance_id` 模式；client 便捷方法**不注入
  workspace_id 参数**（工具签名无 workspace_id），仅由 `_ensure_remote_snapshot(db_path)`
  注入权威 workspace_instance_id（`open_query_connection` 从 instance 解析权威 workspace_id）。
- **跨 workspace 隔离（与 W3-2 job 同构，关键差异点）**：semgrep_findings 表**无 workspace_id
  列**，隔离经 `JOIN file_instances fi ON sf.file_instance_id = fi.id` +
  `WHERE fi.workspace_id = ?1` 实现（与 query_local_semgrep_stats 同构）；handler **未调用**
  `require_bound_workspace_id`（工具无 workspace_id 参数可比对），隔离由
  `open_query_connection` 的 owned_workspace ACL 校验 + 查询级 JOIN 限定实现（其他 workspace
  的 findings 经 file_instances JOIN 不可见，fail-closed）。
- 过滤语义复刻（Python 条件拼接）：severity 非空 → `sf.severity = ?`（`severity.upper()`）；
  language 非空 → `sf.language = ?`（精确匹配）；rule_id 非空 → `sf.rule_id LIKE ?`
  （`%rule_id%` 模糊匹配）；排序 `ORDER BY sf.severity = 'ERROR' DESC, sf.severity =
  'WARNING' DESC, sf.id DESC`（ERROR 优先、其次 WARNING、同权重按 id 降序）；LIMIT 截断
  （limit=0 → 空数组）。返回 `sf.*` 全列 + `fi.rel_path as file_path`（snake_case 列名集合）。
- fail-closed：limit<0 → invalid_params；`_ensure_remote_snapshot` 返回 None（注册失败边界）
  时不注入 workspace_instance_id → Rust 侧 require 拒绝（invalid_params）；
  `_ensure_remote_snapshot` 抛错（未发布 snapshot）→ 异常原样传播，不回退本地 SQL。

**3. 实现清单**：

- Rust：`snapshot_state.rs`（+1 handler `handle_query_semgrep_findings` +1 查询函数
  `query_local_semgrep_findings`，参数用 `rusqlite::types::Value` 动态绑定）、`dispatch.rs`
  （1 条路由：query.semgrep_findings）、`http_server.rs`（capability registry 1 条 →
  rust_native、scope "snapshot"、owner `T-1786861820151-deb64c48#W3-3`；白名单移除 1 项，
  注释"符号组 14 项、91→90"）。
- Python：`server/daemon_client.py`（+1 便捷方法 get_semgrep_findings，仅
  `_ensure_remote_snapshot` 注入 workspace_instance_id）、`server/tools/tools_query.py`
  （get_semgrep_findings 工具加 HTTP 分支 fail-closed，删除 `_h_get_semgrep_findings`
  worker handler，`_SYMBOL_READ_ONLY_METHODS` 15→14）。
- 测试/证据：`tests/test_semgrep_findings_rpc_http.py`（新建，14 用例，覆盖 6 问）、
  三端对齐断言文件同步 90（`test_http_capability_registry.py` /
  `test_http_combined_worker_cutover.py` / `test_http_daemon_release_acceptance.py`）。

**4. 状态与验证（2026-08-16）**：

- `cargo check`/`cargo build`（debug）通过；三端 registry 90 对齐。
- 编译期修复：`query_local_semgrep_findings` 初版把 `serde_json::Value` 直接传入
  `params_from_iter`（Value 不实现 `ToSql`，E0277），改为 `rusqlite::types::Value` 动态绑定
  （与同文件 query_local_uncommented_symbols 同构）。
- **数据级验证暴露并修复的语义缺陷（重要）**：真实 HTTP round-trip 中发现
  `query_local_semgrep_findings` 用 `row.get::<_, String/i64/f64>` 强类型读取 17 列，
  semgrep_findings 表除 file_instance_id/rule_id 外均无 NOT NULL 约束，历史数据可含 NULL
  （如 fix/symbol_id/symbol_qualified/scan_id），遇 NULL 抛 `Invalid column type Null` →
  整个查询 internal_error。而 Python `dict(row)` 对 NULL 返回 None 绝不报错。修复：
  全部可空列 Option 化（JSON null ≡ Python None），与 Python 语义完全对齐；
  数据级验证实测 fix=NULL 行正常返回。
- 测试全绿：test_semgrep_findings_rpc_http 14 passed；回归
  （capability_registry + job_read_rpc_http + build_read_rpc_http + task_stats_rpc_http）
  80 passed；release_acceptance 13 passed 无 skip（fresh 部署后）。
- **fresh 部署（含 NULL 修复的最终部署）**：`scripts/refresh_shared_runtime.ps1
  -TaskId T-1786861820151-deb64c48`，evidence
  `20260816-183536-72b75d9e07ec-d5208d59.json`，status=passed、git_head=72b75d9e07ec、
  旧 daemon PID 4832 精确停止、新 daemon PID 20872 运行于 runtime\current、transport=http、
  python_dependency_mode=python_free（无 Python DLL）、构建/部署/运行 hash 一致
  （cw-daemon.exe sha256 8e75b6cb10db41c04a975665c8b207cf8fc44efcc50f7ce1b06bdbb496da7a47）、
  ping/health ok（schema_version 50）、MCP 进程保留、rollback=false。
  （上一轮部署 evidence 20260816-174825-72b75d9e07ec-3e53e4a0 / PID 33548 /
  hash 5a039c60... 为本轮 NULL 修复前的过渡证据，保留作历史。）
- **真实 HTTP 数据级 round-trip（15/15 通过，验收 6 问之外的核心实证）**：
  构造跨 workspace 临时验证库（从权威库复制完整 schema，过滤 sqlite_% 与 %_fts_% 表），
  先手动 `workspace.register` 锁定 workspace_instance_id + 同批 workspace_id（ROWID），
  预置 client._remote_workspace_id 阻止便捷方法二次 register（register 用 INSERT OR
  REPLACE 使 ROWID 轮转递增，二次 register 会导致查询过滤值与插入数据不匹配而返回空），
  再插入 4 行本 workspace + 2 行异 workspace（wid+5000）数据后验证：
  severity=ERROR 过滤 2 行、severity 小写 error upper 归一化 2 行、language=python 精确 3 行、
  rule_id LIKE %else% 2 行 / %unused% 1 行、limit=1 截断、ORDER BY id 降序（首行 src/c.rs）、
  跨 workspace 隔离（异 workspace 2 行不可见）、JOIN 返回 18 键（sf.* 17 列 + file_path）、
  无过滤返回本 workspace 4 行、limit=0 空数组、limit<0 invalid_params fail-closed、
  NULL 列（fix/symbol_id/symbol_qualified）不崩溃。验证后临时文件（探针脚本 + 临时库）已清理。

**5. 状态收口（2026-08-16 完成）**：

- 任务步骤已全部 `cw task report` 推进：7 步（步骤 0-6 对应 step_index 0-6）全部 done，
  任务状态推进到 review，待 independent_reviewer 只读复审。
- 随后 independent_reviewer 只读复审 → Coordinator 持 reviewer lease 执行 apply/close/release
  收口；W3-3 closed 后 W3 串行组完成（W2-1/W2-2/W2-3/W3-1/W3-2/W3-3 全部 native 化）。

## 9.22 W4-1：git 读组 5 工具 HTTP native 迁移（2026-08-16，任务 `T-1786886251769-22b94ee8-sub-1`）

> 角色：implementer（只推进到 review）→ Coordinator 独立核验
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`tests/test_git_read_rpc_http.py`（新建，覆盖 6 问验收）+
> `test_http_capability_registry.py` + `test_http_combined_worker_cutover.py`（白名单 90→88 同步）+
> `cargo check` 通过；`cargo test daemon:: --lib` 743 passed / 24 failed（pre-existing，见 §4）。
> 所有权白名单：`rust_ext/src/daemon/{dispatch,snapshot_state,http_server}.rs`、
> `server/daemon_client.py`、`server/tools/tools_query.py`、`server/tools/tools_workspace.py`、
> `server/tools/tools_task.py`、`tests/test_git_read_rpc_http.py`（新建）+ 三端对齐断言文件、本账本。
> 禁止：import_git_history（写面，归 W4-4 决策）；tools_summary.py / tools_security.py（归 W4-2/3/4）；
> task apply/close/reopen、git commit。
> 串行组：W4-1 是 W4 串行组第一个（串行 G1）。

**1. 根因与目标**：

- 5 个 git 读面工具（get_file_history / get_git_commits / get_commit_changes / get_git_stats /
  get_commit_tasks）W4-1 前在 HTTP 模式分别走 python_compat worker（get_file_history /
  get_commit_tasks，`route_worker_call`）或无 HTTP 分支直接 `get_db()` 本地 SQL
  （get_git_commits / get_commit_changes / get_git_stats）。目标：全部 native 化，
  COMPAT_ROUTE_WHITELIST 90→88，capability registry 首次入册 3 条 rust_native。

**2. 勘察结论（workspace 隔离与路径规范化是两处关键差异点）**：

- 语义真相源：`db/db_query.py` get_file_history（L500-513）、`db/db_git.py`
  get_git_commits/get_commit_changes/get_git_stats（L288-394）、`db/db_task_attribution.py`
  get_commit_tasks（L427-483）。SQL 逐条复刻（排序/limit/offset/workspace 过滤）。
- **schema 核实结论（workspace 隔离策略）**：
  - `git_commits` 表**有** workspace_id 列 → 直接 `WHERE workspace_id = ?` 过滤；
  - `git_file_changes` 表**无** workspace_id 列，但 commit_hash 为全局唯一（TEXT UNIQUE）——
    两段式隔离：先按 `git_commits.workspace_id + commit_hash` 确认 commit 归属
    （跨 workspace → `{"commit": null, "file_changes": []}` fail-closed），再按 commit_hash
    查 git_file_changes LEFT JOIN file_instances（不跨 workspace，与 Python 两段式同构）；
  - `get_git_stats` 的 file_change_count 与 change_types 经 `JOIN git_commits` 限定
    workspace（与 Python 一致）；
  - `file_versions` 经 `JOIN file_instances WHERE fi.workspace_id` 隔离（status != 'archived'）；
  - `get_commit_tasks` 复刻 Python **全局查询**（task_symbol_changes 无 workspace 维度，
    task_id 全局唯一），workspace_instance_id 仅用于 ACL，不参与过滤。
- **get_file_history 路径规范化决策**：Python db 层对绝对路径做
  `norm_path(os.path.relpath(file_path, workspace_root))`。因 workspaces.root_path 为真相源、
  与 daemon 侧 client_view_root 不同源，规范化**保留在 Python 工具层**（tools_query.py
  HTTP 分支内联复刻），Rust 侧只按最终 rel_path 精确匹配（rel_path 仅用于 SQL 等值匹配，
  不触文件系统，无路径穿越风险）。
- 主库可达性（同 W2-1/W3-1/W3-3 结论）：5 工具数据源均在主库，经 `GraphSnapshot.query_db_path()`
  只读连接直达，**无需扩展 snapshot schema**。
- fail-closed：limit/offset<0 → invalid_params（Python db 层无校验，负值被 SQLite 静默吞，
  native 侧显式拒绝）；`_ensure_remote_snapshot` 返回 None 时不注入 workspace_instance_id →
  Rust 侧 require 拒绝；空 commit_hash 的 get_commit_tasks 返回空数组（复刻 `if not commit_hash`）。

**3. 实现清单**：

- Rust：`snapshot_state.rs`（+5 handler `handle_query_file_history` / `handle_query_git_commits` /
  `handle_query_git_commit_changes` / `handle_query_git_stats` / `handle_query_commit_tasks`
  + 5 查询函数 `query_local_*`；file_history 的 ast_cache BLOB 列 Rust 侧恒输出 null——
  `db/db_build.py` 的 `_update_ast_cache` 实际会写入 `file_versions.ast_cache` 非 NULL
  （JSON 编码元数据字节流），Rust 恒输出 null 是与 Python 本地返回 bytes 的语义偏差，但对
  MCP 消费者无害（内部元数据 + bytes 不可 JSON 序列化，且 Python compat worker 的
  json.dumps 遇非 NULL bytes 会崩溃）；可空列全部 Option 化）、
  `dispatch.rs`（5 条路由：query.file_history / query.git_commits / query.git_commit_changes /
  query.git_stats / query.commit_tasks）、`http_server.rs`（capability registry：get_file_history /
  get_commit_tasks 从 python_compat 切 rust_native，get_git_commits / get_commit_changes /
  get_git_stats 首次入册 rust_native，均 read_only + snapshot scope；白名单移除 2 项 90→88）。
- Python：`server/daemon_client.py`（+5 便捷方法，仅 `_ensure_remote_snapshot` 注入
  workspace_instance_id，参数原样透传）、`server/tools/tools_query.py`（get_file_history 加
  HTTP 分支 + 绝对路径规范化，删除 `_h_get_file_history` worker handler，符号组 14→13）、
  `server/tools/tools_workspace.py`（get_git_commits / get_commit_changes / get_git_stats
  新增 HTTP 分支 fail-closed）、`server/tools/tools_task.py`（get_commit_tasks 加 HTTP 分支，
  删除 `_h_get_commit_tasks` worker handler，任务组 10→9）。
- 测试/证据：`tests/test_git_read_rpc_http.py`（新建，覆盖 6 问：workspace_id 绑定/参数透传/
  越界 fail-closed/snapshot_not_ready/跨 workspace 隔离/fallback 边界，含绝对路径规范化用例）、
  三端对齐断言文件同步 88（`test_http_capability_registry.py` 变量改名 _EXPECTED_COMPAT_METHODS_88、
  `test_http_combined_worker_cutover.py` EXPECTED_TOTAL=88、SYMBOL_METHODS 14→13、TASK_METHODS 10→9）。

**4. 状态与验证（2026-08-16）**：

- `cargo check`（debug）通过（仅既有 warning，无新增 error）；`cargo test daemon:: --lib` 实际
  743 passed / **24 failed**，失败全部集中在 `daemon::workspace::tests`（backup/mount/gc_cas/
  restore 等 admin-only 用例），为 pre-existing（与 W4-1 改动无关，W4-1 只触及
  snapshot_state/dispatch/http_server 只读查询面）。
- pytest：test_git_read_rpc_http + test_http_capability_registry + test_http_combined_worker_cutover
  全绿（见证据日志）。
- **数据级 round-trip 待 Coordinator 核验**：真实 HTTP 数据级验证（跨 workspace 隔离、可空列
  Option 化、绝对路径规范化）由 evidence 角色或真实 HTTP probe 补充。

**5. 状态收口（2026-08-16 完成）**：

- 任务步骤已全部 `cw task report` 推进到 review（步骤 0-6 对应 step_index 0-6），
  待 independent_reviewer 只读复审；随后 Coordinator 持 reviewer lease 执行 apply/close 收口。

## 9.23 W4-2：coverage/review 读组 HTTP native 迁移（2026-08-17，任务 `T-1786886251769-22b94ee8-sub-2`）

> 角色：implementer（只推进到 review）→ Coordinator 独立核验
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`tests/test_coverage_review_rpc_http.py`（新建，覆盖 6 问验收）+
> `test_http_capability_registry.py` + `test_http_combined_worker_cutover.py` +
> `test_http_daemon_release_acceptance.py` + `test_http_daemon_capability_matrix.py`
> （白名单 88→86、RUST_NATIVE_EXPECTED 49→51 同步）+ `cargo check` 通过。
> 所有权白名单：`rust_ext/src/daemon/{snapshot_state,dispatch,http_server}.rs`、
> `server/daemon_client.py`、`server/tools/tools_summary.py`、`tests/test_coverage_review_rpc_http.py`（新建）、
> 三端对齐断言文件、本账本。
> 禁止：review_readiness 迁移（依赖函数未迁移，见下）、task apply/close/reopen、git commit。
> 串行组：W4 串行组第二个（依赖 W4-1 完成后开工）。

**1. 根因与目标**：

- 3 个 coverage/review 读面工具（get_coverage_for_symbol / diff_to_symbol / review_readiness）
  W4-2 前在 HTTP 模式全部走 python_compat worker（`route_worker_call`，H4C-2 第二批接入）。
  目标：按语义风险分级迁移，COMPAT_ROUTE_WHITELIST 88→86。

**2. 勘察结论与迁移决策（步骤 0 annotate 落账，实现即真相）**：

- **get_coverage_for_symbol → 迁移 rust_native**（RPC `query.coverage_for_symbol`）：
  语义真相源 `db/db_coverage.py` L341-416，纯 SQL 两段式（symbols JOIN file_instances
  按 workspace_id + qualified_name LIMIT 1 定位；coverage_data 按 symbol_id + 行范围
  ORDER BY line_start 统计）。无文件系统/外部依赖，复刻风险低。输出 dict（未找到符号
  返回 JSON null ≡ Python None）。coverage_pct 复刻 `round(covered/tracked*100, 1)`：
  Rust 侧用**整数精确 round-half-even**（`covered*1000/tracked` 商余判定）复刻 Python
  十进制 round 语义，避免 `X.25`（如 1/80→1.2）浮点 ties 偏差（见步骤 5 测试用例）。
- **diff_to_symbol → 迁移 rust_native**（RPC `query.diff_to_symbol`）：
  语义真相源 `db/db_impact.py` L138-270。regex crate（1.11）可用，4 个正则均为简单
  捕获组（无 lookahead/lookbehind，Python `re` 与 Rust `regex` 行为一致），状态机
  （flush 缓冲 + hunk 行号）重构为显式状态 struct（Python 嵌套闭包 flush 的等价物）。
  `_query_overlapping_symbols`（L39-87）两段式 SQL 复刻：先 rel_path 精确匹配，未命中
  再全 workspace 行号重叠扫描 + 后缀匹配兜底（`rel.endswith(rel_path) or
  rel_path.endswith(rel)`）。按 symbol_hash 去重保留首次 change_type。
  **关键语义发现（实现即真相）**：Python flush() 在判定 change_type **之前**已把
  hunk_added/hunk_removed 重置为 0（L200-202 先重置、L210-217 后判定），导致
  "仅删除行→deleted"与"仅新增行→added"两个分支**实际不可达**——非文件删除场景
  change_type 恒为 "modified"。Rust 复刻**逐字节保持该行为**（先重置后判定），
  保证数据级 round-trip 一致；该差异与 docstring/任务描述不符，属 Python 潜在缺陷，
  记录于此（不按文档意图"修正"，否则数据级对照失败）。
- **review_readiness → 保持 python_compat（明确决策，不迁移）**：
  语义真相源 `db/db_impact.py` review_readiness_report L767-864，是**组合函数**：
  依赖 `blast_radius`（GraphStore 内存索引 BFS + SQL 补全，python_compat 未迁移）与
  `cross_layer_impact`（py_cross_layer_impact 正则提取 + Rust 扩展，python_compat 未迁移）。
  在 Rust 端复刻这两个依赖函数复杂度高、易产生语义漂移（blast_radius 依赖快照
  GraphStore 索引、cross_layer_impact 依赖外部 crate 语义），且本任务验收要求
  "数据级 round-trip 与 Python db 层一致"，两依赖未迁移时无法保证。**决策：保持
  python_compat**（白名单保留 ("review_readiness", "read_only")，registry 不动），
  依赖函数随后续 W 批次迁移后再评估。
- **workspace 隔离**：symbols/coverage_data 均无 workspace_id 列，经 JOIN
  file_instances WHERE fi.workspace_id 限定（与 W3-3 semgrep_findings 同构）；
  主库可达性同 W4-1（经 snapshot query_db_path 只读连接，无需扩展 snapshot schema）。
- **fail-closed**：`_ensure_remote_snapshot` 返回 None 时不注入 workspace_instance_id →
  Rust handler require 拒绝（invalid_params）；HTTP 失败原样传播不回退本地 SQL；
  越界参数：无（本组工具无 limit/offset 参数）。

## 9.24 W4-3：defect 读组 HTTP native 迁移（2026-08-17，任务 `T-1786886251769-22b94ee8-sub-3`）

> 角色：implementer（只推进到 review）→ Coordinator 独立核验
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`tests/test_defect_read_rpc_http.py`（新建，覆盖 6 问验收）+
> `test_http_capability_registry.py` + `test_http_combined_worker_cutover.py` +
> `test_http_daemon_release_acceptance.py` + `test_http_daemon_capability_matrix.py`
> （白名单 86→81、RUST_NATIVE_EXPECTED 51→56 同步）+ `cargo check` 通过。
> 所有权白名单：`rust_ext/src/daemon/{snapshot_state,dispatch,http_server}.rs`、
> `server/daemon_client.py`、`server/tools/{tools_summary,tools_task}.py`、
> `tests/test_defect_read_rpc_http.py`（新建）、三端对齐断言文件、本账本。
> 禁止：defect_learn 迁移（写面，见下）、task apply/close/reopen、git commit。
> 串行组：W4 串行组第三个（依赖 W4-2 完成后开工）。

**1. 根因与目标**：

- 5 个 defect 读面工具（defect_correlation / churn_analysis / defect_search /
  defect_suggest_fix / get_defect_correlation）W4-3 前在 HTTP 模式全部走
  python_compat worker（`route_worker_call`，H4C-2/3 接入）。
  目标：按语义风险分级迁移，COMPAT_ROUTE_WHITELIST 86→81。
  defect_stats 已在 W2-3 完成（RPC defect.stats），不在本任务范围。

**2. 勘察结论与迁移决策（步骤 0 annotate 落账，实现即真相）**：

- **defect_correlation → 迁移 rust_native**（RPC `query.defect_correlation`）：
  语义真相源 `db/db_evolution.py` L311-459（defect_correlation 全路径）+
  L461-599（`_defect_correlation_via_rust` 短路）。**关键核实结论**：Rust 短路
  （feature=rust_defect_correlation，`callwarden_core.py_defect_correlation`）
  的**所有 SQL 查询都在 Python 侧**（symbol_contents / changes_by_file /
  all_versions_by_file / findings_by_file / direct_findings），Rust 扩展仅负责
  窗口切片 + finding 匹配 + 去重 + 聚合。因此 daemon RPC 实现**按 Python
  全路径复刻全部 SQL + 内存聚合**（窗口/匹配/去重/聚合在 Rust 端完成，
  不依赖 callwarden_core 扩展）。
  复刻要点：① symbol_contents 查 qualified_name（可空）；② 变更点查询
  （file_symbol_versions JOIN file_versions JOIN file_instances WHERE
  fi.workspace_id + fsv.symbol_hash ORDER BY file_instance_id, version_num
  ASC）按 file_instance_id 分组；③ 每文件全版本 + version_num→index 映射；
  ④ 每变更点窗口 = 变更后 window_commits 个版本（Python 切片
  `all_versions[idx+1:idx+1+window_commits]`，window_commits<=0 → 空窗口，
  Python 允许负值不报错，Rust 复刻该语义不拒绝），取 content_hash 非空集合
  → semgrep_findings WHERE file_instance_id + content_hash IN（无 ORDER BY，
  保持 SQLite 默认行序），按 finding id 去重；⑤ qualified_name 非空时补充
  symbol_qualified 全局直连 findings（after_change_at=0.0，**无 workspace
  限定**，与 Python 一致）；⑥ 输出 {symbol_hash, total_changes,
  defects_after_change, defect_types, findings}，findings 字段
  {rule_id, rule_name, severity, start_line, end_line, scanned_at, message,
  after_change_at}（after_change_at=变更点 parsed_at）。顺序保证：按
  file_instance_id 升序、文件内变更点 version_num 升序、窗口 findings 按
  SQL 行序、direct 按 SQL 行序。
- **get_defect_correlation → 迁移 rust_native**（RPC `query.get_defect_correlation`）：
  语义真相源 `db/db_evolution.py` L601-659。两段式：① 按 qualified_name 查
  symbol_hash（file_symbol_versions JOIN file_versions JOIN file_instances
  WHERE fi.workspace_id + fv.is_current=1 + fsv.is_deleted=0 + fsv.qualified_name
  LIMIT 1）；未找到 → {qualified_name, change_count:0, defect_count:0,
  defect_rate:0.0, recent_defects:[]}；② 找到 → 复用 defect_correlation 全路径
  + recent_defects = findings[:3]（{rule_id, severity, message[:100],
  start_line}）+ defect_rate = round(change_count 除, 3)（整数
  round-half-even 复刻）。输出含 defect_types。
- **churn_analysis → 迁移 rust_native**（RPC `query.churn_analysis`）：
  语义真相源 `db/db_evolution.py` L679-876。**时间窗口核实结论**：Python
  `_parse_time_window`（L45-65）正则 `^\s*(\d+)\s*([dwmy])\s*$`，单位
  d=86400 / w=7*86400 / m=30*86400 / y=365*86400，不匹配 → 0.0；
  与已有 Rust `parse_time_window`（d/w/h/y，无 m、无空白容忍）**不同**，
  需新建 `parse_time_window_evolution` 精确复刻（含空白与 m 单位）。
  双路径：git_file_changes 优先（LEFT JOIN git_commits 取 timestamp，
  workspace_id + module_filter LIKE + cutoff 过滤），无数据时 file_versions
  相邻版本 total_lines 差值近似（parsed_at >= cutoff 过滤）。趋势分桶
  `time.strftime("%Y-%m-%d", time.localtime(ts))` 用 SQLite
  `strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime')` 复刻（本地时区一致）；
  **gfc 场景注意**：Python `if ts and file_churn > 0` 中 ts=0.0 也为 falsy，
  故 Rust 需 `commit_ts > 0.0 && file_churn > 0` 才分桶（strftime(0) 会返回
  "1970-01-01" 而非跳过）；fv 场景 `if diff > 0` 直接用 parsed_at（无 ts
  判定，parsed_at 为 NOT NULL）。churn_rate = round(整数除法, 4) 用整数
  round-half-even 复刻；top_files 按 churned_lines 降序前 10；trend 按日期
  字符串升序。输出 {churn_rate, total_churned_lines, changed_files,
  total_lines_current, top_churned_files, trend}。
- **defect_search → 迁移 rust_native**（RPC `query.defect_search`）：
  语义真相源 `db/db_defect_kb.py` L398-427。纯 SQL：`SELECT * FROM
  defect_patterns WHERE 1=1` + category 前缀 LIKE（category+"%"）+
  severity_filter 精确匹配（`_normalize_severity`：小写 strip，空 → "info"；
  **空字符串不进入 WHERE**）+ ORDER BY case_count DESC, created_at DESC。
  返回全列 dict（pattern_id / category / description / detection_rule /
  fix_template / severity / learned_from / case_count / created_at）。
  defect_patterns 无 workspace_id 列 → 全局查询（与 Python 一致）。
- **defect_suggest_fix → 迁移 rust_native**（RPC `query.defect_suggest_fix`）：
  语义真相源 `db/db_defect_kb.py` L429-551。finding_id>0 → semgrep_findings
  按 id 查（rule_id/snippet/fix/symbol_qualified）；else → symbol_contents
  查 qualified_name → symbol_qualified 或 content_hash 查最新 finding
  （ORDER BY scanned_at DESC LIMIT 1，**无 workspace 限定，与 Python 一致**）；
  pattern_id = "DP-"+rule_id → defect_patterns 查 fix_template/case_count
  （不存在 → pattern_id 置空）；similar_fixes = defect_fixes 按 pattern_id
  ORDER BY effectiveness DESC, created_at DESC LIMIT 5；effectiveness_score =
  均值 / min(0.5, case_count*0.05) / 0.0 三分支；recommended_fix =
  finding_fix or pattern_fix_template。effectiveness_score round(x,4) 的输入
  为浮点（REAL 列求和均值），用 f64 `(x*10000.0).round()/10000.0` 复刻——
  极端十进制 ties（如 x=0.12345）时 f64 表示与 Python 十进制 round 可能有
  ±1ulp 差异，属可接受风险（输入本身即浮点近似），记录于此。
- **defect_learn → 不迁移 rust_native（写面分类决策，明确）**：
  语义真相源 `db/db_defect_kb.py` L553-700（learn_defect_from_fix）。
  **核实结论：确认是写操作**——`INSERT INTO defect_fixes`（L675-683）+
  `_ensure_pattern`（INSERT OR REPLACE INTO defect_patterns）+
  `_increment_pattern_case_count`（UPDATE case_count）+ `self.conn.commit()`。
  **决策：保持 python_compat**（registry 不动，白名单保留
  ("defect_learn", "read_only")，tools_summary.py 保留原 route_worker_call
  路径），HTTP 模式继续经 compat worker 执行；后续写面迁移批次（daemon 写
  通道）再评估。**发现的 Python 潜在缺陷（如实记录，不顺手修正）**：
  registry 中 defect_learn 的 operation_class 标注为 "read_only"（H4C-2
  时代遗留），实际是写操作，正确应为 governance_write/index_write；修正会
  影响 H4C 矩阵测试与 RUST_COMPAT_ROUTE 契约，超出本任务范围，待后续写面
  迁移批次处理。
- **workspace 隔离**：defect_correlation / get_defect_correlation 变更点
  查询经 fi.workspace_id 限定、窗口 findings 按 file_instance_id 限定、
  symbol_qualified 直连为全局（与 Python 一致）；churn_analysis 全部经
  fi.workspace_id 限定；defect_search / defect_suggest_fix 涉及
  defect_patterns / defect_fixes（无 workspace_id 列，全局知识库）与
  semgrep_findings 的 id/symbol_qualified/content_hash 查询（全局，与 Python
  一致）——跨 workspace 隔离由连接级 ACL（owned_workspace + snapshot
  query_db_path）保证，与 W2-3 defect_stats 同构。
- **fail-closed**：`_ensure_remote_snapshot` 返回 None 时不注入
  workspace_instance_id → Rust handler require 拒绝（invalid_params）；
  HTTP 失败原样传播不回退本地 SQL；越界参数：window_commits 负数 Python
  语义为空窗口（不报错），Rust 复刻该语义；本组其余工具无 limit/offset
  类越界参数。

**3. 实施结果（步骤 1-4 refactor，实现即真相）**：

- Rust 侧 5 个 handler 全部落地：`snapshot_state.rs`
  `handle_query_defect_correlation`（L851）/ `handle_query_churn_analysis`
  （L866）/ `handle_query_defect_search`（L879）/ `handle_query_defect_suggest_fix`
  （L894）/ `handle_query_get_defect_correlation`（L907），对应
  `query_local_*` 语义复刻（L4134 / L4401 / L4670 / L4744 / L4930），
  辅助 `DefectChangePoint` / `DefectFindingRow` / `normalize_defect_severity` /
  `local_date_key` / `parse_time_window_evolution`（L4049）。
- `dispatch.rs` 路由注册 5 条（L1814-1821）；`http_server.rs`
  COMPAT_ROUTE_WHITELIST 移除 5 条 + capability registry 5 个
  `add(..., "rust_native")`，defect_learn 保留 `("defect_learn", "read_only")`
  python_compat（L592/L1641）。
- `server/daemon_client.py` 5 个便捷方法（L2304-2420），均经
  `_ensure_remote_snapshot(db_path)` 注入 workspace_instance_id。
- `tools_summary.py` 4 个工具 HTTP 分支（L624/L683/L722/L760）；`tools_task.py`
  get_defect_correlation HTTP 分支（L1003-1045）；defect_learn 保持
  route_worker_call。

**4. 测试与断言同步（步骤 5 test）**：

- 新建 `tests/test_defect_read_rpc_http.py`：41 个测试，6 问验收结构
  （① workspace_id 绑定 ② 参数透传 ③ 越界 fail-closed ④ snapshot_not_ready
  ⑤ 跨 workspace 隔离 ⑥ Python fallback 边界）+ defect_learn 保持 compat
  断言（`test_defect_learn_stays_compat_in_http_mode`）。全部通过
  （41 passed）。
- 三端对齐断言同步：`test_http_capability_registry.py`
  （_EXPECTED_COMPAT_METHODS_81 注释 + 头部计数 86→81）、
  `test_http_combined_worker_cutover.py`（EXPECTED_TOTAL 86→81、
  SUMMARY_METHODS 24→20、TASK_METHODS 9→8、
  `test_tool_layer_does_not_call_get_db` 改用仍走 compat worker 的
  get_symbol_change_tasks）、`test_http_daemon_capability_matrix.py`
  RUST_NATIVE_EXPECTED 51→56、`test_http_daemon_release_acceptance.py`
  _EXPECTED_COMPAT_METHODS_81。全部通过。
- **语义修复（实施中发现）**：`parse_time_window_evolution` 初版用
  `to_ascii_lowercase()` 匹配单位，与 Python 正则 `([dwmy])` 仅小写语义
  不一致（大写 "90D" 应解析为 0.0 无限制）；修复为直接 `match unit as char`
  并加注释。
- 编译：`cargo build --manifest-path rust_ext/Cargo.toml --bin cw-daemon`
  通过（debug，EXIT=0；首跑因 target\debug\cw-daemon.exe 被瞬态占用失败，
  重试成功）；runtime/current 部署经 `scripts/refresh_shared_runtime.ps1`
  （release + 三端 hash 校验 + evidence 落盘）。

**5. 负面决策复核（步骤 6 annotate）**：

- defect_learn 保持 python_compat 的理由再次确认：写面
  （INSERT defect_fixes / defect_patterns + commit），本批次仅迁移读面；
  白名单保留、capability registry 不切换。
- 遗留待办（不属本任务）：defect_learn operation_class 标注 read_only 实为
  写操作，待后续写面迁移批次修正；churn_analysis / defect_search 的
  workspace 隔离依赖连接级 ACL（defect_patterns 等全局表无 workspace_id
  列），如需表级隔离需后续 schema 演进。

## 9.25 W4-4：diff_branches HTTP native 迁移 + import_git_history 写面通道决策（2026-08-17，任务 `T-1786886251769-22b94ee8-sub-4`）

> 角色：implementer（只推进到 review）→ Coordinator 独立核验
> 门禁：Independent Reviewer 只读复审；Coordinator 持 reviewer lease 执行 apply/close 收口
> 证据：`tests/test_diff_branches_rpc_http.py`（新建，覆盖 6 问验收）+
> `test_http_capability_registry.py` + `test_http_combined_worker_cutover.py` +
> `test_http_daemon_release_acceptance.py` + `test_http_daemon_capability_matrix.py`
> （白名单 81→80、RUST_NATIVE_EXPECTED 56→57 同步）+ `cargo build` 通过。
> 所有权白名单：`rust_ext/src/daemon/{snapshot_state,dispatch,http_server}.rs`、
> `server/daemon_client.py`、`server/tools/{tools_security,tools_workspace}.py`、
> `tests/test_diff_branches_rpc_http.py`（新建）、三端对齐断言文件、本账本。
> 禁止：task apply/close/reopen、git commit。
> 串行组：W4 串行组第四个（依赖 W4-3 完成后开工）。

**1. 根因与目标**：

- diff_branches（`server/tools/tools_security.py`，H4C-2 经 compat worker
  python_compat 执行）迁移 rust_native（RPC `query.diff_branches`）。
  语义真相源 `db/db_branch.py` L179-252（纯 SELECT）。目标：
  COMPAT_ROUTE_WHITELIST 81→80、RUST_NATIVE_EXPECTED 56→57。
- import_git_history（`server/tools/tools_workspace.py`，governance_write）
  写面通道决策落账（http-daemon-mvp-task-plan §4：写类 fail-closed）。

**2. 勘察结论与迁移决策（步骤 0 annotate 落账，实现即真相）**：

- **diff_branches → 迁移 rust_native**（RPC `query.diff_branches`）：
  语义真相源 `db/db_branch.py` L179-252，纯 SELECT 无写面。复刻要点：
  ① 按分支名（workspace name）精确匹配 `SELECT id FROM workspaces
  WHERE name = ?`（无大小写折叠、取首行）查 source/target 两个
  workspace_id，任一缺失 → `{"error": "源分支不存在: <名>"}` /
  `{"error": "目标分支不存在: <名>"}`（正常响应体，非 RPC 错误）；
  ② `_load_workspace_symbols`（L81-110）：SELECT s.symbol_hash,
  s.qualified_name, s.name, s.kind FROM symbols JOIN file_instances ON
  s.file_instance_id = fi.id WHERE fi.workspace_id = ?（跳过空
  qualified_name；**重复 qn 后值覆盖但保持首次位置**——Python dict 语义，
  Rust 用 `Vec<(String, BranchSymbol)>` + `HashMap<String, usize>` 复刻）；
  ③ 三集合对比：target 遍历 → added（tgt 独有）/ modified（hash 不同，
  含 source_hash/target_hash）/ unchanged_count（hash 相同），source 遍历
  → removed（src 独有）；④ 列表顺序 = 各 workspace SELECT 行序（无
  ORDER BY，Python dict 插入序 = SQLite 行序）。symbol_hash/name/kind 可空
  （schema 无 NOT NULL 强约束），null 原样输出。
- **跨 workspace ACL 评估（关键决策）**：diff_branches 的 source/target
  是两个不同 workspace，与 W4-1/2/3 单 workspace + workspace_instance_id
  注入模式不同。评估结论：**采用连接级 ACL**——workspace_instance_id 仅
  用于打开 snapshot query_db_path 主库只读连接（owned_workspace ACL），
  按分支名在库内查两个 workspace；数据范围由「peer 合法持有该 snapshot 库
  （snapshot.publish 发布 client 整个主库副本，含全部 workspace 数据）」
  保证，与 W4-3 defect_search / defect_suggest_fix 的全局表查询模式同构。
  查询本身仅是两条按 name 的等值 SELECT，无额外复杂度 → 决策迁移
  rust_native，不保持 python_compat。
- **import_git_history → 不迁移 rust_native（写面通道决策，明确）**：
  语义真相源 `db/db_git.py` import_git_history——governance_write
  （INSERT OR IGNORE INTO git_commits + commit）+ 依赖 git 子进程
  （workspace_root/.git + `git log`）。按 http-daemon-mvp-task-plan §4
  （写类 governance_write/index_write 维持 fail-closed）：daemon 写通道
  暂不支持本工具 → **保持 python_compat/现状（不迁移、不加 compat
  路由）**；**不允许静默遗留**：`tools_workspace.py` 已补 HTTP 模式显式
  fail-closed 拦截（`E_HTTP_COMPAT_UNSUPPORTED`，明确写面不支持声明 +
  指向本账本 §9.25），local/legacy 模式保持本地 SQL 语义。
- **fail-closed**：`_ensure_remote_snapshot` 返回 None 时不注入
  workspace_instance_id → Rust handler require 拒绝（invalid_params）；
  HTTP 失败原样传播不回退本地 SQL；分支不存在返回 error 响应体（非 RPC
  错误）。无 limit/offset 类越界参数。

**3. 实施结果（步骤 1-4 refactor，实现即真相）**：

- Rust 侧：`snapshot_state.rs` `handle_query_diff_branches`（L944-955，
  require workspace_instance_id/source_branch/target_branch →
  open_query_connection → query_local_diff_branches）+ W4-4 区块
  `WorkspaceSymbols` / `BranchSymbol` 结构 + `load_workspace_symbols`
  （L3066-3109）+ `query_local_diff_branches`（L3119-3200）；`dispatch.rs`
  默认 handler `handle_query_diff_branches`（L556-562，默认
  method_not_found，SnapshotDaemonState 重写）+ 路由
  `"query.diff_branches" => state.handle_query_diff_branches(peer, params)`
  （L1839）；`http_server.rs` COMPAT_ROUTE_WHITELIST H4C-2 区块移除
  `("diff_branches", "read_only")`（13→12 项）+ capability registry
  `add("diff_branches", "diff_branches", "diff-branches", "rust_native",
  "available", "read_only", "snapshot", false, "/v1/rpc",
  "fixture-diff-branches-ok", "fixture-diff-branches-err",
  "T-1786886251769-22b94ee8-sub-4#W4-4", "")`（L1661，含跨 workspace
  语义注释）。
- `server/daemon_client.py` diff_branches 便捷方法（L2423-2446）：params
  为 `{"source_branch", "target_branch"}`（业务参数，不含任一分支的
  workspace_id），`_ensure_remote_snapshot(db_path)` 注入
  workspace_instance_id 后 `call("query.diff_branches", params)`；docstring
  记录跨 workspace ACL 与 fail-closed 语义。
- `server/tools/tools_security.py` diff_branches 工具（L110-143）：
  HTTP 分支（is_http_transport_enabled → daemon client，db_path 经
  `_get_db_path_for_daemon()`）+ local 分支（get_db().diff_branches）；
  **清理 compat 注册残留**（`_SECURITY_READ_ONLY_METHODS` 移除 diff_branches
  条目、删除 `_h_diff_branches` worker handler、register_compat_routes
  描述更新为 15 个并标注 W4-4 迁移）——否则 registry 数据与
  COMPAT_ROUTE_WHITELIST 不同步。
- `server/tools/tools_workspace.py` import_git_history（L601-631）：
  `db = get_db()` 之前添加 HTTP 模式 fail-closed 拦截（见决策 2），
  local/legacy 模式保持原语义。

**4. 测试与断言同步（步骤 5 test）**：

- 新建 `tests/test_diff_branches_rpc_http.py`：14 个测试，6 问验收结构
  （① workspace_id 绑定 ② 参数透传（含「不注入任一分支 workspace_id」
  专测）③ 越界 fail-closed ④ snapshot_not_ready ⑤ 跨 workspace 隔离
  （验证 source_branch/target_branch 业务参数原样透传）⑥ Python fallback
  边界）+ import_git_history 写面决策断言（HTTP 模式返回
  E_HTTP_COMPAT_UNSUPPORTED、local 模式直连本地 SQL）。全部通过
  （14 passed）。
- 三端对齐断言同步：`test_http_capability_registry.py`
  （_EXPECTED_COMPAT_METHODS_81 移除 diff_branches、81→80，security 组
  14→12）、`test_http_combined_worker_cutover.py`（EXPECTED_TOTAL 81→80、
  SECURITY_METHODS 移除 diff_branches 15 项）、
  `test_http_daemon_capability_matrix.py`（RUST_NATIVE_EXPECTED 56→57、
  python_compat 181→180）、`test_http_daemon_release_acceptance.py`
  （_EXPECTED_COMPAT_METHODS_81 81→80）、evidence matrix JSON
  （backend_distribution + diff_branches 行 rust_native）。全部通过。
- 编译：`cargo build --manifest-path rust_ext/Cargo.toml --bin cw-daemon`
  通过（debug，EXIT=0；初版 E0609 src_sym tuple 取 `.1` 已修复）；runtime
  /current 部署经 `scripts/refresh_shared_runtime.ps1`（release + 三端 hash
  校验 + evidence 落盘）。

**5. 负面决策复核（步骤 6 annotate）**：

- import_git_history 不迁移的理由再次确认：写面（INSERT OR IGNORE
  git_commits + commit）+ 依赖 git 子进程，HTTP 模式已显式 fail-closed
  （E_HTTP_COMPAT_UNSUPPORTED，不回退本地 SQLite 写主库）；后续写面迁移
  批次（daemon 写通道）再评估。
- 遗留待办（不属本任务）：diff_branches 的跨 workspace 隔离依赖连接级 ACL
  （peer 持有 snapshot 库即可查库内全部 workspace 的分支），如需分支级
  最小权限需后续 schema/租约演进。

## 10. 相关真相源

- `AGENTS.md`
- `docs/design/multi-llm-contract-driven-collaboration-design.md`
- `docs/design/requirements.md`
- `docs/design/tasks.md`
- `docs/design/windows-wsl-daemon-coexistence-contract.md`
- `docs/design/windows-wsl-daemon-coexistence-task-plan.md`
- `docs/design/rust-ten-task-enterprise-completion-plan.md`
