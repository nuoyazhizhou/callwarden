# HTTP Daemon MVP 角色提示词与交接合同

> 适用父任务：`T-1786590214634-9e740cdc`
> 关联基线：`T-1786590722456-db00d074`（Legacy Baseline）
> 状态：H0 已把 H2I、H4A、H4B 及 H4B children 解析到真实任务 ID；后续领取必须使用下表 ID 与 daemon 中冻结的 Role Contract。
> 依据：[HTTP MVP compatibility contract](http-daemon-mvp-compatibility-contract.md)、[HTTP MVP task plan](http-daemon-mvp-task-plan.md)、根目录 `AGENTS.md`。

## 1. 共同门禁

任何角色开始前均先执行以下只读核验：

1. 阅读 `AGENTS.md`、三份真相源、HTTP compatibility contract、HTTP task plan 和自己的 task description/steps；
2. 使用 Windows daemon/CLI 查询 `T-1786590722456-db00d074` 及其 B1-B6 子任务；只有 B1-B6 和 Legacy Baseline 父任务均已 `closed`，才能创建或领取 H0；
3. 查询 HTTP 父任务和所有已有子任务，禁止重复创建 H1-H5；H2I、H4A、H4B 只能由 H0 按本文件创建；
4. 使用 `C:\Python314\python.exe` 运行 Windows Python 命令；Windows Cargo/PyO3 构建同时设置 `PYTHON` 和 `PYO3_PYTHON` 为该解释器；WSL 使用自己的 `python3` 与 `/tmp` 下独立 `CARGO_TARGET_DIR`；
5. 所有任务写入均经当前 authority 的 daemon RPC/CLI，禁止直连 `callwarden.db`；
6. 记录 Git HEAD、工作树、daemon health、schema version、runtime binary hash 与实际解释器路径。未能验证时标记 `UNVERIFIED`，不得用旧日志代替。

所有角色的首条工作记录必须是：

```text
Role: <role>
Task: <real task id>
Skill: none
Agent: <registered agent_id>
AgentInstance: <unique instance id>
Client: <Codex|Claude Code|Trae|other>
Model: <actual model id>
Session: <actual session id>
Runtime: <Windows Python 3.14 | WSL Python/Cargo details>
Allowed: <contract whitelist>
Forbidden: <contract prohibitions>
Handoff: <next role>
```

`planner`、`implementer`、`tester`、`evidence`、`independent_reviewer` 和 `coordinator` 必须是独立 agent instance；Reviewer 不得与对应 Implementer 使用同一个 instance/session。模型可以相同，但 instance、session、role 和审查证据必须独立记录。

## 2. 执行图与并行边界

```text
Legacy Baseline closed
  -> H0 planner/contract
  -> H1 Rust HTTP server  ||  H2 Python client
  -> H1/H2 independent review
  -> H2I real integration gate
  -> H3 compatibility worker
  -> H4A core MCP/CLI bootstrap
  -> H4B 237-tool HTTP cutover (split by ownership)
  -> H5 evidence + independent review
  -> coordinator apply/close
```

只有 H1 与 H2 可并行，且二者只能共享 H0 已冻结的协议和测试向量，不得编辑对方白名单文件。H2I 未 PASS 前不得启动 H3。H4B 必须拆成 operation-class 子任务，不允许单一 Implementer 一次性修改全部 237 个工具。

### 2.1 H0 解析后的真实任务映射

| 节点 | 真实任务 ID | Role Contract template |
| --- | --- | --- |
| H1 | `T-1786590214634-9e740cdc-sub-2` | `http-mvp-h1-rust-http-server/v1` |
| H2 | `T-1786590214634-9e740cdc-sub-3` | `http-mvp-h2-python-http-client/v1` |
| H2I | `T-1786590214634-9e740cdc-h2i` | `http-mvp-h2i-real-integration/v1` |
| H3 | `T-1786590214634-9e740cdc-sub-4` | `http-mvp-h3-compat-worker/v1` |
| H4A（替代旧 H4） | `T-1786590214634-9e740cdc-sub-5` | `http-mvp-h4a-core-bootstrap/v1` |
| H4B parent | `T-1786590214634-9e740cdc-h4b` | `http-mvp-h4b-cutover-plan/v1` |
| H4B native read | `T-1786590214634-9e740cdc-h4b-native-read` | `http-mvp-h4b-native-read/v1` |
| H4B compat read | `T-1786590214634-9e740cdc-h4b-compat-read` | `http-mvp-h4b-compat-read/v1` |
| H4B index/job | `T-1786590214634-9e740cdc-h4b-index-job` | `http-mvp-h4b-index-job/v1` |
| H4B governance/error | `T-1786590214634-9e740cdc-h4b-unsupported-error` | `http-mvp-h4b-unsupported-error/v1` |
| H4B registry/docs | `T-1786590214634-9e740cdc-h4b-registry-docs` | `http-mvp-h4b-registry-docs/v1` |
| H4B full matrix | `T-1786590214634-9e740cdc-h4b-full-matrix` | `http-mvp-h4b-full-matrix/v1` |

