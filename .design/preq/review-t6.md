# T6 评审报告：前置条件告知端到端集成测试（批次 1 最后一单）

- **任务**：T-1795663571791-34b69f20（REQ-PREQ-01，test step S-1795663571796-3508971c）
- **评审人**：柯明察（callwarden-alm-reviewer，T6 独立会话身份）
- **评审对象**：commit 521514b（分支 run/preq-t6-integration，worktree preq-t6-integration）。集成树 = 40a21d4（T2）+ merge T3 68e57a4 + merge T4 193b928 + merge T5 e7164da + 本 commit，三支干净合并无冲突。
- **改动**：task_collab_tests_prerequisites.rs 新增 412 行 + task_collab_tests.rs mod 注册 +2 行（后者不在步骤白名单，三渠道披露）
- **口径**：以 daemon check items 四要素 + ac-map.md 判据为准；不采信 executor 自报结论，只认代码/diff/测试输出。

---

## 结论：PASS

521514b 的 4 个集成测试真实覆盖 check items 四要素，断言精确无 MASKING，三处投影同构为逐字节 assert_eq；五单改动在集成树全部在位；独立复跑 27 passed / 0 failed 与 executor 声明一致；全量回归唯一失败为 http_server 网络测试 OS 资源瞬时耗尽（单独重跑通过、与本次改动零代码依赖）——裁：证据足够，不阻断。findings 见下（🟡 1、💭 4、🔴 0）。

---

## 一、diff 与测试内容核验（412 行全文阅读）

### 4 测试 ↔ check items 四要素对照

| check item | 测试 | 断言核验 |
|---|---|---|
| ① is_hard=1 + provider=open → 三处投影含 prerequisites 且同构；claim E_PREREQUISITE_NOT_MET | `prerequisite_unmet_blocks_claim_and_three_projections_agree` | claim_step `expect_err` + `assert_eq!(err.code, ERR_PREREQUISITE_NOT_MET)`；零副作用 `bindings COUNT == 0`；status 投影 len==1 + 四字段精确 assert_eq；顶层透传一致；status_tree↔status 逐字节 assert_eq；status↔next_action 逐字节 assert_eq ✓ |
| ② provider=closed → claim 成功、投影 [] | `prerequisite_satisfied_allows_claim_and_projection_empty` | claim ok==true + `bindings COUNT == 1`（放行恰写 1 条）；status governance.prerequisites==[] + 顶层 []；next_action==[] ✓ |
| ③ capability 关闭态 → 与存量一致 | `prerequisite_gate_disabled_keeps_claim_compatible` | `claim_step_inner(..., gate_enabled=false)` 直测：硬依赖未满足仍 ok==true + bindings==1（AC-10 兼容分支端到端）✓ |
| ④ capability 投影暴露 prerequisite_gate_v1 三键 | `capability_projection_exposes_prerequisite_gate_v1` | name=="prerequisite_gate_v1"（字面）+ schema_version==CAPABILITY_SCHEMA_VERSION + enabled==true ✓ |

**MASKING 审查**：全文无 `if is_err(){assert_eq!(code,X)}` 恒真模式；无宽松 `contains` 断言；全部精确 assert_eq / expect_err / COUNT 值断言。**同构断言为 serde_json Value 结构相等（逐字节语义），非宽松比较**——测试 1 中 status↔status_tree 与 status↔next_action 两条 assert_eq 构成三处传递闭包。✓

### 端到端口径核验（executor 风险 2）

- claim 走 `task_loop::claim::claim_step`（公开 API，binding 层）——gate 挂载点即 T4 交付位置（claim.rs write_domain 步骤 6.5，binding 写入前）。`E_PREREQUISITE_NOT_MET` 常量定义于 claim.rs:51，仅 task_loop claim 路径存在 gate。
- task_collab `handle_task_claim` 为 assignment 层，**不经过 gate**——不是 gate 的有效测试口径。
- 测试 fixture 用 TaskCollabStore + task_loop 公开 API（create_task/set_task_contract/claim_step）共用于同一 DB——「claim 门禁 + 三处投影」真集成。
- **裁：口径与 check items 意图一致** ✓（e2e.md 第 7 节已如实登记此口径限定）。

