# Callwarden MCP 功能恢复：现有任务评审与建议任务树

**审查性质**：只读独立评审与任务树草案；未创建、拆分、修改或推进任何 CW 任务。  
**审查对象**：父任务 `T-1787203926824-9f873bfc`、三个直接子任务，以及 `deliverables/mcp-tools-implementation-map.md` 和 `tool_migration_matrix.json`。  
**评审结论**：**BLOCKED — 不建议按现有 S1 → S2 → S3 三阶段直接推进。**

> 当前最优先事项不是“把 239 个 MCP 工具全部改为 Rust”，也不是“先让 Python 客户端完全不碰 `db/`”。而是先恢复一个**可观测、单权威、可重放**的 daemon 控制面，然后以只读能力为先、按 operation class 和共享路由内核分批恢复工具。否则会把原本能本地工作的能力直接切断，却没有可靠的 daemon 替代路径。

## 1. 对现有父任务的判断

现有父任务设定为“Python 只做 MCP/CLI client，所有业务逻辑下沉 Rust daemon”，其直接子任务为：S1 将 `cli/main.py` 迁移为 daemon RPC、S2 将 79 个 compatibility 工具迁到 Rust handler、S3 下线 Python `db/` 目录。父任务当前 `open`，S1 已进入 `review`，S2/S3 尚为 `open`，但三者都没有可核验步骤记录。[1]

这个方向作为**终局愿景**没有问题；问题是它把“终局架构收敛”与“当前功能恢复”混成同一条线。映射报告显示 239 个注册工具中，只有 160 个被称为可经 HTTP 访问，另有 79 个仍属于 HTTP fail-closed 或本地 SQL 路径。[2] 而同一天生成的迁移矩阵却把全部 239 个工具都标记了 `rust_native`、`task_rpc` 或 `python_compat` target，并以 `stable`、`migrated`、`transition` 覆盖全部条目。[3] 两份真相源对“已可用”和“目标可迁移”的语义不一致，尚不能直接拿来当 S1/S2/S3 的完成证明。

| 现有项 | 评审结论 | 原因 |
|---|---|---|
| S1：CLI 迁移 + `check_client_purity=0` | **应退回并缩小** | 静态禁止 client 本地 DB 访问只能证明“断开本地路径”，不能证明同名 daemon RPC 已可用、正确或可重放。 |
| S2：79 个 compat 工具一次性 Rust 化 | **应拆分并延后** | 这 79 个全部是 read-only `python_compat`；兼容 worker 可作为恢复阶段的合法 bridge，不应先把它们一次性重写成 Rust。 |
| S3：清理 PyO3、下线 `db/` 目录 | **必须冻结** | Rust daemon 仍需 SQLite schema/存储，且当前 runtime/control-plane 未闭环。此项只能是最后的 retirement，而非恢复阶段。 |
| 239 工具全量单目标 | **不适合作为首个可交付目标** | 应以 capability registry 中“可验证可用”的工具组分批恢复，而不是先追求所有工具均为 Rust 原生。 |

`check_client_purity.py` 还明确跳过 compatibility worker 的 `_h_*` 和只读 DB 绑定逻辑，承认该 worker 是过渡期允许存在的能力。[4] 因而 S1 的“零 DB 引用”与 S2 的“79 compat 全部改 Rust”不是恢复功能的正确先后关系。

## 2. 迁移应遵循的恢复原则

新的父任务应把目标改写为：

> **在不删除 Python compatibility worker、Python `db/` 或既有可用读路径的前提下，建立 Rust daemon 的唯一外部入口与可验证 capability registry；按只读、索引写、受保护写和治理写四类能力分批切换，任何未验收工具保持明确 `disabled`，不得静默 fallback 或假装 available。**

这里的基本分组不应是一工具一子任务。239 个独立任务会造成治理开销、工作树冲突和重复验收；更可靠的切分单位是：**共享路由内核 + 同一 operation class + 同一 fixture 族 + 非重叠路径白名单**。

迁移矩阵的风险结构为 158 个 read-only、76 个 protected mutation、5 个 governance write；其中 79 个 `python_compat` 全部是 read-only。[3] 这支持一个很清晰的顺序：先完成控制面与 read-only，再处理可重算 index/async job，最后才开放会改变任务、租约、verdict、evidence 和 gate 的治理写入。

## 3. 建议的新父任务

### 建议标题

`Rust daemon MCP 恢复与渐进切换（单权威控制面 / capability 逐批上线）`

### 建议成功定义

父任务完成时，不以“删掉 Python 代码行数”作为标准，而必须同时满足：

