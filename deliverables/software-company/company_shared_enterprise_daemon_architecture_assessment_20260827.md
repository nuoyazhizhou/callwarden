# 公司级共享 Enterprise Daemon：现状边界与演进评估

**日期**：2026-08-27  
**性质**：只读架构评估；不创建任务、不修改配置、不启动服务、不访问数据库。  
**前置约束**：P0-K 仍在独立复审流程中。本文件仅定义未来架构边界，不能用于跳过现有治理、部署或 runtime convergence 门禁。

## 结论

**是的。当前设计不是“公司级共享 daemon”，而是“每个操作系统用户一个本机 authority cell”。** 两个 Windows 登录用户不能安全地把同一份本地 SQLite 文件和同一个现有 Enterprise daemon 当作可并发访问的共享服务。

原因不是 HTTP 尚未完成，而是当前 security principal 与数据根目录本来就按 OS user 收敛：Windows endpoint 名称是 `\\.\pipe\callwarden-<owner-sid>`，其 SDDL 只授予该 owner SID；默认 codegraph DB 路径是 `%USERPROFILE%\.callwarden\callwarden.db`。即使手动使第二名用户获得文件访问权限，也仍然违反 daemon 的 peer-identity/workspace ownership 假设，并会让 SQLite 共享文件锁、恢复、备份和审计归属失去单写者保证。[1] [2]

> **正确的公司级目标不是让多个用户直接共享一个 SQLite 文件；而是让多个用户通过受认证的 company authority service 共享同一个“组织—项目—workspace/revision”事实层。数据库只能由该服务进程访问，客户端永远只调用 API。**

| 问题 | 当前单用户 Enterprise daemon | 公司级共享 authority 的目标 |
|---|---|---|
| 身份根 | UDS peer UID / named-pipe client SID | 组织身份 + 设备/用户认证；本机 peer credential 仅用于本机 agent 到 local relay 的证明 |
| endpoint | 每 user 的 socket/`callwarden-<SID>` pipe | 受 TLS 保护的 company endpoint；或公司服务器上的 local pipe/UDS 仅给服务进程 |
| 数据根 | `%USERPROFILE%\.callwarden\callwarden.db` | 服务账户拥有的 organization/project 数据库；客户端无 DB file ACL |
| SQLite 写入者 | 一个 user-local daemon | 每 tenant/cell 一个 authority writer；以后可演进为集群数据库 |
| Role Worker | 本机凭据 + task-local role contract | 稳定 CW-local worker identity 仍保留，但绑定 org/project/task 和 authenticated principal，不绑定 provider/model/session |
| 多 agent 并发 | 同用户多 client → local daemon lease/fencing | 多用户、多设备 client → central authority 的 lease/fencing/ledger，单一 authoritative writer / transactional store |

## 一、现有设计为什么不能直接共享

### 1. Endpoint 是 owner-SID 专属的

Windows listener 在 bind 时读取**当前运行 daemon 的用户 SID**，据此生成 `\\.\pipe\callwarden-<SID>`，并建立只给 owner SID 的 SDDL；源码还明确规定其他 SID 不授权。[1] 所以，两个 Windows 用户甚至不会默认发现或连接同一个 endpoint。

这项限制是有意的。当前 daemon 将 pipe token 所得 SID 视为 OS 提供、不可由 request body 伪造的 peer identity；Unix 对应 `SO_PEERCRED` / `LOCAL_PEERCRED`。客户端传入的 `agent_id`、`model_id`、`session_id` 不是这个层的授权根。[2]

### 2. 默认 database path 也是 per-user

`DaemonConfig.resolve_codegraph_db_path()` 在未配置 template 时回退到 `~/.callwarden/callwarden.db`；在 Windows 这就是 daemon 运行账户的用户目录。当前 `codegraph_db_path_template` 允许按 `workspace_instance_id` 分开文件，但仍没有 organization/tenant、网络数据库、跨用户 ACL、远程认证或 multi-writer coordination 的模型。[3]

