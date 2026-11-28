# preq — 治理 findings 记账台账（append-only）

- Feature: `preq` ／ 用途: 非 blocking 治理 findings 的登记与排期追踪（仅记账，不改在途契约/状态）
- 建账时间: 2026-11-28 01:32 UTC
- 依据 HEAD: `aacc4e6e8830e144d9e27255fc4a7991ac3833af`
- 来源: T5（`T-1795663552556-ba42830c`）adjudication 裁决 **CLOSED** 时如实登记的 3 条 non-blocking findings，主理人转交 planner 记账排期；findings 原文已固化于 `.design/preq/review-t5.md`（Findings 节），本台账不重复评审内容、只做排期追踪。
- 纪律声明: 本台账为**新增独立文件**，未改动 `contracts.md` / `task-map.md` / `ac-map.md` / `requirements-brief.md` / `symbol-anchor.md` 任何一字；未改任何 daemon 任务状态。T7（review 中）、T1/T2（评审中）、T3（实现中）的契约与状态零触碰。

---

## 批次落定触发条件（决策点）

**当前批次状态**（2026-11-28 01:32 UTC 只读核对）：

| 单 | 状态 |
|---|---|
| T1 / T2 / T7 | review / review_pending（评审闭环 task #7 in_progress，含 O-1 裁决） |
| T3 | in_progress（僵尸 lease 已由新会话 `sess-t3-exec-43681c00` 恢复重认领，实现进行中，task #6） |
| T4 | 已闭环（review + adjudication 完成） |
| T5 | **CLOSED**（本次 findings 来源） |
| T6 | open（等 T1–T5） |

**触发条件**：T1/T2/T7 评审闭环（含 O-1 裁决落定）且 T3 走完实现→review→闭环。在此之前**不立任何 amendment 单**——T7 文档正在评审中，此时修订 REQ-PREQ-06 文档或改卡 2 措辞都会作废在途评审。

---

## Findings 台账

| # | 等级 | 原文摘要（源：review-t5.md + 主理人转交） | 状态 | 处置去向 | 时机 |
|---|---|---|---|---|---|
| F-1 | 🟡 evidence | T5 证据为 executor 自证、无独立 tester 复跑；全量回归仅截断件（1878 ok / 0 FAILED / 无结论行）；AC-11 定向口径 16 passed / 0 failed / CARGO_RC=0 证据完整。建议后续批次由 tester 复跑定向或补跑全量 | 已登记，未处置 | 候选 **T10**（tester 取证单，role=tester）：复跑 `cargo test capability` 定向或补跑全量回归，补独立证据等级。**不并入 T6**——T6 是 executor 实现单（集成测试代码），与「独立 tester 复跑」角色不同，混并会削弱 F-1 要求的独立性 | 批次落定后、T8 注入前后均可；与 T6 执行期可并行（T6 交付后跑最省） |
| F-2 | 🟡 contract-wording | contracts.md 卡 2 GWT 要点 2「capabilities 列表含该 projection」措辞与实现（键值映射透传）不完全字面一致，属契约措辞非实现偏差。建议 T7 文档（REQ-PREQ-06 amendment）修订时对齐 | 已登记，未处置 | 候选 **T9**（文档修订单，REQ-PREQ-06 follow-up）：对齐卡 2 GWT 措辞（"列表"→"键值映射透传"），**不回改 contracts.md 在途评审引用**，只在 amendment 文档与后续 revision 的卡 2 中修正 | 批次落定后（T7 评审闭环后），T8 注入前立单最佳 |
| F-3 | 💭 nit | "prerequisites" 键名系契约未指定的自由裁量（实现与 T2 投影字段/T4 门禁命名语义对齐，reviewer 判定合法），建议 REQ-PREQ-06 amendment 显式固化，防后续实现漂移 | 已登记，未处置 | **并入 T9**（同文件同根因：都在 amendment 文档固化命名与措辞），合并关系在此声明：F-2 + F-3 → T9 单张，check_items 分列两条 | 同 F-2 |