1. daemon 能稳定自举，HTTP manifest/health/capability registry 在重启、stale PID、错误 authority 下均可诊断并 fail-closed；
2. capability registry 是单一可用性真相源，每个 `available` 工具都具备 success fixture 与 structured-error fixture；
3. 239 个 MCP 名称均在 registry 中有明确状态：`available`、`disabled` 或 `unsupported`，没有“Python 实际可用、HTTP 却不可知”的灰区；
4. 每一批已上线工具完成 MCP → Python thin shell → HTTP daemon 的真实进程级 round-trip；
5. 全部 governance write 只在 durable request ledger、assignment、lease/fencing 和 authoritative clock 完整接管后才可用；
6. Python `db/` 的删除被单列为后续 retirement 任务，且只在不存在任何 runtime 依赖后评审，不能与功能恢复并行。

## 4. 建议子任务树与执行顺序

| 序号 | 子任务标题与建议范围 | 依赖 | 首个可验收交付 | 禁止事项 |
|---:|---|---|---|---|
| R0 | **恢复基线与 capability 真相源对齐**：生成器、`mcp-tools-implementation-map.md`、`tool_migration_matrix.json`、registry status/fixture schema | 无 | 239 工具逐项产生唯一 registry row；映射与矩阵计数、backend、status、operation class 一致；unknown 不能 available | 不迁移工具业务；不改 `db/`。 |
| R1 | **daemon 自举、manifest 与只读诊断恢复**：HTTP manifest、autostart、`/health`、`/capabilities`、stale recovery 指引 | R0 | `cw task/status`、health、capabilities 在 fresh daemon 下可读；缺 manifest、stale PID、wrong authority 都返回稳定结构化错误 | 不添加 SQLite fallback；不发布生产安全声明。 |
| R2 | **治理写一致性收敛**：把实际 `TaskCollabStore` 写路径切入 durable operation ledger、MutationAuthContext、assignment→lease→fencing 校验 | R1 | commit 后 response-drop、daemon restart、同 request_id replay不重复；旧 fencing 不能经 `task.report` 等旁路写状态 | 不开放新的治理工具；不把 token 仅设计为不可恢复的一次性响应。 |
| R3 | **基础 read-only 工具组（FS + workspace + metrics）**：`file_read/list/grep/symbol_content`、workspace status/list、基础 metrics | R1 | 每个工具有 success/error fixture，MCP 与 CLI 使用同一 HTTP RPC；无 SQLite client fallback | 不处理任何写操作或 PyO3 删除。 |
| R4 | **native code-graph read-only 工具组**：callers/callees/call-chain/search/symbol/history/stats/coverage 等，按已有 `existing-native` 的 read-only 子集切分 | R3 | 每组与历史后端进行 golden-result 对照；错误语义与 timeout 固定 | 不混入 edit、job 或任务状态写。 |
| R5 | **compat read-only 工具组**：以 `P0-compat` 的 79 个只读工具分为 3–4 个按模块的 shard，由 daemon-managed Python worker 服务 | R1、R3 | worker 私有 IPC、workspace context 注入、崩溃/超时错误、读结果 fixture 通过 | 不把 worker 的 read-only 权限扩大为任何治理写；不急于 Rust 重写。 |
| R6 | **可重算 index / job 工具组**：refresh、build、scan、embed、dependency jobs，按 job type/FS mutation 分 shard | R1、R3 | job submit/status/cancel，重复 request_id 结果稳定；daemon 不可用时按操作分类处理 | 不与 task/verdict/lease 写混合；禁止长 verifier 持有 SQLite 写事务。 |
| R7 | **普通 protected mutation 工具组**：admin、edit、rule、branch 等按 `T02-admin`、`T02-edit` 拆分 | R2、R3 | 每一 shard 具备 durable idempotency、授权、负向 fixture 与 rollback 边界 | 不将 task state、lease、verdict、gate 与普通编辑 mutation 混在同一任务。 |
| R8 | **task/collaboration governance 工具组**：task lifecycle、assignment、lease、handoff、verdict、evidence、gate | R2、R3 | 所有状态改变都通过 assignment + valid lease + fencing + durable ledger；并发、断连与重启回归通过 | 不保留 `task.report` 等无 lease 旁路；HTTP dev profile 不宣称独立身份已证明。 |
| R9 | **CLI/MCP thin-shell 收敛与旧路径拒绝**：按已上线 registry row 改造 `cli/`、`server/tools/`、路由矩阵 | R3–R8 按批完成 | `check_client_purity` 作为末端静态门禁；每个工具实际 round-trip 可用，不仅 AST 零违例 | 不把尚未上线工具强行改为 HTTP available。 |
| R10 | **Python/PyO3/`db/` retirement 可行性评审**：只做依赖清单与删除条件，不删除 | R0–R9 全部完成并稳定运行 | 证明无生产 client/worker/import 依赖，给出退役 diff、回滚方案和全量回归证据 | 不在恢复父任务内删除 `db/` 或强制 Rust 化所有 compat read。 |

