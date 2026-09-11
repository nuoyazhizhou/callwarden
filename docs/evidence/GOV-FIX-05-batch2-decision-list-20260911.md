# GOV-FIX-05 批次 2 裁决清单（110 张无合同非终态卡）

> 生成：2026-09-11；批次 1（121 张 Rust 迁移系/临时验证/注释系）另行收尾。

> 每张卡处置选项：**A** supersede→GOV-FIX-04 卡收尾（视为已落地/废弃）；**B** 补合同保留 backlog；**C** 暂不动。

## ROOT 其他（41 张）

| 状态 | task_id | 标题 |
|---|---|---|
| open | `T-1785475902294-290b8c27` | 修复协同设计文档规格引用诊断 |
| in_progress | `T-1785728415363-5e9fba35` | 6.2 实现依赖解析、边归一化与最小 cycle path |
| open | `T-1785737161674-580ba85a` | 1.2 实现 Control/Treatment 最小披露投影与追加式 JSONL 采集 |
| open | `T-1785737342694-bc82e7c3` | 8.5 Identity fail-closed 接入 Evidence Gate |
| open | `T-1785738463253-6e8040b4` | 1.4 接入 P0 CLI，提供批次冻结、纳样、verdict/reveal 记录、暂停与报告命令 |
| open | `T-1786086931742-f0779120` | fail_closed_test |
| open | `T-1786119756687-15279ddc` | 修复 e2e workflow daemon job 依赖安装与 pyproject 包遗漏 |
| open | `T-1786147822854-f475f969` | 独立核验 Windows daemon 发布闭环与任务关闭证据 |
| open | `T-1786149478402-dd360810` | ci/local-bundle 主 job 发布验收修复（FOREIGN KEY + 跨 mount） |
| open | `T-1786270972818-0a98fe4e` | W2/D0 小任务化收口：证据、归属与独立复审 |
| open | `T-1786279058392-9a29523b` | 共享任务写入收敛：daemon 单写点与证据原子绑定 |
| review | `T-1786282419616-81bd89c8` | AGENTS.md 记录 Windows daemon 与 WSL 权威库边界 |
| review | `T-1786285248430-2438be90` | AGENTS.md 增加 Agent 身份与技能选择协议 |
| open | `T-1786343552670-27c901a0` | W2 coexistence P1 endpoint and bridge health hardening |
| open | `T-1786348617897-7ed15d90` | 修复 MCP file_list 缺少 os 导入 |
| open | `T-1786350952829-238e257c` | 修复 Windows daemon 与 WSL MCP bridge 共享链路 |
| open | `T-STEP3-CONTRACT-60676385` | step3 contract e2e |
| open | `T-1786461699588-60cb05b8` | P2 CLI 共享 daemon 侧 |
| open | `T-1786461700913-afca987c` | P2 CLI 共享 cli 侧 |
| open | `T-1786546893737-2fc8e998` | TokenSlim 逐函数中文注释总控（沿调用链·自底向上·手工注释） |
| open | `T-1786547532846-fda85ad0` | [path_optimizer][DEFECT] generate_prefix_candidates 不支持 Wind |
| open | `T-1786549306943-0e115310` | [encoding_fallback] 缺陷：mojibake 标记字符集在三处不一致 |
| in_progress | `T-1786633796145-baf26208` | Runtime refresh deployment provenance: Python 3.14 and activ |
| open | `T-1786634991996-29380530` | capture-diff explicit commit scope excludes unrelated worktr |
| open | `T-1786804435485-cc2eefc8` | 设计 cw task loop 与角色交接协议 |
| in_progress | `T-1786965084142-ba502368` | Authority maintenance: rebuild and deploy Python 3.14 callwa |
| in_progress | `T-1786966646248-6f1c0890` | Harden Python core runtime deployment and authority verifica |
| review | `T-1786974139922-30dda944` | Harden implementer remediation escalation protocol |
| open | `T-1786981983239-5afcc768` | Freeze three-role governance requirements |
| open | `T-1787460614353-5888a86c` | cw |
| open | `T-1787759859947-e3e1fd70` | CLI task-bound reviewer verdict create entry |
| open | `T-1787786475332-c46bcc58` | 修复 reviewer handoff 证据绑定与 quality findings schema |
| open | `T-TEST-44acef0f35` | 直接 daemon 调用诊断 |
| open | `T-HTTP-aee14d9fee` | http transport 诊断 |
| open | `T-1788268328552-7bc5c03e` | T-B 特征聚合器 feature_builder |
| open | `T-1788268329559-205beba6` | T-C 接入 ContentAnalyzer.quick_analyze |
| open | `T-1788268330901-bc8075f7` | T-D 接入 PluginDispatcher.dispatch_slice |
| open | `T-1788268331924-1177d1cf` | T-F 回归验证与单测 |
| open | `T-1788268333016-286ad6c0` | T-E 增量追加接口 feature_reader.append |
| review | `T-1788389484437-c3f4118c` | E2E lifecycle full proof (T-1788382908707-bbdd0cfc) |
| open | `T-1788941704764-9457a3a4` | CR4 验收卡：含 manifest sha sha256:95298729f3357cdbe76d8f8e91f120 |

