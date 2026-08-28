# T-1787787736395-61aa7408 治理闭环修复证据

## Scope

- 修复 `task.create` 在 daemon 事务内写入现代 Task Contract、三角色 Role Contract lineage/revision 和 step binding。
- 修复 `task_quality_findings` 对 canonical schema 的读取，并保留 `details` 兼容别名。
- 为 `task.report` 持久化真实 `request_id` 与 step provenance；`executor_ready_for_review` 必须引用匹配的 report request、step、证据路径和 SHA-256。
- 为既有数据库补齐 `task_events.request_id`、`task_events.step_id` 兼容列；不回写历史事件。

## Verification

- `tokenslim run cargo check --manifest-path rust_ext/Cargo.toml`：通过（既有 warning，无 error）。
- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml task_create_writes_modern_governance_projection_atomically`：通过。
- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml quality_findings_uses_canonical_schema_and_legacy_details_alias`：通过。
- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml executor_handoff_requires_persisted_report_provenance`：通过。
- `scripts/refresh_shared_runtime.ps1 -TaskId T-1787786475332-c46bcc58 -Configuration release`：部署成功，runtime refresh id `20260827-073520-a6afc28f8fac-4ffd5007`。
- 修复后通过 `python cw.py task create` 创建本任务；随后 `python cw.py task next-action T-1787787736395-61aa7408 --workspace-instance-id ws-1 --json` 返回 `READY/CLAIM`，且返回完整 task/role contract hash。

## Authority notes

- `T-1787786475332-c46bcc58` 是旧 daemon 创建的缺少现代合同投影的历史记录，保持 BLOCKED，未用 SQL 或历史覆盖方式修补。
- 本证据文件绑定当前任务 report；commit hash 在提交后追加到任务 ledger，不改变本文件的测试事实。
