# C-14..C-15 承接卡 adjudicator 关闭证据：T-1789340885245-071cb9b4

- 结论：Adjudicator 独立复核后接受 reviewer PASS verdict，apply + close 收口任务。
- Reviewer verdict：`V-da5ad4a015f77515dc890d76`（blind_first_pass / pass / findings=0，event_id 632）
- 绑定 step：`S-1789340885248-0754adec`（step_index 4，最后一步 `release_verify`）
- Snapshot：`dfcac6f16b827a30`；view_manifest_hash：
  `86ad4321954e9999e0cebe58962bf777edd7fbd85b5c0af03a9263ad114ba0c5`；workspace：1
- Task Contract：`TC-T-1789340885245-071cb9b4` r1
  `sha256:8050e0816d872eac4a1c3dc8e7bd8870460adb11bd7edb6e2f2ba4f9072ba500`
- Role Contract（reviewer）：`rcl-T-1789340885245-071cb9b4-reviewer` r1
  `sha256:3f691024970453f94a17d7ddc7a5845db2eaf19f1d25af9b8c9000272a6f8863`
- 证据 manifest：`deliverables/software-company/T-1789340885245-071cb9b4-evidence.md`
  `SHA256:2BF673EB8227A28FBCD9BE4744E2CF5BB0A60831A5AF3C9C13F04478972B6ED8`
- Reviewer 独立复核报告：`deliverables/software-company/T-1789340885245-071cb9b4-reviewer-review.md`
  `SHA256:7BB7EC99FF2BE1D3FE9B9EB0DD8A6FF733003E9D87610D2556FE3915F0CB7C75`
- step0 契约裁决：`deliverables/software-company/T-1789340885245-071cb9b4-contract-adjudication.md`
  `SHA256:6BBECF9B9949302886FA94C9DDCC6F444B7D31B9DC6B0747F284CD26A6E5D18A`

## 1. verdict provenance 校验（adjudicator 独立只读复核）

- `task_verdict_events` id=632 该行：phase=`blind_first_pass`、overall=`pass`、
  `clause_results=[]`、`findings=[]`、`amendment_ref=''`（盲审阶段未引用 sealed verdict）、
  step_id=`S-1789340885248-0754adec`、snapshot_id=`dfcac6f16b827a30`、
  view_manifest_hash=`86ad4321…ba0c5`、workspace_id=1；
  `reviewer_identity` 内嵌 request_id=`rev-c14c15-wb-blind-pass-7f21b3c9`、
  identity=`reviewer-c14c15-wb-01` / `inst-review-c14c15-20260914` /
  `sess-review-c14c15-20260914` / `workbuddy` / role=`reviewer`，
  role_contract=`rcl-T-1789340885245-071cb9b4-reviewer` r1 同 hash。
- 归一化：`normalization_version=verdict-normalization/v1`，
  rules hash `sha256:b41cbdb3…b0e8d`；`canonicalization_version=role-contract-c14n/v1`。
- `task_events` id=8861 `handoff_structured`（来自 reviewer）：
  `outcome=reviewer_pass`、`from_role=reviewer` → `next_role=adjudicator`、
  `next_action=apply`、`independence_requirement=required`、
  `source_verdict_id=V-da5ad4a015f77515dc890d76`、
  `evidence_path/hash` 与上表 reviewer 回执逐字一致、`fencing_counter=2`。
- 关闭时点复核：manifest 与 reviewer 回执两个文件**自 handoff 起未再改动**，
  哈希与 handoff / step#4 report 记录逐字一致（未在多轮复核中漂移）。
- Reviewer 盲审口径（不采信 executor 报告）：独立重放 `cargo build` 零 error、
  `pytest tests/test_c14_c15_assignment_contract.py tests/test_cli_011_http_rpc.py -q`
  19 passed、`scripts/verify_route_matrix.py` 门禁全绿、五面契约逐面只读对照，并
  独立确认规则 43 部署门禁三方哈希一致后，对**已部署 daemon** 重放
  `create → show → revoke → show → replay` 五步往返全部符合契约。

## 2. apply/close 依据

- `task.apply` / `task.close` 的保护门禁均为
  `validate_lease_for_mutation(task_id, role="reviewer", token, counter, identity)`
  （`task_collab_lifecycle_apply.rs:36-44` / `:124-132`），即**要求 reviewer lease 凭证 +
  holder 一致**。据此，本卡由**adjudicator 身份**持有 role=`reviewer` 的 lease 完成
  apply/close（与 C-13 卡 `T-1789340885170-02a8fe9c`、W17 卡 `T-1789301330757-87f33c34` 先例一致）。
