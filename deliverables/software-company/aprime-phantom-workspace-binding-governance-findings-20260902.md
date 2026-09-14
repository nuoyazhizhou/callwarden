# A′ Phantom Workspace Binding — Governance Findings Packet

- **日期**: 2026-09-02
- **主题**: A′ 任务树 phantom workspace binding 系统性缺口（U-1 / U-5 / 全树 ws-1、ws-10）
- **触发卡**: `T-1788339177806-d7198e98`（Role Prompt v1 bootstrap recovery provenance finalization）——deliverable 层面独立 Reviewer **PASS**，但 daemon 侧 verdict 无法持久化
- **状态**: 本卡**冻结**于 review 态；本纪要约文件为治理 backlog 输入，交 planner / 治理维护路径
- **纪律**: 全程只读核验（daemon / git / 文件三方），无 SQL、无 RPC 绕过、无写操作落库

---

## 1. 结论摘要

1. `T-1788339177806-d7198e98` 交付物核验 **PASS**（15 项可核验主张全一致，见 reviewer 验证记录），**非 Executor 缺陷**。
2. 其 reviewer PASS 落库与闭环被 **authority 层系统性缺口**阻断；经全部 CLI 工具面逐一源码核查，**现行 daemon 下 phantom-bound 卡无任何 CLI 级闭环路径**。
3. 追加发现：daemon 存在 **workspace registry 与 task-DB 双存储分裂**（workspace.status/list 可见 1101；task.create/attest 的 `workspaces` 表校验无 1101、却有 id=1）——这是 U-1 及 A′ 树 phantom 问题的更深层根因，也是「治理卡也无法在 1101 下创建」的原因。

## 2. 冻结卡状态（T-1788339177806-d7198e98）

- lifecycle `review` / workflow `review_pending` / review.state `pending` verdict=-（daemon 实时投影）
- 3/3 step done；TC r1 `sha256:72d03ec0…`；Reviewer RC r1；提交链 `1373f78→b8dceb8→df81574`（base `b77de50`，白名单 4 路径，`git diff --check` rc=0）
- Reviewer 验证记录: `docs/evidence/role-prompt-v1-bootstrap-recovery-reviewer-verification-T-1788339177806-d7198e98.md`（sha256 `4c668843fc92b1a698e09b1487570e1385d59bd299f53a5e794e79db5a356e8d`）
- Executor 收尾 receipt: `docs/evidence/role-prompt-v1-bootstrap-recovery-finalization.json`（task_binding.workspace = id 1 / ws-1 / `tb-…-ws-1` / `wc-ws-1-3609283044`；`unresolved_carryover` U-1..U-5 全披露）

### 2.1 S0 补做：治理身份注册核验（2026-09-02 18:3x，只读）

**新 gap（只读面缺失）**：daemon capability 全量方法中 agent/identity 相关仅有 `get_action_identity` / `check_action_identity`（代码编辑身份，非 agent 注册表）；**`agent_registrations` 表（`storage.rs:2067`，v50 迁移目标）无任何只读查询 RPC**——`agent.register`（`dispatch.rs:2179`）是唯一 `agent.*` 方法，client 侧亦仅 `daemon_client.agent_register`。⇒ 在「禁止 SQL/通用 RPC」纪律下，**「某治理身份当前是否存活于活跃库 agent_registrations」无法实时只读核验**。建议治理 backlog 补 `agent.list`/`agent.status` 只读 RPC（写路径执行前置，见 §6 卡 B 前置）。

**文件证据（历史已注册并使用身份，receipt 佐证，非当前活性证明）**：

| agent_id | role（registered） | 佐证 |
|---|---|---|
| `exec-recover-br01-20260901`（inst `inst-exec-bc045fae` / sess `sess-exec-bc045fae`） | executor（runtime implementer） | finalization receipt `task_binding.executor_identity` |
| `adjudicator-workbuddy-p0adj-01` | adjudicator | GATE-0 receipt（attest 实操 lease `L-8abfb66aa90f3c8b`、fencing=3）；P0-B..F 收尾（项目 memory 佐证） |
| independent_reviewer（read-only） | reviewer（legacy runtime） | 本卡 verification md 记录；**具体 agent_id 未在 evidence 留痕**（当前 lease 审计为空），待 daemon 提供只读面后补核 |

**结论**：任何后续写路径（attest / supersede / verdict / apply / close）执行前，须先经授权以幂等 `agent.register` 重注册或等 `agent.list` 就绪后再核活性——本限制不得绕过（不直连 SQL）。

### 2.2 S0 补做：A′ 树 phantom-binding 卡清单（权威绑定，来源逐一标注）