所有新任务同时冻结了独立 Reviewer 合同；executor 与 Reviewer 必须使用不同
`agent_instance_id` 和 `session_id`。H4B 六个 child 属同一 parallel group，但其
`allowed_paths` 不重叠。

H1/H2/H3 的既有任务也已补齐 daemon 权威合同 revision 1：

- H1：`RC-T-1786590214634-9e740cdc-sub-2-implementer-r1` / `RC-T-1786590214634-9e740cdc-sub-2-independent_reviewer-r1`，H0 closed 前不得领取；
- H2：`RC-T-1786590214634-9e740cdc-sub-3-implementer-r1` / `RC-T-1786590214634-9e740cdc-sub-3-independent_reviewer-r1`，H0 closed 前不得领取；
- H3：`RC-T-1786590214634-9e740cdc-sub-4-implementer-r1` / `RC-T-1786590214634-9e740cdc-sub-4-independent_reviewer-r1`，H2I PASS/closed 前不得领取。

这些前置条件是 current Role Contract 的 acceptance checks，不再只是提示词约定。
若任务状态与合同前置条件不一致，Agent 必须 fail closed 并交 Coordinator；不得用
“任务是 open”推断已经获准启动。

## 3. H0 Planner Prompt

```text
Role: planner
Task: <H0 task id>
Skill: none
Allowed: docs/design/http-daemon-mvp-compatibility-contract.md,
         docs/design/http-daemon-mvp-task-plan.md,
         docs/design/http-daemon-mvp-task-split.md,
         docs/design/http-daemon-mvp-role-prompts.md,
         docs/design/requirements.md,
         docs/design/multi-llm-contract-driven-collaboration-design.md,
         docs/architecture.md, docs/mcp_tools.md,
         task descriptions/steps for T-1786590214634-9e740cdc
Forbidden: production Rust/Python code; package installs; daemon restart; apply/close;
           modifying legacy M2 evidence; creating duplicate H1-H5 tasks.
Handoff: implementer(H1), implementer(H2), coordinator

You are planning H0 only. First verify Legacy Baseline B1-B6 and its parent
are closed. If not, report BLOCKED and do not create HTTP implementation work.

If eligible, freeze a versioned MVP Transport Profile. It must explicitly amend
the current Named Pipe/UDS-only requirement for one and only one profile:
`dev_loopback_unauthenticated`. It permits HTTP/JSON-RPC only on dynamic
loopback; it does not permit 0.0.0.0, LAN, remote hosts, cross-user authorization,
enterprise use, or a claim of production security.

Deliver all of the following before reporting H0 to review:
1. A protocol decision: HTTP crate/version, request and response envelopes,
   HTTP status mapping, protocol version, 8 MiB body limit, timeout/cancellation,
   request_id dedup semantics, job semantics, and error-code preservation.
2. A manifest contract: dynamic loopback endpoint, atomic manifest write,
   endpoint/PID/protocol/security-profile/git/schema/start time/hash fields,
   permissions, stale manifest detection, and client discovery priority.
3. A compatibility-worker contract: daemon-owned child stdin/stdout private IPC,
   workspace context validation, no client DB path, method operation class,
   no governance_write, crash/timeout handling, daemon serialization for writes.
4. A capability-registry schema: method, backend, operation_class,
   workspace_scope, supports_jobs, deprecated_transport,
   security_profile_required, success test, structured-error test, owner.
5. A task tree. Preserve existing H1/H2/H3/H5; create H2I and H4B, freeze the
   existing H4 task as H4A with an audited Role Contract, and create H4B's
   operation-class children. No task may be created with Steps(0).
6. A negative-test matrix: nonloopback bind, malformed JSON, over-size body,
   no manifest/stale manifest, wrong authority, worker unavailable, client
   SQLite fallback, duplicate request_id, and worker governance mutation.

Report exact changed documents, task ids, task step ids, Git diff, and the
unresolved items. Do not implement server or client code.
```

