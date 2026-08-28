# A″：PyO3 数据库 / daemon transport 调用面收敛

**状态**：待独立 Reviewer 审阅的任务草案；**不是**已创建的 CW task。  
**建议挂载父任务**：`T-1787203926824-9f873bfc`。  
**建议类型**：与 A′ 并列的独立 Epic 子任务，不挂在 `T-1787293451688-c14b1e44`（A′）之下。  
**建议交接起点**：`executor / planner`。  
**本草案不授权**：创建任务、修改既有任务、重开/关闭任务、runtime refresh、修改 source 或直接访问数据库。

---

## 1. 建卡结论与前置门禁

A″ 不是对 A′ 现有 MCP/CLI 卡的补丁，而是下一阶段的**客户端边界收敛 Epic**。A′ 的职责是把公开 MCP/CLI 的单条业务链路收敛为 Rust daemon authority；A″ 的职责是清理 Python 进程内仍残留的 **daemon client、IPC framing、authority/DB-adjacent PyO3 调用面**，使 Python 完成 HTTP/MCP/CLI thin-client 化。两者的实现所有权、可测试对象和退役风险不同，必须保持 sibling 关系。[1]

当前只读 task tree 表明 A′ 处于 `review`，有 187 个直接子任务、190 个后代，其中 156 个仍在 `review`、1 个 `in_progress`。因此现在不应把 A″ 实施卡插入 A′，也不应通过在审 A′ 卡扩 scope 的方式“顺手删 PyO3”。[2]

| 建卡或释放条件 | 判定方式 | 未满足时的处置 |
|---|---|---|
| P0-K 已独立 Reviewer PASS，且由独立 Adjudicator 正确处置 | append-only verdict/lease/fencing/task status 证据 | 不创建 A″；不得部署或 refresh |
| live authority 与受控 `runtime/current` 收敛 | 最新 manifest、PID、binary SHA、schema、commit 与受控制品对照 | 不创建 A″；漂移作为 deployment governance finding 保留 |
| A′ 已闭环 | A′、全部 descendant 与必要 gate 均为 `closed`；当前矩阵 `python_compat=0` | 不创建任何 A″ 子卡 |
| Python compatibility worker 无公开工具 backend | HTTP `/v1/meta/tools` 与生成矩阵均无 `target_backend=python_compat` | 不让 A″ 承接剩余 MCP business logic |
| 旧 S3 历史卡的处置独立完成 | 只通过正式 append-only supersede/retirement governance | A″ 不修改、不 reopen、不冒充 successor |

> **硬顺序**：A″ 可以先作为“待评审草案”存在；但只有上述五项都满足后，才能由合法 planner/executor 通过 daemon 创建 A″ 父任务。父任务创建后也只能释放首卡 A″-G0。A″-G0 得到独立 review、adjudication 和 `applied` 后，才允许释放第一张实现子卡。

---

## 2. 建议父任务卡描述（可直接作为 task description）

