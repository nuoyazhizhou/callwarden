# HTTP Daemon MVP Compatibility Contract

> 状态：Transport Profile v1 / Protocol v1 已冻结，待实现与独立复审
> 任务：`T-1786590214634-9e740cdc`
> 范围：统一本机 MCP/CLI 到 Rust daemon 的 HTTP/JSON-RPC 传输，并保持未迁移能力可用
> 前置基线：`T-1786590722456-db00d074`（237 个工具统一入口可用性）

## 1. 目标

HTTP MVP 不负责先解决全部业务缺陷。开始 H0 前，前置 Legacy Baseline 必须先完成：237 个工具均有明确的 backend（`rust_native`、`python_compat` 或 `unsupported`），并且所有声明可用的工具都能通过当前统一入口执行。这个基线不要求工具已经 Rust 化。

M2.1-M2.5 是 legacy transport 下的查询迁移证据，不能被改写或作为 HTTP 验收替代。当前 M2.5 已由 Legacy Baseline B3 收口；HTTP MVP 只复用其业务 handler 和公开 method 契约。

第一版只解决传输和自举问题：

1. MCP、CLI、Agent 都通过同一个 HTTP/JSON-RPC endpoint 访问 daemon。
2. Rust `cw-daemon` 成为唯一对外服务入口和 RPC dispatch owner。
3. 未迁移到 Rust 的 Python 能力由 daemon 管理的 compatibility worker 执行。
4. 客户端不再打开 SQLite、WAL、CAS 或 snapshot 数据库。
5. 现有 Rust query handler 可以复用，不因 transport 切换而重写业务逻辑。
6. 后续可以按 capability 将 `python_compat` 逐个切换为 `rust_native`。

## 2. 明确不在 MVP 内

MVP 暂不实现：

- token、证书、TLS、mTLS、远程授权；
- 局域网或公网监听；
- HTTP 文件上传、FD 传递和 streaming；
- 全量 237 个 MCP 工具的 Rust 重写；
- 删除 Named Pipe、UDS 或 bridge；
- 改写已关闭的 M0/M1/M2 任务或历史证据。

无认证模式只允许开发机 loopback：daemon 必须绑定 `127.0.0.1`，不得绑定 `0.0.0.0`、局域网地址或公网地址。任何显式非 loopback endpoint 在 MVP 中都应 fail-closed，并返回 `E_HTTP_MVP_LOOPBACK_ONLY`。

这是一个显式的开发期传输例外，不得静默与现有 Requirements 14.20 的 TCP/HTTPS 禁止规则并存。H0 必须通过一个版本化的 MVP Transport Profile 说明该例外仅覆盖 `dev_loopback_unauthenticated`，更新受影响的 requirements/design/IPC 文档及负向测试；远程、跨用户、企业和发布配置仍维持原 Named Pipe/UDS 安全约束，直到认证阶段另行批准。

### 2.1 MVP Transport Profile v1

冻结标识为 `http-mvp-transport-profile/v1`，唯一 profile 名称为
`dev_loopback_unauthenticated`。它是显式 opt-in 的开发期 overlay，不改变默认
`Named Pipe (Windows) / UDS (Linux, macOS)` Daemon_Endpoint：

> 注：H6 迁移期对「启用默认值」有临时覆盖，见 §2.2（仅放宽默认启用；loopback-only、
> fail-closed、manifest 校验等安全实现不变，迁移完成后恢复本节冻结语义）。

| 项目 | 冻结决定 |
| --- | --- |
| 启用 | 仅显式 `CW_DAEMON_TRANSPORT=http` 或等价开发配置；不得作为 release/enterprise 默认值 |
| bind | 仅 IP loopback `127.0.0.0/8` 或 `::1`，默认 `127.0.0.1:0` 动态端口；hostname 必须先解析且全部结果均为 loopback |
| 禁止 | `0.0.0.0`、`::`、LAN、远程地址、端口转发、代理暴露、容器 host publish |
| 身份 | 无 OS Peer_Credential；adapter 只能使用标记为 `synthetic_local_owner` 的本地开发身份 |
| 安全声明 | 不提供生产安全、跨用户 ACL、独立 Reviewer 身份证明、远程授权、token/TLS/mTLS |
| fallback | 一旦选择 HTTP profile，client 对 read/index/governance 全部 fail closed，均不得回退本地 SQLite、Named Pipe 或 UDS |
| 生命周期 | 只对 Protocol v1 有效；任何扩大地址、身份或部署范围的变更必须发布新 profile/version 并重新审查 requirements |

