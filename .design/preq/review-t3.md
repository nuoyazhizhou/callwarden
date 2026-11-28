# T3 评审报告：task.status 治理投影 prerequisites 字段（AC-06）

- **任务**：T-1795663535104-aa05bd64（REQ-PREQ-01，冻结卡 4，AC-06）
- **评审人**：柯明察（callwarden-alm-reviewer，T3 独立会话身份）
- **评审对象**：commit 68e57a4（分支 impl/t3-status-projection，worktree impl-t3-status-43681c00，基线 40a21d4），2 files +194
- **口径**：PASS/FAIL 以 `.design/preq/contracts.md` 冻结卡 4 原文 + `.design/preq/ac-map.md` AC-06 判据为准；不采信 executor 自报结论，只认代码/diff/测试输出。

---

## 结论：PASS

68e57a4 满足冻结卡 4 全部改动点与 AC-06 判据，无 🔴 blocker。executor 自述 4 项风险经独立核验后裁处如下：测试文件白名单出入——披露足够（三渠道可追溯，与 T4 同构）；行号漂移——按符号定位全部命中，无漂移；worktree 残留 untracked 文件——💭 nit；全量 --lib 未跑——定向 3 + task_collab 190 回归已足够，不要求补跑（理由见 findings T3-04）。

---

## 一、diff 实际核验记录

### task_collab_query.rs（+19，sha256 617c877e… 独立验证 = commit blob = manifest 声明）

6 处插入，逐处核对：

1. **:106 输出 allowlist**（`res.insert` 循环，task.status 顶层拍平）：追加 `"prerequisites"`——governance 键存在即透传顶层 ✓
2. **:694 输出 allowlist**（`object.insert` 循环，status_tree 列表项顶层拍平）：追加 `"prerequisites"` ✓
3. **终态短路**（tree_governance_projection 内，先于 evaluate_next_action）：固定 JSON 补 `"prerequisites": Vec::<Value>::new()`，位于 blocking_reasons 之后 ✓（卡 4 异常路径「closed/reverted → []」）
4. **无 binding 路径**：固定 JSON 补 `[]`，blocking_reasons 显式暴露「缺少不可变 workspace binding/capture」✓
5. **策略错误路径**（Ok 分支内 get_current_task_contract_policy_state Err）：固定 JSON 补 `[]` ✓
6. **引擎 Err 分支**（evaluate_next_action Err，即卡 4 :1178「无法生成治理投影」路径）：固定 JSON 补 `[]`，错误入 blocking_reasons 不静默 ✓

### success 路径数据流核验（executor 宣称「零改动即同构」）

`tree_governance_projection`（:1023 起）核心结构：

```
终态短路（固定 JSON，含 prerequisites=[]）
  → 无 binding（固定 JSON，含 prerequisites=[]）
  → match evaluate_next_action(conn, &instance, task_id) {
        Ok(mut projection) => {
            策略状态检查（Err → 固定 JSON，含 prerequisites=[]）
            policy overlay：object.insert(identity_policy/…) / blocked_reason 时改写
              workflow_status/next_role/… —— 均不触碰 prerequisites 键
            projection  // 原样返回，prerequisites 由 40a21d4 的 evaluate_next_action 携带
        }
        Err(error) => 固定 JSON（含 prerequisites=[]）
    }
```

**success 路径的 prerequisites 由 evaluate_next_action（T2 交付 40a21d4）携带**——40a21d4 已保证其全部 8 个 outcome 构造点穿参、render 恒插键。tree_governance_projection 的 Ok 分支原样复用该 projection，policy overlay 只增改特定键不删 prerequisites。**executor 宣称成立**。

### task_collab_tests_projection.rs（+175，sha256 7f56ec93… 独立验证 = commit blob = manifest 声明）

3 个测试，断言精确无 MASKING 恒真：

1. `tree_governance_projection_lists_unmet_hard_dependency`：
   - `handle_task_status` → `governance.prerequisites` 数组 len==1，四字段精确 assert_eq（provider_task_id="t3-provider"、lifecycle_status="open"、dep_type="requires_existing"、unmet_reason="provider_not_completed"）
   - 顶层透传断言：`resp["prerequisites"] == resp["governance"]["prerequisites"]`（:106 allowlist 生效）
   - **同构核心断言**：`resp["governance"]["prerequisites"] == evaluate_next_action(...)["prerequisites"]` 逐字节一致——正是 AC-06 判据要求的「两投影对比测试」✓