### 五单改动在位抽查（集成树 HEAD=521514b）

| 单 | 文件 | 在位证据 |
|---|---|---|
| T1 引擎 | task_loop/prerequisites.rs | 存在，evaluate_task_prerequisites/capability_projection 20 处 ✓ |
| T2 next_action | task_loop/next_action.rs | prerequisites 50 处 ✓ |
| T3 status 投影 | task_collab_query.rs | prerequisites 9 处 ✓ |
| T4 claim 门禁 | task_loop/claim.rs | ERR_PREREQUISITE_NOT_MET :51/:272/:299 ✓ |
| T5 capability 接线 | dispatch.rs | :454-458 capability_projection 透传 + 测试 ✓ |

---

## 二、独立验证（不采信 executor 自报）

1. **git 位置**：521514b 在分支 run/preq-t6-integration，merge 链完整（40a21d4→68e57a4→193b928→b4d30e6→e7164da→d550a20→521514b）；worktree HEAD = 521514b ✓
2. **哈希链三方一致**：worktree 源文件 sha256 = evidence-manifest 声明 = commit blob sha256（tests_prerequisites.rs adf6fa60…、task_collab_tests.rs 9f474de4…）✓
3. **证据文件 SHA-256**：e2e.md（e508e7d8…）、e2e-test.log（ea792361…）、full-test.log（003450bd…）、rerun-flaky.log（c26d9eaf…）、keylines（b3640fd3…）、manifest（14860c44…，与 handoff 声明一致）全部匹配 ✓
4. **reviewer 独立复跑**（worktree HEAD=521514b，独立 CARGO_TARGET_DIR）：`cargo test prerequisites --lib` → **27 passed / 0 failed**（CARGO_RC=0，2137 filtered out），4 个新测试全绿——与 executor 声明一致 ✓
5. **全量日志核验**：2163 passed / 1 failed（443.79s）；唯一失败 `http_server::tests::test_rpc_multipart_refresh_returns_202_async`，panic 栈在 tokio I/O driver（os error 10055 系统缓冲区不足 / 1450 系统资源不足），非断言失败；同日志多个测试「running for over 60 seconds」佐证资源压力；单独重跑 1 passed / CARGO_RC=0 ✓

---

## 三、executor 自述 4 项风险裁处

1. **全量 1 个 http_server 网络测试 flaky**——**裁：证据足够，不阻断 T6 关单**。理由：(a) 失败是 OS 层资源瞬时耗尽（accept failed 10055 + I/O driver polling panic 1450），非逻辑断言失败；(b) 与本次改动零代码依赖（http_server 网络层 vs task_collab 纯测试文件 + mod 注册）；(c) 单独重跑通过（绿证据在盘 hash 验证）；(d) 同批其他网络测试（含同模块 multipart/tls 系列）在压力下均通过。建议：交 tester 在 T10 或后续批次复跑全量确认稳定性（💭 T6-02，候选 T10 已有此意向）。
2. **端到端口径**（claim_step binding 层 vs handle_task_claim assignment 层）——**裁：口径与 check items 意图一致**（见上文核验）。handle_task_claim 不经过 gate，claim_step 是唯一有效口径；e2e.md 第 7 节已如实登记。💭 T6-03 留档。
3. **mod 注册文件不在白名单**（task_collab_tests.rs +2 行）——**裁：披露足够**（与 T3 同构：report 文本 + e2e.md + commit message 三渠道，sha256 链 reviewer 独立验证；白名单准入是 daemon 设计非 executor 过错）。💭 T6-04 与 T3-02 合并建议。
4. **红→绿中间态已如实登记**——e2e.md 第 6 节文字登记（首轮 3 测试红于 E_WORKSPACE_AUTHORITY_MISMATCH，fixture 假 hash 与 seed_workspace capture 链不一致，修为逐字节一致后全绿）。**但首轮红日志原始输出未落盘**（证据目录 6 个文件无红日志）→ 🟡 T6-01。