### 3. “把 SQLite 放到共享盘”不是解决方案

即使两个用户指向同一 UNC/SMB 上的 `callwarden.db`，问题也没有消失：Windows file ACL 不能代替 daemon authorization；SQLite 的文件锁语义和 WAL 对网络共享文件并不是公司级多用户并发事务协议；进程 crash、网络瞬断、checkpoint 和备份会使恢复边界不清。更重要的是，这会绕过“authority process 是唯一写入者”的基本模型。

因此，不能采用如下做法：共享 `%USERPROFILE%\.callwarden`、放宽 pipe SDDL 给所有用户、让客户端直接在网络盘打开 DB，或把现有 `dev_loopback_unauthenticated` TCP HTTP 公开给内网。这些都会让本机可信身份、审计 principal、writer serialization 与数据完整性失效。

## 二、需要区分的两个目标

“全公司共享 Enterprise daemon”可能指两种不同架构，它们不应使用相同的实现路径。

| 目标 | 最小部署形态 | 推荐数据层 | 适合范围 | 不解决的问题 |
|---|---|---|---|---|
| **A. 同一台 Windows Server 上的多个 AD/本地用户共享** | 一个以专用 service account 运行的 `cw-authority` 服务 | 初期可用该服务本机私有 SQLite；仅服务账户可读写 | 小团队、单机、低可用性要求 | 不支持跨机器高可用；需处理 Windows 用户到 service 的网络认证 |
| **B. 多台开发机/多个 Windows 用户共享组织级工作控制台与项目索引** | 独立 company authority service，客户端以 HTTPS 调用 | 服务端数据库；长期建议 PostgreSQL 或等价事务数据库；artifact/object store | 企业、多设备、项目共享、审计/备份/扩展 | 需要组织身份、设备/服务认证、schema tenancy 与 artifact pipeline |

A 是把现有 daemon 的运行账户从“某个开发者”提升为“服务账户”；B 才是用户所说的真正“全公司共享”。两者都不能让 client 直接共享 SQLite；区别只在于 A 的 SQLite 仍可由单一 server process 安全拥有，而 B 需要把 availability、backup、concurrency 和 organization tenancy 一开始就设计为服务端能力。

## 三、建议的公司级终态

### 1. 双层 authority，而非一层裸 HTTP

```text
Developer / Agent / GoodBuddy runtime
        │
        │ TLS HTTP/JSON-RPC + authenticated principal
        ▼
Company CW Authority
  ├─ Organization / project / workspace-revision resolver
  ├─ Authorization policy (principal → org/project → action)
  ├─ Role Worker validation (credential proof; stable worker role)
  ├─ Lease / fencing / append-only operation ledger
  ├─ Rust domain handlers
  ├─ Artifact ingest and snapshot pipeline
  └─ Company-owned transactional store
        ▲
        │ optional local relay only
        │ UDS / named pipe with OS peer credential
        │
Local CW relay / local agent runtime
```

公司服务的 HTTP 请求必须有独立的 network authentication，不应把 `127.0.0.1` 的 loopback 身份模型放大到 LAN/WAN。推荐以 **mTLS 设备身份 + OIDC/企业 SSO 用户身份**，或在最小初版用 mTLS service/client certificate；证书/identity 从公司 PKI 或设备管理系统获取。TLS 层得到 authenticated principal 后，Rust policy engine 再将其映射到 org/project membership 和 task capability。

本机 named pipe/UDS 仍然有价值，但职责变为：本机 local agent 与 local relay 之间的 OS-user boundary；不是公司服务器的最终 client authorization。Windows SID、provider account、model、agent instance、runtime session 分别只是不同层次的事实：SID/证书证明传输或设备主体；Role Worker credential 证明稳定 CW-local role；provider/model/session 仅作追加 provenance。

### 2. 数据分域

