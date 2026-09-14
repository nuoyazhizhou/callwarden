# GATE-0（legacy Epic binding attestation）收尾报告

> 完成时间：2026-09-02 05:30（GMT+8）｜提交：`b9dd816` + `9855da6`（已 push origin/master）

## 1. 收尾目标与结论

GATE-0 卡（`deliverables/software-company/role-prompt-v1-gate-task-manifest.json` 411-690 行）要求三份证据文件落地，其中 **finalization 的 `committed_head_file_hashes` hash 回填口径**是本轮唯一未决事项。收尾结论：

| 事项 | 定案 | 依据 |
|---|---|---|
| input 声明 hash `447508d9…` | **不可改写**，接受"冻结于生成时点" | daemon `task_operation_ledger` attest 记录 `evidence_hash` 绑定（2^23 字段组合 × 128 序列化变体穷举零命中） |
| receipt 声明 hash `a1b35b13…` | **不可改写**，同上 | verdict `V-9c2e52fb` post_reveal_amendment clause 绑定 |
| finalization `committed_head_file_hashes` | 回填**占位版 blob sha256** = `87259e3e…` | BR-02 两段式语义，`git show b9dd816:<path> \| sha256sum` 完全复现（同构于 BR-02 ca8c2fb 验证） |
| 5 个 `*_receipt_hash` 占位符 | 替换为 **daemon 权威事件行 JSON 规范化 sha256** | daemon 对 apply/close/lease.release 不返回 hash；事件行即权威事实 |

## 2. 三份证据文件（已提交）

| 文件 | 最终磁盘 sha256 | 声明 hash（绑定方） | 提交 |
|---|---|---|---|
| `docs/evidence/role-prompt-v1-gate0-binding-attestation-input.json` | `26461d36…` | `447508d9…`（daemon 账本） | `b9dd816` |
| `docs/evidence/role-prompt-v1-gate0-binding-attestation-receipt.json` | `82d68488…` | `a1b35b13…`（V-9c2e52fb） | `b9dd816` |
| `docs/evidence/role-prompt-v1-gate0-binding-attestation-finalization.json` | `ffc84eca…` | 自回填 `87259e3e…`（BR-02 两段式） | `b9dd816`（占位版）→ `9855da6`（回填版） |

**BR-02 语义验证路径**（已实测通过）：

```
git show b9dd816:docs/evidence/...finalization.json | sha256sum
→ 87259e3e1be9e3c8ff9451d0ae52507f48351b7fc0418c9158640813e07cba2d  ✓（== 回填值）
```

## 3. 5 个 receipt hash 的权威来源

daemon 不返回 apply/close/lease.release 的 "receipt hash"，故按 evidence_writer 规则序列化 **daemon 权威事件行**：

| 字段 | 值 | 来源（task_lease_events / task_events） |
|---|---|---|
| `reviewer_gate_lease_release_receipt_hash` | `133ff462…` | `EVT-51fe4cf527ebfdb9` L-8abfb66a fencing3 release @1788317602 |
| `review_vehicle_apply_receipt_hash` | `43e97a53…` | event 6521 review→applied @1788317632.3366036 |
| `review_vehicle_close_receipt_hash` | `f23e2bb4…` | event 6522 applied→closed @1788317639.3852534 |
| `review_vehicle_complete_projection_hash` | `4b5c4e53…` | `cw task next-action` COMPLETE 投影子集 |
| `adjudicator_finalizer_lease_release_receipt_hash` | `7f9c3a26…` | `EVT-bf5f5aa87ba11b8c` L-15230f79a fencing4 release @1788317648 |

算法：`json.dumps(event_row_dict, sort_keys=True, ensure_ascii=False, separators=(',',':'))` 的 sha256（已写入 finalization `authority_note`）。

## 4. 验收断言核对（11/11 PASS）

时序（daemon 权威时间线，全部吻合）：

```
V-a04f3fee blind pass @1788316889
→ V-9c2e52fb post_reveal_amendment @1788317595（amendment_ref=V-a04f3fee ✓，绑定 a1b35b13 ✓）
→ reviewer lease L-8abfb66a release @1788317602   （amendment 后、apply 前 ✓）
→ finalizer lease L-15230f79a acquire @1788317609（reviewer release 之后 ✓）
→ apply @1788317632.3366036 / close @1788317639.3852534（holder=adjudicator ✓）
→ finalizer lease release @1788317648（close 后 ✓）
```

- 断言 1：`cw task next-action T-1787203926824-9f873bfc` 不再报 E_WORKSPACE_AUTHORITY_UNAVAILABLE（governance_blocked 为 legacy 既有状态）✓
- 断言 2/3：legacy 恰好 1 binding（`tb-…-9f873bfc-ws-1`）+ 1 capture（`wc-ws-1-355448432`），workspace 1/ws-1 匹配 anchor ✓
- 断言 4/5/6：同请求重放幂等；不同请求 E_LEGACY_BIND_ALREADY_BOUND；error 仅入账 1 条 ledger，无重复 binding/capture/event ✓
- 断言 7：amendment 引用 INITIAL_REVIEW_VERDICT_ID + GATE0_RECEIPT_SHA256 ✓
- 断言 8：review vehicle applied/closed/COMPLETE，legacy Epic 生命周期未变（in_progress）✓
- 断言 9/10：两段 lease 释放时序正确 ✓
- 断言 11：finalization 引用 input/receipt hash 而不修改二者 ✓

## 5. required_fields / forbidden_fields 核对

- input **16/16**、receipt **9/9**、finalization **12/12** 全部命中（`adjudicator_finalizer_lease_id`/`fencing_counter` 为嵌套对象 `adjudicator_finalizer_lease.{lease_id,fencing_counter}`，与 receipt `already_bound_error_code` 嵌套先例一致；daemon 源码无 required_fields 硬校验，字段存在性由审查方递归定位）。
- forbidden_fields 扫描：input 命中 `REVIEWER_LEASE_TOKEN` 仅为 `operation.secret_runtime_values` **元数据声明**（声明不写入 token），文件中无真实 token，合规。

## 6. Git 记录

```
9855da6  GATE-0 finalization: backfill committed_head_file_hashes (two-phase self-hash, BR-02 semantics)
b9dd816  GATE-0 evidence: legacy Epic binding attestation input/receipt/finalization (placeholder)
af0c068  REVIEWER_PASS（previous）
```

已 push：`af0c068..9855da6 origin/master`。

## 7. 后继

- **GATE-0 successor 前置全部满足**（daemon status=attested / post_reveal 生效 / review vehicle COMPLETE / finalization frozen）→ **GATE-1A**（parent-aware governed task.create daemon hardening）可创建。
- 遗留开放项（非阻断）：A′ phantom ws-1 绑定重新 attestation（P0-B 能力就绪）；runtime vs target 二进制 hash 漂移；capability registry 与 dispatch 漂移（P0C-R1）。
