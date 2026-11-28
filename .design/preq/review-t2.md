# T2 评审报告：next_action 投影 prerequisites 字段（O-1 双实现裁决）

- **任务**：T-1795663530764-a75be4a4（REQ-PREQ-01，冻结卡 3，AC-04/AC-05）
- **评审人**：柯明察（callwarden-alm-reviewer，T2 独立会话身份）
- **评审对象**：两支独立实现（O-1 风险项）
  - **40a21d4**：origin/master HEAD「feat(task-loop): next_action 投影增加 prerequisites 字段」
  - **b950e36**：分支 run/t2-parallel-b950e36（worktree run-20261126-164401-a75be4a，HEAD 链 b950e36→f1b5fbb→89e874a→59de124→f0ab6b1）
- **口径**：PASS/FAIL 以 `.design/preq/contracts.md` 冻结卡 3 原文 + `.design/preq/ac-map.md` AC-04/AC-05 判据为准；不采信 executor 自报结论，只认代码/diff/测试输出。

---

## 结论：PASS（裁决采纳 40a21d4，落选 b950e36）

40a21d4 满足冻结卡 3 全部改动点与 AC-04/AC-05 判据，无 🔴 blocker；findings 见下（全为 🟡/💭）。b950e36 可观察行为与 40a21d4 一致、测试多 2 个场景，但其签名改动点偏离冻结卡 3 字面（公共出口附加 vs inner 组装时填充），且替换 master 将破坏 T4/T5 已基于 40a21d4 建立的治理链——不予采纳，处置建议见文末。

---

## 一、两支 diff 实际核验记录

### 40a21d4（origin/master HEAD）

diff 全文已读（next_action.rs +101/-13、next_action_test.rs +106）：

1. `struct Rendered` 增 `prerequisites: Value` 字段（带 REQ-PREQ-02 doc 注释）——卡 3 签名点① ✓
2. 新增 `prerequisites_to_json(evaluation)`：仅投影 `hard_unmet`，五字段 `{provider_task_id, lifecycle_status, dep_type, unmet_reason, freshness_status}`——卡 3 出参契约 shape ✓（AC-04）
3. `render` 在 `"blocking_reasons"` 之后、`revision_hint` 之前 `m.insert("prerequisites", ...)`——卡 3 签名点③，位置逐字符合 ✓
4. `evaluate_next_action_inner` 在 `verify_capture` 之后、合同解析之前调用 `super::prerequisites::evaluate_task_prerequisites(conn, workspace_id, task_id)?`——卡 3 签名点②「在组装 Rendered 时调用」**字面符合** ✓；`?` 向上抛 = 引擎 DB 失败整体报错（fail-closed，卡 3 错误变体）✓
5. 8 个 outcome 构造点全部穿参（`wait_if_active_lease`→`wait_outcome`、`blocked`、`claim_outcome`、`wait_outcome`、`review_outcome`、`revise_outcome`、`adjudicate_outcome`，外加 `complete_outcome`/`terminal_outcome` 硬编码 `json!([])`）✓
6. 终态短路（closed/reverted）先于 binding 查询与引擎计算，硬编码 `json!([])` ✓（卡 4 closed 语义一致）
7. 4 个新增测试断言精确，无 MASKING 恒真模式：
   - `next_action_prerequisites_lists_unmet_hard_dependency`：逐字段 assert_eq 四元组（provider_task_id="t-dep"、lifecycle_status="open"、dep_type="requires_existing"、unmet_reason="provider_not_completed"）✓ AC-04
   - `next_action_prerequisites_empty_array_when_no_dependency`：assert_eq `json!([])` ✓ AC-05
   - `next_action_prerequisites_empty_when_provider_completed`：provider applied → `[]` ✓
   - `next_action_prerequisite_engine_failure_fails_whole_evaluation`：DROP TABLE task_dependencies → `expect_err` + `assert_eq!(err.code, "internal_error")` ✓ 失败分支

### b950e36（分支 run/t2-parallel-b950e36）

diff 全文已读（next_action.rs +380/-3）：

1. `struct Rendered` 增 `prerequisites: Value` 字段，8 个构造点默认 `json!([])`（占位保证字段存在）✓
2. `render` 同样在 blocking_reasons 之后、revision_hint 之前插键 ✓
3. 新增 `attach_prerequisites_projection(conn, task_id, &mut value)` 在**公共出口** `evaluate_next_action`（inner 之后、`inbound_handoff::attach_projection` 之前）附加：
   - 终态检查：`map.get("lifecycle_status")` ∈ {closed, reverted} → 保持 `[]`（不评估依赖）
   - binding 查询 `task_workspace_bindings`：无 binding → 保持 `[]`（防御性；实际 inner 已在 binding 缺失时 Err，此分支当前不可达）
   - 引擎失败 `?` 向上抛（fail-closed）✓
   - 五字段 shape 与 40a21d4 相同 ✓
