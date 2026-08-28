# A″：PyO3 数据库 / daemon transport 调用面收敛——逐小任务拆分草案

**性质**：待独立审查的任务树草案；不代表任务已创建或可领取。  
**建议父任务**：未来在 `T-1787203926824-9f873bfc` 下建立 A″，作为 A′ 的 sibling。  
**约束**：本草案不改变 A′、P0-K、旧 S3、runtime/current 或任何 daemon 状态；不创建 child，不触发部署。

## 1. 为什么必须拆到“一个 export / 一个不可分语义对”

原 A″-01 至 A″-17 仍是 API-family 层级，不能让 executor 直接领取。一个 executor 小卡只能有**一个明确退役对象**：一个 PyO3 export，或一个不能拆开的 request/response 语义对；只允许修改该 export 的 Rust registration、G0 证明存在的 caller、一个 HTTP successor 和专属测试。它不能顺手移除同模块的其他 export，不能迁移 MCP/CLI 业务逻辑，不能动 SQLite schema、task governance 或 live runtime。

当前静态清单从 `rust_ext/src/lib.rs` 的 `wrap_pyfunction!` registry 得到 **162 个 exports**。其中可能涉及 daemon/authority 的只有 34 项：9 个 daemon client、14 个 protocol/peercred/dispatch metadata、11 个 authority/budget/health helper；另有 128 项是 local core 或 nontransport，A″ 必须明确保留而非 HTTP 化。[1] 目前在 `cli/`、`server/`、`cw.py`、`config.py` 中未找到显式 `callwarden_core.<export>(...)` 调用；这不是“零调用方、可以删除”的结论，因为仍可能有 import alias、动态加载或外部 ABI consumer。因此先有 G0，再按 G0 manifest 的真相**条件释放**小卡。[1]

> **总原则**：A″ 不批量入库。先建立 A″ 父任务，随后只释放 A″-G0；G0 被独立 Reviewer PASS、Adjudicator `apply` 后，才根据它的逐项 disposition 创建下表中的第一张合格小卡。每张实施卡 `closed` 且从 source/runtime evidence 再次验证后，才能创建它的直接后继。

## 2. A″ 父任务的最终 child 结构

```text
A″ parent
├─ A″-G0  PyO3 surface manifest / successor / retireability Gate
├─ A″-01…09   daemon::client exports（每 export 一卡；A″-07 另受 artifact Gate）
├─ A″-10…18   protocol exports（每 export 一卡）
├─ A″-19…20   peercred metadata exports（每 export 一卡）
├─ A″-21…23   dispatch metadata exports（每 export 一卡）
├─ A″-24…34   authority / health / budget exports（每 export 一卡）
├─ A″-G1      artifact / snapshot HTTP-successor contract Gate
├─ A″-35      build_publish_params_py（仅当 G1 已 applied）
├─ A″-36      Python HTTP-only / zero-callers end-state verification
└─ A″-37      PyO3 ABI deprecation or removal disposition
```

A″-01…34 是**预定义卡槽**，不是马上要建的 34 张任务。每个卡槽只有在 A″-G0 的 `pyo3_surface_manifest_v1` 将该 symbol 标记为 `replace_with_http_client` 或 `retire_after_zero_callers` 后才具备创建资格；标为 `retain_local_core`、`requires_artifact_contract`、`requires_separate_authority_contract` 或 `unknown_blocked` 的项目不能创建 implementation card。

## 3. 所有实施小卡共有的四步与硬约束

每一张具体实现卡都只允许下列四个 steps，避免“迁移、测试、删除、部署”同时混在一个无边界步骤里。

