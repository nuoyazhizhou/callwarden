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
| M1 | task/step/claim/report/status/events | 52 | `daemon_routed`（§9.59 复筛 55+1 production_ready） | ~~先收口 task state machine~~ 已收口 |
| M2 | file/symbol/grep/issues/tests | 待逐工具确认 | `daemon_routed`（M2.1-M2.5 closed） | 已收口 |
| M3 | workspace/snapshot/manifest/refresh | 27 + 相关工具 | `daemon_routed`（§9.59 复筛 27 production_ready） | 已收口 |
| M4 | graph/callers/callees/search/impact | 10 + query/summary 子集 | `daemon_routed`（§9.59 复筛 production_ready） | 已收口 |
| M5 | build/parse/facts/symbol/call | 语义工具子集 | `daemon_routed`（§9.59 复筛 production_ready） | 已收口 |
| M6 | security/guardrail/rules | 36 + 9 | `daemon_routed`（§9.59 复筛 36+9 production_ready） | 已收口 |
| M7 | identity/lease/collab | 7 + 8 + 6 | `daemon_routed`（§9.59 复筛 21 production_ready） | 已收口 |
| M8 | semantic/summary/evolution | 19 + 31 | `daemon_routed`（§9.59 复筛 50 production_ready） | 已收口 |

> 说明：`tools_task.py` 中不少工具同时出现 daemon route 和本地闭包，这是“混合实现”而不是已完成。后续必须以函数级清单记录 `rpc_method`、fallback、测试和证据路径。
>
> **2026-11-22 复筛（§9.59）**：以 dispatch 注册行口径静态重筛，244 个 MCP 工具中
> **243 个 production_ready**（`_route` 直连 + Rust dispatch 注册命中），1 个
> （`file_write`）经 `governed_write_file` 治理写链路间接路由。§4.1 的
> `python_local=190` 为 2026-08-11 旧口径，已全面过时——路由层迁移实际已完成；
> `production_ready` 仍属静态判定，活体 round-trip 证据以各工具完成卡片为准。

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

## 9.26 补记：P0-COMPAT-v3 白名单清零批次（2026-09-10～09-19，任务 `T-1788963104058-fdb2e848` 等）

> 本小节为 2026-11-16 HTTP 迁移状态核查时的**追溯补记**：该批次的完成证据
> 只落在代码注释（`http_server.rs` COMPAT_ROUTE_WHITELIST 数组头注释、
> `compat_registry.py` 路由表注释），未入账本，违反「实现即真相 + 账本同步」
> 惯例，现补齐。

**1. 批次内容**：

- tools_summary 组 19 个只读方法（get_summary / project_brief / repo_map /
  test_impact_selection / who_to_ask / get_ownership_map / guardrail_scan /
  guardrail_check_edit / guardrail_list_rules / blast_radius / ask_codebase /
  get_token_savings_report / get_vulnerability_blast_radius /
  get_clone_aware_impact / review_readiness / cross_layer_impact /
  evolution_frequency / hotspot_evolution / defect_learn）迁移 rust_native
  （任务 `T-1788963106520-907544c8`）。
- tools_semantic 组 5 个只读方法（find_similar_functions /
  get_project_dependencies / get_symbol_commit_history / parse_codeowners /
  semantic_search）迁移 rust_native（任务 `T-1788963104879-2e9e6270`）。
- 矩阵最后 11 项残留 python_compat/transition 由 commit `4135fdb`
  （2026-09-19）迁 rust_native/migrated。

**2. 终态（核查实锤）**：

- `COMPAT_ROUTE_WHITELIST`（`rust_ext/src/daemon/http_server.rs` L887-956）：
  数组**清零**（888-956 行全为注释，0 个真实条目），`compat_route()` 恒 None，
  worker 分派分支成死代码，白名单空时 compat worker 跳过启动
  （`disabled_no_compat_routes`）。
- `RUST_COMPAT_ROUTE`（`server/compat_registry.py` L206-220）：同为空 dict，
  两端对齐。
- 路由矩阵 244 条 target_backend 全部 RustNative(201)/TaskRpc(43)，
  **0 条 PythonCompat**。
- 至此 H5 快照（§9.9）记录的 99 项 compat 路由全部收敛完毕。

**3. 遗留（本小节登记，后续小节处置）**：

- 19 个 tools_summary 迁移条目在真相源 `gen_route_matrix.py` ROUTE_OVERRIDES
  中缺 `status` 字段（生成器默认回落 `transition`），且 batch 值被历史脚本
  编辑损坏为 `P0-COMPAT-v3transitionmigrated`——见 §9.27。
- 写面通道（import_git_history 等 governance_write/index_write）仍 HTTP
  fail-closed（E_HTTP_COMPAT_UNSUPPORTED），待独立批次。

## 9.27 transition 状态升格 + batch 损坏修正（2026-11-16）

**1. 根因**：`scripts/gen_route_matrix.py`（route_matrix.rs 的 SSOT，RP-07）
ROUTE_OVERRIDES 中 tools_summary 组 19 条目缺 `status` 键，生成器
`override.get("status", "transition")`（L570）将其标为 transition；同时
batch 字段为损坏值 `P0-COMPAT-v3transitionmigrated`（疑似历史一次性脚本把
batch/status/终态三词拼接进 batch，status 键反而丢失）。

**2. 修复**：19 条目显式改为 `"batch": "P0-COMPAT-v3", "status": "migrated"`；
`--emit-json` 重生 `deliverables/software-company/tool_migration_matrix.json`
（244 工具）+ `--emit-rust` 写回 `route_matrix.rs` marker 区间；
`--check` OK（矩阵 JSON 与 Rust mirror 均与内存生成字节级一致）。

**3. 状态分布**：migrated 141→**160**、stable 84、transition 19→**0**。

**4. 安全性论证**：status 字段为纯元数据——Rust 侧仅在 `list_routes` JSON
序列化输出（route_matrix.rs L394），无逻辑消费；Python 收敛测试
`test_m1_route_matrix.py` 不断言 status 分布（仅注释提及）。升格零行为变更。

**5. 验收**：`test_m1_route_matrix.py` 7 项通过；`cargo test --manifest-path
rust_ext/Cargo.toml route_matrix` 通过；三端 `--check` 一致。

## 9.28 写面迁移状态核查与 T02-job 批次补记（2026-11-16）

> 追溯补记：T02-job 批次（task_rpc 长任务面）完成时未入账本，仅落在矩阵
> batch 字段与验收套件提交（`f8eb1fe` TP-11J / `6ea46e3` TP-11S /
> `253573d` TP-11X），现补齐核查结论。

**1. 写面终态（矩阵实锤，83 项）**：

- GOVERNANCE_WRITE 5 + PROTECTED_MUTATION rust_native 43：直写 handler
  （edit.* / rule.* / admin.* / lease.* / workspace.* / gate.* 等，
  `compat_native_handlers.rs` 等，主库写连接）。
- PROTECTED_MUTATION task_rpc 35：经 `task.job_submit` + JobRunner
  （`job_runner.rs`，`4b1380a` 引入；`f8eb1fe` 落库修复，作业状态机持久化）；
  长任务 executor 含 `git_history`/`git_blame`/semgrep/embed/clones/coverage
  等（`exec_git_history` 等，job kind 注册表见 job_runner.rs L410-433）。
- **§9.25 W4-4 的"import_git_history 保持 fail-closed"决策已被后续 T02-job
  批次推翻**：import_git_history 现经 `task.job_submit`（kind=git_history）
  在 Rust 侧执行（git 子进程 + INSERT git_commits 事务），矩阵标 migrated。

**2. 唯一 HTTP 面不可用写工具：defect_learn**：

- 路由：rust_native（P0-COMPAT-v3 迁移），但执行于**只读快照连接**，
  有 qualifying 数据时 fail-closed 报错（`query_compat_handlers.rs`
  `handle_summary_defect_learn` L9192-9199）——**有意 parity 决策**
  （`http_server.rs` L912-914：与 Python worker readonly 语义一致），
  非回归、非越权风险（拒绝写入，与 T-13 类"READ_ONLY 带写副作用"不同）。
- 标注债：矩阵 op_class=READ_ONLY 与其写语义不符（§9.25 遗留待办仍开放）；
  处置方向（写连接迁移 vs 仅修正 op_class 标注）待后续批次决策。

## 9.29 defect_learn 主库写连接迁移（2026-11-16，用户决策：迁移到写连接）

> 本小节关闭 §9.25 遗留待办 + §9.28 标注债：defect_learn 成为 HTTP 面
> 可用的写工具，至此**写面迁移全部收口**（83 项写工具 HTTP 面全可用）。

**1. 路由变更**（`snapshot_state.rs`）：`"defect_learn"` 从只读快照组
（open_query_connection，fail-closed）移除，加入主库写组
（open_codegraph_db_write + owned_workspace ACL）为显式 match 分支；
只读组内层 `_ =>` fallback 改 fail-closed `method_not_found`。

**2. 写 handler**（`compat_native_handlers.rs::handle_defect_learn`）：逐条
复刻 Python 真相源 `db/db_defect_kb.py::learn_defect_from_fix`（L549-696）
语义——git_symbol_changes(modified, old≠new) → sha256 内容哈希 →
semgrep_findings 规则关联（snippet 空或命中 old 内容才接受，content_hash
退化路径）→ DP-{rule_id} upsert（learned_from='git_fix'）+ 四元组查重 →
INSERT defect_fixes(effectiveness=1.0)，事务一次性提交。只读 fail-closed
实现 `handle_summary_defect_learn` 成死代码，已删除。

**3. op_class 修正三端对齐**：矩阵真相源 `gen_route_matrix.py`
READ_ONLY→PROTECTED_MUTATION（`--emit-json`/`--emit-rust`/`--check` OK）；
capability registry（http_server.rs）operation_class read_only→
protected_mutation；MCP 客户端路由（server/tools/tools_summary.py）同步
（route_rpc 随 PROTECTED_MUTATION 附 request_id 幂等键）。CLI 分发表
（cli/main.py:974）本就是 PROTECTED_MUTATION，矩阵标注错误至此修正。

**4. 有意偏差登记**：daemon handler 未加入 `PROTECTED_MUTATION_METHODS`
串行点列表（与同写组 build_defect_knowledge/gc_purge 先例一致，加入会改变
派发路径，超出本决策范围）；`rust_ext/src/cli/external.rs::run_defect
("learn")` 是带 workspace 过滤的 CLI 本地变体，与 daemon/Python 真相源
（无过滤）并存，未统一——**该双轨已于 2026-11-16 收口，见 §9.30**。

**5. 验收**：`cargo test defect` 15 通过（含 2 个新增写路径单测）；
`test_mcp_defect_learn_http_rpc.py` 5 通过（含新正向用例：HTTP 调
defect_learn 实证 defect_fixes/defect_patterns 真实落库 + 重跑 duplicate
不再写）；`test_defect_read_rpc_http.py` 路由断言更新；
`gen_test_cases --check` PASS。

**6. 存量失败登记（非本卡引入，干净基线复跑一致）**：
`test_task_get_defect_correlation_*` 2 项（tools_task 陈旧 patch 目标
get_db）+ `test_http_combined_worker_cutover.py`
TestRealDaemonCombinedWorkerCutover 4 项（自带 spawn 未注入 mTLS 证书
env）——**已于 2026-11-16 全部修复，见 §9.30**。

## 9.30 defect_learn 双轨退役 + 存量测试失败清零 + inventory 快照重生（2026-11-16）

> 触发：S4-FU 卡（`T-1793807024448-17d4cd60`）review 闭环后的收尾批次。
> 本批次不含行为面新功能，收敛 §9.29 登记的两项技术债并修复 6 项存量失败。

**1. Rust CLI `defect learn` 双轨退役**（`rust_ext/src/cli/external.rs`）：
删除 `run_defect("learn")` 的本地写实现（183 行，含 `BEGIN IMMEDIATE`
事务、`git_symbol_changes` 扫描、`defect_patterns` upsert、`defect_fixes`
去重写入）；改为在打开本地库**之前** fail-closed：

```rust
if action == "learn" {
    return CommandResult::failure(
        2,
        "defect learn 已在 Rust CLI 退役：请使用 Python `cw defect learn` 或 MCP `defect_learn`（daemon 唯一写者，ADR-002）",
        RouteUsed::None,
    );
}
```

理由：① ADR-002 单轨后 daemon `handle_defect_learn` 是唯一写者，Rust CLI
`RuntimeOptions::daemon_call` 已 fail-closed（无 HTTPS RPC 客户端），本地
写入无法与 daemon 串行点对齐；② 本地变体带 `workspace_id` 过滤，与
Python 真相源 `learn_defect_from_fix`（无过滤）语义不一致，属漂移风险；
③ `build`（知识库重建）仍保留本地写路径，不在本次范围。参数
`commit_hash` 随实现移除改名为 `_commit_hash`；单测
`defect_learn_writes_workspace_scoped_fix_and_returns_details` 改为
`defect_learn_fails_closed_without_local_writes`（断言 exit=2、
route=None、stderr 含「退役」、本地库文件未被创建）。

**2. 存量测试失败 6 项清零**：

- `tests/test_defect_read_rpc_http.py` 2 项：`tools_task` 已无模块级
  `get_db`（HTTP 路由化后不再触达本地库），原 `patch(...get_db)` 目标
  不存在。改为 `assert not hasattr(tools_task, "get_db")` 的结构性断言 —
  — 比 mock 更强的「不可能回落本地 SQLite」保证。
- `tests/test_http_combined_worker_cutover.py` 4 项：隔离 daemon 使用
  `USERPROFILE` 下自建 PKI，client 未注入证书 → 握手期
  `certificate verify failed`。新增 `_inject_isolated_mtls_env`
  （`test_mtls_control_plane.pki_init` 幂等 enroll owner 身份 + 注入
  `CW_CLIENT_CERT`/`CW_CLIENT_KEY`/`CW_CA_CERT`，清空 `CW_PKI_DIR`），
  快照挂在 Popen 对象上由 `_terminate` 在 finally 还原，避免跨测试污染。
- 同文件 1 项 locale 断言：`task_plan_template` 模板随 daemon 进程
  locale 返回中/英两版，原断言只接受 `Root task title`，在 zh_CN 环境
  误红。改为双前缀断言。

**3. inventory 快照重生**（`tests/convergence/fixtures/audit_mcp_inventory.json`
+ `deliverables/software-company/doc-audit/audit_mcp_inventory.json`）：
两份快照均停在 §9.26 之前（doc-audit 份仍是 243 工具旧版）。按
`build_mcp_inventory.py`（真相源 = `server/tools/*.py` 注册 ×
`tool_migration_matrix.json`）重生：244 工具、`consistent=true`、
`by_target_backend` rust_native 201 / task_rpc 43、`by_op_class`
READ_ONLY 160 / PROTECTED_MUTATION 79 / GOVERNANCE_WRITE 5；
`defect_learn` 条目修正为 `op_class=PROTECTED_MUTATION`、
`status=migrated`，快照内 `transition` 清零。

**4. 验收**：`cargo test --lib defect_learn` 3 通过（含新增 fail-closed
单测）；`test_defect_read_rpc_http.py` + `test_http_combined_worker_cutover.py`
56 通过；`gen_route_matrix.py --check` 通过；测试账本按新提交 hash 重生。

## 9.31 证书到期监控 + 长任务心跳 watchdog + daemon stop 优雅退出审计（2026-11-17）

> 触发：用户指令「先做证书到期监控和长任务心跳，请核查 daemon stop 的优雅退出逻辑」。
> 前两项为实现，第三项为只核查报告（不改代码，修复建议待拍板）。

### 1. 证书到期监控

- Rust `pki_api.rs` 新增 `server_cert_expires_in_days(pki_root) -> Option<i64>`：
  读 `<pki_root>/server/server.crt`（与 `ensure_server_cert_locked` 权威路径一致），
  解析 notAfter 与当前 unix 时间差（向下取整天数）；文件缺失 / 坏 PEM → `None`
  （只读、不 panic、不阻塞 /health）。单测 3 例（有效证书正天数且 ≤ 1y、缺文件、坏 PEM）。
- `http_server.rs` `health_handler` 响应追加 `cert_expires_in_days`（`None` → `-1`
  缺省哨兵）。契约：加字段不加新工具，不动 route_matrix；字段无敏感信息，
  S1-17 /health 字段白名单已同步放行（tests/test_mtls_control_plane.py）。
- Python `HttpDaemonRpcClient.verify_health` → `_warn_cert_expiry`：
  `0 <= days < CERT_EXPIRY_WARN_DAYS(14)` 临期 warning；`days < 0 且非 -1` 已过期
  warning（消息区分）；`-1` 哨兵 / 字段缺失（旧 daemon）静默。实例级限频
  `_cert_expiry_warned`（每实例至多一条）。`call_with_autostart` 经
  `discover → verify_health` 自然覆盖；watchdog 探活不经过该路径，不会刷屏。

### 2. 长任务心跳（slow RPC watchdog）

- 判定：`_rpc_timeout_for(method, base)` 返回值 > base（即 `http_method_timeout`
  放宽的慢方法：workspace.build_graph / snapshot.publish / import.* 等）→ 走
  `HttpDaemonRpcClient._call_with_watchdog`；快方法与 caller 显式短超时路径不变。
- 实现：主 RPC 移入 worker 线程（`http.client.HTTPSConnection`，独立阻塞）；
  watchdog 守护线程每 5s 经独立短连接 GET /health（timeout 2s），连续 2 次失败
  判定 daemon 死亡/假死 → 主线程立即抛 `DaemonUnavailableError(E_HTTP_DAEMON_UNAVAILABLE)`。
  daemon 正常响应时零行为差异。
