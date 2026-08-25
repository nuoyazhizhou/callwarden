# A′ 流水线独立 Reviewer 复审报告（review 批次，系统级 BLOCKED）

- **审查日期：** 2026-08-23
- **Reviewer 身份：** independent_reviewer（本会话独立实例；会话 Expert `ex_cBJxOIMUkEnj`）
- **Parent 任务：** `T-1787293451688-c14b1e44`（A′ 逐链路 Rust daemon 迁移恢复，Epic）
- **审查范围：** 该 parent 下 **65 个 `review` 状态直接子任务**（CLI-01/02/03 + MCP-001..MCP-062）
- **审查方式：** 对权威任务库 `~/.callwarden/callwarden.db` 只读查询（不修改任何计划/代码/证据/任务状态）
- **权威库：** `C:\Users\wanpi\.callwarden\callwarden.db`（≈300MB，890+ 任务）

---

## 1. 身份与职责声明（强制 envelope 前置）

```text
Role: reviewer
RuntimeRole: independent_reviewer
Task: T-1787293451688-c14b1e44（取其 65 个 review 子任务）
Skill: none
Allowed: 只读核验 review 子任务的 task_steps / task_contract_revisions / task_verdict_events / task_evidence_events / task_events；输出 reviewer_pass / reviewer_blocked
Forbidden: 修改计划/代码/证据/任务状态；apply/close；创建整改步骤
Handoff: reviewer_pass → adjudicator（apply/close）；reviewer_blocked → executor
```

---

## 2. 范围确认（数据事实）

- parent `T-1787293451688-c14b1e44` 自身状态 `open`，depth=0，187 个直接子任务。
- 直接子任务状态分布：`open` 122、`review` **65**。
- 65 个 review 子任务全部 `steps=4/4 done`（action：inspect_contract / implement / test / release_verify，result 均为 "report submitted"）。
- 55/65 已存在一条 prior `pass` verdict（reviewer 身份均为 `reviewer-wb-186loop`，phase=`blind_first_pass`）；10/65 无任何 verdict。
- 0/65 已 `applied`。

---

## 3. 独立核验发现（finding，可复核）

### F1 — 证据总账全空（致命）
`task_evidence_events` 中 65 个 review 子任务的记录数 = **0 / 65**。
A′ parent 合同「必须证据」条款要求每张卡至少包含：Python 版本、Rust build/test 输出、CLI 进程输出、HTTP round-trip 输出、fail-closed 拒绝矩阵、runtime/current 与运行 PID 的 hash/manifest 对照、capability registry row。这些在权威证据总账中**无任何一条**被记录。

### F2 — 状态机流转无证据指针（致命）
65 个卡的 `task_events` 共 **456 行**流转记录，仅 **1 行** 带有非空 `evidence_path`/`evidence_hash`（实质为 0）。即 `executor → review` 的 `reported` 流转以及所有 `claimed/reported` 事件都没有挂载任何证据指针。

### F3 — prior pass verdict 为自证式空 findings（致命）
55 条 prior `pass` verdict 的 `findings` 字段 **100% 为空数组 `[]`**。其 `attestation` 仅自述 "audit chain 1000/1000 verified broken=0, reviewer lease held"，但**无任何可复核项、无 evidence_path、无 commit/file/symbol hash、无 verifier config hash**。空 findings 的 verdict 不符合 A′ 合同「Reviewer 必须独立核验」的定义，不能视为有效的独立复审。

### F4 — 实现本身疑似落地，但不可独立验证（事实，非定论）
- `git log` 显示真实提交（如 `MCP-062: get_applicable_rules -> Rust daemon native (T-1787321713038-dd021610)`），提交信息引用了任务 ID（弱溯源）。
- `rust_ext/src/daemon/http_server.rs` 确实包含 CLI-01 范围的 `health_handler` / `build_capability_registry` / `GET /health` 路由。
- 但上述均**不在权威证据总账内**，独立 Reviewer 无法通过结构化证据（commit hash、file hash、symbol hash、verifier config hash、payload hash）核验其正确性、范围收敛性与不变量。即实现"看起来做了"，但无法证明"按合同做了且只做了范围内的事"。

