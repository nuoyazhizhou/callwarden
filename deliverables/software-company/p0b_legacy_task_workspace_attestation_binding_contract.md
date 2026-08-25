# P0-B：历史任务 workspace authority attestation / binding（supersede 前置）

**父任务：** `T-1787203926824-9f873bfc`  
**任务类型：** governance / 独立修复任务  
**创建角色：** executor / planner  
**实施角色：** executor / implementer  
**独立复审：** reviewer / independent_reviewer  
**最终收尾：** adjudicator  
**目标工作区：** `workspace_id=1`、`workspace_instance_id=ws-1`

> 目的：为历史上由旧 Python 直写路径创建、因而**从未拥有不可变 `task_workspace_binding`** 的任务提供一个正式、append-only、可审计、幂等、fail-closed 的 authority attestation/binding RPC。该能力只用于解除 `task.supersede` 的可验证前置条件；不得覆盖任何既有 binding、任务状态、描述、verdict、close 时间或历史事件。

## 1. 问题事实与范围

`task.supersede` 已正确要求被替代任务与 successor 都拥有同一 workspace authority binding。当前 `T-1787203937201-0a156564`（S2-original）为 `open` 且没有 `task_workspace_bindings` 行；A′ successor `T-1787293451688-c14b1e44` 和 S2-rebuilt 均绑定 `workspace_id=1 / ws-1`。因此，直接对 original 发起 supersede 应 fail-closed，而直接 SQLite 补写 binding 会绕过 audit、identity、lease/fencing 与 operation ledger。

本任务只实现**历史无绑定任务的补充性 attestation**。它不实现 supersede，不执行实际 supersede，不新增任何 MCP 业务工具迁移，不修改 A′ 70 个 `python_compat` scope，也不处理 `cli/main.py` 的 296 处 S1-review 引用清理。

## 2. 新能力合同

新增 daemon-native governance mutation：`task.attest_legacy_workspace_binding`。

| 参数 | 规则 |
|---|---|
| `legacy_task_id` | 必须存在，且尚无 `task_workspace_bindings` 行。已有 binding 一律拒绝，不更新或替换。 |
| `anchor_task_id` | 必须存在且已经绑定；充当历史 attestation 的授权锚点。不得等于 `legacy_task_id`。 |
| `workspace_id`、`workspace_instance_id` | 必须显式提供，并与 anchor 的不可变 binding/capture 及稳定 workspace identity 一致。 |
| `identity` | 必须是已注册的完整四字段身份，且 `role=adjudicator`。 |
| `lease_token`、`fencing_counter` | 必须是 **anchor_task_id** 上有效 reviewer lease 的真实凭证；用于在 legacy task 尚未绑定、无法自行取得 lease 时建立一次性 bootstrap governance 授权。 |
| `request_id` | 必须进入持久化 operation ledger；同 canonical 参数重放返回原结果，参数不一致拒绝。 |
| `evidence_path`、`evidence_hash` | 必须指向可复现 evidence manifest；缺失一律 fail-closed。 |

成功时必须在**一个 SQLite 事务**中完成：有效的 authority/identity/lease/fencing/domain 校验、追加或复用与 anchor 稳定 identity 相符的 `workspace_authority_capture`、插入 legacy task 的唯一 `task_workspace_binding`、写入带 actor/lease/fencing/evidence provenance 的 append-only `task_events` 审计行，以及写入可重放 operation ledger result。任务的 `status`、description、verdict、close 字段不得改变。

完成 P0-B 后，legacy task 必须仍为 `open`，但可凭其刚获得的 binding 正常取得自己的 reviewer lease；之后由独立 Adjudicator 对 legacy task 与 A′ successor 另行执行正式 `task.supersede`。

## 3. 允许路径与 Rust 目标

| 层 | 允许路径 / 函数 | 具体交付 |
|---|---|---|
| Rust domain | `rust_ext/src/daemon/task_collab.rs` | 将严格审计过的 workspace binding/capture helper 提升为受限 `pub(crate)` 复用接口；不得放宽 create 路径的 explicit authority 规则。 |
| Rust governance | `rust_ext/src/daemon/task_supersede.rs` | 新增 `handle_task_attest_legacy_workspace_binding`（或同等专用 handler）及 domain validation、operation-ledger dedupe、anchor lease/fencing 校验、append-only audit。复用 P0-H stable error 设计，不复用/复制不安全本地 SQL。 |
| Rust dispatch | `rust_ext/src/daemon/dispatch.rs` | 将精确 RPC 名称路由到 handler；未命中或不完整参数一律 fail-closed。 |
| Python thin client | `server/daemon_client.py` | 增加无本地 SQLite fallback 的 `task_attest_legacy_workspace_binding` 包装，完整透传 authority、identity、lease/fencing、request/evidence 字段。 |
| Python CLI | `cli/main.py` | 增加 `cw task attest-legacy-workspace-binding` 子命令和参数校验，只调用 daemon-native client，不得本地补写。 |
| 测试 | Rust 单测与 Python CLI/client 测试 | 覆盖正常 attestation、重放、已有 binding、跨 workspace anchor、身份缺失/角色错误、lease 缺失或 stale fencing、证据缺失、legacy/anchor 自引用、旧 task status 不变、direct local fallback 禁止。 |

## 4. 禁止范围

不得修改 SQLite schema；不得直接执行 `INSERT/UPDATE` 到用户真实数据库以补绑定；不得改旧 S2 的标题、描述、status、verdict、`closed_at` 或历史 evidence；不得在本任务中实施 `task.supersede`、`task.apply`、`task.close`；不得更改工具迁移矩阵；不得重写 CLI-01 或 S1-review scope。

## 5. 实施步骤、测试与验收

1. **Contract/route implementation。** 完成 daemon handler、受限 helper、dispatch、thin client 和 CLI 适配；所有写入仅由 daemon 在同一事务执行。
2. **正向 fixture。** 以临时 DB 构造已绑定 anchor 与无 binding legacy task；使用 registered adjudicator identity、anchor reviewer lease/fencing 和 manifest 成功 attestation。验证 legacy binding 与 anchor 的 workspace/stable identity 相符，task status 未变，audit/ledger 完整。
3. **幂等和并发语义。** 同 request replay 返回同一 attestation 结果；同 request 不同 canonical 参数被拒；并发/重试不能产生第二 binding/capture/event。
4. **负向矩阵。** 至少覆盖：已绑定 legacy、跨 workspace、anchor 未绑定、self-reference、缺失 identity、错误 role、身份未注册、lease 缺失、lease token 不符、stale fencing、证据缺失、workspace instance 不符与 daemon 不可用时 Python/CLI 无 local fallback。
5. **release evidence。** 执行合约指定测试并以 release daemon 实际 RPC round-trip 验证；记录 build/version/PID、manifest hash、关键响应与只读数据库投影。不得以静态源码或测试替代 runtime proof。

## 6. 完成与交接

Executor 的完成仅为 scope 内代码、测试和 evidence 已就绪，并以 `executor_ready_for_review` 交给独立 Reviewer。Reviewer PASS 仅可交给 Adjudicator。Adjudicator ACCEPT 后必须以真实身份与 reviewer lease/fencing 对 **P0-B 自身**执行 `apply → close → task.next_action=COMPLETE`；P0-B 的成功不自动 supersede 任何旧 S2。两个 S2 的 supersede 属于后续独立治理动作。