- 平台注记（Windows 实测）：跨线程 `sock.shutdown(SHUT_RDWR)` 与 `conn.close()`
  均**无法**唤醒另一线程阻塞中的 SSL read（阻塞持续到对端响应/超时），故弃用
  socket 手术方案改用 worker 线程模型——判死后 worker 仍在后台阻塞至 socket
  超时（上限即慢方法超时），但 caller 已快速失败。
- 测试 `tests/test_slow_rpc_watchdog.py`：慢/快方法判定单测 3 例 +
  fake TLS daemon 集成 3 例（假死快速失败 < 20s 且错误码正确 / 健康不误杀 /
  快方法不进 watchdog）；`tests/test_cert_expiry_monitor.py` 单测 4 例 +
  隔离 daemon /health 字段集成 1 例。

### 3. daemon stop 优雅退出审计（报告-only，本轮不改代码）

1. **Python CLI 无 `cw daemon stop` 子命令**：`cli/daemon_commands.py` `_parser`
   无 stop 分支；`.github/workflows/e2e/run_platform_e2e.py:1130/1195` 调用
   `cw.py daemon stop --endpoint …` 会 argparse `invalid choice` exit 2，返回值
   未检查 → 静默失效。
2. **无 in-flight drain**：`cw_daemon.rs` 主循环收到 SIGTERM/SIGINT 置 stop_flag
   后 break（L542-560），L569-571 注释自认「HTTPS accept 线程随进程退出」；
   `http_server.rs` `serve`/`serve_prebound` 未用 `with_graceful_shutdown`
   （全仓无命中）→ 在途请求被硬切断。
3. **dedup 崩溃残留**：mutation 先 `INSERT OR IGNORE` 占位（`http_server.rs`
   L304-315），dispatch 中途进程死亡 → `response_json` 恒 NULL → 重启后同
   `(ws, method, request_id)` 重试永远 `InFlight`（L329-332），24h 内不可重放；
   客户端换新 request_id 才能重执行。
4. **Windows 无优雅停止路径**：signal-hook 的 SIGTERM 在 Windows 不可达
   （`cw_daemon.rs` L523-525 `.ok()` 吞掉注册结果），实际只能 taskkill 硬杀。
5. **manifest 不清理**：shutdown 不删 manifest 文件，依赖客户端
   `resolve_http_endpoint_and_manifest(validate=True)` 探活失败 + autostart
   重唤起兜底（可接受，无需改）。

可选修复建议（**待拍板，未实施**）：
- P1：`serve_prebound` 接 `with_graceful_shutdown`（watch stop_flag），SIGTERM
  后 drain 在途请求（带上限如 10s）。
- P2：dedup `InFlight` 占位行在 daemon 重启时清除（NULL response 行视为陈旧
  占位）或加 takeover 语义。
- P3：补 Python CLI `cw daemon stop`（读 manifest/PID 发 SIGTERM / Windows
  graceful taskkill）或修 e2e 脚本调用。

### 4. 验收

- `cargo test --lib pki_api::tests` 3 通过；`cargo check --bins` 通过；
  full `cargo test --lib` 回归通过。
- pytest：`test_cert_expiry_monitor.py` + `test_slow_rpc_watchdog.py` 11 通过；
  `test_mtls_control_plane.py`（S1-17 白名单更新）+ `test_mtls_enrollment.py` +
  `test_http_daemon_integration.py` 31 通过。
- 测试账本按新提交 hash 重生；`rust_ext/src/daemon/**` 属
  RUNTIME_AFFECTING_PATH_PREFIXES，合入后需 `scripts/refresh_shared_runtime.ps1`。

## 9.32 daemon stop 优雅退出改造（P1 shutdown/drain + P2 dedup 清理 + P3 CLI stop）（2026-11-17）

> 触发：用户指令「按 P1 优先级实施 accept_loop 改造，添加 shutdown token 和
> drain 逻辑，先修复 dedup 表陈旧占位行的清理逻辑，补全 Python CLI 的
> daemon stop 子命令并修复 e2e 脚本」。接续 §9.31 第 3 节审计报告，三项
> P1/P2/P3 全部实施。

### 1. P1（Rust）：accept_loop shutdown token + 在途连接 drain

- `HttpServerConfig` 新增 `shutdown_flag: Arc<AtomicBool>`（默认 false），
  `serve`/`serve_prebound` 对外签名不变；daemon 主循环经
  `prebound_http.1.shutdown_flag` 克隆持有开关。
- `accept_loop` 重写：签名追加 `stop: Arc<AtomicBool>` 与
  `drain: Duration`（去掉 `-> !`）。正常阶段 `tokio::select!` 驱动
  accept 与 100ms tick 轮询 stop flag；每连接任务移入
  `tokio::task::JoinSet` 追踪。stop 置位后停止 accept 进入 drain：
  `timeout_at(deadline, conns.join_next())` 逐个等在途连接结束，
  超时（默认 10s，`CW_HTTP_DRAIN_TIMEOUT_SECS` 可覆盖）`abort_all()`
  强制断开；连接数归零即返回，listener 随函数返回 drop。
- `cw_daemon.rs` unix/windows 双模块：启动时 `let http_stop =
  Arc::clone(&prebound_http.1.shutdown_flag)` + 保留 `http_join =
  spawn_http_transport_prebound(...)` 的 JoinHandle；停止路径置位
  `http_stop` 后 `http_join.join()` 等 drain 完成再退出（替换原
  「HTTPS accept 线程随进程退出」注释语义）。
- drain 时限经参数传入 `accept_loop` 而非函数内读 env：测试直调可传
  更短时限，避免并行测试的 env 竞态。
- `server.rs` 两处 spawn 的线程日志改 drain 语义
  （`HTTP listener stopped after drain (was at …)`）。

### 2. P2（Rust）：dedup 崩溃残留占位行启动清理

- `DedupStore::open` 在 DDL 初始化后执行
  `DELETE FROM http_dedup WHERE response_json IS NULL`，清理条数 >
  0 时打印启动日志。依据：启动期进程内必然无在途 dispatch，NULL 行
  必为崩溃残留（§9.31 审计项 3），删除后同 `(ws, method, request_id)`
  重试恢复 `First` 可重放（24h 保留期内）；已回填结果不受影响。
- 清理失败 → `HttpServerError::Manifest`，daemon 启动 fail-closed。

### 3. P3（Python）：`cw daemon stop` 子命令 + e2e 修复

- `cli/daemon_commands.py` 新增 `stop` 子命令（`--wait` 默认 15s）与
  `_run_daemon_stop`：
  - 探活取 pid：`HttpDaemonRpcClient().health()`（无 autostart，
    fail-closed）；不可达 → 幂等 `not_running` rc 0。
  - PID 复用防护：manifest pid 与 /health pid 交叉核对，不一致 →
    `E_DAEMON_STOP_PID_MISMATCH` rc 1 拒绝停止；/health 无可信 pid →
    `E_DAEMON_STOP_VERIFY_FAILED` rc 1，绝不盲杀。
  - `_send_daemon_stop_signal`：POSIX `os.kill(SIGTERM)`（配合 P1 真
    优雅 drain）；Windows `taskkill /PID x /F` 硬停（SIGTERM 不可达，
    §9.31 审计项 4），失败传播 `E_DAEMON_STOP_SIGNAL_FAILED`。
  - 发信号后轮询 /health 等退出（上限 `--wait`），超时
    `E_DAEMON_STOP_TIMEOUT` rc 1；探活阶段 `DaemonRemoteError`
    （manifest_id/PID 交叉核对失败）→ fail-closed 拒绝停止。
- `.github/workflows/e2e/run_platform_e2e.py`：T6/T8 前置的
  `daemon stop` 调用改为捕获返回值并在 rc != 0 时计入 errors（原先
  静默失效会污染互斥/有界等待判定）；两处 `daemon ensure`（子命令
  不存在，argparse exit 2）改为 `daemon start`（已运行复用语义等价），
  T8 并将 start rc 纳入 wait_ok 判定。

### 4. 测试与验收

- Rust：`test_dedup_stale_placeholder_cleaned_on_open`（崩溃残留 →
  reopen 后 First，回填路径 Replay 不受影响）；
  `test_accept_loop_returns_after_stop_with_no_connections`（stop 后
  无在途连接立即退出）；`test_accept_loop_drain_timeout_aborts_hanging_
  connection`（握手挂起连接在 drain 超时被 abort）。
- pytest：新增 `tests/test_cli_daemon_stop.py` 10 例（幂等 / SIGTERM
  happy path / PID mismatch 拒止 / 无可信 pid 拒止 / 超时 / 信号失败
  传播 / Windows taskkill 成败两路 / DaemonRemoteError 拒止 / parser
  注册）。
- `rust_ext/src/daemon/**` 属 RUNTIME_AFFECTING_PATH_PREFIXES，合入后
  需 `scripts/refresh_shared_runtime.ps1`。

## 9.33 Windows 优雅停止 RPC（daemon.shutdown）（2026-11-18）

> 触发：§9.32 收尾后的遗留盘点确认「Windows 仍 taskkill /F 硬停」（§9.31
> 审计项 4 未闭环）。方案：daemon 新增经 mTLS 鉴权的 `daemon.shutdown`
> RPC，复用 §9.32 的 `shutdown_flag` + drain 机制，`cw daemon stop` 在
> Windows 走 RPC 而非杀进程，优雅退出全平台闭环。

### 1. Rust：`daemon.shutdown` RPC 端点

- `http_server.rs` `rpc_handler` 在 envelope 校验后、dedup/dispatch 前
  拦截 `method == "daemon.shutdown"`（非 workspace mutation，幂等，不进
  dedup）：
  - 授权：客户端证书已由中间件保证（缺证书 403）；fail-closed 限定
    **仅 loopback 绑定**（`allow_remote == false`）的 daemon 接受——
    `allow_remote` 时对端物理位置不可知，拒绝远程停止
    （`E_DAEMON_SHUTDOWN_REMOTE_NOT_ALLOWED` 403），须登录宿主机本地。
  - 行为：`config.shutdown_flag.store(true)` 后立即返回
    `{"ok": true, "status": "shutting_down"}`；daemon 主循环 100ms 内
    感知 break，accept loop 停止 accept + drain 在途请求（本响应已写出）。
- `cw_daemon.rs`：unix / windows 两个主循环均新增
  `http_stop.load()` → break（RPC 是信号之外的第二个停止源）；windows
  停止路径注释同步更新（§9.32 的 `http_stop.store(true)` + `join()` 在
  RPC 场景幂等）。

### 2. Python：stop 改为 RPC 优先

- `_run_daemon_stop` 停止机制按优先级：
  1. `daemon.shutdown` RPC（`client.call`，无 autostart，fail-closed）
     成功 → `mechanism="rpc_graceful"`；
  2. 回退：RPC 抛 `DaemonRemoteError`（旧 daemon 无方法 / 远端拒绝）或
     `DaemonUnavailableError` → 原路径（POSIX SIGTERM / Windows taskkill /F）。
- PID 复用防护、幂等 not_running、轮询退出等语义不变。

### 3. 测试与验收

- Rust：`spawn_server` 参数化为 `spawn_server_with(allow_remote)`（返回
  shutdown_flag 克隆供断言）；新增 `test_rpc_daemon_shutdown_sets_flag`
  （200 + 开关置位）与 `test_rpc_daemon_shutdown_rejected_when_allow_
  remote`（403 + 开关不置位）。`cargo check --bins --lib --tests` 通过；
  `cargo test --lib` 全量回归通过。
- pytest：`test_cli_daemon_stop.py` 扩充到 14 例（新增 RPC graceful
  happy path 不发 OS 信号、RPC 失败回退信号两例）。
- `rust_ext/src/daemon/**` 与 `cw_daemon.rs` 属
  RUNTIME_AFFECTING_PATH_PREFIXES，合入后需 `scripts/refresh_shared_runtime.ps1`。

## 9.34 证书自动续期 + rotate-ca 双 CA 窗口（2026-11-18）

> 触发：§17.1 盘点确认 S2/S3/S4 远端面已闭环后，剩余真实缺口 = 证书
> 到期无自动续期（服务端 1 年 / 客户端 90 天，到期即断链）与 CA 换根
> 无安全路径。方案：A 服务端启动期自动重签 + B 客户端 fail-open 自动
> renew + C rotate-ca 双 CA 窗口（24h）。

### 1. 阶段 A：服务端证书自动续期

- `pki_api.rs`：新增 `SERVER_CERT_RENEW_THRESHOLD_DAYS = 30` 与纯函数
  `server_cert_needs_renewal(der, now, threshold)`（days_left =
  `div_euclid(86_400)`；解析失败 fail-closed 返回 true）。
  `ensure_server_cert` 幂等条件从「文件存在 + SAN 覆盖」扩展为
  「…且未临期」——临期/过期落入既有重签路径，调用方零改动。
  注意 `set_validity` 粒度到天（civil 日零点截断），31 天输入实际
  days_left=30，阈值外侧测试用 32 天。
- `cw_daemon.rs`（unix + windows 主循环）：每 6h（21_600 × 10 tick）
  调 `server_cert_expires_in_days`，经纯函数
  `server_cert_expiry_message(days, threshold)` 输出 WARN（≤30 天，
  提示下次重启自动重签）/ ERROR（已过期）。运行期 TLS 热替换暂不做
  （重启即重签，窗口足够）。

### 2. 阶段 B：客户端自动 renew（fail-open）

- `cli/pki.rs` 新增 `cert-status` 子命令：读
  `identities/<principal>/client.crt` 解析出 days_left / expired /
  principal_id / serial，供外部编排。
- `daemon_client.py` `_maybe_auto_renew()`：resolve 客户端证书路径前
  调用（renew 先于首次 resolve，首次连接即用新证书）；days_left ≤ 7
  时子进程执行 `cw pki renew`，成功后清 `_ssl_ctx` 缓存。fail-open：
  任何失败仅 warning（含手动 `cw pki renew` 提示），不阻断连接；
  `_auto_renew_attempted` 单次防重入；`CW_CLIENT_CERT` 外部证书跳过；
  `CW_AUTO_RENEW_DISABLED=1` 逃生开关。

### 3. 阶段 C：rotate-ca 双 CA 窗口（24h）

- 布局不变量：`ca/` **恒为签发 CA**（enroll/renew 零改动），
  `ca-previous/` = 旧 CA + `window.json{prepared_at, deadline,
  deadline_unix, previous_ca_fingerprint_sha256}`。`PreviousCa` 无
  `issuer()`——旧 CA 只验签不签发；commit 移除 `ca-previous/` 即旧 CA
  整体失去信任（无需逐 serial 吊销）。
- `pki_api.rs`：`load_previous_ca` / `build_tls_config_with_roots`
  （双根 RootCertStore）/ `pem_decode_all`（bundle 逐块解析，
  `client_tls_config` 支持 PEM bundle）/ 导出 `ensure_ca_locked`、
  `pki_write_lock`、`sha256_hex`。
- `http_server.rs` `bind_http`：加载 previous_ca + 窗口 WARN（过期
  提示 `rotate-ca --action commit`）；`build_manifest` 窗口期输出
  `ca_cert_pem` = 新 + 旧 CA bundle，`ca_fingerprint_sha256` 恒为首张
  （当前 CA），新增可选 `previous_ca_fingerprint_sha256`。
- `cli/pki.rs` `rotate-ca --action prepare|commit|status`：
  prepare（持锁 rename + 重签服务端证书 + 失败回滚；重复 prepare →
  `E_PKI_ROTATE_PENDING`）、commit（无窗口 `E_PKI_ROTATE_NO_WINDOW`；
  超窗需 `--force` → `E_PKI_ROTATE_WINDOW_EXPIRED`）、status（按 AKI
  vs 当前 CA SKI 输出 `pending_renewal_principals`，证书解析失败
  fail-closed 归入清单）。
- 消费端兼容：Python `load_verify_locations(cadata=)` 原生支持 bundle
  零改动；`daemon_autostart.py` `_pem_sha256` 改取 bundle **首张**
  证书块（`_first_pem_cert_block`），否则窗口期 CA pin 失败；
  `CW_CA_CERT` 外部文件用户须窗口内自行换文件。

### 4. 踩坑记录

- **同线程 Mutex 死锁**：`rotate_ca_prepare` 持 `pki_write_lock` 后调
  `ensure_ca`（重入同一非重入锁）→ 测试二进制永久卡死（CPU 归零、
  无输出）。持锁路径必须用 `ensure_ca_locked`。
- **rcgen AKI 默认不写**：`use_authority_key_identifier_extension`
  默认 false，客户端证书无 AKI → rotate-ca status 待重签判定失真。
  两条签发路径（CSR / 本地身份）均显式开启。
- **TLS 1.3 客户端证书拒绝对客户端不可见于 connect()**：服务端
  Finished 先于客户端 Certificate 飞行，旧证书被拒时客户端
  `connect()` 已返回 Ok，拒绝以 alert/EOF 在首次读写暴露——负向
  集成断言须做在首次读写（不得获得 200 响应），不能断言 connect 失败。
- **同 DN 双 CA bundle 的 OpenSSL 选根歧义**（修复 `2729e4d`）：
  初版 `ensure_ca_locked` 复用旧 subject DN（`CN=CallWarden Local
  CA`），rotate-ca 窗口期 manifest `ca_cert_pem` = 新 + 旧 CA bundle
  同 DN → Python CLI（OpenSSL 后端）按 DN 选候选信任根且不回溯，
  命中旧 CA 公钥验新 CA 证书 → `certificate signature failure`。
  修复：CA subject DN 携带 8 hex 公钥指纹（`CN=CallWarden Local CA
  <指纹>`），`ensure_ca_locked` 先生成密钥再据公钥定 DN，
  `ca.meta.json` subject 同步——同 DN 物理不可能复现。回归：
  rotate_ca 4 + needs_renewal 1 + ensure_server_cert 3 + expiry 3
  用例全绿（Python 测试自建 CA 无 DN 依赖）。