`task show` 文本/JSON 投影**不含 workspace 绑定列**（v2/本卡 show 均无 workspace 字段）；绑定以 receipt `daemon_readback` / attestation receipt 为权威：

| 卡 | workspace binding（权威值） | workspace_capture | 状态（daemon 实时） | 来源 |
|---|---|---|---|---|
| Epic（GATE-0 legacy）`T-1787203926824-9f873bfc` | id=1 / ws-1（`tb-T-1787203926824-9f873bfc-ws-1`） | `wc-ws-1-355448432`（attest 后） | tree 根，下挂 MCP-00x 子卡 closed/governance_blocked | GATE-0 receipt `operation.*` |
| GATE-0 anchor = v2 `T-1788315869918-0cb69b10` | id=1 / ws-1（`tb-T-1788315869918-0cb69b10-ws-1`） | `wc-ws-1-214138640` | closed / completed | finalization `daemon_readback.v2_vehicle` + `task show` |
| v1 vehicle `T-1788253722521-3b2f8420` | id=10 / ws-10（`tb-T-1788253722521-3b2f8420-ws-10`） | `wc-ws-10-1200792964` | open / queued（未 supersede） | finalization `daemon_readback.v1_vehicle` + `supersede_request.v1_append_only_verified` + `task show` |
| 本卡 `T-1788339177806-d7198e98` | id=1 / ws-1（`tb-T-1788339177806-d7198e98-ws-1`） | `wc-ws-1-3609283044` | review / review_pending（冻结） | finalization `task_binding.workspace` + `daemon_readback.pre_claim_gate_readback` + `task show` |
| BR-03 e2e B `T-1788313854785-dd64cebc` | id=10 / ws-10（title 自带） | 未核验 | open / governance_blocked | `cw task list` |

注：Epic 下 MCP-00x 子卡批量 closed / governance_blocked，绑定未逐一核验（超本卡范围），疑同 phantom 系，列治理卡 B 影响面候选。

## 3. 门禁矩阵（CLI 层无闭环，均源码实证）

| 工具 | 门禁（源码） | 对本卡（ws-1）/ v1（ws-10） |
|---|---|---|
| `attest-legacy-workspace-binding` | already_bound：`rust_ext/src/daemon/task_supersede.rs:755-770` | 已绑定 → `E_LEGACY_BIND_ALREADY_BOUND`（GATE-0 receipt 实测该码）；attest 仅服务 **unbound** legacy 卡 |
| `bootstrap-reviewer-pass` | empty-projection：`task_loop/bootstrap_review_bridge.rs:74-98,296-298` | 有 TC/RC/step 投影 → 拒（桥仅限无投影 legacy 卡） |
| 标准 `verdict.submit` | 非空 `snapshot_id`：`task_collab_verdict.rs:26`；snapshot 依赖 registry | ws-1 在 registry 侧不可解析 → snapshot 不可建 |
| `task.supersede` | same-workspace：`task_supersede.rs:891 validate_supersede_domain` | 跨 workspace（1→1101、10→1）拒绝；registry id auto-inc，无法创建 id=1/10 的同 ws 目标卡 |
| registry 补 id=1/10 | auto-increment / binding 不可变（P0-D） | 无 seed/UPDATE 原语 |
| **创建 1101 真实绑定治理卡** | `bind_task_to_workspace` step0：`task_collab.rs:178-193` 查 task-DB `workspaces` 表 | **实测 `E_WORKSPACE_AUTHORITY_MISMATCH: task-DB 中不存在 workspace_id=1101`** |

## 4. 追加发现：workspace registry 与 task-DB 双存储分裂（根因层）

**矛盾事实**：
- `cw workspace list` / `cw daemon status 1101` → **可见** 1101 / `4baea3ff12c2ea5c`（今日 17:15 注册，registered_at 1788338130）
- `cw task create --workspace-id 1101 --workspace-instance-id 4baea3ff12c2ea5c` → **E_WORKSPACE_AUTHORITY_MISMATCH: task-DB 中不存在 workspace_id=1101**（`task_collab.rs:185-192` 查同事务 task-DB 的 `workspaces` 表）
- 同日早前 GATE-0 attest 对 `workspace_id=1/ws-1` **成功**（说明 task-DB `workspaces` 存在 id=1 行）；而 `cw daemon status 1` → `workspace_not_found: 1`（registry 侧无 id=1）

**推论**：daemon 内两个校验面读**不同存储**：
- task 事务库（tasks/contracts/bindings/`workspaces` 表）：含 A′ 历史 workspace（id=1/10…），故 create/attest 的「workspace 存在」校验可过
- workspace registry（status/list/snapshot/bootstrap 解析）：无 id=1/10、有 1101 → verdict/snapshot/bootstrap 全部 `workspace_not_found` / 不可解析

