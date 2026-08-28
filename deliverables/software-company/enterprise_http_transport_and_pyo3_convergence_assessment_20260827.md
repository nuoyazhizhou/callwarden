# Enterprise HTTP Transport 与 PyO3 收敛评估

**日期**：2026-08-27  
**性质**：只读架构评估与任务边界草案。未创建任务、未改变 daemon、未访问数据库、未触发部署。  
**前置治理约束**：P0-K 尚待独立 Reviewer 审查；本文件不授予任何实现、受控刷新、`apply` 或 `close` 权限。

## 执行结论

可以把下一阶段的目标定义为：**所有跨进程的业务 RPC 统一使用 HTTP/JSON-RPC 语义，并由 Rust `cw-daemon` 独占业务授权、并发串行化和 SQLite 权威写入。** 但不应把目标表述为“删除 named pipe / UDS / PyO3，所有东西都改为 127.0.0.1 HTTP”。这会错误地把三种不同的边界混在一起：HTTP 是应用协议；UDS/named pipe 是本机可信传输与 OS peer credential 的来源；PyO3 是 Python 进程内调用 Rust 计算的 ABI。

> **推荐决策**：采用“HTTP 语义统一 + 本机可信传输保留”的方案。Unix 上让 HTTP/1.1 运行在 UDS 上，Windows 上让 HTTP/1.1 运行在 `\\.\pipe\callwarden-<user-sid>` 上；在 HTTP request context 中注入由 socket/pipe 接收阶段取得的 `TransportPeerIdentity`。这样业务接口只有一套 HTTP RPC，但 `SO_PEERCRED`、`LOCAL_PEERCRED` 和 named-pipe SID 仍是不可伪造的授权根。[1] [2]

这不是保守地维持“双业务协议”。需要被收敛的是现有长度前缀 JSON IPC 与 HTTP JSON-RPC 的**业务协议、请求校验、错误模型、幂等键、capability metadata 和 client SDK**；UDS/named pipe 仅降级为 HTTP 的本机承载层。当前的 TCP loopback HTTP MVP 不能直接成为 Enterprise 替代，因为它明确合成 `local-owner` peer，而并没有每请求 OS peer credential。[3]

| 决策点 | 推荐结论 | 原因 |
|---|---|---|
| MCP/CLI business RPC | 统一为 HTTP/JSON-RPC | Python 已有 fail-closed HTTP thin client；接口、观测、跨语言 SDK 更一致。[4] |
| Unix UDS 与 Windows named pipe | **保留为本机 HTTP 的传输承载与身份来源** | 其提供 OS 级 uid/gid/SID 与 Windows SDDL 隔离；TCP loopback 没有等价 peer credential。[1] |
| TCP loopback HTTP | 保留为受限开发/诊断 overlay，不能作为 Enterprise authorized transport | 当前 profile 是 `dev_loopback_unauthenticated`，且 dispatch 使用合成 peer。[3] |
| PyO3 transport/client helpers | 逐步由 HTTP client SDK 替代 | 这是 transport-adjacent 薄层，适合去嵌入式化。 |
| PyO3 parsing/watcher/index/compute core | **不迁移到 HTTP**；保留为 Rust library，必要时由 daemon 进程内使用 | 它们不是跨进程 transport，改为 HTTP 会把本地高频计算变成网络调用并扩大故障面。[5] |
| UDS SCM_RIGHTS / memfd 大载荷 | 用 HTTP artifact/upload contract 取代后才可退役 | 普通 8 MiB JSON RPC 不等价于附带 sealed memfd 的零/低拷贝大载荷路径。[6] |

## 一、当前源码事实

### 1. Enterprise IPC 的安全职责

当前 `transport.rs` 对 Unix 和 Windows 定义了统一的 `TransportPeerIdentity`。Unix 身份来自 `SO_PEERCRED` 或 `LOCAL_PEERCRED`，Windows 身份来自 named-pipe 对端访问令牌 SID；客户端自己在 request body 填写的 agent/session 字段不参与该层授权。[1] Windows pipe 名由用户 SID 派生，且 SDDL 只允许 owner SID 连接；Unix socket 使用 owner + `0660`。因此，现有 IPC 不是单纯“旧格式的 RPC”，而是本机授权锚点。