## 合并关系与候选单（**均未建单**，防抢跑声明）

- **T9**（候选）：`docs/design/cw-role-handoff-task-loop-v2-amendment.md:prerequisite-gate fix 固化 prerequisites 键名并对齐卡2 措辞` —— 承接 F-2 + F-3（同文件同根因合并），REQ-PREQ-06 follow-up，action=fix，文档 only，零生产代码。
- **T10**（候选）：T5 证据独立复跑（F-1）—— role=tester，定向 `cargo test capability`（+可选全量），产出独立 evidence manifest；不并 T6。
- 两张候选单**均未在 daemon 创建**、未写入 task-map.md（避免 pre-commit 在途批次）。批次落定后由主理人裁定立单与否及先后（T8 注入前后均可）。

## 状态备注

- F-1/F-2/F-3 均 **non-blocking**，不阻断 T5 CLOSED 的效力；T5 裁决与关单不受影响。
- 本台账后续只追加不重写：每条 finding 处置完（建单/明确放弃）追加一行处置记录（日期、task_id 或「放弃理由」），不删原行。

---

## 追加记录 ② — T2 评审 findings（2026-11-28，主理人转交）

- 来源: T2（`T-1795663530764-a75be4a4`）评审 **PASS**（verdict `V-e849c2af69b79d309e931b30`，event 922），已转 adjudication_pending 待关单。5 条 non-blocking findings（2🟡+3💭）追加记账。
- 批次状态更新（以此为准，覆盖前节快照）：**T2 → adjudication_pending**；**O-1 双实现裁决已落定**——40a21d4 为权威交付，b950e36 落选归档（分支保留）；T1/T7 仍在 review；T3 自愈恢复中。触发条件不变：T1/T7 评审闭环 + T2 关单 + T3 闭环。

### Findings 台账（T2 批次）

| # | 等级 | 原文摘要（主理人转交） | 状态 | 处置去向 | 时机 |
|---|---|---|---|---|---|
| T2-01 | 🟡 | 40a21d4 缺「终态 closed/reverted 投影 prerequisites==[]」定向测试（实现硬编码 `json!([])` 但无测试锁定） | 已登记，未处置 | 候选 **T11**：从落选支 b950e36 摘「closed 终态 []」测试场景入 master `next_action_test.rs`，直接关闭本条 | 批次落定后由主理人裁定 |
| T2-02 | 🟡 | 40a21d4 缺「多条 unmet 依赖投影」测试（len>1 无断言） | 已登记，未处置 | 候选 **T11**（同上，第二场景「多依赖多条」一并摘入） | 同 T2-01 |
| T2-03 | 💭 | b950e36 出口附加模式本身合理，落选是契约字面+治理连续性原因，非设计错误 | 已登记，**无需处置**（分支保留归档即关闭，无后续动作） | 归档：分支 `run/t2-parallel-b950e36` 保留不删，供 T11 摘测试用 | 已关闭 |
| T2-04 | 💭 adjacent | next_action.rs ≈1888 行超 1500 门禁，既有债务非本次引入 | 已登记，**不处置**（不属 preq scope，避免 scope creep；记为已知代码健康债务） | 无候选单；留待独立的代码健康/重构任务（若主理人另立） | 不排期 |
| T2-05 | 💭 adjacent | 卡3「binding 缺失 → blocked+[]」措辞与基线实际（Err `E_WORKSPACE_AUTHORITY_UNAVAILABLE`）不符，两支均无法字面满足——建议契约修订时澄清 | 已登记，未处置 | **并入候选 T9**（与 F-2/F-3 同类：契约措辞澄清，同属文档/契约修订） | 同 T9（批次落定、T7 闭环后） |

### 合并关系总表（本轮更新，覆盖前节视图；候选单**均未建单**）