这与 U-2（runtime vs target 二进制漂移）同属 **deployment/runtime 一致性缺口**：疑似 daemon 进程持有旧 task-DB 文件句柄或 registry 与 task 库不同源。**修复须在 daemon 侧统一两存储的 workspace authority 视图**（重启到正确 DB 源 + registry 与 task-DB 同步，或加 reconciliation 原语），CLI 层不可解。

## 5. 建议能力设计方向（交评审）

- **方案甲：workspace authority reconciliation / legacy alias**——registry/解析层增加 append-only alias：binding instance ∈ {ws-1, ws-10} 且 registry 无对应条目时，映射到权威 workspace 1101/`4baea3ff12c2ea5c`（capture 引用）。不 UPDATE 历史 binding（保持 P0-D 不可变），使 verdict/snapshot/supersede 均可在 1101 authority 下通过。
- **方案乙：受审计 binding 迁移 RPC**——治理角色 + reviewer lease/fencing + 证据门禁下改写 `task_workspace_bindings` + `workspace_authority_captures` + `task_events`（与 P0-B attest 同族但允许已绑定卡迁移）。
- 评审必答：与 P0-B attest 能力边界、`E_WORKSPACE_AUTHORITY_MISMATCH` 语义、BR-01 严格 create 校验、双存储一致性的兼容；回归清单（create/attest/supersede/verdict/snapshot/bootstrap）。
- **前置**：先查清双存储分裂并统一（§4），否则任何新卡都建不到 1101。

## 6. 建议的治理卡规格（供 planner 派生，勿在本卡下直接执行）

- **卡 A（阻塞前置）**：daemon 双存储 authority 一致性修复（registry ↔ task-DB `workspaces` 同步/统一），验收 = `task create --workspace-id 1101 …` 成功
- **卡 B**：workspace authority reconciliation 能力（方案甲/乙评审→实现→部署→回归），前置含补 `agent.list`/`agent.status` 只读 RPC（§2.1）
- **卡 C（依赖 A/B）**：`T-1788339177806-d7198e98` 解除冻结 → 标准 verdict.submit → adjudicator apply/close；U-5（v1 ws-10 → v2 ws-1 supersede，当前架构性不可执行；v2 已 closed、BR-04 事实完成，v1 为孤儿 open 卡）按 B 落地后补记
- 预期收益：A′ 树 phantom 卡全量闭环；registry 无 id 的 binding 不再产生「半可写半不可写」分裂态

### 6.1 可直接粘贴输入（⚠️ 前置：卡 A 落地前 `create --workspace-id 1101` 必报 `E_WORKSPACE_AUTHORITY_MISMATCH`，属预期；以下供 A 修复后的治理会话使用）

**卡 A**（desc 写入临时文件后引用，避免 shell 转义污染）：

```bash
cat > /tmp/cardA_desc.txt <<'EOF'
# 治理卡 A — daemon workspace authority 双存储一致性修复（阻塞前置）

## 背景（权威发现，见 aprime-phantom-workspace-binding-governance-findings-20260902.md §4）
daemon 内 workspace authority 存在双存储分裂：
- task 事务库 workspaces 表：含 A′ 历史 workspace id=1/10（故 task.create/attest 的 workspace 存在性校验可过）
- workspace registry（workspace.status/list/snapshot/bootstrap 解析）：无 id=1/10、有 1101（故 verdict/snapshot/bootstrap 全 workspace_not_found）
同域疑点：U-2 runtime vs target 二进制漂移；疑似 daemon 进程持有旧 task-DB 句柄或 registry 与 task 库不同源。

## Scope（冻结）
1. 定位两存储各自的 DB 文件/连接与启动路径，确认为何分裂（可能 root：registry 重建后 task-DB 未同步、或 daemon 打开旧库）
2. 统一方案：registry 与 task-DB workspaces 同源同步（方向由设计评审定：registry 为权威 → task-DB 镜像/upsert，或反之）
3. 验收：cw task create --workspace-id 1101 --workspace-instance-id 4baea3ff12c2ea5c 成功；cw daemon status 1 与 status 1101 语义一致

## 禁止
改历史 binding 行（保持 P0-D 不可变）；直连 SQLite 做治理写；跳过 reviewer/adjudicator 门禁。
EOF
cw task create \
  --title "A' daemon workspace authority dual-store consistency repair (blocking prereq)" \
  --desc "$(cat /tmp/cardA_desc.txt)" \
  --workspace-id 1101 --workspace-instance-id 4baea3ff12c2ea5c \
  --steps '[{"action":"annotate","target_file":"docs/design/aprime-workspace-dualstore-consistency-design.md"}]'
```