```markdown
# A″：PyO3 数据库 / daemon transport 调用面收敛（Python HTTP thin-client 化）

**任务类型**：独立 Epic 子任务 / A″ client-boundary convergence parent  
**父任务**：`T-1787203926824-9f873bfc`  
**工作区 authority**：创建时由 daemon 返回并冻结；不得手填/复用陈旧 capture。  
**创建角色**：executor / planner  
**交接起点**：reviewer  
**版本**：A″ v1（冻结后不可回写历史 contract）

## 目标

在 A′ 完成公开 MCP/CLI Python compatibility 后端到 Rust daemon 的业务迁移之后，逐一清理 Python 中仍承担以下语义的 PyO3 调用面：

1. daemon client / legacy IPC call；
2. IPC framing、response/error builder、request builder；
3. daemon authority / local DB-adjacent helper；
4. 仅当存在完整 artifact transfer contract 时，snapshot/FD 相关参数构造。

终态要求：Python 的公开 CLI、MCP 与 SDK adapter 仅做 HTTP API request shaping、response formatting、非秘密配置读取和错误呈现；业务 SQL、workspace/task authorization、lease/fencing、operation ledger 和 daemon lifecycle authority 均由 Rust `cw-daemon` 拥有。

## 非目标

本任务**不**把所有 `callwarden_core` PyO3 exports 改成 HTTP。Parser、watcher、tree-sitter、图/向量/clone 算法、CAS、压缩、hash、backup 内核等本地 Rust compute 继续保留为 Rust library 或 daemon 进程内实现；它们不是 client transport，也不应为追求“全 HTTP”制造远程调用、额外拷贝或新的 failure domain。

本任务不包含 MCP business handler、CLI business command、Role Worker/governance policy、公司级 TLS/SSO/组织鉴权、远程部署、database engine migration、schema 重构或旧 S3 的状态修改。这些必须有独立任务与独立审查。

## 安全与一致性不变量

1. Python、Java 或 MCP client 不得 `sqlite3.connect`、构造 `CodeGraphDB`、直接写 CAS 或绕过 daemon 作任何 business SQL fallback。
2. Enterprise/HTTP transport 不可用时 fail-closed；禁止退回本地 SQLite、legacy daemon IPC、provider token、伪造 identity、伪造 lease 或 placeholder credential。
3. Role Worker 的本地 raw credential 只用于其 task role authorization；不得将其重用为通用 HTTP bearer secret、DB password 或 provider credential。
4. provider account/model/agent/session 是可变、无秘密、append-only runtime provenance；不得成为权限锚点。
5. 删除任一 PyO3 export 前必须已有完整 HTTP successor、所有 Python call site 已通过 import/use-site audit、source/black-box/no-fallback tests 均通过，并保留 ABI/compat decision evidence。
6. 本任务的任何 child 不得发布/替换 live daemon；部署只可在独立 Reviewer PASS 后，由合法 Adjudicator 的受控 deployment card 完成。

## 父任务步骤

1. **govern**：核验 P0-K、A′、matrix、runtime/current/live authority、旧 S3 的前置状态；将其作为 append-only evidence 固化。任何前置不满足即停在 `blocked_to_user`，不创建 child。
2. **release_gate**：A″-G0 被独立 review + applied 后，按本 parent 的 successor rules 一次只释放一张可迁移 child。
3. **completion_verify**：核验所有 A″ child closed、所有已批准 retirement export 无 Python use site、Python HTTP-only denial matrix 通过、旧 S3 处置已独立完成；之后才可移交 final review。

## Role Contract 摘要

- Executor：仅实现当前被领取的一张 child；可 report/evidence，不能 apply/close、不能发布 runtime、不能创建后继卡。
- Reviewer：只读复现当前 child；只提交 PASS 或 BLOCKED，禁止改 source/task/evidence/contract。
- Adjudicator：只在独立 PASS 后核验 identity/lease/fencing/evidence 后 apply/close；不得补代码、补计划或重塑历史。

## 滚动派发规则

- A″-G0 `applied` 前，不得创建或领取任何 A″ 实施卡。
- 每一个 port family 的前一张卡须 `closed`，且 registry/manifest/no-fallback evidence 为 green，才可释放其后一张卡。
- `snapshot.publish` / FD / artifact 子卡还必须通过 artifact transfer Gate；不得被普通 request builder 卡解锁。
- 任意 BLOCKED 必须在同一 child 内 append `fix_defect` remediation；不得创建无边界 remediation child。
```

---

## 3. 首卡：A″-G0 调用面清单与退役契约 Gate

### 3.1 建议标题与卡级身份

> **A″-G0 [Gate/client_boundary]：PyO3 daemon/authority 调用面清单、HTTP successor 与退役契约冻结**

| 字段 | 建议值 |
|---|---|
| `port_type` | `client_boundary` |
| `port_key` | `callwarden_core::daemon_client / daemon_query authority surface` |
| `gate` | `true` |
| `successor_rule` | 只有独立 Reviewer PASS 且 Adjudicator `apply` 后，才可建立/领取 A″-01；任何字段缺失或 runtime drift 时 fail-closed |
| 父任务 | A″（创建时 daemon 返回真实 task ID） |
| 依赖 | P0-K、受控 runtime convergence、A′ 全部 closed、`python_compat=0`、旧 S3 独立 disposition |
| 默认交接 | executor → reviewer → adjudicator → complete |

### 3.2 唯一目标

本卡**只产出清单与冻结决策**；不删除 export、不改生产 route、不更新 matrix、不部署。它必须为 `rust_ext/src/lib.rs` 中每个与 daemon/authority 相关的 PyO3 export 建立一个 canonical `pyo3_surface_manifest_v1` 条目，确定它属于：