| Step | action | 允许工作 | 完成判定 |
|---|---|---|---|
| 0 | `inspect_contract` | 读取 G0 manifest、精确 import graph、HTTP successor、existing tests 与 ABI consumer | 目标 symbol 只有一个 disposition；所有 caller/owner/contract 已列出；任何 unknown → 报告 BLOCKED |
| 1 | `port_one_surface` | 在 Rust daemon/internal module 实现或复用 single HTTP successor；迁移 G0 列出的该 symbol callers；只修改该 `wrap_pyfunction!` registration | Python 无直接 DB/legacy IPC fallback；行为保持；未删其它 export |
| 2 | `verify_one_surface` | 运行该 symbol 的 unit + HTTP success/malformed/unavailable/restart tests；执行 AST/import zero-call audit | success/error equality 或明确 versioned change；failure fail-closed；零未解释 caller |
| 3 | `evidence_handoff` | 记录 source diff、test hashes、capability/runtime evidence 与 ABI disposition；`task.report` 交给 reviewer | 所有约束可复现；不部署、不 apply/close |

所有卡共同禁止：`db/`、schema/migration、`task_collab.rs` governance、Role Worker/lease/verdict、A′ 既有卡、旧 S3、共享 SQLite、Python `sqlite3.connect`/`CodeGraphDB` fallback、direct CAS write、provider token/raw role credential、`refresh_shared_runtime.ps1` 或替换当前 live daemon。

---

## 4. G0：唯一先行 Gate（不可省略）

### A″-G0 [Gate/client_boundary]

**标题**：`A″-G0 [Gate/client_boundary]：PyO3 daemon/authority surface manifest、HTTP successor 与 retireability 冻结`  
**唯一所有权**：完整、可审计地给 162 个 `callwarden_core` exports 做分类；不改 production source。  
**目标文件**：只读 `rust_ext/src/lib.rs`、`rust_ext/src/daemon/client.rs`、`rust_ext/src/daemon_query.rs`、`server/daemon_client.py`、`server/daemon_protocol.py`、`server/ipc_transport.py`、`cli/`；新 evidence 仅在 `deliverables/software-company/aprime2/`。  
**依赖**：P0-K 独立 PASS/adjudication、受控 runtime convergence、A′ closed、matrix `python_compat=0`、旧 S3 独立 disposition。  
**后继规则**：G0 必须 `applied`，并且对于某个 symbol `disposition in {replace_with_http_client, retire_after_zero_callers}`，才可**单独**创建对应 A″-NN。

G0 产物必须是 `pyo3_surface_manifest_v1.json` 和 `.md`，一项一行：`symbol`、`category`、`source_definition`、`wrap_registration`、`all_python_imports`、`all_external_consumers`、`current_semantics`、`db_or_authority_effect`、`HTTP_successor`、`disposition`、`required_gate`、`removal_condition`、`ABI_version_decision`、`owner`。缺任何列均 fail-closed。

---

## 5. daemon client：每个 export 一张卡

下表每一行是一个**独立小卡**。所有 Python caller 均以 G0 import graph 为白名单；当前“未命中显式模块限定调用”不构成自动 removal 许可。

