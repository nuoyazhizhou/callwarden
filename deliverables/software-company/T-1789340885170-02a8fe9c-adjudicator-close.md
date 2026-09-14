# C-13 承接卡 adjudicator 关闭证据：T-1789340885170-02a8fe9c

- 结论：Adjudicator 接受独立 reviewer PASS verdict，apply + close 收口任务。
- Reviewer verdict：`V-76f8df41df79cc908c9d7875`（blind_first_pass / pass / findings=0）
- 绑定 step：`S-1789340885182-035c9600`（step_index 4，最后一步 `release_verify`）
- Snapshot：`a87ae6c43b7bb6cd`；view_manifest_hash：
  `7a87c1f1864b16986c5c43e964dad15d51c3043077224ee0421e7d1d130c1784`；workspace：1
- Task Contract：`TC-T-1789340885170-02a8fe9c` r1 `sha256:152d3c90…9487`
- 证据 manifest：`deliverables/software-company/T-1789340885170-02a8fe9c-evidence.md`
  `sha256:390ca6b4ae773e8b9d3709dfc002dc8c719343f1dc3e5d93952aa3128c049b28`
- Reviewer 独立复核报告：`deliverables/software-company/T-1789340885170-02a8fe9c-reviewer-review.md`
  `sha256:F3090DDB200420A3DCE016F6A295D512007DB9969CF29D3BFC68E1A43EFFE489`

## 1. verdict provenance 校验（adjudicator 独立只读复核）

- `task_verdict_events` 该行：phase=`blind_first_pass`、overall=`pass`、
  step_id=`S-1789340885182-035c9600`、snapshot_id=`a87ae6c43b7bb6cd`、
  view_manifest_hash=`7a87c1f1…c1784`、workspace_id=1，
  reviewer identity = `reviewer-c13-wb-01` / `sess-review-c13-20260914` / `workbuddy`（role=reviewer）。
- 归一化：`normalization_version=verdict-normalization/v1`，
  hash `sha256:b41cbdb3…b0e8d`，`revoked=False`；
  `pass` 经 `normalize_overall` 兜底映射保持 `pass`（非 UNVERIFIED）。
- reviewer 盲审内容：不采信 executor 报告，独立实跑 `cargo build` 零 error、
  `scripts/verify_route_matrix.py` 门禁全绿（243 方法一致）、
  `tests/test_c13_semgrep_dispatch_wiring.py` 9 passed，并逐行复核
  `mod.rs` 模块声明、4 个 dispatch arm 与 CLI `_METHOD_MAP` 逐字一致；
  `forbidden_paths` 未触碰。
- 证据文件哈希在关闭时点复核，与 report / handoff 记录逐字一致：
  manifest `390CA6B4…49B28`、reviewer review `F3090DDB…FE489`（均未改动）。

## 2. apply/close 依据

- `task.apply` / `task.close` 的保护门禁均为
  `validate_lease_for_mutation(task_id, role="reviewer", token, counter, identity)`
  （`task_collab_lifecycle_apply.rs:36-44` / `124-132`），即**要求 reviewer lease 凭证 + holder 一致**；
  此处不使用 `validate_reviewer_lease_for_adjudication`（该函数只用于
  `task.claim.recover` 等跨角色恢复面）。据此，本卡由**adjudicator 身份**持有
  role=`reviewer` 的 lease 完成 apply/close（与 W17 卡 `T-1789301330757-87f33c34` 先例一致）。
- **诚实披露（lease 取得方式）**：reviewer 盲审阶段的 lease `L-526f03de5b10db00`
  （counter=1）的 raw token 按设计**仅在 acquire 响应返回一次、未落盘**，关闭阶段已不可再用；
  其 holder `reviewer-c13-wb-01` 不在 `agent_registrations` 中（该身份未走 `agent.register`），
  故该 lease 命中 daemon 权威 **orphan 回收**条件（`holder_registration_missing`）。
  处理方式：以 adjudicator 身份执行 `lease.acquire --role reviewer`，daemon 在同一事务内
  将旧 lease 置 `expired` 并追加 `expire` 审计事件（`task_lease_events` id=5836 为原 acquire，
  回收事件同事务追加），随后签发新 reviewer lease `L-e762fb30d9a426b9`（counter=2，递增）。
  **未绕过门禁、未伪造 token、未直接写库**。
- 执行链：
  1. `lease.acquire --role reviewer`（identity = adjudicator，counter=2）→ `L-e762fb30d9a426b9`；
  2. `task.apply`（`lease-token`=该 lease raw token，`fencing-counter=2`）→ `review` → `applied`，
     `applied_at=1789352822.8872607`；
  3. 复核 `workflow_status` 转 `applied`；
  4. `task.close`（同一 lease）→ `applied` → `closed`，`closed_at=1789352837.3268495`；
     叶子步骤门禁满足（5 step 全 done）；
  5. `lease.release`（reviewer `L-e762fb30d9a426b9` 与 adjudicator `L-b333d7766b2e20d2` 均 released）。
- 权威事件链（`task_events`，event_id / monotonic_seq）：`handoff_structured`
  （8840 / 1786265402985，reviewer→adjudicator）→ `applied`（8843 / 1786265402988）
  → `closed`（8844 / 1786265402989）。
- `action_identities`：apply / close 两条 `state_transition` 均记录
  `adjudicator-c13-wb-01` / `sess-adj-c13-20260914` / `workbuddy` / role `adjudicator`。

## 3. 残余与如实披露（不隐瞒）

- **C-07 端到端「后」回执仍未取得**：运行中的共享 daemon 二进制早于本卡修复，
  且 Windows 管道名由用户 SID 派生、无法并起隔离实例；重建共享 runtime 属本卡
  `forbidden_paths`（证据 §4.2 已披露）。本卡提供的是「前」真实回执 + 进程内
  dispatch 行为级可达性证明，未以该证明冒充端到端已完成。
- **`change_audit` 归属限制**：`dispatch.rs`（+13）在卡级 `allowed_paths` 内但不在任何
  step 的 `target_file`，`tests/`、`rust_ext` 为目录级 scope，故三处改动未入 step `changes`；
  以证据 §1/§5 + 工作树真实 diff + 本次提交留痕替代（未伪造目录级 `file_path`）。
- **lease 残留**：implementer lease `L-9067737a5aea3b48`（`executor-pytreg-01`，counter=1）
  在 `task_leases` 中 status 仍为 `active` 但 `expires_at=1789344660` 已过期；
  因任务已终态、不影响门禁，未做额外清理（daemon 未提供非 holder 的强制释放入口）。

## 4. 结论

任务 `T-1789340885170-02a8fe9c`（C-13：`semgrep_handlers` 编译接线与 semgrep RPC route 落地）
的 5 个 executor step 全部 done 并经独立 reviewer 盲审通过，adjudicator 独立复核一致后关闭。

终态：`lifecycle_status=closed` / `workflow_status=completed` / `decision=COMPLETE` /
`next_action=finalize` / `review=not_in_review` / `verdicts=1` /
`blocking_conditions=[]`。

提交：`1afabc8`（`[T-1789340885170-02a8fe9c]` 前缀，未复用 PYT 卡 `T-1788871227327-45c94bd8` id）。

闭环后解锁：**C-07 端到端**（`cw semgrep scan`）的 daemon 侧阻断已移除，
剩余端到端回执待共享 runtime 重建后补。