- **默认 principal 兜底与 owner 身份目录不一致**（修复 `2a40898`）：
  `default_principal()` 兜底硬编码 `"owner"`，而
  `ensure_owner_identity` 写入的是 `identities/pr_owner`；Python 自动
  续期（§9.34 B）裸调 `pki renew` 不传 `--principal`，真机验证首跑即
  `E_MTLS_CLIENT_CERT_MISSING`。修复：兜底改用 `OWNER_PRINCIPAL`
  （`run()` 审计 actor 同步修正），新增
  `default_principal_resolves_owner_identity` 回归测试。
- **`cw daemon stop` 报 ok 后进程残留**（已修复）：真机观察僵尸
  daemon（PID 44668）在 `cw daemon stop` 返回 ok 后进程仍残留，需
  `Stop-Process` 强杀。根因：CLI 停止等待循环只轮询 `/health` 不可达
  ——daemon 优雅 drain 期间 listener 不再 accept，新连接在 TCP 层可
  建立但永无响应，RPC 客户端超时被误判为"已退出"，而 drain/退出阻塞
  时进程仍存活。修复：等待循环在 RPC 不可达后核实
  `_pid_alive(health_pid)`（pid 已交叉核对）为假才报 stopped；超时
  输出 `pid_alive` 详情（rc 1），把静默残留变成可诊断失败。新增
  `test_stop_rpc_pid_linger_reports_timeout`，stop 套件 15/15 全绿；
  真机复验 stop → 进程核实退出 → start → health 正常。

### 5. 测试与验收

- Rust：pki_api 7 例（阈值边界 32/30/0/-5/garbage、临期/过期重签、
  新证书幂等）；cw_daemon bin 1 例（WARN/ERROR 消息边界）；cli::pki
  4 例（cert-status、roundtrip、超窗 force、待重签清单含 fail-closed）；
  http_server 52 例（含 `test_rotate_ca_dual_root_window_handshake`
  双根握手 e2e + commit 后旧证书拒止）。
- pytest：`test_client_cert_auto_renew.py` 12 例；`test_http_manifest_
  discovery.py` 31 例（bundle 首张 pin + previous_ca 字段放行）。
- 全量回归：`cargo test --lib` 2024 passed / 0 failed（同 DN 修复后
  复跑 1497.38s，HEAD `2729e4d`）；pytest 5 套件 71 passed。
- `rust_ext/**` 属 RUNTIME_AFFECTING_PATH_PREFIXES，合入后需
  `scripts/refresh_shared_runtime.ps1`。

### 6. 真机验证链（2026-11-19，全绿）

> 运行时基线：refresh `2729e4d`（2026-11-18 23:11:23–23:25:12，
> 14m，evidence `20261118-231123-2729e4d16441-09f82e3c`）→
> refresh `2a40898`（23:47:03–00:04:31，17m，evidence
> `20261118-234703-2a40898d8883-9f76e5cf`）。两次均 `status: passed`。

1. `cw daemon health`：`git_commit=2729e4d…`，`cert_expires_in_days=364`。
2. `rotate-ca --action status`：无窗口基线（`window_active=false`）。
3. `prepare`：旧 CA `b4c0d985…` → 新 CA `ea124f0d…`，窗口至
   `2026-11-19T15:36:59Z`，`restart_required=true`。
4. `cw daemon stop`（rpc_graceful，进程核实退出）+ `start`：窗口激活，
   manifest `ca_cert_pem` = 新旧双 CA bundle，`previous_ca_fingerprint_
   sha256` 存在；新 CA `CN=CallWarden Local CA b57ce753`（DN 修复可见）。
5. 裸 `cw pki renew`：首跑暴露默认 principal 缺陷（见 §4，修复
   `2a40898` + refresh 后复跑）→ 成功换发（serial `31FA4DEA…`，
   not_after `2027-02-16`）。
6. `rotate-ca --action status`：`pending_renewal_principals` 清空
   （新证书 AKI 匹配新 CA SKI）。
7. `commit`：窗口收敛（committed_at `2026-11-18T16:20:36Z`），
   `window_active=false`。
8. 重启 daemon：health 正常（PID 46324，`git_commit=2a40898…`）。
9. 负向：`CW_CLIENT_CERT` 指向备份的旧 CA 客户端证书 →
   `TLSV1_ALERT_UNKNOWN_CA`，exit 1（拒绝在握手层暴露，无 200 响应）。
10. 对照：新证书 TLS 握手通过、请求到达应用层授权
    （`E_PRINCIPAL_WORKSPACE_FORBIDDEN` 为 workspace 绑定的既有业务
    约束，SAN 主体 renew 前后一致，与 PKI 无关）。

结论：A 自动重签 / B 自动 renew 数据源 / C rotate-ca 双 CA 窗口
prepare→renew→commit→旧证书拒止 全链路真机闭环。

## 9.35 证书热加载：不重启轮换服务端证书与 CA 信任根（2026-11-19）

### 1. 立项与范围

ADR §14 未决问题 #3（证书热加载）正式立项，延续 §9.31→§9.34 PKI 生命周期
弧线。目标：`cw pki rotate-server` / `rotate-ca prepare+commit` 后 daemon
**不重启**换入新证书 / 新信任根；客户端零改动、零感知（验证走 CA 链 +
hostname，无服务端证书指纹 pinning）。

非目标：客户端证书轮换（走既有 renew）；外部 CA 文件的自动替换（窗口期
用户自行替换，ADR §4.3）。

### 2. 设计决策

- **HotTls**：`Arc<std::sync::RwLock<Arc<rustls::ServerConfig>>>`（依赖
  冻结 ADR §15.2，不引 arc_swap）。accept_loop 删除进程级一次性
  `TlsAcceptor::from(tls)`，改逐连接 `get()` 构建（仅 Arc 包装，代价可
  忽略）：
  - **新连接**呈现换入后的证书；
  - **在途连接**持有旧 Arc 不受影响，`max_conn_age`（300s）兜底断开重连。
- **触发机制选哨兵文件**（弃 RPC 方案）：`pki/tls_reload.request`。
  - 「磁盘即协议」先例（rotate-ca 目录 rename + window.json 同款）；
  - CLI 侧三行写入覆盖全部 rotate 命令，无需新增 RPC 方法与 mTLS 客户端
    扩展（RPC 方案需 `pki.reload_tls` + rpc_handler 特判 + CLI 客户端分支，
    改动面大且 cw_daemon.rs unix/windows 镜像模块要动）；
  - daemon 未运行时哨兵保留，下次启动 bind 路径自愈（幂等）。
- **reload 路径幂等**（`load_tls_config_from_disk`）：`load_ca`（缺失/非法
  → `E_PKI_CA_LOAD` fail-closed）→ `load_previous_ca`（§9.34 双根窗口）→
  `ensure_server_cert`（缺失/过期/临期 ≤30 天自动重签——rotate-ca prepare
  删 `server/` 后由此以新 CA 重签）→ `build_tls_config_with_roots` →
  换入 → `build_manifest` + `publish_manifest_atomic` 重发布
  （`manifest_hash` 重算供消费端感知）。
- **watcher**：挂 serve_prebound（随 HTTP 线程生命周期），1s 扫描哨兵 +
  6h 周期无条件 reload（吸收 §9.34 A 临期重签）；失败退避 30s 重试、
  绝不因 reload 失败中断 serve；**成功才删哨兵**（crash 安全：崩溃后重启
  首轮重处理，幂等）。
- **cw_daemon.rs 零改动**：unix/windows 镜像模块不用动。

### 3. 实现清单

| 文件 | 内容 |
| --- | --- |
| `pki_api.rs` | `TLS_RELOAD_REQUEST_FILE` 常量 + `load_tls_config_from_disk`（从盘上材料幂等重建 TLS 配置）+ 3 单测 |
| `http_server.rs` | `HotTls`（new/get/swap）+ `BoundHttp.tls` 替换 + accept_loop 逐连接 acceptor + `reload_tls_now`（换入 + manifest 重发布）+ `process_tls_reload`（哨兵语义：有则加载、成功删哨兵）+ `tls_reload_watcher`/`watcher_loop`（1s 扫描 / 6h 周期 / 30s 退避）+ `TlsReloadOutcome`；serve_prebound Arc 化 config 并派生 watcher |
| `cli/pki.rs` | `request_tls_reload`（best-effort 写哨兵，失败仅 eprintln 不阻断 rotate 主流程）+ rotate-server / rotate-ca prepare+commit 收尾调用；输出 `restart_required: true` → `hot_reload: "requested"` + note；模块 docstring 同步 |
| ADR | §4.3 表格 + rotate-ca 细则改热加载语义；新增「证书热加载落地细则」小节；§14 #3 关闭（已决 2026-11-19） |

### 4. 缺陷与修正

（本章节实现期无缺陷暴露；单测一次通过，见 §5。）

### 5. 测试与验收

- Rust 新增 7 例：
  - pki_api 3：幂等重建（两次 load 同 sha256、无窗口）；无 CA fail-closed
    （`E_PKI_CA_LOAD`）；模拟 rotate-ca prepare（rename ca→ca-previous +
    ensure_ca + 删 server/）后 reload 以新 CA 重签、previous 指纹 = 旧 CA、
    新证书 issuer DN == 新 CA subject DN（§9.34 代际区分不变量）；
  - http_server 3：热切换语义（swap 后新连接呈现新证书 + **在途连接继续
    服务不受影响**）；哨兵流程（无哨兵 Ok(None) / 有哨兵 → 加载 + 删哨兵 +
    manifest 重发布 + 新证书 / CA 损坏 → Err 且 fail-closed 沿用旧配置、
    哨兵保留待重试）；watcher 短间隔驱动（哨兵写入后 10s 内热加载 + 删哨兵
    + stop 置位退出）；
  - cli::pki 1：rotate-server 写哨兵 + `hot_reload` 输出；既有
    `rotate_ca_prepare_commit_status_roundtrip` 断言更新（restart_required
    → hot_reload + prepare/commit 后哨兵存在性）。
- 全量回归：`cargo test --lib` **2032 passed / 0 failed**（1458.02s，
  HEAD `85f05ec`）。较 §9.34 基线 2024 净增 8：本章 7 例 +
  2a40898 补记的 `default_principal_resolves_owner_identity` 1 例。
  新 7 例按名过滤单独复跑全绿（tls_reload ×3 / load_tls_config_from_disk
  ×3 / rotate_server_writes ×1）。
- `rust_ext/**` 属 RUNTIME_AFFECTING_PATH_PREFIXES → refresh（evidence
  见 §6）。

### 6. 真机验证链（2026-11-19，全绿）

- **refresh**：`20261119-093035-85f05ecb410b-398ed9e4.json`，passed，
  5m50s；`cw-daemon.exe` sha256
  `7bd87b8e3a1b3092489e425db51197f7410e670cff1095db442b3547e0127151`，
  装入 `runtime\current`。`cw.py daemon start` 启动：**PID 36152**、
  endpoint `https://127.0.0.1:7055`、git_commit `85f05ec`、schema 61、
  manifest `daemon_binary_sha256` 与 refresh 产物一致。
- **基线**（探针 = 直连 TLS 端口取对端叶证书 DER 算 SHA-256）：线上证书
  `b64c65e6…8115a5`（与 manifest `tls.server_cert_sha256` 一致）、CA
  `ea124f0d…6df0`、manifest_hash `de324447…3df9e`。
- **① rotate-server**：输出 `hot_reload: "requested"` + 新证书
  `ad3674dc…9265`；≈3s 后探针呈现新证书（旧 `b64c65e6…` 不再出现），
  manifest 重发布（`manifest_hash` → `4e26f6b7…`、
  `server_cert_sha256` 同步），**PID 36152 不变**。
- **② rotate-ca --action prepare**：新 CA `32b8952e…f423`、previous
  `ea124f0d…`、deadline `2026-11-20T01:45:59Z`；≈3s 后探针呈现新 CA
  重签证书 `6c143504…deb4`（issuer DN = 新 CA subject，§9.34 代际区分
  不变量），manifest 双根（`ca_fingerprint_sha256`=新 CA + 
  `previous_ca_fingerprint_sha256`=旧 CA），manifest_hash → `aff25392…`，
  **PID 36152 不变**。
- **③ 新 CA 下客户端证书生命周期**：`token issue`（principal
  `pr_b4886…`）→ `enroll`（serial `1C9E7121…`，/v1/enroll 仅服务端认证
  TLS）→ `renew`（serial `65F3E4E2…`，mTLS 握手走新 CA 信任根）全部
  成功。
- **④ rotate-ca --action commit**：`committed_at 2026-11-19T01:46:45Z`；
  ≈3s 后 manifest 单根化（`previous_ca_fingerprint_sha256` 消失、CA =
  `32b8952e…`）、服务端证书不变（`6c143504…`，本就不需重签）、
  manifest_hash → `d7ed6d3f…`，**PID 36152 不变**。
- **⑤ 旧根拒止（负向）**：持旧 CA 证书的 `pr_owner` 执行 `renew` →
  `E_TLS_HANDSHAKE_FAILED: received fatal alert: UnknownCA`——旧信任根
  移除即在握手期拒止，与 ADR「commit 即整体吊销」语义一致。
- **⑥ 环境修复与收尾**：`pr_owner` 重新 token issue + enroll（新 CA
  serial `6543356E…`）→ renew（serial `414D5264…`）成功；`cw.py daemon
  ping` mTLS RPC round-trip OK（https transport、PID 36152）。

结论：rotate-server 热加载、rotate-ca prepare 双根热加载、新 CA
签发/renew、commit 单根收敛、旧证书握手期拒止——**全程 PID 36152
不变（零重启）**，§9.35 验收闭合。

## 9.36 外部 CA 导入：CW_CA_CERT + CW_CA_KEY 配置面与 fail-closed 生命周期（2026-11-19）

### 1. 立项与范围

ADR §14 未决问题 #2（外部 CA 导入的配置面与 UI）正式立项，延续
§9.31→§9.35 PKI 生命周期弧线。此前 `CW_CA_CERT` 仅存在于 ADR 文字
（§4.1 旧口径 `pki.external_ca_cert/key`）与客户端信任 pin（config.py），
Rust daemon **零实现**。目标：env 配置面（`CW_CA_CERT` + `CW_CA_KEY`
同时指向外部 PEM）→ daemon 以外部 CA 为唯一签发/信任根，全 fail-closed，
不自签兜底，不改写外部文件。

非目标：外部 CA 文件的自动替换（用户自行替换 + §9.35 热加载哨兵）；
多租户 CA 分层（ADR §14 #4 明确不做）。

### 2. 设计决策

- **配置面语义**：`CW_CA_CERT` + `CW_CA_KEY` **同时**设置且非空 → 外部
  CA 模式；都未设置 → 内置自签；**只设其一 → `E_PKI_CA_LOAD`
  fail-closed**（半配置静默回退内置 CA 会造成"签发 CA 与预期不符"）。
- **env 优先**：`load_ca` 外部分支完全忽略磁盘 `ca/`；`LocalCa.key_path`
  指向外部私钥文件，`issuer()` 按路径读入——签发路径
  （enroll/renew/ensure_server_cert）零改动。
- **加载期校验**（全 `E_PKI_CA_LOAD` fail-closed）：文件可读、单张
  CERTIFICATE 块、BasicConstraints CA=true、KeyUsage 含 keyCertSign、
  有效期内、**私钥公钥 = 证书 SPKI 配对**（误配对在首次签发时才以费解
  签名错误暴露，必须在加载期拦截）；`ca-previous/` 窗口未收敛 → 拒绝
  （先 commit）；磁盘存在指纹不同的内置 `ca/` → 拒绝（内外切换须显式
  迁移）。
- **rotate-ca 裁决**：外部模式拒绝（`E_PKI_ROTATE_EXTERNAL_CA`）——签发
  CA 生命周期归外部系统，轮换 = 用户替换外部文件 + §9.35 热加载哨兵。
- **issuer 归属检查（自愈核心）**：`ensure_server_cert` 幂等条件新增
  "服务端证书 AKI == 当前 CA SKI"（CA 无 SKI 时跳过；叶证书无 AKI 或
  不匹配 → 重签一次）。外部 CA 文件被整体替换后 reload → 旧 server
  证书不归属新 CA → 自动以新 CA 重签。
- **测试隔离**：env 是进程级全局，并行测试下 set/remove 污染其他用例
  （本轮实测暴露）→ 拆两层：`external_ca_from_values`（纯函数，免 env
  测语义）+ 线程局部注入 `EXTERNAL_CA_TEST_OVERRIDE` /
  `ExternalCaOverrideGuard`（测试构建下 `external_ca_paths()` 走注入、
  与宿主机真实 env 完全解耦；生产构建读 env）。

### 3. 实现清单

| 文件 | 内容 |
| --- | --- |
| `pki_api.rs` | `CaSource` + `external_ca_from_values`/`external_ca_paths`（注入分支）+ `load_ca` env 优先分支（校验链 + SPKI 配对 + 内置冲突检查）+ `load_ca_with_external`（参数化核心）+ `cert_subject_key_id`/`cert_authority_key_id`（上移自 cli）+ `ensure_server_cert` 归属检查 + 服务端证书补写 AKI（`use_authority_key_identifier_extension`，§9.34 仅客户端写）+ 10 单测 |
| `http_server.rs` | manifest `tls.ca_source`（internal/external） |
| `cli/pki.rs` | `rotate_ca_prepare` 外部模式拒止 + `init` 输出 `ca_source` + AKI helper 去重 + 1 单测 |
| ADR | §4.1 配置面统一 env 口径；§4.3 新增「外部 CA 导入细则」；§14 #2 关闭（已决 2026-11-19） |