| 小卡 | 唯一 PyO3 export | 精确 Rust 位置 | HTTP successor / 只准修改的 client 位置 | 直接依赖 | 专属验证 |
|---|---|---|---|---|---|
| A″-01 | `daemon::client::daemon_client_call_py` | `rust_ext/src/daemon/client.rs`; `lib.rs` Unix-only registration | `server/daemon_client.py::HttpDaemonRpcClient.call`；删除/弃用这个**Unix-only UDS client entry**的 G0 callers | G0 applied；symbol 为 `retire_after_zero_callers`；无 FD caller | Unix legacy-IPC caller zero；HTTP JSON-RPC success/malformed/unavailable；没有 `UnixStream` fallback |
| A″-02 | `daemon::client::build_request_py` | `daemon/client.rs::build_request`; `lib.rs` registration | canonical HTTP envelope builder in `server/daemon_client.py::HttpDaemonRpcClient.call` or generated SDK | A″-01 closed；symbol 为 `replace_with_http_client` | `jsonrpc/protocol_version/id/method/params` request golden；request-id replay semantics；legacy request call sites zero |
| A″-03 | `daemon::client::parse_rpc_response_py` | `daemon/client.rs::parse_rpc_response`; `lib.rs` registration | `HttpDaemonRpcClient._handle_response` structured error mapping | A″-02 closed | success result、JSON-RPC error、non-JSON status、request-id mismatch golden；no direct PyO3 parser consumer |
| A″-04 | `daemon::client::build_query_request_py` | `daemon/client.rs`; `lib.rs` registration | query helper 内的 `HttpDaemonRpcClient.call` parameter normalization | A″-02 and A″-03 closed | query params/`workspace_instance_id` success、unknown workspace、invalid params、daemon unavailable |
| A″-05 | `daemon::client::build_simple_request_py` | `daemon/client.rs`; `lib.rs` registration | health/status/list thin wrappers | A″-02 and A″-03 closed | health/status/list response & stable error code; source audit proves no PyO3 builder fallback |
| A″-06 | `daemon::client::build_rpc_request_py` | `daemon/client.rs`; `lib.rs` registration | generic client RPC serializer only; preserve `request_id` dedup | A″-02 and A″-03 closed | one read + one mutation request canonicalization, retry with same id, conflict with mismatched id, no legacy transport fallback |
| A″-07 | `daemon::client::build_connect_params_py` | `daemon/client.rs`; `lib.rs` registration | HTTP `workspace.connect` / agent-connect param adapter | A″-06 closed；P0-K role/provenance contract stable | runtime provenance accepted after agent/model/session change; client cannot submit authorization identity; invalid workspace failure |
| A″-08 | `daemon::client::build_refresh_params_py` | `daemon/client.rs`; `lib.rs` registration | HTTP workspace refresh param adapter | A″-06 closed | target/path/budget validation carried by daemon; stale authority/unavailable denial; no Python local refresh DB path |
| A″-09 | `daemon::client::build_publish_params_py` | `daemon/client.rs`; `lib.rs` registration | **no implementation before A″-G1**; eventual artifact-id-only `snapshot.publish` HTTP params | A″-G1 applied；symbol allowed by G1 | artifact id + hash success; unknown/mismatched/cancelled artifact failure; no `db_path`/FD/SCM_RIGHTS client behavior |

**A″-01 special boundary**：`daemon_client_call_py` is Unix-only and may use UDS. It is not removed merely because TCP HTTP exists. The G0 manifest must prove all production callers have a secure HTTP successor; `call_with_fd` / snapshot publish callers route to G1, never into A″-01.

**A″-06 special boundary**：if mutation policy/identity is still under a separately governed P0-K task, A″-06 is not released. Its test may exercise a safe daemon fixture but cannot relax Role Worker/lease/fencing semantics.

---

## 6. Protocol framing：每个 export 一张卡

`daemon_query.rs` explicitly states these APIs are pure calculation helpers for the legacy UDS framed protocol, while socket I/O and dispatch stay outside PyO3. They are not automatically HTTP helpers: HTTP uses a different JSON-RPC envelope and no four-byte length prefix.[2]

