# Callwarden Rust HTTP daemon：Lease、Fencing 与多 Agent 并发一致性只读评审

**审查角色**：reviewer / independent_reviewer  
**关联任务**：`T-1786983366974-8811ccec`  
**审查边界**：仅读取 Windows 本机仓库中的 `cw`、设计文档与源码；未执行任何任务、租约、数据库、daemon 或源码写操作。  
**结论等级**：**CHANGES REQUESTED — 当前不应将现有 HTTP daemon 路径宣称为“企业级多 Agent 状态一致性已解决”。**

> **核心结论**：当前架构已经具备正确方向的三项基础：进程内写串行化、数据库的单活 lease 约束，以及单调 fencing counter。它们足以避免一部分“同一 daemon 进程内的双活写入”。但生产实际路由仍走 `TaskCollabStore` 旧路径，而非较新的持久化 operation ledger/wrapper；因此断线重试、daemon 重启、身份与角色授权、以及所有治理写入的 fencing 覆盖仍有实质缺口。fencing 目前是局部并发互斥机制，并不是端到端的状态一致性证明。

## 1. 审查范围与运行时事实

本次先按项目约定运行了 `tokenslim workspace --format llm`，并以 Windows 本机的 Python 3.14 调用只读 `cw`。`cw task show T-1786983366974-8811ccec` 与 `cw --file …` 均因已有 HTTP manifest 指向失活 PID 而返回 `E_HTTP_MANIFEST_STALE`；该 fail-closed 行为与 HTTP 契约一致，但使任务状态无法通过活的 daemon 读取。因此本报告以本机的冻结需求、协议文档和当前源码为事实基础，而非以源代码推断为运行时已验收。

| 项目 | 已核实事实 | 评审判断 |
|---|---|---|
| HTTP 客户端发现 | manifest 的 owner/hash/authority/PID/health 失配时拒绝连接且不回退 SQLite | **正确的 fail-closed 边界**；本机 stale manifest 说明运维恢复仍需可观测且可操作。 |
| 全局写串行化 | `dispatch_rpc` 对 `PROTECTED_MUTATION_METHODS` 调用单个 `SerializationPoint` | **同一 daemon 进程内有效**，但仅是进程内 mutex，不天然覆盖第二个 daemon 进程。 |
| DB 单活 | `task_leases` 有 `(workspace_id, task_id, role) WHERE status='active'` 部分唯一索引 | **正确的最后一道防双活约束**。 |
| fencing | acquire 使用同 task+role 历史 `MAX(fencing_counter)+1`；受保护路径比较当前 counter | **数学方向正确**，但实际覆盖面与重试语义不完整。 |
| 真实 handler 路径 | `dispatch.rs` 将 `lease.*`、`task.apply/close`、verdict/evidence 路由到 `TaskCollabStore` | 需要按**旧路径**而不是仅按 `task_loop/lifecycle_lease.rs` 评价上线风险。 |

## 2. 做得正确、应保留的设计

`task_leases` 的部分唯一索引使单个 `(workspace, task, role)` 在 SQLite 层不能出现两个 `active` lease；在一条 acquire 事务中回收过期/明确 stale 的 lease 后，再以历史最大 counter 加一创建新 lease。这一组合能保证新 owner 获得更大的 fencing token，并使旧 token 在受校验的写路径上失效。[1]

daemon 分发层把 `lease.acquire|renew|release`、`task.apply|close`、`verdict.submit`、`evidence.append` 与 `gate.decide` 标记为 protected mutation，并经进程内的串行化点执行。`SerializationPoint` 在超时前不执行闭包，因而不会出现“排队超时但仍写入”的基本错误。[2] HTTP job 和 Named Pipe server 均调用 `dispatch_rpc`，没有看到其中之一显式绕过该串行化入口。[3]

旧 holder 的恢复策略也基本合理：明确失活、缺注册或 heartbeat 逾期的 active lease 才会被回收；一旦新 lease 获得更高 counter，旧 session 即使恢复，只要其 mutation 被正确验证，就会得到 `E_LEASE_FENCING_STALE`。这正是 fencing 应承担的“过期/分区客户端不能继续写”的职责。[4]

## 3. 阻断问题

### B1. 实际生产路径没有持久化、命名空间正确的 mutation 去重