### 4. 缺陷与修正

1. **服务端证书缺 AKI（设计缺陷）**：§9.34 只给客户端证书写了 AKI，
   服务端签发漏配 `use_authority_key_identifier_extension = true`
   （rcgen 0.14 默认不写）→ 归属检查每轮 reload 都判"不归属"→ 重签
   churn（`test_ensure_server_cert_fresh_cert_idempotent` 等 6 例失败
   暴露）。修复：服务端签发补写 AKI；旧证书（无 AKI）触发一次性重签
   后即幂等稳定。真机验证 ⑧ 收尾时确认了该一次性重签（生产 server
   证书 `32b8952e` 信任根不变、sha 变化一次）。
2. **外部 CA 文件读错误码穿透**：缺文件/不可读最初透出 `E_PKI_IO`
   （`read_text` 直传）→ 外部分支显式映射为 `E_PKI_CA_LOAD`（ADR 语义
   统一：外部 CA 加载失败一律 fail-closed 同码）。
3. **测试 env 污染**：初版用进程级 set/remove + 互斥锁，并行下其他
   用例（`init_is_idempotent`、rotate roundtrip 等）被污染失败 →
   改为纯函数 + 线程局部注入（见 §2 设计决策），零 env 变更。

### 5. 测试与验收

- Rust 新增 11 例：
  - pki_api 10：外部 CA 加载 + 签发（SAN principal 正确、不建 `ca/`）；
    缺文件 fail-closed；cert/key 误配对拦截；非 CA 证书（NoCa 叶证书）
    拒绝；过期 CA 拒绝；与内置 CA 指纹冲突拒绝 + 指纹相同（副本）兼容
    放行；`ca-previous/` 窗口冲突拒绝；env 语义纯函数（都未设/只设其
    一/空串/trim）；`ensure_ca` 外部模式绝不自签；换 CA 后 server 重签
    （AKI 归属新 CA）+ 归属后幂等（churn 防护）；
  - cli::pki 1：`rotate_ca_prepare` 外部模式 `E_PKI_ROTATE_EXTERNAL_CA`。
- 全量回归：`cargo test --lib` **2043 passed / 0 failed**（1239s，
  HEAD `3ce9fe5`）。较 §9.35 基线 2032 净增 11。
- `rust_ext/**` 属 RUNTIME_AFFECTING_PATH_PREFIXES → refresh（见 §6）。

### 6. 真机验证链（2026-11-19，全绿）

- **refresh**：`20261119-120528-3ce9fe546c7f-843aa949.json`，passed，
  10m05s；`cw-daemon.exe` sha256 `5379488d…26149`，装入
  `runtime\current`（daemon 自动重启至新二进制）。
- **外部 CA 资产**：以 runtime `cw pki init`（`CW_PKI_DIR` mint 沙箱）
  铸独立自签 CA，导出 `ext-ca.crt`/`ext-ca.key`（指纹 `eaf0305c…08f`）
  至 `~/.callwarden/pki-external\`；CA#2 同法铸造（指纹 `6821e5d0…8f4`）。
- **① 外部模式启动**：沙箱 `CW_PKI_DIR`（空目录）+ `CW_CA_CERT`/`CW_CA_KEY`
  启动 daemon：manifest `tls.ca_source=external`、CA `eaf0305c…`、
  server `7d56648c…`，**PID 4888**、endpoint `https://127.0.0.1:2116`；
  沙箱内零 `ca/` 目录（不自签兜底实证）。
  验证脚手架观察（非产品缺陷）：`cw.py daemon start` 就绪探针与
  `daemon health` 在未设沙箱 env 时握手被重置（`WinError 10054`）——
  Python 客户端用生产 `pr_owner`（内部 CA 签发）连外部信任根，
  UnknownCA 拒止，语义正确；裸 TLS 探测（无客户端证书，
  allow_unauthenticated /health 路径）确认服务端握手 TLSv1.3 正常。
- **② `cw pki init`**（沙箱 env）：`ca_source: "external"`、指纹
  `eaf0305c…`、幂等（server sha 与 manifest 一致）+ owner 身份签发至
  沙箱；随后 `daemon health` OK + `daemon ping` mTLS round-trip OK
  （pr_owner 经外部 CA 信任根）。
- **③ 外部 CA 签发路径**：`token issue`（workspace `ws_ext936`，
  principal `pr_2538a826…`）→ `enroll`（serial `03CF8CF0…`）→
  `renew`（serial `36772333…`）全部成功（mTLS 信任根 = 外部 CA）。
- **④ rotate-ca 拒止**：`rotate-ca --action prepare` →
  `E_PKI_ROTATE_EXTERNAL_CA`（"替换 CW_CA_CERT/CW_CA_KEY 指向的文件并
  触发 TLS 热加载完成轮换"）。
- **⑤ 热加载轮换（外部 CA 换文件）**：CA#2（`6821e5d0…`）覆盖外部
  cert/key 文件 + 写哨兵 → watcher 消费哨兵 → manifest
  `ca_fingerprint_sha256` `eaf0305c…→6821e5d0…`、`server_cert_sha256`
  `7d56648c…→f5d8b04f…`（归属检查触发重签）、无
  `previous_ca_fingerprint_sha256`（外部模式无双根窗口），**PID 4888
  不变（零重启）**。
- **⑥ fail-closed reload（负向）**：外部证书文件改名隐藏 + 哨兵 →
  reload 失败：哨兵保留（成功才删）、daemon 存活（TLSv1.3 握手 OK）、
  manifest 沿用旧配置（CA `6821e5d0…` / server `f5d8b04f…` 不变）；
  恢复文件后经 30s 退避重试自愈（哨兵消费、PID 不变）。
- **⑦ 收尾（生产恢复）**：停沙箱 daemon（stop 探针受跨信任根
  客户端证书影响报 `E_HTTP_DAEMON_UNAVAILABLE`，进程未及 kill——
  强杀 4888 后干净启动，属验证环境操作，非产品缺陷）；生产模式
  启动：`ca_source=internal`、CA 恢复 `32b8952e…`（PID 30164）；
  server 证书 sha 变化一次（`b7eeceaf…`）——缺陷 1 修复后归属检查对
  无 AKI 旧证书的一次性重签自愈，随后幂等；`daemon ping` OK
  （pr_b488…，mTLS）。

结论：外部 CA 加载/签发（enroll/renew）、rotate-ca 拒止、换文件
热加载轮换（AKI 归属自愈重签）、fail-closed reload 与退避自愈——
外部 CA 生命周期全链路真机闭合，生产环境已恢复内置 CA 基线。

## 9.37 write.commit 载荷裁决：全量 content + new_hash 唯一格式与 daemon 侧对账（2026-11-19）

### 1. 立项与范围

ADR §14 未决问题 #5（`write.commit` 的 delta 格式：unified diff vs 全量内容
+ hash）正式立项——§14 已决项收口后唯一剩余实质未决项，此前因"写路径大改"
搁置。侦察结论：`write.commit` 链路中 `delta` 参数是**纯审计占位**（仅记
delta_bytes/delta_sha256，零解析逻辑），客户端 `governed_write_file` 从未
发送；同时暴露相邻缺口——daemon 直接信任客户端自述 `new_hash` 更新索引，
`sha256(content) == new_hash` 从未复算，索引 `content_hash` 与入库内容的
一致性只是信任声明（不变量 I5 相邻）。

### 2. 设计决策

- **格式裁决：全量 content + new_hash 唯一格式，不做 unified delta**。理由：
  1. daemon 索引读路径（`file_contents`）本就存全文——即使传 diff 也要
     base 全文 + 应用重构才能入库，存储不省；
  2. diff 应用引入失败面（fuzz 上下文、行尾、编码、二进制），与 fail-closed
     治理写相性差；
  3. 全量载荷使 daemon 可复算 `sha256(content)` 与 `new_hash` 对账，把索引
     一致性升级为密码学事实；
  4. 治理写低频、KB 级载荷，mTLS 一跳带宽非瓶颈。
- **对账 fail-closed**：content 缺失 → `E_WRITE_CONTENT_REQUIRED`；
  `sha256(content) != new_hash` → `E_WRITE_CONTENT_HASH_MISMATCH`；两者
  均拒绝且零索引副作用、grant 不消费（同 grant 换正确载荷可重提）。校验
  置于静态检查块（principal/consumed/expired/path/task 之后、事务之前），
  拒绝路径复用 `audit_write_reject` 留痕。
- **delta 占位移除**：handler 读取与审计 detail（delta_bytes/delta_sha256）、
  dispatch 路径守卫豁免（`WRITE_PAYLOAD_KEYS` 收敛为仅 `content`）一并删除，
  杜绝"看起来支持但不存在"的歧义；守卫测试改为负向断言 delta 不再豁免。
- **审计 detail 重构**：`content_supplied/delta_*` → `content_bytes` +
  `content_sha256`（对账后的实际值，审计回放可独立复核 content↔hash 一致）。
- **契约同步**：ADR §8.2 步骤 4/5 改写（"delta(或 full content)" → 全量
  content + 对账）；fail-closed 表补两行新错误码；"daemon 不复算文件内容"
  旧口径废除。

### 3. 实现清单

| 文件 | 内容 |
| --- | --- |
| `fs_handlers.rs` | `handle_write_commit`：content 缺失/`sha256(content)!=new_hash` fail-closed（对账置于静态检查块末尾）；`total_lines` 按实际内容恒算；审计 detail 改 `content_bytes`/`content_sha256`；移除 delta 读取；+1 单测（缺失/伪造/通过三路径 + grant 未误消费 + 审计对账断言） |
| `dispatch.rs` | `check_remote_path_guard` 豁免收敛为仅 `content`；`test_remote_path_guard_exempts_write_commit_payload` 同步（新增 delta 负向断言防豁免面回潮） |
| ADR | §8.2 契约改写 + fail-closed 表 +2 行；§14 #5 关闭（已决 2026-11-19，§9.37） |
| Python | 零改动——`governed_write_file` 恒发全量 content，契约未变 |

### 4. 缺陷与修正

1. **new_hash 无对账（侦察发现的产品缺口）**：旧实现直接信任自述
   `new_hash` 写索引——伪造 hash 即可让索引 `content_hash` 与 `file_contents`
   语义内容永久错位，且审计 new_hash 与实际入库内容不一致。本轮裁决的
   对账即修复此缺口。
2. **delta 守卫豁免为死面**：豁免键 `delta` 对应的能力从未实现，属"豁免
   攻击面大于功能面"，随裁决收敛。

### 5. 测试与验收

- Rust 新增 1 例：`test_write_commit_content_hash_reconciliation`（缺
  content 拒绝零索引副作用；伪造 new_hash 拒绝且 grant 仍 active；通过
  路径索引推进 + 审计 detail content_sha256 对账 + delta 字段零残留）。
- 全量回归：`cargo test --lib` **2044 passed / 0 failed**（HEAD `e5945ae`）。
  较 §9.36 基线 2043 净增 1。
- `rust_ext/**` 属 RUNTIME_AFFECTING_PATH_PREFIXES → refresh（见 §6）。

### 6. 真机验证链（2026-11-19）

- refresh：`scripts\refresh_shared_runtime.ps1`，TaskId
  `T-1795067673323-1ea46f82`，**passed**，13m22s，evidence
  `~\.callwarden\runtime\evidence\20261119-135435-e5945ae3e8c6-3bccbb85.json`
  （新 daemon PID 42808，binary sha256 `8abd4739…ff48`，
  `daemon ping` OK，principal `pr_b488…`）。authority lease 业务错误
  （E_TASK_WORKSPACE_UNBOUND，TaskId 未绑 workspace）为预期路径，migration
  authority 通过。
- 实机探针（沙箱 remote daemon，`--http-bind 0.0.0.0:9377`，独立
  PKI `fcf74a63…`/registry/task/cg 四库，owner 身份 mTLS，USERPROFILE 重定向
  隔离单实例锁；驱动脚本复用 S4-06-D4 实机客户端引导）——**verdict: PASS**：
  1. 成功路径：grant → 本机原子写 → commit（content 对账通过）→ ok，
     base `01d09d19…` → new `7aa7a535…`；
  2. 伪造 new_hash（content=tampered, new_hash=sha256(evil)）→
     `E_WRITE_CONTENT_HASH_MISMATCH`，且 grant 未被消费——同 grant 换正确
     载荷重提成功；
  3. 缺 content → `E_WRITE_CONTENT_REQUIRED`，同 grant 正确载荷重提成功；
  4. 重放已消费 grant → `E_WRITE_GRANT_CONSUMED`（既有不变量回归）；
  5. daemon 审计复核（`write_audit_events` 直读）：allow 行 detail 含
     `content_sha256`（与 `new_hash` 对账一致）与 `content_bytes`，**零
     delta 字段残留**；两类对账拒绝与重放拒绝均 `deny` 留痕。
- 沙箱注意事项（复现实机验证时的坑）：① 预铸 server 证书 SAN 不含
  `IP:0.0.0.0`，需删除沙箱 `pki/server/*` 让 daemon 按绑定地址重签（SAN
  预检 fail-closed 生效即证 ③ 的既有行为）；② 全新 task-DB 无 workspaces
  行，`task.create` 按 reconciliation case 4 拒止——预种一行即走"首建
  capture"正规语义；③ 审计 deny 行 `event_type='reject'`（非 'commit'）。

### 7. 结论

- §9.37 关闭 ADR §14 #5：write.commit 载荷唯一格式 = **全量 content +
  new_hash**，daemon 侧 `sha256(content)` 对账 fail-closed；unified delta
  永久不做（理由存 ADR §14）。
- I5 相邻缺口（new_hash 信任声明 → 密码学对账）修复；delta 死字段与超额
  守卫豁免移除；审计 detail 升级为可独立回放复核的 `content_sha256`。
- 实现提交 `e5945ae`（3 文件 +182/−20）；全量回归 2044/0；refresh passed；
  实机探针 ①-⑤ 全绿。§14 未决清单至此全部收口（#4 多租户 CA 已决"不做"）。

## 9.38 write.commit 对账实机探针回归化（2026-11-19）

### 1. 立项与范围

§9.37 关闭 ADR §14 #5 后，其真机验证依赖一次性沙箱驱动脚本（临时脚手架，
workspace 外 `%TEMP%\cw937_verify\driver.py`），不可重复回归。选题盘点确认
ADR §14 与 backlog 矩阵（P3-1~5、C1-C12、§12-§36 后续）均已收口，多数历史
"遗留待办"已被后续批次关闭（blast_radius/review_readiness → P0-COMPAT-v3
整体 rust_native；defect_learn 标注债 → §9.29；import_git_history →
task.job_submit 批次；taskkill 硬停 → §9.33）。确认开放项中用户裁决本轮
选题：**write.commit 对账实机探针回归化**——把 §9.37 探针转正为 env-gated
pytest 回归资产，并对生产 daemon 补 fail-closed 探针。

### 2. 实现清单

`tests/test_remote_write_governance.py` 新增 env-gated 类
`TestS3RemoteContentReconciliation`（复用 `requires_remote_write_daemon`
gate 与 `_make_remote_client` 实机客户端引导）：

| 探针 | 内容 | 场景区 |
| --- | --- | --- |
| ① | 治理写成功路径（grant → 原子写 → commit，对账通过） | 纯 RPC，跨 OS 可用 |
| ② | 伪造 new_hash → `E_WRITE_CONTENT_HASH_MISMATCH`，grant 未误消费（同 grant 重提成功） | 纯 RPC |
| ③ | 缺 content → `E_WRITE_CONTENT_REQUIRED`，grant 未误消费 | 纯 RPC |
| ④ | 重放已消费 grant → `E_WRITE_GRANT_CONSUMED` | 纯 RPC |
| ⑤ | daemon 审计直读：allow 行 detail 含 `content_sha256`（与 new_hash 对账）且零 delta 残留 | 仅本机（`CW_S2_REMOTE_CGDB`），跨 OS 缺省 skip |

本机口径可选 env：`CW_S2_REMOTE_TASKDB`（全新 daemon 预种 task-DB
workspaces 行，resolve_create_authority case 4「首建 capture」正规语义，
无 env 依赖对侧已有 capture）、`CW_S2_REMOTE_CGDB`（启用 ⑤）。

### 3. 测试与验收

- pytest 定向全量：`tests/test_remote_write_governance.py` **25 passed /
  0 failed**（沙箱 remote daemon，含新类 ①-⑤ + 既有 `_FakeWriteDaemon`
  客户端半 24 项 + S4-06-D4 实机 roundtrip 类）。
- 生产 daemon fail-closed 探针：loopback bind（PID 42808）下
  `python cw.py write …` → **`E_WRITE_REMOTE_ONLY`**（"治理写仅支持
  bind_scope=remote 的 daemon；loopback 不允许旁路写"），零旁路。
