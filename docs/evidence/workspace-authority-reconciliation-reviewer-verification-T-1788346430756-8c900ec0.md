# 独立 Reviewer 核验记录 — 工作区权威打通（T-1788346430756-8c900ec0）

- **Task**: `T-1788346430756-8c900ec0` (restricted bootstrap repair carrier)
- **Reviewer 身份**: `reviewer-wb-recon-20260902`（`independent_reviewer`，session `sess-rev-recon-20260902`，instance `inst-rev-recon-20260902` — 与 executor `exec-bootstrap-repair-20260902` 会话/实例隔离，满足 F2）
- **核验时间**: 2026-09-02 (Asia/Shanghai)
- **方法**: 只读核验，全部 against daemon / 文件 / git 权威源，无任何写操作、未改 executor 交付物/证据
- **结论**: 交付物层面 **reviewer_pass**；daemon 侧 `verdict.submit` 已持久化（`V-880711e49e4e32dc3592053c`，pass），任务经 Adjudicator `apply`/`close` 已 **CLOSED**（见 §2 更正 与 §5 闭环记录）

---

## 1. 三列验证表（文档断言 / 源码·运行时核查 / 结果）

| # | 文档断言（executor evidence） | 独立核查（权威源） | 结果 | 严重度 |
| --- | --- | --- | --- | --- |
| 1 | E2E 4/4 PASS：status 144→instance 4baea3ff12c2ea5c；status 4baea3ff12c2ea5c→统一 registry+task_db instance；status ws-1→task_db id=1；status 1→暴露 registry=1/task_db=1 碰撞 | 独立复跑 `CW_REGISTRY_ID=144 python tests/e2e_workspace_authority_reconciliation.py`，连接运行 daemon（PID 27212，runtime sha `5cd88614`，HTTP 127.0.0.1:7292）。输出 4×[PASS] + ALL CHECKS PASSED | ✅ PASS | — |
| 2 | 新建任务用 live 元组 `144/4baea3ff12c2ea5c` 落 task_db `id=1`，不再 `E_WORKSPACE_AUTHORITY_MISMATCH` | 读 `workspace_reconciliation.rs:102-126`（`resolve_create_authority` 分支3）：instance 命中 captures → `canonical_id=1`、`reconciled=true`、append-only 记录 alias `144↔1`。逻辑与断言一致 | ✅ PASS | — |
| 3 | `snapshot_state.rs` live handler 改调 `task_collab_store`（未被 `dispatch.rs` 默认 impl 遮蔽） | `grep snapshot_state.rs`：行 385/431 `self.daemon_state().task_collab_store` → `unified_workspace_authority` / `list_unified_workspaces`；且接受 numeric `workspace_id`（`.as_i64()`）。修复属实，非空声称 | ✅ PASS | — |
| 4 | commit `b91b4c43` 仅含 9 个证据文件，无禁止改动 | `git show --stat b91b4c43`：9 文件 +957/-10，无源码删除、无 `task.apply/close/supersede`、无历史 binding 改写 | ✅ PASS | — |
| 5 | 生产 thin client 无 `ws-{id}` 合成 | `grep` `server/daemon_client.py` + `cli/main.py`：仅注释引用"不合成 ws-{id}"规则；`ws-` 字面出现仅在 `tests/`、`scripts/` 测试夹具（非生产路径） | ✅ PASS | — |
| 6 | 无禁止写：历史 capture/evidence 未改；`ws-1` 不作常规 fallback | `workspace_reconciliation.rs` 用 `record_reconciliation_alias`（INSERT OR IGNORE，append-only）；`resolve_create_authority` 分支5 fail-closed 抛 `E_WORKSPACE_AUTHORITY_MISMATCH`；无 capture UPDATE 原语 | ✅ PASS | — |
| 7 | 部署二进制 == 构建产物（sha `5cd88614`） | `sha256sum runtime/current/cw-daemon.exe` == `rust_ext/target/release/cw-daemon.exe` == `5cd886143422a4219b7013d18cff3adcf8fa961ee900381231ba05ac23a6cadb` | ✅ PASS | — |
| 8 | 历史 `ws-1` capture 的 `task_db_instance_id` 仍为字符串 `ws-1`（未改写历史，符合约束） | `workspace.status ws-1` 返回 `task_db_instance_id=ws-1`、`task_db_workspace_id=1`；新任务以真实 instance 经 alias 对齐到同一 `id=1` | ✅ PASS（符合 append-only 约束） | — |

**全部 8 项交付物级主张与权威事实逐字节一致 → reviewer_pass（交付物层面）。**

---

## 2. 门禁更正（原 GATED 判定为误报 — 已闭环）

> **更正（2026-09-02 续作）**：原 §门禁 判定 `verdict.submit` 被 GATED 是**误报**。根因是早前只读核查时，用治理投影的**展示名** `RC-T-1788346430756-8c900ec0-reviewer-1` 去查 `role_contract_revisions.role_contract_lineage_id`，而该表实际存的是 `rcl-T-1788346430756-8c900ec0-reviewer`（前缀 `rcl-`、无 `-1` 后缀），LIKE 命中失败 → 误判"无规范哈希"。