`server.rs` 进一步表明：IPC 连接经历 peer credential 提取后才 dispatch；Unix 还支持 `SCM_RIGHTS` 传递文件描述符。现有 HTTP listener 被定义为与 UDS/named-pipe **并行**运行、共享同一 `SerializationPoint` 的 MVP overlay，而不是后者的安全等价替代。[2]

### 2. 当前 HTTP MVP 的能力与缺口

Rust HTTP server 已提供 `GET /health`、`GET /capabilities`、`POST /v1/rpc`、jobs endpoints、8 MiB body cap、严格 JSON-RPC 封装和跨重启的 mutation dedup store。它还会在 bind 后原子写 manifest。这些均是未来统一协议的可复用基础。[3]

然而，它的安全 profile 明确为 `dev_loopback_unauthenticated`，绑定只允许 loopback。更关键的是，HTTP handler 在 dispatch 前调用 `synthetic_local_owner_peer()`；源码注释也明确写为“无 OS peer cred over HTTP”。因此即便 endpoint 只绑定 `127.0.0.1`，也不能把此 profile 宣称为满足 Enterprise 的 peer-credential 身份模型。[3]

| 当前 HTTP 已有能力 | Enterprise 尚缺的能力 |
|---|---|
| JSON-RPC v2 request/response、strict parser、8 MiB 上限 | 由真实 peer credential 或等价 proof 生成的 request auth context |
| manifest + health/PID/git/schema 交叉核验 | manifest/health 必须带 security-profile version、identity binding epoch、auth capability revision |
| mutation `request_id` dedup | 认证 nonce、重放窗口、principal-bound idempotency key、token/epoch revoke |
| Rust handler 与 shared serialization point | HTTP-over-UDS/named-pipe acceptor，或替代的 PoP authenticator |
| Python HTTP thin client（fail-closed） | named-pipe/UDS HTTP client adapter、capability negotiation、artifact streaming |

### 3. PyO3 不是单一 transport 层

`callwarden_core` 暴露了大量本地 Rust API：树解析、snapshot、watcher、clone detection、impact/vector、CAS/backup、daemon protocol helpers、CLI formatting/config/router 等。仅 daemon client/protocol 相关的导出是其中一小部分；其中还包含 Unix-only `daemon_client_call_py` 与若干 request builder。[5]

因此“迁移 PyO3 到 HTTP”必须拆成三类，不能整体替换。

| PyO3 类别 | 例子 | 处置 |
|---|---|---|
| daemon client / IPC framing | `daemon_client_call_py`、protocol encode/decode、request builders | 替换为生成或手写的 HTTP client adapter；最终移除不再需要的 IPC framing export。 |
| daemon authority helper | peercred availability、dispatch metadata、ACL/budget helpers | 仅把需要远程调用的内容暴露为 daemon `/v1/*` capabilities；低层 peercred 不应被 HTTP client 伪造。 |
| embedded core compute | parser、watcher、vector/graph/clone、CAS/backup 计算 | 留在 Rust library；daemon 使用它们作为进程内 implementation。Python 若确有本地开发工具需求，可继续经 PyO3 调用非 authority pure functions。 |

## 二、三种可行架构与取舍

| 方案 | 描述 | 安全与运行特性 | 实施复杂度 | 结论 |
|---|---|---|---|---|
| A. **HTTP over UDS / named pipe** | 将 HTTP/1.1 connection server 放到 UnixStream / NamedPipeServer；accept 时取得真实 peer credential 并注入 request extensions | 保持 UID/GID/SID、Windows SDDL、Unix FD fast path 的安全属性；业务协议单一 | 中等：需要跨平台 HTTP stream adapter 与 Python client adapter | **推荐的 Enterprise 收敛目标** |
| B. Loopback TCP HTTP + local proof-of-possession | 移除客户端 UDS/pipe；启动时发给 owner ACL/OS-keystore 保护的本地 key，HTTP 请求对 canonical request + nonce + time + manifest epoch 签名 | 可统一使用普通 HTTP client；但不再有每连接 kernel peer credential，需要完整 PoP、重放防护、进程/用户 ACL 测试 | 高：这是新的认证系统，不能仅靠动态端口或 manifest 文件 | 可选的后续产品模式，不是现有 Enterprise IPC 的无损替换 |
| C. 维持现状双协议 | Enterprise writes 继续 length-prefixed IPC；MCP/CLI 继续 loopback HTTP | 最低短期风险，但长期协议、SDK、测试和诊断面持续重复 | 低 | 仅作为过渡，不作为终态 |