4. 6 个内联测试（`#[cfg(test)] mod prerequisite_projection_tests`）全为精确 assert_eq，无恒真模式：unmet 五字段、无依赖 `[]`、satisfied `[]`、closed 终态 `[]`、多依赖 2 条、引擎失败整体报错

### 关键行为分歧点核验

| 场景 | 40a21d4 | b950e36 | 契约卡 3 要求 |
|---|---|---|---|
| 引擎调用位置 | inner 内（verify_capture 后） | 公共出口 attach | 「evaluate_next_action_inner（:1461）在组装 Rendered 时调用」→ **40a21d4 字面符合** |
| 引擎 DB 失败 | `?` 上抛，整体失败 | `?` 上抛，整体失败 | 一致 ✓ |
| 终态 closed/reverted | 短路硬编码 `[]`（先于 binding/引擎） | attach 按 lifecycle_status 拦截保持 `[]` | 一致 ✓ |
| binding 缺失（非终态） | inner 既有 `Err(E_WORKSPACE_AUTHORITY_UNAVAILABLE)`（先于引擎计算，40a21d4 未改基线行为） | attach 防御性保持 `[]`（实际不可达，inner 已 Err） | 「沿用既有 blocking_reasons 路径，prerequisites 置 []」——基线实际是 Err 而非 blocked，**两支均无法字面满足此条**，见 finding T2-05 |
| 无未满足硬依赖 | 引擎返回空 → `[]` | 引擎返回空 → `[]` | AC-05 ✓ |

---

## 二、独立验证（不采信 executor 自报）

1. **git 位置**：`git log --oneline -3 origin/master` → 40a21d4 为 origin/master HEAD ✓；分支 run/t2-parallel-b950e36 存在，commit b950e36e07d08ae2fee5b48f6bef68a25f6046d0 ✓
2. **b950e36 独立复跑**（worktree f0ab6b1，其 next_action.rs 与 b950e36 经 `git diff` 确认完全相同）：`cargo test next_action --lib` → **39 passed; 0 failed; 0 ignored**（2113 filtered out），6 个 prerequisite_projection_tests 全过 ✓
3. **40a21d4 独立复跑**（`git archive 40a21d4` 导出 rust_ext+db 到临时目录，只读不动 git）：`cargo test next_action --lib` → **37 passed; 0 failed**（与 executor 证据声明一致）
4. **证据链**：worktree docs/evidence/T-1795663530764-a75be4a4-next-action-prereq-40a21d4-verify.md 实测 SHA-256 = `2e10e1370b8bae326b664a0554d1afad0a9883a66321013327f98f36c2fd5f5c`，与 executor handoff 声明一致 ✓；b950e36 证据文件 SHA-256 = `a3baa8236f3522e0263029feec2b5069bdedfa01208536716cdb9d106911b80e` ✓
5. **治理事件**：daemon task.events 17963/17964（executor 补报）明确「权威交付物 40a21d4（origin/master，e133f34..40a21d4）」；snapshot_id=649d7703252fc19f

---

## 三、裁决理由（为何采纳 40a21d4）

1. **契约字面符合**：冻结卡 3 签名改动点②明确写「`evaluate_next_action_inner`（:1461）在组装 `Rendered` 时调用 `evaluate_task_prerequisites` 填充」。40a21d4 在 inner 内调用，逐字符合；b950e36 在公共出口附加——可观察行为一致，但属签名偏离（executor 已在证据文件中自我披露为「设计偏离说明」）。按「以 contracts.md 冻结契约原文为口径」的要求，字面符合者优先。
2. **治理连续性**：40a21d4 已在 origin/master（权威基线），T4（claim.rs 硬依赖门禁）与 T5（dispatch.rs capability 聚合）均基于它开发。采纳 b950e36 意味着重做 master 基线并使 T4/T5 的 diff 基线失效——成本高、回归风险大、且无对应收益（两支可观察行为一致）。
3. **executor 补报确认**：事件 17963/17964 明确权威交付物为 40a21d4，b950e36 作为平行实现披露二选一。
4. **证据链完整**：40a21d4 验证证据 SHA-256 实测匹配，独立复跑 37/37 通过。
5. **行数门禁**：40a21d4 改后 next_action.rs ≈1888 行（改前 1800 已超 1500 硬阈值，既有债务）；b950e36 改后 2146 行，超得更多。

**b950e36 的相对优点（如实记录）**：多 2 个测试场景（closed 终态 `[]`、多依赖多条投影），且公共出口附加模式本身是合理设计（12 条规则路径统一出口、避免约 25 处穿参）。落选不等于设计错误——见 finding T2-03 与处置建议。

---

## 四、覆盖矩阵

