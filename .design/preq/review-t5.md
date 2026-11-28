# T5 独立评审报告 — capability 聚合接线（prerequisite_gate_v1）

- **Role**: reviewer（柯明察）
- **Task**: T-1795663552556-ba42830c（T5 capability 接线，REQ-PREQ-03 / AC-11）
- **Skill**: code-review-cw（按独立评审纪律执行）
- **独立性**: 全新 daemon reviewer 身份 `reviewer-t5-<随机>` 系列，与 executor（`inst-t5-exec-408070ec` / `sess-t5-exec-408070ec`）的 agent_instance_id 与 session_id 均不同（daemon 注册回执见 verdict 回执消息）。
- **评审对象**: worktree `C:/git_work/callwarden-worker/runs/impl-t5-capability-408070ec`，分支 `impl/t5-capability`，基线 `origin/master=40a21d4`，commit `e7164da`
- **改动面**: 仅 `rust_ext/src/daemon/dispatch.rs`（+48/-0，1 file changed）——`handle_health` 追加 `prerequisites` 键透传 T1 的 `capability_projection()` + tests 模块 2 个新测试。`git show e7164da --stat` 确认无越界改动，**对 T4 闭环产物 claim.rs 零触碰**。
- **只读声明**: 评审过程未改代码/证据/任务状态、未建整改单、未 apply/close/supersede；daemon 阶段仅 verdict.submit 一个写操作。

---

## 结论：PASS

无 in-scope 🔴 blocker。AC-11 三键、聚合冒烟、回归断言全部核验通过；executor 两个不确定点均判定为可接受（详见下）。2 条 🟡（证据等级限制 / 契约措辞）与 1 条 💭，均不阻断。

---

## 核验事实（哈希与原文）

- 4 件证据 sha256 与 manifest 声明**逐字一致**（AC-11.md=`277fe67b…`、capability-test.log=`e32e66f4…`、keylines=`98816aeb…`、fullrun-note=`8539cc66…`，本次评审独立重算）。
- worktree `dispatch.rs` sha256=`aa3f1b7f…` 与 manifest 声明一致。
- `.t5_test.log` line 1321：`test result: ok. 16 passed; 0 failed; 0 ignored; 0 measured; 2136 filtered out`；line 1323：`CARGO_RC=0`。
- 16 个匹配测试含：2 个本卡新测试（`capability_health_projection_exposes_prerequisite_gate_v1`、`capability_health_aggregate_keeps_role_prompt_compiler_v1`）+ T1 `capability_projection_shape` + task_prompt `task_prompt_rpc_capability_projection_fields` 回归 + 12 个既有 capability 测试，全部 ok。

## AC-11 三键与 T1 声明一致性 ✅

T1 `prerequisites.rs:31-33`：`CAPABILITY_NAME="prerequisite_gate_v1"`、`CAPABILITY_SCHEMA_VERSION="prerequisite_capability_v1"`；`capability_projection()` 固定三键（name/schema_version/enabled，enabled=capability_enabled()=true）。dispatch 测试逐键断言此三值 + `prereq.len()==3`，且 T1 自身 `capability_projection_shape` 测试同轮通过——**两层一致，无漂移**。

## 聚合冒烟真实性 ✅

测试直调 `state.handle_health(make_peer())`，断言输出 `out["prerequisites"]` 为对象且三键齐——**真实覆盖 handle_health 出口**，非 mock 投影。回归测试断言 `out["role_prompt_compiler"]` 五键（name/schema_version/enabled/manifest_sha256/compiler_policy_sha256）不变，证明追加接线未破坏 RP-05 既有透传。

## MASKING 断言审查 ✅

无 `if is_err() { assert_eq!(code, X) }` 恒真模式；全部为 `assert_eq!` 精确值断言 + `.expect(...)` 兜底 + `prereq.len()==3` 键数强断言（防额外字段夹带）。声明函数无失败路径，成功路径断言合理。

## 调用图影响半径 ✅

`handle_health` 为 `DaemonStateExt` trait 默认实现（dispatch.rs:434-461），被 health RPC 调用；追加键为纯增量 insert，不改既有键、不改其他方法。`capability_projection()` 来自 T1 prerequisites.rs（基线已存在，本次未改）。影响半径封闭在 health 投影出口。