HTTP Protocol v1 要求 mutation 按 `(workspace_instance_id, method, request_id)` 与 canonical params hash 去重，同 id+同参返回原结果，同 id+异参拒绝，且记录跨 daemon restart 保存。[5] 新 `task_loop/operation_store.rs` 已实现这种持久化 ledger；但实际 dispatch 仍调用 `TaskCollabStore` 的 handler，而其 `dedup_cache` 只是内存 `HashMap<String, Value>`：仅按 `request_id` 键入、最多约 1000 条后整体清空、重启即丢失，也没有 method/workspace/参数 hash 维度。[6]

这会产生三个一致性后果。第一，提交已成功但响应在连接断开时丢失，重试可能在 cache 尚未写入、cache 被清空或 daemon 重启后再次执行。第二，同一 request_id 在不同 workspace 或不同方法复用时，有机会得到另一操作的缓存结果；这与协议规定的 key 明显不一致。第三，当前 handler 多在 SQLite commit 后才调用 `save_dedup`，commit 与内存缓存之间天然存在崩溃窗口。

`lease.acquire` 使问题更严重：raw token 只在首次响应后附加，而新 wrapper 把 ledger 结果先持久化为**不含 raw token**的响应；若 commit 成功但响应丢失，安全重放不能重新取得 token，调用方既无法 renew/release，也无法携 token 做 protected mutation。旧路径只在同一进程内存 cache 尚存时可重放 token，重启后则同样失效。[4] [6]

**必须修订**：先将所有外部治理写入统一切到 `TaskMutationExecutor + OperationStore`；ledger response 与 domain mutation 必须同事务提交。对于 acquire 的一次性机密，采用“客户端提供高熵 request secret 并只存其派生/加密封装”、或“以 daemon key 加密保存可在同 key 安全重放的 token envelope”等可恢复设计；不能把“只返回一次 token”与“HTTP 断线安全重试”同时当作已满足。

### B2. `task.report` 是受保护状态写入，但未要求 lease 或 fencing

`task.report` 被列入 `PROTECTED_MUTATION_METHODS`，并可写 `change_audit`、把 step 标为 `done/failed`、创建 `fix_defect`、以及将 task 推进到 `review` 或回退 `in_progress`。[2] 但其实际 handler 只检查旧 claim 生命周期事件，并未读取 `lease_token` 或 `fencing_counter`，也未调用 `validate_lease_for_mutation`。[7]

因此，旧 executor 在其 lease 已被接管后，仍可能通过 report 写入任务状态；更糟的是，claim 已被 recover/release 时 `get_task_claim_info` 返回 `None`，report 的 owner 检查便不再构成积极授权。该写路径绕过 fencing，直接破坏“新 lease counter N 发出后，同 task/role 的低 counter protected mutation 必须拒绝”的 Requirement 1.11 / 11.8 语义。[8]

**必须修订**：为每一个会改变 task、step、Evidence、Verdict、Gate 或治理审计状态的外部方法建立强制的 `MutationAuthContext`；它必须包含 canonical workspace、method、task、role、lease token、fencing counter、holder identity、assignment revision 和 request_id。dispatch 不应只靠方法名单做串行化，还必须在领域层强制此 context。先至少补齐 `task.report`，再以测试/静态表驱动审计整个 `PROTECTED_MUTATION_METHODS` 集合。

### B3. assignment 与角色授权在代码中没有成为 lease 的前置条件

schema 已定义 `task_assignments`，需求明确 assignment 要绑定 task、role、holder identity，并要求 valid lease 之外仍检查角色授权和 Evidence Gate。[1] [8] 但对 `task_collab.rs` 和 `lifecycle_lease.rs` 的文本核验没有发现 `task_assignments` 的读取或校验。acquire 只要求调用方传入完整的 JSON identity；它既不验证此 identity 有 active assignment，也不验证 `params.role == identity.role`。`task.apply/close` 固定查询 `reviewer` lease，却只在 identity 存在时比较 agent/session/model，未强制 identity 的 role 为 reviewer。[4] [9]

这意味着任意同机调用方只要 reviewer lease 为空，就可声明自己是任意 `agent_id/session_id/model_id` 并请求 `role=reviewer` 来取得 reviewer lease；它解决的是“同时只能一个”的排他性，而不是“谁有资格取得”的授权性。fencing 无法补救这个问题，因为它会忠实保护错误授予的最新 holder。

**必须修订**：在 acquire 和每个 protected mutation 中以同一事务验证 `(workspace_id, task_id, role, agent_id, session_id, model_id, assignment_revision/status)`；role 必须是冻结枚举且与 identity/Role Contract 完全一致。assignment 创建/撤销也必须在同一 authority 域中生成可检查的 revision 或 fencing 世代。