| 改动点 | 调用方 | 覆盖 | 缺口 |
|---|---|---|---|
| Rendered.prerequisites 字段 + render 插键 | task.next_action RPC（dispatch 层） | 4 个定向测试 + 基线 33 个 next_action 测试 | 多依赖条目、终态场景无断言（T2-01/T2-02） |
| inner 调引擎 + `?` 上抛（fail-closed） | 同上 | `next_action_prerequisite_engine_failure_fails_whole_evaluation`（DROP TABLE → internal_error） | — |
| 8 构造点穿参 | 全部 outcome 分支（claim/wait/review/revise/blocked/adjudicate/complete/terminal） | 基线 37 测试覆盖各分支可达性 | 各分支 prerequisites 值正确性仅 unmet/empty 两场景锁定 |
| complete/terminal 硬编码 `[]` | 终态短路 | 基线 closed_task_complete_none / reverted_without_contract_or_binding_is_terminal（不检查 prerequisites） | **终态 `[]` 无定向断言（T2-01）** |
| AC-05 空数组语义 | 无依赖/provider 已完成 | 2 个定向测试 | — |

## 五、未覆盖路径

1. **closed/reverted 终态投影 prerequisites==[] 的定向断言**（40a21d4 实现硬编码但无测试锁定；b950e36 有 closed_task_projects_empty_array 可摘）
2. **多条 unmet 依赖的数组投影**（len>1 与逐条断言；b950e36 有 multiple_unmet_dependencies_projected 可摘）
3. **binding 缺失场景的响应 shape**（两支均整体 Err、无 prerequisites 字段；契约措辞与基线行为不一致，见 T2-05）
4. **全量 cargo test --lib 未由 reviewer 独立复跑**（仅 next_action 定向 37/39 复跑；executor 证据声明全量 2150 通过，未独立核验）

---

## 六、Findings

- 🟡 **T2-01**（next_action_test.rs / 40a21d4）：缺「终态（closed/reverted）投影 prerequisites==[]」的定向测试——实现侧 complete_outcome/terminal_outcome 硬编码 `json!([])`，但 4 个新增测试无一覆盖终态场景；基线 closed/reverted 测试不检查 prerequisites 键。若未来终态短路被重排（如移到引擎计算之后），无测试报警。——建议：从 b950e36 摘入 `closed_task_projects_empty_array` 场景（纯测试增量，不改实现）。
- 🟡 **T2-02**（next_action_test.rs / 40a21d4）：缺「多条 unmet 依赖投影」测试——`prerequisites_to_json` 的数组多条形态（len==2 及逐条断言）无锁定，单条测试不能防「collect/迭代错误只投影首条」类回归。——建议：从 b950e36 摘入 `multiple_unmet_dependencies_projected` 场景。
- 💭 **T2-03**（b950e36 / 设计模式）：公共出口附加模式（attach_prerequisites_projection）本身合理——12 条规则路径共用同一投影出口保证字段齐全，避免在超行数门禁文件中穿参约 25 处。落选原因是契约字面 + 治理连续性，不是设计错误。若未来 next_action.rs 拆分重构，此模式可作参考。
- 💭 **T2-04**（next_action.rs / 行数门禁，adjacent）：40a21d4 后 next_action.rs ≈1888 行，超 1500 硬阈值——改前 1800 行已超，属既有债务非本次引入。建议 Planner 另立重构任务（b950e36 证据文件中的拆分建议可参考：outcome 构造器模块 / 测试文件 / 查询层三拆）。
- 💭 **T2-05**（contracts.md 卡 3 / 契约措辞，adjacent）：卡 3 异常路径「task 无 binding/workspace 无法解析 → 沿用既有 blocking_reasons 路径，prerequisites 署 []」与基线实际行为不符——基线（两支共同前提）对 binding 缺失走 `Err(E_WORKSPACE_AUTHORITY_UNAVAILABLE)`（整体失败、无 prerequisites 字段），并非 blocked outcome。两支均无法按字面满足此条；40a21d4 沿用基线 Err 路径符合「沿用既有」+ fail-closed 精神，不构成缺陷。建议 Planner 后续修订契约时澄清此条（Err 与 blocked 二选一）。

---

## 七、落选支（b950e36）处置建议

1. **分支保留归档**：run/t2-parallel-b950e36 及其 worktree 证据链（f1b5fbb 证据清单 / 89e874a ledger / 59de124 40a21d4 验证证据 / f0ab6b1 ledger）保留，不合并、不删除（本评审不动 git，交 Planner 处置）。
2. **测试摘精华**：建议后续独立小任务将 b950e36 的 2 个测试场景（closed 终态 `[]`、多依赖多条）摘入 master 的 next_action_test.rs——纯测试增量，不改实现，直接关闭 T2-01/T2-02。
3. **设计模式留档**：attach_prerequisites_projection 的统一出口模式留作 next_action.rs 未来拆分重构的参考（T2-03/T2-04）。

---

## 八、verdict 元数据

- task_id: T-1795663530764-a75be4a4
- step_id: S-1795663530767-a785ae4c
- snapshot_id: 649d7703252fc19f
- 裁决：采纳 40a21d4（origin/master），落选 b950e36（run/t2-parallel-b950e36）
- findings 数：5（🟡 2、💭 3、🔴 0）