R0–R2 是**控制面恢复前置**，必须串行；R3、R4、R5 可在 R1 后并行，但仅在各自文件白名单不重叠时并行；R6、R7、R8 都必须依赖 R2。R9 不是一个提前迁移任务，而是每个 capability shard 已具备实测 daemon backend 后的收敛任务。R10 是独立的末期退役工程。

## 5. 对既有父任务的建议处置

不建议删除既有 `T-1787203926824-9f873bfc`，因为它记录了终局方向；但应将其定位改为**长期收敛 Epic**，而不是当前恢复执行父任务。

| 既有任务 | 建议处置 | 说明 |
|---|---|---|
| `T-1787203926824-9f873bfc` | 保留为 long-horizon Epic，冻结直接执行 | 其目标仍有价值，但需改写完成定义与依赖。 |
| `T-1787203937193-0993d120`（S1，review） | **BLOCKED / 退回重拆** | `check_client_purity` 不能替代 runtime capability fixture；只可在 R9 中按已上线批次收敛。 |
| `T-1787203937201-0a156564`（S2） | 不启动；拆为 R5 的 3–4 个 read-only compat shard | 79 项全部是 read-only，适合作为 daemon worker 过渡层，而非一次性 Rust 重写。 |
| `T-1787203937208-0a795c68`（S3） | 冻结至 R10 | 下线 Python `db/` 是 retirement，不是恢复前提。 |

## 6. 每个工具分组子任务的固定 Role Contract

每个 R3–R8 工具 shard 都应使用一致的最小任务契约：

| 字段 | 固定要求 |
|---|---|
| Scope | 只包含一个 registry batch/shard、对应 Python thin shell、Rust handler 或 compat binding、该组 fixture。 |
| 输出 | 更新 registry row、success fixture、structured-error fixture、route matrix 条目与进程级测试证据。 |
| 验收 | MCP → Python shell → HTTP → daemon/worker；daemon restart；超时；同 request_id replay（所有写工具）；无 client SQLite fallback。 |
| 角色 | Executor 只实现和记录证据；Reviewer 只读执行 fixture；Adjudicator 只在 reviewer PASS 后决定 accept 或 return。 |
| 排除 | 不触碰其他 shard、全局 `db/` 删除、任务/lease 架构、未经 R2 允许的 governance write。 |

## 7. 建议的首个可执行恢复里程碑

第一个真正可交付的里程碑应是：**“MCP 基础只读恢复 20–30 工具”**，不是“239 工具全部 Rust 化”。建议由 R0、R1、R3 的最小版本构成，优先包含：health、capabilities、workspace list/status、file read/list/grep、symbol content、get symbol、callers/callees、search symbols、stats、task status/list、lease status。它们能让 Agent 重新理解工作区、定位任务和读取设计，不会扩大治理写面。

达成此里程碑后，才把 compatibility worker 的 79 个 read-only 工具作为功能覆盖扩展；而 task/lease/verdict/evidence 等写工具必须等待 R2 的 durable operation + authorization chain 完整落地。

## References

[1]: file:///C:/git_work/callwarden/ "CW task details: T-1787203926824-9f873bfc and child tasks queried with `cw task show`"
[2]: file:///C:/git_work/callwarden/deliverables/mcp-tools-implementation-map.md "MCP implementation map, lines 1–46"
[3]: file:///C:/git_work/callwarden/deliverables/software-company/tool_migration_matrix.json "Canonical tool migration matrix, metadata and classified tool rows"
[4]: file:///C:/git_work/callwarden/scripts/check_client_purity.py "Static thin-shell gate, lines 1–23 and 45–118"

---

**Handoff:**

```text
from_role: reviewer
outcome: reviewer_blocked
next_role: executor
next_action: 经用户确认后创建“Rust daemon MCP 恢复与渐进切换”父任务，并先仅拆分 R0、R1、R2 与 R3 的最小无重叠子任务；既有 S1 退回重拆、S2/S3 冻结。
reason: 现有 S1–S3 将功能恢复、Python 薄壳静态清理和 Python DB 退役混合，且映射文档与迁移矩阵的可用性语义不一致。
independence_requirement: not_required
```