本 profile 是 Requirements 14.20 “无监听 TCP”的唯一版本化例外；它不豁免
Requirements 14.5/14.9 的 Peer_Credential 与跨用户授权语义，也不把 HTTP 请求产生的
synthetic identity 当作 Attestation 或 Independent_Review 证明。未显式选择本 profile 时，
Requirements 14.2/14.3/14.18–14.21 原样生效。

### 2.2 迁移期临时例外（H6，2026-08-15）

> 状态：迁移期临时覆盖，随 H6（`T-1786785215323-16b46c99`）生效；**非** release/enterprise 默认。
> 本小节只放宽 §2.1 的「启用」默认值，不修改 loopback-only / fail-closed / manifest 校验等既有安全实现。

**用户决策**：“我们在做迁移，并不是发布，安全性全部迁移完成后再考虑。”——迁移期
允许将 `dev_loopback_unauthenticated` 作为**默认** transport（未显式指定
`CW_DAEMON_TRANSPORT` 时 HTTP 启用），提前打通 Rust daemon 唯一入口与客户端迁移路径；
token/TLS/mTLS、跨用户 ACL 等安全能力留到迁移完成后统一补齐。

| 项目 | 迁移期临时决定（H6） | 与冻结 §2.1 的关系 |
| --- | --- | --- |
| 启用 | 未显式指定 `CW_DAEMON_TRANSPORT`（或为 `http`/`auto`）时默认启用 HTTP（loopback 动态端口 + manifest）；显式 `named-pipe`/`uds`/`windows-bridge`/`cli-bridge` 回落旧通道 | 临时覆盖 §2.1「不得作为 release/enterprise 默认值」；§2.1 冻结语义保留，安全迁移完成后恢复显式 opt-in |
| bind / loopback-only | 不变：仅 `127.0.0.1`/`::1`，非 loopback 绑定前 fail-closed 退出、不发布 manifest | 不改动 |
| manifest 校验 / authority / fail-closed | 不变：client 联网前校验 manifest hash / protocol / security_profile / authority / PID；失败不回退 SQLite / Named Pipe / UDS | 不改动 |
| 客户端默认 | `is_http_transport_enabled()` 未显式设置时返回 True；`get_daemon_client()` 默认返回 `HttpDaemonRpcClient` | 不改动 client 侧既有安全校验 |
| 期限 | 仅限迁移期；完成认证/TLS 等安全能力并评审后，恢复 §2.1 的显式 opt-in 语义或发布新 profile | — |

## 3. 分层架构

```text
MCP / CLI / Agent
        |
        | HTTP JSON-RPC client
        v
Rust cw-daemon HTTP server
        |
        +-- rust_native dispatch handlers
        |
        +-- python_compat adapter -> daemon-managed Python worker
        |
        +-- SQLite / CAS / Snapshot / Task / Lease
```

### 3.1 Rust daemon

Rust daemon 负责：

- HTTP listener 生命周期；
- JSON-RPC envelope 解析和响应；
- request id、超时和 body size 基本校验；
- capability registry；
- workspace、snapshot、task、lease 的权威边界；
- 将已迁移方法路由到 Rust handler；
- 将兼容方法路由到 Python worker；
- daemon 进程内的 mutation serialization point。

由于 MVP 没有请求方认证，HTTP adapter 只能在 `security_profile=dev_loopback_unauthenticated` 下为现有 dispatch 构造“daemon owner”本地身份。它不得声称提供跨用户 ACL、独立 Reviewer 身份证明或远程授权；这些操作在后续认证 slice 中重新收紧。

### 3.2 Python client

`server/daemon_client.py` 和 CLI 只负责：