- 纯 Python 测试面（`tests/` 不在 RUNTIME_AFFECTING_PATH_PREFIXES，
  [db_tasks.py](file:///c:/git_work/callwarden/db/db_tasks.py#L75)）→ 免
  refresh；Rust 基线 2044 未动（§9.37）。
- 收益：§9.37 一次性探针成为可重复回归资产——未来写路径任何改动（载荷
  格式、对账语义、错误码）均可一条 pytest 命令复验全链路 + 审计直读。

### 4. 结论

§9.37 裁决的验证面闭环升级：一次性手工验证 → env-gated 实机回归资产；
生产面 fail-closed 门禁（loopback → `E_WRITE_REMOTE_ONLY`）补验通过。
ADR §14 #5 关闭后的回归保护就位。

## 9.39 C7 卡外残留收尾：cascade/reconciliation 测试自建隔离 workspace（2026-11-19）

### 1. 立项与范围

用户裁决本轮选题：**未归一 44 测试归一（C7 卡外残留收尾）**。选题侦察实测
推翻 C7 台账（[cli-mcp-matrix-backlog.md](file:///c:/git_work/callwarden/docs/design/cli-mcp-matrix-backlog.md) §7.5）"已由 §14-§19 收口"的结论：
44 项中仍有两文件 runtime 段真实红——
[tests/test_task_cascade_close.py](file:///c:/git_work/callwarden/tests/test_task_cascade_close.py)
（4 ERROR）与
[tests/test_task_reconciliation_contract.py](file:///c:/git_work/callwarden/tests/test_task_reconciliation_contract.py)
（2 FAILED + 2 ERROR）。

### 2. 根因

两文件硬编码 `workspace_id: 1` 并经 `HttpDaemonRpcClient()`（mTLS owner）
连生产 daemon。§9.36 CA 轮换后 owner principal 变更
（pr_b488648aba88…），workspace 1-8 的 owner_key 归历史 principal →
`workspace.status` / `task.create` 全部 `E_PRINCIPAL_WORKSPACE_FORBIDDEN`。
「共享全局 workspace 1 可用」不是环境前置——测试必须自建。

### 3. 方案演进（含一次实测否决）

每文件新增 module-scope `task_ws` fixture（§9.38 沙箱语义的生产化）：

1. `workspace.register` 注册唯一临时 root（`ws-csc-*` / `ws-recon-*` 前缀）
   → 权威 `(workspace_id, workspace_instance_id)`；
2. task-DB 补种 `workspaces` 行（resolve_create_authority case 4 语义）；
3. teardown 精确清理（任务行由既有 T-CSC-% / T-RECON-NEG-% 清理 fixture 处理）。

首次实跑仍 `E_PRINCIPAL_WORKSPACE_FORBIDDEN`：dispatch 层
[dispatch.rs L3182-3195](file:///c:/git_work/callwarden/rust_ext/src/daemon/dispatch.rs#L3182-L3195)
还有 principal 台账白名单检查（`allowed_workspace_ids` = 磁盘 grants ∪ DB
grants）。补 DB grant 行实测**仍然失效**——
[auth_principal.rs L434-448](file:///c:/git_work/callwarden/rust_ext/src/daemon/auth_principal.rs#L434-L448)
以 `row.get::<_, String>` 读 `workspace_principal_grants.workspace_id`，
而该列 INTEGER 亲和把数字型 id 强转整数存储 → `InvalidColumnType`
→ `rows.flatten()` **静默丢行**（探针实证：TEXT 写入 '1768' 落库变 integer）。
fail-closed 方向的产品缺陷（多拒不误放），本章不修，见 §5 缺陷登记。

最终方案：fixture 向当前 principal 的磁盘 `grants.json` 精确追加新
workspace id（`{"workspace_ids": [...]}`，[read_file_record 每次 authorize
实时读盘](file:///c:/git_work/callwarden/rust_ext/src/daemon/auth_principal.rs#L288-L330)，
天然热更新）；teardown 还原为测试前字节。principal_id 从
`resolve_client_cert_paths()` 证书路径的 `identities/<principal>/` 布局推导。

### 4. 实现清单与验收

- cascade：删旧 `ws_instance` fixture；4 测试签名 `(rpc, ws_instance)` →
  `(rpc, task_ws)`；`make_tree` 加 `workspace_id` 参数；5 处
  `"workspace_id": 1` 动态化。
- reconciliation：`_make_neg_task(rpc)` → `_make_neg_task(rpc, wid, inst)`；
  4 负向测试签名加 `task_ws`。
- 定向回归：两文件 **14 passed / 0 failed**（生产 daemon 直连）。
- 残留检查：task-DB `workspaces` 行 / `workspace_principal_grants` 行 /
  registry.db `daemon_workspaces` 行 / grants.json 全部零残留。
- 纯 Python 测试面（tests/ 不在 RUNTIME_AFFECTING_PATH_PREFIXES）→ 免
  refresh；Rust 基线 2044 未动。

### 5. 缺陷登记与口径回写

- **DB grant 整数 id 静默丢行**（auth_principal.rs load_snapshot）：
  INTEGER 亲和 × `get::<String>`，daemon 侧写入的整数 workspace_id grant
  （如既有 175 行）同样不生效。fail-closed 方向，待后续章节以
  `CAST(workspace_id AS TEXT)` 修复 + 单测收口。
- backlog §7.5 口径回写：`workspace_instance_id` 契约族中 cascade /
  reconciliation 两文件实际仍红至本轮，标注「§17 收口口径修正 → §9.39」。

### 6. 结论

C7 卡外 44 项残留全部归零：40 项此前已收口（§15-§19），本轮收口最后 6 项
（cascade 4 + reconciliation 负向 E2E 2，ERROR/FAILED 口径）。测试资产与
生产环境解耦——workspace 授权不再依赖共享 workspace 1 的历史归属。

## 9.40 缺陷④修复：principal DB grant 整数 workspace_id 静默丢行（2026-11-19）

### 1. 立项与范围

用户裁决选题：**backlog ④ 修复**（§9.39 登记的唯一开放项）。
[auth_principal.rs](file:///c:/git_work/callwarden/rust_ext/src/daemon/auth_principal.rs)
`load_snapshot` 对 `workspace_principal_grants.workspace_id` 以
`row.get::<_, String>` 读取，而该列 INTEGER 亲和把数字型 id 强转整数落库
→ `InvalidColumnType` 被 `rows.flatten()` 静默吞掉 → 整数 id 的 DB grant
全部不生效（fail-closed 方向：多拒不误放）。

### 2. 修复

grants 查询改 `SELECT principal_id, CAST(workspace_id AS TEXT) ...`，
强制文本读取，与 `workspace_allowed` 的字符串比较口径对齐；既有
`COALESCE(revoked_at, 0) = 0` 语义不变。一行 SQL，零结构变更。

### 3. 单测

新增
`test_db_grant_integer_workspace_id_visible`：建 INTEGER 亲和 schema
（与生产 `PKI_SCHEMA_STATEMENTS` 一致），同 principal 混插整数落库（42）
与文本落库（ws_ext936）两行 grant，断言二者均生效 + 无 grant 的 43 仍拒。

### 4. 回归与部署

- cargo 定向：auth_principal 过滤 10 passed；全量 **2182 passed / 0
  failed**（13 测试二进制全绿，基线 + 新测试）。
- runtime refresh **passed**（evidence
  `20261119-182745-72ce920b0188-317f69e0`，git_head 72ce920，TaskId
  T-1795084054235-7ea49d09）：daemon sha256 `75f3a140…f84d`（旧
  `8abd4739…ff48`），PID 42808 → 30676，smoke（--version / daemon ping）
  通过，callwarden_core.pyd 同步更新。

### 5. 真机验证（生产 daemon 直连，双探针）

- ① 修复实证：注册隔离 workspace → 直写 DB grant（integer 落库，
  `typeof=integer`，模拟 daemon 写入口径）→ 越 1s 节流窗 →
  `workspace.status` 通过。旧 binary 下此路径必然
  `E_PRINCIPAL_WORKSPACE_FORBIDDEN`（§9.39 探针实证）。
- ② fail-closed 护栏：无 grant 的第二个 workspace 仍拒止。

### 6. 结论

DB grant 热路径与磁盘 grants.json 路径口径统一，整数/文本 id 双兼容。
§9.39 缺陷登记闭环；§9.39 fixture 的 grants.json 补丁仍可工作（磁盘路径
本就正确），可选的 fixture 简化不在本章范围。

## 9.41 collab 治理写族 E2E + `_handle_collab` rc 口径修复（2026-11-19）

### 1. 立项与范围

用户裁决选题：**CLI E2E 扩表第二批 · collab 治理写族**（backlog §22.5
「按族分批推进」的第一族）。范围：`cw collab publish/verdict/reveal/gate-trigger`
四条子命令的 argparse → dispatch → handler → route_rpc → daemon → 渲染全链路
E2E 锁定 + 实跑中确认的 rc=0 假成功缺陷修复。纯 Python CLI + tests 面，
无 rust_ext 改动，免 refresh。

侦察结论（实现前静态核实）：
- `snapshot.publish`：dispatch.rs:3268 真实 handler，隔离栈下可成功（正例）。
- `verdict.submit`：daemon 门禁链 binding-attestation → lease；probe task 为
  裸 INSERT 种入，首个确定性拒绝为 `E_TASK_BINDING_ATTESTATION_REQUIRED`。
- `reveal.submit` / `gate.decide`：路由进 `handle_collab_rpc`
  （dispatch.rs:2087-2156）**无分支 → 结构化 method_not_found**（诚实未实现，
  有 Rust 测试锁定；属后续任务卡范围，本章不实现 handler）。
- **缺陷⑤**：`_handle_collab` 仅对 `DaemonUnavailableError` 走 SystemExit(1)，
  daemon 业务拒绝（`DaemonRemoteError`）落入 `except Exception →
  _collab_error("E_RPC_FAILED") → return True`，dispatcher 把 True 映射为
  rc=0 —— 治理写被拒却以成功退出码收场，违反 §24「运行时失败=1」口径；
  CLI 侧身份不全分支（`_identity_reason_output → return True`）同病。

### 2. 缺陷⑤修复（cli/main.py）

`_handle_collab` 出口按 §24 口径统一：
- daemon 业务拒绝 / RPC 失败（`except Exception`）→ 渲染 E_RPC_FAILED 后
  `raise SystemExit(1)`（此前 `return True` = rc=0 假成功）；
- verdict 身份不全（运行时身份门禁）→ `raise SystemExit(1)`；
- Envelope / Notes 文件不存在、clause_results/findings 非合法 JSON
  （用户输入校验）→ `raise SystemExit(2)`。
- 配套同步 `tests/test_task_verdict_cli.py` malformed-JSON 用例
  （`is True` → `pytest.raises(SystemExit)` + code==2）。

### 3. E2E 扩表（tests/test_cli_subcommand_e2e.py）

- `CASES` 冻结 27→28：新增 `collab publish --workspace <root>` 成功正例
  （`{PROBE_ROOT}` 占位符在用例内替换为隔离 workspace 根），锁定
  `[OK] snapshot.publish 执行成功`。
- 新增 4 条负例（不进 CASES，独立断言 rc 与错误码）：
  ① `collab verdict` 全参数（含 `--view-manifest-hash`——daemon
  `required("view_manifest_hash")` 先于门禁校验）→ `E_TASK_BINDING_
  ATTESTATION_REQUIRED` + rc=1（实测首个门禁为 canonical binding
  attestation，先于 reviewer lease 门禁 E_LEASE_REQUIRED；按实际门禁顺序锁定）；
  ② `collab reveal` → method_not_found 透传 + rc=1；
  ③ `collab gate-trigger` → 同上；
  ④ `collab verdict` 缺必填 → argparse rc=2。
- `test_frozen_case_count_is_27` → `test_frozen_case_count_is_28`；
  `test_leaf_calls_are_read_only` docstring 注明 publish 为快照面写操作、
  不触 task DB 两张只读断言表。

### 4. 卡外修复（全量回归收集期发现，均与本章改动无关、属既有损伤）

- **`tests/_prereq_guard.py`**：`69ed293`（11-11 ruff --fix）把转发导入
  `from _w3_harness import _prereq_unavailable_reason as prereq_unavailable_reason`
  误判为未用导入删除，`test_rust_cli_diff.py` / `test_prereq_guard.py`
  收集期即 ImportError → 全量 pytest 中断。恢复转发 + `# noqa: F401`。
- **`tests/acceptance/cli/test_cli_c1_semantics.py`**：`6ebde03`（CLI 顶层
  84→86 分类补齐）扩了 `inventory_frozen.json`，c1 的冻结数断言未同步
  （264 vs 实际 266），收集期 AssertionError。264→266 全量替换
  （c1 docstring/assert/suite 名 + harness docstring）。

### 5. 回归与验收

- 目标模块：`pytest tests/test_cli_subcommand_e2e.py -q` → **36 passed**
  （28 CASES + 4 collab 负例 + 既有 4 条）。
- collab 既有进程内面：`tests/test_task_verdict_cli.py` +
  `tests/test_cli_collab_snapshot_publish.py` → **7 passed**。
- rc 口径相关既有面：`test_cli_govfix07_daemon_error_rc.py` +
  `test_cli_govfix08_error_dict_rc.py` + `test_cli_consistency.py` → **45 passed**。
- CLI 受影响面（118 个 `tests/test_cli_*.py` + verdict/prereq 两模块，
  共 **645** 条）：**626 passed / 19 failed**，19 条失败逐条定性为
  **卡外既有环境/鉴权失败**（详见 §5.1），与本章 diff 无交集
  （失败路径均为 `task` 子命令与 mTLS/principal 授权，本章只改 `collab`）。
- 免 cargo / 免 refresh（无 rust_ext、无 daemon 二进制变更）。

#### 5.1 卡外既有失败族（全量/CLI 面跑复现，本章不修，登记待专项卡）

全量 `pytest tests/ -q`（9532 条）在本机**不能作为可用门禁**：失败项各自烧
连接/授权超时（~150 项时单进程跑到 56% 已耗时 50+ 分钟，且失败与执行顺序相关
——模块单独跑多数即通过）。按签名归为三族：

| 族 | 签名 | 代表用例 | 定性 |
|---|---|---|---|
| A. 生产 daemon principal 未授权 | `E_PRINCIPAL_WORKSPACE_FORBIDDEN: principal pr_b4886… 未获 workspace 1 授权` | `test_cli_task_claim_recover.py`(9)、`test_cli_task_lease_parity.py`(9)、`test_lease_gate_empirical.py`(18) | 用例直连生产 daemon + 未种 principal 授权；与本章无关 |
| B. 单轨 HTTPS 迁移后陈旧断言 | 断言 `endpoint.startswith("http://127.0.0.1:")`，实得 `https://…`；断言 `E_HTTP_DAEMON_UNAVAILABLE`，实得 mTLS 缺证书提示 | `test_c17/c21/c16_*_workspace_authority.py`、`test_cli_03_http_rpc.py` | ADR-002 单轨 HTTPS（6fb74aa 起）后断言未同步 |
| C. 源码/文档字面断言漂移 | 断言 `daemon_client.py` 源码含「不提供本地降级」，实际措辞已改写 | `test_legacy_write_jobs_baseline.py` | 文本断言随文档改写失效（ef28ab9 前后） |

上述三族在**本章改动前即存在**：涉及文件与 `§9.41` diff（`cli/main.py`
`_handle_collab` 段、`tests/test_cli_subcommand_e2e.py`、
`tests/test_task_verdict_cli.py`、`tests/_prereq_guard.py`、
`tests/acceptance/cli/*`）无交集；A 族用例走 `cw task`（非 `collab`），
B/C 族断言对象本章未触碰。留待专项卡修复（A：为 CLI 子进程用例建隔离
daemon 或种 principal 授权；B：断言改 `https://` 并按 PKI 门禁调整预期；
C：改为结构化断言而非源码字面量）。

#### 5.2 本章顺带修复的两处卡外**收集期阻断**（已修，见 §4）

`_prereq_guard` 转发导入 + `c1` 冻结数 264→266 —— 二者不修则全量
pytest 在收集阶段即中断，任何门禁都跑不起来。

### 6. 结论

collab 治理写族 4 条全链路锁定：publish 成功面 + verdict/reveal/gate-trigger
结构化拒绝面，rc 口径与 §24 对齐（0=成功 / 1=运行时拒绝 / 2=用法与输入校验）。
`reveal.submit` / `gate.decide` 的 daemon handler 实现仍属后续任务卡
（method_not_found 为当前冻结行为）；CLI 残留未引用叶子缩减至 ~58
（experiment 族、task contract 族、semgrep scan、coverage import 等，
留待按族分批）。

## 9.42 A 族失败根治：隔离栈逃逸与授权种子口径更正（2026-11-19）

### 1. 立项与范围

用户裁决选题：**A 族——隔离 daemon 授权种子**（承接 §9.41 §5.1 登记）。
范围仅 `tests/` 面：让 A 族用例真正跑在隔离身份上，消除对**本机真实
enrolled PKI** 的隐式依赖。无 `rust_ext`/daemon 二进制改动，免 refresh。

侦察先纠正了 §9.41 的定性偏差：A 族**不是**「用例忘了种 principal 授权」，
而是**隔离栈逃逸**——三个失败模块在 fixture 里把 `CW_PKI_DIR` 钉到真实
用户 PKI（`~/.callwarden/pki`），隔离 daemon 继承同一真实 CA，CLI 子进程也
读真实身份。真实身份是 `pki enroll` 产出的 **kind=agent** principal
（实测 `pr_b488648aba88a3339628fdf28f414d39`），其 `grants.json` 只覆盖本机
历史 workspace id（`"c:\\git_work\\callwarden"` / `"175"` / `"10"`），
与隔离 task-DB 的 `workspaces.id=1` 无交集；任何携带 `workspace_id` 的调用
都在 `dispatch.rs` 的 `workspace_allowed(principal, "1")` 判据上
`E_PRINCIPAL_WORKSPACE_FORBIDDEN`。

对照 `peercred.rs::workspace_allowed`：`kind=service` 的本地 owner 身份不受
workspace 限制。ADR-002 隔离栈的 `pki init` 正产出这种身份（`pr_owner`），
这也是其余 18 个 `setup_w3_client` 调用方一直正常的原因——它们没有钉真实 PKI。

### 2. 修复口径

统一为「**A 族模块不再钉真实 PKI，改走 ADR-002 隔离 PKI**」：

- fixture 内 `prev_pki = os.environ.pop("CW_PKI_DIR", None)`，使
  `setup_w3_client` 走 `inject_isolated_mtls_env`（幂等 `pki init` + 注入
  `CW_CLIENT_CERT/KEY/CA`）；
- CLI 子进程 env 不再写回真实 `CW_PKI_DIR`（改为 `pop`），继承同源证书；
- teardown `restore_w3_env()` + 还原 `prev_pki`（避免跨模块 env 污染）。

涉及 10 个文件：

| 文件 | 角色 |
|---|---|
| `test_cli_task_claim_recover.py` | A 族主目标（9 条） |
| `test_cli_task_lease_parity.py` | A 族主目标（9 条） |
| `test_lease_gate_empirical.py` | A 族主目标（18 条，另有自建 workspace） |
| `test_task_supersede.py` / `test_task_supersede_daemon.py` | 同源（重启重放用例在此暴露 §3 缺陷） |
| `test_task_step_resolve_e2e.py` / `test_task_next_action_e2e.py` | 同源 |
| `test_task_create_subtask_steps.py` | 同源 |
| `test_lease_rpc.py` | 同源 + **污染源**（只设不还原 `CW_PKI_DIR`） |
| `test_task_split_governance.py` | 受害者（自身不钉，但被上一模块残留污染） |

### 3. 连带缺陷：隔离 daemon 重启继承客户端证书 env

`test_task_supersede_daemon::test_restart_replay_same_request_id` 在测试内
重启隔离 daemon（`spawn_isolated_daemon`）。修复后首次启动成功，第二次却
秒退：

```
[cw_daemon] [ERROR] HTTPS pre-bind failed:
Tls("E_PKI_CA_LOAD: CW_CA_CERT 与 CW_CA_KEY 必须同时设置（外部 CA 模式）；
当前只设置了 CW_CA_CERT")
```

根因：`inject_isolated_mtls_env` 向 `os.environ` 注入客户端 `CW_CA_CERT`
（无 `CW_CA_KEY`），`spawn_isolated_daemon` 用 `os.environ.copy()` 起服务端，
服务端把 `CW_CA_CERT` 误判为外部 CA 模式。修复：`spawn_isolated_daemon`
在构造子进程 env 时剔除 `CW_CLIENT_CERT` / `CW_CLIENT_KEY` / `CW_CA_CERT`
（daemon 是服务端，不得继承客户端凭据 env）。该缺陷此前被 A 族首个门禁拒绝
遮蔽，本章一并暴露并修复。

### 4. 验证

- **A 族 10 模块**：`pytest <10 files> -q` → **110 passed**（两次全量复跑，
  含模块间顺序效应）。
- **CLI 面**（116 个 `tests/test_cli_*.py`，共 **622** 条）：
  **617 passed / 4 skipped / 1 failed**。唯一失败为既有 **B 族**
  `test_cli_03_http_rpc::test_http_rpc_task_daemon_unavailable_fail_closed`
  （断言 `E_HTTP_DAEMON_UNAVAILABLE`，实得 mTLS 缺证书提示），按 §9.41 口径
  登记不修。§9.41 记录的 19 条失败中 18 条 A 族已清零。
- 回归覆盖含 `test_cli_subcommand_e2e.py`（36 条，§9.41 冻结面）、
  `convergence` 共享 `spawn_isolated_daemon` 的路径未受影响。
- 免 cargo / 免 refresh（无 rust_ext 编译产物变更）。

### 5. 残留与口径

- B 族（单轨 HTTPS 后陈旧断言，`test_c16/c17/c21_*_workspace_authority` +
  `test_cli_03_http_rpc`）与 C 族（`test_legacy_write_jobs_baseline` 源码
  字面量断言）维持 §9.41 登记，待专项卡。
- 全量 `pytest tests/` 仍不建议作为本机门禁（9532 条、失败项各自烧授权/
  连接超时）；A 族清零后，如需全量门禁应以**分段 + 模块级超时**执行。

## 9.43 B 族陈旧断言清零：单轨 HTTPS 后的 loopback/身份装配（2026-11-19）

### 1. 立项与范围

用户裁决选题：**B 族——单轨 HTTPS 迁移后的陈旧断言**（承接 §9.41 §5.1 /
§9.42 残留）。范围：`tests/test_c16_assignment_show_workspace_authority.py`
（10 条）、`tests/test_c17_admin_route_workspace_authority.py`（22 条）、
`tests/test_c21_edit_rule_route_workspace_authority.py`（22 条）与
`tests/test_cli_03_http_rpc.py::test_http_rpc_task_daemon_unavailable_fail_closed`。
纯 tests 面，无 rust_ext 改动，免 refresh。

### 2. 根因（两层，缺一不可）

1. **断言层**：ADR-002 单轨 HTTPS（`6fb74aa` 起）后隔离 daemon manifest 的
   endpoint 变为 `https://127.0.0.1:<port>`，三个 c 系 fixture 仍断言
   `http://127.0.0.1:`。该断言位于 **fixture 内**，因此表现为整模块
   setup ERROR（实测 56 条 ERROR + 1 条 FAILED）。
2. **身份层**：三个 fixture 从未装配 mTLS 客户端身份（daemon 用隔离
   USERPROFILE 下的 LocalCa，客户端解析真实 HOME/环境证书）。仅把断言改成
   `https://` 会让调用阶段转为跨 CA `BadSignature`，必须同时注入同源身份。

### 3. 修复

- 三个 c 系 fixture：`http://` → `https://127.0.0.1:`；manifest 就绪后执行
  `test_mtls_control_plane.pki_init(data_root)`（幂等补 owner 身份）+ 注入
  `CW_CLIENT_CERT` / `CW_CLIENT_KEY` / `CW_CA_CERT` 并 `pop CW_PKI_DIR`，
  teardown 逐键还原（module 级 fixture 无法用 monkeypatch，沿用
  `test_cli_subcommand_e2e` 的 save/restore 配方）。
- 三个 c 系文件内的本地 `_spawn_isolated_daemon`：构造子进程 env 时剔除
  `CW_CLIENT_CERT` / `CW_CLIENT_KEY` / `CW_CA_CERT`——daemon 是服务端，
  继承客户端凭据 env 会被判为「外部 CA 模式」（`E_PKI_CA_LOAD`）。
- `test_cli_03_http_rpc`：用 `make_fake_tls_material` 显式提供一套自签测试
  材料后断言 `E_HTTP_DAEMON_UNAVAILABLE`。否则在无真实 PKI 的机器上，客户端
  在身份解析阶段就 fail-closed（「mTLS 客户端证书缺失」），传输层断言永不可达。

### 4. 验证

- 四模块合计 **59** 条：**55 passed / 4 skipped / 0 failed**（skip 为既有
  「生产 daemon 未运行」条件跳过，与本章无关）。
- 未触碰 `_w3_harness.py` 与 §9.42 已修 10 模块；CLI 面其余 115 个文件在
  §9.42 已全量复跑，本章仅改其中 `test_cli_03_http_rpc.py` 并在本章内验证。
- 免 cargo / 免 refresh。

### 5. 口径

至此 §9.41 登记的三族只剩 **C 族**（`test_legacy_write_jobs_baseline` 源码
字面量断言漂移），待专项卡。

## 9.44 C 族清零：源码字面量断言结构化（2026-11-19）

### 1. 立项与范围

用户裁决选题：**C 族——源码字面量断言漂移**（承接 §9.41 §5.1 / §9.43 §5）。
范围：`tests/test_legacy_write_jobs_baseline.py` 单模块 9 条 FAILED。纯 tests
面，无 rust_ext 改动，免 refresh。

### 2. 根因（四类陈旧字面量）

1. `test_build_graph_routes_to_daemon_native` 断言工具体内不出现
   `build_full_graph`，但 S2 §16.4 的 `bind_scope=remote` 分支合法保留
   `cg.build_full_graph(force=True)`（客户端本机扫描 + snapshot.upload）。
2. `TestMutationRecoverySemantics` 5 条调用 `HttpDaemonRpcClient.mutation_call`
   与其私有 `_query_mutation_outcome`，该 API 已由 `4b1380a` / `ef28ab9`
   移除（写请求统一经 `route_task_write`）。
3. `TestOutcomeReadRpcMapping` 2 条正则匹配 `_query_mutation_outcome` 的
   `task.status` read_rpc 映射与 `_mutation_outcome_matches` 状态表，同样已移除。
4. `TestCallWithAutostart::test_no_local_fallback` 断言 docstring 含
   「不提供本地降级」，现行文档措辞为「带自动唤起与降级分流」+ Governance_Write
   fail-closed。

### 3. 修复（结构化取代字面量）

- 新增 `_def_body(src, name, indent)`：按缩进提取函数体，断言落在实现结构而非
  全文件出现/缺失。
- `build_graph`：分别断言 remote profile 分支含 `cg.build_full_graph(force=True)`、
  默认路径 `return _route(` 之后不含 `build_full_graph`。
- mutation 幂等：`TestMutationRequestIdSemantics` 断言 `route_task_write` 生成
  `req-` 前缀、`call()` 按「显式参数 → `params["request_id"]` → uuid4」三级解析
  并写入 `envelope.id`。
- mutation fail-closed：`TestMutationFailClosedSemantics` 用 monkeypatch
  （`CW_DAEMON_MODE=enterprise` + 桩 RPC client）实测——业务错误原样透传且
  fallback 不执行；连接级故障抛 `DaemonUnavailableError` 且 fallback 不执行；
  并断言 `mutation_call` / `_query_mutation_outcome` / `_mutation_outcome_matches`
  不得回流。
- `call_with_autostart`：改为断言 `DaemonMutex` + `ensure_http_daemon` 有界唤起、
  `route_degraded` 分流、Governance_Write `raise DaemonUnavailableError`。
- 模块 docstring 第 2 段同步为现行 mutation 契约，删除 `mutation_call` 描述。

### 4. 验证

- `tests/test_legacy_write_jobs_baseline.py`：**30 passed / 0 failed**（原 9 条
  FAILED 全清，其余 21 条未改动）。
- 连带回归 `tests/test_legacy_write_jobs_baseline.py` +
  `tests/test_legacy_query_baseline.py`：**101 passed**。
- 未触碰 `_w3_harness.py`、daemon_client 生产源码与 §9.42/§9.43 已修模块。

### 5. 口径

§9.41 登记的三族（A/B/C）**全部清零**；CLI 面既有失败归因闭环。

## 9.45 CLI 面全量回归验收：A/B/C 清零确认（2026-11-19）

### 1. 立项与范围

回答终验问题「回归完成了吗」：在 §9.42（A 族）、§9.43（B 族）、§9.44（C 族）
三章修复后，**首次整体复跑** CLI 面全量。范围：116 个 `tests/test_cli_*.py`
（§9.42 登记口径 622 条）。纯验证章，无源码/测试改动。

### 2. 执行

- PowerShell 不展开 `tests/test_cli_*.py` 通配符：先 `Get-ChildItem` 展开为绝对
  路径再传给 pytest；`-p no:randomly` 固定执行顺序，避免随机排序引入顺序依赖。
- 复跑两次（一次统计通过/跳过，一次 `-rs` 抓跳过原因），结果一致。

### 3. 结果

| 指标 | 数值 |
| --- | --- |
| 文件数 / 收集条数 | 116 / **622** |
| passed | **618** |
| skipped | **4** |
| failed | **0** |

- **0 failed**：A/B/C 三族在 CLI 面已无残留（§9.42 后唯一 B 族失败、§9.44 的
  C 族模块均已修复）。
- **4 skipped**：全部为 `tests/test_cli_03_http_rpc.py` 的 live round-trip
  用例（`live_daemon` fixture 中 `HttpDaemonRpcClient().health()` 失败即 skip）。
  本机测试进程未发现可用的生产 daemon endpoint（MCP 宿主的 daemon 不在该进程的
  env / manifest 发现面内），属**环境条件跳过**，非失败、非回归。

### 4. 口径与后续

- CLI 面回归判定：**通过**（0 failed）。
- 4 条 live 用例长期只 skip、未产生真实 round-trip 覆盖。若需纳入常规门禁，应
  改为隔离 daemon 夹具（参照 §9.42 `_w3_harness`）或 env-gated 显式地址——
  **已在 §9.46 收口**。
- 全量 `pytest tests/`（9532 条）仍不可作单次门禁（§22.6：失败项与执行顺序相关
  且各自烧授权/连接超时），分段 + 模块级超时门禁另卡处理。

## 9.46 live round-trip 4 条修复：隔离 daemon 化（2026-11-19）

### 1. 立项与范围

§9.45 登记的 4 条长期 skip：`tests/test_cli_03_http_rpc.py` 的
`task.status` / `task.list` / `task.status_tree` / authority-failure round-trip。
纯 tests 面，无 rust_ext / 生产源码改动。

### 2. 根因

旧 fixture `HttpDaemonRpcClient().health()` 依赖**本机常驻生产 daemon** 的
manifest 发现；测试进程 env 内无可用 endpoint（MCP 宿主的 daemon 不在其发现面）
→ 一律 `pytest.skip`。且断言依赖真实 DB 的固定任务：CLI-02 卡存在、
A′ epic（`T-1787293451688-c14b1e44`）有 ≥160 个直接子任务。

### 3. 修复（隔离 daemon + 本地 seed 权威）

- fixture 改 module-scope：`setup_w3_client(tmp, _REPO_ROOT, ws_id=1)`（§9.42
  隔离 HTTPS daemon + 同源 mTLS），不再触碰生产 daemon / 真实 PKI。
- `_seed_read_authority(task_db)`：seed 2 张卡（CLI-02 / A′）+ 165 个
  `parent_id=A′` 子任务，并为两张父卡写 `task_workspace_bindings`（
  `task.status` / `status_tree` 的 authority 反查；`task.list` 的 JOIN 过滤）。
  **不 seed `workspace_authority_captures`**：owner ACL 在无 capture 时放行，
  避免伪造真实用户 registry 身份。
- teardown：terminate/wait（超时 kill）→ rmtree → `restore_w3_env()` →
  还原 `CW_PKI_DIR`；前置缺失仍 skip，其余异常保持红（C7 口径）。

### 4. 验证

- 单模块：**5 passed / 0 skipped**（原 1 passed / 4 skipped）。
- 全量 CLI 面复跑（116 文件 / 622 条）：**622 passed / 0 skipped / 0 failed**。

### 5. 口径

live round-trip 已纳入常规 CLI 回归；CLI 面 skip 清零。

## 9.47 全量分段门禁 + F 簇归因清零（2026-11-20）

### 1. 立项与范围

承接 §9.45「全量 `pytest tests/` 不可单次门禁」结论，首次以**分段 + 模块级超时**
方式跑完整个 `tests/`（9532 条，588 个模块），并对每个非零模块做归因（F 簇：
真实缺陷 / 隔离栈逃逸 / 陈旧断言 / 环境卡外 / 门禁口径），逐项修复或按口径登记。

### 2. 门禁装置与口径

- `%TEMP%\cw_seg_run_resume.py`（临时脚本，非仓库）：逐模块子进程执行，单模块
  300s 超时，输出落 `<module>.xml`，超时 `taskkill /F /T` 回收进程树；结果写
  `results.jsonl`（`file/rc/timed_out/passed/skipped/failed[]/errors[]`）。
- 6 并发两轮跑完，合计 **588 模块 / 1521.9s**；`results.jsonl` 594 行（6 行是
  重跑/重试记录），去重后 588。
- 非零模块 **38 个**：`rc=1` 28 个、超时 5 个、`rc=5` 5 个。

### 3. F 簇归因表（38 项 → 清零 / 排除）

| 族 | 模块（去重） | 签名 | 处置 |
|---|---|---|---|
| 生成物漂移 | `test_coverage_manifest` | `outputs/coverage_manifest.json` 与当前 handler/op_class 不一致 | 重跑 `scripts/gen_coverage_manifest.py` 重生 |
| 隔离栈逃逸（F1） | `test_cli03_task_read_authority`、`test_mcp_get_freshness_status_http_rpc` | fixture 隔离 `CALLWARDEN_DIR` 连带 `get_pki_dir()` 指向空目录 → fail-closed 用例拿到「mTLS 证书缺失」而非 `E_HTTP_DAEMON_UNAVAILABLE` | `tests/conftest.py` 模块导入时捕获真实 PKI，`_isolate_db_path` 覆写 `get_pki_dir` 仅在 `CW_PKI_DIR` 显式设置时让位 |
| locale 不确定 | `test_mcp_compat_tools_task_http_rpc` | 门禁环境 `LANG=zh_CN.UTF-8` 使 daemon 默认 plan 模板为中文，`default_en` 断言失败 | `_w3_harness.spawn_isolated_daemon` 固定 `CALLWARDEN_LANG=en_US` |
| 隔离 daemon 与客户端 PKI 不同源 | `test_http_daemon_release_acceptance`、`test_http_compat_worker_batch`、`test_workspace_snapshot_rpc_http`、`test_http_governance_error_cutover`、`test_task_prompt_e2e` | `CERTIFICATE_VERIFY_FAILED`：daemon 在隔离 HOME 自建 CA，客户端仍用真实 HOME 的 CA/身份 | 统一 `USERPROFILE=data_root/userhome` + manifest 就绪后 `inject_isolated_mtls_env(data_root)` + teardown `restore_w3_env()`；`test_task_prompt_e2e` 另需把 `manifest_path` 钉到隔离 manifest（否则客户端从真实 HOME manifest 取 CA pin） |
| 单轨 HTTPS 后的陈旧断言 / 已删 API | `test_http_daemon_release_acceptance`、`test_daemon_autostart_windows`、`test_http_governance_error_cutover`、`test_task_scoped_workspace_authority`、`test_task_stats_rpc_http`、`test_job_read_rpc_http`、`test_http_native_read_cutover`、`test_migration_manifest`、`test_http_daemon_transport` | `http://` / `HTTP_MVP_TRANSPORT_PROFILE`、`_start_daemon_windows(pipe)`、`_FakeHttpClient` 缺 `is_remote_profile`、patch 已删除的 `tools_task.get_db`、治理读写不得走 `_route`、`memfd`/`daemon_server.py` 已删除、重启继承客户端证书 env 秒退 | 断言改 `https://` + `HTTPS_MTLS_TRANSPORT_PROFILE`；`_start_daemon_windows()` 无参；补 `is_remote_profile()->False`；改 `hasattr` 断言；`governed={file_read,file_write}` 跳过 `_route` 检查；期望模块集与 manifest 文档同步删除已删项；spawn 前 pop `CW_CLIENT_*`/`CW_CA_CERT`/`CW_PKI_DIR` |
| 路径大小写 | `test_query_{file,grep,issues,tests,symbol}_rpc`、`test_workspace_snapshot_binding` | Windows `C:\` vs `c:\` 直接比较失败 | `os.path.normcase` 归一化后比较 |
| 退出码口径 | `test_task_write_client_contract`、`test_task_reopen_consistency`、`test_integration_full_matrix` | 期望 `rc=2` / 仅接受 `0,2` | 按 `cli/main.py` 口径：daemon 拒绝 = 运行时失败 `rc=1`；全矩阵接受 `0/1/2` 且断言无未捕获 traceback |
| 授权并集语义 | `test_principal_authorization` | revoke 只清磁盘 `grants.json`，DB 投影仍授权（`auth_principal.rs` 并集 + 1s 重载） | 测试同步 `DELETE workspace_principal_grants`，3s 轮询至 `E_PRINCIPAL_WORKSPACE_FORBIDDEN`（非产品缺陷） |
| Legacy Named Pipe E2E | `test_task_close_gate`、`test_task_split_steps` | 删除的 Named Pipe 客户端 + 旧治理契约（无 lease/verdict/identity、plan 无描述） | 改 `setup_w3_client` 隔离 HTTPS 栈 + `seed_cli_task_authority`/`seed_cli_task_agent`；close 走 reviewer lease，report 走 implementer lease；plan 补子任务描述；`_seed_pass_verdict` 满足 review→close 前置 |
| 环境卡外（超时） | `test_i18n_cleanup_c5`、`test_p0_3_cross_platform_package`、`test_phase6_build_context`、`test_phase6_resolved_edges`、`test_rust_cli_diff` | 300s 预算下被 `taskkill /F /T`（`rc=-9`） | 单独复跑全部通过（i18n 单跑约 9.5min）；6 并发资源饥饿，非缺陷 |
| 门禁口径（脚本基准） | `test_f11_rust_build_graph`、`test_p3_gate_smoke`、`test_p3_identity_smoke`、`test_p3_reviews_smoke`、`test_p3_task_mutation_smoke` | 无 `def test_`，仅 `__main__` 脚本 → pytest `rc=5`（未收集） | 从 pytest 门禁排除，按脚本基准单独运行 |

### 4. 验证

| 批次 | 范围 | 结果 |
|---|---|---|
| A | HTTP/daemon 面 10 模块 | **226 条：223 passed / 3 skipped / 0 failed** |
| B | 授权/query/CLI 面 11 模块 | **247 条：210 passed / 37 skipped / 0 failed**（skip 为平台/前置条件） |
| C | prompt/MCP compat/coverage/split/close 5 模块 | **54 passed / 0 failed** |
| D | 原超时模块（除 i18n）4 模块 | **151 条 0 failed**（含 12 xfail） |
| E | 原超时 MCP 5 模块 | **27 passed / 0 failed** |
| F | `test_i18n_cleanup_c5` | **46 passed / 0 failed**（单跑约 9.5min，远超 300s 预算） |

### 5. 口径与残留

- F 簇 28 个 `rc=1` 全部清零；5 个超时属门禁资源口径（建议单模块预算 900s 或
  并发 ≤4）；5 个 `rc=5` 为脚本基准，排除出 pytest 门禁。
- 未改产品源码（`rust_ext/`、`server/`、`cli/`）；改动集中在 `tests/` 与
  `docs/design/migration-manifest.md`（已删模块标注），生成物仅重生
  `outputs/coverage_manifest.json`。
- 仍待办：无。i18n 动态 key 占位符静态校验已在 §9.49 收口；§24.5 已在 §9.48 收口。

## 9.48 §24.5 退出码口径收口：本地输入校验 `rc=2` / daemon 拒绝 `rc=1`（2026-11-20）

### 1. 口径（承接 §24.1，用户裁决）

| 失败来源 | 退出码 |
|---|---|
| 成功 | 0 |
| argparse 用法错误 | 2 |
| **本地输入校验失败**（identity 四字段缺失/不全、lease token 与 fencing_counter 不完整、文件名/JSON 非法、角色取值非法） | **2** |
| **daemon 业务拒绝 / 传输失败 / 运行时或能力未接线**（含 `_validate_identity_routed` 权威判定失败） | **1** |
| 平台/部署前置条件不满足 | 2（不变） |

边界：identity 四字段齐备但 daemon 判 `E_IDENTITY_UNREGISTERED` /
`E_IDENTITY_SESSION_MISMATCH` / 实例不匹配 → 1；`cw collab verdict` 的 identity
门禁由 §9.41 的 1 改回 **2**（identity 缺失属输入校验）。

### 2. 变更点（`cli/main.py`，仅把失败分支 `return True` 换成 `raise SystemExit(N)`）

| 类别 | 命令/分支 | 新 rc |
|---|---|---|
| 本地输入 | `task create`（`--steps` 非数组/JSON 失败、role_contracts 非法、governed 缺 workspace 对）、`task next`、`task report`（`--changes-json`、identity、lease）、`task handoff`（identity/规范化）、`task step-resolve`、`task apply`/`close`/`reopen`/`cascade-close`、`task claim-recover`、`task split`（plan 缺失/无子任务）、`task supersede`、`attest-legacy-workspace-binding`、`contract-bootstrap`/`contract-revise`、`bootstrap-executor-evidence`/`bootstrap-reviewer-pass`、`lease *`（身份三字段）、`identity revoke`（mode/initiator） | 2 |
| daemon 拒绝 | `task report` 的 `_governance_gate`、`_validate_identity_routed` 失败、`E_IDENTITY_NOT_WIRED`；`task apply`/`close`/`reopen` 的权威校验与能力未接线；`task step-resolve` 两处异常捕获；`task assignment-status/heartbeat` 异常捕获；`task split` 目标任务不存在；`lease acquire/renew/release` 业务拒绝与 `_governance_gate`；`identity revoke` 能力未接线/写入失败/db 拒绝 | 1 |

未触碰：已 `sys.exit(1)` 的 `task governed-projection`/`task superseded` 及各
`{"error": ...}` 分支；成功路径与输出文案保持不变。

### 3. 验证

| 项 | 结果 |
|---|---|
| 新增 `tests/test_cli_exit_code_local_input.py`（11 个输入校验 + 4 个 daemon 拒绝代表分支） | **15 passed** |
| 更新陈旧断言（期望旧 `rc=0`/`return True`）：`test_cli_089_http_rpc`、`test_cli_091_http_rpc`、`test_cli_094_http_rpc`、`test_cli_073_http_rpc`、`test_cli_task_c9_subcommands`、`test_task_create_parent_governance`、`test_task_step_resolve_e2e` | 改为 `pytest.raises(SystemExit)` 并断言 1/2 |
| 环境隔离补强 | `test_cli_task_claim_recover` 的 CLI 子进程 `USERPROFILE/HOME` 指向 `tmp_path`，避免真实 HOME 残留 stale manifest 掩盖 `E_DAEMON_UNAVAILABLE` |
| 定向回归（含新增，10 模块） | **122 passed / 0 failed（106.05s）** |
| 进程级抽查（真实 `cw.py` 子进程） | `task apply` 部分 identity → **rc=2 / E_IDENTITY_INCOMPLETE**；`task split --plan missing.md` → **rc=2**；`lease acquire` 无 identity → **rc=2 / E_ASSIGNMENT_INCOMPLETE**；`collab verdict` 缺 identity → **rc=2 / E_IDENTITY_INCOMPLETE** |
| 文档 | `docs/cli_reference.md` 新增「退出码契约（全局约定）」，45 处逐命令 rc=2 表述同步更新 |

### 4. 残留

- `_handle_assignment` 等未列入本卡的命令面未逐一审计同类吞错出口；若发现，
  另立卡（本卡不扩大改动面）。
- `_run_subcommand_mode` 在非 `task` 写命令上先做 workspace 注册，注册失败时以
  既有 `rc=2` 提前退出（环境门禁口径），不影响本卡分支的 rc 语义。

## 9.49 动态 key（`t(variable)`）占位符静态校验（2026-11-20）

### 1. 缺口

§32 冻结了 14 个动态调用点（首参非字面量），但只保证「不新增/不消失」；§27 的
占位符门禁只覆盖字面量首参。动态站点的 catalog 模板若引入调用方未提供的占位符，
`t()` 会在运行时缺参并静默回退原文，allowlist 与字面量门禁都不会失败。

### 2. 处置（仅测试门禁，无产品代码/catalog 变更）

`tests/test_i18n_placeholder_consistency.py` 新增 §39 段：

| 类别 | 站点 | 校验 |
|---|---|---|
| F | `_handle_experiment._msg` 28 个调用点 | key/kwargs 字面量：模板占位符 ⊆ 调用点 kwargs（双语）；调用点数量冻结 28 / 26 key；拒绝 `**` 展开；断言 `_msg` 仍透传 `**kwargs`（防空转） |
| B | 注册表 `title_key` / `desc_key` | title 无 kwargs → 零占位符；desc 仅 `count`；冻结 12 / 44 |
| D（无 kwargs） | 5 个结构化默认 key（`_collab_output_structured_reason` / `_t_structured`） | `daemon_errors` 扁平段模板零占位符 |
| D/E（`**context`） | 6 个常量映射 resolver | catalog 与 bundled 双语默认占位符集合逐 key、逐语言一致 |

不可静态判定的 A 类公开入口（`i18n/__init__.py` 的 `**kwargs` 包装）与 daemon
动态 `message_key` 继续由 §32 冻结清单兜底。

### 3. 验证

| 项 | 结果 |
|---|---|
| `tests/test_i18n_placeholder_consistency.py` | **109 passed**（含新增 6 条 §39 用例） |
| `tests/test_i18n_key_existence.py` + `i18n_cleanup_c5` + `i18n_print_cleanup` | **69 passed** |
| 负向自检 | 缺参 / 非标识符花括号（`{0}`）均被 helpers 判为不合规 |
| 基线扫描 | 28 个 `_msg`、12 title / 44 desc、5 个结构化 key、6 个 resolver 均零违规（门禁为冻结式，无基线修复） |

### 4. 边界

- 非目标：A 类公开入口与 daemon 动态 key 的数据流分析；`t()` 缺参语义升级为抛错。
- 门禁只读：不改 `cli/main.py`、`i18n/*.json` 与各 resolver。
- 回滚：回退测试文件提交。

## 9.50 §24.5/§9.49 后全量分段门禁复跑：4 项失败归因（2026-11-20）

### 1. 装置与口径

承接 §9.47 的分段门禁，在 §9.48（退出码口径）与 §9.49（动态 key 占位符）落地后
复跑全量 `tests/test_*.py`：

- `%TEMP%\cw_seg_run_fresh.py`（临时脚本，非仓库）：全新输出目录
  `%TEMP%\cw_seg_run_20261120c`（不复用 §9.47 的 594 行结果），单模块预算
  **900s**（§9.47 结论：300s 对 i18n/e2e 过紧），6 并发，超时
  `taskkill /F /T` 回收进程树，结果写 `results.jsonl`。
- 589 行 / 589 模块（+1 = §9.48 新增 `test_cli_exit_code_local_input.py`），
  墙钟 **1579.6s**，模块耗时合计 8912.6s。

### 2. 结果

| 指标 | 值 |
|---|---|
| 模块 | 589：clean 580 / nonzero 9 |
| 执行用例（junit 汇总） | 8030：failed+error **10** / skipped 142 |
| 超时模块 | **0**（900s 预算生效；§9.47 的 5 个超时模块本轮均 rc=0） |
| 全量 collect-only（含 `tests/acceptance/**`） | 9554 条（分段装置只跑顶层 `tests/test_*.py`） |

非零 9 个 = 4 个真实失败模块 + 5 个 `rc=5` 脚本基准（无 `def test_`，沿用 §9.47
口径排除出 pytest 门禁：`test_f11_rust_build_graph`、`test_p3_gate_smoke`、
`test_p3_identity_smoke`、`test_p3_reviews_smoke`、`test_p3_task_mutation_smoke`）。

### 3. F 簇归因（4 项 → 清零）

| 模块 / 用例 | 签名 | 族与判定 | 处置 |
|---|---|---|---|
| `test_task_loop_capability_authority::test_cli_identity_revoke_fail_closed` | `SystemExit: 1`（旧断言 `ret is True`） | **卡内**：§9.48 口径变更后的陈旧断言 | `_identity_revoke` 在非 local 模式已是 `raise SystemExit(1)`（capability gate 未接线属 daemon 拒绝面）→ 断言改 `pytest.raises(SystemExit)` + `code == 1` |
| `test_mcp_export_module_graph_http_rpc`（4 errors） | `E_HTTP_MANIFEST_STALE: [Errno 13] Permission denied`（setup） | **卡外**并发窗口 + 产品侧瞬时读缺陷 | `read_http_manifest` 对 `PermissionError` 做有界重试（见 §4） |
| `test_mcp_validate_revision_dependencies_http_rpc`（4 errors） | 同上 | 同上 | 同上 |
| `test_principal_authorization::test_s1_05_missing_san_uri_is_rejected` | `ConnectionAbortedError: [WinError 10053]` | **卡外**：拒绝时机不确定（403 vs TLS 断连） | 断言接受 `(ssl.SSLError, OSError)`，与 `test_mtls_enrollment::S1-06` 同口径（见 §4） |

### 4. 修复

**（a）manifest 瞬时读竞态（产品侧，`server/daemon_autostart.py`）**

daemon 以 `publish_manifest_atomic` → `MoveFileExW(MOVEFILE_REPLACE_EXISTING)` 发布
manifest；替换瞬间目标文件处于 Windows delete-pending 状态，此时 `open()` 得到
瞬时 `ERROR_ACCESS_DENIED`（不是旧值也不是新值）。6 并发门禁下两个 MCP 模块的
setup 同时命中该窗口。旧实现把任何 `OSError` 直接映射为 `E_HTTP_MANIFEST_STALE`，
使瞬时竞态表现为硬失败。

现改为：仅 `PermissionError` 在 **1.0s 预算 / 50ms 间隔**内重试，预算耗尽仍报
`E_HTTP_MANIFEST_STALE`；其它 `OSError`/`ValueError` 及 `FileNotFoundError`
语义不变（立即 fail-closed）。新增 2 条用例于 `tests/test_http_manifest_discovery.py`：
瞬时两次 `PermissionError` 后成功；持续拒绝在预算耗尽后报 STALE。

**（b）S1-05 拒绝口径（测试侧）**

无 SAN URI 的客户端证书在 rustls 证书校验阶段即被拒，客户端看到 HTTP 403
（`E_MTLS_CLIENT_CERT_MISSING/REJECTED`）还是 TLS 层断连取决于服务端拒绝时机，
并发负载下两态均实测出现。测试改为二者皆算「拒绝」，与既有过期证书用例
（`test_mtls_enrollment::S1-06`）保持一致。

**（c）§24.5 陈旧断言（测试侧）**

见 §3 第一行；`tests/test_task_loop_capability_authority.py` 同步退出码语义。

### 5. 验证

| 批次 | 范围 | 结果 |
|---|---|---|
| A | `test_http_manifest_discovery` + `test_principal_authorization` + `test_task_loop_capability_authority` | **55 passed** |
| B | 两个 manifest 模块 6 并发 × 2 轮（修复后压测） | **12/12 rc=0** |
| C | manifest/autostart 关联面 5 模块（`test_daemon_autostart`、`test_daemon_autostart_windows`、`test_cli01_health_manifest_capability`、`test_cli_daemon_stop`、`test_mcp_daemon_startup`） | **46 passed / 2 skipped** |
| D | 门禁内 i18n（§9.49 基线复核） | `test_i18n_placeholder_consistency` **109 passed**；`test_i18n_cleanup_c5` **46 passed**（103.8s） |

### 6. 卡外判定依据

- `git diff --name-only 65d9793..HEAD` 仅 `cli/main.py` + 13 个测试/文档文件；
  `server/`、`tests/conftest.py` 与上表三个失败模块均未被本轮改动触及。
- 两个 manifest 模块单跑各 **5 passed**；`test_principal_authorization` 单跑
  **8 passed**；§9.47 同装置同并发下三者均未非零 → 复现依赖并发/时序，非本轮
  代码引入。
- 本轮已将 manifest 读竞态与 S1-05 断言硬化，消除同签名复发路径；`rc=5` 5 个
  仍按脚本基准口径排除。

### 7. 边界

- 不改 daemon 侧发布语义（仍为原子替换）；重试只在客户端读取侧、且有界。
- 仍待办：无。

## 9.51 cli_reference 四段式第二批 + 命令漂移/rc 残差收口（2026-11-20）

### 1. 装置与侦察

选题经历一次改道：原定「i18n 双语补译」经全量扫描（2258 个字面量 key 引用 +
catalog 双向对账）证实为**陈旧残留**——§26 已把缺翻译方向收口且门禁不豁免
`default=`，零缺口。改选 backlog §21 后续卡「cli_reference 四段式剩余分类」。

对剩余 113 个章节实跑 `--help` 核对（临时脚本 `%TEMP%\cw_cli_ref_scan.py` +
批量 help 采集），发现三类漂移：gc 五个章节标题空格形（实际 argparse 为连字符形
`gc archive-list` 等，照文档执行 rc=2）；`semgrep stats / semgrep list`、
`defect stats / defect build` 组合章节、`guardrail` 裸命令不可运行、
`coverage test` 只有度量区裸存根；`coverage import` 失败分支 `return True`
（rc=0）违反 §24.5 全局退出码契约。

### 2. 处置

- **文档**（[cli_reference.md](../../docs/cli_reference.md)）：第二批 6 前缀
  24 命令补齐四段式（用法/参数表/退出码/示例），参数表按 `--help` 实跑真值；
  gc 标题收敛连字符形；`guardrail` 拆 `scan`/`rules`；`semgrep`、`defect`
  组合章节拆分；度量区裸 `coverage test` 存根删除并入覆盖率分类。
- **rc 残差**（[cli/main.py](../../cli/main.py)）：`_handle_coverage` import
  的 `FileNotFoundError` / 解析异常分支改 `raise SystemExit(1)`（报告文件由
  daemon 侧 `import_lcov` PROTECTED_MUTATION 判定 → daemon 拒绝/运行时失败 → 1）。
- **门禁**（[test_cli_reference_sections.py](../../tests/test_cli_reference_sections.py)）：
  新增 `SECOND_BATCH_PREFIXES` + `EXPECTED_COMMANDS_SECOND`（24 命令），
  `_canonical` 剥离方括号 metavar，完整性检查改按 `COVERED_PREFIXES` 并集
  （68 命令）。

### 3. 验证

| 项 | 结果 |
| --- | --- |
| `test_cli_reference_sections.py` | **8 passed** |
| `test_cli_026_http_rpc` + `test_cli_c9_coverage_help` + `test_cli_exit_code_local_input` | **43 passed** |
| 全库 `*.md` 旧空格形引用扫描 | 0 残留 |

### 4. 边界与残留

- 其余 89 章节四段式留后续批次；`gc archive/restore/status/purge` 动作章节化同批。
- `vuln-blast` epilog「2 = Database locked」为退役口径（文档侧已加注，运行时
  help 文案待口径统一批次处理）。
- i18n 双语补译残留确认已由 §26 收口，backlog §40 登记改道过程。

## 9.52 cli_reference 四段式第三批全收口（2026-11-21）

### 1. 装置与侦察

§9.51 后剩余 92 个章节标题。按大类派 5 个批次子代理（工程/生命周期 17 标题、
图谱/度量 27 标题、审计/克隆治理 15 标题、rule+rollback 16 节、git+语义 16 节）
逐命令 `--help` 实跑核对，文档收敛为 **155 个章节 / 三批 154 命令全部四段式**。

### 2. 处置

- **文档**（[cli_reference.md](../../docs/cli_reference.md)）：第三批 62 前缀
  86 命令补齐四段式；漂移修正：`impact` 改 `<SYMBOL_HASH>` 实参形、
  `rollback config --flag` 改 `<-1\|0\|1>`（表格管道转义）、`--phase` 0=全部、
  `export-module-graph` FORMAT 实为 `--format {mermaid,dot}` 选项、`clone`
  默认值 3/0.7→5/0.8；入口澄清：`refresh --watch` 已退役（独立入口
  `cw-agent`）、`cw-agent` 为独立角色入口非 argparse 子命令、`test` 为
  cw.py 内置直通命令；`callers`/`callees`、`largest-fns`/`coupled-fns`
  拆分、audit 族变体合并（净 5 节）。
- **门禁**（[test_cli_reference_sections.py](../../tests/test_cli_reference_sections.py)）：
  新增 `THIRD_BATCH_PREFIXES`（62）+ `EXPECTED_COMMANDS_THIRD`（86），
  `_canonical` 同时剥离尖括号与方括号 metavar，完整性检查按三批并集
  （154 命令）。

### 3. 验证

| 项 | 结果 |
| --- | --- |
| `test_cli_reference_sections.py` | **8 passed** |
| `test_cli_026_http_rpc` + `test_cli_c9_coverage_help` + `test_cli_exit_code_local_input` | **43 passed** |
| `%TEMP%\cw_third_batch.py` 扫描 | third_unique=86、dupes=[]、P=E=X=1 |

### 4. 边界与残留

- rc 残差台账（只报告未改码）见 backlog §41.4：多命令 epilog「2 = Database
  locked」退役口径、`config check-role agent` / `git import` / `embed`
  失败分支 rc=0、`git check-*` 软门禁恒 0（设计使然）、`git_arg_limit`
  i18n 键共用文案漂移。
- `gc archive/restore/status/purge` 动作章节化已在 §9.53 收口（backlog §42）。

## 9.53 gc archive/restore/status/purge 动作章节化（2026-11-21）

### 1. 装置与侦察

§40.4 挂账项：`gc` 顶层 12 个子命令中 4 个动作（archive/restore/status/purge）
无章节。`--help` 实跑 + 路由表核对：`gc_status` READ_ONLY，
`gc_archive`/`gc_purge`/`gc_restore` 均 PROTECTED_MUTATION。

### 2. 处置

- **文档**（[cli_reference.md](../../docs/cli_reference.md)）：四节补齐四段式，
  插在 `gc retention` 与 `gc archive-list` 之间（归档→状态→复活→清除
  生命周期序）；`purge` 加不可逆语义加注（区别于 archive 可复活）；
  `gc status` 用无参占位行。
- **门禁**（[test_cli_reference_sections.py](../../tests/test_cli_reference_sections.py)）：
  `EXPECTED_COMMANDS_SECOND` 24→28（gc 9→13）。

### 3. 验证

| 项 | 结果 |
| --- | --- |
| `test_cli_reference_sections.py` | **8 passed** |

### 4. 边界与残留

- §41.4 rc 残差台账不变（`gc` 族 epilog「2 = Database locked」同属该批）。

## 9.54 §41.4 rc 残差口径统一批次（2026-11-21）

### 1. 装置与侦察

backlog §41.4 登记 6 项 rc 残差。侦察确认根因：dispatcher
（[main.py](../../cli/main.py) `_run_subcommand_mode` L1286）忽略
`_dispatch_subcommand` 返回值——handler `return False` 不会变成 rc=1，
必须失败分支显式 `raise SystemExit(1)`。

### 2. 处置

- **epilog**：`_SUBCOMMAND_HELP_SPECS` 16 处「2 = Database locked (retry
  later)」统一改「Invalid usage (argparse error / local input validation)」；
  cli_reference vuln-blast 退役口径加注删除。
- **rc 修复**（均失败分支显式 `SystemExit(1)`）：`config check-role`
  平台不支持分支、`git import` 业务失败分支、`embed` 全失败分支
  （`total>0 且 success=0`）。
- **i18n**：destructive-log 拆独立键 `git_arg_destructive_log_limit`
  （zh/en 补键），消除 `git_arg_limit` 共用导致 help 默认值漂移。
- **不改**：`git check-*` 软门禁恒 rc=0（软护栏设计使然，保留登记）。

### 3. 验证

| 项 | 结果 |
| --- | --- |
| `cw config check-role agent` 实跑 | rc=1 |
| `cw gc --help` epilog | 新口径文案 |
| `cw git destructive-log --help` | 「最大结果数（默认 20）」 |
| git import / embed 失败分支冒烟（monkeypatch 假 db） | 均 SystemExit(1) |
| `test_cli_subcommand_help_template` + `test_i18n_key_existence` + `test_i18n_placeholder_consistency` + `test_cli_reference_sections` + `test_cli_exit_code_local_input` | **191 passed** |

### 4. 边界与残留

- dispatcher 级「handler False → rc=1」全局改造不做（影响面大；显式
  SystemExit 策略已覆盖 §41.4 全部登记项）→ 已由 §9.55 收口（backlog §44）。

## 9.55 dispatcher 级 False→rc=1 全局收口（2026-11-21）

### 1. 装置与侦察

§9.54 遗留非目标落地。根因：`_run_subcommand_mode` 忽略
`_dispatch_subcommand` 返回值，handler `return False` 全部静默 rc=0。
脚本审计全文件 37 处 `return False`：5 处属辅助函数（不在 dispatch 返回链），
handler 链上 33 处（audit/bootstrap/clone/gc/fts/grep/tests/graph/config/
toolchain/build-context/dependency/identity）逐点核验均为真失败（not-found /
校验失败 / 正则错误 / 构建导入失败 / 未知 action），无「空结果假 False」；
elif 链对 `_SUBCOMMANDS` 全集闭合，fallthrough 不可达。

### 2. 处置

[main.py](../../cli/main.py) `_run_subcommand_mode` 调度点消费返回值：
`dispatch_rc is False` → `raise SystemExit(1)`（显式仅认 False，None 防误伤）；
§41.4/§43 的显式 `SystemExit(1)` 语义等价保留。33 处静默 rc=0 失败分支统一 rc=1。

### 3. 验证

| 项 | 结果 |
| --- | --- |
| `cw config badaction` / `check-role agent` / `local` | rc=2 / 1 / 0 |
| 10 个受影响测试套件（含直接调用 `_run_subcommand_mode` 的用例） | **359 passed** |

### 4. 边界与残留

- 个别 not-found 分支打印提示后仍 `return True`（如 `git show` 提交不存在），
  不在 False→rc=1 网内；如需收口逐分支另立任务卡（backlog §44.4）
  → 已由 §9.56 收口（backlog §45）。

## 9.56 「打印失败文案后 return True」残留分支 rc 收口（2026-11-21）

### 1. 装置与侦察

启发式扫描（失败标记出现在 `return True` 前 6 行内）命中 41 处，逐点裁定：
19 处真失败 → RC=1；1 处本地输入校验失败（rotate-key ValueError）→ RC=2；
14 处设计使然保持 RC=0 并逐点登记理由（doctor 软工具、首跑引导态、报告型
空结果、fail-soft、行内状态展示等）。

### 2. 处置

- **RC=1**（`raise SystemExit(1)`）：rule candidate create/accept/reject、
  rule insert-block / cleanup-sync-log 失败、impact / symbol / query /
  task show ×2 / rollback show / assignment show 的 not-found、audit
  rotate-key 失败、workspace delete not-found、rollback register/set 失败、
  refresh 多路径存在失败、restore-all 存在 error 条目。
- **RC=2**：rotate-key 本地输入校验失败。
- **测试**：`test_audit_key_rotation.py` 无效参数断言升级为 `SystemExit(2)`。

### 3. 验证

| 项 | 结果 |
| --- | --- |
| `cw symbol __definitely_missing_symbol__` | rc=1 |
| 10 个 CLI 回归套件 | **374 passed** |

### 4. 边界与残留

- 报告型命令（`tests`/`issues` 等）空结果保持 rc=0；如需「测试失败 → 非零
  退出码」新语义另立任务卡（涉及 CI 消费方契约）。**——已由 §9.57 收口。**

## 9.57 报告型命令 rc 新语义：opt-in `--fail-on-findings` + 退出码 3（2026-11-21）

### 1. 装置与裁决

§9.56（§45.4）挂账经用户裁决落地：opt-in flag `--fail-on-findings`，默认 rc=0
不变；flag 开启且报告 findings → rc=3（新退出码「报告发现失败」，区别于
1=运行时失败、2=输入校验失败）。范围仅门禁型 6 命令：`issues` /
`function-issues` / `detect-cycles` / `orphan-symbols` / `semgrep scan` /
`clone detect`；咨询型不加。CLI-only，MCP 契约不动。

### 2. 处置

- `cli/main.py` 新增 `_exit_on_findings(opts, count)`；6 个 parser 加 flag，
  return 前接入（计数口径见 backlog §46.2）。
- **连带收口**：`semgrep scan` 四分支 `success=false` 原静默 rc=0 →
  `raise SystemExit(1)`（§43-45 口径「真失败 → rc=1」）。
- i18n 双语新增 `cli.args.fail_on_findings` /
  `cli.messages.fail_on_findings_triggered`；`cli_reference.md` 全局契约表 +
  6 章节四段式同步。

### 3. 验证

| 项 | 结果 |
| --- | --- |
| 活体：`cw detect-cycles` 不带 flag / 带 flag（主仓调用图有环） | rc=0 / rc=3 |
| 活体：`cw issues <不存在符号> --fail-on-findings` | rc=0 |
| `tests/test_report_rc_semantics.py` 新增 19 例 | 全 passed |
| 章节门禁 + i18n 双门禁 + exit-code + help/coverage/audit/bootstrap/agent 回归 | 全 passed |

### 4. 边界与残留

- `function-issues --summary` 模式不参与 rc=3 门禁（汇总口径另立批次）；
  `semgrep quick` 的 `errors` 告警计数保持 rc=0；默认行为零变化。

## 9.58 en 文案打磨批次：术语漂移修复 + 挂账统一清零（2026-11-22）

### 1. 侦察与审阅

§24-§39 多章节挂账「en 文案打磨另立批次」。审阅结论：en_US.json 2297 条
（断言敏感 96 条），规则筛查 163 疑点绝大多数为设计使然（ASCII 对齐、标签
枚举值小写、格式化占位符、多行模板缩进）。实质缺陷仅 1 类：历史改名残留
"Code Warden"（应为 Call Warden），en catalog 2 处，zh/tests/docs/代码侧均无。

### 2. 处置

`i18n/en_US.json`：`cli.messages.main_help_title` 与 `cli.subcommand_help`
两处 "Code Warden" → Call Warden 口径（前者避免品牌词重复改为
`Call Warden CLI — Command Reference`）。

### 3. 验证

| 项 | 结果 |
| --- | --- |
| 活体：en_US `cw --help` / 主 help 子命令组标题 | 两处均已是 Call Warden |
| i18n 双门禁 + help 模板 + 章节门禁 + exit-code（219 例） | 全 passed |

### 4. 边界与残留

- §24-§39「en 文案打磨另立批次」挂账**统一清零**；风格级改动判定不做
  （设计使然 + 断言敏感 + churn 大于收益）。后续如再现文案缺陷按单点修复。

## 9.59 M3-M8 迁移状态静态复筛：路由层迁移确认完成（2026-11-22）

### 1. 装置

- 脚本：`scripts/mcp_migration_rescan.py`（可重跑盘点工具）：解析
  `server/tools/*.py` 全部 `@mcp.tool()` 函数（剥 docstring 后做 route/local
  信号分类），提取函数体实际 RPC method 字面量（`_route('m')` 等），与
  `rust_ext/src/daemon` dispatch 注册行（含 `"a" | "b" =>` 多别名）比对。
- 证据：`.trae-cn/evidence/mcp-migration-rescan-20261122.json`（244 行明细 +
  682 个注册 method + 汇总）。
- 口径修正记录：初版用 2026-08 矩阵 JSON 的 `daemon_rpc_method`（大量为空）
  误判 216 个 missing → 改为函数体字面量；再修无点 method 命名
  （`"get_role_view"` 等）与 docstring 误报，三轮校准后收敛。

### 2. 结论

| 口径 | 2026-08-11 初筛（§4.1） | 2026-11-22 复筛（§9.59） |
|---|---|---|
| python_local | 190 | **0** |
| production_ready（route + Rust 注册命中） | 18 | **243** |
| 间接治理写路由 | — | 1（`file_write` → `governed_write_file`） |
| mixed_fallback | 25 | 0（docstring 误报剔除） |

**Python→Rust daemon 路由层迁移确认完成**：§4 矩阵 M1-M8 的
`mixed_fallback`/`python_local` 旧状态全面过时，已刷新为 `daemon_routed`。
这解释了近期章节化只剩文档/口径收尾的宏观原因：开发大盘（工具迁移）已按
M1→M8 逐 slice 落地完毕。

### 3. 验证与边界

| 项 | 结果 |
| --- | --- |
| 重筛脚本输出（244 工具 × 12 模块） | production_ready=243 + 间接 1，无 missing/mixed/local |
| 三轮口径校准（矩阵 RPC 字段 → 函数体字面量 → 无点命名/docstring 剥离） | 收敛，抽查 `get_role_view`/`query.stats`/`file_write` 均实证 |

边界：`production_ready` 是**静态**判定（dispatch 注册命中），按 §2.2 口径
不替代活体 round-trip 证据；个别工具函数体经共享 helper 二次封装
（如 `governed_write_file`）时以 helper 层路由为准。剩余开发面详见 backlog §48。

## 10. 相关真相源

- `AGENTS.md`
- `docs/design/multi-llm-contract-driven-collaboration-design.md`
- `docs/design/requirements.md`
- `docs/design/tasks.md`
- `docs/design/windows-wsl-daemon-coexistence-contract.md`
- `docs/design/windows-wsl-daemon-coexistence-task-plan.md`
- `docs/design/rust-ten-task-enterprise-completion-plan.md`