2. `tree_governance_projection_closed_task_prerequisites_empty`：closed 终态（即使声明了依赖）→ `governance.prerequisites == []` 且顶层一致 ✓
3. `tree_governance_status_tree_node_carries_prerequisites`：`handle_task_status_tree` → 节点 `governance.prerequisites` len==1 + provider_task_id/unmet_reason 断言 ✓

### 契约卡 4 逐条对照

| 卡 4 条目 | 实现 | 判定 |
|---|---|---|
| tree_governance_projection 在 blocking_reasons 之后追加 prerequisites 键 | 4 个固定 JSON 路径均在 blocking_reasons 之后；success 路径由 evaluate_next_action 的 render 保证（blocking_reasons 之后、revision_hint 之前） | ✓ |
| :106/:694 若构成输出 allowlist，同步追加 | 两处均核实为 allowlist（governance 键拍平顶层），已追加 | ✓ |
| 出参与卡 3 同构（同引擎同 shape），三处投影一致（AC-06） | 同引擎（evaluate_task_prerequisites 经 evaluate_next_action）、同 shape（五字段）；测试 1 逐字节同构断言 | ✓ |
| 引擎失败 → :1178 既有 fail-closed 模式不静默 | Err 分支 blocking_reasons 带 error.message + prerequisites=[] | ✓ |
| 异常路径 closed/reverted → prerequisites==[] | 终态短路固定 `[]`，测试 2 锁定 | ✓ |

---

## 二、独立验证（不采信 executor 自报）

1. **git 位置**：68e57a4 在分支 impl/t3-status-projection，父提交即 40a21d4（基线正确）✓；worktree impl-t3-status-43681c00 HEAD = 68e57a4 ✓
2. **哈希链三方一致**：worktree 源文件 sha256 = evidence-manifest 声明 = `git show 68e57a4:<path>` commit blob sha256（query.rs 617c877e…、tests_projection.rs 7f56ec93…）——executor「commit blob 与被测源文件逐字节一致」宣称**独立验证成立** ✓
3. **证据文件 SHA-256**：AC-06.md（37075892…）、test.log（cd79f551…）、keylines（80d1312a…）全部与 manifest 声明一致 ✓
4. **executor 测试日志**：尾部原文 3 passed / 0 failed / 2150 filtered out / CARGO_RC=0，与证据声明一致 ✓
5. **reviewer 独立复跑**（worktree HEAD=68e57a4，独立 CARGO_TARGET_DIR）：
   - `cargo test tree_governance --lib` → **3 passed / 0 failed**（CARGO_RC=0）
   - `cargo test task_collab --lib` → **190 passed / 0 failed**（CARGO_RC=0）
   - 与 executor 声明一致 ✓

---

## 三、executor 自述 4 项风险裁处

1. **测试文件白名单出入**（step target_file 白名单仅含 task_collab_query.rs，changes-json 只报 query.rs）——**裁：披露足够，不构成 blocker**。理由：(a) 测试文件为配套验证代码，非生产逻辑；(b) 三渠道披露可追溯——report 文本明确列出 +175、AC-06 证据含 sha256、commit message 说明，且 reviewer 已独立验证 sha256 链；(c) 与 T4 同构（T4 测试文件 next_action_test.rs +106 同样不在白名单）；(d) 白名单准入是 daemon 设计（changes-json 只报 target_file），非 executor 过错。建议见 T3-02。
2. **行号漂移**（契约 :1017/:1030-1153/:106/:694/:1178 为快照行号）——**裁：按符号定位全部命中，无漂移**。reviewer 独立按符号核对：tree_governance_projection 函数体 4 个固定路径、:104-109/:695-700 两个 allowlist 循环、:1191 引擎 Err 分支，全部与 diff 一致。不构成 finding。
3. **worktree 残留 untracked .t3_tests_keylines.txt**——**裁：💭 nit（T3-03）**。证据副本已在主树 evidence 目录且 sha256 匹配，残留文件不在 commit、不影响被测源文件哈希链。建议清理。
4. **全量 --lib 未跑**（仅定向 3 + task_collab 190 回归，2150+ filtered out）——**裁：足够，不要求补跑（T3-04 如实登记证据等级）**。理由见 findings。

---

## 四、覆盖矩阵