## G0 实验系列（17 张）

| 状态 | task_id | 标题 |
|---|---|---|
| open | `T-1785841342380-ad4024b4` | G0 真实 blind review 批次数据积累（≥10 非平凡 code_change + reviewer 盲评） |
| open | `T-1785982037303-579c5787` | 只读复审 G0 blind review 修复 |
| open | `T-1785982277055-743858bc` | 只读复审 G0 blind review 追加修复 |
| open | `T-1785982398879-6fef862d` | 最终只读确认 G0 malformed metrics 回归 |
| open | `T-1785984007622-b7cd6523` | 设计新的 G0 批次创建与独立评审 Agent 提示词 |
| open | `T-1785995349926-6796a55a` | 独立核验 G0 批次 B-1785989307324 是否具备 Reviewer 执行条件 |
| open | `T-1785995504693-bd71f273` | 修复 G0 Treatment post-reveal Implementer_Notes 交付与 Reviewer 隔 |
| open | `T-1786004065320-ab3bd656` | 独立核验 G0 批次 B-1785989307324 最终评审报告 |
| open | `T-1786004743428-bdc5252c` | G0 experiment v2 证据归档与受控采样 |
| in_progress | `T-1786005257064-a0cd263e` | G0 non-product batch creation and frozen reviewer handoff |
| open | `T-1786005595538-a9464ab7` | G0 v2 分层配对分组协议修复 |
| open | `T-1786014154340-bad6c7a2` | G0 adjudicate B-1786007826323-60a9ada9 P08 contamination |
| in_progress | `T-1786022666252-b49dbdc8` | G0 阳性问题与污染批次治理 |
| in_progress | `T-1786022678812-7654a336` | 5 新 paired G0 批次独立验收与 Reviewer 交接 |
| open | `T-1786025511793-9d6fe534` | Independent review: G0 remediation tasks 1-4 |
| open | `T-1786026015859-2b0e778d` | 补齐 G0 整改 1-4 的任务归因证据 |
| open | `T-1786032523916-1bcb2835` | 恢复 G0 Creator 证据源并执行 v3 预检 |

## GovSuite-P11（9 张）

| 状态 | task_id | 标题 |
|---|---|---|
| open | `T-1786460623654-de2ba928` | GovSuite-P6 PII/靶场/账本执行器与 doctor 闭环 |
| open | `T-1786460635578-a4de2008` | GovSuite-P7 确定性收敛、集成验证与门禁自举 |
| open | `T-1786507603905-4f523f10` | GovSuite-P6F1 治理文档补充 evidence 子命令声明（盲审 Finding 1） |
| open | `T-1786512215092-ef772ac0` | GovSuite-P7盲审Finding：整理流程步骤2失败场景测试与 RULE 提炼重试幂等性 |
| open | `T-1786546085027-e4f33584` | GovSuite-P11 结构类检查器集合与 Checkpoint 12（tasks 11.x + 10.4） |
| applied | `T-1786546118176-9cc83798` | P11.3 RoleMatrixCheck + SeparationOfDutyCheck + Property 9/1 |
| open | `T-1786546152624-a206d1f8` | P11.5 DomainDocCheck + Property 14（tasks 11.14/11.15） |
| open | `T-1786546160758-86d3e11c` | P11.6 SkillLayoutCheck + Property 26（tasks 11.16/11.17） |
| open | `T-1786546169231-7fd9a8e0` | P11.7 Property 5 无检测实现绝不绿灯（tasks 10.4） |

## 多 LLM 契约协同系列（8 张）

| 状态 | task_id | 标题 |
|---|---|---|
| open | `T-1785474546353-d2189544` | 修订多 LLM 契约协同设计 |
| open | `T-1785479479193-0bb5009b` | 精确化多 LLM 协同需求 |
| in_progress | `T-1785481343378-4332b579` | 重构多 LLM 契约协同规格 |
| open | `T-1785482545658-48effbc1` | 独立盲审多 LLM 契约协同规格 |
| open | `T-1785482674104-b574e8ed` | Analyze multi-llm-contract-collaboration requirements |
| in_progress | `T-1785574343867-04c2894e` | 2. G0 检查点：确认 P0 自动化测试通过，并仅在真实批次输出 `eligible_for_p1=true` 时继续 |
| open | `T-1785736740535-62bf8f1f` | 评估多 LLM 协同规格对 Agent 长程编排能力的覆盖 |
| in_progress | `T-1785767529975-fd298f11` | 审计缺陷修复批次（P1-P4，并行 4+1 线） |

## W2/D0 收口（8 张）