- endpoint 发现和连接；
- 参数适配；
- JSON-RPC response/error 转换；
- bounded timeout 和 daemon unavailable 错误；
- 不包含业务 SQL，不创建 SQLite fallback。

### 3.3 Python compatibility worker

compatibility worker 是过渡实现，不是对外 endpoint：

- 由 daemon 启动、停止和探测；
- 使用 Python 3.14（Windows）或独立 WSL/Linux Python 环境；
- 不接受 MCP/CLI 直接连接；
- 通过 daemon 内部 adapter 接收带 method、params 和已验证 workspace context 的调用；
- worker 与 daemon 只使用 daemon 创建的 child `stdin/stdout` 私有 IPC；不得使用“等价”外部 socket，也不得额外暴露 TCP/HTTP 监听端口；
- 帧格式为 4-byte big-endian payload length + UTF-8 JSON object，单帧上限 8 MiB；stdout 只输出协议帧，日志只写 stderr；
- 每帧必须包含 `worker_protocol_version=1`、`request_id`、`method`、`params`、`workspace_instance_id`、`workspace_id`、`operation_class` 和 deadline；不得包含 `db_path`；
- daemon 在发帧前验证并注入 workspace context。worker 只能使用该显式 context，通过 authority 配置解析用户级数据库，不得接受客户端或 frame 传入的 DB path，不得查询或选择 active workspace；
- worker 的 DB 写操作必须经过 daemon 的兼容写锁，不能与 Rust mutation 并发写同一数据库；
- 每个 compatibility method 必须声明 `read_only`、`index_write` 或 `governance_write`；MVP 中 compatibility worker 禁止执行 `governance_write`；
- worker 不得执行 task/lease 的权威治理写入，除非明确调用 Rust daemon 内部接口；
- worker 启动/崩溃返回 `E_COMPAT_WORKER_UNAVAILABLE`，协议损坏返回 `E_COMPAT_WORKER_PROTOCOL`，deadline 超时先终止当前 worker、再返回 `E_COMPAT_WORKER_TIMEOUT`；三者都保留 request_id、可重试标记和恢复指引，不得偷偷切换客户端本地 SQLite。

## 4. HTTP endpoint

### 4.1 MVP endpoint

H1 的 Rust HTTP 栈冻结为 `axum 0.8.x` + `hyper 1.x` + `tokio 1.x`；具体 patch
版本必须由实现时的 `rust_ext/Cargo.lock` 固定并记录。Protocol v1 只允许 HTTP/1.1，
不启用 HTTP/2、WebSocket、SSE、压缩请求体或 streaming。

daemon 默认以动态 loopback 端口启动：

```text
http://127.0.0.1:0
```

daemon 必须原子写入 authority-scoped manifest。客户端发现优先级固定为：显式
`CW_DAEMON_HTTP_ENDPOINT`（仍须验证 loopback/profile）> 显式 authority-scoped manifest
路径 > 当前 authority 的默认 manifest；禁止端口扫描或回退其他 transport。固定端口仅作为
显式开发调试配置，不能是默认值。

manifest schema 固定为 `callwarden-http-manifest/v1`，至少包含：

`manifest_version`、`manifest_id`（每次启动随机）、`authority_id`、`endpoint`、
`pid`、`process_start_time`、`daemon_executable`、`daemon_binary_sha256`、
`protocol_version`、`supported_protocol_versions`、`security_profile`、
`git_commit`、`schema_version`、`started_at`、`capability_registry_revision`、
`worker_status`、`manifest_hash`。

`manifest_hash` 是排除自身后按 UTF-8、按 key 排序、无多余空白的 canonical JSON
计算的 SHA-256。daemon 必须在同目录创建 owner-only 临时文件，flush/fsync 后原子 replace；
Windows ACL 仅 owner SID，Unix mode `0600`。client 在联网前依次验证：文件权限/owner、hash、
authority、profile、协议交集、endpoint loopback、PID 存活、process start time 与 executable
匹配，再调用 `/health` 核对 manifest_id、PID、endpoint、Git、schema 和 registry revision。
任一不符返回 `E_HTTP_MANIFEST_STALE` 或更具体的结构化错误，不连接、不删除 manifest、
不自动启动未知 binary。