| 小卡 | 唯一 PyO3 export | 精确 Rust 位置 | 唯一 successor / scope | 直接依赖 | 专属验证 |
|---|---|---|---|---|---|
| A″-10 | `daemon_query::protocol_constants` | `rust_ext/src/daemon_query.rs::protocol_constants`; `lib.rs` registration | HTTP `/capabilities` / versioned constants payload；不得复制 UDS frame constants 到 client | G0 applied；`replace_with_http_client` | client capability negotiation；old header/max-FD constants 无 production PyO3 consumer |
| A″-11 | `daemon_query::protocol_encode_payload` | `daemon_query.rs::protocol_encode_payload`; `lib.rs` registration | Python `json.dumps` in HTTP client or generated SDK canonical serializer | A″-02 closed | UTF-8、Unicode、nested object、invalid non-object request; no UDS payload encoder consumer |
| A″-12 | `daemon_query::protocol_decode_payload` | `daemon_query.rs::protocol_decode_payload`; `lib.rs` registration | `HttpDaemonRpcClient._handle_response` JSON decode only | A″-03 closed | malformed UTF-8/JSON/non-object/valid response error map; zero old payload decoder caller |
| A″-13 | `daemon_query::protocol_build_frame` | `daemon_query.rs::protocol_build_frame`; `lib.rs` registration | none in HTTP; retire export while retaining private Rust helper only if server still uses it | A″-01 closed；no remaining legacy Python IPC business client | four-byte frame use-sites zero; no HTTP client frames request; unit proves any retained Rust internal helper unchanged |
| A″-14 | `daemon_query::protocol_parse_header` | `daemon_query.rs::protocol_parse_header`; `lib.rs` registration | none in HTTP; retire external export only | A″-13 closed | all Python users zero; header invalid/short tests stay at Rust internal protocol test layer |
| A″-15 | `daemon_query::protocol_validate_message_size` | `daemon_query.rs::protocol_validate_message_size`; `lib.rs` registration | `HttpDaemonRpcClient` HTTP 8 MiB limit and server 413 behavior; no UDS constant fallback | A″-10 closed；A″-13 closed | size 0/8MiB/8MiB+1/HTTP 413 matrix; no Python PyO3 size validator user |
| A″-16 | `daemon_query::protocol_parse_response` | `daemon_query.rs::protocol_parse_response`; `lib.rs` registration | HTTP JSON-RPC result/error mapping only | A″-03 closed | structured remote error stable mapping; no `ok/result` legacy response parser caller |
| A″-17 | `daemon_query::protocol_make_ok_response` | `daemon_query.rs::protocol_make_ok_response`; `lib.rs` registration | daemon `http_server.rs` response emitter, not Python client; external PyO3 export retire/deprecate only | A″-16 closed | HTTP success envelope is daemon-produced; source audit no Python response-builder; protocol Rust unit remains |
| A″-18 | `daemon_query::protocol_make_error_response` | `daemon_query.rs::protocol_make_error_response`; `lib.rs` registration | daemon `http_server.rs` error emitter, not Python client; external PyO3 export retire/deprecate only | A″-16 and A″-17 closed | malformed method/params/error envelope golden; no Python response-builder; errors cannot be locally synthesized as authority response |

A″-13 to A″-15 must preserve daemon-internal framing code where legacy local transport continues to exist. The task removes a **Python-exposed export/caller**, not the Rust IPC primitive. Deleting the server protocol or UDS/named-pipe transport belongs to a future, separately reviewed transport retirement Epic.

---

## 7. Peer credential metadata：每个 export 一张卡

These two exports are neither database access nor an HTTP client. They expose local platform information. The expected G0 outcome is usually `requires_separate_authority_contract` or `retire_after_zero_callers`, not a hasty HTTP migration. The two cards exist only if G0 proves an external client-facing Python dependency.

| 小卡 | 唯一 PyO3 export | 精确 Rust 位置 | 唯一 successor / scope | 直接依赖 | 专属验证 |
|---|---|---|---|---|---|
| A″-19 | `daemon_query::peercred_is_available` | `daemon_query.rs::peercred_is_available`; `lib.rs` registration | versioned daemon capability metadata, or retire if only obsolete diagnostic call sites remain | G0 applied；not `retain_local_core`；future transport auth contract must be referenced | Windows false/Unix true parity if retained; client cannot infer/claim peer identity from response; caller zero or capability test |
| A″-20 | `daemon_query::peercred_info` | `daemon_query.rs::peercred_info`; `lib.rs` registration | read-only `/capabilities` transport-profile field, never an authorization input | A″-19 closed | platform/method metadata accuracy; request body cannot override UID/SID; no PyO3 query caller |

A″-19/20 cannot move actual `SO_PEERCRED` / `LOCAL_PEERCRED` / named-pipe SID extraction into a HTTP body. The primitive remains server-side. A later transport/auth Epic decides if and how to expose non-authoritative diagnostics.

---

## 8. Dispatch metadata：每个 export 一张卡

These are duplicate local metadata views of Rust dispatch. They must converge to a daemon-owned, versioned capability view, without making Python the owner of an allowlist.