| 候选 | 承接 findings | 性质 | 说明 |
|---|---|---|---|
| **T9** | F-2 + F-3 + **T2-05** | 文档/契约措辞修订（REQ-PREQ-06 amendment + 卡2/卡3 措辞澄清），action=fix，零生产代码 | 同根因合并（契约措辞类），check_items 分列三条 |
| **T10** | F-1 | role=tester 独立复跑 T5 证据 | 不变；不并 T6 |
| **T11**（新增） | T2-01 + T2-02 | 测试补强（test-only）：从落选支 b950e36 摘 2 个测试场景（closed 终态 []、多依赖多条）入 master `next_action_test.rs` | 直接关闭 T2-01/T2-02；test-only，零生产逻辑改动 |

- 三张候选单**均未在 daemon 创建**、未写入 task-map.md。批次落定后由主理人统一裁定立单与否及 T8 注入前后时机。
- 纪律重申：本轮仅追加记账，未碰任何在途契约与状态（T3 自愈恢复中、T1/T7 评审中、T2 关单中）。

---

## 追加记录 ③ — T1/T7 评审 findings（2026-11-28，主理人转交）

- 来源: T1（`T-1795663519023-eb82b9a8`）评审 **PASS**（verdict `V-fdd317ab411ea0db7c934b7b`，event 923）；T7（`T-1795663574847-eaea3d24`）评审 **PASS**（verdict `V-9167998417b1b66366014a2a`，event 924）。两单均转 adjudication_pending 待关单。7 条 findings（2🟡+5💭）追加记账。
- 批次状态更新（本轮只读核验 daemon 投影）：**T2 已 closed**（lifecycle=closed / workflow=completed / decision=COMPLETE，触发条件不再含 T2）；**T1/T7 → adjudication_pending**（关单指派已发）；T3 实现中。**触发条件收敛为：T1/T7 关单 + T3 闭环。**

### Findings 台账（T1/T7 批次）

| # | 等级 | 原文摘要（主理人转交） | 状态 | 处置去向 | 时机 |
|---|---|---|---|---|---|
| T1-01 | 🟡 | 契约卡1「数据源 dependency_edges WHERE is_hard=1」与实现（task_dependencies，is_informational==0 为 hard）文字偏差——实现正确（dependency_edges 仅物化 artifact/interface 硬边且 is_hard 恒 1，requires_existing/未知类型/信息性边不可表示），偏差已 docstring 文档化 | 已登记，未处置 | **并入候选 T9**：在 .design/preq/ 追加一页契约附录（记录数据源决策与依据，不改写历史行），T9 check_items 加一条 | 同 T9（批次落定后） |
| T1-02 | 💭 | requires_artifact 双故障优先级取舍（lifecycle 未完成优先报 provider_not_completed；行缺失优先报 artifact_unresolved），契约未规定、实现取舍合理 | 已登记，**不处置** | 记录供批次 2 排序设计参考（T8 设计输入） | 批次 2 设计时引用 |
| T1-03 | 💭 | read_declarations 不按 contract_revision 过滤，旧 revision 声明行持续参与判定——与契约基线（dependency_edges INSERT OR IGNORE 无清理）语义等价，不构成回归 | 已登记，**不处置** | 无动作 | — |
| T1-04 | 💭 | docstring 引用 brief §4.4 准确（正向确认） | 已登记，**无需处置** | 无动作 | — |
| T7-01 | 🟡 | 147742e 孤儿提交——**已解决**（主理人归位）：原链经新分支 preq/t7-orphan-archive（→06a4d5b）逐字可达；内容经 preq/t7-doc-placement（基于 40a21d4，6828b2b 文档 + bcf4cfa 证据 manifest）并入 preq 线，blob 哈希与原提交逐一相同 | **已处置（关闭）** | 处置结果记录在案。本轮只读核验：两分支及 06a4d5b/6828b2b/bcf4cfa 三提交均存在，t7-doc-placement 链为 40a21d4→6828b2b→bcf4cfa，orphan-archive 链为 c5c459e→06a4d5b ✅。**连带效果：resume-plan O-2 的「T7 孤儿提交」部分已解除**（preq 实现线现含 §8 文档），O-2 余下部分（基线合并策略）仍待主理人裁定 | 已关闭 |
| T7-02 | 💭 | §8.6 异常编号与 brief §4.3 八场景矩阵重排错位风险 | 已登记，**不建单** | 下次文档维护时加「brief §4.3 #N」原编号注（可顺手并入 T9 文档修订，若届时 T9 未关） | 随 T9 或下次文档维护 |
| T7-03 | 💭 | is_hard=0 词汇 vs is_informational 列名——沿自契约卡1原文、语义无歧义 | 已登记，未处置 | **并入候选 T9**（批次 2 文档统一词汇时一并处理） | 同 T9 |