固定接口：

```text
GET  /health
GET  /capabilities
POST /v1/rpc
POST /v1/jobs
GET  /v1/jobs/{job_id}
POST /v1/jobs/{job_id}/cancel
```

MVP 中长任务先允许 job API；同步 RPC 不得无限阻塞 HTTP 请求。job 的底层执行可先由 compatibility worker 承担，Rust 原生 job runner 后续替换。

### 4.2 JSON-RPC request

```json
{
  "jsonrpc": "2.0",
  "id": "request-id",
  "protocol_version": "1",
  "method": "query.file",
  "params": {
    "workspace_instance_id": "workspace-id",
    "file_path": "src/example.py"
  }
}
```

要求：

- `jsonrpc` 必须严格等于 `"2.0"`；notification 和 batch request 在 MVP 中禁用；
- `id` 必须是 1–128 字节的非空 UTF-8 string，并与 `request_id` 同义；一次 client retry 中必须保持不变；
- `protocol_version` 必须为 string `"1"`；不兼容返回 `E_PROTOCOL_VERSION_UNSUPPORTED`；
- `method` 必须是 capability registry 中的 method；
- `params` 必须是 JSON object；
- `Content-Type` 必须为 `application/json`；
- MVP body 上限为 8 MiB，以收到的原始 body bytes 计；超出返回 `E_REQUEST_TOO_LARGE`；
- `params.deadline_ms` 可选，范围 1–120000；缺省同步 timeout 为 30000 ms。server 采用 client deadline 与服务端上限的较小者；
- client disconnect/cancel 对 read-only 是 best-effort cancel；mutation 一旦进入串行化点可能继续完成，client 必须凭同一 request_id 查询/重放结果，不能把断连解释为失败；
- 预计超过同步上限或声明 `supports_jobs=true` 的长任务必须走 job API。

对 mutation，daemon 必须以 `(workspace_instance_id, method, request_id)` 持久化去重，
并绑定 canonical params hash。完全相同的重放返回原结果；同 key 不同 params 返回
`E_REQUEST_ID_REUSE_MISMATCH`；记录必须跨 daemon restart 保留至少 24 小时，job 则保留到
终态后至少 24 小时。连接中断时 client 先查询结果或安全重放同一 request id，禁止生成
新 id 后盲目重试。HTTP transport 不得降低现有 task/lease fencing 或状态机门禁。

### 4.3 response/error

成功：

```json
{
  "jsonrpc": "2.0",
  "id": "request-id",
  "result": {},
  "server": {
    "protocol_version": "1",
    "git_commit": "...",
    "schema_version": 50
  }
}
```

失败：

```json
{
  "jsonrpc": "2.0",
  "id": "request-id",
  "error": {
    "code": -32000,
    "message": "snapshot is not ready",
    "data": {
      "code": "E_SNAPSHOT_NOT_READY",
      "message_key": "snapshot_not_ready",
      "retryable": false,
      "recovery": "publish a snapshot before querying",
      "request_id": "request-id"
    }
  }
}
```

JSON-RPC 2.0 标准错误码固定为 parse `-32700`、invalid request `-32600`、method
not found `-32601`、invalid params `-32602`、internal error `-32603`；Call Warden
稳定 `E_*` code 固定放在 `error.data.code`，不得用字符串替代标准整数 `error.code`。

HTTP status 映射固定如下；任何带有效 JSON-RPC id 的业务拒绝都返回 200 并保留 error
envelope，不能包装成连接失败：

| HTTP | 场景 |
| --- | --- |
| 200 | RPC success 或业务/dispatch JSON-RPC error |
| 202 | job 已接受，返回 job_id 与 status URL |
| 400 | malformed JSON / invalid JSON-RPC request（id 不可恢复时为 null） |
| 404 / 405 | HTTP route 或 verb 不存在，不用于 method-not-found |
| 413 | raw body 超过 8 MiB |
| 415 | Content-Type 非 application/json |
| 426 | Protocol v1 无交集 |
| 429 | 有界 queue/job capacity 已满 |
| 503 | daemon/compat worker 暂时 unavailable，且尚未进入业务 handler |
| 504 | transport deadline 在 handler 接受前到期 |