---

## 四、覆盖矩阵

| check item | 测试覆盖 | 缺口 |
|---|---|---|
| ① 阻断 + 三处同构 | 测试 1：E_PREREQUISITE_NOT_MET + 零副作用 + 四字段精确 + 三处逐字节 assert_eq | — |
| ② 放行 + 空数组 | 测试 2：ok + bindings==1 + status/顶层/next_action 全 [] | — |
| ③ 兼容分支 | 测试 3：claim_step_inner(false) 放行 | — |
| ④ capability 三键 | 测试 4：name/schema_version/enabled 精确断言 | — |
| 全量回归 | full-test.log 2163 passed / 1 flaky（单独重跑通过） | 未做第三轮全量复跑（T6-02） |
| 红→绿过程证据 | e2e.md 第 6 节文字登记 | 首轮红日志原始输出未落盘（T6-01） |

## 五、未覆盖路径

1. **首轮红日志原始输出**（红→绿链路仅文字自述，无法独立验证红先于绿发生——T6-01）
2. **全量第三轮复跑**（flaky 稳定性未二次确认——T6-02，建议交 tester）
3. 信息性依赖（is_hard=false）claim 放行 + warning 路径不在本卡四要素（T4 定向测试已覆盖 AC-09，非本单缺口）

---

## 六、Findings

- 🟡 **T6-01**（证据链 / 红绿中间态）：首轮红日志原始输出未落盘——e2e.md 第 6 节仅文字登记「首轮 3 测试红于 E_WORKSPACE_AUTHORITY_MISMATCH」，证据目录 6 个文件（e2e.md/e2e-test.log/full-test.log/rerun-flaky.log/keylines/manifest）无红日志。红→绿链路无法独立验证红确实先于绿发生（单 commit 交付，git 无中间态）。缓解：绿日志在盘 + hash 验证 + 4 测试断言质量独立核验（精确、非恒真），最终验收不受影响。——建议：后续测试单交付时将红→绿两轮日志都落盘登记（红日志是 TDD 过程证据）。
- 💭 **T6-02**（全量 flaky，adjacent）：http_server test_rpc_multipart_refresh_returns_202_async 全量并行 443s 时 OS 资源瞬时失败（10055/1450），单独重跑通过，与本次改动零代码依赖。裁：证据足够不阻断。建议交 tester 在 T10 或后续批次复跑全量确认稳定性。
- 💭 **T6-03**（端到端口径留档）：claim 门禁集成口径 = task_loop claim_step（binding 层，gate 挂载点），非 handle_task_claim（assignment 层不经过 gate）。口径与 check items 意图一致，e2e.md 第 7 节已登记。后续同类集成测试沿用此口径。
- 💭 **T6-04**（白名单披露，adjacent）：mod 注册文件 task_collab_tests.rs 不在步骤 target_file 白名单——与 T3 同构三渠道披露，sha256 链独立验证。与 T3-02 合并建议：Planner 将配套测试/mod 注册文件纳入白名单或 changes-json 报告范围。
- 💭 **T6-05**（worktree 卫生）：preq-t6-integration worktree 残留 untracked `.t6_tests_keylines.txt`（同 T3-03 模式，证据副本已在主树且 hash 匹配）。建议 executor 清理。

---

## 七、verdict 元数据

- task_id: T-1795663571791-34b69f20
- step_id: S-1795663571796-3508971c
- snapshot_id: 2d07b1b0344ade54
- 裁决：PASS（采纳 521514b，批次 1 五单集成闭环）
- findings 数：5（🟡 1、💭 4、🔴 0）
