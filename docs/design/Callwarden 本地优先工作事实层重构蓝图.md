# Callwarden 本地优先工作事实层重构蓝图

**审查范围：** 只读盘点现有 Python/SQLite/MCP、Rust HTTP daemon、Task Envelope/三角色设计与本机调用状态；提出 GoodBuddy/私有云环境下数据主权、跨 Agent/模型续接和渐进重构方案。

**本轮身份：** `reviewer / independent_reviewer / T-1786983366974-8811ccec / Skill: persistent-computing`。

## 结论

你提出的问题不应被定义为“怎样导出聊天记录”，而应被定义为：

> **怎样使一个工程工作单元脱离任何 Agent 账户、LLM 会话和供应商工作区，成为由用户或企业持有、可验证、可复制、可恢复、可由新运行时继续执行的工作事实。**

答案不是把供应商聊天完整镜像到 CW，也不是让每个 Agent 直接读写同一份联网 SQLite。正确的目标是建立一个**本地优先的工作事实层（Local-First Work Fact Layer，以下简称 WFL）**：

1. **CW daemon** 是一个逻辑工作区的唯一可写 authority；
2. **Task Envelope** 是由该 authority 派生的、不可变且可携带的任务续接包；
3. **事件账本 + 内容寻址工件库**保存工作事实，不依赖供应商聊天可导出；
4. **GoodBuddy/其他 Agent runtime** 是可替换的执行器，通过 adapter 接入；
5. **私有云**默认是加密复制、备份和可选的私有 authority，不是共享挂载的 SQLite 文件；
6. 断网或供应商账号失效时，新的 Agent 领取同一 Task Envelope，而不是依赖复制粘贴聊天。

这会把“切换 WorkBuddy/TraeWork 用户后需要翻页复制最终报告”的问题，转化为“新 runtime 用同一任务 ID 和一个经过验证的 continuation bundle 续接”。供应商聊天仍可作为可选附件；它不再是项目唯一的工作记忆。

---

## 1. 当前迁移断层盘点

### 1.1 已有资产不是空白，而是尚未收口

| 资产 | 只读核验结果 | 可复用价值 | 当前限制 |
|---|---|---|---|
| MCP 工具面 | 当前连接器能够发现 **239** 个工具；迁移矩阵也记录 239 项：126 `rust_native`、73 `python_compat`、40 `task_rpc`。 | 可作为不同 Agent 的能力发现和只读工程上下文入口。 | 实际无副作用 `get_stats` 调用返回 `E_HTTP_MANIFEST_MISSING`；发现不等于可调用。 |
| Python 客户端 | `server/daemon_client.py` 同时含 HTTP、Named Pipe/UDS、`sqlite3`、SQL fallback、snapshot 发布和 task route。 | 可保留为薄适配器、协议兼容层和 local test path。 | 仍是多代路径共存的集中点，非真正“Python 只做壳”。 |
| Rust daemon | 已有统一 dispatch、序列化点、daemon task handlers、lease、HTTP client/health/manifest 模型。 | 可以成为每个逻辑工作区的单写 authority。 | 实际 task RPC 仍路由到 `TaskCollabStore`；新 task-loop 事务账本模块尚未接管公共 task 写入。 |
| 任务与证据数据 | `tasks`、`task_steps`、`task_events`、identity、Role Contract、assignment、lease、lease event 等表已存在。 | 是 WFL 的事实核心雏形。 | 历史 task 缺 task→workspace 强绑定时无法安全跨机器/跨 authority 续接。 |
| C5 的 CAS/备份/恢复 | Rust backup 具有原子临时目录发布、校验、`registry.db`/`cas.db`/`audit.db`/`daemon.json`/snapshots 全量布局；已有 crash recovery 思路。 | 是内容寻址工件、离线备份和私有云复制的基础。 | snapshot generation 重启后丢失，Python compat 与 Rust 仍有局部双实现。 |
| 三角色 / Envelope 设计 | 已冻结的设计已明确 Task/Role Contract 双哈希、workspace binding、next_action、lease/fencing、operation ledger 和 append-only handoff/verdict。 | 目标模型是正确的。 | 文档明确它是分阶段待落地能力；未完成的 capability 必须 fail-closed，不能宣称已经上线。 |