### 4.4 Job 与取消

`POST /v1/jobs` 接受与 RPC 相同的 envelope，返回 202；`GET` 返回
`queued|running|succeeded|failed|cancel_pending|cancelled`，并携带原 request_id、method、
timestamps、progress、result 或完整 structured error。`POST .../cancel` 幂等：排队任务直接
cancelled；运行中 read/index job best-effort；已经提交的 mutation 不回滚并返回
`cancel_pending` 或终态。cancel 自身不得绕过 operation_class、lease/fencing 或去重规则。

## 5. capability registry

`GET /capabilities` 至少返回：

```json
{
  "protocol_version": "1",
  "server_mode": "dev_loopback_unauthenticated",
  "methods": {
    "query.file": {"backend": "rust_native", "status": "available"},
    "get_uncommented_symbols": {"backend": "python_compat", "status": "available"},
    "unknown.method": {"backend": "none", "status": "unsupported"}
  }
}
```

backend 只有三种：

- `rust_native`：Rust daemon 直接执行；
- `python_compat`：daemon 管理的 Python worker 执行；
- `none`：明确不支持。

禁止通过“自动回退本地 SQLite”伪造 `available`。

capability registry 还必须标明 `operation_class`、`workspace_scope`、`supports_jobs`、`deprecated_transport` 和 `security_profile_required`。H4 的 237 工具切换以这份 registry 为唯一切换清单。

Protocol v1 的每个公开 capability row 至少包含：

| 字段 | 约束 |
| --- | --- |
| `method` | 稳定 RPC method，唯一 |
| `mcp_entry` / `cli_entry` | 公开 MCP 名与 CLI 命令；无对应入口时显式 `null` |
| `backend` | `rust_native|python_compat|none` |
| `status` | `available|unsupported|disabled` |
| `operation_class` | `read_only|index_write|governance_write`；mixed entry 必须拆分 component rows |
| `workspace_scope` | `none|authority|workspace|snapshot` |
| `supports_jobs` | boolean |
| `security_profile_required` | MVP 可用项必须为 `dev_loopback_unauthenticated` 或更严格 profile |
| `http_route` | `/v1/rpc` 或 `/v1/jobs`；unsupported 仍给出其拒绝 route |
| `success_fixture` | 可执行 fixture id；不适用时说明原因，不能写 unknown |
| `structured_error_fixture` | 至少一个稳定错误 fixture id |
| `owner` | task id + role + source/evidence shard |
| `deprecated_transport` | legacy transport 状态与退役条件 |

`backend=none` 必须对应 `status=unsupported|disabled`。任何 row 的 backend、route、
operation_class、fixture 或 owner 为 `unknown`/空值时，均不得标为 `available`；registry
聚合与 `/capabilities` 必须 fail closed。

## 6. 自举基线

HTTP MVP 必须先让以下能力通过真实进程级 round-trip：

1. `health`；
2. `capabilities`；
3. workspace register/list/active；
4. task status/read 和 task write 的 daemon 路由；
5. `query.file`、`query.symbol`、`query.grep`、`query.issues`；
6. `stats`、`get_uncommented_symbols`；
7. 至少一个 Python compatibility worker 方法；
8. 一个长任务 job：submit/status/cancel；
9. daemon 重启后 manifest、workspace 和任务状态可恢复；
10. MCP 和 CLI 使用 HTTP client 时不直接打开 SQLite。

11. `/health` 明确返回 `security_profile=dev_loopback_unauthenticated`，并包含 endpoint、PID、Git commit、schema version、worker 状态和 capability registry revision。

## 7. 迁移规则

每个 capability 迁移必须遵循：

```text
compat contract
  -> HTTP route
  -> python_compat 可用
  -> rust_native 实现
  -> 双后端结果对照
  -> 切换 capability registry
  -> 独立 Reviewer
  -> 删除 compatibility 路径
```

客户端 method 名称保持稳定。后端切换不改变 MCP/CLI 用户接口。

## 8. 证据与门禁

