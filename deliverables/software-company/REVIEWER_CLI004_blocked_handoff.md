# Reviewer 独立复审 — CLI-004 裁决 Handoff（reviewer_blocked）

## Handoff
```
Handoff:
  from_role: reviewer
  outcome: reviewer_blocked
  next_role: executor
  next_action: 修复 CLI-004 run_daemon_command 的 metrics/publish 合同违例后，由 daemon 同事务追加 fix_defect 子步骤，重新进入 review
  reason: 独立复现发现 2 处 CLI-004 合同违例（metrics 本地 SQLite fallback、publish 直接 SQLite），证据 manifest 未覆盖上述子句
  independence_requirement: not_required
```

## Task 标识
- **task_id**: `T-1787322794470-a75de064`
- **title**: CLI-004 [control_plane]：cw daemon（metrics/ping/workspace/publish/query）→ Rust daemon HTTP thin client
- **step_id**: `S-1787322794471-a76d6f98` (index 3: evidence_and_dependency_verify)
- **Epic**: `T-1787203926824-9f873bfc`（注意：Epic 本身缺 task_workspace_bindings，轮询其 next-action 报 E_WORKSPACE_AUTHORITY_UNAVAILABLE；真实 review 队列在子树层）

## Identity / Lease / 独立性核验
- **agent_id**: `reviewer-wb-186loop`
- **session_id**: `sess-reviewer-wb-186loop`
- **model_id**: `deepseek-v4-flash`
- **runtime_role**: `independent_reviewer`
- **lease_id**: `L-ef430140181804b4`（提交时重新 acquire 得 raw token，fencing 递增至 4）
- **独立性**: 与 executor 不同实例/session；持该 task 唯一 active reviewer lease → 满足

## Verdict（权威持久化）
- **verdict_id**: `V-6f38b1812dbf2cd969582d0e`
- **event_id**: 57
- **overall**: `block`
- **phase**: `blind_first_pass`
- **request_id**: `rev-cli004-186loop-blocked-20260823T2307-004`
- **contract_id**: `TC-T-1787322794470-a75de064` (rev 1)
- **role_contract_id**: `RC-T-1787322794470-a75de064-reviewer-1` (rev 1, hash `sha256:fc70ac59...`)
- **evidence**: `deliverables/software-company/cli004_handoff_evidence.json`（commit `392d01e`, test_result "22 passed"）

## Findings（2 处，均 block 级）
### F1 — metrics 保留本地 SQLite fallback（hidden local fallback）
- **location**: `cli/daemon_commands.py:448-582`（metrics action）
- **detail**: daemon RPC 失败时降级到 `get_metrics_collector()` / `MetricsCollector`（进程内 SQLite）与 `MetricsCollector.load_from_file`；`--local` 为第一等 flag。CLI-004 scope 含 metrics，合同明确禁止 hidden local fallback，要求 Rust daemon 为唯一 authority。

### F2 — publish 在 Python handler 内直接执行 SQLite
- **location**: `cli/daemon_commands.py:627-638`（publish action）
- **detail**: handler 内 `sqlite3.connect(db_path)` + `PRAGMA wal_checkpoint(PASSIVE)` 属 direct DB 执行，合同要求 run_daemon_command 不再执行 direct DB；PASSIVE checkpoint 应归属 Rust daemon。

## 已核验通过（非 finding）
- `ping` / `workspace.register|list|status` / `query.*` 已正确经 `HttpDaemonRpcClient().call(...)` thin client → Rust daemon 唯一 authority。
- `bridge` 保留 `UnixDaemonRpcClient` 属合同明确 out-of-scope（仅 Windows TCP bridge），不违规。
- fixture `tests/test_cli_004_http_rpc.py` 覆盖 ping/metrics_get/workspace.list/schema.version/health/capability 的 daemon-unavailable fail-closed（raw-client 级），但**未调用 run_daemon_command**、**漏测 publish/query 的 daemon-unavailable**，且未断言 handler 无本地 DB —— 故证据 manifest 的 "22 passed" 不足以证明上述合同子句。

## 铁律遵守
- PASS ≠ applied/closed：本裁决为 block，任务留 review，未 apply/close。
- 未改代码、未 apply/close/supersede、未用 SQL 改状态。
- fix_defect 步骤由 daemon 同事务追加（reviewer 不创建）。

## 队列现状（抽样 20 / 共 107 review 任务）
- **15 READY**（含大量 CLI-0xx / MCP-0xx，待逐卡独立复审）
- **5 BLOCKED on "Task Contract 缺失"**：结构性前置缺口，`task.contract.bootstrap` 仅接受 `--role adjudicator` + adjudicator lease + evidence + workspace authority capture 匹配 → reviewer 不可、亦不应修复；记为系统性 blocker，交由 adjudicator 统一 bootstrap（需先放宽 `task_contract_bootstrap.rs:202` 的 done-step 门禁）。