- `retain_local_core`：本地 Rust compute，永久不属于 HTTP retirement；
- `replace_with_http_client`：已有或应新增的 HTTP client successor；
- `retire_after_zero_callers`：已有 successor，仅待 call site 归零和兼容窗口完成；
- `requires_artifact_contract`：受 FD/memfd/large-payload semantics 约束，不能直接替换；
- `requires_separate_authority_contract`：涉及 peer credential/ACL/policy，必须另建 auth contract；
- `unknown_blocked`：任何 import/call-site/ABI/side effect 不明之项，不得删除。

### 3.3 只读基线（已取得）

静态清单从 `rust_ext/src/lib.rs` 的 `wrap_pyfunction!` 导出获得 **162** 项：`daemon_client_candidate=9`、`protocol_or_peercred_candidate=14`、`authority_helper_candidate=11`、`local_core_or_nontransport=128`。显式 `callwarden_core.<export>(...)` 搜索在 `cli/`、`server/`、`cw.py`、`config.py` 中命中为零；这只能证明没有显式模块限定调用，**不能**证明不存在 `from callwarden_core import x`、动态加载或外部用户调用。因此它是 A″-G0 的输入，而不是删除依据。[3]

| 候选族 | 数量 | G0 的必做结论 | G0 后可否自动删 |
|---|---:|---|---|
| `daemon::client::*` | 9 | 列出 HTTP successor、Python import/call sites、ABI consumers、legacy IPC/FD 依赖 | 否 |
| `daemon_query::protocol_* / peercred_* / dispatch_*` | 14 | 区分 HTTP envelope helper、legacy framing、local peercred primitive、daemon metadata | 否 |
| `daemon_query` authority/budget/health helpers | 11 | 判断是应转 HTTP capability、daemon internal，还是应保留 local diagnostic pure function | 否 |
| local compute/nontransport | 128 | 明示 `retain_local_core` 或 out-of-scope 理由 | 不适用 |

### 3.4 允许与禁止路径

| 类别 | 路径/对象 |
|---|---|
| 允许 | `deliverables/software-company/` 的 G0 evidence 与 manifest；`docs/design/` 的 A″ contract draft；`rust_ext/src/lib.rs`、`rust_ext/src/daemon/client.rs`、`rust_ext/src/daemon_query.rs`、`server/daemon_client.py`、`server/ipc_transport.py`、`cli/` 仅作**只读**调用面映射；专属静态分析测试/fixture |
| 禁止 | 所有 production export 删除或 signature 修改；`db/`、schema/migration；`task_collab.rs`、role/lease/verdict；`scripts/refresh_shared_runtime.ps1`；runtime/current/live daemon；A′ task cards；旧 S3 task；任何 credential/session/role-session 文件 |

### 3.5 必交证据与验收

1. `pyo3_surface_manifest_v1.json`：162 exports 全覆盖、唯一 symbol、分类、owner、source file、所有 known Python/external call site、successor、ABI decision、dependency/gate、retirement condition；无 secret。
2. `pyo3_surface_manifest_v1.md`：人可读表格、未知项及 BLOCKED 理由。
3. `pyo3_import_use_audit.py`：AST + import graph、动态 import/search 规则、external ABI 搜索范围、false-positive/negative 限制。
4. 交叉核验：HTTP `/v1/meta/tools`、迁移矩阵、`RpcDBProxy`/`HttpDaemonRpcClient`、Rust dispatch 与 manifest 中 `replace_with_http_client` 项一致。
5. deny matrix：G0 本身无 source behavior change；验证未触碰生产 export、未发起 task mutation、未读取 role credential、未做 runtime refresh。

G0 的 pass 条件是“每一个候选均有可复核 disposition，且每一项 implementation card 至多覆盖一个 API family/一个 clear successor”。**G0 PASS 不代表任何 export 已经删除。**

### 3.6 G0 handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 只读核验 162 个 PyO3 export 是否逐项在 manifest 中存在唯一 disposition；随机抽样复现 import/use-site audit，确认 HTTP successor、FD/artifact 依赖和 retain-local-core 边界没有被混淆；确认 G0 未改任何生产 export、runtime 或任务状态。
  reason: A″-G0 是 client_boundary Gate；只有审查、裁决且 applied 后才可释放第一个 PyO3 transport/authority implementation child。
  independence_requirement: required