| 改动点 | 调用方 | 覆盖 | 缺口 |
|---|---|---|---|
| 终态短路 +[] | task.status / status_tree | 测试 2（closed，含顶层透传断言） | reverted 终态无定向断言（与 closed 共享同一固定 JSON 模式） |
| 无 binding 路径 +[] | task.status | 代码核对（固定 JSON 字面量） | 无定向测试（需构造无 binding task） |
| 策略错误路径 +[] | task.status | 代码核对（固定 JSON 字面量） | 无定向测试（需构造 policy 读取失败） |
| 引擎 Err +[] | task.status | 代码核对（固定 JSON 字面量） | 无定向测试（需构造引擎 DB 失败） |
| :106 allowlist（status 顶层） | task.status | 测试 1 顶层透传断言 | — |
| :694 allowlist（列表项顶层） | status_tree | 测试 3 节点携带断言 | — |
| success 路径（复用 evaluate_next_action） | task.status governance | 测试 1 同构逐字节断言 | — |
| AC-06 三处一致 | status / status_tree / next_action | 测试 1（status↔next_action 逐字节）+ 测试 3（status_tree 携带） | status_tree↔next_action 无逐字节断言（由共享 tree_governance_projection 代码路径保证） |

## 五、未覆盖路径

1. 无 binding / 策略错误 / 引擎 Err 三个固定 JSON 路径无定向测试（代码核对确认字面量正确；三路径均为编译期固定 `[]`，测试价值低但非零——若未来改为动态计算则需补测试）
2. reverted 终态无定向断言（与 closed 共享同一固定 JSON 模式，风险低）
3. status_tree↔next_action 的逐字节同构断言（由共享函数保证，测试 3 只断言节点携带 + 两字段）
4. 全量 --lib（2150+ 存量测试）未由 reviewer 独立复跑——task_collab 190 回归已覆盖改动模块全部公共路径（见 T3-04）

---

## 六、Findings

- 🟡 **T3-01**（task_collab_tests_projection.rs）：无 binding / 策略错误 / 引擎 Err 三个 fail-closed 固定路径无定向测试——当前为编译期固定 `json!` 字面量（`"prerequisites": Vec::<Value>::new()`），代码核对正确；但若未来这些路径改为动态计算（如无 binding 时尝试评估依赖），无测试报警。closed 终态有测试 2 锁定，三个错误路径没有。——建议：补 1 个「无 binding task → governance.prerequisites==[] 且 blocking_reasons 非空」测试（构造最简单，其余两路径同理）。
- 💭 **T3-02**（流程，adjacent）：测试文件不在 step target_file 白名单、changes-json 不报——本次靠 report/证据/commit 三渠道披露补足，可追溯但依赖 reviewer 主动核验。建议 Planner 后续将配套测试文件纳入步骤白名单或 changes-json 报告范围，减少披露依赖。
- 💭 **T3-03**（worktree 卫生）：impl-t3-status-43681c00 残留 untracked `.t3_tests_keylines.txt`（证据副本已在主树 evidence 目录，sha256 匹配）。建议 executor 清理，避免后续会话误认为未登记变更。
- 💭 **T3-04**（证据等级，如实登记）：全量 --lib（2150+ 存量测试）未跑，仅定向 3 + task_collab 190 回归。**裁：足够**——(a) 改动面封闭：task_collab_query.rs 纯增量加键（6 处插入，无既有逻辑修改），tests_projection.rs 纯测试增量；(b) task_collab 190 回归覆盖该模块全部公共路径（task.status/status_tree 既有测试全在其中）；(c) 与 T2/T4/T5 同等等级（T2 仅 next_action 定向 37）；(d) AC-06 验收口径即「两投影对比测试」，定向测试 1 正是此设计，证据完整。
- 💭 **T3-05**（覆盖补充，adjacent）：status_tree↔next_action 无逐字节同构断言（测试 3 只断言节点携带 + 两字段）——由三处共享 tree_governance_projection/evaluate_next_action 代码路径保证，风险低。若追求完全锁定可补 status_tree 节点与 next_action 的对比断言。

---

## 七、verdict 元数据

- task_id: T-1795663535104-aa05bd64
- step_id: S-1795663535108-aa49c3d8
- 裁决：PASS（采纳 68e57a4）
- findings 数：5（🟡 1、💭 4、🔴 0）
