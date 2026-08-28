# T-1787787736395-61aa7408 治理闭环修复证据 v2

## Scope

- `task.create` 在 daemon 原子事务内写入现代 Task Contract、三角色 Role Contract lineage/revision 和 step binding。
- `task_quality_findings` 使用 canonical schema，返回 `evidence` 并提供兼容 `details` 别名；CLI 正确解包 daemon 的 `{task_id, findings}` 响应。
- `task.report` 持久化真实 `request_id` 与 step provenance；`executor_ready_for_review` 强制引用匹配的 report request、step、证据路径和 SHA-256。
- TaskCollabStore 启动路径为既有 v60 数据库幂等补齐 `task_events.request_id/step_id`，不改写历史事件。

## Verification

- `tokenslim run cargo check --manifest-path rust_ext/Cargo.toml`：通过。
- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml task_create_writes_modern_governance_projection_atomically`：通过。
- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml quality_findings_uses_canonical_schema_and_legacy_details_alias`：通过。
- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml executor_handoff_requires_persisted_report_provenance`：通过。
- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml task_event_report_provenance_compat_adds_missing_columns`：通过。
- `scripts/refresh_shared_runtime.ps1 -TaskId T-1787787736395-61aa7408 -Configuration release`：最终部署成功，runtime refresh id `20260827-093510-a6afc28f8fac-82f21509`。
- 修复后 `task create` 返回完整现代合同；`task next-action --workspace-instance-id ws-1` 返回 `READY/CLAIM`，领取后 report 返回真实 request id；report 后 `next-action` 返回 `READY/REVIEW`。
- `python cw.py task findings T-1787787736395-61aa7408`：daemon 返回 `{task_id, findings}` 后 CLI 成功输出 `Findings: 0`，不再触发 `'str' object has no attribute get'`。

## Handoff provenance

- 最新 report request 在 daemon 成功响应后记录；结构化 handoff 必须引用该 request 与本文件 hash。
- 修复前创建的 `T-1787786475332-c46bcc58` 仍保持历史 BLOCKED，未通过 SQL 或覆盖历史记录修补。