### 合并关系总表（本轮更新，覆盖前节视图；候选单**均未建单**）

| 候选 | 承接 findings | 性质 | 说明 |
|---|---|---|---|
| **T9** | F-2 + F-3 + T2-05 + **T1-01** + **T7-03**（另可顺手带 T7-02 编号注） | 文档/契约措辞与基线附录修订（REQ-PREQ-06 amendment + 卡1/卡2/卡3 措辞澄清 + 数据源决策附录），action=fix，零生产代码 | 同根因合并（契约措辞/基线类），check_items 分列五条（+可选一条） |
| **T10** | F-1 | role=tester 独立复跑 T5 证据 | 不变；不并 T6 |
| **T11** | T2-01 + T2-02 | test-only：从 b950e36 摘 2 个测试场景入 master `next_action_test.rs` | 不变 |

- 已关闭/无动作项：T7-01（已归位）、T2-03（归档）、T1-02（批次2参考）、T1-03（语义等价）、T1-04（正向确认）、T2-04（scope 外债务）。
- 四张候选单（T9/T10/T11 + 前述）**均未在 daemon 创建**、未写入 task-map.md。批次落定（T1/T7 关单 + T3 闭环）后由主理人统一裁定。
- 纪律重申：本轮仅追加记账，在途契约与状态零触碰。

---

## 追加记录 ④ — T3 评审 findings（2026-11-28，主理人转交）

- 来源: T3（`T-1795663535104-aa05bd64`）评审 **PASS**（verdict `V-6b2bd809e164a2c32bd49049`，event 929），已转 adjudication_pending 待关单。5 条 findings（1🟡+4💭）追加记账。
- 批次状态更新（本轮只读核验 daemon 投影）：**T1、T7 均已 closed**（lifecycle=closed / workflow=completed / decision=COMPLETE，与触发条件收敛一致）；T2/T4/T5 此前已 closed；**T3 → adjudication_pending**（关单指派已发）；**T6 已由 executor 领取**（lease fencing=1，raw token 按新纪律存档），集成工作树 `preq-t6-integration` 建立中。**剩余触发条件：T3 关单 + T6 全链（实现→评审→关单）。**

### Findings 台账（T3 批次）

| # | 等级 | 原文摘要（主理人转交） | 状态 | 处置去向 | 时机 |
|---|---|---|---|---|---|
| T3-01 | 🟡 | 无 binding/策略错误/引擎 Err 三个 fail-closed 固定 JSON 路径无定向测试（当前为编译期固定字面量，代码核对正确） | 已登记，未处置 | **并入候选 T11**（test-only，与 T2-01/T2-02 同类）：补 1 个无 binding 场景测试，check_items 加一条 | 批次落定后由主理人裁定 |
| T3-02 | 💭 | 测试文件白名单披露依赖——step target_file 白名单未含配套测试文件（changes-json 只报主文件），本单经三渠道披露、reviewer 裁定足够 | 已登记，**流程备忘**（不建单） | **治理流程改进**：后续任务卡的 target_file 白名单应直接纳入配套测试文件（daemon 白名单准入设计使然，非 executor 过错）。后续开卡时执行 | 下次开卡起 |
| T3-03 | 💭 | worktree impl-t3-status-43681c00 残留 untracked `.t3_tests_keylines.txt`（证据副本已在主树且 sha256 匹配）——无害 | 已登记，**不建单** | 集成时顺手清理即可 | T6 集成期 |
| T3-04 | 💭 | 全量 --lib 未跑（仅定向 3 + task_collab 190 回归）——reviewer 裁定足够（改动面封闭纯增量加键 + 190 回归覆盖该模块全部公共路径 + 与 T2/T4/T5 同等等级） | 已登记，证据等级如实记录 | **若立候选 T10**（tester 独立复跑），把 T3 全量 --lib 补跑并入 T10 check_items | 同 T10 |
| T3-05 | 💭 | status_tree↔next_action 无逐字节断言（共享代码路径保证，风险低） | 已登记，**不处置** | 无动作 | — |