**卡 B**（依赖 A）：

```bash
cat > /tmp/cardB_desc.txt <<'EOF'
# 治理卡 B — workspace authority reconciliation 能力（方案甲/乙评审→实现→部署→回归）

## 背景
A′ 树 phantom-binding 卡（清单见 governance-findings §2.2：Epic ws-1、v1/v2 ws-1、本卡 ws-1、BR-03 e2e B ws-10）在现行 daemon 下无 CLI 闭环路径（§3 门禁矩阵全实证）。目标：在 1101/4baea3ff12c2ea5c authority 下恢复 verdict/snapshot/supersede 可执行性。

## Scope（待评审冻结）
- 方案甲：registry/解析层 append-only alias（binding instance ∈ {ws-1,ws-10} 且 registry 无条目 → 映射 1101 capture），不 UPDATE 历史 binding
- 方案乙：受审计 binding 迁移 RPC（治理角色 + reviewer lease/fencing + 证据门禁下改写 bindings/captures/task_events）
- 必答评审项：与 P0-B attest 边界、E_WORKSPACE_AUTHORITY_MISMATCH 语义、BR-01 严格 create 校验、双存储一致性（依赖卡 A）兼容
- 前置：补 agent.list/agent.status 只读 RPC（身份活性核验，见 findings §2.1）
- 回归清单：create/attest/supersede/verdict/snapshot/bootstrap

## 禁止
改历史 binding 行（除非走方案乙且经评审冻结）；直连 SQLite；绕过 lease/fencing 门禁。
EOF
cw task create \
  --title "A' workspace authority reconciliation capability (option A/B + agent read-only RPC)" \
  --desc "$(cat /tmp/cardB_desc.txt)" \
  --workspace-id 1101 --workspace-instance-id 4baea3ff12c2ea5c \
  --steps '[{"action":"annotate","target_file":"docs/design/aprime-workspace-authority-reconciliation-design.md"}]'
```

**卡 C**（依赖 A/B；承接本卡 T-1788339177806-d7198e98 解冻闭环）：

```bash
cat > /tmp/cardC_desc.txt <<'EOF'
# 治理卡 C — 解除 T-1788339177806-d7198e98 冻结并按 reconciliation 闭环

## 背景
本卡 deliverable 层面独立 Reviewer PASS 成立（verification md sha256 4c668843fc92b1a698e09b1487570e1385d59bd299f53a5e794e79db5a356e8d），因 phantom binding（ws-1）verdict 无法落库而冻结于 review 态（findings §2/§3）。

## Scope（依赖卡 A/B 落地）
1. 经授权身份（见 findings §2.1）走标准 verdict.submit（reviewer lease + 非空 snapshot_id）→ adjudicator apply/close
2. U-5 补记：v1（T-1788253722521-3b2f8420, ws-10）→ v2（T-1788315869918-0cb69b10, ws-1）supersede 在 reconciliation 后按 same-workspace 语义重评（v1 为孤儿 open 卡；v2 已 closed，BR-04 事实完成）
3. 收尾 receipt 追加 supersede/close 响应（append-only）

## 禁止
bootstrap-reviewer-pass（empty-projection 门禁）；attest-legacy-workspace-binding（already_bound）；伪造 snapshot/lease/identity。
EOF
cw task create \
  --title "A' unfreeze T-1788339177806-d7198e98 and close via standard verdict (post-reconciliation)" \
  --desc "$(cat /tmp/cardC_desc.txt)" \
  --workspace-id 1101 --workspace-instance-id 4baea3ff12c2ea5c \
  --steps '[{"action":"annotate","target_file":"docs/evidence/role-prompt-v1-bootstrap-recovery-reconciliation-closure.md"}]'
```

依赖链：**A → B → C**（C 依赖 A 的 create 能力 + B 的 reconciliation 语义）。

## 7. 证据引用

- `docs/evidence/role-prompt-v1-bootstrap-recovery-finalization.json`
- `docs/evidence/role-prompt-v1-bootstrap-recovery-reviewer-verification-T-1788339177806-d7198e98.md`
- `docs/evidence/role-prompt-v1-gate0-binding-attestation-receipt.json`（already_bound_probe）
- 源码：`rust_ext/src/daemon/task_supersede.rs`（attest 616/755/891、supersede 272）、`task_loop/bootstrap_review_bridge.rs:74`、`task_collab_verdict.rs:26`、`task_collab.rs:170-193`（bind_task_to_workspace step0 查 task-DB `workspaces`）
- daemon 实时：`cw daemon health`（b77de50/pid 13632/7292）、`cw daemon status 1`（not_found）、`cw daemon status 1101`（4baea3ff12c2ea5c）
