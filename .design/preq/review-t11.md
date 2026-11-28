# T11 独立评审报告：投影测试补强（test-only）

- **任务**：T-1795837031464-eef45a20（action=implement）
- **评审对象**：run/preq-t6-integration @ **0dc8992**（parent 78d7ddc，即 T1–T7 集成基线）
- **评审人**：reviewer-t1-t7（独立身份，instance `reviewer-t1-t7-inst` ≠ session `sess-reviewer-t1-t7-t11`）
- **评审日期**：2026-11-29
- **结论**：**PASS**（0 blocker / 0 major / 2 nit）

---

## 结论先行

T11 以纯测试方式（2 files，+189 行，零源码改动）为 T6 投影语义补齐了四类回归防线：终态短路（closed/reverted）×2、多硬依赖逐元素断言 ×1、unbound fail-closed 两层断言 ×1。红绿链语义精确——红轮 1 的 4 个失败逐条命中被测 mutation，红轮 2 精确红于 BLOCKED→COMPLETE 静默放行，绿轮 10 passed；全量 2105 passed / 1 failed（资源压力型假失败，单独重跑 ok）/ 62 filtered（恰为 snapshot_state 全部 62 个 #[test]，与 T11 改动零交集）。评审独立复跑 4 个定向测试 10 passed / RC=0，与绿轮完全一致。**结论 PASS**。

## 评审输入

| 输入 | 值 |
|---|---|
| 提交 | 0dc8992（2 files +189：next_action_test.rs +117 / task_collab_tests_projection.rs +72） |
| AC.md | sha256 10e6a390… |
| red-round1.log / red-round2.log / green-test.log / full-test.log / rerun-flaky.log / tests-keylines.txt | sha256 前缀与 evidence-manifest.md（a8075b34…）逐条匹配 |
| 治理链 | 事件 18363(open→in_progress)→18364(assignment_claimed)→18569(reported, step S-1795837031479-efd46b10)→18572(handoff_structured)；report req-bb9781a27429 |
| 租约链 | fencing 1→4，最终 released |

## 核验项逐项

### 1. 提交核验 ✓
`git show 0dc8992`：parent 恰为 78d7ddc；恰 2 个测试文件，合计 +189 行；零源码/零配置改动，全部为 `#[test]`。集成 worktree HEAD 即 0dc8992，与派工单一致。

### 2. 红轮 1 语义（4 FAILED，RC=101）✓
逐条比对，四个失败全部命中被测 mutation，无无关红：
- `next_action_prerequisites_empty_when_task_closed_terminal` 与 `..._reverted_terminal`：终态短路被摘除后 prerequisites 返回依赖数组而非 `[]`——两测试各断言失败；
- `next_action_lists_multiple_unmet_hard_dependencies`：push 后提前 break 导致 `left=1, right=3`（len 断言失败）；
- 旧版测试 3 panic 于 `E_TASK_BINDING_MISSING`——**顺带实证入口门禁真实存在且先于投影层**（为核验点①提供调用链证据）。

### 3. 红轮 2 语义 ✓
恰 1 FAILED：`tree_governance_projection_unbound_task_fail_closed_not_unmet`，`left=COMPLETE right=BLOCKED`——mutation 将 fail-closed 决策改为静默放行后被精确捕获。红点与被测语义一一对应，无过拟合断言。

### 4. 绿轮与独立复跑 ✓
- green-test.log：10 passed / 0 failed / RC=0 / 1.25s；
- 评审独立复跑（在集成 worktree，执行器遗留目标目录增量构建 1m31s）：`cargo test --lib -- next_action_prerequisites tree_governance` → **10 passed / 0 failed / 2158 filtered / 3.43s / RC=0**，与绿轮完全一致。