- **诚实披露（lease 生命周期，含一次无效尝试）**：
  1. reviewer 盲审阶段首租 `L-e80c89ca5ead72f0`（counter=1）raw token 仅进程内使用、
     未落盘，会话中断后不可再用；其 holder `reviewer-c14c15-wb-01` 未走 `agent.register`，
     故被 daemon 权威 **orphan 回收**（`holder_registration_missing`，status=`expired`）。
  2. 同一 reviewer 身份重新 acquire 得 `L-1b6fed9867d7feaf`（counter=2），
     同进程内完成 `verdict.submit` 与 `task.handoff`，随后 `lease.release`（released）。
  3. adjudicator 首次 acquire 得 `L-a6b3fbefff607c5e`（counter=3），但
     `cw task apply --json` 因该子命令无 `--json` 参数被 argparse 拒绝（RC=2，**未产生任何写入**），
     该 lease 随即 release。
  4. adjudicator 重新 acquire 得 `L-fdd705414242b5e8`（counter=4），完成
     `apply → close → release`（released）。
  **未绕过门禁、未伪造 token、未直接写库**。
- 执行链（单进程，token 不落盘）：
  1. `lease.acquire --role reviewer`（identity = adjudicator，counter=4）→ `L-fdd705414242b5e8`；
  2. `task.apply`（token=该 lease raw token，`fencing-counter=4`）→ `review` → `applied`，
     `applied_at=1789362829.3678992`；
  3. 复核 `workflow_status=applied_pending_close` / `lifecycle_status=applied`；
  4. `task.close`（同一 lease）→ `applied` → `closed`，`closed_at=1789362831.0826275`；
  5. 复核 `workflow_status=completed` / `lifecycle_status=closed` / `next_action=finalize`；
  6. `lease.release`（reviewer `L-fdd705414242b5e8` released）。
- 权威事件链（`task_events`，event_id / monotonic_seq）：
  `handoff_structured`（8861 / 1786265403011，reviewer→adjudicator）→
  `applied`（8864 / 1786265403014）→ `closed`（8865 / 1786265403015）。
- `action_identities`：apply / close 两条 `state_transition` 均记录
  `adjudicator-c14c15-wb-01` / `sess-adj-c14c15-20260914` / `workbuddy` / role `adjudicator`。

## 3. 残余与如实披露（不隐瞒）

- **C-16 出界未修**：`assignment_show` 的 positive 分支对生产调用方仍不可达
  （CLI/MCP 不给数值 `workspace_id`），根因落在 `task_collab_lease.rs` 与
  `server/daemon_client.py`，均不在本卡 `executor_allowed`；已在
  `pyt_regression_step4_handoff_backlog.md` §W15 登记另建承接卡。
- **C-17 剩余暴露面**：`snapshot_state.rs` admin 路由块对其余 18 个 admin handler 存在
  同类「registry 代理 id 当 task DB workspace_id」的命名空间风险，本卡仅收口 assignment
  两处，其余**未改、未评估**（evidence §4.2 / backlog §W14 已登记，另卡承接）。
- **未新增隔离 daemon live pytest**：共享夹具 `_pick_bin()` 优先解析
  `rust_ext/target/release/cw-daemon.exe`（早于本卡修复），直接复用会得到环境失真的失败；
  改动共享夹具超出 `executor_allowed`。本卡口径为「源码文本确定性回归 19 例 + 规则 43
  部署门禁哈希一致 + 对已部署 daemon 的独立往返重放」，详见 evidence §4.5。
- **`cargo test` 未全量重跑**：全量耗时约 21 分钟且存在 6 例**环境既有失败**
  （全在 `src/cli/` router/runtime/refresh，与本卡模块无关；executor 以 `git stash`
  回落 HEAD 基线对照同样失败），reviewer 以同口径子集独立复现 34 passed / 6 failed，
  判定非本卡引入。
- **lease 残留**：implementer lease `L-fdb1065d0d53dec9`（`executor-pytreg-01`，counter=2）
  在 `task_leases` 中 status 仍为 `active` 但 `expires_at=1789361295` 已过期；因任务已终态、
  不影响门禁，未做额外清理（daemon 未提供非 holder 的强制释放入口）。与 C-13 先例一致。

## 4. 结论

任务 `T-1789340885245-071cb9b4`（C-14 + C-15 + 卡内实测新发现 C-17：daemon assignment
create/revoke 契约一致性修复，assignment_id 单源）的 5 个 executor step 全部 done 并经
独立 reviewer 盲审通过，adjudicator 独立复核 verdict provenance 一致后受保护关闭。

终态：`lifecycle_status=closed` / `workflow_status=completed` / `next_action=finalize` /
`verdicts=1`（`V-da5ad4a015f77515dc890d76` / pass / findings=0）。

提交：`[T-1789340885245-071cb9b4]` 前缀（未复用 PYT 卡 `T-1788871227327-45c94bd8` id）。