### 为什么不建议裸 loopback HTTP

裸 `127.0.0.1:<dynamic-port>` 的 manifest 只能改善发现与误连，不能证明 TCP client 的 Windows SID 或 Unix UID。端口可被其它本机 user/session 连接；动态端口与仅 owner 可读的 manifest 不是认证机制。若选择方案 B，最低要求为：OS-protected private key、每请求签名、method/path/body digest、timestamp 窗口、服务端 nonce replay ledger、manifest epoch/PID bind、key rotation/revoke、同一 user 与跨 user 的负向测试。**不得**借用 provider token、agent session ID、model ID 或 Role Worker raw credential 充当通用 transport secret。

Role Worker credential 仍只应该承担“已登记的稳定 CW-local worker 可否以 executor/reviewer/adjudicator 角色操作某 task”的应用授权，而不能扩大为所有本机 HTTP client 的 bearer token。传输 principal、Role Worker actor 和可变 runtime provenance 必须保持三个独立层次。

## 三、建议的终态：Enterprise HTTP-Local v1

### 1. 分层模型

```text
Python CLI / MCP thin client / Java client
                 │
                 │ HTTP/1.1 + JSON-RPC 2.0
                 ▼
    HTTP local transport adapter (not business logic)
      ├─ Unix: UDS → SO_PEERCRED / LOCAL_PEERCRED
      └─ Windows: Named pipe → SID from pipe token
                 │ inject immutable TransportPeerIdentity
                 ▼
  Rust cw-daemon HTTP router + strict envelope + dedup
                 │
                 ▼
    dispatch / Role Worker / leases / fencing / ledger
                 │
                 ▼
       Rust domain handlers + SQLite authority
```

`TransportPeerIdentity` 必须在 accept stage 创建，成为不可由 body/header 覆盖的 request extension。`dispatch_rpc` 的入参继续使用这个 identity；HTTP headers 只承载 request metadata，绝不承载可自报的 owner/SID/UID。业务 handler 不应该知道请求是 TCP、UDS 还是 named pipe，且所有 transport 最终共用一套 strict envelope、dedup 和 `SerializationPoint`。

### 2. endpoint 与 manifest

建议将 manifest 提升至 `callwarden-http-manifest/v2`，其中只保存非秘密元数据：transport kind、endpoint descriptor、PID、binary hash、schema、git commit、security profile、capability registry revision、identity-binding epoch、started-at。任何 manifest 均不得保存 credential、private key、role worker raw credential 或 provider token。

| transport kind | endpoint 示例 | profile | 允许的能力 |
|---|---|---|---|
| `http+unix` | `~/.callwarden/run/cw.sock` | `enterprise_peercred_v1` | 完整 policy-permitted RPC；真实 UID/GID 进 request context |
| `http+npipe` | `\\.\pipe\callwarden-<user-sid>` | `enterprise_peercred_v1` | 完整 policy-permitted RPC；真实 SID 进 request context |
| `http+tcp-loopback` | `http://127.0.0.1:<ephemeral>` | `dev_loopback_unauthenticated` | 只限现有受限开发/诊断表面；非 Enterprise governance authority |
| `http+tcp-loopback` + PoP | `http://127.0.0.1:<ephemeral>` | `enterprise_local_pop_v1` | 仅在独立 threat model、key lifecycle 与全部负向测试通过后启用 |

### 3. 大载荷与 snapshot