### 合并关系总表（本轮更新，覆盖前节视图；候选单**均未建单**）

| 候选 | 承接 findings | 性质 | 说明 |
|---|---|---|---|
| **T9** | F-2 + F-3 + T2-05 + T1-01 + T7-03（可选带 T7-02） | 文档/契约措辞与基线附录修订，action=fix，零生产代码 | 不变（5 条 + 可选 1 条） |
| **T10** | F-1 + **T3-04** | role=tester 独立复跑：T5 `cargo test capability` 定向 + **T3 全量 --lib 补跑** | check_items 扩为两项 |
| **T11** | T2-01 + T2-02 + **T3-01** | test-only：从 b950e36 摘 2 个场景入 master `next_action_test.rs` + **补 1 个无 binding 场景测试** | check_items 扩为三项 |

- **流程备忘（非候选单）**：T3-02——后续开卡 target_file 白名单纳入配套测试文件。
- 已关闭/无动作项累计：T7-01（归位）、T2-03（归档）、T1-02（批次2参考）、T1-03、T1-04、T2-04（scope 外）、T3-03（集成期清理）、T3-05（共享路径保证）。
- 候选单**均未在 daemon 创建**、未写入 task-map.md。批次落定（T3 关单 + T6 全链）后由主理人统一裁定。
- 纪律重申：本轮仅追加记账，在途契约与状态零触碰。

---

## 追加记录 ⑤ — T6 评审 findings + 批次落定预告（2026-11-28，主理人转交）

- 来源: T6（`T-1795663571791-34b69f20`）评审 **PASS**（verdict `V-8fb9755d221a02328c571e6f`，event 933），已转 adjudication_pending 待关单。5 条 findings（1🟡+4💭）追加记账。
- 批次状态: **T6 → adjudication_pending**（关单指派已发）。关单回执到后 **7 单全部闭环**，放行判据（全 7 单到 review + 全程无 E_* + ≥1 单 apply/close 全链路）**正式满足**，进入批次落定裁定。

### Findings 台账（T6 批次）

| # | 等级 | 原文摘要（主理人转交） | 状态 | 处置去向 | 时机 |
|---|---|---|---|---|---|
| T6-01 | 🟡 | 首轮红日志未落盘（红→绿过程证据缺口，绿日志+hash+断言质量核验缓解，验收不受影响） | 已登记，**流程备忘**（与 T3-02 同类，不建单） | 流程备忘：后续测试类任务卡要求**红绿两轮日志都落盘** | 下次开测试卡起 |
| T6-02 | 💭 | 全量回归 1 个 http_server 测试 flaky（OS 资源瞬时耗尽，单独重跑通过，reviewer 裁定足够不阻断） | 已登记，未处置 | **并入候选 T10** check_items（第三项：tester 复跑 T6 全量确认稳定性）。T10 扩为**三项** | 同 T10 |
| T6-03 | 💭 | 端到端口径留档（claim_step binding 层口径正确，handle_task_claim assignment 层不经过 gate 非有效口径） | 已登记，**不处置** | 留档备查 | — |
| T6-04 | 💭 | mod 注册白名单披露 | 已登记，**与 T3-02 合并为同一条流程备忘** | 并入流程备忘 | 同 T3-02 |
| T6-05 | 💭 | worktree preq-t6-integration 残留 untracked `.t6_tests_keylines.txt`——无害 | 已登记，**不建单** | 集成收尾时清理 | T6 关单/集成收尾 |