### 5. 全量与 snapshot_state skip（executor 核验点②）✓
- full-test.log：2105 passed / 1 failed / 62 filtered / 1436.80s；
- `snapshot_state.rs` @0dc8992 恰 **62 个 `#[test]`**，与 filtered 数吻合；总数对账 **2164+4 = 2168 = 2106+62** 闭合；
- 全量日志中 snapshot_state 零出现（skip 生效，命令 `cargo test --lib -- --skip snapshot_state` 与 tests-keylines.txt 一致）；
- 62 个测试与 T11 改动两个文件零交集；skip 系 T10-01 病灶（资源病态挂起）之环境阻塞，已在 AC.md 如实登记，非隐瞒。**零回归证明的覆盖范围不含 snapshot_state 模块**——此边界已在「未覆盖路径」明示。

### 6. decompress bomb 假失败判定（executor 核验点③）✓
- 唯一失败 `test_decompress_gz_bounded_rejects_decompression_bomb`："gzip stream error: out of memory" @workspace.rs:8629——资源压力型失败；
- 同一日志多条 60s+ 编译警告佐证全量跑期间机器高压；
- rerun-flaky.log：单独重跑 1 passed / 0.33s / RC=0；
- 该测试与 T11 改动零代码关联（不同模块，不在 4 个定向测试覆盖范围）。
**判定充分**，无需追加连续 3 次重跑（该测试非 flaky 逻辑型，而是资源压力型；且与被审改动无关）。

### 7. 测试 3 两层断言口径（executor 核验点①）✓
daemon check item 原文写 `E_WORKSPACE_AUTHORY_UNAVAILABLE`（原文如此，系笔误）；实际调用链上 unbound 任务在 `handle_task_status` 入口即被更早门禁 `E_TASK_BINDING_MISSING` 拒绝。测试的两层口径：
- **层①**：`handle_task_status` 入口返回 Err `E_TASK_BINDING_MISSING`（红轮 1 panic 已实证该门禁在位）；
- **层②**：直调 `tree_governance_projection`，断言 `decision==BLOCKED` / `workflow_status==governance_blocked` / `prerequisites==[]` / `blocking_reasons` 含 "binding"。

源码核验 @0dc8992（`task_collab_query.rs`）：binding-miss 分支（`let Some(instance) = else`）产出 `governance_blocked` / `BLOCKED` / `prerequisites: Vec::<Value>::new()` / reasons 含 binding——与断言逐字段一致；终态短路分支与引擎 Err 分支同样带 `prerequisites: []`（AC-06 语义三处齐全）。

**结论：两层口径忠实且强于 check item 字面**——它覆盖了真实调用链上的门禁（层①）与投影层兜底（层②），fail-closed 意图完全覆盖。daemon 原文的错误码笔误不构成测试缺陷（测试测的是真实行为，不是笔误）。

### 8. 治理链与证据完整性 ✓
事件序列 18363→18572 与派工单一致（18572 = handoff_structured，review→review）；report req-bb9781a27429 与 step S-1795837031479-efd46b10 绑定正确；8 份证据 sha256 前缀与 evidence-manifest.md 逐条匹配；评审身份五元组独立（instance ≠ session，未借用他人身份）。

## executor 三点核验结论（转交原文回答）

1. **测试 3 两层口径**：符合 check item 本意且更强。check item 的本意是 fail-closed（unbound 必须 BLOCKED 而非放行/unmet），两层断言分别钉住入口门禁（真实路径，`E_TASK_BINDING_MISSING`）与投影层兜底（`BLOCKED` + `prerequisites==[]`）。daemon 原文错误码 `E_WORKSPACE_AUTHORY_UNAVAILABLE` 系笔误，测试覆盖真实调用链是正确选择。💭 级措辞问题见 findings。
2. **snapshot_state 62 测试 skip**：合法且如实登记。62 恰为该模块全部测试（与 filtered 数、总数对账三方吻合），与 T11 改动零交集；skip 是 T10-01 环境病灶的隔离手段，不是掩盖。零回归声明应理解为「不含 snapshot_state 的 2106 测试全绿」。
3. **decompress bomb 假失败**：判定充分。资源压力型（out of memory）+ 同日志高压佐证 + solo 重跑 ok + 与改动零代码关联，四路证据足够，无需连续 3 次重跑。

## Findings