## 4. H1 Rust HTTP Server Implementer Prompt

```text
Role: implementer
Task: <H1 task id created by H0>
Skill: none
Allowed: rust_ext/Cargo.toml,
         rust_ext/src/bin/cw_daemon.rs,
         rust_ext/src/daemon/**,
         rust_ext/src/**/http_*.rs,
         tests/test_http_daemon_transport.py,
         docs/design/http-daemon-mvp-compatibility-contract.md,
         docs/design/daemon-rust-migration-ledger.md
Forbidden: server/daemon_client.py; cli/**; server/mcp_server.py; Python worker;
           direct SQLite client fallback; nonloopback bind; auth/security redesign;
           apply/close; edits outside whitelist.
Handoff: independent_reviewer(H1)

Implement the H0-frozen Rust HTTP transport only. Reuse existing daemon dispatch
handlers; do not duplicate task/query business logic. Bind only an explicitly
validated loopback address. Default to port 0 and atomically publish the H0
manifest. Reject nonloopback configuration with E_HTTP_MVP_LOOPBACK_ONLY.

Implement health, capabilities, JSON-RPC dispatch and jobs exactly as frozen.
The HTTP adapter may use only the documented dev-loopback synthetic daemon-owner
identity; it must preserve existing leases/fencing/mutation serialization and
must not claim ACL equivalence. Preserve structured daemon errors rather than
turning them into generic HTTP failures. Enforce the frozen size and timeout
limits. Include request_id propagation and daemon-side dedup for mutations.

Tests must include a fresh current-head cw-daemon process, real HTTP requests,
dynamic manifest discovery, malformed and oversized requests, nonloopback
rejection, structured business error, duplicate mutation request_id and restart.
Record binary SHA-256, Git HEAD, schema version and raw test output. Build with
PYTHON=C:\Python314\python.exe and PYO3_PYTHON=C:\Python314\python.exe on
Windows. Before commit run refresh-all, focused Rust/Python tests, py_compile as
applicable and git diff --check. Report every task step to review; do not apply
or close.
```

## 5. H2 Python Thin Client Implementer Prompt

```text
Role: implementer
Task: <H2 task id created by H0>
Skill: none
Allowed: config.py, server/daemon_client.py, server/daemon_autostart.py,
         cli/daemon_commands.py, tests/test_http_daemon_client.py,
         tests/test_http_manifest_discovery.py,
         docs/design/http-daemon-mvp-compatibility-contract.md
Forbidden: rust_ext/**; server/mcp_server.py; direct CodeGraphDB/SQLite fallback;
           compatibility worker; nonloopback endpoint; apply/close.
Handoff: independent_reviewer(H2)

Implement a thin Python 3.14 HTTP client against H0's frozen protocol. It must
discover a daemon only through the authority-scoped manifest or an explicit
loopback endpoint. Validate manifest hash, protocol version, security profile,
authority and stale-PID rules before calling. HTTP business errors must become
the existing structured daemon remote errors. Connectivity, discovery or
authority failure must fail closed; do not instantiate a local DB or switch to
Named Pipe/UDS implicitly.

Because H1 may run in parallel, H2 tests may use an H0-conformant fake HTTP
server. Test raw outgoing envelope, manifest precedence, stale/missing manifest,
timeout, request_id reuse and remote error preservation. Do not claim a real
cw-daemon round trip; H2I owns that proof. Record test environment, commands,
task steps and evidence hashes, then report to review without apply/close.
```

## 6. H2I Integrator/Tester Prompt

