# P0-J 部署与本地 Role Worker 执行证据

**P0-J**：`T-P0J-ROLE-WORKER-IDENTITY`  
**部署治理子任务 P0-J-D**：`T-1787402257549-67ba81e6`  
**记录日期**：2026-08-22  
**秘密处理**：本文件、所有 receipts 和本记录引用的日志均不包含 raw Role Worker credential、lease token 或外部供应商 token。此类值仅保存在当前 Windows 用户的 ACL 受限 `%USERPROFILE%\.callwarden\role-sessions\<handle>\credentials.bin`。

## 结论摘要

P0-J 的 Rust Role Worker domain、schema v60、HTTP client forwarding、staged `identity_policy=role_worker_v1` bootstrap policy 以及 dispatch routes 已通过受控 runtime refresh 部署。新 HTTP authority 的 Role Worker status route 不再返回 `method_not_found`，而是在无参数探测时确定性返回 `E_ROLE_WORKER_AUTH_REQUIRED`；这证明 route 已部署并拒绝未授权请求。

本地 CW worker 身份已经分别为 executor、reviewer 和 adjudicator 建立。它们使用三组不同 `role_worker_id`、instance 与 CSPRNG credential；provider、agent、model 和 session 仅作为无秘密 runtime provenance 写入，不能充当角色授权条件。三个 raw credential 都不在 DB、deliverables 或日志中。

| 项目 | 实际结果 | 证据 |
|---|---|---|
| Refresh TaskId gate | 支持 opaque `T-P0J-ROLE-WORKER-IDENTITY` 和 legacy numeric ID，拒绝路径、空白、option-like、shell-like 输入 | `deploy_governance_task_id_evidence.md`、`deploy_governance_task_id_gate_test.log` |
| 受控刷新 | 第二次 refresh 已构建、替换、重启并通过其 smoke/manifest/hash 流程；第三次 refresh 部署缺失 dispatch route recovery | `%USERPROFILE%\.callwarden\runtime\evidence\20260822-205323-*.json` 及后续 runtime evidence；`p0j_dispatch_recovery_controlled_refresh.log` |
| 新 authority | `cw-daemon.exe` 从 `.callwarden\runtime\current` 启动；HTTP manifest endpoint 为 `http://127.0.0.1:7129`，binary SHA-256 前缀为 `ac626c5f…`，health `worker_status=healthy` | `p0j_postdeploy_readonly_probe_v2.json` |
| Role Worker route | 无 `role_worker_id` 的 status 请求被明确拒绝为 `E_ROLE_WORKER_AUTH_REQUIRED`，取代早先的 `method_not_found` | `p0j_postdeploy_readonly_probe_v2.json` |
| executor worker | `cw-executor-p0j-v1` / `inst-cw-executor-p0j-v1-20260822` 已 active；status 不回显 hash、credential 或 runtime payload | `p0j_executor_role_worker_enrollment_receipt.json`、`p0j_executor_role_worker_status.json` |
| reviewer worker | `cw-reviewer-p0j-v1` / `inst-cw-reviewer-p0j-v1-20260822` 已由 daemon enrollment | `p0j_reviewer_adjudicator_worker_enrollment_receipt.json` |
| adjudicator worker | `cw-adjudicator-p0j-v1` / `inst-cw-adjudicator-p0j-v1-20260822` 已由 daemon enrollment | `p0j_reviewer_adjudicator_worker_enrollment_receipt.json` |
| role-worker bootstrap | P0-J-D 以显式 `identity_policy=role_worker_v1`、独立 reviewer lease 和 adjudicator worker credential 成功补齐缺失 `task_contract_revisions`、三角色 lineage/revision 与 pending step binding | `p0jd_role_worker_contract_bootstrap_receipt.json` |

## 关键异常及保留事实

P0-J-D 最初原子建卡后只具有 legacy `role_contracts`，没有 `task_contract_revisions` projection。该缺口与 CLI-02/CLI-03/MCP-001 的历史问题同型；它已通过新 role-worker-v1 bootstrap 仅对 **P0-J-D 这一张卡** append-only 补齐。没有批量修改 A′ 其他任务卡。

受控 refresh 在用户直接授权后发生于 P0-J-D 的独立 Reviewer PASS 之前。该情况未被隐瞒或回写：executor 已把它作为同一任务内的失败 step 记录，daemon 将追加 remediation step。P0-J-D 因而仍为 `in_progress`，不得作为已合规闭环、不得 apply/close。未来独立 Reviewer 必须复核该执行时序、source diff、受控 refresh receipts、runtime hash 和 post-deploy probes，并由独立 Adjudicator 决定整改处置。

> 这项 remediation 只记录并处理 P0-J-D 的部署顺序问题；它不会撤销已存在的 runtime evidence、worker provenance 或历史事件。

## 未完成与禁止项

P0-J 本身尚未经过独立 Reviewer verdict 或 Adjudicator closure。P0-J-D 也尚有 remediation。尽管 role workers 已 enrollment，CLI-02、CLI-03 和 MCP-001 的 bootstrap 尚未执行；它们必须一个任务一轮，在新 role-worker policy 下完成独立 executor/reviewer/adjudicator workflow 后才可写入。

不得使用历史 `reviewer-wb-186loop` token、不得直接写 SQLite、不得将 provider account/token/model change 作为角色失效依据，也不得因为 runtime 已部署就跳过 P0-J/P0-J-D 的独立审查。

## 推荐独立审查顺序

1. 独立 Reviewer 先检查 P0-J 设计、role_worker domain、schema v60、dispatch recovery、CSPRNG/hash-only persistence、negative tests 和 post-deploy probe。
2. 独立 Reviewer 对 P0-J-D 检查 refresh gate diff、negative input test、first failed release build、second/third controlled refresh receipts，以及“review 前刷新” remediation 的完整 provenance。
3. Adjudicator 只能在 Reviewer PASS 后，使用独立 adjudicator local Role Worker credential 和当前 reviewer lease 作出 apply/close 或退回执行者的决定。
4. 只有 P0-J 的独立治理闭环完成后，才开始 CLI-02，再依序 CLI-03、MCP-001 的单卡 bootstrap/review。

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立审查 P0-J Role Worker 实现、v60 migration、dispatch recovery、受控 deployment receipts、三角色 worker enrollment 与 P0-J-D 的预审查部署 remediation；不得修改源码或任务状态。
  reason: P0-J capability 已部署并通过 HTTP route/worker status 探测；P0-J-D 已 append-only 记录其 review-before-deploy 违例，必须由独立审查决定整改处置。
  independence_requirement: required
```