- 💭 **0dc8992 rust_ext/src/daemon/task_loop/next_action_test.rs 测试3注释**：注释将层②（直调 `tree_governance_projection`）描述为「沿引擎 Err 转 BLOCKED」，实际走的是 binding-miss 分支（`let Some(instance) = else` 提前返回 governance_blocked）；引擎 Err 分支存在但对 unbound 输入不可达（入口门禁先拒）。断言本身逐字段正确、fail-closed 语义完全覆盖，仅注释路径归属措辞不精确。建议后续顺手修正，不阻塞。
- 💭 **.design/preq/evidence/AC.md 行 8**：写「+71 行」而实际提交 task_collab_tests_projection.rs 为 +72 行（git show 核验）。文档漂移，不影响任何结论。

## 覆盖矩阵

| 改动点 | 调用方 | 覆盖 | 缺口 |
|---|---|---|---|
| 终态短路 prerequisites==[]（closed） | next_action 引擎 | 红轮1+绿轮（closed 终态） | 无 |
| 终态短路 prerequisites==[]（reverted） | next_action 引擎 | 红轮1+绿轮（reverted 终态） | 无 |
| 多硬依赖逐元素断言（len==3） | next_action 引擎 | 红轮1（len 1≠3）+绿轮 | 无 |
| unbound fail-closed（handle_task_status 入口） | task_collab 状态机 | 红轮1 panic 实证 + 层①断言 | 无 |
| unbound fail-closed（投影层） | tree_governance_projection | 红轮2（COMPLETE≠BLOCKED）+ 层②断言 | 无 |
| 全量回归（2106 测试） | 全 lib | full-test.log + 独立复跑 | snapshot_state 62 测试被 skip（T10-01 病灶，零交集，已登记） |
| decompress bomb（资源压力型） | workspace.rs | solo 重跑 ok | 非本次改动，仅假失败甄别 |

## 未覆盖路径

1. **snapshot_state 模块（62 测试）**：因 T10-01 资源病态被 skip，零回归证明不含该模块。与 T11 改动零交集，风险可控；该模块回归由 T10-01 后续单（T-1795847624350-48202e98）负责。
2. **引擎 Err 分支 → BLOCKED 转换**：源码存在该分支且带 `prerequisites: []`，但 unbound 输入不可达（入口门禁先拒），无直接测试。属防御性分支，💭 findings 已注。
3. **daemon check item 原文笔误（E_WORKSPACE_AUTHORY_UNAVAILABLE）**：未修正 daemon 侧文档，测试以真实调用链为准。属 adjacent 文档问题，不混入本次结论。

## 证据等级声明

- 全部关键数字（commit diff、红绿链、全量计数、62 对账、独立复跑、sha256、治理事件）均由评审独立取证或复算，未采信 executor 自报结论；
- 独立复跑使用执行器遗留的构建目标目录（增量），未重头全量构建——定向测试结果与绿轮日志逐字一致，足以支撑绿轮真实性；
- 治理数据（task_events / tasks / 租约）经 daemon SQLite 只读查询核验。

## A′ 环治理记录

| 步骤 | 值 |
|---|---|
| 身份注册 | agent_id=reviewer-t1-t7，instance=reviewer-t1-t7-inst，session=sess-reviewer-t1-t7-t11，model=workbuddy-agent |
| get_role_view | view_manifest_hash=b73d8a3fae7d87993ce3df05d8f44c0111c0931c403ff470f855cccdac7bf9f5（blind，contract_hash 20cbbba6… 匹配） |
| lease.acquire | L-9320f468346933e1，fencing=1，raw token 存档 git 外（/c/Users/wanpi/.callwarden-reviewer/lease_t11_token.txt，600） |
| verdict.submit | blind_first_pass，overall=pass，snapshot_id=0b782e8d059e5593，request_id=req-reviewer-t1-t7-t11-blind-20261129-01 → **verdict_id V-ea585adb7dd5b09fc317bec5，event id 945，replayed=false** |
| lease.release | status=released，released_at 1795885023 |

无治理事故（T10 的 WAL 陈旧读问题本次未复现，verdict 一次成功）。