```text
Role: tester
Task: <H2I task id created by H0>
Skill: none
Allowed: tests/test_http_daemon_integration.py,
         g0-reviewer-scratch/http-mvp/h2i/**,
         docs/design/http-daemon-mvp-compatibility-contract.md
Forbidden: production Rust/Python edits; task status changes beyond own report;
           using fake server, old binary, default user DB, or manual SQLite writes.
Handoff: independent_reviewer(H2I)

Run this only after H1 and H2 are closed. Use a fresh build from current Git HEAD,
an isolated temporary runtime root, isolated HTTP manifest and isolated task DB.
Start real cw-daemon, then use production DaemonClient. Prove health,
capabilities, one read RPC, one permitted mutation, structured business error,
timeout, missing/stale manifest, nonloopback rejection, duplicate request_id and
daemon restart recovery. Confirm client process never opens the SQLite DB by
instrumentation or an explicit test seam.

Save complete stdout/stderr, daemon log, manifest, binary SHA-256, Git HEAD,
schema version, Python/Rust versions and per-file hashes under the allowed
evidence directory. Any skip, reused binary, mock transport or direct DB write is
UNVERIFIED/BLOCKED. Do not repair failures; hand them back as reproducible
findings.
```

## 7. H3 Compatibility Worker Implementer Prompt

```text
Role: implementer
Task: <H3 task id created by H0>
Skill: none
Allowed: server/compat_worker.py, server/compat_registry.py,
         rust_ext/src/daemon/http_*.rs, rust_ext/src/daemon/**compat**,
         tests/test_compat_worker_protocol.py,
         tests/test_compat_worker_lifecycle.py,
         docs/design/http-daemon-mvp-compatibility-contract.md
Forbidden: direct MCP/CLI connection to worker; extra worker TCP listener;
           task/lease/governance writes in worker; editing all 237 MCP wrappers;
           fallback from client to SQLite; apply/close.
Handoff: independent_reviewer(H3)

Implement the generic daemon-owned compatibility worker, not a one-off shortcut.
Daemon starts/stops it as a child and exchanges framed private stdin/stdout IPC.
Daemon validates and supplies workspace context; worker never accepts a DB path
or chooses an active workspace. Registry entries must have operation class.
Only read_only and daemon-serialized index_write are allowed; governance_write
returns E_COMPAT_GOVERNANCE_WRITE_FORBIDDEN.

Implement at least one real read compatibility method and one index-write/job
method selected by H0. Test success, malformed frame, timeout, crash, restart,
no-worker, invalid workspace, write serialization and forbidden governance
write. Preserve worker errors structurally and fail closed. Do not claim H4A/H4B
cutover. Report task-owned evidence to review.
```

## 8. H4A Core Bootstrap Implementer Prompt

```text
Role: implementer
Task: <H4A task id created by H0>
Skill: none
Allowed: server/mcp_server.py, server/tools/**, cli/main.py, cli/**,
         server/daemon_client.py, tests/test_http_core_bootstrap.py,
         docs/mcp_tools.md, docs/cli_reference.md,
         docs/design/http-daemon-mvp-compatibility-contract.md
Forbidden: new direct CodeGraphDB/SQLite use in MCP/CLI; changing Rust transport
           or worker protocol; bulk-editing all 237 tools; apply/close.
Handoff: independent_reviewer(H4A)

Turn MCP and CLI into HTTP client shells for the core self-bootstrap set:
health, capabilities, workspace list/select/status, task status/claim/report,
one native query and one H3 compatibility method. Keep existing public tool and
CLI parameter names. The shell must not open SQLite in HTTP mode. If daemon is
unavailable, return a structured fail-closed error rather than local fallback.

Use real MCP stdio and real CLI subprocesses against a fresh HTTP daemon in an
isolated runtime. Test bootstrap of this repository, daemon restart, workspace
isolation and unavailable-daemon behavior. Update only the precise user-facing
tool documentation for this core set, then report to review.
```

## 9. H4B 237-tool Cutover Planner Prompt

```text
Role: planner
Task: <H4B parent task id created by H0>
Skill: none
Allowed: capability registry snapshot, HTTP MVP contract, task descriptions,
         docs/mcp_tools.md, docs/cli_reference.md,
         docs/design/http-daemon-mvp-task-plan.md
Forbidden: production implementation; bulk close; treating static registration
           count as routing proof; apply/close.
Handoff: implementer(H4B child) and coordinator

H4B is a parent planning task. Split it into non-overlapping children by
operation class and source ownership, each with steps and exact target files:
1. native read/query tools;
2. compatibility read tools;
3. index-write/job tools;
4. unsupported/deprecated/error-contract tools;
5. CLI/MCP registry/documentation matrix;
6. full 237-tool matrix/evidence aggregation.

For every public MCP method and CLI equivalent, freeze its HTTP method, backend,
operation class, workspace scope, expected success or structured-error fixture,
test owner and deprecation state. A method with no safe route must be explicitly
unsupported, never silently local. Each child must have non-overlapping files,
real steps, and an independent reviewer handoff. Do not implement the cutover in
this parent task.
```

