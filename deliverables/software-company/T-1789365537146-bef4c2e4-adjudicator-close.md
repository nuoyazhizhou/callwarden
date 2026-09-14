# C-16 承接卡 adjudicator 关闭证据：T-1789365537146-bef4c2e4

- **结论**：Adjudicator 独立复核 reviewer PASS verdict 与全部门禁后接受，`apply` + `close` 收口任务。
- **Reviewer verdict**：`V-4c428e7c30851e88a67a9e5f`（`blind_first_pass` / `pass`，event_id 633，
  findings=4）
- **绑定 step**：`S-1789365537156-bf90f8bc`（step_index 3，最后一步 `release_verify`）
- **Snapshot**：`dfcac6f16b827a30`；**view_manifest_hash**：
  `3ea38b6d7ab60d7ed8d05d9c1662cdf3a63756da200f82f43164f8446019a8b8`；workspace：1
- **Task Contract**：`TC-T-1789365537146-bef4c2e4` r1
  `sha256:e6d41da96fdd13a4c13b0bdc78371471feaa0ed1c60b541bda13412934388ba2`
- **Role Contract（reviewer）**：`rcl-T-1789365537146-bef4c2e4-reviewer` r1
  `sha256:3f691024970453f94a17d7ddc7a5845db2eaf19f1d25af9b8c9000272a6f8863`
- **证据 manifest**：`deliverables/software-company/T-1789365537146-bef4c2e4-evidence.md`
  `SHA256:A114C601B12EAE43A4D83A938489B5BBD1B9B6908EF66460DAAE9AFC4CEC26E8`
- **Reviewer 独立复核报告**：`deliverables/software-company/T-1789365537146-bef4c2e4-reviewer-review.md`
  `SHA256:1B96CE3E35585C0E0AED5AECED26E8975924B37E855EE09287E3C2204E9AB4B6`
- **step0 契约裁决**：`deliverables/software-company/T-1789365537146-bef4c2e4-contract-adjudication.md`
  `SHA256:AED36C140A9E5BC1013CDFBA77CAF098195F474DD335A268D761AAD96D977D18`
- **提交**：`5cfb158`（`[T-1789365537146-bef4c2e4] fix(rust_ext): assignment_show 走权威 workspace
  resolver（C-16）`，2 files / +360 / −4）

---

## 1. verdict provenance 校验（adjudicator 独立只读复核）

`task_verdict_events` id=633 实测：

| 字段 | 值 |
|---|---|
| `verdict_id` | `V-4c428e7c30851e88a67a9e5f` |
| `step_id` | `S-1789365537156-bf90f8bc`（== handoff / assignment 的 source step） |
| `overall` / `phase` | `pass` / `blind_first_pass` |
| `snapshot_id` | `dfcac6f16b827a30`（非空） |
| `view_manifest_hash` | `3ea38b6d…9a8b8`（非空） |
| `workspace_id` | 1（== `task_workspace_bindings` 解析值） |
| `contract_hash` | `sha256:e6d41da9…388ba2`（== Task Contract r1） |
| `role_contract_lineage_id` / `revision` / `hash` | `rcl-…-reviewer` / 1 / `sha256:3f691024…6f8863`（== Role Contract r1） |

- 归一化：`normalization_version=verdict-normalization/v1`，
  rules hash `sha256:b41cbdb3…b0e8d`（`revoked=False`）；canonicalization `role-contract-c14n/v1`。
- reviewer_identity 三重（agent/instance/session）与执行者、adjudicator **均不同**
  （`reviewer-wb-c16-01` / `inst-rev-wb-c16-01` / `sess-rev-wb-c16-20260914`）。

## 2. 全门禁核查（adjudicator 独立复算，全部通过）

| 门禁 | 复核方式 | 结果 |
|---|---|---|
| 提交对象合法性 | `git show --name-only 5cfb158` | 仅 2 文件；`server/`/`db/**`/refresh 脚本命中 0 |
| 源码哈希自证 | 独立 `sha256` | `lease.rs`=`14E1D280F6874123…`、测试=`CF684C39CE3494D3…`（=披露值） |
| diff 卫生 | `git diff --check HEAD~1..HEAD` | rc=0 |
| 部署门禁三元哈希 | 读 `runtime/evidence/20260914-150702-…-e53fee93.json` | `status=passed`/`rollback=false`/`git_head=5cfb1589f6df`(==HEAD)/`daemon_runtime.sha256=87c6200…`/`ping_exit_code=0` |
| 产物身份自证 | 独立 `sha256(runtime/current)` | `87C6200950A91713…`（== 门禁记录，逐字一致） |
| **对部署产物复跑** | 10 例负向矩阵 vs `87C6200…` | **10 passed** |
| **反证（判别力）** | 同套用例 vs 修复前基线 `BE67915C…` | **7 failed / 3 passed**（旧「静默 none」语义） |
| 工作树纪律 | `git ls-files deliverables/_insp_c16*` | 空（探针/隔离 target 未入库） |

## 3. apply / close 依据与执行链

