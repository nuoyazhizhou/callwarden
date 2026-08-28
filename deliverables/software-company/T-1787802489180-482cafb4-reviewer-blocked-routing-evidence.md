# T-1787802489180-482cafb4 验收证据

## 任务与范围

- task_id: `T-1787802489180-482cafb4`
- step_id: `S-1787802489186-4888be1c`
- scope: daemon 原生 `task.handoff` 的 task-level `reviewer_blocked` 路由、CLI thin adapter 的 `step_id=null` 透传，以及对应正负向测试。
- 未修改历史 verdict/evidence；未使用 SQL、直接数据库状态修改或 legacy fallback。

## 实现结果

1. daemon 接受结构化 `reviewer_blocked` 的 `step_id: null`，仅该 outcome 可以使用 task-level handoff；其它 outcome 仍拒绝空步骤。
2. daemon 在同一事务中持久化结构化 handoff、来源 verdict/findings 绑定的 `fix_defect` step，并把任务恢复为 `in_progress`；task-level remediation 不猜测文件/符号范围，由 Executor 在整改步骤中明确。
3. Python CLI 将显式 `--step-id null` 转换为 JSON `null`，不承担业务状态写入或 remediation 逻辑。
4. 已新增 CLI 正负向测试与 Rust daemon 原子路由测试。

## 测试

```text
tokenslim run python -m pytest tests/test_task_handoff_structured.py -q
7 passed

tokenslim run cargo test --manifest-path rust_ext/Cargo.toml reviewer_blocked
4 passed
```

覆盖点包括：CLI task-level null 接受、其它 handoff 的 null 拒绝、daemon task-level block 原子生成 `fix_defect`、任务恢复 `in_progress`、source verdict/findings provenance、以及既有 reviewer-blocked 回归测试。

## Runtime 部署与 round-trip

部署命令：

```text
.\scripts\refresh_shared_runtime.ps1 -TaskId T-1787802489180-482cafb4 -Configuration release
```

权威部署证据：

- path: `C:\Users\wanpi\.callwarden\runtime\evidence\20260827-120232-6df85d8f934e-d80b77b2.json`
- status: `passed`
- runtime_version: `20260827-120232-6df85d8f934e-d80b77b2`
- git_head: `6df85d8f934e62f09ff3c4882faa57d6da165624`
- running daemon: PID `17744`
- daemon executable SHA-256: `26681f821847a4a676eb8d96b55771e422237a2b9bef2d99090401b7d7949a4d`
- expected daemon SHA-256: `26681f821847a4a676eb8d96b55771e422237a2b9bef2d99090401b7d7949a4d`
- ping: exit code `0`, transport `http`, `python_dependency_mode=python_free`

部署后通过当前 daemon 执行：

```text
tokenslim run python cw.py task next-action T-1787802489180-482cafb4 --workspace-instance-id ws-1 --json
```

结果为 `decision=READY`、`action=CLAIM`、`required_role=executor`，并返回本任务步骤 `S-1787802489186-4888be1c`，证明客户端到运行时 daemon 的任务路由可用。task-level reviewer BLOCKED 的完整 mutation 由 Rust 定向测试在临时隔离数据库中验证，未对生产任务伪造 Reviewer 身份或写入测试 verdict。

