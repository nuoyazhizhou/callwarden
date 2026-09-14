# A″-G0 Adjudicator 收尾交接（HTTP-only，无直接 SQLite/CAS）

> 固定角色合同：`deliverables/software-company/aprime2_role_contracts/adjudicator_g0_v1.md`
> 任务：T-1787800241077-e7fd7231（A″-G0）
> 审定的 reviewer verdict：V-ab6cf197d2f01283baea19eb（blind_first_pass，PASS，findings=0）
> 绑定 step：S-1787800317700-b1dfcdfc（step3 prepare_independent_review）
> 共享 snapshot：02cf30ebfce924b0

本文件是 adjudicator 对 A″-G0 独立复核并执行 `apply → close` 的只读收尾记录。Adjudicator 身份：
`adjudicator-wb-rp01-01` / `inst-adjudicator-wb-rp01-01` / 独立 role_session
（与 executor、reviewer 的 role_session 分离）。本流程未触碰直接 SQLite/CAS、
未改动 production source/runtime/deployment/matrix/contract，也未创建或领取
任何 A″-01…A″-37 implementation microtask。

## 一、HTTP 只读投影核验的 verdict provenance

1. `task.governance_projection.get`：
   - `identity_policy = "role_worker_v1"`（declared）；
   - `task_contract` = TC-T-1787800241077-e7fd7231 rev 2，
     hash `sha256:5c08d275f2f9971f351776fc3adda2ee6899263afe5914abc76f8db10d82021b`；
   - `reviewer_role_contract` = RC-T-1787800241077-e7fd7231-reviewer-1 rev 1，
     prompt_template `cw.aprime2.g0.reviewer.v1`，prompt_hash
     `40c770e2a7ac2e9679888aace130405bf284225d2c10dbecdaf2b47059029545`
     （= 磁盘 E6a `independent_reviewer_g0_v1.md` 字节 sha256，可复核）；
   - `verdicts` 最新一条 = V-ab6cf197d2f01283baea19eb，overall=pass，
     normalized `verdict-normalization/v1`，submitted_at=1788857211；
   - `review.state = passed`、`review.verdict_id = V-ab6cf197…`、`findings_count = 0`；
   - `review_input_snapshot.snapshot_id = 02cf30ebfce924b0`、
     evidence_path = `g0_independent_reviewer_handoff.md`；
   - governance：`review / adjudication_pending / READY / ADJUDICATE`，
     required_role=adjudicator，blocking_conditions=[]。
2. Reviewer verdict 来自独立 reviewer worker（reviewer 固定角色合同 rev1 绑定），
   其 role_session 与 executor/adjudicator 分离（本轮 .rw_creds 三组互异）。
3. `task.events` reported evidence_hash（每 step 主证据）规范化为 `sha256:<hex>`
   且 ⊆ 磁盘证据文件字节 sha256；四份主证据（E0 json 2008c5cc…、
   E2 audit 6e23a3ac…、E3 map 96831cb0…、handoff a9251532…）逐一命中。
4. reviewer 的 lease/fencing：reviewer 以其独立 role lease + fencing 提交 verdict，
   daemon 侧 `verdict.submit` 强校验 reviewer lease token + fencing counter（fail-closed）；
   adjudicator 本轮另获自己的 adjudicator lease 与 fencing（详见下）。

## 二、合同六项逐项核验结论

1. **TC identity_policy**：TC rev 2 显式 `identity_policy=role_worker_v1`；
   executor/reviewer/adjudicator 三者 role contract 的 prompt_template/prompt_hash
   均可经 HTTP projection + 磁盘复核 → 满足。
2. **Reviewer 来自不同 stable worker + evidence 对应真实 G0 inventory**：
   reviewer/adjudicator role_session 分离；evidence_hash 均对应实际磁盘
   E0–E6/handoff 文件 → 满足。
3. **Reviewer lease/fencing + adjudicator 自身 lease/fencing**：verdict 经由
   daemon 权威路径（reviewer role lease + fencing）落账；adjudicator 以
   `role=adjudicator` 获取独立 lease 执行收尾 → 满足。
4. **162 全量 / 34 candidate 唯一 disposition / 128 local-core 不误纳入 HTTP migration**：
   由 reviewer PASS 复核 + E0 全量清单冻结（retain_local_core 133 /
   replace_with_http_client 16 / requires_separate_authority_contract 7 /
   retire_after_zero_callers 5 / requires_artifact_contract 1 = 162，含 128 local-core
   全 retain）→ 满足；adjudicator 不做二次生产改动。
5. **零 production write / blockers 照实**：G0 四步全只读静态证据；release blockers
   现场复核：P0-K closed、A′ closed、root/route in_progress 三项 met；
   old S3 open、matrix python_compat=0 未复跑、runtime convergence 未捕获、
   G0 applied pending 四项 blocked/未证 → 满足。
6. **next_action / status machine**：governance `READY / ADJUDICATE`、
   required_role=adjudicator、blocking_conditions=[]，允许 apply/close；
   无 stale fencing / authority / manifest mismatch → 满足。

## 三、ACCEPT 后动作（唯一允许范围）

- adjudicator lease.acquire（独立 session）→ `task.handoff`
  `from_role=adjudicator, outcome=adjudicator_accepted`；
- 以同一 holder 取得 reviewer-role lease（Rust `task.apply`/`task.close` 要求
  reviewer lease 凭证）→ `task.apply` → `task.close`；
- release 全部 lease；
- 只读终态 `task.status` = closed。

> **范围声明**：`accepted_and_closed` 只表示 A″-G0 inventory Gate 已闭环。
> 它**不**授权任何 A″-01…A″-37 实施。任何 A″ implementation card 仍须在
> A′ closed、matrix `python_compat=0`、live/runtime convergence、old S3
> independent disposition、以及各自 artifact-specific G1 requirement 重新
> 验证通过后，才可由合法 planner 逐张创建。
