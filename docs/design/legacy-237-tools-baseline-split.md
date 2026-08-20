# Legacy 237 Tools Baseline 子任务拆分

## B1：runtime 与工具清单冻结
冻结 Python/Rust/daemon/MCP runtime，生成 237 工具矩阵并标记所有未知路由。
- audit @ docs/design/daemon-rust-migration-ledger.md
- evidence @ .trae-cn/evidence/mcp-tool-matrix-baseline.json
- test @ tests/test_legacy_237_tools_baseline.py

## B2：workspace/snapshot/bootstrap
修复并验证 workspace active、snapshot publish、stats、uncommented、bootstrap_status 和 build 长任务的 legacy 可用入口。
- audit @ server/tools/tools_workspace.py
- fix @ rust_ext/src/daemon/snapshot_state.rs
- test @ tests/test_legacy_workspace_bootstrap.py

## B3：file/symbol/grep/issues/tests
完成 M2.5 并核对五类查询的统一入口、fresh runtime 和结构化拒绝路径。
- implement @ tests/test_query_tests_rpc.py
- test @ tests/test_legacy_query_baseline.py
- evidence @ docs/design/daemon-rust-migration-ledger.md

## B4：task/lease/governance
验证任务、步骤、identity、lease、apply/close 通过 daemon 单写点运行，保护业务错误语义。
- audit @ rust_ext/src/daemon/task_collab.rs
- test @ tests/test_lease_gate_empirical.py
- test @ tests/test_task_close_gate.py

## B5：refresh/semgrep/jobs/index writes
验证 refresh、build、semgrep、长任务和派生索引写入的 timeout、状态和恢复语义。
- audit @ server/daemon_client.py
- test @ tests/test_legacy_write_jobs_baseline.py
- evidence @ docs/design/legacy-237-tools-baseline-plan.md

## B6：剩余工具组与全量验收
完成剩余工具分组核验，生成 237 工具最终矩阵和全量 baseline 报告，交独立 Reviewer。
- test @ tests/test_legacy_237_tools_baseline.py
- evidence @ .trae-cn/evidence/mcp-tool-matrix-baseline.json
- handoff @ docs/design/http-daemon-mvp-task-plan.md