| 小卡 | 唯一 PyO3 export | 精确 Rust 位置 | 唯一 successor / scope | 直接依赖 | 专属验证 |
|---|---|---|---|---|---|
| A″-21 | `daemon_query::dispatch_list_methods` | `daemon_query.rs::dispatch_list_methods`; static `METHODS`; `lib.rs` registration | daemon `/capabilities` or `/v1/meta/tools` canonical registry | G0 applied；capability route exists | every advertised method maps to dispatch registry; PyO3 list caller zero; no static Python authoritative list |
| A″-22 | `daemon_query::dispatch_list_error_codes` | `daemon_query.rs`; `lib.rs` registration | daemon-owned error catalog/capability metadata | A″-21 closed | unknown/removed code contract; HTTP error data code matches catalog; no client-side error authority |
| A″-23 | `daemon_query::dispatch_is_admin_method` | `daemon_query.rs`; `ADMIN_ONLY_METHODS`; `lib.rs` registration | server-authoritative capability/property view; client display only | A″-21 closed | admin decision still made inside dispatch; caller cannot use metadata to bypass authorization; no PyO3 predicate caller |

---

## 9. Authority, path, health 与 budget：每个 export 一张条件卡

这一组是最容易被误迁移的部分。它们有些是 local pure calculation，有些是 authorization decision support；没有 G0 的 call graph 和 a security owner review，不得假设“HTTP 化”正确。每张卡都要求 **client no longer takes the final policy decision**；但不会让客户端把 local OS facts 当作 network credentials。

| 小卡 | 唯一 PyO3 export | 精确 Rust 位置 | 可能的单一收敛目的 | 直接依赖 | 专属验证 |
|---|---|---|---|---|---|
| A″-24 | `daemon_query::validate_owned_path` | `daemon_query.rs`; `lib.rs` registration | daemon-internal path validation; client gets structured error only | G0 marks client-authority caller；path-policy contract available | path traversal/symlink/outside-root denied server-side; no Python final allow decision |
| A″-25 | `daemon_query::check_path_within_workspace` | `daemon_query.rs`; `lib.rs` registration | daemon-internal workspace-root confinement | A″-24 closed if same caller chain | `..`, casing, symlink, cross-workspace denial; no PyO3 caller |
| A″-26 | `daemon_query::is_admin_uid` | `daemon_query.rs`; `lib.rs` registration | dispatch/auth internal decision; client only sees allowed/denied | G0 + separate auth contract approves | body-supplied UID ignored; non-admin fails; no HTTP client makes admin decision |
| A″-27 | `daemon_query::current_daemon_uid_py` | `daemon_query.rs`; `lib.rs` registration | non-authoritative daemon diagnostic field or retirement | A″-26 closed | diagnostics do not grant role/access; no local UID-based fallback |
| A″-28 | `daemon_query::check_workspace_owner` | `daemon_query.rs`; `lib.rs` registration | workspace authorization inside Rust handler | A″-24/A″-25 closed；workspace auth contract | cross-owner/workspace access fails before data response; client cannot inject owner field |
| A″-29 | `daemon_query::health_check_all` | `daemon_query.rs`; `lib.rs` registration | daemon `/health` canonical response; client only displays/parses | A″-21 closed | HTTP health success/unavailable/stale-manifest; no PyO3 health caller; no synthetic healthy state |
| A″-30 | `daemon_query::budget_create` | `daemon_query.rs`; `lib.rs` registration | **usually retain local core**; implementation card only if G0 proves a client authority use | G0 marks eligible + budget contract | deterministic budget result; server retains final resource enforcement; no client bypass |
| A″-31 | `daemon_query::budget_preset` | `daemon_query.rs`; `lib.rs` registration | **usually retain local core** or server capability metadata | G0 marks eligible | preset values versioned; client input cannot exceed server budget |
| A″-32 | `daemon_query::budget_tracker_new` | `daemon_query.rs`; `lib.rs` registration | **normally retain daemon-internal/local core**; no HTTP object proxy | G0 eligible only | object lifecycle not recreated in Python; server backpressure invariant remains |
| A″-33 | `daemon_query::budget_tracker_visit_node` | `daemon_query.rs`; `lib.rs` registration | **normally retain daemon-internal/local core** | A″-32 closed if eligible | visit count/budget exceed behavior remains Rust internal; no client-side enforcement claim |
| A″-34 | `daemon_query::budget_tracker_truncate_results` | `daemon_query.rs`; `lib.rs` registration | **normally retain daemon-internal/local core** | A″-32/A″-33 closed if eligible | truncation/budget limit server-side; output metadata is non-authoritative provenance |