```

---

## 4. A″ 后续子任务拆分方案

### 4.1 释放原则

下面是**后续卡模板与次序**，不应在 G0 之前批量入库。每张卡必须由 G0 manifest 给出精确 function/export/call-site 白名单，并在上一张相关 family 卡 `closed` 后才可创建。对于 G0 决定 `retain_local_core` 或 `requires_separate_authority_contract` 的项，永远不建立 A″ implementation card。

| 阶段 | 建议卡 ID（逻辑） | 单一所有权 | 依赖 | 产出 |
|---|---|---|---|---|
| A″-01 | `daemon_client_call_py` Unix IPC 调用退役 | 一项 Unix-only PyO3 client export | G0 applied；HTTP client success/failure parity | Python 不再调用该 export；Rust export 能安全 retire 或明确 deprecate |
| A″-02 | HTTP envelope request builder 收敛 | `build_request_py` + `parse_rpc_response_py` 这个不可分的 request/response pair | A″-01 closed | HTTP client canonical serializer/parser；no IPC fallback |
| A″-03 | Query request builder 收敛 | `build_query_request_py` | A″-02 closed | `HttpDaemonRpcClient`/Java contract 同形参数；response parity |
| A″-04 | Simple RPC request builder 收敛 | `build_simple_request_py` | A″-02 closed | health/status/list HTTP wrapper；无 PyO3 builder consumer |
| A″-05 | General RPC request builder 收敛 | `build_rpc_request_py` | A″-02 closed | register/backup/restore/gc/snapshot/mount client request contract |
| A″-06 | Connect/refresh builder 收敛 | `build_connect_params_py` + `build_refresh_params_py` 作为 agent-session pair | A″-02 closed；P0-K role model stable | runtime provenance only；无外部 session 授权锚点 |
| A″-07 | HTTP protocol helper retirement | `protocol_constants`、`protocol_decode_payload`、`protocol_parse_response` 的 HTTP-side helpers | A″-02 closed | Python HTTP codec/typed contract；旧 PyO3 calls zero |
| A″-08 | Legacy IPC framing family retirement | `protocol_encode_payload`、`protocol_build_frame`、`protocol_parse_header`、`protocol_validate_message_size` | A″-01 closed；无 legacy IPC business client | length-prefix IPC framing Python consumers zero |
| A″-09 | Legacy IPC error builder retirement | `protocol_make_ok_response`、`protocol_make_error_response` | A″-08 closed | error mapping only through HTTP JSON-RPC contract |
| A″-10 | Local authority metadata → HTTP capability | `dispatch_list_methods`、`dispatch_list_error_codes`、`dispatch_is_admin_method` | G0 applied；capability endpoint contract | `/capabilities`/`/v1/meta/tools` is canonical external metadata source |
| A″-11 | Local health helper disposition | `health_check_all` | A″-10 closed | HTTP health capability or explicit retained local diagnostic; no ambiguous duplicate authority |
| A″-12 | Local ACL/path helper disposition | `validate_owned_path`、`check_path_within_workspace`、`check_workspace_owner` | G0 must mark exact paths | helpers move daemon-internal or become policy capability; no client-side authorization decision |
| A″-13 | Local UID/budget helper disposition | `is_admin_uid`、`current_daemon_uid_py`、`budget_*` | A″-12 closed | authorization/budget lives inside Rust daemon; client sees structured denial/metadata only |
| A″-14 | Artifact/snapshot parameter boundary Gate | `build_publish_params_py` only; **design/contract card first** | A″-05 closed; artifact transfer contract independent PASS | chooses retain/replace only; no FD removal yet |
| A″-15 | Artifact HTTP successor | `snapshot.publish` payload/artifact path after A″-14 | artifact Gate applied | large payload/cancel/hash/retry/cleanup parity; no direct CAS |
| A″-16 | Python HTTP-only end-state verification | all A″ approved retirements as evidence only | all created A″ children closed | source + runtime capability + failure denial matrix and compatibility report |
| A″-17 | Legacy PyO3 ABI retirement disposition | release/deprecation/removal decision | A″-16 independent PASS | semver/migration notice, user script compatibility and rollback decision |

### 4.2 特别不建立实施卡的项目

以下不是“漏迁移”，而是 A″ 应明确排除的对象：tree-sitter/parse、watcher 本地事件采集、graph/impact/vector/clone compute、CAS/hash/compression、backup inner algorithms、纯 formatting/config utility。它们仍可被 daemon 进程内调用或作为本地 Rust SDK 使用；真正的 client DB/authority path 已通过 A′ / A″ 收敛。任何把它们逐个 HTTP 化的要求都要另立性能、payload、offline/sidecar contract，不得附带在 A″。

### 4.3 每张 implementation child 的固定 description 模板

```markdown
# A″-NN：<one API family> → HTTP thin-client / daemon-internal 收敛