实现 Agent 只能推进到 `review`，必须记录：

- Git HEAD、Python 解释器、Rust 版本；
- HTTP endpoint、daemon PID、daemon binary Git commit；
- `/health` 和 `/capabilities` 原始响应；
- 成功、错误、超时、重启恢复的原始日志；
- Python compatibility worker 的启动命令和版本；
- MCP/CLI 不直连 SQLite 的静态检查；
- focused pytest、Rust focused test、`cw --refresh-all`、`py_compile`、`git diff --check`；
- task_steps、task_events、change_audit 和证据文件 hash。

任何 HTTP 服务绑定非 loopback、客户端继续打开 SQLite、worker 绕过 daemon 写锁、使用旧 binary 证明当前源码，或者把 unauthenticated HTTP 说成生产安全，均为 BLOCKED。

## 9. Protocol v1 负向验收矩阵

| 场景 | 期望 |
| --- | --- |
| bind `0.0.0.0` / `::` / LAN / remote / hostname 含非-loopback 解析 | `E_HTTP_MVP_LOOPBACK_ONLY`，listener 未建立，manifest 未发布 |
| malformed JSON / batch / notification / params 非 object | 标准 JSON-RPC error + 冻结 HTTP status，handler 未执行 |
| body > 8 MiB / 非 JSON content type | 413 / 415，handler 未执行 |
| 缺失、hash 错、权限错、PID 重用或 health 不匹配 manifest | `E_HTTP_MANIFEST_*`，不连接、不 fallback |
| authority/profile/protocol 不匹配 | fail closed；426 或结构化 authority/profile error |
| worker unavailable/crash/malformed frame/timeout | 对应 `E_COMPAT_WORKER_*`，无本地 SQLite fallback |
| HTTP client 任意错误时尝试 CodeGraphDB/sqlite/Named Pipe/UDS fallback | 测试失败并 BLOCKED |
| 同 request_id + 同 params mutation | 返回原结果，不重复副作用 |
| 同 request_id + 不同 params | `E_REQUEST_ID_REUSE_MISMATCH`，无副作用 |
| worker 收到 governance_write 或 frame 含 db_path | `E_COMPAT_GOVERNANCE_WRITE_FORBIDDEN` / `E_COMPAT_WORKER_PROTOCOL` |
| job cancel 与 mutation commit 竞态 | 不回滚已提交 mutation；返回可查询终态 |
| registry row 含 unknown 但 status=available | registry 构建/启动失败 |

## 10. H0 冻结的实现入口

H0 只冻结契约和创建任务入口，不交付 HTTP 实现。实际任务关系冻结如下：

| 逻辑节点 | 真实任务 ID | 冻结关系 |
| --- | --- | --- |
| H1 | `T-1786590214634-9e740cdc-sub-2` | H0 closed 后可与 H2 并行 |
| H2 | `T-1786590214634-9e740cdc-sub-3` | H0 closed 后可与 H1 并行 |
| H2I | `T-1786590214634-9e740cdc-h2i` | H1/H2 reviewed 且 closed 后执行；PASS 前禁止 H3 |
| H3 | `T-1786590214634-9e740cdc-sub-4` | 依赖 H2I PASS |
| H4A | `T-1786590214634-9e740cdc-sub-5` | 既有 H4 的正式替代名称；Role Contract `http-mvp-h4a-core-bootstrap/v1` 已冻结 |
| H4B | `T-1786590214634-9e740cdc-h4b` | 依赖 H4A Reviewer PASS 与 Coordinator close；自身仅做 237 工具 cutover 规划/聚合 |
| H5 | `T-1786590214634-9e740cdc-sub-6` | 依赖 H4B 及全部 children 完成 |

H4B 的六个 ownership shard 是
`-native-read`、`-compat-read`、`-index-job`、`-unsupported-error`、
`-registry-docs` 和 `-full-matrix`。每个任务均在 daemon 中冻结了 executor 与
Independent Reviewer Role Contract、实际步骤和 `target_file`；任何后续文档中的简称都
必须解析到本节真实 ID，不得创建第二棵 H2I/H4A/H4B 树。