现有 Unix IPC 对大于 16 MiB 的载荷使用 `memfd_create + seals + SCM_RIGHTS`，并限制单连接/全局 inflight bytes。普通 `/v1/rpc` 的 8 MiB JSON 上限不是功能等价物。[6]

应新增**独立的 artifact transfer contract**，而不是放宽 RPC body：例如 `POST /v1/artifacts/uploads` 创建上传、分块 upload、daemon 重算摘要并写入受控 staging、`snapshot.publish` 仅引用 daemon-issued artifact id。Windows/macOS 可用分块 byte stream；Unix 的 sealed memfd 可作为 `http+unix` 的可选传输优化，但不能再由业务层直接依赖。只有 artifact contract 完成 durability、size limits、hash verification、cancel/cleanup 和 recovery tests 后，才能退役 IPC-only FD method。

## 四、分阶段迁移与禁止顺序

以下是**规划草案**，不是现在应创建或执行的任务。依照既有治理约束，必须先完成 P0-K 的独立 review、必要 adjudication 与受控 runtime convergence，之后才可逐卡建立新的独立 ownership。

| 阶段 | 单一交付目标 | allowed scope | 明确禁止 | 通过门槛 |
|---|---|---|---|---|
| E0 | 冻结 Enterprise HTTP-Local v1 contract、threat model、endpoint/manifest v2 | 设计、测试矩阵、capability inventory | 改 transport、修改 live daemon、复用 raw role credential | reviewer 独立 PASS |
| E1 | 抽取 peer-authenticated HTTP request context | `TransportPeerIdentity` extension、shared dispatch adapter、negative tests | TCP synthetic peer 用于 Enterprise | UDS UID/SID mismatch fail；body identity 不能覆盖 OS peer |
| E2 | Unix `http+unix` vertical slice | 一个 read-only RPC 和一个 protected mutation | 退役现有 UDS legacy RPC | same request/response/error/dedup parity；SCM_RIGHTS 不在此卡处理 |
| E3 | Windows `http+npipe` vertical slice | pipe HTTP stream adapter、SID propagation、precreated instance invariant | Windows AF_UNIX、裸 TCP Enterprise bind | foreign SID denied；busy-window/replenish invariant；same serialization |
| E4 | 跨语言 client convergence | Python CLI/MCP thin client 和 Java client 各一个最小 read/write slice | Python SQL fallback、Pyo3 broad removal | no DB import/connection；HTTP transport failure fail-closed |
| E5 | Artifact streaming / snapshot publish | artifact endpoints、hash/retry/cancel/recovery | 将 >8MiB blob 塞入 RPC、直接 client 写 CAS | Unix/Linux/Windows parity；digest mismatch zero commit |
| E6 | PyO3 transport-adjacent retirement | 移除已无调用方的 IPC framing/client exports | parser/watcher/vector/CAS pure/local compute exports | import/use-site zero、ABI compatibility decision、full client contract tests |
| E7 | Legacy IPC business-RPC retirement | 仅在所有 capability parity manifest 为 green 后禁用 length-prefix business method | 删除 peercred primitive、本机 HTTP transport、artifact fast path | versioned migration/rollback + independent review + adjudication |

### 兼容期规则

在 E1–E6，旧 IPC 与 HTTP-local 可共存，但**同一业务 method 必须指定一个 authority implementation**。两条 wire transport 可以共同调用同一 Rust handler；不得维护两套 Python/Rust business implementation，更不得按 daemon 不可用回退到 Python SQLite。

每个 method 的 registry 至少应增加：`protocol_versions`、`transport_profiles`、`authoritative_handler`、`authn_requirement`、`payload_class`、`idempotency_requirement` 与 `legacy_retirement_gate`。调用方只依 capability manifest negotiate，不能根据操作系统或异常消息静默降级。

## 五、PyO3 退役判据

PyO3 的减少应以 **per-export reachability** 为单位，而不是按 crate 或模块一刀切。对于每个候选 export，先记录调用方、是否触碰 SQLite/authority、是否 CPU-bound/同步、是否需要 FD/OS handle、是否被第三方脚本依赖。只有同时满足“跨进程 client 行为”“已存在 HTTP SDK counterpart”“无本地高频/zero-copy 价值”“compat period metrics 为零”时，才进入退役卡。