公司数据至少分为四类，不能再混在某一用户的单一 `callwarden.db` 中。

| 数据域 | authoritative key | 建议保存位置 | 访问边界 |
|---|---|---|---|
| 组织控制面 | `org_id` | company authority DB | 组织管理员、组织 policy |
| 项目/代码图 | `org_id + project_id + workspace_revision_id` | server DB + artifact/object storage | 获授权项目成员；revision immutable |
| 任务治理面 | `org_id + project_id + task_id` | transaction-capable authority DB | Role Worker、lease/fencing、append-only ledger |
| 本机运行时/凭据 | device/local user + opaque handle | 本机 ACL 目录 / OS keystore | 仅本机 owner；不得上传 raw credential |

当前 schema 中的 `workspace_instance_id` 可以继续作为 host/local capture 的 provenance 维度，但不能再充当公司级 project 的唯一标识。公司级是 `org_id/project_id`；workspace instance 表示某台机器/某份工作树的某个 snapshot capture。这样两个开发者可以对同一项目贡献不同 revision，并由 authority 通过 hash、parent revision 与 merge policy 构建一致的共享事实。

### 3. 数据库演进原则

第一阶段可以让**一个 company authority process**独占一份本机数据库，保留 SQLite 作为 server-private embedded store；此时所有用户通过 API 访问，SQLite 仍只有一个 writer，不在共享网络目录。这很适合验证 multi-user auth、organization namespace 和 task governance。

但如果目标包括多 daemon 副本、故障切换、跨机部署、长期审计或大量并发项目，则应把 authority 的 transactional state（task/lease/ledger/role worker metadata、workspace registry、policy）迁移到 PostgreSQL 等服务端事务数据库。Code graph/full-text/vector/artifact 可按 workload 分开，但**task fencing 与 operation ledger 必须与 authority 提交点处于可原子化的存储边界**。在这以前，不能部署两个 daemon 同时写同一 SQLite。

## 四、公司级授权模型

### 1. Principal 不是 runtime string

公司 authority 至少需要以下分层：

| 层 | 证明什么 | 允许作为授权锚点？ |
|---|---|---|
| Transport/device authentication | 哪台受管设备或哪项受信服务在连接 | 是，mTLS certificate/key 或受保护的 service identity |
| User / workload principal | 哪位员工、CI workload 或 agent host 被认证 | 是，OIDC claim / service account，需短时会话与可撤销性 |
| Organization/project membership | principal 对哪个 `org_id/project_id` 有何权限 | 是，authority policy database |
| CW Role Worker | 稳定 local worker 的冻结角色与 task separation | 是，按 P0-K 的 local credential + frozen mapping；不得替代 corporate auth |
| Agent/model/provider/session | 由哪个工具/版本/会话产生行为 | 否，只作 append-only provenance |

这正好延续已有 Role Worker 的修正方向：公司服务不能因为员工切换模型、供应商账号或 agent runtime 而让角色失效；反过来，任何 runtime string 也不能自称为 Reviewer/Adjudicator。多用户时，还需另加“role worker 是否允许代表此 authenticated company principal 在此 org/project/task 工作”的授权绑定。

### 2. 共享数据的并发边界

现有 lease/fencing 与 append-only ledger 不需要丢弃，反而应成为公司级控制面核心。变化是它们的 scope 从单 user/workspace 提升为：

```text
(org_id, project_id, task_id, role)
```

fencing counter、operation idempotency 和 verdict/evidence provenance 都必须由 central authority transaction 生成。客户端、MCP、CLI、agent runtime 只能提交 request；它们不能对 DB 发 SQL，也不能生成最终 fencing counter。对于一个 task，Reviewer proof 与 Adjudicator worker proof 仍需是独立 worker，外部 user/session/model 的变化只被记录。

## 五、对 HTTP 与 PyO3 迁移的修正

上一份 transport 评估中“HTTP-over-UDS/named-pipe”仍是**单机 Enterprise 模式**的正确终态；它解决同一 OS user 下 protocol 统一，同时保留 peer credential。它本身不使两个用户共享数据库。

