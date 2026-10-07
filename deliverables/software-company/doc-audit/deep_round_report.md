# 深度参数轮报告:EXPECTED_BUSINESS → 真实 PASS 转化

日期:2026-09-30
环境:Windows,Python 3.14,生产 HTTP daemon(git_commit 含全部修复),真实 workspace = callwarden 仓库根(task-DB 已有 authority capture)

## 1. 目标与方法

把 T2/T3 的 EXPECTED_BUSINESS(路由通但前置/参数不满足而业务拒绝)尽量转成**真实 PASS**(工具真正跑成功)。

**升级**:
1. `param_provider` 从骨架升级到深度 —— SeedContext 增加深度前置字段(lease_token / fencing_counter / identity / branch / candidate / symbol_hash / evidence_path 等);`_resolve_by_name` 扩充 identity 族、lease 族、编辑 patch 族、治理 evidence/verdict/contract 族、gc 族等专有参数名族。
2. **深度前置状态工厂**(`_deep_factory.py`):在真实 workspace 上预建真实实体 —— `task.create`(真 task_id + step_id)→ `agent.register`(真 identity)→ `lease.acquire`(真 token + fencing_counter=1),全部来自 daemon 真实响应,填入 SeedContext。
3. 用 **callwarden 仓库根 workspace**(task-DB 已有 authority capture)而非全新临时 workspace —— 关键发现:全新 register 的 workspace 在 task-DB 无行,task.create 报 `E_WORKSPACE_AUTHORITY_MISMATCH`,task 编排类工具本就无法在其上 PASS。

## 2. 转化结果

| 面 | 原始(T2/T3) | 深度轮 | 真实 PASS 变化 |
| --- | --- | --- | --- |
| MCP | 157 PASS / 72 EB | **163 PASS / 66 EB** | **+6 真实 PASS** |
| CLI | 70 PASS / 114 EB / 18 DEFECT | **82 PASS / 104 EB / 16 DEFECT** | **+12 真实 PASS** |

### MCP 转化的 15 个 EB→PASS(task 编排/lease 域)
task_create、task_status、task_status_tree、task_split、task_report_step、task_governance_projection、task_resolve_block、task_get_role_prompt、work_next_job、assignment_show、lease_status、lease_release、lease_list_events、record_task_symbol_change、test_impact_selection

(注:同时有 9 个 PASS→EB 退化,因 workspace 从种子临时目录换成 callwarden 根,file 路径/format 等参数需随之调整 —— 净 +6。)

### CLI 转化的 11 个 EB→PASS
build-context list/show/activate/delete/resolve、task handoff/report/show/split/status-tree/claim-recover —— 全部因有了**真实 task_id + lease + identity**而跑通。

## 3. 剩余 66 个 MCP EB 的精确根因分类

深度轮的价值不仅是转化,更是把"笼统的参数不足"**精确定位**为 7 类:

| 类别 | 数量 | 性质 | 可转化? |
| --- | --- | --- | --- |
| **SCHEMA_CONTRACT_MISMATCH** | 5 | **真实工具壳缺陷**:MCP schema 声明的参数名与 daemon 契约字段名不符 | 需修工具壳 |
| **WORKSPACE_INST_MISSING** | 7 | **疑似工具壳缺陷**:schema 未声明 workspace_instance_id,工具壳也未注入,daemon 却要求 | 需修工具壳 |
| VALIDATION_TYPE | 9 | pydantic 类型校验失败(provider 值类型 / 工具壳 schema) | 部分可调 |
| NEED_EXTERNAL_ENTITY | 15 | 需真实 job/branch/candidate/git数据;其中 branch/candidate 因 register_branch/rule_candidate_create 是 A 类 method_not_found **根本建不了** | 多数不可转 |
| GOVERNANCE_STATE | 9 | 需精确治理状态机前置(verdict phase=blind_first_pass、active assignment、failed step、session 匹配) | 需完整治理流程 |
| FILE_PATH | 6 | 文件路径格式(callwarden 根符号→文件路径解析) | provider 可调 |
| PROVIDER_FIXABLE | 9 | 部分专有参数可补(symbol_hash/audit_id/rule/name) | provider 可调 |
| OTHER | 6 | 各种边界(gc_policy FK/capture-diff multiple ws 等) | 混合 |

### 重点:发现的真实工具壳缺陷(SCHEMA_CONTRACT_MISMATCH,5 个)

MCP 工具的 `inputSchema` 声明的参数名与它转发给 daemon RPC 的字段名**不一致**,导致"LLM 按 schema 正确调用也会失败":

| 工具 | schema 声明 required | daemon 实际要求 |
| --- | --- | --- |
| `diff_callers` | `qualified_name`(+left/right_workspace_id) | daemon 报缺 `symbol_a` |
| `diff_callees` | 同上 | daemon 报缺 `symbol_a` |
| `propose_symbol_patch` | `symbol_name` | daemon 报缺 `qualified_name` |
| `propose_range_patch` | — | daemon 报缺 `new_content`(schema 用 patch?) |
| `get_tested_functions` | — | daemon 报缺 `qualified_name`(schema 用 test_qualified_name?) |

**性质**:这是比 T3 CLI bug 更隐蔽的缺陷 —— schema 与 daemon 契约脱节,LLM/客户端按文档调用必失败。建议作为新一批工具壳修复项(与 T3 的 CLI bug 同类)。

### WORKSPACE_INST_MISSING(7)

import_envelope_dependencies、publish_interface、record_artifact_identity、assignment_create、extract_rule_candidates_from_quality_findings、resolve_gate_findings、run_check_gate —— schema 未声明 workspace_instance_id,工具壳转发时也未由 route_rpc 注入(task-scoped 路径未覆盖),daemon 却要求。疑似工具壳/route_rpc 注入缺陷。

## 4. CLI 剩余 DEFECT(16)

- 10 个 A 类 compat worker 禁用连锁(method_not_found,已立技术债 T-1790723830548-12a53c8c)
- 2 个新发现 rc=0 掩盖:`gc audit-list`('dry_run' KeyError)、`fn-metrics`/`complexity`/`coupling`(疑似字段问题,待确认)
- `daemon publish`:internal_error build_and_publish(需真实 db_path 参数)

## 5. 结论

- **深度轮达成**:MCP 真实 PASS 157→163,CLI 70→82;task 编排/lease 域从"业务拒绝"转为"真正跑通"(有了真实 task+step+lease+identity 前置)。
- **精确定位了剩余 EB**:不再是"参数不足"的笼统结论,而是 7 类明确根因。
- **新发现真实缺陷**:5 个 SCHEMA_CONTRACT_MISMATCH + 7 个 WORKSPACE_INST_MISSING = **12 个工具壳 schema/契约缺陷**(LLM 按 schema 调用会失败),建议作为新修复批次。
- **本质不可转的 ~40 个**:需外部数据(git/semgrep/job)、A 类连锁阻断的实体(branch/candidate)、完整治理状态机 —— 这些属于集成测试/端到端业务流程范畴,非参数轮能覆盖。

**对"全部能用了吗"的最终回答**:
- MCP:163/243 真实跑通,12 个工具壳 schema 缺陷待修,其余需外部前置或完整治理流程。
- CLI:82/234 真实跑通,10 个 A 类技术债 + 若干需真实业务流程。
- 无"方法缺失/daemon 不可用/崩溃"级阻断(除 A 类 compat 连锁);工具整体**可路由、参数校验正确**,核心编排/查询/lease 域已验证真正可用。