### 合并关系总表（本轮更新，覆盖前节视图；候选单**均未建单**）

| 候选 | 承接 findings | 性质 | 说明 |
|---|---|---|---|
| **T9** | F-2 + F-3 + T2-05 + T1-01 + T7-03（可选带 T7-02） | 文档/契约措辞与基线附录修订，action=fix，零生产代码 | 不变（5 条 + 可选 1 条） |
| **T10** | F-1 + T3-04 + **T6-02** | role=tester 独立复跑：T5 capability 定向 + T3 全量 --lib + **T6 全量（flaky 稳定性确认）** | 扩为**三项** |
| **T11** | T2-01 + T2-02 + T3-01 | test-only：b950e36 摘 2 场景 + 无 binding 场景测试 | 不变（三项） |

- **流程备忘（非候选单，累计三条）**：T3-02（target_file 白名单纳入配套测试文件）+ T6-01（红绿两轮日志落盘）+ T6-04（mod 注册白名单披露，并入 T3-02 条目）。
- 无动作项累计：T7-01（归位）、T2-03（归档）、T1-02（批次2参考）、T1-03、T1-04、T2-04（scope 外）、T3-03、T3-05、T6-03（留档）、T6-05（收尾清理）。
- 候选单**均未在 daemon 创建**、未写入 task-map.md。等 T6 关单 + 主理人批次落定裁定后统一处置。
- 纪律重申：本轮仅追加记账，在途契约与状态零触碰。

---

## 追加记录 ⑥ — 批次落定处置记录（2026-11-28，主理人裁定后执行）

- **批次落定确认**: T6 关单回执已确认，批次 1 七单全部 daemon 核验 closed，放行判据（全 7 单 review + 无 E_* + ≥1 单 apply/close 全链路）**正式满足**。
- **新基线**: `run/preq-t6-integration @ 78d7ddc`（含 T1–T7 完整交付）。

### 建单回执（主理人裁定 → G6 建单）

| 单 | task_id | 内容 | 派工建议 |
|---|---|---|---|
| **T11** | `T-1795837031464-eef45a20` | test-only 投影测试补强 3 项（closed 终态 [] / 多依赖 / 无 binding fail-closed），REQ-PREQ-01 | executor，**先于 T8**（同一条 preq 线串行） |
| **T8** | `T-1795837037942-710955b4` | work_next 派工过滤（卡6，REQ-PREQ-05 should；推荐/排序 deferred 不纳入验收），blocked-by T11 | executor，T11 之后串行 |
| **T10** | `T-1795837042787-91db8edc` | tester 独立复跑 3 项（T5 capability 定向 / T3 全量 / T6 全量含 flaky test_rpc_multipart_refresh_returns_202_async），REQ-GAP | tester（纪有据），与 executor 线**并行** |

### T9 处置（不建单）

- 由 planner 直接产出契约附录工件：**`C:/git_work/callwarden/.design/preq/contracts-appendix.md`**（新增独立文件，append-only，不改写 contracts.md/ac-map.md/requirements-brief.md 历史行）。
- 承接 F-2/F-3（附录 A）、T2-05（附录 B）、T1-01（附录 C）、T7-03（附录 D）＋附带 T7-02 备注。

### 全量处置对账（25 条 findings 终态）