公司级模式则改为：客户端与 company authority 使用 authenticated TLS HTTP；本机 IPC 只用于 local relay、agent sidecar 或本机开发工具，不能被误当成跨用户共享协议。PyO3 也应拆开处理：

| PyO3 范围 | 公司级处置 |
|---|---|
| daemon/IPC client builder、legacy framing | 由 HTTP client SDK（Python/Java）替换；client 无 SQLite access |
| daemon-side Rust domain / parser / index compute | 保持 Rust process-internal library；由 authority worker 调用 |
| desktop local parsing/watcher | 可保留 PyO3 或 local sidecar，将 signed/hashed artifact 上传给 company authority |
| Rust transport peercred primitive | 保留在 local relay；不能在 TLS request body 中模拟 |

这意味着“迁移到 HTTP”是正确的，但要拆成 **单机 peer-authenticated HTTP** 与 **公司 TLS-authenticated HTTP** 两个 profile。二者请求 envelope 可以相同，认证适配器、endpoint discovery、manifest 和 tenancy 不能相同。

## 六、建议的任务顺序与绝对禁止项

当前不应立即执行公司级改造。首先完成既有 58 个 `python_compat` 的 Rust business migration，以及 P0-K reviewer/adjudication/受控 runtime convergence。之后以一张仅设计/contract 卡开始，不建立大批实施任务。

| 阶段 | 单卡目标 | 关键验收 |
|---|---|---|
| C0 | `Company Authority v1` threat model、org/project/revision schema、auth profiles、data ownership contract | 独立 review 通过；明确所有 principal 与 secret boundaries |
| C1 | company service bootstrap：single server process + service-owned data root；无 client DB file access | two Windows principals 读同 org/project 成功，未授权 principal 拒绝 |
| C2 | TLS/mTLS + enterprise identity adapter；principal-to-membership policy | wrong/expired/revoked certificate/token fail-closed；no body claim override |
| C3 | task/lease/ledger tenant scoping 与 Role Worker principal binding | cross-org/task/role worker isolation；fencing stale rejected |
| C4 | artifact/snapshot ingest 与 revision model | two users upload distinct workspace revisions；hash/ACL/retention correct |
| C5 | Python/Java HTTP SDK convergence，移除 client-side DB/legacy IPC business paths | source and black-box no-fallback tests |
| C6 | scale-out data plane readiness：transactional DB migration、backup/restore/DR、observability | no multi-daemon SQLite writer；recovery drill and audit-chain verification |

在 C0–C6 中，以下行为应保持禁止：共享用户目录或网络盘 SQLite；放宽 per-user pipe SDDL 当作公司认证；裸 TCP loopback HTTP 用作 enterprise endpoint；让 MCP/CLI/Java client 直接访问 DB；把 provider token 或 role worker credential 用作 corporate bearer token；为图省事在未通过 P0-K 独立复审时重启/替换 live authority。

## 参考

[1] [`rust_ext/src/daemon/transport_windows.rs`](../../rust_ext/src/daemon/transport_windows.rs)：Windows pipe 由 owner SID 命名，SDDL 仅授权 owner SID。  
[2] [`rust_ext/src/daemon/transport.rs`](../../rust_ext/src/daemon/transport.rs)：`TransportPeerIdentity` 仅由 OS peer credential 派生，客户端字段不参与授权。  
[3] [`server/daemon_config.py`](../../server/daemon_config.py)：默认 `~/.callwarden/callwarden.db` 与 `codegraph_db_path_template` 的单用户回退语义。  
[4] [`enterprise_http_transport_and_pyo3_convergence_assessment_20260827.md`](enterprise_http_transport_and_pyo3_convergence_assessment_20260827.md)：单机 HTTP-over-UDS/named-pipe 收敛与 TCP loopback identity 边界的前置评估。