| 可以优先替换 | 必须保留或另行设计 |
|---|---|
| `daemon_client_call_py`（Unix-only IPC client） | parser、tree-sitter、symbol extraction、graph/vector/clone compute |
| protocol request builders/response parsers | Rust watcher 本地事件采集与 daemon 内部处理 |
| CLI router 中仅用于 IPC endpoint 的 helpers | peer credential extraction 与 OS ACL primitives |
| 对外 daemon metadata helpers（以 `/capabilities` 代替） | CAS/backup/snapshot internals；其远程化需 artifact contract |

## 六、验收测试必须先于迁移

| 范畴 | 必须存在的正向与负向测试 |
|---|---|
| peer identity | Unix UID/GID、macOS local peer credential、Windows foreign SID；客户端 body/header 伪造 UID/SID/agent/session 一律不改变 authorization result |
| transport parity | 同一 method 走 legacy IPC 与 http+unix/http+npipe 时得到相同 handler、error code、dedup/fencing 行为与审计 principal |
| governance | Role Worker role mismatch/revocation、reviewer/adjudicator worker separation、lease/fencing stale、malformed identity policy；均在写前失败且不产生业务写入 |
| manifest | forged/stale PID、wrong binary hash、wrong profile、wrong binding epoch、endpoint not owner-scoped 均 fail-closed |
| payload | >8MiB artifact 分块、digest mismatch、cancel、partial upload cleanup、retry idempotency；Unix fast path 与 cross-platform streamed path 语义一致 |
| performance | local read p50/p95、mutation p95、backpressure、concurrent client mix；与当前 UDS/pipe baseline 比较而不是拍脑袋设阈值 |
| fallback | Enterprise profile 下 HTTP/pipe/UDS unavailable 时无 Python SQLite、无 direct `CodeGraphDB`、无 provider token/placeholder credential fallback |

## 七、对当前工作序列的影响

当前最合理的顺序是：先把 58 个 `python_compat` MCP backend 按逐工具任务迁移到 Rust，完成已启动的 P0-K 治理写路径独立审查与合规部署；随后才启动 E0 设计卡。原因是 HTTP transport 收敛会放大现有 daemon authority 的能力面，不能在治理 mutation 仍处于 reviewer-blocked/remediation 期间引入新的 auth profile 或 live transport switch。

因此，**现在不要创建 E1–E7 实施卡，不要执行 runtime refresh，也不要尝试让裸 HTTP 代替 named pipe/UDS。** 可以在 P0-K 通过后，以 E0 单卡冻结契约；E1、E2、E3、E4、E5、E6、E7 应是独立、可审查、可回滚的 task，不批量建卡、不并行跨越 peer identity 与 artifact durability gate。

## References

[1] [`rust_ext/src/daemon/transport.rs`](../../rust_ext/src/daemon/transport.rs)：跨平台 `TransportPeerIdentity`、UDS/named-pipe endpoint 及 OS peer credential 安全约束。  
[2] [`rust_ext/src/daemon/server.rs`](../../rust_ext/src/daemon/server.rs)：UDS/named-pipe dispatch 与 HTTP MVP 并行 transport、shared serialization 模型。  
[3] [`rust_ext/src/daemon/http_server.rs`](../../rust_ext/src/daemon/http_server.rs)：`dev_loopback_unauthenticated` profile、HTTP endpoints、dedup 与 synthetic local-owner peer。  
[4] [`server/daemon_client.py`](../../server/daemon_client.py)：HTTP thin client manifest/health fail-closed 与现有 IPC/HTTP 共存分支。  
[5] [`rust_ext/src/lib.rs`](../../rust_ext/src/lib.rs)：PyO3 导出面，包括 narrow daemon client slice 与 broader local Rust core。  
[6] [`server/ipc_transport.py`](../../server/ipc_transport.py)：UDS framing、sealed memfd/SCM_RIGHTS 大载荷语义与平台 fallback。