### 1.2 断链的精确位置

| 断链 | 代码/文档事实 | 对“可迁移工作包”的影响 |
|---|---|---|
| **MCP discovery ≠ invocation** | HTTP client `discover()` 强制要求 authority-scoped manifest，并会对 `/health` 的 `manifest_id`/PID 交叉校验；当前调用缺 manifest。 | 新 runtime 不能仅凭发现的 tool schema 就继续工作；需要可信 bootstrap/health/capability discovery。 |
| **HTTP task 路由仍依赖本地 SQLite** | `route_task_write/read/route_rpc` 对 task/lease 会调用 `_inject_workspace_id()`；该函数通过本地 `get_db()` 的 active workspace 补数值 workspace ID。 | 跨机器、跨账号或私有云 client 仍依赖本机旧 DB 状态，无法只靠 Task Envelope 完成无缝续接。 |
| **新 task-loop 未成为实际任务写 authority** | `dispatch.rs` 中 `task.create/claim/report/handoff/apply/close/lease.*` 仍交由 legacy `TaskCollabStore` handler；task_loop 的 public control-plane 当前只见 `task_loop.public_promote`。 | operation ledger、v1 handoff 和 envelope 语义尚不能被当成全量生产保证。 |
| **幂等性不完整** | `mutation_call()` 明确注释：`task.create` 的权威 event schema 尚未持久化 request_id；响应丢失后必须 fail-closed，否则可能重复建任务。 | 可迁移/可恢复设计必须将所有任务域 mutation——包括 create——纳入持久化 ledger。 |
| **权威时钟边界泄漏** | 客户端 `get_authoritative_clock()` 在异常时回退本地 `time.time()`。 | 本地墙钟不能参与 protected mutation、lease expiry 或 event ordering 的权威判断。 |
| **旧路径尚可直接写/读** | 迁移账本记录 `daemon_client.py` 仍保留 `sqlite3`、`_sql_fallbacks` 与 get_db 路径；matrix 中 73 项仍为 transition。 | 不能直接把整个 SQLite 目录“同步到云端”并让多个客户端写；会制造第二 authority。 |

> **判断：** 当前系统不是“旧架构完全报废、新架构完全不可用”，而是存在多个局部正确、总体未收口的 authority。重构目标应首先是消灭“谁有权写同一工作事实”的歧义，而不是迁移更多工具数量。

---

## 2. 设计原则：工作事实优先于聊天记录

供应商聊天不能读取、工具调用不可导出、账号切换即丢历史时，无法可靠地事后爬取或还原完整工作过程。屏幕自动化最多取回可见最终文本，不能证明工具执行、文件基线、失败重试和权限边界；它不应成为生产迁移方案。

WFL 应将数据分为四类，并明确每类是否可迁移、是否可重建、是否可同步。

| 数据类 | 典型内容 | 权威位置 | 是否复制/导出 | 绝不能做的事 |
|---|---|---|---|---|
| **A. 不可变工作事实** | Task/Step、合同版本、状态事件、assignment、verdict、gate、evidence metadata、handoff、request outcome。 | CW task ledger。 | 必须可导出、签名校验、顺序重放。 | 让 runtime 私有会话成为唯一来源。 |
| **B. 内容寻址工件** | 计划、结构化摘要、diff、测试日志、命令摘要、截图、产物、报告、补丁、文件快照。 | CAS/Object Store；ledger 仅引用 hash。 | 必须可复制，可选择端到端加密。 | 将大 blob 塞进 task row 或只存供应商 URL。 |
| **C. 可重建派生数据** | symbol graph、embedding、snapshot cache、全文索引、代码度量、临时 search index。 | 本地 daemon cache。 | 可选复制优化；丢失后由 repo+facts 重建。 | 把它当作 task 状态真相或把快照 PID 同步到云。 |
| **D. 秘密与短暂能力** | 模型 API key、OAuth cookie、原始 lease token、会话 cookie、HTTP manifest、daemon PID、临时 capability。 | OS secure store / daemon memory。 | 默认不导出；只导出可撤销的公钥或 identity 摘要。 | 同步 token、cookie、私钥、明文 key 或把 manifest 当永久身份。 |