### F5 — 工作树存在多工作流混杂（上下文风险）
`git status` 显示未提交修改涉及 `dispatch.rs` / `task_collab.rs` / `task_loop/mod.rs` / `sqlite_query.rs` / `server/daemon_autostart.py` / `server/daemon_client.py` / `db/schema.py` 等治理与迁移文件，且有多份未跟踪的 HTML/Markdown 设计文档。这些修改疑似混入了 P0-H / P0-J 等其它活跃任务线，与 A′ 卡片的"逐卡单一 scope"原则存在交叉污染风险，进一步要求以权威证据而非口头/自述结论来定界。

---

## 4. Reviewer 裁决

**`reviewer_blocked`（系统级，覆盖全部 65 个 review 子任务）**

理由：权威证据总账为空（F1/F2），prior pass 为自证式空 findings（F3），独立 Reviewer 没有"足以限定安全路径的事实"（AGENTS.md BLOCKED 条款）。实现疑似落地但不可验证（F4），且工作树存在跨任务线混杂（F5）。在每张卡补齐结构化证据并通过独立核验前，任何卡都**不得**进入 adjudicator `apply`；prior `reviewer-wb-186loop` 的空 findings pass 不构成 review 完成。

> 注：本裁决为独立 Reviewer 实例的复审结论。要将本 verdict 持久化进 `task_verdict_events`，需为本会话/身份发放针对这些卡的 **reviewer lease**（当前 prior 用的是 `reviewer-wb-186loop` 身份，本实例不冒充）。在 lease 就绪前，本结论以本文件为权威载体。

---

## 5. 强制交接 envelope

```text
Handoff:
  from_role: reviewer
  outcome: reviewer_blocked
  next_role: executor
  next_action: >
    对 65 张 review 卡逐张通过 daemon 证据路径补齐 task_evidence_events（commit hash、
    file hash、symbol hash、verifier config hash、payload hash、evidence_path/hash 随
    task_events 流转挂载）；重新提交 review。CLI-01（control_plane Gate）作为首卡须最先
    满足，其 successor_rule 禁止在 Gate 真实 applied 前释放后继。prior 的空 findings pass
    须视为无效、不计入 review 完成。
  reason: >
    F1 证据总账 0/65；F2 456 流转仅 1 行带证据指针；F3 55 条 prior pass 的 findings 100%
    为空（自证式，非独立核验）；F4 实现疑似落地但不可验证；F5 工作树跨任务线混杂。
  independence_requirement: not_required
```

---

## 6. 给 Executor 的整改要求（daemon 将在同 parent 追加 fix_defect step）

1. **证据回填（必须，逐卡）：** 经 daemon 证据路径为每张 review 卡写入 `task_evidence_events`，并让 `task_events` 的 `reported→review` 流转携带 `evidence_path` + `evidence_hash`。
2. **Verdict 自证清理：** prior `reviewer-wb-186loop` 的空 findings pass 不得作为 review 完成依据；如需保留，须补录 findings 并经独立 Reviewer 重新核验。
3. **Gate 优先：** CLI-01（Gate/control_plane）最先补齐证据并重新 review；其 `successor_rule` 在 Gate 真实 `applied` 前禁止创建 `CLI-02/03` 及任何 MCP 首端口 Gate。
4. **范围收敛核验：** 逐卡确认实现只覆盖该卡 `port_type`/Python 入口/Rust 目标，未搭车修改 `cli/main.py` 296 处旧 S1 引用、未改 `db/schema.py`/`task_collab.rs` 治理 mutation。
5. **工作树隔离：** 提交 A′ 卡片证据前，先厘清并隔离 P0-H/P0-J 等其它任务线的未提交修改，避免交叉污染。

---

## 7. 结论摘要

- 65 张 review 卡的实现工作**疑似真实发生**（提交 + Rust 代码存在），但**证据治理完全缺失**。
- 55 条 prior pass 为**自证式空 findings**，不能视为有效独立复审。
- 独立 Reviewer 无法验证，故全批次 **reviewer_blocked**，路由 **executor** 补齐证据后重新 review。
- 在证据补齐前，adjudicator **不得**对任一卡 `apply`/`close`。