**父任务**：A″（真实 daemon task ID）  
**port_type**：`client_boundary`  
**port_key**：`<唯一 PyO3 symbol family>`  
**gate**：false  
**execution_dependency**：`<前卡>` 已 `closed`；A″-G0 已 `applied`。

## 唯一范围

- PyO3 export：`<precise rust symbol(s)>`；最多一个不可分 API family。
- Rust target：`<exact .rs file and functions>`。
- Python callers：`<exact caller files/functions from G0 manifest>`。
- HTTP successor：`<method + request/response schema + capability row>`。
- Tests：source import/use-site zero；success/error/no-daemon/no-local-SQL parity；request-id/retry if mutation.

## 禁止范围

- 不修改任何其他 PyO3 export 或 API family；不修改 `db/`、schema、task/lease/verdict/Role Worker、A′ cards、旧 S3、runtime refresh。
- 不得将 provider/model/session/role worker raw credential 作为 HTTP transport credential。
- 不得添加 Python SQLite/CodeGraphDB fallback，daemon 不可用时 fail-closed。

## Pass 条件

1. 所有 manifest 列出的 caller 已迁移或显式保留；零未解释 caller。
2. canonical HTTP successor 的 success、malformed request、daemon unavailable、restart/idempotency（如适用）均被测试。
3. Python source 不导入 DB，不直接访问 SQLite/CAS，不保留 legacy IPC fallback。
4. 独立 Reviewer 复现；独立 Adjudicator 以真实 identity/lease 执行 apply/close；未部署的 source changes 不声称为 live runtime。
```

---

## 5. 与 A′、旧 S3 和后续 HTTP/鉴权 Epic 的关系

| 任务/阶段 | A″ 是否修改 | 关系 |
|---|---|---|
| A′ `T-1787293451688-c14b1e44` | 否 | A″ 只能在 A′ fully closed 和 matrix `python_compat=0` 后开始；A′ 继续完成其已建 CLI/MCP 单链路卡。 |
| P0-K | 否 | A″ 以 P0-K reviewer/adjudication/runtime convergence 为创建前置；不得复用 P0-K executor/reviewer/adjudicator identity。 |
| 旧 S3 `T-1787203937208-0a795c68` | 否 | 历史范围过宽；不重开、不修改，日后单独 append-only disposition/supersede。 |
| Enterprise HTTP-local transport | 否 | A″ 只去除 client-side legacy transport helper；HTTP-over-UDS/npipe 或 TLS company authority 需独立 transport contract。 |
| 公司级鉴权与共享 daemon | 否 | 需单独的 org/project/principal/data-plane Epic；A″ 仅提供“客户端不直连 DB”的先决条件。 |

## 6. 审查问题清单

Independent Reviewer 应在允许入库前回答：

1. A″ 是否确实与 A′ 的 MCP/CLI business chain 无 scope overlap？
2. 是否把 128 项 local Rust core 显式排除，而非假设所有 PyO3 都应远程化？
3. 五项 creation gate 是否足够严格，且 P0-K/live runtime drift 未被文本 PASS 掩盖？
4. A″-G0 是否只产出清单/contract，未暗中给予删除或部署权限？
5. 是否所有 implementation card 都是一个 API family、一个 HTTP successor、一个独立 evidence family？
6. `build_publish_params_py` 是否被延后到 artifact contract，而没有错误塞入普通 8 MiB JSON RPC？
7. 是否明确禁止共享 SQLite、客户端 DB fallback、借用 token/identity 和跨卡批量迁移？

## References

[1] [`enterprise_http_transport_and_pyo3_convergence_assessment_20260827.md`](enterprise_http_transport_and_pyo3_convergence_assessment_20260827.md)：HTTP、UDS/named-pipe 与 PyO3 的层次边界。  
[2] [`candidate_task_tree_summary_20260827.json`](candidate_task_tree_summary_20260827.json)：A′ 当前只读 task tree 状态与 descendant 汇总。  
[3] [`pyo3_authority_surface_inventory_20260827.json`](pyo3_authority_surface_inventory_20260827.json)：162 PyO3 export 的静态分类输入。  
[4] [`cw_cli_mcp_rust_daemon_data_access_audit_20260827.md`](cw_cli_mcp_rust_daemon_data_access_audit_20260827.md)：239 MCP 工具中 58 个 python_compat、CLI Proxy 与 Rust backend 的当前静态审计。  
[5] [`AGENTS.md`](../../AGENTS.md)：三角色职责、append-only history、task scope 与 apply/close 门禁。


---

## 修订记录 A″-R1（2026-08-27）：创建门禁下移

**用户决策**：A′ 的大部分既有迁移 child 已进入 `review`，因此不再把“A′ parent 全部 closed、矩阵 `python_compat=0`、旧 S3 已独立 disposition、live/runtime SHA 已收敛”设为 **A″ parent 或 A″-G0 的创建前置**。允许在总父任务下现在创建一个可见的 A″ 路线图 parent 及唯一 A″-G0。

这不是放开实现。上述条件被改为 **A″ implementation microtask（A″-01 及以后）的 release/claim 前置**。A″-G0 是无 production source change、无 runtime change、无 deployment 的静态 calling-surface manifest Gate，可以在已满足 P0-K closed、root parent active、动态 HTTP authority manifest/PID/health 一致的条件下开展。A″-G0 仍不能创建、领取或派发 A″-01…A″-37；它只能审计、列清单、冻结 successor/retain/blocked disposition，并交给独立 Reviewer。

| 约束 | R0（旧草案） | R1（本次修订） |
|---|---|---|
| 创建 A″ parent | 等 A′ fully closed + matrix 清零 + runtime 收敛 + S3 disposition | 允许现在以 daemon append-only `task.create` 创建 |
| 创建 A″-G0 | 同上 | 允许作为唯一初始 child 创建 |
| 执行 A″-G0 | 被 R0 全部条件阻断 | 允许仅做静态 inventory/contract evidence；禁止 production edits、deployment、runtime refresh |
| 创建/领取 A″-01…A″-37 | 不适用 | 仍要求 A′ fully closed、`python_compat=0`、runtime convergence、旧 S3 独立 disposition，且 G0 必须 reviewer-pass + adjudicator-applied |
| 旧 S3 | 独立 disposition | 不变；A″ 不修改、不重开、不继承其 ownership |

A″ parent 与 A″-G0 任务描述必须显式写入 `visibility_only_until_release_gates=true` 及这一区分，防止任何 `open`/`review`/文本 handoff 被误读为 implementation authorization。


## 修订记录 A″-R2（2026-08-27）：可见路线图卡的 Role Worker bootstrap 边界

在准备入库时复核 `TaskCollabStore::handle_task_create` 发现：该 handler 会在同一 SQLite transaction 写入 task、workspace binding、steps、legacy role contracts 和 modern governance projection；它通过内部 `task_create_contract_envelope(task_id, title, description, steps)` 创建 revision-1 envelope。然而当前 helper 不读取 caller 传入的 `task_contract_envelope`，且其生成 envelope 没有 `identity_policy` 字段。因此，向 `task.create` 传入 `identity_policy=role_worker_v1` 不能作为已冻结 policy 的证据。

本次用户授权创建的 A″ parent 与唯一 A″-G0 将采用该已部署的原子建卡路径，**但只作为可见、不可领取的路线图与静态 Gate 卡**。创建 description 将显式声明：在任何 `task.next_action`、claim、report、reviewer verdict、apply 或 close 之前，必须由真正独立的 Reviewer/Adjudicator Role Worker 通过正式 daemon `task.contract_bootstrap` 为每张卡追加带 `identity_policy=role_worker_v1` 的 Task Contract revision，并保存无秘密 receipt。当前窗口不得借用 reviewer/adjudicator credential 或代替其执行 bootstrap。

| 事项 | 允许 | 禁止 |
|---|---|---|
| `task.create` A″ parent/G0 | 允许；原子 task/workspace/steps/role contracts/projection，且不含 secret | 不把 generic revision-1 标称为 Role Worker policy 已冻结 |
| role-worker policy bootstrap | 留给独立 Reviewer + Adjudicator workflow，使用 daemon HTTP append-only path | 当前 executor/planner 不借用另一角色凭据，不伪造 token/identity，不直接 SQLite |
| A″-G0 静态盘点 | bootstrap receipt 后、前置 release gate 满足时才可 claim | 不能在 generic/legacy contract 下用 runtime string 领取或执行 |
| A″-01…37 | 仍不创建也不领取，直到 G0 applied 与 R1 execution gates 全部满足 | 不以 parent `open`、子卡 `open`、聊天 PASS 或文本 handoff 推断授权 |

这不是对 P0-K scope 的回退或修订；而是如实承认 `task.create` 的当前 envelope policy gap，并阻止它成为外部身份绑定、legacy 隐式降级或无 contract 预建卡的旁路。