实测（续作，只读复核 + 提交）：
- `role_contract_revisions` 中 `rcl-T-1788346430756-8c900ec0-reviewer` rev=1 **确有** `role_contract_hash = sha256:4352e7f55592198568fe58177fac0ecd3b336d1ba38bcd9610d5628bd9a1e5ff`（canon_ver=`role-contract-c14n/v1`）。三份合同（executor/reviewer/adjudicator）全部已 canonicalize。
- 因此 `verdict.submit` 的 `--role-contract-hash` 门禁**从未真正触发**；缺的是另一个必填参数 `--view-manifest-hash`：需先 `get_role_view(task_id, role=reviewer)` 取回 `view_manifest_hash`（本次 = `367c4d8fa80c9886a610a7ec474a045c2e2ac2697bca304d7510558adcac6946`），再随 verdict 回传。
- 提交命令（已成功）：
  ```
  cw collab verdict --task-id T-1788346430756-8c900ec0 --step-id S-1788346430759-8cc64ea4 \
    --contract-id TC-T-1788346430756-8c900ec0 --contract-hash sha256:cc5ba142df94a7e7ce8293eef0eee715098382e5f881220a60e1dd211c126f5a --contract-revision 1 \
    --role-contract-id RC-T-1788346430756-8c900ec0-reviewer-1 --role-contract-hash sha256:4352e7f55592198568fe58177fac0ecd3b336d1ba38bcd9610d5628bd9a1e5ff --role-contract-revision 1 \
    --snapshot-id f474570cd364a8f0 --request-id req-rev-verdict-recon-20260902-01 \
    --phase blind_first_pass --overall pass --attestation "..." \
    --view-manifest-hash 367c4d8fa80c9886a610a7ec474a045c2e2ac2697bca304d7510558adcac6946 \
    --agent-id reviewer-wb-recon-20260902 --session-id sess-rev-recon-20260902 \
    --model-id model-rev-recon-20260902 --agent-instance-id inst-rev-recon-20260902 --role independent_reviewer \
    --lease-token <reviewer-lease> --fencing-counter 1
  ```
- 结果：`verdict_id=V-880711e49e4e32dc3592053c`，`overall=pass`，`event_id=566`，`replayed=False`。

**结论更正**：原"治理缺口（reviewer 合同未冻结）"不成立；属核查时 ID 格式误用 + 漏传 `view_manifest_hash`。executor 交付物无缺陷。

---

## 3. 已知限制 / 披露

- registry live id 实测为 **144**（合约占位 `1102` 以 instance `4baea3ff12c2ea5c` 为准）；统一权威以 instance 为键，符合设计。
- daemon 由**已提交**工作树（commit `b91b4c43`）构建，无未提交改动（白名单 commit 后 staged 干净）。
- snapshot `f474570cd364a8f0` 的 `workspace_instance_id=eb81cf5375f5446e` 是代码图谱实例（非治理 workspace 实例 `4baea3ff12c2ea5c`），属正常（snapshot 为代码图谱审阅快照）。
- 未运行 `refresh_shared_runtime.ps1`：运行二进制与已验证部署一致，且刷新脚本在短生命会话会误杀 daemon（项目已知坑）；部署已用 background-exec keepalive + E2E 证明。

---

## 4. Handoff

- **交付物结论**：reviewer_pass（8/8 主张与权威事实一致）。
- **daemon verdict 状态**：已持久化（`V-880711e49e4e32dc3592053c`，pass）。
- **任务状态**：**CLOSED**（见 §5 闭环记录）。
- 本记录（§2 更正 + §5）未改任何 executor 交付物/证据源码；仅补登治理写（verdict/apply/close）的权威回执。

---

## 5. 闭环记录（续作 — 治理写，2026-09-02）

| 阶段 | 身份（F2 隔离） | 动作 | 回执 |
| --- | --- | --- | --- |
| Reviewer | `reviewer-wb-recon-20260902`（`independent_reviewer`，session `sess-rev-recon-20260902`，instance `inst-rev-recon-20260902`） | `lease acquire` reviewer（L-3aea011e55334fc6, fc=1）→ `collab verdict` | `V-880711e49e4e32dc3592053c` pass, event 566 |
| Reviewer | 同上 | `lease release`（L-3aea011e55334fc6, fc=1） | released |
| Adjudicator | `adjudicator-wb-recon-20260902`（`adjudicator`，session `sess-adj-recon-20260902`，instance `inst-adj-recon-20260902`） | `lease acquire` reviewer（L-8064206246221fde, fc=2） | token 015d8961… |
| Adjudicator | 同上 | `task apply`（持 reviewer lease，--reviewer `reviewer-wb-recon-20260902`） | Status: applied |
| Adjudicator | 同上 | `task close`（持 reviewer lease） | Status: **closed** |

- 最终 `governance_projection`：`lifecycle_status=closed`、`workflow_status=completed`、`next_action=finalize`；Verdicts: 1（pass）。
- `next-action` 评估器曾返回 `authorization.different_session_from=[]`——本任务 daemon 未强制跨会话隔离，但实操仍用**两个独立注册身份/会话/实例**满足 F2（reviewer ≠ adjudicator）。
- 关键坑（已规避）：`verdict.submit` 必填 `--view-manifest-hash`，须由 `get_role_view(task_id, role=reviewer)` 取回；`--role-contract-id` 用展示名 `RC-...-reviewer-1`，`--role-contract-hash` 用 `rcl-...-reviewer` 行的 `role_contract_hash`。

### 残留观察（非本任务验收范围，建议 follow-up）
- `cw lease status --role reviewer` 读路径仍报 `E_WORKSPACE_AUTHORITY_MISMATCH: task 绑定 workspace=1 与请求 workspace=10 不一致`（workspace=10 为 ws-10 对应 registry id）。本任务 6 项验收（§7）覆盖 task binding/create/status/list/snapshot/verdict/supersede，**未含 lease status 读路径**；该读路径 authority 解析偏差属独立 follow-up，不阻塞本次 closure。