---

## executor 两个不确定点的明确判定

### 1. 键名 "prerequisites" —— 可接受，非 finding

- 契约原文（contracts.md 卡2）：「`dispatch.rs`（聚合接线，:450 附近）」「紧邻追加一行」——**未指定键名**。
- RP-05 既有模式（dispatch.rs:446-451 注释）：「capability 投影由域模块提供……dispatch 只做字段透传」，`role_prompt_compiler` 即以键值形式透传（非数组）。
- 实现选 `prerequisites` 键与 T2 next_action 投影字段、T4 门禁命名语义对齐（同一前置条件域），且与既有键值透传模式一致。
- 卡2 GWT 要点 2「capabilities 列表含该 projection」的「列表」为泛指聚合投影措辞，非 JSON 数组要求——既有 role_prompt_compiler 同样不是列表。
- **判定：键名合法，属实现自由裁量且语义对齐正确。** 建议 T7 文档（REQ-PREQ-06 amendment）固化键名（💭-1）。

### 2. 全量回归只有截断件 —— 定向证据足够支撑 AC-11，不强制补跑

- ac-map.md AC-11 验收方式：`capability 投影测试`；任务卡指定 `cargo test capability`。**定向口径证据完整**（结论行 16 passed / 0 failed + CARGO_RC=0 + 16 测试逐条 ok）。
- 全量截断件如实登记：1878 条 `... ok`、0 条 FAILED、0 条真实 panic，仅缺结论行（会话中断进程被杀）；executor **未声称全量通过**，诚实披露合规。
- 风险面评估：dispatch.rs 改动为纯增量（+48，两处 insert + 2 个新测试），零修改既有代码；截断日志已覆盖 1878 个测试逐条 ok（含 dispatch 大部分测试面）；T4 同 crate claim 57/57 绿证明编译链接健康。
- **判定：AC-11 定向证据充分，截断件不构成 blocker。** 记 🟡 证据等级限制（见 findings）。

---

## 覆盖矩阵

| 改动点 | 调用方 | 覆盖 | 缺口 |
|---|---|---|---|
| handle_health 追加 prerequisites 键 | health RPC 出口 | 接线冒烟测试（三键 + len==3）+ RP-05 回归测试 | MCP/CLI 端到端序列化未验证 |
| capability_projection() 透传 | T1 prerequisites.rs（未改） | T1 capability_projection_shape + dispatch 三键断言双层一致 | 无 |
| role_prompt_compiler 既有键 | RP-05 既有透传 | 五键回归断言 + task_prompt_rpc_capability_projection_fields | 无 |
| 2 个新测试 | dispatch::tests | 16 passed 全绿（结论行 + RC=0） | 无独立 tester 复跑（executor 自证） |

## Findings

- 🟡 **[evidence]** T5 证据为 executor 自证（无独立 tester 复跑），且全量回归仅截断件（1878 ok / 0 FAILED / 无结论行）。AC-11 定向口径证据完整，不阻断；建议后续批次由 tester 复跑定向或补跑全量，提升证据等级。
- 🟡 **[contract-wording]** 卡2 GWT 要点 2「capabilities 列表」措辞与实现（键值映射）不完全字面一致；实现与 RP-05 既有模式一致，属契约措辞非实现偏差。建议 T7 文档修订时对齐措辞并固化键名。
- 💭 **[nit]** 键名 `prerequisites` 为契约未指定的自由裁量，与 T2/T4 命名对齐合理；建议在 REQ-PREQ-06 amendment 中显式固化，防后续实现漂移。

## 未覆盖路径

1. health 投影经 MCP/CLI 端到端序列化（本次止于 handle_health Rust 出口）。
2. 全量回归结论行（截断件无 test result / CARGO_RC，1878 ok 为逐条快照）。
3. 独立 tester 复跑（T5 无 tester 取证，executor 自证）。

---

## Handoff

`PASS` → daemon verdict 补登（blind_first_pass）→ 交 adjudicator。两条 🟡 请 planner 决定是否后续处理，不阻断 T5 关单。