| finding | 终态 |
|---|---|
| F-1 | 已建单 T10 |
| F-2 / F-3 | T9 附录 A（工件） |
| T2-01 / T2-02 | 已建单 T11 |
| T2-03 | 已关闭（b950e36 归档） |
| T2-04 | 不处置（scope 外代码健康债务） |
| T2-05 | T9 附录 B（工件） |
| T1-01 | T9 附录 C（工件） |
| T1-02 / T1-03 / T1-04 | 不处置（批次2参考 / 语义等价 / 正向确认） |
| T7-01 | 已关闭（147742e 归位核验） |
| T7-02 | 附录附带备注 |
| T7-03 | T9 附录 D（工件） |
| T3-01 | 已建单 T11 |
| T3-02 | 流程备忘（后续开卡：target_file 白名单纳入配套测试文件） |
| T3-03 | T6 集成期清理 |
| T3-04 | 已建单 T10 |
| T3-05 | 不处置（共享代码路径保证） |
| T6-01 | 流程备忘（红绿两轮日志落盘） |
| T6-02 | 已建单 T10 |
| T6-03 | 留档不处置 |
| T6-04 | 流程备忘（并入 T3-02 条目） |
| T6-05 | 集成收尾清理 |

### 暂缓项（主理人裁定）

- preq 线 → 本地 master 合并（Option A）与共享 daemon 部署刷新：**等用户确认时机后再动**；主工作树由另一功能线持有且有未提交改动，禁止触碰。

---

## 追加记录 ⑦ — T10 复跑新发现 T10-01（2026-11-28，主理人转交建单）

- 来源: T10（`T-1795837042787-91db8edc`）tester 独立复跑最终报告 + manifest `docs/evidence/prerequisite_rerun_manifest.md`（sha256 145c411b…843303）。T10 三项复跑在 T6 全量环节暴露**新稳定性缺陷**，主理人裁定批次 3 注入建单。
- **T10-01（🔴 稳定性缺陷）**: 全量 `cargo test --lib` 可复现间歇病态——5 次尝试 3 次异常（STATUS_STACK_OVERFLOW 0xc00000fd / RSS 14.1GB / RSS 39 秒 35MB→18.6GB 看门狗终止）；模块隔离 `cargo test snapshot_state --lib` 同样爆炸（21.1GB，16 个 test_query_*/gc/health 挂起）；病灶锁定 **snapshot_state 注册路径**（tempdir + workspace.register + validate_owned_path_any），非跨测试干扰；与 T6-02 flaky（os error 10055/1450 资源耗尽族）疑似同根因升级版。证据日志：docs/evidence/prerequisite-rerun-t6-full-lib-attempt{1,2,3}*.log + -snapshot-state-isolated.log + 看门狗日志。
- **处置**: 已建单 **T10-01 = `T-1795847624350-48202e98`**（action=fix，3 steps：① tester 单线程定位首挂测试（与 executor T8 并行）→ ② executor 审查修复 validate_owned_path_any/tempdir 生命周期竞态 → ③ 全量回归稳定性验证 ≥3 次不复现）。REQ-GAP（无对应需求条目）；基线基于主仓 master 独立分支（避开另一功能线 resources.rs 在途改动，主工作树禁碰）；与 preq 交付线 78d7ddc 无关，不影响 T8 卡序（T8 仍等 T11 回执）。
- 状态: 已登记，已建单（批次 3）。findings 总数 26 条。

---

## 追加记录 ⑧ — T10 评审 findings（2026-11-28，主理人转交）

- 来源: T10（`T-1795837042787-91db8edc`）评审 **PASS**（reviewer-t1-t7，五项重点独立核验），2 条 💭 findings 入账。
- **治理收尾记录**: tester 五报未带 step_id 致任务未进 review——已按 reviewer 配方补 report（step `S-1795837042794-923c0f8c` + evidence 三元组与事件 18394 一致），流程缺口已闭合，T10 进入正常评审闭环。

### Findings 台账（T10 评审批次）