- `task.apply` / `task.close` 保护门禁均为
  `validate_lease_for_mutation(task_id, role="reviewer", token, counter, identity)`
  （`task_collab_lifecycle_apply.rs:36-44` / `:124-132`）→ **要求 reviewer lease + holder 一致**。
  据此由 **adjudicator 身份**持有 `role=reviewer` 的 lease 完成 apply/close
  （与 C-13 `T-1789340885170-02a8fe9c`、C-14/15 `T-1789340885245-071cb9b4`、W17 先例一致）。
- **执行链（单进程，raw token 不落盘）**：
  1. `lease.acquire --role reviewer`（identity = adjudicator）→ `L-04a6fb12dde9af37`（counter=2）；
  2. `task.apply`（同一 lease，`--role adjudicator`）→ `review` → `applied`，
     `applied_at=1789370911.5248685`；
  3. 复核 `workflow_status=applied_pending_close` / `lifecycle_status=applied`；
  4. `task.close`（同一 lease）→ `applied` → `closed`，`closed_at=1789370922.1573942`；
  5. 复核 `workflow_status=completed` / `next_action=finalize` / `decision=COMPLETE`；
  6. `lease.release`（`L-04a6fb12dde9af37` released）。
- **权威事件链**（`task_events` event_id / monotonic_seq / reason_code）：

  | event_id | 迁移 | reason_code | role |
  |---|---|---|---|
  | 8887 | `in_progress`→`review` | `reported` | implementer |
  | 8889 | `assignment`→`assignment`（reviewer 入队） | `assignment_queued` | reviewer |
  | 8890 | `review`→`review` | `handoff_structured` | reviewer |
  | 8892 | `assignment`→`assignment`（adjudicator 入队） | `assignment_queued` | adjudicator |
  | 8893 | `review`→`applied` | `applied` | adjudicator |
  | 8894 | `applied`→`closed` | `closed` | adjudicator |

- **`action_identities`**（本卡 7 条，全链身份隔离）：
  - executor ×4（`executor-wb-c16-01` / `sess-exec-wb-c16-20260914` / implementer）；
  - reviewer ×1（`task.handoff`，`reviewer-wb-c16-01` / `sess-rev-wb-c16-20260914` / reviewer）；
  - adjudicator ×2（apply + close `state_transition`，`adjudicator-wb-c16-01` /
    `sess-adj-wb-c16-20260914` / adjudicator）。
- **lease 审计链**（`cw lease list`）：

  | lease | counter | role | holder | 事件 |
  |---|---|---|---|---|
  | `L-4b02f58827facc02` | 1 | implementer | executor | acquire→renew→release |
  | `L-b810e86703dbb212` | 1 | reviewer | reviewer | acquire→release |
  | `L-04a6fb12dde9af37` | 2 | reviewer | adjudicator | acquire→release |

  **无 lease 残留**（3 条全部 released）。

## 4. 相邻缺陷与残留（如实披露，不隐瞒）

- **F2 · MCP-015 陈旧断言（转承接卡 C-18）**：`tests/test_mcp_assignment_show_http_rpc.py` 4 例
  （`test_assignment_show_no_match` / `_with_role` / `_unknown_workspace` /
  `_new_client_instance_stable`）编码权威模型前的「静默 none」语义，C-16 后必然转红；**本卡未改**
  （属 MCP-015 `T-1788963088148-495d7208` 产物 + 全 W3 家族共享件）。已登记 backlog §W19。
- **F3 · refresh 脚本脏树打戳**：`scripts/refresh_shared_runtime.ps1:537` 不校验工作树脏否；
  本卡已「先提交、再重跑门禁」自愈。
- **F4 · `_w3_harness.find_daemon_binary()` 假阳性风险**：按 mtime 竞争 + MSYS 路径静默跳过
  （`:589-612`）；本卡新测试自带严格优先级 `_find_daemon_binary()` 不受影响。
- **F1 · §3.4 范围偏差**：`.trim()` 落 step2 白名单外文件，已披露 + step3 重取证 + 独立复跑验证，
  reviewer 判为可接受（**非** BLOCKED）。
- **C-17（卡 D）**：`T-1789365537230-c3f02eb4` **仍 `open`**，不在本卡 scope；
  admin 路由 19 个待盘点 handler 未评估。**本卡不冒充其结论。**

## 5. 结论

任务 `T-1789365537146-bef4c2e4`（C-16 承接：daemon `assignment_show` 走权威 workspace resolver，
打通 CLI 端 `show→create→revoke` 往返）的 4 个 executor step 全部 done，经**独立 reviewer 盲审
PASS**，adjudicator 独立复核 verdict provenance 与全部门禁一致后受保护关闭。

终态：`lifecycle_status=closed` / `workflow_status=completed` / `next_action=finalize` /
`decision=COMPLETE` / `action=NONE` / `verdicts=1`（`V-4c428e7c30851e88a67a9e5f` / pass / findings=4）。

提交前缀 `[T-1789365537146-bef4c2e4]`（本卡自身 task_id，未复用 PYT 卡 `[T-1788871227327-45c94bd8]`
或卡 A `[T-1789340885245-071cb9b4]`）。