| 状态 | task_id | 标题 |
|---|---|---|
| applied | `T-1786271097643-bbe3fc69` | A2 W2.3 Linux UDS evidence |
| open | `T-1786271097643-6c42234e` | A3 W2.3 attribution audit |
| open | `T-1786271097643-dc0fddab` | A4 W2.4 Linux dual UID evidence |
| open | `T-1786271097643-3043411b` | A5 W2.4 attribution evidence |
| open | `T-1786271097643-a2aef12d` | A6 D0 Python 3.14 focused change |
| open | `T-1786271097644-e25dfe47` | A7 D0 status reconciliation |
| open | `T-1786271097644-12cd9903` | A8 GD gate decision matrix |
| open | `T-1786271097644-aa476121` | A9 independent final review |

## Windows-Unified-Daemon-Writer（6 张）

| 状态 | task_id | 标题 |
|---|---|---|
| open | `T-1786086175167-609c9e86` | Windows-Unified-Daemon-Writer |
| open | `T-1786086193630-0` | Phase 1: Route Matrix & Design |
| open | `T-1786086193630-1` | Phase 2: Rust CLI Task Routing |
| open | `T-1786086193630-2` | Phase 3: Python CLI & MCP Task Routing |
| open | `T-1786086193630-3` | Phase 4: Daemon RPC Handlers Completion |
| open | `T-1786086193630-4` | Phase 5: Windows Named Pipe Writer E2E Test & Verification |

## 六个主线闭环（5 张）

| 状态 | task_id | 标题 |
|---|---|---|
| open | `T-1785627354578-72afd2f8` | W2 Client Agent 与 daemon 闭环 |
| open | `T-1785627354578-2bec6602` | W3 默认切换与 rollback 窗口 |
| open | `T-1785627354579-4e7e219b` | W4 删除 Python fallback 与死代码 |
| open | `T-1785627354579-90fa4ff5` | W5 发布与企业证据 |
| open | `T-1785627354579-b9c96db4` | W6 最终 parity、灾备与独立复审 |

## CallWarden Codex Agent Orchest（5 张）

| 状态 | task_id | 标题 |
|---|---|---|
| in_progress | `T-1787041854214-27341530-sub-1` | 1. Protocol and ownership design |
| open | `T-1787041854214-27341530-sub-2` | 2. Coordination loop |
| open | `T-1787041854214-27341530-sub-3` | 3. Codex agent runner |
| open | `T-1787041854214-27341530-sub-4` | 4. CallWarden adapter |
| open | `T-1787041854214-27341530-sub-5` | 5. End-to-end and recovery tests |

## 零散修复（2 张）

| 状态 | task_id | 标题 |
|---|---|---|
| open | `T-1785563665152-199dc3da` | Fix requirements.txt dependency defects and spec revocation/ |
| open | `T-1785574553601-3c891439` | 规格三件套入 git 暂存并把实施计划导入 cw 任务库 |

## 审计缺陷修复批次（P1-P4，并行 4+1 线）（1 张）

| 状态 | task_id | 标题 |
|---|---|---|
| in_progress | `T-1785767529978-bacba8f1` | E. 门禁重验（G0/G1/G2/G4/根 verify） |

## 跟踪：Rust daemon 未嵌入 Python，snap（1 张）

| 状态 | task_id | 标题 |
|---|---|---|
| review | `T-1786260491528-b56dd7b4` | 修复 Linux cw-daemon PyO3 链接边界并完成 W2.3 WSL 验收 |

## VERIFY-steps真实落库验收(整改)（1 张）

| 状态 | task_id | 标题 |
|---|---|---|
| open | `T-1786429502267-daf28674-sub-1` | 步骤 |

## Harden Python core runtime dep（1 张）

| 状态 | task_id | 标题 |
|---|---|---|
| in_progress | `T-1786966646248-6f1c0890-sub-1` | Fix blocked runtime deployment attribution and gate evidence |

## Authority maintenance: rebuild（1 张）

| 状态 | task_id | 标题 |
|---|---|---|
| in_progress | `T-1786965084142-ba502368-sub-1` | Fix blocked authority recovery runtime-layout evidence |

## Harden implementer remediation（1 张）

| 状态 | task_id | 标题 |
|---|---|---|
| review | `T-1786977060319-261c9060` | Replace task governance with executor-reviewer-adjudicator l |

## Replace task governance with e（1 张）

| 状态 | task_id | 标题 |
|---|---|---|
| review | `T-1786978884825-f322ec2c` | Add mandatory three-role handoff envelope |

## Add mandatory three-role hando（1 张）

| 状态 | task_id | 标题 |
|---|---|---|
| in_progress | `T-1786979390625-b7267ccc` | Correct handoff envelope routing semantics |

## Codex Agent Orchestrator（1 张）

| 状态 | task_id | 标题 |
|---|---|---|
| open | `T-1787041854214-27341530` | CallWarden Codex Agent Orchestrator loop |