| # | 等级 | 原文摘要（主理人转交） | 状态 | 处置去向 | 时机 |
|---|---|---|---|---|---|
| T10-02 | 💭 | attempt2 RSS 14.1GB 数字仅 manifest 文字记载、日志无采样，不可由日志独立重建（attempt1/attempt3/隔离诊断三路已落盘，病态结论不单独依赖该数字） | 已登记，记录性不阻断 | 处置：**T10-01 定位 manifest 标注来源为 tester 外部监控**（T10-01 = `T-1795847624350-48202e98` 执行时落实） | T10-01 步骤① 执行时 |
| T10-03 | 💭 | manifest 行 17/55「projection 测试」命名欠精确（实为 `task_collab::tests::prerequisites` 模块，计数正确 194=190+4） | 已登记，记录性不阻断 | 处置：**T10 manifest 追加勘误注记**（append-only） | T10 manifest 维护时 |

- 两条均 💭 不阻断，T10 PASS 结论不受影响；处置动作分别落在 T10-01 执行期与 T10 manifest 维护期，非本台账范围（本台账仅记账）。
- findings 总数 26 → **28 条**（T10-01 稳定性缺陷 + T10-02/T10-03 评审记录性 findings）。

---

## 追加记录 ⑨ — T10 关单回执 + 三项治理观察（2026-11-28，主理人转交）

### T10 关单回执

- adjudicator（全新身份 `adjudicator-t10-dc8227bf`，fencing=3）以 verdict `V-e578422ed60d1e4087dde9be`（pass）执行 task.apply → task.close（事件 18420/18421），投影 status=closed / workflow=completed / next_role=complete。
- **批次 1 七单 + T10 全部闭环**；F-1 / T3-04 / T6-02 三项原 finding 以独立复跑证据关闭（对应 T10 单项核销）。

### 治理观察（均不阻断，登记待裁）

| # | 等级 | 内容（主理人转交） | 状态 | 处置去向 | 时机 |
|---|---|---|---|---|---|
| G-1 | 💭 governance adjacent | 证据管理流程瑕疵：manifest v1（145c411b，事件流引用值）在 verdict 提交前以 append-only 勘误追加 §9 两段（对 reviewer 两条 nit 的闭环响应），现文件 sha256=02557c4b 与事件流不一致——adjudicator 查明后采信 PASS（v1 完整保留、日志哈希不受影响、实质依据未受损） | 已登记，**已并入流程备忘** | **已升格为第四条流程备忘**（与 T3-02/T6-01/T6-04 并列），表述见追加记录 ⑩：证据文件任何变更（含 append-only 勘误）须重新 task.report 声明新哈希，或以新文件版本追加并在 report 中指明版本号；禁止静默改写已 report 引用的证据文件 | 已执行（2026-11-28 主理人裁定） |
| G-2 | 💭 daemon 基础设施缺陷候选 | daemon WAL 陈旧读：verdict 首提三度被拒（E_VERDICT_TASK_NOT_IN_REVIEW），根因 tester 收尾报落 WAL 未 checkpoint、verdict 写事务读主文件旧状态，而 autocommit 读路径可见 WAL 新帧——同 daemon 两读路径结果相悖；reviewer 以 PRAGMA wal_checkpoint(PASSIVE) 处置 | 已登记，**批次 4 候选** | **批次 4 候选，preq 收尾后与部署刷新统一裁定**（与 G-3 合并登记为 daemon 基础设施候选单，暂不立单；属 daemon 基础设施线，非 preq scope，见追加记录 ⑩） | 批次 4 候选，preq 收尾后与部署刷新统一裁定 |
| G-3 | 💭 daemon 卫生 | tester 的 executor lease 已过期（1795844589）但 lease.status 仍显示 active | 已登记，**批次 4 候选** | daemon 卫生问题；**批次 4 候选，preq 收尾后与部署刷新统一裁定**（与 G-2 合并登记为 daemon 基础设施候选单，暂不立单）；不影响裁决 | 批次 4 候选，preq 收尾后与部署刷新统一裁定 |

- 三项均不阻断，T10 闭环不受影响。G-2/G-3 属 daemon 基础设施/卫生问题（非 preq scope）；G-1 为治理流程教训（候选流程备忘第四条）。
- 本记录为纯文档追加：零 daemon 写操作、零生产代码。