### B4. HTTP 迁移 profile 下的 identity 不是可证明的 Agent 身份

HTTP 合约已经诚实声明 `dev_loopback_unauthenticated` 没有 OS Peer Credential，synthetic local owner 不得被当作跨用户 ACL、Attestation 或独立审核证明。[5] 但当前 HTTP handler 构造的 peer 只是 daemon owner 的 synthetic SID/UID，而 ActionIdentity 从请求 JSON 直接解析；lease holder 的匹配正依赖这组客户端自报字段。[10]

这在开发机 profile 下可接受为“同一 OS 用户、协作但不对抗”的便利模型，**不能**作为企业级多 Agent 角色隔离、独立审核或抗伪造身份机制。即使未来换回 Windows Named Pipe，OS SID 只能证明本机用户，不会自动证明某个 agent/session/model 的真实角色。

**必须修订**：明确两级安全语义。开发 HTTP 仅提供 single-user cooperative correctness，禁止产生“独立 reviewer 已证明”的强结论；企业路径需要把 agent/session identity 绑定到 daemon 验证的注册凭证（例如 daemon 签名的短期 session capability、受控 runtime 启动证明或 platform credential），并将 token、assignment 与此凭证一并核验。

## 4. 重要但次级的问题

| 优先级 | 问题 | 影响与建议 |
|---|---|---|
| 高 | `renew` 中 fencing counter 与 identity 都是 optional；`apply/close` 的 identity 同样可缺省 | 与 Requirement 11.4“必须提供当前 holder identity 与 current counter”不符。改为 schema/解析层必填，不要把 bearer token 当作唯一身份。 |
| 高 | lease token 由时间、PID 与低位时间值构成后再 SHA-256，并非 CSPRNG | 哈希不会增加熵；应使用 OS CSPRNG 生成至少 256-bit raw secret，并做恒定时间比较。 |
| 中 | 部分治理事件使用 `now_ts()` 的直接 wall clock，而 lease 用注入的 `AuthoritativeClock` | report/evidence/gate 的时间可能在系统回拨时倒退，破坏 verdict/reveal/evidence 的统一排序。所有治理时间统一从同一 clock 接口获取。 |
| 中 | 判断过期使用 `now > expires_at` 而不是 `now >= expires_at` | 到期临界点仍被当作有效；规范化为半开区间 `[acquired_at, expires_at)`。 |
| 中 | `SerializationPoint` 是 1ms 自旋互斥，不是有序队列 | 不直接造成双写，但没有 FIFO、公平性或 backlog 限制；长 verifier 若误入串行区会造成饥饿。换为有界 FIFO queue，明确 admission、deadline 与取消点。 |
| 中 | 串行化只在单 daemon 进程内成立 | 第二 daemon 进程会形成第二串行化点；必须用跨进程 singleton lock、manifest ownership 与启动竞态测试证明 HTTP 与 Named Pipe 均不能双启动。 |

## 5. 对核心问题的直接判断

**Lease + fencing 的核心机制本身没有方向性错误，但当前实现存在“授权不完整、覆盖不全、重试不耐久”的设计缺陷。**

它现在能够较可靠地处理以下窄场景：单 daemon 进程在线、同一 `(task, role)` 的 active lease 已正确授予、调用者持有未丢失 token、实际 handler 确实调用 lease validation、且请求未跨重启/断线重试。在这个前提下，新的 counter 会拒绝旧 counter 的 `apply/close/verdict/evidence/gate/handoff` 等已经接入的路径。

它**尚不能**保证以下企业目标：在断线/重启后 exactly-once 地恢复 mutation；确保旧 executor 无法通过 report 等旁路推进状态；确保 reviewer lease 只能授予已被 assignment 的真实 reviewer；或证明 HTTP/本机 peer 下不同 Agent 会话的身份与独立性。因此应把当前状态表述为：

> **“已实现部分 daemon 内排他与 stale-writer fencing；尚未完成端到端的 durable mutation protocol、assignment-bound authorization 与 attested multi-agent identity。”**

## 6. 推荐的修订顺序（先安全，后扩展）