这意味着 CW 不需要保存隐藏推理，也不需要把所有聊天都保存下来。它只需要在以下节点要求 adapter 生成**结构化、可见、可审计的 checkpoint**：需求冻结、计划冻结、开始执行、每次状态改变、测试/工件产生、handoff、review 和终态。聊天全文是可选证据附件；Task Envelope 是必需的可续接事实。

---

## 3. 目标架构：本地优先，而非“共享 SQLite 去中心化”

### 3.1 推荐拓扑

```text
┌─────────────────────────────────────────────────────────────┐
│ 本地设备 / 私有执行节点                                      │
│                                                             │
│  GoodBuddy Main / Claude Code / Codex / Qoder / 其他 Runtime │
│          │  CW Runtime Adapter（无模型主密钥）               │
│          ▼                                                  │
│  CW daemon ── 单一 writer ── Work Fact Store (SQLite)       │
│      │       task ledger / contracts / assignment / gate     │
│      │       CAS references / workspace binding              │
│      ├── encrypted local CAS (artifacts)                     │
│      └── read-only MCP capability endpoint                   │
└─────────────────────────────────────────────────────────────┘
                       │
          signed + content-addressed + encrypted replication
                       │
┌─────────────────────────────────────────────────────────────┐
│ 用户控制的私有云 / NAS / 企业私有对象存储                    │
│  - append-only encrypted event packs                         │
│  - encrypted CAS blobs                                      │
│  - signed replica manifest / backup catalog                  │
│  - 可选：私有 CW authority（只在需要远程协同时启用）        │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 必须接受的分布式系统事实

“数据完全属于本地或私有云”可以做到；“所有设备在离线时无冲突地共同改同一个任务状态”则不能靠复制 SQLite 或 CRDT 自动做到。

任务的 `claim`、`lease`、`fencing`、`apply/close`、gate 决策是**权限和顺序敏感**状态，需要总顺序和唯一 writer。因此推荐模型是：

1. **一个逻辑 workspace 在任一时刻只有一个 task authority。** 本地模式下是本机 daemon；团队远程模式下是企业私有网络中的 CW authority。
2. **所有其他节点可读、可构建、可产生 proposal package，但不能离线直接确认 task 状态迁移。**
3. 离线 Agent 的产出以 `proposal`/artifact/evidence pack 保存；恢复连接后由 authority 以当前 lease、contract、base commit 和 event head 重新验证并接受或拒绝。
4. 发生两个设备同时编辑同一 scope 时，依赖 Git/worktree 与 task contract 解决代码冲突；不尝试用 CRDT 合并 `closed`、`review pass` 等治理事实。

这样不是降低“去中心化”，而是把去中心化放在**数据所有权和可恢复复制**，而非把安全状态机误做成无主多写数据库。

### 3.3 三种部署档位

| 档位 | authority | 私有云作用 | 使用场景 | 不做什么 |
|---|---|---|---|---|
| **L0：纯本地** | 每个项目由本机 CW daemon。 | 无；可用离线加密备份磁盘。 | 个人、单机、供应商账号切换。 | 不声称多机并发协同。 |
| **L1：本地优先 + 私有复制** | 某一设备或显式选定节点在某段时间担任 authority。 | NAS/VPN/S3 兼容私有对象存储保存加密 event/CAS packs。 | 个人多设备、小团队、灾备。 | 不挂载共享 `.db` 让多 daemon 同时写。 |
| **L2：私有协作 authority** | 企业私有网络中的 CW daemon，客户端通过 mTLS/attested adapter 接入。 | 同时做备份、对象库和健康/审计副本。 | 多人、多 runtime、受监管团队。 | 不把模型 API key 或供应商会话迁入 CW。 |

L0 是应首先实现的产品；L1 以导出/导入与加密 replication 加强连续性；只有真实存在跨主机并发写的团队，才进入 L2。

---

## 4. Task Envelope：从“聊天上下文”变成可验证续接包

### 4.1 Envelope 的角色

Task Envelope 不是 task 表的 JSON dump，也不是给 LLM 的无限 prompt。它是 authority 基于某一事件头生成的**不可变视图**，只包含下一位合法执行者完成一个有限动作所需的最小、可验证上下文。

以下规则必须冻结：

- Envelope **永远不可作为写入授权**；write 仍需 daemon 在事务内重新检查 assignment、lease、fencing、合同、workspace binding 和当前 event head。
- Envelope 不含 raw lease token、API key、cookie、隐藏推理和可伪造的 `role`。
- Envelope 的 `head_event_hash`、contract hash、workspace capture 和 artifact refs 使它成为可验证 checkpoint；任意状态变化后生成新 revision。
- 换 Agent/模型时，新 adapter 领取新的 session 并请求同一 task 的最新 Envelope；旧 Envelope 只能用于阅读和构建 proposal，不可继续推进状态。

### 4.2 建议 v1 数据结构

```json
{
  "schema": "cw.task-envelope/v1",
  "envelope_id": "ENV-...",
  "task_id": "T-...",
  "step_id": "S-...",
  "logical_workspace_id": "WS-...",
  "workspace_capture": {
    "capture_id": "WC-...",
    "repo_identity": "sha256:...",
    "base_commit": "...",
    "base_ref": "refs/cw/integration",
    "worktree_descriptor_ref": "cas:sha256:..."
  },
  "event_cursor": {
    "ledger_seq": 418,
    "head_event_hash": "sha256:...",
    "issued_at": "authoritative timestamp"
  },
  "task_contract": {"id": "TC-...", "revision": 7, "hash": "sha256:..."},
  "role_contract": {"id": "RC-...", "revision": 2, "hash": "sha256:..."},
  "action": {
    "decision": "READY",
    "kind": "CLAIM | REVIEW | ADJUDICATE | REVISE",
    "required_governance_role": "executor",
    "required_runtime_role": "implementer",
    "allowed_paths": ["..."],
    "forbidden_paths": ["..."],
    "commands": ["tokenslim run ..."],
    "acceptance_checks": ["..."],
    "required_evidence": ["commit", "test_log"]
  },
  "continuation": {
    "completed_fact_refs": ["event:...", "cas:sha256:..."],
    "open_questions": ["..."],
    "blocked_conditions": [],
    "required_handoff": {"target_role": "reviewer", "new_session": true}
  },
  "provenance": {
    "previous_runtime_summary_ref": "cas:sha256:...",
    "tool_schema_hash": "sha256:...",
    "public_prompt_summary_hash": "sha256:..."
  },
  "integrity": {
    "authority_id": "...",
    "authority_generation": 12,
    "payload_hash": "sha256:...",
    "signature": "detached signature"
  }
}
```

`previous_runtime_summary_ref` 必须是工具调用和产物的**结构化摘要**，例如“已运行命令、退出码、生成的 blob、未解决错误、未修改路径”，而不是供应商隐藏 Chain-of-Thought。供应商原始会话即使不可读，新的 Agent 仍可根据该摘要、Git base、事实事件和附件继续工作。

### 4.3 四个实际续接流程

| 场景 | 新 runtime 获得什么 | 旧 runtime 能否继续写 | 结果 |
|---|---|---|---|
| 用户切换账号或薅积分 | 最新 Envelope + CAS 工件 + task event cursor。 | 只要旧 lease 未过期，理论上仍可；新 runtime 需独立 registration/lease。 | 用户无需复制聊天，只需在新 runtime 打开同一 task。 |
| 切换模型 / GoodBuddy runtime | 同一 Envelope；runtime/model provenance 作为新 session attestation。 | 旧 session 的 lease 过期、revoke 或 release 后不可再写。 | 任务保持连续，评测账本能比较新旧组合。 |
| 原 Agent 断线，结果未知 | adapter 用同一 `request_id` 查询 authority ledger。 | 无结果前不可盲目重放 create/report/handoff。 | 只存在一个可重放结果；不重复建 task/事件。 |
| 供应商彻底不可访问 | CW 从本地/私有副本恢复 task ledger、CAS 和 Git ref；新 Agent 再注册。 | 不适用。 | 聊天历史可缺失，但工作事实、证据和下一动作仍存在。 |

---

## 5. 私有持久化、加密与迁移格式

### 5.1 推荐的导出单元：CW Work Package

不要导出一个活跃的 `callwarden.db` 让他机直接打开。应使用可验证的 `cw-work-package/v1`：

```text
CWPK/<package_id>/
  manifest.canonical.json        # schema、authority、event head、包索引
  ledger/events.ndjson           # append-only events，hash chain
  ledger/contracts/*.json        # task/role/workspace contracts
  ledger/projections.json        # 可重建缓存，可选
  objects/<sha256>               # content-addressed artifacts
  git/refs.json                  # refs/base commit/worktree descriptor
  attestations/public.json       # 公钥、撤销摘要、无 secret identity provenance
  verify.json                    # 签名、对象哈希、导入前校验结果
```

`manifest` 只引用对象 hash；object 可按内容独立去重。创建包时必须先 checkpoint，生成临时目录，计算所有 hash，签名，再原子 rename。现有 C5 全量 backup 的临时 `.partial` → rename、校验和与多库布局是可复用方向，但 WFL 应另有明确 manifest，不能把 `daemon.json`、PID 和运行时 endpoint 当作迁移身份。

### 5.2 加密和密钥边界

| 项目 | 推荐做法 |
|---|---|
| 本地静态数据 | 操作系统全盘加密为最低线；WFL 包和 CAS 对象可选每 workspace 数据密钥加密。 |
| 私有云复制 | 客户端先加密、服务器只保存密文对象；服务器不持有模型 key、原始 lease token 或供应商 cookie。 |
| 设备身份 | 每个 CW adapter/daemon 有可撤销的设备/实例密钥；私钥留 OS secure store，ledger 只保存公钥和 attestation。 |
| 人工恢复 | 企业持有恢复密钥或多方恢复机制；不要把唯一恢复密钥绑定单个 Agent 账户。 |
| 日志 | 工具参数和输出做大小/敏感字段限制；保留 hash、摘要和受控附件，而非无边界记录所有 prompt。 |

### 5.3 同步协议

同步的最小正确单位是**不可变对象和事件段**，不是 WAL、SHM 或 live SQLite 文件。

1. 本地 authority 提交一个 task-domain transaction；
2. 同事务写 task event 与 operation ledger result；
3. commit 后生成可复制的 event segment 和 CAS object refs；
4. replicator 将加密对象上传到私有目标；
5. 远端/第二设备先验证签名、hash chain 和 manifest head，再构建只读投影；
6. 欲进行写入时，第二设备必须显式获取 authority，而非直接修改同步得到的 DB。

对于 L1 离线多设备，建议使用 **proposal pack**：离线 Agent 可以产生 diff、test log、artifact 和结构化建议；它们进入 CAS，但 task state 不直接推进。恢复到 authority 后，由新的 Envelope 和一笔受保护 mutation 重新验证。这样保留离线生产力，又不伪造多主一致性。

---

## 6. 针对当前代码的重构策略

### 6.1 不要全量重写；先建立唯一事实核

当前迁移矩阵中 126 项已标记 Rust native、73 项仍是 Python compat、40 项 task RPC，且有 73 项 transition。正确恢复次序不是逐一迁移 239 个工具，而是先让**所有和工作事实有关的路径**收口。

| 重构域 | 当前状态 | 目标收口 | 必须避免 |
|---|---|---|---|
| Manifest / health / capability | 真实 MCP 调用缺 authority manifest 即不可用。 | daemon 启动原子发布 manifest；health 返回 authority/generation/capability digest；client 明确诊断缺失、陈旧、错 authority。 | 为恢复可用性而关闭 manifest 校验或静默回退 SQLite。 |
| Workspace identity | task/lease HTTP route 仍从本机 active DB 注入 `workspace_id`。 | `logical_workspace_id` + immutable `task_workspace_binding` 成为唯一输入；adapter 从 Envelope 带 binding，不查本地 active workspace 补齐。 | 用 cwd、路径 hash 或 workspace 1 兜底。 |
| Task operation ledger | 设计已定义 task-domain ledger；实际 legacy handlers 仍主要工作。 | 将 `task.create/claim/report/handoff/apply/close/lease.*` 等作为一个原子 cutover domain，包含 create 的 request id。 | 一个方法一个方法半切换，使同一 task 同时有两种 dedup truth。 |
| Evidence/CAS | 现有 task event 有 evidence path/hash，C5 有 CAS/backup。 | evidence_path 迁移为 immutable CAS ref；本地路径只是 materialization hint。 | 将 `C:\...` 当跨设备可复用证据定位。 |
| Runtime adapter | MCP 现为工具入口，role/identity 部分由客户端自报。 | GoodBuddy Main/未来 adapter 生成 session attestation、运行摘要和安全 capability injection。 | 让模型自己生成 agent identity 或直接持有 DB/lease token。 |
| Graph/snapshot | 仍有 Python/Rust snapshot 双路径和可重建缓存。 | 保持可重建，按 query slice 迁移；与 WFL task ledger 分开。 | 阻塞任务事实层重构，等待全部代码图谱工具 Rust 化。 |

### 6.2 需要立即修订的两个边界

1. **禁止 task/lease 的 workspace ID 从本地 active DB 隐式注入。** 这会让远程或全新设备无法从 Envelope 续接，且将旧 SQLite 变成 HTTP authority 的隐藏依赖。新 API 只接收已验证的 `workspace_instance_id`/binding reference，daemon 内部完成逻辑 workspace 映射。

2. **将 `task.create` 纳入持久化 operation ledger。** 当前 response-drop 时 create fail-closed 是正确的短期保守行为，但不是长期可迁移能力。一个 work package 的导入、创建、handoff、evidence、verdict 都需要 `(logical_workspace_id, canonical_method, request_id)` + canonical parameter hash 的持久重放。

---

## 7. 建议的父任务—子任务树（草案，不创建）

当前绑定任务 `T-1786983366974-8811ccec` 已为 closed 的三角色治理实施任务，不应擅自向其中追加重构工作。若用户批准，应新建一个父任务：

> **P-WFL：本地优先 Work Fact Layer 与 Task Envelope 迁移恢复**

| 阶段 | 子任务 | 依赖 | 交付与验收 |
|---|---|---|---|
| **R0** | 运行时与 authority 基线冻结 | 无 | 记录 Git commit、daemon binary、schema、authority、manifest、capability digest、工具矩阵；禁止通过关闭安全门禁“恢复”。 |
| **R1** | WFL 数据分类与 backup manifest v1 | R0 | 规范 A/B/C/D 数据清单、CWPK schema、导入/导出前校验、secret exclusion tests；复用 C5 原子 backup 机制。 |
| **R2** | HTTP manifest/health/capability 恢复 | R0 | manifest 原子写、启动/重启/陈旧/错 authority 负测；MCP 至少一个 read-only call 真实成功。 |
| **R3** | Workspace Binding v1 | R1、R2 | 新增 immutable logical workspace binding/capture；task/lease route 移除 `_inject_workspace_id()` 对 local active DB 的生产依赖；历史任务只读 `UNVERIFIED`。 |
| **R4** | Task Operation Ledger Foundation | R1、R3 | 统一 `request_id`/canonical params/response replay；至少覆盖 create、claim、report、handoff、lease、apply、close；response-drop/restart 负测。 |
| **R5** | Task Envelope v1 + `task.next_action` | R3、R4 | daemon 只读生成、签名/头 hash、CAS refs、deterministic BLOCKED；不自动 claim/lease。 |
| **R6** | Legacy TaskCollabStore cutover | R4、R5 | task-domain methods 一次性切到 ledger wrapper；旧 handler 只保留 explicit legacy/test route；双写检测与拒绝。 |
| **R7** | Runtime Adapter Reference PoC | R2、R5、R6 | 先选 GoodBuddy 或一个本地 CLI runtime；register→Envelope→evidence→handoff→新 runtime resume 全链 E2E。 |
| **R8** | CW Work Package export/import + L0 restore | R1、R5、R6 | 离线机器导出包；新 profile/新设备导入；Git/CAS/task next_action 一致；无供应商聊天可读前提。 |
| **R9** | 私有复制与 proposal pack | R8 | 加密 event/CAS replication；断网 proposal；回连后 authority 接受/拒绝；不共享 SQLite/WAL。 |
| **R10** | 跨 runtime 评测账本 | R7、R8 | 保存 runtime/model/skill/version/成本/耗时/返工/证据摘要；支持可导出比较。 |

**并发规则：** R2 可与 R1 并行；R3 必须在 R2 后；R4-R6 必须串行；R7 只能在 R6 后开始。严禁让多个 Agent 同时修改 `dispatch.rs`、task ledger schema 和 legacy task handler；这些是单一 authority cutover 的同一所有权面。

---

## 8. 首个可验证的最小产品

不要先开发私有云、P2P、企业 SSO 或完整多 Agent 平台。最小可验证成果应是：

> 在 Windows 本机，Agent A 的供应商账户不再可用后，用户启动 GoodBuddy 的另一 runtime 或一个本地 CLI Agent；它不读取旧聊天，也不复制文字，只凭 `cw-work-package` 和最新 Task Envelope，在同一 Git base/worktree 上理解已完成事项、核验测试与证据、领取下一步，并在旧 session 失效后安全续接。

该成果需要的验收矩阵如下：

| 测试 | 应通过的事实 |
|---|---|
| 切换 runtime | 新 runtime 能显示同一 task、同一 contract hash、同一 base commit、相同待办与 evidence refs。 |
| 旧 lease | 过期/释放/更高 fencing 后，旧 runtime 对 report/handoff/apply/close 的尝试稳定失败。 |
| 响应丢失 | create/report/handoff 等重试不会产生第二任务或第二事件；同 request_id 得到原 response/error。 |
| daemon restart | restart 后旧 manifest 被拒绝；重新 discover 后可从 ledger/CAS 生成同一最新 Envelope。 |
| 无供应商聊天 | 删除 runtime 的本地 conversation cache 后，task 仍能继续；缺失的原聊天只显示 provenance gap，不伪造工具历史。 |
| 新设备导入 | 从 CWPK 导入后校验签名、event head、CAS hash、Git base；不能直接复用旧 token/cookie。 |
| 私有复制故障 | 复制中断只产生未完成对象，不会推进 remote head；恢复后可校验并重试。 |

---

## 9. 最终建议

**应该开始这项盘点和重构，但不应把它作为“重新发明 Agent runtime”的工程。**

Callwarden 的产品边界应收缩并强化为：用户/企业拥有的工作事实、工程状态、证据、合同和可迁移评测账本。GoodBuddy、Qoder、WorkBuddy、Claude Code、Codex 等负责模型体验、工具调度、桌面/IDE 和专家团队。供应商账户切换、模型替换、聊天不可读、甚至服务完全不可达，不应再摧毁工作连续性。

最关键的技术决策只有一句：

> **复制不可变事实和内容对象；不要复制或多主写活 SQLite。以单一 daemon authority 维护任务状态，以签名的 Task Envelope 让任何合格 runtime 续接工作。**

在当前仓库中，先恢复 manifest/health，移除 task route 对 local active DB 的隐性依赖，再完成 task-domain durable operation ledger 和 Task Envelope v1，才有资格开始 GoodBuddy adapter 和私有云同步。

## References

[1]: `docs/design/daemon-rust-migration-ledger.md` — Rust daemon 迁移账本、MCP 状态与单写原则。
[2]: `server/daemon_client.py` — HTTP manifest/health、route_task_*、route_rpc、mutation retry 的实际实现。
[3]: `rust_ext/src/daemon/dispatch.rs` — 当前 task/lease RPC 的实际生产分发与 task_loop control-plane 范围。
[4]: `docs/design/cw-role-handoff-task-loop.md` — 已冻结的 Task Envelope、ledger、capability 与 handoff 设计基线。
[5]: `docs/design/agent-task-contract-design.md` — Agent Identity、Role Contract、Task Envelope 与 Git 协同模型。
[6]: `docs/design/c5-replicator-snapshot-disaster-recovery-contract.md` — CAS、backup/restore、crash recovery 和 Python/Rust 收敛现状。
[7]: `deliverables/software-company/tool_migration_matrix.json` — 当前 239 MCP 工具迁移矩阵。

---

**Handoff**

```text
from_role: reviewer
outcome: reviewer_blocked
next_role: user
next_action: 决定是否接受 P-WFL 的边界与“单 authority + 签名 Envelope + 加密 event/CAS replication”原则；接受后，由 executor 先创建 R0–R3 独立实现任务，不得直接修改已关闭的三角色治理任务。
reason: 当前 HTTP MCP 真实调用仍缺 manifest，task/lease route 仍隐式依赖本地 active SQLite，且 task-loop ledger 尚未接管所有生产 task mutation。
independence_requirement: not_applicable
```