A″-30…34 have a deliberately high likelihood of **not being created**. The current task family is “PyO3 database / daemon transport / authority client surface”, not “remote every Rust pure helper”. If G0 labels one `retain_local_core`, the correct result is a documented non-task disposition—this prevents busywork that degrades latency and increases API surface.

---

## 10. Artifact Gate 与收尾卡

### A″-G1 [Gate/artifact_boundary]

**标题**：`A″-G1 [Gate/artifact_boundary]：snapshot.publish 的 FD/memfd 语义与 HTTP artifact successor 契约`  
**唯一范围**：为 `build_publish_params_py` / `snapshot.publish` 冻结 “artifact-id + staged upload + daemon recomputed hash + cancel/retry/cleanup” 契约；不实现 upload、不删 FD。  
**原因**：现有 UDS transport 对大载荷可使用 sealed memfd + `SCM_RIGHTS`，而普通 HTTP RPC body 有 8 MiB cap，二者不等价。[3]

G1 通过且 `applied` 后，A″-09/A″-35 才有资格。G1 的 failure matrix：oversize、hash mismatch、wrong workspace, cancellation、partial upload cleanup、retry with same id、unexpected FD/legacy client fallback。G1 如果 BLOCKED，所有 artifact/client removal work stays blocked; no workaround by increasing JSON limit or passing `db_path` over HTTP.

### A″-35

**标题**：`A″-35 [client_boundary/artifact]：build_publish_params_py → artifact-id-only HTTP snapshot.publish 参数收敛`  
**唯一 export**：`daemon::client::build_publish_params_py`。  
**Rust target**：`rust_ext/src/daemon/client.rs` and `lib.rs` registration; successor handler/API defined by G1.  
**Python target**：`server/daemon_client.py::_ensure_remote_snapshot` and only G1-listed caller(s).  
**Pass**：client no longer opens/passes DB path or FD; daemon accepts only verified artifact id; no direct CAS write; full G1 failure matrix passes.  
**Dependency**：A″-G1 `applied` + A″-09 `closed`.

### A″-36

**标题**：`A″-36 [Gate/end_state]：Python HTTP-only client boundary / zero-callers / no-fallback 验证`  
**范围**：不删除额外 API；只复核所有 completed A″ cards, `callwarden_core` imports, Python database imports/connection patterns, HTTP capability parity and daemon-unavailable denial behavior。  
**Pass**：no unapproved PyO3 daemon/authority caller, no `sqlite3.connect`/`CodeGraphDB` in CLI/MCP client paths, no legacy IPC fallback for completed scope; 128 `retain_local_core` entries have explicit rationale; live authority proof must be current, not stale source build.  
**Dependency**：所有被创建的 A″-01…35 都 `closed`。

### A″-37

**标题**：`A″-37 [release_compatibility]：PyO3 ABI deprecation/removal disposition 与 rollback contract`  
**范围**：对已由 A″-36 验证零 caller 的 export 制定 versioned deprecation/removal policy, release note, feature flag/rollback window; no broad source cleanup beyond the approved export list.  
**Pass**：external plugin/script consumer impact assessed; removal is per export; retained internal Rust primitives remain; rollback points documented; independent reviewer accepts compatibility evidence.  
**Dependency**：A″-36 applied。

---

## 11. 每张小卡的完整 task description 模板

以下模板应由 planner 在 G0 后，**按一张一张**填入真正的 symbol、caller 和 successor。它避免 card 标题正确而 scope 不可执行的问题。