| 顺序 | 必须完成的变更 | 可验收结果 |
|---|---|---|
| 1 | 统一外部 governance/protected mutation 到 `OperationStore`；停用 `TaskCollabStore.dedup_cache` 作为正确性机制 | 同 request_id 重启后同参重放原结果、异参拒绝；commit 后断连不重复改变状态。 |
| 2 | 将 `MutationAuthContext` 设为全部治理写入的强制输入，补齐 `task.report` 等旁路 | 旧 fencing counter 或缺 token/identity/assignment 的任一治理写入均不写 DB。 |
| 3 | 强制 assignment→role→identity→lease 的链式绑定；role 不可由请求自由声明 | executor 不能 acquire reviewer lease；撤销 assignment 后同 counter/token 立即拒绝。 |
| 4 | 以 CSPRNG、恒定时间校验和可恢复的安全响应协议重做 lease token | acquire 成功响应丢失后客户端能安全获得同一能力，且 token 不可预测。 |
| 5 | 统一 AuthoritativeClock 与全局 singleton / FIFO serialization queue | 回拨、重启、并发启动、排队超时、慢 verifier、双 daemon 都有负向测试。 |
| 6 | 最后再提升 HTTP/Named Pipe 为企业 profile | HTTP 保持明确 dev-only；企业模式具备可验证 peer/session capability 与 Attestation。 |

## 7. 最小必需回归矩阵

实施上述修改前，不建议进入“多窗口 Planner/Reviewer/Adjudicator 并行生产运行”。至少应增加以下黑盒/进程级测试：

1. 同一 `lease.acquire` 在 commit 后 response-drop、daemon restart、同 request_id retry 下只产生一个 lease，并恢复可用 credential；异参复用拒绝。
2. executor A 的 lease 被过期回收、B 获得 counter `N+1` 后，A 对 **所有**治理写方法（尤其 `task.report`）都被 `E_LEASE_FENCING_STALE` 拒绝，任务/step/evidence 不变。
3. 无 assignment、role 不匹配、identity 省略、counter 省略、token 错误、token 过期的 acquire/renew/apply/close/verdict/evidence/gate 均 fail closed。
4. 两个 HTTP client 与两个重启 daemon 实例竞争同 task/role；验证只有一个进程可服务、只有一个 active lease、所有 committed operation 可在 ledger 重放。
5. 系统时钟回拨、lease 到期边界、heartbeat stale 与网络分区恢复；确认不会复活旧 lease 或倒置审计事件时间。

## References

[1]: file:///C:/git_work/callwarden/db/schema.py "P4 lease/assignment schema and task operation ledger (lines 1574–1702)"
[2]: file:///C:/git_work/callwarden/rust_ext/src/daemon/dispatch.rs "Protected mutation list and serialization dispatch (lines 1646–1872)"
[3]: file:///C:/git_work/callwarden/rust_ext/src/daemon/server.rs "Named Pipe dispatch entry (lines 559–579)"
[4]: file:///C:/git_work/callwarden/rust_ext/src/daemon/task_collab.rs "Active lease handlers and lease validation (lines 4286–5050)"
[5]: file:///C:/git_work/callwarden/docs/design/http-daemon-mvp-compatibility-contract.md "HTTP Protocol v1 and manifest/mutation contract"
[6]: file:///C:/git_work/callwarden/rust_ext/src/daemon/task_loop/operation_store.rs "Durable dedup ledger implementation (lines 1–256)"
[7]: file:///C:/git_work/callwarden/rust_ext/src/daemon/task_collab.rs "task.report writes and status transition (lines 2143–2432)"
[8]: file:///C:/git_work/callwarden/docs/design/requirements.md "Requirements 1, 11 and 14 (lines 96–113, 309–327, 394–449)"
[9]: file:///C:/git_work/callwarden/rust_ext/src/daemon/task_loop/lifecycle_lease.rs "New wrapper lease semantics and optional identity/counter paths (lines 94–229, 305–374, 797–1299)"
[10]: file:///C:/git_work/callwarden/rust_ext/src/daemon/task_collab.rs "Client-declared ActionIdentity parsing (lines 68–117)"

---

**Handoff:**

```text
from_role: reviewer
outcome: reviewer_blocked
next_role: user
next_action: 先确定是否接受 B1–B4 为当前实现的 blocking findings；接受后应由 executor 创建并拆分持久化 operation ledger cutover、MutationAuthContext 覆盖、assignment/identity binding、CSPRNG token 与多进程回归任务。
reason: 当前 active TaskCollabStore 路径未达到目标契约要求的 durable dedupe、all-protected-mutation fencing 和可验证角色授权。
independence_requirement: not_applicable
```