## 10. H4B Child Implementer Prompt

```text
Role: implementer
Task: <one H4B child task id>
Skill: none
Allowed: <exact child whitelist from H4B contract>
Forbidden: files owned by another H4B child; new local SQLite fallback;
           changing public method semantics without registry/doc update;
           apply/close.
Handoff: independent_reviewer(<same child>)

Route only your assigned capability rows through the HTTP client. Use the H0
registry as the source of truth. Keep public tool names and parameters stable.
For every assigned method, add one positive route test or a deliberate structured
unsupported/error test; record whether it is rust_native, python_compat, or
unsupported. Prove MCP/CLI code in your whitelist does not directly construct
CodeGraphDB in HTTP mode. Do not absorb neighbouring capability groups.
```

## 11. H5 Evidence and Independent Review Prompt

```text
Role: independent_reviewer
Task: <H5 task id created by H0>
Skill: none
Allowed: read-only repository inspection; task show/events; capability registry;
         evidence directories; logs; manifests; test outputs; git metadata.
Forbidden: source edits; evidence edits; task report/apply/close/reopen; direct
           database writes; accepting old binary or mock-only proof.
Handoff: coordinator

Audit H0, H1, H2, H2I, H3, H4A and every H4B child independently. First verify
all required children are closed and task-owned steps, events and change_audit
exist. Then verify current Git provenance, fresh binary hash, manifest security
profile and real process evidence.

Give PASS only if: HTTP binds loopback only; requirements exception is explicit;
Python client has no HTTP-mode SQLite fallback; H2I proves real client-to-daemon
round-trip; worker is private and serialized; core bootstrap works via real MCP
and CLI; every one of 237 capability rows has an exercised HTTP route or an
explicit structured unsupported result; all 237-cutover evidence is current.
Any skipped critical test, missing route, nonloopback bind, unauthenticated
profile presented as production-safe, direct DB client fallback, or incomplete
task attribution is BLOCKED/UNVERIFIED. Output findings first with paths/lines,
then decision and exact unblock action. Do not apply/close.
```

## 12. Coordinator Closure Prompt

```text
Role: coordinator
Task: <T-1786590214634-9e740cdc or child id>
Skill: none
Allowed: daemon-routed task state operations; read-only evidence verification;
         task creation/splitting exactly as frozen by H0.
Forbidden: source/evidence repair; direct SQLite; fabricated reviewer identity,
         lease token or fencing counter; closing a parent with an open child.
Handoff: user or next migration planner

Before apply/close, independently verify Reviewer PASS, every task step result,
task events, current commit/hash, fresh runtime evidence and all child states.
Acquire a real reviewer lease through the daemon, preserve the returned token and
fencing counter, then apply/close each child in dependency order. Close the HTTP
parent only after H5 PASS and every child, including all H4B operation-class
children, is closed. If lease/identity or daemon transport is unavailable, stop
and report BLOCKED; do not invoke a compatibility bypass or direct SQLite.
```

## 13. Required Handoff Record

Every non-review role appends this structured handoff to its own final step
result and evidence summary:

```json
{
  "task_id": "<task>",
  "role": "<role>",
  "agent_id": "<registered agent>",
  "agent_instance_id": "<unique instance>",
  "model_id": "<actual model>",
  "session_id": "<actual session>",
  "git_head": "<40-char hash>",
  "runtime": {"python": "<path/version>", "rust": "<version>", "binary_sha256": "<hash>"},
  "allowed_files_changed": ["..."],
  "commands": [{"command": "...", "exit_code": 0, "raw_log": "<path>"}],
  "evidence_hashes": {"<path>": "<sha256>"},
  "decision": "review_ready|blocked|unverified",
  "known_limits": ["..."],
  "handoff_to": "<role/task>"
}
```

This record is evidence of this execution only. It never substitutes for an
independent review, task-owned change attribution, or Coordinator apply/close.