```markdown
# A″-NN [client_boundary]：<exact PyO3 export> → <single HTTP successor or retirement>

**父任务**：A″（由 daemon 创建后返回的真实 task id）  
**port_type**：`client_boundary`  
**port_key**：`callwarden_core::<exact symbol>`  
**gate**：false  
**execution_dependency**：A″-G0 已 `applied`；`pyo3_surface_manifest_v1` 中此 symbol 的 disposition 为 `<allowed>`；`<direct predecessor>` 已 `closed`。

## 唯一范围

- **PyO3 export**：`<exact Rust symbol>`，只改 `rust_ext/src/lib.rs` 中该 symbol 的 `wrap_pyfunction!` registration，以及 `<exact module>.rs` 中该 export 的 external ABI boundary。
- **Python callers**：仅 `<G0 manifest exact files/functions>`；不得碰任何未列 caller。
- **HTTP successor**：`<exact HTTP method/endpoint + request/response contract>`。
- **Rust authority**：`<exact handler/internal module>`；业务/authorization/DB logic 不得移回 Python。
- **测试**：`<one unit file>` + `<one HTTP/client fixture>` + `<AST/import zero-caller scan>`。

## 禁止

不得修改第二个 export、MCP/CLI business handler、schema/db、task governance、role-worker/lease/fencing、old S3、runtime/current、release/deploy scripts。daemon unavailable must fail closed; no local SQLite, legacy IPC, direct CAS or credential fallback.

## 验收

1. G0 已登记的 caller 全部迁移、retired 或具有明确 retained rationale；无未解释 caller。
2. HTTP success、malformed input、daemon unavailable、restart/request-id behavior（若有 mutation）均通过。
3. Python client paths 无 DB/SQL/CAS/direct legacy transport fallback。
4. live runtime 未被本卡替换；evidence 区分 source test 与 live proof。

## Handoff

Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 只读复现该唯一 export 的 import/use-site audit、HTTP parity/no-fallback tests 和 source scope；确认未跨越到第二个 PyO3 export 或 live deployment。
  reason: 本卡只收敛一个 client-boundary API；通过后是否 release direct successor 仍由 parent successor rule 决定。
  independence_requirement: required
```

## 12. 任务树的最终治理规则

1. **一张小卡只允许一个 export。** 例外只有 `build_request_py` 与其 HTTP response counterpart 不可同时删除时的顺序依赖；它们仍是两张卡，不同 executor work item。
2. **G0 是唯一入口，G1 是 artifact 唯一入口。** 没有 Gate `applied`，不得预建所有 34/37 卡；不得以文本 PASS、chat handoff 或临时 token 代替。
3. **A″ 是 A′ 的 successor phase，而非平行抢跑。** A′ 仍有 156 个 `review` descendant 和 1 个 `in_progress` descendant；只有 A′ 真正 closed 和 58 项 Python compatibility 清零后，才能创建 A″。 [4]
4. **P0-K 不可绕过。** 它仍是当前 role-worker governance mutation 修复与 live authority convergence 的前置；A″ 没有权限补它、替它 deployment 或改它的 review result。
5. **旧 S3 不重开。** `S3 PyO3 直调清理 + db/ 目录下线` 保留 append-only historical record；其 future disposition 是另一项治理动作，不作为 A″ executor 的隐式权限。

## References

[1] [`pyo3_authority_surface_inventory_20260827.json`](pyo3_authority_surface_inventory_20260827.json)：162 项 `wrap_pyfunction!` export 的静态分类结果。  
[2] [`rust_ext/src/daemon_query.rs`](../../rust_ext/src/daemon_query.rs)：legacy UDS framing、peercred、dispatch metadata 和 PyO3 boundary 的当前定义。  
[3] [`server/ipc_transport.py`](../../server/ipc_transport.py)：large payload 的 sealed memfd/SCM_RIGHTS 与 platform fallback 语义。  
[4] [`candidate_task_tree_summary_20260827.json`](candidate_task_tree_summary_20260827.json)：A′ task tree 当前只读状态汇总。  
[5] [`aprime2_pyo3_daemon_transport_convergence_task_draft_20260827.md`](aprime2_pyo3_daemon_transport_convergence_task_draft_20260827.md)：A″ parent、前置门禁与 scope boundary 原草案。
